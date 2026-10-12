"""Check native Codex routing with synthetic logins and temporary gateway files."""
import ast
from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"luna", "gpt-6-luna", "sol", "gpt-6-sol", "gpt-6.1-sol", "astra", "gpt-6-astra"}
TEMPLATE = json.loads((ROOT / "config/gateway.codex.example.yaml").read_text())
SPEC = importlib.util.spec_from_file_location("enable_codex_routing", ROOT / "scripts/enable-codex.py")
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)
OTHER_ROUTE = {"model_name": "other/model", "litellm_params": {"model": "openai/model"}}
OTHER_FALLBACK = {"other/model": ["other/backup"]}
HOST = {
    "model_list": [OTHER_ROUTE, {"model_name": "other/backup"}, {"model_name": "codex3/stale"}],
    "router_settings": {
        "fallbacks": [OTHER_FALLBACK, {"codex-auto/stale": ["codex3/stale"], "other/mixed": ["other/backup"]}],
        "max_fallbacks": 5,
        "num_retries": 0,
        "cooldown_time": 75,
        "retry_policy": {"AuthenticationErrorRetries": 0},
    },
    "litellm_settings": {"drop_params": False},
    "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
}


class NativeRoutingTests(unittest.TestCase):
    def assert_routes(self, config, accounts):
        order = ["codex3", "codex2", "codex1"] if accounts == 3 else ["codex2", "codex1"]
        entries = {entry["model_name"]: entry for entry in config["model_list"]}
        self.assertEqual(len(entries), len(config["model_list"]), "duplicate routes")
        automatic = {name for name in entries if name.startswith("codex-auto/")}
        self.assertEqual(automatic, {f"codex-auto/{model}" for model in MODELS})
        fallbacks = [(source, targets) for item in config["router_settings"]["fallbacks"]
                     for source, targets in item.items() if source.startswith("codex-auto/")]
        self.assertEqual(len(fallbacks), 7, "duplicate or stale automatic fallback sources")
        self.assertEqual(dict(fallbacks), {
            f"codex-auto/{model}": [f"{account}/{model}" for account in order[1:]] for model in MODELS})
        for model in MODELS:
            source = f"codex-auto/{model}"
            self.assertEqual(entries[source]["litellm_params"]["api_base"], f"http://{order[0]}:4000/v1")
            for target in dict(fallbacks)[source]:
                self.assertIn(target, entries)
                self.assertNotEqual(target.split("/", 1)[0], order[0], "self account fallback")
                self.assertEqual(entries[target]["litellm_params"]["api_base"],
                                 f"http://{target.split('/', 1)[0]}:4000/v1")
        if accounts == 2:
            self.assertNotIn("codex3", json.dumps(config))
        expected = copy.deepcopy(TEMPLATE["model_list"])
        expected = [entry for entry in expected if not entry["model_name"].startswith("codex3/") or accounts == 3]
        for entry in expected:
            if entry["model_name"].startswith("codex-auto/"):
                entry["litellm_params"]["api_base"] = f"http://{order[0]}:4000/v1"
        actual = [entry for entry in config["model_list"] if entry["model_name"].startswith("codex")]
        self.assertEqual(actual, expected, "models, prices, capabilities or request settings changed")

    def login(self, root, account, data=None):
        path = root / f"state/codex{account}/auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data if data is not None else {"access_token": "test", "refresh_token": "test"}))

    def host_path(self, root):
        path = root / ".local/config/gateway.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(HOST))
        return path

    def generate(self, root, args=()):
        with patch.object(GENERATOR, "ROOT", root), patch.dict(os.environ), redirect_stdout(io.StringIO()):
            os.environ.pop("GATEWAY_CONFIG", None)
            GENERATOR.main(args)
        return json.loads((root / ".local/config/gateway.yaml").read_text())

    def assert_other_settings(self, config):
        self.assertEqual([entry for entry in config["model_list"] if not entry["model_name"].startswith("codex")],
                         HOST["model_list"][:2])
        self.assertEqual([item for item in config["router_settings"]["fallbacks"]
                          if not any(source.startswith("codex") for source in item)],
                         [OTHER_FALLBACK, {"other/mixed": ["other/backup"]}])
        self.assertEqual({key: value for key, value in config["router_settings"].items() if key != "fallbacks"},
                         {key: value for key, value in HOST["router_settings"].items() if key != "fallbacks"})
        for key in ("litellm_settings", "general_settings"):
            self.assertEqual(config[key], HOST[key])

    def test_three_account_template_and_defaults_agree(self):
        self.assert_routes(TEMPLATE, 3)
        tree = ast.parse((ROOT / "image/routers/quota_router.py").read_text())
        default = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "DEFAULT_ORDER" for target in node.targets))
        self.assertEqual(default.split(","), list(GENERATOR.DEFAULT_ACCOUNT_ORDER))
        self.assertEqual(default, "codex3,codex2,codex1")

    def test_detected_accounts_have_exact_routes(self):
        for accounts in (2, 3):
            with self.subTest(accounts=accounts), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for account in range(1, accounts + 1):
                    self.login(root, account)
                self.host_path(root)
                config = self.generate(root)
                self.assert_routes(config, accounts)
                self.assert_other_settings(config)

    def test_explicit_selection_upgrade_downgrade_and_repeated_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for account in (1, 2, 3):
                self.login(root, account)
            path = self.host_path(root)
            for accounts in (2, 3, 2):
                config = self.generate(root, ("--accounts", str(accounts)))
                self.assert_routes(config, accounts)
                self.assert_other_settings(config)
                before = path.read_bytes()
                self.assertEqual(self.generate(root, ("--accounts", str(accounts))), config)
                self.assertEqual(path.read_bytes(), before)

    def test_automatic_detection_upgrades_and_downgrades_with_logins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for account in (1, 2):
                self.login(root, account)
            self.host_path(root)
            self.assert_routes(self.generate(root), 2)
            self.login(root, 3)
            self.assert_routes(self.generate(root), 3)
            self.login(root, 3, {"device_code_requested_at": 1000})
            config = self.generate(root)
            self.assert_routes(config, 2)
            self.assert_other_settings(config)

    def test_explicit_missing_or_incomplete_login_refuses_without_writing(self):
        for accounts, account, data, message in (
                (2, 2, None, "Run sh scripts/login-codex.sh 2 first"),
                (2, 2, {"access_token": "test"}, "Account 2 has no complete login"),
                (3, 3, None, "Run sh scripts/login-codex.sh 3 first"),
                (3, 3, {"refresh_token": "test"}, "Account 3 has no complete login")):
            with self.subTest(accounts=accounts, account=account, data=data), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for number in range(1, account):
                    self.login(root, number)
                if data is not None:
                    self.login(root, account, data)
                path = self.host_path(root)
                before = path.read_bytes()
                with self.assertRaises(SystemExit) as error:
                    self.generate(root, ("--accounts", str(accounts)))
                self.assertEqual(str(error.exception), message)
                self.assertEqual(path.read_bytes(), before)

    def test_automatic_detection_refuses_a_gap_before_two_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.login(root, 1)
            self.login(root, 3)
            path = self.host_path(root)
            before = path.read_bytes()
            with self.assertRaises(SystemExit) as error:
                self.generate(root)
            self.assertEqual(str(error.exception), "Run sh scripts/login-codex.sh 2 first")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
