"""单进程 CodeArts 路由：chat / responses / messages 与用量归因。

假华为云上游 + 真 hub handler；不碰 WorkBuddy 账号池、不碰外网。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-codearts-routing-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]
USAGE = os.environ["WB_PROXY_USAGE_DIR"]

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


def post(path, payload, key):
    req = urllib.request.Request(
        HUB + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def sse_objects(raw):
    out = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            out.append(json.loads(data))
        except Exception:
            pass
    return out


class FakePool(object):
    """WorkBuddy 池的替身：账号操作一律报错，守卫推送是每条请求的固定动作。

    契约要求号池守卫对每个提供方生效，所以 CodeArts 请求也会推进 WorkBuddy 的
    守卫；这里把这些调用做成空操作，真正的账号访问仍然会被 __getattr__ 抓住。
    """

    _guard_static = None

    def get(self, uid):
        return None

    def apply_credit_rates(self, per_day=None):
        return None

    def apply_reserve_credits(self, values=None):
        pass

    def apply_expiring_window(self, values=None):
        pass

    def apply_daily_token_limit(self, values=None, usage=None):
        pass

    def apply_daily_credit_limit(self, values=None, credits=None,
                                 free_models=None):
        pass

    def apply_model_daily_token_limit(self, values=None, per_model=None):
        pass

    def __getattr__(self, name):
        raise AssertionError("WorkBuddy pool used: %s" % name)


srv, base, state = start_server()
C.SNAP_HOST = base
C.SNAP_MANAGER = base + "/snap-manager"
C.STS_HOST = base
C.BENEFIT_HOST = base
C.PORTAL_HOST = base + "/portal"

hub = wb_startup.GatewayServer(("127.0.0.1", 0), wb_http.Handler)
threading.Thread(target=hub.serve_forever, daemon=True).start()
HUB = "http://127.0.0.1:%d" % hub.server_address[1]

try:
    wb_proxy.POOL = FakePool()
    wb_proxy.ROUTE = ("intl",)
    wb_settings.set_enable_anthropic_messages(ACCOUNTS, True)
    wb_settings.set_api_keys(ACCOUNTS, [{
        "key": "hub-key-1234", "name": "test", "route": ["codearts"],
    }])
    wb_proxy.API_KEY = None
    C.save_config(ACCOUNTS, {"benefit_auto_claim": True})
    auth = {
        "user_id": "u1", "user_name": "账号一", "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }
    C.pool_for(ACCOUNTS).add(C.save_auth(ACCOUNTS, auth))
    C.invalidate_models()

    chat = {"model": "GLM-5.2",
            "messages": [{"role": "user", "content": "hi"}]}

    print("== chat stream ==")
    status, raw = post("/v1/chat/completions", dict(chat, stream=True),
                       "hub-key-1234")
    check("stream status", status == 200, (status, raw[:300]))
    events = sse_objects(raw)
    text = "".join(
        e["choices"][0]["delta"].get("content") or ""
        for e in events if e.get("choices") and e["choices"][0].get("delta"))
    check("stream text", "你好" in text, text)
    check("stream usage", any(e.get("usage") for e in events), events[-3:])
    check("upstream stream forced", state.last_body.get("stream") is True)
    check("upstream request encrypted", state.last_encrypted is True)
    check("upstream signature verifies", state.last_signature_ok is True)
    check("upstream body key order",
          list(state.last_body) == ["model", "max_tokens", "tool_stream",
                                    "messages", "stream", "user_prompt"],
          list(state.last_body))
    check("upstream has no chat id",
          "chat_id" not in state.last_body
          and "prompt_cache_key" not in state.last_body, state.last_body)
    check("upstream user prompt",
          state.last_body.get("user_prompt") == "hi", state.last_body)
    check("upstream signed",
          str(state.last_headers.get("authorization") or "").startswith(
              "SDK-HMAC-SHA256"), state.last_headers)

    print("== chat nonstream ==")
    status, raw = post("/v1/chat/completions", dict(chat, stream=False),
                       "hub-key-1234")
    check("nonstream status", status == 200, (status, raw[:300]))
    obj = json.loads(raw)
    check("nonstream text", obj["choices"][0]["message"]["content"] == "你好",
          obj)

    state.chat_mode = "text"
    status, raw = post("/v1/chat/completions", dict(chat, stream=False), "hub-key-1234")
    check("first snapshot survives aggregation", status == 200 and
          json.loads(raw)["choices"][0]["message"]["content"] == "你好", (status, raw))
    state.chat_mode = "delta"

    print("== benefit header ==")
    state.require_benefit_header = True
    status, raw = post("/v1/chat/completions",
                       dict(chat, model="deepseek-v4-pro-0901", stream=False),
                       "hub-key-1234")
    check("benefit status", status == 200, (status, raw[:300]))
    chat_req = [r for r in state.requests
                if r["path"] == "/api/v2/chat/completions"][-1]
    check("benefit header sent", chat_req["headers"].get("maas_type") == "benefit",
          chat_req["headers"])
    state.require_benefit_header = False

    print("== responses ==")
    responses_req = {
        "model": "GLM-5.2",
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": False,
    }
    status, raw = post("/v1/responses", responses_req, "hub-key-1234")
    check("responses status", status == 200, (status, raw[:300]))
    check("responses text", "你好" in raw, raw[:300])

    status, raw = post("/v1/responses",
                       dict(responses_req, stream=True), "hub-key-1234")
    check("responses stream status", status == 200, (status, raw[:300]))
    check("responses stream completed", "response.completed" in raw, raw[:300])

    print("== messages ==")
    messages_req = {
        "model": "GLM-5.2", "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}], "stream": False,
    }
    status, raw = post("/v1/messages", messages_req, "hub-key-1234")
    check("messages status", status == 200, (status, raw[:300]))
    check("messages text", "你好" in raw, raw[:300])

    status, raw = post("/v1/messages",
                       dict(messages_req, stream=True), "hub-key-1234")
    check("messages stream status", status == 200, (status, raw[:300]))
    check("messages stream text", "你好" in raw, raw[:300])

    print("== usage attribution ==")
    rows = []
    with open(os.path.join(USAGE, "usage.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    check("usage rows written", len(rows) >= 6, len(rows))
    check("all account codearts",
          all(r.get("account") == "u1" for r in rows),
          [r.get("account") for r in rows])

    print("== key restrictions ==")
    image_chat = dict(chat, model="Qwen3-VL-235B",
                      messages=[{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}]}])
    before = len([r for r in state.requests
                  if r["path"] == "/api/v2/chat/completions"])
    status, raw = post("/v1/chat/completions", image_chat, "hub-key-1234")
    after = len([r for r in state.requests
                 if r["path"] == "/api/v2/chat/completions"])
    check("image forwarded to upstream", status == 200
          and after == before + 1, (status, raw, before, after))
    content = ((state.last_body or {}).get("messages") or [{}])[0].get("content")
    check("image content preserved", isinstance(content, list)
          and any(isinstance(p, dict) and p.get("type") == "image_url"
                  and (p.get("image_url") or {}).get("url")
                  == "data:image/png;base64,abc" for p in content), content)
    wb_settings.set_api_keys(ACCOUNTS, [
        {"key": "model-limit", "name": "model", "route": ["codearts"], "models": ["other-model"]},
        {"key": "allowed-account", "name": "allowed", "route": ["codearts"], "accounts": ["u1"]},
        {"key": "hub-key-1234", "name": "test", "route": ["codearts"]},
    ])
    before = len(state.requests)
    status, raw = post("/v1/chat/completions", chat, "model-limit")
    check("model restriction enforced", status == 400 and len(state.requests) == before, (status, raw))
    status, raw = post("/v1/chat/completions", chat, "allowed-account")
    check("allowed account works", status == 200, (status, raw[:300]))

    print("== errors ==")
    state.chat_error = (400, {"error_msg": "bad request"})
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("400 passthrough", status == 400, (status, raw[:300]))
    check("400 message", "bad request" in raw, raw[:300])

    state.chat_error = (500, {"error_msg": "boom"})
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("500 preserved", status == 500, (status, raw[:300]))

    print("== pool pressure ==")
    pool = C.pool_for(ACCOUNTS)
    account = pool.get("u1")
    pool.set_concurrency_limit("u1", 1)
    pool.acquire(account)
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("busy pool -> 429", status == 429, (status, raw[:300]))
    check("busy pool retry-after", "1" in raw, raw[:300])
    pool.release(account)
    pool.set_concurrency_limit("u1", 1)

    pool.note_cooldown(account, 60, "routing test cooling")
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("cooling pool -> 429", status == 429, (status, raw[:300]))
    check("cooling pool retry-after", "retry_after" in raw, raw[:300])
    pool.clear_cooldown("u1")

    original_chat = C._client_chat

    def local_failure(*args, **kwargs):
        error = C.CodeArtsUnavailable("连不上 CodeArts 聊天接口：名字解析失败")
        error.cause = __import__("socket").gaierror(-2, "Name or service not known")
        raise error

    state.chat_error = None
    C._client_chat = local_failure
    try:
        status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    finally:
        C._client_chat = original_chat
    check("local network -> 503", status == 503, (status, raw[:300]))
    check("local network message", "本机网络不可用" in raw, raw[:300])
    check("local network does not cool the account",
          account.cool_until == 0, account.cool_until)

    pool.disable("u1")
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("no usable account -> 503", status == 503, (status, raw[:300]))
    pool.enable("u1")

    rows = []
    with open(os.path.join(USAGE, "usage.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    limited = [row for row in rows if row.get("status") == 429]
    check("rate limited rows recorded", bool(limited), rows[-3:])
    check("rate limited outcome", all(row.get("outcome") == "rate_limited"
                                     for row in limited), limited[-2:])
    failures = [row for row in rows if row.get("status") == 500]
    check("upstream failure attributed",
          bool(failures) and failures[-1].get("account") == "u1", failures[-2:])

    state.chat_error = None
    C.SNAP_HOST = "http://127.0.0.1:1"
    status, raw = post("/v1/chat/completions", chat, "hub-key-1234")
    check("unreachable -> 502", status == 502, (status, raw[:300]))
    check("unreachable message", "CodeArts" in raw or "华为云" in raw, raw[:300])
finally:
    try:
        hub.shutdown()
        hub.server_close()
    except Exception:
        pass
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
