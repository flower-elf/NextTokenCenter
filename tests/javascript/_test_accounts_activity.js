/* 账号今日活动：Buddy 加油站、成长计划连登、今日活跃与 CodeArts 签到的判定与汇总。 */
import assert from 'node:assert/strict';
import './_channels_fixture.js';

import * as activity from '../../app/web/js/pages/accounts/activity.js';
import { fmtDate } from '../../app/web/js/core/format.js';

const now = new Date(2026, 9, 18, 12, 0, 0).getTime();
const stamp = fmtDate(now);
assert.equal(stamp, '2026-10-18');

const cn = (over = {}) => Object.assign({ uid: 'cn-1', realm: 'cn', enabled: true, nickname: '甲' }, over);
const intl = (over = {}) => Object.assign({ uid: 'intl-1', realm: 'intl', enabled: true, nickname: '乙' }, over);

// ---- 流水优先：今天最近的一条按 uid 建表
const byUid = activity.latestByUid([
  { uid: 'cn-1', task: 'checkin', ok: false, ts: stamp + 'T11:00:00', message: '上游拒绝' },
  { uid: 'cn-1', task: 'checkin', ok: true, ts: stamp + 'T09:00:00', message: '' },
]);
assert.equal(byUid['cn-1'].ok, false, '最新在前，第一次见到的就是最近一条');

// ---- 国内版：Buddy 加油站与成长连登
let view = activity.activityView(cn(), byUid, now);
assert.equal(view.gas.kind, 'failed');
assert.equal(view.gas.text, '失败');
assert.equal(view.streak.kind, 'none');
assert.equal(activity.isDone(view), false);

view = activity.activityView(cn({
  lastCheckin: { at: Math.floor(now / 1000) - 3600, ok: true, message: '' },
  lastActivityReport: stamp + ' 09:31:00',
}), {}, now);
assert.equal(view.gas.kind, 'done');
assert.equal(view.gas.text, '已领');
assert.equal(view.streak.kind, 'done');
assert.equal(view.streak.text, '已签到');
assert.equal(activity.isDone(view), true);

const yesterday = new Date(now - 86400 * 1000);
view = activity.activityView(cn({
  lastCheckin: { at: Math.floor(yesterday.getTime() / 1000), ok: true, message: '' },
}), {}, now);
assert.equal(view.gas.kind, 'none', '昨天的记录不算今天');
assert.equal(view.gas.text, '未领');

view = activity.activityView(cn(), {
  'cn-1': { uid: 'cn-1', task: 'checkin', ok: true, ts: stamp + 'T08:00:00' },
}, now);
assert.equal(view.gas.kind, 'ok');
assert.equal(activity.isDone(view), true);

// ---- 国际版：今日活跃
view = activity.activityView(intl(), {
  'intl-1': { uid: 'intl-1', task: 'daily_chat', ok: true, ts: stamp + 'T10:00:00' },
}, now);
assert.equal(view.daily.kind, 'ok');
assert.equal(view.daily.text, '已完成');
assert.equal(activity.isDone(view), true);

view = activity.activityView(intl({ lastDailyChat: stamp + ' 10:05:00' }), {}, now);
assert.equal(view.daily.kind, 'done');
assert.equal(view.daily.text, '已完成');

view = activity.activityView(intl({ lastDailyChat: fmtDate(yesterday) + ' 10:05:00' }), {}, now);
assert.equal(view.daily.kind, 'none');
assert.equal(view.daily.text, '未打卡');

// ---- CodeArts：当天签到状态来自统一字段 lastCheckin
const ca = (over = {}) => Object.assign({ uid: 'ca-1', realm: 'codearts', enabled: true, nickname: '丙' }, over);
const at = Math.floor(now / 1000);
assert.equal(activity.activityKind('codearts'), 'codearts');
assert.equal(activity.activityKind('cn'), 'buddy');
assert.equal(activity.activityKind('intl'), 'daily');
assert.equal(activity.activityKind('future'), '');

let caView = activity.activityView(ca({ lastCheckin: { at, ok: true, message: '签到成功', credits: 10 } }), {}, now);
assert.equal(caView.kind, 'codearts');
assert.equal(caView.checkin.kind, 'done');
assert.equal(caView.checkin.hint, '签到成功');
assert.equal(activity.isDone(caView), true);

caView = activity.activityView(ca({ lastCheckin: { at, ok: false, message: '活动未开始' } }), {}, now);
assert.equal(caView.checkin.kind, 'failed');
assert.equal(caView.checkin.hint, '活动未开始');
assert.equal(activity.isDone(caView), false);

assert.equal(activity.activityView(ca(), {}, now).checkin.kind, 'none', '没有记录按未签到处理');
assert.equal(activity.activityView(ca({ lastCheckin: { at: at - 86400, ok: true } }), {}, now).checkin.kind, 'none',
  '昨天的签到不算今天');
assert.equal(activity.isDone(activity.activityView(ca(), {}, now)), false, '未签到不算完成');

// 汇总：停用账号不计入总数，未签与失败都算待处理
const caAccounts = [
  ca({ uid: 'ca-1', lastCheckin: { at, ok: true } }),
  ca({ uid: 'ca-2', nickname: '丁' }),
  ca({ uid: 'ca-3', enabled: false }),
];
const caSummary = activity.todaySummary(caAccounts, {}, 'codearts', now);
assert.equal(caSummary.total, 2);
assert.equal(caSummary.done, 1);
assert.deepEqual(caSummary.pending.map((a) => a.uid), ['ca-2']);
assert.equal(activity.summaryStat(caSummary, { doneWord: '已签到', pendingWord: '未签到' }).tone, 'warning');

// ---- 只给还没打卡的账号补跑
const accounts = [
  intl({ uid: 'intl-1', nickname: '甲' }),
  intl({ uid: 'intl-2', nickname: '乙', lastDailyChat: stamp + ' 10:05:00' }),
  intl({ uid: 'intl-3', nickname: '丙', enabled: false }),
  cn(),
];
const pending = activity.pendingDailyChat(accounts, {}, now);
assert.deepEqual(pending.map((a) => a.uid), ['intl-1'], '停用的与今天已打卡的都不在名单里');

// ---- 汇总与文案
const summary = activity.todaySummary(accounts, {}, 'intl', now);
assert.equal(summary.total, 2, '停用账号不计入总数');
assert.equal(summary.done, 1);
assert.equal(summary.pending.length, 1);
assert.equal(activity.summaryText(summary, '已打卡', '未打卡'), '部分已打卡 (1/2)');
assert.equal(activity.namesOf(summary.pending).join('、'), '甲');

const allDone = activity.todaySummary([
  intl({ uid: 'a', lastDailyChat: stamp + ' 10:00:00' }),
], {}, 'intl', now);
assert.equal(activity.summaryText(allDone, '已打卡', '未打卡'), '今日已打卡 (1/1)');
assert.equal(activity.summaryText({ total: 0, done: 0, pending: [] }, '已打卡', '未打卡'), '无账号');

// 汇总卡：数值、色调与待处理名单沿用同一套口径
assert.deepEqual(activity.summaryStat(summary, { doneWord: '已打卡', pendingWord: '未打卡' }), {
  value: '部分已打卡 (1/2)', tone: 'warning', hint: '待处理：甲',
});
assert.deepEqual(activity.summaryStat(allDone, { doneWord: '已打卡', pendingWord: '未打卡' }), {
  value: '今日已打卡 (1/1)', tone: 'success', hint: '今天全部完成',
});

const streak = activity.streakSummary([
  cn({ uid: 'a', lastActivityReport: stamp + ' 09:00:00' }),
  cn({ uid: 'b' }),
], 'cn', now);
assert.deepEqual(streak, { total: 2, done: 1 });

console.log('账号今日活动断言通过');
