"""Offline header-boundary checks across gateway, quota router and account proxies.

Only loopback fake providers receive requests. These checks do not measure a
cache percentage or transfer a cache between accounts.
"""
from contextlib import asynccontextmanager, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

import httpx
from fastapi import FastAPI
import uvicorn
import yaml

import chatgpt_session_id as hook
import quota_router
from test_chatgpt_auth_file import Account, free_port, sse
from test_chatgpt_session_id import Upstream, SYSTEM, FIRST, ANSWER, SECOND

ROOT = Path(__file__).resolve().parents[1]
ALIASES = {"luna": "gpt-6-luna", "gpt-6-luna": "gpt-6-luna", "sol": "gpt-6.1-sol",
           "gpt-6.1-sol": "gpt-6.1-sol", "gpt-6-sol": "gpt-6-sol",
           "astra": "gpt-6-astra", "gpt-6-astra": "gpt-6-astra"}
TOOLS = [{"type": "function", "function": {
    "name": "probe", "description": "Return a probe", "parameters": {"type": "object", "properties": {}}}}]


def provider_stream(body):
    """Return actual tool events so the client can verify the stream after both hops."""
    completed = json.loads(sse("ok").split("data: ", 1)[1])["response"]
    completed["model"] = body["model"]
    events = []
    if body.get("tools"):
        item = {"type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                "name": "probe", "arguments": "{}", "status": "completed"}
        events = [{"type": "response.output_item.added", "output_index": 0, "item": item},
                  {"type": "response.output_item.done", "output_index": 0, "item": item}]
        completed["output"] = [item]
    events.append({"type": "response.completed", "response": completed})
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


class LocalAccountsTransport(httpx.AsyncBaseTransport):
    """Change only the connection destination; keep the real quota/account HTTP path."""

    def __init__(self, bases, accounts, usage):
        self.transport = httpx.AsyncHTTPTransport()
        self.bases = bases
        self.names = {account.account_id: name for name, account in accounts.items()}
        self.usage = usage
        self.attempts = []

    async def handle_async_request(self, request):
        if str(request.url) == quota_router.USAGE_URL:
            name = self.names[request.headers["ChatGPT-Account-Id"]]
            used = self.usage.get(name, 0)
            return httpx.Response(200, json={"rate_limit": {
                "allowed": used < 100, "limit_reached": used >= 100,
                "primary_window": {"used_percent": used, "reset_at": 10000},
                "secondary_window": None}}, request=request)
        name = request.url.host
        if name not in self.bases:
            raise AssertionError("non-local quota-router destination")
        self.attempts.append({"account": name, "body": json.loads(await request.aread())})
        request.url = httpx.URL(self.bases[name] + request.url.raw_path.decode())
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


class CacheContinuityTests(unittest.TestCase):
    """Use real LiteLLM proxies, generated native routes and the actual quota app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="litellm-test-cache-continuity-")
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.root = Path(cls.tmp.name)
        cls.key = "sk-synthetic-router-order"
        cls.accounts = {}
        tokens = cls.root / "tokens"
        tokens.mkdir()
        for name in ("codex1", "codex2", "codex3"):
            account = cls.accounts[name] = Account(tokens, name)
            data = json.loads(account.file.read_text())
            data["account_id"] = account.account_id
            account.file.write_text(json.dumps(data))
        cls.upstream = Upstream(response_sse=provider_stream)
        cls.addClassCleanup(cls.upstream.close)
        cls.logs = []
        cls.bases = {}
        cls.start_script = cls.root / "start-proxy.py"
        # Local test controls reset state between cases or simulate cooldown expiry.
        # They do not change the router's policy or the service configuration.
        cls.start_script.write_text('''import sys, time
from litellm.proxy import proxy_server
from litellm.proxy.proxy_cli import run_server

@proxy_server.app.post("/test/router-state/{action}")
def state(action: str):
    router = proxy_server.llm_router
    cache = router.cooldown_cache.in_memory_cache
    if action == "reset":
        router.cache.flush_cache()
        router.fail_calls.clear()
        cache.flush_cache()
    elif action == "expire":
        for value in cache.cache_dict.values():
            value["timestamp"] = time.time() - value["cooldown_time"] - 1
    else:
        raise AssertionError("unknown test action")
    return {"ok": True}

sys.exit(run_server())
''')
        processes = []
        for name, account in cls.accounts.items():
            config = yaml.safe_load((ROOT / "config/codex.yaml").read_text())
            config["general_settings"]["master_key"] = cls.key
            process, base = cls.start_proxy(name, config, account.dir)
            cls.bases[name] = base
            processes.append((process, base))
        for process, base in processes:
            cls.wait_ready(process, base)

        cls.now = [1000]
        cls.usage = {}
        cls.transport = LocalAccountsTransport(cls.bases, cls.accounts, cls.usage)

        @asynccontextmanager
        async def lifespan(app):
            async with httpx.AsyncClient(transport=cls.transport) as client:
                app.state.key = cls.key
                app.state.client = client
                app.state.router = quota_router.Router(client, quota_router.account_order(), tokens,
                                                       clock=lambda: cls.now[0])
                yield

        # Original routes and real Quota decisions; only lifespan dependencies are local fakes.
        cls.quota_app = FastAPI(routes=quota_router.app.routes, lifespan=lifespan)
        port = free_port()
        cls.quota_base = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(cls.quota_app, host="127.0.0.1", port=port,
                                              log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        def stop_quota():
            server.should_exit = True
            thread.join(30)
            if thread.is_alive():
                raise AssertionError("test quota server did not stop")
        cls.addClassCleanup(stop_quota)
        cls.wait_ready(None, cls.quota_base)

        spec = importlib.util.spec_from_file_location("enable_codex_cache", ROOT / "scripts/enable-codex.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        processes = []
        cls.gateways = {}
        for mode in ("native3", "native2", "quota"):
            synthetic = cls.root / mode
            for number in (1, 2, 3):
                path = synthetic / f"state/codex{number}/auth.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"access_token": "test", "refresh_token": "test"}))
            config_path = synthetic / ".local/config/gateway.yaml"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"model_list": [], "router_settings": {"num_retries": 0},
                                                "litellm_settings": {"num_retries": 0, "set_verbose": False}}))
            with patch.object(generator, "ROOT", synthetic), patch.dict(os.environ), redirect_stdout(io.StringIO()):
                os.environ.pop("GATEWAY_CONFIG", None)
                generator.main(("--accounts", "2" if mode == "native2" else "3"))
            config = json.loads(config_path.read_text())
            for entry in config["model_list"]:
                params = entry["litellm_params"]
                account = httpx.URL(params["api_base"]).host
                params["api_base"] = cls.bases[account] + "/v1"
                if mode == "quota" and entry["model_name"].startswith("codex-auto/"):
                    params["api_base"] = cls.quota_base + "/v1"
            if mode == "quota":
                config["router_settings"].pop("fallbacks")
            config["general_settings"] = {"master_key": cls.key, "disable_spend_logs": True}
            assert "missing_session_id" not in config["general_settings"]
            process, base = cls.start_proxy(mode, config, cls.root / "unused")
            cls.gateways[mode] = base
            processes.append((process, base))
        for process, base in processes:
            cls.wait_ready(process, base)

    @classmethod
    def start_proxy(cls, name, config, token_dir):
        path = cls.root / f"{name}.yaml"
        path.write_text(yaml.safe_dump(config))
        log = open(cls.root / f"{name}.log", "w+")
        cls.logs.append(log)
        cls.addClassCleanup(log.close)
        port = free_port()
        # Do not inherit host keys, config paths or credential settings.
        env = {name: os.environ[name] for name in ("PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE") if name in os.environ}
        env.update({"HOME": str(cls.root), "CHATGPT_AUTH_FILE_HOOK": "off", "CHATGPT_TOKEN_DIR": str(token_dir),
                    "CHATGPT_API_BASE": cls.upstream.base, "CODEX_MASTER_KEY": cls.key,
                    "LITELLM_MASTER_KEY": cls.key, "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                    "LITELLM_TELEMETRY": "False", "LITELLM_DISABLE_NO_REDIS_WARNING": "true"})
        process = subprocess.Popen(
            [sys.executable, str(cls.start_script),
             "--config", str(path), "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(process.wait, 30)
        cls.addClassCleanup(process.terminate)
        return process, f"http://127.0.0.1:{port}"

    @classmethod
    def wait_ready(cls, process, base):
        deadline = time.monotonic() + 120
        while True:
            try:
                with urlopen(base + "/health/liveliness", timeout=2):
                    return
            except OSError:
                if (process is not None and process.poll() is not None) or time.monotonic() > deadline:
                    raise AssertionError("local test proxy did not start")
                time.sleep(0.1)

    def native_state(self, action="reset", base=None):
        with httpx.Client(timeout=30, trust_env=False) as client:
            for target in ([base] if base else [*self.bases.values(), *self.gateways.values()]):
                response = client.post(target + f"/test/router-state/{action}",
                                       headers={"Authorization": "Bearer " + self.key})
                self.assertEqual(response.status_code, 200, response.text[-500:])

    def setUp(self):
        self.native_state()
        self.upstream.requests.clear()
        self.upstream.status_by_account.clear()
        self.transport.attempts.clear()
        self.usage.clear()
        self.now[0] = 1000
        self.quota_app.state.router = quota_router.Router(
            self.quota_app.state.client, quota_router.account_order(), self.root / "tokens", clock=lambda: self.now[0])

    def body(self, alias="luna", grown=False, **extra):
        return {"model": f"codex-auto/{alias}", "messages": [SYSTEM, FIRST, ANSWER, SECOND] if grown else [SYSTEM, FIRST],
                "stream": True, **extra}

    def post(self, base, body):
        self.upstream.requests.clear()
        self.transport.attempts.clear()
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(base + "/v1/chat/completions", json=body,
                                   headers={"Authorization": "Bearer " + self.key})
        self.assertEqual(response.status_code, 200, response.text[-1000:])
        self.assertIn("data: [DONE]", response.text)
        if body.get("tools"):
            compact = response.text.replace(" ", "")
            self.assertIn('"finish_reason":"tool_calls"', compact)
            self.assertIn('"name":"probe"', compact)
            self.assertIn('"arguments":"{}"', compact)
        requests = copy.deepcopy(self.upstream.requests)
        self.assertTrue(requests, "no fake-provider request")
        if SECOND in body.get("messages", []):
            for request in requests:
                self.assertIn(SECOND["content"], json.dumps(request["body"]["input"]))
        return requests, response

    def names(self, requests):
        names = {account.account_id: name for name, account in self.accounts.items()}
        return [names[request["account_id"][0]] for request in requests]

    def assert_boundary(self, requests, model, expected=None, tools=False):
        ids = [request["session_id"] for request in requests]
        if expected is None:
            # LiteLLM can normalize the system content when tools are present.
            # Check the observed header across attempts and growing turns instead.
            expected = ids[0] if tools else [hook.cache_session_id({"model": model, "messages": [SYSTEM, FIRST]})]
        self.assertEqual(len(expected), 1)
        self.assertEqual(ids, [expected] * len(requests))
        self.assertTrue(expected[0].startswith("prefix-") or expected == ["caller-session"])
        for request in requests:
            self.assertEqual(request["path"], "/responses")
            account = next(account for account in self.accounts.values()
                           if request["account_id"] == [account.account_id])
            self.assertEqual(request["authorization"], ["Bearer " + account.access])
            self.assertEqual(request["body"]["model"], model)
            self.assertNotIn("litellm_session_id", request["body"])
            self.assertNotIn("session_id", request["body"])
            self.assertEqual(request["body"]["stream"], True)
            self.assertIn(FIRST["content"], json.dumps(request["body"]["input"]))
            if tools:
                self.assertEqual([tool["name"] for tool in request["body"]["tools"]], ["probe"])
                self.assertEqual(request["body"]["tool_choice"], "required")
                self.assertEqual(request["body"]["reasoning"]["effort"], "xhigh")
        return expected

    def test_native_three_accounts_keep_primary_and_header_for_every_alias(self):
        for alias, model in ALIASES.items():
            for grown in (False, False, True):
                with self.subTest(alias=alias, grown=grown):
                    requests, _ = self.post(self.gateways["native3"], self.body(alias, grown))
                    self.assertEqual(self.names(requests), ["codex3"])
                    self.assert_boundary(requests, model)

    def test_native_two_accounts_keep_primary_and_header_for_every_alias(self):
        for alias, model in ALIASES.items():
            for grown in (False, False, True):
                with self.subTest(alias=alias, grown=grown):
                    requests, _ = self.post(self.gateways["native2"], self.body(alias, grown))
                    self.assertEqual(self.names(requests), ["codex2"])
                    self.assert_boundary(requests, model)

    def test_explicit_routes_keep_account_model_and_header(self):
        for account in self.accounts:
            for alias, model in ALIASES.items():
                first = None
                for grown in (False, True):
                    with self.subTest(account=account, alias=alias, grown=grown):
                        body = {**self.body(alias, grown), "model": f"{account}/{alias}"}
                        requests, _ = self.post(self.gateways["native3"], body)
                        self.assertEqual(self.names(requests), [account])
                        first = self.assert_boundary(requests, model, first)

    def test_native_429_cascade_keeps_header_model_and_subsequent_turn(self):
        for account in ("codex3", "codex2"):
            self.upstream.status_by_account[self.accounts[account].account_id] = 429
        requests, response = self.post(self.gateways["native3"], self.body("luna", tools=TOOLS,
                                                                                tool_choice="required", reasoning_effort="xhigh"))
        self.assertEqual(self.names(requests), ["codex3", "codex2", "codex1"])
        expected = self.assert_boundary(requests, "gpt-6-luna", tools=True)
        self.assertIn('"tool_calls"', response.text)
        for grown in (False, True):
            requests, response = self.post(self.gateways["native3"], self.body("luna", grown, tools=TOOLS,
                                                                                  tool_choice="required", reasoning_effort="xhigh"))
            self.assertEqual(self.names(requests)[-1], "codex1")
            self.assertEqual(len(self.names(requests)), len(set(self.names(requests))), "native fallback loop")
            self.assert_boundary(requests, "gpt-6-luna", expected, tools=True)
            self.assertIn('"tool_calls"', response.text)
        self.upstream.status_by_account.clear()
        self.native_state("expire", self.gateways["native3"])
        requests, _ = self.post(self.gateways["native3"], self.body("luna", True, tools=TOOLS,
                                                                    tool_choice="required", reasoning_effort="xhigh"))
        self.assertEqual(self.names(requests), ["codex3"])
        self.assert_boundary(requests, "gpt-6-luna", expected, tools=True)

    def test_native_two_account_fallback_keeps_header_on_subsequent_turns(self):
        self.upstream.status_by_account[self.accounts["codex2"].account_id] = 429
        requests, _ = self.post(self.gateways["native2"], self.body("sol"))
        self.assertEqual(self.names(requests), ["codex2", "codex1"])
        expected = self.assert_boundary(requests, "gpt-6.1-sol")
        for grown in (False, True):
            requests, _ = self.post(self.gateways["native2"], self.body("sol", grown))
            self.assertEqual(self.names(requests)[-1], "codex1")
            self.assert_boundary(requests, "gpt-6.1-sol", expected)

    def test_native_fallback_aliases_keep_provider_model_and_prefix(self):
        for account in ("codex3", "codex2"):
            self.upstream.status_by_account[self.accounts[account].account_id] = 429
        for alias, model in ALIASES.items():
            self.native_state()
            with self.subTest(alias=alias):
                requests, _ = self.post(self.gateways["native3"], self.body(alias))
                self.assertEqual(self.names(requests), ["codex3", "codex2", "codex1"])
                expected = self.assert_boundary(requests, model)
                requests, _ = self.post(self.gateways["native3"], self.body(alias, True))
                self.assertEqual(self.names(requests)[-1], "codex1")
                self.assert_boundary(requests, model, expected)

    def test_native_gateway_caller_ids_do_not_replace_the_account_prefix(self):
        for keys in ({"litellm_session_id": "caller-session"}, {"metadata": {"session_id": "caller-session"}}):
            for grown in (False, True):
                requests, _ = self.post(self.gateways["native2"], self.body("sol", grown, **keys))
                expected = self.assert_boundary(requests, "gpt-6.1-sol")
                self.assertNotEqual(expected, ["caller-session"])

    def test_quota_gateway_keeps_primary_and_header_for_repeated_turns(self):
        for selected in ("codex3", "codex2"):
            self.quota_app.state.router.quotas["codex3"].token_path = (
                self.accounts["codex3"].file if selected == "codex3" else self.root / "missing-login.json")
            for alias, model in ALIASES.items():
                for grown in (False, False, True):
                    with self.subTest(selected=selected, alias=alias, grown=grown):
                        requests, _ = self.post(self.gateways["quota"], self.body(alias, grown))
                        self.assertEqual(self.names(requests), [selected])
                        self.assertEqual([attempt["account"] for attempt in self.transport.attempts], [selected])
                        self.assert_boundary(requests, model)
            if selected == "codex2":
                self.quota_app.state.router.quotas["codex3"].token_path = self.accounts["codex3"].file
                requests, _ = self.post(self.gateways["quota"], self.body("sol", True))
                self.assertEqual(self.names(requests), ["codex3"])
                self.assert_boundary(requests, "gpt-6.1-sol")

    def test_quota_429_cascade_keeps_tools_header_and_preempts_after_recovery(self):
        for account in ("codex3", "codex2"):
            self.upstream.status_by_account[self.accounts[account].account_id] = 429
        expected = None
        for grown, expected_accounts in ((False, ["codex3", "codex2", "codex1"]), (True, ["codex1"]), (True, ["codex1"])):
            requests, response = self.post(self.gateways["quota"], self.body("gpt-6-sol", grown, tools=TOOLS,
                                                                                 tool_choice="required", reasoning_effort="xhigh"))
            self.assertEqual(self.names(requests), expected_accounts)
            self.assertEqual([attempt["account"] for attempt in self.transport.attempts], expected_accounts)
            expected = self.assert_boundary(requests, "gpt-6-sol", expected, tools=True)
            self.assertIn('"tool_calls"', response.text)
        self.upstream.status_by_account.clear()
        self.now[0] += quota_router.CACHE_SECONDS
        requests, _ = self.post(self.gateways["quota"], self.body("gpt-6-sol", True, tools=TOOLS,
                                                                  tool_choice="required", reasoning_effort="xhigh"))
        self.assertEqual(self.names(requests), ["codex3"])
        self.assert_boundary(requests, "gpt-6-sol", expected, tools=True)

    def test_quota_exhaustion_skips_accounts_without_rotating_eligible_preference(self):
        self.usage["codex3"] = 100
        for grown in (False, False, True):
            requests, _ = self.post(self.gateways["quota"], self.body("astra", grown))
            self.assertEqual(self.names(requests), ["codex2"])
            self.assert_boundary(requests, "gpt-6-astra")
        self.usage["codex2"] = 100
        self.now[0] += quota_router.CACHE_SECONDS
        requests, _ = self.post(self.gateways["quota"], self.body("astra", True))
        self.assertEqual(self.names(requests), ["codex1"])
        self.assert_boundary(requests, "gpt-6-astra")
        self.usage.clear()
        self.now[0] += quota_router.CACHE_SECONDS
        requests, _ = self.post(self.gateways["quota"], self.body("astra", True))
        self.assertEqual(self.names(requests), ["codex3"])
        self.assert_boundary(requests, "gpt-6-astra")

    def test_quota_gateway_caller_id_is_dropped_but_prefix_stays_stable(self):
        for grown in (False, True):
            requests, _ = self.post(self.gateways["quota"], self.body("luna", grown, litellm_session_id="caller-session"))
            self.assertEqual(self.names(requests), ["codex3"])
            self.assert_boundary(requests, "gpt-6-luna")

    def test_proxy_logs_do_not_leak_synthetic_tokens_or_prompt(self):
        self.post(self.gateways["quota"], self.body())
        for log in self.logs:
            log.seek(0)
            output = log.read()
            for account in self.accounts.values():
                for token in account.secrets():
                    self.assertNotIn(token, output)
            self.assertNotIn(FIRST["content"], output)

    def test_direct_quota_caller_id_reaches_provider_on_all_attempts(self):
        for account in ("codex3", "codex2"):
            self.upstream.status_by_account[self.accounts[account].account_id] = 429
        for grown in (False, True):
            body = {**self.body("luna", grown), "model": "gpt-6-luna", "litellm_session_id": "caller-session"}
            requests, _ = self.post(self.quota_base, body)
            self.assertEqual(self.names(requests), ["codex3", "codex2", "codex1"] if not grown else ["codex1"])
            self.assert_boundary(requests, "gpt-6-luna", ["caller-session"])
