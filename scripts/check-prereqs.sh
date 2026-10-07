#!/bin/sh
# Check the host tools before the install and at the start of a pipeline run.
# EXPLAINER.md (section "Prerequisites") and docs/ci.md describe it.
#
# Usage: scripts/check-prereqs.sh [--runtime docker|podman]
#   --runtime   the container runtime to check. Default: docker when the
#               docker CLI exists, otherwise podman.
#
# Hard requirements (exit code 1 when one is missing):
#   git 2.24, python3 3.9, Node.js major version 24, sh, curl, and one runtime:
#   docker  Compose 2.35.0 and Buildx 0.19
#   podman  Podman 4.0 and a Compose provider (docker compose or docker-compose)
# Node.js has no use inside this repo. The build pipelines run on Node 24, so
# the check refuses another major version (docs/ci.md).
# Exit code 2: a usage error.
# Portable POSIX sh; works with the BSD tools of macOS and the GNU tools of Linux.
set -eu

NODE_MAJOR=24

usage() { sed -n '2,15p' "$0"; }

runtime=
while [ "$#" -gt 0 ]; do
	case "$1" in
	--runtime)
		[ "$#" -ge 2 ] || { usage >&2; exit 2; }
		runtime=$2
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		usage >&2
		exit 2
		;;
	esac
	shift
done
case $runtime in
'' | docker | podman) ;;
*)
	usage >&2
	exit 2
	;;
esac

failures=0
ok() { printf 'ok   - %s\n' "$1"; }
fail() {
	printf 'FAIL - %s\n' "$1"
	failures=$((failures + 1))
}

# first_version TEXT: the first dotted number in TEXT, without a leading v.
first_version() {
	printf '%s\n' "$1" | tr ' ,' '\n\n' | sed -n 's/^v\{0,1\}\([0-9][0-9]*\(\.[0-9][0-9]*\)*\).*/\1/p' | sed -n 1p
}

# version_ge HAVE WANT: HAVE is WANT or later (up to three components).
version_ge() {
	printf '%s %s\n' "$1" "$2" | awk '{
		n = split($1, a, "."); m = split($2, b, ".")
		for (i = 1; i <= 3; i++) {
			x = (i <= n) ? a[i] + 0 : 0; y = (i <= m) ? b[i] + 0 : 0
			if (x > y) exit 0
			if (x < y) exit 1
		}
		exit 0
	}'
}

# require NAME MINIMUM COMMAND...: the command prints a version at or above MINIMUM.
require() {
	name=$1 minimum=$2
	shift 2
	if ! command -v "$1" >/dev/null 2>&1; then
		fail "$name: command $1 not found (minimum $minimum)"
		return 0
	fi
	text=$("$@" 2>&1 </dev/null) || text=
	have=$(first_version "$text")
	if [ -z "$have" ]; then
		fail "$name: no version in the output of '$*' (minimum $minimum)"
	elif version_ge "$have" "$minimum"; then
		ok "$name $have (minimum $minimum)"
	else
		fail "$name $have (minimum $minimum)"
	fi
}

for tool in sh curl; do
	if command -v "$tool" >/dev/null 2>&1; then ok "$tool"; else fail "$tool: command not found"; fi
done
require git 2.24 git --version
require python3 3.9 python3 --version

# Node.js: the major version must be NODE_MAJOR. A later major is not accepted.
if ! command -v node >/dev/null 2>&1; then
	fail "node: command not found (required major version $NODE_MAJOR)"
else
	text=$(node --version 2>&1 </dev/null) || text=
	have=$(first_version "$text")
	major=${have%%.*}
	if [ -z "$have" ]; then
		fail "node: no version in the output of 'node --version' (required major version $NODE_MAJOR)"
	elif [ "$major" = "$NODE_MAJOR" ]; then
		ok "node $have (required major version $NODE_MAJOR)"
	else
		fail "node $have (required major version $NODE_MAJOR)"
	fi
fi

if [ -z "$runtime" ]; then
	if command -v docker >/dev/null 2>&1; then runtime=docker; else runtime=podman; fi
fi
case $runtime in
docker)
	if command -v docker >/dev/null 2>&1; then
		ok "docker CLI ($(command -v docker))"
	else
		fail "docker: command not found"
	fi
	require 'docker compose' 2.35.0 docker compose version --short
	require 'docker buildx' 0.19 docker buildx version
	;;
podman)
	require podman 4.0 podman --version
	if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
		require 'docker compose (provider)' 2.35.0 docker compose version --short
		if [ -z "${DOCKER_HOST:-}" ]; then
			printf 'note - DOCKER_HOST is not set; set it to the Podman socket before docker compose (docs/mac.md)\n'
		fi
	elif command -v docker-compose >/dev/null 2>&1; then
		require 'docker-compose (provider)' 2.35.0 docker-compose version --short
		printf 'note - the login scripts call docker compose; install the docker CLI with the compose plugin for them\n'
	else
		fail "compose provider: neither 'docker compose' nor 'docker-compose' found (minimum 2.35.0)"
	fi
	;;
esac

if [ "$failures" -eq 0 ]; then
	printf 'check-prereqs: ok (%s)\n' "$runtime"
	exit 0
fi
printf 'check-prereqs: %s hard requirement(s) missing (%s)\n' "$failures" "$runtime" >&2
exit 1
