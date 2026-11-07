import assert from 'node:assert/strict';

import { oauthPollState } from '../../app/web/js/pages/accounts/oauth-poll.js';

assert.equal(oauthPollState(null), 'pending');
assert.equal(oauthPollState({ status: 'pending' }), 'pending');
assert.equal(oauthPollState({ status: 'done', account: { uid: 'u1' } }), 'done');
assert.equal(oauthPollState({ status: 'done' }), 'done');
assert.equal(oauthPollState({ account: { uid: 'u1' } }), 'done');
assert.equal(oauthPollState({ status: 'error', message: 'bad' }), 'error');

console.log('CodeArts OAuth poll state checks passed');
