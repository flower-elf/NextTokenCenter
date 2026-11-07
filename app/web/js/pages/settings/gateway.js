// 设置 → 网关行为：与渠道无关的设置。渠道专属的设置在各渠道自己的标签里。
import { messagesFormatOptions, messagesFormatValue } from './logic.js';
import { cardsTab, createFieldCard } from './fieldcard.js';
import { DISPATCH_HINT } from '../../core/channels.js';

const SCHEDULE = [
  { group: 'checkin', label: '签到与活跃上报时段', fallback: [9, 21] },
  { group: 'travel', label: '猫猫旅行时段', fallback: [9, 21] },
  { group: 'keepalive', label: '保活时段', fallback: [22] },
  { group: 'cat', label: '夜猫任务时段', fallback: [1] },
];

export function createGatewayTab(page) {
  const scheduler = createFieldCard(page, {
    icon: 'Clock',
    title: '定时任务',
    desc: '签到与保活各渠道共用，猫猫旅行与夜猫任务只有国内版执行。',
    sections: [{ fields: [
      ...SCHEDULE.map(({ group, label, fallback }) => ({
        id: group, key: 'scheduler_hours.' + group, kind: 'hours', default: fallback, whole: true,
        label, desc: '填写 0 到 23 的整点，留空表示不运行。',
      })),
      { id: 'taskJitter', key: 'task_jitter_max', kind: 'int', default: 5, min: 2, max: (view) => Number(view.task_jitter_max_limit) || 600, suffix: '秒',
        label: '任务随机间隔', desc: '处理每个账号前随机等待 2 秒到这个值之间。' },
    ] }],
  });

  const routing = createFieldCard(page, {
    icon: 'Route',
    title: '号池调度',
    desc: DISPATCH_HINT,
    sections: [{ fields: [
      { id: 'sessionAffinity', key: 'session_affinity', kind: 'switch', default: true,
        label: '会话粘性', desc: '同一段对话固定使用同一个账号。' },
    ] }],
  });

  const credits = createFieldCard(page, {
    icon: 'Coins',
    title: '积分刷新',
    desc: '所有渠道共用，后台按这个间隔刷新账号余额。',
    sections: [{ fields: [
      { id: 'creditsRefresh', key: 'credits_refresh_hours', kind: 'number', default: 12, max: 720, suffix: '小时',
        label: '积分后台刷新间隔', desc: '0 表示关闭后台刷新。' },
    ] }],
  });

  const api = createFieldCard(page, {
    icon: 'Plug',
    title: '接口',
    desc: '客户端接口的翻译方式与网关代执行的工具。',
    sections: [{ fields: [
      { id: 'messagesFormat', key: 'messages_format', kind: 'select',
        read: (view) => messagesFormatValue(view.messages_format, view.messages_format_default),
        options: messagesFormatOptions('', '').map(({ value, label }) => ({ value, label })),
        label: 'Messages 接口翻译', desc: '/v1/messages 把 Anthropic 请求翻译成哪种上游格式。' },
      { id: 'localWeb', key: 'local_web_tools', kind: 'switch', default: false,
        label: '本地网络工具', desc: '网关代为执行 web_search 与 web_fetch，会主动出网。' },
    ] }],
  });

  return cardsTab(page, { name: '网关行为', cards: () => [scheduler, routing, credits, api] });
}
