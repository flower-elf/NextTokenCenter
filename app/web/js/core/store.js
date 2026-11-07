import { getJSON, postJSON } from './api.js';
import { setChannels } from './channels.js';

// 跨页面共享的状态；页面用 subscribe 监听变化，变化原因写在 reason 里。
// 渠道相关字段在 initRealms 读到 GET /realm 之后才有值，外壳在那之后才渲染。
const state = {
  viewRealm: '',     // 页面正在查看的渠道（右上角切换）
  activeRoute: [],   // 网关默认路由：参与混合调度的渠道集合，没有绑定路由的 Key 在这些渠道之间调度
  realms: [],        // 可选渠道，来自 GET /realm 的 options
  panelStatus: {},   // GET /panel/status
  version: '',
};

/** 写入 GET /realm 的渠道声明与可选渠道。 */
function adoptRealms(data) {
  setChannels(data.channels);
  state.realms = (data.options || []).slice();
  if (!state.realms.length) throw new Error('GET /realm returned no channels');
}

/** 写入 GET /realm 的默认路由；只保留可用渠道，顺序保持不变。 */
function adoptRoute(route) {
  state.activeRoute = (Array.isArray(route) ? route : []).filter((id) => state.realms.includes(id));
}

/** 页面默认查看的渠道：默认路由的第一项，路由为空时用第一个可选渠道。 */
function defaultView() {
  return state.activeRoute[0] || state.realms[0];
}

const listeners = new Set();

export function getState() {
  return state;
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit(reason) {
  for (const fn of [...listeners]) fn(reason, state);
}

export function setViewRealm(realm) {
  if (!state.realms.includes(realm)) throw new Error('unknown realm: ' + realm);
  if (state.viewRealm === realm) return;
  state.viewRealm = realm;
  const url = new URL(location.href);
  url.searchParams.set('view', realm);
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  emit('viewRealm');
}

/** 只刷新网关默认路由，不改动正在查看的渠道。 */
export async function refreshActiveRoute() {
  const data = await getJSON('/realm');
  const previousRealms = state.realms.join('|');
  const previousRoute = state.activeRoute.join('|');
  adoptRealms(data);
  adoptRoute(data.route);
  if (previousRoute !== state.activeRoute.join('|')) emit('activeRoute');
  const changed = previousRealms !== state.realms.join('|');
  if (!state.realms.includes(state.viewRealm)) {
    state.viewRealm = defaultView();
    emit('viewRealm');
  } else if (changed) {
    emit('realms');
  }
  return state.activeRoute.slice();
}

export async function switchActiveRoute(route) {
  const list = Array.isArray(route) ? route.slice() : [];
  const data = await postJSON('/realm', { route: list });
  state.activeRoute = Array.isArray(data.route) ? data.route.slice() : list;
  emit('activeRoute');
  return state.activeRoute.slice();
}

/** 启动时调用一次：查看的渠道优先取地址栏 ?view=，否则取默认路由的第一个渠道。 */
export async function initRealms() {
  const data = await getJSON('/realm');
  adoptRealms(data);
  adoptRoute(data.route);
  const pinned = new URLSearchParams(location.search).get('view');
  state.viewRealm = state.realms.includes(pinned) ? pinned : defaultView();
  if (!state.realms.includes(state.viewRealm)) state.viewRealm = state.realms[0];
  emit('init');
}

export function setPanelStatus(status) {
  state.panelStatus = status || {};
  emit('panelStatus');
}

export function setVersion(version) {
  state.version = version || '';
  emit('version');
}
