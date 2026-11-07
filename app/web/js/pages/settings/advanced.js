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
  return cardsTab(page, { name: '高级', cards: () => [flags] });
}
