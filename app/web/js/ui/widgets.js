import { html, nothing, classMap, ref } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { channel, channelTitle } from '../core/channels.js';
import { sectionOf } from '../core/routes.js';
import { hrefFor } from '../core/router.js';
import { copyWithToast } from './copy.js';

// 各页面共用的模板片段，全部返回 lit-html 模板。

/** 页头；所在页面属于某个二级导航组时，标题下方显示该组的切换。 */
export function pageHeader({ path, title, desc, actions = nothing }) {
  const section = path ? sectionOf(path) : [];
  return html`
    <div class="page-head">
      <div class="col page-head-main">
        <div>
          <h1 class="page-title">${title}</h1>
          ${desc ? html`<p class="page-desc">${desc}</p>` : nothing}
        </div>
        ${section.length > 1 ? sectionTabs(section.map((r) => ({ id: r.path, label: r.title, icon: r.icon, href: hrefFor(r.path) })), path) : nothing}
      </div>
      <div class="page-actions">${actions}</div>
    </div>`;
}

/** 胶囊式二级导航（链接）。items: [{ id, label, icon, href }] */
export function sectionTabs(items, activeId) {
  return html`
    <nav class="seg" aria-label="页内导航" ${ref((nav) => revealActiveTab(nav, activeId))}>
      ${items.map((item) => html`
        <a class=${classMap({ 'seg-item': true, active: item.id === activeId })} href=${item.href}
           aria-current=${item.id === activeId ? 'page' : 'false'}>
          ${item.icon ? icon(item.icon) : nothing}${item.label}
        </a>`)}
    </nav>`;
}

// 标签条在窄屏上横向滚动：挂载时把当前标签滚到中间，打开深层标签不会看不见选中项。
// ref 回调在子节点提交前触发，放到微任务里再查当前标签；WeakMap 记住每个标签条已经
// 滚动过的标签，定时重绘时不会跟用户抢滚动位置。
const revealedTabs = new WeakMap();

function revealActiveTab(nav, activeId) {
  if (!nav) return;
  queueMicrotask(() => {
    if (revealedTabs.get(nav) === activeId) return;
    const active = nav.querySelector('.seg-item.active');
    if (!active) return;
    revealedTabs.set(nav, activeId);
    active.scrollIntoView({ block: 'nearest', inline: 'center' });
  });
}

/** 胶囊式分段选择（按钮）。items: [{ id, label, count? }]；onSelect(id)。 */
export function segmented(items, activeId, onSelect, extraClass = '') {
  return html`
    <div class="seg ${extraClass}" role="tablist" ${ref((el) => revealActiveTab(el, activeId))}>
      ${items.map((item) => html`
        <button type="button" role="tab" class=${classMap({ 'seg-item': true, active: item.id === activeId })}
                aria-selected=${item.id === activeId ? 'true' : 'false'} @click=${() => onSelect(item.id)}>
          ${item.icon ? icon(item.icon) : nothing}${item.label}
          ${item.count !== undefined ? html`<span class="nums count">${item.count}</span>` : nothing}
        </button>`)}
    </div>`;
}

/** 筛选芯片；选中为实心按钮，未选为描边按钮。 */
export function chip(label, active, onClick, count) {
  return html`
    <button type="button" class="btn btn-xs ${active ? 'btn-primary' : 'btn-outline'}" @click=${onClick}>
      ${label}${count !== undefined ? html`<span class="count">${count}</span>` : nothing}
    </button>`;
}

/** 统计卡。tone：success / warning / danger / info / violet，缺省为中性。 */
export function statCard({ label, value, icon: iconName, tone = '', hint = nothing, hintTone = '', delay = 0 }) {
  return html`
    <div class="stat" style="animation-delay:${delay}s">
      <div class="stat-top">
        <span class="stat-label">${label}</span>
        ${iconName ? html`<span class="stat-icon ${tone ? 'tone-' + tone : ''}">${icon(iconName)}</span>` : nothing}
      </div>
      <div class="stat-value ${tone ? 'tone-' + tone : ''}">${value}</div>
      ${hint !== nothing && hint !== '' && hint !== undefined ? html`<div class="stat-hint ${hintTone ? 'tone-' + hintTone : ''}">${hint}</div>` : nothing}
    </div>`;
}

export function emptyState({ icon: iconName = 'Inbox', title, desc = '', action = nothing }) {
  return html`
    <div class="empty">
      <div class="empty-icon">${icon(iconName)}</div>
      <div class="empty-title">${title}</div>
      ${desc ? html`<div class="empty-desc">${desc}</div>` : nothing}
      ${action}
    </div>`;
}

/** 首屏加载失败：整块替换页面内容。 */
export function loadErrorPage(message, onRetry) {
  return html`
    <div class="load-error" role="alert">
      <div class="load-error-icon">${icon('TriangleAlert')}</div>
      <div class="text-sm medium">${message}</div>
      <div class="text-xs muted" style="max-width:420px">可以先点击「重试」；如果反复出现，请查看「运行日志」页里的报错。</div>
      ${onRetry ? html`<button class="btn btn-outline btn-sm" @click=${onRetry}>${icon('RefreshCw')}重试</button>` : nothing}
    </div>`;
}

/** 部分数据刷新失败：在页面顶部显示一条，不影响已经显示的内容。 */
export function loadErrorInline(message, onRetry) {
  return html`
    <div class="notice notice-warn" role="status">
      ${icon('TriangleAlert')}<span class="grow">${message}</span>
      ${onRetry ? html`<button class="btn btn-ghost" @click=${onRetry}>${icon('RefreshCw')}重试</button>` : nothing}
    </div>`;
}

/** 提示条；kind 取 warn 或 info。 */
export function notice(kind, content, iconName) {
  const icons = { warn: 'TriangleAlert', info: 'Info' };
  return html`<div class="notice notice-${kind}">${icon(iconName || icons[kind])}<div class="grow">${content}</div></div>`;
}

export function skeletonBlock(height = 96, radius = 20) {
  return html`<span class="skel" style="height:${height}px;border-radius:${radius}px"></span>`;
}

export function skeletonStats(count) {
  return html`<div class="grid-stats cols-${count}">${Array.from({ length: count }, () => skeletonBlock(96))}</div>`;
}

export function skeletonRows(count = 5) {
  return html`<div class="card col" style="gap:12px">${Array.from({ length: count }, () => html`<span class="skel" style="height:20px"></span>`)}</div>`;
}

/** 徽章。tone：success / warning / danger / severe / info / violet / sky / muted。 */
export function badge(text, tone = 'muted', { title = '', small = false, iconName = '' } = {}) {
  return html`<span class="badge ${small ? 'badge-sm' : ''} tone-${tone}" title=${title || nothing}>${iconName ? icon(iconName) : nothing}${text}</span>`;
}

/** 渠道徽章：名称与色调来自渠道登记表。 */
export function channelBadge(id) {
  const meta = channel(id);
  return badge(meta.label, meta.tone, { small: true, title: channelTitle(id) });
}

/** 开关。onChange(checked)。 */
export function switchControl({ checked, disabled = false, onChange, label = '' }) {
  return html`
    <label class="switch" title=${label || nothing}>
      <input type="checkbox" .checked=${!!checked} ?disabled=${disabled} aria-label=${label || nothing}
             @change=${(e) => onChange(e.target.checked)}>
      <span></span>
    </label>`;
}

/** 分页条：共 N unit · 第 p / P 页。numbers 传页码窗口（省略项写作 '...'）时多一排页码按钮。 */
export function pager({ page, pages, total, unit = '条', onPage, numbers = [] }) {
  return html`
    <div class="pager">
      <span>共 ${total} ${unit} · 第 ${page} / ${pages} 页</span>
      <span class="pager-buttons">
        <button class="btn btn-outline btn-icon btn-xs" aria-label="上一页" ?disabled=${page <= 1} @click=${() => onPage(page - 1)}>${icon('ChevronLeft')}</button>
        ${numbers.map((item) => item === '...'
          ? html`<span class="pager-gap">…</span>`
          : html`<button class="btn btn-xs ${item === page ? 'btn-primary' : 'btn-outline'}"
                        aria-label=${'第 ' + item + ' 页'} @click=${() => onPage(item)}>${item}</button>`)}
        <button class="btn btn-outline btn-icon btn-xs" aria-label="下一页" ?disabled=${page >= pages} @click=${() => onPage(page + 1)}>${icon('ChevronRight')}</button>
      </span>
    </div>`;
}

/** 复制按钮；text 可以是字符串或返回字符串的异步函数。 */
export function copyButton(text, { label = '', title = '复制', toastText } = {}) {
  return html`
    <button class="btn btn-ghost ${label ? 'btn-sm' : 'btn-icon btn-xs'}" title=${title}
            @click=${(e) => { e.stopPropagation(); copyWithToast(text, toastText); }}>
      ${icon('Copy')}${label}
    </button>`;
}

/** 进度条，pct 取 0–100。 */
export function progressBar(pct, color) {
  const width = Math.max(0, Math.min(100, Number(pct) || 0));
  return html`<div class="bar"><span style="width:${width}%;${color ? 'background:' + color : ''}"></span></div>`;
}

/** 异步操作期间按钮显示转圈并禁用，用法：@click=${busyClick(fn)}。 */
export function busyClick(fn) {
  return async (event) => {
    const button = event.currentTarget;
    if (button.disabled) return;
    button.disabled = true;
    const svg = button.querySelector('svg.lucide');
    if (svg) svg.classList.add('spin');
    try {
      await fn(event);
    } finally {
      button.disabled = false;
      if (svg) svg.classList.remove('spin');
    }
  };
}
