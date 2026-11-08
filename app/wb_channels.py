# 渠道登记表与提供方适配层。
#
# 渠道是客户端能选的出口，提供方是渠道背后的账号池与上游协议。路由是参与混合调度的渠道集合
# （Key 绑定、X-Realm、面板默认出口），没有先后之分：同一个模型由几个渠道提供时，按账号的
# 优先级、在途请求数与权重在这些渠道的全部账号里挑一个。同一提供方的渠道共用账号与模型 id，
# 不能出现在同一条路由里。网关的路由、看板与设置只认这里的登记表：新增提供方时实现一个
# Provider 并登记渠道，调用方不需要再写 if realm == ... 的分支。看板经 GET /realm 读到同一份
# 渠道声明。
#
# 两个提供方对外提供同一套账号方法（accounts/find/views/update/delete/test/refresh_credential/
# refresh_credits/checkin/set_all_enabled/export/import_rows/ready_count/total_count），
# 面板只调用这些方法，不直接碰各自的账号模块。

import copy
import random
import socket
import threading
import time


class ChannelError(Exception):
    """提供方拒绝或无法完成请求；status 与 etype 直接用于 HTTP 应答。

    account_uid、wait、outcome 与 local_network 是给用量流水与限流应答用的：
    失败要归因到具体账号，限流要给出 Retry-After，本机断网不能算到账号头上。
    """

    def __init__(self, message, status=502, etype="api_error", account_uid=None,
                 wait=None, outcome="failed", local_network=False, detail=""):
        super(ChannelError, self).__init__(message)
        self.status = status
        self.etype = etype
        self.message = message
        self.detail = detail
        self.account_uid = account_uid
        self.wait = wait
        self.outcome = outcome
        self.local_network = local_network


#: 本机网络故障的判定标记。与 wb_proxy 共用同一套规则：登录、聊天与面板测试对
#: 「本机没网」的判断必须一致，否则同一个失败在一个入口冷却账号、在另一个入口不冷却。
LOCAL_NETWORK_MARKERS = (
    "gaierror", "getaddrinfo failed", "name or service not known",
    "nodename nor servname", "temporary failure in name resolution",
    "network is unreachable", "network is down", "no route to host",
    "errno -2", "errno -3", "errno -5", "errno 100", "errno 101", "errno 113",
    "winerror 11001", "winerror 10050", "winerror 10051", "winerror 10065",
)


def looks_like_local_network(exc):
    """这次失败是「本机没网」还是「上游出问题」：两者在账号治理上必须分开。"""
    if isinstance(exc, socket.gaierror):
        return True
    reason = getattr(exc, "reason", None)
    text = "%s %s %s" % (type(exc).__name__, exc, "" if reason is None else reason)
    lower = text.lower()
    return any(marker in lower for marker in LOCAL_NETWORK_MARKERS)


# features 的含义见看板 core/channels.js 的说明；这里只做声明，后端不按它分支。
CHANNELS = (
    {
        "id": "intl", "label": "国际版", "host": "www.workbuddy.ai",
        "icon": "Globe", "tone": "sky", "provider": "workbuddy",
        "features": {"checkin": "", "dailyChat": True, "testModel": True, "media": True},
    },
    {
        "id": "cn", "label": "国内版", "host": "www.workbuddy.cn",
        "icon": "House", "tone": "warning", "provider": "workbuddy",
        "features": {"checkin": "buddy", "dailyChat": False, "testModel": True, "media": True},
    },
    {
        # 没有可以写进括号的上游域名：标题直接用渠道名，避免出现「CodeArts（华为云 CodeArts）」。
        "id": "codearts", "label": "CodeArts", "host": "",
        "icon": "Cloud", "tone": "violet", "provider": "codearts",
        "features": {"checkin": "codearts", "dailyChat": False, "testModel": False, "media": False},
    },
)

_BY_ID = {channel["id"]: channel for channel in CHANNELS}

#: WorkBuddy 的出站身份选项，面板的身份下拉直接渲染。
WORKBUDDY_IDENTITIES = (
    {"value": "workbuddy", "label": "WorkBuddy"},
    {"value": "vscode", "label": "VS Code"},
    {"value": "cli", "label": "CLI"},
)


def ids():
    return tuple(channel["id"] for channel in CHANNELS)


def known(realm):
    return realm in _BY_ID


def label(realm):
    channel = _BY_ID.get(realm)
    return channel["label"] if channel else str(realm or "")


def public():
    """给看板的渠道声明，调用方可以随意修改返回值。"""
    return copy.deepcopy(list(CHANNELS))


def parse_route(value):
    """把路由写法解析成渠道 id 元组，按登记表顺序排列。接受列表或逗号分隔的字符串，空值得到
    空元组；未知渠道、重复渠道和同一提供方的两个渠道抛 ValueError。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError("路由必须是渠道 id 列表")
    route = []
    owners = {}
    for item in items:
        if not isinstance(item, str):
            raise ValueError("路由必须是渠道 id 列表")
        cid = item.strip().lower()
        if not cid:
            continue
        channel = _BY_ID.get(cid)
        if channel is None:
            raise ValueError("未知渠道：%s，可选 %s" % (cid, "、".join(ids())))
        if cid in route:
            raise ValueError("渠道 %s 重复出现" % channel["label"])
        other = owners.get(channel["provider"])
        if other:
            raise ValueError("“%s”与“%s”属于同一提供方，路由里只能保留其中一个"
                             % (label(other), channel["label"]))
        owners[channel["provider"]] = cid
        route.append(cid)
    order = ids()
    return tuple(sorted(route, key=order.index))


#: 混合调度的随机源；测试可以换成固定种子的实例。
_rng = random.Random()
_affinity = None
_affinity_lock = threading.Lock()


def _channel_affinity():
    """会话上一轮去了哪个渠道。账号池各自记账号，这里只记渠道，让多轮对话留在同一个
    渠道里，保住上游的前缀缓存。"""
    global _affinity
    with _affinity_lock:
        if _affinity is None:
            import wb_accounts
            _affinity = wb_accounts.SessionAffinity()
        return _affinity


def _mix(serving, session_key):
    """在提供同一模型的几个渠道之间挑一个，不区分渠道，按账号决定去向。分层与各账号池
    自己的选号一致：会话粘性 > 优先级数字最小的一层 > 层内手动权重大于 1 的账号（按权重
    抽）> 层内临期积分账号（份额按到期分摊）> 层内最空闲的账号。选中的渠道再由自己的账号
    池按同样的分层挑具体账号。"""
    import wb_settings
    sticky = (session_key and wb_settings.session_affinity(_gateway().ACCOUNTS_DIR))
    candidates = []
    for realm, upstream in serving.items():
        for acct in provider(realm).servable_accounts(realm, upstream):
            candidates.append((realm, acct))
    if not candidates:
        return next(iter(serving))
    if sticky:
        last = _channel_affinity().get(session_key)
        if last in serving and any(realm == last and acct["room"] for realm, acct in candidates):
            return last
    open_slots = [item for item in candidates if item[1]["room"]] or candidates

    def draw(items, share):
        return _rng.choices([realm for realm, _acct in items],
                            weights=[max(1, share(acct)) for _realm, acct in items])[0]

    # 优先级是整层判定：层内没有空名额时这层不参与，交给下一层。
    best = min(acct["priority"] for _realm, acct in open_slots)
    tier = [item for item in open_slots if item[1]["priority"] == best]
    manual = [item for item in tier if item[1]["weight"] > 1]
    urgent = [item for item in tier if item[1]["expiry"] > 0]
    if manual:
        realm = draw(manual, lambda acct: acct["weight"])
    elif urgent:
        realm = draw(urgent, lambda acct: acct["expiry"])
    else:
        least = min(acct["active"] for _realm, acct in tier)
        realm = draw([item for item in tier if item[1]["active"] == least], lambda acct: 1)
    if sticky:
        _channel_affinity().bind(session_key, realm)
    return realm


def select(route, model, session_key=None):
    """在路由里为模型挑渠道，返回 (渠道, 上游 id, 通告 id)；没写模型时两个 id 都是空值。

    别名把各渠道的源 id 合并成同一个通告 id，关闭的模型不参与调度。路由只有一个渠道时直接用它。
    多个渠道时，目录里有这个模型且没有关闭的渠道一起参与混合调度（见 _mix）。只在关闭名单里
    出现时取那个渠道，由关闭检查说明模型已关闭；哪个渠道都没有时取路由里的第一个渠道，由上游
    报错说明。"""
    import wb_models
    import wb_settings
    accounts_dir = _gateway().ACCOUNTS_DIR
    aliases = wb_settings.model_aliases(accounts_dir)
    public = wb_models.canonical_model_id(aliases, model) if model else model

    def upstream_for(realm, available):
        """available 是目录 id 的小写到原始写法的映射；上游按大小写区分模型 id，能在目录里
        找到时转发目录里的原始写法。"""
        disabled = {m.lower() for m in wb_settings.disabled_models(accounts_dir, realm)}
        upstream = wb_models.upstream_model_id(aliases, public, available, disabled)
        low = upstream.lower()
        served = low not in disabled and available is not None and low in available
        if available is not None and low in available:
            upstream = available[low]
        return upstream, served

    if not model:
        return route[0], model, model
    if len(route) == 1:
        realm = route[0]
        return realm, upstream_for(realm, provider(realm).cached_model_ids(realm))[0], public
    serving = {}
    listed = []
    for realm in route:
        ids = provider(realm).model_ids(realm)
        upstream, served = upstream_for(realm, ids)
        if served:
            serving[realm] = upstream
        elif ids is not None and upstream.lower() in ids:
            listed.append((realm, upstream))
    if serving:
        realm = _mix(serving, session_key)
        return realm, serving[realm], public
    if listed:
        return listed[0][0], listed[0][1], public
    realm = route[0]
    return realm, upstream_for(realm, provider(realm).cached_model_ids(realm))[0], public


def _gateway():
    import wb_http
    return wb_http._g()


def _realm_wanted(realm, own_realms):
    """把面板的 realm 过滤变成该提供方的 realm 集合；None 表示不过滤。"""
    value = str(realm or "").strip().lower()
    if not value or value == "all":
        return None
    return {value} & set(own_realms) or set()


def _account_dir(pool):
    """账号池自己的目录；面板与测试可能把池放在别处。"""
    directory = getattr(pool, "dir", None)
    return directory or _gateway().ACCOUNTS_DIR


def _resolve_proxy_slot(accounts, slot_id):
    """把面板给的出口值解析成槽 id："" 直连、"auto" 挑空闲启用槽、其余必须是已知槽。"""
    import wb_settings
    value = str(slot_id or "").strip()
    if not value:
        return ""
    entries = wb_settings.proxy_slots(_gateway().ACCOUNTS_DIR)
    if value == "auto":
        bound = {getattr(account, "proxy_slot", "") for account in accounts
                 if getattr(account, "proxy_slot", "")}
        for entry in entries:
            if entry.get("enabled", True) and entry["id"] not in bound:
                return entry["id"]
        return ""
    if any(entry.get("id") == value for entry in entries):
        return value
    raise ValueError("unknown proxy slot")


def _not_found():
    return ChannelError("没有这个账号", 404, "invalid_request_error")


def _missing_account(accounts_dir):
    """账号池尚未初始化：面板拿到 503，而不是假装导入成功。"""
    return ChannelError(
        "账号池还没有初始化：%s" % accounts_dir, 503, "server_error")


def _workbuddy_last_checkin(value):
    """WorkBuddy 只存一个签到时刻字符串，统一视图里换成 {at, ok, message}。"""
    if not value:
        return None
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    at = 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            at = time.mktime(time.strptime(text, fmt))
            break
        except ValueError:
            continue
    return {"at": int(at), "ok": True, "message": "已签到"}


def _workbuddy_view(account):
    """统一账号视图：补齐两个提供方共有的键，WorkBuddy 自己的键原样保留。"""
    view = account.public()
    view["provider"] = "workbuddy"
    view["identity"] = view.get("product") or "workbuddy"
    view["identityOptions"] = [dict(item) for item in WORKBUDDY_IDENTITIES]
    view["lastCheckin"] = _workbuddy_last_checkin(view.get("lastCheckin"))
    view.setdefault("group", "")
    view.setdefault("weight", 1)
    view.setdefault("concurrencyLimit", 0)
    view.setdefault("activeRequests", 0)
    view.setdefault("disabledModels", [])
    view.setdefault("creditExpiries", [])
    view.setdefault("freeModels", [])
    return view


class WorkBuddyProvider(object):
    name = "workbuddy"
    label = "WorkBuddy"
    # 国际版与国内版是同一家的模型，共用一份关闭名单
    disabled_key = "disabled_models"
    excluded_seen_key = "excluded_seen"
    realms = ("intl", "cn")

    # ---- 账号 API ----
    def _pool(self):
        return _gateway().POOL

    def accounts(self):
        pool = self._pool()
        return list(pool.accounts) if pool else []

    def find(self, uid):
        pool = self._pool()
        return pool.get(uid) if pool else None

    def views(self, realm=None):
        wanted = _realm_wanted(realm, self.realms)
        if wanted is not None and not wanted:
            return []
        return [_workbuddy_view(account) for account in self.accounts()
                if wanted is None or account.realm in wanted]

    def update(self, uid, changes):
        import wb_accounts
        gateway = _gateway()
        account = self.find(uid)
        if account is None:
            raise _not_found()
        if not isinstance(changes, dict):
            raise ValueError("changes must be an object")
        payload = dict(changes)
        identity = payload.pop("identity", None)
        unknown = sorted(key for key in payload
                         if key not in ("enabled", "tasksEnabled", "priority",
                                        "weight", "concurrencyLimit", "group",
                                        "note", "disabledModels", "proxySlot"))
        if unknown:
            raise ValueError("unknown account field: %s" % ", ".join(unknown))
        raw_note = payload.get("note")
        if isinstance(raw_note, str) \
                and len(raw_note.strip()) > wb_accounts.MAX_ACCOUNT_NOTE:
            raise ValueError("note must be at most %d characters"
                             % wb_accounts.MAX_ACCOUNT_NOTE)
        raw_group = payload.get("group")
        if isinstance(raw_group, str) \
                and len(raw_group.strip()) > wb_accounts.MAX_ACCOUNT_GROUP:
            raise ValueError("group must be at most %d characters"
                             % wb_accounts.MAX_ACCOUNT_GROUP)
        raw_models = payload.get("disabledModels")
        if raw_models is not None:
            if not isinstance(raw_models, (list, tuple, set)) or any(
                    not isinstance(item, str) for item in raw_models):
                raise ValueError("disabledModels must be a list of model ids")
        clean, problem = wb_accounts.parse_account_set(payload) if payload else ({}, "")
        if problem:
            raise ValueError(problem)
        if identity is not None:
            import wb_identity
            product = str(identity).strip().lower()
            if product not in wb_identity.VALID_PRODUCTS:
                raise ValueError("identity must be one of: %s"
                                 % ", ".join(wb_identity.VALID_PRODUCTS))
            clean["identity"] = product
        pool = self._pool()
        if "proxySlot" in clean:
            slot_id = _resolve_proxy_slot(self.accounts(), clean["proxySlot"])
            pool.set_proxy_slot(uid, slot_id)
            gateway.log("account %s proxy slot set to %s"
                        % (str(uid)[:8], slot_id or "(direct)"),
                        tag="accounts")
        if "enabled" in clean:
            pool.set_enabled(uid, clean["enabled"])
            if clean["enabled"]:
                gateway.clear_stand_down()
            gateway.log("account %s %s"
                        % (str(uid)[:8], "enabled" if clean["enabled"] else "disabled"),
                        tag="accounts")
        if "tasksEnabled" in clean:
            pool.set_tasks_enabled(uid, clean["tasksEnabled"])
            gateway.log("account %s tasks %s"
                        % (str(uid)[:8],
                           "enabled" if clean["tasksEnabled"] else "disabled"),
                        tag="accounts")
        if "group" in clean:
            pool.set_group(uid, clean["group"])
            gateway.log("account %s group set to %s"
                        % (str(uid)[:8], clean["group"] or "(none)"), tag="accounts")
        if "weight" in clean:
            pool.set_weight(uid, clean["weight"])
            gateway.log("account %s weight set to %d"
                        % (str(uid)[:8], clean["weight"]), tag="accounts")
        if "priority" in clean:
            pool.set_priority(uid, clean["priority"])
            gateway.log("account %s priority set to %d"
                        % (str(uid)[:8], clean["priority"]), tag="accounts")
        if "concurrencyLimit" in clean:
            pool.set_concurrency_limit(uid, clean["concurrencyLimit"])
            gateway.log("account %s concurrency limit set to %d"
                        % (str(uid)[:8], clean["concurrencyLimit"]), tag="accounts")
        if "note" in clean:
            pool.set_note(uid, clean["note"])
            gateway.log("account %s note set" % str(uid)[:8], tag="accounts")
        if "disabledModels" in clean:
            pool.set_disabled_models(uid, clean["disabledModels"])
            gateway.log("account %s disabled models: %s"
                        % (str(uid)[:8],
                           ",".join(clean["disabledModels"]) or "(none)"),
                        tag="accounts")
        if "identity" in clean:
            account = self.find(uid)
            if account is not None and account.set_product(clean["identity"]):
                account.save(_account_dir(pool))
            gateway.log("account %s identity -> %s"
                        % (str(uid)[:8], clean["identity"]), tag="accounts")
        return _workbuddy_view(self.find(uid))

    def delete(self, uid):
        gateway = _gateway()
        pool = self._pool()
        if pool is None or not pool.remove(uid):
            raise _not_found()
        gateway.log("account %s deleted" % str(uid)[:8], tag="accounts")

    def test(self, uid, model=None):
        """真实测试：发一条最小对话；成功即清掉这个账号的本地惩罚。"""
        import wb_panel_routes
        import wb_settings
        gateway = _gateway()
        account = self.find(uid)
        if account is None:
            raise _not_found()
        model = model or wb_settings.test_model(_account_dir(self._pool()),
                                                account.realm)
        row = wb_panel_routes.probe_account(account, model)
        if row.get("ok"):
            # 测试成功说明账号能用：清掉冷却、模型冷却与连续失败计数。
            account.clear_error()
            account.save(_account_dir(self._pool()))
        gateway.log("account test: uid=%s model=%s wall=%sms ok=%s%s"
                    % (str(account.uid)[:8], row.get("model"), row.get("elapsed_ms"),
                       row.get("ok"),
                       "" if row.get("ok") else " error=%s" % (row.get("error") or "")),
                    level="INFO" if row.get("ok") else "WARN", tag="accounts")
        return {"ok": bool(row.get("ok")), "uid": account.uid,
                "model": row.get("model") or model,
                "reply": row.get("reply") or "",
                "elapsed_ms": int(row.get("elapsed_ms") or 0),
                "error": row.get("error") or ""}

    def refresh_credential(self, uid):
        gateway = _gateway()
        account = self.find(uid)
        if account is None:
            raise _not_found()
        if not account.can_refresh():
            return {"ok": True, "uid": account.uid, "error": "",
                    "expiresAt": account.public().get("expiresAt")}
        ok = account.refresh(force=True)
        account.save(_account_dir(self._pool()))
        gateway.log("account %s: 手动刷新凭据 %s"
                    % (str(account.uid)[:8],
                       "成功" if ok else "失败: " + (account.last_error or "")),
                    level="INFO" if ok else "WARN", tag="accounts")
        return {"ok": bool(ok), "uid": account.uid,
                "error": "" if ok else (account.last_error or "refresh failed"),
                "expiresAt": account.public().get("expiresAt")}

    def refresh_credits(self, uids=None):
        wanted = {str(uid) for uid in uids} if uids is not None else None
        rows = []
        seen = set()
        for account in self.accounts():
            if wanted is not None and account.uid not in wanted:
                continue
            # 整批刷新跳过停用与不参与任务的账号；显式 uid 是操作者的动作。
            if wanted is None and (not account.enabled or not account.tasks_enabled):
                continue
            seen.add(account.uid)
            res = account.fetch_credits()
            rows.append({"uid": account.uid, "ok": bool(res.get("ok")),
                         "credits": account.credits,
                         "error": res.get("error", "")})
        missing = sorted(wanted - seen) if wanted is not None else []
        for uid in missing:
            rows.append({"uid": uid, "ok": False, "credits": None,
                         "error": "no such account"})
        return rows

    def checkin(self, uids=None):
        wanted = {str(uid) for uid in uids} if uids is not None else None
        rows = []
        seen = set()
        for account in self.accounts():
            if wanted is not None:
                if account.uid not in wanted:
                    continue
            elif account.realm != "cn" or not account.enabled or not account.tasks_enabled:
                continue
            seen.add(account.uid)
            res = account.checkin(trigger="manual")
            rows.append({"uid": account.uid, "ok": bool(res.get("ok")),
                         "message": str(res.get("msg") or res.get("error") or ""),
                         "credits": float(res.get("credits") or 0)})
        missing = sorted(wanted - seen) if wanted is not None else []
        for uid in missing:
            rows.append({"uid": uid, "ok": False, "message": "no such account",
                         "credits": 0})
        return rows

    def set_all_enabled(self, enabled, realm=None, group=None):
        gateway = _gateway()
        pool = self._pool()
        if pool is None:
            return []
        value = str(realm or "").strip().lower()
        if value and value not in ("all",) + tuple(self.realms):
            return []
        wanted_realm = value if value in self.realms else None
        wanted_group = str(group or "").strip()
        targets = [
            account for account in self.accounts()
            if (wanted_realm is None or account.realm == wanted_realm)
            and (not wanted_group or account.group == wanted_group)
            and account.enabled != bool(enabled)
        ]
        pool.set_all_enabled(bool(enabled), realm=wanted_realm,
                             group=wanted_group or None)
        if enabled:
            gateway.clear_stand_down()
        gateway.log("accounts %s: %d accounts"
                    % ("enabled" if enabled else "disabled", len(targets)),
                    tag="accounts")
        return [account.uid for account in targets]

    def export(self, uids=None, include_secrets=True):
        import wb_accounts
        document = wb_accounts.build_export_document(
            self.accounts(), realm=None, include_secrets=include_secrets,
            uids=uids)
        rows = document["accounts"]
        for row in rows:
            row["provider"] = "workbuddy"
        return rows

    def import_rows(self, rows, dry_run=False, overwrite=False):
        imported, skipped, errors = [], [], []
        usable = []
        for index, row in enumerate(rows or ()):
            if not isinstance(row, dict):
                errors.append({"index": index + 1, "reason": "row must be an object"})
                continue
            owner = str(row.get("provider") or "").strip().lower()
            if owner and owner != self.name:
                skipped.append({"uid": str(row.get("uid") or ""),
                                "reason": "belongs to %s" % owner})
                continue
            usable.append(row)
        pool = self._pool()
        if pool is None:
            raise _missing_account(_gateway().ACCOUNTS_DIR)
        if dry_run:
            report = pool.preview_import_rows(usable, overwrite=overwrite)
        else:
            report = pool.import_rows(usable, overwrite=overwrite)
            if report["added"] or report["updated"]:
                _gateway().log(
                    "account import: %d added, %d updated, %d skipped, %d invalid"
                    % (len(report["added"]), len(report["updated"]),
                       len(report["skipped"]), len(report["invalid"])),
                    tag="accounts")
        imported = ([{"uid": uid, "action": "added"} for uid in report["added"]]
                    + [{"uid": uid, "action": "updated"} for uid in report["updated"]])
        skipped.extend(report["skipped"])
        errors.extend({"index": item.get("index"), "reason": item.get("reason")}
                      for item in report["invalid"])
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def ready_count(self, realm=None):
        wanted = _realm_wanted(realm, self.realms)
        if wanted is not None and not wanted:
            return 0
        return sum(1 for account in self.accounts()
                   if (wanted is None or account.realm in wanted)
                   and account.enabled and account.servable()
                   and account.has_request_capacity())

    def total_count(self, realm=None):
        wanted = _realm_wanted(realm, self.realms)
        if wanted is not None and not wanted:
            return 0
        return sum(1 for account in self.accounts()
                   if wanted is None or account.realm in wanted)

    def apply_pool_guards(self, refresh=False):
        """把号池守卫推进 WorkBuddy 账号，返回推了几条守卫。

        保留积分与临期窗口是静态设置，值没变就不重推；每日三关要跟着用量走，
        配了限额就每次刷新。
        """
        import wb_settings
        import wb_usage
        pool = self._pool()
        if pool is None:
            return 0
        directory = _gateway().ACCOUNTS_DIR
        guards = wb_settings.limit_values_many(
            directory, ("reserve_credits", "daily_token_limit",
                        "daily_credit_limit", "model_daily_token_limit",
                        "expiring_window_days"))
        token_limits = guards["daily_token_limit"]
        credit_limits = guards["daily_credit_limit"]
        model_limits = guards["model_daily_token_limit"]
        static = (tuple(sorted(guards["reserve_credits"].items())),
                  tuple(sorted(guards["expiring_window_days"].items())))
        published = 0
        if refresh or static != getattr(pool, "_guard_static", None):
            pool.apply_reserve_credits(guards["reserve_credits"])
            pool.apply_expiring_window(guards["expiring_window_days"])
            pool._guard_static = static
            published += 2
        rotation_on = wb_settings.session_rotation_megatokens(directory) > 0
        wants = (any(value > 0 for value in token_limits.values())
                 or any(value > 0 for value in credit_limits.values())
                 or any(value > 0 for value in model_limits.values())
                 or rotation_on)
        stats = wb_usage.daily_usage_stats(ttl=0 if refresh else None) if wants else None
        if refresh or any(value > 0 for value in token_limits.values()):
            pool.apply_daily_token_limit(
                token_limits, stats["tokens"] if stats is not None else None)
            published += 1
        if refresh or any(value > 0 for value in credit_limits.values()):
            pool.apply_daily_credit_limit(
                credit_limits, stats["credits"] if stats is not None else None,
                wb_usage.free_models_by_realm())
            published += 1
        if (refresh or any(value > 0 for value in model_limits.values())
                or rotation_on):
            pool.apply_model_daily_token_limit(
                model_limits, stats["models"] if stats is not None else None)
            published += 1
        return published

    def apply_routing_inputs(self, rates=None):
        """把用量日志算出的积分消耗速度推进账号；没有映射时不动，空映射表示清零。"""
        pool = self._pool()
        if pool is None or rates is None:
            return 0
        pool.apply_credit_rates(rates)
        return len(rates)

    # ---- 目录与上游 ----
    def configured(self, accounts_dir):
        return True

    def model_entries(self, realm):
        import wb_models
        import wb_modelsdev
        try:
            entries = wb_models.fetch_models(realm=realm)
        except Exception as exc:
            raise ChannelError(str(exc))
        # 上下文长度的本地缓存在冷却期内至多后台预热一次，取不到时用内置清单
        wb_modelsdev.refresh_async(_gateway().ACCOUNTS_DIR, log=_gateway().log)
        return [wb_models.model_entry(mid, meta) for mid, meta in entries]

    def catalog_meta(self, realm):
        import wb_models
        return wb_models.catalog_meta(realm)

    def cached_model_ids(self, realm):
        import wb_models
        return wb_models.cached_model_ids(realm)

    def model_ids(self, realm):
        """目录 id 的小写到原始写法的映射，缓存过期时向上游取数；取不到时返回 None。"""
        import wb_models
        try:
            entries = wb_models.fetch_models(realm=realm)
        except Exception as exc:
            _gateway().log("catalog %s unavailable for routing: %s" % (realm, exc))
            return None
        return {str(mid).strip().lower(): str(mid).strip() for mid, _meta in entries}

    def servable_accounts(self, realm, model):
        """此刻能接这个模型的账号，给混合调度比较优先级、在途数与权重；只看本地状态。"""
        pool = _gateway().POOL
        return pool.servable_accounts(realm, model) if pool is not None else []

    def panel_models(self, realm, refresh):
        """面板模型表：可用模型加上游列出但被筛选规则排除的模型，条目带 excluded。"""
        import wb_models
        result = {}
        if refresh:
            outcome = wb_models.refresh_catalog(realm)
            result["refresh_ok"] = bool(outcome.get("ok"))
            result["refresh_msg"] = outcome.get("msg") or ""
        entries = wb_models.fetch_models(realm=realm)
        info = wb_models.catalog_meta(realm) or {}
        served = {mid for mid, _ in entries}
        saved = wb_models.read_cached_remote_catalog(realm) or ([], {})
        extra = list(wb_models._catalog_excluded.get(realm) or [])
        for mid in saved[0]:
            if mid not in served and mid not in extra:
                extra.append(mid)
        wb_models.seed_default_disabled(extra)
        raw_meta = wb_models._catalog_raw_meta.get(realm) or {}
        data = []
        for mid, meta in list(entries) + [
                (m, raw_meta.get(m) or saved[1].get(m) or {}) for m in extra]:
            item = wb_models.model_entry(mid, meta)
            item["excluded"] = mid not in served
            data.append(item)
        source = info.get("source")
        result.update({
            "data": data,
            "source": {"remote": "server", "endpoint": "server",
                       "cache": "cache", "legacy": "cache"}.get(source, source),
            "fetched_at": info.get("fetched_at"),
        })
        return result

    def note_session_usage(self, session_key, account, models, tokens):
        """把这次请求在若干模型别名上消耗的 Token 记到会话上。"""
        pool = self._pool()
        if pool is not None:
            pool.affinity.consume(session_key, models, tokens)

    def open_stream(self, payload, session_key, realm, only_uid=None, lease=None):
        return _gateway().open_upstream(payload, session_key=session_key,
                                        target_realm=realm, only_uid=only_uid,
                                        lease=lease)


class CodeArtsProvider(object):
    name = "codearts"
    label = "CodeArts"
    disabled_key = "codearts_disabled_models"
    excluded_seen_key = "codearts_excluded_seen"
    realms = ("codearts",)
    #: 默认排除的模型：首次出现在目录里时加入关闭名单并在模型页标为已排除，操作者重新开启后
    #: 不会再被关掉。支持 * 通配，不区分大小写。
    DEFAULT_EXCLUDED = ("deepseek-v4.1-flash", "deepseek-v4-pro-0813", "speed", "openpangu-*")

    @classmethod
    def default_excluded(cls, mid):
        import fnmatch
        low = str(mid or "").strip().lower()
        return any(fnmatch.fnmatchcase(low, pattern) for pattern in cls.DEFAULT_EXCLUDED)

    def _entries_with_exclusion(self, cfg):
        """账号目录的条目；默认排除的模型首次出现时写进关闭名单。"""
        import wb_codearts
        import wb_models
        entries = wb_codearts.model_entries(cfg)
        wb_models.seed_default_disabled(
            [item["id"] for item in entries if self.default_excluded(item.get("id"))],
            realm=self.realms[0])
        return entries

    @staticmethod
    def _config():
        import wb_codearts
        return wb_codearts.load_config(_gateway().ACCOUNTS_DIR)

    @staticmethod
    def _pool():
        import wb_codearts
        return wb_codearts.pool_for(_gateway().ACCOUNTS_DIR)

    @staticmethod
    def _error(exc):
        """把 CodeArts 异常映射成 ChannelError：上游状态码原样保留，本机故障另算。"""
        import wb_codearts
        uid = getattr(exc, "account_uid", None)
        if isinstance(exc, wb_codearts.CodeArtsLocalNetwork):
            return ChannelError(str(exc), 503, "server_error", account_uid=uid,
                                local_network=True)
        if isinstance(exc, wb_codearts.CodeArtsNoAccounts):
            return ChannelError(str(exc), 503, "server_error")
        if isinstance(exc, wb_codearts.CodeArtsBusy):
            return ChannelError(str(exc), 429, "rate_limit_error",
                                account_uid=uid, wait=1, outcome="rate_limited")
        if isinstance(exc, wb_codearts.CodeArtsCooling):
            wait = max(1, int(getattr(exc, "wait", 1) or 1))
            return ChannelError(str(exc), 429, "rate_limit_error",
                                account_uid=uid, wait=wait, outcome="rate_limited")
        if isinstance(exc, wb_codearts.CodeArtsBadRequest):
            return ChannelError(str(exc), 400, "invalid_request_error", account_uid=uid)
        if isinstance(exc, wb_codearts.CodeArtsNotFound):
            return ChannelError(str(exc), 404, "invalid_request_error", account_uid=uid)
        if isinstance(exc, wb_codearts.CodeArtsConflict):
            return ChannelError(str(exc), 409, "invalid_request_error", account_uid=uid)
        if isinstance(exc, wb_codearts.CodeArtsAuthError):
            status = int(exc.status or 401)
            # 换 token 被拒可能带 400，对客户端而言都是需要重新授权。
            if status not in (401, 403):
                status = 401
            etype = "authentication_error" if status == 401 else "permission_error"
            return ChannelError(str(exc), status, etype, account_uid=uid)
        status = getattr(exc, "status", None)
        if isinstance(exc, wb_codearts.CodeArtsUpstreamError) and status == 429:
            wait = getattr(exc, "retry_after", None) or 1
            return ChannelError(str(exc), 429, "rate_limit_error", account_uid=uid,
                                wait=max(1, int(wait)), outcome="rate_limited")
        if status:
            return ChannelError(str(exc), int(status), "api_error", account_uid=uid)
        return ChannelError(str(exc), 502, "api_error", account_uid=uid)

    # ---- 账号 API ----
    def accounts(self):
        return list(self._pool().accounts)

    def find(self, uid):
        return self._pool().get(uid)

    def views(self, realm=None):
        import wb_codearts
        return wb_codearts.account_views(_gateway().ACCOUNTS_DIR, realm=realm)

    def update(self, uid, changes):
        import wb_codearts
        try:
            return wb_codearts.update_account(_gateway().ACCOUNTS_DIR, uid, changes)
        except wb_codearts.CodeArtsNotFound as exc:
            raise ChannelError(str(exc), 404, "invalid_request_error")
        except wb_codearts.CodeArtsBadRequest as exc:
            raise ValueError(str(exc))

    def delete(self, uid):
        import wb_codearts
        try:
            wb_codearts.remove(self._config(), uid)
        except wb_codearts.CodeArtsNotFound as exc:
            raise ChannelError(str(exc), 404, "invalid_request_error")

    def test(self, uid, model=None):
        import wb_codearts
        try:
            config = self._config()
            # 显式传入的模型优先；否则用设置里的测试模型，空值交给账号自己的默认模型。
            model = model or config.get("test_model") or None
            return wb_codearts.test_account(config, uid, model=model)
        except wb_codearts.CodeArtsNotFound as exc:
            raise ChannelError(str(exc), 404, "invalid_request_error")

    def refresh_credential(self, uid):
        import wb_codearts
        try:
            return wb_codearts.refresh_credential(self._config(), uid)
        except wb_codearts.CodeArtsNotFound as exc:
            raise ChannelError(str(exc), 404, "invalid_request_error")

    def refresh_credits(self, uids=None):
        import wb_codearts
        return wb_codearts.refresh_credits(self._config(), uids=uids)

    def checkin(self, uids=None):
        import wb_codearts
        cfg = self._config()
        pool = self._pool()
        if uids is None:
            targets = wb_codearts.checkin_due(cfg)
            manual = False
        else:
            wanted = {str(uid) for uid in uids}
            targets = [account for account in pool.accounts
                       if account.uid in wanted]
            # 面板的逐行签到是操作者的明确动作，不受尝试次数与重试间隔拦截。
            manual = True
        rows = []
        seen = set()
        for account in targets:
            seen.add(account.uid)
            try:
                res = wb_codearts.checkin(cfg, account.uid, manual=manual)
                rows.append({"uid": account.uid, "ok": bool(res.get("ok")),
                             "message": str(res.get("message") or ""),
                             "credits": float(res.get("credits") or 0)})
            except wb_codearts.CodeArtsError as exc:
                rows.append({"uid": account.uid, "ok": False,
                             "message": str(exc), "credits": 0})
        if uids is not None:
            for uid in sorted(wanted - seen):
                rows.append({"uid": uid, "ok": False,
                             "message": "account not found", "credits": 0})
        return rows

    def set_all_enabled(self, enabled, realm=None, group=None):
        import wb_codearts
        return wb_codearts.set_all_enabled(self._config(), enabled,
                                           realm=realm, group=group)

    def export(self, uids=None, include_secrets=True):
        import wb_codearts
        return wb_codearts.export_rows(_gateway().ACCOUNTS_DIR, uids=uids,
                                       include_secrets=include_secrets)

    def import_rows(self, rows, dry_run=False, overwrite=False):
        import wb_codearts
        return wb_codearts.import_rows(self._config(), rows, dry_run=dry_run,
                                       overwrite=overwrite)

    def ready_count(self, realm=None):
        wanted = _realm_wanted(realm, self.realms)
        if wanted is not None and not wanted:
            return 0
        now = time.time()
        return sum(1 for account in self._pool().accounts
                   if account.healthy(now) and account.has_capacity())

    def total_count(self, realm=None):
        wanted = _realm_wanted(realm, self.realms)
        if wanted is not None and not wanted:
            return 0
        return len(self._pool().accounts)

    def apply_pool_guards(self, refresh=False):
        import wb_codearts
        return wb_codearts.apply_pool_guards(self._config(), refresh=refresh)

    def apply_routing_inputs(self, rates=None):
        """CodeArts 的消耗速度来自套餐 used 的日增量；`rates` 不适用。"""
        import wb_codearts
        return wb_codearts.apply_routing_inputs(self._config(), rates=rates)

    # ---- 目录与上游 ----
    def configured(self, accounts_dir):
        import wb_codearts
        return wb_codearts.configured(accounts_dir)

    def model_entries(self, realm):
        import wb_codearts
        cfg = self._config()
        if not wb_codearts.configured_config(cfg):
            raise ChannelError("没有可用账号，请在账号页添加或启用", 503, "server_error")
        try:
            return self._entries_with_exclusion(cfg)
        except wb_codearts.CodeArtsError as exc:
            raise self._error(exc)

    def catalog_meta(self, realm):
        import wb_codearts
        return wb_codearts.models_meta(self._config())

    def cached_model_ids(self, realm):
        import wb_codearts
        return wb_codearts.cached_model_ids(_gateway().ACCOUNTS_DIR)

    def model_ids(self, realm):
        """所有账号目录的并集加内置种子，小写 id 到原始写法的映射；没有可用账号时返回 None。"""
        try:
            entries = self.model_entries(realm)
        except ChannelError as exc:
            _gateway().log("catalog %s unavailable for routing: %s" % (realm, exc))
            return None
        return {str(item.get("id") or "").strip().lower(): str(item.get("id") or "").strip()
                for item in entries}

    def servable_accounts(self, realm, model):
        """此刻能接这个模型的账号，给混合调度比较优先级、在途数与权重；只看本地状态。"""
        import wb_codearts
        return wb_codearts.servable_accounts(_gateway().ACCOUNTS_DIR, model)

    def panel_models(self, realm, refresh):
        import wb_codearts
        cfg = self._config()
        result = {}
        if refresh:
            outcome = wb_codearts.refresh_models(cfg)
            result["refresh_ok"] = bool(outcome.get("ok"))
            result["refresh_msg"] = outcome.get("message") or ""
        try:
            data = self._entries_with_exclusion(cfg)
        except wb_codearts.CodeArtsError as exc:
            raise self._error(exc)
        import wb_settings
        disabled = {m.lower() for m in wb_settings.disabled_models(_gateway().ACCOUNTS_DIR, realm)}
        for item in data:
            # 默认排除且仍在关闭名单里的模型标为已排除；操作者重新开启后按普通模型显示
            item["excluded"] = (self.default_excluded(item.get("id"))
                                and str(item.get("id") or "").lower() in disabled)
        meta = wb_codearts.models_meta(cfg)
        result.update({
            "data": data,
            # 账号取到过目录时来自上游，否则是内置种子表
            "source": "server" if meta.get("fetched_at") else "bundled",
            "fetched_at": meta.get("fetched_at"),
        })
        return result

    def note_session_usage(self, session_key, account, models, tokens):
        """把这次请求在若干模型别名上消耗的 Token 记到会话上。"""
        pool = getattr(account, "pool", None)
        if pool is not None:
            pool.affinity.consume(session_key, models, tokens)

    def open_stream(self, payload, session_key, realm, only_uid=None, lease=None):
        """返回 (流, 账号, 思考档位)，与 WorkBuddy 的 open_upstream 同形。

        CodeArts 自己按账号占下并发名额、在响应流关闭时归还；`lease` 只为
        调用方统一签名而存在。
        """
        import wb_codearts
        import wb_usage
        cfg = self._config()
        try:
            body = wb_codearts.build_chat_body(payload, session_key)
            # 客户端没给会话标识时按消息前缀派生，与 WorkBuddy 共用同一个开关与
            # 超限分页规则；否则同一段对话每轮都会换账号，前缀缓存拿不到。
            page = None
            if not session_key:
                session_key, page = _gateway().affinity_route(body.get("messages"))
                audit = wb_usage.current_audit()
                if audit is not None:
                    audit["session_key"] = session_key
            allowed = [only_uid] if only_uid else None
            stream, account, _billed = wb_codearts.open_chat(
                cfg, body, session_key, allowed_accounts=allowed, page=page)
        except wb_codearts.CodeArtsError as exc:
            raise self._error(exc)
        return stream, account, body.get("reasoning_effort")


def wb_codearts_not_found_types():
    """延迟导入：仅在映射异常时用到，避免模块层的循环依赖。"""
    import wb_codearts
    return (wb_codearts.CodeArtsNotFound,)


_PROVIDERS = {provider.name: provider for provider in (WorkBuddyProvider(), CodeArtsProvider())}


def provider(realm):
    """渠道背后的提供方。登记表里没有的渠道按 WorkBuddy 处理：账号池里没有这个渠道的账号，
    请求会以「没有可用账号」失败。"""
    channel = _BY_ID.get(realm)
    return _PROVIDERS[channel["provider"] if channel else WorkBuddyProvider.name]


def owner(uid):
    """这个 uid 属于哪个提供方；找不到时返回 None。"""
    for candidate in _PROVIDERS.values():
        if candidate.find(uid) is not None:
            return candidate
    return None


def find_account(uid):
    """返回 (提供方, 账号)；找不到时两者都是 None。"""
    for candidate in _PROVIDERS.values():
        account = candidate.find(uid)
        if account is not None:
            return candidate, account
    return None, None


def note_session_usage(session_key, account_uid, models, tokens):
    """把一次请求在若干模型别名上消耗的 Token 记到会话上，供轮换阈值判断。"""
    if not session_key or not account_uid or not tokens:
        return
    provider, account = find_account(account_uid)
    if provider is None or account is None:
        return
    provider.note_session_usage(session_key, account, models, tokens)


def all_views(realm=None):
    """每个提供方的统一账号视图，按登记顺序拼接。"""
    views = []
    for candidate in _PROVIDERS.values():
        views.extend(candidate.views(realm=realm))
    return views
