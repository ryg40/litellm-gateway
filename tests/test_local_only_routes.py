"""A local-only route has no path to a model outside the private network."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import gateway_config  # noqa: E402

ROUTE = "local/test"
OTHER = "cloud/test"


def config(**settings):
    return {
        "model_list": [
            {"model_name": ROUTE, "litellm_params": {"api_base": "http://127.0.0.1:9000/v1"}},
            {"model_name": OTHER, "litellm_params": {"model": "openai/test"}},
        ],
        "router_settings": settings,
    }


class ProblemTests(unittest.TestCase):
    def problems(self, value):
        return gateway_config.local_only_problems(value, [ROUTE])

    def test_clean_route_passes(self):
        self.assertEqual(self.problems(config()), [])

    def test_container_name_passes(self):
        value = config()
        value["model_list"][0]["litellm_params"]["api_base"] = "http://engine:9000/v1"
        self.assertEqual(self.problems(value), [])

    def test_fallback_on_another_route_passes(self):
        self.assertEqual(self.problems(config(fallbacks=[{OTHER: [ROUTE]}])), [])

    def test_each_fallback_setting_fails(self):
        for name in gateway_config.FALLBACK_SETTINGS:
            with self.subTest(name=name):
                self.assertEqual(len(self.problems(config(**{name: [{ROUTE: [OTHER]}]}))), 1)

    def test_wildcard_fallback_fails(self):
        self.assertEqual(len(self.problems(config(fallbacks=[{"*": [OTHER]}]))), 1)

    def test_default_fallbacks_fail(self):
        self.assertEqual(len(self.problems(config(default_fallbacks=[OTHER]))), 1)

    def test_litellm_settings_are_checked(self):
        value = config()
        value["litellm_settings"] = {"fallbacks": [{ROUTE: [OTHER]}]}
        self.assertEqual(len(self.problems(value)), 1)

    def test_second_deployment_without_base_fails(self):
        value = config()
        value["model_list"].append({"model_name": ROUTE, "litellm_params": {"model": "openai/test"}})
        self.assertEqual(len(self.problems(value)), 1)

    def test_public_base_fails(self):
        value = config()
        value["model_list"][0]["litellm_params"]["api_base"] = "https://api.example.com/v1"
        self.assertEqual(len(self.problems(value)), 1)

    def test_missing_route_fails(self):
        value = config()
        del value["model_list"][0]
        self.assertEqual(len(self.problems(value)), 1)


class WriteAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.target = self.root / "gateway.yaml"
        (self.root / ".env").write_text(f"LOCAL_ONLY_ROUTES={ROUTE}\n")

    def run_check(self, value):
        self.target.write_text(json.dumps(value))
        env = dict(os.environ, GATEWAY_CONFIG=str(self.target), LOCAL_ONLY_ROUTES=ROUTE)
        return subprocess.run([sys.executable, str(ROOT / "scripts/check-local-only-routes.py")],
                              env=env, capture_output=True, text=True, check=False)

    def test_save_refuses_a_breach_and_keeps_the_file(self):
        self.target.write_text("old")
        with self.assertRaises(SystemExit):
            gateway_config.save(self.root, self.target, config(fallbacks=[{ROUTE: [OTHER]}]))
        self.assertEqual(self.target.read_text(), "old")

    def test_save_writes_a_clean_config(self):
        gateway_config.save(self.root, self.target, config())
        self.assertEqual(json.loads(self.target.read_text()), config())

    def test_command_passes_a_clean_config(self):
        self.assertEqual(self.run_check(config()).returncode, 0)

    def test_command_fails_on_a_breach(self):
        result = self.run_check(config(fallbacks=[{ROUTE: [OTHER]}]))
        self.assertEqual(result.returncode, 1)
        self.assertIn("fallback on", result.stdout)


if __name__ == "__main__":
    unittest.main()
