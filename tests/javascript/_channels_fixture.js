// 看板纯逻辑测试用的渠道声明：形状与 GET /realm 的 channels 相同，导入即写入登记表。
import { setChannels } from '../../app/web/js/core/channels.js';

export const CHANNELS = [
  { id: 'intl', label: '国际版', host: 'www.workbuddy.ai', icon: 'Globe', tone: 'sky', provider: 'workbuddy',
    features: { checkin: '', dailyChat: true, testModel: true, media: true } },
  { id: 'cn', label: '国内版', host: 'www.workbuddy.cn', icon: 'House', tone: 'warning', provider: 'workbuddy',
    features: { checkin: 'buddy', dailyChat: false, testModel: true, media: true } },
  { id: 'codearts', label: 'CodeArts', host: '', icon: 'Cloud', tone: 'violet', provider: 'codearts',
    features: { checkin: 'codearts', dailyChat: false, testModel: false, media: false } },
];

setChannels(CHANNELS);
