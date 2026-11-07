// 渠道登记表的看板副本：内容来自 GET /realm 的 channels（后端 wb_channels.py 是唯一来源），
// 启动时由 core/store.js 写入。页面只按这里的字段分支，新增渠道不需要改看板代码。
// 纯函数，可以直接在 Node 里测试。
//
// 每个渠道：{ id, label, host, icon, tone, provider, features }
//   provider：账号来源。workbuddy 与 codearts 都通过 /accounts 与统一的账号接口管理
//   features.checkin    签到按钮的种类：'buddy' 是 Buddy 加油站，'codearts' 是 CodeArts 签到
//   features.dailyChat  每日活跃打卡（含网页通道）
//   features.testModel  设置页可以指定接口测试模型
//   features.media      可以作为媒体生成的渠道

let CHANNELS = new Map();

/** 写入后端给出的渠道声明，顺序即界面上的显示顺序。 */
export function setChannels(list) {
  CHANNELS = new Map((list || []).map((item) => [item.id, Object.freeze({
    ...item,
    features: Object.freeze({ ...(item.features || {}) }),
  })]));
}

/** 渠道的完整声明。登记表里没有的 id 也返回同形对象：名称就是 id，不声明任何功能。 */
export function channel(id) {
  const key = String(id || '');
  return CHANNELS.get(key) || {
    id: key, label: key || '—', host: '', icon: 'Server', tone: 'muted',
    provider: key, features: {},
  };
}

export function channelLabel(id) {
  return channel(id).label;
}

/** 带域名的完整名称，例如「国内版（www.workbuddy.cn）」。 */
export function channelTitle(id) {
  const meta = channel(id);
  return meta.host ? `${meta.label}（${meta.host}）` : meta.label;
}

export function channelFeature(id, name) {
  return channel(id).features[name] || false;
}

export function channelProvider(id) {
  return channel(id).provider;
}

/** 从可用渠道里挑出带某项功能的渠道，顺序保持不变。 */
export function channelsWith(ids, feature) {
  return (ids || []).filter((id) => channelFeature(id, feature));
}

/** 下拉与分段控件用的选项；extra 放在最前面，例如「全部」或「跟随面板默认路由」。 */
export function channelOptions(ids, extra = []) {
  return [...extra, ...(ids || []).map((id) => ({ id, label: channelLabel(id) }))];
}

// 混合调度的规则只有一句：会话粘性 > 优先级数字小的一层 > 层内手动权重大于 1 的账号
// > 层内临期积分 > 层内忙闲。看板各处引用这里，改规则时只改一处。
export const DISPATCH_HINT = '优先级数字小的账号先接单，同级先给权重大的账号。';

// 渠道路由是参与混合调度的渠道集合，没有先后之分：同一个模型由几个渠道提供时，网关在这些渠道的
// 全部账号里按上面的规则挑选。同一 provider 的渠道共用账号来源，互斥，一条路由里
// 最多出现一个。如下都是纯函数，返回按登记表顺序排列的新数组，不修改传入的路由。

/** 按登记表顺序排列渠道 id。 */
function sortByRegistry(list) {
  const order = [...CHANNELS.keys()];
  const rank = (id) => (order.includes(id) ? order.indexOf(id) : order.length);
  return [...list].sort((a, b) => rank(a) - rank(b));
}

/** 路由里与 id 同属一个 provider 的其它渠道。 */
export function routeConflicts(route, id) {
  return (route || []).filter((other) => other !== id && channelProvider(other) === channelProvider(id));
}

/** 选中或取消 id：已选中时移除；新选中时替换掉同 provider 的渠道。 */
export function toggleRouteChannel(route, id) {
  const list = Array.isArray(route) ? route : [];
  if (list.includes(id)) return sortByRegistry(list.filter((other) => other !== id));
  return sortByRegistry([...list.filter((other) => channelProvider(other) !== channelProvider(id)), id]);
}

/** 路由的显示文字：渠道名称用「 + 」连接；路由为空时用 followText，例如「跟随面板默认路由」。 */
export function routeText(route, followText = '') {
  const list = Array.isArray(route) ? route : [];
  if (!list.length) return followText;
  return sortByRegistry(list).map(channelLabel).join(' + ');
}
