// 按渠道的账号来源打开对应的添加账号弹窗。
import { channelProvider } from '../../../core/channels.js';

export async function openAccountDialog(realm, options = {}) {
  if (channelProvider(realm) === 'codearts') {
    const mod = await import('./codearts.js');
    return mod.openCodeartsAccountDialog(options);
  }
  const mod = await import('./add.js');
  return mod.openAddAccountDialog({ ...options, realm });
}
