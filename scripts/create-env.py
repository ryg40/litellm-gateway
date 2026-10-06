#!/usr/bin/env python3
"""Create .env from .env.example with a new random value for each secret of the stack.

Usage: python3 scripts/create-env.py [DIRECTORY]
DIRECTORY holds .env.example and gets .env; the default is the repo root.

The secrets that the stack makes itself (GENERATED) get a new random value
when .env.example leaves them empty or holds a REPLACE_WITH_ placeholder.
The file gets mode 0600. The script refuses to replace a .env that exists.
It prints key names only, never a value. Standard library only (Python 3.9).
"""
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parent.parent

# Key name -> function that makes a new value. The master keys of LiteLLM
# start with sk-. The database password goes into DATABASE_URL, so it uses
# hex digits only.
GENERATED = {
    "LITELLM_MASTER_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "LITELLM_SALT_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "CODEX_MASTER_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "COPILOT_MASTER_KEY": lambda: "sk-" + secrets.token_urlsafe(32),
    "POSTGRES_PASSWORD": lambda: secrets.token_hex(24),
    "UI_PASSWORD": lambda: secrets.token_urlsafe(18),
}
PLACEHOLDER = "REPLACE_WITH_"


def render(example_text):
    """Return (text of .env, generated key names, key names that still hold a placeholder)."""
    lines, made, left = [], [], []
    for line in example_text.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and not key.startswith("#"):
            if key in GENERATED and (not value.strip() or PLACEHOLDER in value):
                line = f"{key}={GENERATED[key]()}"
                made.append(key)
            elif PLACEHOLDER in value:
                left.append(key)
        lines.append(line)
    missing = sorted(set(GENERATED) - set(made))
    if missing:
        raise SystemExit(f"create-env: .env.example has no empty value for {', '.join(missing)}")
    return "\n".join(lines) + "\n", made, left


def main(argv):
    if len(argv) > 2 or (len(argv) == 2 and argv[1].startswith("-")):
        raise SystemExit(__doc__.split("\n\n")[1])
    directory = Path(argv[1]) if len(argv) == 2 else ROOT
    target = directory / ".env"
    if os.path.lexists(target):
        raise SystemExit(f"create-env: {target} exists; the script does not replace it. "
                         "Remove it yourself only when you know that no database uses its keys.")
    text, made, left = render((directory / ".env.example").read_text())
    # O_EXCL: no replacement, also when another process creates the file now.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        os.unlink(target)
        raise
    print(f"create-env: wrote {target} (mode 0600) with new values for {', '.join(made)}")
    if left:
        print(f"create-env: replace the placeholder of {', '.join(left)} in .env")


if __name__ == "__main__":
    main(sys.argv)
