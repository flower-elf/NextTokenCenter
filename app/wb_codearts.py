# -*- coding: utf-8 -*-
"""华为云 CodeArts（盘古助手/码道）原生客户端与账号池。

这是单进程版本：wb-hub 直接完成 OAuth 登录、token 续期、账号轮转、模型发现、
聊天 SSE 与签到，不再依赖独立的 codearts2api 服务。

协议来源：官方 CodeArts IDE 26.10.300 与 CodeArts CLI 26.10.15 的安装包请求实现，
以及 codearts2api 的 Go 实现。关键点：

- 聊天：`POST https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions`
  `X-Security-token` + 华为云 AK/SK `SDK-HMAC-SHA256` 签名；先做 ECDH P-256 密钥交换，
  再用 SM4-GCM 加密正文；请求头按官方 CodeArts IDE 或 CLI 档案生成
  （`x-ot-client-*`、`plugin-*`、`ide-*`），`stream=true`。
- 登录：华为云 CodeArts OAuth2（PKCE + DPoP）→ `sts.cn-north-4/v1/oauth2/tokens`
  换 STS 临时 AK/SK + security_token；refresh_token 与 client_id、DPoP 公钥绑定。
- 模型：agent-center / builtin / 限时福利三路合并，福利模型聊天时加
  `maas_type: benefit`。
- 签到：`/v1/ops/delivery|claim|confirm`，`Agent-Type: PromptCenter`。

依赖：Python 3.12+；`cryptography`、`brotli`、`zstandard` 以外只用标准库。密码学原语在
`wb_codearts_crypto.py`，官方报文格式在 `wb_codearts_wire.py`。
"""

import collections
import copy
import datetime
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import wb_accounts
import wb_settings
import wb_codearts_crypto as crypto
import wb_codearts_wire as wire
import wb_upstream_pool

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SNAP_HOST = os.environ.get("WB_CODEARTS_SNAP_HOST") or \
    "https://snap-access.cn-north-4.myhuaweicloud.com"
SNAP_MANAGER = os.environ.get("WB_CODEARTS_SNAP_MANAGER") or \
    SNAP_HOST + "/snap-manager"
PORTAL_HOST = os.environ.get("WB_CODEARTS_PORTAL_HOST") or \
    "https://codearts.huaweicloud.com/portal"
STS_HOST = os.environ.get("WB_CODEARTS_STS_HOST") or \
    "https://sts.cn-north-4.myhuaweicloud.com"
BENEFIT_HOST = os.environ.get("WB_CODEARTS_BENEFIT_HOST") or \
    "https://opengw.developer.huaweicloud.com"

#: 华为云积分按北京时间划分自然日：签到活动的起止时刻都是 16:00Z。
CREDIT_TZ = datetime.timezone(datetime.timedelta(hours=8))

CLIENT_ID = "codearts-agent"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"
REDIRECT_PATH = "/oauth/callback"

#: 官方 CodeArts IDE 与 CodeArts CLI 的聊天标头不同，登录用的 OAuth 客户端也不同。
#: 版本取自安装包：IDE 26.10.300（插件 snap_vscode），CLI 26.10.15。
CLIENT_TYPE_PROFILES = {
    "ide": {
        "client_type": "IDE",
        "client_version": "26.10.300",
        "function": "agent-general",
        "plugin_name": "codemate_vscode",
        "plugin_version": "26.10.300",
        "ide_name": "VSCode",
        "ide_version": "26.10.300",
        # 内置 agentkernelServer 的 AI SDK 版本与 key-exchange 的 Bun 默认 UA。
        "chat_user_agent": "ai-sdk/provider-utils/4.0.1 runtime/bun/1.3.14",
        "sdk_user_agent": "Bun/1.3.14",
        # IDE 固定 providerID=inferhub-provider，福利模型也带 tool_stream。
        "tool_stream_always": True,
        "device_ide_name": "CodeArts Agent",
        "device_client_type": "CodeArts Agent",
        "oauth_client_id": "codearts-snap",
        "oauth_plugin_name": "snap_vscode",
        "oauth_plugin_version": "26.10.300",
        "oauth_uri_scheme": "codearts-snap",
        "oauth_callback_url": True,
        "checkin_channel": "IDE",
        # 读取积分（statistics/plugin）走华为云 Node SDK：签名覆盖下面的头与空 x-auth-token，
        # Accept、Content-Type 与 User-Agent 在签名之后由 SDK 的 HTTP 客户端补上。
        "statistics": {
            "signed": {
                "x-auth-token": "",
                "ide-name": "CodeArts Agent",
                "ide-version": "26.10.300",
                "plugin-name": "snap_vscode",
                "plugin-version": "26.10.300",
            },
            "token_header": "X-Security-Token",
            "unsigned": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "huaweicloud-usdk-nodejs/3.0 "
                              "DevKit-VSCode:huaweicloud.codearts-snap|CodeArts Agent:D1",
            },
        },
    },
    "cli": {
        "client_type": "CLI",
        "client_version": "26.10.15",
        "function": "agent-tui",
        "plugin_name": "CLI",
        "plugin_version": "26.10.15",
        "ide_name": "CLI",
        "ide_version": "",
        "chat_user_agent": "ai-sdk/provider-utils/4.0.21 runtime/bun/1.3.14",
        "sdk_user_agent": "codearts/26.10.15",
        # 福利模型走 benefit-model provider，CLI 不发 tool_stream。
        "tool_stream_always": False,
        "device_ide_name": "",
        "device_client_type": "",
        "oauth_client_id": "CodeArts_Tui",
        "oauth_plugin_name": "",
        "oauth_plugin_version": "",
        "oauth_uri_scheme": "",
        "oauth_callback_url": False,
        "checkin_channel": "CLI",
        # CLI 用 Bun 的 fetch：只签 Agent-Type，Content-Type 被签名器删掉，其余是 Bun 默认头。
        "statistics": {
            "signed": {"Agent-Type": "PromptCenter"},
            "token_header": "X-Security-token",
            "unsigned": {"User-Agent": "Bun/1.3.14", "Accept": "*/*"},
        },
    },
}
CLIENT_TYPES = tuple(CLIENT_TYPE_PROFILES)
DEFAULT_CLIENT_TYPE = "ide"

EP_CHAT = "/api/v2/chat/completions"
EP_OAUTH_TOKENS = "/v1/oauth2/tokens"
EP_LOGIN_TICKET = "/v1/login/ticket"
EP_MODEL_BUILTIN = "/v1/model/builtin"
EP_AGENT_LIST = "/v1/agent-center/agents/useragents"
EP_AGENT_DETAIL = "/v1/agent-center/agents/detail"
EP_BENEFIT_CONFIG = "/api/v1/gateway/config"
EP_BENEFIT_CLAIM = "/api/v1/benefit/claim"
EP_CREDIT_DELIVERY = "/v1/ops/delivery"
EP_CREDIT_CLAIM = "/v1/ops/claim"
EP_CREDIT_CONFIRM = "/v1/ops/confirm"
EP_PACKAGE_INFO = "/v1/statistics/plugin"

#: 积分活动里「签到」的判据。控制台上的「每日签到赢100积分」是一条
#: `USER_LOGIN` / `triggerEvent=user.login` 的 MANUAL_CLAIM 活动，
#: 但 campaignId 每换季都会变（2026-10 是 34），所以按类型挑，不按 id 硬编码。
#: 1 / 4 是历史 id（4 是「新用户注册送 4000 积分」），留着兼容老账号。
CHECKIN_CAMPAIGN_IDS = (1, 4)
CHECKIN_CAMPAIGN_TYPES = ("USER_LOGIN",)

#: 冷启动种子：首次发现前也能列出/路由福利模型。
SEED_BENEFIT_MODELS = (
    "deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "glm-5.3-flash",
)
SEED_KNOWN_MODELS = (
    "GLM-5.2", "GLM-5.2-ArkTS-SPARK", "GLM-5.1", "GLM-4.7",
    "OpenPangu-2.0-Pro", "OpenPangu-2.0-Flash",
    "Qwen3-VL-235B", "Qwen3.5-397B-A17B-VL", "Qwen3.6-27B-VL",
)
SEED_MODELS = tuple(SEED_KNOWN_MODELS) + tuple(SEED_BENEFIT_MODELS)

#: 模型目录缓存 1 小时；失败时保留上一轮。
CATALOG_TTL = 3600.0
#: 单个账号的模型发现最短间隔（避免每次 /v1/models 都打三路上游）。
CATALOG_MIN_INTERVAL = 60.0
#: 错误阈值与冷却：连续失败到这个次数就整账号停 ERR_COOLDOWN 秒。
ERR_THRESHOLD = 3
ERR_COOLDOWN = 600.0
#: token 提前刷新窗口。
REFRESH_SKEW = 1800.0
#: OAuth 会话 TTL。
OAUTH_TTL = 600.0
#: 错误体读取上限。
ERROR_BODY_MAX = 256 * 1024

DEFAULTS = {
    "client_type": DEFAULT_CLIENT_TYPE,
    "stream_timeout_seconds": 600.0,
    "models_ttl_seconds": 300.0,
    "benefit_auto_claim": True,
    "chat_encryption": True,
    "credits_auto_claim": False,
    "checkin_max_attempts": 3,
    "checkin_retry_gap_seconds": 3600.0,
    "checkin_timeout_seconds": 30.0,
    # 账号行「测试」按钮使用的模型；空串表示用账号自己的默认模型。
    "test_model": "",
}
#: 保存设置时的取值范围。面板先按同一套边界校验，这里再拦一次，超范围直接报错，
#: 不静默夹回边界值。
CONFIG_BOUNDS = {
    "stream_timeout_seconds": (1.0, 3600.0),
    "models_ttl_seconds": (60.0, 86400.0),
    "checkin_max_attempts": (1, 10),
    "checkin_retry_gap_seconds": (0.0, 86400.0),
    "checkin_timeout_seconds": (1.0, 600.0),
}
DEFAULT_ACCOUNT_PRIORITY = 100
MAX_ACCOUNT_PRIORITY = 9999
#: 单账号并发上限的默认值；旧安装用一次迁移读到的全局值覆盖它。
DEFAULT_CONCURRENCY_LIMIT = 1
#: 单账号并发上限的取值范围：上游每个账号最多同时保留 3 个会话，超出返回 TM.00001041。
MIN_CONCURRENCY_LIMIT = 1
MAX_CONCURRENCY_LIMIT = 3

#: 账号身份的选项，面板的身份下拉直接渲染这两项。
IDENTITY_OPTIONS = (
    {"value": "ide", "label": "IDE"},
    {"value": "cli", "label": "CLI"},
)

#: 账号管理动作的日志标签，与 WorkBuddy 的账号动作一致。
ACCOUNT_LOG_TAG = "accounts"

#: Verified with the upstream validation error: reasoning_effort must be one of
#: none/low/medium/high/xhigh/max. `minimal` is accepted but not in that list;
#: `auto` is rejected.
REASONING_EFFORTS = {
    "deepseek-v4.1-flash": ["none", "low", "medium", "high", "xhigh", "max"],
    "deepseek-v4.1-flash-1017": ["none", "low", "medium", "high",
                                 "xhigh", "max"],
    # Verified from the upstream validation message for the GLM backend.
    "glm-5.2": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
    "glm-5.2-sft-harmony": ["none", "minimal", "low", "medium", "high",
                            "xhigh", "max"],
    "glm-5.3": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
    # 常思考模型：上游明确只接受 low/high/max。
    "glm-5.3-flash": ["low", "high", "max"],
}


def client_profile(client_type=None):
    """按配置取官方客户端档案；未知值回落到 IDE。"""
    key = normalize_client_type(client_type)
    return CLIENT_TYPE_PROFILES[key]


def normalize_client_type(client_type=None):
    key = str(client_type or DEFAULT_CLIENT_TYPE).strip().lower()
    return key if key in CLIENT_TYPES else DEFAULT_CLIENT_TYPE


def _account_profile(acct):
    if hasattr(acct, "effective_client_type"):
        return client_profile(acct.effective_client_type())
    config = getattr(getattr(acct, "pool", None), "config", {}) or {}
    return client_profile(config.get("client_type"))


def _requested_effort(payload):
    """按客户端常见的几种写法取思考档位。"""
    if not isinstance(payload, dict):
        return ""
    effort = payload.get("reasoning_effort")
    if effort is None:
        effort = payload.get("reasoningEffort")
    if effort is not None:
        return str(effort).strip().lower()
    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        kind = str(thinking.get("type") or "").strip().lower()
        if kind == "disabled":
            return "none"
        if kind == "enabled":
            return "high"
    if "enable_thinking" in payload:
        return "high" if payload.get("enable_thinking") else "none"
    return ""

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class CodeArtsError(Exception):
    def __init__(self, message, status=None, code=None):
        super(CodeArtsError, self).__init__(message)
        self.status = status
        self.code = code
        # 失败归到哪个账号：用量流水与面板都按这个字段归因。
        self.account_uid = None


class CodeArtsUnavailable(CodeArtsError):
    pass


class CodeArtsNoAccounts(CodeArtsUnavailable):
    """池子里没有可派单的账号：客户端收到 503，等运营去账号页处理。"""

    def __init__(self, message):
        super(CodeArtsNoAccounts, self).__init__(message, status=503)


class CodeArtsBusy(CodeArtsError):
    """可服务的账号都在并发上限内：等一轮在途请求结束后重试。"""

    def __init__(self, message, wait=1):
        super(CodeArtsBusy, self).__init__(message, status=429)
        self.wait = int(wait)


class CodeArtsCooling(CodeArtsError):
    """所有账号都停在冷却或守卫里：把最短的恢复时间回给客户端。"""

    def __init__(self, message, wait=1):
        super(CodeArtsCooling, self).__init__(message, status=429)
        self.wait = int(wait)


class CodeArtsLocalNetwork(CodeArtsUnavailable):
    """本机网络不通：不归咎账号，客户端收到 503。"""

    def __init__(self, detail):
        super(CodeArtsLocalNetwork, self).__init__(str(detail), status=503)
        self.local_network = True


class CodeArtsAuthError(CodeArtsError):
    pass


class CodeArtsBadRequest(CodeArtsError):
    pass


class CodeArtsNotFound(CodeArtsError):
    pass


class CodeArtsConflict(CodeArtsError):
    pass


class CodeArtsUpstreamError(CodeArtsError):
    """上游 HTTP 200 但 SSE 内嵌业务错误（排队/TPM/模型未注册等）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _positive_float(value, default, low, high):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return min(max(out, float(low)), float(high))


def _positive_int(value, default, low, high):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return int(default)
    return min(max(out, int(low)), int(high))


def _account_priority(value):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return DEFAULT_ACCOUNT_PRIORITY
    return min(max(out, 0), MAX_ACCOUNT_PRIORITY)


def _bounded_int(value, default, low, high):
    """状态文件里的整数值；缺失或坏值用默认值，其余夹回边界。"""
    if value is None or isinstance(value, bool):
        return int(default)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return min(max(number, int(low)), int(high))


def _concurrency_limit(value, default):
    """单账号并发上限夹到允许范围；旧版的 0 表示不限，按允许的最大值处理。"""
    return _bounded_int(value, default, 0, MAX_CONCURRENCY_LIMIT) or MAX_CONCURRENCY_LIMIT


def _strict_number(name, value, low, high, integer=False):
    """保存设置或账号字段时用：超范围直接报错，方便面板把原因回给用户。"""
    if isinstance(value, bool) or value is None:
        raise ValueError("%s must be a number" % name)
    if integer and isinstance(value, float) and not value.is_integer():
        raise ValueError("%s must be a whole number" % name)
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % name)
    if number < low or number > high:
        raise ValueError("%s must be between %g and %g" % (name, low, high))
    return number


def _normalise_models(models):
    """人工禁用的模型名单：小写、去重、去空；非法形状直接报错。"""
    if models is None:
        return []
    if not isinstance(models, (list, tuple, set)):
        raise ValueError("disabledModels must be a list of model ids")
    out = []
    seen = set()
    for item in models:
        if not isinstance(item, str):
            raise ValueError("disabledModels must be a list of model ids")
        mid = item.strip().lower()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def _realm_value(values, realm):
    """某个守卫在渠道上的生效值；还没有按渠道的槽位时退回全局默认值。"""
    if not isinstance(values, dict):
        return 0
    value = values.get(realm)
    if value is None:
        value = values.get("global")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _retry_after_seconds(headers):
    """429 响应里的 Retry-After，只认秒数。"""
    if not headers:
        return None
    raw = headers.get("Retry-After") if hasattr(headers, "get") else None
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else None


def _reset_hint(text):
    """错误正文里的恢复时刻：秒数按「从现在起」解释，长数字按 epoch 解释。"""
    match = re.search(r"(?:reset|retry|recover)[a-z_ -]{0,14}?(\d{4,13})",
                      str(text or "").lower())
    if not match:
        return None
    number = int(match.group(1))
    if number > 10 ** 9:
        return float(number)
    return time.time() + float(number)


def _looks_like_local_network(exc):
    """本机断网与上游故障在账号治理上必须分开，判定与 WorkBuddy 共用一份规则。"""
    import wb_channels
    return wb_channels.looks_like_local_network(exc)


_LEGACY_MIGRATED = set()


def _legacy_migrated_key(accounts_dir):
    return os.path.abspath(str(accounts_dir or "."))


def _take_legacy_max_concurrent(accounts_dir):
    """旧版全局 max_concurrent：第一次读到时搬给各账号，并从设置里删掉。

    全局值只该迁移一次。留在 settings.json 里会让后来新增的账号继续继承一个
    早已删除的开关。
    """
    key = _legacy_migrated_key(accounts_dir)
    with _LOCK:
        if key in _LEGACY_MIGRATED:
            return None
        data = wb_settings.load(accounts_dir)
        section = data.get("codearts")
        if not isinstance(section, dict) or "max_concurrent" not in section:
            _LEGACY_MIGRATED.add(key)
            return None
        legacy = _positive_int(section.pop("max_concurrent"), DEFAULT_CONCURRENCY_LIMIT,
                               MIN_CONCURRENCY_LIMIT, MAX_CONCURRENCY_LIMIT)
        wb_settings.save(accounts_dir, data)
        _LEGACY_MIGRATED.add(key)
        return legacy


def _clean_test_model(value):
    """空串表示用账号自己的默认模型；非法值按空串处理。"""
    model, problem = wb_settings.parse_test_model(value)
    return "" if problem else model


def load_config(accounts_dir):
    """读取并收敛 CodeArts 配置；非法值回落默认，不阻塞网关启动。"""
    raw = wb_settings.load(accounts_dir).get("codearts")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "client_type": normalize_client_type(raw.get("client_type")),
        "stream_timeout_seconds": _positive_float(
            raw.get("stream_timeout_seconds"),
            DEFAULTS["stream_timeout_seconds"], 1.0, 3600.0),
        "models_ttl_seconds": _positive_float(
            raw.get("models_ttl_seconds"), DEFAULTS["models_ttl_seconds"],
            60.0, 86400.0),
        "benefit_auto_claim": raw.get("benefit_auto_claim", True) is not False,
        "chat_encryption": raw.get("chat_encryption", True) is not False,
        "credits_auto_claim": raw.get("credits_auto_claim", False) is True,
        "checkin_max_attempts": _positive_int(
            raw.get("checkin_max_attempts"),
            DEFAULTS["checkin_max_attempts"], 1, 10),
        "checkin_retry_gap_seconds": _positive_float(
            raw.get("checkin_retry_gap_seconds"),
            DEFAULTS["checkin_retry_gap_seconds"], 0.0, 86400.0),
        "checkin_timeout_seconds": _positive_float(
            raw.get("checkin_timeout_seconds"),
            DEFAULTS["checkin_timeout_seconds"], 1.0, 600.0),
        "test_model": _clean_test_model(raw.get("test_model")),
        "accounts_dir": accounts_dir,
    }


def _clean_test_model(value):
    """空串表示用账号自己的默认模型；非法值按空串处理。"""
    model, problem = wb_settings.parse_test_model(value)
    return "" if problem else model


def save_config(accounts_dir, patch):
    """保存配置；返回完整配置。非法值抛 ValueError，不静默夹回边界值。

    整段读-改-写必须在 `_LOCK` 内完成。只靠 `wb_settings.save()` 内部那把锁
    是不够的——它只保护「写」这一步，`load_config()` 读到的旧值到
    `wb_settings.save()` 落盘之间有一个窗口。面板连续点两次保存、或者两个
    浏览器同时保存时，后一次会用「读到旧值 + 自己那一项」覆盖掉前一次刚写
    进去的另一项，且双方都拿到 200，没有任何报错。

    锁序：这里持有 `_LOCK` 时会调用 `wb_settings.load/save`，它们各自持
    `wb_settings._lock`。反向（`wb_settings` 持锁时调 `wb_codearts`）不存在，
    见 `wb_settings.set_codearts_config()` —— 它只做转发，不持锁。所以
    `_LOCK -> wb_settings._lock` 是唯一的获取顺序，不会死锁。
    """
    if not isinstance(patch, dict):
        raise ValueError("codearts settings must be an object")
    accepted = set(DEFAULTS)
    unknown = sorted(key for key in patch if key not in accepted)
    if unknown:
        raise ValueError("unknown codearts setting: %s" % ", ".join(unknown))
    checked = {}
    if "client_type" in patch:
        client_type = str(patch.get("client_type") or "").strip().lower()
        if client_type not in CLIENT_TYPES:
            raise ValueError("client_type must be one of: %s"
                             % ", ".join(CLIENT_TYPES))
        checked["client_type"] = client_type
    if "test_model" in patch:
        model, problem = wb_settings.parse_test_model(patch.get("test_model"))
        if problem:
            raise ValueError(problem)
        checked["test_model"] = model
    for key, (low, high) in CONFIG_BOUNDS.items():
        if key not in patch:
            continue
        integer = isinstance(low, int) and isinstance(high, int)
        checked[key] = _strict_number(key, patch.get(key), low, high,
                                      integer=integer)
    for key in ("benefit_auto_claim", "credits_auto_claim", "chat_encryption"):
        if key not in patch:
            continue
        if not isinstance(patch.get(key), bool):
            raise ValueError("%s must be true or false" % key)
        checked[key] = patch[key]
    with _LOCK:
        current = load_config(accounts_dir)
        merged = dict(current)
        merged.update(checked)
        # 重新读 settings：`load_config` 只返回收敛后的 codearts 段，写回去要
        # 保留其它段（api_keys / realm / 媒体配置…），所以必须拿整份再合并。
        data = wb_settings.load(accounts_dir)
        data["codearts"] = {k: merged[k] for k in DEFAULTS}
        wb_settings.save(accounts_dir, data)
        return load_config(accounts_dir)


def _has_usable_account(pool):
    return any(not acct.disabled for acct in pool.accounts)


def configured(accounts_dir):
    """渠道有可用账号时返回 True；停用全部账号等同于不可用。"""
    try:
        return _has_usable_account(pool_for(accounts_dir))
    except Exception:
        return False


def masked_config(accounts_dir):
    """设置页读的配置视图；只读设置与账号计数，不触发任何后台任务。"""
    cfg = load_config(accounts_dir)
    pool = pool_for(accounts_dir)
    out = {k: v for k, v in cfg.items() if k != "accounts_dir"}
    out["configured"] = _has_usable_account(pool)
    out["accounts"] = len(pool.accounts) if pool else 0
    out["healthy"] = pool.stats()["healthy"] if pool else 0
    return out


# ---------------------------------------------------------------------------
# 账号文件与状态
# ---------------------------------------------------------------------------

def accounts_path(accounts_dir):
    return os.path.join(accounts_dir, "codearts")


def _auth_filename(uid):
    safe = str(uid or "").replace(os.sep, "_").replace("/", "_")
    return "codearts-%s.json" % (safe or "unknown")


def _atomic_write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 临时文件名带进程与线程标识：多个账号同时失败会并发写 state.json，
    # 共用同一个临时文件会在 os.replace() 上互相踩掉。
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_auths(accounts_dir):
    """读取 accounts/codearts/codearts-*.json。"""
    folder = accounts_path(accounts_dir)
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if not name.startswith("codearts-") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("cloud_dragon_token") or not data.get("user_id"):
            continue
        data["_path"] = path
        out.append(data)
    return out


def save_auth(accounts_dir, auth):
    path = auth.get("_path") or os.path.join(
        accounts_path(accounts_dir), _auth_filename(auth.get("user_id")))
    auth = dict(auth)
    auth.pop("_path", None)
    _atomic_write(path, auth)
    auth["_path"] = path
    return auth


def delete_auth(accounts_dir, uid):
    path = os.path.join(accounts_path(accounts_dir), _auth_filename(uid))
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def _parse_expiration(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    text = text.replace("Z", "+00:00")
    try:
        import datetime
        return datetime.datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def _fmt_duration(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, _ = divmod(rest, 60)
    if hours:
        return "%dh%dm" % (hours, minutes)
    return "%dm" % minutes


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

class CodeArtsAccount(object):
    """一个华为云 CodeArts 账号；也是 wb-hub 回程处理器需要的账号表面。"""

    uid = ""
    realm = "codearts"
    product = "codearts"
    provider = "codearts"
    proxy = None
    chat_path = EP_CHAT

    def __init__(self, auth, pool):
        self.auth = dict(auth)
        self.pool = pool
        self.uid = str(auth.get("user_id") or "")
        self.name = self.uid
        self.nickname = str(auth.get("user_name") or self.uid)
        # proxy_slot is the persisted binding; proxy is the runtime URL that
        # CodeArtsPool.apply_proxy_slots() derives from the current settings.
        self.proxy_slot = str(auth.get("proxySlot") or "").strip()
        self.proxy = ""
        self.disabled = False
        self.disabled_reason = ""
        self.cool_until = 0.0
        self.err_count = 0
        self.last_error = ""
        self.last_error_detail = ""
        self.active_concurrent = 0
        # 单账号所有模型合计的在途请求上限。
        self.concurrency_limit = _concurrency_limit(
            None, getattr(pool, "inherited_concurrency_limit", DEFAULT_CONCURRENCY_LIMIT))
        self.priority = _account_priority(auth.get("priority"))
        self.weight = 1
        self.group = ""
        self.disabled_models = set()
        self.note = ""
        self.tasks_enabled = True
        self.last_used = 0.0
        self.models = []
        self.model_benefit = {}
        self.models_fetched_at = 0.0
        self.checkin = None
        # 今日签到尝试：{"day": 本地日, "count": 次数, "at": 最近一次时刻}，
        # 用来落实 checkin_max_attempts 与 checkin_retry_gap_seconds。
        self.checkin_attempts = None
        self.info = None
        self.info_fetched_at = 0.0
        # 今日积分消耗的基线：当天第一份余额快照里的 used，落盘后跨重启仍然有效。
        self.credit_day = None
        self.model_cooldowns = {}
        self.model_cooldowns_at = {}
        # 模型限流阶梯的连击计数与最近一次撞线时刻。
        self.unscoped_streak = 0
        self.last_soft_at = 0.0
        self.reserve_credits = 0
        self.daily_token_limit = 0
        self.daily_tokens_today = None
        self.daily_credit_limit = 0
        self.model_daily_token_limit = 0
        self.model_daily_tokens = None
        self.expiring_window_days = 0
        self.credit_rate_per_day = 0.0
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._models_lock = threading.Lock()
        self._info_lock = threading.Lock()
        self._pick_lock = threading.Lock()
        self._pick_stamps = collections.deque()

    def chat_base_url(self):
        return SNAP_HOST

    # ---- 派单条件 ----
    def model_disabled(self, mid):
        """这个账号被人工禁用了该模型？空名单恒为 False。"""
        if not mid or not self.disabled_models:
            return False
        return str(mid).strip().lower() in self.disabled_models

    def is_cli_identity(self):
        return _account_profile(self)["client_type"] == "CLI"

    def effective_client_type(self):
        """账号显式渠道优先；旧账号跟随 CodeArts 全局设置。"""
        return normalize_client_type(
            self.auth.get("client_type")
            or (self.pool.config or {}).get("client_type"))

    # ---- 积分与限额 ----
    def credits_view(self):
        """统一账号视图里的 credits：WorkBuddy 的 size 对应套餐总额。"""
        info = self.info or {}
        total = ((info.get("credits") or {}).get("total") or {})
        if not total:
            return None
        return {
            "remain": float(total.get("remain") or 0),
            "size": float(total.get("amount") or 0),
            "used": float(total.get("used") or 0),
            "expiring": float(total.get("expiring") or 0),
            "updated_at": int(self.info_fetched_at or 0),
            "package": str(info.get("package_name") or ""),
            "unit": "credit",
        }

    @property
    def credits(self):
        """余额快照；与 WorkBuddy 的账号属性同名，供共用的流水与富集代码读取。"""
        return self.credits_view()

    def credit_expiries(self, now=None):
        """积分到期列表 [{at, amount, name}]，按到期时刻升序。

        官方客户端只读 `statistics/plugin` 的聚合值，上游没有逐笔到期接口，这里按各类积分的
        有效期规则拆开：
        - 套餐积分（usageBasicPackageCredit）按月发放、周期结束清零，`end_date` 是官方客户端
          显示的额度重置日，按北京时间当天零点计；
        - 赠送与按需积分里的 `package_credit_expiring_amount` 是当天签到积分的剩余：签到积分
          当日有效且优先消耗，实测这个值等于 100 减去签到后的用量，按北京时间当天结束时清零；
        - 其余赠送积分（例如注册赠送）上游不给到期日，不进入列表。
        """
        info = self.info or {}
        credits = info.get("credits") or {}
        now = time.time() if now is None else now
        rows = []
        basic = float((credits.get("basic") or {}).get("remain") or 0)
        end_date = str(info.get("end_date") or "").strip()
        if end_date and basic > 0:
            reset = datetime.datetime.combine(datetime.date.fromisoformat(end_date[:10]),
                                              datetime.time(), CREDIT_TZ)
            rows.append({"at": int(reset.timestamp()), "amount": round(basic, 2), "name": "套餐积分"})
        expiring = sum(float((credits.get(kind) or {}).get("expiring") or 0)
                       for kind in ("bonus", "on_demand"))
        if expiring > 0:
            today = datetime.datetime.fromtimestamp(now, CREDIT_TZ).date()
            day_end = datetime.datetime.combine(today + datetime.timedelta(days=1),
                                                datetime.time(), CREDIT_TZ)
            rows.append({"at": int(day_end.timestamp()) - 1, "amount": round(expiring, 2),
                         "name": "当日到期积分"})
        rows.sort(key=lambda row: row["at"])
        return rows

    def soonest_expiring_days(self, now=None):
        now = time.time() if now is None else now
        days = None
        for row in self.credit_expiries(now):
            if row["at"] <= now:
                continue
            left = (row["at"] - now) / 86400.0
            if days is None or left < days:
                days = left
        return days

    def _expiring_rows(self, within_days, now=None):
        try:
            window = float(within_days or 0)
        except (TypeError, ValueError):
            return []
        if window <= 0:
            return []
        now = time.time() if now is None else now
        rows = []
        for row in self.credit_expiries(now):
            days = (row["at"] - now) / 86400.0
            if 0 <= days <= window:
                rows.append((days, float(row["amount"])))
        rows.sort()
        return rows

    def expiring_rate_per_day(self, within_days, now=None):
        """窗口内临期积分每天至少要消耗掉多少；余额分几笔到期时按各自梯度累计。"""
        best = 0.0
        cumulative = 0.0
        for days, amount in self._expiring_rows(within_days, now=now):
            cumulative += amount
            best = max(best, cumulative / max(days, 1.0 / 24.0))
        return best

    def in_expiring_window(self, now=None):
        try:
            window = int(self.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        if window <= 0:
            return False
        soonest = self.soonest_expiring_days(now=now)
        return soonest is not None and soonest <= window

    def credit_target_per_day(self, now=None):
        """到期前每天至少要消耗掉的积分；没有余额快照时返回 None。"""
        info = self.info if isinstance(self.info, dict) else {}
        if not ((info.get("credits") or {}).get("total")):
            return None
        now = time.time() if now is None else now
        best = 0.0
        cumulative = 0.0
        for row in self.credit_expiries():
            if row["at"] <= now:
                continue
            cumulative += float(row["amount"])
            days = max((row["at"] - now) / 86400.0, 1.0 / 24.0)
            best = max(best, cumulative / days)
        return best

    def reserve_blocked(self):
        """余额不高于保留线时停机；只有已知余额才会拦截。"""
        reserve = int(self.reserve_credits or 0)
        if reserve <= 0:
            return False
        credits = self.credits_view()
        if not credits:
            return False
        return float(credits["remain"]) <= reserve

    def model_is_free(self, model):
        return bool(model) and self.is_benefit(model)

    def daily_limit_blocked(self):
        try:
            limit = int(self.daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0 or self.daily_tokens_today is None:
            return False
        try:
            return int(self.daily_tokens_today) >= limit
        except (TypeError, ValueError):
            return False

    def model_token_limit_blocked(self, model=None):
        try:
            limit = int(self.model_daily_token_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0 or not model or not isinstance(self.model_daily_tokens, dict):
            return False
        key = str(model).strip().lower()
        for mid, used in self.model_daily_tokens.items():
            if str(mid).strip().lower() != key:
                continue
            try:
                return int(used) >= limit
            except (TypeError, ValueError):
                return False
        return False

    def _daily_credit_growth(self, now=None):
        """今日积分消耗：当前套餐 used 减去当天基线；基线未建立时返回 0。"""
        info = self.info if isinstance(self.info, dict) else {}
        used = ((info.get("credits") or {}).get("total") or {}).get("used")
        if used is None:
            return None
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        baseline = self.credit_day if isinstance(self.credit_day, dict) else None
        if not baseline or baseline.get("day") != today:
            return 0.0
        try:
            return max(0.0, float(used) - float(baseline.get("used") or 0))
        except (TypeError, ValueError):
            return None

    @property
    def daily_credits_today(self):
        return self._daily_credit_growth()

    def record_credit_baseline(self, now=None):
        """把今天第一份余额快照记为基线；跨天后重新建立。"""
        info = self.info if isinstance(self.info, dict) else {}
        used = ((info.get("credits") or {}).get("total") or {}).get("used")
        if used is None:
            return False
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        baseline = self.credit_day if isinstance(self.credit_day, dict) else None
        if baseline and baseline.get("day") == today:
            return False
        self.credit_day = {"day": today, "used": float(used)}
        return True

    def credit_limit_reached(self):
        try:
            limit = int(self.daily_credit_limit or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return False
        used = self.daily_credits_today
        if used is None:
            return False
        return float(used) >= limit

    def credit_limit_blocked(self, model=None):
        """达到每日积分限额后免费（福利）模型照常服务。"""
        if not model or self.model_is_free(model):
            return False
        return self.credit_limit_reached()

    def wait_seconds(self, model="", now=None):
        """这个账号还要等多久才能服务 `model`（0 = 现在就能）。"""
        now = time.time() if now is None else now
        wait = max(0.0, self.cool_until - now)
        if model:
            key = str(model).strip().lower()
            wait = max(wait, max(0.0, self.model_cooldowns.get(key, 0.0) - now))
        return wait

    def blocked_reason(self, model="", now=None):
        """此刻为什么不能接单；可用时返回空字符串。"""
        now = time.time() if now is None else now
        if self.disabled:
            return "已停用"
        if now < self.cool_until:
            return "冷却中（%d 秒后恢复）" % int(self.cool_until - now + 0.999)
        if model:
            if self.model_disabled(model):
                return "该模型已被人工禁用"
            key = str(model).strip().lower()
            until = self.model_cooldowns.get(key, 0.0)
            if until > now:
                return "该模型冷却中（%d 秒后恢复）" % int(until - now + 0.999)
        if self.reserve_blocked():
            return "余额低于保留线"
        if self.daily_limit_blocked():
            return "今日 Token 额度用尽"
        if self.credit_limit_blocked(model):
            return "今日积分额度用尽"
        if self.model_token_limit_blocked(model):
            return "该模型今日额度用尽"
        return ""

    def has_capacity(self):
        limit = int(self.concurrency_limit or 0)
        return limit <= 0 or self.active_concurrent < limit

    def mark_pick(self, now=None):
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            stamps.append(now)
            while stamps and now - stamps[0] > wb_accounts.LOAD_WINDOW_SECONDS:
                stamps.popleft()

    def recent_load(self, now=None):
        now = time.time() if now is None else now
        with self._pick_lock:
            stamps = self._pick_stamps
            while stamps and now - stamps[0] > wb_accounts.LOAD_WINDOW_SECONDS:
                stamps.popleft()
            return len(stamps)

    # ---- 惩罚 ----
    def note_success(self, model=""):
        """一次成功请求清掉账号级惩罚与本次模型的冷却。"""
        key = str(model or "").strip().lower()
        if key:
            self.model_cooldowns.pop(key, None)
            self.model_cooldowns_at.pop(key, None)
        self.cool_until = 0.0
        self.err_count = 0
        self.last_error = ""
        self.last_error_detail = ""
        self.unscoped_streak = 0
        self.last_soft_at = 0.0

    def note_failure(self, now=None):
        """传输失败与未知错误：累计到阈值后整账号冷却。"""
        now = time.time() if now is None else now
        self.err_count += 1
        if self.err_count >= ERR_THRESHOLD:
            self.cool_until = max(self.cool_until, now + ERR_COOLDOWN)
            self.err_count = 0
        return self.cool_until

    def note_cooldown(self, seconds, now=None):
        """固定时长的账号冷却；已有更长的冷却时保持更长的那个。"""
        now = time.time() if now is None else now
        self.cool_until = max(self.cool_until, now + max(0.0, float(seconds)))
        return self.cool_until

    def note_model_rate(self, model, wait=None, reset_at=None, now=None):
        """按模型的限流：优先用上游给的恢复时刻，否则 60 秒起步逐次翻倍。

        同一个模型的冷却时刻只延长不缩短：上游两次 429 之间可能夹着一个更长的
        窗口，用新的短值覆盖它等于提前放行。阶梯只在连续限流时升级，隔一天再撞
        一次从第一档重新开始。
        """
        key = str(model or "").strip().lower()
        if not key:
            return 0.0
        now = time.time() if now is None else now
        if reset_at:
            until = float(reset_at)
        else:
            if (wb_accounts.FAIL_WINDOW and self.last_soft_at
                    and now - self.last_soft_at > wb_accounts.FAIL_WINDOW):
                self.unscoped_streak = 0
            self.last_soft_at = now
            self.unscoped_streak += 1
            until = now + wb_accounts.unverified_soft_backoff(self.unscoped_streak)
        if wait:
            until = max(until, now + max(0.0, float(wait)))
        self.model_cooldowns[key] = max(self.model_cooldowns.get(key, 0.0), until)
        self.model_cooldowns_at.setdefault(key, now)
        return self.model_cooldowns[key]

    def clear_penalties(self):
        """清掉全部本地惩罚：冷却、错误计数、模型冷却。"""
        self.cool_until = 0.0
        self.err_count = 0
        self.last_error = ""
        self.last_error_detail = ""
        self.model_cooldowns.clear()
        self.model_cooldowns_at.clear()
        self.unscoped_streak = 0
        self.last_soft_at = 0.0

    def model_cooldowns_view(self, now=None):
        now = time.time() if now is None else now
        rows = []
        for model, until in self.model_cooldowns.items():
            if until <= now:
                continue
            row = {"model": model, "expiresAt": int(until)}
            started = self.model_cooldowns_at.get(model)
            if started:
                row["startedAt"] = int(started)
            rows.append(row)
        rows.sort(key=lambda item: (item["expiresAt"], item["model"]))
        return rows

    def last_checkin_view(self):
        entry = self.checkin if isinstance(self.checkin, dict) else None
        if not entry:
            return None
        out = {
            "at": int(_parse_expiration(entry.get("checked_at"))),
            "ok": bool(entry.get("ok")),
            "message": str(entry.get("message") or ""),
        }
        if isinstance(entry.get("credits"), (int, float)):
            out["credits"] = float(entry["credits"])
        return out

    # ---- 凭证 ----
    def credential(self):
        return {
            "access_key_id": self.auth.get("access_key_id") or "",
            "secret_access_key": self.auth.get("secret_access_key") or "",
            "security_token": self.auth.get("cloud_dragon_token") or "",
        }

    def expires_at(self):
        return _parse_expiration(self.auth.get("expiration"))

    def token_remaining(self):
        exp = self.expires_at()
        if not exp:
            return 0
        return max(0, exp - time.time())

    def expired(self):
        exp = self.expires_at()
        return bool(exp and exp <= time.time())

    def expiring_soon(self, skew=REFRESH_SKEW):
        exp = self.expires_at()
        return bool(exp and exp - time.time() <= skew)

    def update_auth(self, token):
        creds = token.get("credentials") or {}
        self.auth.update({
            "user_id": token.get("user_id") or self.uid,
            "user_name": token.get("user_name") or self.nickname,
            "domain_id": token.get("domain_id") or self.auth.get("domain_id") or "",
            "cloud_dragon_token": creds.get("security_token") or "",
            "access_key_id": creds.get("access_key_id") or "",
            "secret_access_key": creds.get("secret_access_key") or "",
            "expiration": creds.get("expiration") or "",
            "refresh_token": token.get("refresh_token") or self.auth.get("refresh_token") or "",
            "updated_at": int(time.time()),
        })
        self.auth = save_auth(self.pool.accounts_dir, self.auth)
        # 换到新令牌只说明凭据可用：账号的启用状态由操作者决定，不在这里恢复。
        self.last_error = ""

    # ---- 状态快照 ----
    def view(self):
        """统一账号视图：两个提供方的键完全一致，面板只认这一份。"""
        now = time.time()
        cooling = (not self.disabled) and now < self.cool_until
        exp = self.expires_at()
        models = [str(item.get("id") or "") for item in (self.models or [])
                  if item.get("id")] or None
        free = [str(item.get("id") or "") for item in (self.models or [])
                if item.get("id") and item.get("benefit")]
        daily_credits = self.daily_credits_today
        token_remaining = self.token_remaining()
        return {
            "uid": self.uid,
            "nickname": self.nickname or self.uid,
            "realm": self.realm,
            "provider": "codearts",
            "platform": "CodeArts",
            "identity": self.effective_client_type(),
            "identityOptions": [dict(item) for item in IDENTITY_OPTIONS],
            "enabled": not self.disabled,
            "tasksEnabled": bool(self.tasks_enabled),
            "group": self.group,
            "priority": int(self.priority),
            "weight": int(self.weight),
            "concurrencyLimit": int(self.concurrency_limit),
            "concurrencyMin": MIN_CONCURRENCY_LIMIT,
            "concurrencyMax": MAX_CONCURRENCY_LIMIT,
            "activeRequests": int(self.active_concurrent),
            "note": self.note,
            "disabledModels": sorted(self.disabled_models),
            "proxySlot": self.proxy_slot,
            "proxy": self.proxy,
            "expiresAt": (exp if exp else None),
            "issuedAt": int(self.auth.get("updated_at") or 0),
            "expiresIn": (_fmt_duration(exp - now) if exp else None),
            "hasRefreshToken": bool(self.auth.get("refresh_token")),
            "lastError": self.disabled_reason or self.last_error,
            "lastErrorDetail": self.last_error_detail or None,
            "inCooldown": cooling,
            "cooldownFor": (max(0, int(round(self.cool_until - now))) or None
                            if cooling else None),
            "modelCooldowns": self.model_cooldowns_view(now),
            "fails": int(self.err_count),
            "addedAt": int(self.auth.get("added_at")
                           or self.auth.get("updated_at") or 0),
            "file": (os.path.basename(self.auth["_path"])
                     if self.auth.get("_path") else None),
            "credits": self.credits_view(),
            "creditExpiries": self.credit_expiries(),
            "reserveCredits": int(self.reserve_credits or 0),
            "reserveBlocked": self.reserve_blocked(),
            "dailyTokenLimit": int(self.daily_token_limit or 0),
            "dailyTokensToday": (int(self.daily_tokens_today)
                                 if isinstance(self.daily_tokens_today, int)
                                 else None),
            "dailyLimitBlocked": self.daily_limit_blocked(),
            "dailyCreditLimit": int(self.daily_credit_limit or 0),
            "dailyCreditsToday": (round(float(daily_credits), 2)
                                  if isinstance(daily_credits, (int, float))
                                  else None),
            "creditLimitReached": self.credit_limit_reached(),
            "modelDailyTokenLimit": int(self.model_daily_token_limit or 0),
            "modelDailyTokens": ({str(k): int(v)
                                  for k, v in self.model_daily_tokens.items()}
                                 if isinstance(self.model_daily_tokens, dict)
                                 else None),
            "lastCheckin": self.last_checkin_view(),
            "canCheckin": True,
            "canDailyChat": False,
            "models": models,
            "freeModels": free,
            # 面板旧字段仍在用，值取自统一字段，读起来一致。
            "name": self.name,
            "user_name": self.nickname,
            "disabled": self.disabled,
            "cooling": cooling,
            "until": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.localtime(self.cool_until))
                      if cooling else ""),
            "err_count": self.err_count,
            "reason": self.disabled_reason or self.last_error,
            "last_error": self.last_error,
            "active_concurrent": self.active_concurrent,
            "max_concurrent": self.concurrency_limit,
            "token_remaining": _fmt_duration(token_remaining),
            "expires_at": (time.strftime("%Y-%m-%dT%H:%M:%S",
                                         time.localtime(exp)) if exp else ""),
            "proxy_slot": self.proxy_slot,
            "client_type": self.effective_client_type(),
            "tasks_enabled": bool(self.tasks_enabled),
            "checkin": self.checkin,
            "package": ((self.info or {}).get("package_name") or ""),
        }

    def healthy(self, now=None, model=""):
        """能否被选中：启用、不停在冷却与各项限额内。

        故意不检查 token 是否过期：过期账号仍要被选中，交给 `_ensure_token`
        刷新；把过期号直接过滤掉会让整池过期时一个号都选不到、也无法续期。
        并发名额由 `has_capacity()` 单独判断，这里只看账号自身状态。
        """
        now = time.time() if now is None else now
        if self.disabled or now < self.cool_until:
            return False
        if model:
            if self.model_disabled(model):
                return False
            key = str(model).strip().lower()
            if self.model_cooldowns.get(key, 0.0) > now:
                return False
            if self.model_token_limit_blocked(model):
                return False
        if self.reserve_blocked() or self.daily_limit_blocked():
            return False
        if self.credit_limit_blocked(model):
            return False
        return True

    def checked_in_today(self, now=None):
        """Whether this account already completed a successful check-in today.

        A failed attempt (``ok`` false) does not count, so a later slot can
        retry after an earlier failure. The timestamp is local time, matching
        the scheduler's clock.
        """
        entry = self.checkin if isinstance(self.checkin, dict) else {}
        if not entry.get("ok"):
            return False
        text = str(entry.get("checked_at") or "")
        if len(text) < 10:
            return False
        today = time.strftime("%Y-%m-%d",
                              time.localtime(now if now is not None else time.time()))
        return text[:10] == today

    def _checkin_attempt_entry(self, now=None):
        """今天已尝试签到的次数与最近一次时刻；跨天从零开始。"""
        now = time.time() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        entry = self.checkin_attempts if isinstance(self.checkin_attempts, dict) else {}
        if entry.get("day") != today:
            return {"day": today, "count": 0, "at": 0.0}
        try:
            count = max(0, int(entry.get("count") or 0))
        except (TypeError, ValueError):
            count = 0
        try:
            at = float(entry.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        return {"day": today, "count": count, "at": at}

    def note_checkin_attempt(self, now=None):
        """记一次签到尝试（成功与否都算），供次数与重试间隔判断。"""
        now = time.time() if now is None else now
        entry = self._checkin_attempt_entry(now)
        self.checkin_attempts = {"day": entry["day"],
                                 "count": entry["count"] + 1, "at": now}
        return self.checkin_attempts

    def checkin_attempt_blocked(self, max_attempts=1, gap_seconds=0, now=None):
        """今天的尝试次数或重试间隔还挡着这个账号时返回 True。"""
        now = time.time() if now is None else now
        entry = self._checkin_attempt_entry(now)
        try:
            limit = int(max_attempts or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0 and entry["count"] >= limit:
            return True
        try:
            gap = float(gap_seconds or 0)
        except (TypeError, ValueError):
            gap = 0.0
        return bool(gap > 0 and entry["at"] and now - entry["at"] < gap)

    def has_model(self, model):
        mid = str(model or "").lower()
        if not mid:
            return True
        if not self.models:
            return True  # 目录还没发现，先给它一次机会
        return any(str(m.get("id") or "").lower() == mid for m in self.models)

    def is_benefit(self, model):
        return bool(self.model_benefit.get(str(model or "").lower()))


# ---------------------------------------------------------------------------
# 账号池
# ---------------------------------------------------------------------------

class CodeArtsPool(object):
    def __init__(self, accounts_dir, config):
        self.accounts_dir = accounts_dir
        self.folder = accounts_path(accounts_dir)
        self.state_file = os.path.join(self.folder, "state.json")
        self.config = dict(config)
        self.accounts = []
        # 旧版全局并发上限只读一次：搬给还没有单账号上限的账号，随后从设置里删除。
        self.inherited_concurrency_limit = (
            _take_legacy_max_concurrent(accounts_dir)
            or DEFAULT_CONCURRENCY_LIMIT)
        self.affinity = wb_accounts.SessionAffinity()
        self._cursor = 0
        self._manual_weights = {}
        self._expiry_weights = {}
        self._lock = threading.RLock()
        # 状态落盘串行化：多个账号同时失败会并发保存，后写的快照不能缺前一次
        # 刚记下的冷却。
        self._state_lock = threading.Lock()
        self.log = lambda message: None
        self.load()

    # ---- 持久化 ----
    def load(self):
        auths = load_auths(self.accounts_dir)
        state = self._load_state()
        accounts = []
        migrated = False
        for auth in auths:
            acct = CodeArtsAccount(auth, self)
            entry = state.get(acct.uid) or {}
            acct.err_count = int(entry.get("err_count") or 0)
            acct.cool_until = float(entry.get("cool_until") or 0)
            acct.disabled = bool(entry.get("disabled"))
            acct.disabled_reason = str(entry.get("reason") or "")
            acct.last_error = str(entry.get("last_error") or "")
            acct.last_error_detail = str(entry.get("last_error_detail") or "")
            acct.note = str(entry.get("note") or "")[:wb_accounts.MAX_ACCOUNT_NOTE]
            acct.tasks_enabled = bool(entry.get("tasks_enabled", True))
            acct.group = str(entry.get("group") or "")[:wb_accounts.MAX_ACCOUNT_GROUP]
            acct.weight = _bounded_int(entry.get("weight"), 1, 1,
                                       wb_accounts.MAX_ACCOUNT_WEIGHT)
            if "concurrency_limit" not in entry:
                migrated = True
            acct.concurrency_limit = _concurrency_limit(
                entry.get("concurrency_limit"), self.inherited_concurrency_limit)
            try:
                acct.disabled_models = set(
                    _normalise_models(entry.get("disabled_models")))
            except ValueError:
                acct.disabled_models = set()
            acct.unscoped_streak = _bounded_int(
                entry.get("unscoped_streak"), 0, 0, 10 ** 6)
            try:
                acct.last_soft_at = float(entry.get("last_soft_at") or 0)
            except (TypeError, ValueError):
                acct.last_soft_at = 0.0
            cooled = entry.get("model_cooldowns")
            if isinstance(cooled, dict):
                for mid, until in cooled.items():
                    try:
                        acct.model_cooldowns[str(mid)] = float(until)
                    except (TypeError, ValueError):
                        continue
            started = entry.get("model_cooldowns_at")
            if isinstance(started, dict):
                for mid, at in started.items():
                    try:
                        acct.model_cooldowns_at[str(mid)] = float(at)
                    except (TypeError, ValueError):
                        continue
            if isinstance(entry.get("credit_day"), dict):
                acct.credit_day = entry["credit_day"]
            if isinstance(entry.get("checkin_attempts"), dict):
                acct.checkin_attempts = entry["checkin_attempts"]
            if isinstance(entry.get("checkin"), dict):
                acct.checkin = entry["checkin"]
            accounts.append(acct)
        with self._lock:
            self.accounts = accounts
        self.apply_proxy_slots()
        if migrated:
            self.save_state()

    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            return {}
        if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
            raw = raw["accounts"]
        out = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("name"):
                    out[str(item["name"])] = item
        return out

    def save_state(self):
        """串行落盘；同一个池的并发保存不会互相覆盖刚记下的冷却。"""
        with self._state_lock:
            self._write_state()

    def _write_state(self):
        items = []
        with self._lock:
            accounts = list(self.accounts)
        for acct in accounts:
            items.append({
                "name": acct.uid,
                "err_count": acct.err_count,
                "cool_until": acct.cool_until,
                "disabled": acct.disabled,
                "reason": acct.disabled_reason,
                "last_error": acct.last_error,
                "last_error_detail": acct.last_error_detail,
                "checkin": acct.checkin,
                "note": acct.note,
                "tasks_enabled": bool(acct.tasks_enabled),
                "group": acct.group,
                "weight": int(acct.weight),
                "concurrency_limit": int(acct.concurrency_limit),
                "disabled_models": sorted(acct.disabled_models),
                "unscoped_streak": int(acct.unscoped_streak),
                "last_soft_at": float(acct.last_soft_at or 0),
                "model_cooldowns": dict(acct.model_cooldowns),
                "model_cooldowns_at": dict(acct.model_cooldowns_at),
                "credit_day": acct.credit_day,
                "checkin_attempts": acct.checkin_attempts,
            })
        try:
            _atomic_write(self.state_file, items)
        except Exception:
            pass

    # ---- 查询 ----
    def get(self, uid):
        with self._lock:
            for acct in self.accounts:
                if acct.uid == uid or acct.name == uid:
                    return acct
        return None

    def apply_proxy_slots(self, slots=None):
        """Re-resolve every account's runtime `proxy` from its bound slot.

        Mirrors wb_accounts.AccountPool.apply_proxy_slots(): `proxySlot` is the
        persisted source of truth and `proxy` is derived here, so every outbound
        CodeArts call site can just use `acct.proxy`. A disabled or missing slot
        falls back to direct, while the binding stays on disk for re-enabling.
        """
        if slots is None:
            slots = wb_settings.proxy_slots(self.accounts_dir)
        by_id = {entry["id"]: entry for entry in slots if entry.get("enabled")}
        with self._lock:
            for acct in self.accounts:
                slot = by_id.get(acct.proxy_slot)
                acct.proxy = slot["url"] if slot else ""

    def stats(self):
        now = time.time()
        total = healthy = disabled = cooling = 0
        with self._lock:
            accounts = list(self.accounts)
        for acct in accounts:
            total += 1
            if acct.disabled:
                disabled += 1
            elif now < acct.cool_until:
                cooling += 1
            else:
                healthy += 1
        return {"total": total, "healthy": healthy, "disabled": disabled,
                "cooling": cooling}

    # ---- 增删启停 ----
    def add(self, auth):
        acct = CodeArtsAccount(auth, self)
        if not acct.auth.get("added_at"):
            # 首次见到这份凭据时记下加入时间；之后刷新令牌不再改动它。
            acct.auth["added_at"] = int(time.time())
            acct.auth = save_auth(self.accounts_dir, acct.auth)
        added = acct
        with self._lock:
            for index, existing in enumerate(self.accounts):
                if existing.uid == acct.uid:
                    # Re-login returns a fresh auth document without the
                    # panel-side binding. Keep the slot the operator chose.
                    if not acct.proxy_slot and existing.proxy_slot:
                        acct.proxy_slot = existing.proxy_slot
                    if acct.proxy_slot:
                        acct.auth["proxySlot"] = acct.proxy_slot
                    # _save_login() wrote the fresh token document before the
                    # pool saw it; persist the preserved binding here too.
                    acct.auth = save_auth(self.accounts_dir, acct.auth)
                    existing.auth = acct.auth
                    existing.proxy_slot = acct.proxy_slot
                    existing.nickname = acct.nickname
                    existing.disabled = False
                    existing.disabled_reason = ""
                    existing.last_error = ""
                    existing.err_count = 0
                    existing.cool_until = 0
                    existing.models_fetched_at = 0
                    self.accounts[index] = existing
                    added = existing
                    break
            else:
                self.accounts.append(acct)
        # 落盘与出口解析放在池锁之外：save_state 自己有一把状态锁，持池锁再取它
        # 会与「先取状态锁再读账号」的其它路径形成反向顺序。
        self.save_state()
        self.apply_proxy_slots()
        return added

    def remove(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        with self._lock:
            self.accounts = [a for a in self.accounts if a.uid != uid]
        delete_auth(self.accounts_dir, uid)
        self.save_state()
        return True

    def enable(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.disabled = False
        acct.disabled_reason = ""
        acct.clear_penalties()
        self.save_state()
        return True

    def disable(self, uid, reason="manual disable"):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.disabled = True
        acct.disabled_reason = reason
        acct.last_error = reason
        self.save_state()
        return True

    def clear_cooldown(self, uid):
        acct = self.get(uid)
        if acct is None:
            return False
        self.note_success(acct, "")
        return True

    def clear_penalties(self, acct):
        """一次成功的人工测试后清掉这个账号的全部本地惩罚。"""
        with acct._lock:
            acct.clear_penalties()
        self.save_state()

    def set_note(self, uid, note):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.note = str(note or "").strip()[:wb_accounts.MAX_ACCOUNT_NOTE]
        self.save_state()
        return True

    def set_tasks_enabled(self, uid, enabled):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.tasks_enabled = bool(enabled)
        self.save_state()
        return True

    def set_priority(self, uid, priority):
        acct = self.get(uid)
        if acct is None:
            return None
        acct.priority = _account_priority(priority)
        acct.auth["priority"] = acct.priority
        acct.auth = save_auth(self.accounts_dir, acct.auth)
        return acct

    def set_client_type(self, uid, client_type):
        acct = self.get(uid)
        if acct is None:
            return None
        acct.auth["client_type"] = normalize_client_type(client_type)
        acct.auth = save_auth(self.accounts_dir, acct.auth)
        return acct

    def set_group(self, uid, group):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.group = str(group or "").strip()[:wb_accounts.MAX_ACCOUNT_GROUP]
        self.save_state()
        return True

    def set_weight(self, uid, weight):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.weight = _bounded_int(weight, 1, 1, wb_accounts.MAX_ACCOUNT_WEIGHT)
        self.save_state()
        return True

    def set_concurrency_limit(self, uid, limit):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.concurrency_limit = _concurrency_limit(limit, DEFAULT_CONCURRENCY_LIMIT)
        self.save_state()
        return True

    def set_disabled_models(self, uid, models):
        acct = self.get(uid)
        if acct is None:
            return False
        acct.disabled_models = set(_normalise_models(models))
        self.save_state()
        return True

    def update(self, uid, changes):
        """应用一组合法字段；校验由 `update_account()` 完成。"""
        acct = self.get(uid)
        if acct is None:
            return None
        if "enabled" in changes:
            if changes["enabled"]:
                acct.disabled = False
                acct.disabled_reason = ""
                acct.clear_penalties()
            else:
                acct.disabled = True
                acct.disabled_reason = acct.disabled_reason or "manual disable"
            # 与 WorkBuddy 相同：只给 enabled 时任务参与一起跟随；显式 tasksEnabled 优先。
            if "tasksEnabled" not in changes:
                acct.tasks_enabled = bool(changes["enabled"])
        if "tasksEnabled" in changes:
            acct.tasks_enabled = bool(changes["tasksEnabled"])
        if "priority" in changes:
            acct.priority = int(changes["priority"])
            acct.auth["priority"] = acct.priority
            acct.auth = save_auth(self.accounts_dir, acct.auth)
        if "weight" in changes:
            acct.weight = int(changes["weight"])
        if "concurrencyLimit" in changes:
            acct.concurrency_limit = int(changes["concurrencyLimit"])
        if "group" in changes:
            acct.group = str(changes["group"] or "")[:wb_accounts.MAX_ACCOUNT_GROUP]
        if "note" in changes:
            acct.note = str(changes["note"] or "")[:wb_accounts.MAX_ACCOUNT_NOTE]
        if "disabledModels" in changes:
            acct.disabled_models = set(changes["disabledModels"])
        if "proxySlot" in changes:
            acct.proxy_slot = str(changes["proxySlot"] or "")
            acct.auth["proxySlot"] = acct.proxy_slot
            acct.auth = save_auth(self.accounts_dir, acct.auth)
        if "identity" in changes:
            acct.auth["client_type"] = normalize_client_type(changes["identity"])
            acct.auth = save_auth(self.accounts_dir, acct.auth)
        self.save_state()
        if "proxySlot" in changes:
            self.apply_proxy_slots()
        return acct

    def set_all_enabled(self, enabled, realm=None, group=None):
        """整组或整池启停，返回真正变了的 uid；realm 不是本渠道时什么也不做。"""
        if realm and realm not in ("all", "codearts"):
            return []
        group = str(group or "").strip()
        changed = []
        with self._lock:
            for acct in self.accounts:
                if group and acct.group != group:
                    continue
                if bool(acct.disabled) == (not enabled):
                    continue
                acct.disabled = not enabled
                acct.tasks_enabled = bool(enabled)
                if enabled:
                    acct.disabled_reason = ""
                    acct.clear_penalties()
                changed.append(acct.uid)
        if changed:
            self.save_state()
        return changed

    # ---- 调度 ----
    def pick(self, model="", session_key=None, exclude=None, allowed_accounts=None):
        """选一个能接单的账号：会话粘性 > 优先级 > 手动权重 > 临期积分 > 智能评分。

        优先级数字最小的一层先接单，整层都没有可交的账号时才看下一层。
        """
        exclude = set(exclude or ())
        now = time.time()
        with self._lock:
            accounts = list(self.accounts)
        # 会话粘性：同一段对话优先回到上次的账号；开关关掉时不看绑定。
        if session_key and wb_settings.session_affinity(self.accounts_dir):
            for uid in self.affinity.recent(session_key):
                if uid in exclude:
                    continue
                if allowed_accounts is not None and uid not in allowed_accounts:
                    continue
                acct = self.get(uid)
                if (acct and acct.healthy(now, model) and acct.has_capacity()
                        and acct.has_model(model)):
                    acct.mark_pick(now)
                    self.affinity.bind(session_key, uid)
                    return acct
        candidates = [
            a for a in accounts
            if a.uid not in exclude
            and (allowed_accounts is None or a.uid in allowed_accounts)
            and a.healthy(now, model)
            and a.has_capacity()
        ]
        capable = [a for a in candidates if a.has_model(model)]
        pool = capable or candidates
        for tier in self._priority_tiers(pool):
            picked = self._pick_weighted_first(tier)
            if picked is None:
                picked = self._pick_expiring_first(tier, now)
            if picked is None:
                picked = self._pick_scored(tier, now)
            if picked is not None:
                picked.mark_pick(now)
                return picked
        return None

    @staticmethod
    def _priority_tiers(pool):
        """按优先级数字分组，数字小的层在前。"""
        tiers = {}
        for acct in pool:
            tiers.setdefault(int(acct.priority), []).append(acct)
        for priority in sorted(tiers):
            yield tiers[priority]

    def _pick_weighted_first(self, pool):
        """手动权重 > 1 的账号先接单，彼此按权重平滑加权轮询。"""
        ready = [(acct, acct.uid, int(acct.weight))
                 for acct in pool if int(acct.weight or 1) > 1]
        if not ready:
            return None
        return self._smooth_weighted_pick(ready, "_manual_weights")

    def _pick_expiring_first(self, pool, now):
        """窗口内到期的积分先花掉；按「临期余额除以剩余天数」加权轮询。"""
        urgent = [a for a in pool if a.in_expiring_window(now)]
        if not urgent:
            return None
        return self._smooth_weighted_pick(
            [(acct, acct.uid, self.expiry_weight(acct, now)) for acct in urgent],
            "_expiry_weights")

    @staticmethod
    def expiry_weight(acct, now):
        """临期账号的派单份额：窗口内到期余额折算的每日最低消耗，至少为 1。"""
        try:
            window = int(acct.expiring_window_days or 0)
        except (TypeError, ValueError):
            window = 0
        days = acct.soonest_expiring_days(now=now)
        rate = acct.expiring_rate_per_day(window, now=now)
        if rate > 0:
            return max(1, int(round(rate)))
        if days is None:
            return 1
        return max(1, int(round(window - days)) + 1)

    def _smooth_weighted_pick(self, entries, state_name):
        """平滑加权轮询（nginx 算法）：权重相同退化成普通轮转。"""
        keys = set(key for _account, key, _weight in entries)
        with self._lock:
            stored = getattr(self, state_name)
            state = dict((key, value) for key, value in stored.items()
                         if key in keys)
            total = 0
            chosen = None
            for account, key, weight in entries:
                state[key] = state.get(key, 0) + weight
                total += weight
                if chosen is None or state[key] > state[chosen[1]]:
                    chosen = (account, key)
            if chosen is None:
                return None
            state[chosen[1]] -= total
            setattr(self, state_name, state)
        return chosen[0]

    def _pick_scored(self, pool, now):
        """按积分到期义务与最近一小时的接单份额评分：欠账多且最近清闲的先派。优先级
        已由外层分层决定，评分不再重复考虑。
        """
        total = len(pool)
        if total == 0:
            return None
        with self._lock:
            start = self._cursor
        targets = {acct.uid: acct.credit_target_per_day(now=now) for acct in pool}
        known = sorted(value for value in targets.values() if value is not None)
        if not known:
            median = 0.0
        else:
            mid = len(known) // 2
            median = (known[mid] if len(known) % 2
                      else (known[mid - 1] + known[mid]) / 2.0)
        backlog = {}
        for acct in pool:
            target = targets[acct.uid]
            owed = median if target is None else target
            backlog[acct.uid] = max(0.0, owed - float(acct.credit_rate_per_day or 0))
        total_backlog = sum(backlog.values())
        loads = {acct.uid: acct.recent_load(now) for acct in pool}
        total_hour = sum(loads.values())
        ranked = []
        for index, acct in enumerate(pool):
            share = (backlog[acct.uid] / total_backlog) if total_backlog > 0 else 0.0
            done = (loads[acct.uid] / float(total_hour)) if total_hour > 0 else 0.0
            ranked.append((done - share, -share, loads[acct.uid],
                           (index - start) % total, index))
        ranked.sort()
        index = ranked[0][4]
        with self._lock:
            self._cursor = (index + 1) % total
        return pool[index]

    def bind_session(self, session_key, uid):
        if not session_key or not uid:
            return
        if not wb_settings.session_affinity(self.accounts_dir):
            return
        self.affinity.bind(session_key, uid)

    def demote_session(self, session_key, uid):
        """失败账号从会话里丢掉；其余热账号保留，下一次先试它们。"""
        if not session_key or not uid:
            return
        self.affinity.demote(session_key, uid)

    def acquire(self, acct, force=False):
        with acct._lock:
            if not force and not acct.has_capacity():
                return False
            acct.active_concurrent += 1
            acct.last_used = time.time()
            return True

    def release(self, acct):
        with acct._lock:
            if acct.active_concurrent > 0:
                acct.active_concurrent -= 1

    def note_success(self, acct, model=""):
        with acct._lock:
            dirty = bool(acct.err_count or acct.cool_until or acct.last_error
                         or acct.model_cooldowns or acct.unscoped_streak)
            acct.note_success(model)
        if dirty:
            self.save_state()

    def note_failure(self, acct, message, detail=""):
        with acct._lock:
            acct.last_error = str(message or "")[:200]
            acct.last_error_detail = str(detail or "")[:2000]
            acct.note_failure()
        self.save_state()

    def note_model_rate(self, acct, model, message, wait=None, reset_at=None,
                        detail=""):
        with acct._lock:
            acct.last_error = str(message or "")[:200]
            acct.last_error_detail = str(detail or "")[:2000]
            acct.note_model_rate(model, wait=wait, reset_at=reset_at)
        self.save_state()

    def note_cooldown(self, acct, seconds, message, detail=""):
        with acct._lock:
            acct.last_error = str(message or "")[:200]
            acct.last_error_detail = str(detail or "")[:2000]
            acct.note_cooldown(seconds)
        self.save_state()

    def note_checkin_attempt(self, acct):
        """记一次签到尝试并落盘；次数与间隔跨重启仍然有效。"""
        with acct._lock:
            acct.note_checkin_attempt()
        self.save_state()


_POOLS = {}
_POOLS_LOCK = threading.Lock()


def pool_for(accounts_dir):
    """按 accounts_dir 取单例池；配置变化时刷新池配置。

    这里只读配置与账号，不触发任何后台任务：定时签到与保活由调度器统一负责，
    设置页刷新或一次模型列表请求都不该顺手打上游。
    """
    key = os.path.abspath(str(accounts_dir or "."))
    cfg = load_config(accounts_dir)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = CodeArtsPool(accounts_dir, cfg)
            _POOLS[key] = pool
        else:
            pool.config = cfg
    return pool


def invalidate_pool(accounts_dir=None):
    with _POOLS_LOCK:
        if accounts_dir is None:
            _POOLS.clear()
        else:
            _POOLS.pop(os.path.abspath(str(accounts_dir)), None)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _opener(proxy=""):
    """Build (and cache) an opener pinned to one proxy slot.

    Empty/blank means direct, and direct deliberately ignores the process
    environment proxies: a CodeArts account that is not bound to a slot must
    keep the pre-slot behaviour (no accidental HTTP_PROXY). One opener per
    proxy URL keeps each account's traffic on its own exit IP.
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return _DIRECT_OPENER
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            _OPENER_CACHE[proxy] = opener
        return opener


def _error_text(raw):
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return raw.decode("utf-8", "replace")[:500]
    if isinstance(parsed, dict):
        for key in ("error_msg", "message", "error_description", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value[:500]
        code = parsed.get("error_code") or parsed.get("code")
        if code not in (None, "", 0, "0"):
            return "error_code=%s" % code
    return raw.decode("utf-8", "replace")[:500]


def _http(method, url, headers=None, body=None, timeout=30.0, proxy=""):
    """发一个普通 HTTP 请求，返回 (status, raw_bytes)。"""
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method=method)
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            return resp.status, resp.read(ERROR_BODY_MAX * 4)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(ERROR_BODY_MAX)
        except Exception:
            raw = b""
        return exc.code, raw
    except (urllib.error.URLError, socket.timeout, TimeoutError,
            ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        error = CodeArtsUnavailable(
            "连不上华为云 CodeArts：%s - 请检查本机网络或代理。" % reason)
        # 原始异常留着，调用方据此区分「本机断网」与「上游故障」。
        error.cause = exc
        raise error


def _json_http(method, url, headers=None, body=None, timeout=30.0, proxy=""):
    status, raw = _http(method, url, headers, body, timeout, proxy)
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        parsed = None
    if status >= 400:
        message = _error_text(raw)
        if status in (401, 403):
            raise CodeArtsAuthError(message, status=status)
        if status == 400:
            raise CodeArtsBadRequest(message, status=status)
        if status == 404:
            raise CodeArtsNotFound(message, status=status)
        raise CodeArtsError("华为云返回 HTTP %s：%s" % (status, message),
                            status=status)
    if not isinstance(parsed, dict):
        raise CodeArtsError("华为云返回了无法解析的响应：%s"
                            % raw[:200].decode("utf-8", "replace"))
    return parsed


def _normalize_token(payload):
    """把 oauth2/tokens 响应归一成统一结构。"""
    creds = payload.get("credentials") or {}
    legacy = payload.get("credential") or {}
    if not creds.get("security_token") and legacy.get("securitytoken"):
        creds = {
            "access_key_id": legacy.get("access") or "",
            "secret_access_key": legacy.get("secret") or "",
            "security_token": legacy.get("securitytoken") or "",
            "expiration": legacy.get("expires_at") or "",
        }
    return {
        "user_id": str(payload.get("user_id") or ""),
        "user_name": str(payload.get("user_name") or ""),
        "domain_id": str(payload.get("domain_id") or ""),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "credentials": {
            "access_key_id": str(creds.get("access_key_id") or ""),
            "secret_access_key": str(creds.get("secret_access_key") or ""),
            "security_token": str(creds.get("security_token") or ""),
            "expiration": str(creds.get("expiration") or ""),
        },
    }


def request_token(form, dpop_jwk, timeout=30.0, proxy=""):
    url = STS_HOST + EP_OAUTH_TOKENS
    body = urllib.parse.urlencode(form).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "DPoP": crypto.dpop_proof(dpop_jwk, url),
    }
    payload = _json_http("POST", url, headers, body, timeout, proxy)
    token = _normalize_token(payload)
    if not token["credentials"]["security_token"]:
        raise CodeArtsError("登录响应缺少 security_token：%s"
                            % json.dumps(payload, ensure_ascii=False)[:300])
    return token


def exchange_code(code, verifier, port, dpop_jwk, client_id=CLIENT_ID,
                  proxy=""):
    form = {
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": "http://127.0.0.1:%d%s" % (port, REDIRECT_PATH),
    }
    return request_token(form, dpop_jwk, proxy=proxy)


def refresh_token_for(auth, timeout=30.0, proxy=""):
    jwk = auth.get("dpop_private_key")
    if not isinstance(jwk, dict) or not jwk.get("d"):
        jwk = crypto.jwk_from_keypair(*crypto.generate_keypair())
    form = {
        "client_id": str(auth.get("client_id") or CLIENT_ID),
        "grant_type": "refresh_token",
        "refresh_token": str(auth.get("refresh_token") or ""),
    }
    if str(auth.get("client_type") or "").lower() != "cli":
        form["code_verifier"] = str(auth.get("code_verifier") or "")
    try:
        return request_token(form, jwk, timeout, proxy)
    except CodeArtsBadRequest as exc:
        message = str(exc)
        lowered = message.lower()
        if ("invalid_grant" in lowered or "expiredrefreshtoken" in lowered
                or "invaliddpopheader" in lowered
                or "invalid client id" in lowered
                or "has been used" in lowered):
            raise CodeArtsAuthError(
                "refresh_token 已失效或被拒，需要重新登录：%s" % message,
                status=exc.status)
        raise


def poll_ticket(ticket_id, secret, timeout=30.0,
                plugin_name=PLUGIN_NAME, plugin_version=PLUGIN_VERSION,
                proxy=""):
    url = SNAP_MANAGER + EP_LOGIN_TICKET + "?" + urllib.parse.urlencode(
        {"ticket_id": ticket_id, "secret": secret})
    headers = {}
    if plugin_name:
        headers = {"plugin-name": plugin_name, "plugin-version": plugin_version}
    payload = _json_http("GET", url, headers, None, timeout, proxy)
    return _normalize_token(payload)


# ---------------------------------------------------------------------------
# 华为云签名请求
# ---------------------------------------------------------------------------

def _signed_headers(acct, method, url, headers, body):
    cred = acct.credential()
    return crypto.sign_request(
        method, url, headers, body,
        cred["access_key_id"], cred["secret_access_key"],
        cred["security_token"])


def _sign_base(headers, agent_type):
    """签名只覆盖 Content-Type 与调用方头；语言和 Agent-Type 签完再补。"""
    base = {"Content-Type": "application/json"}
    base.update(headers or {})
    language = str(base.pop("X-Language", "zh-cn"))
    agent = str(base.pop("Agent-Type", agent_type or ""))
    return base, language, agent


def signed_get(acct, url, headers=None, timeout=30.0, agent_type=""):
    base, language, agent = _sign_base(headers, agent_type)
    signed = _signed_headers(acct, "GET", url, base, b"")
    signed["X-Language"] = language
    if agent:
        signed["Agent-Type"] = agent
    return _json_http("GET", url, signed, None, timeout,
                      getattr(acct, "proxy", ""))


def signed_post(acct, url, payload, headers=None, timeout=30.0, agent_type=""):
    raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    base, language, agent = _sign_base(headers, agent_type)
    signed = _signed_headers(acct, "POST", url, base, raw)
    signed["X-Language"] = language
    if agent:
        signed["Agent-Type"] = agent
    return _json_http("POST", url, signed, raw, timeout,
                      getattr(acct, "proxy", ""))


# ---------------------------------------------------------------------------
# 模型目录
# ---------------------------------------------------------------------------

#: 常见别名到上游模型 id 的固定映射；目录还没拉到时的兜底。
_MODEL_ALIASES = {
    "snap-chat": "GLM-5.2", "glm-5.2": "GLM-5.2",
    "glm-5.1": "GLM-5.1", "glm-4.7": "GLM-4.7",
    "qwen3-vl-235b": "Qwen3-VL-235B",
    "qwen3.5-397b-a17b-vl": "Qwen3.5-397B-A17B-VL",
    "qwen3.6-27b-vl": "Qwen3.6-27B-VL",
}


def _canonical_model(acct, model):
    mid = str(model or "").strip()
    if not mid:
        return mid
    lower = mid.lower()
    for item in acct.models or ():
        if str(item.get("id") or "").lower() == lower:
            return str(item["id"])
        if str(item.get("name") or "").lower() == lower:
            return str(item["id"])
    for seed in SEED_KNOWN_MODELS + SEED_BENEFIT_MODELS:
        if seed.lower() == lower:
            return seed
    return _MODEL_ALIASES.get(lower, mid)


def _model_available(acct, model):
    """账号目录、内置种子或固定别名认识这个模型时为 True。"""
    lower = str(model or "").strip().lower()
    if not lower:
        return False
    for item in acct.models or ():
        if lower in (str(item.get("id") or "").lower(),
                     str(item.get("name") or "").lower()):
            return True
    return lower in {seed.lower() for seed in SEED_MODELS} or lower in _MODEL_ALIASES


#: CodeArts models known to read images even when a discovery source omits
#: `supports_images`. Deliberately CodeArts-specific: several overlapping ids
#: are marked as vision by other catalogues, but the CodeArts backend answers
#: "The request model is not multimodal" for them.
_KNOWN_VISION_IDS = {
    "qwen3-vl-235b", "qwen3.5-397b-a17b-vl",
    "qwen3.6-27b-vl", "kimi-k2.6-vl",
}


def _known_vision(mid):
    """Fallback vision metadata when a discovery source omits the flag."""
    return str(mid or "").strip().lower() in _KNOWN_VISION_IDS


def _wb_catalog_limits(mid):
    """Context/output limits from the WorkBuddy catalog for the same model id.

    CodeArts' builtin list often omits context_window/max_tokens. For ids that
    are the same backend model, the WorkBuddy catalogue has the spec; use it as
    a fallback rather than advertising no limit at all.
    """
    key = str(mid or "").strip().lower()
    if not key:
        return 0, 0
    try:
        import wb_catalog
        for table in (wb_catalog.STATIC_MODELS, wb_catalog.STATIC_CN_MODELS):
            for item in table:
                if str(item.get("id") or "").strip().lower() != key:
                    continue
                return (int(item.get("maxInputTokens") or 0),
                        int(item.get("maxOutputTokens") or 0))
    except Exception:
        pass
    return 0, 0


def _merge_models(sets):
    merged = {}
    for source, items in sets:
        for item in items or []:
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            key = mid.lower()
            current = merged.get(key)
            if current is None:
                merged[key] = {
                    "id": mid,
                    "name": str(item.get("name") or mid),
                    "context_window": int(item.get("context_window") or 0),
                    "max_tokens": int(item.get("max_tokens") or 0),
                    "desc": str(item.get("desc") or ""),
                    "benefit": bool(item.get("benefit")),
                    "supports_images": (item.get("supports_images")
                                        if isinstance(item.get("supports_images"), bool)
                                        else None),
                    "model_category": str(item.get("model_category") or ""),
                    "credits": str(item.get("credits") or ""),
                }
                continue
            if not current.get("context_window") and item.get("context_window"):
                current["context_window"] = int(item.get("context_window"))
            if not current.get("max_tokens") and item.get("max_tokens"):
                current["max_tokens"] = int(item.get("max_tokens"))
            if item.get("benefit"):
                current["benefit"] = True
            if not current.get("desc") and item.get("desc"):
                current["desc"] = item["desc"]
            if item.get("supports_images") is True:
                current["supports_images"] = True
            elif (item.get("supports_images") is False
                  and current.get("supports_images") is None):
                current["supports_images"] = False
            if not current.get("model_category") and item.get("model_category"):
                current["model_category"] = item["model_category"]
            if not current.get("credits") and item.get("credits"):
                current["credits"] = item["credits"]
    return list(merged.values())


def _fetch_agent_models(acct, timeout):
    # 官方 CLI/IDE 内核：AgentCenter 列表 → 主智能体详情 → data.gpts.models。
    # 列表只带 offset/limit，Agent-Type 是 AgentCenter；PromptCenter 是模型
    # 目录和 variants 接口的 Agent-Type。
    url = SNAP_HOST + EP_AGENT_LIST + "?" + urllib.parse.urlencode(
        {"offset": 0, "limit": 100})
    listing = signed_get(acct, url, timeout=timeout, agent_type="AgentCenter")
    agents = listing.get("agents") or []
    agent_id = ""
    for item in agents:
        alias = item.get("alias") or {}
        if (item.get("agent_name") == "CodeAgent"
                and alias.get("alias_zh_cn") == "智能体"
                and item.get("show_in_ide")):
            agent_id = str(item.get("agent_id") or "")
            break
    if not agent_id:
        for item in agents:
            if item.get("is_primary_agent"):
                agent_id = str(item.get("agent_id") or "")
                break
    if not agent_id and agents:
        agent_id = str(agents[0].get("agent_id") or "")
    if not agent_id:
        return []
    detail_url = SNAP_HOST + EP_AGENT_DETAIL + "?" + urllib.parse.urlencode(
        {"agent_id": agent_id})
    detail = signed_get(acct, detail_url, timeout=timeout,
                        agent_type="AgentCenter")
    models = ((detail.get("gpts") or {}).get("models") or [])
    out = []
    for item in models:
        params = item.get("model_parameters") or {}
        mid = (params.get("model_id") or item.get("model_id")
               or item.get("model_name") or "")
        name = str(item.get("model_name") or mid)
        if not mid:
            continue
        supports = params.get("supports_images", item.get("supports_images"))
        out.append({
            "id": str(mid),
            "name": name,
            "context_window": int(params.get("context_window") or 0),
            "max_tokens": int(params.get("max_tokens") or 0),
            "desc": str(params.get("model_desc") or ""),
            "benefit": False,
            "supports_images": (supports if isinstance(supports, bool) else None),
            "model_category": str(params.get("model_category")
                                  or item.get("model_category") or ""),
        })
    return out


def _credit_label(rows):
    """The multiplier a request normally pays, in the catalogue's `x0.18` form.

    `credit` holds one row per input-length tier and, on some models, one per
    time slot. An `off_peak` row is a discount (its `before_display_ratio` is
    the regular rate), so it must not be advertised as the model's price. A
    model without a usable row stays blank: no multiplier does not mean free.
    """
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        if row.get("time_slot_id") not in (None, "", "default"):
            continue
        text = str(row.get("ratio_display") or "").strip().lower()
        if not text.endswith("x"):
            continue
        try:
            float(text[:-1])
        except ValueError:
            continue
        return "x" + text[:-1]
    return ""


def _fetch_builtin_models(acct, timeout):
    data = signed_get(acct, SNAP_HOST + EP_MODEL_BUILTIN,
                      timeout=timeout, agent_type="PromptCenter")
    out = []
    for item in data.get("builtinModels") or []:
        mid = item.get("model_id") or item.get("model_name") or ""
        if not mid:
            continue
        out.append({
            "id": str(mid),
            "name": str(item.get("model_name") or mid),
            "context_window": int(item.get("context_window") or 0),
            "max_tokens": int(item.get("max_tokens") or 0),
            "desc": str(item.get("model_desc") or ""),
            "benefit": False,
            "supports_images": (item.get("supports_images")
                                if isinstance(item.get("supports_images"), bool)
                                else None),
            "model_category": str(item.get("model_category") or ""),
            "credits": _credit_label(item.get("credit")),
        })
    return out


def _claim_benefit(acct, timeout):
    try:
        signed_post(acct, BENEFIT_HOST + EP_BENEFIT_CLAIM, {},
                    timeout=timeout)
    except CodeArtsError:
        pass


def _fetch_benefit_models(acct, timeout, auto_claim=True):
    if auto_claim:
        _claim_benefit(acct, timeout)
    data = signed_get(acct, BENEFIT_HOST + EP_BENEFIT_CONFIG,
                      timeout=timeout)
    out = []
    result = data.get("result") or {}
    for item in result.get("models") or []:
        mid = item.get("model_id") or item.get("model_name") or ""
        if not mid:
            continue
        out.append({
            "id": str(mid),
            "name": str(item.get("model_name") or mid),
            "context_window": int(item.get("context_window") or 0),
            "max_tokens": int(item.get("max_tokens") or 0),
            "desc": str(item.get("model_desc") or ""),
            "benefit": True,
            "supports_images": (item.get("supports_images")
                                if isinstance(item.get("supports_images"), bool)
                                else None),
            "model_category": str(item.get("model_category") or ""),
        })
    return out


def fetch_models(acct, timeout=20.0, auto_claim=True):
    """三路发现并写入账号目录；单路失败不影响其他路。

    三路互不依赖，并行跑：最坏等待从「3 × timeout」降到「1 × timeout」。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    sets = []
    errors = []
    tasks = {
        "agent": lambda: _fetch_agent_models(acct, timeout),
        "builtin": lambda: _fetch_builtin_models(acct, timeout),
        "benefit": lambda: _fetch_benefit_models(acct, timeout, auto_claim),
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                if items:
                    sets.append((name, items))
            except CodeArtsError as exc:
                errors.append("%s: %s" % (name, exc))
            except Exception as exc:
                errors.append("%s: %s" % (name, exc))
    merged = _merge_models(sets)
    if not merged:
        # 全失败：保留旧目录；没有旧目录就用种子。
        if acct.models:
            return acct.models
        merged = _merge_models([("seed", [
            {"id": mid, "name": mid,
             "benefit": mid.lower() in {m.lower() for m in SEED_BENEFIT_MODELS},
             "supports_images": _known_vision(mid)}
            for mid in SEED_MODELS])])
    acct.models = merged
    acct.model_benefit = {str(m["id"]).lower(): bool(m.get("benefit"))
                          for m in merged}
    acct.models_fetched_at = time.time()
    return merged


def ensure_models(acct, force=False, timeout=20.0):
    cfg = acct.pool.config
    ttl = float(cfg.get("models_ttl_seconds") or DEFAULTS["models_ttl_seconds"])
    with acct._models_lock:
        age = time.time() - (acct.models_fetched_at or 0)
        if acct.models and not force and age < min(ttl, CATALOG_TTL):
            return acct.models
        try:
            return fetch_models(acct, timeout=timeout,
                                auto_claim=cfg.get("benefit_auto_claim", True))
        except CodeArtsError:
            if acct.models:
                return acct.models
            raise


def cached_model_ids(accounts_dir):
    """账号已发现目录的小写 id 到原始写法的映射，不发起网络请求；还没有目录时返回 None。"""
    pool = pool_for(accounts_dir)
    ids = {}
    for acct in list(pool.accounts):
        for item in (acct.models or ()):
            mid = str(item.get("id") or "").strip()
            if mid:
                ids.setdefault(mid.lower(), mid)
    return ids or None


def servable_accounts(accounts_dir, model):
    """此刻能服务这个模型的健康账号摘要，给跨渠道混合调度用；并发占满的账号标成没有空位。"""
    pool = pool_for(accounts_dir)
    now = time.time()
    return [{"uid": acct.uid, "priority": int(acct.priority), "weight": int(acct.weight),
             "active": int(acct.active_concurrent), "room": acct.has_capacity(),
             "expiry": (CodeArtsPool.expiry_weight(acct, now)
                        if acct.in_expiring_window(now) else 0)}
            for acct in list(pool.accounts)
            if acct.healthy(now, model) and acct.has_model(model)]


def model_entries(config):
    """所有账号目录的并集 + 种子；给 `/v1/models` 用。"""
    pool = pool_for(config["accounts_dir"])
    sets = []
    for acct in list(pool.accounts):
        try:
            _ensure_token(acct)
        except CodeArtsError:
            pass
        try:
            ensure_models(acct, timeout=8.0)
        except CodeArtsError:
            pass
        if acct.models:
            sets.append(("account", acct.models))
    merged = _merge_models(sets)
    if not merged:
        merged = _merge_models([("seed", [
            {"id": mid, "name": mid,
             "benefit": mid.lower() in {m.lower() for m in SEED_BENEFIT_MODELS},
             "supports_images": _known_vision(mid)}
            for mid in SEED_MODELS])])
    now = int(time.time())
    out = []
    for item in merged:
        entry = {
            "id": item["id"],
            "object": "model",
            "created": now,
            "owned_by": "codearts",
            "provider": "codearts",
            "name": item.get("name") or item["id"],
        }
        ctx = int(item.get("context_window") or 0)
        out_tokens = int(item.get("max_tokens") or 0)
        if not ctx or not out_tokens:
            wb_ctx, wb_out = _wb_catalog_limits(item["id"])
            ctx = ctx or wb_ctx
            out_tokens = out_tokens or wb_out
        if ctx:
            entry["context_window"] = ctx
            entry["context_length"] = ctx
            entry["max_input_tokens"] = ctx
        if out_tokens:
            entry["max_tokens"] = out_tokens
            entry["max_output_tokens"] = out_tokens
            entry["max_completion_tokens"] = out_tokens
            entry["max_output"] = out_tokens
        if item.get("benefit"):
            entry["benefit"] = True
        if item.get("credits"):
            entry["credits"] = item["credits"]
        supports = item.get("supports_images")
        if not isinstance(supports, bool):
            supports = _known_vision(item["id"])
        vision = bool(supports)
        inputs = ["text"] + (["image"] if vision else [])
        # Same convention set WorkBuddy emits: clients read different spellings
        # for vision support, and some clients need input modalities.
        entry["capabilities"] = {"vision": vision, "tool_calls": True}
        entry["supports_vision"] = vision
        entry["supports_images"] = vision
        entry["supports_tool_calls"] = True
        entry["vision"] = vision
        entry["multimodal"] = vision
        entry["abilities"] = {"vision": vision, "functionCall": True,
                              "function_call": True}
        entry["input_modalities"] = inputs
        entry["output_modalities"] = ["text"]
        entry["modalities"] = {"input": inputs, "output": ["text"]}
        entry["architecture"] = {
            "input_modalities": inputs,
            "output_modalities": ["text"],
            "modality": "+".join(inputs) + "->text",
        }
        if item.get("desc"):
            entry["description"] = item["desc"]
        if item.get("model_category"):
            entry["category"] = item["model_category"]
        efforts = REASONING_EFFORTS.get(str(item["id"]).lower())
        if efforts:
            entry["reasoning_efforts"] = list(efforts)
            entry["supports_reasoning"] = True
            capabilities = entry.setdefault(
                "capabilities", {"vision": False, "tool_calls": True})
            capabilities["reasoning"] = True
        if str(item["id"]).lower() == "glm-5.3-flash":
            entry["reasoning_can_disable"] = False
            entry["always_reasoning"] = True
        out.append(entry)
    return out


def models_meta(config):
    pool = pool_for(config["accounts_dir"])
    stamps = [a.models_fetched_at for a in pool.accounts if a.models_fetched_at]
    return {
        "source": "codearts",
        "fetched_at": max(stamps) if stamps else None,
        "stale": False,
        "accounts": len(pool.accounts),
    }


def invalidate_models():
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    for pool in pools:
        for acct in pool.accounts:
            acct.models = []
            acct.model_benefit = {}
            acct.models_fetched_at = 0


def refresh_models(config):
    pool = pool_for(config["accounts_dir"])
    fetched = 0
    for acct in list(pool.accounts):
        # 取数失败时 ensure_models 会退回旧目录，按取数时刻判断这次是否真的取到
        before = acct.models_fetched_at
        try:
            ensure_models(acct, force=True, timeout=20.0)
        except CodeArtsError:
            pass
        if acct.models_fetched_at > before:
            fetched += 1
    entries = model_entries(config)
    message = ("已从 %d 个账号取到模型目录" % fetched if fetched
               else "没有账号返回模型目录，当前显示上次取到的目录或内置模型表")
    return {"ok": bool(fetched), "models": len(entries), "message": message,
            "catalog": models_meta(config)}


# ---------------------------------------------------------------------------
# 聊天
# ---------------------------------------------------------------------------

#: 官方 OUTPUT_TOKEN_MAX：目录没有输出上限时用它。
OUTPUT_TOKEN_MAX = 32000
#: 有界内存状态：会话、轮次与密钥都按楼层淘汰，长时间运行不会无限增长。
_SESSION_STATE = collections.OrderedDict()
_SESSION_STATE_LOCK = threading.Lock()
_SESSION_STATE_LIMIT = 4096
_TURN_STATE = collections.OrderedDict()
_TURN_STATE_LOCK = threading.Lock()
_TURN_STATE_LIMIT = 2048
_MISSING = object()


def _session_for(session_key, messages):
    """一个对话一个 `ses_` 标识；没有会话键时用首条消息的哈希兜底。"""
    key = str(session_key or "")
    if not key:
        head = json.dumps(messages[:1] if messages else [],
                          ensure_ascii=False, separators=(",", ":"))
        key = "messages:" + hashlib.sha256(head.encode("utf-8")).hexdigest()[:32]
    with _SESSION_STATE_LOCK:
        current = _SESSION_STATE.get(key)
        if current is None:
            current = wire.session_id()
            _SESSION_STATE[key] = current
            while len(_SESSION_STATE) > _SESSION_STATE_LIMIT:
                _SESSION_STATE.popitem(last=False)
        else:
            _SESSION_STATE.move_to_end(key)
        return key, current


def _turn_ids(session_key, messages):
    """同一轮重试复用 `msg_`/trace/inference；新一轮换一套，span 与 snap 后缀每次新生成。"""
    key, session = _session_for(session_key, messages)
    digest = hashlib.sha256(json.dumps(
        messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    state_key = (key, digest)
    with _TURN_STATE_LOCK:
        current = _TURN_STATE.get(state_key)
        if current is None:
            current = {
                "msg": wire.message_id(),
                "trace": wire.trace_id(),
                "inference": secrets.token_hex(16),
            }
            _TURN_STATE[state_key] = current
            while len(_TURN_STATE) > _TURN_STATE_LIMIT:
                _TURN_STATE.popitem(last=False)
        else:
            _TURN_STATE.move_to_end(state_key)
        current = dict(current)
    current["session"] = session
    current["span"] = wire.span_id()
    current["snap"] = current["inference"] + "_" + secrets.token_hex(8)
    return session, current


def _content_parts(content):
    """按官方 TD 编码器整理 user 内容：单段文本写字符串，其余写部件数组。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for piece in content:
        if isinstance(piece, str):
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = str(piece.get("type") or "")
        if ptype in ("text", "input_text", "output_text", "summary_text"):
            if piece.get("text") is not None:
                parts.append({"type": "text", "text": str(piece["text"])})
            continue
        if ptype in ("input_audio", "audio"):
            audio = piece.get("input_audio")
            if isinstance(audio, dict) and audio.get("data"):
                parts.append({"type": "input_audio", "input_audio": {
                    "data": str(audio.get("data")),
                    "format": str(audio.get("format") or "wav"),
                }})
            continue
        if ptype == "file":
            raw_file = piece.get("file")
            if isinstance(raw_file, dict) and raw_file.get("file_data"):
                parts.append({"type": "file", "file": {
                    "filename": str(raw_file.get("filename") or "document.pdf"),
                    "file_data": str(raw_file.get("file_data")),
                }})
            continue
        if ptype not in ("image_url", "input_image", "image") \
                and "image_url" not in piece:
            continue
        raw = piece.get("image_url") or piece.get("url")
        url = ""
        if isinstance(raw, dict):
            url = str(raw.get("url") or "")
        elif raw:
            url = str(raw)
        if not url and isinstance(piece.get("source"), dict):
            source = piece["source"]
            stype = str(source.get("type") or "")
            if stype == "base64":
                media = str(source.get("media_type") or "image/png")
                data = str(source.get("data") or "")
                if data:
                    url = "data:%s;base64,%s" % (media, data)
            elif stype == "url":
                url = str(source.get("url") or "")
        if not url and piece.get("data"):
            mime = str(piece.get("mimeType")
                       or piece.get("mime_type") or "image/png")
            url = "data:%s;base64,%s" % (mime, piece["data"])
        if url:
            image = {"url": url}
            if isinstance(raw, dict) and raw.get("detail"):
                image["detail"] = str(raw["detail"])
            parts.append({"type": "image_url", "image_url": image})
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


def _text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict)
            and part.get("type") in ("text", "input_text", "output_text"))
    if content is None:
        return ""
    return str(content)


def _tool_calls(value):
    calls = []
    for item in value or ():
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict) or not function.get("name"):
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments if arguments is not None else {},
                                   ensure_ascii=False, separators=(",", ":"))
        calls.append({
            "id": str(item.get("id") or ""),
            "type": "function",
            "function": {"name": str(function["name"]),
                         "arguments": arguments},
            **({"extra_content": item["extra_content"]}
               if isinstance(item.get("extra_content"), dict) else {}),
        })
    return calls


def _encode_messages(messages, model):
    """官方消息编码：assistant 的 reasoning_content 只在有值或模型是 deepseek 时出现。"""
    encoded = []
    deepseek = str(model or "").lower().startswith("deepseek")
    for message in messages:
        role = str(message.get("role") or "")
        if role == "system":
            encoded.append({"role": "system",
                            "content": _text_content(message.get("content"))})
            continue
        if role == "user":
            encoded.append({"role": "user",
                            "content": _content_parts(message.get("content"))})
            continue
        if role == "assistant":
            item = {"role": "assistant",
                    "content": _text_content(message.get("content"))}
            reasoning = message.get("reasoning_content")
            if reasoning is None:
                reasoning = message.get("reasoning")
            reasoning = "" if reasoning is None else str(reasoning)
            if reasoning or deepseek:
                item["reasoning_content"] = reasoning
            calls = _tool_calls(message.get("tool_calls"))
            if calls:
                item["tool_calls"] = calls
            encoded.append(item)
            continue
        if role == "tool":
            encoded.append({
                "role": "tool",
                "tool_call_id": str(message.get("tool_call_id") or ""),
                "content": _text_content(message.get("content")),
            })
            continue
    return encoded


class ChatBody(dict):
    """请求体连同「客户端是否显式给了输出上限」一起传给选号后的收敛步骤。"""

    requested_max = None


_REASONING_MODEL = re.compile(r"^(o1|o3|o4|gpt-5|gpt-6)([.\-]|$)")
_GPT6_NO_FORCE_NONE = ("gpt-6-astra", "gpt-6.1-sol")


def _completion_tokens(model):
    return bool(_REASONING_MODEL.match(str(model or "").lower()))


def _forces_reasoning_none(model):
    lower = str(model or "").lower()
    if re.match(r"^gpt-5([.\-]|$)", lower):
        return True
    if re.match(r"^gpt-6([.\-]|$)", lower):
        return not lower.startswith(_GPT6_NO_FORCE_NONE)
    return False


def _model_display_name(acct, model):
    key = str(model or "").lower()
    for item in acct.models or ():
        if str(item.get("id") or "").strip().lower() == key:
            return str(item.get("name") or item.get("id") or model)
    return str(model)


def build_chat_body(payload, session_key=None):
    """把客户端载荷收敛成 CodeArts `/api/v2/chat/completions` 的请求体。"""
    if not isinstance(payload, dict):
        raise CodeArtsBadRequest("request body must be a JSON object")
    from wb_sanitize import (cleanup_orphan_tool_calls, merge_adjacent_tool_calls,
                             normalize_roles, normalize_tools,
                             repack_tool_result_blocks)
    model = str(payload.get("model") or "GLM-5.2")
    body = ChatBody()
    body["model"] = model
    key = "max_completion_tokens" if _completion_tokens(model) else "max_tokens"
    requested = payload.get("max_tokens")
    if requested is None:
        requested = payload.get("max_completion_tokens")
    if requested is not None:
        if isinstance(requested, bool):
            raise CodeArtsBadRequest("%s must be a positive integer" % key,
                                     status=400)
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            raise CodeArtsBadRequest("%s must be a positive integer" % key,
                                     status=400)
        if requested <= 0:
            raise CodeArtsBadRequest("%s must be a positive integer" % key,
                                     status=400)
        body[key] = requested
        body.requested_max = requested
    else:
        body[key] = OUTPUT_TOKEN_MAX
        body.requested_max = None
    for field in ("frequency_penalty", "presence_penalty"):
        if payload.get(field) is not None:
            body[field] = payload[field]
    if isinstance(payload.get("response_format"), dict):
        body["response_format"] = payload["response_format"]
    stop = payload.get("stop")
    if isinstance(stop, str):
        body["stop"] = [stop]
    elif isinstance(stop, list):
        body["stop"] = [str(item) for item in stop]
    if payload.get("seed") is not None:
        body["seed"] = payload["seed"]
    body["tool_stream"] = True
    effort = _requested_effort(payload)
    if effort:
        body["reasoning_effort"] = effort
    messages = normalize_roles(payload.get("messages") or [])
    messages = [m for m in messages if isinstance(m, dict)]
    messages = merge_adjacent_tool_calls(messages)[0]
    messages = repack_tool_result_blocks(messages)[0]
    messages = cleanup_orphan_tool_calls(messages)[0]
    if not messages:
        raise CodeArtsBadRequest("messages is empty")
    body["messages"] = _encode_messages(messages, model)
    if isinstance(payload.get("tools"), list):
        body["tools"] = payload["tools"]
    normalize_tools(body)
    tools = body.get("tools")
    if isinstance(tools, list):
        kept = []
        seen = set()
        for tool in tools:
            if not (isinstance(tool, dict)
                    and isinstance(tool.get("function"), dict)):
                continue
            name = tool["function"].get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            kept.append(tool)
        if kept:
            body["tools"] = kept
        else:
            body.pop("tools", None)
    if body.get("tools"):
        choice = payload.get("tool_choice")
        if isinstance(choice, dict):
            # 上游以 400 拒绝对象形式的 tool_choice；收窄工具清单再用 required，仍是官方客户端会发的形状。
            function = choice.get("function")
            name = function.get("name") if isinstance(function, dict) else ""
            named = [tool for tool in body["tools"] if tool["function"].get("name") == name]
            if named:
                body["tools"] = named
                choice = "required"
            else:
                choice = "auto"
        elif choice not in ("auto", "none", "required"):
            choice = "auto"
        body["tool_choice"] = choice
    else:
        body.pop("tool_choice", None)
    body["stream"] = True
    body["user_prompt"] = wire.last_user_prompt(messages)
    return body


_CHAT_BODY_ORDER = (
    "model", "max_completion_tokens", "max_tokens", "frequency_penalty",
    "presence_penalty", "response_format", "stop", "seed", "tool_stream",
    "reasoning_effort", "messages", "tools", "tool_choice", "stream",
    "user_prompt",
)


def _order_chat_body(body):
    return {key: body[key] for key in _CHAT_BODY_ORDER if key in body}


def _finalize_chat_body(acct, body, model):
    """按账号补上模型上限、tool_stream 与键序。"""
    result = dict(body)
    requested = getattr(body, "requested_max", _MISSING)
    if requested is _MISSING:
        requested = body.get("max_completion_tokens")
        if requested is None:
            requested = body.get("max_tokens")
    result.pop("max_completion_tokens", None)
    result.pop("max_tokens", None)
    limit = _model_output_limit(acct, model)
    if requested is not None:
        if isinstance(requested, bool):
            raise CodeArtsBadRequest("max_tokens must be a positive integer",
                                     status=400)
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            raise CodeArtsBadRequest("max_tokens must be a positive integer",
                                     status=400)
        if requested <= 0:
            raise CodeArtsBadRequest("max_tokens must be a positive integer",
                                     status=400)
        requested = min(requested, limit)
    else:
        requested = limit
    result["model"] = model
    result["max_completion_tokens" if _completion_tokens(model)
           else "max_tokens"] = requested
    profile = _account_profile(acct)
    if profile.get("tool_stream_always") or not acct.is_benefit(model):
        result["tool_stream"] = True
    else:
        result.pop("tool_stream", None)
    if _forces_reasoning_none(model) and result.get("tools"):
        result["reasoning_effort"] = "none"
    result["stream"] = True
    if "user_prompt" not in result:
        result["user_prompt"] = wire.last_user_prompt(
            result.get("messages") or [])
    return _order_chat_body(result)


_KEY_CACHE = collections.OrderedDict()
_KEY_CACHE_LOCK = threading.Lock()
_KEY_CACHE_LIMIT = 512


def _credential_key(acct):
    cred = acct.credential()
    secret = hashlib.sha256(
        ((cred.get("secret_access_key") or "") + "\x00"
         + (cred.get("security_token") or "")).encode("utf-8")).hexdigest()
    return (str(getattr(acct, "uid", "") or ""), cred.get("access_key_id") or "",
            secret)


def _account_key(acct, profile, timeout):
    """按凭据缓存 SM4 会话密钥；交换失败时返回 None，这一次聊天回落明文。"""
    cache_key = _credential_key(acct)
    now = time.time()
    with _KEY_CACHE_LOCK:
        entry = _KEY_CACHE.get(cache_key)
        if entry and entry["expires_at"] > now:
            _KEY_CACHE.move_to_end(cache_key)
            return entry["key"], entry["key_id"]
        if entry:
            _KEY_CACHE.pop(cache_key, None)
    try:
        key, key_id, expires = wire.key_exchange(
            SNAP_HOST, acct.credential(), profile, proxy=acct.proxy,
            timeout=min(20.0, timeout))
    except Exception:
        return None
    ttl = max(30.0, float(expires or 0) - 30.0)
    with _KEY_CACHE_LOCK:
        _KEY_CACHE[cache_key] = {"key": key, "key_id": key_id,
                                 "expires_at": time.time() + ttl}
        while len(_KEY_CACHE) > _KEY_CACHE_LIMIT:
            _KEY_CACHE.popitem(last=False)
    return key, key_id


class ChatHeartbeat(object):
    """聊天会话心跳：回答开始发 busy，期间每 30 秒一次，结束发 idle。"""

    def __init__(self, interval=None, max_lifetime=86400.0):
        self.interval = float(interval or wire.HEARTBEAT_INTERVAL)
        self.max_lifetime = float(max_lifetime)
        self._lock = threading.Lock()
        self._entries = collections.OrderedDict()
        self._limit = 1024

    def busy(self, session, sender):
        with self._lock:
            entry = self._entries.get(session)
            if entry is not None:
                entry["count"] += 1
                entry["sender"] = sender
                self._entries.move_to_end(session)
                return
            entry = {"count": 1, "sender": sender,
                     "stop": threading.Event(), "thread": None,
                     "started": time.time()}
            self._entries[session] = entry
            while len(self._entries) > self._limit:
                old_key, old = self._entries.popitem(last=False)
                old["stop"].set()
                self._send(old["sender"], "idle")
            thread = threading.Thread(target=self._run, args=(session, entry),
                                      daemon=True)
            entry["thread"] = thread
            thread.start()
        self._send(sender, "busy")

    def idle(self, session):
        sender = None
        with self._lock:
            entry = self._entries.get(session)
            if entry is None:
                return
            entry["count"] -= 1
            if entry["count"] > 0:
                return
            self._entries.pop(session, None)
            entry["stop"].set()
            sender = entry["sender"]
            thread = entry["thread"]
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._send(sender, "idle")

    def _run(self, session, entry):
        while not entry["stop"].wait(self.interval):
            with self._lock:
                if self._entries.get(session) is not entry:
                    return
                if time.time() - entry["started"] > self.max_lifetime:
                    self._entries.pop(session, None)
                    entry["stop"].set()
                    sender = entry["sender"]
                    expired = True
                else:
                    sender = entry["sender"]
                    expired = False
            if expired:
                self._send(sender, "idle")
                return
            self._send(sender, "busy")

    @staticmethod
    def _send(sender, status):
        if sender is None:
            return
        try:
            sender(status)
        except Exception:
            pass


_CHAT_HEARTBEAT = ChatHeartbeat()


def _heartbeat_sender(acct, session):
    def send(status):
        wire.send_heartbeat(SNAP_HOST, acct.credential(), session, status,
                            proxy=acct.proxy, timeout=10.0)
    return send


class CodeArtsStream(object):
    """把华为云 SSE 包成 wb-hub 回程处理器能吃的行迭代器。

    wb-hub 的 `_chat_stream_response` / `aggregate_stream` 只认标准 OpenAI
    chunk（choices[].delta / finish_reason / usage）。这里把 CodeArts 的
    `text` 全文快照、`output` 终态和顶层 `delta` 都归一成标准 chunk。
    """

    def __init__(self, raw, model, acct, pool, heartbeat=None,
                 heartbeat_session=""):
        self.raw = raw
        self.model = model
        self.acct = acct
        self.pool = pool
        self._wb_billed_model = model
        self._pending = b""
        self._done = False
        self._finish_sent = False
        self._last_text = ""
        self._usage = None
        self._created = int(time.time())
        self._closed = False
        self._released = False
        self._heartbeat = heartbeat
        self._heartbeat_session = str(heartbeat_session or "")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        """关闭响应并归还并发名额；重复调用只生效一次。

        预检失败时 `_client_chat` 只丢响应不归还名额（名额归调用方管），因此它走
        `_discard()`。这里做成幂等，任何路径重复关闭都不会多算一次释放。
        """
        if self._closed:
            return
        self._closed = True
        self._discard_raw()
        self._stop_heartbeat()
        self._release()

    def _release(self):
        """归还并发名额；上游读完就还，不等下游把响应写完。

        非流式路径先聚合成整体再回包：若等调用方 close()，客户端拿到 200 后立刻发
        下一条请求时名额还没归还，会被误判成全忙。
        """
        if self._released:
            return
        self._released = True
        try:
            self.pool.release(self.acct)
        except Exception:
            pass

    def _discard(self):
        """预检失败：丢掉响应，但名额由选号方统一归还。"""
        if self._closed:
            return
        self._closed = True
        self._discard_raw()

    def _stop_heartbeat(self):
        if self._heartbeat is not None and self._heartbeat_session:
            session = self._heartbeat_session
            self._heartbeat_session = ""
            self._heartbeat.idle(session)

    def _discard_raw(self):
        try:
            self.raw.close()
        except Exception:
            pass

    def preflight(self):
        """读到首个 data 事件；HTTP 200 里内嵌的业务错误在这里抛出。"""
        while True:
            line = self.raw.readline()
            if not line:
                return
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                continue
            data = stripped[5:].strip()
            if not data:
                continue
            self._pending = line if line.endswith(b"\n") else line + b"\n"
            self._raise_embedded(data)
            return

    def _raise_embedded(self, data):
        text = data.decode("utf-8", "replace")
        if text == "[DONE]":
            return
        try:
            payload = json.loads(text)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        code = payload.get("error_code") or payload.get("code")
        if code not in (None, "", 0, "0"):
            message = str(payload.get("error_msg") or payload.get("message")
                          or text)[:300]
            lowered = message.lower()
            if ("request param is invalid" in lowered
                    or "request model is not multimodal" in lowered
                    or "tool function cannot empty" in lowered):
                raise CodeArtsBadRequest(message, status=400, code=str(code))
            raise CodeArtsUpstreamError(message, code=str(code))

    def _chunk(self, delta, finish=None):
        payload = {
            "id": "chatcmpl-codearts",
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish}],
        }
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _text_delta(self, text):
        if text is None:
            return b""
        text = str(text)
        if text == self._last_text:
            return b""
        if text.startswith(self._last_text):
            delta = text[len(self._last_text):]
        else:
            delta = text
        self._last_text = text
        return self._chunk({"content": delta}) if delta else b""

    def __iter__(self):
        while not self._done:
            if self._pending:
                line, self._pending = self._pending, b""
            else:
                line = self.raw.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                continue
            data = stripped[5:].strip()
            if not data:
                continue
            text = data.decode("utf-8", "replace")
            if text == "[DONE]":
                self._done = True
                break
            self._raise_embedded(data)
            try:
                payload = json.loads(text)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._usage = usage
            choices = payload.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] if isinstance(choices[0], dict) else {}
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        delta = {"role": message.get("role") or "assistant",
                                 "content": message.get("content")}
                        if message.get("tool_calls"):
                            delta["tool_calls"] = message["tool_calls"]
                    else:
                        delta = {}
                out = {
                    "id": payload.get("id") or "chatcmpl-codearts",
                    "object": "chat.completion.chunk",
                    "created": int(payload.get("created") or self._created),
                    "model": payload.get("model") or self.model,
                    "choices": [{
                        "index": choice.get("index", 0),
                        "delta": delta,
                        "finish_reason": choice.get("finish_reason"),
                    }],
                }
                yield ("data: " + json.dumps(out, ensure_ascii=False) + "\n\n").encode("utf-8")
                if choice.get("finish_reason"):
                    self._finish_sent = True
                continue
            # 顶层 delta（部分 CodeArts 事件）
            delta = payload.get("delta")
            if isinstance(delta, dict):
                yield self._chunk(delta)
                continue
            # 全文快照
            if "text" in payload:
                text_value = str(payload.get("text") or "")
                if text_value.strip() == "[DONE]" or text_value.strip().lower() == "done":
                    self._done = True
                    break
                chunk = self._text_delta(text_value)
                if chunk:
                    yield chunk
                continue
            # 终态 output 数组
            output = payload.get("output")
            if isinstance(output, list):
                text_out = "".join(
                    str(item.get("text") or "") for item in output
                    if isinstance(item, dict) and item.get("type") == "output_text")
                chunk = self._text_delta(text_out)
                if chunk:
                    yield chunk
        self._stop_heartbeat()
        if not self._finish_sent:
            yield self._chunk({}, "stop")
        if self._usage:
            yield ("data: " + json.dumps({
                "id": "chatcmpl-codearts",
                "object": "chat.completion.chunk",
                "created": self._created,
                "model": self.model,
                "choices": [],
                "usage": self._usage,
            }, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"
        # 上游这条会话已经结束：立刻归还名额，不等调用方 close()。
        self._release()


def _ensure_token(acct, force=False):
    """必要时换新令牌；force 用于 401/403 后的原账号重试。

    刷新失败不再自动停用账号：凭据错误由调用方冷却一段时间并记下原因，启用状态
    始终由操作者掌握。
    """
    if not force and not acct.expiring_soon() and not acct.expired():
        return
    # refresh_token 是一次性的：同一账号并发刷新会让后一个请求拿到已消费的
    # token。串行化，并在拿到锁后重新判断（前一个请求可能已经刷好了）。
    stale_token = acct.auth.get("cloud_dragon_token")
    with acct._refresh_lock:
        if force:
            if acct.auth.get("cloud_dragon_token") != stale_token:
                return              # 别的请求已经换过，不要白白再消费一次
        elif not acct.expiring_soon() and not acct.expired():
            return
        try:
            token = refresh_token_for(acct.auth, proxy=acct.proxy)
        except CodeArtsBadRequest as exc:
            # 换 token 的请求本身被拒（凭据与 DPoP/refresh_token 不匹配）：
            # 这是凭据问题，交给调用方冷却并记下原因。
            raise CodeArtsAuthError(str(exc), status=exc.status or 401)
        except CodeArtsError:
            if not force and not acct.expired():
                return
            raise
        acct.update_auth(token)


def _is_concurrency_message(message):
    text = str(message or "").lower()
    return ("并发会话" in text or "tm.00001041" in text
            or "concurrent session" in text or "maximum number of active"
            in text)


def _model_output_limit(acct, model):
    """Advertised output limit for the canonical CodeArts model."""
    key = str(model or "").strip().lower()
    for item in acct.models or ():
        if str(item.get("id") or "").strip().lower() != key:
            continue
        for field in ("max_tokens", "max_output_tokens",
                      "max_completion_tokens"):
            try:
                value = int(item.get(field) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    try:
        _ctx, value = _wb_catalog_limits(model)
        if value > 0:
            return value
    except Exception:
        pass
    return OUTPUT_TOKEN_MAX


def _client_chat(acct, body, session_key):
    model = _canonical_model(acct, body.get("model"))
    upstream_body = _finalize_chat_body(acct, body, model)
    messages = upstream_body["messages"]
    session, turn = _turn_ids(session_key, messages)
    benefit = acct.is_benefit(model)
    profile = _account_profile(acct)
    plaintext = json.dumps(upstream_body, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    timeout = float((getattr(acct.pool, "config", {}) or {}).get(
        "stream_timeout_seconds") or DEFAULTS["stream_timeout_seconds"])
    pool_config = getattr(acct.pool, "config", {}) or {}
    key_material = None
    if pool_config.get("chat_encryption", True) is not False:
        key_material = _account_key(acct, profile, timeout)
    display = _model_display_name(acct, model)
    last_error = None
    for attempt in range(3):
        attempt_turn = dict(turn)
        attempt_turn["span"] = wire.span_id()
        attempt_turn["snap"] = attempt_turn["inference"] + "_" + secrets.token_hex(8)
        request = wire.chat_request(
            profile, model, display, benefit, attempt_turn, acct.credential(),
            SNAP_HOST, plaintext, key_material)
        try:
            resp = wb_upstream_pool.open_ordered(
                request["url"], "POST", request["headers"], request["body"],
                timeout=timeout, proxy=acct.proxy,
                pooled=wb_upstream_pool.enabled())
        except urllib.error.HTTPError as exc:
            err = b""
            try:
                err = exc.read(ERROR_BODY_MAX)
                err = wire.decode_body(
                    err, exc.headers.get("Content-Encoding") or "")
            except Exception:
                pass
            if (key_material and str(
                    exc.headers.get("X-Encrypt-Algo") or "").lower()
                    == wire.ENCRYPT_ALGO):
                try:
                    err = wire.decrypt_line(
                        err, key_material[0],
                        exc.headers.get("X-Encrypt-Key-Id") or key_material[1])
                except Exception:
                    pass
            message = _error_text(err)
            if exc.code in (401, 403):
                raise CodeArtsAuthError(message, status=exc.code)
            if exc.code == 400 and _is_concurrency_message(message):
                last_error = CodeArtsUpstreamError(
                    message, status=429, code="TM.00001041")
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise last_error
            if exc.code == 400:
                raise CodeArtsBadRequest(message, status=exc.code)
            if exc.code == 429:
                error = CodeArtsUpstreamError(message, status=exc.code, code="429")
                # 上游给的恢复时刻优先；没有时按模型冷却的阶梯退避。
                error.retry_after = _retry_after_seconds(exc.headers)
                error.reset_at = _reset_hint(message)
                raise error
            raise CodeArtsError("CodeArts 返回 HTTP %s：%s" % (exc.code, message),
                                status=exc.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", None) or exc
            error = CodeArtsUnavailable("连不上 CodeArts 聊天接口：%s" % reason)
            error.cause = exc
            raise error
        except wb_upstream_pool.PoolBypass as exc:
            raise CodeArtsUnavailable("CodeArts 聊天接口无法使用当前代理：%s" % exc)
        encrypted = str(resp.headers.get("X-Encrypt-Algo") or "").lower()
        decrypt_key = None
        decrypt_id = ""
        if encrypted:
            if encrypted != wire.ENCRYPT_ALGO:
                resp.close()
                raise CodeArtsUpstreamError(
                    "CodeArts 应答使用了不支持的加密算法：%s" % encrypted)
            if not key_material:
                resp.close()
                raise CodeArtsUpstreamError("CodeArts 应答已加密，但没有可用的会话密钥")
            decrypt_key = key_material[0]
            decrypt_id = (resp.headers.get("X-Encrypt-Key-Id")
                          or key_material[1])
        try:
            reader = wire.LineReader(resp, decrypt_key, decrypt_id)
        except Exception as exc:
            resp.close()
            raise CodeArtsUpstreamError("CodeArts 应答解码失败：%s" % exc)
        stream = CodeArtsStream(reader, model, acct, acct.pool,
                                heartbeat=_CHAT_HEARTBEAT,
                                heartbeat_session=session)
        try:
            stream.preflight()
        except CodeArtsUpstreamError as exc:
            # 名额归选号方：这里只丢响应，不释放并发计数。
            stream._discard()
            if _is_concurrency_message(str(exc)):
                last_error = exc
                if attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
            raise
        except CodeArtsError:
            stream._discard()
            raise
        except Exception as exc:
            stream._discard()
            raise CodeArtsUpstreamError("CodeArts 应答解码失败：%s" % exc)
        _CHAT_HEARTBEAT.busy(session, _heartbeat_sender(acct, session))
        return stream, model
    raise last_error or CodeArtsUpstreamError("CodeArts 并发会话已满")


def _auth_cooldown(total_accounts):
    """凭据被拒后的冷却：只有一个账号时短一些，等它自愈；多账号时交给别的号。"""
    return 3 if int(total_accounts or 0) <= 1 else 60


def _open_on_account(acct, body, session_key):
    """在一个账号上打开会话：必要时换令牌，凭据被拒时强制续期并重试一次。"""
    _ensure_token(acct)
    try:
        ensure_models(acct, timeout=8.0)
    except CodeArtsError:
        pass
    try:
        return _client_chat(acct, body, session_key)
    except CodeArtsAuthError:
        _ensure_token(acct, force=True)
        return _client_chat(acct, body, session_key)


def _no_account_error(pool, model, allowed_accounts=None):
    """没有账号接下这条请求时说清是哪一类原因：并发满、冷却中、还是池子为空。"""
    now = time.time()
    wanted = [acct for acct in pool.accounts
              if allowed_accounts is None or acct.uid in allowed_accounts]
    if not wanted:
        raise CodeArtsNoAccounts(
            "没有可用的 CodeArts 账号：请确认调试账号属于该渠道。")
    enabled = [acct for acct in wanted if not acct.disabled]
    if not enabled:
        reasons = ["%s 已停用" % acct.nickname for acct in wanted[:2]]
        raise CodeArtsNoAccounts("没有可用的 CodeArts 账号：" + "；".join(reasons))
    free = [acct for acct in enabled if acct.healthy(now, model)]
    if free:
        raise CodeArtsBusy(
            "所有可用的 CodeArts 账号都在处理请求，请等一轮在途请求结束后重试。",
            wait=1)
    reasons = []
    waits = []
    for acct in enabled:
        reason = acct.blocked_reason(model, now)
        if reason:
            reasons.append("%s %s" % (acct.nickname, reason))
        wait = acct.wait_seconds(model, now)
        if wait > 0:
            waits.append(wait)
    message = "没有可用的 CodeArts 账号：" + (
        "；".join(reasons[:2]) or "请检查账号的模型禁用与限额设置。")
    if waits:
        raise CodeArtsCooling(message, wait=max(1, int(min(waits) + 0.999)))
    raise CodeArtsNoAccounts(message)


def open_chat(config, body, session_key=None, allowed_accounts=None):
    """选账号、续期、发现模型并打开 SSE；返回 (stream, account, model)。"""
    pool = pool_for(config["accounts_dir"])
    # 聊天路径顺带推进号池守卫与积分消耗速度：CodeArts 不走 WorkBuddy 的每日
    # 守卫推送，少了这一步，日限额与智能评分在纯 CodeArts 网关上永远不更新。
    apply_pool_guards(config)
    accounts = list(pool.accounts)
    if not accounts:
        raise CodeArtsNoAccounts(
            "没有可用的 CodeArts 账号：请在看板「账号」页面添加至少一个账号。")
    model = str(body.get("model") or "")
    exclude = set()
    last_error = None
    last_uid = None
    attempts = max(2, len(accounts) + 1)
    for _ in range(attempts):
        acct = pool.pick(model, session_key=session_key, exclude=exclude,
                         allowed_accounts=allowed_accounts)
        if acct is None:
            break
        if not pool.acquire(acct):
            exclude.add(acct.uid)
            continue
        handed = False
        try:
            stream, used = _open_on_account(acct, body, session_key)
            handed = True
            pool.note_success(acct, model=used)
            pool.bind_session(session_key, acct.uid)
            return stream, acct, used
        except CodeArtsBadRequest:
            raise
        except CodeArtsAuthError as exc:
            # 换过令牌还是 401/403：冷却这个账号，交给别的账号；不停用。
            pool.note_cooldown(acct, _auth_cooldown(len(accounts)), str(exc))
            pool.demote_session(session_key, acct.uid)
            exclude.add(acct.uid)
            last_error, last_uid = exc, acct.uid
        except CodeArtsUpstreamError as exc:
            if _is_concurrency_message(str(exc)):
                pool.note_cooldown(acct, 60, str(exc))
            elif exc.status == 429 or "429" in str(exc.code or ""):
                used = _canonical_model(acct, body.get("model"))
                pool.note_model_rate(
                    acct, used, str(exc),
                    wait=getattr(exc, "retry_after", None),
                    reset_at=getattr(exc, "reset_at", None))
            else:
                pool.note_failure(acct, str(exc))
            pool.demote_session(session_key, acct.uid)
            exclude.add(acct.uid)
            last_error, last_uid = exc, acct.uid
        except CodeArtsUnavailable as exc:
            cause = getattr(exc, "cause", None)
            if _looks_like_local_network(cause or exc):
                raise CodeArtsLocalNetwork(str(exc))
            pool.note_failure(acct, str(exc))
            pool.demote_session(session_key, acct.uid)
            exclude.add(acct.uid)
            last_error, last_uid = exc, acct.uid
        except CodeArtsError as exc:
            pool.note_failure(acct, str(exc))
            pool.demote_session(session_key, acct.uid)
            exclude.add(acct.uid)
            last_error, last_uid = exc, acct.uid
        finally:
            if not handed:
                pool.release(acct)
    if last_error is not None:
        if last_uid and not getattr(last_error, "account_uid", None):
            last_error.account_uid = last_uid
        raise last_error
    _no_account_error(pool, model, allowed_accounts)


class DeviceSessions(object):
    """按账号登记设备会话：CLI/IDE 启动时登记，之后每 120 秒查一次状态。"""

    def __init__(self, config, interval=None):
        self.config = config
        self.interval = float(interval or wire.DEVICE_POLL_INTERVAL)
        self.last_error = ""
        self._lock = threading.Lock()
        self._states = collections.OrderedDict()
        self._limit = 512
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        states = self._take_states()
        for (uid, identity), state in states:
            if identity == "ide":
                self._call(uid, "ide", "status", status=1)

    def _take_states(self):
        with self._lock:
            states = list(self._states.items())
            self._states.clear()
        return states

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.last_error = str(exc)[:300]
            self._stop.wait(5.0)

    def tick(self, now=None):
        now = time.time() if now is None else float(now)
        pool = pool_for(self.config["accounts_dir"])
        alive = set()
        evicted = []
        for acct in list(pool.accounts):
            cred = acct.credential()
            if (acct.disabled or not cred.get("security_token")
                    or acct.expired()):
                continue
            identity = normalize_client_type(acct.effective_client_type())
            key = (acct.uid, identity)
            alive.add(key)
            with self._lock:
                state = self._states.get(key)
                if state is None:
                    state = {"next": now + self.interval}
                    self._states[key] = state
                    while len(self._states) > self._limit:
                        old_key, _old = self._states.popitem(last=False)
                        evicted.append(old_key)
                    created = True
                else:
                    self._states.move_to_end(key)
                    created = False
            if created:
                if identity == "cli":
                    self._account_call(acct, "cli", "create")
                else:
                    self._account_call(acct, "ide", "create", status=0)
            elif now >= state["next"]:
                state["next"] = now + self.interval
                self._account_call(acct, identity, "poll")
        with self._lock:
            stale = [key for key in self._states if key not in alive]
            for key in stale:
                self._states.pop(key, None)
        for key in evicted + stale:
            self._stop_identity(key)
        return len(alive)

    def _stop_identity(self, key):
        identity = key[1]
        if identity != "ide":
            return
        acct = pool_for(self.config["accounts_dir"]).get(key[0])
        if acct is not None and acct.credential().get("security_token"):
            self._call(key[0], "ide", "status", status=1)

    def _account_call(self, acct, identity, action, status=0):
        profile = client_profile(identity)
        try:
            if identity == "cli":
                wire.device_session_cli(SNAP_HOST, acct.credential(), action,
                                        profile, proxy=acct.proxy,
                                        timeout=15.0)
            else:
                wire.device_session_ide(SNAP_HOST, acct.credential(), action,
                                        profile, status=status,
                                        proxy=acct.proxy, timeout=15.0)
        except Exception as exc:
            self.last_error = str(exc)[:300]

    def _call(self, uid, identity, action, status=0):
        acct = pool_for(self.config["accounts_dir"]).get(uid)
        if acct is None:
            return
        self._account_call(acct, identity, action, status=status)


_DEVICE_KEEPER = None
_DEVICE_KEEPER_LOCK = threading.Lock()


def start_device_sessions(config):
    """启动设备会话循环；同一个账号目录只保留一个循环。"""
    global _DEVICE_KEEPER
    with _DEVICE_KEEPER_LOCK:
        if _DEVICE_KEEPER is None:
            _DEVICE_KEEPER = DeviceSessions(config)
            _DEVICE_KEEPER.start()
        return _DEVICE_KEEPER


def stop_device_sessions():
    global _DEVICE_KEEPER
    with _DEVICE_KEEPER_LOCK:
        keeper = _DEVICE_KEEPER
        _DEVICE_KEEPER = None
    if keeper is not None:
        keeper.stop()


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

_OAUTH_LOCK = threading.RLock()
_OAUTH_SESSIONS = {}
_OAUTH_TTL_SECONDS = OAUTH_TTL


class OAuthSession(object):
    def __init__(self, session_id, ticket_id, secret, verifier, dpop_jwk, port,
                 client_type=DEFAULT_CLIENT_TYPE, client_id=CLIENT_ID,
                 plugin_name=PLUGIN_NAME, plugin_version=PLUGIN_VERSION,
                 slot_id="", proxy=""):
        self.id = session_id
        self.ticket_id = ticket_id
        self.secret = secret
        self.verifier = verifier
        self.dpop_jwk = dpop_jwk
        self.port = port
        self.client_type = client_type
        self.client_id = client_id
        self.plugin_name = plugin_name
        self.plugin_version = plugin_version
        # 登录用的出口：换 token 与账号后续调用走同一个 IP。
        self.slot_id = slot_id
        self.proxy = proxy
        self.created_at = time.time()
        self.status = "pending"
        self.message = "等待浏览器完成授权…"
        self.account = None
        self.ticket_fallback = False
        self.auth_url = ""

    def public(self):
        out = {
            "ok": self.status != "error",
            "status": self.status,
            "message": self.message,
            "session_id": self.id,
            "auth_url": self.auth_url,
        }
        if self.slot_id:
            out["proxySlot"] = self.slot_id
        if self.account:
            out["account"] = self.account
        return out


def _gc_oauth(now=None):
    now = now or time.time()
    for key, session in list(_OAUTH_SESSIONS.items()):
        if now - session.created_at > _OAUTH_TTL_SECONDS:
            _OAUTH_SESSIONS.pop(key, None)


def _login_exit(accounts_dir, slot_id):
    """登录出口：返回 (槽 id, 代理 URL)，与面板的账号出口绑定同一套语义。"""
    resolved = resolve_slot(accounts_dir, slot_id)
    if not resolved:
        return "", ""
    for entry in wb_settings.proxy_slots(accounts_dir):
        if entry.get("id") == resolved and entry.get("enabled", True):
            return resolved, entry.get("url") or ""
    return resolved, ""


def oauth_start(config, port=8788, proxy_slot=""):
    _gc_oauth()
    client_type = normalize_client_type(config.get("client_type"))
    profile = client_profile(client_type)
    slot_id, proxy = _login_exit(config.get("accounts_dir"), proxy_slot)
    ticket_id = crypto.random_hex(16)
    secret = crypto.random_hex(16)
    verifier, challenge = crypto.pkce_pair()
    dpop_jwk = crypto.jwk_from_keypair(*crypto.generate_keypair())
    session_id = secrets.token_hex(8)
    query = {
        "locale": "zh-cn",
        "client_id": profile["oauth_client_id"],
        "port": str(int(port)),
        "code_challenge": challenge,
        "code_challenge_method": "SHA-256",
        "ticket_id": ticket_id,
    }
    if client_type == "ide":
        query["theme"] = "2"
    if profile["oauth_uri_scheme"]:
        query["uri_scheme"] = profile["oauth_uri_scheme"]
    if profile["oauth_plugin_name"]:
        query["plugin-name"] = profile["oauth_plugin_name"]
        query["plugin-version"] = profile["oauth_plugin_version"]
    if profile["oauth_callback_url"]:
        query["auth_callback_url"] = ("http://127.0.0.1:%d%s"
                                      % (int(port), REDIRECT_PATH))
    auth_url = PORTAL_HOST + "/authorize?" + urllib.parse.urlencode(query)
    session = OAuthSession(session_id, ticket_id, secret, verifier, dpop_jwk,
                           int(port), client_type=client_type,
                           client_id=profile["oauth_client_id"],
                           plugin_name=profile["oauth_plugin_name"],
                           plugin_version=profile["oauth_plugin_version"],
                           slot_id=slot_id, proxy=proxy)
    session.auth_url = auth_url
    with _OAUTH_LOCK:
        # 官方 IDE 的浏览器回调只带 code/secret，不带可路由的会话标识。
        # 同一实例里只保留最新一次待授权会话，旧会话立即作废，避免重试后
        # 回调落到上一次失败请求上。
        for old in _OAUTH_SESSIONS.values():
            if old.port == int(port) and old.status == "pending":
                old.status = "error"
                old.message = "已被新的登录请求替换"
        _OAUTH_SESSIONS[session_id] = session
    return {
        "ok": True,
        "session_id": session_id,
        "auth_url": auth_url,
        "expires_in": int(_OAUTH_TTL_SECONDS),
        "proxySlot": slot_id,
        "message": "请在浏览器打开授权链接，完成后等待自动检测",
    }


def _find_session(session_id=None, ticket_id=None, secret=None):
    _gc_oauth()
    with _OAUTH_LOCK:
        if session_id:
            return _OAUTH_SESSIONS.get(session_id)
        if secret:
            for session in _OAUTH_SESSIONS.values():
                if session.secret == secret and session.status == "pending":
                    return session
            return None
        if ticket_id:
            for session in _OAUTH_SESSIONS.values():
                if session.ticket_id == ticket_id and session.status == "pending":
                    return session
            return None
        pending = [s for s in _OAUTH_SESSIONS.values() if s.status == "pending"]
        pending.sort(key=lambda s: s.created_at, reverse=True)
        return pending[0] if pending else None


class _CredentialHolder(object):
    """只带 credential() 的轻量对象，给 signed_get 查身份用。"""

    def __init__(self, access_key_id, secret_access_key, security_token):
        self._cred = {
            "access_key_id": access_key_id or "",
            "secret_access_key": secret_access_key or "",
            "security_token": security_token or "",
        }

    def credential(self):
        return dict(self._cred)


def identity_from_refresh_token(refresh_token):
    """refresh_token 是 JWT 时取 sub；portal 回调不带身份时的兜底。"""
    parts = str(refresh_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = json.loads(crypto.b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("sub") or "")


def lookup_identity(creds):
    """用刚拿到的 STS 凭证查身份：caller-identity → current/user。

    caller-identity 有时只给 principal_id/account_id，不给人名；此时继续查
    current/user 把昵称补上，避免账号表里显示一串 UID。
    """
    holder = _CredentialHolder(creds.get("access_key_id"),
                               creds.get("secret_access_key"),
                               creds.get("security_token"))
    first_uid = first_name = first_domain = ""
    try:
        data = signed_get(holder, STS_HOST + "/v5/caller-identity", timeout=10)
        first_uid = str(data.get("principal_id") or data.get("account_id") or "")
        urn = str(data.get("principal_urn") or "")
        first_name = urn.rsplit("/", 1)[-1] if "/" in urn else ""
        first_domain = str(data.get("account_id") or "")
        if first_uid and first_name and first_name != first_uid:
            return first_uid, first_name, first_domain
    except CodeArtsError:
        pass
    try:
        data = signed_get(holder, SNAP_MANAGER + "/v1/current/user", timeout=10)
        uid = str(data.get("user_id") or "")
        name = str(data.get("user_name") or "")
        domain = str(data.get("domain_id") or "")
        if uid or first_uid:
            return (first_uid or uid,
                    name or first_name,
                    domain or first_domain)
    except CodeArtsError:
        pass
    return first_uid, first_name, first_domain


def _save_login(config, session, token):
    uid = str(token.get("user_id") or "")
    name = str(token.get("user_name") or "")
    domain = str(token.get("domain_id") or "")
    creds = token.get("credentials") or {}
    if not uid or not name:
        # portal 回调只回 code，token 响应通常不带身份；用 STS 凭证补查。
        looked_uid, looked_name, looked_domain = lookup_identity(creds)
        uid = uid or looked_uid
        name = name or looked_name
        domain = domain or looked_domain
    if not uid:
        uid = identity_from_refresh_token(token.get("refresh_token") or "")
    if not uid:
        # 最后兜底：refresh_token 的稳定哈希，避免写出 codearts-unknown.json。
        import hashlib
        uid = "codearts-" + hashlib.sha256(
            str(token.get("refresh_token") or "").encode("utf-8")).hexdigest()[:12]
    if not name:
        name = uid
    auth = {
        "user_id": uid,
        "user_name": name,
        "domain_id": domain,
        "cloud_dragon_token": token["credentials"]["security_token"],
        "access_key_id": token["credentials"]["access_key_id"],
        "secret_access_key": token["credentials"]["secret_access_key"],
        "expiration": token["credentials"]["expiration"],
        "refresh_token": token.get("refresh_token") or "",
        "code_verifier": session.verifier,
        "client_id": getattr(session, "client_id", CLIENT_ID),
        "client_type": getattr(session, "client_type", DEFAULT_CLIENT_TYPE),
        "dpop_private_key": session.dpop_jwk,
        "updated_at": int(time.time()),
    }
    slot_id = str(getattr(session, "slot_id", "") or "")
    if slot_id:
        auth["proxySlot"] = slot_id
    auth = save_auth(config["accounts_dir"], auth)
    pool_for(config["accounts_dir"]).add(auth)
    session.status = "done"
    session.message = "登录成功：%s" % auth["user_name"]
    session.account = {"uid": uid, "nickname": auth["user_name"]}
    return auth


def _exchange_session(config, session, code):
    token = exchange_code(code, session.verifier, session.port,
                          session.dpop_jwk, session.client_id,
                          proxy=getattr(session, "proxy", ""))
    return _save_login(config, session, token)


def oauth_poll(config, session_id):
    session = _find_session(session_id=session_id)
    if session is None:
        raise CodeArtsNotFound("会话不存在或已过期，请重新发起授权")
    if session.status in ("done", "error"):
        return session.public()
    if session.ticket_fallback:
        try:
            token = poll_ticket(session.ticket_id, session.secret,
                                plugin_name=session.plugin_name,
                                plugin_version=session.plugin_version,
                                proxy=getattr(session, "proxy", ""))
        except CodeArtsError as exc:
            session.status = "error"
            session.message = str(exc)
            return session.public()
        if token.get("user_id") and token["credentials"]["security_token"]:
            _save_login(config, session, token)
        else:
            session.message = "等待浏览器完成登录…"
    return session.public()


def _loopback_host(host):
    return str(host or "").lower() in ("127.0.0.1", "localhost", "::1")


def _safe_oauth_redirect(url):
    try:
        target = urllib.parse.urlsplit(str(url or ""))
        portal = urllib.parse.urlsplit(PORTAL_HOST)
        return (target.scheme in ("http", "https") and target.scheme == portal.scheme
                and target.hostname == portal.hostname and target.port == portal.port
                and not target.username and not target.password)
    except ValueError:
        return False


def oauth_callback(config, session_id, callback_url):
    session = _find_session(session_id=session_id)
    if session is None:
        raise CodeArtsNotFound("会话不存在或已过期，请重新发起授权")
    parsed = urllib.parse.urlsplit(str(callback_url or "").strip())
    if (parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path != REDIRECT_PATH or not _loopback_host(parsed.hostname)):
        raise CodeArtsBadRequest("只接受华为返回的 127.0.0.1 OAuth 回调地址")
    if parsed.port and session.port and parsed.port != session.port:
        raise CodeArtsBadRequest("回调端口与当前授权会话不匹配")
    query = urllib.parse.parse_qs(parsed.query)
    code = (query.get("code") or [""])[0]
    secret = (query.get("secret") or [""])[0]
    redirect = (query.get("redirect") or [""])[0]
    if code:
        _exchange_session(config, session, code)
        return session.public()
    if secret and redirect:
        if not _safe_oauth_redirect(redirect):
            raise CodeArtsBadRequest("授权跳转地址不属于 CodeArts 门户")
        session.secret = secret
        session.ticket_fallback = True
        session.message = "回调已接收，继续等待授权结果…"
        return {
            "ok": True, "status": "continue", "next_url": redirect,
            "message": session.message,
        }
    raise CodeArtsBadRequest("回调地址缺少 code 或 secret")


def oauth_callback_get(config, query):
    """浏览器 GET /oauth/callback 的处理；返回 HTML。

    官方 IDE 使用 `auth_callback_url` 时，回调只带 code 或 secret，可能没有
    `state`。因此先做 ticket_id / state 精确匹配；只有一个待授权会话时允许
    直接采用它；存在多个待授权会话时仍然拒绝，避免把授权码兑换到别的登录。

    华为云也会把授权链接里的 `ticket_id` 改写成 `state` 再送回来
    （实测：授权 URL 带 `ticket_id=<32 hex>`，回调带 `state=<hex>.pkg`），
    所以两个名字都认。
    """
    def value(key):
        raw = query.get(key)
        if isinstance(raw, list):
            return raw[0] if raw else ""
        return raw or ""
    ticket_id, callback_secret = value("ticket_id"), value("secret")
    state = value("state")
    code = value("code")
    if not ticket_id and not callback_secret and not state and not code:
        return 400, "<h3>回调缺少会话标识，请回到看板粘贴完整回调地址。</h3>"
    session = None
    if ticket_id or callback_secret:
        session = _find_session(ticket_id=ticket_id, secret=callback_secret)
    if session is None and state:
        # 华为云的回调形态：ticket_id 被改名成 state。逐级尝试，
        # 全部精确匹配，不做「随便找一个 pending」的猜测。
        for candidate in _state_candidates(state):
            session = _find_session(ticket_id=candidate)
            if session is not None:
                break
    if session is None and (code or callback_secret):
        # 官方 IDE 的 auth_callback_url 只回 code 或 secret，不带 state。
        # 浏览器回调只能确定一个待授权会话时采用它；有多个会话时拒绝猜测。
        with _OAUTH_LOCK:
            pending = [s for s in _OAUTH_SESSIONS.values()
                       if s.status == "pending"]
        if len(pending) == 1:
            session = pending[0]
    if session is None:
        return 400, "<h3>登录会话不存在或已过期，请回到看板重新发起。</h3>"
    code = value("code")
    secret = value("secret")
    redirect = value("redirect")
    try:
        if code:
            _exchange_session(config, session, code)
            return 200, "<h3>CodeArts 登录成功，可以关闭此页面。</h3>"
        if secret and redirect:
            if not _safe_oauth_redirect(redirect):
                raise CodeArtsBadRequest("授权跳转地址不属于 CodeArts 门户")
            session.secret = secret
            session.ticket_fallback = True
            session.message = "回调已接收，继续等待授权结果…"
            import html as html_module
            html = ('<meta http-equiv="refresh" content="0;url=%s">'
                    '<h3>正在继续华为云授权…</h3>' % html_module.escape(redirect, quote=True))
            return 200, html
    except CodeArtsError as exc:
        session.status = "error"
        session.message = str(exc)
        import html as html_module
        return 400, "<h3>登录失败：%s</h3>" % html_module.escape(str(exc))
    return 400, "<h3>回调地址缺少 code 或 secret。</h3>"


def _state_candidates(state):
    """把回调的 `state` 展开成若干可能的 ticket_id，按可信度排列。

    实测华为云返回的 `state` 是 `<ticket_id>.pkg` 形状（ticket_id 为 32 位
    hex），同时也见过原样返回。这里两种形状都给出来，由 `_find_session()`
    做精确匹配——一个都匹配不上就返回 None，绝不会登进错误的会话。
    """
    text = str(state or "").strip()
    if not text:
        return []
    out = []
    head = text.split(".", 1)[0]
    if head and head != text:
        out.append(head)
    out.append(text)
    # 去重但保序；ticket_id 只可能是 hex，非 hex 的候选留着也无害（匹配不上）。
    seen = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


# ---------------------------------------------------------------------------
# 管理 API（本地账号池）
# ---------------------------------------------------------------------------

def _credit_metric(metrics, name):
    for item in metrics or ():
        if isinstance(item, dict) and item.get("name") == name:
            return {
                "amount": float(item.get("package_credit_amount") or 0),
                "used": float(item.get("package_credit_used") or 0),
                "remain": float(item.get("package_credit_remain") or 0),
                "expiring": float(item.get("package_credit_expiring_amount") or 0),
            }
    return None


def _statistics_headers(acct, url):
    """按账号身份复现官方客户端读取 statistics/plugin 的请求头：签名集合、令牌头写法与
    签名后补上的传输头都取自 `CLIENT_TYPE_PROFILES[...]["statistics"]`。"""
    profile = _account_profile(acct)["statistics"]
    cred = acct.credential()
    headers = crypto.sign_request(
        "GET", url, profile["signed"], b"",
        cred["access_key_id"], cred["secret_access_key"], cred["security_token"],
        content_sha256=False, security_token_header=profile["token_header"])
    headers.update(profile["unsigned"])
    return headers


def fetch_account_info(acct, timeout=15.0):
    """查账户/套餐/积分：`snap-manager/v1/statistics/plugin`，与官方客户端同一个请求。"""
    url = SNAP_MANAGER + EP_PACKAGE_INFO
    signed = _statistics_headers(acct, url)
    data = _json_http("GET", url, signed, None, timeout, acct.proxy)
    package = data.get("package") or {}
    metrics = data.get("metrics") or []
    credits = {
        "total": _credit_metric(metrics, "usageTotalPackageCredit"),
        "basic": _credit_metric(metrics, "usageBasicPackageCredit"),
        "on_demand": _credit_metric(metrics, "usageOnDemandPackageCredit"),
        "bonus": _credit_metric(metrics, "usageBonusPackageCredit"),
        "unit": "credit",
    }
    models = []
    for item in data.get("model_metrics") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("model_id") or "")
        if not mid:
            continue
        models.append({
            "id": mid,
            "name": str(item.get("model_name") or mid),
            "description": str(item.get("model_desc") or ""),
            "type": str(item.get("model_type") or ""),
            "enabled": item.get("enable") is not False,
            "status": str(item.get("status") or ""),
            "luxury": bool(item.get("is_luxury")),
        })
    info = {
        "is_credit_package": package.get("is_credit_package") is True,
        "is_token_package": package.get("is_token_package") is True,
        "spec_code": str(package.get("spec_code") or ""),
        "package_name": str(package.get("package_name_cn")
                            or package.get("package_name_en") or ""),
        "package_status": str(package.get("status") or ""),
        "start_date": str(data.get("start_date") or ""),
        "end_date": str(data.get("end_date") or ""),
        "credits": credits,
        "models": models,
        "fetched_at": time.time(),
    }
    acct.info = info
    acct.info_fetched_at = time.time()
    # 今日积分消耗按「当天第一份快照的 used」算增量，基线要落盘才能跨重启成立。
    if acct.record_credit_baseline():
        acct.pool.save_state()
    return info


def ensure_account_info(acct, force=False, timeout=15.0):
    if acct.info and not force and time.time() - acct.info_fetched_at < 60.0:
        return acct.info
    with acct._info_lock:
        if acct.info and not force and time.time() - acct.info_fetched_at < 60.0:
            return acct.info
        try:
            return fetch_account_info(acct, timeout=timeout)
        except CodeArtsError:
            if acct.info:
                return acct.info
            raise


def configured_config(config):
    """渠道是否已有可用账号；没有账号时请求自然不可用，不另设开关。"""
    if not config:
        return False
    try:
        pool = pool_for(config["accounts_dir"])
    except Exception:
        return False
    return _has_usable_account(pool)


def _log(message, level="INFO"):
    """账号管理动作写进网关日志；测试里没有网关时不写。"""
    import wb_host
    gateway = wb_host.loaded_gateway("log")
    if gateway is not None:
        gateway.log(message, level=level, tag=ACCOUNT_LOG_TAG)


def enable(config, uid):
    if not pool_for(config["accounts_dir"]).enable(uid):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s enabled" % str(uid)[:8])
    return {"ok": True, "message": "已启用 %s" % uid}


def disable(config, uid, reason="manual disable"):
    if not pool_for(config["accounts_dir"]).disable(uid, reason):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s disabled" % str(uid)[:8])
    return {"ok": True, "message": "已停用 %s" % uid}


def remove(config, uid):
    if not pool_for(config["accounts_dir"]).remove(uid):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s deleted" % str(uid)[:8])
    return {"ok": True, "deleted": True, "message": "已删除 %s" % uid}


def clear_cooldown(config, uid):
    if not pool_for(config["accounts_dir"]).clear_cooldown(uid):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s penalties cleared" % str(uid)[:8])
    return {"ok": True, "message": "已清冷却 %s" % uid}


def set_note(config, uid, note):
    pool = pool_for(config["accounts_dir"])
    if not pool.set_note(uid, note):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s note set (%d chars)"
         % (str(uid)[:8], len(pool.get(uid).note or "")))
    return {"ok": True, "uid": uid, "note": pool.get(uid).note}


def set_tasks_enabled(config, uid, enabled):
    pool = pool_for(config["accounts_dir"])
    if not pool.set_tasks_enabled(uid, enabled):
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s tasks %s"
         % (str(uid)[:8], "enabled" if enabled else "disabled"))
    return {"ok": True, "uid": uid,
            "tasks_enabled": bool(pool.get(uid).tasks_enabled)}


def set_priority(config, uid, priority):
    try:
        value = int(priority)
    except (TypeError, ValueError):
        raise CodeArtsBadRequest("priority must be an integer")
    if not 0 <= value <= MAX_ACCOUNT_PRIORITY:
        raise CodeArtsBadRequest("priority must be between 0 and %d"
                                 % MAX_ACCOUNT_PRIORITY)
    pool = pool_for(config["accounts_dir"])
    acct = pool.set_priority(uid, value)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s priority set to %d" % (str(uid)[:8], value))
    return {"ok": True, "uid": uid, "priority": acct.priority}


def set_client_type(config, uid, client_type):
    value = str(client_type or "").strip().lower()
    if value not in CLIENT_TYPES:
        raise CodeArtsBadRequest("client_type must be one of: %s"
                                 % ", ".join(CLIENT_TYPES))
    pool = pool_for(config["accounts_dir"])
    acct = pool.set_client_type(uid, value)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s identity -> %s" % (str(uid)[:8], value))
    return {"ok": True, "uid": uid, "client_type": acct.effective_client_type()}


def _credit_remain(acct):
    credits = acct.credits_view()
    return None if credits is None else credits.get("remain")


def refresh_credential(config, uid):
    """强制换一次令牌；失败只报错，不停用账号。"""
    pool = pool_for(config["accounts_dir"])
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    error = ""
    try:
        _ensure_token(acct, force=True)
    except CodeArtsError as exc:
        error = str(exc)[:300]
        acct.last_error = error[:200]
        pool.save_state()
    _log("codearts account %s: 手动刷新凭据 %s"
         % (str(acct.uid)[:8], "成功" if not error else "失败: " + error),
         level="INFO" if not error else "WARN")
    exp = acct.expires_at()
    return {"ok": not error, "uid": acct.uid, "error": error,
            "expiresAt": int(exp) if exp else None}


def refresh_credits(config, uids=None):
    """刷新积分快照，返回每个账号一行；余额增加时写一条积分流水。"""
    pool = pool_for(config["accounts_dir"])
    wanted = {str(uid) for uid in uids} if uids is not None else None
    rows = []
    seen = set()
    for acct in list(pool.accounts):
        if wanted is not None and acct.uid not in wanted:
            continue
        # 显式指定 uid 是操作者的动作；整批刷新才跳过停用与不参与任务的账号。
        if wanted is None and (acct.disabled or not acct.tasks_enabled):
            continue
        if wanted is not None:
            seen.add(acct.uid)
        before = _credit_remain(acct)
        ok = False
        error = ""
        try:
            _ensure_token(acct)
            ensure_account_info(acct, force=True, timeout=15.0)
            ok = True
        except CodeArtsError as exc:
            error = str(exc)[:300]
        after = _credit_remain(acct)
        if ok and before is not None and after is not None and after > before:
            wb_accounts.record_credit_event(acct, before, after)
        rows.append({"uid": acct.uid, "ok": ok,
                     "credits": acct.credits_view() if ok else None,
                     "error": error})
    missing = sorted(wanted - seen) if wanted is not None else []
    for uid in missing:
        rows.append({"uid": uid, "ok": False, "credits": None,
                     "error": "account not found"})
    if rows:
        _log("codearts credits refreshed: %d accounts, %d failed"
             % (len(rows), sum(1 for row in rows if not row["ok"])),
             level="INFO" if all(row["ok"] for row in rows) else "WARN")
    return rows


def _test_model(acct):
    """测试用最便宜的可服务模型：优先非福利模型，其次已知模型。"""
    disallowed = set(acct.disabled_models or ())
    for item in acct.models or ():
        mid = str(item.get("id") or "")
        if mid and mid.lower() not in disallowed and not item.get("benefit"):
            return mid
    for seed in SEED_KNOWN_MODELS:
        if seed.lower() not in disallowed:
            return seed
    return "GLM-5.2"


def _stream_reply(stream):
    """把归一化后的 SSE 片段拼回完整回复文本。"""
    parts = []
    for raw in stream:
        text = raw.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            continue
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        delta = (choices[0] or {}).get("delta") or {}
        piece = delta.get("content")
        if piece:
            parts.append(str(piece))
    return "".join(parts)


def test_account(config, uid, model=None):
    """真实测试：给这个账号发一条最小对话；成功即清掉它的本地惩罚。"""
    pool = pool_for(config["accounts_dir"])
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    model = str(model or "").strip()
    try:
        ensure_models(acct, timeout=15.0)
    except CodeArtsError:
        pass
    # 设置里指定了测试模型时就只用它：这个账号的目录里没有时如实报错，
    # 不退回账号自己的默认模型。
    if model and acct.models and not _model_available(acct, model):
        return {"ok": False, "uid": acct.uid, "model": model, "reply": "",
                "elapsed_ms": 0,
                "error": "账号的模型目录里没有 %s，请在设置里换一个测试模型。"
                         % model}
    if not model:
        model = _test_model(acct)
    body = build_chat_body({
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }, "codearts-test-%s" % acct.uid)
    started = time.time()
    row = {"ok": False, "uid": acct.uid, "model": model, "reply": "",
           "elapsed_ms": 0, "error": ""}
    stream = None
    # 测试要能诊断冷却中的账号，因此绕过本地守卫占一个名额，由 close() 归还。
    pool.acquire(acct, force=True)
    try:
        _ensure_token(acct)
        try:
            ensure_models(acct, timeout=15.0)
        except CodeArtsError:
            pass
        stream, used = _client_chat(acct, body, "codearts-test-%s" % acct.uid)
        row["model"] = used
        row["reply"] = _stream_reply(stream)[:200]
        row["ok"] = True
    except CodeArtsError as exc:
        row["error"] = str(exc)[:300]
        acct.last_error = row["error"][:200]
        acct.last_error_detail = str(exc)[:2000]
        pool.save_state()
    finally:
        if stream is not None:
            stream.close()          # 同时归还上面占下的名额
        else:
            pool.release(acct)
    row["elapsed_ms"] = int((time.time() - started) * 1000)
    if row["ok"]:
        pool.clear_penalties(acct)
    return row


def apply_proxy_slots(accounts_dir, slots=None):
    """Re-resolve CodeArts account exits after the panel saves slots."""
    pool_for(accounts_dir).apply_proxy_slots(slots)


def resolve_slot(accounts_dir, slot_id):
    """把面板给的出口值解析成持久化的槽 id。

    "auto" 挑一个既没绑定账号、也被启用的槽；没有空闲槽时直连。未知槽直接
    报错，避免把出口静默改回直连；已停用的槽仍可绑定，重新启用后自动生效。
    """
    value = str(slot_id or "").strip()
    if not value:
        return ""
    entries = wb_settings.proxy_slots(accounts_dir)
    if value == "auto":
        pool = pool_for(accounts_dir)
        bound = {acct.proxy_slot for acct in pool.accounts if acct.proxy_slot}
        for entry in entries:
            if entry.get("enabled", True) and entry["id"] not in bound:
                return entry["id"]
        return ""
    for entry in entries:
        if entry["id"] == value:
            return value
    raise ValueError("unknown proxy slot")


def set_proxy_slot(config, uid, slot_id):
    """Bind one CodeArts account to a proxy slot (empty string = direct)."""
    pool = pool_for(config["accounts_dir"])
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    slot_id = resolve_slot(pool.accounts_dir, slot_id)
    acct.proxy_slot = slot_id
    acct.auth["proxySlot"] = slot_id
    acct.auth = save_auth(pool.accounts_dir, acct.auth)
    pool.apply_proxy_slots()
    _log("codearts account %s proxy slot set to %s"
         % (str(uid)[:8], slot_id or "(direct)"))
    return {
        "ok": True,
        "uid": acct.uid,
        "proxy_slot": acct.proxy_slot,
        "proxy": acct.proxy,
        "message": "出口已更新",
    }


def drop_missing_bindings(accounts_dir, slots):
    """Clear CodeArts bindings that point at a slot which no longer exists."""
    valid = {entry["id"] for entry in slots}
    pool = pool_for(accounts_dir)
    changed = 0
    for acct in list(pool.accounts):
        if acct.proxy_slot and acct.proxy_slot not in valid:
            acct.proxy_slot = ""
            acct.auth.pop("proxySlot", None)
            acct.auth = save_auth(accounts_dir, acct.auth)
            changed += 1
    # Always re-resolve: a surviving slot may have had its URL edited.
    pool.apply_proxy_slots(slots)
    return changed


def _checkin_limits(config):
    """签到的尝试次数与重试间隔；配置缺失或坏值时用默认值。"""
    try:
        max_attempts = int((config or {}).get("checkin_max_attempts")
                           or DEFAULTS["checkin_max_attempts"])
    except (TypeError, ValueError):
        max_attempts = int(DEFAULTS["checkin_max_attempts"])
    raw_gap = (config or {}).get("checkin_retry_gap_seconds")
    try:
        gap = float(DEFAULTS["checkin_retry_gap_seconds"]
                    if raw_gap is None else raw_gap)
    except (TypeError, ValueError):
        gap = float(DEFAULTS["checkin_retry_gap_seconds"])
    return max(1, max_attempts), max(0.0, gap)


def checkin_due(config, now=None):
    """今天还没签到、参与任务、且没有用完尝试次数或撞上重试间隔的账号。"""
    pool = pool_for(config["accounts_dir"])
    now = time.time() if now is None else now
    max_attempts, gap = _checkin_limits(config)
    return [acct for acct in list(pool.accounts)
            if not acct.disabled and acct.tasks_enabled
            and not acct.checked_in_today(now)
            and not acct.checkin_attempt_blocked(max_attempts, gap, now)]


def keepalive_due(config):
    """令牌即将过期或已过期的账号；只续期，不发消息。"""
    pool = pool_for(config["accounts_dir"])
    return [acct for acct in list(pool.accounts)
            if not acct.disabled and acct.tasks_enabled
            and (acct.expiring_soon() or acct.expired())]


def keepalive_account(config, uid):
    """续期一个账号；调度器可以在账号之间插入自己的抖动。"""
    pool = pool_for(config["accounts_dir"])
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    if acct.disabled or not acct.tasks_enabled:
        return {"uid": acct.uid, "ok": True, "refreshed": 0, "error": ""}
    if not (acct.expiring_soon() or acct.expired()):
        return {"uid": acct.uid, "ok": True, "refreshed": 0, "error": ""}
    try:
        _ensure_token(acct)
        return {"uid": acct.uid, "ok": True, "refreshed": 1, "error": ""}
    except CodeArtsError as exc:
        acct.last_error = str(exc)[:200]
        pool.save_state()
        return {"uid": acct.uid, "ok": False, "refreshed": 0,
                "error": str(exc)[:300]}


def keepalive(config, gap_sleep=None):
    """保活：给即将过期的账号换新令牌；停用与不参与任务的账号跳过。

    `gap_sleep` 在第二个及以后的账号之前调用一次，调度器用它落实各账号
    之间的共享抖动。
    """
    due = keepalive_due(config)
    refreshed = 0
    for index, acct in enumerate(due):
        if index and gap_sleep is not None:
            gap_sleep()
        row = keepalive_account(config, acct.uid)
        refreshed += int(row.get("refreshed") or 0)
    return {"ok": True, "refreshed": refreshed, "checked": len(due),
            "message": "已检查 %d 个账号，续期 %d 个" % (len(due), refreshed)}


def _credit_request(acct, method, path, payload=None, timeout=30.0):
    url = SNAP_HOST + path
    if method == "GET":
        return signed_get(acct, url, timeout=timeout, agent_type="PromptCenter")
    return signed_post(acct, url, payload or {}, timeout=timeout,
                       agent_type="PromptCenter")


def _is_checkin_campaign(item):
    """这条积分活动是不是「签到类」，值得领取。

    campaignId 每换季都会变，所以判据是活动类型与触发事件；历史 id 只作兼容。
    """
    if item.get("campaignId") in CHECKIN_CAMPAIGN_IDS:
        return True
    if str(item.get("type") or "").upper() in CHECKIN_CAMPAIGN_TYPES:
        return True
    return str((item.get("extra") or {}).get("triggerEvent")
               or "").lower() == "user.login"


_CHECKIN_LOCKS = {}
_CHECKIN_LOCKS_LOCK = threading.Lock()


def _checkin_lock(uid):
    """每个账号一把签到锁（进程内），面板点击与自动签到撞车时只放一个过去。"""
    with _CHECKIN_LOCKS_LOCK:
        lock = _CHECKIN_LOCKS.get(uid)
        if lock is None:
            lock = threading.Lock()
            _CHECKIN_LOCKS[uid] = lock
        return lock


def checkin_account(acct, timeout=None):
    """签到入口：同账号串行，避免两条路径同时 claim 同一条活动。"""
    if timeout is None:
        timeout = float((getattr(acct.pool, "config", {}) or {}).get(
            "checkin_timeout_seconds") or DEFAULTS["checkin_timeout_seconds"])
    lock = _checkin_lock(acct.uid)
    if not lock.acquire(False):
        return {"ok": False, "credits": 0.0, "campaigns": [],
                "message": "该账号签到正在进行中，稍后刷新即可"}
    try:
        return _checkin_account(acct, timeout=timeout)
    finally:
        lock.release()


def _checkin_account(acct, timeout=30.0):
    channel = _account_profile(acct)["checkin_channel"]
    data = _credit_request(acct, "GET",
                           EP_CREDIT_DELIVERY + "?channel=" + channel,
                           timeout=timeout)
    items = ((data.get("data") or {}).get("items") or [])
    results = []
    total_credits = 0.0
    for item in items:
        campaign_id = item.get("campaignId")
        if not _is_checkin_campaign(item):
            continue
        status = str(item.get("status") or "").strip()
        title = str(item.get("title") or ("积分活动 %s" % campaign_id))
        unit = item.get("benefitUnit")
        if unit and unit != "CREDIT":
            continue
        if status in ("CONFIRMED", "CONSUMED"):
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": True, "status": status, "message": "已领取"})
            continue
        if status == "ELIGIBLE":
            if not item.get("claimable"):
                results.append({"campaign_id": campaign_id, "title": title,
                                "ok": False, "status": status,
                                "message": "暂不可领取，稍后重试"})
                continue
            _credit_request(acct, "POST", EP_CREDIT_CLAIM, {
                "campaignId": campaign_id, "channel": channel,
                "idempotentKey": "claim_%s_%d" % (campaign_id, int(time.time() * 1000)),
            }, timeout=timeout)
            credits = float(item.get("pendingTotalAmount")
                            or item.get("benefitAmount") or 0)
            total_credits += credits
            status = "CLAIMED"
        if status in ("CLAIMED", "ELIGIBLE"):
            _credit_request(acct, "POST", EP_CREDIT_CONFIRM,
                            {"campaignId": campaign_id}, timeout=timeout)
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": True, "status": "CONFIRMED",
                            "credits": total_credits,
                            "message": "领取成功（%g 积分）" % total_credits
                            if total_credits else "已补确认"})
        elif status not in ("CONFIRMED", "CONSUMED"):
            results.append({"campaign_id": campaign_id, "title": title,
                            "ok": False, "status": status,
                            "message": "未获得活动资格"})
    message = "; ".join("%s: %s" % (r["title"], r["message"]) for r in results)
    if not message:
        message = "无可领取的签到或新手活动"
    result = {"ok": all(r.get("ok") for r in results) if results else True,
              "credits": total_credits, "message": message,
              "campaigns": results}
    before = _credit_remain(acct)
    acct.checkin = {"ok": result["ok"], "message": message,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "credits": float(total_credits)}
    acct.info_fetched_at = 0.0
    try:
        ensure_account_info(acct, force=True, timeout=timeout)
    except CodeArtsError:
        pass
    after = _credit_remain(acct)
    if before is not None and after is not None and after > before:
        wb_accounts.record_credit_event(acct, before, after)
    acct.pool.save_state()
    return result


def checkin(config, uid="", manual=False):
    """签到入口：`uid` 指定单个账号，留空时处理所有到期待签的账号。

    `manual=True` 给面板的单账号/整批点击用：操作者明确要求，就不再按尝试次数
    与重试间隔拦人。定时任务走默认值，次数与间隔都生效。
    """
    return _checkin(config, uid, manual=manual)


def _checkin(config, uid="", manual=False):
    pool = pool_for(config["accounts_dir"])
    if uid:
        # 显式指定账号：冷却与任务开关不拦，账号停用也不拦。
        acct = pool.get(uid)
        if acct is None:
            raise CodeArtsNotFound("account not found")
        if not manual:
            max_attempts, gap = _checkin_limits(config)
            now = time.time()
            if acct.checkin_attempt_blocked(max_attempts, gap, now):
                entry = acct._checkin_attempt_entry(now)
                if entry["count"] >= max_attempts:
                    message = ("今日签到尝试次数已用完（%d 次），明天再试"
                               % max_attempts)
                else:
                    message = ("距上次签到尝试不足 %d 秒，稍后再试"
                               % int(gap))
                return {"ok": False, "credits": 0.0, "campaigns": [],
                        "message": message}
        pool.note_checkin_attempt(acct)
        _ensure_token(acct)
        result = checkin_account(acct)
        _log("codearts account %s check-in: %s"
             % (str(acct.uid)[:8],
                result.get("message") if result.get("ok") else "failed"),
             level="INFO" if result.get("ok") else "WARN")
        return result
    results = []
    for acct in checkin_due(config):
        pool.note_checkin_attempt(acct)
        try:
            _ensure_token(acct)
            results.append({"uid": acct.uid, **checkin_account(acct)})
        except CodeArtsError as exc:
            results.append({"uid": acct.uid, "ok": False, "credits": 0.0,
                            "message": str(exc)})
    total = sum(float(r.get("credits") or 0) for r in results)
    if results:
        _log("codearts check-in: %d accounts, %g credits"
             % (len(results), total))
    return {"ok": all(r.get("ok") for r in results) if results else True,
            "credits": total,
            "message": "已处理 %d 个账号，共领取 %g 积分" % (len(results), total),
            "results": results}


# ---------------------------------------------------------------------------
# 统一账号 API（面板与 wb_channels 的提供方适配层调用）
# ---------------------------------------------------------------------------

#: `update` 接受的字段；与契约 §2.2 一致，其他键一律报错。
CHANGE_KEYS = ("enabled", "tasksEnabled", "priority", "weight",
               "concurrencyLimit", "group", "note", "disabledModels",
               "proxySlot", "identity")

#: 账号导出里属于凭据的字段；不带密钥导出时全部省略。
EXPORT_SECRET_KEYS = ("cloud_dragon_token", "access_key_id", "secret_access_key",
                      "refresh_token", "code_verifier", "dpop_private_key")

#: 号池守卫键，与 wb_settings 的分组一致。
GUARD_KEYS = ("reserve_credits", "daily_token_limit", "daily_credit_limit",
              "model_daily_token_limit", "expiring_window_days")


def _realm_matches(realm, account_realm="codearts"):
    return not realm or realm in ("all", account_realm)


def account_views(accounts_dir, realm=None):
    """统一账号视图列表；别的渠道的 realm 过滤得到空表。"""
    if not _realm_matches(realm):
        return []
    pool = pool_for(accounts_dir)
    return [acct.view() for acct in sorted(pool.accounts, key=lambda a: a.uid)]


def _validate_changes(accounts_dir, changes):
    """校验 `update` 请求体；未知或超范围的字段抛 ValueError。"""
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object")
    unknown = sorted(key for key in changes if key not in CHANGE_KEYS)
    if unknown:
        raise ValueError("unknown account field: %s" % ", ".join(unknown))
    out = {}
    for key, value in changes.items():
        if key in ("enabled", "tasksEnabled"):
            out[key] = bool(value)
        elif key == "priority":
            out[key] = _strict_number(key, value, 0, MAX_ACCOUNT_PRIORITY,
                                      integer=True)
        elif key == "weight":
            out[key] = _strict_number(key, value, 1,
                                      wb_accounts.MAX_ACCOUNT_WEIGHT,
                                      integer=True)
        elif key == "concurrencyLimit":
            out[key] = _strict_number(key, value, MIN_CONCURRENCY_LIMIT,
                                      MAX_CONCURRENCY_LIMIT, integer=True)
        elif key == "group":
            if value is not None and not isinstance(value, str):
                raise ValueError("group must be a string")
            out[key] = str(value or "").strip()[:wb_accounts.MAX_ACCOUNT_GROUP]
        elif key == "note":
            if value is not None and not isinstance(value, str):
                raise ValueError("note must be a string")
            text = str(value or "").strip()
            if len(text) > wb_accounts.MAX_ACCOUNT_NOTE:
                raise ValueError("note must be at most %d characters"
                                 % wb_accounts.MAX_ACCOUNT_NOTE)
            out[key] = text
        elif key == "disabledModels":
            out[key] = _normalise_models(value)
        elif key == "proxySlot":
            out[key] = resolve_slot(accounts_dir, value)
        elif key == "identity":
            text = str(value or "").strip().lower()
            if text not in CLIENT_TYPES:
                raise ValueError("identity must be one of: %s"
                                 % ", ".join(CLIENT_TYPES))
            out[key] = text
    if not out:
        raise ValueError("nothing to update: pass 'enabled', 'tasksEnabled', "
                         "'priority', 'weight', 'concurrencyLimit', 'group', "
                         "'note', 'disabledModels', 'proxySlot' or 'identity'")
    return out


def update_account(accounts_dir, uid, changes):
    pool = pool_for(accounts_dir)
    acct = pool.get(uid)
    if acct is None:
        raise CodeArtsNotFound("account not found")
    clean = _validate_changes(accounts_dir, changes)
    updated = pool.update(acct.uid, clean)
    if updated is None:
        raise CodeArtsNotFound("account not found")
    _log("codearts account %s updated: %s"
         % (str(uid)[:8], ", ".join(sorted(clean))))
    return updated.view()


def set_all_enabled(config, enabled, realm=None, group=None):
    """整池或整组启停，返回真正变了的 uid。"""
    pool = pool_for(config["accounts_dir"])
    changed = pool.set_all_enabled(enabled, realm=realm, group=group)
    if changed:
        _log("codearts accounts %s: %d accounts"
             % ("enabled" if enabled else "disabled", len(changed)))
    return changed


def export_rows(accounts_dir, uids=None, include_secrets=True):
    """导出账号行：凭证字段加面板字段，行上带 provider 与 realm。"""
    pool = pool_for(accounts_dir)
    wanted = {str(uid) for uid in uids} if uids is not None else None
    rows = []
    for acct in sorted(pool.accounts, key=lambda a: a.uid):
        if wanted is not None and acct.uid not in wanted:
            continue
        row = {
            "provider": "codearts",
            "realm": "codearts",
            "uid": acct.uid,
            "nickname": acct.nickname,
            "domain_id": acct.auth.get("domain_id") or "",
            "client_id": acct.auth.get("client_id") or CLIENT_ID,
            "client_type": acct.effective_client_type(),
            "expiration": acct.auth.get("expiration") or "",
            "enabled": not acct.disabled,
            "tasksEnabled": bool(acct.tasks_enabled),
            "group": acct.group,
            "priority": int(acct.priority),
            "weight": int(acct.weight),
            "concurrencyLimit": int(acct.concurrency_limit),
            "disabledModels": sorted(acct.disabled_models),
            "note": acct.note,
            "proxySlot": acct.proxy_slot,
        }
        for key in EXPORT_SECRET_KEYS:
            if key not in acct.auth:
                continue
            if not include_secrets:
                continue
            value = acct.auth.get(key)
            row[key] = copy.deepcopy(value) if isinstance(value, dict) else value
        rows.append(row)
    return rows


def _auth_from_row(row):
    """把一行导出数据还原成 (凭证档, 面板字段)；缺凭据时抛 ValueError。"""
    uid = str(row.get("uid") or row.get("user_id") or "").strip()
    if not uid:
        raise ValueError("cannot determine uid (no uid field)")
    missing = []
    for key in ("cloud_dragon_token", "access_key_id", "secret_access_key",
                "refresh_token"):
        if not str(row.get(key) or "").strip():
            missing.append(key)
    dpop = row.get("dpop_private_key")
    if not isinstance(dpop, dict) or not dpop.get("d"):
        missing.append("dpop_private_key")
    if missing:
        raise ValueError("missing credential fields: %s（导出时需包含密钥）"
                         % ", ".join(missing))
    auth = {
        "user_id": uid,
        "user_name": str(row.get("nickname") or row.get("user_name") or uid),
        "domain_id": str(row.get("domain_id") or ""),
        "cloud_dragon_token": str(row.get("cloud_dragon_token") or ""),
        "access_key_id": str(row.get("access_key_id") or ""),
        "secret_access_key": str(row.get("secret_access_key") or ""),
        "expiration": str(row.get("expiration") or ""),
        "refresh_token": str(row.get("refresh_token") or ""),
        "code_verifier": str(row.get("code_verifier") or ""),
        "client_id": str(row.get("client_id") or CLIENT_ID),
        "client_type": normalize_client_type(
            row.get("client_type") or row.get("identity")),
        "dpop_private_key": copy.deepcopy(dpop),
        "updated_at": int(time.time()),
    }
    panel = {}
    for key in ("enabled", "tasksEnabled", "priority", "weight",
                "concurrencyLimit", "group", "note", "disabledModels",
                "proxySlot"):
        if key in row:
            panel[key] = row[key]
    if "identity" in row and "client_type" not in row:
        panel["identity"] = row["identity"]
    return auth, panel


def import_rows(config, rows, dry_run=False, overwrite=False):
    """导入账号行；返回 {"imported", "skipped", "errors"}。"""
    accounts_dir = config["accounts_dir"]
    pool = pool_for(accounts_dir)
    imported, skipped, errors = [], [], []
    seen = set()
    for index, row in enumerate(rows or ()):
        if not isinstance(row, dict):
            errors.append({"index": index + 1, "reason": "row must be an object"})
            continue
        provider = str(row.get("provider") or "").strip().lower()
        if provider and provider != "codearts":
            skipped.append({"uid": str(row.get("uid") or ""),
                            "reason": "belongs to %s" % provider})
            continue
        try:
            auth, panel = _auth_from_row(row)
        except ValueError as exc:
            errors.append({"index": index + 1, "reason": str(exc)})
            continue
        uid = auth["user_id"]
        if uid in seen:
            skipped.append({"uid": uid, "reason": "duplicate inside the document"})
            continue
        seen.add(uid)
        existing = pool.get(uid) is not None
        if existing and not overwrite:
            skipped.append({"uid": uid, "reason": "already exists"})
            continue
        # 槽 id 是本机配置：外面导进来的绑定在本机不存在时按直连处理。
        if panel.get("proxySlot"):
            try:
                panel["proxySlot"] = resolve_slot(accounts_dir, panel["proxySlot"])
            except ValueError:
                panel["proxySlot"] = ""
        try:
            changes = _validate_changes(accounts_dir, panel)
        except ValueError as exc:
            errors.append({"index": index + 1, "reason": str(exc)})
            continue
        imported.append({"uid": uid, "action": "updated" if existing else "added"})
        if dry_run:
            continue
        saved = save_auth(accounts_dir, auth)
        acct = pool.add(saved)
        if acct is None:
            errors.append({"index": index + 1, "reason": "account was not added"})
            imported.pop()
            continue
        pool.update(uid, changes)
    if not dry_run and imported:
        _log("codearts account import: %d imported, %d skipped, %d invalid"
             % (len(imported), len(skipped), len(errors)))
    return {"imported": imported, "skipped": skipped, "errors": errors}


def apply_pool_guards(config, refresh=False):
    """把号池守卫推进 CodeArts 账号：限额按渠道读设置，用量来自日志与余额快照。

    日积分按「当天第一份套餐快照的 used」算增量，落在账号状态里；日 Token 与按模型
    Token 来自用量日志，与 WorkBuddy 同一份计数。日志读不出时保持上次计数不动。
    """
    accounts_dir = config["accounts_dir"]
    pool = pool_for(accounts_dir)
    values = wb_settings.limit_values_many(accounts_dir, GUARD_KEYS)
    wants = any(
        any(int(value or 0) > 0 for value in values[key].values())
        for key in ("daily_token_limit", "daily_credit_limit",
                    "model_daily_token_limit"))
    tokens = models = None
    if wants:
        import wb_usage
        stats = wb_usage.daily_usage_stats(ttl=0 if refresh else None)
        if stats is not None:
            tokens = stats["tokens"]
            models = stats["models"]
    with pool._lock:
        accounts = list(pool.accounts)
    for acct in accounts:
        acct.reserve_credits = _realm_value(values["reserve_credits"], acct.realm)
        acct.expiring_window_days = _realm_value(
            values["expiring_window_days"], acct.realm)
        acct.daily_token_limit = _realm_value(
            values["daily_token_limit"], acct.realm)
        acct.daily_credit_limit = _realm_value(
            values["daily_credit_limit"], acct.realm)
        acct.model_daily_token_limit = _realm_value(
            values["model_daily_token_limit"], acct.realm)
        if tokens is not None:
            acct.daily_tokens_today = tokens.get(acct.uid)
        if models is not None:
            acct.model_daily_tokens = models.get(acct.uid) or {}
    _push_credit_rates(pool)
    return len(GUARD_KEYS)


def _push_credit_rates(pool, rates=None):
    """把今日积分消耗折算成每天的速度，供智能选路评分。

    CodeArts 不上报按请求的积分，今日消耗只能来自套餐 `used` 的日增量；`rates`
    是其它提供方从用量日志算出的映射，CodeArts 忽略它，始终用本地快照。
    """
    import wb_usage
    scale = wb_usage.credit_rate_scale()
    with pool._lock:
        accounts = list(pool.accounts)
    for acct in accounts:
        try:
            today = acct.daily_credits_today
            acct.credit_rate_per_day = float(today or 0.0) * scale
        except (TypeError, ValueError):
            acct.credit_rate_per_day = 0.0
    return len(accounts)


def apply_routing_inputs(config, rates=None):
    """把账号的积分消耗速度推进池子；`rates` 只对用量日志口径的提供方有意义。"""
    return _push_credit_rates(pool_for(config["accounts_dir"]), rates=rates)

