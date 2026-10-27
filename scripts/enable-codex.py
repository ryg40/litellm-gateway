#!/usr/bin/env python3
"""Add the Codex routes of config/gateway.codex.example.yaml to the host gateway file
after the isolated account logins. The tracked files stay unchanged."""
import argparse
import json
from pathlib import Path
import sys

# Import the helper without a __pycache__ directory in the checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CODEX_ROUTES = gateway_config.REPO / "config/gateway.codex.example.yaml"
MAX_ACCOUNTS = 3
DEFAULT_ACCOUNT_ORDER = ("codex3", "codex2", "codex1")
PREFIXES = tuple(f"codex{n}/" for n in range(1, MAX_ACCOUNTS + 1)) + ("codex-auto/",)


def complete_login(account):
    path = ROOT / f"state/codex{account}/auth.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    return bool(data.get("access_token") and data.get("refresh_token"))


def main(argv=()):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, choices=range(2, MAX_ACCOUNTS + 1),
                        help="number of logged-in accounts codex1..codexN (default: every account "
                             f"with a complete login, codex1 to codex{MAX_ACCOUNTS})")
    args = parser.parse_args(list(argv))
    accounts = args.accounts
    if accounts is None:
        accounts = 0
        while accounts < MAX_ACCOUNTS and complete_login(accounts + 1):
            accounts += 1
    for account in range(1, max(accounts, 2) + 1):
        if not (ROOT / f"state/codex{account}/auth.json").exists():
            raise SystemExit(f"Run sh scripts/login-codex.sh {account} first")
        if not complete_login(account):
            raise SystemExit(f"Account {account} has no complete login")
    keep = tuple(f"codex{n}/" for n in range(1, accounts + 1)) + ("codex-auto/",)

    order = [name for name in DEFAULT_ACCOUNT_ORDER if f"{name}/" in keep]
    routes = json.loads(CODEX_ROUTES.read_text())
    for entry in routes["model_list"]:
        if entry["model_name"].startswith("codex-auto/"):
            entry["litellm_params"]["api_base"] = f"http://{order[0]}:4000/v1"
    path, config = gateway_config.load(ROOT)
    config["model_list"] = [entry for entry in config["model_list"]
                            if not entry["model_name"].startswith(PREFIXES)]
    config["model_list"].extend(entry for entry in routes["model_list"] if entry["model_name"].startswith(keep))
    router = config.setdefault("router_settings", {})
    kept_fallbacks = [{name: targets for name, targets in item.items()
                       if not name.startswith(PREFIXES)} for item in router.get("fallbacks", [])]
    router["fallbacks"] = [item for item in kept_fallbacks if item]
    # Rebuild from the selected accounts, not filtered template targets: an
    # omitted preferred account must not become a self fallback on codex2.
    for entry in routes["model_list"]:
        source = entry["model_name"]
        if source.startswith("codex-auto/"):
            model = source.split("/", 1)[1]
            router["fallbacks"].append({source: [f"{name}/{model}" for name in order[1:]]})
    # Each codex-auto route has accounts - 1 fallback targets. Raise a lower value of
    # the host file, for example 1 from a file for two accounts; keep a higher one.
    router["max_fallbacks"] = max(router.get("max_fallbacks") or 0, accounts - 1)
    gateway_config.save(ROOT, path, config)
    print(f"Routes for {accounts} accounts written.")
    print("Run docker compose -f compose.yaml -f compose.codex.yaml up -d --force-recreate")
    print("Test codex-auto/luna, sol, astra, and terra through the public endpoint.")


if __name__ == "__main__":
    main(sys.argv[1:])
