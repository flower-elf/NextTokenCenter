// 设置 → WorkBuddy：只对 WorkBuddy 账号池（国际版与国内版）生效的设置。
import { getJSON } from '../../core/api.js';
import { getState } from '../../core/store.js';
import { channelLabel, channelProvider, channelsWith, channelTitle } from '../../core/channels.js';
import { identityOptions, testModelOptions, testModelSelected, validateTestModel } from './logic.js';
import { cardsTab, createFieldCard } from './fieldcard.js';
import { createPoolGuardCard } from './poolguard.js';

const PROMPT_MODES = [
  { value: 'passthrough', label: '原样透传' },
  { value: 'custom', label: '使用自定义提示词文件' },
  { value: 'append', label: '在请求末尾追加提示词文件' },
];
/** WorkBuddy 提供方下带某项功能的渠道。 */
function workbuddyChannels(feature) {
  return channelsWith(getState().realms, feature).filter((id) => channelProvider(id) === 'workbuddy');
}

export function createWorkbuddyTab(page) {
  const catalogs = {};
  const realms = getState().realms.filter((id) => channelProvider(id) === 'workbuddy');
  const guards = realms.map((realm) => createPoolGuardCard(page, realm));

  const pool = createFieldCard(page, {
    icon: 'Users',
    title: '账号池',
    desc: '国际版与国内版共用的定时任务与失败处理。',
    sections: [
      { fields: [
        { id: 'tasksAuto', key: 'workbuddy_tasks_auto', kind: 'switch', default: true,
          label: '定时任务', desc: '关闭后定时任务不再执行，手动操作不受影响。' },
      ] },
      { label: '失败处理', fields: [
        { id: 'autoSwitch', key: 'auto_switch_product', kind: 'switch', default: false,
          label: '429 自动切换出站身份', desc: '收到上游 429 时切换出站身份后重试。' },
        { id: 'freeFallback', key: 'free_model_credit_fallback', kind: 'switch', default: false, env: 'free_model_credit_fallback',
          label: '免费模型积分回退', desc: '免费额度耗尽时改用同系列付费模型，会消耗积分。' },
        { id: 'egressBackoff', key: 'egress_backoff_enabled', kind: 'switch', default: false,
          label: '出站连接失败退避', desc: '传输失败与 502/503/504 按出站连接退避，不计入账号熔断。' },
      ] },
    ],
  });

  const daily = createFieldCard(page, {
    icon: 'CalendarCheck',
    title: '每日活跃',
    desc: '带每日活跃的渠道打卡方式。',
    sections: [{ fields: [
      { id: 'dailyWeb', key: 'daily_chat_web', kind: 'switch', default: true,
        label: '网页通道打卡', desc: '发起一次网页端对话计入官方每日活跃，会消耗少量积分。' },
      { id: 'dailyWebRetry', key: 'daily_chat_web_retry', kind: 'switch', default: true,
        label: '失败后重试', desc: '一轮失败时重新创建会话再试。' },
      { id: 'dailyWebAttempts', key: 'daily_chat_web_attempts', kind: 'int', default: 3, min: 1, max: (view) => Number(view.daily_chat_web_attempts_max) || 5, suffix: '次',
        label: '尝试次数', desc: '一轮最多尝试几次。' },
      { id: 'dailyWebTimeout', key: 'daily_chat_web_timeout', kind: 'int', default: 120, min: 1, max: (view) => Number(view.daily_chat_web_timeout_max) || 3600, suffix: '秒',
        label: '单轮超时', desc: '一轮最多等待多少秒。' },
      { id: 'dailyChatModel', key: 'daily_chat_model', kind: 'text', placeholder: '留空使用内置模型',
        validate: validateTestModel, label: '打卡模型', desc: '网页通道打卡使用的模型。' },
    ] }],
  });

  const upstream = createFieldCard(page, {
    icon: 'Server',
    title: '上游请求',
    desc: '发往 WorkBuddy 上游的请求参数。',
    sections: [
      { fields: [
        { id: 'header', key: 'upstream.header_timeout_seconds', kind: 'int', default: 120, min: 1, max: 86400, suffix: '秒',
          label: '响应头超时', desc: '等待上游响应头的最长时间。' },
        { id: 'idle', key: 'upstream.idle_timeout_seconds', kind: 'int', default: 300, min: 1, max: 86400, suffix: '秒',
          label: '流式空闲超时', desc: '两次数据之间的最长空闲时间。' },
        { id: 'keepalive', key: 'upstream_keepalive', kind: 'switch', default: true, env: 'upstream_keepalive',
          label: '连接复用', desc: '复用上游 TCP/TLS 连接。' },
        { id: 'deviceToken', key: 'upstream.device_token', kind: 'text', placeholder: '可留空',
          label: '设备令牌', desc: '出站请求附带的设备令牌。' },
        { id: 'deviceTokenFile', key: 'upstream.device_token_file', kind: 'text', placeholder: '可留空',
          label: '设备令牌文件', desc: '从文件读取设备令牌，优先于上一项。' },
      ] },
      { label: '请求内容', fields: [
        { id: 'promptMode', key: 'prompt.mode', kind: 'select', default: 'passthrough', options: PROMPT_MODES,
          label: '提示词模式', desc: '客户端没有 system 消息时如何处理网关提示词。' },
        { id: 'promptFile', key: 'prompt.file', kind: 'text', placeholder: '可留空',
          label: '提示词文件', desc: '自定义或追加模式使用的文件路径。' },
        { id: 'dsh', key: 'dsh_tool_neutralization', kind: 'switch', default: false, env: 'dsh_tool_neutralization',
          label: 'DSH 工具名中性化', desc: '把 DSH 工具调用名换成通用名称。' },
        { id: 'promptCacheKey', key: 'prompt_cache_key', kind: 'switch', default: false, env: 'prompt_cache_key',
          label: 'prompt_cache_key', desc: '向上游附带 prompt_cache_key。' },
      ] },
    ],
  });

  const testModels = createFieldCard(page, {
    icon: 'Zap',
    title: '接口测试模型',
    desc: '账号行「测试」按钮使用的模型，默认由网关按渠道选择。',
    sections: [{ fields: workbuddyChannels('testModel').map((realm) => ({
      id: 'test_' + realm, key: 'test_model_' + realm, kind: 'select', default: '',
      validate: validateTestModel,
      // 服务端的取值是「生效值」，没自定义过时也等于默认模型。这里按服务端给的自定义标记
      // 回到空值，下拉才会选中「默认」，而不是把一个与默认相同的模型当成自定义。
      read: (view) => {
        const custom = !!(view.test_model_is_custom && view.test_model_is_custom[realm]);
        return custom ? testModelSelected(view['test_model_' + realm]) : '';
      },
      options: (current) => testModelOptions(catalogs[realm] || [], current).map(({ value, label }) => ({ value, label })),
      label: channelLabel(realm), desc: channelTitle(realm),
    })) }],
  });

  const media = createFieldCard(page, {
    icon: 'Image',
    title: '媒体生成',
    desc: '图片与视频生成，经 WorkBuddy 账号转发。',
    sections: [{ fields: [
      { id: 'enabled', key: 'media.enabled', kind: 'switch', default: false,
        label: '启用媒体生成', desc: '打开后提供 /v1/images/* 与 /v1/videos/*。' },
      { id: 'realm', key: 'media.realm', kind: 'select', default: 'cn',
        options: () => workbuddyChannels('media').map((id) => ({ value: id, label: channelTitle(id) })),
        label: '媒体渠道', desc: 'API Key 绑定了其它渠道时请求会被拒绝。' },
      { id: 'identity', key: 'media.identity', kind: 'select', default: 'workbuddy',
        options: (current, view) => identityOptions(view, 'workbuddy'),
        label: '出站身份', desc: '媒体端点使用的客户端身份。' },
      { id: 'imageMax', key: 'media.image_max_per_minute', kind: 'int', default: 10, min: 1, max: 120, suffix: '次',
        label: '图片生成每分钟上限', desc: '按 API Key 计算。' },
      { id: 'videoMax', key: 'media.video_max_seconds', kind: 'int', default: 60, min: 1, max: 600, suffix: '秒',
        label: '视频时长上限', desc: '超过的请求在本地拒绝。' },
      { id: 'dailyCredits', key: 'media.daily_credits_per_key', kind: 'int', default: 0, min: 0, max: 1000000, suffix: '积分',
        label: '每个 Key 每日积分上限', desc: '只为媒体请求记账，0 表示不限制。' },
      { id: 'videoConcurrency', key: 'media.video_concurrency', kind: 'int', default: 3, min: 1, max: 20, suffix: '个',
        label: '视频生成并发', desc: '多出的任务在本地排队。' },
    ] }],
  });

  async function loadCatalogs() {
    const realms = workbuddyChannels('testModel');
    if (!realms.length) return;
    const results = await Promise.allSettled(realms.map((realm) => getJSON('/v1/models?realm=' + realm)));
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        catalogs[realms[index]] = ((result.value && result.value.data) || []).map((m) => m.id).filter(Boolean);
      }
    });
    page.draw();
  }

  return cardsTab(page, {
    name: 'WorkBuddy',
    // 只有某个渠道真的声明了这项功能时才画对应的卡片，避免出现没有账号可用的设置。
    cards: () => [
      pool, ...guards,
      ...(workbuddyChannels('dailyChat').length ? [daily] : []),
      upstream,
      ...(workbuddyChannels('testModel').length ? [testModels] : []),
      ...(workbuddyChannels('media').length ? [media] : []),
    ],
    onMount: loadCatalogs,
  });
}
