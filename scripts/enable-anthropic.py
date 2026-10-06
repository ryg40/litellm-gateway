#!/usr/bin/env python3
"""Install a separate Anthropic API key or token without reading CLI credentials."""
import getpass
import os
from pathlib import Path
import sys

# Import the helper without a __pycache__ directory in the checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Resolve the host file first: a wrong GATEWAY_CONFIG stops before .env changes.
path, config = gateway_config.load(ROOT)
key = getpass.getpass("Separate Anthropic API key or OAuth token: ").strip()
if not key.startswith("sk-ant-") or any(c.isspace() for c in key):
    raise SystemExit("Expected an Anthropic credential without whitespace")
os.umask(0o077)
env_path = ROOT / ".env"
lines = [line for line in env_path.read_text().splitlines() if not line.startswith("ANTHROPIC_API_KEY=")]
lines.append("ANTHROPIC_API_KEY=" + key)
env_path.write_text("\n".join(lines) + "\n")
os.chmod(env_path, 0o600)
config["model_list"] = [m for m in config["model_list"] if m["model_name"] != "anthropic/*"]
config["model_list"].append({"model_name": "anthropic/*", "litellm_params": {
    "model": "anthropic/*", "api_key": "os.environ/ANTHROPIC_API_KEY"
}})
gateway_config.save(ROOT, path, config)
print("Run docker compose up -d --force-recreate gateway, then test an available Anthropic model.")
print("OAuth tokens do not auto-refresh here. Replace them before expiry.")
