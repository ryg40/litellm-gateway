"""Account-order switching with synthetic host files and no live Docker operations."""
import argparse
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("switch_codex_order", SCRIPTS / "switch-codex-order.py")
SWITCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SWITCH)
REAL_COMMAND = SWITCH.command

HOST = '''services:
  gateway:
    container_name: keep-this-name
  codex-router:
    environment:
      CODEX_ACCOUNT_ORDER: "codex3,codex2,codex1" # keep comment
    volumes: !override
      - ./state/codex2:/tokens/codex2:ro
      - ./state/codex3:/tokens/codex3:ro
'''


class SwitchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / SWITCH.OVERRIDE
        self.target.parent.mkdir()
        self.target.write_text(HOST)
        self.target.chmod(0o640)
        self.files = [self.root / name for name in ("compose.yaml", "compose.codex.yaml", SWITCH.OVERRIDE)]
        (self.root / ".env").write_text("COMPOSE_FILE=" + ":".join(str(p) for p in self.files) + "\n")
        for name in ("codex2", "codex3"):
            (self.root / "state" / name).mkdir(parents=True)
        self.gateway_path = self.root / "gateway.json"
        self.gateway = {"model_list": [{"model_name": "codex-auto/luna", "litellm_params": {
            "api_base": "http://codex-router:4000/v1"}}]}
        self.gateway_path.write_text(json.dumps(self.gateway))
        self.rendered = {"name": "litellm", "services": {
            "codex-router": {"image": "image:fixed", "environment": {"CODEX_ACCOUNT_ORDER": "codex3,codex2,codex1"},
                             "volumes": [{"type": "bind", "source": str(self.root / "state" / name),
                                          "target": "/tokens/" + name, "read_only": True}
                                         for name in ("codex2", "codex3")]},
            "gateway": {"volumes": [{"type": "bind", "source": str(self.gateway_path),
                                     "target": "/config/config.yaml"}]},
            "database": {"image": "postgres:fixed"}}}
        self.live = {"Id": "container-before", "Image": "sha256:running", "Config": {
            "Env": ["CODEX_ACCOUNT_ORDER=codex3,codex2,codex1"], "Labels": {
                "com.docker.compose.project.working_dir": str(self.root),
                "com.docker.compose.config-hash": "same-hash"}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}}}
        self.image = "sha256:running"
        self.hash = "same-hash"
        self.fail_up = False
        self.unhealthy = False
        self.extra_change = False
        self.later_order = None
        self.calls = []
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.commands = patch.object(SWITCH, "command", side_effect=self.fake_command)
        self.commands.start()
        self.addCleanup(self.commands.stop)

    def fake_command(self, args, root):
        self.calls.append(args)
        self.assertEqual(root, self.root)
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": self.image}])
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.live])
        if "--hash" in args:
            return "codex-router " + self.hash + "\n"
        if "config" in args:
            rendered = copy.deepcopy(self.rendered)
            path = Path(args[max(i for i, arg in enumerate(args) if arg == "-f") + 1])
            order = SWITCH.ORDER_LINE.search(path.read_text())["value"].split('"')[1]
            rendered["services"]["codex-router"]["environment"]["CODEX_ACCOUNT_ORDER"] = self.later_order or order
            if self.extra_change and path != self.target:
                rendered["services"]["database"]["image"] = "postgres:changed"
            return json.dumps(rendered)
        if "ps" in args:
            return self.live["Id"] + "\n"
        if "up" in args:
            if self.fail_up:
                raise SWITCH.Refused("simulated recreate failure")
            order = SWITCH.ORDER_LINE.search(self.target.read_text())["value"].split('"')[1]
            self.live["Config"]["Env"] = ["CODEX_ACCOUNT_ORDER=" + order]
            self.live["Id"] = "container-after"
            if self.unhealthy:
                self.live["State"]["Health"]["Status"] = "unhealthy"
            return ""
        self.fail("Unexpected command: " + repr(args))

    def invoke(self, order=("2-3-1",), dry_run=False, yes=True):
        args = argparse.Namespace(order=list(order), dry_run=dry_run, yes=yes, status=False)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = SWITCH.run(args, self.root)
        return result, output.getvalue()

    def assert_unchanged(self):
        self.assertEqual(self.target.read_text(), HOST)
        self.assertFalse(any("up" in args for args in self.calls))
        self.assertEqual(list(self.target.parent.iterdir()), [self.target])

    def test_order_forms(self):
        for values in (["2-3-1"], ["2", "3", "1"], ["codex2,codex3,codex1"], ["CODEX2", "CODEX3", "CODEX1"]):
            self.assertEqual(SWITCH.parse_order(values), ["codex2", "codex3", "codex1"])
        for values in ([], ["1-1-2"], ["1-2"], ["1-2-4"], ["1; touch /tmp/oops"], ["1-2-3-1"]):
            with self.subTest(values=values), self.assertRaises(SWITCH.Refused):
                SWITCH.parse_order(values)

    def test_replace_preserves_comments_tags_and_crlf(self):
        original = HOST.replace("\n", "\r\n")
        result = SWITCH.replace_order(original, ["codex2", "codex3", "codex1"])
        self.assertEqual(result, original.replace("codex3,codex2,codex1", "codex2,codex3,codex1"))
        for text in ("services: {}", HOST + HOST):
            with self.assertRaises(SWITCH.Refused):
                SWITCH.replace_order(text, ["codex2", "codex3", "codex1"])

    def test_status_reads_without_writing(self):
        _, output = self.invoke(order=(), yes=False)
        self.assertIn("Running order:    codex3,codex2,codex1", output)
        self.assert_unchanged()

    def test_dry_run(self):
        _, output = self.invoke(dry_run=True)
        self.assertIn("Dry run", output)
        self.assert_unchanged()

    def test_cancelled(self):
        with patch("builtins.input", return_value="n"):
            _, output = self.invoke(yes=False)
        self.assertIn("Cancelled", output)
        self.assert_unchanged()

    def test_apply_only_recreates_router_and_preserves_mode(self):
        _, output = self.invoke()
        self.assertIn("Verified running order: codex2,codex3,codex1", output)
        self.assertEqual(self.target.read_text(), HOST.replace("codex3,codex2,codex1", "codex2,codex3,codex1"))
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        backups = list(self.target.parent.glob("compose.host.before-codex-order-*.yaml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), HOST)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        up = [args for args in self.calls if "up" in args]
        self.assertEqual(len(up), 1)
        self.assertEqual(up[0][up[0].index("up"):], ["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                         "--force-recreate", "--wait", "--wait-timeout", "120", "codex-router"])

    def test_switch_back(self):
        self.invoke()
        _, output = self.invoke(order=("3-2-1",))
        self.assertIn("Verified running order: codex3,codex2,codex1", output)
        self.assertEqual(self.target.read_text(), HOST)

    def test_same_order_is_noop(self):
        _, output = self.invoke(order=("3-2-1",))
        self.assertIn("Nothing changed", output)
        self.assert_unchanged()

    def test_invalid_order_never_calls_docker(self):
        with self.assertRaises(SWITCH.Refused):
            self.invoke(order=("2-2-1",))
        self.assertEqual(self.calls, [])
        self.assert_unchanged()

    def test_native_routes_refused(self):
        self.gateway["model_list"][0]["litellm_params"]["api_base"] = "http://codex3:4000/v1"
        self.gateway_path.write_text(json.dumps(self.gateway))
        with self.assertRaisesRegex(SWITCH.Refused, "All codex-auto routes"):
            self.invoke()
        self.assert_unchanged()

    def test_fallbacks_refused(self):
        for settings in ({"default_fallbacks": ["codex1/luna"]},
                         {"fallbacks": [{"codex-auto/luna": ["codex1/luna"]}]},
                         {"fallbacks": [{"*": ["codex1/luna"]}]}):
            self.gateway["router_settings"] = settings
            self.gateway_path.write_text(json.dumps(self.gateway))
            with self.subTest(settings=settings), self.assertRaises(SWITCH.Refused):
                self.invoke()
        self.assert_unchanged()

    def test_missing_readonly_mount_refused(self):
        with self.assertRaisesRegex(SWITCH.Refused, "Add ./state/codex1"):
            self.invoke(order=("1-2-3",))
        self.rendered["services"]["codex-router"]["volumes"][0]["read_only"] = False
        with self.assertRaisesRegex(SWITCH.Refused, "Add ./state/codex2"):
            self.invoke()
        self.assert_unchanged()

    def test_missing_state_directory_refused(self):
        (self.root / "state/codex2").rmdir()
        with self.assertRaisesRegex(SWITCH.Refused, "state/codex2 is missing"):
            self.invoke()
        self.assert_unchanged()

    def test_image_drift_refused(self):
        self.image = "sha256:different"
        with self.assertRaisesRegex(SWITCH.Refused, "configured image differs"):
            self.invoke()
        self.assert_unchanged()

    def test_deployment_drift_refused(self):
        self.hash = "different-hash"
        with self.assertRaisesRegex(SWITCH.Refused, "Resolve the deployment drift"):
            self.invoke()
        self.assert_unchanged()

    def test_later_override_refused(self):
        self.later_order = "codex3,codex2,codex1"
        with self.assertRaisesRegex(SWITCH.Refused, "later override"):
            self.invoke()
        self.assert_unchanged()

    def test_unrelated_render_change_refused(self):
        self.extra_change = True
        with self.assertRaisesRegex(SWITCH.Refused, "more than the router order"):
            self.invoke()
        self.assert_unchanged()

    def test_concurrent_file_change_kept(self):
        def answer(_):
            self.target.write_text(HOST + "# another writer\n")
            return "y"
        with patch("builtins.input", side_effect=answer), self.assertRaisesRegex(SWITCH.Refused, "changed during confirmation"):
            self.invoke(yes=False)
        self.assertTrue(self.target.read_text().endswith("# another writer\n"))
        self.assertFalse(any("up" in args for args in self.calls))

    def test_concurrent_container_or_image_change_refused(self):
        for field in ("container", "image"):
            with self.subTest(field=field):
                def answer(_):
                    if field == "container":
                        self.live["Id"] = "changed-by-another-deploy"
                    else:
                        self.image = "sha256:changed-during-confirmation"
                    return "y"
                with patch("builtins.input", side_effect=answer), self.assertRaisesRegex(SWITCH.Refused, "changed during confirmation"):
                    self.invoke(yes=False)
                self.assert_unchanged()

    def test_failure_keeps_requested_file_and_backup_without_second_restart(self):
        self.fail_up = True
        with self.assertRaisesRegex(SWITCH.Refused, "simulated recreate failure"):
            self.invoke()
        self.assertIn("codex2,codex3,codex1", self.target.read_text())
        self.assertEqual(len(list(self.target.parent.glob("compose.host.before-codex-order-*.yaml"))), 1)
        self.assertEqual(len([args for args in self.calls if "up" in args]), 1)

    def test_unhealthy_not_reported_as_success(self):
        self.unhealthy = True
        with self.assertRaisesRegex(SWITCH.Refused, "health checks"):
            self.invoke()

    def test_inactive_override_refused(self):
        (self.root / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.codex.yaml\n")
        with self.assertRaisesRegex(SWITCH.Refused, "must include"):
            self.invoke()
        self.assert_unchanged()

    def test_lock_blocks_second_switch(self):
        import fcntl
        with (self.root / ".local/.codex-order.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(SWITCH, "REPO", self.root), patch.object(SWITCH, "run") as run, \
                    patch.object(sys, "argv", ["switch-codex-order.py", "2-3-1", "--yes"]), \
                    patch("sys.stderr", new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as error:
                    SWITCH.main()
                self.assertEqual(error.exception.code, 1)
                self.assertIn("Another account switch", output.getvalue())
                run.assert_not_called()

    def test_real_compose_merge_with_synthetic_host_files(self):
        if not shutil.which("docker"):
            self.skipTest("Docker Compose is not installed")
        base = {"name": "litellm", "services": {
            "gateway": {"image": "gateway:fixed"},
            "codex-router": {"image": "gateway:fixed", "environment": {"KEEP": "yes"},
                "volumes": ["./state/codex2:/tokens/codex2:ro", "./state/codex3:/tokens/codex3:ro"]}}}
        (self.root / "compose.yaml").write_text(json.dumps(base))
        candidate = self.root / ".local/candidate.yaml"
        candidate.write_text(SWITCH.replace_order(HOST, ["codex2", "codex3", "codex1"]))
        with patch.object(SWITCH, "command", wraps=REAL_COMMAND):
            before = SWITCH.config(self.root, [self.files[0], self.target])
            after = SWITCH.config(self.root, [self.files[0], candidate])
        before["services"]["codex-router"]["environment"]["CODEX_ACCOUNT_ORDER"] = "codex2,codex3,codex1"
        self.assertEqual(before, after)
        SWITCH.check_mounts(after["services"]["codex-router"], ["codex2", "codex3", "codex1"], self.root)


class CommandTests(unittest.TestCase):
    def test_command_failure_does_not_echo_credentials(self):
        result = subprocess.CompletedProcess([], 1, "SECRET_STDOUT", "SECRET_STDERR")
        with patch.object(SWITCH.subprocess, "run", return_value=result):
            with self.assertRaises(SWITCH.Refused) as error:
                SWITCH.command(["docker", "compose", "config"], Path("/tmp"))
        self.assertNotIn("SECRET", str(error.exception))


if __name__ == "__main__":
    unittest.main()
