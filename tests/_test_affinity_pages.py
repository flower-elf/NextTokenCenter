"""验证会话亲和的暖账号回退契约。

账号不可用时，会话沿最近使用列表回退到见过它的暖账号，前缀缓存继续命中。

运行方式：python tests/run_all.py affinity。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account(uid, realm="cn", **extra):
    data = {"uid": uid, "accessToken": "token-%s" % uid, "realm": realm}
    data.update(extra)
    return wb_accounts.Account(data)


def credits(*days, remain=100):
    """构造一个额度数据，其套餐在给定天数后过期。"""
    packages = []
    for index, day in enumerate(days):
        packages.append({
            "name": "pkg%d" % index,
            "package_code": "package",
            "remain": remain,
            "used": 0,
            "size": remain,
            "days_left": day,
            "no_expiry": False,
            "is_expired": day is not None and day < 0,
        })
    total = remain * len(packages)
    return {"remain": total, "used": 0, "size": total, "packages": packages}


def make_pool(accounts, window=None):
    directory = tempfile.mkdtemp(prefix="affinity-pages-")
    pool = wb_accounts.AccountPool(directory, log=lambda _m: None)
    pool.accounts = accounts
    if window is not None:
        pool.apply_expiring_window({"global": window, "intl": window, "cn": window})
    return pool


class SessionAffinityHistoryTests(unittest.TestCase):
    """绑定是一段短的最近使用历史。"""

    def test_the_last_account_is_the_front_entry(self):
        affinity = wb_accounts.SessionAffinity()
        affinity.bind("s", "a")
        affinity.bind("s", "b")
        self.assertEqual(affinity.get("s"), "b")
        self.assertEqual(affinity.recent("s"), ["b", "a"])

    def test_binding_again_moves_an_account_to_the_front(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b", "c"):
            affinity.bind("s", uid)
        affinity.bind("s", "a")
        self.assertEqual(affinity.recent("s"), ["a", "c", "b"])

    def test_the_history_is_capped(self):
        affinity = wb_accounts.SessionAffinity(history=3)
        for uid in ("a", "b", "c", "d"):
            affinity.bind("s", uid)
        self.assertEqual(affinity.recent("s"), ["d", "c", "b"])

    def test_demote_drops_one_account_and_keeps_the_rest(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b", "c"):
            affinity.bind("s", uid)
        affinity.demote("s", "c")
        self.assertEqual(affinity.recent("s"), ["b", "a"])
        affinity.demote("s")
        self.assertEqual(affinity.recent("s"), ["a"])

    def test_demoting_the_last_account_forgets_the_conversation(self):
        affinity = wb_accounts.SessionAffinity()
        affinity.bind("s", "a")
        affinity.demote("s", "a")
        self.assertIsNone(affinity.get("s"))
        self.assertEqual(affinity.recent("s"), [])

    def test_unbind_forgets_the_whole_conversation(self):
        affinity = wb_accounts.SessionAffinity()
        for uid in ("a", "b"):
            affinity.bind("s", uid)
        affinity.unbind("s")
        self.assertEqual(affinity.recent("s"), [])

    def test_expired_entries_drop_out(self):
        affinity = wb_accounts.SessionAffinity(ttl=0.05)
        affinity.bind("s", "old")
        time.sleep(0.06)
        affinity.bind("s", "new")
        self.assertEqual(affinity.recent("s"), ["new"])

    def test_the_front_entry_slides_its_expiry(self):
        affinity = wb_accounts.SessionAffinity(ttl=0.3)
        affinity.bind("s", "a")
        time.sleep(0.2)
        self.assertEqual(affinity.get("s"), "a")
        time.sleep(0.2)
        self.assertEqual(affinity.get("s"), "a",
                         "the account serving the conversation must not expire "
                         "while it keeps serving it")


class WarmFallbackTests(unittest.TestCase):
    """账号不可用时，会话交给见过它的暖账号。"""

    def _pool_with_warm_history(self):
        """构造 fresh（不可用、最新）→ warm（较早）→ urgent（额度将过期）的历史。

        额度过期偏好会把新会话分给 urgent，本会话必须落到 warm。
        """
        fresh = make_account("fresh")
        warm = make_account("warm")
        urgent = make_account("urgent", credits=credits(1))
        pool = make_pool([fresh, warm, urgent], window=7)
        pool.affinity.bind("s", "warm")
        pool.affinity.bind("s", "fresh")
        fresh.note_error("boom")            # 冷却 60 秒
        return pool, fresh, warm, urgent

    def test_the_fallback_account_becomes_the_binding(self):
        pool, _fresh, warm, _urgent = self._pool_with_warm_history()
        picked = pool.pick_for_session(realm="cn", session_key="s")
        self.assertEqual(picked.uid, warm.uid)
        self.assertEqual(pool.affinity.get("s"), warm.uid)
        # 绑定保留，下一轮仍直接选中 warm。
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="s").uid,
                         warm.uid)

    def test_a_conversation_without_history_uses_the_pool(self):
        pool, _fresh, _warm, urgent = self._pool_with_warm_history()
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="new").uid,
                         urgent.uid)

    def test_a_disabled_account_is_skipped_without_losing_the_history(self):
        pool, _fresh, warm, _urgent = self._pool_with_warm_history()
        pool.get("warm").enabled = False
        # 两个暖账号都不可用，由池决定去向；它们仍留在历史里，恢复后继续可用。
        self.assertEqual(pool.pick_for_session(realm="cn", session_key="s").uid,
                         "urgent")
        history = pool.affinity.recent("s")
        self.assertIn("warm", history)
        self.assertIn("fresh", history)


if __name__ == "__main__":
    unittest.main(verbosity=2)
