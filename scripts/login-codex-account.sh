#!/bin/sh
# Device-code login for one ChatGPT account of the gateway (chatgpt_auth_file).
# The login runs in a separate, short-lived container, never in the gateway.
# The auth file goes to $CODEX_ACCOUNTS_DIR/<account>/auth.json with mode 0600.
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
account=${1:-}
case "$account" in
  "" | [!a-z0-9]* | *[!a-z0-9_-]*)
    echo "Usage: sh scripts/login-codex-account.sh <account> (a-z, 0-9, _ and -)" >&2
    exit 2 ;;
esac
if [ "${#account}" -gt 32 ]; then
  echo "The account name has more than 32 characters." >&2
  exit 2
fi
base=${CODEX_ACCOUNTS_DIR:-$repo/state/codex-accounts}
user=${CODEX_ACCOUNT_USER:-1000:1000}
# LITELLM_IMAGE, else the image of the service gateway in the Compose file.
# --no-env-resolution: Compose does not read the env files of the services.
image=${LITELLM_IMAGE:-}
if [ -z "$image" ]; then
  image=$("${DOCKER:-docker}" compose -f "$repo/compose.yaml" config --no-env-resolution --format json 2>/dev/null |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["gateway"]["image"])' 2>/dev/null) || image=
fi
if [ -z "$image" ]; then
  echo "No image for the service gateway in compose.yaml. Set LITELLM_IMAGE." >&2
  exit 2
fi
dir=$base/$account
umask 077
mkdir -p "$dir"
chmod 700 "$dir"
if [ "$(id -u)" = 0 ]; then
  chown "$user" "$dir"
fi
# No env file, no project network. The image may load the hooks; the
# chatgpt_auth_file hook stays off.
exec "${DOCKER:-docker}" run --rm --name "litellm-login-$account" \
  --user "$user" --cap-drop ALL --security-opt no-new-privileges:true \
  -e HOME=/tmp -e LITELLM_LOCAL_MODEL_COST_MAP=True -e LITELLM_TELEMETRY=False \
  -e CHATGPT_TOKEN_DIR=/tokens -e CHATGPT_AUTH_FILE=auth.json -e CHATGPT_AUTH_FILE_HOOK=off \
  -v "$dir:/tokens" \
  -v "$repo/scripts/login-codex-account.py:/login.py:ro" \
  --entrypoint python "$image" /login.py
