"""scripts/create-env.py and the placeholder stop of scripts/verify.py. Standard library only."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
GENERATED = ("LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "CODEX_MASTER_KEY", "COPILOT_MASTER_KEY", "POSTGRES_PASSWORD", "UI_PASSWORD")
# The layout of .env.example: empty secrets, fixed values, comments with placeholders.
EXAMPLE = """# comment
LITELLM_MASTER_KEY=
CODEX_MASTER_KEY=
COPILOT_MASTER_KEY=
UI_PASSWORD=
POSTGRES_PASSWORD=
LITELLM_SALT_KEY=
UI_USERNAME=admin
OPENROUTER_API_KEY=
LOCAL_API_KEY=local-no-key
# ANTHROPIC_API_KEY=REPLACE_WITH_SEPARATE_KEY_OR_TOKEN
"""


def parse(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith("#"))


class CreateEnvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="litellm-test-hooks-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def run_script(self, directory, example=EXAMPLE):
        (directory / ".env.example").write_text(example)
        return subprocess.run([sys.executable, str(SCRIPTS / "create-env.py"), str(directory)],
                              capture_output=True, text=True, timeout=30)

    def test_new_values_and_mode(self):
        first, second = self.dir / "a", self.dir / "b"
        first.mkdir()
        second.mkdir()
        old_umask = os.umask(0o022)
        try:
            results = [self.run_script(first), self.run_script(second)]
        finally:
            os.umask(old_umask)
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        a, b = (parse((d / ".env").read_text()) for d in (first, second))
        for directory, env in ((first, a), (second, b)):
            self.assertEqual(stat.S_IMODE((directory / ".env").stat().st_mode), 0o600)
            for key in GENERATED:
                self.assertGreaterEqual(len(env[key]), 24, key)
            self.assertEqual(len({env[key] for key in GENERATED}), len(GENERATED))
            self.assertTrue(env["LITELLM_MASTER_KEY"].startswith("sk-"))
            self.assertRegex(env["POSTGRES_PASSWORD"], r"^[0-9a-f]{48}$")
            self.assertEqual((env["UI_USERNAME"], env["OPENROUTER_API_KEY"]), ("admin", ""))
        # Each run makes new values.
        for key in GENERATED:
            self.assertNotEqual(a[key], b[key], key)
        # No value in the output of the script.
        for result, env in zip(results, (a, b)):
            for key in GENERATED:
                self.assertNotIn(env[key], result.stdout + result.stderr)
        self.assertEqual((first / ".env").read_text().splitlines()[-1],
                         "# ANTHROPIC_API_KEY=REPLACE_WITH_SEPARATE_KEY_OR_TOKEN")

    def test_refuses_an_existing_env(self):
        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                directory = self.dir / kind
                directory.mkdir()
                if kind == "file":
                    (directory / ".env").write_text("LITELLM_MASTER_KEY=keep\n")
                else:
                    os.symlink(directory / "missing", directory / ".env")
                before = os.readlink(directory / ".env") if kind == "symlink" else (directory / ".env").read_text()
                result = self.run_script(directory)
                self.assertEqual(result.returncode, 1)
                self.assertIn("exists; the script does not replace it", result.stderr)
                after = os.readlink(directory / ".env") if kind == "symlink" else (directory / ".env").read_text()
                self.assertEqual(before, after)
                self.assertFalse((directory / "missing").exists())

    def test_old_placeholders_are_replaced_and_others_named(self):
        old = EXAMPLE.replace("LITELLM_MASTER_KEY=", "LITELLM_MASTER_KEY=sk-REPLACE_WITH_RANDOM_SECRET") \
                     .replace("OPENROUTER_API_KEY=", "OPENROUTER_API_KEY=REPLACE_WITH_OPENROUTER_KEY")
        result = self.run_script(self.dir, old)
        self.assertEqual(result.returncode, 0, result.stderr)
        env = parse((self.dir / ".env").read_text())
        self.assertNotIn("REPLACE_WITH_", env["LITELLM_MASTER_KEY"])
        self.assertIn("replace the placeholder of OPENROUTER_API_KEY", result.stdout)

    def test_example_without_a_secret_stops(self):
        result = self.run_script(self.dir, EXAMPLE.replace("UI_PASSWORD=\n", ""))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no empty value for UI_PASSWORD", result.stderr)
        self.assertFalse((self.dir / ".env").exists())


class VerifyPlaceholderTests(unittest.TestCase):
    """scripts/verify.py stops before a request when .env holds REPLACE_WITH_ or no master key."""

    def run_verify(self, env_text):
        with tempfile.TemporaryDirectory(prefix="litellm-test-hooks-") as tmp:
            (Path(tmp) / "scripts").mkdir()
            for name in ("verify.py", "gateway_config.py"):
                shutil.copy(SCRIPTS / name, Path(tmp) / "scripts" / name)
            (Path(tmp) / ".env").write_text(env_text)
            # Port 9 (discard) on 127.0.0.1: a request would fail with another message.
            return subprocess.run([sys.executable, str(Path(tmp) / "scripts" / "verify.py"),
                                   "--base", "http://127.0.0.1:9"], capture_output=True, text=True, timeout=30)

    def test_placeholder_stops(self):
        result = self.run_verify("LITELLM_MASTER_KEY=sk-REPLACE_WITH_RANDOM_SECRET\nUI_PASSWORD=x\n"
                                 "# ANTHROPIC_API_KEY=REPLACE_WITH_X\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("REPLACE_WITH_ in LITELLM_MASTER_KEY.", result.stderr)
        self.assertNotIn("sk-REPLACE", result.stderr)
        self.assertNotIn("ANTHROPIC", result.stderr)

    def test_empty_master_key_stops(self):
        result = self.run_verify("LITELLM_MASTER_KEY=\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("LITELLM_MASTER_KEY in .env is empty", result.stderr)


if __name__ == "__main__":
    unittest.main()
