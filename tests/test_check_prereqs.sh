#!/bin/sh
# Offline test for scripts/check-prereqs.sh. Fake tools on PATH stand in for
# the real ones, so the result does not depend on the host.
#
# Usage: tests/test_check_prereqs.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
check=$repo_root/scripts/check-prereqs.sh

work=$(mktemp -d "${TMPDIR:-/tmp}/test-check-prereqs.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
expect_status() {
    name=$1; want=$2; shift 2
    got=0; "$@" >"$work/out" 2>&1 || got=$?
    if [ "$got" -eq "$want" ]; then pass "$name"; else fail "$name (exit $got, expected $want)"; cat "$work/out"; fi
}
expect_output() {
    name=$1; pattern=$2
    if grep -q -- "$pattern" "$work/out"; then pass "$name"; else fail "$name (no line matches '$pattern')"; cat "$work/out"; fi
}

# fake NAME SCRIPT_BODY: a tool on the fake PATH. $1.. are its arguments.
fake() {
    printf '#!/bin/sh\n%s\n' "$2" >"$work/bin/$1"
    chmod +x "$work/bin/$1"
}
# The real shell tools that the script itself uses, and nothing else from the host.
mkdir -p "$work/base"
for tool in sh tr sed awk; do
    ln -s "$(command -v "$tool")" "$work/base/$tool"
done
reset_bin() {
    rm -rf "$work/bin"; mkdir -p "$work/bin"
    fake git 'echo "git version 2.39.5"'
    fake python3 'echo "Python 3.11.2"'
    fake curl 'exit 0'
    fake node 'echo "v24.11.1"'
    fake docker 'case "$1 $2" in
    "compose version") echo "2.35.0" ;;
    "buildx version") echo "github.com/docker/buildx v0.19.0 1234567" ;;
    *) exit 1 ;;
esac'
}
# Only the fake directory and the four real shell tools: no real node, docker or podman.
run_check() { PATH="$work/bin:$work/base" sh "$check" "$@"; }

reset_bin
expect_status 'all tools present: exit 0' 0 run_check
expect_output 'node 24 reported ok' '^ok   - node v\{0,1\}24'
expect_output 'compose reported ok' '^ok   - docker compose 2.35.0'

reset_bin; fake node 'echo "v22.22.3"'
expect_status 'node 22: exit 1' 1 run_check
expect_output 'node 22 reported as failure' '^FAIL - node 22'

reset_bin; fake node 'echo "v25.0.0"'
expect_status 'node 25: exit 1 (major must be 24)' 1 run_check

reset_bin; rm "$work/bin/node"
expect_status 'no node: exit 1' 1 run_check
expect_output 'missing node reported' '^FAIL - node: command not found'

reset_bin; fake docker 'case "$1 $2" in
    "compose version") echo "2.34.0" ;;
    "buildx version") echo "github.com/docker/buildx v0.19.0 1234567" ;;
esac'
expect_status 'compose 2.34.0: exit 1' 1 run_check
expect_output 'old compose reported' '^FAIL - docker compose 2.34.0'

reset_bin; fake git 'echo "git version 2.23.0"'
expect_status 'git 2.23: exit 1' 1 run_check

reset_bin; fake podman 'echo "podman version 5.3.1"'
expect_status 'podman with docker compose provider: exit 0' 0 run_check --runtime podman
expect_output 'podman reported ok' '^ok   - podman 5.3.1'
expect_output 'DOCKER_HOST note without the variable' '^note - DOCKER_HOST is not set'

reset_bin; fake podman 'echo "podman version 5.3.1"'; rm "$work/bin/docker"
expect_status 'podman without a compose provider: exit 1' 1 run_check --runtime podman
expect_output 'missing provider reported' '^FAIL - compose provider'

reset_bin; fake podman 'echo "podman version 5.3.1"'; rm "$work/bin/docker"
fake docker-compose 'echo "2.35.0"'
expect_status 'podman with docker-compose provider: exit 0' 0 run_check --runtime podman

reset_bin; rm "$work/bin/docker"; fake podman 'echo "podman version 5.3.1"'; fake docker-compose 'echo "2.36.0"'
expect_status 'runtime default falls back to podman without docker' 0 run_check
expect_output 'summary names podman' '^check-prereqs: ok (podman)'

expect_status 'unknown option: exit 2' 2 run_check --bogus
expect_status 'unknown runtime: exit 2' 2 run_check --runtime lxc

if [ "$failures" -eq 0 ]; then echo "all tests passed"; else echo "$failures test(s) failed"; exit 1; fi
