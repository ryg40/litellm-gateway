#!/usr/bin/env python3
"""Switch an existing codex-router host override without restarting other services."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from gateway_config import REPO, setting

OVERRIDE = ".local/compose.host.yaml"
ORDER_LINE = re.compile(
    r"(?m)^(?P<prefix>[ \t]+CODEX_ACCOUNT_ORDER:[ \t]*)(?P<value>[^\r\n]*)(?P<end>\r?$)"
)
ACCOUNTS = {"codex1", "codex2", "codex3"}


class Refused(Exception):
    pass


def parse_order(values):
    parts = re.split(r"[,\s-]+", " ".join(values).strip().lower())
    order = ["codex" + part if part in {"1", "2", "3"} else part for part in parts]
    if len(order) != 3 or set(order) != ACCOUNTS:
        raise Refused("Give each account once, for example 2-3-1 or 3 2 1.")
    return order


def replace_order(text, order):
    matches = list(ORDER_LINE.finditer(text))
    if len(matches) != 1:
        raise Refused(f"{OVERRIDE} must have exactly one CODEX_ACCOUNT_ORDER mapping entry.")
    match = matches[0]
    _, marker, comment = match["value"].partition("#")
    # Keep the surrounding host configuration, comments and line endings unchanged.
    replacement = '"' + ",".join(order) + '"'
    if marker:
        replacement += " " + marker + comment
    return text[:match.start("value")] + replacement + text[match.end("value"):]


def command(args, root):
    result = subprocess.run(args, cwd=root, capture_output=True, text=True)
    if result.returncode:
        # Compose diagnostics can contain interpolated credentials. Do not echo them.
        raise Refused(f"{args[0]} {args[1]} failed (exit {result.returncode}); no command output is shown.")
    return result.stdout


def compose_command(root, files):
    args = ["docker", "compose", "--project-directory", str(root)]
    for path in files:
        args.extend(["-f", str(path)])
    return args


def config(root, files):
    return json.loads(command(compose_command(root, files) +
                              ["config", "--format", "json", "--no-env-resolution"], root))


def compose_files(root):
    value = setting(root, "COMPOSE_FILE")
    if not value:
        raise Refused(f"Set COMPOSE_FILE in .env to include compose.codex.yaml and {OVERRIDE} first.")
    separator = setting(root, "COMPOSE_PATH_SEPARATOR") or os.pathsep
    files = [(root / part).resolve() for part in value.split(separator)]
    target = (root / OVERRIDE).resolve()
    if files.count(target) != 1:
        raise Refused(f"The active COMPOSE_FILE must include {OVERRIDE} exactly once.")
    return files


def router_config(rendered):
    try:
        return rendered["services"]["codex-router"]
    except KeyError:
        raise Refused("The active Compose configuration has no codex-router.") from None


def check_routes(rendered):
    gateway = rendered["services"].get("gateway", {})
    mounts = [v for v in gateway.get("volumes", []) if v.get("target") == "/config/config.yaml"]
    if len(mounts) != 1 or mounts[0].get("type") != "bind":
        raise Refused("Cannot find the gateway configuration bind mount.")
    try:
        data = json.loads(Path(mounts[0]["source"]).read_text())
    except (OSError, ValueError):
        raise Refused("The gateway configuration must be readable JSON, as used by the host scripts.") from None
    routes = [item for item in data.get("model_list", [])
              if item.get("model_name", "").startswith("codex-auto/")]
    if not routes or any(item.get("litellm_params", {}).get("api_base", "").rstrip("/") !=
                         "http://codex-router:4000/v1" for item in routes):
        raise Refused("All codex-auto routes must use http://codex-router:4000/v1; native fallbacks are not changed.")
    for section in ("router_settings", "litellm_settings"):
        settings = data.get(section) or {}
        if settings.get("default_fallbacks"):
            raise Refused("Remove default_fallbacks before using this router-only switch.")
        for field in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks"):
            for entry in settings.get(field) or []:
                if any(key == "*" or key.startswith("codex-auto/") for key in entry):
                    raise Refused("codex-auto has gateway fallbacks; this tool changes only codex-router.")


def check_mounts(router, order, root):
    token_dir = router.get("environment", {}).get("CODEX_TOKEN_DIR", "/tokens").rstrip("/")
    for account in order[:-1]:
        expected = str((root / "state" / account).resolve())
        mounts = [v for v in router.get("volumes", []) if v.get("target") == f"{token_dir}/{account}"]
        if (len(mounts) != 1 or mounts[0].get("source") != expected or
                mounts[0].get("type") != "bind" or not mounts[0].get("read_only")):
            raise Refused(f"Add ./state/{account}:{token_dir}/{account}:ro to {OVERRIDE} before this order.")
        if not Path(expected).is_dir():
            raise Refused(f"state/{account} is missing; no token directory is created by this tool.")


def live_router(root, files):
    ids = command(compose_command(root, files) + ["ps", "--all", "--quiet", "codex-router"], root).split()
    if len(ids) != 1:
        raise Refused("Exactly one existing codex-router container is required.")
    live = json.loads(command(["docker", "inspect", ids[0]], root))[0]
    labels = live["Config"].get("Labels") or {}
    if Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != root:
        raise Refused("The router belongs to another checkout.")
    return live


def check_deployment(root, files, live):
    hashes = command(compose_command(root, files) + ["config", "--hash", "codex-router"], root).split()
    recorded = (live["Config"].get("Labels") or {}).get("com.docker.compose.config-hash")
    if len(hashes) != 2 or hashes != ["codex-router", recorded]:
        raise Refused("The running router differs from Compose. Resolve the deployment drift before switching.")


def live_order(live):
    for entry in live["Config"].get("Env", []):
        if entry.startswith("CODEX_ACCOUNT_ORDER="):
            return entry.partition("=")[2]
    return "codex3,codex2,codex1"


def write_atomic(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix=".codex-order-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args, root=REPO):
    root = root.resolve()
    order = parse_order(args.order) if args.order else None
    if not order and (args.dry_run or args.yes):
        raise Refused("Give an order with --dry-run or --yes.")
    files = compose_files(root)
    before = config(root, files)
    router = router_config(before)
    live = live_router(root, files)
    print("Configured order: " + router.get("environment", {}).get("CODEX_ACCOUNT_ORDER", "(default)"))
    print("Running order:    " + live_order(live))
    if not order:
        return 0
    check_routes(before)
    if live["State"]["Status"] != "running":
        raise Refused("The router is not running. Restore it before switching the order.")
    image = json.loads(command(["docker", "image", "inspect", router["image"]], root))[0]
    if image["Id"] != live["Image"]:
        raise Refused("The configured image differs from the running image. Resolve the deployment drift first.")
    target = root / OVERRIDE
    if target.is_symlink() or not target.is_file():
        raise Refused(f"{OVERRIDE} must be a regular file, not a symbolic link.")
    check_deployment(root, files, live)
    original = target.read_bytes()
    changed = replace_order(original.decode(), order).encode()
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=".codex-order-preview-", suffix=".yaml", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(changed)
        candidate_files = [Path(temporary) if path == target.resolve() else path for path in files]
        after = config(root, candidate_files)
    finally:
        os.unlink(temporary)
    candidate = router_config(after)
    requested = ",".join(order)
    if candidate.get("environment", {}).get("CODEX_ACCOUNT_ORDER") != requested:
        raise Refused("A later override prevents the requested order.")
    check_mounts(candidate, order, root)
    expected = json.loads(json.dumps(before))
    expected["services"]["codex-router"]["environment"]["CODEX_ACCOUNT_ORDER"] = requested
    if after != expected:
        raise Refused("The proposed configuration changes more than the router order.")
    if requested == live_order(live) and requested == router["environment"].get("CODEX_ACCOUNT_ORDER"):
        print("The requested order is already configured and running. Nothing changed.")
        return 0
    print("Requested order: " + requested)
    print("Only codex-router will be recreated, with no build, pull or dependency restart.")
    print("Warning: Recreating the router can interrupt active Codex requests and clears its quota cache.")
    if args.dry_run:
        print("Dry run: no configuration or service changed.")
        return 0
    if not args.yes and input("Apply this order? [y/N] ").strip().lower() not in {"y", "yes"}:
        print("Cancelled. Nothing changed.")
        return 0
    # Refuse an edit made while the owner was reading the confirmation prompt.
    if target.read_bytes() != original or config(root, files) != before:
        raise Refused("The configuration changed during confirmation. Run the command again.")
    if live_router(root, files)["Id"] != live["Id"]:
        raise Refused("The router changed during confirmation. Run the command again.")
    latest_image = json.loads(command(["docker", "image", "inspect", router["image"]], root))[0]
    if latest_image["Id"] != live["Image"]:
        raise Refused("The image changed during confirmation. Run the command again.")
    backup = None
    if original != changed:
        fd, name = tempfile.mkstemp(prefix="compose.host.before-codex-order-", suffix=".yaml", dir=target.parent)
        backup = Path(name)
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
        write_atomic(target, changed, mode)
        print(f"Backup: {backup.relative_to(root)}")
    try:
        command(compose_command(root, files) + ["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                "--force-recreate", "--wait", "--wait-timeout", "120", "codex-router"], root)
        current = live_router(root, files)
        if (live_order(current) != requested or current["Image"] != live["Image"] or
                current["State"].get("Health", {}).get("Status") != "healthy"):
            raise Refused("The router did not pass the order, image and health checks.")
    except (Refused, OSError, ValueError):
        print("The requested configuration remains on disk; the live result is not verified.")
        print("Run --status and inspect the router. No automatic rollback or second restart was attempted.")
        if backup:
            print(f"The previous override is in {backup.relative_to(root)}.")
        raise
    print("Verified running order: " + requested + "; router healthy; image unchanged.")
    print("Quota blocks can still select a later account. No inference or login request was sent.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        "Examples: %(prog)s 2-3-1; %(prog)s 3 2 1 --dry-run; %(prog)s --status. "
        "Uses the active COMPOSE_FILE and edits only .local/compose.host.yaml. "
        "The first two accounts must already have read-only token mounts.")
    parser.add_argument("order", nargs="*", help="all three accounts, in preference order")
    parser.add_argument("--status", action="store_true", help="show configured and running orders; change nothing")
    parser.add_argument("--dry-run", action="store_true", help="validate and show the change without applying it")
    parser.add_argument("--yes", action="store_true", help="apply without the confirmation prompt")
    args = parser.parse_args()
    if args.status and (args.order or args.dry_run or args.yes):
        parser.error("--status cannot be combined with an order, --dry-run or --yes")
    try:
        if args.order and not args.dry_run:
            # Serialize this tool's writes and restarts; a second switch must start again.
            with (REPO / ".local/.codex-order.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Refused("Another account switch is in progress.") from None
                return run(args)
        return run(args)
    except (Refused, OSError, ValueError, EOFError) as error:
        parser.exit(1, f"Stopped: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. Check --status before trying again.\n")


if __name__ == "__main__":
    raise SystemExit(main())
