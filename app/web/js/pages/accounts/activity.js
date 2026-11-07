// 账号今天的活动状态，种类来自渠道登记表：Buddy 加油站（签到）与成长计划的连登活跃上报，
// 每日活跃打卡，或 CodeArts 的当天签到。事实来源是 GET /activity/history?range=today 的流水
// 与账号的统一字段 lastCheckin；WorkBuddy 的 lastDailyChat / lastActivityReport 只在
// 「今天确实做过、但这一版还没有历史行」时补一个中性状态。没有记录不等于失败。

import { fmtDate, fmtDateTimeMarked } from '../../core/format.js';
import { channelFeature } from '../../core/channels.js';

export const BUDDY_LABEL = 'Buddy 加油站';

const CHECKIN_LABELS = { buddy: BUDDY_LABEL + '签到', codearts: '签到', daily: '签到' };

/** 签到动作的按钮文案，账号页与任务页共用一个来源。 */
export function checkinLabel(kind) {
  return CHECKIN_LABELS[kind] || '签到';
}

function startsToday(value, stamp) {
  return !!value && String(value).startsWith(stamp);
}

/** 流水按最新在前，每个 uid 只留最近一条。 */
export function latestByUid(rows) {
  const out = {};
  (rows || []).forEach((row) => {
    const uid = String((row && row.uid) || '');
    if (uid && !out[uid]) out[uid] = row;
  });
  return out;
}

function fromRow(row, label, okText, failText) {
  return {
    kind: row.ok ? 'ok' : 'failed',
    text: row.ok ? okText : failText,
    at: String(row.ts || '').replace('T', ' ').slice(0, 19),
    hint: String(row.message || ''),
  };
}

/**
 * 统一视图里的 lastCheckin：{at: 秒级时间戳, ok, message, credits?}。
 * 没有记录时返回 null，由调用方按「还没签到」展示。
 */
function lastCheckinOf(account) {
  const entry = account && account.lastCheckin;
  if (!entry || typeof entry !== 'object') return null;
  const at = Number(entry.at);
  return {
    at: Number.isFinite(at) && at > 0 ? at : 0,
    ok: entry.ok === true,
    message: String(entry.message || ''),
  };
}

/** 账号的活动种类：'buddy'、'daily'、'codearts'，渠道没有每日活动时为空串。 */
export function activityKind(realm) {
  const checkin = channelFeature(realm, 'checkin');
  if (checkin) return checkin;
  return channelFeature(realm, 'dailyChat') ? 'daily' : '';
}

export function activityView(account, byUid, now = Date.now()) {
  const uid = String((account && account.uid) || '');
  const row = (byUid || {})[uid] || null;
  const stamp = fmtDate(now);
  const kind = activityKind(account && account.realm);

  if (kind === 'buddy') {
    let gas = { kind: 'none', text: '未领', at: '', hint: '今天还没有 Buddy 加油站的记录' };
    if (row && row.task === 'checkin') gas = fromRow(row, BUDDY_LABEL, '已领', '失败');
    else {
      const entry = lastCheckinOf(account);
      if (entry && entry.ok && fmtDate(entry.at) === stamp) {
        gas = { kind: 'done', text: '已领', at: fmtDateTimeMarked(entry.at, now), hint: '今天已完成' };
      }
    }
    const streak = startsToday(account.lastActivityReport, stamp)
      ? { kind: 'done', text: '已签到', at: String(account.lastActivityReport), hint: '连登活跃已上报' }
      : { kind: 'none', text: '未签到', at: '', hint: '今天还没有成长计划连登活跃的记录' };
    return { kind, gas, streak };
  }
  if (kind === 'daily') {
    let daily = { kind: 'none', text: '未打卡', at: '', hint: '今天还没有每日活跃的记录' };
    if (row && row.task === 'daily_chat') daily = fromRow(row, '今日活跃', '已完成', '失败');
    else if (startsToday(account.lastDailyChat, stamp)) {
      daily = { kind: 'done', text: '已完成', at: String(account.lastDailyChat), hint: '今天已完成' };
    }
    return { kind, daily };
  }
  if (kind === 'codearts') {
    const entry = lastCheckinOf(account);
    let checkin = { kind: 'none', text: '未签到', at: '', hint: '今天还没有签到的记录' };
    if (entry && entry.at > 0 && fmtDate(entry.at) === stamp) {
      checkin = entry.ok
        ? { kind: 'done', text: '已签到', at: fmtDateTimeMarked(entry.at, now), hint: entry.message || '今天已完成签到' }
        : { kind: 'failed', text: '失败', at: fmtDateTimeMarked(entry.at, now), hint: entry.message || '今天签到失败' };
    }
    return { kind, checkin };
  }
  return { kind };
}

/** 这项活动今天算不算完成：流水成功或本地记下的完成时间都算；没有每日活动的渠道不算未完成。 */
export function isDone(status) {
  if (!status || !status.kind) return true;
  const item = status.kind === 'buddy' ? status.gas
    : status.kind === 'codearts' ? status.checkin
      : status.daily;
  return !!item && (item.kind === 'ok' || item.kind === 'done');
}

/** 带每日活跃的渠道里今天还没打卡的已启用账号，顺序与账号列表一致。 */
export function pendingDailyChat(accounts, byUid, now = Date.now()) {
  return (accounts || []).filter((account) => activityKind(account.realm) === 'daily'
    && account.enabled && !isDone(activityView(account, byUid, now)));
}

/** 某个渠道今天的完成情况：{total, done, pending: [账号]}。 */
export function todaySummary(accounts, byUid, realm, now = Date.now()) {
  const visible = (accounts || []).filter((account) => account.realm === realm && account.enabled);
  const pending = visible.filter((account) => !isDone(activityView(account, byUid, now)));
  return { total: visible.length, done: visible.length - pending.length, pending };
}

/** 某个渠道成长计划（连登活跃上报）今天的完成情况。 */
export function streakSummary(accounts, realm, now = Date.now()) {
  const stamp = fmtDate(now);
  const visible = (accounts || []).filter((account) => account.realm === realm && account.enabled);
  const done = visible.filter((account) => startsToday(account.lastActivityReport, stamp)).length;
  return { total: visible.length, done };
}

/** 悬停提示里的名单：昵称或 UID 前缀。 */
export function namesOf(accounts) {
  return (accounts || []).map((account) => account.nickname || String(account.uid || '').slice(0, 8));
}

/** 卡片上的「已领 x/y」文案。 */
export function summaryText(summary, doneWord, pendingWord) {
  if (!summary.total) return '无账号';
  if (summary.done === summary.total) return '今日' + doneWord + ' (' + summary.done + '/' + summary.total + ')';
  if (summary.done > 0) return '部分' + doneWord + ' (' + summary.done + '/' + summary.total + ')';
  return '今日' + pendingWord + ' (0/' + summary.total + ')';
}

/** 今日活动汇总卡的数值、色调与提示；账号页与任务页共用同一套口径。 */
export function summaryStat(summary, { doneWord, pendingWord }) {
  return {
    value: summaryText(summary, doneWord, pendingWord),
    tone: summary.total && summary.done === summary.total ? 'success' : 'warning',
    hint: summary.pending.length
      ? '待处理：' + namesOf(summary.pending).join('、')
      : '今天全部完成',
  };
}
