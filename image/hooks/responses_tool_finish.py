"""Responses-to-Chat bridge fixes, tested with LiteLLM 1.101.0 and 1.103.0.

The upstream bridge drops completion-only function arguments, observed with
parallel Codex tool calls. It also loses tool evidence when terminal output is
empty. Keep both forms of evidence per iterator. Never end early or change
incomplete, failed or cancelled events. Remove when the pinned upstream bridge
passes the regression tests without this hook.
"""
from litellm_versions import TESTED_VERSIONS, untested  # noqa: F401  (TESTED_VERSIONS for the tests)


def install():
    problem = untested()
    if problem:
        raise SystemExit(problem)
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        OpenAiResponsesToChatCompletionStreamIterator as Iterator,
    )
    from litellm.responses.litellm_completion_transformation.transformation import (
        LiteLLMCompletionResponsesConfig,
    )
    from litellm.types.utils import (
        ChatCompletionToolCallChunk, Delta, ModelResponseStream, StreamingChoices,
    )

    if getattr(Iterator, "_gateway_tool_finish", False):
        return
    original = Iterator.chunk_parser

    def chunk_parser(self, chunk):
        result = original(self, chunk)
        event = chunk.get("type")
        choices = result.choices if result is not None else []
        calls = [call for choice in choices if choice.delta
                 for call in choice.delta.tool_calls or []]
        if calls:
            self._gateway_saw_tool = True

        if event in ("response.output_item.added", "response.output_item.done",
                     "response.function_call_arguments.delta", "response.function_call_arguments.done"):
            item = chunk.get("item") or {}
            is_arguments = event.startswith("response.function_call_arguments.")
            if is_arguments or item.get("type") in ("function_call", "custom_tool_call"):
                if not hasattr(self, "_gateway_tool_calls"):
                    self._gateway_tool_calls = {}
                states = self._gateway_tool_calls
                output_index = chunk.get("output_index")
                item_id = item.get("id") or chunk.get("item_id")
                call_id = item.get("call_id") or chunk.get("call_id")
                keys = [(kind, value) for kind, value in (
                    ("output_index", output_index), ("item_id", item_id), ("call_id", call_id),
                ) if value is not None]
                if keys:
                    state = next((states[key] for key in keys if key in states), None)
                    if state is None:
                        state = {"item": {}, "head_sent": False, "has_arguments": False, "emitted": False}
                    for key in keys:
                        states[key] = state
                    metadata = state["item"]
                    metadata.update(item)
                    for field, value in (("id", item_id), ("call_id", call_id), ("name", chunk.get("name"))):
                        if value is not None:
                            metadata[field] = value
                    if output_index is not None:
                        state["output_index"] = output_index
                    # Use the parser's chat index, not the Responses output index.
                    for call in calls:
                        state["index"] = call.index
                        if call.id or (call.function and call.function.name):
                            state["head_sent"] = True
                        if call.function and call.function.arguments:
                            state["has_arguments"] = True
                    arguments = chunk.get("arguments") if is_arguments else item.get("arguments")
                    completed = event in ("response.function_call_arguments.done", "response.output_item.done")
                    status = item.get("status") or (chunk.get("response") or {}).get("status")
                    if (completed and isinstance(arguments, str)
                            and metadata.get("type", "function_call") == "function_call"
                            and status not in ("incomplete", "failed", "cancelled")
                            and not state["has_arguments"] and not state["emitted"]):
                        # The first completion wins, even when there is nothing to recover.
                        state["emitted"] = True
                        if arguments == "" and state["head_sent"]:
                            return result
                        if "index" not in state:
                            state["index"] = self._sequential_tool_call_index(
                                self._tool_call_index_map, state.get("output_index", keys[0]))
                        tool = ChatCompletionToolCallChunk(
                            id=None if state["head_sent"] else
                            LiteLLMCompletionResponsesConfig._tool_call_id_from_responses_item(
                                metadata.get("id"), metadata.get("call_id")),
                            index=state["index"], type="function",
                            function={"name": None if state["head_sent"] else metadata.get("name") or None,
                                      "arguments": arguments},
                        )
                        if result is None:
                            result = self._with_stream_scoped_id(ModelResponseStream(choices=[]))
                        if not result.choices:
                            result.choices.append(StreamingChoices(index=0, delta=Delta(), finish_reason=None))
                        choice = result.choices[0]
                        if choice.delta is None:
                            choice.delta = Delta()
                        choice.delta.tool_calls = Delta(tool_calls=[*(choice.delta.tool_calls or []), tool]).tool_calls
                        self._gateway_saw_tool = True

        if event == "response.completed" and getattr(self, "_gateway_saw_tool", False) and result is not None:
            for choice in result.choices:
                if choice.finish_reason == "stop":
                    choice.finish_reason = "tool_calls"
        return result

    Iterator.chunk_parser = chunk_parser
    Iterator._gateway_tool_finish = True
