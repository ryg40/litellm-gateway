"""Keep host values out of the tracked gateway examples and the Compose file."""

import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = re.compile(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)\b")
CODEX = ("codex1/", "codex2/", "codex3/", "codex-auto/")


class HostSeparationTest(unittest.TestCase):
    def test_gateway_example_is_generic(self):
        text = (ROOT / "config/gateway.example.yaml").read_text()
        self.assertIsNone(PRIVATE.search(text))
        config = json.loads(text)
        names = [entry["model_name"] for entry in config["model_list"]]
        self.assertIn("openrouter/*", names)
        self.assertFalse([name for name in names if name.startswith(CODEX)])
        local = [entry["litellm_params"] for entry in config["model_list"] if "api_base" in entry["litellm_params"]]
        self.assertTrue(local)
        for params in local:
            self.assertTrue(params["api_base"].startswith("os.environ/"))
        self.assertNotIn("pass_through_endpoints", config["general_settings"])
        self.assertNotIn("fallbacks", config.get("router_settings", {}))

    def test_codex_example_has_only_codex_routes(self):
        config = json.loads((ROOT / "config/gateway.codex.example.yaml").read_text())
        names = [entry["model_name"] for entry in config["model_list"]]
        self.assertTrue(names)
        self.assertTrue(all(name.startswith(CODEX) for name in names))
        for item in config["router_settings"]["fallbacks"]:
            for source, targets in item.items():
                self.assertIn(source, names)
                self.assertTrue(set(targets) <= set(names))

    def test_compose_has_no_host_values(self):
        text = (ROOT / "compose.yaml").read_text()
        self.assertIsNone(PRIVATE.search(text))
        self.assertNotIn("external: true", text)
        self.assertNotRegex(text, r"(?m)^\s*container_name:")
        self.assertIn("${GATEWAY_CONFIG:-./config/gateway.example.yaml}", text)
        self.assertIn("${GATEWAY_PORT:-127.0.0.1:4321}", text)
        self.assertRegex(text, r"(?m)^name: litellm$")

    def test_compose_uses_the_gateway_image(self):
        # The files must exist; the test target of the Dockerfile copies them.
        for path in (ROOT / "compose.yaml", ROOT / "compose.codex.yaml", ROOT / "compose.copilot.yaml"):
            self.assertTrue(path.exists(), f"{path} is missing")
            text = path.read_text()
            with self.subTest(path.name):
                self.assertIn("image: ${GATEWAY_IMAGE:-litellm-gateway:local}", text)
                self.assertNotIn("PYTHONPATH", text)
                self.assertNotIn("./image/", text)
                self.assertNotIn("CHATGPT_AUTH_FILE_HOOK", text)
        self.assertRegex((ROOT / "compose.yaml").read_text(), r"(?m)^      target: gateway$")


if __name__ == "__main__":
    unittest.main()
