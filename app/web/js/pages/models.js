import { html, render, nothing, repeat, live } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtAgo, fmtDateTime, spaceMixed } from '../core/format.js';
import { channelLabel } from '../core/channels.js';
import { toast, toastError } from '../ui/toast.js';
import { pageHeader, statCard, chip, emptyState, loadErrorPage, loadErrorInline,
         skeletonStats, skeletonRows, switchControl, badge, busyClick } from '../ui/widgets.js';
import { CAPABILITIES, LONG_CONTEXT_TOKENS, accountAvailableCount, accountCanUse,
         aliasBadge, aliasEntries, capabilityBadges, capabilityLabel, contextText, contextTitle,
         coverageFor, creditInfo, fmtTokens, hasCredits, hasExcluded, hiddenExcludedCount,
         outputText, panelModelsUrl, realmAccounts, reasoningLevels, selectModels,
         sourceLabel, summary, withAlias, withEnabled, withoutAlias } from './models/logic.js';
import { confirmDialog } from '../ui/dialog.js';
import { loadAccounts } from './accounts/actions.js';

// 模型页：目录来自 GET /panel/models，每次勾选只把这一项写进当前渠道的关闭名单；目录按 5 分钟缓存，只在手动点击时重新读取。
// 账号选择器把所有账号聚合成一列覆盖率，也可以只看单个账号能用的模型。别名表来自 GET /settings 的 model_aliases，所有渠道共用。

export function mount(host, ctx) {
  const realm = getState().viewRealm;
  const state = {
    data: null,          // null 表示首屏还没读到数据
    accounts: [],
    scope: 'all',        // all = 聚合视图；其他值是账号 uid
    source: '',
    fetchedAt: null,
    error: '',
    inline: '',          // 刷新失败的原因，保留已有数据
    query: '',
    capability: 'all',
    sort: 'catalog',
    showExcluded: false,
    pending: new Set(),  // 正在保存勾选状态的模型 id
    aliases: {},
    aliasSource: '',
    aliasTarget: '',
    aliasBusy: false,
  };
  let saveQueue = Promise.resolve();

  async function load(force) {
    const url = panelModelsUrl(realm, force);
    try {
      const [reply, accountsReply, settings] = await Promise.all([
        getJSON(url),
        loadAccounts().catch(() => null),
        getJSON('/settings'),
      ]);
      state.data = reply.data || [];
      state.aliases = settings.model_aliases || {};
      if (accountsReply) state.accounts = accountsReply.accounts || [];
      if (state.scope !== 'all' && !currentAccount()) state.scope = 'all';
      state.source = reply.source || '';
      state.fetchedAt = reply.fetched_at || null;
      state.error = '';
      state.inline = '';
      if (force) {
        if (reply.refresh_ok) toast.success('已从服务器重新获取模型目录');
        else toast.warn('服务器没有返回新的目录', reply.refresh_msg || '仍在显示上次获取的结果');
      }
    } catch (err) {
      if (state.data === null) state.error = err.message;
      else state.inline = '刷新模型目录失败：' + err.message;
      if (force) toastError('重新获取失败', err);
    }
    draw();
  }

  function realmAccountList() {
    return realmAccounts(state.accounts, realm);
  }

  function currentAccount() {
    if (state.scope === 'all') return null;
    return realmAccountList().find((a) => a.uid === state.scope) || null;
  }

  const refresh = () => load(true);

  async function toggleModel(id, enabled) {
    if (state.pending.has(id)) return;
    state.data = withEnabled(state.data, id, enabled);
    state.pending.add(id);
    draw();
    // 保存按点击顺序排队，界面上的勾选状态与服务端写入顺序一致。
    // 每次只提交这一项，不会用本页清单覆盖其他渠道的关闭名单。
    const job = saveQueue.then(async () => {
      let reply = null;
      try {
        reply = await postJSON('/settings/save', { model_enabled: { realm, model: id, enabled } });
        if (enabled) toast.success('已开始处理 ' + id);
        else toast.warn('已停止处理 ' + id);
      } catch (err) {
        state.data = withEnabled(state.data, id, !enabled);
        toastError('保存模型处理状态失败', err);
      } finally {
        state.pending.delete(id);
        // 以服务端整理后的清单为准；同时还有别的勾选在保存时不覆盖它，等那一次的结果
        if (reply && reply.model_enabled && !state.pending.size) {
          const disabled = new Set((reply.model_enabled.disabled_models || []).map((v) => String(v).toLowerCase()));
          state.data = state.data.map((m) => ({ ...m, enabled: !disabled.has(m.id.toLowerCase()) }));
        }
        draw();
      }
    });
    saveQueue = job.catch(() => {});
    return job;
  }

  async function saveAliases(next, done) {
    state.aliasBusy = true;
    draw();
    try {
      const reply = await postJSON('/settings/save', { model_aliases: next });
      state.aliases = reply.model_aliases || {};
      done();
      // 别名改变了模型表里的通告角标，重新读取一次目录
      await load(false);
    } catch (err) {
      toastError('保存模型别名失败', err);
    } finally {
      state.aliasBusy = false;
      draw();
    }
  }

  function addAlias(event) {
    event.preventDefault();
    const source = state.aliasSource.trim();
    const target = state.aliasTarget.trim();
    if (!source || !target || state.aliasBusy) return;
    saveAliases(withAlias(state.aliases, source, target), () => {
      state.aliasSource = '';
      state.aliasTarget = '';
      toast.success(`已把 ${source} 通告为 ${target}`);
    });
  }

  function removeAlias(entry) {
    confirmDialog({
      title: '删除模型别名',
      desc: `删除后，${entry.source} 恢复用原 id 通告。`,
      confirmText: '删除',
      danger: true,
      onConfirm: () => saveAliases(withoutAlias(state.aliases, entry.source),
                                   () => toast.success('已删除别名 ' + entry.source)),
    });
  }

  function aliasCard() {
    const rows = aliasEntries(state.aliases);
    const ready = state.aliasSource.trim() && state.aliasTarget.trim() && !state.aliasBusy;
    return html`
      <div class="card models-alias">
        <div class="card-head">
          <div class="grow">
            <h2 class="card-title">${icon('Link')}模型别名</h2>
            <div class="card-sub">把不同渠道里的同一个模型合并成一个通告 id，所有渠道共用这张表。</div>
          </div>
        </div>
        ${rows.length ? html`
          <div class="alias-list">
            ${rows.map((entry) => html`
              <div class="alias-row">
                <span class="cell-mono break-all">${entry.source}</span>
                ${icon('ArrowRight')}
                <span class="cell-mono strong break-all">${entry.target}</span>
                <button class="btn btn-ghost btn-icon btn-xs tone-danger" title=${'删除别名 ' + entry.source}
                        aria-label="删除别名" ?disabled=${state.aliasBusy}
                        @click=${() => removeAlias(entry)}>${icon('Trash2')}</button>
              </div>`)}
          </div>` : html`<div class="text-xs muted">还没有别名。</div>`}
        <form class="alias-form" @submit=${addAlias}>
          <input class="input input-sm" list="alias-model-ids" aria-label="源模型 id"
                 placeholder="源模型 id，例如 deepseek-v4.1-flash-1017"
                 .value=${live(state.aliasSource)}
                 @input=${(e) => { state.aliasSource = e.target.value; draw(); }}>
          ${icon('ArrowRight')}
          <input class="input input-sm" aria-label="通告为"
                 placeholder="通告为，例如 deepseek-v4.1-flash"
                 .value=${live(state.aliasTarget)}
                 @input=${(e) => { state.aliasTarget = e.target.value; draw(); }}>
          <button class="btn btn-primary btn-sm" type="submit" ?disabled=${!ready}>${icon('Plus')}添加</button>
          <datalist id="alias-model-ids">
            ${(state.data || []).map((m) => html`<option value=${m.id}></option>`)}
          </datalist>
        </form>
      </div>`;
  }

  function aliasTag(m) {
    const info = aliasBadge(m);
    return info ? badge(info.text, info.tone, { small: true, title: info.title }) : nothing;
  }

  function sourceLine() {
    const parts = ['数据来源：' + sourceLabel(state.source)];
    if (state.fetchedAt) parts.push(`获取时间：${fmtDateTime(state.fetchedAt)}，${fmtAgo(state.fetchedAt)}`);
    return html`<div class="text-2xs muted">${parts.join(' · ')}</div>`;
  }

  function staleNotice() {
    if (state.source === 'server' || state.source === 'cache') return nothing;
    return html`<div class="notice notice-warn" role="status">${icon('TriangleAlert')}
      <div class="grow">没有从上游取到模型目录，当前显示的是${sourceLabel(state.source)}。</div></div>`;
  }

  function stats() {
    const s = summary(state.data);
    const account = currentAccount();
    const available = account ? accountAvailableCount(state.data, account) : s.enabled;
    const maxOutHint = s.maxOutput ? `单次最多输出 ${fmtTokens(s.maxOutput)}` : nothing;
    return html`
      <div class="grid-stats cols-4">
        ${statCard(account
          ? { label: '该账号可用模型', value: available, icon: 'Boxes',
              hint: `共 ${s.total} 个 · 网关处理中 ${s.enabled} 个`, delay: 0 }
          : { label: '可用模型', value: s.total, icon: 'Boxes',
              hint: `网关处理中 ${s.enabled} 个`, hintTone: s.enabled < s.total ? 'warning' : '', delay: 0 })}
        ${statCard({ label: '支持推理', value: s.reasoning, icon: 'Brain', hint: '目录里带思考档位的模型', delay: 0.05 })}
        ${statCard({ label: '长上下文模型', value: s.longContext, icon: 'Maximize2', hint: `上下文不少于 ${fmtTokens(LONG_CONTEXT_TOKENS)} 的模型`, delay: 0.1 })}
        ${statCard({ label: '最长上下文', value: fmtTokens(s.maxContext), icon: 'Gauge', hint: maxOutHint, delay: 0.15 })}
      </div>`;
  }

  function filterCard() {
    const hidden = hiddenExcludedCount(state.data);
    const excluded = hasExcluded(state.data);
    const creditSort = hasCredits(state.data);
    return html`
      <div class="card models-filter">
        <label class="row row-tight">
          <span class="chips-label">${icon('Users')}查看</span>
          <select class="select input-sm" title="全部账号是聚合视图，显示每个模型的账号覆盖率"
                  .value=${live(state.scope)}
                  @change=${(event) => { state.scope = event.target.value; draw(); }}>
            <option value="all" .selected=${state.scope === 'all'}>全部账号（聚合）</option>
            ${realmAccountList().map((account) => html`
              <option value=${account.uid} .selected=${state.scope === account.uid}>
                ${account.nickname || String(account.uid || '').slice(0, 8)}${account.enabled === false ? '（已停用）' : ''}
              </option>`)}
          </select>
        </label>
        <label class="input-search models-search">
          ${icon('Search')}
          <input class="input" type="search" placeholder="搜索模型名 / 模型 ID / 说明"
                 .value=${live(state.query)}
                 @input=${(e) => { state.query = e.target.value; draw(); }}>
        </label>
        <div class="chips">
          <span class="chips-label">${icon('Shapes')}能力</span>
          ${CAPABILITIES.map((c) => chip(c.label, state.capability === c.id, () => {
            state.capability = c.id;
            draw();
          }))}
          ${creditSort ? html`
            <span class="chips-sep"></span>
            ${chip('倍率从低到高', state.sort === 'credit', () => {
              state.sort = state.sort === 'credit' ? 'catalog' : 'credit';
              draw();
            })}
            ${chip('上下文从大到小', state.sort === 'context', () => {
              state.sort = state.sort === 'context' ? 'catalog' : 'context';
              draw();
            })}` : nothing}
        </div>
        ${excluded ? html`
          <div class="models-filter-tail">
            <span class="text-2xs muted">${hidden ? `${hidden} 个被排除的模型默认不显示` : ''}</span>
            ${switchControl({
              checked: state.showExcluded,
              label: '显示被排除的模型',
              onChange: (value) => { state.showExcluded = value; draw(); },
            })}
            <span class="text-xs">显示被排除的模型</span>
          </div>` : nothing}
      </div>`;
  }

  function creditCell(m, account = null) {
    const info = creditInfo(m, realm, account);
    if (!info) return html`<span class="muted">—</span>`;
    const title = info.free
      ? (m.benefit === true ? '上游目录把这个模型标为福利模型，按免费计费' : '按免费计费')
      : '';
    return html`
      <span class="row row-tight">
        ${info.free ? badge(info.label, 'success', { title }) : badge(info.label, 'muted')}
        ${info.note ? html`<span class="text-3xs tone-violet nowrap">${info.note}</span>` : nothing}
      </span>`;
  }

  function modelCell(m) {
    return html`
      <div class="model-cell">
        <div class="row-wrap" style="gap:6px">
          <span class="strong">${m.name || m.id}</span>
          ${m.excluded ? badge('被排除', 'warning', { small: true, title: '筛选规则排除了它，它不会出现在 /v1/models。' }) : nothing}
          ${aliasTag(m)}
          ${capabilityBadges(m).map((b) => badge(b.label, b.tone, { small: true, title: b.title }))}
        </div>
        ${m.name && m.name !== m.id ? html`<span class="mono text-2xs muted break-all">${m.id}</span>` : nothing}
      </div>`;
  }

  function enableSwitch(m) {
    const busy = state.pending.has(m.id);
    return switchControl({
      checked: !!m.enabled,
      disabled: busy,
      label: m.enabled ? '已开启处理' : '未处理',
      onChange: (value) => toggleModel(m.id, value),
    });
  }

  function effortCell(m) {
    const levels = reasoningLevels(m);
    if (!levels.length) return html`<span class="muted">—</span>`;
    return html`<span class="row-wrap" style="gap:4px">${levels.map((level) => badge(level, 'violet', { small: true, title: '思考档位' }))}</span>`;
  }

  function accountCell(m, account) {
    if (!m.enabled) {
      return badge('已停止处理', 'danger', { small: true, title: '网关已停止处理这个模型。' });
    }
    if (!accountCanUse(m, account)) {
      return badge('已排除', 'warning', { small: true, title: '这个账号不接这个模型，请求会交给其他账号' });
    }
    return badge('可用', 'success', { small: true });
  }

  function coverageCell(m) {
    const coverage = coverageFor(m, realmAccountList());
    return html`<span class="nums ${coverage.can === 0 ? 'tone-warning' : ''}"
                      title="已启用账号中可以接这个模型的数量">${coverage.text}</span>`;
  }

  function table(rows) {
    const account = currentAccount();
    return html`
      <div class="table-wrap desktop-only">
        <table class="table">
          <thead>
            <tr>
              <th>模型</th>
              <th>上下文</th>
              <th>最大输出</th>
              <th>思考档位</th>
              <th>积分倍率</th>
              ${account ? html`<th class="center">该账号</th>` : html`<th>账号可用</th>`}
              ${account ? nothing : html`<th class="center">启用</th>`}
            </tr>
          </thead>
          <tbody>
            ${repeat(rows, (m) => m.id, (m) => html`
              <tr class=${m.enabled ? '' : 'row-off'}>
                <td>${modelCell(m)}</td>
                <td class="nums" title=${contextTitle(m) || nothing}>${contextText(m)}</td>
                <td class="nums">${outputText(m)}</td>
                <td>${effortCell(m)}</td>
                <td>${creditCell(m, account)}</td>
                ${account ? html`<td class="center">${accountCell(m, account)}</td>`
                          : html`<td>${coverageCell(m)}</td>`}
                ${account ? nothing : html`<td class="center">${enableSwitch(m)}</td>`}
              </tr>`)}
          </tbody>
        </table>
      </div>
      <div class="mobile-list">
        ${rows.map((m) => html`
          <div class="mobile-item ${m.enabled ? '' : 'row-off'}">
            <div class="row-between">
              <div class="grow">${modelCell(m)}</div>
              ${account ? accountCell(m, account) : enableSwitch(m)}
            </div>
            ${account ? nothing : html`<div class="kv"><span>账号可用</span><span>${coverageCell(m)}</span></div>`}
            <div class="kv"><span>上下文</span><span>${contextText(m)}</span></div>
            <div class="kv"><span>最大输出</span><span>${outputText(m)}</span></div>
            <div class="kv"><span>思考档位</span><span>${effortCell(m)}</span></div>
            <div class="kv"><span>积分倍率</span><span>${creditCell(m, account)}</span></div>
          </div>`)}
      </div>`;
  }

  function body() {
    const rows = selectModels(state.data, state);
    const filtered = state.query.trim() || state.capability !== 'all';
    return html`
      ${sourceLine()}
      ${stats()}
      ${aliasCard()}
      ${filterCard()}
      <div class="card card-flush">
        ${rows.length ? table(rows) : emptyState(filtered
          ? { icon: 'SearchX', title: '没有匹配的模型', desc: '换个关键词，或把能力筛选切回「全部」。' }
          : { icon: 'Boxes', title: '暂无模型', desc: '没有读到任何模型，请确认账号可用后重新获取。' })}
        <div class="card-foot split">
          <span>已筛选出 ${rows.length} 个，共 ${(state.data || []).length} 个</span>
          <span>${capabilityLabel(state.capability)}${state.query.trim() ? ` · 关键词「${state.query.trim()}」` : ''}</span>
        </div>
      </div>`;
  }

  function view() {
    const actions = html`
      <button class="btn btn-outline btn-sm" title="重新获取模型目录，平时按 5 分钟缓存。"
              @click=${busyClick(refresh)}>${icon('RefreshCw')}重新获取</button>`;
    return html`
      <div class="page" aria-busy=${!!state.pending.size}>
        ${pageHeader({
          path: 'models',
          title: '模型中心',
          desc: spaceMixed(`${channelLabel(realm)}账号可用的模型与账号覆盖率。`),
          actions,
        })}
        ${state.error ? loadErrorPage('读取模型目录失败：' + state.error, () => load(false))
          : state.data === null ? html`${skeletonStats(4)}${skeletonRows(6)}`
          : html`${state.inline ? loadErrorInline(state.inline, refresh) : nothing}${staleNotice()}${body()}`}
      </div>`;
  }

  function draw() {
    render(view(), host);
  }

  draw();
  load(false);
  return { refresh: () => load(false) };
}
