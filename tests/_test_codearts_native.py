"""单进程 CodeArts：配置、账号文件、账号池、模型、续期、签到、错误分类、OAuth。"""
import io
import json
import os
import socket
import sys
import time
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-native-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]

import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_settings  # noqa: E402
from _fake_codearts import start_server, stop_server, verify_signature  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def make_auth(state, uid="u1", name="账号一", expiration=None):
    return {
        "user_id": uid,
        "user_name": name,
        "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key,
        "secret_access_key": state.secret_key,
        "expiration": expiration or state.expiration,
        "refresh_token": state.refresh_token,
        "code_verifier": "verifier",
        "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
        "updated_at": int(time.time()),
    }


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

try:
    print("== config ==")
    cfg = C.load_config(ACCOUNTS)
    check("max_concurrent removed", "max_concurrent" not in cfg, cfg)
    check("default client type", cfg["client_type"] == "ide")
    check("default checkin attempts", cfg["checkin_max_attempts"] == 3, cfg)
    check("default checkin gap", cfg["checkin_retry_gap_seconds"] == 3600.0, cfg)
    check("default checkin timeout", cfg["checkin_timeout_seconds"] == 30.0, cfg)
    C.save_config(ACCOUNTS, {"client_type": "cli", "checkin_max_attempts": 5,
                             "checkin_timeout_seconds": 45})
    cfg = C.load_config(ACCOUNTS)
    check("client type saved", cfg["client_type"] == "cli", cfg)
    check("checkin attempts saved", cfg["checkin_max_attempts"] == 5, cfg)
    check("checkin timeout saved", cfg["checkin_timeout_seconds"] == 45.0, cfg)

    def rejects(label, patch):
        try:
            C.save_config(ACCOUNTS, patch)
            check(label, False)
        except ValueError:
            check(label, True)

    rejects("invalid client type rejected", {"client_type": "nope"})
    rejects("unknown setting rejected", {"max_concurrent": 2})
    rejects("stream timeout out of range", {"stream_timeout_seconds": 0})
    rejects("models ttl out of range", {"models_ttl_seconds": 1})
    rejects("checkin attempts out of range", {"checkin_max_attempts": 99})
    rejects("checkin gap out of range", {"checkin_retry_gap_seconds": 99999})
    rejects("checkin timeout out of range", {"checkin_timeout_seconds": 0})
    rejects("benefit flag must be boolean", {"benefit_auto_claim": "yes"})
    rejects("encryption flag must be boolean", {"chat_encryption": "yes"})
    rejects("bad test model rejected", {"test_model": "glm 5.2"})
    C.save_config(ACCOUNTS, {"test_model": " GLM-4.7 "})
    check("test model saved", C.load_config(ACCOUNTS)["test_model"] == "GLM-4.7",
          C.load_config(ACCOUNTS)["test_model"])
    check("masked config exposes test model",
          C.masked_config(ACCOUNTS)["test_model"] == "GLM-4.7")
    C.save_config(ACCOUNTS, {"test_model": ""})
    check("empty test model clears",
          C.load_config(ACCOUNTS)["test_model"] == "")
    check("encryption on by default",
          C.load_config(ACCOUNTS)["chat_encryption"] is True)
    C.save_config(ACCOUNTS, {"chat_encryption": False})
    check("encryption switch saved",
          C.load_config(ACCOUNTS)["chat_encryption"] is False)
    C.save_config(ACCOUNTS, {"chat_encryption": True})
    check("encryption switch restored",
          C.load_config(ACCOUNTS)["chat_encryption"] is True)
    check("rejected values unchanged",
          C.load_config(ACCOUNTS)["stream_timeout_seconds"] == 600.0)
    C.save_config(ACCOUNTS, {"client_type": "ide"})
    check("masked no secrets", "api_key" not in C.masked_config(ACCOUNTS))
    check("masked no trigger fields", "max_concurrent" not in C.masked_config(ACCOUNTS))

    print("== legacy concurrency migration ==")
    legacy_dir = os.path.join(_TMP.name, "legacy-accounts")
    os.makedirs(legacy_dir, exist_ok=True)
    wb_settings.save(legacy_dir, {"codearts": {"max_concurrent": 3}})
    C.save_auth(legacy_dir, make_auth(state, uid="legacy-1", name="旧账号"))
    legacy_pool = C.pool_for(legacy_dir)
    legacy_acct = legacy_pool.get("legacy-1")
    check("legacy limit inherited", legacy_acct.concurrency_limit == 3,
          legacy_acct.concurrency_limit)
    check("legacy setting removed from disk",
          "max_concurrent" not in (wb_settings.load(legacy_dir).get("codearts") or {}),
          wb_settings.load(legacy_dir).get("codearts"))
    legacy_pool.save_state()
    reopened = C.CodeArtsPool(legacy_dir, C.load_config(legacy_dir))
    check("migrated value persisted",
          reopened.get("legacy-1").concurrency_limit == 3,
          reopened.get("legacy-1").concurrency_limit)
    C.invalidate_pool(legacy_dir)

    print("== auth storage ==")
    auth = make_auth(state)
    saved = C.save_auth(ACCOUNTS, auth)
    check("auth file exists", os.path.isfile(saved["_path"]), saved["_path"])
    loaded = C.load_auths(ACCOUNTS)
    check("auth loaded", len(loaded) == 1 and loaded[0]["user_id"] == "u1")
    check("dpop persisted", isinstance(loaded[0].get("dpop_private_key"), dict))

    print("== pool ==")
    pool = C.pool_for(ACCOUNTS)
    acct = pool.add(saved)
    check("pool has account", pool.get("u1") is acct)
    check("addedAt recorded", acct.auth.get("added_at", 0) > 0
          and acct.view()["addedAt"] == acct.auth["added_at"],
          acct.auth.get("added_at"))
    check("stats healthy", pool.stats()["healthy"] == 1, pool.stats())
    check("default concurrency limit", acct.concurrency_limit == 1)
    check("pick", pool.pick("GLM-5.2") is acct)
    check("acquire once", pool.acquire(acct) is True)
    check("second acquire blocked at limit 1", pool.acquire(acct) is False)
    pool.release(acct)
    check("release", acct.active_concurrent == 0)
    pool.set_concurrency_limit("u1", 2)
    check("limit saved", acct.concurrency_limit == 2)
    check("acquire under limit", pool.acquire(acct) is True)
    check("acquire up to limit", pool.acquire(acct) is True)
    check("acquire above limit", pool.acquire(acct) is False)
    pool.release(acct)
    pool.release(acct)
    pool.set_concurrency_limit("u1", 0)
    check("legacy zero is capped at the upstream session limit",
          acct.concurrency_limit == 3
          and [pool.acquire(acct) for _ in range(4)] == [True, True, True, False])
    for _ in range(3):
        pool.release(acct)
    try:
        C.update_account(ACCOUNTS, "u1", {"concurrencyLimit": 4})
        check("limit above 3 rejected", False)
    except ValueError:
        check("limit above 3 rejected", True)
    pool.disable("u1", "manual")
    check("disabled", acct.disabled is True and pool.stats()["disabled"] == 1)
    pool.enable("u1")
    check("enabled again", acct.disabled is False)

    print("== failure cooldown ==")
    pool.note_failure(acct, "boom")
    pool.note_failure(acct, "boom")
    check("below threshold stays healthy", acct.healthy() is True, acct.err_count)
    pool.note_failure(acct, "boom")
    check("cooldown after threshold", acct.cool_until > time.time())
    check("cooling account not healthy", acct.healthy() is False)
    check("blocked reason names cooling", "冷却" in acct.blocked_reason(), acct.blocked_reason())
    pool.clear_cooldown("u1")
    check("cooldown cleared", acct.cool_until == 0)
    import threading as _threading
    workers = [_threading.Thread(target=pool.note_failure,
                                 args=(acct, "concurrent")) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    with open(pool.state_file, encoding="utf-8") as fh:
        persisted = json.load(fh)
    check("concurrent state writes stay valid",
          isinstance(persisted, list) and persisted
          and persisted[0]["name"] == "u1", persisted[:1])
    pool.clear_cooldown("u1")

    print("== no lazy jobs ==")
    with mock.patch.object(C.threading, "Thread") as worker:
        C.pool_for(ACCOUNTS)
        C.masked_config(ACCOUNTS)
        C.configured(ACCOUNTS)
        check("pool access spawns no worker", not worker.called)
    pool.config["credits_auto_claim"] = True
    pool.set_tasks_enabled("u1", False)
    check("task opt-out excludes checkin due", C.checkin_due(C.load_config(ACCOUNTS)) == [])
    acct.auth["expiration"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    check("task opt-out excludes keepalive due",
          C.keepalive_due(C.load_config(ACCOUNTS)) == [])
    before = state.token_calls
    result = C.keepalive(C.load_config(ACCOUNTS))
    check("task opt-out skips keepalive refresh",
          result["refreshed"] == 0 and state.token_calls == before, result)
    check("task opt-out skips credit refresh",
          C.refresh_credits(C.load_config(ACCOUNTS)) == [])
    pool.set_tasks_enabled("u1", True)
    result = C.keepalive(C.load_config(ACCOUNTS))
    check("keepalive refreshes opted-in account", result["refreshed"] == 1, result)
    check("token restored", acct.expires_at() > time.time())
    pool.config["credits_auto_claim"] = False

    print("== model id identity ==")
    acct.models = [
        {"id": "deepseek-v4.1-flash", "max_tokens": 1000},
        {"id": "deepseek-v4.1-flash-1017", "max_tokens": 2000},
    ]
    check("1017 canonical id stays exact",
          C._canonical_model(acct, "DEEPSEEK-V4.1-FLASH-1017")
          == "deepseek-v4.1-flash-1017")
    check("base canonical id stays exact",
          C._canonical_model(acct, "DEEPSEEK-V4.1-FLASH")
          == "deepseek-v4.1-flash")
    check("1017 has its own output limit",
          C._model_output_limit(acct, "deepseek-v4.1-flash-1017") == 2000)
    check("base has its own output limit",
          C._model_output_limit(acct, "deepseek-v4.1-flash") == 1000)

    print("== proxy slots ==")
    import urllib.request
    slots = wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-1", "name": "one", "url": "http://127.0.0.1:7890",
         "enabled": True},
        {"id": "slot-2", "name": "two", "url": "http://127.0.0.1:7891",
         "enabled": False},
    ])
    acct.proxy_slot = "slot-1"
    acct.auth["proxySlot"] = "slot-1"
    acct.auth = C.save_auth(ACCOUNTS, acct.auth)
    pool.apply_proxy_slots(slots)
    check("proxy slot resolved", acct.proxy == "http://127.0.0.1:7890",
          acct.proxy)
    acct.proxy_slot = "slot-2"
    pool.apply_proxy_slots(slots)
    check("disabled slot falls back direct", acct.proxy == "", acct.proxy)
    acct.proxy_slot = ""
    pool.apply_proxy_slots(slots)
    check("unbound slot is direct", acct.proxy == "", acct.proxy)
    acct.proxy_slot = "slot-1"
    pool.apply_proxy_slots(slots)
    opener = C._opener(acct.proxy)
    handler = next(h for h in opener.handlers
                   if isinstance(h, urllib.request.ProxyHandler))
    check("opener pins http/https",
          handler.proxies.get("http") == acct.proxy
          and handler.proxies.get("https") == acct.proxy, handler.proxies)
    direct_proxies = [h.proxies for h in C._opener("").handlers
                      if isinstance(h, urllib.request.ProxyHandler)]
    check("direct opener ignores env proxies",
          all(not proxies for proxies in direct_proxies), direct_proxies)
    seen = []
    orig_json = C._json_http
    C._json_http = lambda method, url, headers=None, body=None, \
        timeout=30.0, proxy="": (seen.append(proxy) or {})
    C.signed_get(acct, "http://example.test")
    C._json_http = orig_json
    check("signed_get threads account proxy", seen == [acct.proxy], seen)
    # Re-login writes a fresh auth document without panel-side fields; the
    # existing slot binding must survive and be written back.
    relogin = dict(acct.auth)
    relogin.pop("proxySlot", None)
    relogin = C.save_auth(ACCOUNTS, relogin)
    pool.add(relogin)
    check("relogin keeps proxy binding",
          pool.get("u1").proxy_slot == "slot-1",
          pool.get("u1").proxy_slot)
    check("relogin persists proxy binding",
          C.load_auths(ACCOUNTS)[0].get("proxySlot") == "slot-1",
          C.load_auths(ACCOUNTS)[0])
    check("relogin re-resolves proxy",
          pool.get("u1").proxy == "http://127.0.0.1:7890",
          pool.get("u1").proxy)
    # A real HTTP proxy must receive the absolute-form request line.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    proxied = []

    class ProxyProbe(BaseHTTPRequestHandler):
        def do_GET(self):
            proxied.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    proxy_srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyProbe)
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()
    proxy_url = "http://127.0.0.1:%d" % proxy_srv.server_address[1]
    status, raw = C._http("GET", "http://codearts.invalid/probe",
                          proxy=proxy_url)
    proxy_srv.shutdown()
    proxy_srv.server_close()
    check("http request goes through proxy", status == 200 and raw == b"{}",
          (status, raw))
    check("proxy saw absolute URI",
          proxied == ["http://codearts.invalid/probe"], proxied)
    # Do not leave the fake proxy bound for the chat tests below.
    acct.proxy_slot = ""
    acct.auth.pop("proxySlot", None)
    acct.auth = C.save_auth(ACCOUNTS, acct.auth)
    wb_settings.set_proxy_slots(ACCOUNTS, [])
    pool.apply_proxy_slots([])

    print("== models ==")
    models = C.fetch_models(acct)
    ids = [m["id"] for m in models]
    check("agent/builtin/benefit merged", set(ids) ==
          {"GLM-5.2", "GLM-4.7", "Qwen3-VL-235B",
           "deepseek-v4-pro-0901"}, ids)
    check("benefit marked", acct.is_benefit("deepseek-v4-pro-0901") is True)
    check("normal not benefit", acct.is_benefit("GLM-5.2") is False)
    agent_req = next((r for r in state.requests
                      if r["path"] == "/v1/agent-center/agents/useragents"), None)
    check("agent catalog uses AgentCenter",
          bool(agent_req) and agent_req["headers"].get("agent-type") == "AgentCenter",
          agent_req)
    auth_header = str(state.last_headers.get("authorization") or "")
    signed_part = auth_header.split("SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("Agent-Type and X-Language stay unsigned",
          "agent-type" not in signed_part and "x-language" not in signed_part,
          signed_part)
    entries = C.model_entries(C.load_config(ACCOUNTS))
    check("entries owned_by", entries[0]["owned_by"] == "codearts")
    check("entries benefit flag",
          any(e.get("benefit") for e in entries if e["id"] == "deepseek-v4-pro-0901"))
    check("models meta source", C.models_meta(C.load_config(ACCOUNTS))["source"] == "codearts")

    print("== credits ==")
    info = C.fetch_account_info(acct)
    check("credit package", info["is_credit_package"] is True, info)
    check("credit package name", info["package_name"] == "体验版", info)
    check("credit total", info["credits"]["total"]["remain"] == 4499.19, info)
    check("credit used", info["credits"]["total"]["used"] == 0.81, info)
    check("credit models", info["models"][0]["name"] == "GLM-5.2", info)

    def stats_request(client_type):
        acct.auth["client_type"] = client_type
        C.fetch_account_info(acct)
        req = [r for r in state.requests if r["path"] == "/snap-manager/v1/statistics/plugin"][-1]
        signed = req["headers"]["authorization"].split("SignedHeaders=", 1)[1].split(",", 1)[0]
        return signed, req["headers"]

    original_type = acct.auth.get("client_type")
    signed, headers = stats_request("ide")
    check("IDE statistics request signs the SDK identity headers",
          signed == "host;ide-name;ide-version;plugin-name;plugin-version;"
                    "x-auth-token;x-sdk-date;x-security-token"
          and headers["plugin-name"] == "snap_vscode"
          and "agent-type" not in headers and "x-sdk-content-sha256" not in headers,
          (signed, headers))
    signed, headers = stats_request("cli")
    check("CLI statistics request signs only Agent-Type",
          signed == "agent-type;host;x-sdk-date;x-security-token"
          and headers["user-agent"] == "Bun/1.3.14"
          and "content-type" not in headers and "x-language" not in headers,
          (signed, headers))
    if original_type is None:
        acct.auth.pop("client_type", None)
    else:
        acct.auth["client_type"] = original_type
    view = acct.view()
    check("view credits remain", view["credits"]["remain"] == 4499.19, view["credits"])
    check("view credits size maps amount", view["credits"]["size"] == 4500.0,
          view["credits"])
    check("view credits updated_at", view["credits"]["updated_at"] > 0, view["credits"])
    check("view credit expiries",
          view["creditExpiries"] and view["creditExpiries"][0]["at"] > time.time(),
          view["creditExpiries"])
    check("view models list", isinstance(view["models"], list)
          and "GLM-5.2" in view["models"], view["models"])
    check("view free models", "deepseek-v4-pro-0901" in view["freeModels"],
          view["freeModels"])
    check("view identity options",
          [item["value"] for item in view["identityOptions"]] == ["ide", "cli"],
          view["identityOptions"])
    check("daily credit baseline", acct.credit_day["day"]
          == time.strftime("%Y-%m-%d") and acct.daily_credits_today == 0.0,
          acct.credit_day)
    acct.info["credits"]["total"]["used"] = 5.81
    check("daily credit growth", acct.daily_credits_today == 5.0,
          acct.daily_credits_today)
    acct.info["credits"]["total"]["used"] = 0.81

    print("== refresh ==")
    acct.auth["expiration"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 10))
    before = state.token_calls
    C._ensure_token(acct)
    check("refresh called", state.token_calls > before)
    check("token restored", acct.expires_at() > time.time())
    check("auth file rewritten", C.load_auths(ACCOUNTS)[0]["expiration"] ==
          acct.auth["expiration"])
    forced = C.refresh_credential(C.load_config(ACCOUNTS), "u1")
    check("forced refresh ok", forced["ok"] is True and forced["expiresAt"],
          forced)

    print("== keepalive jitter ==")
    expired = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - 10))
    acct.auth["expiration"] = expired
    acct.auth = C.save_auth(ACCOUNTS, acct.auth)
    other = pool.add(C.save_auth(ACCOUNTS, make_auth(
        state, uid="u2", name="账号二", expiration=expired)))
    sleeps = []
    result = C.keepalive(C.load_config(ACCOUNTS),
                         gap_sleep=lambda: sleeps.append(1))
    check("keepalive covers both accounts",
          result["checked"] == 2 and result["refreshed"] == 2, result)
    check("keepalive gap runs once", sleeps == [1], sleeps)
    check("keepalive_account row", C.keepalive_account(
        C.load_config(ACCOUNTS), "u2")["refreshed"] == 0)
    pool.remove("u2")
    check("second account removed", pool.get("u2") is None)
    check("keepalive clears due",
          C.keepalive_due(C.load_config(ACCOUNTS)) == [])

    print("== content parts ==")
    parts_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}],
    }, "sess-parts")
    check("multiple text parts stay an array",
          parts_body["messages"][0]["content"] == [
              {"type": "text", "text": "hello "},
              {"type": "text", "text": "world"}], parts_body)
    single_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "hello"}]}],
    }, "sess-single-part")
    check("single text part becomes a string",
          single_body["messages"][0]["content"] == "hello", single_body)
    image_body = C.build_chat_body({"model": "Qwen3-VL-235B",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AAA"}},
        ]}]}, "sess-image")
    image_parts = image_body["messages"][0]["content"]
    check("image parts preserved", isinstance(image_parts, list)
          and image_parts[0] == {"type": "text", "text": "look"}
          and image_parts[1] == {"type": "image_url",
                                 "image_url": {"url": "data:image/png;base64,AAA"}},
          image_body)
    anthropic_body = C.build_chat_body({"model": "Qwen3-VL-235B",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": "BBB"}},
        ]}]}, "sess-anthropic-image")
    anthropic_parts = anthropic_body["messages"][0]["content"]
    check("anthropic image normalized",
          anthropic_parts == [{"type": "image_url", "image_url": {
              "url": "data:image/jpeg;base64,BBB"}}], anthropic_body)

    print("== message encoding ==")
    tool_message_body = C.build_chat_body({
        "model": "deepseek-v4.1-flash-1017",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "ping", "arguments": {"a": 1}}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "pong"},
        ],
    }, "sess-message-encoding")
    encoded = tool_message_body["messages"]
    check("system message shape",
          list(encoded[0]) == ["role", "content"]
          and encoded[0]["content"] == "sys", encoded[0])
    check("user message shape",
          list(encoded[1]) == ["role", "content"], encoded[1])
    check("assistant key order",
          list(encoded[2]) == ["role", "content", "reasoning_content",
                              "tool_calls"], encoded[2])
    check("deepseek reasoning content always sent",
          encoded[2]["reasoning_content"] == "", encoded[2])
    check("assistant arguments are a compact string",
          encoded[2]["tool_calls"][0]["function"]["arguments"] == '{"a":1}',
          encoded[2]["tool_calls"])
    check("tool message shape",
          list(encoded[3]) == ["role", "tool_call_id", "content"]
          and encoded[3]["content"] == "pong", encoded[3])
    safe_prompt = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content":
                "  line one\n<system-reminder>secret</system-reminder>"
                " line two\t" + "x" * 600},
        ],
    }, "sess-user-prompt")
    prompt = safe_prompt["user_prompt"]
    check("user prompt strips reminders and whitespace",
          "secret" not in prompt and "\n" not in prompt
          and "\t" not in prompt and prompt.startswith("line one")
          and len(prompt) == 500, (len(prompt), prompt[:80]))
    check("model name encoding",
          C.wire.encode_model_name("a b/c") == "a%20b%2Fc",
          C.wire.encode_model_name("a b/c"))

    print("== reasoning effort ==")
    effort_body = C.build_chat_body({
        "model": "GLM-5.2",
        "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-effort")
    check("effort forwarded", effort_body.get("reasoning_effort") == "high",
          effort_body)
    thinking_body = C.build_chat_body({
        "model": "GLM-5.2",
        "thinking": {"type": "enabled"},
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-thinking")
    check("thinking maps to high",
          thinking_body.get("reasoning_effort") == "high", thinking_body)
    try:
        auto_body = C.build_chat_body({
            "model": "GLM-5.2", "reasoning_effort": "auto",
            "messages": [{"role": "user", "content": "hi"}],
        }, "sess-bad-effort")
        check("effort sent as client wrote it",
              auto_body.get("reasoning_effort") == "auto", auto_body)
    except C.CodeArtsBadRequest:
        check("effort sent as client wrote it", False)
    always_body = C.build_chat_body({
        "model": "glm-5.3-flash", "reasoning_effort": "none",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-always")
    check("always-thinking keeps the client level",
          always_body.get("reasoning_effort") == "none", always_body)
    forced_body = C.build_chat_body({
        "model": "gpt-5.1", "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "a", "parameters": {}}}],
    }, "sess-gpt5-tools")
    forced_body = C._finalize_chat_body(acct, forced_body, "gpt-5.1")
    check("gpt-5 with tools forces effort none",
          forced_body.get("reasoning_effort") == "none", forced_body)
    check("gpt-5 uses max_completion_tokens",
          "max_completion_tokens" in forced_body
          and "max_tokens" not in forced_body, forced_body)
    sampling_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "stop": "END", "seed": 7, "top_k": 5,
        "temperature": 0.5, "top_p": 0.9, "parallel_tool_calls": True,
        "frequency_penalty": 0.2, "presence_penalty": 0.1,
        "response_format": {"type": "json_object"},
    }, "sess-sampling")
    check("official sampling fields forwarded",
          sampling_body.get("stop") == ["END"]
          and sampling_body.get("seed") == 7
          and sampling_body.get("frequency_penalty") == 0.2
          and sampling_body.get("presence_penalty") == 0.1
          and sampling_body.get("response_format") == {"type": "json_object"},
          sampling_body)
    check("unsent sampling fields dropped",
          "top_k" not in sampling_body
          and "temperature" not in sampling_body
          and "top_p" not in sampling_body
          and "parallel_tool_calls" not in sampling_body,
          sampling_body)

    print("== output token clamp ==")
    body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 384000,
    }, "sess-clamp")
    stream, _acct, _model = C.open_chat(C.load_config(ACCOUNTS), body,
                                        "sess-clamp")
    b"".join(stream)
    stream.close()
    sent = state.last_body or {}
    check("output tokens clamped to advertised max",
          sent.get("max_tokens") == 8192, sent)
    check("completion spelling removed",
          "max_completion_tokens" not in sent, sent)
    bad = None
    try:
        C.build_chat_body({
            "model": "GLM-5.2",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": "abc",
        }, "sess-bad-max")
        check("invalid max_tokens rejected locally", False)
    except C.CodeArtsBadRequest:
        check("invalid max_tokens rejected locally", True)
    check("invalid parameter did not cool the account",
          acct.cool_until == 0, acct.cool_until)

    embedded = C.CodeArtsStream(
        io.BytesIO(b'data: {"error_code":"invalid_param","error_msg":'
                   b'"The request param is invalid, Please check it"}\n\n'),
        "GLM-5.2", acct, acct.pool)
    try:
        embedded.preflight()
        check("embedded parameter error is a 400", False)
    except C.CodeArtsBadRequest as exc:
        check("embedded parameter error is a 400", exc.status == 400, exc.status)

    state.embedded_error = {"error_code": "500", "error_msg": "boom"}
    try:
        C.open_chat(C.load_config(ACCOUNTS),
                    C.build_chat_body({"model": "GLM-5.2",
                                       "messages": [{"role": "user",
                                                     "content": "hi"}]},
                                      "sess-preflight"),
                    "sess-preflight")
        check("embedded failure raises", False)
    except C.CodeArtsUpstreamError:
        check("embedded failure raises", True)
    state.embedded_error = None
    check("failed preflight releases the slot", acct.active_concurrent == 0,
          acct.active_concurrent)
    pool.clear_cooldown("u1")

    stream = C.CodeArtsStream(io.BytesIO(b'data: {"text":"hello"}\n\ndata: [DONE]\n\n'),
                              "model", acct, acct.pool)
    stream.preflight()
    result = b''.join(stream)
    check("first SSE text normalized", b'"content": "hello"' in result, result)
    # 非流式路径先聚合再回包：上游读完就得归还名额，否则客户端紧接着的请求会被判成全忙。
    pool.acquire(acct)
    drained = C.CodeArtsStream(io.BytesIO(b'data: {"text":"done"}\n\ndata: [DONE]\n\n'),
                               "model", acct, acct.pool)
    drained.preflight()
    b"".join(drained)
    check("exhausted stream returns the slot before close",
          acct.active_concurrent == 0, acct.active_concurrent)
    drained.close()
    check("close after exhaustion does not release again",
          acct.active_concurrent == 0, acct.active_concurrent)
    pool.set_concurrency_limit("u1", 2)
    pool.acquire(acct)               # 另一个在途请求
    pool.acquire(acct)               # 这个流自己占的名额
    probe = C.CodeArtsStream(io.BytesIO(b""), "model", acct, acct.pool)
    probe.close()
    probe.close()
    check("stream close only releases once", acct.active_concurrent == 1,
          acct.active_concurrent)
    pool.release(acct)
    pool.set_concurrency_limit("u1", 1)

    print("== named tool choice ==")
    tool_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "a", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "parameters": {}}},
        ],
        "tool_choice": {"type": "function", "function": {"name": "b"}},
    }, "sess-tool")
    check("named tool choice narrows the tools and requires the call",
          tool_body["tool_choice"] == "required"
          and [t["function"]["name"] for t in tool_body["tools"]] == ["b"], tool_body)
    auto_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function",
                   "function": {"name": "a", "parameters": {}}}],
    }, "sess-tool-auto")
    check("tools without choice send auto",
          auto_body.get("tool_choice") == "auto", auto_body)

    print("== malformed tools ==")
    dirty_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "ok", "parameters": {}}},
            {"type": "function"},
            {"type": "web_search"},
            {"type": "function", "function": {"name": "no_params"}},
            {"type": "function", "function": {"name": "ok"}},
            {"name": "flat", "description": "d"},
        ],
    }, "sess-dirty-tools")
    names = [t["function"]["name"] for t in dirty_body.get("tools") or []]
    check("unusable tools dropped", names == ["ok", "no_params", "flat"], names)
    check("missing parameters defaulted",
          dirty_body["tools"][1]["function"]["parameters"]
          == {"type": "object", "properties": {}}, dirty_body)
    check("flat tool wrapped",
          dirty_body["tools"][2]["type"] == "function"
          and dirty_body["tools"][2]["function"]["parameters"]
          == {"type": "object", "properties": {}}, dirty_body)
    empty_tools_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}, {"type": "web_search"}],
        "tool_choice": "auto",
    }, "sess-empty-tools")
    check("all-unusable tools removed",
          "tools" not in empty_tools_body and "tool_choice" not in empty_tools_body,
          empty_tools_body)

    print("== encrypted error bodies ==")
    state.error_encoding = "gzip"
    state.chat_error = (400, {"error_msg": "The request param is invalid, Please check it"})
    state.chat_error_remaining = -1
    try:
        C.open_chat(C.load_config(ACCOUNTS),
                    C.build_chat_body({"model": "GLM-5.2",
                                       "messages": [{"role": "user",
                                                     "content": "hi"}]},
                                      "sess-enc-400"), "sess-enc-400")
        check("encrypted 400 raises", False)
    except C.CodeArtsBadRequest as exc:
        check("encrypted 400 message decoded",
              "request param is invalid" in str(exc), exc)
    state.chat_error = (400, {"error_msg": "并发会话数已达上限(3个)，请关闭部分会话后重试。"})
    state.chat_error_remaining = 1
    stream, current, _model = C.open_chat(
        C.load_config(ACCOUNTS),
        C.build_chat_body({"model": "GLM-5.2",
                           "messages": [{"role": "user", "content": "hi"}]},
                          "sess-enc-concurrency"), "sess-enc-concurrency")
    joined = b"".join(stream)
    stream.close()
    check("encrypted concurrency error retried",
          "你好" in joined.decode("utf-8", "replace"), joined[:120])
    state.chat_error = (429, {"error_msg": "TPM limit reached"})
    state.chat_headers = {"Retry-After": "42"}
    state.chat_error_remaining = -1
    try:
        C.open_chat(C.load_config(ACCOUNTS),
                    C.build_chat_body({"model": "GLM-5.2",
                                       "messages": [{"role": "user",
                                                     "content": "hi"}]},
                                      "sess-enc-429"), "sess-enc-429")
        check("encrypted 429 raises", False)
    except C.CodeArtsUpstreamError as exc:
        check("encrypted 429 retry-after kept",
              getattr(exc, "retry_after", None) == 42,
              getattr(exc, "retry_after", None))
    state.chat_error = None
    state.chat_headers = {}
    state.chat_error_remaining = -1
    state.error_encoding = ""
    pool.clear_cooldown("u1")
    pool.note_success(acct, model="GLM-5.2")
    acct.model_cooldowns.clear()

    print("== concurrency retry ==")
    state.chat_error = (400, {"error_msg": "并发会话数已达上限(3个)，请关闭部分会话后重试。"})
    state.chat_error_remaining = 1
    body = C.build_chat_body({"model": "GLM-5.2",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}, "sess-retry")
    stream, acct3, model3 = C.open_chat(C.load_config(ACCOUNTS), body, "sess-retry")
    raw = b"".join(stream)
    stream.close()
    check("concurrency retry succeeds", "你好" in raw.decode("utf-8", "replace"),
          raw[:200])
    state.chat_error = None
    state.chat_error_remaining = -1

    print("== session header ==")

    def _chat_of(session_key, messages=None, client_type=None):
        if client_type:
            acct.auth["client_type"] = client_type
        body = C.build_chat_body({
            "model": "GLM-5.2",
            "messages": messages or [{"role": "user", "content": "hi"}],
            "stream": False,
        }, session_key)
        stream, _acct, _model = C.open_chat(C.load_config(ACCOUNTS), body, session_key)
        b"".join(stream)
        stream.close()
        for req in reversed(state.requests):
            if req["path"].endswith("/chat/completions"):
                return (req["headers"], req["header_list"], state.last_body,
                        state.last_signature_ok, state.last_encrypted)
        return {}, [], {}, False, False

    def _is_official_id(value, prefix):
        text = str(value or "")
        start = len(prefix) + 1
        return (text.startswith(prefix + "_") and len(text) == start + 26
                and all(c in "0123456789abcdef" for c in text[start:start + 12])
                and all(c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                        for c in text[start + 12:]))

    ot_first, order_first, body_first, sig_first, enc_first = _chat_of("sess-ot-1")
    ot_repeat, _order, _body, _sig, _enc = _chat_of("sess-ot-1")
    ot_second, _order2, body_second, _sig2, _enc2 = _chat_of("sess-ot-2")
    ot_turn2, _order3, _body3, _sig3, _enc3 = _chat_of(
        "sess-ot-1", [{"role": "user", "content": "hi"},
                      {"role": "assistant", "content": "hello"},
                      {"role": "user", "content": "again"}])
    ses_id = ot_first.get("x-ot-session-id") or ""
    check("session id format", _is_official_id(ses_id, "ses"), ses_id)
    check("session id stable per conversation",
          ses_id == (ot_repeat.get("x-ot-session-id") or ""), ses_id)
    check("session id differs per conversation",
          ses_id != (ot_second.get("x-ot-session-id") or ""),
          (ses_id, ot_second.get("x-ot-session-id")))
    check("user-session-id mirrors it",
          ot_first.get("user-session-id") == ses_id, ot_first.get("user-session-id"))
    check("parent session id empty",
          ot_first.get("x-ot-parent-session-id") == "",
          ot_first.get("x-ot-parent-session-id"))
    check("message id format", _is_official_id(ot_first.get("user-msg-id"), "msg"),
          ot_first.get("user-msg-id"))
    check("message id stable across retry",
          ot_first.get("user-msg-id") == ot_repeat.get("user-msg-id"),
          (ot_first.get("user-msg-id"), ot_repeat.get("user-msg-id")))
    check("message id changes per user turn",
          ot_first.get("user-msg-id") != ot_turn2.get("user-msg-id"),
          (ot_first.get("user-msg-id"), ot_turn2.get("user-msg-id")))
    check("trace id stable across retry",
          ot_first.get("x-ot-trace-id") == ot_repeat.get("x-ot-trace-id"),
          (ot_first.get("x-ot-trace-id"), ot_repeat.get("x-ot-trace-id")))
    check("trace id changes per turn",
          ot_first.get("x-ot-trace-id") != ot_turn2.get("x-ot-trace-id"),
          (ot_first.get("x-ot-trace-id"), ot_turn2.get("x-ot-trace-id")))
    check("span differs per request",
          ot_first.get("x-ot-span-id") != ot_repeat.get("x-ot-span-id"),
          (ot_first.get("x-ot-span-id"), ot_repeat.get("x-ot-span-id")))
    snap_first = ot_first.get("x-snap-traceid") or ""
    snap_repeat = ot_repeat.get("x-snap-traceid") or ""
    check("snap trace format",
          len(snap_first) == 49 and snap_first[32] == "_"
          and all(c in "0123456789abcdef" for c in snap_first.replace("_", "")),
          snap_first)
    check("snap trace prefix per turn, suffix per request",
          snap_first[:32] == snap_repeat[:32] and snap_first[33:] != snap_repeat[33:],
          (snap_first, snap_repeat))
    check("official IDE client type",
          ot_first.get("x-ot-client-type") == "IDE"
          and ot_first.get("x-ot-client-version") == "26.10.300",
          ot_first)
    check("official IDE function and plugin headers",
          ot_first.get("x-ot-function") == "agent-general"
          and ot_first.get("plugin-name") == "codemate_vscode"
          and ot_first.get("ide-name") == "VSCode"
          and ot_first.get("ide-version") == "26.10.300", ot_first)
    check("IDE chat user agent",
          ot_first.get("user-agent")
          == "ai-sdk/provider-utils/4.0.1 runtime/bun/1.3.14", ot_first)
    check("model name url encoded",
          ot_first.get("model-name") == "GLM-5.2", ot_first)
    check("encrypted chat signature", sig_first, ot_first.get("authorization"))
    check("encrypted chat header order",
          [name for name, _value in order_first] == [
              "Authorization", "Content-Type", "Host", "User-Agent",
              "X-Encrypt-Algo", "X-Encrypt-Iv", "X-Encrypt-Key-Id",
              "X-Encrypt-Tag", "X-Encrypt-Ts", "X-Sdk-Content-Sha256",
              "client-ip", "created-time", "ide-name", "ide-version",
              "model-id", "model-name", "plugin-name", "plugin-version",
              "user-msg-id", "user-session-id", "x-language",
              "x-ot-client-type", "x-ot-client-version", "x-ot-function",
              "x-ot-parent-session-id", "x-ot-session-id", "x-ot-span-id",
              "x-ot-trace-id", "x-sdk-date", "x-security-token",
              "x-snap-traceid", "Connection", "Accept", "Accept-Encoding",
              "Content-Length"], order_first)
    signed = ot_first.get("authorization", "").split("SignedHeaders=", 1)[-1] \
        .split(",", 1)[0]
    check("encrypted signed headers", signed ==
          "content-type;host;x-sdk-content-sha256;x-sdk-date;x-security-token",
          signed)
    check("encrypted content type",
          ot_first.get("content-type") == "application/octet-stream"
          and enc_first is True, ot_first.get("content-type"))
    check("body key order", list(body_first) ==
          ["model", "max_tokens", "tool_stream", "messages", "stream",
           "user_prompt"], body_first)
    check("no chat id or cache key",
          "chat_id" not in body_first and "prompt_cache_key" not in body_first,
          body_first)
    check("user prompt from last user text",
          body_first.get("user_prompt") == "hi", body_first)
    check("tool stream for IDE", body_first.get("tool_stream") is True,
          body_first)

    # 密钥交换失败回落明文：签名集合与头序要回到官方的 22 个头。
    state.key_exchange_error = (500, {"error_msg": "kex down"})
    C._KEY_CACHE.clear()
    plain_first, plain_order, plain_body, plain_sig, plain_enc = _chat_of(
        "sess-plain")
    state.key_exchange_error = None
    C._KEY_CACHE.clear()
    plain_signed = plain_first.get("authorization", "").split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("plaintext fallback used", plain_enc is False)
    check("plaintext signature covers 22 IDE headers",
          plain_signed ==
          "client-ip;created-time;host;ide-name;ide-version;model-id;"
          "model-name;plugin-name;plugin-version;user-msg-id;user-session-id;"
          "x-language;x-ot-client-type;x-ot-client-version;x-ot-function;"
          "x-ot-parent-session-id;x-ot-session-id;x-ot-span-id;"
          "x-ot-trace-id;x-sdk-date;x-security-token;x-snap-traceid",
          plain_signed)
    check("plaintext signature verifies", plain_sig)
    check("plaintext content type",
          plain_first.get("content-type") == "application/json", plain_first)
    check("plaintext header order",
          [name for name, _value in plain_order] == [
              "Authorization", "Content-Type", "Host", "User-Agent",
              "client-ip", "created-time", "ide-name", "ide-version",
              "model-id", "model-name", "plugin-name", "plugin-version",
              "user-msg-id", "user-session-id", "x-language",
              "x-ot-client-type", "x-ot-client-version", "x-ot-function",
              "x-ot-parent-session-id", "x-ot-session-id", "x-ot-span-id",
              "x-ot-trace-id", "x-sdk-date", "x-security-token",
              "x-snap-traceid", "Connection", "Accept", "Accept-Encoding",
              "Content-Length"], plain_order)

    C.save_config(ACCOUNTS, {"client_type": "cli"})
    cli_first, cli_order, cli_body, cli_sig, cli_enc = _chat_of("sess-cli")
    check("CLI client type",
          cli_first.get("x-ot-client-type") == "CLI"
          and cli_first.get("x-ot-client-version") == "26.10.15",
          cli_first)
    check("CLI plugin headers",
          cli_first.get("plugin-name") == "CLI"
          and cli_first.get("ide-name") == "CLI", cli_first)
    check("CLI function", cli_first.get("x-ot-function") == "agent-tui",
          cli_first)
    check("CLI has no ide-version",
          "ide-version" not in cli_first, cli_first)
    check("CLI chat user agent",
          cli_first.get("user-agent")
          == "ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14", cli_first)
    cli_signed = cli_first.get("authorization", "").split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("CLI encrypted signed headers", cli_signed ==
          "content-type;host;x-sdk-content-sha256;x-sdk-date;x-security-token",
          cli_signed)
    benefit_body = C.build_chat_body({
        "model": "deepseek-v4-pro-0901",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-cli-benefit")
    benefit_body = C._finalize_chat_body(acct, benefit_body,
                                         "deepseek-v4-pro-0901")
    check("CLI benefit model omits tool_stream",
          "tool_stream" not in benefit_body, benefit_body)
    acct.auth.pop("client_type", None)
    C.save_config(ACCOUNTS, {"client_type": "ide"})

    print("== text snapshot stream ==")
    state.chat_mode = "text"
    body = C.build_chat_body({"model": "GLM-5.2",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": False}, "sess-text")
    stream, acct2, model2 = C.open_chat(C.load_config(ACCOUNTS), body, "sess-text")
    raw = b"".join(stream)
    stream.close()
    text = raw.decode("utf-8", "replace")
    chunks = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith('data: {')]
    joined = ''.join(((chunk.get('choices') or [{}])[0].get('delta') or {}).get('content') or ''
                     for chunk in chunks if chunk.get('choices'))
    check("text stream content", joined == "你好", joined)
    check("text stream no done content", '"content": "[DONE]"' not in text, text[:300])
    state.chat_mode = "delta"

    print("== checkin ==")
    import wb_usage  # 接上积分流水文件
    state.grant_credits = True
    result = C.checkin(C.load_config(ACCOUNTS))
    state.grant_credits = False
    check("checkin ok", result["ok"] is True, result)
    check("checkin credits", result["credits"] == 1000, result)
    check("checkin stored", acct.checkin and acct.checkin.get("ok") is True)
    check("checkin credits stored", acct.checkin.get("credits") == 1000.0,
          acct.checkin)
    events_path = wb_usage.credit_events_file()
    events = []
    if os.path.isfile(events_path):
        with open(events_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
    check("checkin records credit event",
          any(event["uid"] == "u1" and event["delta"] == 1000.0
              for event in events), events)
    # 恢复套餐快照，后面的用分断言仍按原始数字。
    state.credit_metrics[0]["package_credit_used"] = 0.81
    state.credit_metrics[0]["package_credit_remain"] = 4499.19
    acct.info["credits"]["total"]["used"] = 0.81
    acct.info["credits"]["total"]["remain"] = 4499.19
    pool.save_state()
    reloaded = C.CodeArtsPool(ACCOUNTS, C.load_config(ACCOUNTS))
    check("checkin persisted", reloaded.get("u1").checkin == acct.checkin,
          reloaded.get("u1").checkin)
    saved_checkin = acct.checkin
    check("checked_in_today true", acct.checked_in_today() is True)
    check("checkin view shape", acct.last_checkin_view()["ok"] is True
          and acct.last_checkin_view()["at"] > 0
          and "credits" in acct.last_checkin_view(),
          acct.last_checkin_view())
    check("checkin due excludes today", C.checkin_due(C.load_config(ACCOUNTS)) == [])
    acct.checkin_attempts = None
    acct.checkin = {"ok": True, "checked_at": "2000-01-01T00:00:00"}
    check("checked_in_today old false", acct.checked_in_today() is False)
    check("checkin due includes old", len(C.checkin_due(C.load_config(ACCOUNTS))) == 1)
    acct.checkin = {"ok": False,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    check("checked_in_today failed false", acct.checked_in_today() is False)
    acct.checkin = saved_checkin

    print("== checkin attempts ==")
    acct.checkin = None
    acct.checkin_attempts = None
    cfg = dict(C.load_config(ACCOUNTS), checkin_max_attempts=2,
               checkin_retry_gap_seconds=3600)
    check("fresh account due", len(C.checkin_due(cfg)) == 1)
    with mock.patch.object(C, "checkin_account",
                           lambda a, timeout=None: {"ok": False, "credits": 0.0,
                                                    "message": "boom",
                                                    "campaigns": []}):
        failed = C.checkin(cfg, "u1")
    check("failed attempt recorded", failed["ok"] is False
          and acct.checkin_attempts["count"] == 1, acct.checkin_attempts)
    check("gap blocks the next slot", C.checkin_due(cfg) == [])
    blocked = C.checkin(cfg, "u1")
    check("scheduled retry blocked by gap",
          blocked["ok"] is False and "稍后再试" in blocked["message"], blocked)
    manual_row = C.checkin(cfg, "u1", manual=True)
    check("manual retry bypasses limits", manual_row["ok"] is True, manual_row)
    check("manual attempt counted", acct.checkin_attempts["count"] == 2,
          acct.checkin_attempts)
    acct.checkin = None
    # 「两小时后」必须落在同一天：23 点后加两小时会跨到次日，尝试次数按日重置，
    # 这条断言会在夜里偶发失败。封顶到今天的最后一秒。
    lt = time.localtime()
    end_of_day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 23, 59, 59, 0, 0, -1))
    later = min(time.time() + 7200, end_of_day)
    check("exhausted attempts block due", C.checkin_due(cfg, now=later) == [])
    blocked = C.checkin(cfg, "u1")
    check("exhausted message", "尝试次数" in blocked["message"], blocked)
    pool.save_state()
    reloaded = C.CodeArtsPool(ACCOUNTS, C.load_config(ACCOUNTS))
    check("attempts persisted",
          reloaded.get("u1").checkin_attempts["count"] == 2,
          reloaded.get("u1").checkin_attempts)
    acct.checkin_attempts = None
    acct.checkin = saved_checkin

    print("== auth failure ==")
    state.chat_error = (401, {"error_msg": "bad token"})
    state.chat_error_remaining = -1
    state.reset_requests()
    try:
        C.open_chat(C.load_config(ACCOUNTS), dict(body), "sess-401")
        check("401 raises", False)
    except C.CodeArtsAuthError as exc:
        check("401 raises", exc.status == 401, exc.status)
        check("401 attributed", exc.account_uid == "u1", exc.account_uid)
    check("401 refreshed once then retried", state.token_calls >= 1)
    check("401 does not disable", acct.disabled is False)
    check("401 cools the account", acct.cool_until > time.time())
    state.chat_error = None
    state.chat_error_remaining = -1
    pool.clear_cooldown("u1")

    print("== local network ==")
    def local_failure(*args, **kwargs):
        error = C.CodeArtsUnavailable("连不上 CodeArts 聊天接口：名字解析失败")
        error.cause = socket.gaierror(-2, "Name or service not known")
        raise error

    with mock.patch.object(C, "_client_chat", local_failure):
        try:
            C.open_chat(C.load_config(ACCOUNTS), dict(body), "sess-local")
            check("local network raises", False)
        except C.CodeArtsLocalNetwork:
            check("local network raises", True)
    check("local network does not cool the account",
          acct.cool_until == 0, acct.cool_until)
    check("local network marker helper",
          C._looks_like_local_network(socket.gaierror(-2, "x")) is True)

    print("== pool state errors ==")
    pool.set_concurrency_limit("u1", 1)
    pool.acquire(acct)
    try:
        C.open_chat(C.load_config(ACCOUNTS), dict(body), "sess-busy")
        check("busy pool raises", False)
    except C.CodeArtsBusy as exc:
        check("busy pool raises", exc.status == 429 and exc.wait == 1, exc.wait)
    pool.release(acct)
    check("busy pool did not penalise", acct.cool_until == 0, acct.cool_until)
    pool.note_cooldown(acct, 60, "test cooling")
    try:
        C.open_chat(C.load_config(ACCOUNTS), dict(body), "sess-cooling")
        check("cooling pool raises", False)
    except C.CodeArtsCooling as exc:
        check("cooling pool waits", exc.wait >= 1, exc.wait)
    pool.clear_cooldown("u1")
    pool.set_concurrency_limit("u1", 1)
    empty_dir = os.path.join(_TMP.name, "empty-accounts")
    os.makedirs(empty_dir, exist_ok=True)
    try:
        C.open_chat(C.load_config(empty_dir), dict(body), "sess-empty")
        check("empty pool raises", False)
    except C.CodeArtsNoAccounts as exc:
        check("empty pool raises 503", exc.status == 503, exc.status)
    C.invalidate_pool(empty_dir)

    print("== model cooldown ==")
    state.chat_error = (429, {"error_msg": "TPM limit reached"})
    state.chat_headers = {"Retry-After": "42"}
    state.chat_error_remaining = -1
    try:
        C.open_chat(C.load_config(ACCOUNTS), dict(body), "sess-429")
        check("429 raises", False)
    except C.CodeArtsUpstreamError as exc:
        check("429 raises", exc.status == 429, exc.status)
        check("429 keeps Retry-After", getattr(exc, "retry_after", None) == 42,
              getattr(exc, "retry_after", None))
    state.chat_error = None
    state.chat_headers = {}
    state.chat_error_remaining = -1
    check("429 parks only the model",
          acct.model_cooldowns.get("glm-5.2", 0) > time.time() + 30,
          acct.model_cooldowns)
    check("model cooldown blocks that model",
          acct.healthy(model="GLM-5.2") is False)
    check("other models stay healthy", acct.healthy(model="GLM-4.7") is True)
    check("account stays enabled", acct.disabled is False)
    pool.save_state()
    reloaded = C.CodeArtsPool(ACCOUNTS, C.load_config(ACCOUNTS))
    check("model cooldown persisted",
          reloaded.get("u1").model_cooldowns.get("glm-5.2", 0) > time.time(),
          reloaded.get("u1").model_cooldowns)
    view = acct.view()
    check("view exposes model cooldown",
          view["modelCooldowns"] and view["modelCooldowns"][0]["model"] == "glm-5.2",
          view["modelCooldowns"])
    pool.note_success(acct, model="GLM-5.2")
    check("success clears that model", "glm-5.2" not in acct.model_cooldowns,
          acct.model_cooldowns)

    print("== account test ==")
    pool.note_cooldown(acct, 30, "before test")
    acct.model_cooldowns["glm-5.2"] = time.time() + 30
    row = C.test_account(C.load_config(ACCOUNTS), "u1")
    check("test account ok", row["ok"] is True, row)
    check("test account reply", row["reply"] == "你好", row)
    check("test account shape",
          set(row) == {"ok", "uid", "model", "reply", "elapsed_ms", "error"}, row)
    check("test clears cooldowns", acct.cool_until == 0
          and not acct.model_cooldowns, (acct.cool_until, acct.model_cooldowns))
    state.reset_requests()
    row = C.test_account(C.load_config(ACCOUNTS), "u1", model="no-such-model")
    check("model outside account catalog is reported",
          row["ok"] is False and "no-such-model" in row["error"], row)
    check("model outside account catalog sends no chat request",
          not [r for r in state.requests
               if r["path"] == "/api/v2/chat/completions"], state.requests)

    print("== credits refresh ==")
    rows = C.refresh_credits(C.load_config(ACCOUNTS), uids=["u1"])
    check("credits refresh row", len(rows) == 1 and rows[0]["ok"] is True
          and rows[0]["credits"]["remain"] == 4499.19, rows)
    check("credits refresh shape",
          set(rows[0]) == {"uid", "ok", "credits", "error"}, rows)
    import wb_usage  # 接上积分流水文件
    state.credit_metrics[0]["package_credit_used"] = 0.0
    state.credit_metrics[0]["package_credit_remain"] = 4500.0
    C.refresh_credits(C.load_config(ACCOUNTS), uids=["u1"])
    state.credit_metrics[0]["package_credit_used"] = 0.81
    state.credit_metrics[0]["package_credit_remain"] = 4499.19
    events_path = wb_usage.credit_events_file()
    events = []
    if os.path.isfile(events_path):
        with open(events_path, encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
    check("credit event on balance increase",
          bool(events) and events[-1]["uid"] == "u1"
          and events[-1]["delta"] > 0 and events[-1]["realm"] == "codearts",
          events)

    print("== oauth ==")
    started = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    check("oauth session", bool(started.get("session_id")))
    check("oauth url", "authorize" in started["auth_url"] and
          "code_challenge_method=SHA-256" in started["auth_url"])
    import urllib.parse
    ide_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(started["auth_url"]).query)
    check("IDE oauth client", ide_query.get("client_id") == ["codearts-snap"],
          ide_query)
    check("IDE oauth plugin",
          ide_query.get("plugin-name") == ["snap_vscode"]
          and ide_query.get("plugin-version") == ["26.10.300"], ide_query)
    check("IDE oauth callback",
          ide_query.get("auth_callback_url") ==
          ["http://127.0.0.1:8788/oauth/callback"], ide_query)
    pending = C.oauth_poll(C.load_config(ACCOUNTS), started["session_id"])
    check("oauth pending", pending["status"] == "pending", pending)
    done = C.oauth_callback(
        C.load_config(ACCOUNTS), started["session_id"],
        "http://127.0.0.1:8788/oauth/callback?code=abc")
    check("oauth done", done["status"] == "done", done)
    check("oauth account saved", C.pool_for(ACCOUNTS).get("u1") is not None)

    C.save_config(ACCOUNTS, {"client_type": "cli"})
    cli_started = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    cli_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(cli_started["auth_url"]).query)
    check("CLI oauth client", cli_query.get("client_id") == ["CodeArts_Tui"],
          cli_query)
    check("CLI oauth has no plugin",
          "plugin-name" not in cli_query and "uri_scheme" not in cli_query,
          cli_query)
    C.save_config(ACCOUNTS, {"client_type": "ide"})

    print("== oauth proxy slot ==")
    wb_settings.set_proxy_slots(ACCOUNTS, [
        {"id": "slot-a", "name": "a", "url": "http://127.0.0.1:7892",
         "enabled": True},
    ])
    slot_started = C.oauth_start(C.load_config(ACCOUNTS), port=8788,
                                 proxy_slot="auto")
    check("oauth auto slot", slot_started.get("proxySlot") == "slot-a",
          slot_started)
    slot_session = C._find_session(session_id=slot_started["session_id"])
    check("oauth session keeps slot proxy",
          slot_session.proxy == "http://127.0.0.1:7892", slot_session.proxy)
    # 这一节不建假代理，回调走直连；槽的解析与落盘仍然照常验证。
    slot_session.proxy = ""
    slot_done = C.oauth_callback(
        C.load_config(ACCOUNTS), slot_started["session_id"],
        "http://127.0.0.1:8788/oauth/callback?code=abc")
    check("oauth slot callback", slot_done["status"] == "done", slot_done)
    check("oauth slot persisted",
          C.load_auths(ACCOUNTS)[0].get("proxySlot") == "slot-a",
          C.load_auths(ACCOUNTS)[0])
    try:
        C.oauth_start(C.load_config(ACCOUNTS), port=8788, proxy_slot="nope")
        check("oauth unknown slot rejected", False)
    except ValueError:
        check("oauth unknown slot rejected", True)
    wb_settings.set_proxy_slots(ACCOUNTS, [])

    bare = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    status, html = C.oauth_callback_get(
        C.load_config(ACCOUNTS), {"code": ["abc"]})
    check("bare code callback succeeds",
          status == 200 and "登录成功" in html, (status, html[:160]))
    check("bare callback session done",
          C.oauth_poll(C.load_config(ACCOUNTS), bare["session_id"])["status"]
          == "done")
    started2 = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    cont = C.oauth_callback(
        C.load_config(ACCOUNTS), started2["session_id"],
        "http://127.0.0.1:8788/oauth/callback?secret=s1&redirect="
        + __import__("urllib.parse").parse.quote(C.PORTAL_HOST + "/x?ticket_id="
                                                  + started2["session_id"], safe=""))
    check("oauth continue", cont["status"] == "continue", cont)
    check("oauth next_url", cont["next_url"].startswith(C.PORTAL_HOST), cont)
    check("oauth rejects foreign redirect", not C._safe_oauth_redirect("https://evil.invalid/"))
    check("oauth rejects embedded credentials", not C._safe_oauth_redirect("https://codearts.huaweicloud.com@evil.invalid/"))

    print("== oauth without identity ==")
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "sub-123"}).encode()).decode().rstrip("=")
    check("refresh token sub", C.identity_from_refresh_token("a." + payload + ".b") == "sub-123")
    state.omit_identity = True
    started3 = C.oauth_start(C.load_config(ACCOUNTS), port=8788)
    done3 = C.oauth_callback(
        C.load_config(ACCOUNTS), started3["session_id"],
        "http://127.0.0.1:8788/oauth/callback?code=abc")
    check("oauth no identity done", done3["status"] == "done", done3)
    saved3 = [a for a in C.load_auths(ACCOUNTS) if a["user_id"] == "u1"]
    check("identity looked up", bool(saved3) and saved3[0]["user_name"] == "账号一",
          saved3)
    check("oauth client id persisted",
          bool(saved3) and saved3[0].get("client_id") == "codearts-snap", saved3)
    check("oauth channel persisted",
          bool(saved3) and saved3[0].get("client_type") == "ide", saved3)
    state.omit_identity = False

    print("== wire contract ==")
    state.reset_requests()
    state.key_exchange_error = None
    C._KEY_CACHE.clear()
    wire_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-wire")
    stream, current, _model = C.open_chat(C.load_config(ACCOUNTS), wire_body,
                                          "sess-wire")
    raw = b"".join(stream)
    stream.close()
    check("encrypted chat answered", "你好" in raw.decode("utf-8", "replace"), raw[:200])
    check("chat request encrypted", state.last_encrypted is True)
    check("chat signature verifies", state.last_signature_ok is True)
    key_req = next(r for r in state.requests
                   if r["path"] == "/api/v1/security/key-exchange")
    check("key exchange header order",
          [name for name, _value in key_req["header_list"]] == [
              "Authorization", "Content-Type", "Host", "X-Sdk-Content-Sha256",
              "X-Sdk-Date", "X-Security-Token", "Connection", "User-Agent",
              "Accept", "Accept-Encoding", "Content-Length"],
          key_req["header_list"])
    key_signed = key_req["headers"]["authorization"].split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("key exchange signed headers", key_signed ==
          "content-type;host;x-sdk-content-sha256;x-sdk-date;x-security-token",
          key_signed)
    check("key exchange body shape",
          {"clientPublicKey", "sessionId", "timestamp"}
          <= set(json.loads(key_req["raw_body"])), key_req["raw_body"][:160])
    check("key exchange body key order",
          list(json.loads(key_req["raw_body"]))
          == ["clientPublicKey", "sessionId", "timestamp"], key_req["raw_body"][:160])
    check("key exchange user agent",
          key_req["headers"]["user-agent"] == "Bun/1.3.14",
          key_req["headers"].get("user-agent"))
    check("key exchange signature verifies", verify_signature(
        "POST", key_req["path"], key_req["query"], key_req["header_list"],
        key_req["raw_body"], state.secret_key,
        payload_hash="UNSIGNED-PAYLOAD"), key_req["headers"]["authorization"])

    before_credential = C._credential_key(current)
    saved_token = current.auth.get("cloud_dragon_token")
    current.auth["cloud_dragon_token"] = str(saved_token) + "x"
    changed_credential = C._credential_key(current)
    current.auth["cloud_dragon_token"] = saved_token
    check("key cache keyed by credential",
          before_credential != changed_credential,
          (before_credential, changed_credential))
    old_session_limit = C._SESSION_STATE_LIMIT
    old_turn_limit = C._TURN_STATE_LIMIT
    C._SESSION_STATE_LIMIT = 3
    C._TURN_STATE_LIMIT = 3
    for index in range(6):
        C._session_for("bounded-session-%d" % index, [])
    check("session state is bounded",
          len(C._SESSION_STATE) <= 3, len(C._SESSION_STATE))
    for index in range(6):
        C._turn_ids("bounded-turn-%d" % index,
                    [{"role": "user", "content": str(index)}])
    check("turn state is bounded",
          len(C._TURN_STATE) <= 3, len(C._TURN_STATE))
    C._SESSION_STATE_LIMIT = old_session_limit
    C._TURN_STATE_LIMIT = old_turn_limit

    C.save_config(ACCOUNTS, {"chat_encryption": False})
    C._KEY_CACHE.clear()
    state.reset_requests()
    exchanges_before = len(state.key_exchanges)
    switch_body = C.build_chat_body({
        "model": "GLM-5.2",
        "messages": [{"role": "user", "content": "hi"}],
    }, "sess-plain-switch")
    switch_stream, _acct, _model = C.open_chat(
        C.load_config(ACCOUNTS), switch_body, "sess-plain-switch")
    switch_raw = b"".join(switch_stream)
    switch_stream.close()
    switch_req = [r for r in state.requests
                  if r["path"] == "/api/v2/chat/completions"][-1]
    switch_signed = switch_req["headers"]["authorization"].split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("encryption switch-off sends plaintext",
          state.last_encrypted is False
          and "你好" in switch_raw.decode("utf-8", "replace"), switch_raw[:120])
    check("encryption switch-off skips key exchange",
          len(state.key_exchanges) == exchanges_before)
    check("encryption switch-off signs identity headers",
          switch_signed.startswith("client-ip;created-time;host;ide-name;"))
    C.save_config(ACCOUNTS, {"chat_encryption": True})
    C._KEY_CACHE.clear()

    for encoding in ("gzip", "br", "zstd"):
        state.response_encoding = encoding
        stream, current, _model = C.open_chat(
            C.load_config(ACCOUNTS),
            C.build_chat_body({"model": "GLM-5.2",
                               "messages": [{"role": "user", "content": "hi"}]},
                              "sess-%s" % encoding), "sess-%s" % encoding)
        compressed = b"".join(stream)
        stream.close()
        check("%s stream decoded" % encoding,
              "你好" in compressed.decode("utf-8", "replace"), compressed[:120])
    state.response_encoding = ""

    print("== heartbeat ==")
    state.reset_requests()
    stream, current, _model = C.open_chat(
        C.load_config(ACCOUNTS),
        C.build_chat_body({"model": "GLM-5.2",
                           "messages": [{"role": "user", "content": "hi"}]},
                          "sess-heartbeat"), "sess-heartbeat")
    b"".join(stream)
    stream.close()
    beats = [item for item in state.heartbeats if item["session"].startswith("ses_")]
    check("heartbeat busy then idle",
          [item["status"] for item in beats[-2:]] == ["busy", "idle"],
          [item["status"] for item in beats])
    beat = beats[-2]
    check("heartbeat body and session",
          beat["body"] == "{}" and beat["session"] == beats[-1]["session"]
          and beat["session"].startswith("ses_"), beat)
    check("heartbeat header order",
          [name for name, _value in beat["header_list"]] == [
              "Accept", "Accept-Encoding", "Authorization", "Content-Type",
              "Host", "User-Agent", "user-session-id", "x-client-type",
              "x-language", "x-sdk-date", "x-security-token", "x-snap-traceid",
              "Content-Length"], beat["header_list"])
    beat_signed = beat["headers"]["authorization"].split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("heartbeat signed headers", beat_signed ==
          "accept;content-type;host;user-session-id;x-client-type;x-language;"
          "x-sdk-date;x-security-token;x-snap-traceid", beat_signed)
    check("heartbeat signature verifies", verify_signature(
        "PUT", "/snap-manager/v1/chat-session/heartbeat",
        "status=" + beat["status"], beat["header_list"], b"{}",
        state.secret_key), beat["headers"]["authorization"])

    sent = []
    heartbeat = C.ChatHeartbeat(interval=0.05)
    heartbeat.busy("ses-timer", lambda status: sent.append(status))
    heartbeat.busy("ses-timer", lambda status: sent.append(status))
    deadline = time.time() + 2.0
    while len(sent) < 2 and time.time() < deadline:
        time.sleep(0.02)
    heartbeat.idle("ses-timer")
    heartbeat.idle("ses-timer")
    check("heartbeat repeats while busy and idles once",
          sent[0] == "busy" and sent[-1] == "idle"
          and len(sent) >= 2 and sent.count("idle") == 1, sent)

    print("== device sessions ==")
    C.save_config(ACCOUNTS, {"client_type": "ide"})
    cli_auth = make_auth(state, uid="device-cli", name="设备账号")
    cli_auth["client_type"] = "cli"
    pool.add(C.save_auth(ACCOUNTS, cli_auth))
    state.reset_requests()
    keeper = C.DeviceSessions(C.load_config(ACCOUNTS), interval=120)
    keeper.tick(now=1000)
    posts = [r for r in state.requests
             if r["method"] == "POST" and r["path"] == "/v1/sessions"]
    check("device registration posts once per account", len(posts) == 2,
          [(r["headers"].get("agent-type"), r["raw_body"][:60]) for r in posts])
    ide_post = next(r for r in posts
                    if b"request_time" in r["raw_body"])
    cli_post = next(r for r in posts if b"CodeArts Agent CLI" in r["raw_body"])
    ide_body = json.loads(ide_post["raw_body"])
    check("IDE device body shape",
          list(ide_body) == ["device_name", "device_type", "ip_address",
                             "request_time", "client_type", "client_version",
                             "status"]
          and ide_body["status"] == 0
          and ide_body["client_type"] == "CodeArts Agent"
          and ide_body["client_version"] == "26.10.300", ide_body)
    cli_body = json.loads(cli_post["raw_body"])
    check("CLI device body shape",
          list(cli_body) == ["client_type", "device_name", "device_type",
                             "ip_address", "client_version"]
          and cli_body["client_type"] == "CodeArts Agent CLI"
          and cli_body["client_version"] == "26.10.15", cli_body)
    check("CLI device header order",
          [name for name, _value in cli_post["header_list"]] == [
              "Agent-Type", "Authorization", "Content-Type", "Host",
              "X-Sdk-Date", "X-Security-token", "Connection", "User-Agent",
              "Accept", "Accept-Encoding", "Content-Length"],
          cli_post["header_list"])
    cli_signed = cli_post["headers"]["authorization"].split(
        "SignedHeaders=", 1)[-1].split(",", 1)[0]
    check("CLI device signed headers", cli_signed ==
          "agent-type;content-type;host;x-sdk-date;x-security-token",
          cli_signed)
    check("CLI device signature verifies", verify_signature(
        "POST", cli_post["path"], cli_post["query"], cli_post["header_list"],
        cli_post["raw_body"], state.secret_key), cli_post["headers"]["authorization"])
    check("IDE device signature verifies", verify_signature(
        "POST", ide_post["path"], ide_post["query"], ide_post["header_list"],
        ide_post["raw_body"], state.secret_key), ide_post["headers"]["authorization"])
    state.reset_requests()
    keeper.tick(now=1130)
    polls = [r for r in state.requests
             if r["method"] == "GET" and r["path"] == "/v1/sessions"]
    check("device poll every 120 seconds", len(polls) == 2, len(polls))
    check("CLI poll query order",
          next(r for r in polls if r["headers"].get("agent-type")
               and r["headers"]["user-agent"].startswith("codearts"))
          ["query"] == "client_type=CodeArts%20Agent%20CLI&device_name="
          + urllib.parse.quote(socket.gethostname())
          + "&client_version=26.10.15&page=1&page_size=10",
          [r["query"] for r in polls])
    pool.disable("device-cli")
    state.reset_requests()
    keeper.tick(now=1260)
    cli_activity = [r for r in state.requests
                    if r["path"] == "/v1/sessions"
                    and "CodeArts%20Agent%20CLI" in r["query"]]
    check("disabled CLI stops registering", not cli_activity, cli_activity)
    pool.disable("u1")
    state.reset_requests()
    keeper.tick(now=1270)
    logout = [r for r in state.requests
              if r["method"] == "POST" and r["path"] == "/v1/sessions"]
    check("disabled IDE marks the device session idle",
          bool(logout) and json.loads(logout[-1]["raw_body"])["status"] == 1,
          [r["raw_body"][:120] for r in logout])
    pool.enable("u1")
    keeper.stop()
    pool.remove("device-cli")
    C.invalidate_pool(ACCOUNTS)

    print("== pool summary ==")
    check("configured", C.configured(ACCOUNTS) is True)
    check("stats total", C.pool_for(ACCOUNTS).stats()["total"] >= 1)
finally:
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
