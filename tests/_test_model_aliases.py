"""模型别名与按出口关闭模型：别名表校验、请求 id 的换算、/v1/models 的通告、
关闭名单按出口隔离，以及用量行写出实际调用的出口与上游 id。
不访问上游，也不使用真实凭据。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-model-alias-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_proxy  # wb_usage 经网关模块取运行态，先加载它
import wb_settings
import wb_usage
from wb_models import announce_model_aliases, canonical_model_id, upstream_model_id

SOURCE = "deepseek-v4.1-flash-1017"
TARGET = "deepseek-v4.1-flash"
ALIASES = {SOURCE: TARGET}


class ParseAliasesTests(unittest.TestCase):
    def test_valid_table_is_trimmed(self):
        out, problem = wb_settings.parse_model_aliases({" %s " % SOURCE: " %s " % TARGET})
        self.assertEqual(problem, "")
        self.assertEqual(out, ALIASES)

    def test_invalid_tables_are_refused(self):
        cases = {
            "not an object": [SOURCE, TARGET],
            "points to itself": {"glm-5.3": "GLM-5.3"},
            "two levels": {"a-1": "a", "a": "b"},
            "listed twice": {"A-1": "x", "a-1": "y"},
            "bad id": {"has space": TARGET},
            "non-string": {SOURCE: 7},
        }
        for name, value in cases.items():
            with self.subTest(name):
                out, problem = wb_settings.parse_model_aliases(value)
                self.assertIsNone(out)
                self.assertTrue(problem)


class UpstreamIdTests(unittest.TestCase):
    def test_source_id_is_announced_as_target(self):
        self.assertEqual(canonical_model_id(ALIASES, SOURCE.upper()), TARGET)
        self.assertEqual(canonical_model_id(ALIASES, "glm-5.3"), "glm-5.3")

    def test_realm_with_its_own_enabled_target_keeps_it(self):
        self.assertEqual(upstream_model_id(ALIASES, TARGET, {TARGET, SOURCE}, set()), TARGET)

    def test_disabled_target_falls_back_to_the_source(self):
        self.assertEqual(upstream_model_id(ALIASES, TARGET, {TARGET, SOURCE}, {TARGET}), SOURCE)

    def test_realm_without_target_uses_the_source(self):
        self.assertEqual(upstream_model_id(ALIASES, TARGET, {SOURCE}, set()), SOURCE)

    def test_client_sending_the_source_id_is_routed_the_same_way(self):
        self.assertEqual(upstream_model_id(ALIASES, SOURCE, {TARGET}, set()), TARGET)
        self.assertEqual(upstream_model_id(ALIASES, SOURCE, {SOURCE}, set()), SOURCE)

    def test_nothing_usable_returns_the_target_for_the_disabled_check(self):
        self.assertEqual(upstream_model_id(ALIASES, TARGET, {TARGET, SOURCE}, {TARGET, SOURCE}),
                         TARGET)

    def test_unknown_catalog_counts_every_id_as_available(self):
        self.assertEqual(upstream_model_id(ALIASES, TARGET, None, set()), TARGET)
        self.assertEqual(upstream_model_id(ALIASES, TARGET, None, {TARGET}), SOURCE)


class AnnounceTests(unittest.TestCase):
    def test_source_entry_is_renamed(self):
        out = announce_model_aliases(ALIASES, [{"id": SOURCE, "name": "DS 1017"}, {"id": "glm-5.3"}])
        self.assertEqual([e["id"] for e in out], [TARGET, "glm-5.3"])
        self.assertEqual(out[0]["name"], TARGET)

    def test_source_entry_is_dropped_when_target_is_listed(self):
        out = announce_model_aliases(ALIASES, [{"id": SOURCE}, {"id": TARGET}])
        self.assertEqual([e["id"] for e in out], [TARGET])

    def test_input_entries_are_not_modified(self):
        entries = [{"id": SOURCE}]
        announce_model_aliases(ALIASES, entries)
        self.assertEqual(entries, [{"id": SOURCE}])


class DisabledPerRealmTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="realm-", dir=_TMP)

    def test_codearts_list_is_separate_from_workbuddy(self):
        wb_settings.set_disabled_models(self.dir, ["default-model"])
        wb_settings.set_model_enabled(self.dir, "codearts", TARGET, False)
        self.assertFalse(wb_settings.model_served(self.dir, TARGET, "codearts"))
        self.assertTrue(wb_settings.model_served(self.dir, TARGET, "cn"))
        self.assertEqual(wb_settings.disabled_models(self.dir, "intl"), ["default-model"])

    def test_toggle_changes_only_that_model(self):
        wb_settings.set_disabled_models(self.dir, ["default-model", "fast-model"], "cn")
        wb_settings.set_model_enabled(self.dir, "intl", "glm-5.3", False)
        wb_settings.set_model_enabled(self.dir, "cn", "FAST-MODEL", True)
        self.assertEqual(wb_settings.disabled_models(self.dir, "cn"),
                         ["default-model", "glm-5.3"])


class UsageRouteColumnTests(unittest.TestCase):
    def test_routed_request_records_channel_and_upstream_id(self):
        audit = {"key_id": "k", "ip": "", "api": "chat", "status": None,
                 "realm": "codearts", "upstream_model": SOURCE}
        row = wb_usage.record_usage(TARGET, {"prompt_tokens": 1, "completion_tokens": 1,
                                             "total_tokens": 2}, audit=audit)
        self.assertEqual((row["realm"], row["model"], row["requested_model"]),
                         ("codearts", SOURCE, TARGET))
        err = wb_usage.record_error(TARGET, 502, "boom", audit=audit)
        self.assertEqual((err["realm"], err["model"], err["requested_model"]),
                         ("codearts", SOURCE, TARGET))

    def test_billed_model_from_upstream_wins(self):
        audit = {"realm": "intl", "upstream_model": SOURCE}
        row = wb_usage.record_usage("paid-model", None, requested_model=TARGET, audit=audit)
        self.assertEqual((row["model"], row["requested_model"]), ("paid-model", TARGET))


if __name__ == "__main__":
    unittest.main()
