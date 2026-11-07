import base64
import hashlib
import json
import re
import secrets
import socket
import threading
import time
import urllib.parse
import zlib

import brotli
import zstandard

import wb_codearts_crypto as crypto
import wb_upstream_pool

CHAT_PATH = "/api/v2/chat/completions"
KEY_EXCHANGE_PATH = "/api/v1/security/key-exchange"
HEARTBEAT_PATH = "/snap-manager/v1/chat-session/heartbeat"
SESSIONS_PATH = "/v1/sessions"

ENCRYPT_ALGO = "sm4-gcm"
CHAT_ACCEPT_ENCODING = "gzip, deflate, br, zstd"
SDK_ACCEPT_ENCODING = "gzip, compress, deflate, br"
SDK_USER_AGENT = "huaweicloud-usdk-nodejs/3.0"
HEARTBEAT_INTERVAL = 30.0
DEVICE_POLL_INTERVAL = 120.0

_B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_identifier_lock = threading.Lock()
_identifier_ms = 0
_identifier_counter = 0


class WireError(Exception):
    """上游报文与官方格式不符。"""


def _compact(payload):
    return json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def _sdk_date(now=None):
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _created_time(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def identifier(prefix):
    """`ses_`/`msg_` 标识：毫秒时间戳 × 4096 + 计数器，低 6 字节 hex，后接 14 位 base62。"""
    global _identifier_ms, _identifier_counter
    ms = int(time.time() * 1000)
    with _identifier_lock:
        if ms != _identifier_ms:
            _identifier_ms = ms
            _identifier_counter = 0
        _identifier_counter += 1
        counter = _identifier_counter
    value = (ms * 4096 + counter) & 0xFFFFFFFFFFFF
    suffix = "".join(_B62[byte % 62] for byte in secrets.token_bytes(14))
    return "%s_%012x%s" % (prefix, value, suffix)


def session_id():
    return identifier("ses")


def message_id():
    return identifier("msg")


def trace_id():
    return secrets.token_hex(16)


def span_id():
    return secrets.token_hex(8)


def encode_model_name(name):
    """JS encodeURIComponent 的等价写法。"""
    return urllib.parse.quote(str(name or ""), safe="-_.!~*'()")


_LOCAL_IP = ""
_LOCAL_IP_LOCK = threading.Lock()


def client_ip():
    """第一个非内部 IPv4；找不到时用 127.0.0.1。"""
    global _LOCAL_IP
    with _LOCAL_IP_LOCK:
        if _LOCAL_IP:
            return _LOCAL_IP
        cached = _probe_client_ip()
        _LOCAL_IP = cached
        return cached


def _probe_client_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 53))
            address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            return address
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            address = str(info[4][0])
            if address.startswith(("127.", "169.254.", "0.")):
                continue
            return address
    except OSError:
        pass
    return "127.0.0.1"


def last_user_prompt(messages):
    """官方 `user_prompt`：最后一条 user 文本，去系统提醒、压空白、截 500 字符。"""
    for message in reversed(messages or ()):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict) and part.get("type") == "text")
        else:
            text = ""
        text = _SYSTEM_REMINDER.sub("", text)
        text = _SYSTEM_REMINDER_TAIL.sub("", text).strip()
        if len(text) > 500:
            text = text[:500]
        return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return ""


_SYSTEM_REMINDER = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
_SYSTEM_REMINDER_TAIL = re.compile(r"<system-reminder[\s\S]*$")


def _identity_headers(profile, model_id, model_name, benefit, turn, token,
                      created, sdk_date):
    headers = {
        "client-ip": client_ip(),
        "created-time": created,
        "ide-name": profile["ide_name"],
        "model-id": str(model_id),
        "model-name": encode_model_name(model_name),
        "plugin-name": profile["plugin_name"],
        "plugin-version": profile["plugin_version"],
        "user-msg-id": turn["msg"],
        "user-session-id": turn["session"],
        "x-language": "zh-cn",
        "x-ot-client-type": profile["client_type"],
        "x-ot-client-version": profile["client_version"],
        "x-ot-function": profile["function"],
        "x-ot-parent-session-id": "",
        "x-ot-session-id": turn["session"],
        "x-ot-span-id": turn["span"],
        "x-ot-trace-id": turn["trace"],
        "x-sdk-date": sdk_date,
        "x-security-token": str(token or ""),
        "x-snap-traceid": turn["snap"],
    }
    if profile.get("ide_version"):
        headers["ide-version"] = profile["ide_version"]
    if benefit:
        headers["maas_type"] = "benefit"
    return headers


def _sorted_header_items(values):
    """AI SDK 的 Headers 按 ASCII 排序后交给 Bun。"""
    return [(name, values[name]) for name in sorted(values)]


def _transport_tail(content_length):
    items = [("Connection", "keep-alive"), ("Accept", "*/*"),
             ("Accept-Encoding", CHAT_ACCEPT_ENCODING)]
    if content_length is not None:
        items.append(("Content-Length", str(content_length)))
    return items


def sign(method, url, signed_values, body, credential, payload_hash=None,
         x_date=None):
    """签名只覆盖传入的头；令牌头由调用方写进 `signed_values`。"""
    return crypto.sign_request(
        method, url, signed_values, body or b"",
        credential.get("access_key_id") or "",
        credential.get("secret_access_key") or "",
        "", x_date=x_date, content_sha256=False, payload_hash=payload_hash,
        include_security_token=False)


def chat_request(profile, model_id, model_name, benefit, turn, credential,
                 base_url, plaintext, key_material=None):
    """生成一次聊天请求。`key_material` 为 (会话密钥, keyId) 时加密正文。"""
    url = base_url + CHAT_PATH
    now = time.time()
    sdk_date = _sdk_date(now)
    created = _created_time(now)
    token = credential.get("security_token") or ""
    if key_material:
        key, key_id = key_material
        ts = str(int(now * 1000))
        iv = secrets.token_bytes(12)
        aad = ("POST|%s|%s" % (CHAT_PATH, ts)).encode("utf-8")
        ciphertext, tag = crypto.sm4_gcm_encrypt(key, iv, aad, plaintext)
        wire_body = base64.b64encode(ciphertext)
        encrypt = {
            "X-Encrypt-Algo": ENCRYPT_ALGO,
            "X-Encrypt-Iv": base64.b64encode(iv).decode("ascii"),
            "X-Encrypt-Key-Id": key_id,
            "X-Encrypt-Tag": base64.b64encode(tag).decode("ascii"),
            "X-Encrypt-Ts": ts,
        }
        signed_values = {
            "Content-Type": "application/octet-stream",
            "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",
            "x-security-token": token,
        }
        auth = sign("POST", url, signed_values, wire_body, credential,
                    payload_hash="UNSIGNED-PAYLOAD", x_date=sdk_date)
        values = _identity_headers(profile, model_id, model_name, benefit,
                                   turn, token, created, sdk_date)
        values.update(encrypt)
        values.update({
            "Authorization": auth["Authorization"],
            "Content-Type": "application/octet-stream",
            "Host": urllib.parse.urlsplit(url).netloc,
            "User-Agent": profile["chat_user_agent"],
            "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",
        })
        order = _sorted_header_items(values)
        return {"url": url, "body": wire_body, "encrypted": True,
                "key_id": key_id, "headers": order + _transport_tail(len(wire_body))}

    signed_values = _identity_headers(profile, model_id, model_name, benefit,
                                      turn, token, created, sdk_date)
    auth = sign("POST", url, signed_values, plaintext, credential,
                payload_hash=_sha256_hex(plaintext), x_date=sdk_date)
    values = dict(signed_values)
    values.update({
        "Authorization": auth["Authorization"],
        "Content-Type": "application/json",
        "Host": urllib.parse.urlsplit(url).netloc,
        "User-Agent": profile["chat_user_agent"],
    })
    order = _sorted_header_items(values)
    return {"url": url, "body": plaintext, "encrypted": False, "key_id": "",
            "headers": order + _transport_tail(len(plaintext))}


def key_exchange(base_url, credential, profile, proxy="", timeout=20.0):
    """ECDH P-256 换 SM4 会话密钥；返回 (key, keyId, expiresIn)。"""
    url = base_url + KEY_EXCHANGE_PATH
    private, public_b64 = crypto.ecdh_keypair()
    body = _compact({
        "clientPublicKey": public_b64,
        "sessionId": secrets.token_hex(16),
        "timestamp": int(time.time() * 1000),
    })
    sdk_date = _sdk_date()
    token = credential.get("security_token") or ""
    signed_values = {
        "Content-Type": "application/json",
        "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",
        "X-Security-Token": token,
    }
    auth = sign("POST", url, signed_values, body, credential,
                payload_hash="UNSIGNED-PAYLOAD", x_date=sdk_date)
    values = {
        "Authorization": auth["Authorization"],
        "Content-Type": "application/json",
        "Host": urllib.parse.urlsplit(url).netloc,
        "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",
        "X-Sdk-Date": sdk_date,
        "X-Security-Token": token,
    }
    order = _sorted_header_items(values)
    order += [("Connection", "keep-alive"), ("User-Agent", profile["sdk_user_agent"]),
              ("Accept", "*/*"), ("Accept-Encoding", CHAT_ACCEPT_ENCODING),
              ("Content-Length", str(len(body)))]
    resp = wb_upstream_pool.open_ordered(url, "POST", order, body,
                                         timeout=timeout, proxy=proxy,
                                         pooled=wb_upstream_pool.enabled())
    try:
        payload = json.loads(read_body(resp).decode("utf-8"))
    finally:
        resp.close()
    server_public = str(payload.get("serverPublicKey") or "")
    key_id = str(payload.get("keyId") or "")
    if not server_public or not key_id:
        raise WireError("key exchange response is missing fields")
    key = crypto.derive_session_key(private, server_public)
    return key, key_id, int(payload.get("expiresIn") or 0)


def send_heartbeat(base_url, credential, session, status, proxy="",
                   timeout=10.0):
    """`PUT /snap-manager/v1/chat-session/heartbeat?status=busy|idle`，体固定 `{}`。"""
    query = urllib.parse.urlencode({"status": status})
    url = base_url + HEARTBEAT_PATH + "?" + query
    body = b"{}"
    sdk_date = _sdk_date()
    token = credential.get("security_token") or ""
    values = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "user-session-id": session,
        "x-client-type": "kernel",
        "x-language": "zh-cn",
        "x-snap-traceid": secrets.token_hex(16),
    }
    if token:
        values["x-security-token"] = token
    signed_values = dict(values)
    auth = sign("PUT", url, signed_values, body, credential,
                payload_hash=_sha256_hex(body), x_date=sdk_date)
    values.update({
        "Authorization": auth["Authorization"],
        "Host": urllib.parse.urlsplit(url).netloc,
        "x-sdk-date": sdk_date,
        "User-Agent": SDK_USER_AGENT,
        "Accept-Encoding": SDK_ACCEPT_ENCODING,
    })
    order = sorted(values.items(), key=lambda item: item[0].lower())
    order.append(("Content-Length", str(len(body))))
    resp = wb_upstream_pool.open_ordered(url, "PUT", order, body,
                                         timeout=timeout, proxy=proxy,
                                         pooled=wb_upstream_pool.enabled())
    try:
        resp.read()
    finally:
        resp.close()
    return True


def device_session_cli(base_url, credential, action, profile, proxy="",
                       timeout=15.0):
    """CLI 的设备会话：POST 登记，GET 轮询。"""
    if action == "create":
        body = _compact({
            "client_type": "CodeArts Agent CLI",
            "device_name": socket.gethostname(),
            "device_type": "Windows",
            "ip_address": client_ip(),
            "client_version": profile["client_version"],
        })
        method = "POST"
        url = base_url + SESSIONS_PATH
        body_bytes = body
    else:
        query = urllib.parse.urlencode({
            "client_type": "CodeArts Agent CLI",
            "device_name": socket.gethostname(),
            "client_version": profile["client_version"],
            "page": "1",
            "page_size": "10",
        }, quote_via=urllib.parse.quote)
        method = "GET"
        url = base_url + SESSIONS_PATH + "?" + query
        body_bytes = None
    sdk_date = _sdk_date()
    token = credential.get("security_token") or ""
    signed_values = {"Agent-Type": "PromptCenter",
                     "Content-Type": "application/json"}
    if token:
        signed_values["X-Security-token"] = token
    auth = sign(method, url, signed_values, body_bytes or b"", credential,
                payload_hash=_sha256_hex(body_bytes or b""), x_date=sdk_date)
    values = dict(signed_values)
    values.update({
        "Authorization": auth["Authorization"],
        "Host": urllib.parse.urlsplit(url).netloc,
        "X-Sdk-Date": sdk_date,
    })
    order = _sorted_header_items(values)
    order += [("Connection", "keep-alive"),
              ("User-Agent", "codearts/" + profile["client_version"]),
              ("Accept", "*/*"),
              ("Accept-Encoding", CHAT_ACCEPT_ENCODING)]
    if body_bytes is not None:
        order.append(("Content-Length", str(len(body_bytes))))
    resp = wb_upstream_pool.open_ordered(url, method, order, body_bytes,
                                         timeout=timeout, proxy=proxy,
                                         pooled=wb_upstream_pool.enabled())
    try:
        raw = read_body(resp)
    finally:
        resp.close()
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}


def _device_request_time(now=None):
    stamp = time.localtime(now)
    return time.strftime("%Y-%m-%d %H:%M:%S", stamp)


def device_session_ide(base_url, credential, action, profile, status=0,
                       proxy="", timeout=15.0):
    """IDE 的设备会话：登录时 POST status=0，轮询 GET，退出时 POST status=1。"""
    token = credential.get("security_token") or ""
    sdk_date = _sdk_date()
    common = {
        "content-type": "application/json",
        "Agent-Type": "PromptCenter",
        "X-Language": "zh-cn",
        "x-snap-traceid": secrets.token_hex(16),
        "ide-name": profile.get("device_ide_name") or "CodeArts Agent",
        "ide-version": profile["client_version"],
        "plugin-name": profile["plugin_name"],
        "plugin-version": profile["plugin_version"],
    }
    if token:
        common["X-Security-Token"] = token
    if action in ("create", "status"):
        body = _compact({
            "device_name": socket.gethostname(),
            "device_type": "Windows",
            "ip_address": client_ip(),
            "request_time": _device_request_time(),
            "client_type": profile.get("device_client_type") or "CodeArts Agent",
            "client_version": profile["client_version"],
            "status": int(status),
        })
        method = "POST"
        url = base_url + SESSIONS_PATH
        body_bytes = body
        payload_hash = _sha256_hex(body)
    else:
        query = urllib.parse.urlencode({
            "client_type": profile.get("device_client_type") or "CodeArts Agent",
            "device_name": socket.gethostname(),
            "ip_address": client_ip(),
            "device_type": "Windows",
            "page": "1",
            "page_size": "10",
        }, quote_via=urllib.parse.quote)
        method = "GET"
        url = base_url + SESSIONS_PATH + "?" + query
        body_bytes = None
        payload_hash = _sha256_hex(b"")
    auth = sign(method, url, common, body_bytes or b"", credential,
                payload_hash=payload_hash, x_date=sdk_date)
    values = dict(common)
    values.update({
        "Authorization": auth["Authorization"],
        "Host": urllib.parse.urlsplit(url).netloc,
        "X-Sdk-Date": sdk_date,
        "Accept": "application/json",
        "Accept-Encoding": SDK_ACCEPT_ENCODING,
        "User-Agent": SDK_USER_AGENT,
    })
    order = sorted(values.items(), key=lambda item: item[0].lower())
    if body_bytes is not None:
        order.append(("Content-Length", str(len(body_bytes))))
    resp = wb_upstream_pool.open_ordered(url, method, order, body_bytes,
                                         timeout=timeout, proxy=proxy,
                                         pooled=wb_upstream_pool.enabled())
    try:
        raw = read_body(resp)
    finally:
        resp.close()
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# 响应解码：压缩 + SSE 逐行解密
# ---------------------------------------------------------------------------

def _decoder(encoding):
    parts = []
    for token in [item.strip().lower() for item in str(encoding or "").split(",")
                  if item.strip()]:
        if token in ("identity", ""):
            continue
        if token in ("gzip", "x-gzip"):
            parts.append(("zlib", zlib.decompressobj(16 + zlib.MAX_WBITS)))
        elif token == "deflate":
            parts.append(("zlib", zlib.decompressobj(zlib.MAX_WBITS)))
        elif token == "br":
            parts.append(("br", brotli.Decompressor()))
        elif token == "zstd":
            parts.append(("zstd", zstandard.ZstdDecompressor().decompressobj()))
        else:
            raise WireError("unsupported content-encoding: %s" % token)
    # Content-Encoding 按应用顺序列出，解码要反过来。
    parts.reverse()
    return parts


def _decode(chunk, decoders):
    for kind, decoder in decoders:
        if kind == "br":
            chunk = decoder.process(chunk)
        else:
            chunk = decoder.decompress(chunk)
    return chunk


def decode_body(raw, encoding):
    """整段响应的解压（错误体、JSON 应答）；不用于 SSE。"""
    decoders = _decoder(encoding)
    return _decode(raw, decoders) if decoders else raw


def read_body(resp):
    """读完响应体并按 Content-Encoding 解压。"""
    return decode_body(resp.read(), resp.headers.get("Content-Encoding") or "")


def decrypt_line(raw, key, key_id, index=0):
    """解密单独一行 `iv:tag:ciphertext`，用于加密的错误响应体。"""
    stripped = raw.strip()
    parts = stripped.split(b":")
    if not key or len(parts) != 3:
        return raw
    iv = base64.b64decode(parts[0])
    tag = base64.b64decode(parts[1])
    ciphertext = base64.b64decode(parts[2])
    aad = ("%s|%d" % (str(key_id or ""), index)).encode("utf-8")
    return crypto.sm4_gcm_decrypt(key, iv, tag, aad, ciphertext)


class LineReader(object):
    """把响应体解压成字节流，按 SSE 行返回；加密响应逐行解密。"""

    def __init__(self, resp, decrypt_key=None, decrypt_key_id=""):
        self.resp = resp
        encoding = resp.headers.get("Content-Encoding") or ""
        self.decoders = _decoder(encoding)
        self.decrypt_key = decrypt_key
        self.decrypt_key_id = str(decrypt_key_id or "")
        self._buffer = b""
        self._eof = False
        self._index = 0
        self._closed = False

    def _fill(self, want_newline=True):
        while True:
            if want_newline and b"\n" in self._buffer:
                return
            if self._eof:
                return
            chunk = self.resp.read(65536)
            if not chunk:
                self._eof = True
                return
            self._buffer += _decode(chunk, self.decoders)

    def _decrypt(self, line):
        if not self.decrypt_key:
            return line
        stripped = line.strip()
        if not stripped:
            return line
        parts = stripped.split(b":")
        if len(parts) != 3:
            raise WireError("encrypted SSE line is not iv:tag:ciphertext")
        iv = base64.b64decode(parts[0])
        tag = base64.b64decode(parts[1])
        ciphertext = base64.b64decode(parts[2])
        aad = ("%s|%d" % (self.decrypt_key_id, self._index)).encode("utf-8")
        try:
            plaintext = crypto.sm4_gcm_decrypt(
                self.decrypt_key, iv, tag, aad, ciphertext)
        except Exception as exc:
            raise WireError("encrypted SSE line failed to decrypt: %s" % exc)
        self._index += 1
        return plaintext

    def readline(self):
        self._fill()
        if b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
        elif self._buffer:
            line, self._buffer = self._buffer, b""
        else:
            return b""
        return self._decrypt(line) + b"\n"

    def read(self, size=-1):
        if size is None or size < 0:
            self._fill(want_newline=False)
            while not self._eof:
                chunk = self.resp.read(65536)
                if not chunk:
                    self._eof = True
                    break
                self._buffer += _decode(chunk, self.decoders)
            data, self._buffer = self._buffer, b""
        else:
            while len(self._buffer) < size and not self._eof:
                chunk = self.resp.read(65536)
                if not chunk:
                    self._eof = True
                    break
                self._buffer += _decode(chunk, self.decoders)
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        if self.decrypt_key:
            raise WireError("cannot read a decrypted body as a byte stream")
        return data

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.resp.close()
        except Exception:
            pass