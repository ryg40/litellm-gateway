import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from litellm.completion_extras.litellm_responses_transformation import transformation
from litellm.completion_extras.litellm_responses_transformation.transformation import (
    OpenAiResponsesToChatCompletionStreamIterator as Iterator,
)
from responses_tool_finish import TESTED_VERSIONS, install


def iterator():
    return Iterator(iter([]), True)


def tool_event(index=0, kind="function_call"):
    return {"type": "response.output_item.added", "output_index": index,
            "item": {"type": kind, "id": f"fc_{index}", "call_id": f"call_{index}",
                     "name": "probe", "arguments": "{}", "input": "probe"}}


def terminal(kind="completed", output=None):
    return {"type": f"response.{kind}", "response": {
        "status": kind, "output": output or [],
        "incomplete_details": {"reason": "max_output_tokens"} if kind == "incomplete" else None,
    }}


class FinishTests(unittest.TestCase):
    def test_tool_with_empty_terminal_output(self):
        stream = iterator()
        first = stream.chunk_parser(tool_event())
        self.assertIsNone(first.choices[0].finish_reason)
        last = stream.chunk_parser(terminal())
        self.assertEqual(last.choices[0].finish_reason, "tool_calls")

    def test_text_is_stop(self):
        stream = iterator()
        stream.chunk_parser({"type": "response.output_text.delta", "delta": "OK"})
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "stop")

    def test_parallel_calls_do_not_finish_early(self):
        stream = iterator()
        for index in [1, 3]:
            chunk = stream.chunk_parser(tool_event(index))
            self.assertIsNone(chunk.choices[0].finish_reason)
            done = {**tool_event(index), "type": "response.output_item.done"}
            self.assertIsNone(stream.chunk_parser(done).choices[0].finish_reason)
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_incomplete_not_upgraded(self):
        stream = iterator()
        stream.chunk_parser(tool_event())
        self.assertEqual(stream.chunk_parser(terminal("incomplete")).choices[0].finish_reason, "length")

    def test_stream_state_is_isolated(self):
        one, two = iterator(), iterator()
        one.chunk_parser(tool_event())
        self.assertEqual(two.chunk_parser(terminal()).choices[0].finish_reason, "stop")
        self.assertEqual(one.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_terminal_output_and_custom_calls(self):
        for kind in ["function_call", "custom_tool_call"]:
            stream = iterator()
            stream.chunk_parser(tool_event(kind=kind))
            self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")
        self.assertEqual(iterator().chunk_parser(terminal(output=[tool_event()["item"]])).choices[0].finish_reason, "tool_calls")

    def test_patch_is_idempotent(self):
        # A fresh class shows that install() patches once and only once.
        def original(self, chunk):
            return SimpleNamespace(choices=[])

        fresh = type("FreshIterator", (), {"chunk_parser": original})
        with patch.object(transformation, "OpenAiResponsesToChatCompletionStreamIterator", fresh):
            install()
            patched = fresh.chunk_parser
            self.assertIsNot(patched, original)
            self.assertTrue(fresh._gateway_tool_finish)
            install()
            self.assertIs(fresh.chunk_parser, patched)
        # The start-up hook already patched the real class; a second call keeps it.
        self.assertTrue(Iterator._gateway_tool_finish)
        before = Iterator.chunk_parser
        install()
        self.assertIs(Iterator.chunk_parser, before)

    def test_version_check(self):
        def original(self, chunk):
            return SimpleNamespace(choices=[])

        for value, allowed in [*((v, True) for v in TESTED_VERSIONS), ("1.102.0", False), ("1.104.0", False)]:
            with self.subTest(version=value):
                fresh = type("FreshIterator", (), {"chunk_parser": original})
                with patch.object(transformation, "OpenAiResponsesToChatCompletionStreamIterator", fresh), \
                        patch("litellm_versions.version", return_value=value):
                    if allowed:
                        install()
                        self.assertIsNot(fresh.chunk_parser, original)
                    else:
                        with self.assertRaisesRegex(SystemExit, f"^LiteLLM {value} is not tested .*"
                                                                f"\\(tested: {', '.join(TESTED_VERSIONS)}\\)"):
                            install()
                        self.assertIs(fresh.chunk_parser, original)

    def test_untested_version_stops_the_interpreter(self):
        # site.py only prints an Exception from sitecustomize; SystemExit stops the start.
        hooks = Path(__file__).resolve().parents[1] / "image" / "hooks"
        with tempfile.TemporaryDirectory(prefix="litellm-test-codex-accounts-") as tmp:
            Path(tmp, "sitecustomize.py").write_text(
                "import importlib.metadata\n"
                "importlib.metadata.version = lambda name: '1.102.0'\n"
                "from responses_tool_finish import install\n"
                "install()\n")
            env = {**os.environ, "PYTHONPATH": os.pathsep.join([tmp, str(hooks)])}
            result = subprocess.run([sys.executable, "-c", "print('process continued')"], env=env,
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("process continued", result.stdout)
        self.assertIn("LiteLLM 1.102.0 is not tested", result.stderr)

    def test_shipped_sitecustomize_stops_on_untested_version(self):
        # The shipped image/hooks/sitecustomize.py, not a stub. A fake dist-info
        # before site-packages makes importlib.metadata report LiteLLM 9.9.9.
        hooks = Path(__file__).resolve().parents[1] / "image" / "hooks"
        with tempfile.TemporaryDirectory(prefix="litellm-test-hooks-") as tmp:
            info = Path(tmp, "litellm-9.9.9.dist-info")
            info.mkdir()
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: litellm\nVersion: 9.9.9\n")
            for switch in ("off", "on"):
                with self.subTest(switch=switch):
                    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(hooks), tmp]),
                           "CHATGPT_AUTH_FILE_HOOK": switch}
                    code = "import sys; print('process continued', 'litellm' in sys.modules)"
                    result = subprocess.run([sys.executable, "-c", code], env=env,
                                            capture_output=True, text=True, timeout=120)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertNotIn("process continued", result.stdout)
                    self.assertEqual(result.stderr.strip(), "sitecustomize: LiteLLM 9.9.9 is not tested with the "
                                     f"hooks of this image (tested: {', '.join(TESTED_VERSIONS)}). Add the version "
                                     "to TESTED_VERSIONS in image/hooks/litellm_versions.py, run scripts/build.sh "
                                     "test with this BASE_IMAGE, and keep the version only when the tests pass.")
            # The same interpreter without the fake version starts and loads the hooks.
            env = {**os.environ, "PYTHONPATH": str(hooks)}
            result = subprocess.run([sys.executable, "-c", "import sys; print('litellm' in sys.modules)"], env=env,
                                    capture_output=True, text=True, timeout=120)
            # LiteLLM prints other lines at import; the last line is the result.
            self.assertEqual((result.returncode, result.stdout.strip().splitlines()[-1]), (0, "True"), result.stderr)

    def test_failed_response_not_upgraded(self):
        stream = iterator()
        stream.chunk_parser(tool_event())
        result = stream.chunk_parser(terminal("failed"))
        self.assertIsNone(result.choices[0].finish_reason)


if __name__ == "__main__":
    unittest.main()
