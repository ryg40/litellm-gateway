#!/usr/bin/env python3
"""Run inside a one-off container of scripts/login-copilot.sh. Never prints a token.

login   device-code login of one GitHub account, then the Copilot token exchange
models  ids of the models that the Copilot API lists for the account
"""
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
    auth.get_access_token()
    # The exchange fails when the account has no Copilot entitlement.
    auth.get_api_key()
    print("Login complete. The token files stay in the token directory.")
    # At the first login the service is not in COMPOSE_FILE yet, and the recreate would fail.
    print("First login: continue with docs/copilot.md, Set up, step 3.")
    print("Later login: docker compose up -d --force-recreate copilot")


def models(auth, endpoints):
    if not os.path.isfile(auth.access_token_file):
        sys.exit("No access token in the token directory. Run the login first.")
    key = auth.get_api_key()
    base = (auth.get_api_base() or os.getenv("GITHUB_COPILOT_API_BASE") or DEFAULT_GITHUB_COPILOT_API_BASE).rstrip("/")
    response = httpx.get(f"{base}/models", headers=get_copilot_default_headers(key), timeout=30)
    if response.status_code != 200:
        sys.exit(f"The models endpoint gave HTTP {response.status_code}.")
    for entry in sorted(response.json().get("data", []), key=lambda item: str(item.get("id"))):
        line = str(entry.get("id"))
        if endpoints:
            line += " " + ",".join(entry.get("supported_endpoints") or [])
            # policy.state: "enabled", "disabled" (the account must enable the model in the
            # GitHub Copilot settings; a request gets "The requested model is not supported"),
            # or absent (no policy, the model is available).
            line += " policy=" + str((entry.get("policy") or {}).get("state") or "none")
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
