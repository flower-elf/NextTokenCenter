"""PRO ONLY 模型只派给付费账号：tier 表来自 /v3/config，免费账号被拦下。"""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts
import wb_tiers


def _token(uid):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (part({"alg": "none"}),
                          part({"sub": uid, "exp": 4102444800}))


def _account(credits):
    return wb_accounts.Account({
        "uid": "u-1", "accessToken": _token("u-1"), "realm": "cn",
        "enabled": True, "credits": credits,
    })


class ProOnlyTests(unittest.TestCase):
    def setUp(self):
        wb_tiers.reset()
        wb_tiers.ingest("cn", {"modelTiers": [
            {"modelIds": ["pro-model"], "requiredUserType": "standard"},
            {"modelIds": ["free-model"], "requiredUserType": "personal_free_user"},
        ]})

    def tearDown(self):
        wb_tiers.reset()

    def test_free_account_cannot_take_a_pro_model(self):
        account = _account({"is_paid_user": False})
        self.assertFalse(account.servable("pro-model"))
        self.assertTrue(account.servable("free-model"))
        self.assertTrue(account.servable("unknown-model"))

    def test_paid_account_can_take_a_pro_model(self):
        account = _account({"is_paid_user": True})
        self.assertTrue(account.servable("pro-model"))

    def test_unknown_tier_map_keeps_old_behaviour(self):
        wb_tiers.reset()
        account = _account({"is_paid_user": False})
        self.assertTrue(account.servable("pro-model"))


if __name__ == "__main__":
    unittest.main()
