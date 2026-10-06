#!/usr/bin/env python3
"""Verify authentication and model listing; optionally run small completion tests."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

# Import the helper without a __pycache__ directory in the checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def default_base():
    # Same variable and default as the published port in compose.yaml.
    port = gateway_config.setting(ROOT, "GATEWAY_PORT") or "127.0.0.1:4321"
    return "http://" + (port if ":" in port else "127.0.0.1:" + port)


def request(base, path, key=None, body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + path, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        # Never print upstream errors, headers, or credentials.
        return error.code, json.loads(error.read())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=default_base())
    parser.add_argument("--model", action="append", default=[])
    args = parser.parse_args()
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    # A placeholder of an old .env.example is a known value: stop before any request.
    placeholders = sorted(name.strip() for name, value in env.items() if "REPLACE_WITH_" in value)
    if placeholders:
        sys.exit(f"verify: .env still holds REPLACE_WITH_ in {', '.join(placeholders)}. "
                 "Set a new value; python3 scripts/create-env.py makes a new .env.")
    key = env.get("LITELLM_MASTER_KEY", "").strip()
    if not key:
        sys.exit("verify: LITELLM_MASTER_KEY in .env is empty; python3 scripts/create-env.py makes a new .env.")
    for token in (None, "sk-invalid-test"):
        status, _ = request(args.base, "/v1/models", token)
        # A missing or unknown key must give HTTP 401 or 403. Each other status
        # fails the check, also a 400 no_db_connection of LiteLLM without a
        # database: that status does not show an authentication rejection.
        assert status in (401, 403), f"Authentication rejection failed: HTTP {status}"
    status, data = request(args.base, "/v1/models", key)
    assert status == 200 and data.get("data"), f"Model listing failed: HTTP {status}"
    print(f"Authentication passed; {len(data['data'])} model entries")
    for model in args.model:
        status, response = request(args.base, "/v1/chat/completions", key,
                                   {"model": model, "messages": [{"role": "user", "content": "Reply only OK."}],
                                    "max_tokens": 128})
        assert status == 200 and response.get("choices"), f"{model}: HTTP {status}"
        print(f"{model}: completion passed")


if __name__ == "__main__":
    main()
