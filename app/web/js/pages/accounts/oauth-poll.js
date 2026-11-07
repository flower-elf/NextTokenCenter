// CodeArts 授权轮询结果的状态判定：成功是 done，失败是 error。
export function oauthPollState(result) {
  if (!result || typeof result !== 'object') return 'pending';
  if (result.status === 'error') return 'error';
  if (result.status === 'done' || result.account) return 'done';
  return 'pending';
}
