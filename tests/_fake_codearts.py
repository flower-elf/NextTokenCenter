# -*- coding: utf-8 -*-
"""测试用的假华为云 CodeArts 上游。

模拟单进程版本用到的端点：OAuth 换 token、聊天 SSE、模型发现、福利领取、
每日签到。测试把 `wb_codearts.SNAP_HOST` / `STS_HOST` / `BENEFIT_HOST`
指到本服务，不碰外网、不碰真实账号。

文件名不以 `_test_` 开头，`tests/run_all.py` 不会把它当套件跑。
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brotli
import zstandard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

NOW = int(time.time())


def _trailing_uri(path):
    out = "/".join(urllib.parse.quote(segment, safe="")
                   for segment in (path or "/").split("/"))
    return out if out.endswith("/") else out + "/"


def _canonical_query(query):
    pairs = urllib.parse.parse_qsl(query or "", keep_blank_values=True)
    pairs.sort()
    return "&".join(urllib.parse.quote(key, safe="") + "="
                    + urllib.parse.quote(value, safe="")
                    for key, value in pairs)


def verify_signature(method, path, query, header_list, body, secret,
                     payload_hash=None):
    """按 SignedHeaders 重算签名；测试用它确认官方签名集合。"""
    lower = {name.lower(): value for name, value in header_list}
    authorization = lower.get("authorization", "")
    if "SignedHeaders=" not in authorization or "Signature=" not in authorization:
        return False
    signed = authorization.split("SignedHeaders=", 1)[1].split(",", 1)[0]
    names = signed.split(";")
    signature = authorization.rsplit("Signature=", 1)[1]
    if any(name not in lower for name in names):
        return False
    if payload_hash is None:
        payload_hash = (lower.get("x-sdk-content-sha256")
                        or hashlib.sha256(body or b"").hexdigest())
    values = {name: lower[name] for name in names}
    canonical = "\n".join([
        method.upper(),
        _trailing_uri(path),
        _canonical_query(query),
        "".join(name + ":" + values[name] + "\n" for name in sorted(values)),
        ";".join(sorted(values)),
        payload_hash,
    ])
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256", lower.get("x-sdk-date", ""),
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ])
    expected = hmac.new(secret.encode("utf-8"),
                        string_to_sign.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _compress(raw, encoding):
    if encoding == "gzip":
        return zlib.compress(raw, 6, wbits=16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.compress(raw, 6)
    if encoding == "br":
        return brotli.compress(raw)
    if encoding == "zstd":
        return zstandard.ZstdCompressor().compress(raw)
    raise ValueError("unsupported encoding: %s" % encoding)


class FakeState(object):
    def __init__(self):
        self.token = "TOKEN"
        self.refresh_token = "refresh-1"
        self.api_key = "AK"
        self.secret_key = "SK"
        self.user_id = "u1"
        self.user_name = "账号一"
        self.domain_id = "d1"
        self.expiration = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.requests = []
        self.last_headers = None
        self.last_header_list = None
        self.last_body = None
        self.last_encrypted = False
        self.last_signature_ok = False
        self.key_exchange_error = None   # (status, body_dict) 或 None
        self.key_exchanges = []
        self.key_id = ""
        self.sm4_key = None
        self.encrypt_response = True
        self.require_encryption = False
        self.response_encoding = ""      # gzip | deflate | br | zstd
        self.heartbeats = []
        self.sessions = []
        self.session_status = 0
        self.chat_text = "你好"
        self.chat_mode = "delta"  # delta | text
        self.chat_error = None    # (status, body_dict) 或 None
        self.chat_error_remaining = -1  # -1 = 一直失败；N = 失败 N 次后恢复
        self.chat_headers = {}    # 聊天错误响应附带的标头（Retry-After 等）
        self.error_encoding = ""  # 聊天错误体的压缩方式
        self.grant_credits = False  # 领取活动时把额度写回套餐快照
        self.embedded_error = None  # {"error_code": ..., "error_msg": ...}
        self.require_benefit_header = False
        self.omit_identity = False
        self.builtin = [
            {"model_id": "GLM-5.2", "model_name": "GLM-5.2",
             "context_window": 131072, "max_tokens": 8192,
             "credit": [
                 {"input_from": 0, "input_to": 32000, "ratio_display": "0.7x",
                  "time_slot_id": None, "before_display_ratio": None},
                 {"input_from": 32001, "input_to": -1, "ratio_display": "0.7x",
                  "time_slot_id": None, "before_display_ratio": None},
             ]},
            # 错峰档排在前面：标价不能取到折扣。
            {"model_id": "GLM-4.7", "model_name": "GLM-4.7",
             "credit": [
                 {"ratio_display": "0.09x", "time_slot_id": "off_peak",
                  "before_display_ratio": "0.18x"},
                 {"ratio_display": "0.18x", "time_slot_id": "default",
                  "before_display_ratio": None},
             ]},
            {"model_id": "Qwen3-VL-235B", "model_name": "Qwen3-VL-235B",
             "context_window": 131072, "max_tokens": 8192,
             "supports_images": True, "model_category": "图片理解",
             "credit": []},
        ]
        self.agent_models = [
            {"model_name": "GLM-5.2", "model_id": "GLM-5.2",
             "model_parameters": {"context_window": 131072, "max_tokens": 8192}},
        ]
        self.benefit_models = [
            {"model_id": "deepseek-v4-pro-0901", "model_name": "deepseek-v4-pro-0901",
             "context_window": 65536, "max_tokens": 4096},
        ]
        self.campaigns = [{
            "campaignId": 1, "title": "每日签到", "status": "ELIGIBLE",
            "claimable": True, "benefitUnit": "CREDIT",
            "pendingTotalAmount": 1000,
            "extra": {"startTime": "2026-01-01T00:00:00Z",
                      "endTime": "2026-12-31T00:00:00Z"},
        }]
        self.token_calls = 0
        self.model_calls = 0
        self.package = {
            "spec_code": "codearts.agent.individual.trial",
            "status": "normal",
            "package_name_cn": "体验版",
            "package_name_en": "Trial",
            "is_token_package": False,
            "is_credit_package": True,
        }
        self.credit_metrics = [
            {"name": "usageTotalPackageCredit", "package_credit_amount": 4500.0,
             "package_credit_used": 0.81, "package_credit_remain": 4499.19,
             "package_credit_expiring_amount": 0},
            {"name": "usageBasicPackageCredit", "package_credit_amount": 500.0,
             "package_credit_used": 0.0, "package_credit_remain": 500.0,
             "package_credit_expiring_amount": 0},
            {"name": "usageOnDemandPackageCredit", "package_credit_amount": 0,
             "package_credit_used": 0, "package_credit_remain": 0,
             "package_credit_expiring_amount": 0},
            {"name": "usageBonusPackageCredit", "package_credit_amount": 4000.0,
             "package_credit_used": 0.81, "package_credit_remain": 3999.19,
             "package_credit_expiring_amount": 0},
        ]
        self.model_metrics = [
            {"model_id": "GLM-5.2", "model_name": "GLM-5.2",
             "model_desc": "最新旗舰模型", "model_type": "system",
             "enable": True, "status": "normal", "is_luxury": False},
        ]

    def reset_requests(self):
        self.requests = []
        self.last_headers = None
        self.last_header_list = None
        self.last_body = None
        self.last_encrypted = False
        self.last_signature_ok = False
        self.heartbeats = []
        self.sessions = []
        self.key_exchanges = []


def _sse_chunks(state, model):
    created = int(time.time())
    if state.chat_mode == "text":
        events = [
            {"text": "你", "prompt_tokens": 3, "completion_tokens": 1},
            {"text": "你好", "prompt_tokens": 3, "completion_tokens": 2},
            {"text": "[DONE]", "error_code": "0"},
        ]
        out = b""
        for event in events:
            out += ("data: " + json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        return out
    chunks = [
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                      "finish_reason": None}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {"content": state.chat_text},
                      "finish_reason": None}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "chatcmpl-fake", "object": "chat.completion.chunk",
         "created": created, "model": model, "choices": [],
         "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                   "total_tokens": 5}},
    ]
    out = b""
    for chunk in chunks:
        out += ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n").encode("utf-8")
    out += b"data: [DONE]\n"
    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state = None

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload, headers=None):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(raw)

    def _send_chat_error(self, status, payload, headers=None, encrypted=False):
        state = self.state
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        extra = {}
        if encrypted and state.sm4_key:
            key_id = "resp-" + state.key_id
            iv = secrets.token_bytes(12)
            encryptor = Cipher(algorithms.SM4(state.sm4_key),
                               modes.GCM(iv)).encryptor()
            encryptor.authenticate_additional_data(
                ("%s|0" % key_id).encode("utf-8"))
            ciphertext = encryptor.update(raw) + encryptor.finalize()
            raw = b":".join([
                base64.b64encode(iv),
                base64.b64encode(encryptor.tag),
                base64.b64encode(ciphertext),
            ])
            extra["X-Encrypt-Algo"] = "sm4-gcm"
            extra["X-Encrypt-Key-Id"] = key_id
        if state.error_encoding:
            raw = _compress(raw, state.error_encoding)
            extra["Content-Encoding"] = state.error_encoding
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        for name, value in extra.items():
            self.send_header(name, value)
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, raw, encrypted=False):
        state = self.state
        extra = {}
        body = raw
        if encrypted and state.sm4_key:
            key_id = "resp-" + state.key_id
            lines = []
            index = 0
            for line in raw.split(b"\n"):
                if not line.strip():
                    lines.append(line)
                    continue
                iv = secrets.token_bytes(12)
                encryptor = Cipher(algorithms.SM4(state.sm4_key),
                                   modes.GCM(iv)).encryptor()
                encryptor.authenticate_additional_data(
                    ("%s|%d" % (key_id, index)).encode("utf-8"))
                ciphertext = encryptor.update(line) + encryptor.finalize()
                lines.append(":".join([
                    base64.b64encode(iv).decode("ascii"),
                    base64.b64encode(encryptor.tag).decode("ascii"),
                    base64.b64encode(ciphertext).decode("ascii"),
                ]).encode("ascii"))
                index += 1
            body = b"\n".join(lines)
            extra["X-Encrypt-Algo"] = "sm4-gcm"
            extra["X-Encrypt-Key-Id"] = key_id
        if state.response_encoding:
            body = _compress(body, state.response_encoding)
            extra["Content-Encoding"] = state.response_encoding
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method):
        raw = self._read()
        path, _, query = self.path.partition("?")
        header_list = list(self.headers.raw_items())
        lower = {name.lower(): value for name, value in header_list}
        state = self.state
        state.requests.append({
            "method": method, "path": path, "query": query,
            "body": raw.decode("utf-8", "replace"), "raw_body": raw,
            "headers": lower, "header_list": header_list,
        })
        state.last_headers = lower
        state.last_header_list = header_list
        return path, query, raw, header_list, lower

    def _key_exchange(self, raw, header_list):
        state = self.state
        if state.key_exchange_error:
            self._send_json(*state.key_exchange_error)
            return
        payload = json.loads(raw.decode("utf-8"))
        state.last_signature_ok = verify_signature(
            "POST", "/api/v1/security/key-exchange", "", header_list, raw,
            state.secret_key, payload_hash="UNSIGNED-PAYLOAD")
        server = ec.generate_private_key(ec.SECP256R1())
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), base64.b64decode(payload["clientPublicKey"]))
        shared = server.exchange(ec.ECDH(), peer)
        state.sm4_key = hashlib.sha256(shared).digest()[:16]
        state.key_id = secrets.token_hex(16)
        public = server.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        state.key_exchanges.append({
            "session_id": payload.get("sessionId"),
            "timestamp": payload.get("timestamp"),
            "body": payload,
        })
        self._send_json(200, {
            "expiresIn": 7200,
            "algorithm": "sm4-gcm",
            "serverPublicKey": base64.b64encode(public).decode("ascii"),
            "keyId": state.key_id,
        })

    def _decrypt_chat(self, raw, lower):
        state = self.state
        if str(lower.get("x-encrypt-algo") or "").lower() != "sm4-gcm":
            state.last_encrypted = False
            return None if state.require_encryption else raw
        if not state.sm4_key:
            return None
        try:
            iv = base64.b64decode(lower["x-encrypt-iv"])
            tag = base64.b64decode(lower["x-encrypt-tag"])
            ciphertext = base64.b64decode(raw)
            ts = lower["x-encrypt-ts"]
            decryptor = Cipher(algorithms.SM4(state.sm4_key),
                               modes.GCM(iv, tag)).decryptor()
            decryptor.authenticate_additional_data(
                ("POST|/api/v2/chat/completions|" + ts).encode("utf-8"))
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        except Exception:
            return None
        state.last_encrypted = True
        return plaintext

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _token_payload(self):
        state = self.state
        payload = {
            "refresh_token": state.refresh_token,
            "credentials": {
                "access_key_id": state.api_key,
                "secret_access_key": state.secret_key,
                "security_token": state.token,
                "expiration": state.expiration,
            },
        }
        if not state.omit_identity:
            payload.update({
                "user_id": state.user_id,
                "user_name": state.user_name,
                "domain_id": state.domain_id,
            })
        return payload

    def do_POST(self):
        path, query, raw, header_list, lower = self._record("POST")
        state = self.state
        state.last_body = None
        if path == "/api/v1/security/key-exchange":
            self._key_exchange(raw, header_list)
            return
        if path == "/api/v2/chat/completions":
            plaintext = self._decrypt_chat(raw, lower)
            if plaintext is None:
                self._send_json(400, {"error_msg": "chat body is not usable"})
                return
            state.last_signature_ok = verify_signature(
                "POST", path, query, header_list, plaintext, state.secret_key,
                payload_hash=(lower.get("x-sdk-content-sha256")
                              if state.last_encrypted else None))
            try:
                state.last_body = json.loads(plaintext.decode("utf-8"))
            except Exception:
                state.last_body = {}
            token = lower.get("x-security-token") or lower.get("x-auth-token")
            if token != state.token:
                self._send_json(401, {"error_msg": "bad token"})
                return
            if state.require_benefit_header and lower.get("maas_type") != "benefit":
                self._send_json(200, {"error_code": "InferHub.002002009.404",
                                      "error_msg": "model is not registered"})
                return
            encrypted = state.last_encrypted and state.encrypt_response
            if state.chat_error:
                if state.chat_error_remaining == 0:
                    state.chat_error = None
                else:
                    if state.chat_error_remaining > 0:
                        state.chat_error_remaining -= 1
                    self._send_chat_error(*state.chat_error,
                                          headers=state.chat_headers,
                                          encrypted=encrypted)
                    return
            if state.embedded_error:
                raw_sse = ("data: " + json.dumps(state.embedded_error) + "\n").encode()
                self._send_sse(raw_sse, encrypted=encrypted)
                return
            self._send_sse(_sse_chunks(state, state.last_body.get("model") or "GLM-5.2"),
                           encrypted=encrypted)
            return
        if path == "/v1/sessions":
            state.last_signature_ok = verify_signature(
                "POST", path, query, header_list, raw, state.secret_key)
            state.sessions.append({"body": json.loads(raw.decode("utf-8")),
                                   "headers": lower})
            self._send_json(200, {"data": {"session_id": secrets.token_hex(8),
                                            "sessions": []}})
            return
        if path == "/v1/oauth2/tokens":
            state.token_calls += 1
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
            if form.get("grant_type", [""])[0] == "refresh_token":
                if not self.headers.get("DPoP"):
                    self._send_json(400, {"error": "invalid_grant",
                                          "error_msg": "missing DPoP"})
                    return
            self._send_json(200, self._token_payload())
            return
        try:
            state.last_body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            state.last_body = {}
        if path == "/api/v1/benefit/claim":
            self._send_json(200, {"error_code": "0000", "error_msg": ""})
            return
        if path == "/v1/ops/claim":
            if state.grant_credits and state.credit_metrics:
                total = state.credit_metrics[0]
                total["package_credit_used"] = max(
                    0.0, float(total.get("package_credit_used") or 0) - 1000.0)
                total["package_credit_remain"] = (
                    float(total.get("package_credit_remain") or 0) + 1000.0)
            self._send_json(200, {"code": 0, "message": "ok", "data": {}})
            return
        if path == "/v1/ops/confirm":
            self._send_json(200, {"code": 0, "message": "ok", "data": {}})
            return
        self._send_json(404, {"error_msg": "not found"})

    def do_PUT(self):
        path, query, raw, header_list, lower = self._record("PUT")
        state = self.state
        if path != "/snap-manager/v1/chat-session/heartbeat":
            self._send_json(404, {"error_msg": "not found"})
            return
        status = urllib.parse.parse_qs(query).get("status", [""])[0]
        state.last_signature_ok = verify_signature(
            "PUT", path, query, header_list, raw, state.secret_key)
        state.heartbeats.append({
            "status": status,
            "headers": lower,
            "header_list": header_list,
            "body": raw.decode("utf-8", "replace"),
            "session": lower.get("user-session-id", ""),
        })
        self._send_json(200, {})

    def do_GET(self):
        path, query, raw, header_list, lower = self._record("GET")
        state = self.state
        if path == "/v1/sessions":
            params = urllib.parse.parse_qs(query)
            state.last_signature_ok = verify_signature(
                "GET", path, query, header_list, b"", state.secret_key)
            session = {
                "user_id": state.user_id,
                "ip_address": params.get("ip_address", [""])[0],
                "device_name": params.get("device_name", [""])[0],
                "status": state.session_status,
            }
            state.sessions.append({"query": params, "headers": lower})
            self._send_json(200, {"data": {"sessions": [session]}})
            return
        if path == "/v1/login/ticket":
            self._send_json(200, self._token_payload())
            return
        if path == "/v1/model/builtin":
            state.model_calls += 1
            self._send_json(200, {"builtinModels": state.builtin})
            return
        if path == "/v1/agent-center/agents/useragents":
            self._send_json(200, {"agents": [{
                "agent_id": "agent-1", "agent_name": "CodeAgent",
                "is_primary_agent": True, "show_in_ide": True,
                "alias": {"alias_zh_cn": "智能体"},
            }]})
            return
        if path == "/v1/agent-center/agents/detail":
            self._send_json(200, {"gpts": {"models": state.agent_models}})
            return
        if path == "/api/v1/gateway/config":
            self._send_json(200, {"error_code": "0000", "result": {
                "base_url": "https://example.test", "models": state.benefit_models}})
            return
        if path == "/snap-manager/v1/statistics/plugin":
            self._send_json(200, {
                "metrics": state.credit_metrics,
                "model_metrics": state.model_metrics,
                "package": state.package,
                "start_date": "2026-10-31",
                "end_date": "2026-12-01",
            })
            return
        if path == "/v1/ops/delivery":
            self._send_json(200, {"code": 0, "message": "ok",
                                  "data": {"items": state.campaigns}})
            return
        if path == "/v5/caller-identity":
            self._send_json(200, {"account_id": state.domain_id,
                                  "principal_id": state.user_id,
                                  "principal_urn": "iam::%s:user/%s" % (
                                      state.domain_id, state.user_name)})
            return
        if path == "/v1/current/user":
            self._send_json(200, {"user_id": state.user_id,
                                  "user_name": state.user_name,
                                  "domain_id": state.domain_id})
            return
        self._send_json(404, {"error_msg": "not found"})


def start_server(state=None):
    state = state or FakeState()
    handler = type("FakeHuawei", (_Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    return server, base, state


def stop_server(server):
    try:
        server.shutdown()
        server.server_close()
    except Exception:
        pass
