"""单账号任务开关与账号分组：持久化、批量启停与导入字段。"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import wb_accounts


def _token(uid):
    def part(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (part({"alg": "none"}),
                          part({"sub": uid, "exp": 4102444800}))


class TaskAndGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wb-tasks-group-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def _account(self, uid, **overrides):
        data = {"uid": uid, "accessToken": _token(uid), "refreshToken": "rt",
                "realm": "cn", "enabled": True}
        data.update(overrides)
        return wb_accounts.Account(data)

    def test_tasks_follow_enabled_until_explicitly_set(self):
        self.assertTrue(self._account("u-1").tasks_enabled)
        self.assertFalse(self._account("u-2", enabled=False).tasks_enabled)
        self.assertTrue(self._account(
            "u-3", enabled=False, tasksEnabled=True).tasks_enabled)
        self.assertFalse(self._account(
            "u-4", enabled=True, tasksEnabled=False).tasks_enabled)

    def test_setters_persist_and_keep_axes_independent(self):
        pool = wb_accounts.AccountPool(self.dir)
        pool.add(self._account("u-1"))
        pool.set_enabled("u-1", False)
        view = pool.set_tasks_enabled("u-1", True)
        self.assertFalse(view["enabled"])
        self.assertTrue(view["tasksEnabled"])
        pool.set_group("u-1", " team ")
        self.assertEqual(pool.get("u-1").group, "team")

        reloaded = wb_accounts.AccountPool(self.dir)
        reloaded.load()
        account = reloaded.get("u-1")
        self.assertFalse(account.enabled)
        self.assertTrue(account.tasks_enabled)
        self.assertEqual(account.group, "team")

    def test_group_bulk_toggle_only_touches_that_group(self):
        pool = wb_accounts.AccountPool(self.dir)
        pool.add(self._account("u-a", group="team"))
        pool.add(self._account("u-b", group="team"))
        pool.add(self._account("u-c", group="other"))
        changed = pool.set_group_enabled("team", False)
        self.assertEqual(sorted(changed), ["u-a", "u-b"])
        self.assertFalse(pool.get("u-a").enabled)
        self.assertFalse(pool.get("u-a").tasks_enabled)
        self.assertTrue(pool.get("u-c").enabled)

        pool.set_all_enabled(True, group="team")
        self.assertTrue(pool.get("u-a").enabled)
        self.assertTrue(pool.get("u-a").tasks_enabled)
        self.assertTrue(pool.get("u-c").enabled)

    def test_parse_account_set_accepts_tasks_and_group(self):
        changes, problem = wb_accounts.parse_account_set(
            {"tasksEnabled": False, "group": " team "})
        self.assertEqual(problem, "")
        self.assertFalse(changes["tasksEnabled"])
        self.assertEqual(changes["group"], "team")
        _, problem = wb_accounts.parse_account_set({"group": 3})
        self.assertTrue(problem)

    def test_import_row_carries_tasks_and_group(self):
        kwargs = wb_accounts.normalise_import_row({
            "uid": "u-import",
            "accessToken": _token("u-import"),
            "refreshToken": "rt",
            "realm": "cn",
            "tasksEnabled": False,
            "group": " batch ",
        }, realm="cn")
        self.assertFalse(kwargs["tasksEnabled"])
        self.assertEqual(kwargs["group"], "batch")
        account = wb_accounts.Account(kwargs)
        self.assertFalse(account.tasks_enabled)
        self.assertEqual(account.group, "batch")


if __name__ == "__main__":
    unittest.main()
