"""Tests for image/hooks/chatgpt_session_id.py: a stable ChatGPT session id.

Run them as tests/README.md states. A fake ChatGPT backend on 127.0.0.1 records
the headers of each request; the tests use no network and no real account.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import litellm
from litellm.proxy.utils import ProxyLogging

import chatgpt_session_id as hook
import quota_router
from test_chatgpt_auth_file import Account, free_port, sse

SYSTEM = {"role": "system", "content": "You are a probe."}
FIRST = {"role": "user", "content": "first question"}
ANSWER = {"role": "assistant", "content": "answer"}
SECOND = {"role": "user", "content": "second question"}
CHAT = {"model": "luna", "messages": [SYSTEM, FIRST]}
RESPONSES = {"model": "luna", "instructions": "You are a probe.", "input": [FIRST]}
# Each place where a client can put its session id, and the endpoint body it goes with.
CLIENT_KEYS = {
    "litellm_session_id": {"litellm_session_id": "client-id"},
    "extra_body": {"extra_body": {"litellm_session_id": "client-id"}},
    "session_id": {"session_id": "client-id"},
    "metadata.session_id": {"metadata": {"session_id": "client-id"}},
}


def router():
    return litellm.Router(model_list=[
        {"model_name": "luna", "litellm_params": {"model": "chatgpt/responses/gpt-6-luna"}},
        {"model_name": "other", "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-none",
                                                   "api_base": "http://127.0.0.1:9/v1"}},
        {"model_name": "mixed", "litellm_params": {"model": "chatgpt/responses/gpt-6-luna"}},
        {"model_name": "mixed", "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-none",
                                                   "api_base": "http://127.0.0.1:9/v1"}},
    ])


class StepTests(unittest.TestCase):
    """set_session_id(): the step that the proxy runs before its pre-call hooks."""

    def setUp(self):
        self.router = router()

    def session(self, body):
        data = copy.deepcopy(body)
        self.assertTrue(hook.set_session_id(data, self.router))
        rest = {k: v for k, v in data.items() if k != "litellm_session_id"}
        self.assertEqual(rest, body)  # the step adds one key and changes no other
        return data["litellm_session_id"]

    def test_sets_the_derived_id_without_a_client_id(self):
        for body in (CHAT, RESPONSES):
            self.assertEqual(self.session(body), hook.cache_session_id(body))
            self.assertTrue(self.session(body).startswith("prefix-"))

    def test_same_id_for_a_conversation_that_grows(self):
        self.assertEqual(self.session(CHAT), self.session({**CHAT, "messages": [SYSTEM, FIRST, ANSWER, SECOND]}))
        self.assertEqual(self.session(RESPONSES), self.session({**RESPONSES, "input": [FIRST, ANSWER, SECOND]}))

    def test_other_id_for_another_first_user_message(self):
        self.assertNotEqual(self.session(CHAT), self.session({**CHAT, "messages": [SYSTEM, SECOND]}))
        self.assertNotEqual(self.session(RESPONSES), self.session({**RESPONSES, "input": [SECOND]}))

    def test_client_id_wins(self):
        for body in (CHAT, RESPONSES):
            for name, keys in CLIENT_KEYS.items():
                with self.subTest(name=name, endpoint="messages" in body):
                    data = copy.deepcopy({**body, **keys})
                    hook.set_session_id(data, self.router)
                    self.assertEqual(data["litellm_session_id"], "client-id")
        data = {**RESPONSES, "litellm_metadata": {"session_id": "client-id"}}
        hook.set_session_id(data, self.router)
        self.assertEqual(data["litellm_session_id"], "client-id")

    def test_request_with_litellm_session_id_is_unchanged(self):
        data = {**CHAT, "litellm_session_id": "client-id", "session_id": "other"}
        before = copy.deepcopy(data)
        self.assertFalse(hook.set_session_id(data, self.router))
        self.assertEqual(data, before)

    def test_derived_id_wins_over_the_automatic_ids(self):
        # The proxy sets litellm_call_id for each request, and the router litellm_trace_id.
        from litellm.llms.chatgpt.common_utils import ensure_chatgpt_session_id
        data = {**CHAT, "litellm_call_id": "call", "litellm_trace_id": "trace"}
        hook.set_session_id(data, self.router)
        self.assertEqual(ensure_chatgpt_session_id(data), hook.cache_session_id(CHAT))

    def test_other_model_groups_are_unchanged(self):
        for model in ("other", "mixed", "unknown", None, 7):
            with self.subTest(model=model):
                data = {**CHAT, "model": model}
                before = copy.deepcopy(data)
                self.assertFalse(hook.set_session_id(data, self.router))
                self.assertEqual(data, before)
        data = copy.deepcopy(CHAT)
        self.assertFalse(hook.set_session_id(data, None))
        self.assertEqual(data, CHAT)

    def test_router_error_leaves_the_request(self):
        class Broken:
            def get_model_list(self, model_name=None):
                raise RuntimeError("no list")
        data = copy.deepcopy(CHAT)
        self.assertFalse(hook.set_session_id(data, Broken()))
        self.assertEqual(data, CHAT)

    def test_derivation_error_leaves_the_request(self):
        with patch.object(hook, "cache_session_id", side_effect=RuntimeError("no id")):
            data = copy.deepcopy(CHAT)
            self.assertFalse(hook.set_session_id(data, self.router))
            self.assertEqual(data, CHAT)

    def test_router_uses_the_same_function(self):
        self.assertIs(quota_router.cache_session_id, hook.cache_session_id)


class InstallTests(unittest.TestCase):
    def test_installed_once(self):
        self.assertTrue(ProxyLogging._gateway_session_id)
        before = ProxyLogging.pre_call_hook
        hook.install()
        self.assertIs(ProxyLogging.pre_call_hook, before)

    def test_pre_call_hook_runs_the_step(self):
        from litellm.caching.caching import DualCache
        from litellm.proxy import proxy_server
        from litellm.proxy._types import UserAPIKeyAuth

        logging = ProxyLogging(user_api_key_cache=DualCache())

        def call(body, call_type):
            return asyncio.run(logging.pre_call_hook(
                user_api_key_dict=UserAPIKeyAuth(), data=copy.deepcopy(body), call_type=call_type))

        with patch.object(proxy_server, "llm_router", router()):
            self.assertEqual(call(CHAT, "acompletion")["litellm_session_id"], hook.cache_session_id(CHAT))
            self.assertEqual(call(RESPONSES, "aresponses")["litellm_session_id"],
                             hook.cache_session_id(RESPONSES))
            # Another endpoint, another provider and no router: no new key.
            self.assertNotIn("litellm_session_id", call({"model": "luna", "input": "text"}, "aembedding"))
            self.assertNotIn("litellm_session_id", call({**CHAT, "model": "other"}, "acompletion"))
        with patch.object(proxy_server, "llm_router", None):
            self.assertNotIn("litellm_session_id", call(CHAT, "acompletion"))
        # An error in the derivation: the request goes on without the id.
        with patch.object(proxy_server, "llm_router", router()), \
                patch.object(hook, "cache_session_id", side_effect=RuntimeError("no id")):
            self.assertEqual(call(CHAT, "acompletion"), CHAT)


class Upstream:
    """A fake backend on 127.0.0.1 for ChatGPT and for an OpenAI-compatible provider."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requests = self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
                requests.append({"path": self.path, "body": body,
                                 "headers": {k.lower(): v for k, v in self.headers.items()},
                                 "session_id": self.headers.get_all("session_id") or [],
                                 "authorization": self.headers.get_all("authorization") or [],
                                 "account_id": self.headers.get_all("chatgpt-account-id") or []})
                if self.path.endswith("/chat/completions"):
                    kind, answer = "application/json", json.dumps({
                        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-x",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                else:
                    kind, answer = "text/event-stream", sse("ok")
                self.send_response(200)
                self.send_header("content-type", kind)
                self.send_header("content-length", str(len(answer.encode())))
                self.end_headers()
                self.wfile.write(answer.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyTests(unittest.TestCase):
    """A real proxy process sends each request to the fake backend: the session_id header as ChatGPT gets it.

    The proxy runs with the chatgpt_auth_file hook on, so the tests also check the account headers.
    """

    @classmethod
    def setUpClass(cls):
        import yaml

        cls.tmp = tempfile.TemporaryDirectory(prefix="litellm-test-session-hook-")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        root = base / "tokens"
        root.mkdir(mode=0o700)
        cls.account = Account(root, "a")
        cls.upstream = Upstream()
        cls.addClassCleanup(cls.upstream.close)
        config = base / "proxy.yaml"
        config.write_text(yaml.safe_dump({"model_list": [
            {"model_name": "luna", "litellm_params": {"model": "chatgpt/responses/gpt-6-luna",
                                                      "chatgpt_auth_file": "a/auth.json"}},
            {"model_name": "other", "litellm_params": {"model": "openai/gpt-x", "api_key": "sk-other",
                                                       "api_base": cls.upstream.base + "/v1"}},
        ], "litellm_settings": {"num_retries": 0}}))
        cls.key = "sk-" + secrets.token_hex(8)
        port = free_port()
        env = {**os.environ, "CHATGPT_AUTH_FILE_HOOK": "on", "CHATGPT_AUTH_FILE_ROOT": str(root),
               "CHATGPT_TOKEN_DIR": str(base / "process"), "CHATGPT_API_BASE": cls.upstream.base,
               "LITELLM_MASTER_KEY": cls.key, "LITELLM_LOCAL_MODEL_COST_MAP": "True",
               "LITELLM_TELEMETRY": "False", "HOME": str(base)}
        for name in ("DATABASE_URL", "CHATGPT_AUTH_FILE_REQUIRED"):
            env.pop(name, None)
        cls.log = open(base / "proxy.log", "w+")
        cls.addClassCleanup(cls.log.close)
        cls.proxy = subprocess.Popen(
            [sys.executable, "-c", "import sys; from litellm.proxy.proxy_cli import run_server; sys.exit(run_server())",
             "--config", str(config), "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.proxy.wait, 30)
        cls.addClassCleanup(cls.proxy.terminate)
        cls.base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 120
        while True:
            try:
                with urllib.request.urlopen(cls.base + "/health/liveliness", timeout=2):
                    break
            except OSError:
                if cls.proxy.poll() is not None or time.monotonic() > deadline:
                    cls.log.seek(0)
                    raise AssertionError("proxy did not start:\n" + cls.log.read()[-3000:])
                time.sleep(0.5)

    def post(self, path, body, headers=None):
        """Send one request and return the one request that the backend received."""
        self.upstream.requests.clear()
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, text = response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            status, text = error.code, error.read().decode()
        self.assertEqual(status, 200, text[-1000:])
        self.assertEqual(len(self.upstream.requests), 1, self.upstream.requests)
        received = self.upstream.requests[0]
        if received["path"].endswith("/responses"):
            self.assertEqual(received["authorization"], ["Bearer " + self.account.access])
            self.assertEqual(received["account_id"], [self.account.account_id])
        return received

    ENDPOINTS = {"/v1/chat/completions": (CHAT, {**CHAT, "messages": [SYSTEM, FIRST, ANSWER, SECOND]},
                                          {**CHAT, "messages": [SYSTEM, SECOND]}),
                 "/v1/responses": (RESPONSES, {**RESPONSES, "input": [FIRST, ANSWER, SECOND]},
                                   {**RESPONSES, "input": [SECOND]})}

    def test_header_is_the_derived_id(self):
        for path, (first, grown, other) in self.ENDPOINTS.items():
            for stream in (False, True):
                with self.subTest(path=path, stream=stream):
                    expected = [hook.cache_session_id(first)]
                    self.assertEqual(self.post(path, {**first, "stream": stream})["session_id"], expected)
                    self.assertEqual(self.post(path, {**grown, "stream": stream})["session_id"], expected)
                    self.assertEqual(self.post(path, {**other, "stream": stream})["session_id"],
                                     [hook.cache_session_id(other)])

    def test_header_is_the_client_id(self):
        for path, (first, _, _) in self.ENDPOINTS.items():
            for name, keys in CLIENT_KEYS.items():
                with self.subTest(path=path, key=name):
                    self.assertEqual(self.post(path, {**first, **keys})["session_id"], ["client-id"])
            with self.subTest(path=path, key="x-litellm-session-id"):
                received = self.post(path, first, {"x-litellm-session-id": "client-id"})
                self.assertEqual(received["session_id"], ["client-id"])

    def test_session_id_stays_out_of_the_chatgpt_body(self):
        for path, (first, _, _) in self.ENDPOINTS.items():
            with self.subTest(path=path):
                body = self.post(path, first)["body"]
                self.assertNotIn("litellm_session_id", body)
                self.assertNotIn("session_id", body)

    def test_previous_response_id_and_tools_pass(self):
        tools = [{"type": "function", "name": "probe", "description": "probe",
                  "parameters": {"type": "object", "properties": {}}}]
        received = self.post("/v1/responses", {**RESPONSES, "previous_response_id": "resp_1", "tools": tools})
        self.assertEqual(received["body"]["previous_response_id"], "resp_1")
        self.assertEqual([t["name"] for t in received["body"]["tools"]], ["probe"])
        self.assertEqual(received["session_id"], [hook.cache_session_id(RESPONSES)])

    def test_other_provider_request_is_unchanged(self):
        body = {"model": "other", "messages": [SYSTEM, FIRST]}
        received = self.post("/v1/chat/completions", body)
        self.assertEqual(received["path"], "/v1/chat/completions")
        self.assertEqual(received["body"], {"model": "gpt-x", "messages": [SYSTEM, FIRST]})
        self.assertNotIn("session_id", received["headers"])
        self.assertFalse([name for name in received["headers"] if "session" in name], received["headers"])

    def test_log_has_no_token_and_no_prompt(self):
        self.post("/v1/chat/completions", CHAT)
        self.log.seek(0)
        output = self.log.read()
        for value in self.account.secrets():
            self.assertNotIn(value, output)
        self.assertNotIn(FIRST["content"], output)


if __name__ == "__main__":
    unittest.main()
