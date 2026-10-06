"""Check the model_info template for a model that the cost map lacks.

config/model-info.example.yaml has the entries. Each section runs in its own
Python process: a Router registers into the cost map of the whole process.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/model-info.example.yaml"
NEW = "gpt-6.1-sol"
PRICES = {"input_cost_per_token": 2, "output_cost_per_token": 10,
          "cache_read_input_token_cost": 0.1, "cache_creation_input_token_cost": 2.5}
FLAGS = ("supports_reasoning", "supports_xhigh_reasoning_effort",
         "supports_function_calling", "supports_tool_choice")
SECTIONS = {"gateway_openai": "chat", "gateway_litellm_proxy": "chat"}
# The account service has two entries: the route and an entry that only registers
# the key responses/<model> for the parameter check.
ACCOUNT_KEY = "responses/" + NEW

# Loads the model_list of argv[1] into a Router and sends each model name one
# request with xhigh and a forced tool choice. mock_response gives the answer;
# the parameter check runs before it.
DRIVER = r'''
import json, sys
import litellm
from litellm import Router

NEW = "gpt-6.1-sol"
TOOLS = [{"type": "function", "function": {
    "name": "ping", "description": "Return pong",
    "parameters": {"type": "object", "properties": {}}}}]
out = {"known_before": NEW in litellm.model_cost, "requests": {}}
router = Router(model_list=json.loads(sys.argv[1]), num_retries=0)
for name in router.get_model_names():
    try:
        router.completion(model=name, messages=[{"role": "user", "content": "ping"}],
                          reasoning_effort="xhigh", tools=TOOLS, tool_choice="required",
                          mock_response="pong")
        out["requests"][name] = "ok"
    except Exception as problem:
        out["requests"][name] = f"{type(problem).__name__}: {str(problem)[:160]}"
try:
    out["info"] = dict(litellm.get_model_info(NEW))
except Exception as problem:
    out["info"] = None
out["ids"] = router.get_model_ids()
print("RESULT " + json.dumps(out, default=str))
'''

# Loads the model_list of argv[1] into a Router, then replaces the cost map as the
# reload endpoint of the proxy does, without its database. Records the cost of one
# request per model name before and after the swap.
SWAP_DRIVER = r'''
import asyncio, json, sys
import litellm
from litellm import Router
from litellm.litellm_core_utils.get_model_cost_map import refetch_model_cost_map
from litellm.proxy import proxy_server

NEW = "gpt-6.1-sol"
router = Router(model_list=json.loads(sys.argv[1]), num_retries=0)

def costs():
    found = {}
    for name in router.get_model_names():
        answer = router.completion(model=name, messages=[{"role": "user", "content": "ping"}],
                                   reasoning_effort="xhigh", mock_response="pong")
        found[name] = answer._hidden_params.get("response_cost")
    return found

out = {"before": costs(), "map_before": id(litellm.model_cost)}
fresh = asyncio.run(refetch_model_cost_map(url=litellm.model_cost_map_url))
out["fresh_has"] = NEW in fresh.model_cost_map
proxy_server._swap_in_model_cost_map(fresh.model_cost_map)
out["map_after"] = id(litellm.model_cost)
out["info"] = dict(litellm.get_model_info(NEW))
out["after"] = costs()
print("RESULT " + json.dumps(out, default=str))
'''


# A Router with the model_list of argv[1] and a stand-in for the Codex backend on
# loopback. The stand-in records each request body and answers as the backend does:
# HTTP 400 when "stream" is not true, else a Responses stream with one tool call.
# Sends argv[2] one streaming and one non-streaming chat request.
BACKEND_DRIVER = r'''
import asyncio, json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN = []

def events(model):
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "ping",
            "arguments": "{}", "status": "completed"}
    start = {"id": "resp_1", "object": "response", "created_at": 1790000000, "status": "in_progress",
             "model": model, "output": [], "parallel_tool_calls": True, "tool_choice": "required", "tools": []}
    end = dict(start, status="completed", output=[item],
               usage={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})
    found = [{"type": "response.created", "response": start},
             {"type": "response.output_item.added", "output_index": 0,
              "item": dict(item, arguments="", status="in_progress")},
             {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": "{}"},
             {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0, "arguments": "{}"},
             {"type": "response.output_item.done", "output_index": 0, "item": item},
             {"type": "response.completed", "response": end}]
    return "".join("event: %s\ndata: %s\n\n" % (event["type"], json.dumps(dict(event, sequence_number=number)))
                   for number, event in enumerate(found)).encode()

class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        reasoning = body.get("reasoning") or {}
        SEEN.append({"path": self.path, "model": body.get("model"), "stream": body.get("stream", "absent"),
                     "effort": reasoning.get("effort"), "tool_choice": body.get("tool_choice")})
        if body.get("stream") is True:
            answer, kind, status = events(body.get("model")), "text/event-stream", 200
        else:
            answer, kind, status = json.dumps({"detail": "Stream must be set to true"}).encode(), "application/json", 400
        self.send_response(status)
        self.send_header("content-type", kind)
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["CHATGPT_API_BASE"] = "http://127.0.0.1:%d" % server.server_port

from litellm import Router

TOOLS = [{"type": "function", "function": {
    "name": "ping", "description": "Return pong",
    "parameters": {"type": "object", "properties": {}}}}]
router = Router(model_list=json.loads(sys.argv[1]), num_retries=0)

async def request(stream):
    del SEEN[:]
    found = {}
    try:
        answer = await router.acompletion(model=sys.argv[2], messages=[{"role": "user", "content": "ping"}],
                                          stream=stream, reasoning_effort="xhigh", tools=TOOLS,
                                          tool_choice="required")
        if stream:
            found["finish"] = [choice.finish_reason async for chunk in answer
                               for choice in chunk.choices if choice.finish_reason]
        else:
            found["finish"] = [answer.choices[0].finish_reason]
    except Exception as problem:
        found["error"] = f"{type(problem).__name__}: {str(problem)[:200]}"
    found["backend"] = list(SEEN)
    return found

out = {"stream": asyncio.run(request(True)), "no_stream": asyncio.run(request(False))}
print("RESULT " + json.dumps(out, default=str))
'''


def token(claims):
    part = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return ".".join([part({"alg": "none"}), part(claims), "x"])


def run(model_list, driver=DRIVER, *arguments):
    # The chatgpt provider reads a token file when the Router adds the deployment.
    # A token that is not real and does not expire keeps the run offline.
    with tempfile.TemporaryDirectory() as tokens:
        access = token({"exp": 4102444800, "https://api.openai.com/auth": {"chatgpt_account_id": "acct-test"}})
        Path(tokens, "auth.json").write_text(json.dumps({
            "access_token": access, "refresh_token": "test", "id_token": access,
            "expires_at": 4102444800, "account_id": "acct-test"}))
        env = {**os.environ, "OPENAI_API_KEY": "sk-test", "CODEX_MASTER_KEY": "sk-test",
               "LITELLM_LOCAL_MODEL_COST_MAP": "True", "CHATGPT_TOKEN_DIR": tokens}
        done = subprocess.run([sys.executable, "-c", driver, json.dumps(model_list), *arguments],
                              env=env, capture_output=True, text=True, timeout=300)
    lines = [line for line in done.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, done.stderr[-2000:]
    return json.loads(lines[-1][len("RESULT "):])


class ModelInfoTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = yaml.safe_load(EXAMPLE.read_text())

    def test_example_has_the_three_sections_and_the_required_keys(self):
        self.assertEqual(set(self.example), {*SECTIONS, "account"})
        prefixes = {"gateway_openai": "openai/", "gateway_litellm_proxy": "litellm_proxy/"}
        for section, mode in SECTIONS.items():
            with self.subTest(section=section):
                entries = self.example[section]["model_list"]
                for entry in entries:
                    self.assertEqual(entry["litellm_params"]["model"], prefixes[section] + NEW)
                with_info = [entry["model_info"] for entry in entries if "model_info" in entry]
                self.assertEqual(len(with_info), 1, "only one entry of a proxy has the id")
                info = with_info[0]
                self.assertEqual(info["id"], NEW)
                self.assertEqual(info["litellm_provider"], "openai")
                self.assertEqual(info["mode"], mode)
                for key, rate in PRICES.items():
                    self.assertAlmostEqual(info[key] * 1_000_000, rate)
                for flag in FLAGS:
                    self.assertIs(info[flag], True)

    def test_each_section_passes_xhigh_and_tool_choice_and_has_the_prices(self):
        for section, mode in SECTIONS.items():
            with self.subTest(section=section):
                entries = self.example[section]["model_list"]
                result = run(entries)
                self.assertFalse(result["known_before"], f"the cost map has {NEW}: use another example model")
                self.assertEqual(result["requests"], {entry["model_name"]: "ok" for entry in entries})
                self.assertIn(NEW, result["ids"])
                info = result["info"]
                self.assertIsNotNone(info, "get_model_info does not know the model")
                self.assertEqual(info["litellm_provider"], "openai")
                self.assertEqual(info["mode"], mode)
                for key, rate in PRICES.items():
                    self.assertAlmostEqual(info[key] * 1_000_000, rate)
                self.assertTrue(info["supports_tool_choice"])

    def test_entries_without_the_id_or_the_provider_fail_the_check(self):
        # The control: an openai/ entry is refused when model_info lacks a required key.
        for section in ("gateway_openai",):
            for missing in ("id", "litellm_provider"):
                with self.subTest(section=section, missing=missing):
                    entry = json.loads(json.dumps(self.example[section]["model_list"][0]))
                    del entry["model_info"][missing]
                    result = run([entry])
                    answer = result["requests"][entry["model_name"]]
                    self.assertTrue(answer.startswith("UnsupportedParamsError"), answer)

    def test_account_form_has_a_route_entry_and_a_key_entry(self):
        route, key = self.example["account"]["model_list"]
        self.assertEqual(route["model_name"], NEW)
        self.assertEqual(route["litellm_params"]["model"], "chatgpt/responses/" + NEW)
        # id or litellm_provider on the route entry turn the upstream stream off.
        self.assertNotIn("id", route["model_info"])
        self.assertNotIn("litellm_provider", route["model_info"])
        self.assertEqual(route["model_info"]["mode"], "responses")
        self.assertIs(route["model_info"]["supports_native_streaming"], True)
        self.assertEqual(key["model_info"]["id"], ACCOUNT_KEY)
        self.assertEqual(key["model_info"]["litellm_provider"], "openai")
        self.assertEqual(key["model_info"]["mode"], "responses")
        self.assertFalse(key["litellm_params"]["model"].startswith("chatgpt/"),
                         "a chatgpt/ model on the key entry replaces the keys of the route entry")
        for info in (route["model_info"], key["model_info"]):
            for flag in FLAGS:
                self.assertIs(info[flag], True)

    def test_account_form_sends_stream_true_to_the_backend(self):
        # Codex refuses a request without "stream": true. The client may ask for a
        # stream or not; the request to the backend must have it in both cases.
        entries = self.example["account"]["model_list"]
        for label, model_list in (("as written", entries), ("key entry first", entries[::-1])):
            with self.subTest(order=label):
                result = run(model_list, BACKEND_DRIVER, NEW)
                for kind in ("stream", "no_stream"):
                    found = result[kind]
                    self.assertNotIn("error", found)
                    self.assertEqual(found["finish"], ["tool_calls"])
                    self.assertEqual(found["backend"], [{"path": "/responses", "model": NEW, "stream": True,
                                                         "effort": "xhigh", "tool_choice": "required"}])

    def test_account_route_entry_with_the_openai_provider_loses_the_stream(self):
        # The control, and the fault of the first form: one entry with the bare id and
        # litellm_provider openai. The lookup of chatgpt/responses/<model> then fails,
        # LiteLLM simulates the stream and sends the backend request without "stream".
        route = json.loads(json.dumps(self.example["account"]["model_list"][0]))
        route["model_info"].update(id=NEW, litellm_provider="openai")
        found = run([route], BACKEND_DRIVER, NEW)["stream"]
        self.assertIn("Stream must be set to true", found.get("error", ""))
        self.assertEqual([seen["stream"] for seen in found["backend"]], ["absent"])

    def test_account_route_entry_alone_fails_the_check(self):
        # The control for the key entry: without it the parameter check refuses tool_choice.
        found = run(self.example["account"]["model_list"][:1], BACKEND_DRIVER, NEW)["stream"]
        self.assertTrue(found.get("error", "").startswith("UnsupportedParamsError"), found)
        self.assertEqual(found["backend"], [])

    def test_litellm_proxy_hop_needs_model_info_only_for_the_prices(self):
        # litellm_proxy/ has no capability check, so the request passes without model_info.
        entry = json.loads(json.dumps(self.example["gateway_litellm_proxy"]["model_list"][0]))
        del entry["model_info"]
        result = run([entry])
        self.assertEqual(result["requests"], {entry["model_name"]: "ok"})
        info = result["info"] or {}
        self.assertFalse(info.get("input_cost_per_token"), "a price without model_info")
        self.assertFalse(info.get("output_cost_per_token"), "a price without model_info")

    def test_codex_gateway_example_passes_the_check_and_keeps_the_cost_after_a_swap(self):
        # config/gateway.codex.example.yaml: Luna and Sol have their prices in model_info,
        # so the cost does not depend on the static hook entries after a reload.
        entries = json.loads((ROOT / "config/gateway.codex.example.yaml").read_text())["model_list"]
        names = [entry["model_name"] for entry in entries]
        result = run(entries)
        self.assertEqual(result["requests"], {name: "ok" for name in names})
        for model in ("gpt-6-luna", "gpt-6-sol", NEW):
            self.assertIn(model, result["ids"])
        result = run(entries, SWAP_DRIVER)
        self.assertNotEqual(result["map_before"], result["map_after"], "the cost map was not replaced")
        for name in names:
            with self.subTest(model_name=name):
                self.assertGreater(result["before"][name], 0)
                self.assertEqual(result["after"][name], result["before"][name])

    def test_an_alias_entry_before_the_id_entry_gives_cost_zero(self):
        # The control for the order rule of the template.
        entries = self.example["gateway_openai"]["model_list"]
        result = run(list(reversed(entries)), SWAP_DRIVER)
        self.assertEqual(set(result["before"].values()), {0.0})

    def test_cost_stays_after_a_swap_of_the_cost_map(self):
        # The second gateway_openai entry has no model_info and uses the registered key.
        entries = self.example["gateway_openai"]["model_list"]
        self.assertEqual([("model_info" in entry) for entry in entries], [True, False])
        result = run(entries, SWAP_DRIVER)
        self.assertFalse(result["fresh_has"], f"the cost map has {NEW}: use another example model")
        self.assertNotEqual(result["map_before"], result["map_after"], "the cost map was not replaced")
        for key, rate in PRICES.items():
            self.assertAlmostEqual(result["info"][key] * 1_000_000, rate)
        for entry in entries:
            with self.subTest(model_name=entry["model_name"]):
                before = result["before"][entry["model_name"]]
                self.assertGreater(before, 0)
                self.assertEqual(result["after"][entry["model_name"]], before)


if __name__ == "__main__":
    unittest.main()
