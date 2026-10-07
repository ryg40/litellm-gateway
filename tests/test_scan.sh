#!/bin/sh
# Offline test for scripts/scan.sh, scripts/git-hooks and scripts/install-hooks.sh.
# Each section tests a scanner rule or a hook failure case.
# All repositories are temporary. The scanner image must be present
# (docker pull, see docs/secret-handling.md); no network is used.
#
# Usage: sh tests/test_scan.sh
#
# The synthetic token and the host values are built at run time from parts,
# so no tracked file contains them.

set -eu

repo_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

work=$(mktemp -d "${TMPDIR:-/tmp}/test-scan.XXXXXX")
work=$(CDPATH='' cd -- "$work" && pwd -P)
trap 'rm -rf "$work"' EXIT
# In sh, an INT or TERM handler does not end the script; exit runs the EXIT trap.
trap 'exit 130' INT TERM

# Prefix of the names of the helper containers.
SCAN_NAME_PREFIX=${SCAN_NAME_PREFIX:-litellm-test-scan-}
export SCAN_NAME_PREFIX
# Isolate Git from the global and system configuration (and its hooks).
: >"$work/gitconfig"
GIT_CONFIG_GLOBAL=$work/gitconfig GIT_CONFIG_NOSYSTEM=1
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL \
    GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
unset SCAN_REF GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE 2>/dev/null || true

failures=0
pass() { echo "ok   - $1"; }
fail() { echo "FAIL - $1"; failures=$((failures + 1)); }
# expect_status <name> <status> <command...>: output goes to $work/out.
expect_status() {
    name=$1; want=$2; shift 2
    got=0; "$@" >"$work/out" 2>&1 || got=$?
    if [ "$got" -eq "$want" ]; then pass "$name"; else fail "$name (exit $got, expected $want)"; sed 's/^/     /' "$work/out"; fi
}
expect_out() {
    name=$1; text=$2
    if grep -qF -- "$text" "$work/out"; then pass "$name"; else fail "$name (no '$text')"; sed 's/^/     /' "$work/out"; fi
}
expect_no_out() {
    name=$1; text=$2
    if grep -qF -- "$text" "$work/out"; then fail "$name"; else pass "$name"; fi
}

# Synthetic values, built from parts.
token=$(printf '%s%s%s%s' ghp _ Zx9QwEr7TyUi3OpA s5DfGh1JkLz2XcVbNm4Q)
host_ip=$(printf '%s.%s.%s' 192 168 7.42)
host_path=$(printf '/var/lib/%s' example-state)
host_name=$(printf '%s-%s' example home)
allow=$(printf 'gitleaks:%s' allow)
ip10=$(printf '%s.%s' 10 20.30.40)

# A copy of the scan, so that the untracked .local/host-values.deny of this
# checkout does not change the results. The tests write the local deny list
# of the copy ($local_deny) when they need one.
tool=$work/tool
mkdir -p "$tool/scripts"
cp "$repo_root/.gitleaks.toml" "$tool/"
cp "$repo_root/scripts/scan.sh" "$repo_root/scripts/host-values.deny" "$repo_root/scripts/host-values.regex" \
    "$tool/scripts/"
scan=$tool/scripts/scan.sh
local_deny=$tool/.local/host-values.deny
# write_local_deny <pattern>...: the local deny list of the copy.
write_local_deny() {
    mkdir -p "$tool/.local"
    { printf '# local deny list of the test\n'; printf '%s\n' "$@"; } >"$local_deny"
}
write_local_deny "$host_path" "$host_name"

g() { git -c init.defaultBranch=main "$@"; }
new_repo() {
    rm -rf "$1"; mkdir -p "$1"
    g -C "$1" init -q
    printf 'hello\n' >"$1/README"
    g -C "$1" add README
    g -C "$1" commit -q -m init
}
in_repo() { dir=$1; shift; (cd "$dir" && "$@"); }

echo "# negative control"
expect_status "selftest mode finds the synthetic token and a host value" 0 sh "$scan" selftest
mkdir "$work/nc"
printf 'api = "%s"\n' "$token" >"$work/nc/token.txt"
new_repo "$work/nc-repo"
cp "$work/nc/token.txt" "$work/nc-repo/token.txt"
g -C "$work/nc-repo" add token.txt
expect_status "a synthetic token in a temporary file gives a finding" 1 in_repo "$work/nc-repo" sh "$scan" tree
expect_out "the finding names the file, the line and the rule" "token.txt:1  rule github-pat"
expect_no_out "the output does not contain the token" "$token"

echo "# tree mode"
new_repo "$work/clean"
expect_status "clean tree: exit 0" 0 in_repo "$work/clean" sh "$scan" tree
expect_out "clean tree: summary line" "scan: 0 secret finding(s), 0 host-value finding(s)"

new_repo "$work/wt"
printf 'x = "%s"\n' "$token" >"$work/wt/README"
expect_status "unstaged change of a tracked file: finding" 1 in_repo "$work/wt" sh "$scan" tree
printf 'x = "%s"\n' "$token" >"$work/wt/untracked.txt"
g -C "$work/wt" checkout -q README
expect_status "untracked file: not scanned" 0 in_repo "$work/wt" sh "$scan" tree
g -C "$work/wt" add untracked.txt
expect_status "staged new file: finding" 1 in_repo "$work/wt" sh "$scan" tree
expect_status "staged mode: finding" 1 in_repo "$work/wt" sh "$scan" staged
expect_out "staged mode names the file" "untracked.txt:1"

echo "# host values"
new_repo "$work/host"
mkdir -p "$work/host/config"
printf 'api_base: http://%s:8080\n' "$host_ip" >"$work/host/config/a.yaml"
printf 'state: %s/x\n' "$host_path" >"$work/host/config/b.yaml"
g -C "$work/host" add config
expect_status "host value, --level warn: exit 0" 0 in_repo "$work/host" sh "$scan" --level warn tree
expect_out "host value, warn: printed" "warn  host value  config/a.yaml:1  rule host-value-private-192"
expect_out "host value, warn: local pattern printed" "config/b.yaml:1  rule host-value-local-1"
expect_status "host value, --level fail: exit 1" 1 in_repo "$work/host" sh "$scan" --level fail tree
expect_out "host value, fail: printed" "FAIL  host value  config/a.yaml:1"
expect_status "host value, default level on branch main is warn" 0 in_repo "$work/host" sh "$scan" tree
g -C "$work/host" checkout -q -b portable
expect_status "host value, default level on branch portable is fail" 1 in_repo "$work/host" sh "$scan" tree
g -C "$work/host" commit -q -m host
g -C "$work/host" checkout -q -b other
g -C "$work/host" tag portable-v1
expect_status "host value, default level with a tag portable-v* at HEAD is fail" 1 in_repo "$work/host" sh "$scan" tree
expect_status "host value, SCAN_REF=refs/heads/x sets warn" 0 in_repo "$work/host" env SCAN_REF=refs/heads/x sh "$scan" tree

echo "# deny list files"
new_repo "$work/deny"
mkdir -p "$work/deny/scripts" "$work/deny/other"
# A deny list with the patterns of the local list and an address.
{ cat "$repo_root/scripts/host-values.deny"; printf '%s\n' "$host_path" "$host_name" "$host_ip"; } \
    >"$work/deny/scripts/host-values.deny"
g -C "$work/deny" add scripts
expect_status "the tracked deny list itself is not reported" 0 in_repo "$work/deny" sh "$scan" --level fail tree
cp "$work/deny/scripts/host-values.deny" "$work/deny/other/host-values.deny"
cp "$work/deny/scripts/host-values.deny" "$work/deny/scripts/host-values.deny.bak"
g -C "$work/deny" add other scripts
expect_status "a copy of the deny list at another path is reported" 1 in_repo "$work/deny" sh "$scan" --level fail tree
expect_out "copy in other/ is reported" "other/host-values.deny:"
expect_out "copy with another name is reported" "scripts/host-values.deny.bak:"
g -C "$work/deny" rm -q --cached other/host-values.deny scripts/host-values.deny.bak
printf 'key = "%s"\n' "$token" >>"$work/deny/scripts/host-values.deny"
g -C "$work/deny" add scripts/host-values.deny
expect_status "a secret in the deny list is reported" 1 in_repo "$work/deny" sh "$scan" --level fail tree
expect_out "secret in the deny list: rule" "scripts/host-values.deny:"
g -C "$work/deny" rm -q --cached scripts/host-values.deny

echo "# local deny list"
new_repo "$work/local"
mkdir -p "$work/local/.local"
cp "$local_deny" "$work/local/.local/host-values.deny"
printf 'name: %s\n' "$host_name" >"$work/local/.local/notes.txt"
printf 'host: %s\n' "$host_name" >"$work/local/a.txt"
g -C "$work/local" add a.txt
expect_status "a pattern of the local deny list gives a finding" 1 in_repo "$work/local" sh "$scan" --level fail tree
expect_out "local pattern: rule" "FAIL  host value  a.txt:1  rule host-value-local-2"
expect_no_out "the untracked local deny list is not reported" ".local/host-values.deny"
expect_no_out "an untracked file in .local/ is not scanned" ".local/notes.txt"
expect_status "local pattern, staged: finding" 1 in_repo "$work/local" sh "$scan" --level fail staged
expect_out "local pattern, staged: rule" "a.txt:1  rule host-value-local-2"
g -C "$work/local" rm -q --cached a.txt
expect_status "the local deny list in a local-dev style checkout: clean" 0 in_repo "$work/local" sh "$scan" --level fail tree
g -C "$work/local" add -f .local/host-values.deny
expect_status "a tracked local deny list: policy finding" 1 in_repo "$work/local" sh "$scan" --level fail tree
expect_out "tracked local deny list: policy rule" "FAIL  policy  .local/host-values.deny  rule policy-local-file"
expect_no_out "tracked local deny list: no host-value finding in it" "host value  .local/host-values.deny"
expect_status "a tracked local deny list, staged: policy finding" 1 in_repo "$work/local" sh "$scan" staged
expect_out "tracked local deny list, staged: policy rule" "FAIL  policy  .local/host-values.deny  rule policy-local-file"
g -C "$work/local" rm -q --cached .local/host-values.deny
mv "$local_deny" "$work/local-deny.saved"
printf 'host: %s\n' "$host_name" >"$work/local/a.txt"
printf 'api_base: http://%s:8080\n' "$ip10" >"$work/local/b.txt"
g -C "$work/local" add a.txt b.txt
expect_status "no local deny list: the scan runs" 1 in_repo "$work/local" sh "$scan" --level fail tree
expect_no_out "no local deny list: no local rule" "host-value-local-"
expect_out "no local deny list: the regex list works alone" "b.txt:1  rule host-value-private-10"
expect_status "no local deny list: selftest" 0 sh "$scan" selftest
# A tracked deny list with no pattern and a regex list with no rule: no rule at all.
cp "$tool/scripts/host-values.regex" "$work/regex.saved"
grep '^#' "$work/regex.saved" >"$tool/scripts/host-values.regex"
expect_status "no host-value rule at all: usage error" 2 sh "$scan" selftest
expect_out "no host-value rule: message" "no host-value rule in"
write_local_deny "$host_name"
expect_status "only a local pattern and no regex rule: selftest" 0 sh "$scan" selftest
cp "$work/regex.saved" "$tool/scripts/host-values.regex"
mv "$work/local-deny.saved" "$local_deny"
printf 'bad"pattern\n' >>"$local_deny"
expect_status "a quote in the local deny list: usage error" 2 sh "$scan" selftest
expect_out "a quote in the local deny list: message" ".local/host-values.deny: a pattern contains a quote"
write_local_deny "$host_path" "$host_name"

echo "# history mode"
new_repo "$work/hist"
printf 'x = "%s"\n' "$token" >"$work/hist/old.txt"
g -C "$work/hist" add old.txt
g -C "$work/hist" commit -q -m "add token"
old_commit=$(g -C "$work/hist" rev-parse --short=12 HEAD)
g -C "$work/hist" rm -q old.txt
g -C "$work/hist" commit -q -m "remove token"
expect_status "history: tree is clean now" 0 in_repo "$work/hist" sh "$scan" tree
expect_status "history: token in an old commit gives a finding" 1 in_repo "$work/hist" sh "$scan" history
expect_out "history: the finding names the commit" "old.txt:1  rule github-pat  commit $old_commit"
expect_no_out "history: the output does not contain the token" "$token"
expect_status "history: a range without the commit is clean" 0 in_repo "$work/hist" sh "$scan" history HEAD~1..HEAD
expect_status "all: finds the old commit" 1 in_repo "$work/hist" sh "$scan" all

echo "# content does not switch the scan off"
new_repo "$work/allow"
printf 'x = "%s" # %s\napi_base: http://%s:8080 # %s\n' "$token" "$allow" "$host_ip" "$allow" >"$work/allow/a.txt"
g -C "$work/allow" add a.txt
expect_status "gitleaks:allow, tree: finding" 1 in_repo "$work/allow" sh "$scan" --level fail tree
expect_out "gitleaks:allow does not hide a secret" "a.txt:1  rule github-pat"
expect_out "gitleaks:allow does not hide a host value" "a.txt:2  rule host-value-private-192"
expect_status "gitleaks:allow, staged: finding" 1 in_repo "$work/allow" sh "$scan" staged
expect_out "gitleaks:allow, staged: the secret" "a.txt:1  rule github-pat"
g -C "$work/allow" commit -q -m allow
allow_commit=$(g -C "$work/allow" rev-parse HEAD)
# A tracked .gitleaksignore with the fingerprint of the finding.
printf '%s:a.txt:github-pat:1\n' "$allow_commit" >"$work/allow/.gitleaksignore"
mkdir -p "$work/allow/sub"
printf '{}\n' >"$work/allow/sub/.gitleaksbaseline"
g -C "$work/allow" add .gitleaksignore sub
g -C "$work/allow" commit -q -m ignore
expect_status ".gitleaksignore, history: finding" 1 in_repo "$work/allow" sh "$scan" history
expect_out ".gitleaksignore, history: the secret is still found" "a.txt:1  rule github-pat  commit $(printf '%.12s' "$allow_commit")"
expect_out ".gitleaksignore, history: policy finding" "FAIL  policy  .gitleaksignore  rule policy-ignore-file"
expect_out ".gitleaksbaseline, history: policy finding" "sub/.gitleaksbaseline  rule policy-ignore-file"
expect_status ".gitleaksignore, tree: finding" 1 in_repo "$work/allow" sh "$scan" tree
expect_out ".gitleaksignore, tree: policy finding" "FAIL  policy  .gitleaksignore  rule policy-ignore-file"
expect_out ".gitleaksbaseline, tree: policy finding" "FAIL  policy  sub/.gitleaksbaseline  rule policy-ignore-file"
new_repo "$work/ign"
printf 'x = "%s"\n' "$token" >"$work/ign/a.txt"
printf 'a.txt:github-pat:1\n' >"$work/ign/.gitleaksignore"
mkdir -p "$work/ign/sub"
printf '{}\n' >"$work/ign/sub/.gitleaksbaseline"
g -C "$work/ign" add a.txt sub
expect_status "untracked .gitleaksignore, staged .gitleaksbaseline, staged: finding" 1 in_repo "$work/ign" sh "$scan" staged
expect_out "untracked .gitleaksignore does not hide a staged secret" "a.txt:1  rule github-pat"
expect_out ".gitleaksbaseline, staged: policy finding" "FAIL  policy  sub/.gitleaksbaseline  rule policy-ignore-file"
g -C "$work/ign" add .gitleaksignore
expect_status "staged .gitleaksignore, staged: finding" 1 in_repo "$work/ign" sh "$scan" staged
expect_out ".gitleaksignore, staged: policy finding" "FAIL  policy  .gitleaksignore  rule policy-ignore-file"
g -C "$work/ign" commit -q -m baseline
expect_status ".gitleaksbaseline in a commit, all: finding" 1 in_repo "$work/ign" sh "$scan" all
expect_out ".gitleaksbaseline, all: policy finding with the commit" "sub/.gitleaksbaseline  rule policy-ignore-file (gitleaks ignore or baseline file)  commit"

echo "# archives, dumps, bundles, messages, names, symlinks"
new_repo "$work/arc"
mkdir -p "$work/arc-src"
printf 'x = "%s"\n' "$token" >"$work/arc-src/t.txt"
gzip -c "$work/arc-src/t.txt" >"$work/arc/t.txt.gz"
(cd "$work/arc-src" && python3 -m zipfile -c "$work/arc/t.zip" t.txt && tar -cf "$work/arc/t.tar" t.txt)
# Content only: a gzip file with another name, a Git bundle, a pg_dump text dump.
cp "$work/arc/t.txt.gz" "$work/arc/data.bin"
g -C "$work/arc" bundle create -q "$work/arc/repo.txt" HEAD 2>/dev/null
printf -- '--\n-- %s database dump\n--\n' PostgreSQL >"$work/arc/notes.txt"
ln -s "$token" "$work/arc/link"
printf 'ok\n' >"$work/arc/$token.txt"
g -C "$work/arc" add .
expect_status "archives and more, tree: finding" 1 in_repo "$work/arc" sh "$scan" tree
for f in t.txt.gz t.zip t.tar data.bin repo.txt notes.txt; do
    expect_out "tree: policy finding for $f" "FAIL  policy  $f  rule policy-archive"
done
expect_out "tree: token inside the gzip file" "t.txt.gz:1  rule github-pat"
expect_out "tree: token inside the zip file" "t.zip!t.txt:1  rule github-pat"
expect_out "tree: token inside the tar file" "t.tar!t.txt:1  rule github-pat"
expect_out "tree: symlink target text" "FAIL  secret  link:1  rule github-pat"
expect_out "tree: file name" "FAIL  secret  (file names):"
expect_no_out "tree: the output does not contain the token" "$token"
expect_status "archives and more, staged: finding" 1 in_repo "$work/arc" sh "$scan" staged
expect_out "staged: policy finding for a zip file" "FAIL  policy  t.zip  rule policy-archive"
expect_out "staged: policy finding for a bundle by content" "FAIL  policy  repo.txt  rule policy-archive"
expect_out "staged: file name" "FAIL  secret  (file names):"
g -C "$work/arc" commit -q -m archives
arc_commit=$(g -C "$work/arc" rev-parse --short=12 HEAD)
expect_status "archives and more, history: finding" 1 in_repo "$work/arc" sh "$scan" history
expect_out "history: policy finding for a tar file" "FAIL  policy  t.tar  rule policy-archive (archive, database dump or Git bundle)  commit $arc_commit"
expect_out "history: policy finding for a dump by content" "FAIL  policy  notes.txt  rule policy-archive"
expect_out "history: file name" "FAIL  secret  (file names):"
expect_out "history: symlink target text" "FAIL  secret  link:1  rule github-pat  commit $arc_commit"
new_repo "$work/msg"
printf 'x\n' >"$work/msg/x.txt"
g -C "$work/msg" add x.txt
g -C "$work/msg" commit -q -m "add x" -m "key $token" -m "host $host_ip"
msg_commit=$(g -C "$work/msg" rev-parse --short=12 HEAD)
expect_status "commit message: tree is clean" 0 in_repo "$work/msg" sh "$scan" tree
expect_status "commit message, history: finding" 1 in_repo "$work/msg" sh "$scan" history
expect_out "history: token in the commit message" "FAIL  secret  (commit message):3  rule github-pat  commit $msg_commit"
expect_out "history: host value in the commit message" "(commit message):5  rule host-value-private-192"
expect_no_out "history: the output does not contain the token" "$token"
expect_status "commit message, all: finding" 1 in_repo "$work/msg" sh "$scan" all
expect_out "all: token in the commit message" "(commit message):3  rule github-pat  commit $msg_commit"
# .gitignore of this repository ignores the extensions.
for f in x.tar.gz x.zip x.tar x.bundle x.sql x.dump x.db x.7z; do
    expect_status ".gitignore ignores $f" 0 git -C "$repo_root" check-ignore -q --no-index "$f"
done

echo "# the true|false|null allowlist is anchored"
new_repo "$work/anchor"
printf 'password = "%s%s"\ntoken = "%s%s%s"\n' false Passw0rdX9kQ ghp_ falseZx9QwEr7TyUi3O pAs5DfGh1JkLz2XcV >"$work/anchor/a.txt"
g -C "$work/anchor" add a.txt
expect_status "a secret that contains false: finding" 1 in_repo "$work/anchor" sh "$scan" tree
expect_out "password with false: finding" "a.txt:1  rule generic-api-key"
expect_out "token with false: finding" "a.txt:2  rule github-pat"

echo "# a path with a blank, a comma and a colon"
odd="$work/odd dir,x:y"
new_repo "$odd/repo"
printf 'x = "%s"\n' "$token" >"$odd/repo/leak.txt"
g -C "$odd/repo" add leak.txt
expect_status "odd path, staged: finding" 1 in_repo "$odd/repo" sh "$scan" staged
expect_out "odd path, staged: the file" "leak.txt:1  rule github-pat"
g -C "$odd/repo" commit -q -m leak
expect_status "odd path, history: finding" 1 in_repo "$odd/repo" sh "$scan" history
expect_out "odd path, history: the file" "leak.txt:1  rule github-pat"
g -C "$odd/repo" worktree add -q "$odd/wt" -b wt 2>/dev/null
expect_status "odd path, history in a worktree: finding" 1 in_repo "$odd/wt" sh "$scan" history
expect_status "odd path, TMPDIR with a blank: tree finding" 1 in_repo "$odd/repo" env TMPDIR="$odd" sh "$scan" tree

echo "# private address ranges"
new_repo "$work/ranges"
{
    printf 'a = http://%s:80/\n' "$ip10"
    printf 'b = %s.%s\n' 172 20.1.2
    printf 'c = %s.%s\n' 192 168.1.2
    printf 'd = %s.%s\n' 100 100.1.2
    printf 'e = [%s::1]\n' fd12:3456
    printf 'f = host=%s.%s\n' 10 1.2.3
} >"$work/ranges/hosts.txt"
v=$(printf '%s.%s' 10 2.3.4)
{
    printf 'pkg==%s\npkg>=%s\nimage: repo:%s\nv%s\n%s.dev0\npkg-%s.tar\n' "$v" "$v" "$v" "$v" "$v" "$v"
    printf '%s.0.0.1 %s.0.2.1 %s.51.100.1 %s.0.113.1 %s:db8::1\n' 127 192 198 203 2001
    printf '%s.%s %s.%s %s.%s %s.%s\n' 172 15.1.1 172 32.1.1 100 63.1.1 100 128.1.1
} >"$work/ranges/clean.txt"
g -C "$work/ranges" add .
expect_status "private ranges, --level fail: finding" 1 in_repo "$work/ranges" sh "$scan" --level fail tree
expect_out "10/8" "hosts.txt:1  rule host-value-private-10"
expect_out "172.16/12" "hosts.txt:2  rule host-value-private-172"
expect_out "192.168/16" "hosts.txt:3  rule host-value-private-192"
expect_out "100.64/10" "hosts.txt:4  rule host-value-shared-100"
expect_out "IPv6 unique local" "hosts.txt:5  rule host-value-private-ula"
expect_out "10/8 after =" "hosts.txt:6  rule host-value-private-10"
expect_no_out "no finding for a version pin, loopback, documentation and public addresses" "clean.txt"

echo "# hooks in a temporary clone"
g init -q --bare "$work/remote.git"
g clone -q "$work/remote.git" "$work/clone" 2>/dev/null
c=$work/clone
mkdir -p "$c/scripts/git-hooks"
cp "$repo_root/.gitleaks.toml" "$c/"
cp "$repo_root/scripts/scan.sh" "$repo_root/scripts/host-values.deny" "$repo_root/scripts/host-values.regex" \
    "$repo_root/scripts/install-hooks.sh" "$c/scripts/"
cp "$repo_root/scripts/git-hooks/"* "$c/scripts/git-hooks/"
g -C "$c" add .
g -C "$c" commit -q -m "add scan"
# The untracked local deny list of the clone (.gitignore of the clone is empty).
mkdir -p "$c/.local"
cp "$local_deny" "$c/.local/host-values.deny"
printf '.local/\n' >>"$c/.git/info/exclude"
expect_status "install-hooks.sh --check before install: exit 1" 1 in_repo "$c" sh scripts/install-hooks.sh --check
expect_status "install-hooks.sh installs" 0 in_repo "$c" sh scripts/install-hooks.sh
expect_status "install-hooks.sh --check after install: exit 0" 0 in_repo "$c" sh scripts/install-hooks.sh --check
g -C "$c" push -q origin main >/dev/null 2>&1
before=$(g -C "$c" rev-parse HEAD)
printf 'x = "%s"\n' "$token" >"$c/leak.txt"
g -C "$c" add leak.txt
expect_status "pre-commit: a commit with a token is refused" 1 g -C "$c" commit -q -m leak
expect_out "pre-commit: message" "pre-commit: commit refused"
expect_status "pre-commit: HEAD is unchanged" 0 test "$(g -C "$c" rev-parse HEAD)" = "$before"
g -C "$c" commit -q --no-verify -m "leak without hook"
expect_status "pre-push: a push with a token is refused" 1 g -C "$c" push -q origin main
expect_out "pre-push: message" "pre-push: push refused"
expect_status "pre-push: the remote branch is unchanged" 0 test "$(g -C "$work/remote.git" rev-parse main)" = "$before"

# Continue on a branch without the leak commit.
g -C "$c" checkout -q -b clean "$before"
printf 'api_base: http://%s\n' "$host_ip" >"$c/host.yaml"
g -C "$c" add host.yaml
expect_status "pre-commit: a host value on another branch is a warning" 0 g -C "$c" commit -q -m host
expect_status "pre-push: a host value to branch main is a warning" 0 g -C "$c" push -q origin clean:main
expect_status "pre-push: a host value to branch portable is refused" 1 g -C "$c" push -q origin clean:portable
printf 'name: %s\n' "$host_name" >"$c/name.txt"
g -C "$c" add name.txt
expect_status "pre-commit: a local pattern on another branch is a warning" 0 g -C "$c" commit -q -m name
expect_out "pre-commit: the local rule is printed" "warn  host value  name.txt:1  rule host-value-local-2"
expect_status "pre-push: a local pattern to branch portable is refused" 1 g -C "$c" push -q origin clean:portable
expect_out "pre-push: the local rule refuses the push" "FAIL  host value  name.txt:1  rule host-value-local-2"
g -C "$c" tag portable-v1
expect_status "pre-push: a host value to a tag portable-v* is refused" 1 g -C "$c" push -q origin portable-v1
# shellcheck disable=SC2016 # the inner sh expands $1
expect_status "pre-push: a new branch with a new clean commit is accepted" 0 sh -c '
    cd "$1" && git checkout -q -b feature && printf "ok\n" >ok.txt && git add ok.txt &&
    git commit -q -m ok && git push -q origin feature' sh "$c"

g -C "$c" worktree add -q "$work/clone-wt" -b wtbranch 2>/dev/null
printf 'x = "%s"\n' "$token" >"$work/clone-wt/leak.txt"
g -C "$work/clone-wt" add leak.txt
expect_status "pre-commit in a worktree: a commit with a token is refused" 1 g -C "$work/clone-wt" commit -q -m leak
g -C "$work/clone-wt" commit -q --no-verify -m leak
expect_status "pre-push in a worktree: a push with a token is refused" 1 g -C "$work/clone-wt" push -q origin wtbranch

echo "# hooks fail closed, pre-merge-commit, a branch without the scan"
g -C "$c" checkout -q -f clean
rm "$c/scripts/scan.sh"
printf 'ok\n' >"$c/f1.txt"
g -C "$c" add f1.txt
expect_status "pre-commit: scan.sh gone from the working tree, in the index: refused" 1 g -C "$c" commit -q -m f1
expect_out "pre-commit: fail closed message" "scripts/scan.sh is not in the working tree, but it is in the index or in HEAD"
g -C "$c" rm -q --cached scripts/scan.sh
expect_status "pre-commit: the commit that deletes scan.sh: refused" 1 g -C "$c" commit -q -m "remove scan"
expect_status "pre-push: scan.sh gone from the working tree: refused" 1 g -C "$c" push -q origin clean:main
g -C "$c" restore --staged scripts/scan.sh
g -C "$c" checkout -q -- scripts/scan.sh
rm -r "$c/scripts/git-hooks"
expect_status "dispatcher: hooks gone from the working tree: refused" 1 g -C "$c" commit -q -m f1
expect_out "dispatcher: fail closed message" "is not in the working tree, but"
g -C "$c" checkout -q -- scripts/git-hooks
expect_status "pre-commit: the restored hooks accept a clean commit" 0 g -C "$c" commit -q -m f1
# git commit -a and git commit <path> use a temporary index (GIT_INDEX_FILE).
printf 'x = "%s"\n' "$token" >"$c/f1.txt"
expect_status "pre-commit: git commit -a with a token is refused" 1 g -C "$c" commit -q -a -m leak
expect_status "pre-commit: git commit <path> with a token is refused" 1 g -C "$c" commit -q -m leak f1.txt
g -C "$c" checkout -q -- f1.txt
# pre-merge-commit: a merge without a conflict brings a token.
g -C "$c" checkout -q -b leaky
printf 'x = "%s"\n' "$token" >"$c/merge-leak.txt"
g -C "$c" add merge-leak.txt
g -C "$c" commit -q --no-verify -m "leak for merge"
g -C "$c" checkout -q clean
before=$(g -C "$c" rev-parse HEAD)
expect_status "pre-merge-commit: a merge with a token is refused" 1 g -C "$c" merge -q --no-ff -m merge leaky
expect_out "pre-merge-commit: message" "pre-merge-commit: commit refused"
g -C "$c" merge --abort 2>/dev/null || true
expect_status "pre-merge-commit: HEAD is unchanged" 0 test "$(g -C "$c" rev-parse HEAD)" = "$before"
g -C "$c" checkout -q -b clean-side
printf 'ok\n' >"$c/side.txt"
g -C "$c" add side.txt
g -C "$c" commit -q -m side
g -C "$c" checkout -q clean
expect_status "pre-merge-commit: a clean merge is accepted" 0 g -C "$c" merge -q --no-ff -m merge clean-side
# A branch that never had the scan, like main of this repository.
g -C "$c" checkout -q --orphan plain
g -C "$c" rm -rq --cached .
rm -rf "$c/scripts" "$c/.gitleaks.toml"
printf 'plain\n' >"$c/plain.txt"
g -C "$c" add plain.txt
expect_status "a branch without the scan: commit accepted" 0 g -C "$c" commit -q -m plain
expect_out "a branch without the scan: the reason is printed" "pre-commit: this branch has no scripts/git-hooks/pre-commit and no scripts/scan.sh; no scan"
g -C "$c" checkout -q -f clean

expect_status "install-hooks.sh --uninstall" 0 in_repo "$c" sh scripts/install-hooks.sh --uninstall
expect_status "core.hooksPath is removed" 1 g -C "$c" config --get core.hooksPath
common=$(g -C "$c" rev-parse --git-common-dir)
case $common in
    /*) ;;
    *) common=$c/$common ;;
esac
expect_status "the dispatcher copies are removed" 1 test -e "$common/scan-hooks"

echo
if [ "$failures" -eq 0 ]; then
    echo "all tests passed"
else
    echo "$failures test(s) failed"
    exit 1
fi
