#!/bin/sh
# Offline test for scripts/login-copilot.sh. It uses --dry-run and a fake docker
# command that fails when it is called, so no container starts and no network is used.
#
# Usage: sh tests/test_login_copilot.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
login=$repo_root/scripts/login-copilot.sh

work=$(mktemp -d "${TMPDIR:-/tmp}/test-login-copilot.XXXXXX")
trap 'rm -rf "$work"' EXIT
# In sh, an INT or TERM handler does not end the script; exit runs the EXIT trap.
trap 'exit 130' INT TERM

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
expect() {
    name=$1; shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}
not() { ! "$@"; }
expect_status() {
    name=$1; want=$2; shift 2
    got=0; "$@" >"$work/out" 2>&1 || got=$?
    if [ "$got" -eq "$want" ]; then pass "$name"; else fail "$name (exit $got, expected $want)"; cat "$work/out"; fi
}
has() { grep -q -F -- "$1" "$work/out"; }

# A docker command that records a call and fails.
printf '#!/bin/sh\necho called >>"%s/docker-calls"\nexit 97\n' "$work" >"$work/docker"
chmod +x "$work/docker"
DOCKER=$work/docker LITELLM_IMAGE=example/gateway:test COPILOT_TOKEN_DIR=$work/tokens
export DOCKER LITELLM_IMAGE COPILOT_TOKEN_DIR
unset COPILOT_USER COPILOT_LOGIN_NETWORK COPILOT_LOGIN_NAME GITHUB_COPILOT_DEVICE_CODE_URL \
    GITHUB_COPILOT_ACCESS_TOKEN_URL GITHUB_COPILOT_API_KEY_URL GITHUB_COPILOT_API_BASE 2>/dev/null || true

expect_status "dry run of the login" 0 sh "$login" --dry-run
expect "dry run prints one line" [ "$(wc -l <"$work/out")" -eq 1 ]
expect "dry run names the docker command" has "$work/docker run --rm --name litellm-copilot-login"
expect "user 1000:1000" has "--user 1000:1000"
expect "all capabilities dropped" has "--cap-drop ALL --security-opt no-new-privileges:true"
expect "token directory variable" has "-e GITHUB_COPILOT_TOKEN_DIR=/state/copilot"
expect "token directory mount" has "-v $work/tokens:/state/copilot"
expect "login module mounted read-only" has "-v $repo_root/scripts/login-copilot.py:/login.py:ro"
expect "image and module at the end" has "--entrypoint python example/gateway:test /login.py"
expect "no env file" not has "--env-file"
expect "no network option by default" not has "--network"
expect "no URL variable by default" not has "GITHUB_COPILOT_DEVICE_CODE_URL"
expect "dry run makes no token directory" [ ! -e "$work/tokens" ]
expect "dry run does not call docker" [ ! -e "$work/docker-calls" ]

expect_status "dry run of the model list" 0 sh "$login" --dry-run models --endpoints
expect "the model command goes to the module" has "/login.py models --endpoints"

GITHUB_COPILOT_DEVICE_CODE_URL=https://ghe.example.invalid/login/device/code COPILOT_LOGIN_NETWORK=testnet \
    COPILOT_USER=1234:1234 expect_status "dry run with settings" 0 sh "$login" --dry-run login
expect "URL variable passed by name only" has "-e GITHUB_COPILOT_DEVICE_CODE_URL "
expect "URL value not in the command" not has "ghe.example.invalid"
expect "network option" has "--network testnet"
expect "user setting" has "--user 1234:1234"

expect_status "unknown command" 2 sh "$login" --dry-run bogus
expect_status "too many arguments" 2 sh "$login" --dry-run login extra
expect_status "unknown option of models" 2 sh "$login" --dry-run models --all
expect "usage errors do not call docker" [ ! -e "$work/docker-calls" ]

# Without --dry-run the script makes the directory with mode 0700 and calls docker.
# The container user is the user of the test, so that a host user that is not root can own the directory.
COPILOT_USER=$(id -u):$(id -g)
export COPILOT_USER
expect_status "real run calls docker" 97 sh "$login"
expect "docker called one time" [ "$(wc -l <"$work/docker-calls")" -eq 1 ]
expect "token directory has mode 0700" [ "$(stat -c %a "$work/tokens" 2>/dev/null || stat -f %Lp "$work/tokens")" = 700 ]

expect "token directory has the owner of COPILOT_USER" [ "$(stat -c %u "$work/tokens" 2>/dev/null || stat -f %u "$work/tokens")" = "$(id -u)" ]

# A missing parent directory does not get the mode 0700 of the token directory.
old_umask=$(umask)
umask 022
COPILOT_TOKEN_DIR=$work/clone/state/copilot expect_status "real run with a missing parent directory" 97 sh "$login"
umask "$old_umask"
expect "the parent directory keeps the mode of the caller" [ "$(stat -c %a "$work/clone/state" 2>/dev/null || stat -f %Lp "$work/clone/state")" = 755 ]
expect "the nested token directory has mode 0700" [ "$(stat -c %a "$work/clone/state/copilot" 2>/dev/null || stat -f %Lp "$work/clone/state/copilot")" = 700 ]

# A host user that is not root cannot give the directory to the container user: the script stops.
# Root can, so the script sets the owner.
rm -f "$work/docker-calls"
other=$(($(id -u) + 4242))
if [ "$(id -u)" = 0 ]; then
    COPILOT_USER=$other:$other COPILOT_TOKEN_DIR=$work/tokens2 expect_status "root: real run for another user" 97 sh "$login"
    expect "root: the directory has the owner of COPILOT_USER" [ "$(stat -c %u "$work/tokens2" 2>/dev/null || stat -f %u "$work/tokens2")" = "$other" ]
else
    COPILOT_USER=$other:$other COPILOT_TOKEN_DIR=$work/tokens2 expect_status "not root: wrong owner stops the script" 2 sh "$login"
    expect "not root: message names the owner" has "must have the owner $other"
    expect "not root: no directory" [ ! -e "$work/tokens2" ]
    expect "not root: docker not called" [ ! -e "$work/docker-calls" ]
    COPILOT_USER=$other:$other expect_status "not root: an existing directory with the wrong owner stops the script" 2 sh "$login"
fi

# The script and the service use the same paths.
expect "default token directory is state/copilot" grep -q -F 'dir=${COPILOT_TOKEN_DIR:-$repo/state/copilot}' "$login"
expect "the service mounts state/copilot at /state/copilot" grep -q -F -- '- ./state/copilot:/state/copilot' "$repo_root/compose.copilot.yaml"
expect "the service reads the token directory /state/copilot" grep -q -F 'GITHUB_COPILOT_TOKEN_DIR: /state/copilot' "$repo_root/compose.copilot.yaml"
expect "the login names the recreate of the service copilot" grep -q -F 'docker compose up -d --force-recreate copilot' "$repo_root/scripts/login-copilot.py"
expect "the login names the set-up steps for the first login" grep -q -F 'First login: continue with docs/copilot.md' "$repo_root/scripts/login-copilot.py"
expect "the login says to enter the newest code" grep -q -F 'Enter the newest code' "$repo_root/scripts/login-copilot.py"

if [ "$failures" -gt 0 ]; then
    echo "$failures failure(s)"
    exit 1
fi
echo "all tests passed"
