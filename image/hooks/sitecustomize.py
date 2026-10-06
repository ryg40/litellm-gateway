"""Declare Codex reasoning and tool capabilities before the proxies start.

LiteLLM checks bare gpt-6-luna and gpt-6-sol in the gateway, and their
responses-prefixed names in Codex account proxies. Deployment metadata only
registers provider-prefixed names, so checks fail before reaching Codex.
The openai provider on the responses-prefixed lookup keys is only for
capability detection; routing remains chatgpt. Do not drop parameters or
reduce reasoning. Remove the hook after upstream supports these names.
"""

import os
import sys

from litellm_versions import untested


def _stop(message):
    # site.py reports a SystemExit from sitecustomize as "Fatal Python error"
    # with a traceback. Print only the message, and stop with exit code 1.
    sys.stderr.write(f"sitecustomize: {message}\n")
    sys.stderr.flush()
    os._exit(1)


# Before `import litellm`: a stop costs no import time in a restart loop.
_problem = untested()
if _problem:
    _stop(_problem)

import litellm  # noqa: E402

from responses_tool_finish import install  # noqa: E402

install()

from chatgpt_session_id import install as install_chatgpt_session_id  # noqa: E402

install_chatgpt_session_id()

# Preserve upstream Astra metadata under the responses-prefixed lookup key.
# Account proxies validate this key before the chatgpt provider sends a request.
_astra = dict(litellm.model_cost["gpt-6-astra"])
_astra.update(litellm_provider="openai", mode="responses")
litellm.register_model(model_cost={"responses/gpt-6-astra": _astra})

# API-equivalent USD/token estimates, not subscription charges.
# Static estimates per million tokens. Verify current prices before cost reporting.
_luna_cost = {
    "input_cost_per_token": 0.1 / 1_000_000,
    "output_cost_per_token": 0.5 / 1_000_000,
    "cache_read_input_token_cost": 0.01 / 1_000_000,
    "cache_creation_input_token_cost": 0.125 / 1_000_000,
}
_sol_cost = {
    "input_cost_per_token": 2 / 1_000_000,
    "output_cost_per_token": 10 / 1_000_000,
    "cache_read_input_token_cost": 0.2 / 1_000_000,
    "cache_creation_input_token_cost": 2.5 / 1_000_000,
}

litellm.register_model(
    model_cost={
        "gpt-6-luna": {
            **_luna_cost,
            "litellm_provider": "openai",
            "mode": "chat",
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
        "responses/gpt-6-luna": {
            **_luna_cost,
            "litellm_provider": "openai",
            "mode": "responses",
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
        "gpt-6-sol": {
            **_sol_cost,
            "litellm_provider": "openai",
            "mode": "chat",
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
        "responses/gpt-6-sol": {
            **_sol_cost,
            "litellm_provider": "openai",
            "mode": "responses",
            "supports_reasoning": True,
            "supports_xhigh_reasoning_effort": True,
            "supports_function_calling": True,
            "supports_tool_choice": True,
        },
    }
)

from chatgpt_auth_file import install as install_chatgpt_auth_file  # noqa: E402

try:
    install_chatgpt_auth_file()
except SystemExit as stop:
    _stop(stop)
