#!/usr/bin/env python3
"""Run inside a one-off container of scripts/login-copilot.sh. Never prints a token.

login   device-code login of one GitHub account, then a check of the Copilot models endpoint:
        the token line and the entitlement line are separate
models  ids of the models that the Copilot API lists for the account

From LiteLLM 1.110.0 the provider sends the GitHub token to the Copilot API itself.
There is no token exchange and no api-key.json; the models endpoint shows the entitlement.
"""
import hmac
import logging
import os
import sys

os.umask(0o077)
# The provider logs each failed attempt; this script prints one line for a failure.
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)

import httpx  # noqa: E402
from litellm.llms.github_copilot.authenticator import Authenticator  # noqa: E402
from litellm.llms.github_copilot.common_utils import (  # noqa: E402
    DEFAULT_GITHUB_COPILOT_API_BASE,
    GithubCopilotError,
    get_copilot_default_headers,
)


def login(auth):
    # The provider prints the URL and the code. Each code is valid for one minute here:
    # the provider polls 12 times with 5 seconds between, then asks for a new code, 3 times.
    # It polls only for the newest code.
    print("Enter the newest code. A new code replaces the old one after one minute.", flush=True)
    token = auth.get_access_token()
    # The provider logs a failed write of the token file and returns the token anyway.
    if not token_saved(auth.access_token_file, token):
        sys.exit("Token not stored: the token file in the token directory does not hold the new token. "
                 "Check the owner and the mode of the directory, then run the login again.")
    print("Token stored: the token file is in the token directory.", flush=True)
    # An account without a Copilot seat gets no models list. The token stays in both cases.
    entries, problem = copilot_models(auth)
    if problem:
        sys.exit(f"Entitlement not shown: {problem}. The account may have no Copilot seat or no enabled model.")
    enabled = [entry for entry in entries if not policy_disabled(entry)]
    if not enabled:
        sys.exit(f"Entitlement not shown: the models endpoint lists {len(entries)} model(s), each disabled by policy. "
                 "Enable a model in the Copilot settings of the GitHub account.")
    print(f"Entitlement shown: the models endpoint lists {len(entries)} model(s), "
          f"{len(enabled)} not disabled by policy.")
    print("Login complete.")
    # At the first login the service is not in COMPOSE_FILE yet, and the recreate would fail.
    print("First login: continue with docs/copilot.md, Set up, step 3.")
    print("Later login: docker compose up -d --force-recreate copilot")


def token_saved(path, token):
    """True when the token file holds the token. Prints neither value."""
    try:
        with open(path) as f:
            saved = f.read().strip()
    except OSError:
        return False
    return bool(token) and hmac.compare_digest(saved.encode(), str(token).strip().encode())


def policy_disabled(entry):
    """policy.state "disabled": a request to the model gets HTTP 400 until the account enables it."""
    policy = entry.get("policy")
    return isinstance(policy, dict) and policy.get("state") == "disabled"


def model_entries(payload):
    """The entries of a models answer, or None when the answer has another form."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    if not all(isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] for entry in data):
        return None
    return data


def copilot_models(auth):
    """(entries, None) of the Copilot models endpoint for the stored GitHub token, or (None, problem)."""
    key = auth.get_api_key()
    # The address of the account comes from copilot_internal/user of github.com, then the defaults.
    base = (auth.get_api_base() or os.getenv("GITHUB_COPILOT_API_BASE") or DEFAULT_GITHUB_COPILOT_API_BASE).rstrip("/")
    response = httpx.get(f"{base}/models", headers=get_copilot_default_headers(key), timeout=30)
    if response.status_code != 200:
        return None, f"the models endpoint gave HTTP {response.status_code}"
    try:
        entries = model_entries(response.json())
    except ValueError:
        entries = None
    if entries is None:
        return None, "the models endpoint gave an answer of an unexpected form"
    if not entries:
        return None, "the models endpoint listed no model"
    return entries, None


def models(auth, endpoints):
    if not os.path.isfile(auth.access_token_file):
        sys.exit("No access token in the token directory. Run the login first.")
    entries, problem = copilot_models(auth)
    if problem:
        sys.exit(f"No model list: {problem}.")
    for entry in sorted(entries, key=lambda item: item["id"]):
        line = entry["id"]
        if endpoints:
            line += " " + ",".join(str(path) for path in entry.get("supported_endpoints") or [])
            # policy.state: "enabled", "disabled" (the account must enable the model in the
            # GitHub Copilot settings; a request gets "The requested model is not supported"),
            # or absent (no policy, the model is available).
            policy = entry.get("policy") if isinstance(entry.get("policy"), dict) else {}
            line += " policy=" + str(policy.get("state") or "none")
        print(line)


def main(argv):
    command = argv[1] if len(argv) > 1 else "login"
    auth = Authenticator()
    try:
        if command == "login":
            login(auth)
        elif command == "models":
            models(auth, "--endpoints" in argv[2:])
        else:
            sys.exit(f"Unknown command: {command}")
    except GithubCopilotError as error:
        # The message of the provider holds a status and a URL, no token.
        sys.exit(f"Failed: {error.message}")
    except httpx.HTTPError as error:
        sys.exit(f"Failed: {type(error).__name__}")


if __name__ == "__main__":
    main(sys.argv)
