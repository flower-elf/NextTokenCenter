// CodeArts 账号授权弹窗：先选登录使用的代理槽，再申请华为云授权链接，轮询到账号加入后关闭。
// 结构、二维码与状态提示与 WorkBuddy 的添加账号弹窗一致，两个渠道的登录流程看起来是同一套。
import { html, nothing } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { postJSON } from '../../../core/api.js';
import { spaceMixed } from '../../../core/format.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import { copyWithToast } from '../../../ui/copy.js';
import { oauthPollState } from '../oauth-poll.js';
import * as acct from '../actions.js';
import { qrMarkup } from './qr.js';

export function openCodeartsAccountDialog({ onDone } = {}) {
  let session = '';
  let authUrl = '';
  let timer = null;
  let closed = false;
  let proxySlot = '';
  let proxySlots = [];
  let phase = 'idle';        // idle | starting | waiting | ok | failed
  let message = '';
  let failures = 0;

  function stepClass(index) {
    if (index === 1) return phase === 'starting' || phase === 'idle' ? 'active' : phase === 'failed' ? 'active' : 'done';
    if (index === 2) return phase === 'waiting' ? 'active' : phase === 'ok' || phase === 'failed' ? 'done' : '';
    return phase === 'ok' ? 'done' : phase === 'waiting' || phase === 'failed' ? 'active' : '';
  }

  function stepOneText() {
    if (phase === 'starting') return '正在向华为云申请授权链接';
    if (phase === 'failed') return '申请授权链接失败，请点「重新生成链接」再试';
    return '已向华为云申请授权链接';
  }

  function stepThreeText() {
    if (phase === 'waiting') return '等待授权回调，本弹窗每 2 秒自动检测一次';
    if (phase === 'ok') return '授权完成，账号已加入';
    if (phase === 'failed') return '授权没有完成，可以重新生成链接后再试';
    return '等待授权回调，本弹窗每 2 秒自动检测一次';
  }

  const ctl = openDialog({
    title: '添加 CodeArts 账号',
    desc: '先选择代理槽，再在浏览器完成华为云授权。',
    width: 512,
    body: () => html`
      ${proxySlots.length ? html`
        <div class="realm-choice">
          <span class="field-label">登录使用的代理槽</span>
          <select class="select" .value=${proxySlot}
                  @change=${(event) => { proxySlot = event.target.value; start(); }}>
            <option value="">直连（不走代理槽）</option>
            <option value="auto">自动（挑一个空闲槽）</option>
            ${proxySlots.filter((slot) => slot.enabled !== false).map((slot) => html`
              <option value=${slot.id}>${slot.name || slot.id} · ${slot.ip || slot.url || ''}</option>`)}
          </select>
          <span class="text-2xs muted">授权与拉取账号资料都走这个代理槽，登录后账号绑定到该槽。</span>
        </div>` : nothing}
      <div class="step-list">
        <div class=${'step ' + stepClass(1)}>
          <span class="step-n">1</span>
          <div class="step-t">${stepOneText()}</div>
        </div>
        <div class=${'step ' + stepClass(2)}>
          <span class="step-n">2</span>
          <div class="step-t">用浏览器打开链接或扫码，登录华为云并同意授权</div>
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
        <div class=${'step ' + stepClass(3)}>
          <span class="step-n">3</span>
          <div class="step-t">${stepThreeText()}</div>
        </div>
        ${message ? html`<p class="step-message ${phase === 'failed' ? 'tone-warning' : 'muted'}">${spaceMixed(message)}</p>` : nothing}
      </div>`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-primary" ?disabled=${phase === 'starting'} @click=${busyClick(start)}>
        ${phase === 'starting' ? icon('LoaderCircle', 'spin') : icon('RefreshCw')}重新生成链接
      </button>`,
    onClose: () => {
      closed = true;
      clearInterval(timer);
      timer = null;
    },
  });

  acct.loadProxySlots().then((payload) => {
    proxySlots = (payload && payload.slots) || [];
    if (proxySlot && proxySlot !== 'auto'
      && !proxySlots.some((slot) => slot.id === proxySlot)) proxySlot = '';
    ctl.update();
  }).catch((err) => toastError('代理槽没有读取成功，本次只能用直连或自动', err));

  async function start() {
    if (closed) return;
    clearInterval(timer);
    timer = null;
    session = '';
    authUrl = '';
    message = '';
    failures = 0;
    phase = 'starting';
    ctl.update();
    try {
      const started = await postJSON('/codearts/accounts/oauth/start', { proxySlot });
      if (closed) return;
      session = started.session_id || '';
      authUrl = started.auth_url || '';
      if (!session) {
        phase = 'failed';
        message = '服务端没有返回会话编号，请重新生成链接。';
        ctl.update();
        return;
      }
      phase = 'waiting';
      message = '正在等待授权完成，完成后会自动加入账号池。';
      ctl.update();
      timer = setInterval(poll, 2000);
    } catch (err) {
      if (closed) return;
      phase = 'failed';
      message = '发起失败：' + err.message;
      ctl.update();
    }
  }

  async function poll() {
    if (closed || !session) return;
    const mine = session;
    try {
      const result = await postJSON('/codearts/accounts/oauth/poll', { session_id: mine });
      if (session !== mine) return;
      failures = 0;
      const state = oauthPollState(result);
      if (state === 'done') {
        clearInterval(timer);
        timer = null;
        session = '';
        phase = 'ok';
        message = '授权完成，账号已加入账号池。';
        ctl.update();
        toast.success('CodeArts 账号已加入');
        acct.notifyAccountsChanged();
        if (onDone) onDone();
        setTimeout(() => { if (!closed) ctl.close(); }, 1500);
        return;
      }
      if (state === 'error') {
        clearInterval(timer);
        timer = null;
        phase = 'failed';
        message = (result && result.message) || '授权失败，请重新发起授权。';
        ctl.update();
      }
    } catch (err) {
      if (err && err.status === 404) {
        clearInterval(timer);
        timer = null;
        phase = 'failed';
        message = '授权会话已过期，请重新打开添加账号。';
        ctl.update();
        return;
      }
      // 单次轮询失败多为网络抖动，连续三次才提示一次，避免每两秒弹一条。
      failures += 1;
      if (failures === 3) {
        message = '轮询暂时失败，仍在重试：' + err.message;
        ctl.update();
      }
    }
  }

  start();
  return ctl;
}
