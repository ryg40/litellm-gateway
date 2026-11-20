"""Offline checks of the models answer in scripts/login-copilot.py: no network, no token.

The script imports the github_copilot provider of LiteLLM, so these checks run in the
LiteLLM image (tests/README.md). On a host without LiteLLM they are skipped.
"""
import contextlib
import importlib.util
import io
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script():
    # The script sets the umask and the LiteLLM log level for its own process; keep this one.
    umask = os.umask(0o022)
    os.umask(umask)
    level = logging.getLogger("LiteLLM").level
    try:
        spec = importlib.util.spec_from_file_location("login_copilot", ROOT / "scripts/login-copilot.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.umask(umask)
        logging.getLogger("LiteLLM").setLevel(level)
    return module


class Response:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self.payload = payload
        self.text = text

    def json(self):
        if self.text is not None:
            raise ValueError("not JSON")
        return self.payload


TOKEN = "gho_fixture_not_a_token"


class FakeAuth:
    """The calls of the script, without a device flow or a token exchange.

    write=False acts as the provider when it cannot write the token file: it logs
    the error and returns the token anyway.
    """

    def __init__(self, token_file, write=True):
        self.access_token_file = token_file
        self.write = write
        self.logins = 0

    def get_access_token(self):
        self.logins += 1
        if self.write:
            Path(self.access_token_file).write_text(TOKEN)
        return TOKEN

    def get_api_key(self):
        return "gho_fixture_not_a_token"

    def get_api_base(self):
        return "http://copilot.invalid"


@unittest.skipUnless(importlib.util.find_spec("litellm"), "needs the LiteLLM image")
class ModelsAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.token = Path(tmp.name, "access-token")
        self.auth = FakeAuth(str(self.token))

    def run_command(self, command, response, *args, called=True):
        out = io.StringIO()
        with patch.object(self.script.httpx, "get", return_value=response) as get, \
                contextlib.redirect_stdout(out):
            try:
                command(self.auth, *args)
                code = 0
            except SystemExit as stop:
                code = stop.code
        if called:
            get.assert_called_once()
            self.assertEqual(get.call_args.args[0], "http://copilot.invalid/models")
        else:
            get.assert_not_called()
        self.assertNotIn(TOKEN, str(code) + out.getvalue())
        return code, out.getvalue()

    def test_login_with_models_shows_the_entitlement(self):
        code, out = self.run_command(self.script.login, Response(200, {"data": [{"id": "gpt-5.2"}]}))
        self.assertEqual(code, 0)
        self.assertIn("Token stored", out)
        self.assertIn("Entitlement shown: the models endpoint lists 1 model(s), 1 not disabled by policy.", out)
        self.assertIn("Login complete.", out)

    def test_login_stops_when_the_token_file_was_not_written(self):
        # The provider returns the token after a failed write; no discovery then.
        for before in (None, "gho_older_token_fixture"):
            with self.subTest(before=before):
                if before is None:
                    self.token.unlink(missing_ok=True)
                else:
                    self.token.write_text(before)
                self.auth = FakeAuth(str(self.token), write=False)
                code, out = self.run_command(self.script.login, Response(200, {"data": [{"id": "gpt-5.2"}]}),
                                             called=False)
                self.assertTrue(str(code).startswith("Token not stored:"), code)
                self.assertNotIn("Token stored", out)
                self.assertNotIn("Login complete", out)
                self.assertNotIn("gho_older_token_fixture", str(code) + out)

    def test_login_with_the_provider_stops_when_its_write_fails(self):
        # The real provider: the device flow is mocked, and the token path is in a
        # directory that does not exist, so its write fails and it returns the token.
        from litellm.llms.github_copilot import authenticator as provider

        with tempfile.TemporaryDirectory() as tokens, patch.dict(os.environ, {"GITHUB_COPILOT_TOKEN_DIR": tokens}):
            auth = provider.Authenticator()
        auth.access_token_file = str(Path(tokens, "gone", "access-token"))
        out = io.StringIO()
        with patch.object(provider, "can_block_current_thread", return_value=True), \
                patch.object(provider.Authenticator, "_login", return_value=TOKEN), \
                patch.object(self.script.httpx, "get") as get, contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as stop:
                self.script.login(auth)
        get.assert_not_called()
        self.assertTrue(str(stop.exception.code).startswith("Token not stored:"), stop.exception.code)
        self.assertNotIn("Token stored", out.getvalue())
        self.assertNotIn(TOKEN, str(stop.exception.code) + out.getvalue())

    def test_login_needs_a_model_that_policy_does_not_disable(self):
        disabled = {"id": "claude-opus-4.5", "policy": {"state": "disabled"}}
        code, out = self.run_command(self.script.login, Response(200, {"data": [disabled]}))
        self.assertIn("Token stored", out)
        self.assertNotIn("Login complete", out)
        self.assertTrue(str(code).startswith("Entitlement not shown:") and "disabled by policy" in code, code)
        code, out = self.run_command(self.script.login, Response(200, {"data": [
            disabled, {"id": "gpt-5.2", "policy": {"state": "enabled"}}, {"id": "gpt-5-mini"}]}))
        self.assertEqual(code, 0)
        self.assertIn("lists 3 model(s), 2 not disabled by policy.", out)
        self.assertIn("Login complete.", out)

    def test_login_reports_the_token_and_an_inconclusive_entitlement_apart(self):
        cases = {
            "empty list": (Response(200, {"data": []}), "listed no model"),
            "object without data": (Response(200, {"models": [{"id": "gpt-5.2"}]}), "unexpected form"),
            "list answer": (Response(200, [{"id": "gpt-5.2"}]), "unexpected form"),
            "entry without id": (Response(200, {"data": [{"name": "gpt-5.2"}]}), "unexpected form"),
            "not JSON": (Response(200, text="<html>"), "unexpected form"),
            "HTTP 403": (Response(403, {"message": "no seat"}), "HTTP 403"),
        }
        for name, (response, reason) in cases.items():
            with self.subTest(name):
                code, out = self.run_command(self.script.login, response)
                self.assertIn("Token stored", out)
                self.assertNotIn("Login complete", out)
                self.assertIsInstance(code, str)
                self.assertTrue(code.startswith("Entitlement not shown:"), code)
                self.assertIn(reason, code)
                self.assertNotIn("gho_fixture", code + out)

    def test_models_lists_ids_and_refuses_other_answers(self):
        self.token.write_text(TOKEN)
        code, out = self.run_command(self.script.models, Response(200, {"data": [
            {"id": "gpt-5.2", "supported_endpoints": ["/chat/completions"], "policy": {"state": "enabled"}},
            {"id": "claude-sonnet-4.5"}]}), True)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["claude-sonnet-4.5  policy=none",
                                            "gpt-5.2 /chat/completions policy=enabled"])
        for response, reason in ((Response(200, {"data": []}), "listed no model"),
                                 (Response(200, {"unexpected": True}), "unexpected form")):
            with self.subTest(reason):
                code, out = self.run_command(self.script.models, response, False)
                self.assertEqual(out, "")
                self.assertTrue(str(code).startswith("No model list:") and reason in code, code)


if __name__ == "__main__":
    unittest.main()
