"""Check Codex Sol capability lookups with the pinned LiteLLM image.

Run with PYTHONPATH pointing at image/hooks so Python loads sitecustomize.
"""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import litellm
from litellm.utils import get_optional_params
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOLS = [{"type": "function", "function": {
    "name": "ping", "description": "Return pong",
    "parameters": {"type": "object", "properties": {}}}}]


class SolCapabilitiesTest(unittest.TestCase):
    def test_sol_and_luna_keep_xhigh_and_forced_tool_choice(self):
        for model in ("gpt-6-sol", "gpt-6-luna"):
            for lookup, provider in ((model, "openai"), (f"responses/{model}", "chatgpt")):
                with self.subTest(lookup=lookup):
                    self.assertTrue(litellm.model_cost[lookup]["supports_xhigh_reasoning_effort"])
                    result = get_optional_params(
                        model=lookup, custom_llm_provider=provider,
                        reasoning_effort="xhigh", tools=TOOLS, tool_choice="required",
                        drop_params=False,
                    )
                    self.assertEqual(result["reasoning_effort"], "xhigh")
                    self.assertEqual(result["tool_choice"], "required")
                    self.assertEqual(result["tools"], TOOLS)

    def test_account_sol_model_info(self):
        config = yaml.safe_load((ROOT / "config/codex.yaml").read_text())
        sol = next(entry for entry in config["model_list"] if entry["model_name"] == "gpt-6-sol")
        self.assertEqual(sol["litellm_params"]["model"], "chatgpt/responses/gpt-6-sol")
        info = sol["model_info"]
        for capability in ("supports_native_streaming", "supports_xhigh_reasoning_effort",
                           "supports_function_calling", "supports_tool_choice"):
            self.assertIs(info[capability], True)

    def test_account_sol_6_1_has_the_account_form_of_the_template(self):
        config = yaml.safe_load((ROOT / "config/codex.yaml").read_text())
        entries = [entry for entry in config["model_list"] if entry["model_name"] == "gpt-6.1-sol"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["litellm_params"]["model"], "chatgpt/responses/gpt-6.1-sol")
        # The account form of the template: the route entry and the entry with the key
        # responses/gpt-6.1-sol. tests/test_model_info.py checks the form with a backend.
        template = yaml.safe_load((ROOT / "config/model-info.example.yaml").read_text())
        both = [entry for entry in config["model_list"] if "gpt-6.1-sol" in entry["model_name"]]
        self.assertEqual(both, template["account"]["model_list"])
        ids = [entry["model_info"]["id"] for entry in config["model_list"] if "id" in entry["model_info"]]
        self.assertEqual(ids, ["responses/gpt-6.1-sol"])

    def test_example_gateway_sol_alias_and_prices(self):
        text = (ROOT / "config/gateway.codex.example.yaml").read_text()
        config = yaml.safe_load(text)
        self.assertEqual(config, json.loads(text), "the YAML and the JSON reading differ")
        entries = {entry["model_name"]: entry for entry in config["model_list"]}
        self.assertEqual(len(entries), len(config["model_list"]))
        for prefix in ("codex1", "codex2", "codex-auto"):
            self.assertEqual(entries[f"{prefix}/sol"]["litellm_params"]["model"], "openai/gpt-6.1-sol")
            self.assertEqual(entries[f"{prefix}/gpt-6.1-sol"]["litellm_params"]["model"], "openai/gpt-6.1-sol")
            self.assertEqual(entries[f"{prefix}/gpt-6-sol"]["litellm_params"]["model"], "openai/gpt-6-sol")
        # One entry per model has the id and the prices; the others use the registered key.
        with_id = {name: entry["model_info"] for name, entry in entries.items()
                   if "id" in entry.get("model_info", {})}
        self.assertEqual({name: info["id"] for name, info in with_id.items()},
                         {"codex1/gpt-6-luna": "gpt-6-luna", "codex1/gpt-6-sol": "gpt-6-sol",
                          "codex1/gpt-6.1-sol": "gpt-6.1-sol"})
        # The entry with the id stands before each other entry for the same model.
        # With an alias entry first, the router reports cost 0.0 for the model.
        first = {}
        for entry in config["model_list"]:
            first.setdefault(entry["litellm_params"]["model"], entry["model_name"])
        for name, info in with_id.items():
            self.assertEqual(first["openai/" + info["id"]], name)
        template = yaml.safe_load((ROOT / "config/model-info.example.yaml").read_text())
        self.assertEqual(entries["codex1/gpt-6.1-sol"], template["gateway_openai"]["model_list"][0])
        # Luna and Sol: the prices of the static hook entries, so a cost-map reload keeps them.
        for name, info in with_id.items():
            self.assertEqual(info["litellm_provider"], "openai")
            self.assertEqual(info["mode"], "chat")
            for flag in ("supports_reasoning", "supports_xhigh_reasoning_effort",
                         "supports_function_calling", "supports_tool_choice"):
                self.assertIs(info[flag], True)
            if info["id"] in ("gpt-6-luna", "gpt-6-sol"):
                for key in ("input_cost_per_token", "output_cost_per_token",
                            "cache_read_input_token_cost", "cache_creation_input_token_cost"):
                    self.assertAlmostEqual(info[key], litellm.model_cost[info["id"]][key], places=15)

    def test_generator_preserves_sol_aliases(self):
        spec = importlib.util.spec_from_file_location("enable_codex", ROOT / "scripts/enable-codex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for account in (1, 2):
                tokens = root / f"state/codex{account}/auth.json"
                tokens.parent.mkdir(parents=True)
                tokens.write_text(json.dumps({"access_token": "test", "refresh_token": "test"}))
            path = root / ".local/config/gateway.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"model_list": [{"model_name": "codex1/old"}],
                                        "router_settings": {"fallbacks": [{"codex-auto/old": ["codex1/old"]}]}}))
            original_root = module.ROOT
            try:
                module.ROOT = root
                with patch.dict(os.environ), redirect_stdout(io.StringIO()):
                    os.environ.pop("GATEWAY_CONFIG", None)
                    module.main()
            finally:
                module.ROOT = original_root
            fallbacks = json.loads(path.read_text())["router_settings"]["fallbacks"]
            self.assertNotIn({"codex-auto/old": ["codex1/old"]}, fallbacks)
            self.assertIn({"codex-auto/sol": ["codex1/sol"]}, fallbacks)
            entries = json.loads(path.read_text())["model_list"]
            # The short alias follows the newest version; the full names stay pinned.
            for prefix in ("codex1", "codex2", "codex-auto"):
                for alias, target in (("sol", "gpt-6.1-sol"), ("gpt-6-sol", "gpt-6-sol"),
                                      ("gpt-6.1-sol", "gpt-6.1-sol")):
                    match = next(entry for entry in entries if entry["model_name"] == f"{prefix}/{alias}")
                    self.assertEqual(match["litellm_params"]["model"], f"openai/{target}")
            # LiteLLM reads the host file as YAML: each price must be a number there too.
            self.assertEqual(yaml.safe_load(path.read_text()), json.loads(path.read_text()))
            self.assertNotIn("codex1/old", {entry["model_name"] for entry in entries})

    def test_generator_raises_a_low_max_fallbacks(self):
        spec = importlib.util.spec_from_file_location("enable_codex", ROOT / "scripts/enable-codex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # (logins, max_fallbacks of the host file or None, expected value)
        cases = ((3, 1, 2), (2, 1, 1), (3, 5, 5), (2, 5, 5), (3, None, 2), (2, None, 1))
        for logins, before, expected in cases:
            with self.subTest(logins=logins, before=before), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                for account in range(1, logins + 1):
                    tokens = root / f"state/codex{account}/auth.json"
                    tokens.parent.mkdir(parents=True)
                    tokens.write_text(json.dumps({"access_token": "test", "refresh_token": "test"}))
                path = root / ".local/config/gateway.yaml"
                path.parent.mkdir(parents=True)
                settings = {} if before is None else {"max_fallbacks": before}
                path.write_text(json.dumps({"model_list": [], "router_settings": settings}))
                original_root = module.ROOT
                try:
                    module.ROOT = root
                    with patch.dict(os.environ), redirect_stdout(io.StringIO()):
                        os.environ.pop("GATEWAY_CONFIG", None)
                        module.main()
                finally:
                    module.ROOT = original_root
                router = json.loads(path.read_text())["router_settings"]
                self.assertEqual(router["max_fallbacks"], expected)
                targets = [f"codex{n}/sol" for n in (3, 1)][3 - logins:]
                self.assertIn({"codex-auto/sol": targets}, router["fallbacks"])
                # Each fallback target of a route can run.
                self.assertTrue(all(len(t) <= router["max_fallbacks"]
                                    for item in router["fallbacks"] for t in item.values()))


if __name__ == "__main__":
    unittest.main()
