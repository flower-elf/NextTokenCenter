"""统一账号视图、号池守卫、调度语义与 ChannelError 映射。

两个提供方共用同一套账号方法：这里的断言逐项对照契约，避免只有 WorkBuddy 或
只有 CodeArts 生效的行为继续漂移。
"""
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-accounts-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_accounts  # noqa: E402
import wb_channels  # noqa: E402
import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402
from _fake_codearts import start_server, stop_server  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def make_auth(state, uid, name):
    return {
        "user_id": uid, "user_name": name, "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }


UNIFIED_KEYS = (
    "uid", "nickname", "realm", "provider", "platform", "identity",
    "identityOptions", "enabled", "tasksEnabled", "group", "priority", "weight",
    "concurrencyLimit", "activeRequests", "note", "disabledModels", "proxySlot",
    "proxy", "expiresAt", "issuedAt", "expiresIn", "hasRefreshToken",
    "lastError", "lastErrorDetail", "inCooldown", "cooldownFor",
    "modelCooldowns", "fails", "addedAt", "file", "credits", "creditExpiries",
    "reserveCredits", "reserveBlocked", "dailyTokenLimit", "dailyTokensToday",
    "dailyLimitBlocked", "dailyCreditLimit", "dailyCreditsToday",
    "creditLimitReached", "modelDailyTokenLimit", "modelDailyTokens",
    "lastCheckin", "canCheckin", "canDailyChat", "models", "freeModels",
)

srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

try:
    print("== WorkBuddy provider ==")
    wb_pool_dir = os.path.join(_TMP.name, "wb-pool")
    wb_pool = wb_accounts.AccountPool(wb_pool_dir)
    cn = wb_accounts.Account({"uid": "wb-cn", "accessToken": "t", "realm": "cn"})
    intl = wb_accounts.Account({"uid": "wb-intl", "accessToken": "t", "realm": "intl"})
    wb_pool.accounts = [cn, intl]
    wb_proxy.POOL = wb_pool
    WB = wb_channels.provider("intl")
    check("workbuddy provider", WB.name == "workbuddy")
    check("workbuddy accounts", len(WB.accounts()) == 2)
    check("workbuddy find", WB.find("wb-cn") is cn and WB.find("nope") is None)
    views = WB.views()
    check("workbuddy view keys", all(key in views[0] for key in UNIFIED_KEYS),
          [key for key in UNIFIED_KEYS if key not in views[0]])
    check("workbuddy provider field", views[0]["provider"] == "workbuddy", views[0])
    check("workbuddy identity",
          views[0]["identity"] in ("workbuddy", "vscode", "cli"), views[0]["identity"])
    check("workbuddy identity options",
          [item["value"] for item in views[0]["identityOptions"]]
          == ["workbuddy", "vscode", "cli"], views[0]["identityOptions"])
    check("workbuddy realm filter",
          {view["uid"] for view in WB.views("cn")} == {"wb-cn"})
    check("workbuddy foreign realm empty", WB.views("codearts") == [])
    check("owner finds workbuddy", wb_channels.owner("wb-cn") is WB)
    cn.last_checkin = "2026-11-07 12:00:00"
    checkin_view = WB.views("cn")[0]["lastCheckin"]
    check("workbuddy lastCheckin object",
          isinstance(checkin_view, dict) and checkin_view["ok"] is True
          and checkin_view["at"] > 0, checkin_view)
    updated = WB.update("wb-cn", {"note": "hi", "weight": 2, "group": "g",
                                  "concurrencyLimit": 3,
                                  "disabledModels": ["glm-5.3"],
                                  "tasksEnabled": False})
    check("workbuddy update applied", updated["note"] == "hi"
          and updated["weight"] == 2 and updated["group"] == "g"
          and updated["concurrencyLimit"] == 3
          and updated["disabledModels"] == ["glm-5.3"]
          and updated["tasksEnabled"] is False, updated)
    check("workbuddy update persisted",
          wb_pool.get("wb-cn").note == "hi"
          and wb_pool.get("wb-cn").concurrency_limit == 3,
          wb_pool.get("wb-cn").public())
    switched = WB.update("wb-cn", {"identity": "cli"})
    check("workbuddy identity update", switched["identity"] == "cli", switched)
    try:
        WB.update("wb-cn", {"identity": "nope"})
        check("workbuddy identity validated", False)
    except ValueError:
        check("workbuddy identity validated", True)
    try:
        WB.update("wb-cn", {"bogus": 1})
        check("workbuddy unknown field rejected", False)
    except ValueError:
        check("workbuddy unknown field rejected", True)
    try:
        WB.update("wb-cn", {"note": "x" * 201})
        check("workbuddy note limit", False)
    except ValueError:
        check("workbuddy note limit", True)
    try:
        WB.update("wb-cn", {"disabledModels": [1]})
        check("workbuddy disabled models validated", False)
    except ValueError:
        check("workbuddy disabled models validated", True)
    try:
        WB.update("wb-cn", {"proxy": "http://127.0.0.1:1"})
        check("legacy proxy key rejected", False)
    except ValueError:
        check("legacy proxy key rejected", True)
    check("workbuddy bulk foreign realm no-op",
          WB.set_all_enabled(False, realm="codearts") == [])
    wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-w", "name": "w", "url": "http://127.0.0.1:7899",
         "enabled": True},
    ])
    slotted = WB.update("wb-cn", {"proxySlot": "auto"})
    check("workbuddy auto slot", slotted["proxySlot"] == "slot-w", slotted)
    try:
        WB.update("wb-cn", {"proxySlot": "nope"})
        check("workbuddy unknown slot rejected", False)
    except ValueError:
        check("workbuddy unknown slot rejected", True)
    WB.update("wb-cn", {"proxySlot": ""})
    wb_settings.set_proxy_slots(ACCOUNTS, [])
    check("workbuddy ready count", WB.ready_count() == 2, WB.ready_count())
    check("workbuddy realm ready", WB.ready_count("cn") == 1)
    check("workbuddy total", WB.total_count() == 2 and WB.total_count("chaos") == 0)
    wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "global", 123)
    published = WB.apply_pool_guards(refresh=True)
    check("workbuddy guards published", published >= 3, published)
    check("workbuddy token limit pushed",
          wb_pool.get("wb-cn").daily_token_limit == 123,
          wb_pool.get("wb-cn").daily_token_limit)
    wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "global", 0)
    WB.apply_pool_guards(refresh=True)
    check("workbuddy guard cleared",
          wb_pool.get("wb-cn").daily_token_limit == 0,
          wb_pool.get("wb-cn").daily_token_limit)
    rows = WB.export(uids=["wb-cn"], include_secrets=False)
    check("workbuddy export row", len(rows) == 1
          and rows[0]["provider"] == "workbuddy"
          and "accessToken" not in rows[0], rows)
    import base64
    import json as _json
    jwt_payload = base64.urlsafe_b64encode(
        _json.dumps({"sub": "wb-cn"}).encode()).decode().rstrip("=")
    fake_jwt = "h." + jwt_payload + ".s"
    skipped = WB.import_rows([{"provider": "workbuddy", "uid": "wb-cn",
                               "accessToken": fake_jwt}], dry_run=True)
    check("workbuddy import dry run", skipped["imported"] == []
          and skipped["skipped"][0]["uid"] == "wb-cn", skipped)
    foreign = WB.import_rows([{"provider": "codearts", "uid": "x"}])
    check("workbuddy import foreign skipped",
          foreign["skipped"][0]["reason"] == "belongs to codearts", foreign)
    checkin_rows = WB.checkin(["wb-intl"])
    check("workbuddy checkin realm rule", len(checkin_rows) == 1
          and checkin_rows[0]["ok"] is False, checkin_rows)
    missing_credit = WB.refresh_credits(["nope"])
    check("workbuddy missing credit uid reported",
          len(missing_credit) == 1 and missing_credit[0]["ok"] is False
          and missing_credit[0]["error"], missing_credit)
    missing_checkin = WB.checkin(["nope"])
    check("workbuddy missing checkin uid reported",
          len(missing_checkin) == 1 and missing_checkin[0]["ok"] is False
          and missing_checkin[0]["message"], missing_checkin)
    calls = {}
    original_open = wb_proxy.open_upstream
    wb_proxy.open_upstream = lambda payload, **kw: (
        calls.update(kw) or object())
    try:
        marker = WB.open_stream({"model": "x"}, "sess", "intl", only_uid="wb-cn")
        check("workbuddy forwards only_uid",
              marker is not None and calls.get("only_uid") == "wb-cn"
              and calls.get("target_realm") == "intl", calls)
    finally:
        wb_proxy.open_upstream = original_open

    print("== CodeArts provider ==")
    pool = C.pool_for(ACCOUNTS)
    u1 = pool.add(C.save_auth(ACCOUNTS, make_auth(state, "u1", "账号一")))
    u2 = pool.add(C.save_auth(ACCOUNTS, make_auth(state, "u2", "账号二")))
    CA = wb_channels.provider("codearts")
    views = CA.views("all")
    check("codearts view keys", len(views) == 2
          and all(key in views[0] for key in UNIFIED_KEYS),
          [key for key in UNIFIED_KEYS if key not in views[0]])
    check("codearts provider field",
          all(view["provider"] == "codearts" for view in views))
    check("codearts identity options",
          [item["value"] for item in views[0]["identityOptions"]] == ["ide", "cli"],
          views[0]["identityOptions"])
    check("codearts realm filter",
          CA.views("intl") == [] and len(CA.views("codearts")) == 2)

    print("== guards ==")
    # 与 statistics/plugin 的真实形状一致：套餐积分在 end_date 的周期重置日清零
    u1.info = {"credits": {"total": {"remain": 100.0, "amount": 200.0,
                                     "used": 0.0, "expiring": 0.0},
                           "basic": {"remain": 100.0, "amount": 200.0,
                                     "used": 0.0, "expiring": 0.0}},
               "end_date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 2 * 86400)),
               "package_name": "体验版"}
    u1.info_fetched_at = time.time()
    u1.reserve_credits = 200
    check("reserve blocked", u1.reserve_blocked() is True)
    check("reserve blocks dispatch", u1.healthy(model="GLM-5.2") is False)
    u1.reserve_credits = 0
    u1.daily_token_limit = 5
    u1.daily_tokens_today = 5
    check("daily token guard", u1.daily_limit_blocked() is True
          and u1.healthy(model="GLM-5.2") is False)
    u1.daily_token_limit = 0
    u1.daily_tokens_today = None
    u1.daily_credit_limit = 3
    u1.credit_day = {"day": time.strftime("%Y-%m-%d"), "used": 0.0}
    u1.info["credits"]["total"]["used"] = 5.0
    check("daily credit growth", u1.daily_credits_today == 5.0,
          u1.daily_credits_today)
    check("daily credit guard", u1.credit_limit_reached() is True
          and u1.healthy(model="GLM-5.2") is False)
    u1.model_benefit = {"deepseek-v4-pro-0901": True}
    check("free model exempt", u1.credit_limit_blocked("deepseek-v4-pro-0901")
          is False and u1.healthy(model="deepseek-v4-pro-0901") is True)
    u1.daily_credit_limit = 0
    u1.info["credits"]["total"]["used"] = 0.0
    u1.credit_day = None
    u1.model_daily_token_limit = 50
    u1.model_daily_tokens = {"GLM-5.2": 60}
    check("model token guard", u1.model_token_limit_blocked("GLM-5.2") is True
          and u1.healthy(model="GLM-5.2") is False)
    check("other model unaffected", u1.healthy(model="GLM-4.7") is True)
    u1.model_daily_token_limit = 0
    u1.model_daily_tokens = None
    u1.expiring_window_days = 7
    check("expiring window", u1.in_expiring_window() is True
          and u1.soonest_expiring_days() < 3, u1.soonest_expiring_days())
    check("expiring rate", u1.expiring_rate_per_day(7) > 0)
    # 线上账号的真实快照：500 套餐积分、4000 注册赠送，当天签到的 100 积分剩 99.93
    u1.info = {"credits": {
        "total": {"amount": 4600.0, "used": 0.12, "remain": 4599.88, "expiring": 99.93},
        "basic": {"amount": 500.0, "used": 0.0, "remain": 500.0, "expiring": 0.0},
        "on_demand": {"amount": 0.0, "used": 0.0, "remain": 0.0, "expiring": 0.0},
        "bonus": {"amount": 4100.0, "used": 0.12, "remain": 4099.88, "expiring": 99.93}},
        "end_date": "2026-12-07"}
    cst = datetime.timezone(datetime.timedelta(hours=8))
    noon = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=cst).timestamp()
    rows = u1.credit_expiries(noon)
    check("check-in credits expire at the end of the Beijing day, package credits at the reset date",
          [(r["name"], r["amount"]) for r in rows] == [("当日到期积分", 99.93), ("套餐积分", 500.0)]
          and rows[0]["at"] == datetime.datetime(2026, 11, 7, 23, 59, 59, tzinfo=cst).timestamp()
          and rows[1]["at"] == datetime.datetime(2026, 12, 7, tzinfo=cst).timestamp(), rows)
    # 17:00Z 已是北京时间次日 01:00，签到积分的到期日随之换到次日
    after_midnight = datetime.datetime(2026, 11, 7, 17, 0, tzinfo=datetime.timezone.utc).timestamp()
    check("the Beijing day boundary decides the check-in expiry",
          u1.credit_expiries(after_midnight)[0]["at"]
          == datetime.datetime(2026, 11, 8, 23, 59, 59, tzinfo=cst).timestamp())
    check("soonest expiry is the check-in credits",
          abs(u1.soonest_expiring_days(noon) - (12 * 3600 - 1) / 86400.0) < 1e-6,
          u1.soonest_expiring_days(noon))
    u1.expiring_window_days = 0
    u1.info = None

    print("== apply_pool_guards ==")
    wb_settings.set_limit(ACCOUNTS, "reserve_credits", "global", 11)
    wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "global", 2222)
    wb_settings.set_limit(ACCOUNTS, "daily_credit_limit", "global", 33)
    wb_settings.set_limit(ACCOUNTS, "model_daily_token_limit", "global", 444)
    wb_settings.set_limit(ACCOUNTS, "expiring_window_days", "global", 9)
    wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "codearts", 777)
    published = CA.apply_pool_guards(refresh=True)
    check("guards published", published == len(C.GUARD_KEYS), published)
    check("reserve pushed", u1.reserve_credits == 11 and u2.reserve_credits == 11)
    check("per-realm override pushed", u1.daily_token_limit == 777,
          u1.daily_token_limit)
    check("credit limit pushed", u1.daily_credit_limit == 33)
    check("model limit pushed", u1.model_daily_token_limit == 444)
    check("expiring window pushed", u1.expiring_window_days == 9)
    wb_settings.set_limit(ACCOUNTS, "daily_token_limit", "codearts", None)
    for key in C.GUARD_KEYS:
        wb_settings.set_limit(ACCOUNTS, key, "global", 0)
    CA.apply_pool_guards(refresh=True)
    check("guards cleared", u1.daily_token_limit == 0
          and u1.reserve_credits == 0 and u1.expiring_window_days == 0,
          (u1.daily_token_limit, u1.reserve_credits))

    print("== routing inputs ==")
    import wb_usage
    u1.info = {"credits": {"total": {"remain": 76.0, "amount": 100.0,
                                     "used": 24.0, "expiring": 0.0}},
               "end_date": time.strftime(
                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 86400)),
               "package_name": "体验版"}
    u1.info_fetched_at = time.time()
    u1.credit_day = {"day": time.strftime("%Y-%m-%d"), "used": 0.0}
    scale = wb_usage.credit_rate_scale()
    updated = CA.apply_routing_inputs()
    check("routing inputs cover accounts", updated == 2, updated)
    check("credit rate derived from used growth",
          abs(u1.credit_rate_per_day - 24.0 * scale) < 1e-6,
          (u1.credit_rate_per_day, 24.0 * scale))
    u1.info["credits"]["total"]["used"] = 30.0
    CA.apply_pool_guards(refresh=True)
    check("guards refresh the credit rate",
          abs(u1.credit_rate_per_day - 30.0 * scale) < 1e-6,
          u1.credit_rate_per_day)
    u1.info = None
    u1.credit_day = None
    u1.credit_rate_per_day = 0.0
    check("workbuddy routing inputs",
          WB.apply_routing_inputs({"wb-cn": 5.0}) == 1
          and wb_pool.get("wb-cn").credit_rate_per_day == 5.0,
          wb_pool.get("wb-cn").credit_rate_per_day)
    check("workbuddy routing inputs without rates",
          WB.apply_routing_inputs() == 0)
    wb_pool.get("wb-cn").credit_rate_per_day = 0.0

    print("== dispatch ==")
    pool.set_weight("u2", 5)
    picked = pool.pick("")
    check("weight layer wins", picked.uid == "u2", picked.uid)
    pool.set_weight("u2", 1)
    pool.set_weight("u1", 1)
    # 优先级分层压过层内的手动权重
    pool.set_priority("u1", 10)
    pool.set_priority("u2", 100)
    pool.set_weight("u2", 5)
    picked = pool.pick("")
    check("priority tier outranks weight", picked.uid == "u1", picked.uid)
    pool.set_weight("u2", 1)
    pool.set_priority("u1", 100)
    pool.set_priority("u2", 100)
    # 赠送积分里的临期部分（当天签到积分）按当天结束时清零
    u2.info = {"credits": {"total": {"remain": 50.0, "amount": 100.0,
                                     "used": 50.0, "expiring": 50.0},
                           "bonus": {"remain": 50.0, "amount": 100.0,
                                     "used": 50.0, "expiring": 50.0}},
               "end_date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 20 * 86400)),
               "package_name": "临期"}
    u2.info_fetched_at = time.time()
    u2.expiring_window_days = 7
    picked = pool.pick("")
    check("expiring account goes first", picked.uid == "u2", picked.uid)
    # 层内权重压过临期积分
    pool.set_weight("u1", 5)
    picked = pool.pick("")
    check("weight outranks expiring", picked.uid == "u1", picked.uid)
    pool.set_weight("u1", 1)
    u2.expiring_window_days = 0
    u2.info = None
    # 前面的断言已经记过接单次数；评分按最近一小时份额，先清掉再测份额分配。
    u1._pick_stamps.clear()
    u2._pick_stamps.clear()
    picks = [pool.pick("").uid for _ in range(4)]
    check("smart allocation shares load", len(set(picks)) == 2, picks)
    pool.set_priority("u1", 500)
    pool.set_priority("u2", 10)
    picked = pool.pick("")
    check("priority order", picked.uid == "u2", picked.uid)
    pool.set_priority("u1", 5)
    u1.disabled = True
    picked = pool.pick("")
    check("an unusable top tier falls through", picked.uid == "u2", picked.uid)
    u1.disabled = False
    pool.set_priority("u1", 100)
    pool.set_priority("u2", 100)
    check("session affinity binds", (pool.bind_session("sess-a", "u2"),
                                     pool.pick("", session_key="sess-a").uid)[1]
          == "u2")
    pool.demote_session("sess-a", "u2")
    check("demoted account not resumed",
          pool.pick("", session_key="sess-a").uid != "u2",
          pool.pick("", session_key="sess-a").uid)
    wb_settings.set_session_affinity(ACCOUNTS, False)
    pool.bind_session("sess-off", "u2")
    check("affinity switch off", pool.pick("", session_key="sess-off") is not None)
    wb_settings.set_session_affinity(ACCOUNTS, True)
    check("session affinity object reused",
          isinstance(pool.affinity, wb_accounts.SessionAffinity))

    print("== pinned account ==")
    body = {"model": "GLM-5.2",
            "messages": [{"role": "user", "content": "hi"}], "stream": False}
    stream, used_account, _effort = CA.open_stream(body, "pin-1", "codearts",
                                                    only_uid="u2")
    b"".join(stream)
    stream.close()
    check("only_uid pins the account", used_account.uid == "u2", used_account.uid)
    try:
        CA.open_stream(body, "pin-2", "codearts", only_uid="nope")
        check("unknown pinned account rejected", False)
    except wb_channels.ChannelError as exc:
        check("unknown pinned account rejected", exc.status == 503, exc.status)

    print("== ChannelError mapping ==")
    mapping = CA._error
    busy = mapping(C.CodeArtsBusy("busy"))
    check("busy mapping", busy.status == 429 and busy.wait == 1
          and busy.outcome == "rate_limited", (busy.status, busy.wait))
    cooling = mapping(C.CodeArtsCooling("cool", wait=9))
    check("cooling mapping", cooling.status == 429 and cooling.wait == 9
          and cooling.outcome == "rate_limited", cooling.wait)
    local = mapping(C.CodeArtsLocalNetwork("dns"))
    check("local network mapping", local.status == 503
          and local.local_network is True, (local.status, local.local_network))
    empty = mapping(C.CodeArtsNoAccounts("none"))
    check("no accounts mapping", empty.status == 503, empty.status)
    auth = mapping(C.CodeArtsAuthError("bad", status=403))
    check("auth mapping", auth.status == 403
          and auth.etype == "permission_error", (auth.status, auth.etype))
    auth401 = mapping(C.CodeArtsAuthError("bad", status=401))
    check("401 mapping", auth401.status == 401
          and auth401.etype == "authentication_error", auth401.etype)
    bad = mapping(C.CodeArtsBadRequest("bad", status=400))
    check("bad request mapping", bad.status == 400, bad.status)
    upstream = C.CodeArtsUpstreamError("boom", status=503)
    upstream.account_uid = "u1"
    mapped = mapping(upstream)
    check("upstream status preserved", mapped.status == 503
          and mapped.account_uid == "u1", (mapped.status, mapped.account_uid))
    limited = C.CodeArtsUpstreamError("slow down", status=429, code="429")
    limited.retry_after = 12
    mapped = mapping(limited)
    check("upstream 429 mapping", mapped.status == 429
          and mapped.wait == 12 and mapped.outcome == "rate_limited",
          (mapped.wait, mapped.outcome))
    transport = mapping(C.CodeArtsUnavailable("net"))
    check("transport mapping", transport.status == 502, transport.status)
    check("channel error attributes", hasattr(wb_channels.ChannelError("x"),
                                              "account_uid")
          and wb_channels.ChannelError("x").outcome == "failed")
finally:
    wb_proxy.POOL = None
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
