// 任务页：调度器状态、按渠道登记表显示的每日任务（Buddy 加油站与成长任务、每日活跃打卡）与积分变动记录；任何操作都不会改动右上角「查看的渠道」，页面始终只读 getState().viewRealm。

import { html, nothing, render } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtNumber, spaceMixed } from '../core/format.js';
import { channelLabel } from '../core/channels.js';
import { toast, toastError } from '../ui/toast.js';
import {
  badge, busyClick, emptyState, loadErrorInline, loadErrorPage, pageHeader, pager, segmented, skeletonBlock, skeletonRows, statCard,
} from '../ui/widgets.js';
import * as logic from './tasks/logic.js';
import * as activity from './accounts/activity.js';
import * as acct from './accounts/actions.js';
import { runCheckin, runDailyChatPending, runDailyChatWeb } from './accounts/flows.js';

export const autoRefresh = true;

const CREDIT_PAGE_SIZE = 20;

export function mount(host) {
  const S = {
    scheduler: null,
    accounts: [],
    activity: {},
    growth: null,
    growthUid: 'all',
    mobileTab: 'scheduler',
    running: false,
    runLogs: [],
    creditEvents: null,
    creditTotal: 0,
    creditPage: 1,
    loading: true,
    error: '',
    partial: '',
  };

  function draw() {
    render(view(), host);
  }

  let refreshTimer = null;

  // ---------------------------------------------------------------- 数据

  async function refresh() {
    const realm = getState().viewRealm;
    const problems = [];
    let schedulerOk = false;
    let eventsOk = false;
    try {
      S.scheduler = await getJSON('/scheduler');
      schedulerOk = true;
    } catch (err) {
      problems.push('调度器状态没有读取成功：' + err.message);
    }
    try {
      await loadCreditEvents(realm);
      eventsOk = true;
    } catch (err) {
      problems.push('积分变动记录没有读取成功：' + err.message);
    }
    if (activity.activityKind(realm) === 'buddy') {
      try {
        await loadGrowth();
      } catch (err) {
        problems.push('成长任务没有读取成功：' + err.message);
      }
    }
    try {
      const [accounts, today] = await Promise.all([acct.loadAccounts(), acct.loadActivityToday()]);
      S.accounts = accounts.accounts || [];
      S.activity = activity.latestByUid(today.rows || []);
    } catch (err) {
      problems.push('今日活动没有读取成功：' + err.message);
    }
    if (!schedulerOk && !eventsOk && S.loading) {
      S.error = problems.join(' ');
    } else {
      S.error = '';
    }
    S.partial = problems.join(' ');
    S.loading = false;
    draw();
  }

  async function loadCreditEvents(realm) {
    const offset = (S.creditPage - 1) * CREDIT_PAGE_SIZE;
    const data = await getJSON('/accounts/credit-events?realm=' + encodeURIComponent(realm)
      + '&limit=' + CREDIT_PAGE_SIZE + '&offset=' + offset);
    S.creditEvents = data.events || [];
    S.creditTotal = Number(data.total) || 0;
    // 记录过期或换了版本后，当前页可能已经超出范围，退回最后一页。
    const pages = Math.max(1, Math.ceil(S.creditTotal / CREDIT_PAGE_SIZE));
    if (S.creditPage > pages) {
      S.creditPage = pages;
      await loadCreditEvents(realm);
    }
  }

  async function goCreditPage(next) {
    S.creditPage = next;
    try {
      await loadCreditEvents(getState().viewRealm);
    } catch (err) {
      toastError('积分变动记录没有读取成功', err);
    }
    draw();
  }

  async function loadGrowth() {
    const query = S.growthUid && S.growthUid !== 'all' ? '?uid=' + encodeURIComponent(S.growthUid) : '';
    S.growth = await getJSON('/tasks' + query);
  }

  // ---------------------------------------------------------------- 动作

  async function triggerScheduler() {
    try {
      const result = await postJSON('/scheduler/trigger', {});
      toast.success(result.msg || '已经触发一次巡检');
      // 巡检有几秒延迟，两秒后回读一次结果；卸载时取消，不在已离开的页面上刷新。
      refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, 2000);
    } catch (err) {
      toastError('触发失败', err);
    }
  }

  async function toggleScheduler() {
    try {
      const result = await postJSON('/scheduler/toggle', {});
      toast.success(result.enabled ? '后台调度器已启用' : '后台调度器已暂停');
      await refresh();
    } catch (err) {
      toastError('切换失败', err);
    }
  }

  async function runGrowthTasks() {
    S.running = true;
    S.runLogs = ['[task_runner] 正在接取任务、上报事件并领取奖励，请稍候'];
    draw();
    try {
      const uid = logic.checkinSelection(S.growthUid);
      const payload = uid ? { uid } : {};
      const result = await postJSON('/tasks/run', payload);
      S.runLogs = result.logs || [];
      if (result.ok === false) toast.warn('成长任务没有执行', result.msg || '目标账号正在执行其他任务');
      else if (result.busy_accounts && result.busy_accounts.length) {
        toast.warn('部分账号已跳过', result.msg || ('有 ' + result.busy_accounts.length + ' 个账号正在执行其他任务'));
      } else if (result.credit_added) toast.success('成长任务执行完成', '本次共增加 ' + fmtNumber(result.credit_added) + ' 积分');
      else toast.success('成长任务执行完成', result.msg || '没有新的积分变化');
      await refresh();
    } catch (err) {
      S.runLogs = S.runLogs.concat(['执行出错：' + err.message]);
      toastError('任务执行失败', err);
    } finally {
      S.running = false;
      draw();
    }
  }

  async function triggerCatTravel() {
    try {
      const uid = logic.checkinSelection(S.growthUid);
      const payload = uid ? { uid } : {};
      const result = await postJSON('/tasks/travel', payload);
      if (result.busy_accounts && result.busy_accounts.length) {
        toast.warn('猫猫旅行部分跳过', result.msg || ('有 ' + result.busy_accounts.length + ' 个账号正在执行其他任务'));
      } else if (result.ok) toast.success('猫猫旅行结算完成', result.msg || '');
      else toast.warn('猫猫旅行没有完成', result.msg || '上游没有返回结果');
      await refresh();
    } catch (err) {
      toastError('猫猫旅行失败', err);
    }
  }

  // 批量签到与打卡的流程和账号页共用 accounts/flows.js；apply 只负责把结果刷新回本页。
  const doCheckin = (uid) => runCheckin({ uid, apply: () => refresh() });

  const doDailyChatPending = () => runDailyChatPending({
    accounts: S.accounts,
    byUid: S.activity,
    apply: () => refresh(),
  });

  const doDailyChatWeb = () => runDailyChatWeb({ apply: () => refresh() });

  /** CodeArts 签到：uid 为空时给当前渠道全部可签到账号领取。 */
  async function doCheckinAll(uid = '') {
    try {
      const result = uid ? await acct.checkin(uid) : await acct.checkinRealm(getState().viewRealm);
      const results = result.results || [];
      const okCount = results.filter((row) => row.ok).length;
      const failed = results.length - okCount;
      if (!results.length) toast.warn('没有可签到的账号');
      else if (failed) toast.warn('签到完成，部分账号失败', '成功 ' + okCount + ' 个，失败 ' + failed + ' 个');
      else toast.success('签到完成', '成功 ' + okCount + ' 个账号');
      await refresh();
    } catch (err) {
      toastError('签到失败', err);
    }
  }

  // ---------------------------------------------------------------- 片段

  function schedulerCard(realm) {
    if (S.loading && !S.scheduler) {
      return html`<div class="card">${skeletonBlock(96)}</div>`;
    }
    const view = logic.schedulerView(S.scheduler || {}, realm);
    const logs = logic.schedulerLogLines(S.scheduler);
    return html`
      <div class="card">
        <div class="card-head">
          <h2 class="card-title">${icon('AlarmClock')}后台定时调度器</h2>
          <div class="row-wrap">
            <button class="btn btn-outline btn-sm" title="立刻执行一次巡检"
                    @click=${busyClick(triggerScheduler)}>${icon('Play')}${view.triggerText}</button>
            <button class="btn btn-outline btn-sm" title="暂停后不再自动巡检，手动触发仍然可用"
                    @click=${busyClick(toggleScheduler)}>${view.enabled ? icon('Pause') : icon('Play')}${view.toggleText}</button>
          </div>
        </div>
        <div class="row-wrap">
          <span class="status-dot ${view.enabled ? 'ok' : 'bad'}"></span>
          <span class="text-sm">${view.stateText}</span>
        </div>
        <div class="text-2xs muted">${view.timesText}</div>
        <div class="log-box scroll-slim">
          ${logs.length
            ? logs.map((line) => html`<div class="log-line">${line}</div>`)
            : html`<div class="log-line muted">还没有巡检记录，第一次巡检完成后会显示在这里。</div>`}
        </div>
      </div>`;
  }

  function growthCard(realm) {
    if (S.loading && !S.growth) {
      return html`<div class="card">${skeletonBlock(180)}</div>`;
    }
    const data = S.growth || {};
    const accounts = data.accounts || [];
    const options = logic.growthAccountOptions(accounts, S.growthUid);
    const summary = data.summary || {};
    const tasks = data.tasks || [];
    const travel = logic.travelView(summary.travel);
    return html`
      <div class="card">
        <div class="card-head">
          <div class="grow">
            <h2 class="card-title">${icon('Sparkles')}任务与福利</h2>
            <p class="card-sub">Buddy 加油站与成长任务一起执行，选单个账号时只对该账号生效。</p>
          </div>
          <div class="row-wrap card-actions">
            <select class="select input-sm" title="选择要执行的账号，全部账号会逐个执行"
                    .value=${options.selected}
                    @change=${(event) => { S.growthUid = event.target.value; refresh(); }}>
              ${options.options.map((option) => html`<option value=${option.value} .selected=${option.value === options.selected}>${option.label}</option>`)}
            </select>
            <button class="btn btn-primary btn-sm" title="领取所选账号的 Buddy 加油站积分"
                    @click=${busyClick(() => doCheckin(logic.checkinSelection(S.growthUid)))}>
              ${icon('Fuel')}Buddy 加油站签到
            </button>
            <button class="btn btn-primary btn-sm" title="接取任务、上报事件并领取奖励"
                    ?disabled=${S.running} @click=${busyClick(runGrowthTasks)}>
              ${S.running ? icon('LoaderCircle', 'spin') : icon('Sparkles')}${S.running ? '任务执行中' : '执行任务领积分'}
            </button>
            <button class="btn btn-outline btn-sm" title="让猫猫出门旅行，归来后领取积分奖励"
                    @click=${busyClick(triggerCatTravel)}>${icon('Gift')}猫猫旅行</button>
          </div>
        </div>
        ${data.msg ? loadErrorInline(data.msg, refresh) : nothing}
        <div class="grid-stats cols-4">
          ${activityStatCards(realm, summary)}
          ${statCard({ label: '成长能量余额', value: fmtNumber(summary.energy || 0), icon: 'Zap', tone: 'success' })}
          ${statCard({ label: '猫猫日常状态', value: travel.text, icon: 'Cat', tone: travel.tone, hint: travel.desc })}
        </div>
        <div class="table-wrap">
          <table class="table table-compact">
            <thead>
              <tr>
                <th>任务名称 / 代码</th>
                <th>任务说明</th>
                <th>进度</th>
                <th>奖励</th>
                <th>执行方式</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              ${tasks.length
                ? tasks.map((task) => {
                  const row = logic.growthTaskView(task);
                  return html`
                    <tr>
                      <td><div class="medium">${row.name}</div><div class="cell-mono">${row.code}</div></td>
                      <td class="cell-sub">${row.description}</td>
                      <td class="nums">${row.progress}</td>
                      <td class="nums">${row.rewards}</td>
                      <td>
                        ${badge(row.mode.text, row.mode.tone)}
                        ${row.mode.hint ? html`<div class="cell-sub">${row.mode.hint}</div>` : nothing}
                      </td>
                      <td>${badge(row.badge.text, row.badge.tone)}</td>
                    </tr>`;
                })
                : html`<tr><td colspan="6">${emptyState({ icon: 'Inbox', title: '没有读到成长任务', desc: logic.growthEmptyText(realm) })}</td></tr>`}
            </tbody>
          </table>
        </div>
        ${S.runLogs.length ? html`
          <div class="log-box scroll-slim">
            ${S.runLogs.map((line) => html`<div class="log-line">${line}</div>`)}
          </div>` : nothing}
      </div>`;
  }

  function activityStatCards(realm, growth = null) {
    const summary = activity.todaySummary(S.accounts, S.activity, realm);
    const kind = activity.activityKind(realm);
    if (!kind) return [];
    if (kind === 'buddy') {
      const days = Number(growth && growth.streak_days) || 0;
      return [
        statCard({
          label: '本期 Buddy 加油站活动',
          icon: 'Fuel',
          ...activity.summaryStat(summary, { doneWord: '已领', pendingWord: '未领' }),
        }),
        statCard({
          label: '成长计划连登',
          value: growth ? days + ' 天' : '—',
          icon: 'Flame',
          tone: days > 0 ? 'success' : 'warning',
          hint: '连续登录活跃天数，由调度器每天上报',
        }),
      ];
    }
    return [statCard({
      label: '今日活跃打卡状态',
      icon: 'CalendarCheck',
      ...activity.summaryStat(summary, { doneWord: '已打卡', pendingWord: '未打卡' }),
    })];
  }

  function activityStats(realm) {
    return html`<div class="grid-stats">${activityStatCards(realm)}</div>`;
  }

  function dailyCard(realm) {
    return html`
      <div class="card">
        <div class="card-head">
          <div>
            <h2 class="card-title">${icon('CalendarCheck')}今日活跃</h2>
            <p class="card-sub">每日活跃只认网页端对话，两种打卡都会消耗少量积分。</p>
          </div>
          <div class="row-wrap">
            <button class="btn btn-primary btn-sm" title="今天还没打卡的已启用账号"
                    @click=${busyClick(doDailyChatPending)}>${icon('CalendarCheck')}未活跃账号一键打卡</button>
            <button class="btn btn-outline btn-sm" title="只建网页端会话，会消耗少量积分"
                    @click=${doDailyChatWeb}>${icon('Globe')}网页通道打卡</button>
          </div>
        </div>
        ${activityStats(realm)}
      </div>`;
  }

  function checkinCard(realm) {
    const rows = logic.checkinRows(S.accounts, realm);
    const credited = logic.checkinCredits(rows);
    const summary = activity.todaySummary(S.accounts, S.activity, realm);
    return html`
      <div class="card">
        <div class="card-head">
          <div class="grow">
            <h2 class="card-title">${icon('CalendarCheck')}今日签到</h2>
            <p class="card-sub">${spaceMixed(channelLabel(realm) + '账号每天可领取一次签到积分，也可以在这里立即补签。')}</p>
          </div>
          <div class="row-wrap card-actions">
            <button class="btn btn-primary btn-sm" title="给当前渠道全部启用账号各签到一次"
                    @click=${busyClick(() => doCheckinAll())}>${icon('CalendarCheck')}签到全部账号</button>
          </div>
        </div>
        <div class="grid-stats cols-3">
          ${statCard({
            label: '今日签到情况',
            icon: 'CalendarCheck',
            ...activity.summaryStat(summary, { doneWord: '已签到', pendingWord: '未签到' }),
          })}
          ${statCard({
            label: '今日签到到账积分',
            value: credited === null ? '—' : fmtNumber(credited),
            icon: 'Coins',
            tone: credited ? 'success' : '',
            hint: credited === null ? '上游没有返回签到积分' : '按签到结果返回的积分合计',
          })}
          ${statCard({
            label: '启用账号', value: fmtNumber(rows.length), icon: 'Users',
            hint: '停用的账号不参与签到',
          })}
        </div>
        ${rows.length ? html`
          <div class="table-wrap">
            <table class="table table-compact">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>今日状态</th>
                  <th>最近签到</th>
                  <th class="right">到账积分</th>
                  <th>返回信息</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td><div class="medium truncate">${row.name}</div><div class="cell-mono">${row.uid.slice(0, 10)}</div></td>
                    <td>${badge(row.text, row.tone, { small: true })}</td>
                    <td class="nums">${row.at || '—'}</td>
                    <td class="right nums">${row.credits === null ? '—' : fmtNumber(row.credits)}</td>
                    <td class="cell-sub">${row.message || '—'}</td>
                    <td class="right">
                      ${row.state === 'done'
                        ? nothing
                        : html`<button class="btn btn-outline btn-xs" @click=${busyClick(() => doCheckinAll(row.uid))}>签到</button>`}
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`
          : emptyState({
              icon: 'Users',
              title: '没有可签到的账号',
              desc: spaceMixed('添加并启用' + channelLabel(realm) + '账号后，这里会显示今天的签到状态。'),
            })}
      </div>`;
  }

  function creditEventsCard(realm) {
    if (S.creditEvents === null) {
      return html`<div class="card card-flush">${skeletonRows(4)}</div>`;
    }
    const rows = logic.creditEventRows(S.creditEvents);
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <div>
            <h2 class="card-title">${icon('Coins')}积分变动记录</h2>
            <p class="card-sub">${spaceMixed(channelLabel(realm) + '账号最近 30 天余额增加的记录，最新的在前面。')}</p>
          </div>
          <span class="text-2xs muted">共 ${S.creditTotal} 条</span>
        </div>
        ${rows.length ? html`
          <div class="table-wrap">
            <table class="table table-compact">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>账号</th>
                  <th class="right">变更前</th>
                  <th class="right">变更后</th>
                  <th class="right">变动</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td class="nums">${row.time}</td>
                    <td>${row.account}</td>
                    <td class="right nums">${row.before}</td>
                    <td class="right nums">${row.after}</td>
                    <td class="right nums tone-${row.tone}">${row.deltaText}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>
          ${pager({
            page: S.creditPage,
            pages: Math.max(1, Math.ceil(S.creditTotal / CREDIT_PAGE_SIZE)),
            total: S.creditTotal,
            onPage: goCreditPage,
          })}`
          : emptyState({ icon: 'Coins', title: '还没有积分变动记录', desc: logic.creditEventsEmptyText(realm) })}
      </div>`;
  }

  function view() {
    const realm = getState().viewRealm;
    return html`
      <div class="page">
        ${pageHeader({
          path: 'tasks',
          title: '任务',
          desc: spaceMixed(channelLabel(realm) + '的每日任务与后台调度器状态。'),
          actions: html`<button class="btn btn-outline btn-sm" @click=${busyClick(refresh)}>${icon('RefreshCw')}刷新</button>`,
        })}
        ${S.error ? loadErrorPage(S.error, refresh) : nothing}
        ${!S.error && S.partial ? loadErrorInline(S.partial, refresh) : nothing}
        ${S.error ? nothing : html`
          <div class="tasks-switch">${segmented(sections(realm), S.mobileTab, switchSection, 'seg-sm')}</div>
          ${taskBlock('scheduler', schedulerCard(realm))}
          ${activity.activityKind(realm) === 'daily' ? taskBlock('daily', dailyCard(realm)) : nothing}
          ${activity.activityKind(realm) === 'codearts' ? taskBlock('checkin', checkinCard(realm)) : nothing}
          ${activity.activityKind(realm) === 'buddy' ? taskBlock('growth', growthCard(realm)) : nothing}
          ${taskBlock('credits', creditEventsCard(realm))}`}
      </div>`;
  }

  // 移动端一次只显示一块，桌面端同时可见（与参考项目一致）。
  function sections(realm) {
    const items = [{ id: 'scheduler', label: '调度器' }];
    const kind = activity.activityKind(realm);
    if (kind === 'daily') items.push({ id: 'daily', label: '今日活跃' });
    if (kind === 'buddy') items.push({ id: 'growth', label: '任务与福利' });
    if (kind === 'codearts') items.push({ id: 'checkin', label: '今日签到' });
    items.push({ id: 'credits', label: '积分记录' });
    if (!items.some((item) => item.id === S.mobileTab)) S.mobileTab = 'scheduler';
    return items;
  }

  function switchSection(id) {
    S.mobileTab = id;
    draw();
  }

  function taskBlock(id, content) {
    return html`<div class=${'task-block' + (S.mobileTab === id ? ' active' : '')}>${content}</div>`;
  }

  draw();
  refresh();

  return {
    refresh,
    unmount() {
      clearTimeout(refreshTimer);
    },
  };
}
