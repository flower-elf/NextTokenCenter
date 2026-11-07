// 设置 → CodeArts：只对 CodeArts 账号池生效的设置。签到开关与重试参数放在同一张卡片，
// 与其他渠道的定时任务保持同一套称呼。
import { getJSON } from '../../core/api.js';
import { getState } from '../../core/store.js';
import { channelLabel, channelProvider } from '../../core/channels.js';
import { identityOptions, testModelOptions, validateTestModel } from './logic.js';
import { cardsTab, createFieldCard } from './fieldcard.js';
import { createPoolGuardCard } from './poolguard.js';

export function createCodeartsTab(page) {
  const realms = getState().realms.filter((id) => channelProvider(id) === 'codearts');
  const guards = realms.map((realm) => createPoolGuardCard(page, realm));
  const catalog = [];

  const pool = createFieldCard(page, {
    icon: 'Cloud',
    title: '账号池',
    desc: 'CodeArts 渠道的默认出站身份。',
    sections: [{ fields: [
      { id: 'clientType', key: 'codearts.client_type', kind: 'select', default: 'ide',
        options: (current, view) => identityOptions(view, 'codearts'),
        label: '默认出站身份', desc: '新账号默认使用的出站身份。' },
    ] }],
  });

  const request = createFieldCard(page, {
    icon: 'Server',
    title: '上游请求',
    desc: '发往 CodeArts 上游的请求参数。',
    sections: [{ fields: [
      { id: 'streamTimeout', key: 'codearts.stream_timeout_seconds', kind: 'int', default: 600, min: 1, max: 3600, suffix: '秒',
        label: '流式超时', desc: '等待上游流式数据的最长间隔，超过即超时。' },
      { id: 'modelsTtl', key: 'codearts.models_ttl_seconds', kind: 'int', default: 300, min: 60, max: 86400, suffix: '秒',
        label: '模型目录缓存', desc: '模型目录在这个时长内复用上次结果。' },
      { id: 'benefitAutoClaim', key: 'codearts.benefit_auto_claim', kind: 'switch', default: true,
        label: '自动领取福利模型', desc: '获取模型目录时顺带领取福利模型。' },
      { id: 'chatEncryption', key: 'codearts.chat_encryption', kind: 'switch', default: true,
        label: '请求加密', desc: '按官方客户端加密聊天正文，关闭后发送明文。' },
    ] }],
  });

  const testModels = createFieldCard(page, {
    icon: 'Zap',
    title: '接口测试模型',
    desc: '账号行「测试」按钮发送的模型，默认使用账号自己的模型。',
    sections: [{ fields: [
      { id: 'testModel', key: 'codearts.test_model', kind: 'select', default: '',
        validate: validateTestModel,
        options: (current) => testModelOptions(catalog, current)
          .map(({ value, label }) => ({ value, label })),
        label: channelLabel('codearts'), desc: '默认使用账号自己的模型。' },
    ] }],
  });

  const checkin = createFieldCard(page, {
    icon: 'CalendarCheck',
    title: '自动签到',
    desc: '每天按「网关行为 → 定时任务」的时段签到领取积分。',
    sections: [{ fields: [
      { id: 'tasksAuto', key: 'codearts.credits_auto_claim', kind: 'switch', default: false,
        label: '定时任务', desc: '关闭后不再自动签到，手动签到不受影响。' },
      { id: 'attempts', key: 'codearts.checkin_max_attempts', kind: 'int', default: 3, min: 1, max: 10, suffix: '次',
        label: '尝试次数', desc: '自动签到一轮最多尝试几次。' },
      { id: 'retryGap', key: 'codearts.checkin_retry_gap_seconds', kind: 'int', default: 3600, min: 0, max: 86400, suffix: '秒',
        label: '重试间隔', desc: '两次签到尝试之间至少间隔多少秒。' },
      { id: 'timeout', key: 'codearts.checkin_timeout_seconds', kind: 'int', default: 30, min: 1, max: 600, suffix: '秒',
        label: '单次超时', desc: '单次签到请求最多等待多少秒。' },
    ] }],
  });

  async function loadCatalog() {
    try {
      const data = await getJSON('/v1/models?realm=codearts');
      catalog.length = 0;
      for (const item of (data && data.data) || []) {
        if (item && item.id) catalog.push(item.id);
      }
    } catch (err) {
      catalog.length = 0;
    }
    page.draw();
  }

  return cardsTab(page, {
    name: 'CodeArts',
    cards: () => [pool, ...guards, request, testModels, checkin],
    onMount: loadCatalog,
  });
}
