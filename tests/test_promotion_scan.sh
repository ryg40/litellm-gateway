#!/bin/sh
# Real-scanner integration: scratch clone, clean fixture, local bare remote only.
# Needs the scanner engine described in docs/secret-handling.md.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/promotion-scan-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM
HOME=$tmp GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid
GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid
SCAN_NAME_PREFIX=${SCAN_NAME_PREFIX:-litellm-test-}
export HOME GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL SCAN_NAME_PREFIX
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
# --no-local copies objects without hard links. No original remote is contacted.
git clone -q --no-local --no-checkout --single-branch "$repo" "$tmp/r"
cd "$tmp/r"
git symbolic-ref HEAD refs/heads/local-dev
git read-tree --empty
mkdir -p scripts .local
for file in promote.sh public_check.sh public-patterns.tsv public-allow.tsv scan.sh host-values.deny host-values.regex install-hooks.sh; do
    cp "$repo/scripts/$file" scripts/
done
cp "$repo/.gitleaks.toml" .
mkdir scripts/git-hooks
cp "$repo/scripts/git-hooks/dispatch" "$repo/scripts/git-hooks/pre-push" scripts/git-hooks/
printf '.local/\n' >.gitignore
printf 'synthetic-host-only\n' >.local/host-values.deny
printf 'Release example.\n' >"$tmp/message"
git add . && git commit -qm fixture
git init -q --bare "$tmp/remote.git"
git remote add shared "$tmp/remote.git"
# No real GitHub operation can occur: only its URL resolution is simulated.
real_git=$(command -v git)
export REAL_GIT=$real_git TEST_REMOTE=$tmp/remote.git
mkdir "$tmp/bin"
cat >"$tmp/bin/git" <<'SH'
#!/bin/sh
exec python3 -c '
import os
import sys
args = sys.argv[1:]
for i, arg in enumerate(args):
    if arg == "https://github.com/example/gateway.git" and ("ls-remote" in args or "push" in args) and "--get-url" not in args:
        args[i] = os.environ["TEST_REMOTE"]
    elif "://" in arg and ("ls-remote" in args or "push" in args) and "--get-url" not in args:
        sys.exit("network disabled in tests")
os.execv(os.environ["REAL_GIT"], ["git", *args])
' "$@"
SH
cat >"$tmp/bin/gh" <<'SH'
#!/bin/sh
printf '{"isPrivate":false}\n'
SH
chmod +x "$tmp/bin/git" "$tmp/bin/gh"
PATH=$tmp/bin:$PATH; export PATH
count=0
expect() {
    label=$1 wanted=$2; shift 2
    status=0
    "$@" >"$tmp/next-out" 2>&1 || status=$?
    mv "$tmp/next-out" "$tmp/out"
    if [ "$status" != "$wanted" ]; then
        printf 'FAIL %s: expected %s, got %s\n' "$label" "$wanted" "$status"
        cat "$tmp/out"; exit 1
    fi
    reason=
    case $label in
        'dirty tree') reason='dirty tree' ;;
        'personal identity') reason='personal identity needs --accept-identity' ;;
        'foreign remote ref') reason='remote contains an undocumented ref' ;;
        'public remote without acceptance') reason='--push needs --public-remote' ;;
        'real host rule checks tag text') reason='secret/host scan failed' ;;
        'real secret rule checks tag text') reason='secret/host scan failed' ;;
        'personal outgoing snapshot refused') reason='FAIL policy identity' ;;
    esac
    if [ -n "$reason" ] && ! grep -qF -- "$reason" "$tmp/out"; then
        echo "FAIL $label: wrong refusal reason"; cat "$tmp/out"; exit 1
    fi
    count=$((count + 1)); printf 'ok %s (exit %s)\n' "$label" "$status"
    # Show safe plan/result lines for the integration report.
    grep -E 'plan: (history|visibility)|promote: (refused|dry run)|public-check:' "$tmp/out" || :
}
run() { sh scripts/promote.sh --remote shared --version "$1" --message-file "$tmp/message" ${2+"$2"}; }
expect 'first snapshot' 0 run 0.1.0 --apply
expect 'chain dry run' 0 run 0.2.0
expect 'new-root dry run' 0 run 0.2.0 --new-root
printf 'dirty\n' >untracked
expect 'dirty tree' 1 run 0.2.0
rm untracked
expect 'personal identity' 1 env PORTABLE_PUBLISH_EMAIL=person@example.com sh scripts/promote.sh --remote shared --version 0.2.0 --message-file "$tmp/message"
# A bare remote gets fixture objects without a push of a real repository ref.
git --git-dir="$tmp/remote.git" fetch -q "$tmp/r" portable
git --git-dir="$tmp/remote.git" update-ref refs/heads/foreign "$(git rev-parse portable)"
expect 'foreign remote ref' 1 run 0.2.0
git --git-dir="$tmp/remote.git" update-ref -d refs/heads/foreign
git remote set-url shared https://github.com/example/gateway.git
expect 'public remote without acceptance' 1 run 0.2.0 --push
git remote set-url shared "$tmp/remote.git"
printf 'synthetic-host-only\n' >"$tmp/message"
expect 'real host rule checks tag text' 1 run 0.2.0
expect 'host value stays hidden' 1 grep -F synthetic-host-only "$tmp/out"
printf 'token = "%s%s%s%s"\n' ghp _ Zx9QwEr7TyUi3OpA s5DfGh1JkLz2XcVbNm4Q >"$tmp/message"
expect 'real secret rule checks tag text' 1 run 0.2.0
printf 'Release example.\n' >"$tmp/message"
expect 'public selftest with real deny and scanner' 0 sh scripts/public_check.sh selftest
# Both decoded literal values and decoded secret tokens must reach the real checks.
for encoding in utf-16-le utf-16-be; do
    for kind in literal secret; do
        python3 - "$encoding" "$kind" <<'PY'
from pathlib import Path
import sys
s = 'synthetic-host-only' if sys.argv[2] == 'literal' else 'token = "' + 'ghp' + '_' + 'Zx9QwEr7TyUi3OpA' + 's5DfGh1JkLz2XcVbNm4Q' + '"'
bom = b'\xff\xfe' if sys.argv[1].endswith('le') else b'\xfe\xff'
Path('encoded.txt').write_bytes(bom + s.encode(sys.argv[1]))
PY
        git add encoded.txt
        expect 'real scanner encoded refusal' 1 sh scripts/public_check.sh
        if [ "$kind" = secret ]; then
            grep -qF 'secret/host scan failed' "$tmp/out"
        else
            # The scanner also sees the decoded literal payload.
            grep -qF 'secret/host scan failed' "$tmp/out"
        fi
        git rm -q --cached encoded.txt; rm encoded.txt
    done
done
# An actual push invokes the installed dispatcher and real history scanner.
sh scripts/install-hooks.sh >"$tmp/install-log"
expect 'real installed hook push' 0 sh scripts/promote.sh --remote shared --version 0.2.0 --message-file "$tmp/message" --push --public-remote
# A first chain push must not send unchecked old metadata.
git init -q --bare "$tmp/empty.git"
git remote set-url shared "$tmp/empty.git"
old=$(GIT_AUTHOR_EMAIL=person@example.com GIT_COMMITTER_EMAIL=person@example.com git commit-tree 'HEAD^{tree}' -m old)
git update-ref refs/heads/portable "$old"
expect 'personal outgoing snapshot refused' 1 run 0.3.0
printf 'all %s promotion scanner checks passed\n' "$count"
