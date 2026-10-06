"""Put image/routers on sys.path and stop the run when a hook test is collected
and the start-up hook is not active.

The hook tests check LiteLLM as the proxies run it: with
image/hooks/sitecustomize.py loaded at interpreter start. Other tests in this
directory need only the standard library and run without the hook.
See tests/README.md for the run command.
"""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "image" / "hooks"
# The router modules are mounted into their containers; the tests import them by name.
sys.path.insert(0, str(ROOT / "image" / "routers"))
# Test modules that need the hook. Add a module here when it imports litellm or a hook module.
# quota_router imports the session id derivation from image/hooks.
HOOK_TESTS = {
    "test_chatgpt_auth_file.py",
    "test_chatgpt_session_id.py",
    "test_codex_cost.py",
    "test_model_info.py",
    "test_quota_router.py",
    "test_responses_tool_finish.py",
    "test_sol_capabilities.py",
}


def _problem():
    if HOOKS not in {Path(entry).resolve() for entry in sys.path if entry}:
        return f"{HOOKS} is not on PYTHONPATH"
    hook = sys.modules.get("sitecustomize")
    if hook is None or Path(hook.__file__).resolve() != HOOKS / "sitecustomize.py":
        return "image/hooks/sitecustomize.py did not load at start-up (look for 'Error in sitecustomize')"
    from litellm.completion_extras.litellm_responses_transformation.transformation import (
        OpenAiResponsesToChatCompletionStreamIterator as Iterator,
    )
    if not getattr(Iterator, "_gateway_tool_finish", False):
        return "responses_tool_finish.install() did not patch the stream iterator"
    return None


def pytest_collectstart(collector):
    # Runs before the module is imported, so an import error cannot hide the cause.
    if isinstance(collector, pytest.Module) and collector.path.name in HOOK_TESTS:
        problem = _problem()
        if problem:
            pytest.exit(f"{collector.path.name}: {problem}. Run the tests as tests/README.md states.",
                        returncode=4)
