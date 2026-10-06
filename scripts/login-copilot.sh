#!/bin/sh
# Device-code login for the GitHub Copilot account of the gateway (docs/copilot.md).
# The login runs in a separate, short-lived container, never in the gateway or the service copilot.
# The token files go to $COPILOT_TOKEN_DIR (default state/copilot), the directory that
# compose.copilot.yaml mounts into the service copilot. Owner 1000:1000, mode 0700.
# Run it as root, or as the host user with the uid of $COPILOT_USER (default 1000:1000):
# only root can give the directory to another user.
# After the first login: docs/copilot.md, Set up, step 3. After a later login:
# docker compose up -d --force-recreate copilot
#
# Usage: sh scripts/login-copilot.sh [--dry-run] [login | models [--endpoints]]
#   login    start the device flow; the container prints the URL and the code
#   models   list the model ids that the Copilot API gives to the account
#   --dry-run  print the docker command and change nothing
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
dry_run=
if [ "${1:-}" = "--dry-run" ]; then
  dry_run=1
  shift
fi
command=${1:-login}
case "$command" in
  login) [ "$#" -le 1 ] || command= ;;
  models) [ "$#" -eq 1 ] || { [ "$#" -eq 2 ] && [ "$2" = "--endpoints" ]; } || command= ;;
  *) command= ;;
esac
if [ -z "$command" ]; then
  echo "Usage: sh scripts/login-copilot.sh [--dry-run] [login | models [--endpoints]]" >&2
  exit 2
fi
dir=${COPILOT_TOKEN_DIR:-$repo/state/copilot}
user=${COPILOT_USER:-1000:1000}
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
# No env file, no project network, no master key. An empty PYTHONPATH keeps the
# hooks of the image off: the login needs none, and they print at start.
# The URL variables are for a GitHub Enterprise host; unset, the provider uses github.com.
set -- run --rm --name "${COPILOT_LOGIN_NAME:-litellm-copilot-login}" \
  --user "$user" --cap-drop ALL --security-opt no-new-privileges:true \
  -e HOME=/tmp -e PYTHONPATH= -e LITELLM_LOCAL_MODEL_COST_MAP=True -e LITELLM_TELEMETRY=False \
  -e GITHUB_COPILOT_TOKEN_DIR=/state/copilot \
  ${COPILOT_LOGIN_NETWORK:+--network "$COPILOT_LOGIN_NETWORK"} \
  ${GITHUB_COPILOT_DEVICE_CODE_URL:+-e GITHUB_COPILOT_DEVICE_CODE_URL} \
  ${GITHUB_COPILOT_ACCESS_TOKEN_URL:+-e GITHUB_COPILOT_ACCESS_TOKEN_URL} \
  ${GITHUB_COPILOT_API_KEY_URL:+-e GITHUB_COPILOT_API_KEY_URL} \
  ${GITHUB_COPILOT_API_BASE:+-e GITHUB_COPILOT_API_BASE} \
  -v "$dir:/state/copilot" \
  -v "$repo/scripts/login-copilot.py:/login.py:ro" \
  --entrypoint python "$image" /login.py "$@"
if [ -n "$dry_run" ]; then
  echo "${DOCKER:-docker} $*"
  exit 0
fi
# The container user must own the directory. Root sets the owner; another host user
# cannot, so the script stops when the owner would be wrong.
if [ "$(id -u)" != 0 ]; then
  if [ -d "$dir" ]; then
    owner=$(stat -c %u "$dir" 2>/dev/null || stat -f %u "$dir")
  else
    owner=$(id -u)
  fi
  if [ "$owner" != "${user%%:*}" ]; then
    echo "The token directory $dir must have the owner ${user%%:*}, the user of the service copilot. Run this script as root." >&2
    exit 2
  fi
fi
# The parent (state/ on a new clone) gets the usual mode: with umask 077 it would be
# 0700, and as root no other host user could enter it.
mkdir -p "$(dirname "$dir")"
umask 077
mkdir -p "$dir"
chmod 700 "$dir"
if [ "$(id -u)" = 0 ]; then
  chown "$user" "$dir"
fi
exec "${DOCKER:-docker}" "$@"
