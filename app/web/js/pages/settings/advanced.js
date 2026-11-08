// 设置 → 高级：与渠道无关的功能开关，保存后立即生效；存在同名环境变量时以环境变量为准。
import { cardsTab, createFieldCard } from './fieldcard.js';

export function createAdvancedTab(page) {
  const flags = createFieldCard(page, {
    icon: 'Wrench',
    title: '高级',
    desc: '存在同名环境变量时以环境变量为准。',
    sections: [
      { label: '接口', fields: [
        { id: 'anthropicMessages', key: 'enable_anthropic_messages', kind: 'switch', default: false,
          env: 'enable_anthropic_messages', label: 'Anthropic Messages 入口',
          desc: '打开 /v1/messages，长会话可能增加积分消耗。' },
        { id: 'affinityDebug', key: 'affinity_debug', kind: 'switch', default: false, env: 'affinity_debug',
          label: '亲和调试日志', desc: '把会话标识的派生过程写入运行日志。' },
      ] },
      { label: '性能与缓存', fields: [
        { id: 'usageCache', key: 'usage_cache', kind: 'switch', default: true, env: 'usage_cache',
          label: '用量聚合缓存', desc: '把用量聚合结果写入磁盘，重启后只读新增日志。' },
        { id: 'usageDayBuckets', key: 'usage_day_buckets', kind: 'switch', default: true, env: 'usage_day_buckets',
          label: '日与小时分桶', desc: '窗口统计使用分桶结果，关闭后逐行扫描。' },
        { id: 'recentIndex', key: 'recent_index', kind: 'switch', default: false, env: 'recent_index',
          label: '最近请求索引', desc: '为请求日志建立深分页索引。' },
      ] },
    ],
  });
  const performance = createFieldCard(page, {
    icon: 'Gauge',
    title: '并发与超时',
    desc: '影响请求排队、连接和请求体大小；存在同名环境变量时以环境变量为准。',
    sections: [{ fields: [
      { id: 'maxConcurrentChat', key: 'max_concurrent_chat', kind: 'text', default: '32',
        env: 'max_concurrent_chat', label: '对话并发上限',
        desc: '填写数字或 auto。auto 按可用账号数计算；降低后重启生效。' },
      { id: 'chatSlotWait', key: 'chat_slot_wait', kind: 'int', default: 30, min: 0, max: 600,
        suffix: '秒', env: 'chat_slot_wait', label: '并发排队等待',
        desc: '并发已满时最多等待多久，0 表示不等待。' },
      { id: 'clientTimeout', key: 'client_timeout', kind: 'int', default: 120, min: 1, max: 3600,
        suffix: '秒', env: 'client_timeout', label: '客户端连接超时',
        desc: '客户端连接空闲多久后关闭。' },
      { id: 'maxPayload', key: 'max_payload_mib', kind: 'int', default: 16, min: 1, max: 1024,
        suffix: 'MiB', env: 'max_payload_mib', label: '请求体上限',
        desc: '超过这个大小的请求直接返回 413。' },
    ] }],
  });

  const web = createFieldCard(page, {
    icon: 'Globe',
    title: '本地网络工具',
    desc: '搜索与抓取在网关本地执行，开关在“网关行为”。',
    sections: [{ fields: [
      { id: 'webMaxRounds', key: 'web_max_rounds', kind: 'int', default: 3, min: 1, max: 8,
        env: 'web_max_rounds', label: '最多轮数', desc: '每轮会多调用一次上游。' },
      { id: 'webCooldown', key: 'web_cooldown', kind: 'int', default: 600, min: 0, max: 86400,
        suffix: '秒', env: 'web_cooldown', label: '限流引擎冷却', desc: '0 表示不冷却。' },
      { id: 'webCacheTtl', key: 'web_cache_ttl', kind: 'int', default: 600, min: 0, max: 86400,
        suffix: '秒', env: 'web_cache_ttl', label: '搜索缓存', desc: '0 表示关闭缓存。' },
      { id: 'webMinInterval', key: 'web_min_interval', kind: 'number', default: 0.5, min: 0, max: 60,
        suffix: '秒', env: 'web_min_interval', label: '最小请求间隔', desc: '0 表示不限制。' },
      { id: 'webBudget', key: 'web_budget', kind: 'int', default: 25, min: 0, max: 600,
        suffix: '秒', env: 'web_budget', label: '单次搜索预算', desc: '0 表示不限。' },
    ] }],
  });

  const discovery = createFieldCard(page, {
    icon: 'Radar',
    title: '代理自动发现',
    desc: '“代理槽”页的自动发现按钮使用这里的主机与端口范围。',
    sections: [{ fields: [
      { id: 'discoverHost', key: 'proxy_discover_host', kind: 'text',
        default: 'cli-proxy-mihomo', env: 'proxy_discover_host', label: '发现主机' },
      { id: 'discoverPorts', key: 'proxy_discover_ports', kind: 'text',
        default: '17901-17910', env: 'proxy_discover_ports', label: '端口范围',
        desc: '写成 17901-17910 或单个端口。' },
    ] }],
  });

  return cardsTab(page, { name: '高级', cards: () => [flags, performance, web, discovery] });
}
