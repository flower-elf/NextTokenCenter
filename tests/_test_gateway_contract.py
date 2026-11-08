# -*- coding: utf-8 -*-
"""契约接线：并发名额在生产处理器里生效、ChannelError 的限流应答与用量归属、
WorkBuddy 停止开关不连坐 CodeArts、统一账号接口覆盖两个提供方、未采集扣费。

全程离线，用 python tests/_test_gateway_contract.py 运行。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))
_TMP = tempfile.mkdtemp(prefix="wb-contract-")
os.environ["ACCOUNTS_DIR"] = os.path.join(_TMP, "accounts")
os.environ["WB_PROXY_USAGE_DIR"] = _TMP

import wb_accounts
import wb_channels
import wb_cost
import wb_panel_routes
import wb_proxy as proxy
import wb_usage
import wb_settings


def sse(chunks):
    out = [("data: " + json.dumps(c) + "\n\n").encode("utf-8") for c in chunks]
    out.append(b"data: [DONE]\n\n")
    return out


class FakeUpstream(object):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


class FakeAccount(object):
    uid = "codearts-uid"
    realm = "codearts"


def chat_chunks():
    return [
        {"choices": [{"delta": {"content": "ok"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                   "total_tokens": 4}},
    ]


def stub_pool(account):
    """open_upstream 需要的真实账号 + 最小账号池表面。"""
    class Pool(object):
        accounts = [account]
        dir = os.environ["ACCOUNTS_DIR"]
        affinity = wb_accounts.SessionAffinity()

        def count_ready(self, realm=None, model=None):
            return sum(a.ready(model=model) for a in self.accounts)

        def get(self, uid):
            return account if uid == account.uid else None

        def pick_for_session(self, realm, session_key=None, exclude=(),
                             model=None, page=None, claim=None):
            return next((a for a in self.accounts
                         if a.uid not in exclude and a.realm == realm
                         and a.ready(model=model)
                         and (claim is None or claim(a))), None)

        def apply_daily_token_limit(self, value=None, usage=None):
            return value or 0

        def apply_daily_credit_limit(self, value=None, credits=None,
                                     free_models=None):
            return value or 0

        def apply_model_daily_token_limit(self, value=None, per_model=None):
            return value or 0

        def apply_reserve_credits(self, values=None):
            return None

        def apply_expiring_window(self, values=None):
            return None

        def apply_remaining_weights(self, weights=None):
            return weights or {}

        def apply_credit_rates(self, per_day=None):
            return None

        def set_all_enabled(self, enabled, realm=None, group=None):
            return [a.uid for a in self.accounts]

    return Pool()


class Handler(object):
    """真实处理器方法 + 记录应答的桩。"""

    _handle_messages = proxy.Handler._handle_messages
    _channel_error = proxy.Handler._channel_error
    _anthropic_error = proxy.Handler._anthropic_error
    _anthropic_rate_limited = proxy.Handler._anthropic_rate_limited
    _messages_stream_response = proxy.Handler._messages_stream_response
    _messages_nonstream_response = proxy.Handler._messages_nonstream_response
    _route_model = proxy.Handler._route_model

    class _WFile(object):
        def __init__(self, sink):
            self.sink = sink

        def write(self, data):
            self.sink.append(data)

        def flush(self):
            pass

    def __init__(self, payload, realm="intl"):
        self.payload = payload
        self.realm = realm
        self.path = "/v1/messages"
        self.headers = {}
        self.status = []
        self.written = []
        self.reply = None
        self.audit = None

    @property
    def wfile(self):
        return Handler._WFile(self.written)

    def send_response(self, code):
        self.status.append(("status", code))

    def send_header(self, name, value):
        self.status.append((name, value))

    def end_headers(self):
        self.status.append(("end", None))

    def _handle_expect_continue(self):
        pass

    def _discard_body(self):
        pass

    def _json(self, code, obj):
        self.status.append(("status", code))
        self.written.append(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        self.reply = obj
        return code, obj

    def _error(self, code, message, err_type="server_error"):
        return self._json(code, {"error": {"message": message, "type": err_type}})

    def _request_route(self):
        return (self.realm,)

    def _cross_realm_error(self, model, realm):
        return ""

    def _banned_model_error(self, model, realm=""):
        return ""

    def _key_model_error(self, model):
        return ""

    def _key_id(self):
        return None

    def _debug_account(self):
        return ""


def messages_payload(model="deepseek-v4.1-flash"):
    return {"model": model, "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]}


class SlotLeaseHandlerTests(unittest.TestCase):
    """生产处理器必须真的占名额：账号上限 1 时，另一个模型在途也算满。"""

    def account(self):
        account = wb_accounts.Account({
            "uid": "uid-lease", "realm": "intl", "accessToken": "token"})
        account.concurrency_limit = 1
        return account

    def run_messages(self, account, model="deepseek-v4.1-flash"):
        handler = Handler(messages_payload(model))
        wb_settings.set_messages_format(os.environ["ACCOUNTS_DIR"],
                                        "openai-completions")
        self.assertEqual(wb_settings.messages_format(os.environ["ACCOUNTS_DIR"]),
                         "openai-completions")
        with mock.patch.multiple(proxy, POOL=stub_pool(account),
                                 ACCOUNTS_DIR=os.environ["ACCOUNTS_DIR"]), \
                mock.patch.object(wb_accounts, "urlopen",
                                  lambda *a, **k: FakeUpstream(sse(chat_chunks()))):
            handler._handle_messages(messages_payload(model))
        return handler

    def test_another_model_in_flight_counts_against_the_same_account(self):
        account = self.account()
        self.assertTrue(account.acquire_request("other-model"))
        try:
            handler = self.run_messages(account)
        finally:
            account.release_request("other-model")
        self.assertEqual(handler.status[0], ("status", 429))
        self.assertIn(("Retry-After", "1"), handler.status)
        self.assertEqual(account.active_requests, 0,
                         "被拒绝的请求不占名额")

    def test_the_handler_holds_then_releases_the_lease(self):
        account = self.account()
        handler = self.run_messages(account)
        self.assertEqual(handler.status[0], ("status", 200))
        self.assertEqual(account.active_requests, 0,
                         "响应体读完必须归还名额")

    def test_a_full_account_reports_429_not_an_empty_pool(self):
        account = self.account()
        self.assertTrue(account.acquire_request("glm-5.3"))
        try:
            handler = self.run_messages(account)
        finally:
            account.release_request("glm-5.3")
        self.assertEqual(handler.status[0], ("status", 429))
        self.assertNotIn("no usable account",
                         json.dumps(handler.reply, ensure_ascii=False))


class ChannelErrorTests(unittest.TestCase):
    def test_codearts_429_carries_retry_after_and_an_attributed_usage_row(self):
        log = os.path.join(_TMP, "usage.jsonl")
        if os.path.exists(log):
            os.remove(log)
        exc = wb_channels.ChannelError(
            "usage exceeds frequency limit", 429, "rate_limit_error",
            account_uid="codearts-uid", wait=3, outcome="rate_limited")
        handler = Handler(messages_payload(), realm="codearts")

        def refuse(*_args, **_kwargs):
            raise exc

        with mock.patch.object(wb_channels.CodeArtsProvider, "open_stream",
                               staticmethod(refuse)), \
                mock.patch.object(proxy, "USAGE_LOG", log), \
                mock.patch.object(proxy, "ACCOUNTS_DIR", os.environ["ACCOUNTS_DIR"]):
            handler._handle_messages(messages_payload())
        self.assertEqual(handler.status[0], ("status", 429))
        self.assertIn(("Retry-After", "3"), handler.status)
        body = json.loads(b"".join(handler.written).decode("utf-8"))
        self.assertEqual(body["type"], "error")
        self.assertEqual(body["error"]["type"], "rate_limit_error")
        with open(log, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        row = rows[-1]
        self.assertEqual(row["outcome"], "rate_limited")
        self.assertEqual(row["account"], "codearts-uid")
        self.assertEqual(row["status"], 429)

    def test_local_network_failure_answers_503_without_charging_an_account(self):
        exc = wb_channels.ChannelError(
            "本机网络不可用", 503, "api_error", account_uid="codearts-uid",
            local_network=True)
        handler = Handler(messages_payload(), realm="codearts")
        with mock.patch.object(wb_channels.CodeArtsProvider, "open_stream",
                               staticmethod(lambda *a, **k: (_ for _ in ()).throw(exc))), \
                mock.patch.object(proxy, "ACCOUNTS_DIR", os.environ["ACCOUNTS_DIR"]):
            handler._handle_messages(messages_payload())
        self.assertEqual(handler.status[0], ("status", 503))
        self.assertIn("本次不计入账号熔断",
                      handler.reply["error"]["message"])


class StandDownScopeTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(proxy.STAND_DOWN)

    def tearDown(self):
        proxy.STAND_DOWN.update(self._saved)

    def test_workbuddy_stand_down_does_not_block_codearts(self):
        proxy.STAND_DOWN.update({"active": True, "detail": "403"})
        upstream = FakeUpstream(sse(chat_chunks()))
        with mock.patch.object(wb_channels.CodeArtsProvider, "open_stream",
                               staticmethod(lambda *a, **k:
                                            (upstream, FakeAccount(), None))):
            got, account, _effort = proxy.open_upstream(
                messages_payload(), target_realm="codearts")
        self.assertIs(got, upstream)
        self.assertEqual(account.uid, "codearts-uid")

    def test_workbuddy_requests_still_stop(self):
        proxy.STAND_DOWN.update({"active": True, "detail": "403"})
        with self.assertRaises(proxy.GatewayStandDown):
            proxy.open_upstream(messages_payload(), target_realm="intl")


class AccountPanelTests(unittest.TestCase):
    class Request(object):
        _get_accounts = wb_panel_routes.PanelRoutes._get_accounts
        _route_accounts_set = wb_panel_routes.PanelRoutes._route_accounts_set

        def __init__(self):
            self.sent = None

        def _authorized(self):
            return True

        def _json(self, code, payload):
            self.sent = (code, payload)
            return self.sent

        def _error(self, code, message, err_type="server_error"):
            self.sent = (code, {"error": message})
            return self.sent

    @staticmethod
    def views():
        return [
            {"uid": "wb-uid", "nickname": "WorkBuddy", "realm": "intl",
             "provider": "workbuddy", "enabled": True},
            {"uid": "ca-uid", "nickname": "CodeArts", "realm": "codearts",
             "provider": "codearts", "enabled": True},
        ]

    def test_accounts_listing_covers_both_providers(self):
        request = self.Request()
        with mock.patch.object(wb_channels, "all_views",
                               side_effect=lambda realm=None: self.views()), \
                mock.patch.multiple(proxy, POOL=None,
                                    ACCOUNTS_DIR=os.environ["ACCOUNTS_DIR"]):
            request._get_accounts({"realm": ["all"]})
        status, payload = request.sent
        self.assertEqual(status, 200)
        self.assertEqual({a["provider"] for a in payload["accounts"]},
                         {"workbuddy", "codearts"})
        self.assertIn("limits", payload)
        self.assertIn("stand_down", payload)
        self.assertEqual(payload["stand_down"]["provider"], "workbuddy")

    def test_accounts_set_reaches_a_codearts_account(self):
        seen = {}

        class Provider(object):
            name = "codearts"

            def update(self, uid, changes):
                seen["uid"] = uid
                seen["changes"] = changes
                return {"uid": uid, "provider": "codearts",
                        "concurrencyLimit": changes.get("concurrencyLimit")}

        request = self.Request()
        with mock.patch.object(wb_channels, "owner",
                               side_effect=lambda uid: Provider()), \
                mock.patch.object(wb_channels, "all_views",
                                  side_effect=lambda realm=None: self.views()):
            request._route_accounts_set({
                "uid": "ca-uid", "concurrencyLimit": 4,
                "identity": "cli", "note": "n"})
        status, payload = request.sent
        self.assertEqual(status, 200)
        self.assertEqual(seen["uid"], "ca-uid")
        self.assertEqual(seen["changes"]["concurrencyLimit"], 4)
        self.assertEqual(seen["changes"]["identity"], "cli")
        self.assertEqual(payload["account"]["concurrencyLimit"], 4)


class SchedulerContractTests(unittest.TestCase):
    def test_codearts_mode_names_its_own_jobs_and_hours(self):
        import wb_scheduler
        class Pool(object):
            dir = os.environ["ACCOUNTS_DIR"]
            accounts = []
        scheduler = wb_scheduler.Scheduler(Pool())
        status = scheduler.status()
        self.assertIn("CodeArts 签到", status["mode_codearts"])
        self.assertIn("09:00", status["mode_codearts"])
        self.assertIn("22:00", status["mode_codearts"])


class PersistedRouteTests(unittest.TestCase):
    def write(self, directory, payload):
        path = os.path.join(directory, "active_realm.json")
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh)

    def load(self, directory, route=("intl",)):
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory,
                                 ROUTE=tuple(route),
                                 CURRENT_REALM=route[0]):
            return wb_usage.load_persisted_route()

    def test_a_legacy_realm_file_becomes_a_single_channel_route(self):
        with tempfile.TemporaryDirectory(prefix="wb-route-") as directory:
            self.write(directory, {"realm": "cn"})
            self.assertEqual(self.load(directory), ("cn",))

    def test_a_corrupt_file_falls_back_without_raising(self):
        with tempfile.TemporaryDirectory(prefix="wb-route-") as directory:
            self.write(directory, "{not json")
            self.assertEqual(self.load(directory), ("intl",))

    def test_a_file_without_a_usable_route_keeps_the_registered_first_channel(self):
        with tempfile.TemporaryDirectory(prefix="wb-route-") as directory:
            self.write(directory, {"realm": "eu"})
            self.assertEqual(self.load(directory), (wb_channels.ids()[0],))


class KeyRealmMigrationTests(unittest.TestCase):
    def test_a_legacy_realm_becomes_a_single_channel_route(self):
        entry = wb_settings._clean_key_entry({"key": "abc", "realm": "cn"})
        self.assertEqual(entry["route"], ["cn"])

    def test_an_explicit_empty_route_still_follows_the_panel_default(self):
        entry = wb_settings._clean_key_entry(
            {"key": "abc", "realm": "cn", "route": []})
        self.assertEqual(entry["route"], [])

    def test_an_unknown_legacy_realm_is_not_guessed(self):
        entry = wb_settings._clean_key_entry({"key": "abc", "realm": "eu"})
        self.assertEqual(entry["route"], [])


class SlotLeaseExceptionTests(unittest.TestCase):
    def setUp(self):
        proxy.clear_stand_down()

    def tearDown(self):
        proxy.clear_stand_down()

    def account(self):
        account = wb_accounts.Account({
            "uid": "uid-leak", "realm": "intl", "accessToken": "token"})
        account.concurrency_limit = 2
        return account

    def drive(self, account, error):
        pool = stub_pool(account)

        def urlopen(*_args, **_kwargs):
            raise error

        with mock.patch.multiple(proxy, POOL=pool,
                                 ACCOUNTS_DIR=os.environ["ACCOUNTS_DIR"]), \
                mock.patch.object(wb_accounts, "urlopen", urlopen):
            try:
                proxy.open_upstream(messages_payload(), target_realm="intl",
                                    lease=proxy.SlotLease())
            except Exception as exc:
                return exc
        return None

    def test_a_403_stand_down_hands_back_the_claimed_slot(self):
        import io
        import urllib.error
        account = self.account()
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 403, "restricted", {},
            io.BytesIO(b'{"code": 11140}'))
        raised = self.drive(account, error)
        proxy.clear_stand_down()
        self.assertIsInstance(raised, proxy.GatewayStandDown)
        self.assertEqual(account.active_requests, 0,
                         "停止开关触发时名额必须归还")

    def test_an_unexpected_status_hands_back_the_claimed_slot(self):
        import io
        import urllib.error
        account = self.account()
        error = urllib.error.HTTPError(
            "https://upstream.invalid", 400, "bad request", {},
            io.BytesIO(b"bad"))
        raised = self.drive(account, error)
        self.assertIs(error, raised)
        self.assertEqual(account.active_requests, 0,
                         "未预期状态码抛出时名额必须归还")


class GuardDispatchTests(unittest.TestCase):
    def test_codearts_dispatch_refreshes_guards_and_routing_inputs(self):
        calls = []
        upstream = FakeUpstream(sse(chat_chunks()))

        def open_stream(*_args, **_kwargs):
            calls.append("open")
            return upstream, FakeAccount(), None

        with mock.patch.object(wb_usage, "apply_daily_guards",
                               side_effect=lambda refresh=False: calls.append("guards")), \
                mock.patch.object(wb_usage, "apply_routing_inputs",
                                  side_effect=lambda refresh=False: calls.append("routing")), \
                mock.patch.object(wb_channels.CodeArtsProvider, "open_stream",
                                  staticmethod(open_stream)):
            proxy.open_upstream(messages_payload(), target_realm="codearts")
        self.assertEqual(calls, ["guards", "routing", "open"])


class StandDownBulkEnableTests(unittest.TestCase):
    class Request(object):
        _route_accounts_set_all = wb_panel_routes.PanelRoutes._route_accounts_set_all

        def __init__(self):
            self.sent = None

        def _json(self, code, payload):
            self.sent = (code, payload)
            return self.sent

        def _error(self, code, message, err_type="server_error"):
            self.sent = (code, {"error": message})
            return self.sent

    class Provider(object):
        def __init__(self, name, changed):
            self.name = name
            self.changed = changed

        def set_all_enabled(self, enabled, realm=None, group=None):
            return list(self.changed)

    def call(self, providers):
        request = self.Request()
        with mock.patch.object(wb_panel_routes, "_all_providers",
                               return_value=providers), \
                mock.patch.object(wb_channels, "all_views",
                                  return_value=[]), \
                mock.patch.object(proxy, "clear_stand_down") as clear:
            request._route_accounts_set_all({"enabled": True})
        return request, clear

    def test_a_codearts_bulk_enable_does_not_clear_workbuddy_stand_down(self):
        request, clear = self.call([
            self.Provider("workbuddy", []),
            self.Provider("codearts", ["ca-1"]),
        ])
        self.assertEqual(request.sent[0], 200)
        clear.assert_not_called()

    def test_a_workbuddy_bulk_enable_clears_stand_down(self):
        request, clear = self.call([
            self.Provider("workbuddy", ["wb-1"]),
            self.Provider("codearts", []),
        ])
        self.assertEqual(request.sent[0], 200)
        clear.assert_called_once()


class SchedulerJobTests(unittest.TestCase):
    def scheduler(self, directory):
        import wb_scheduler
        class Pool(object):
            dir = directory
            accounts = []
        return wb_scheduler.Scheduler(Pool())

    def test_codearts_keepalive_receives_the_per_account_jitter(self):
        import wb_codearts
        with tempfile.TemporaryDirectory(prefix="wb-sched-") as directory:
            scheduler = self.scheduler(directory)
            with mock.patch.object(wb_codearts, "keepalive",
                                   return_value={"refreshed": 0}) as keepalive:
                self.assertEqual(scheduler._run_codearts_keepalive(True), 0)
        gap = keepalive.call_args.kwargs["gap_sleep"]
        self.assertIs(gap.__self__, scheduler)
        self.assertEqual(gap.__func__, type(scheduler)._jitter)

    def test_disabled_workbuddy_accounts_are_not_kept_alive(self):
        with tempfile.TemporaryDirectory(prefix="wb-sched-") as directory:
            scheduler = self.scheduler(directory)

            class Account(object):
                uid = "wb-disabled"
                realm = "intl"
                tasks_enabled = True
                enabled = False
                expires_at = time.time() + 600
                last_error = ""

                def __init__(self):
                    self.refreshed = 0

                def refresh(self):
                    self.refreshed += 1
                    return True

                def can_daily_chat(self):
                    return False

            account = Account()
            scheduler.pool.accounts = [account]
            with mock.patch.object(scheduler, "_run_codearts_checkin",
                                   return_value=0), \
                    mock.patch.object(scheduler, "_run_codearts_keepalive",
                                      return_value=0):
                scheduler._run_cycle_inner("test", force=True)
            self.assertEqual(account.refreshed, 0)

            account.enabled = True
            with mock.patch.object(scheduler, "_run_codearts_checkin",
                                   return_value=0), \
                    mock.patch.object(scheduler, "_run_codearts_keepalive",
                                      return_value=0):
                scheduler._run_cycle_inner("test", force=True)
            self.assertEqual(account.refreshed, 1)

    def test_scheduled_checkin_uses_the_non_manual_per_account_path(self):
        import wb_codearts
        with tempfile.TemporaryDirectory(prefix="wb-sched-") as directory:
            wb_settings.set_codearts_config(directory, {"credits_auto_claim": True})
            scheduler = self.scheduler(directory)
            account = mock.Mock(uid="ca-1", disabled=False, tasks_enabled=True,
                                cool_until=0.0)
            account.checked_in_today.return_value = False
            pool = mock.Mock(accounts=[account])
            with mock.patch.object(wb_codearts, "pool_for", return_value=pool), \
                    mock.patch.object(wb_codearts, "checkin",
                                      return_value={"ok": True, "message": "已领取"}) as checkin:
                self.assertEqual(scheduler._run_codearts_checkin(True), 1)
        args, kwargs = checkin.call_args
        self.assertEqual(args[1], "ca-1")
        self.assertNotIn("manual", kwargs)


class StartupSummaryTests(unittest.TestCase):
    def test_the_account_log_covers_every_provider(self):
        import wb_startup
        gw = mock.Mock()
        gw.current_account.return_value = None
        gw.provider_counts.return_value = {"total": 2, "ready": 1,
                                           "by_provider": {}}
        gw.POOL.accounts = []
        gw.ACCOUNTS_DIR = "x"
        gw.PRODUCT_CONFIG_CACHE = os.path.join(_TMP, "missing")
        gw.configured_keys.return_value = []
        gw.API_KEY = None
        gw.ROUTE = ("codearts",)
        args = mock.Mock(host="127.0.0.1", port=1, api_key=None)

        class Provider(object):
            def __init__(self, name, views):
                self.name = name
                self._views = views

            def views(self):
                return self._views

        providers = [
            Provider("workbuddy", [{"uid": "wb-1", "nickname": "WB",
                                    "realm": "intl", "expiresIn": "1d"}]),
            Provider("codearts", [{"uid": "ca-1", "nickname": "CA",
                                   "realm": "codearts", "expiresIn": "2h"}]),
        ]
        with mock.patch.object(wb_startup, "_providers",
                               return_value=providers):
            wb_startup._log_startup_summary(gw, args, False)
        lines = [str(call.args[0]) for call in gw.log.call_args_list]
        self.assertTrue(any("2 total, 1 usable" in line for line in lines))
        self.assertTrue(any("codearts" in line and "1 account(s)" in line
                            for line in lines))
        self.assertTrue(any("ca-1" in line and "CA" in line for line in lines))


class SettingsContractTests(unittest.TestCase):
    class Request(object):
        _handle_settings_save = proxy.Handler._handle_settings_save

        def __init__(self, payload):
            self.payload = payload
            self.status = None
            self.reply = None

        def _payload_or_error(self):
            return self.payload

        def _error(self, status, message, err_type="server_error"):
            self.status = status
            self.reply = message
            return status, message

        def _json(self, status, payload):
            self.status = status
            self.reply = payload
            return status, payload

    def save(self, directory, payload):
        request = self.Request(payload)
        with mock.patch.multiple(proxy, ACCOUNTS_DIR=directory, POOL=None,
                                 SCHEDULER=None, PRICING=None,
                                 CREDITS_REFRESHER=None):
            request._handle_settings_save()
        return request

    def test_workbuddy_task_switch_round_trips(self):
        with tempfile.TemporaryDirectory(prefix="wb-contract-set-") as directory:
            request = self.save(directory, {"workbuddy_tasks_auto": False})
            self.assertEqual(request.status, 200)
            self.assertFalse(wb_settings.workbuddy_tasks_auto(directory))
            self.assertFalse(request.reply["workbuddy_tasks_auto"])

    def test_codearts_out_of_range_values_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="wb-contract-set-") as directory:
            for payload in (
                    {"codearts": {"stream_timeout_seconds": 0}},
                    {"codearts": {"models_ttl_seconds": 10}},
                    {"codearts": {"checkin_max_attempts": 11}},
                    {"codearts": {"checkin_timeout_seconds": 0}},
                    {"codearts": {"credits_auto_claim": "false"}},
                    {"codearts": {"max_concurrent": 3}}):
                request = self.save(directory, payload)
                self.assertEqual(request.status, 400, payload)

    def test_codearts_values_are_visible_after_save(self):
        with tempfile.TemporaryDirectory(prefix="wb-contract-set-") as directory:
            request = self.save(directory, {"codearts": {
                "stream_timeout_seconds": 120,
                "models_ttl_seconds": 600,
                "checkin_max_attempts": 4,
                "checkin_retry_gap_seconds": 1800,
                "checkin_timeout_seconds": 30,
                "credits_auto_claim": True,
            }})
            self.assertEqual(request.status, 200)
            cfg = request.reply["codearts"]
            self.assertEqual(cfg["stream_timeout_seconds"], 120)
            self.assertEqual(cfg["models_ttl_seconds"], 600)
            self.assertEqual(cfg["checkin_max_attempts"], 4)
            self.assertTrue(cfg["credits_auto_claim"])


class AnalyticsEnrichmentTests(unittest.TestCase):
    def test_zero_traffic_accounts_of_every_provider_are_listed(self):
        views = [
            {"uid": "wb-zero", "nickname": "WB", "realm": "intl",
             "credits": {"remain": 1.0}},
            {"uid": "ca-zero", "nickname": "CA", "realm": "codearts",
             "credits": {"remain": 2.0}},
        ]
        with mock.patch.object(wb_channels, "all_views",
                               side_effect=lambda realm=None: views):
            data = wb_usage.compute_usage_analytics(realm=None)
        listed = {row["uid"]: row for row in data["accounts"]}
        self.assertIn("wb-zero", listed)
        self.assertIn("ca-zero", listed)
        self.assertEqual(listed["ca-zero"]["nickname"], "CA")
        self.assertEqual(listed["ca-zero"]["credits"]["remain"], 2.0)


class MissingCreditTests(unittest.TestCase):
    def test_a_row_without_credit_stays_missing_not_zero(self):
        row = wb_usage.record_usage(
            "codearts-model", {"prompt_tokens": 3, "completion_tokens": 1,
                               "total_tokens": 4})
        self.assertIsNone(row["credit"])
        self.assertNotIn(0, [row["credit"]])

    def test_cost_estimate_marks_uncollected_credit_and_skips_the_saving(self):
        out = wb_cost.estimate(
            {"ca-model": {"requests": 2, "prompt_tokens": 1000,
                          "completion_tokens": 500, "total_tokens": 1500,
                          "credit": 0, "credit_missing": 2}},
            {"credit_price": 1.0,
             "models": [{"model": "ca-model", "input": 1.0,
                         "cached_input": 1.0, "output": 1.0}]})
        row = out["models"][0]
        self.assertTrue(row["priced"])
        self.assertAlmostEqual(row["official_cny"], 0.0015)
        self.assertIsNone(row["credit"])
        self.assertTrue(row["credit_missing"])
        self.assertIsNone(row["saved_cny"])
        self.assertIsNone(row["paid_cny"])
        self.assertEqual(out["summary"]["official_cny"], 0.0015)
        self.assertEqual(out["summary"]["priced_models"], 1)
        self.assertEqual(out["summary"]["credit_missing"], 1)
        self.assertEqual(out["summary"]["saved_cny"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
