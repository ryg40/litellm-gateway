"""Keep the dedicated embedding route during local model refreshes."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/sync-local-models.py"
SPEC = importlib.util.spec_from_file_location("sync_local_models", SCRIPT)
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)

FAST = "http://fast.test:9292/v1"
SLOW = "http://slow.test:8001/v1"
EMBEDDING_BASE = "http://embed.test:9293/v1"
EMBEDDING_MODEL = "Embedding-Test"
HOST_VARIABLES = ("GATEWAY_CONFIG", "LOCAL_ENDPOINTS", "LOCAL_EMBEDDING_MODEL",
                  "LOCAL_EMBEDDING_BASE", "LOCAL_EMBEDDING_PROVIDER")
ARGS = ["--endpoint", f"llamaswap={FAST}", "--endpoint", f"vllm={SLOW}",
        "--embedding-model", EMBEDDING_MODEL, "--embedding-base", EMBEDDING_BASE]


def clean_environment():
    return {key: value for key, value in os.environ.items() if key not in HOST_VARIABLES}


def run(root, catalogs, argv):
    def catalog(url, timeout):
        return io.BytesIO(json.dumps({"data": catalogs[url]}).encode())

    original_root = SYNC.ROOT
    try:
        SYNC.ROOT = root
        with patch.dict(os.environ, clean_environment(), clear=True), \
                patch.object(SYNC.urllib.request, "urlopen", side_effect=catalog):
            with redirect_stdout(io.StringIO()):
                SYNC.main(argv)
    finally:
        SYNC.ROOT = original_root


class SyncLocalModelsTest(unittest.TestCase):
    def test_dedicated_embedding_route_with_or_without_fast_catalog_entry(self):
        for fast_has_embedding in (True, False):
            with self.subTest(fast_has_embedding=fast_has_embedding), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / ".local/config/gateway.yaml"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"model_list": [{"model_name": "codex-auto/sol"}]}))
                catalogs = {
                    FAST + "/models": [
                        {"id": "chat"},
                        *([{"id": EMBEDDING_MODEL}] if fast_has_embedding else []),
                    ],
                    EMBEDDING_BASE + "/models": [{"id": EMBEDDING_MODEL}],
                    SLOW + "/models": [{"id": "other"}],
                }
                run(root, catalogs, ARGS)

                entries = json.loads(path.read_text())["model_list"]
                self.assertEqual(len(entries), 4)
                self.assertEqual(entries[0]["model_name"], "codex-auto/sol")
                bases = {item["model_name"]: item["litellm_params"]["api_base"]
                         for item in entries[1:]}
                self.assertEqual(bases["llamaswap/chat"], FAST)
                self.assertEqual(bases["llamaswap/" + EMBEDDING_MODEL], EMBEDDING_BASE)
                self.assertEqual(bases["vllm/other"], SLOW)
                embedding = next(item for item in entries if item["model_name"] ==
                                 "llamaswap/" + EMBEDDING_MODEL)
                self.assertEqual(embedding["model_info"], {"mode": "embedding"})

    def test_unavailable_dedicated_model_does_not_replace_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".local/config/gateway.yaml"
            path.parent.mkdir(parents=True)
            original = json.dumps({"model_list": [{"model_name": "llamaswap/chat"}]})
            path.write_text(original)
            catalogs = {FAST + "/models": [{"id": "chat"}], EMBEDDING_BASE + "/models": []}
            with self.assertRaisesRegex(RuntimeError, "Dedicated embedding model is unavailable"):
                run(root, catalogs, ARGS)
            self.assertEqual(path.read_text(), original)

    def test_endpoints_from_env_file_and_new_host_file_from_example(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text(f"LOCAL_ENDPOINTS=local={FAST}\nGATEWAY_CONFIG=./host/gateway.yaml\n")
            run(root, {FAST + "/models": [{"id": "chat"}]}, [])
            entries = json.loads((root / "host/gateway.yaml").read_text())["model_list"]
            names = [entry["model_name"] for entry in entries]
            # The example routes stay; the local endpoint adds its models.
            self.assertIn("openrouter/*", names)
            self.assertIn("local/chat", names)
            self.assertEqual(sorted(path.name for path in root.rglob("*") if path.is_file()),
                             [".env", "gateway.yaml"])

    def test_no_endpoint_exits_non_zero_and_changes_no_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            for name in ("sync-local-models.py", "gateway_config.py"):
                (root / "scripts" / name).write_bytes((ROOT / "scripts" / name).read_bytes())
            (root / "config").mkdir()
            (root / "config/gateway.example.yaml").write_bytes((ROOT / "config/gateway.example.yaml").read_bytes())
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            result = subprocess.run([sys.executable, "-B", str(root / "scripts/sync-local-models.py")],
                                    cwd=root, env=clean_environment(), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("No local endpoint is set", result.stderr)
            after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_tracked_file_is_never_a_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("GATEWAY_CONFIG=./config/gateway.example.yaml\n")
            with self.assertRaisesRegex(SystemExit, "tracked file"):
                run(root, {}, ["--endpoint", f"local={FAST}"])
            self.assertEqual([path.name for path in root.iterdir()], [".env"])


if __name__ == "__main__":
    unittest.main()
