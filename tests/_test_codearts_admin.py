"""CodeArts 账号管理：提供方账号方法（经 wb_channels）与 OAuth 路由。

账号面板动作统一走提供方 API；这里直接调用同一套方法，避免再依赖已删除的
`/codearts/accounts/*` 管理路由。OAuth 路由仍在 `/codearts/accounts/oauth/*`。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-admin-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_channels  # noqa: E402
import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402
import wb_http  # noqa: E402
import wb_startup  # noqa: E402
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


def call(method, path, payload=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Panel-Token"] = token
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(HUB + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

hub = wb_startup.GatewayServer(("127.0.0.1", 0), wb_http.Handler)
threading.Thread(target=hub.serve_forever, daemon=True).start()
HUB = "http://127.0.0.1:%d" % hub.server_address[1]
HUB_PORT = hub.server_address[1]

try:
    wb_proxy.POOL = None
    wb_proxy.API_KEY = None
    C.save_config(ACCOUNTS, {})
    auth = {
        "user_id": "u1", "user_name": "账号一", "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }
    C.pool_for(ACCOUNTS).add(C.save_auth(ACCOUNTS, auth))

    provider = wb_channels.provider("codearts")
    check("provider resolved", provider.name == "codearts")
    check("owner finds codearts account", wb_channels.owner("u1") is provider)
    check("find_account returns pair",
          wb_channels.find_account("u1") == (provider, provider.find("u1")))
    check("unknown owner", wb_channels.owner("nope") is None)
    check("unknown pair", wb_channels.find_account("nope") == (None, None))
    check("accounts lists live objects",
          [acct.uid for acct in provider.accounts()] == ["u1"])
    views = provider.views("all")
    check("views include codearts", len(views) == 1 and
          views[0]["provider"] == "codearts", views)
    check("views realm filter",
          provider.views("intl") == [] and provider.views("codearts") != [])
    check("ready/total counts", provider.ready_count() == 1
          and provider.total_count() == 1)
    check("ready for foreign realm", provider.ready_count("intl") == 0)

    print("== panel login ==")
    status, body = call("POST", "/panel/login", {"password": "admin"})
    check("panel login", status == 200 and body.get("token"), (status, body))
    token = body.get("token")

    print("== update ==")
    updated = provider.update("u1", {"note": "hello", "tasksEnabled": False,
                                     "priority": 7, "group": "g1", "weight": 3,
                                     "concurrencyLimit": 2})
    check("update applied", updated["note"] == "hello"
          and updated["tasksEnabled"] is False and updated["priority"] == 7
          and updated["group"] == "g1" and updated["weight"] == 3
          and updated["concurrencyLimit"] == 2, updated)
    check("update identity", provider.update("u1", {"identity": "cli"})["identity"] == "cli")
    check("identity persisted",
          C.load_auths(ACCOUNTS)[0].get("client_type") == "cli")
    provider.update("u1", {"identity": "ide"})
    check("disabled models", provider.update(
        "u1", {"disabledModels": ["GLM-5.2", "glm-5.2"]})["disabledModels"]
        == ["glm-5.2"])
    check("disabled model blocks pick",
          C.pool_for(ACCOUNTS).pick("GLM-5.2") is not C.pool_for(ACCOUNTS).get("u1"))
    provider.update("u1", {"disabledModels": []})

    def rejects(label, changes, exc_type=ValueError):
        try:
            provider.update("u1", changes)
            check(label, False)
        except exc_type:
            check(label, True)

    rejects("unknown field rejected", {"bogus": 1})
    rejects("priority out of range", {"priority": 10000})
    rejects("weight out of range", {"weight": 0})
    rejects("concurrency out of range", {"concurrencyLimit": 1001})
    rejects("note too long", {"note": "x" * 201})
    rejects("identity invalid", {"identity": "nope"})
    rejects("proxy slot invalid", {"proxySlot": "nope"})
    rejects("empty change rejected", {})
    try:
        provider.update("nope", {"note": "x"})
        check("missing account rejected", False)
    except wb_channels.ChannelError as exc:
        check("missing account rejected", exc.status == 404, exc.status)
    provider.update("u1", {"tasksEnabled": True})
    view = provider.update("u1", {"enabled": False})
    check("disable stops tasks", view["enabled"] is False
          and view["tasksEnabled"] is False, view)
    view = provider.update("u1", {"enabled": True, "tasksEnabled": False})
    check("explicit tasks flag wins", view["enabled"] is True
          and view["tasksEnabled"] is False, view)
    provider.update("u1", {"tasksEnabled": True})

    print("== proxy slots ==")
    wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-1", "name": "one", "url": "http://127.0.0.1:7890",
         "enabled": True},
    ])
    bound = provider.update("u1", {"proxySlot": "slot-1"})
    check("proxy slot bound", bound["proxySlot"] == "slot-1"
          and bound["proxy"] == "http://127.0.0.1:7890", bound)
    check("proxy slot persisted",
          C.load_auths(ACCOUNTS)[0].get("proxySlot") == "slot-1",
          C.load_auths(ACCOUNTS)[0])
    direct = provider.update("u1", {"proxySlot": ""})
    check("proxy slot cleared", direct["proxySlot"] == ""
          and direct["proxy"] == "", direct)
    auto = provider.update("u1", {"proxySlot": "auto"})
    check("proxy slot auto", auto["proxySlot"] == "slot-1", auto)
    provider.update("u1", {"proxySlot": "slot-1"})
    status, body = call("POST", "/proxy/slots/save", {"slots": []}, token=token)
    check("proxy slots save", status == 200, (status, body))
    check("removed slot clears binding",
          not C.load_auths(ACCOUNTS)[0].get("proxySlot"),
          C.load_auths(ACCOUNTS)[0])

    print("== test / refresh ==")
    row = provider.test("u1")
    check("account test ok", row["ok"] is True and row["reply"] == "你好", row)
    C.save_config(ACCOUNTS, {"test_model": "GLM-4.7"})
    state.reset_requests()
    row = provider.test("u1")
    check("provider uses the configured test model",
          row["ok"] is True and row["model"] == "GLM-4.7", row)
    C.save_config(ACCOUNTS, {"test_model": "no-such-model"})
    state.reset_requests()
    row = provider.test("u1")
    check("provider reports an unavailable configured model",
          row["ok"] is False and "no-such-model" in row["error"], row)
    check("unavailable configured model sends no chat request",
          not [r for r in state.requests
               if r["path"] == "/api/v2/chat/completions"], state.requests)
    C.save_config(ACCOUNTS, {"test_model": ""})
    row = provider.test("u1")
    check("empty setting uses the account default model", row["ok"] is True, row)
    refreshed = provider.refresh_credential("u1")
    check("credential refreshed", refreshed["ok"] is True
          and refreshed["expiresAt"], refreshed)
    try:
        provider.test("nope")
        check("test missing account", False)
    except wb_channels.ChannelError as exc:
        check("test missing account", exc.status == 404)

    print("== credits / checkin ==")
    rows = provider.refresh_credits()
    check("credits refresh", len(rows) == 1 and rows[0]["ok"] is True
          and rows[0]["credits"]["remain"] == 4499.19, rows)
    missing = provider.refresh_credits(["nope"])
    check("credits refresh reports missing uid",
          len(missing) == 1 and missing[0]["ok"] is False
          and missing[0]["error"] == "account not found", missing)
    provider.update("u1", {"tasksEnabled": False})
    check("credit refresh honours task opt-out", provider.refresh_credits() == [])
    check("checkin honours task opt-out", provider.checkin() == [])
    provider.update("u1", {"tasksEnabled": True})
    rows = provider.checkin(["u1"])
    check("checkin row", len(rows) == 1 and rows[0]["ok"] is True
          and rows[0]["credits"] == 1000, rows)
    check("checkin row shape",
          set(rows[0]) == {"uid", "ok", "message", "credits"}, rows[0])
    missing_checkin = provider.checkin(["nope"])
    check("checkin reports missing uid",
          len(missing_checkin) == 1 and missing_checkin[0]["ok"] is False
          and missing_checkin[0]["message"] == "account not found",
          missing_checkin)
    rows = provider.refresh_credits(["u1"])
    check("credit refresh by uid", len(rows) == 1 and rows[0]["ok"] is True, rows)

    print("== enable all ==")
    changed = provider.set_all_enabled(False)
    check("disable all returns changed", changed == ["u1"], changed)
    check("disabled view", provider.views()[0]["enabled"] is False)
    check("disabled ready", provider.ready_count() == 0)
    check("enable all", provider.set_all_enabled(True) == ["u1"])
    check("enabled view", provider.views()[0]["enabled"] is True)
    check("foreign realm bulk is a no-op",
          provider.set_all_enabled(False, realm="intl") == []
          and provider.views()[0]["enabled"] is True)

    print("== export / import ==")
    rows = provider.export()
    check("export one row", len(rows) == 1 and rows[0]["provider"] == "codearts",
          rows)
    check("export carries secrets",
          bool(rows[0].get("refresh_token")) and bool(rows[0].get("dpop_private_key")),
          rows[0].keys())
    bare = provider.export(include_secrets=False)
    check("export without secrets",
          "refresh_token" not in bare[0] and "dpop_private_key" not in bare[0],
          bare[0].keys())
    duplicate = provider.import_rows(rows, dry_run=True)
    check("import dry-run skips existing",
          duplicate["imported"] == [] and duplicate["skipped"][0]["uid"] == "u1",
          duplicate)
    copy = dict(rows[0], uid="u2", nickname="账号二")
    added = provider.import_rows([copy])
    check("import adds account", added["imported"] == [{"uid": "u2", "action": "added"}],
          added)
    check("imported account readable", provider.find("u2") is not None
          and provider.find("u2").group == "g1",
          provider.find("u2") and provider.find("u2").group)
    overwritten = provider.import_rows([copy], overwrite=True)
    check("import overwrite updates",
          overwritten["imported"] == [{"uid": "u2", "action": "updated"}],
          overwritten)
    bad = provider.import_rows([{"provider": "codearts", "uid": "u3"}])
    check("import missing credentials rejected",
          bad["imported"] == [] and bad["errors"][0]["index"] == 1, bad)
    foreign = provider.import_rows([{"provider": "workbuddy", "uid": "wb"}])
    check("import other provider skipped",
          foreign["imported"] == [] and foreign["skipped"][0]["reason"]
          == "belongs to workbuddy", foreign)
    provider.delete("u2")
    check("imported account deleted", provider.find("u2") is None)
    try:
        provider.delete("u2")
        check("delete missing rejected", False)
    except wb_channels.ChannelError as exc:
        check("delete missing rejected", exc.status == 404)

    print("== models route ==")
    status, body = call("GET", "/panel/models?realm=codearts", token=token)
    check("panel codearts models", status == 200
          and body.get("realm") == "codearts"
          and body.get("source") in ("server", "bundled"), (status, body))
    check("panel codearts entries",
          bool(body.get("data")) and body["data"][0].get("owned_by") == "codearts",
          (body.get("data") or [])[:1])

    print("== settings validation ==")
    status, body = call("POST", "/settings/save", {
        "codearts": {"stream_timeout_seconds": 0},
    }, token=token)
    check("settings rejects out of range", status == 400, (status, body))

    print("== oauth ==")
    wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-1", "name": "one", "url": "http://127.0.0.1:7892",
         "enabled": True},
    ])
    status, body = call("POST", "/codearts/accounts/oauth/start",
                        {"proxySlot": "auto"}, token=token)
    check("oauth start", status == 200 and body.get("session_id"), (status, body))
    check("oauth start slot", body.get("proxySlot") == "slot-1", body)
    session_id = body["session_id"]
    # 这一节不建假代理，兑换走直连；槽的解析与持久化仍照常验证。
    C._find_session(session_id=session_id).proxy = ""
    status, body = call("POST", "/codearts/accounts/oauth/poll",
                        {"session_id": session_id}, token=token)
    check("oauth poll pending", status == 200 and body.get("status") == "pending",
          (status, body))
    status, body = call("POST", "/codearts/accounts/oauth/callback", {
        "session_id": session_id,
        "callback_url": "http://127.0.0.1:%d/oauth/callback?code=abc" % HUB_PORT,
    }, token=token)
    check("oauth callback done", status == 200 and body.get("status") == "done",
          (status, body))

    print("== browser callback ==")
    started = call("POST", "/codearts/accounts/oauth/start", {}, token=token)[1]
    with urllib.request.urlopen(HUB + "/oauth/callback?code=abc", timeout=20) as resp:
        html = resp.read().decode("utf-8", "replace")
    check("browser callback without state 200", resp.status == 200, resp.status)
    check("browser callback without state html", "登录成功" in html, html[:200])
    started2 = call("POST", "/codearts/accounts/oauth/start", {}, token=token)[1]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(started2["auth_url"]).query)
    ticket = (query.get("ticket_id") or [""])[0]
    req = urllib.request.Request(
        HUB + "/oauth/callback?code=abc&ticket_id=" + urllib.parse.quote(ticket))
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", "replace")
    check("browser callback 200", resp.status == 200, resp.status)
    check("browser callback html", "登录成功" in html, html[:200])
    check("browser callback saved account",
          C.pool_for(ACCOUNTS).get("u1") is not None)
    wb_settings.set_proxy_slots(ACCOUNTS, [])

    print("== delete ==")
    provider.delete("u1")
    check("account gone", provider.find("u1") is None)
    check("auth file gone", C.load_auths(ACCOUNTS) == [])
finally:
    try:
        hub.shutdown()
        hub.server_close()
    except Exception:
        pass
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
