"""Offline tests of image/hooks/chatgpt_auth_file.py.

All tokens are synthetic and built at run time. No test opens a network
connection: the ChatGPT backend and the OAuth refresh endpoint are fakes.
"""
import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import litellm
from litellm.exceptions import AuthenticationError
from litellm.llms.chatgpt import authenticator as upstream_auth
from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.auth import auth_utils

import chatgpt_auth_file as hook

# The hook is off by default, and sitecustomize.py left it off. These tests
# need it on. install() is idempotent. SwitchTests check the off state in
# separate interpreters.
with patch.dict(os.environ, {hook.SWITCH_ENV: "on"}):
    hook.install()

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "image" / "hooks"
SCRIPTS = ROOT / "scripts"
EXAMPLE = ROOT / "config" / "gateway.codex-accounts.example.yaml"
CLAIM = "https://api.openai.com/auth"


def b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def jwt(account_id, exp):
    """A synthetic JWT: the hook and upstream decode it, nobody verifies it."""
    return ".".join([b64({"alg": "none"}),
                     b64({"exp": exp, CLAIM: {"chatgpt_account_id": account_id},
                          "nonce": secrets.token_hex(8)}),
                     secrets.token_urlsafe(16)])


class Account:
    """One synthetic account with an auth file under the allowed root."""

    def __init__(self, root, name, expired=False):
        self.name = name
        self.account_id = f"acct-{name}-{secrets.token_hex(4)}"
        self.dir = Path(root) / name
        self.dir.mkdir(mode=0o700)
        self.file = self.dir / "auth.json"
        self.access = jwt(self.account_id, int(time.time()) + (-3600 if expired else 3600))
        self.refresh = "rt-" + secrets.token_urlsafe(24)
        self.id_token = jwt(self.account_id, int(time.time()) + 3600)
        self.file.write_text(json.dumps({"access_token": self.access, "refresh_token": self.refresh,
                                         "id_token": self.id_token}))
        self.file.chmod(0o600)

    def secrets(self):
        return [self.access, self.refresh, self.id_token]

    def digest(self):
        return hashlib.sha256(self.file.read_bytes()).hexdigest()


class FakeRefresh:
    """Fake OAuth token endpoint. Each refresh token maps to one account."""

    def __init__(self, accounts, status=200, delay=0.0):
        self.by_refresh = {a.refresh: a for a in accounts}
        self.status = status
        self.delay = delay
        self.calls = []
        self.issued = {}

    def post(self, url, json=None, **kwargs):
        self.calls.append(json["refresh_token"])
        time.sleep(self.delay)
        request = httpx.Request("POST", url)
        account = self.by_refresh[json["refresh_token"]]
        if self.status != 200:
            # An error body that echoes a token: the hook must not repeat it.
            return httpx.Response(self.status, json={"error": "invalid_grant", "echo": account.refresh},
                                  request=request)
        new = jwt(account.account_id, int(time.time()) + 3600)
        self.issued[account.name] = new
        return httpx.Response(200, json={"access_token": new, "id_token": account.id_token,
                                         "refresh_token": account.refresh}, request=request)


def sse(text):
    body = {"id": "resp_" + secrets.token_hex(4), "object": "response", "created_at": 1,
            "status": "completed", "model": "gpt-6-luna",
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
    return f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': body})}\n\n"


class FakeChatGPT:
    """Fake ChatGPT backend. It answers with the account id it received."""

    def __init__(self, status_by_account=None):
        self.requests = []
        self.status_by_account = status_by_account or {}

    async def post(self, handler, url, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        account_id = headers.get("ChatGPT-Account-Id")
        self.requests.append({"url": url, "account_id": account_id,
                              "authorization": headers.get("Authorization")})
        await asyncio.sleep(0.02)
        request = httpx.Request("POST", url)
        status = self.status_by_account.get(account_id, 200)
        if status != 200:
            response = httpx.Response(status, json={"error": {"message": "limit", "type": "usage_limit_reached"}},
                                      request=request)
            raise httpx.HTTPStatusError("limit", request=request, response=response)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(account_id),
                              request=request)


def no_login():
    """Patches that fail the test if any code path starts the device-code login."""
    def boom(*args, **kwargs):
        raise AssertionError("device-code login started")
    return [patch.object(upstream_auth.Authenticator, "_request_device_code", boom),
            patch.object(upstream_auth.Authenticator, "_login_device_code", boom)]


class HookCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="litellm-test-codex-accounts-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.root = base / "tokens"
        self.root.mkdir(mode=0o700)
        self.process_dir = base / "process"
        self.process_dir.mkdir(mode=0o700)
        self.outside = base / "outside"
        self.outside.mkdir(mode=0o700)
        env = patch.dict(os.environ, {"CHATGPT_AUTH_FILE_ROOT": str(self.root),
                                      "CHATGPT_TOKEN_DIR": str(self.process_dir)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(hook.REQUIRED_ENV, None)
        hook._authenticators.clear()
        self.addCleanup(hook._authenticators.clear)
        for p in no_login():
            p.start()
            self.addCleanup(p.stop)

    def refresh(self, fake):
        p = patch.object(upstream_auth, "_get_httpx_client", return_value=fake)
        p.start()
        self.addCleanup(p.stop)

    def backend(self, fake):
        async def post(handler, url, *args, **kwargs):
            return await fake.post(handler, url, *args, **kwargs)
        p = patch.object(AsyncHTTPHandler, "post", post)
        p.start()
        self.addCleanup(p.stop)

    def router(self, deployments, **settings):
        model_list = [{"model_name": group,
                       "litellm_params": {"model": "chatgpt/responses/gpt-6-luna", **params},
                       "model_info": {"id": f"{group}-{i}"}}
                      for i, (group, params) in enumerate(deployments)]
        return litellm.Router(model_list=model_list, **settings)

    @staticmethod
    def ask(router, model):
        """Send one Responses API request and return the output text."""
        async def run():
            return await HookCase.collect(await router.aresponses(model=model, input="hi"))
        return asyncio.run(run())

    @staticmethod
    async def collect(response):
        # ChatGPT always streams; LiteLLM may hand back the stream iterator.
        if not hasattr(response, "output"):
            async for event in response:
                if getattr(event, "type", None) == "response.completed":
                    response = event.response
                    break
        return response.output[0].content[0].text


class ClientRequestTests(HookCase):
    """Condition: a client request cannot set chatgpt_auth_file."""

    BODIES = {
        "root": {"chatgpt_auth_file": "b/auth.json"},
        "extra_body": {"extra_body": {"chatgpt_auth_file": "b/auth.json"}},
        "extra_body_string": {"extra_body": json.dumps({"chatgpt_auth_file": "b/auth.json"})},
        "metadata": {"metadata": {"chatgpt_auth_file": "b/auth.json"}},
        "metadata_string": {"metadata": json.dumps({"chatgpt_auth_file": "b/auth.json"})},
        "litellm_metadata": {"litellm_metadata": {"chatgpt_auth_file": "b/auth.json"}},
        "form_metadata": {"metadata[chatgpt_auth_file]": "b/auth.json"},
        "litellm_params": {"litellm_params": {"chatgpt_auth_file": "b/auth.json"}},
        "litellm_params_metadata": {"litellm_params": {"metadata": {"chatgpt_auth_file": "b/auth.json"}}},
        "fallback_target": {"fallbacks": [{"model": "gpt-6-luna", "chatgpt_auth_file": "b/auth.json"}]},
        "router_settings_override": {"router_settings_override": {
            "fallbacks": [{"gpt-6-luna": [{"model": "x", "chatgpt_auth_file": "b/auth.json"}]}]}},
        "user_config": {"user_config": {"model_list": [{"model_name": "x", "litellm_params": {
            "model": "chatgpt/responses/gpt-6-luna", "chatgpt_auth_file": "b/auth.json"}}]}},
        "input_item": {"input": [{"role": "user", "content": "hi", "chatgpt_auth_file": "b/auth.json"}]},
        "deep": {"a": [{"b": {"c": [{"d": {"chatgpt_auth_file": "b/auth.json"}}]}}]},
    }

    # Too deep to check: HTTP 400, not a RecursionError (which the proxy turns into 401).
    DEEP = {
        "json_text": {"metadata": "[" * 100_000 + '"chatgpt_auth_file"'},
        "nested_dicts": {"metadata": json.dumps(json.loads("[" * 40 + "1" + "]" * 40))
                         .replace("1", '{"chatgpt_auth_file": "b/auth.json"}')},
    }
    # A prompt that quotes the key as JSON text is text, not a parameter.
    PROMPTS = {
        "messages": {"messages": [{"role": "user", "content": json.dumps({"chatgpt_auth_file": "b/auth.json"})}]},
        "message_parts": {"messages": [{"role": "user", "content": [
            {"type": "text", "text": json.dumps({"chatgpt_auth_file": "b/auth.json"})}]}]},
        "input": {"input": json.dumps({"chatgpt_auth_file": "b/auth.json"})},
        "input_items": {"input": [{"role": "user", "content": json.dumps({"chatgpt_auth_file": "x"})}]},
        "prompt": {"prompt": json.dumps({"chatgpt_auth_file": "b/auth.json"})},
        "instructions": {"instructions": json.dumps({"chatgpt_auth_file": "b/auth.json"})},
        "system": {"system": json.dumps([{"chatgpt_auth_file": "b/auth.json"}])},
    }

    def check(self, body, general_settings=None, message="chatgpt_auth_file is not allowed"):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            auth_utils.is_request_body_safe({"model": "gpt-6-luna", "input": "hi", **body},
                                            general_settings or {}, None, "gpt-6-luna")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn(message, caught.exception.detail["error"])

    def test_too_deep_gives_400(self):
        for name, body in self.DEEP.items():
            with self.subTest(path=name):
                self.check(body, message="nests values too deeply")

    def test_prompt_with_json_text_passes(self):
        for name, body in self.PROMPTS.items():
            with self.subTest(path=name):
                self.assertTrue(auth_utils.is_request_body_safe({"model": "gpt-6-luna", **body}, {}, None,
                                                                "gpt-6-luna"))
        # The same text in a parameter field is still refused.
        self.check({"metadata": self.PROMPTS["input"]["input"]})

    def test_client_request_rejects_each_path(self):
        for name, body in self.BODIES.items():
            with self.subTest(path=name):
                self.check(body)

    def test_admin_opt_in_does_not_allow_the_key(self):
        # allow_client_side_credentials opens api_base and others; never this key.
        self.check(self.BODIES["root"], {"allow_client_side_credentials": True})

    def test_request_without_key_passes(self):
        body = {"model": "gpt-6-luna", "input": "the text chatgpt_auth_file is fine in content"}
        self.assertTrue(auth_utils.is_request_body_safe(body, {}, None, "gpt-6-luna"))

    def test_proxy_returns_400(self):
        """The proxy dependency user_api_key_auth turns the rejection into HTTP 400."""
        from fastapi import FastAPI, Depends
        from fastapi.testclient import TestClient
        from litellm.proxy import proxy_server
        from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

        app = FastAPI()

        @app.post("/v1/responses")
        @app.post("/v1/chat/completions")
        async def endpoint(auth=Depends(user_api_key_auth)):
            return {"ok": True}

        from litellm.proxy._types import ProxyException

        @app.exception_handler(ProxyException)
        async def handle(request, exc):
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": exc.message}, status_code=int(exc.code))

        key = "sk-" + secrets.token_hex(8)
        with patch.object(proxy_server, "master_key", key), \
                patch.object(proxy_server, "general_settings", {}), \
                patch.object(proxy_server, "prisma_client", None):
            client = TestClient(app)
            for route in ("/v1/responses", "/v1/chat/completions"):
                for name, body in self.BODIES.items():
                    with self.subTest(route=route, path=name):
                        response = client.post(route, json={"model": "gpt-6-luna", **body},
                                               headers={"Authorization": "Bearer " + key})
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertIn("chatgpt_auth_file is not allowed", response.text)
                for name, body in self.DEEP.items():
                    with self.subTest(route=route, deep=name):
                        response = client.post(route, json={"model": "gpt-6-luna", **body},
                                               headers={"Authorization": "Bearer " + key})
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertIn("nests values too deeply", response.text)
                for name, body in self.PROMPTS.items():
                    with self.subTest(route=route, prompt=name):
                        response = client.post(route, json={"model": "gpt-6-luna", **body},
                                               headers={"Authorization": "Bearer " + key})
                        self.assertEqual(response.status_code, 200, response.text)
                ok = client.post(route, json={"model": "gpt-6-luna", "input": "hi"},
                                 headers={"Authorization": "Bearer " + key})
                self.assertEqual(ok.status_code, 200, ok.text)


class Upstream:
    """A fake ChatGPT backend on 127.0.0.1. It records the headers of each request."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                requests.append({"path": self.path,
                                 "authorization": self.headers.get_all("authorization") or [],
                                 "account_id": self.headers.get_all("chatgpt-account-id") or []})
                body = sse("ok").encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientHeaderTests(HookCase):
    """Condition: a client cannot set Authorization or ChatGPT-Account-Id for a chatgpt deployment.

    A real proxy process sends each request to a fake upstream. The upstream
    records the token and the account id that it receives.
    """

    CLIENT = {"Authorization": "Bearer client-token", "ChatGPT-Account-Id": "acct-client"}

    def setUp(self):
        super().setUp()
        self.a, self.b = Account(self.root, "a"), Account(self.root, "b")
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)

    def test_account_headers_win_in_the_proxy(self):
        import urllib.error
        import urllib.request
        import yaml

        config = self.root.parent / "proxy.yaml"
        config.write_text(yaml.safe_dump({"model_list": [
            {"model_name": "luna-a", "litellm_params": {"model": "chatgpt/responses/gpt-6-luna",
                                                        "chatgpt_auth_file": "a/auth.json"}},
            {"model_name": "luna-b", "litellm_params": {"model": "chatgpt/responses/gpt-6-luna",
                                                        "chatgpt_auth_file": "b/auth.json"}},
        ]}))
        key = "sk-" + secrets.token_hex(8)
        port = free_port()
        env = {**os.environ, hook.SWITCH_ENV: "on", "CHATGPT_API_BASE": self.upstream.base,
               "LITELLM_MASTER_KEY": key, "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_TELEMETRY": "False"}
        for name in ("DATABASE_URL", hook.REQUIRED_ENV):
            env.pop(name, None)
        log = open(self.root.parent / "proxy.log", "w+")
        self.addCleanup(log.close)
        proxy = subprocess.Popen(
            [sys.executable, "-c", "import sys; from litellm.proxy.proxy_cli import run_server; sys.exit(run_server())",
             "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(proxy.wait, 30)
        self.addCleanup(proxy.terminate)
        base = f"http://127.0.0.1:{port}"

        def post(path, body):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                return error.code, error.read().decode()

        deadline = time.monotonic() + 120
        while True:
            try:
                with urllib.request.urlopen(base + "/health/liveliness", timeout=2):
                    break
            except OSError:
                if proxy.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    self.fail("proxy did not start:\n" + log.read()[-3000:])
                time.sleep(0.5)

        lower = {k.lower(): v for k, v in self.CLIENT.items()}
        cases = {
            "responses extra_headers": ("/v1/responses", {"input": "hi", "extra_headers": self.CLIENT}),
            "responses headers": ("/v1/responses", {"input": "hi", "headers": self.CLIENT}),
            "responses lower case": ("/v1/responses", {"input": "hi", "extra_headers": lower}),
            "responses account id only": ("/v1/responses", {"input": "hi", "extra_headers": {
                "chatgpt-account-id": "acct-client"}}),
            "chat extra_headers": ("/v1/chat/completions", {
                "messages": [{"role": "user", "content": "hi"}], "extra_headers": self.CLIENT}),
            "chat headers": ("/v1/chat/completions", {
                "messages": [{"role": "user", "content": "hi"}], "headers": lower}),
        }
        for account, model in ((self.a, "luna-a"), (self.b, "luna-b")):
            for name, (path, body) in cases.items():
                with self.subTest(model=model, case=name):
                    self.upstream.requests.clear()
                    status, text = post(path, {"model": model, **body})
                    self.assertEqual(status, 200, text[-1000:])
                    self.assertEqual(len(self.upstream.requests), 1, self.upstream.requests)
                    received = self.upstream.requests[0]
                    self.assertEqual(received["authorization"], ["Bearer " + account.access])
                    self.assertEqual(received["account_id"], [account.account_id])
        log.seek(0)
        output = log.read()
        for account in (self.a, self.b):
            for value in account.secrets():
                self.assertNotIn(value, output)

    def test_account_headers_win_in_the_router(self):
        # The proxy passes the body to the router as keyword arguments.
        router = self.router([("luna-a", {"chatgpt_auth_file": "a/auth.json"})])
        with patch.dict(os.environ, {"CHATGPT_API_BASE": self.upstream.base}):
            for kwargs in ({"extra_headers": self.CLIENT}, {"headers": self.CLIENT}):
                with self.subTest(kwargs=list(kwargs)):
                    self.upstream.requests.clear()

                    async def run():
                        await self.collect(await router.aresponses(model="luna-a", input="hi", **kwargs))
                        await router.acompletion(model="luna-a", messages=[{"role": "user", "content": "hi"}],
                                                 **kwargs)
                    asyncio.run(run())
                    self.assertEqual(len(self.upstream.requests), 2)
                    for received in self.upstream.requests:
                        self.assertEqual(received["authorization"], ["Bearer " + self.a.access])
                        self.assertEqual(received["account_id"], [self.a.account_id])

    def test_headers_object_keeps_the_account(self):
        headers = ChatGPTResponsesAPIConfig().validate_environment({}, "gpt-6-luna", {"chatgpt_auth_file": "a/auth.json"})
        headers.update(self.CLIENT)
        headers["authorization"] = "Bearer other"
        headers.setdefault("CHATGPT-ACCOUNT-ID", "acct-other")
        headers |= {"Authorization": "Bearer third"}
        for value in (headers, headers.copy(), dict(headers)):
            self.assertEqual({k: v for k, v in value.items() if k.lower() in hook.FIXED_HEADERS},
                             {"Authorization": "Bearer " + self.a.access, "ChatGPT-Account-Id": self.a.account_id})

    def test_sign_request_fails_closed_without_account_headers(self):
        with self.assertRaisesRegex(AuthenticationError, "lost the account values"):
            ChatGPTResponsesAPIConfig().sign_request(headers=dict(self.CLIENT), optional_params={}, request_data={},
                                                     api_base="http://x", model="gpt-6-luna")


class FailFastTests(HookCase):
    """Condition: no login in the gateway; a bad auth file fails at once."""

    def call(self, auth_file, model="gpt-6-luna"):
        started = time.monotonic()
        with self.assertRaises(AuthenticationError) as caught:
            ChatGPTResponsesAPIConfig().validate_environment({}, model, {"chatgpt_auth_file": auth_file})
        self.assertLess(time.monotonic() - started, 2)
        return str(caught.exception)

    def test_missing_auth_file_fails_fast(self):
        message = self.call("nobody/auth.json")
        self.assertIn("chatgpt deployment gpt-6-luna, account nobody:", message)
        self.assertIn("login-codex-account.sh nobody", message)

    def test_client_error_names_the_account_and_the_log_names_the_path(self):
        (self.root / "bad").mkdir()
        (self.root / "bad" / "auth.json").write_text("{not json")
        (self.outside / "auth.json").write_text("{}")
        cases = {"nobody/auth.json": "nobody", "bad/auth.json": "bad", str(self.outside / "auth.json"): "outside",
                 "../outside/auth.json": "outside", "a\0b/auth.json": "a?b"}
        for value, name in cases.items():
            with self.subTest(value=value):
                with self.assertLogs("LiteLLM", level="WARNING") as logs:
                    message = self.call(value)
                self.assertIn(f"account {name}:", message)
                for text in (value, str(self.root), str(self.outside), "auth.json"):
                    self.assertNotIn(text, message)
                # The log line names the configured path (repr) or the real path of the file.
                self.assertIn(value.replace("\0", "\\x00"), "\n".join(logs.output))

    def test_invalid_auth_file_fails_fast(self):
        (self.root / "bad").mkdir()
        (self.root / "bad" / "auth.json").write_text("{not json")
        self.assertIn("missing or not valid", self.call("bad/auth.json"))
        (self.root / "bad" / "auth.json").write_text(json.dumps({"id_token": "x"}))
        self.assertIn("no usable token", self.call("bad/auth.json"))

    def test_recent_device_code_request_does_not_wait(self):
        # Upstream waits up to 300 s for another login when this field is recent.
        (self.root / "wait").mkdir()
        (self.root / "wait" / "auth.json").write_text(json.dumps({"device_code_requested_at": time.time()}))
        self.call("wait/auth.json")

    def test_refresh_failure_fails_fast_and_names_no_token(self):
        account = Account(self.root, "a", expired=True)
        fake = FakeRefresh([account], status=401)
        self.refresh(fake)
        message = self.call("a/auth.json")
        self.assertIn("token refresh for the auth file failed", message)
        self.assertIn("HTTP 401", message)
        self.assertEqual(fake.calls, [account.refresh])
        for value in account.secrets():
            self.assertNotIn(value, message)

    def test_router_request_fails_fast(self):
        router = self.router([("luna", {"chatgpt_auth_file": "gone/auth.json"})], num_retries=0)
        started = time.monotonic()
        with self.assertRaises(Exception) as caught:
            asyncio.run(router.aresponses(model="luna", input="hi"))
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotIn("gone/auth.json", str(caught.exception))
        self.assertIn("gpt-6-luna (id luna-0), account gone", str(caught.exception))


class ProcessAccountTests(HookCase):
    """Condition: the gateway needs no process account and starts no login."""

    def test_get_llm_provider_reads_no_token(self):
        # CHATGPT_TOKEN_DIR is empty. Upstream would start the device-code login here.
        with patch.object(upstream_auth.Authenticator, "get_access_token",
                          side_effect=AssertionError("process token read")):
            model, provider, key, base = litellm.get_llm_provider("chatgpt/responses/gpt-6-luna")
        self.assertEqual(provider, "chatgpt")
        self.assertIsNone(key)
        self.assertEqual(list(self.process_dir.iterdir()), [])

    def test_two_accounts_without_process_account(self):
        a, b = Account(self.root, "a"), Account(self.root, "b")
        backend = FakeChatGPT()
        self.backend(backend)
        with patch.dict(os.environ, {hook.REQUIRED_ENV: "true"}):
            router = self.router([("luna-a", {"chatgpt_auth_file": "a/auth.json"}),
                                  ("luna-b", {"chatgpt_auth_file": "b/auth.json"})])
            self.assertEqual(self.ask(router, "luna-a"), a.account_id)
            self.assertEqual(self.ask(router, "luna-b"), b.account_id)
        self.assertEqual(list(self.process_dir.iterdir()), [])

    def test_required_mode_rejects_deployment_without_key(self):
        # On by default when the hook is on; each value except a false one keeps it on.
        for value in (None, "true", "1", "yes", "on", ""):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is not None:
                    os.environ[hook.REQUIRED_ENV] = value
                with self.assertRaisesRegex(AuthenticationError, "has no chatgpt_auth_file"):
                    ChatGPTResponsesAPIConfig().validate_environment({}, "gpt-6-luna", {})
                with self.assertRaisesRegex(AuthenticationError, "has no chatgpt_auth_file"):
                    ChatGPTConfig().validate_environment({}, "gpt-6-luna", [], {}, {}, None, None)

    def test_deployment_without_key_keeps_process_account(self):
        # Only with CHATGPT_AUTH_FILE_REQUIRED=false.
        process = Account(self.process_dir.parent, "process-acct")
        for value in ("false", "FALSE", "0", "no", "off"):
            with self.subTest(value=value), patch.dict(os.environ, {hook.REQUIRED_ENV: value}):
                self.assertFalse(hook._required())
        with patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": str(process.dir), hook.REQUIRED_ENV: "false"}):
            headers = ChatGPTResponsesAPIConfig().validate_environment({}, "gpt-6-luna", {})
            self.assertEqual(headers["Authorization"], "Bearer " + process.access)
            chat = ChatGPTConfig().validate_environment({}, "gpt-6-luna", [], {}, {}, None, None)
            self.assertEqual(chat["Authorization"], "Bearer " + process.access)


class IsolationTests(HookCase):
    """Condition: one authenticator per file; tokens and refreshes stay separate."""

    def test_one_cached_authenticator_per_file(self):
        Account(self.root, "a")
        Account(self.root, "b")
        first = hook.get_authenticator(hook.resolve_auth_file("a/auth.json"))
        self.assertIs(first, hook.get_authenticator(hook.resolve_auth_file(str(self.root / "a" / "auth.json"))))
        self.assertIsNot(first, hook.get_authenticator(hook.resolve_auth_file("b/auth.json")))
        self.assertEqual(len(hook._authenticators), 2)

    def test_headers_use_the_deployment_account(self):
        a, b = Account(self.root, "a"), Account(self.root, "b")
        config = ChatGPTResponsesAPIConfig()
        for account in (a, b, a):
            headers = config.validate_environment(
                {"Authorization": "Bearer client", "chatgpt-account-id": "client"}, "gpt-6-luna",
                {"chatgpt_auth_file": f"{account.name}/auth.json"})
            self.assertEqual(headers["Authorization"], "Bearer " + account.access)
            self.assertEqual(headers["ChatGPT-Account-Id"], account.account_id)
            self.assertNotIn("chatgpt-account-id", headers)
        chat = ChatGPTConfig().validate_environment({}, "gpt-6-luna", [], {}, {"chatgpt_auth_file": "b/auth.json"},
                                                    "process-key-ignored", None)
        self.assertEqual(chat["Authorization"], "Bearer " + b.access)
        self.assertEqual(chat["ChatGPT-Account-Id"], b.account_id)

    def test_refresh_writes_only_its_own_file(self):
        a, b = Account(self.root, "a", expired=True), Account(self.root, "b")
        fake = FakeRefresh([a, b])
        self.refresh(fake)
        before_b = b.digest()
        headers = ChatGPTResponsesAPIConfig().validate_environment(
            {}, "gpt-6-luna", {"chatgpt_auth_file": "a/auth.json"})
        self.assertEqual(fake.calls, [a.refresh])
        self.assertEqual(headers["Authorization"], "Bearer " + fake.issued["a"])
        stored = json.loads(a.file.read_text())
        self.assertEqual(stored["access_token"], fake.issued["a"])
        self.assertEqual(stored["account_id"], a.account_id)
        self.assertEqual(stat.S_IMODE(a.file.stat().st_mode), 0o600)
        self.assertEqual(b.digest(), before_b)
        self.assertEqual(sorted(p.name for p in a.dir.iterdir()), ["auth.json"])
        self.assertEqual(sorted(p.name for p in b.dir.iterdir()), ["auth.json"])

    def test_concurrent_requests_for_two_accounts(self):
        a, b = Account(self.root, "a", expired=True), Account(self.root, "b", expired=True)
        fake = FakeRefresh([a, b], delay=0.05)
        self.refresh(fake)
        backend = FakeChatGPT()
        self.backend(backend)
        router = self.router([("luna-a", {"chatgpt_auth_file": "a/auth.json"}),
                              ("luna-b", {"chatgpt_auth_file": "b/auth.json"})])

        async def run():
            async def responses(model):
                return await self.collect(await router.aresponses(model=model, input="hi"))

            async def chat(model):
                result = await router.acompletion(model=model, messages=[{"role": "user", "content": "hi"}])
                return result.choices[0].message.content

            calls = [responses(m) for m in ["luna-a", "luna-b"] * 4]
            calls += [chat(m) for m in ["luna-b", "luna-a"] * 2]
            return await asyncio.gather(*calls)

        results = asyncio.run(run())
        expected = [a, b] * 4 + [b, a] * 2
        self.assertEqual(results, [account.account_id for account in expected])
        by_account = {a.account_id: a, b.account_id: b}
        for request in backend.requests:
            account = by_account[request["account_id"]]
            self.assertEqual(request["authorization"], "Bearer " + fake.issued[account.name])
        self.assertEqual(sorted(fake.calls), sorted([a.refresh, b.refresh]))

    def test_threads_refresh_each_account_once(self):
        a, b = Account(self.root, "a", expired=True), Account(self.root, "b", expired=True)
        fake = FakeRefresh([a, b], delay=0.05)
        self.refresh(fake)
        results, errors = [], []

        def work(account):
            try:
                auth = hook.get_authenticator(hook.resolve_auth_file(f"{account.name}/auth.json"))
                results.append((account.name, auth.get_access_token()))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(acc,)) for acc in [a, b] * 5]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(fake.calls), sorted([a.refresh, b.refresh]))
        for name, token in results:
            self.assertEqual(token, fake.issued[name])


class PathTests(HookCase):
    """Condition: the auth file must be inside the allowed directory."""

    def test_paths_inside_root(self):
        Account(self.root, "a")
        real = str((self.root / "a" / "auth.json").resolve())
        self.assertEqual(hook.resolve_auth_file("a/auth.json"), real)
        self.assertEqual(hook.resolve_auth_file(str(self.root / "a" / "auth.json")), real)
        os.symlink(self.root / "a", self.root / "alias")
        self.assertEqual(hook.resolve_auth_file("alias/auth.json"), real)

    def test_default_root_is_chatgpt_token_dir(self):
        os.environ.pop("CHATGPT_AUTH_FILE_ROOT")
        self.assertEqual(hook.allowed_root(), str(self.process_dir.resolve()))

    def test_refused_paths(self):
        (self.outside / "auth.json").write_text("{}")
        os.symlink(self.outside, self.root / "escape")
        os.symlink(self.outside / "auth.json", self.root / "escape.json")
        cases = {
            "outside": str(self.outside / "auth.json"),
            "dotdot": "../outside/auth.json",
            "dotdot_inside": "a/../a/auth.json",
            "absolute_dotdot": str(self.root) + "/../outside/auth.json",
            "symlink_dir": "escape/auth.json",
            "symlink_file": "escape.json",
            "root_itself": str(self.root),
            "empty": "",
            "nul": "a\0b/auth.json",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(hook.AuthFileError):
                    hook.resolve_auth_file(value)
                if value:
                    with self.assertRaisesRegex(AuthenticationError, "chatgpt deployment gpt-6-luna"):
                        ChatGPTResponsesAPIConfig().validate_environment(
                            {}, "gpt-6-luna", {"chatgpt_auth_file": value})


class SecretTests(HookCase):
    """Condition: no token value in a log line, an error message or test output."""

    def test_no_token_in_logs_or_errors(self):
        good, bad = Account(self.root, "good", expired=True), Account(self.root, "bad", expired=True)
        broken = Account(self.root, "broken")
        broken.file.write_text(json.dumps({"access_token": 5, "refresh_token": broken.refresh}) + "x")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        loggers = [logging.getLogger(), logging.getLogger("LiteLLM"), logging.getLogger("LiteLLM Proxy"),
                   logging.getLogger("LiteLLM Router")]
        levels = [lg.level for lg in loggers]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        messages = []
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                self.refresh(FakeRefresh([good]))
                ChatGPTResponsesAPIConfig().validate_environment({}, "m", {"chatgpt_auth_file": "good/auth.json"})
                with patch.object(upstream_auth, "_get_httpx_client", return_value=FakeRefresh([bad], status=400)):
                    for name in ("bad", "broken", "missing"):
                        try:
                            ChatGPTResponsesAPIConfig().validate_environment(
                                {}, "m", {"chatgpt_auth_file": f"{name}/auth.json"})
                        except AuthenticationError as exc:
                            messages.append(str(exc))
        finally:
            for lg, level in zip(loggers, levels):
                lg.removeHandler(handler)
                lg.setLevel(level)
        self.assertEqual(len(messages), 3)
        output = stream.getvalue() + "\n".join(messages)
        for account in (good, bad, broken):
            for value in account.secrets():
                self.assertNotIn(value, output)


def run_python(code, *args, env=None, pythonpath=()):
    """Run a separate interpreter that loads image/hooks/sitecustomize.py at start."""
    base = {k: v for k, v in os.environ.items() if k != hook.SWITCH_ENV}
    base["PYTHONPATH"] = os.pathsep.join([*map(str, pythonpath), str(HOOKS)])
    return subprocess.run([sys.executable, "-c", code, *args], env={**base, **(env or {})},
                          capture_output=True, text=True, timeout=180)


class VersionTests(unittest.TestCase):
    """Condition: stop on an untested version or when upstream has the key."""

    def setUp(self):
        env = patch.dict(os.environ, {hook.SWITCH_ENV: "on"})
        env.start()
        self.addCleanup(env.stop)

    def test_version_check(self):
        for value, allowed in [*((v, True) for v in hook.TESTED_VERSIONS), ("1.102.0", False), ("1.104.0", False)]:
            with self.subTest(version=value), patch("litellm_versions.version", return_value=value):
                if allowed:
                    hook.install()
                else:
                    with self.assertRaisesRegex(hook.StopStart, f"^LiteLLM {value} is not tested"):
                        hook.install()

    def test_stops_when_upstream_has_the_key(self):
        from litellm.types.router import GenericLiteLLMParams
        variants = [
            patch.object(upstream_auth, "get_cached_authenticator", create=True, new=lambda f: None),
            patch.dict(GenericLiteLLMParams.model_fields, {"chatgpt_auth_file": None}),
        ]
        for variant in variants:
            with self.subTest(variant=variant), variant:
                with self.assertRaisesRegex(hook.StopStart, "Upstream LiteLLM has chatgpt_auth_file"):
                    hook.install()

        def init(self, auth_file=None):
            pass
        with patch.object(upstream_auth.Authenticator, "__init__", init):
            with self.assertRaisesRegex(hook.StopStart, "Upstream LiteLLM has chatgpt_auth_file"):
                hook.install()

    def test_untested_version_stops_the_interpreter(self):
        # A RuntimeError in sitecustomize only prints a line; the process goes on.
        with tempfile.TemporaryDirectory(prefix="litellm-test-codex-accounts-") as tmp:
            Path(tmp, "sitecustomize.py").write_text(
                "import importlib.metadata\n"
                "importlib.metadata.version = lambda name: '1.102.0'\n"
                "from chatgpt_auth_file import install\n"
                "install()\n")
            result = run_python("print('process continued')", env={hook.SWITCH_ENV: "on"}, pythonpath=[tmp])
            off = run_python("print('process continued')", pythonpath=[tmp])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("process continued", result.stdout)
        self.assertIn("LiteLLM 1.102.0 is not tested", result.stderr)
        # With the hook off, install() returns before the version check.
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertIn("process continued", off.stdout)

    def test_installed_once(self):
        self.assertTrue(ChatGPTResponsesAPIConfig._gateway_auth_file)
        before = (ChatGPTResponsesAPIConfig.validate_environment, ChatGPTConfig.validate_environment,
                  auth_utils.is_request_body_safe)
        hook.install()
        after = (ChatGPTResponsesAPIConfig.validate_environment, ChatGPTConfig.validate_environment,
                 auth_utils.is_request_body_safe)
        self.assertEqual(before, after)
        self.assertEqual(litellm.types.utils.all_litellm_params.count("chatgpt_auth_file"), 1)

    def test_key_stays_in_litellm_params_and_out_of_provider_body(self):
        from litellm.litellm_core_utils.get_litellm_params import get_litellm_params
        from litellm.utils import get_non_default_completion_params
        self.assertEqual(get_litellm_params(chatgpt_auth_file="a/auth.json")["chatgpt_auth_file"], "a/auth.json")
        self.assertNotIn("chatgpt_auth_file", get_non_default_completion_params({"chatgpt_auth_file": "a"}))


ORIGINS = r"""
import importlib, json, sys
from litellm.llms.chatgpt.chat.transformation import ChatGPTConfig
from litellm.llms.chatgpt.responses.transformation import ChatGPTResponsesAPIConfig
from litellm.litellm_core_utils import get_litellm_params as params
from litellm.types.utils import all_litellm_params

def origin(function):
    return function.__module__ + ":" + function.__qualname__

auth_utils = importlib.import_module("litellm.proxy.auth.auth_utils")
main = importlib.import_module("litellm.main")
print(json.dumps({
    "hook_module_loaded": "chatgpt_auth_file" in sys.modules,
    "active": sys.modules["chatgpt_auth_file"].active(),
    "provider_info": origin(ChatGPTConfig._get_openai_compatible_provider_info),
    "chat_env": origin(ChatGPTConfig.validate_environment),
    "responses_env": origin(ChatGPTResponsesAPIConfig.validate_environment),
    "body_safe": origin(auth_utils.is_request_body_safe),
    "key_in_lists": ["chatgpt_auth_file" in all_litellm_params, "chatgpt_auth_file" in params.OPTIONAL_KWARGS_KEYS,
                     "chatgpt_auth_file" in main.FORWARDED_KWARGS_KEYS],
    "marker": hasattr(ChatGPTResponsesAPIConfig, "_gateway_auth_file"),
}))
"""

UPSTREAM = {
    "provider_info": "litellm.llms.chatgpt.chat.transformation:ChatGPTConfig._get_openai_compatible_provider_info",
    "chat_env": "litellm.llms.chatgpt.chat.transformation:ChatGPTConfig.validate_environment",
    "responses_env": "litellm.llms.chatgpt.responses.transformation:ChatGPTResponsesAPIConfig.validate_environment",
    "body_safe": "litellm.proxy.auth.auth_utils:is_request_body_safe",
}


class SwitchTests(unittest.TestCase):
    """The hook is off by default and then changes nothing."""

    def origins(self, **env):
        result = run_python(ORIGINS, env=env)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_hook_off_keeps_upstream_objects(self):
        for env in ({}, {hook.SWITCH_ENV: "off"}, {hook.SWITCH_ENV: "0"}):
            with self.subTest(env=env):
                found = self.origins(**env)
                self.assertTrue(found["hook_module_loaded"])
                self.assertFalse(found["active"])
                for name, origin in UPSTREAM.items():
                    self.assertEqual(found[name], origin)
                self.assertEqual(found["key_in_lists"], [False, False, False])
                self.assertFalse(found["marker"])

    def test_hook_on_replaces_them(self):
        # The same check sees the change, so the test above can fail.
        for value in ("on", "1", "true", "yes", "ON"):
            with self.subTest(value=value):
                found = self.origins(**{hook.SWITCH_ENV: value})
                self.assertTrue(found["active"])
                for name in UPSTREAM:
                    self.assertTrue(found[name].startswith("chatgpt_auth_file:install."), found[name])
                self.assertEqual(found["key_in_lists"], [True, True, True])

    def test_switch_values(self):
        for value, expected in [("on", True), ("1", True), ("true", True), ("Yes", True), (" on ", True),
                                ("off", False), ("0", False), ("no", False), ("", False)]:
            with self.subTest(value=value), patch.dict(os.environ, {hook.SWITCH_ENV: value}):
                self.assertIs(hook.enabled(), expected)
        with patch.dict(os.environ):
            os.environ.pop(hook.SWITCH_ENV, None)
            self.assertFalse(hook.enabled())


class ConfigGuardTests(unittest.TestCase):
    """With the hook off, a proxy start with the key in its configuration stops."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="litellm-test-codex-accounts-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def config(self, name, text):
        path = self.tmp / name
        path.write_text(text)
        return str(path)

    def start(self, *args, **env):
        return run_python("print('process continued')", *args, env=env)

    def test_key_with_hook_off_stops_the_start(self):
        path = self.config("c.yaml", EXAMPLE.read_text())
        for args, env in ((["--config", path], {}), ([f"--config={path}"], {}), (["-c", path], {}),
                          ([], {"CONFIG_FILE_PATH": path})):
            with self.subTest(args=args, env=env):
                result = self.start(*args, **env)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("process continued", result.stdout)
                self.assertIn("CHATGPT_AUTH_FILE_HOOK is off", result.stderr)
                self.assertIn("environment of the container", result.stderr)

    def test_key_in_include_file_stops_the_start(self):
        self.config("models.yaml", "model_list:\n  - model_name: m\n    litellm_params:\n"
                                   "      model: chatgpt/responses/gpt-6-luna\n      chatgpt_auth_file: a/auth.json\n")
        path = self.config("main.yaml", "include:\n  - models.yaml\n  - main.yaml\n")
        self.assertEqual(self.start("--config", path).returncode, 1)

    def test_other_starts_continue(self):
        plain = self.config("plain.yaml", "model_list:\n  - model_name: m\n    litellm_params:\n"
                                          "      model: openai/m\n# chatgpt_auth_file in a comment\n")
        example = self.config("c.yaml", EXAMPLE.read_text())
        cases = {
            "no key": (["--config", plain], {}),
            "missing file": (["--config", str(self.tmp / "missing.yaml")], {}),
            "not yaml": (["--config", self.config("bad.yaml", "a: [")], {}),
            "no config": ([], {}),
            "hook on": (["--config", example], {hook.SWITCH_ENV: "on"}),
        }
        for name, (args, env) in cases.items():
            with self.subTest(case=name):
                result = self.start(*args, **env)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn("process continued", result.stdout)



class RoutingTests(HookCase):
    """The example configuration: order 1 and 2 in one group, native fallback on 429."""

    def example(self):
        import yaml
        return yaml.safe_load(EXAMPLE.read_text())

    def test_order_fallback_on_429(self):
        # The router settings of the example; accounts a and b stand in for two accounts of the example.
        a, b = Account(self.root, "a"), Account(self.root, "b")
        backend = FakeChatGPT({a.account_id: 429})
        self.backend(backend)
        router = self.router([("luna", {"chatgpt_auth_file": "a/auth.json", "order": 1}),
                              ("luna", {"chatgpt_auth_file": "b/auth.json", "order": 2})],
                             **self.example()["router_settings"])
        self.assertEqual(self.ask(router, "luna"), b.account_id)
        self.assertEqual([r["account_id"] for r in backend.requests], [a.account_id, b.account_id])
        # The 429 put order 1 into cooldown: the next request goes to order 2 at once.
        backend.requests.clear()
        self.assertEqual(self.ask(router, "luna"), b.account_id)
        self.assertEqual([r["account_id"] for r in backend.requests], [b.account_id])

    def test_example_configuration_loads(self):
        config = self.example()
        # Compose sets both switches: environment_variables is applied after the start.
        self.assertNotIn("environment_variables", config)
        text = EXAMPLE.read_text()
        self.assertIn(f"{hook.SWITCH_ENV}: \"on\"", text)
        self.assertIn(f"{hook.REQUIRED_ENV}: \"true\"", text)
        chatgpt = [d for d in config["model_list"] if d["litellm_params"]["model"].startswith("chatgpt/")]
        self.assertGreaterEqual(len(chatgpt), 2)
        files = {d["litellm_params"]["chatgpt_auth_file"] for d in chatgpt}
        self.assertEqual(len(files), 2)
        orders = sorted(d["litellm_params"]["order"] for d in chatgpt if d["model_name"] == chatgpt[0]["model_name"])
        self.assertEqual(orders, [1, 2])
        for value in files:
            Account(self.root, value.split("/")[0])
            hook.resolve_auth_file(value)
        router = litellm.Router(model_list=config["model_list"], **config.get("router_settings", {}))
        self.assertEqual(len(router.get_model_list()), len(config["model_list"]))


class LoginScriptTests(unittest.TestCase):
    """Condition: the login runs in a separate container and writes mode 0600."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="litellm-test-codex-accounts-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def load(self):
        spec = importlib.util.spec_from_file_location("login_codex_account", SCRIPTS / "login-codex-account.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_login_writes_0600_in_account_dir(self):
        token_dir = self.tmp / "acct"
        token_dir.mkdir(mode=0o700)
        written = []

        class FakeAuthenticator:
            def __init__(self):
                self.auth_file = os.path.join(os.environ["CHATGPT_TOKEN_DIR"], "auth.json")

            def get_access_token(self):
                # Default open() mode, as upstream; the umask of the script decides.
                with open(self.auth_file, "w") as f:
                    json.dump({"access_token": secrets.token_hex(8)}, f)
                written.append(self.auth_file)
                return "unused"

        out = io.StringIO()
        old_umask = os.umask(0o022)
        try:
            with patch.dict(os.environ, {"CHATGPT_TOKEN_DIR": str(token_dir)}), \
                    patch.dict(sys.modules), contextlib.redirect_stdout(out):
                sys.modules.pop("chatgpt_auth_file", None)
                self.load().main(FakeAuthenticator)
        finally:
            os.umask(old_umask)
        self.assertEqual(written, [str(token_dir / "auth.json")])
        self.assertEqual(stat.S_IMODE((token_dir / "auth.json").stat().st_mode), 0o600)
        self.assertIn("Login complete", out.getvalue())

    def test_login_refuses_in_gateway_process(self):
        # The hook is active in the test process, as in a gateway with the switch on.
        self.assertTrue(hook.active())
        with self.assertRaises(SystemExit):
            self.load().main(lambda: self.fail("authenticator created"))

    def test_login_runs_with_hook_loaded_and_off(self):
        # The built image loads sitecustomize.py, and so the hook module, in each process.
        token_dir = self.tmp / "acct"
        token_dir.mkdir(mode=0o700)
        code = (
            "import importlib.util, json, os, sys\n"
            "assert 'chatgpt_auth_file' in sys.modules\n"
            f"spec = importlib.util.spec_from_file_location('login', {str(SCRIPTS / 'login-codex-account.py')!r})\n"
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
            "class Fake:\n"
            "    def __init__(self):\n"
            "        self.auth_file = os.path.join(os.environ['CHATGPT_TOKEN_DIR'], 'auth.json')\n"
            "    def get_access_token(self):\n"
            "        open(self.auth_file, 'w').write(json.dumps({'access_token': os.urandom(8).hex()}))\n"
            "module.main(Fake)\n")
        result = run_python(code, env={hook.SWITCH_ENV: "off", "CHATGPT_TOKEN_DIR": str(token_dir)})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("Login complete", result.stdout)
        self.assertEqual(stat.S_IMODE((token_dir / "auth.json").stat().st_mode), 0o600)

    def fake_docker(self, compose_image="example/built:1"):
        """A fake docker: `compose` prints a rendered configuration, `run` records its arguments."""
        log = self.tmp / "docker.args"
        compose_log = self.tmp / "compose.args"
        fake = self.tmp / "docker"
        rendered = json.dumps({"services": {"database": {"image": "postgres:x"},
                                            "gateway": {"image": compose_image}}})
        fake.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = compose ]; then echo "$@" > {compose_log}; '
            + (f"printf '%s\\n' '{rendered}'; exit 0; fi\n" if compose_image else "exit 1; fi\n")
            + f'for a in "$@"; do printf "%s\\n" "$a"; done > {log}\n')
        fake.chmod(0o755)
        self.compose_log = compose_log
        return fake, log

    def run_script(self, *args, compose_image="example/built:1", **env):
        fake, log = self.fake_docker(compose_image)
        base = {"PATH": os.environ["PATH"], "DOCKER": str(fake), "CODEX_ACCOUNTS_DIR": str(self.tmp / "accounts"),
                "LITELLM_IMAGE": "example/litellm@sha256:" + "0" * 64, **env}
        result = subprocess.run(["sh", str(SCRIPTS / "login-codex-account.sh"), *args], env=base,
                                capture_output=True, text=True, timeout=30)
        return result, (log.read_text().splitlines() if log.exists() else None)

    def test_shell_script_starts_one_short_lived_container(self):
        result, argv = self.run_script("acct-a")
        self.assertEqual(result.returncode, 0, result.stderr)
        account_dir = self.tmp / "accounts" / "acct-a"
        self.assertEqual(stat.S_IMODE(account_dir.stat().st_mode), 0o700)
        self.assertEqual(argv[:2], ["run", "--rm"])
        joined = " ".join(argv)
        self.assertIn(f"{account_dir}:/tokens", argv)
        self.assertIn("CHATGPT_TOKEN_DIR=/tokens", argv)
        self.assertIn("CHATGPT_AUTH_FILE_HOOK=off", argv)
        self.assertIn(f"{SCRIPTS / 'login-codex-account.py'}:/login.py:ro", argv)
        self.assertEqual(argv[-3:], ["python", "example/litellm@sha256:" + "0" * 64, "/login.py"])
        for forbidden in ("PYTHONPATH", "hooks", "--env-file", "secrets", "--network"):
            self.assertNotIn(forbidden, joined)

    def test_shell_script_rejects_bad_account_names(self):
        for name in ("", "../x", "a/b", "-a", "A", "a" * 33, "a b"):
            with self.subTest(name=name):
                result, argv = self.run_script(name)
                self.assertEqual(result.returncode, 2)
                self.assertIsNone(argv)
        self.assertFalse((self.tmp / "accounts").exists())

    def test_shell_script_reads_image_from_compose(self):
        # No LITELLM_IMAGE: the image of the service gateway, whatever its name.
        result, argv = self.run_script("acct-b", LITELLM_IMAGE="", compose_image="litellm-local:built")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(argv[-2], "litellm-local:built")
        compose = self.compose_log.read_text().split()
        self.assertIn("--no-env-resolution", compose)
        self.assertEqual(compose[compose.index("-f") + 1], str(ROOT / "compose.yaml"))

    def test_shell_script_without_image_stops(self):
        result, argv = self.run_script("acct-c", LITELLM_IMAGE="", compose_image="")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Set LITELLM_IMAGE", result.stderr)
        self.assertIsNone(argv)


if __name__ == "__main__":
    unittest.main()
