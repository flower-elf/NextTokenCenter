// 账号分组弹窗：只保存在本机账号文件里，留空即从分组移除。
import { html } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import * as acct from '../actions.js';

const DEFAULT_MAX_GROUP = 64;

export function openGroupDialog(account, { onDone, limits } = {}) {
  const maxGroup = Number(limits && limits.groupMax) || DEFAULT_MAX_GROUP;
  let value = String(account.group || '');
  let busy = false;
  const name = account.nickname || String(account.uid || '').slice(0, 8);

  const ctl = openDialog({
    title: '账号分组',
    desc: '给「' + name + '」设置分组名用于筛选与批量启停，留空保存即移出分组。',
    width: 400,
    body: () => html`
      <label class="field">
        <span class="field-label">分组名</span>
        <input class="input" autofocus maxlength=${maxGroup} placeholder="例如：团队" .value=${value}
               @input=${(e) => { value = e.target.value; }}>
        <span class="field-hint">最长 ${maxGroup} 字，只保存在本机。</span>
      </label>`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>取消</button>
      <button class="btn btn-primary" ?disabled=${busy} @click=${busyClick(save)}>${icon('Check')}保存分组</button>`,
  });

  async function save() {
    const text = value.trim();
    if ([...text].length > maxGroup) {
      toast.warn('分组名太长了', '分组名最长 ' + maxGroup + ' 字');
      return;
    }
    busy = true;
    ctl.update();
    try {
      await acct.saveGroup(account.uid, text);
      toast.success(text ? '已加入分组「' + text + '」' : '已移出分组', name);
      acct.notifyAccountsChanged();
      if (onDone) onDone(text);
      ctl.close();
    } catch (err) {
      toastError('保存分组失败', err);
    } finally {
      busy = false;
      ctl.update();
    }
  }

  return ctl;
}
