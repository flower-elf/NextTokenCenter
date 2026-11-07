// 渠道路由编辑器：可选渠道各占一行，点击加入或移出路由。选中的渠道一起参与混合调度，没有先后之分；
// 同一账号来源的渠道互斥，选中一个会替换掉同账号来源的另一个。
import { html, nothing, classMap } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { spaceMixed } from '../core/format.js';
import { getState } from '../core/store.js';
import {
  channelLabel,
  channelTitle,
  routeConflicts,
  toggleRouteChannel,
} from '../core/channels.js';

/**
 * routeEditor({ route, onChange, realms?, label? })。
 * route 是渠道 id 数组；onChange(next) 收到调整后的新数组，由调用方保存并重新绘制。
 */
export function routeEditor({ route = [], onChange, realms = getState().realms, label = '渠道路由' } = {}) {
  const current = Array.isArray(route) ? route : [];
  const rows = (realms || []).map((id) => {
    const selected = current.includes(id);
    const conflicts = selected ? [] : routeConflicts(current, id).map(channelLabel);
    const title = spaceMixed(channelTitle(id) + (conflicts.length ? '；与已选的“' + conflicts.join('、') + '”同一账号来源，只能选一个' : ''));
    return html`
      <div class=${classMap({ 'route-row': true, selected })}>
        <button type="button" role="checkbox" aria-checked=${selected ? 'true' : 'false'}
                class="route-pick" title=${title}
                @click=${() => onChange(toggleRouteChannel(current, id))}>
          <span class="route-check">${selected ? icon('Check') : nothing}</span>
          <span class="route-name">${channelLabel(id)}</span>
        </button>
      </div>`;
  });
  return html`
    <div class="route-editor" role="group" aria-label=${label}>
      ${rows}
      <span class="field-hint">同一账号来源的渠道只能选一个。</span>
    </div>`;
}
