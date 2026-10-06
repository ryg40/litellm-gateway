#!/bin/sh
# Offline test for scripts/setup-remotes.sh and scripts/fetch-upstream.sh.
# A local repository stands in for upstream through UPSTREAM_URL.
#
# Usage: tests/test_setup_remotes.sh

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
setup=$repo_root/scripts/setup-remotes.sh
fetch=$repo_root/scripts/fetch-upstream.sh

work=$(mktemp -d "${TMPDIR:-/tmp}/test-setup-remotes.XXXXXX")
trap 'rm -rf "$work"' EXIT
# In sh, an INT or TERM handler does not end the script; exit runs the EXIT trap.
trap 'exit 130' INT TERM

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
expect_ok() {
    name=$1; shift
    if "$@" >"$work/out" 2>&1; then pass "$name"; else fail "$name"; cat "$work/out"; fi
}
not() { ! "$@"; }
expect() {
    name=$1; shift
    if "$@"; then pass "$name"; else fail "$name"; fi
}
expect_status() {
    name=$1; want=$2; shift 2
    got=0; "$@" >"$work/out" 2>&1 || got=$?
    if [ "$got" -eq "$want" ]; then pass "$name"; else fail "$name (exit $got, expected $want)"; cat "$work/out"; fi
}
expect_fail() {
    name=$1; shift
    if "$@" >"$work/out" 2>&1; then fail "$name"; cat "$work/out"; else pass "$name"; fi
}

git_quiet() { git -c init.defaultBranch=main -c user.name=test -c user.email=test@example.invalid "$@" >/dev/null 2>&1; }

# Fake upstream with two release tags and a non-release tag.
git_quiet init "$work/upstream"
for v in 1.0.0 1.1.0; do
    echo "$v" >"$work/upstream/version.txt"
    git_quiet -C "$work/upstream" add version.txt
    git_quiet -C "$work/upstream" commit -m "release $v"
    git_quiet -C "$work/upstream" tag -a "v$v" -m "v$v"
done
git_quiet -C "$work/upstream" tag nightly
UPSTREAM_URL=file://$work/upstream
export UPSTREAM_URL

# Fake "origin" with credentials in its URL and one local release tag.
git_quiet init "$work/clone"
git_quiet -C "$work/clone" commit --allow-empty -m init
git_quiet -C "$work/clone" tag portable-v1
git -C "$work/clone" remote add origin https://user:s3cret@example.invalid/litellm.git
cd "$work/clone"

expect_fail "--check fails in a new clone" "$setup" --check
expect_ok "first run" "$setup"
expect "origin URL printed without credentials" not grep -q s3cret "$work/out"
git config --get-regexp '^remote\.' >"$work/config1"
expect_ok "second run" "$setup"
git config --get-regexp '^remote\.' >"$work/config2"
expect "second run gives the same configuration" cmp -s "$work/config1" "$work/config2"
expect_ok "--check passes after setup" "$setup" --check
git config --get-regexp '^remote\.' >"$work/config3"
expect "--check changes nothing" cmp -s "$work/config1" "$work/config3"
expect "origin is not changed" [ "$(git config remote.origin.url)" = https://user:s3cret@example.invalid/litellm.git ]
expect "push URL is DISABLED" [ "$(git config remote.upstream.pushurl)" = DISABLED ]
expect "tagOpt is --no-tags" [ "$(git config remote.upstream.tagOpt)" = --no-tags ]
expect_fail "git push upstream fails" git push upstream HEAD:refs/heads/test

git remote set-url --push upstream "$UPSTREAM_URL"
expect_fail "--check fails with a wrong push URL" "$setup" --check
expect_ok "setup repairs the push URL" "$setup"
expect_ok "--check passes after repair" "$setup" --check

git config --add remote.upstream.fetch '+refs/heads/*:refs/remotes/upstream/*'
expect_fail "--check fails with an extra fetch refspec" "$setup" --check
expect_ok "setup repairs the fetch refspec" "$setup"

expect_ok "plain git fetch upstream" git fetch upstream
expect "plain fetch creates no refs" [ -z "$(git for-each-ref refs/upstream refs/remotes/upstream)" ]

for bad in 1.0.0 v1.0 v1.0.0.. 'v1.0.0;x' nightly ''; do
    expect_fail "fetch-upstream refuses '$bad'" "$fetch" "$bad"
done
nl='
'
expect_status "fetch-upstream refuses a name with a newline" 2 "$fetch" "v1.0.0${nl}foo"
expect_status "fetch-upstream refuses a name with a tab" 2 "$fetch" "v1.0.0	"
# stdout only: a file:// server ignores the blob filter with a warning on stderr.
if commit=$("$fetch" v1.0.0 2>/dev/null); then pass "fetch-upstream v1.0.0"; else fail "fetch-upstream v1.0.0"; fi
expect "prints the commit ID" [ "$commit" = "$(git -C "$work/upstream" rev-parse 'v1.0.0^{commit}')" ]
expect_ok "fetch-upstream v1.1.0" "$fetch" v1.1.0
expect "ref refs/upstream/tags/v1.1.0" [ "$(git rev-parse 'refs/upstream/tags/v1.1.0^{commit}')" = "$(git -C "$work/upstream" rev-parse 'v1.1.0^{commit}')" ]
expect "git tag -l shows no upstream tag" [ "$(git tag -l)" = portable-v1 ]
expect_ok "--check passes after fetch" "$setup" --check

secret_url=https://bot:TOPSECRET@mirror.example.invalid/l.git
UPSTREAM_URL=$secret_url "$setup" --check >"$work/out" 2>&1 || true
expect "--check prints the expected URL without credentials" not grep -q TOPSECRET "$work/out"
UPSTREAM_URL=$secret_url "$setup" >"$work/out" 2>&1 || true
expect "setup prints UPSTREAM_URL without credentials" not grep -q TOPSECRET "$work/out"
expect "setup prints the mirror URL" grep -q 'upstream: https://mirror.example.invalid/l.git' "$work/out"
expect_ok "setup restores the test URL" "$setup"

git remote remove origin
"$setup" >"$work/out" 2>&1 || true
expect "warns if origin is absent" grep -q "warning: remote 'origin' is absent" "$work/out"

if [ "$failures" -eq 0 ]; then echo "all tests passed"; else echo "$failures test(s) failed"; exit 1; fi
