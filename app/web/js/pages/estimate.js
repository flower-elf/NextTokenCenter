// 费用估算页：按设置的官方价格折算当前范围的请求费用，并对比积分支出算出省了多少。
// 时间范围控件与用量页共用 stats/range.js。

import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtNumber, fmtCredit, fmtCny, spaceMixed } from '../core/format.js';
import {
  pageHeader, statCard, badge, loadErrorPage, loadErrorInline, skeletonStats, skeletonBlock,
} from '../ui/widgets.js';
import { rangeLabel, scopeText, creditKnown } from './stats/logic.js';
import { createRangeControls } from './stats/range.js';

export const autoRefresh = true;

export function mount(host) {
  let data = null;
  let missing = '';
  let fatal = '';
  let phase = 'loading';
  let seq = 0;
  const rangeCtl = createRangeControls({ redraw: draw, reload: load });

  function draw() {
    render(view(), host);
  }

  async function load() {
    const mine = ++seq;
    try {
      const payload = await getJSON('/usage/cost-estimate' + rangeCtl.query());
      if (mine !== seq) return;
      data = payload;
      missing = '';
      fatal = '';
      phase = 'ready';
    } catch (err) {
      if (mine !== seq) return;
      if (data) {
        missing = err.message || '读取费用估算失败';
        phase = 'ready';
      } else {
        fatal = err.message || '读取费用估算失败';
        phase = 'error';
      }
    }
    draw();
  }

  function retry() {
    phase = 'loading';
    fatal = '';
    draw();
    return load();
  }

  function money(value) {
    return value == null ? '—' : fmtCny(value);
  }

  /** 这一行没有折算积分支出：上游整段窗口都没有返回单次扣费。 */
  function creditUncollected(row) {
    return row.paid_cny == null;
  }

  /** 部分请求没有返回单次扣费；已采集的部分仍参与积分折算。 */
  function creditPartial(row) {
    return !creditUncollected(row) && row.credit_missing === true;
  }

  function priceTitle(row) {
    if (!row.priced || !row.prices) return '设置里没有配置这个模型的价格，按 0 计，不参与估算';
    return '输入 ' + fmtCny(row.prices.input) + ' · 缓存命中 ' + fmtCny(row.prices.cached_input)
      + ' · 输出 ' + fmtCny(row.prices.output) + '（每百万 Token）';
  }

  function statCards() {
    const summary = (data && data.summary) || {};
    const rows = (data && data.models) || [];
    const pricedRows = rows.filter((row) => row.priced);
    const knownPriced = pricedRows.filter((row) => !creditUncollected(row));
    const noPricedCredit = pricedRows.length > 0 && knownPriced.length === 0;
    const savedKnown = !noPricedCredit && creditKnown(summary.saved_cny);
    const tone = savedKnown && summary.saved_cny > 0 ? 'success' : (savedKnown && summary.saved_cny < 0 ? 'danger' : '');
    const missing = Number(summary.credit_missing) || 0;
    return html`
      <div class="grid-stats cols-4">
        ${statCard({
          label: rangeLabel(rangeCtl.range) + ' 官方价格合计',
          value: data ? fmtCny(summary.official_cny || 0) : '—',
          icon: 'ReceiptText',
          hint: data ? summary.priced_models + ' 个模型已定价' : '费用估算未读取到',
        })}
        ${statCard({
          label: '积分折算支出',
          value: data && !noPricedCredit && creditKnown(summary.paid_cny) ? fmtCny(summary.paid_cny) : '—',
          icon: 'Coins',
          hint: noPricedCredit
            ? '已定价模型的积分全部未采集，无法折算'
            : (missing
              ? missing + ' 个模型未采集积分，不参与折算'
              : (data && data.credit_price
                ? '每积分 ' + fmtCny(data.credit_price)
                : '积分折算单价为 0，积分不计支出')),
        })}
        ${statCard({
          label: '省了多少',
          value: data && savedKnown ? fmtCny(summary.saved_cny) : '—',
          icon: 'PiggyBank',
          tone,
          hint: noPricedCredit
            ? '积分未采集，无法计算节省额'
            : (missing
              ? missing + ' 个模型未采集积分，不计入节省额'
              : (data ? '官方价格合计减去积分折算支出' : '费用估算未读取到')),
        })}
        ${statCard({
          label: '未定价模型',
          value: data ? fmtNumber((summary.unpriced_models || []).length || summary.unpriced_models || 0) : '—',
          icon: 'CircleHelp',
          hint: '这些模型按 0 计，不参与估算',
        })}
      </div>`;
  }

  function tableCard() {
    const rows = (data && data.models) || [];
    if (!rows.length) {
      return html`
        <div class="card">
          <div class="empty text-xs muted">${rangeLabel(rangeCtl.range)}没有已完成的请求记录，费用估算没有可折算的用量。</div>
        </div>`;
    }
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <h2 class="card-title">${icon('Calculator')}按模型的费用估算</h2>
          <span class="card-sub">官方价格取自「设置 → 费用估算」，缓存命中按命中价计费。</span>
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>模型</th>
                <th class="right">请求数</th>
                <th class="right matrix-opt">输入 Token</th>
                <th class="right matrix-opt">缓存命中</th>
                <th class="right matrix-opt">输出 Token</th>
                <th class="right">官方价格</th>
                <th class="right">消耗积分</th>
                <th class="right matrix-opt">积分折合</th>
                <th class="right">省了多少</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((row) => {
                const missing = creditUncollected(row);
                const partial = creditPartial(row);
                return html`
                <tr>
                  <td>
                    <div class="cell-mono">${row.model}</div>
                    ${row.priced
                      ? nothing
                      : html`<div class="text-2xs muted">未配置价格，按 0 计</div>`}
                    ${row.priced && missing
                      ? html`<div class="text-2xs muted">上游未返回扣费，官方价格照常计入合计</div>`
                      : nothing}
                  </td>
                  <td class="right nums">${fmtNumber(row.requests)}</td>
                  <td class="right nums matrix-opt">${fmtNumber(row.prompt_tokens)}</td>
                  <td class="right nums matrix-opt">${fmtNumber(row.cached_tokens)}</td>
                  <td class="right nums matrix-opt">${fmtNumber(row.completion_tokens)}</td>
                  <td class="right nums" title=${priceTitle(row)}>${row.priced ? money(row.official_cny) : html`<span class="muted">—</span>`}</td>
                  <td class="right nums">${missing
                    ? badge('不参与积分折算', 'warning', {
                        small: true, title: '上游没有返回单次扣费，积分支出按未采集处理',
                      })
                    : html`<span title=${partial ? '部分请求未返回单次扣费，按已采集部分折算' : nothing}>${fmtCredit(row.credit)}</span>`}</td>
                  <td class="right nums matrix-opt">${row.priced && !missing ? money(row.paid_cny) : html`<span class="muted">—</span>`}</td>
                  <td class="right nums">
                    ${row.priced && !missing && row.saved_cny != null
                      ? html`<span class=${row.saved_cny < 0 ? 'tone-danger' : 'tone-success'}>${money(row.saved_cny)}</span>`
                      : html`<span class="muted">—</span>`}
                  </td>
                </tr>`;
              })}
            </tbody>
          </table>
        </div>
        <div class="card-foot">
          只统计已完成的请求；未返回单次扣费的模型，官方价格照常计入合计，积分支出不参与折算。
          ${(data.unpriced_models || []).length
            ? ' 未定价模型：' + data.unpriced_models.join('、') + '，填写价格后进入合计。'
            : nothing}
        </div>
      </div>`;
  }

  function view() {
    const desc = spaceMixed('按官方价格与积分折算比较当前范围的支出，统计范围：'
      + scopeText(rangeCtl.includeAll, getState().viewRealm) + '。');
    if (phase === 'loading') {
      return html`
        <div class="page">
          ${pageHeader({ path: 'estimate', title: '费用估算', desc, actions: rangeCtl.actions() })}
          ${skeletonStats(4)}
          ${skeletonBlock(320)}
        </div>`;
    }
    if (phase === 'error') {
      return html`<div class="page">${pageHeader({ path: 'estimate', title: '费用估算', desc, actions: rangeCtl.actions() })}${loadErrorPage(fatal || '费用估算没有读取到', retry)}</div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: 'estimate', title: '费用估算', desc, actions: rangeCtl.actions() })}
        ${rangeCtl.range === 'custom' ? rangeCtl.customCard() : nothing}
        ${missing ? loadErrorInline('费用估算没有刷新成功，下面显示的是上次结果：' + missing + '。', retry) : nothing}
        ${statCards()}
        ${data.summary && Number(data.summary.credit_missing) > 0 ? html`
          <div class="notice notice-warn" role="status">${icon('TriangleAlert')}
            <div class="grow">有 ${fmtNumber(data.summary.credit_missing)} 个模型未返回单次扣费，官方价格照常计入合计，积分不参与折算。</div>
          </div>` : nothing}
        ${tableCard()}
      </div>`;
  }

  draw();
  load();

  return {
    refresh: () => load(),
    unmount() { seq += 1; },
  };
}
