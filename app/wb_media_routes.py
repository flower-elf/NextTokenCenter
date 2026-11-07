"""媒体生成路由：生图、改图、生视频与任务查询。

协议层在 wb_media，出站层在 wb_media_client，异步任务表在 wb_media_tasks；
这里只做路由、开关、选号与响应整形。方法以 MediaRoutes 混入 Handler。
"""
import os
import threading
import time

import wb_channels
import wb_media
import wb_media_client
import wb_media_tasks
import wb_settings

_TASKS = None
_TASKS_LOCK = threading.Lock()
_CATALOG = {}
_CATALOG_AT = {}
_CATALOG_TTL = 300.0
_SLOTS = threading.BoundedSemaphore(4)
_RATE = {}
_SPEND = {}
_SPEND_DAY = ""
_GUARD_LOCK = threading.Lock()


def _rate_allow(key, limit):
    now = time.time()
    with _GUARD_LOCK:
        stamps = [at for at in _RATE.get(key, []) if now - at < 60.0]
        if limit and len(stamps) >= limit:
            _RATE[key] = stamps
            return False
        stamps.append(now)
        _RATE[key] = stamps
        return True


def _spend_allowed(key, cost, limit):
    if not limit:
        return True
    day = time.strftime("%Y-%m-%d")
    with _GUARD_LOCK:
        global _SPEND_DAY
        if day != _SPEND_DAY:
            _SPEND.clear()
            _SPEND_DAY = day
        return _SPEND.get(key, 0.0) + cost <= limit


def _spend_add(key, cost):
    if not cost:
        return
    with _GUARD_LOCK:
        _SPEND[key] = _SPEND.get(key, 0.0) + cost


def _g():
    import wb_http
    return wb_http._g()


def _as_text(value):
    return value if isinstance(value, str) else ""


def media_tasks():
    global _TASKS
    with _TASKS_LOCK:
        config = wb_settings.media_config(_g().ACCOUNTS_DIR)
        if _TASKS is None:
            _TASKS = wb_media_tasks.MediaTaskStore(
                out_dir=os.path.join(_g().USAGE_DIR, "media"),
                concurrency=int(config.get("video_concurrency") or 3),
                resolve_account=lambda uid: (_g().POOL.get(uid)
                                             if _g().POOL else None),
                logger=lambda message: _g().log(message, tag="media"),
            )
            _TASKS.set_submitter(_submit_video_task)
        else:
            # 任务表是进程单例；每次取用时同步设置，避免改并发数要重启才生效。
            _TASKS.concurrency = max(1, int(config.get("video_concurrency") or 3))
        return _TASKS


def _submit_video_task(task):
    realm = task.get("realm") or "cn"
    account = _pick_account(realm, task.get("model") or "", set())
    submission = wb_media_client.submit_video(account, task.get("body") or {})
    account.note_success(model=task.get("model") or "")
    return {"upstream_id": submission["taskId"], "account": account.uid}


def _pick_account(realm, model, excluded):
    pool = _g().POOL
    if pool is None:
        raise wb_media.MediaError("NO_ROUTE", "账号池不可用")
    account = pool.pick(realm=realm, exclude=excluded, model=model)
    if account is None:
        raise wb_media.MediaError("NO_ROUTE", "该出口没有可用账号")
    if not account.ready(model=model):
        raise wb_media.MediaError(
            "UPSTREAM", "账号 %s 此刻不可用：%s"
            % ((account.uid or "?")[:8], account.unavailable_reason(model=model)))
    credits = account.credits if isinstance(account.credits, dict) else {}
    remain = credits.get("remain")
    if isinstance(remain, (int, float)) and remain <= 0:
        raise wb_media.MediaError("NO_CREDIT", "账号 %s 的积分不足"
                                  % (account.uid or "?")[:8])
    return account


def _catalog(realm):
    now = time.time()
    cached = _CATALOG.get(realm)
    if cached is not None and now - _CATALOG_AT.get(realm, 0.0) < _CATALOG_TTL:
        return cached
    pool = _g().POOL
    account = pool.representative(realm=realm) if pool else None
    if account is None:
        raise wb_media.MediaError("NO_ROUTE", "该出口没有可用账号，读不到媒体目录")
    payload = wb_media_client.fetch_media_models(account)
    models = wb_media.media_models(payload)
    if not models:
        raise wb_media.MediaError(
            "NO_MEDIA_MODEL", "该出口目录里没有媒体模型")
    _CATALOG[realm] = models
    _CATALOG_AT[realm] = now
    return models


class MediaRoutes(object):
    def _media_config(self):
        config = wb_settings.media_config(_g().ACCOUNTS_DIR)
        wb_media_client.set_identity(config.get("identity"))
        return config

    def _media_error(self, exc):
        mapping = {
            "INVALID_ARGS": (400, "invalid_request_error"),
            "NO_MEDIA_MODEL": (400, "invalid_request_error"),
            "REJECTED": (400, "invalid_request_error"),
            "NO_ROUTE": (404, "invalid_request_error"),
            "NO_CREDIT": (402, "insufficient_quota"),
            "RATE_LIMIT": (429, "rate_limit_error"),
            "AUTH": (502, "server_error"),
            "UPSTREAM": (502, "server_error"),
            "EMPTY_RESULT": (502, "server_error"),
            "QUEUE_FULL": (429, "rate_limit_error"),
        }
        status, err_type = mapping.get(exc.code, (502, "server_error"))
        _g().log("media error: HTTP %d %s" % (status, exc.message), tag="media")
        return self._error(status, exc.message or exc.code, err_type)

    def _media_realm(self, config):
        bound = self._key_route()
        if bound and config["realm"] not in bound:
            raise wb_media.MediaError(
                "INVALID_ARGS",
                "本 Key 绑定的渠道是 %s，媒体配置在 %s 出口"
                % ("、".join(wb_channels.label(r) for r in bound),
                   wb_channels.label(config["realm"])))
        return config["realm"]

    def _handle_media_post(self, path, payload):
        if path in ("/v1/videos", "/videos"):
            return self._media_video_submit(payload)
        config = self._media_config()
        try:
            if not config["enabled"]:
                raise wb_media.MediaError("NO_ROUTE", "not found")
            key_id = self._key_id()
            if not _rate_allow(key_id, int(config.get("image_max_per_minute") or 0)):
                raise wb_media.MediaError(
                    "RATE_LIMIT", "生图请求太频繁，请稍后再试")
            realm = self._media_realm(config)
            kind = "edit" if path in ("/v1/images/edits", "/images/edits") else "generate"
            prompt = payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise wb_media.MediaError("INVALID_ARGS", "prompt 必填")
            models = _catalog(realm)
            tag = (wb_media.TAG_IMAGE_TO_IMAGE if kind == "edit"
                   else wb_media.TAG_TEXT_TO_IMAGE)
            model = wb_media.pick_model(models, payload.get("model"), tag)
            size = wb_media.check_size(payload.get("size"))
            count = payload.get("n")
            count = 1 if count in (None, "") else int(count)
            if kind == "edit":
                images = []
                raw = payload.get("image")
                values = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
                for value in values:
                    images.append(wb_media_client.resolve_input_image(value))
                if not images:
                    raise wb_media.MediaError("INVALID_ARGS", "改图要带 image")
                body = wb_media.build_edit_body(model["id"], prompt, size, images)
            else:
                body = wb_media.build_image_body(model["id"], prompt, size, count)
            want = _as_text(payload.get("response_format")) or "url"
            if want not in ("url", "b64_json"):
                raise wb_media.MediaError(
                    "INVALID_ARGS", "response_format 只能是 url 或 b64_json")
        except wb_media.MediaError as exc:
            return self._media_error(exc)
        cost_hint = wb_media.estimate_cost("image")
        if not _spend_allowed(key_id, cost_hint,
                              int(config.get("daily_credits_per_key") or 0)):
            return self._media_error(wb_media.MediaError(
                "NO_CREDIT", "本 Key 今日的媒体积分额度已用完"))
        if not _SLOTS.acquire(blocking=False):
            return self._media_error(wb_media.MediaError(
                "RATE_LIMIT", "媒体请求并发已满，请稍后重试"))
        try:
            excluded = set()
            started = time.time()
            while True:
                try:
                    account = _pick_account(realm, model["id"], excluded)
                except wb_media.MediaError as exc:
                    return self._media_error(exc)
                try:
                    if kind == "edit":
                        outcome = wb_media_client.edit_image(account, body)
                    else:
                        outcome = wb_media_client.generate_image(account, body)
                except wb_media.MediaError as exc:
                    if exc.code in ("UPSTREAM", "RATE_LIMIT") and len(excluded) < 2:
                        excluded.add(account.uid)
                        continue
                    if exc.code in ("AUTH", "UPSTREAM"):
                        account.note_error(exc.message, model=model["id"])
                    return self._media_error(exc)
                account.note_success(model=model["id"])
                _spend_add(key_id, outcome.get("credit") or cost_hint)
                item = {}
                if outcome.get("url"):
                    item["url"] = outcome["url"]
                else:
                    item["b64_json"] = outcome.get("b64")
                if outcome.get("revised_prompt"):
                    item["revised_prompt"] = outcome["revised_prompt"]
                _g().log("media image: %s %dx%d in %.2fs"
                         % (model["id"], count, 1, time.time() - started), tag="media")
                return self._json(200, {
                    "created": outcome.get("created") or int(time.time()),
                    "data": [item],
                })
        finally:
            _SLOTS.release()

    def _media_video_submit(self, payload):
        config = self._media_config()
        try:
            if not config["enabled"]:
                raise wb_media.MediaError("NO_ROUTE", "not found")
            realm = self._media_realm(config)
            store = media_tasks()
            if store.pending_count() >= store.max_pending:
                raise wb_media.MediaError(
                    "QUEUE_FULL", "生视频队列已满（%d 条）" % store.pending_count())
            models = _catalog(realm)
            wants_image = bool(_as_text(payload.get("image"))
                               or _as_text(payload.get("input_reference")))
            tag = (wb_media.TAG_IMAGE_TO_VIDEO if wants_image
                   else wb_media.TAG_TEXT_TO_VIDEO)
            model = wb_media.pick_model(models, payload.get("model"), tag)
            generation = dict(model.get("videoGeneration") or {})
            request = dict(payload)
            raw_image = (_as_text(request.get("image"))
                         or _as_text(request.get("input_reference")))
            if raw_image:
                request["image"] = wb_media_client.resolve_input_image(raw_image)
            params = wb_media.resolve_video_params(request, generation)
            cap = int(config.get("video_max_seconds") or 0)
            if cap and params["seconds"] > cap:
                raise wb_media.MediaError(
                    "INVALID_ARGS", "视频时长上限为 %d 秒" % cap)
            body = wb_media.build_video_body(model["id"], request, params)
        except wb_media.MediaError as exc:
            return self._media_error(exc)
        key_id = self._key_id()
        cost_hint = wb_media.estimate_cost("video", params["seconds"])
        if not _spend_allowed(key_id, cost_hint,
                              int(config.get("daily_credits_per_key") or 0)):
            return self._media_error(wb_media.MediaError(
                "NO_CREDIT", "本 Key 今日的媒体积分额度已用完"))
        if store.active_count() >= store.concurrency:
            task = store.create_pending(model["id"], params, body,
                                        realm=realm, key=key_id)
            public = wb_media.public_task(task)
            public["queue_position"] = store.queue_position(task["id"])
            public["hint"] = "已排队，未提交上游、未扣积分"
            return self._json(202, public)
        if not _SLOTS.acquire(blocking=False):
            return self._media_error(wb_media.MediaError(
                "RATE_LIMIT", "媒体请求并发已满，请稍后重试"))
        try:
            try:
                account = _pick_account(realm, model["id"], set())
                submission = wb_media_client.submit_video(account, body)
            except wb_media.MediaError as exc:
                return self._media_error(exc)
            task = store.create(model["id"], params, account.uid, body,
                                submission["taskId"], realm=realm,
                                key=key_id)
            _spend_add(key_id, cost_hint)
            _g().log("media video: 已提交 %s (%s, %d 秒)"
                     % (task["id"], model["id"], params["seconds"]), tag="media")
            return self._json(202, wb_media.public_task(task))
        finally:
            _SLOTS.release()

    def _media_task_public(self, task):
        if not task:
            return None
        url = None
        if task.get("status") == "completed" and task.get("videos"):
            try:
                url = media_tasks().sign(task["id"])
            except Exception:
                url = None
        return wb_media.public_task(task, content_url=url)

    def _get_media_models(self, query):
        if not self._authorized():
            return
        config = self._media_config()
        if not config["enabled"]:
            return self._error(404, "not found", "invalid_request_error")
        try:
            realm = query.get("realm", [None])[0] or self._media_realm(config)
            return self._json(200, {"object": "list", "data": _catalog(realm),
                                    "realm": realm})
        except wb_media.MediaError as exc:
            return self._media_error(exc)

    def _get_media_tasks(self, query):
        if not self._authorized():
            return
        store = media_tasks()
        limit = int((query.get("limit") or ["50"])[0] or 50)
        rows = [self._media_task_public(task) for task in store.list(limit)]
        return self._json(200, {"object": "list", "data": rows})

    def _get_media_video(self, path, query):
        if not self._authorized():
            return
        task_id = path.rsplit("/", 1)[-1]
        task = media_tasks().get(task_id)
        if task is None:
            return self._error(404, "no such video task", "invalid_request_error")
        return self._json(200, self._media_task_public(task))

    def _get_media_content(self, task_id, query):
        store = media_tasks()
        token = (query.get("token") or [""])[0]
        index = int((query.get("index") or ["0"])[0] or 0)
        if not store.verify(task_id, index, token):
            return self._error(403, "invalid or expired media token",
                               "invalid_request_error")
        path = store.content_path(task_id, index)
        if not path or not os.path.isfile(path):
            return self._error(404, "media content not found", "invalid_request_error")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._error(500, str(exc))
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
