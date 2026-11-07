"""混合路由：按路由顺序为模型挑渠道，别名合并的模型在首选渠道没有账号能接单时换到下一个渠道。

国内版目录写进进程内缓存，国内版账号是真实 Account；CodeArts 用假华为云上游，不碰外网。
"""
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

from _isolated_dirs import isolated_data_dirs  # noqa: E402

_TMP = isolated_data_dirs("wb-channel-route-")
ACCOUNTS = os.environ["ACCOUNTS_DIR"]
USAGE = os.environ["WB_PROXY_USAGE_DIR"]

import wb_accounts  # noqa: E402
import wb_channels  # noqa: E402
import wb_codearts as C  # noqa: E402
import wb_codearts_crypto as crypto  # noqa: E402
import wb_http  # noqa: E402
import wb_models  # noqa: E402
import wb_proxy  # noqa: E402
import wb_settings  # noqa: E402
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


def call(path, key, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        HUB + path, data=data, method="POST" if data else "GET",
        headers=dict({"Authorization": "Bearer " + key,
                      "Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


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
    pool = wb_accounts.AccountPool(os.path.join(_TMP.name, "pool"))
    cn_account = wb_accounts.Account({"uid": "uid-cn", "accessToken": "t", "realm": "cn"})
    pool.accounts = [cn_account]
    wb_proxy.POOL = pool
    wb_proxy.API_KEY = None
    wb_models._models_cache["cn"] = {
        "at": time.time(), "data": [("deepseek-v4.1-flash", {}), ("glm-5.3", {})]}
    # CodeArts 目录里的写法带大写；别名表用小写写源 id，转发时要换回目录里的写法
    state.builtin.append({"model_id": "DeepSeek-V4.1-Flash-1017",
                          "model_name": "DeepSeek-V4.1-Flash-1017", "credit": []})
    wb_settings.set_model_aliases(ACCOUNTS, {"deepseek-v4.1-flash-1017": "deepseek-v4.1-flash"})
    wb_settings.set_api_keys(ACCOUNTS, [
        {"key": "mixed-key", "name": "mixed", "route": ["cn", "codearts"]},
    ])
    C.pool_for(ACCOUNTS).add(C.save_auth(ACCOUNTS, {
        "user_id": "u1", "user_name": "账号一", "domain_id": "d1",
        "cloud_dragon_token": state.token,
        "access_key_id": state.api_key, "secret_access_key": state.secret_key,
        "expiration": state.expiration, "refresh_token": state.refresh_token,
        "code_verifier": "v", "client_id": C.CLIENT_ID,
        "dpop_private_key": crypto.jwk_from_keypair(*crypto.generate_keypair()),
    }))
    C.invalidate_models()

    print("== select ==")
    mixed = ("cn", "codearts")
    ca_account = C.pool_for(ACCOUNTS).get("u1")
    check("route is a set: order does not matter",
          wb_channels.parse_route(["codearts", "cn"]) == mixed)

    def spread(model, rounds=200, session_key=None):
        seen = {}
        for _ in range(rounds):
            realm, upstream, public = wb_channels.select(mixed, model, session_key=session_key)
            seen[(realm, upstream, public)] = seen.get((realm, upstream, public), 0) + 1
        return seen

    wb_channels._rng = random.Random(7)
    counts = spread("deepseek-v4.1-flash")
    check("merged model is mixed across both channels",
          counts.get(("cn", "deepseek-v4.1-flash", "deepseek-v4.1-flash"), 0) >= 60
          and counts.get(("codearts", "DeepSeek-V4.1-Flash-1017", "deepseek-v4.1-flash"), 0) >= 60,
          counts)
    check("source id is announced as the merged id",
          all(key[2] == "deepseek-v4.1-flash" for key in spread("deepseek-v4.1-flash-1017", 20)))
    ca_account.priority = 10
    check("a smaller account priority wins across channels",
          set(spread("deepseek-v4.1-flash", 40)) == {("codearts", "DeepSeek-V4.1-Flash-1017",
                                                       "deepseek-v4.1-flash")})
    # 临期积分在层内生效：优先级数字更小的渠道仍然先接单
    cn_account.expiring_window_days = 7
    cn_account.credits = {"remain": 500, "size": 500, "packages": [
        {"name": "活动包", "remain": 500, "days_left": 2,
         "expireAt": int(time.time() + 2 * 86400)}]}
    check("a smaller priority outranks expiring credits across channels",
          {key[0] for key in spread("deepseek-v4.1-flash", 40)} == {"codearts"})
    ca_account.priority = 100
    check("expiring credits are spent first within one priority tier",
          {key[0] for key in spread("deepseek-v4.1-flash", 40)} == {"cn"})
    cn_account.expiring_window_days = 0
    cn_account.credits = None
    ca_account.priority = 10
    check("the smaller priority wins again after the window closes",
          {key[0] for key in spread("deepseek-v4.1-flash", 20)} == {"codearts"})
    ca_account.priority = 100
    # 同层之间：手动权重先于忙闲，忙闲先于随机
    ca_account.weight = 3
    check("a manual weight takes the channel",
          {key[0] for key in spread("deepseek-v4.1-flash", 20)} == {"codearts"})
    ca_account.weight = 1
    cn_account.acquire_request("deepseek-v4.1-flash")
    check("the least busy account takes the channel",
          {key[0] for key in spread("deepseek-v4.1-flash", 20)} == {"codearts"})
    cn_account.release_request("deepseek-v4.1-flash")
    cn_account.concurrency_limit = 1
    cn_account.acquire_request("deepseek-v4.1-flash")
    check("an account at its concurrency limit leaves the work to the other channel",
          {key[0] for key in spread("deepseek-v4.1-flash", 40)} == {"codearts"})
    cn_account.release_request("deepseek-v4.1-flash")
    cn_account.concurrency_limit = 0
    sticky = {key[0] for key in spread("deepseek-v4.1-flash", 30, session_key="conv-1")}
    check("one conversation stays on one channel", len(sticky) == 1, sticky)
    wb_settings.set_model_enabled(ACCOUNTS, "codearts", "deepseek-v4.1-flash-1017", False)
    check("a closed model does not take part in mixing",
          {key[0] for key in spread("deepseek-v4.1-flash", 40)} == {"cn"})
    wb_settings.set_model_enabled(ACCOUNTS, "codearts", "deepseek-v4.1-flash-1017", True)
    check("model only in one channel goes there",
          wb_channels.select(mixed, "GLM-5.2")[:2] == ("codearts", "GLM-5.2")
          and wb_channels.select(mixed, "glm-5.3")[0] == "cn")
    check("upstream gets the catalog's own casing",
          wb_channels.select(mixed, "GLM-5.3")[:2] == ("cn", "glm-5.3"))
    wb_settings.set_model_enabled(ACCOUNTS, "codearts", "GLM-5.2", False)
    check("model closed in its only channel goes there for the closed-model message",
          wb_channels.select(mixed, "GLM-5.2")[:2] == ("codearts", "GLM-5.2"))
    status, body = call("/v1/chat/completions", "mixed-key", {
        "model": "GLM-5.2", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    check("closed model is refused locally with the channel named",
          status == 400 and "CodeArts" in body["error"]["message"], (status, body))
    wb_settings.set_model_enabled(ACCOUNTS, "codearts", "GLM-5.2", True)
    cn_account.enabled = False
    check("a channel without servable accounts drops out of mixing",
          set(spread("deepseek-v4.1-flash", 20)) == {("codearts", "DeepSeek-V4.1-Flash-1017",
                                                       "deepseek-v4.1-flash")})
    cn_account.enabled = True

    print("== /v1/models ==")
    status, payload = call("/v1/models", "mixed-key")
    ids = [m["id"] for m in payload.get("data") or []]
    check("union of both catalogs", status == 200
          and {"deepseek-v4.1-flash", "glm-5.3", "GLM-5.2"} <= set(ids), (status, ids))
    check("merged id listed once", ids.count("deepseek-v4.1-flash") == 1, ids)
    check("aliased source id hidden",
          "deepseek-v4.1-flash-1017" not in {mid.lower() for mid in ids}, ids)
    check("route echoed", payload.get("route") == ["cn", "codearts"], payload.get("route"))

    print("== dispatch ==")
    status, body = call("/v1/chat/completions", "mixed-key", {
        "model": "GLM-5.2", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    check("codearts-only model served through the mixed route", status == 200
          and body["choices"][0]["message"]["content"] == "你好", (status, body))
    cn_account.enabled = False
    status, body = call("/v1/chat/completions", "mixed-key", {
        "model": "deepseek-v4.1-flash", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
    check("merged model handed to codearts", status == 200, (status, body))
    check("upstream got the codearts source id",
          (state.last_body or {}).get("model") == "DeepSeek-V4.1-Flash-1017", state.last_body)
    check("client sees the merged id", body.get("model") == "deepseek-v4.1-flash", body)
    req = urllib.request.Request(
        HUB + "/v1/chat/completions", method="POST",
        data=json.dumps({"model": "deepseek-v4.1-flash", "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8"),
        headers={"Authorization": "Bearer mixed-key", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        chunks = [json.loads(line[6:]) for line in resp.read().decode("utf-8").splitlines()
                  if line.startswith("data: {")]
    check("stream chunks carry the merged id",
          chunks and all(c.get("model") == "deepseek-v4.1-flash" for c in chunks if "model" in c),
          [c.get("model") for c in chunks])
    with open(os.path.join(USAGE, "usage.jsonl"), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    check("usage rows name the serving channel",
          [r.get("realm") for r in rows[-2:]] == ["codearts", "codearts"], rows[-2:])

    print("== route header ==")
    wb_settings.set_api_keys(ACCOUNTS, [{"key": "free-key", "name": "free"}])
    status, body = call("/v1/models", "free-key", headers={"X-Realm": "intl,cn"})
    check("conflicting header route rejected", status == 400, (status, body))
    status, body = call("/v1/models", "free-key", headers={"X-Realm": "codearts"})
    check("single channel header still works", status == 200
          and body.get("route") == ["codearts"], (status, body))

    print("== CodeArts default exclusion ==")
    excluded = {"deepseek-v4.1-flash", "deepseek-v4-pro-0813", "speed",
                "openpangu-2.0-flash", "openpangu-2.0-pro"}
    for mid in sorted(excluded):
        state.builtin.append({"model_id": mid, "model_name": mid, "credit": []})
    C.invalidate_models()
    status, body = call("/v1/models", "free-key", headers={"X-Realm": "codearts"})
    ids = {m["id"] for m in body.get("data") or []}
    # deepseek-v4.1-flash 仍会出现：它是别名把 deepseek-v4.1-flash-1017 合并后的通告 id
    check("default-excluded models are not announced",
          status == 200 and not ids & (excluded - {"deepseek-v4.1-flash"}), ids)
    check("the merged id is served by the alias source, not the excluded model",
          wb_channels.select(("codearts",), "deepseek-v4.1-flash")[1] == "DeepSeek-V4.1-Flash-1017")
    check("the 1017 model itself is not default-excluded",
          not wb_channels.provider("codearts").default_excluded("DeepSeek-V4.1-Flash-1017")
          and "deepseek-v4.1-flash-1017" not in {
              m.lower() for m in wb_settings.disabled_models(ACCOUNTS, "codearts")})
    closed = {m.lower() for m in wb_settings.disabled_models(ACCOUNTS, "codearts")}
    check("default-excluded models land in the CodeArts closed list only",
          excluded <= closed
          and "deepseek-v4.1-flash" not in {m.lower() for m in wb_settings.disabled_models(ACCOUNTS, "cn")},
          closed)
    panel = wb_channels.provider("codearts").panel_models("codearts", False)["data"]
    check("panel marks them excluded",
          {m["id"] for m in panel if m["excluded"]} == excluded)
    wb_settings.set_model_enabled(ACCOUNTS, "codearts", "openpangu-2.0-flash", True)
    status, body = call("/v1/models", "free-key", headers={"X-Realm": "codearts"})
    check("a re-enabled model stays enabled",
          "openpangu-2.0-flash" in {m["id"] for m in body.get("data") or []}, body)
finally:
    try:
        hub.shutdown()
        hub.server_close()
    except Exception:
        pass
    stop_server(srv)

print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
