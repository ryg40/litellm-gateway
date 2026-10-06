#!/bin/sh
# Each service has a separate OAuth directory. Existing CLI accounts stay unchanged.
set -eu
cd "$(dirname "$0")/.."
case "${1:-}" in
  1|2|3) service="codex$1" ;;
  *) echo "Usage: sh scripts/login-codex.sh 1|2|3" >&2; exit 2 ;;
esac
docker compose -f compose.yaml -f compose.codex.yaml run --rm --no-deps \
  -v "$PWD/scripts/login-codex.py:/login.py:ro" \
  --entrypoint python "$service" /login.py
