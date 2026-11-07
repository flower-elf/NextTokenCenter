// 任务页的纯逻辑：调度器文案、成长任务状态、猫猫旅行状态、积分变动记录；只依赖 core/format.js、core/channels.js 与账号页的 logic 模块，可以在 Node 里直接测试。

import { fmtCredit, fmtDate, fmtDateTime, fmtNumber, fmtTime, spaceMixed } from '../../core/format.js';
import { channelLabel } from '../../core/channels.js';

/** 调度器状态整理成页面上要显示的四段文字。 */
export function schedulerView(status, realm) {
  const s = status || {};
  const enabled = !!s.enabled;
  // 调度器按渠道给出 mode_<渠道> 说明，没有时用通用说明
  const mode = s['mode_' + realm] || s.mode || '整点排程';
  return {
    enabled,
    stateText: enabled ? '运行中 · ' + mode : '已暂停',
    timesText: '上次巡检 ' + (s.last_run_time || '尚未运行') + ' · 下次巡检 ' + (s.next_run_time || '待调度'),
    toggleText: enabled ? '暂停调度' : '恢复调度',
    triggerText: '立即巡检保活',
    logs: Array.isArray(s.logs) ? s.logs : [],
  };
}

/** 成长任务的进度、奖励与状态徽章。 */
export function growthTaskView(task) {
  const status = String((task && task.status) || '');
  const current = Number((task && task.current) || 0);
  const target = Number((task && task.target) || 0);
  let badge = { text: status || '未知', tone: 'muted' };
  if (status === 'claimed') badge = { text: '已领取', tone: 'muted' };
  else if (status === 'completed' || (target > 0 && current >= target)) badge = { text: '待领奖', tone: 'success' };
  else if (status === 'accepted' || status === 'in_progress') badge = { text: '进行中', tone: 'info' };
  else if (status === 'not_accepted') badge = { text: '未接取', tone: 'muted' };
  const done = status === 'claimed' || status === 'completed' || (target > 0 && current >= target);
  const manual = !!(task && task.manual);
  const mode = manual
    ? { text: '需手动操作', tone: 'warning', hint: (task && task.manual_reason) || '' }
    : { text: '可自动推进', tone: 'success', hint: '' };
  const rewards = [
    task && task.reward_credit ? '+' + fmtNumber(task.reward_credit) + ' 积分' : '',
    task && task.reward_energy ? '+' + fmtNumber(task.reward_energy) + ' 能量' : '',
  ].filter(Boolean).join(' · ');
  return {
    name: (task && task.name) || (task && task.task_code) || '未命名任务',
    code: (task && task.task_code) || '',
    description: (task && task.description) || '—',
    progress: done ? '已达成' : current + ' / ' + target,
    rewards: rewards || '—',
    badge,
    mode,
  };
}

/** 猫猫旅行的归来时刻。剩余时间取同一个响应里的两个字段之差，不依赖本机与服务器的时钟一致。 */
export function travelEtaText(travel, now = Date.now()) {
  const remain = Number((travel && travel.arrive_at) || 0) - Number((travel && travel.server_now) || 0);
  if (!(remain > 0)) return '';
  return fmtTime(now + remain * 1000);
}

export function travelView(travel, now = Date.now()) {
  const tr = travel || {};
  const state = String(tr.state || 'unknown');
  if (state === 'arrived') {
    const reward = Number(tr.reward_credit) || 0;
    return {
      text: '旅行归来，可以领奖',
      tone: 'success',
      desc: reward > 0 ? '奖励 ' + fmtNumber(reward) + ' 积分，下次出发前不领取会作废' : '点「猫猫旅行」领取奖励',
    };
  }
  if (state === 'traveling') {
    const eta = travelEtaText(tr, now);
    return { text: '旅行中', tone: 'info', desc: eta ? '预计 ' + eta + ' 归来' : '预计稍后归来' };
  }
  if (tr.daily_limit_reached) {
    return { text: '今天已经旅行过', tone: 'muted', desc: '本地时间 0 点之后可以再次出发' };
  }
  if (state === 'idle') {
    return { text: '在家', tone: 'muted', desc: '每天可以出发一次' };
  }
  return { text: '状态未知', tone: 'muted', desc: '没有读取到猫猫旅行的状态' };
}

export function growthAccountOptions(accounts, current) {
  const options = [{ value: 'all', label: '全部账号（批量）' }];
  for (const account of accounts || []) {
    const uid = String(account.uid || '');
    options.push({ value: uid, label: (account.nickname || uid.slice(0, 8)) + ' ' + uid.slice(0, 6) });
  }
  const selected = options.some((option) => option.value === current) ? current : 'all';
  return { options, selected };
}

/** 账号选择器的取值：「全部账号」时留空，由网关逐个执行。 */
export function checkinSelection(growthUid) {
  return growthUid && growthUid !== 'all' ? growthUid : undefined;
}

export function creditEventRows(events) {
  return (events || []).map((event) => {
    const delta = Number(event.delta) || 0;
    return {
      at: Number(event.at) || 0,
      time: fmtDateTime(event.at),
      account: event.nickname || String(event.uid || '').slice(0, 8) || '账号',
      realm: event.realm || '',
      before: fmtCredit(event.before),
      after: fmtCredit(event.after),
      deltaText: (delta >= 0 ? '+' : '−') + fmtCredit(Math.abs(delta)),
      tone: delta >= 0 ? 'success' : 'warning',
    };
  });
}

/** 积分变动记录为空时的说明，写清什么时候才会有记录。 */
export function creditEventsEmptyText(realm) {
  return spaceMixed('还没有读到' + channelLabel(realm) + '的积分变动记录，余额增加后会自动记录。');
}

/**
 * 签到类渠道的账号行：只列启用的账号，今天的状态来自统一账号视图的 lastCheckin。
 * 昨天的成功记录不算今天已完成。
 */
export function checkinRows(accounts, realm, now = Date.now()) {
  const stamp = fmtDate(now);
  return (accounts || [])
    .filter((account) => (account.realm || '') === realm && account.enabled)
    .map((account) => {
      const last = account.lastCheckin || null;
      const at = Number(last && last.at) || 0;
      const today = at > 0 && fmtDate(at) === stamp;
      const ok = !!(last && last.ok);
      const credits = Number(last && last.credits);
      return {
        uid: String(account.uid || ''),
        name: account.nickname || String(account.uid || '').slice(0, 8) || '账号',
        state: today ? (ok ? 'done' : 'failed') : 'none',
        text: today ? (ok ? '已签到' : '签到失败') : '未签到',
        tone: today ? (ok ? 'success' : 'danger') : 'muted',
        at: today ? fmtDateTime(at) : '',
        message: String((last && last.message) || ''),
        credits: today && ok && Number.isFinite(credits) && credits > 0 ? credits : null,
      };
    });
}

/** 今日签到到账的积分合计；没有任何一条带积分时返回 null，页面显示破折号。 */
export function checkinCredits(rows) {
  let total = 0;
  let known = 0;
  for (const row of rows || []) {
    if (row.credits === null || row.credits === undefined) continue;
    total += row.credits;
    known += 1;
  }
  return known ? total : null;
}

export function growthEmptyText(realm) {
  return spaceMixed('没有读到成长任务，请确认有可用的' + channelLabel(realm) + '账号后刷新。');
}

export function schedulerLogLines(status, limit = 20) {
  const logs = Array.isArray(status && status.logs) ? status.logs : [];
  return logs.slice(-limit);
}
