"""CodeArts 渠道与路由：持久化、Key 绑定、路由校验、停用收敛与跨区放行。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-realm-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_channels  # noqa: E402
import wb_codearts  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("== settings ==")
wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "global", 5)
check("global limit reaches codearts",
      wb_settings.limit_value(ACCOUNTS, "daily_token_limit", "codearts") == 5,
      wb_settings.limit_value(ACCOUNTS, "daily_token_limit", "codearts"))
wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "global", 0)
check("not configured by default", wb_settings.codearts_configured(ACCOUNTS) is False)

entry = wb_settings._clean_key_entry({"key": "abcd1234", "route": ["cn", "codearts"]})
check("key keeps route order", entry["route"] == ["cn", "codearts"], entry)
entry = wb_settings._clean_key_entry({"key": "abcd1234"})
check("missing route follows panel", entry["route"] == [], entry)

print("== route parsing ==")
check("comma string sorted by registry",
      wb_channels.parse_route("codearts, intl") == ("intl", "codearts"))
check("empty means none", wb_channels.parse_route("") == () and wb_channels.parse_route(None) == ())
for bad in (["intl", "cn"], "cn,intl", ["nope"], ["cn", "cn"], [1]):
    try:
        wb_channels.parse_route(bad)
        check("rejects %r" % (bad,), False)
    except ValueError as exc:
        check("rejects %r" % (bad,), True, exc)

print("== options ==")
check("codearts always selectable",
      wb_proxy.realm_options() == ["intl", "cn", "codearts"])

print("== persistence ==")
wb_proxy.save_persisted_route(("intl", "codearts"))
check("saved route", wb_proxy.ROUTE == ("intl", "codearts")
      and wb_proxy.CURRENT_REALM == "intl")
check("persisted file", json.load(open(wb_proxy.realm_state_file()))["route"] ==
      ["intl", "codearts"])
wb_proxy.ROUTE = ("cn",)
check("channel without accounts dropped", wb_proxy.load_persisted_route() == ("intl",))
wb_codearts.save_auth(ACCOUNTS, {
    "user_id": "realm-probe", "user_name": "probe",
    "cloud_dragon_token": "token", "access_key_id": "ak",
    "secret_access_key": "sk", "refresh_token": "rt",
})
wb_codearts.invalidate_pool(ACCOUNTS)
wb_proxy.save_persisted_route(wb_channels.parse_route(["codearts", "cn"]))
wb_proxy.ROUTE = ("intl",)
check("loaded route with account",
      wb_proxy.load_persisted_route() == ("cn", "codearts")
      and wb_proxy.CURRENT_REALM == "cn")
wb_codearts.pool_for(ACCOUNTS).disable("realm-probe")
check("disabled account means unavailable",
      wb_codearts.configured(ACCOUNTS) is False)
wb_codearts.delete_auth(ACCOUNTS, "realm-probe")
wb_codearts.invalidate_pool(ACCOUNTS)

print("== availability follows accounts ==")
check("no account means unavailable", wb_codearts.configured(ACCOUNTS) is False)
check("configured_config false without accounts",
      wb_codearts.configured_config(wb_codearts.load_config(ACCOUNTS)) is False)
check("pool summary opens", wb_codearts.pool_for(ACCOUNTS).stats()["total"] == 0)
masked = wb_codearts.masked_config(ACCOUNTS)
check("no enable switch in config", "enabled" not in masked, masked)
check("provider views empty", wb_channels.provider("codearts").views() == [])
check("all views include codearts only", wb_channels.all_views("codearts") == [],
      wb_channels.all_views("codearts"))

print("== cross realm ==")
h = wb_proxy.Handler.__new__(wb_proxy.Handler)
h.key_entry = {"name": "测试 Key"}
check("codearts bypasses exclusive realm",
      h._cross_realm_error("deepseek-v4-pro-0813", "codearts") == "")
check("workbuddy still rejects mismatch",
      h._cross_realm_error("deepseek-v4-pro-0813", "intl") != "")

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
