"""账号凭据刷新：请求格式、响应里的有效期、冷却与主动刷新节奏。

与官方客户端一致：手动刷新和 401 重试都直接请求上游，响应里的 expiresIn 决定
新的有效期，后台每 24 小时（临近到期时 5 分钟）续期一次。用例不访问网络。
"""
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts


def _segment(value):
    text = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(text).decode("ascii").rstrip("=")


def _jwt(claims):
    return "%s.%s.%s" % (_segment({"alg": "none", "typ": "JWT"}),
                         _segment(claims), "signature")


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = list(accounts)
        self._lock = threading.RLock()


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = int(time.time())
        self.old_token = _jwt({"iat": self.now - 3600, "exp": self.now + 30 * 86400,
                               "sub": "uid-1"})
        self.new_token = _jwt({"iat": self.now, "exp": self.now + 30 * 86400,
                               "sub": "uid-1"})
        self.account = wb_accounts.Account({
            "uid": "uid-1",
            "nickname": "n",
            "domain": "www.codebuddy.cn",
            "realm": "cn",
            "platform": "CLI",
            "product": "cli",
            "loginProduct": "cli",
            "accessToken": self.old_token,
            "refreshToken": "rt-old",
            "expiresAt": self.now + 30 * 86400,
            "enabled": True,
        }, os.path.join(self.tmp.name, "uid-1.json"))
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _stub(self, payload=None, error=None):
        def fake_http_json(url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            if error is not None:
                raise error
            return payload
        return fake_http_json

    def _response(self, **auth):
        data = {"accessToken": self.new_token, "refreshToken": "rt-new",
                "expiresIn": 30 * 86400, "domain": "www.codebuddy.cn",
                "tokenType": "Bearer"}
        data.update(auth)
        return {"code": 0, "msg": "OK", "data": {"data": data}}

    def test_force_refresh_calls_upstream_even_when_token_is_fresh(self):
        with mock.patch.object(wb_accounts, "http_json",
                               self._stub(self._response(expiresIn=3600))):
            self.assertTrue(self.account.refresh(force=True))
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["url"], "https://www.codebuddy.cn/v2/plugin/auth/token/refresh")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["X-Refresh-Token"], "rt-old")
        self.assertEqual(call["headers"]["X-Auth-Refresh-Source"], "plugin")
        self.assertEqual(call["headers"]["X-Domain"], "www.codebuddy.cn")
        self.assertIn("CLI/", call["headers"]["User-Agent"])
        self.assertEqual(call["headers"]["X-Product"], "SaaS")
        self.assertEqual(call["headers"]["X-IDE-Type"], "CLI")
        self.assertEqual(call["headers"]["X-IDE-Name"], "CLI")
        self.assertEqual(call["headers"]["X-User-Id"], "uid-1")
        self.assertEqual(call["headers"]["Authorization"], "Bearer " + self.old_token)
        self.assertNotIn("X-Product-Version", call["headers"])
        self.assertEqual(self.account.access_token, self.new_token)
        self.assertEqual(self.account.refresh_token, "rt-new")
        self.assertAlmostEqual(self.account.expires_at, self.now + 3600, delta=5)
        self.assertAlmostEqual(self.account.issued_at, self.now, delta=5)

    def test_login_channel_drives_refresh_not_chat_identity(self):
        # 登录渠道决定刷新端点、UA 与 IDE 标头，切换出站身份不改变签发会话。
        self.account.login_product = "workbuddy"
        self.account.product = "cli"
        with mock.patch.object(wb_accounts, "http_json", self._stub(self._response())):
            self.assertTrue(self.account.refresh(force=True))
        call = self.calls[0]
        self.assertEqual(call["url"], "https://www.workbuddy.cn/v2/plugin/auth/token/refresh")
        self.assertTrue(call["headers"]["User-Agent"].startswith("WorkBuddy/"))
        self.assertEqual(call["headers"]["X-IDE-Type"], "WorkBuddy")
        self.assertEqual(call["headers"]["X-IDE-Version"], "5.9.3")
        self.assertNotIn("X-Env-ID", call["headers"])

        other = wb_accounts.Account({
            "uid": "uid-2", "realm": "cn", "product": "workbuddy",
            "loginProduct": "cli", "accessToken": self.old_token,
            "refreshToken": "rt-2", "expiresAt": self.now + 30 * 86400,
            "enabled": True,
        })
        del self.calls[:]
        with mock.patch.object(wb_accounts, "http_json", self._stub(self._response())):
            self.assertTrue(other.refresh(force=True))
        call = self.calls[0]
        self.assertEqual(call["url"], "https://www.codebuddy.cn/v2/plugin/auth/token/refresh")
        self.assertTrue(call["headers"]["User-Agent"].startswith("CLI/"))
        self.assertEqual(call["headers"]["X-IDE-Type"], "CLI")
        self.assertNotIn("X-Env-ID", call["headers"])

        vsc = wb_accounts.Account({
            "uid": "uid-3", "realm": "cn", "product": "cli",
            "loginProduct": "vscode", "accessToken": self.old_token,
            "refreshToken": "rt-3", "expiresAt": self.now + 30 * 86400,
            "enabled": True,
        })
        del self.calls[:]
        with mock.patch.object(wb_accounts, "http_json", self._stub(self._response())):
            self.assertTrue(vsc.refresh(force=True))
        call = self.calls[0]
        self.assertEqual(call["url"], "https://copilot.tencent.com/v2/plugin/auth/token/refresh")
        self.assertTrue(call["headers"]["User-Agent"].startswith("VSCode/"))
        self.assertEqual(call["headers"]["X-IDE-Type"], "VSCode")
        self.assertEqual(call["headers"]["X-Product-Version"], "4.12.39997873")
        self.assertEqual(call["headers"]["X-Env-ID"], "production")

    def test_response_expires_at_wins_over_expires_in(self):
        expires = (self.now + 7200) * 1000
        with mock.patch.object(wb_accounts, "http_json",
                               self._stub(self._response(expiresAt=expires))):
            self.assertTrue(self.account.refresh(force=True))
        self.assertAlmostEqual(self.account.expires_at, self.now + 7200, delta=5)

    def test_jwt_expiry_is_used_when_response_omits_both(self):
        payload = self._response()
        del payload["data"]["data"]["expiresIn"]
        with mock.patch.object(wb_accounts, "http_json", self._stub(payload)):
            self.assertTrue(self.account.refresh(force=True))
        self.assertAlmostEqual(self.account.expires_at, self.now + 30 * 86400, delta=5)

    def test_fresh_token_skips_upstream_without_force(self):
        with mock.patch.object(wb_accounts, "http_json", self._stub(error=AssertionError("no call"))):
            self.assertTrue(self.account.refresh())
        self.assertEqual(self.calls, [])

    def test_success_cooldown_blocks_repeat_force(self):
        with mock.patch.object(wb_accounts, "http_json", self._stub(self._response())):
            self.assertTrue(self.account.refresh(force=True))
            self.assertTrue(self.account.refresh(force=True))
        self.assertEqual(len(self.calls), 1)

    def test_failed_attempt_cooldown_reports_failure(self):
        with mock.patch.object(wb_accounts, "http_json",
                               self._stub(error=RuntimeError("upstream down"))):
            self.assertFalse(self.account.refresh(force=True))
            self.assertFalse(self.account.refresh(force=True))
        self.assertEqual(len(self.calls), 1)

    def test_refresh_persists_the_new_credentials(self):
        with mock.patch.object(wb_accounts, "http_json", self._stub(self._response())):
            self.assertTrue(self.account.refresh(force=True))
        saved = []
        for name in os.listdir(self.tmp.name):
            if name.endswith(".json"):
                with open(os.path.join(self.tmp.name, name), encoding="utf-8") as fh:
                    saved.append(json.load(fh))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["accessToken"], self.new_token)
        self.assertEqual(saved[0]["refreshToken"], "rt-new")
        self.assertAlmostEqual(saved[0]["expiresAt"], self.now + 30 * 86400, delta=5)


class RefresherTests(unittest.TestCase):
    def setUp(self):
        self.now = 1793000000
        token = _jwt({"iat": self.now, "exp": self.now + 30 * 86400, "sub": "uid-1"})
        self.account = wb_accounts.Account({
            "uid": "uid-1", "realm": "cn", "accessToken": token,
            "refreshToken": "rt", "expiresAt": self.now + 30 * 86400, "enabled": True,
        })
        self.account.issued_at = self.now
        self.account.added_at = self.now
        self.pool = FakePool([self.account])
        self.refresher = wb_accounts.CredentialRefresher(self.pool, rng=lambda: 0.0)

    def _age_account(self, hours):
        issued = self.now - int(hours * 3600)
        self.account.access_token = _jwt({"iat": issued,
                                          "exp": self.now + 30 * 86400,
                                          "sub": "uid-1"})
        self.account.issued_at = issued
        self.account.added_at = issued

    def test_delay_matches_the_client_cadence(self):
        delay = self.refresher.delay_seconds(self.account, now=self.now)
        self.assertGreaterEqual(delay, 24 * 3600 - 300)
        self.assertLessEqual(delay, 24 * 3600 + 300)
        # 剩余不足一天时改成几分钟一次。
        self.account.expires_at = self.now + 3600
        self.assertEqual(self.refresher.delay_seconds(self.account, now=self.now), 300)

    def test_due_only_after_the_interval(self):
        self.assertFalse(self.refresher.due(self.account, now=self.now + 3600))
        self.assertTrue(self.refresher.due(self.account, now=self.now + 24 * 3600))

    def test_refresh_once_uses_force_and_does_not_repeat(self):
        self._age_account(25)

        def fake_refresh(force=False):
            self.assertTrue(force)
            self.account.last_refresh_at = self.now
            return True

        with mock.patch.object(self.account, "refresh", side_effect=fake_refresh) as refresh:
            self.assertEqual(self.refresher.refresh_once(now=self.now), "uid-1")
            self.assertIsNone(self.refresher.refresh_once(now=self.now + 1))
        refresh.assert_called_once_with(force=True)

    def test_refresher_skips_api_keys_and_disabled_accounts(self):
        self.account.is_api_key = True
        self.assertFalse(self.refresher.due(self.account, now=self.now + 48 * 3600))
        self.account.is_api_key = False
        self.account.enabled = False
        self.assertFalse(self.refresher.due(self.account, now=self.now + 48 * 3600))

    def test_failure_parks_the_account_with_backoff(self):
        self._age_account(25)
        with mock.patch.object(self.account, "refresh", return_value=False):
            self.assertIsNone(self.refresher.refresh_once(now=self.now))
            self.assertFalse(self.refresher.due(self.account, now=self.now + 60))
        self.assertGreater(self.refresher._parked["uid-1"], self.now + 60)


if __name__ == "__main__":
    unittest.main()
