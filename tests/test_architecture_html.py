"""Check the architecture page builder, the committed page and the host-value rules on the page.

The image test target does not copy docs/. There the tests of the page skip and the
self-test of the builder still runs. No test writes under docs/ or reads a host secret.
"""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-architecture-html.py"
SOURCE = ROOT / "docs" / "diagrams" / "architecture-graph.json"
PAGE = ROOT / "docs" / "architecture.html"


def build(*args):
    return subprocess.run([sys.executable, str(BUILDER), *args], cwd=ROOT, capture_output=True, text=True)


def deny_patterns():
    """Literal patterns of the tracked deny list and, when it exists, of the local deny list."""
    found = []
    for path in (ROOT / "scripts" / "host-values.deny", ROOT / ".local" / "host-values.deny"):
        if path.is_file():
            lines = [line.strip() for line in path.read_text().splitlines()]
            found += [(f"{path.parent.name}/{path.name}:{n}", line.casefold())
                      for n, line in enumerate(lines, 1) if line and not line.startswith("#")]
    return found


def regex_rules():
    """Rules of scripts/host-values.regex as (name, sample, compiled expression)."""
    rules = []
    for line in (ROOT / "scripts" / "host-values.regex").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, sample, expression = line.split(None, 2)
        rules.append((name, sample.replace("_", ".").replace("-", ":"), re.compile(expression)))
    return rules


class BuilderTest(unittest.TestCase):
    def test_checkout_below_local_keeps_public_links_in_checkout(self):
        spec = importlib.util.spec_from_file_location('architecture_builder', BUILDER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / '.local' / 'worktrees' / 'gateway'
            module.ROOT = checkout
            self.assertEqual(module.link_root(checkout / 'docs' / 'architecture.html'), checkout)
            self.assertEqual(module.link_root(checkout / '.local' / 'diagrams' / 'private.html'), checkout)

    def test_selftest_passes(self):
        result = build("--selftest")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("selftest: ok", result.stdout)


@unittest.skipUnless(SOURCE.is_file() and PAGE.is_file(), "docs/ is not in this tree")
class PageTest(unittest.TestCase):
    def test_check_passes_on_the_committed_page(self):
        result = build("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory(prefix="arch-test-") as tmp:
            first, second = Path(tmp, "first.html"), Path(tmp, "second.html")
            for out in (first, second):
                result = build("--out", str(out))
                self.assertEqual(result.returncode, 0, result.stderr)
            # The source links are relative to the output path, so compare two builds at one depth.
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_overlay_build_refuses_docs(self):
        before = PAGE.read_bytes()
        other = ROOT / "docs" / "architecture-overlay-test.html"
        with tempfile.TemporaryDirectory(prefix="arch-test-") as tmp:
            overlay = Path(tmp, "overlay.json")
            overlay.write_text(json.dumps({"edition": {"name": "private edition, this host", "banner": "private edition, this host"}}))
            for out in (PAGE, other):
                result = build("--overlay", str(overlay), "--out", str(out))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("refused", result.stderr)
            self.assertFalse(other.exists())
            private = Path(tmp, ".local", "diagrams", "page.html")
            result = build("--overlay", str(overlay), "--out", str(private))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("private edition, this host", private.read_text())
        self.assertEqual(PAGE.read_bytes(), before)

    def test_page_and_source_match_no_host_value_rule(self):
        texts = {path.name: path.read_text() for path in (PAGE, SOURCE)}
        for rule, pattern in deny_patterns():
            for name, text in texts.items():
                self.assertNotIn(pattern, text.casefold(), f"{name} matches deny rule {rule}")
        rules = regex_rules()
        self.assertTrue(rules)
        for rule, sample, expression in rules:
            # A rule that misses its own sample would make this test pass for nothing.
            self.assertTrue(expression.search(f" {sample} "), f"rule {rule} misses its sample")
            for name, text in texts.items():
                self.assertIsNone(expression.search(text), f"{name} matches rule host-value-{rule}")

    def test_each_source_is_a_tracked_file(self):
        try:
            tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("no Git checkout")
        graph = json.loads(SOURCE.read_text())
        cited = {s for item in graph["nodes"] + graph["views"] for s in item.get("sources", [])}
        for src in sorted(cited):
            path = src.split("#", 1)[0].rstrip("/")
            self.assertTrue(path in tracked or any(t.startswith(path + "/") for t in tracked), f"{src} is not tracked")


if __name__ == "__main__":
    unittest.main()
