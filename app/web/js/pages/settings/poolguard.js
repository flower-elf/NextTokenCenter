// 账号池守卫卡片：每个渠道一张，字段按渠道覆盖值保存，读不到覆盖时显示全局默认值。
// 与 WorkBuddy、CodeArts 的账号池共用同一套字段，避免两边的限额口径不一致。
import { channelLabel } from '../../core/channels.js';
import { spaceMixed } from '../../core/format.js';
import { createFieldCard } from './fieldcard.js';
import { limitScopeValue } from './logic.js';

const GUARDS = [
  { id: 'reserve', key: 'reserve_credits', default: 0, suffix: '积分',
    label: '保留积分', desc: '余额不高于该值时暂停接单，0 表示关闭。' },
  { id: 'dailyLimit', key: 'daily_token_limit', default: 0, suffix: 'token', step: 1000,
    label: '每日 Token 限额', desc: '账号当日用量达到该值后暂停接单，0 表示不限额。' },
  { id: 'dailyCreditLimit', key: 'daily_credit_limit', default: 0, suffix: '积分',
    label: '每日积分限额', desc: '账号当日消耗积分达到该值后暂停付费模型，0 表示不限额。' },
  { id: 'modelDailyTokenLimit', key: 'model_daily_token_limit', default: 0, suffix: 'token', step: 1000,
    label: '单模型每日 Token 限额', desc: '只暂停达到限额的模型，0 表示不限额。' },
  { id: 'expiringWindow', key: 'expiring_window_days', default: 7, max: 365, suffix: '天',
    label: '临期积分优先窗口', desc: '积分在这个天数内到期的账号优先接单，0 表示关闭。' },
];

/** 一个渠道的账号池守卫卡片。realm 决定读写 limits.<key>.<realm>。 */
export function createPoolGuardCard(page, realm) {
  return createFieldCard(page, {
    icon: 'ShieldCheck',
    title: spaceMixed('账号池守卫 · ' + channelLabel(realm)),
    desc: '余额与用量达到阈值时暂停这个渠道接单，0 点或余额恢复后自动解除。',
    sections: [{
      fields: GUARDS.map((guard) => ({
        ...guard,
        key: 'limits.' + guard.key + '.' + realm,
        read: (view) => limitScopeValue(view, guard.key, realm),
      })),
    }],
  });
}
