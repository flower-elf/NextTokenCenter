// 添加账号弹窗：腾讯 OAuth 授权与 API Key 两种方式，只用于账号来源是 workbuddy 的渠道；按渠道分流见 open.js。
import { html, nothing } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { getState } from '../../../core/store.js';
import { channelLabel, channelProvider, channelTitle } from '../../../core/channels.js';
import { spaceMixed } from '../../../core/format.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick, notice, segmented } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import { copyWithToast } from '../../../ui/copy.js';
import * as acct from '../actions.js';
import { PRODUCTS, productLabel } from '../logic.js';
import { qrMarkup } from './qr.js';

/** 走腾讯登录的渠道：可用渠道里账号来源是 workbuddy 的那些。 */
function loginRealms() {
  return getState().realms.filter((id) => channelProvider(id) === 'workbuddy');
}

/**
 * 添加账号 (OAuth)：默认选中当前查看的渠道与 WorkBuddy 桌面端，链接生成后展示二维码与可复制链接，
 * 每 2 秒轮询一次登录状态，关闭弹窗时通知服务端取消本次登录。登录所用的客户端就是新账号的出站身份。
 */
export function openAddAccountDialog(options = {}) {
  const realms = loginRealms();
  const initial = [options.realm, getState().viewRealm].find((id) => realms.includes(id)) || realms[0];
  let realm = initial;
  let product = 'workbuddy';
  let proxySlot = '';
  let proxySlots = [];
  let mode = options.mode === 'apikey' ? 'apikey' : 'oauth';
  let apiKey = '';
  let apiNickname = '';
  let apiBusy = false;
  let apiError = '';
  let phase = 'idle';        // idle | starting | waiting | ok | failed
  let state = '';
  let authUrl = '';
  let loginDomain = '';
  let message = '';
  let failures = 0;
  let timer = null;
  let closed = false;

  const ctl = openDialog({
    title: () => (mode === 'apikey' ? '添加 API Key 账号' : '添加账号'),
    desc: () => (mode === 'apikey'
      ? '粘贴企业版控制台签发的静态 Key，添加前会验证企业额度。'
      : '先选择渠道和客户端，再打开链接或用手机扫码完成授权。'),
    width: 512,
    body: () => html`
      <div class="realm-choice">
        <span class="field-label">添加方式</span>
        ${segmented([
          { id: 'oauth', label: 'OAuth 授权' },
          { id: 'apikey', label: 'API Key' },
        ], mode, switchMode)}
      </div>
      ${mode === 'apikey' ? html`
        <label class="field">
          <span class="field-label">API Key</span>
          <input class="input" type="password" autocomplete="off" spellcheck="false"
                 placeholder="ck_..." .value=${apiKey}
                 @input=${(event) => { apiKey = event.target.value; }}>
          <span class="field-hint">企业版 CodeBuddy 控制台签发，只适用于国内版。</span>
        </label>
        <label class="field">
          <span class="field-label">备注名（可选）</span>
          <input class="input" maxlength="100" placeholder="例如：设计团队" .value=${apiNickname}
                 @input=${(event) => { apiNickname = event.target.value; }}>
        </label>
        ${apiError ? notice('warn', apiError) : nothing}
        <p class="text-2xs muted">验证通过后出站身份默认 CLI，可在账号行切换。</p>
      ` : html`
      <div class="realm-choice">
        <span class="field-label">登录到哪个渠道</span>
        ${segmented(realms.map((id) => ({ id, label: spaceMixed(channelTitle(id)) })), realm, switchRealm)}
      </div>
      <div class="realm-choice">
        <span class="field-label">以哪个官方客户端登录</span>
        ${segmented(PRODUCTS.map((item) => ({ id: item.id, label: item.short })), product, switchProduct)}
        <span class="text-2xs muted">${productLabel(product)}</span>
      </div>
      ${proxySlots.length ? html`
        <div class="realm-choice">
          <span class="field-label">登录使用的代理槽</span>
          <select class="select" .value=${proxySlot}
                  @change=${(event) => { proxySlot = event.target.value; }}>
            <option value="">直连（不走代理槽）</option>
            <option value="auto">自动（挑一个空闲槽）</option>
            ${proxySlots.filter((slot) => slot.enabled !== false).map((slot) => html`
              <option value=${slot.id}>${slot.name || slot.id} · ${slot.ip || slot.url}</option>`)}
          </select>
          <span class="text-2xs muted">授权与拉取账号资料都走这个代理槽，登录后账号绑定到该槽。</span>
        </div>` : nothing}
      <div class="step-list">
        <div class="step ${stepClass(1)}">
          <span class="step-n">1</span>
          <div class="step-t">${stepOneText()}</div>
        </div>
        <div class="step ${stepClass(2)}">
          <span class="step-n">2</span>
          <div class="step-t">用浏览器打开链接或扫码，登录并同意授权</div>
        </div>
        ${authUrl ? html`
          <div class="qr-panel">
            ${qrMarkup(authUrl) ? html`<div class="qr-image">${qrMarkup(authUrl)}</div>` : nothing}
            <div class="qr-side">
              <div class="field-label">授权链接</div>
              <div class="link-box" title=${authUrl}>${authUrl}</div>
              <div class="row-wrap">
                <button class="btn btn-outline btn-sm" @click=${() => copyWithToast(authUrl, '授权链接已复制')}>${icon('Copy')}复制链接</button>
                <a class="btn btn-outline btn-sm" href=${authUrl} target="_blank" rel="noopener">${icon('ExternalLink')}打开链接</a>
              </div>
              <div class="text-2xs muted">手机号也可以登录，按浏览器提示操作。</div>
            </div>
          </div>` : nothing}
        <div class="step ${stepClass(3)}">
          <span class="step-n">3</span>
          <div class="step-t">等待授权回调，本弹窗每 2 秒自动检测一次</div>
        </div>
        ${message ? html`<p class="step-message ${phase === 'failed' ? 'tone-warning' : 'muted'}">${message}</p>` : nothing}
      </div>`}`,
    foot: () => (mode === 'apikey' ? html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-primary" ?disabled=${apiBusy} @click=${busyClick(commitApiKey)}>
        ${apiBusy ? icon('LoaderCircle', 'spin') : icon('KeyRound')}验证并添加
      </button>` : html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-primary" ?disabled=${phase === 'starting'} @click=${busyClick(() => startLogin())}>
        ${phase === 'starting' ? icon('LoaderCircle', 'spin') : icon('RefreshCw')}重新生成链接
      </button>`),
    // 弹窗一关就停止轮询并通知服务端取消这次登录，避免留下没人认领的登录窗口。
    onClose: () => {
      closed = true;
      stopPolling();
      void cancelCurrent();
    },
  });

  acct.loadProxySlots().then((payload) => {
    proxySlots = (payload && payload.slots) || [];
    if (!proxySlots.some((slot) => slot.id === proxySlot)) proxySlot = '';
    ctl.update();
  }).catch(() => {});

  function stepClass(index) {
    if (index === 1) return phase === 'starting' ? 'active' : phase === 'idle' ? 'active' : 'done';
    if (index === 2) return phase === 'waiting' ? 'active' : phase === 'ok' || phase === 'failed' ? 'done' : '';
    return phase === 'ok' ? 'done' : phase === 'waiting' || phase === 'failed' ? 'active' : '';
  }

  function stepOneText() {
    if (phase === 'starting') return spaceMixed('正在向' + channelLabel(realm) + '申请授权链接');
    if (phase === 'failed') return '申请授权链接失败，请点「重新生成链接」再试';
    if (loginDomain) {
      return spaceMixed('已向' + channelLabel(realm) + '（' + loginDomain + '）申请授权链接');
    }
    return spaceMixed('已向' + channelLabel(realm) + '申请授权链接');
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  // 关闭弹窗或切换渠道时通知服务端放弃这次登录；失败要提示，否则服务端会一直保留这个登录窗口。
  function cancelCurrent() {
    if (!state) return Promise.resolve();
    const pending = state;
    state = '';
    return acct.cancelLogin(pending).catch((err) => toastError('取消登录状态失败', err));
  }

  async function switchRealm(next) {
    if (next === realm) return;
    realm = next;
    await startLogin();
  }

  async function switchProduct(next) {
    if (next === product) return;
    product = next;
    await startLogin();
  }

  async function startLogin() {
    stopPolling();
    await cancelCurrent();
    authUrl = '';
    loginDomain = '';
    message = '';
    failures = 0;
    phase = 'starting';
    ctl.update();
    try {
      const started = await acct.startLogin(realm, product, proxySlot);
      state = started.state;
      authUrl = started.authUrl;
      loginDomain = started.domain || '';
      phase = 'waiting';
      message = '正在等待授权完成，完成后会自动加入账号池。';
      ctl.update();
      timer = setInterval(poll, 2000);
    } catch (err) {
      phase = 'failed';
      message = '发起失败：' + err.message;
      ctl.update();
    }
  }

  async function poll() {
    if (!state || closed) return;
    try {
      const result = await acct.pollLogin(state);
      failures = 0;
      message = result.message || result.status || '';
      if (result.status === 'ok') {
        stopPolling();
        state = '';
        phase = 'ok';
        const nickname = (result.account && result.account.nickname) || '新账号';
        message = '登录成功：' + nickname + ' 已加入账号池。';
        ctl.update();
        toast.success('账号已加入账号池', nickname);
        acct.notifyAccountsChanged();
        setTimeout(() => { if (!closed) ctl.close(); }, 1500);
        return;
      }
      if (result.status === 'error' || result.status === 'expired' || result.status === 'unknown') {
        stopPolling();
        phase = 'failed';
      }
      ctl.update();
    } catch (err) {
      // 单次轮询失败多为网络抖动，连续三次失败才提示。
      failures += 1;
      if (failures >= 3) {
        message = '轮询暂时失败，仍在重试：' + err.message;
        ctl.update();
      }
    }
  }

  async function switchMode(next) {
    if (next === mode) return;
    mode = next;
    apiError = '';
    ctl.update();
    if (mode === 'apikey') {
      stopPolling();
      await cancelCurrent();
      phase = 'idle';
      authUrl = '';
      loginDomain = '';
      message = '';
      ctl.update();
      return;
    }
    await startLogin();
  }

  async function commitApiKey() {
    const key = apiKey.trim();
    if (!key) {
      apiError = '请先填入 API Key';
      ctl.update();
      return;
    }
    apiBusy = true;
    apiError = '';
    ctl.update();
    try {
      const result = await acct.addApiKey(key, apiNickname.trim());
      const probe = (result && result.probe) || {};
      const usage = probe.used != null
        ? '，已用 ' + probe.used + (probe.limit != null ? ' / ' + probe.limit : '')
        : '';
      toast.success('API Key 账号已加入账号池',
        (result && result.nickname ? result.nickname : '新账号') + usage);
      acct.notifyAccountsChanged();
      apiKey = '';
      ctl.close();
    } catch (err) {
      apiError = err.message || '验证失败，账号没有写入';
    } finally {
      apiBusy = false;
      ctl.update();
    }
  }

  ctl.dismissible = true;
  if (mode === 'oauth') startLogin();
  return ctl;
}
