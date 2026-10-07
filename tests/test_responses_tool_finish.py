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


def added(index=0, name="probe"):
    event = tool_event(index)
    event["item"].update(call_id=f"call_probe_{index}", name=name, arguments="")
    return event


def argument_event(kind, arguments, index=0):
    return {"type": f"response.function_call_arguments.{kind}", "output_index": index,
            "item_id": f"fc_{index}", "delta" if kind == "delta" else "arguments": arguments}


def item_done(arguments, index=0, name="probe"):
    event = added(index, name)
    event.update(type="response.output_item.done")
    event["item"]["arguments"] = arguments
    return event


def tool_calls(chunk):
    return [call.model_dump(exclude_none=True) for choice in chunk.choices if choice.delta
            for call in choice.delta.tool_calls or []]


def expected_call(arguments, index=0, output_index=0, name="probe"):
    return {"id": f"call_probe_{output_index}", "index": index, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def expected_arguments(arguments, index=0):
    return {"index": index, "type": "function", "function": {"arguments": arguments}}


def terminal(kind="completed", output=None):
    return {"type": f"response.{kind}", "response": {
        "status": kind, "output": output or [],
        "incomplete_details": {"reason": "max_output_tokens"} if kind == "incomplete" else None,
    }}


class ArgumentTests(unittest.TestCase):
    def test_single_delta_call_unchanged(self):
        stream = iterator()
        self.assertEqual(tool_calls(stream.chunk_parser(added())), [expected_call("")])
        for text in ('{"query":', '"value"}'):
            chunk = stream.chunk_parser(argument_event("delta", text))
            self.assertEqual(tool_calls(chunk), [{"index": 0, "type": "function",
                                                 "function": {"arguments": text}}])
            self.assertIsNone(chunk.choices[0].finish_reason)
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_completion_only_emits_first_completion_once(self):
        for first in ("arguments", "item"):
            with self.subTest(first=first):
                stream = iterator()
                initial = stream.chunk_parser(added(3))
                events = [argument_event("done", '{"query":"value"}', 3),
                          item_done('{"query":"value"}', 3)]
                if first == "item":
                    events.reverse()
                result = stream.chunk_parser(events[0])
                self.assertEqual(tool_calls(result), [expected_arguments('{"query":"value"}')])
                self.assertEqual(result.id, initial.id)
                self.assertIsNone(result.choices[0].finish_reason)
                self.assertEqual(tool_calls(stream.chunk_parser(events[1])), [])
                self.assertEqual(tool_calls(stream.chunk_parser(events[0])), [])
                self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_mixed_parallel_calls_keep_chat_indices(self):
        stream = iterator()
        stream.chunk_parser(added(1, "first"))
        stream.chunk_parser(added(3, "second"))
        stream.chunk_parser(argument_event("delta", '{"a":1}', 1))
        self.assertEqual(tool_calls(stream.chunk_parser(argument_event("done", '{"a":1}', 1))), [])
        chunk = stream.chunk_parser(argument_event("done", '{"b":2}', 3))
        self.assertEqual(tool_calls(chunk), [expected_arguments('{"b":2}', 1)])
        self.assertIsNone(chunk.choices[0].finish_reason)
        for index, text, name in [(1, '{"a":1}', "first"), (3, '{"b":2}', "second")]:
            self.assertEqual(tool_calls(stream.chunk_parser(item_done(text, index, name))), [])
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_delta_and_both_completions_do_not_duplicate(self):
        stream = iterator()
        stream.chunk_parser(added())
        results = [stream.chunk_parser(argument_event("delta", part)) for part in ('{"a":', '1}')]
        results += [stream.chunk_parser(argument_event("done", '{"a":1}')),
                    stream.chunk_parser(item_done('{"a":1}'))]
        calls = [call for result in results for call in tool_calls(result)]
        self.assertEqual(len(calls), 2)
        self.assertEqual("".join(call["function"]["arguments"] for call in calls), '{"a":1}')

    def test_arguments_already_in_added_are_not_duplicated(self):
        stream = iterator()
        event = added()
        event["item"]["arguments"] = "{}"
        self.assertEqual(tool_calls(stream.chunk_parser(event)), [expected_call("{}")])
        self.assertEqual(tool_calls(stream.chunk_parser(item_done("{}"))), [])

    def test_added_and_recovery_accumulate_identity_once(self):
        for completion in (argument_event("done", '{"query":"value"}', 3),
                           item_done('{"query":"value"}', 3)):
            with self.subTest(event=completion["type"]):
                stream = iterator()
                accumulated = {}
                for event in (added(3), completion):
                    for call in tool_calls(stream.chunk_parser(event)):
                        target = accumulated.setdefault(call["index"], {"id": "", "name": "", "arguments": ""})
                        target["id"] += call.get("id", "")
                        for field in ("name", "arguments"):
                            target[field] += call["function"].get(field, "")
                self.assertEqual(accumulated, {0: {"id": "call_probe_3", "name": "probe",
                                                   "arguments": '{"query":"value"}'}})

    def test_empty_completion_arguments_emit_only_without_head(self):
        for head_sent in (False, True):
            for first in ("arguments", "item"):
                with self.subTest(head_sent=head_sent, first=first):
                    stream = iterator()
                    if head_sent:
                        stream.chunk_parser(added())
                    completion = argument_event("done", "")
                    completion.update(call_id="call_probe_0", name="probe")
                    events = [completion, item_done("")]
                    if first == "item":
                        events.reverse()
                    expected = [] if head_sent else [expected_call("")]
                    self.assertEqual(tool_calls(stream.chunk_parser(events[0])), expected)
                    self.assertEqual(tool_calls(stream.chunk_parser(events[1])), [])
                    self.assertEqual(tool_calls(stream.chunk_parser(argument_event("done", "{}"))), [])
                    self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_call_identity_fallbacks(self):
        for identity in ("item_id", "call_id"):
            with self.subTest(identity=identity):
                stream = iterator()
                stream.chunk_parser(added(3))
                event = argument_event("done", "{}", 3)
                del event["output_index"]
                if identity == "call_id":
                    del event["item_id"]
                    event["call_id"] = "call_probe_3"
                self.assertEqual(tool_calls(stream.chunk_parser(event)), [expected_arguments("{}")])
                self.assertEqual(tool_calls(stream.chunk_parser(item_done("{}", 3))), [])

    def test_completion_without_added_is_tool_evidence(self):
        for identity in ("output_index", "item_id", "call_id"):
            stream = iterator()
            event = argument_event("done", "{}", 3)
            event.update(call_id="call_probe_3", name="probe")
            if identity != "output_index":
                del event["output_index"]
            if identity == "call_id":
                del event["item_id"]
            self.assertEqual(tool_calls(stream.chunk_parser(event)), [expected_call("{}", output_index=3)])
            self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_item_done_without_added_uses_upstream_id_rule(self):
        for call_id, expected_id in (("call_probe_3", "call_probe_3"), ("call_3", "fc_3"), (None, "fc_3")):
            with self.subTest(call_id=call_id):
                stream = iterator()
                event = item_done("{}", 3)
                event["item"]["call_id"] = call_id
                expected = expected_call("{}", output_index=3)
                expected["id"] = expected_id
                self.assertEqual(tool_calls(stream.chunk_parser(event)), [expected])
                self.assertEqual(tool_calls(stream.chunk_parser(argument_event("done", "{}", 3))), [])

    def test_message_done_does_not_invent_tool(self):
        stream = iterator()
        chunk = stream.chunk_parser({"type": "response.output_item.done", "output_index": 0,
                                     "item": {"type": "message", "arguments": "{}"}})
        self.assertEqual(tool_calls(chunk), [])
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "stop")

    def test_completion_state_is_isolated(self):
        one, two = iterator(), iterator()
        for stream in (one, two):
            stream.chunk_parser(added())
        one.chunk_parser(argument_event("delta", "{}"))
        self.assertEqual(tool_calls(two.chunk_parser(argument_event("done", "{}"))), [expected_arguments("{}")])
        self.assertEqual(tool_calls(one.chunk_parser(argument_event("done", "{}"))), [])

    def test_non_success_terminal_events_are_unchanged(self):
        for kind, finish in (("incomplete", "length"), ("failed", None), ("cancelled", None)):
            for completion in (False, True):
                with self.subTest(kind=kind, completion=completion):
                    stream = iterator()
                    stream.chunk_parser(added())
                    if completion:
                        stream.chunk_parser(argument_event("done", "{}"))
                    chunk = stream.chunk_parser(terminal(kind, [item_done("{}")["item"]]))
                    self.assertEqual(tool_calls(chunk), [])
                    self.assertEqual(chunk.choices[0].finish_reason, finish)

    def test_non_success_item_is_not_recovered(self):
        for status in ("incomplete", "failed", "cancelled"):
            stream = iterator()
            stream.chunk_parser(added())
            event = item_done("{}")
            event["item"]["status"] = status
            self.assertEqual(tool_calls(stream.chunk_parser(event)), [])

    def test_custom_tool_is_unchanged(self):
        stream = iterator()
        event = added()
        event["item"].update(type="custom_tool_call", input="")
        stream.chunk_parser(event)
        chunk = stream.chunk_parser({"type": "response.custom_tool_call_input.delta",
                                     "output_index": 0, "delta": "raw input"})
        self.assertEqual(tool_calls(chunk), [{"index": 0, "type": "function",
                                             "function": {"arguments": "raw input"}}])
        event["type"] = "response.output_item.done"
        event["item"].update(input="raw input", arguments="not function arguments")
        self.assertEqual(tool_calls(stream.chunk_parser(event)), [])
        self.assertEqual(stream.chunk_parser(terminal()).choices[0].finish_reason, "tool_calls")

    def test_completion_fills_empty_parser_results(self):
        from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

        for empty in ("none", "choices", "delta", "existing"):
            with self.subTest(empty=empty):
                def original(self, chunk):
                    if empty == "none":
                        return None
                    result = ModelResponseStream(id="chatcmpl-probe", choices=[])
                    if empty in ("delta", "existing"):
                        result.choices.append(StreamingChoices(index=0, delta=Delta(content="keep")))
                        if empty == "delta":
                            result.choices[0].delta = None
                    return result

                fresh = type("FreshIterator", (Iterator,), {"chunk_parser": original, "_gateway_tool_finish": False})
                with patch.object(transformation, "OpenAiResponsesToChatCompletionStreamIterator", fresh):
                    install()
                    stream = fresh(iter([]), True)
                    result = stream.chunk_parser(item_done("{}", 3))
                self.assertIsInstance(result, ModelResponseStream)
                self.assertEqual(tool_calls(result), [expected_call("{}", output_index=3)])
                self.assertIsNone(result.choices[0].finish_reason)
                if empty != "none":
                    self.assertEqual(result.id, "chatcmpl-probe")
                if empty == "existing":
                    self.assertEqual(result.choices[0].delta.content, "keep")


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
