"""Keep real-scanner promotion fixtures independent of the source release refs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("git"), "Git is required for the host promotion fixture")
class PromotionFixtureTests(unittest.TestCase):
    def test_tagged_portable_source_does_not_enter_fixture(self):
        with tempfile.TemporaryDirectory(prefix="promotion-fixture-source-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            shutil.copytree(ROOT / "scripts", source / "scripts")
            (source / "tests").mkdir()
            shutil.copy2(ROOT / "tests/test_promotion_scan.sh", source / "tests/test_promotion_scan.sh")
            shutil.copy2(ROOT / ".gitleaks.toml", source / ".gitleaks.toml")
            environment = os.environ.copy()
            for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                        "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
                environment.pop(key, None)
            environment.update(HOME=str(root), GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
                               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")

            def git(*args):
                return subprocess.run(["git", *args], cwd=source, env=environment, check=True,
                                      capture_output=True, text=True, timeout=30).stdout

            git("init", "-q")
            git("symbolic-ref", "HEAD", "refs/heads/portable")
            git("add", ".")
            git("commit", "-qm", "Release fixture")
            git("tag", "-a", "portable-v9.9.9", "-m", "Release fixture")
            git("update-ref", "refs/remotes/origin/portable", "HEAD")
            refs = git("show-ref")
            result = subprocess.run(["sh", "tests/test_promotion_scan.sh"], cwd=source,
                                    env=environment, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("all 17 promotion scanner checks passed", result.stdout)
            self.assertEqual(git("show-ref"), refs)
            self.assertEqual(git("status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
