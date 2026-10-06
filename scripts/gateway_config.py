"""Find the host gateway configuration for the scripts in this directory.

Compose mounts ${GATEWAY_CONFIG:-./config/gateway.example.yaml}. The scripts
use the same variable, from the environment or from .env, with the host file
.local/config/gateway.yaml as the default. They never write a tracked example.
"""
import json
import os
from pathlib import Path
import re
import tempfile

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config/gateway.example.yaml"
HOST_DEFAULT = ".local/config/gateway.yaml"
# LiteLLM reads the file as YAML. PyYAML takes 1e-07 as a string: a float needs a dot.
EXPONENT = re.compile(r"(?m)((?:: |^ +)-?[0-9]+)(e[-+]?[0-9]+,?)$")


def setting(root, name):
    """Return a value from the environment, else from root/.env, else None."""
    if os.environ.get(name):
        return os.environ[name]
    env = Path(root) / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name and value.strip():
                return value.strip().strip("\"'")
    return None


def path(root):
    """Return the host configuration path. Stop when it is under config/."""
    root = Path(root).resolve()
    result = root / (setting(root, "GATEWAY_CONFIG") or HOST_DEFAULT)
    result = result.resolve()
    if result.is_relative_to(root / "config"):
        raise SystemExit(f"GATEWAY_CONFIG points to the tracked file {result.relative_to(root)}. "
                         f"Set it to a host file, for example ./{HOST_DEFAULT}.")
    return result


def load(root):
    """Return the host path and its configuration; the example if the host file does not exist."""
    result = path(root)
    return result, json.loads((result if result.exists() else EXAMPLE).read_text())


def save(root, target, config):
    """Write the configuration to the host path atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".gateway-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(EXPONENT.sub(r"\1.0\2", json.dumps(config, indent=2)) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Wrote {target}.")
    if not setting(root, "GATEWAY_CONFIG"):
        print(f"Set GATEWAY_CONFIG=./{HOST_DEFAULT} in .env so that Compose mounts this file.")
