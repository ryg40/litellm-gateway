"""LiteLLM 1.101.0 and 1.103.0 bridge fix for terminal events without repeated output.

Keep tool-call evidence per iterator. Never end early or change incomplete/error
outcomes. Remove after the pinned upstream bridge passes the regression tests.
"""
from litellm_versions import TESTED_VERSIONS, untested  # noqa: F401  (TESTED_VERSIONS for the tests)


def install():
    problem = untested()
    if problem:
        raise SystemExit(problem)
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        OpenAiResponsesToChatCompletionStreamIterator as Iterator,
    )
    if getattr(Iterator, "_gateway_tool_finish", False):
        return
    original = Iterator.chunk_parser

    def chunk_parser(self, chunk):
        result = original(self, chunk)
        if any(choice.delta and choice.delta.tool_calls for choice in result.choices):
            self._gateway_saw_tool = True
        if chunk.get("type") == "response.completed" and getattr(self, "_gateway_saw_tool", False):
            for choice in result.choices:
                if choice.finish_reason == "stop":
                    choice.finish_reason = "tool_calls"
        return result

    Iterator.chunk_parser = chunk_parser
    Iterator._gateway_tool_finish = True
