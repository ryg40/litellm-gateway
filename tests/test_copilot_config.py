"""Check the configuration of the service copilot, the gateway hop example and,
when present, the host proposal file.

COPILOT_PROPOSAL names a proposal file to check with the rules of the gateway example.
The service file is YAML; the test reads it with PyYAML when the module is present
(the gateway image has it) and with a line check otherwise.
"""

import json
import os
from pathlib import Path
import re
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "config/copilot.yaml"
EXAMPLE = ROOT / "config/gateway.copilot.example.yaml"
PRIVATE = re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
# A service entry holds no credential and no address: the provider reads both from the token directory.
SERVICE_PARAMS = {"model", "timeout", "stream_timeout", "extra_headers"}
HOP_PARAMS = {"model", "api_base", "api_key", "timeout", "stream_timeout"}
EXCLUDED = re.compile(r"gpt-3\.5|gpt-4($|[-.o])")
EMBEDDING_MODELS = {"text-embedding-ada-002", "text-embedding-3-small"}
EMBEDDING_INFO = {"mode": "embedding", "output_vector_size": 1536}
# The form that registers an id that the cost map does not have (docs/copilot.md).
MODEL_INFO_KEYS = {"id", "litellm_provider", "mode", "input_cost_per_token", "output_cost_per_token",
                   "supports_function_calling", "supports_tool_choice"}


def service_entries():
    """Return (list of (name, model, timeout, stream_timeout, mode, size), config or None)."""
    text = SERVICE.read_text()
    try:
        import yaml
    except ImportError:
        names = re.findall(r"^  - model_name: (\S+)$", text, re.M)
        models = re.findall(r"^      model: (\S+)$", text, re.M)
        timeouts = [int(v) for v in re.findall(r"^      timeout: (\d+)$", text, re.M)]
        streams = [int(v) for v in re.findall(r"^      stream_timeout: (\d+)$", text, re.M)]
        blocks = re.split(r"^  - model_name: ", text.split("general_settings:")[0], flags=re.M)[1:]
        modes = []
        sizes = []
        for block in blocks:
            mode = re.search(r"^      mode: (\S+)$", block, re.M)
            size = re.search(r"^      output_vector_size: (\d+)$", block, re.M)
            modes.append(mode.group(1) if mode else None)
            sizes.append(int(size.group(1)) if size else None)
        assert len(names) == len(models) == len(timeouts) == len(streams) == len(modes), "service file layout"
        return list(zip(names, models, timeouts, streams, modes, sizes)), None
    config = yaml.safe_load(text)
    return [(e["model_name"], e["litellm_params"]["model"], e["litellm_params"]["timeout"],
             e["litellm_params"]["stream_timeout"], (e.get("model_info") or {}).get("mode"),
             (e.get("model_info") or {}).get("output_vector_size"))
            for e in config["model_list"]], config


class CopilotConfigTest(unittest.TestCase):
    def test_service(self):
        text = SERVICE.read_text()
        self.assertIsNone(PRIVATE.search(text))
        entries, config = service_entries()
        self.assertTrue(entries)
        names = [entry[0] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(EMBEDDING_MODELS, set(names))
        targets = {entry[1].split("/", 1)[1] for entry in entries}
        for name, model, timeout, stream_timeout, mode, size in entries:
            self.assertNotIn("/", name)
            self.assertTrue(model.startswith("github_copilot/"), name)
            upstream = model.split("/", 1)[1]
            self.assertIsNone(EXCLUDED.search(upstream), name)
            self.assertEqual(timeout, stream_timeout, name)
            # Below the 570 s of the gateway hop.
            self.assertLess(timeout, 570, name)
            if upstream in EMBEDDING_MODELS:
                self.assertEqual(mode, "embedding", name)
                self.assertEqual(name, upstream)
                self.assertEqual(size, 1536, name)
                self.assertEqual(timeout, 540, name)
            else:
                self.assertNotEqual(mode, "embedding", name)
            if "codex" in upstream:
                self.assertEqual(mode, "responses", name)
            # A full name is pinned to its own id; only a short alias moves.
            if name in targets:
                self.assertEqual(model, f"github_copilot/{name}")
        if config is None:
            for line in ("  master_key: os.environ/COPILOT_MASTER_KEY", "  disable_spend_logs: true",
                         "  num_retries: 0", "  disable_copilot_system_to_assistant: true"):
                self.assertIn("\n" + line + "\n", text)
            self.assertNotIn("api_key", text)
            self.assertNotIn("api_base", text)
            return
        for entry in config["model_list"]:
            self.assertLessEqual(set(entry["litellm_params"]), SERVICE_PARAMS, entry["model_name"])
        self.assertEqual(config["general_settings"]["master_key"], "os.environ/COPILOT_MASTER_KEY")
        self.assertIs(config["general_settings"]["disable_spend_logs"], True)
        self.assertEqual(config["litellm_settings"]["num_retries"], 0)
        self.assertIs(config["litellm_settings"]["disable_copilot_system_to_assistant"], True)

    def check_hops(self, path):
        text = Path(path).read_text()
        self.assertIsNone(PRIVATE.search(text))
        config = json.loads(text)
        entries = config["model_list"]
        self.assertTrue(entries)
        names = [entry["model_name"] for entry in entries]
        self.assertEqual(len(names), len(set(names)))
        ids = []
        for entry in entries:
            name, params = entry["model_name"], entry["litellm_params"]
            self.assertTrue(name.startswith("copilot/"), name)
            alias = name.split("/", 1)[1]
            # The hop calls the alias that the service copilot serves.
            self.assertEqual(params["model"], f"litellm_proxy/{alias}", name)
            self.assertEqual(params["api_base"], "http://copilot:4000", name)
            self.assertEqual(params["api_key"], "os.environ/COPILOT_MASTER_KEY", name)
            self.assertEqual(set(params), HOP_PARAMS, name)
            self.assertEqual(params["timeout"], 570, name)
            self.assertEqual(params["stream_timeout"], 570, name)
            info = entry.get("model_info")
            if "embedding" in alias:
                self.assertEqual(info, EMBEDDING_INFO, name)
            elif info is not None:
                self.assertNotEqual(info.get("mode"), "embedding", name)
                self.assertLessEqual(MODEL_INFO_KEYS, set(info), name)
                self.assertEqual(info["id"], alias, name)
                ids.append(info["id"])
        self.assertEqual(len(ids), len(set(ids)))
        # The provider setting belongs to the service, not to the gateway.
        self.assertNotIn("disable_copilot_system_to_assistant", config.get("litellm_settings", {}))
        return {name.split("/", 1)[1] for name in names}

    def test_example(self):
        aliases = self.check_hops(EXAMPLE)
        served = {entry[0] for entry in service_entries()[0]}
        self.assertEqual(aliases, served)

    def test_service_without_yaml(self):
        expected, _ = service_entries()
        with mock.patch.dict("sys.modules", {"yaml": None}):
            entries, config = service_entries()
            self.assertEqual(entries, expected)
            self.assertIsNone(config)
            self.test_service()

    def test_service_rejects_invalid_embedding_entries(self):
        text = SERVICE.read_text()
        invalid = [
            text.replace("      mode: embedding\n", ""),
            text.replace("      mode: embedding", "      mode: chat"),
            text.replace("      output_vector_size: 1536", "      output_vector_size: 1024"),
            text.replace("      mode: responses", "      mode: embedding", 1),
            text.replace("  - model_name: text-embedding-ada-002", "  - model_name: embedding"),
            text.replace("general_settings:", """  - model_name: text-embedding-unlisted
    litellm_params:
      model: github_copilot/text-embedding-unlisted
      timeout: 540
      stream_timeout: 540
    model_info:
      mode: embedding
      output_vector_size: 1536
general_settings:"""),
        ]
        for use_yaml in (True, False):
            modules = {} if use_yaml else {"yaml": None}
            for index, content in enumerate(invalid):
                with self.subTest(use_yaml=use_yaml, case=index):
                    with mock.patch.dict("sys.modules", modules), mock.patch.object(Path, "read_text", return_value=content):
                        with self.assertRaises(AssertionError):
                            self.test_service()

    def test_hops_reject_invalid_embedding_entries(self):
        original = json.loads(EXAMPLE.read_text())
        for info in (None, {"mode": "chat", "output_vector_size": 1536},
                     {"mode": "embedding"}, {"mode": "embedding", "output_vector_size": 1024}):
            with self.subTest(info=info):
                config = json.loads(json.dumps(original))
                entry = next(e for e in config["model_list"] if e["model_name"] == "copilot/text-embedding-ada-002")
                entry["model_info"] = info
                with mock.patch.object(Path, "read_text", return_value=json.dumps(config)):
                    with self.assertRaises(AssertionError):
                        self.check_hops(EXAMPLE)
        original["model_list"][0]["model_info"] = dict(EMBEDDING_INFO)
        with mock.patch.object(Path, "read_text", return_value=json.dumps(original)):
            with self.assertRaises(AssertionError):
                self.check_hops(EXAMPLE)

    def test_proposal(self):
        path = os.environ.get("COPILOT_PROPOSAL")
        if not path:
            self.skipTest("COPILOT_PROPOSAL is not set")
        self.check_hops(path)


if __name__ == "__main__":
    unittest.main()
