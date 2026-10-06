#!/bin/sh
# Make an isolated snapshot and one release tag. Dry run unless --apply or --push.
# No image build or service change. See docs/branches.md.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
usage() {
    echo 'usage: scripts/promote.sh --version X.Y.Z --message-file FILE [--remote NAME]'
    echo '       [--apply|--push] [--new-root] [--new-repository] [--public-remote]'
    echo '       [--name NAME] [--email EMAIL] [--accept-identity]'
}
refuse() { echo "promote: refused: $*" >&2; exit 1; }
error() { echo "promote: error: $*" >&2; exit 2; }
public_check() {
    result=0
    sh "$root/scripts/public_check.sh" "$@" || result=$?
    case $result in
        0) ;; 1) refuse 'public check failed' ;; *) error 'public check tool failed' ;;
    esac
}
version= message= remote=github apply=0 push=0 new_root=0 new_repo=0 public=0 accept=0
name=${PORTABLE_PUBLISH_NAME:-litellm-gateway portable}
email=${PORTABLE_PUBLISH_EMAIL:-portable@litellm-gateway.invalid}
while [ $# -gt 0 ]; do
    case $1 in
        --version|--message-file|--remote|--name|--email)
            [ $# -ge 2 ] || { usage >&2; exit 2; }
            case $1 in
                --version) version=$2 ;; --message-file) message=$2 ;; --remote) remote=$2 ;;
                --name) name=$2 ;; --email) email=$2 ;;
            esac
            shift 2 ;;
        --apply) apply=1; shift ;; --push) apply=1; push=1; shift ;;
        --new-root) new_root=1; shift ;; --new-repository) new_repo=1; shift ;;
        --public-remote) public=1; shift ;; --accept-identity) accept=1; shift ;;
        -h|--help) usage; exit 0 ;; *) usage >&2; exit 2 ;;
    esac
done
[ -n "$version" ] && [ -s "$message" ] || { usage >&2; exit 2; }
case $remote in ''|-*|*[!a-zA-Z0-9_.-]*) error 'invalid remote name' ;; upstream) refuse 'upstream is fetch-only' ;; esac
[ "$new_repo" = 0 ] || [ "$new_root" = 1 ] || error '--new-repository requires --new-root'
command -v python3 >/dev/null 2>&1 || error 'python3 is required'
# Resolve the message before changing directories; freeze its bytes for this run.
work=$(mktemp -d "${TMPDIR:-/tmp}/promote.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM
cp "$message" "$work/tag-message" || error 'cannot read message'
cd "$root"
[ -z "$(git status --porcelain --untracked-files=all)" ] || refuse 'dirty tree'
branch=$(git symbolic-ref -q HEAD || :)
[ "$branch" != refs/heads/portable ] || refuse 'portable is checked out'
# Ref updates must not invalidate another checkout either.
if git worktree list --porcelain | grep -q '^branch refs/heads/portable$'; then
    refuse 'portable is checked out in another worktree'
fi
common=$(git rev-parse --git-common-dir)
common=$(CDPATH='' cd -- "$common" && pwd -P)
[ ! -s "$common/shallow" ] || refuse 'shallow repository'
if git config --get core.hooksPath >/dev/null 2>&1; then
    sh scripts/install-hooks.sh --check >"$work/hooks-check" 2>&1 ||
        refuse 'foreign or stale core.hooksPath; install current repository hooks'
    git config --get-all core.hooksPath >"$work/hook-paths"
    while IFS= read -r hook_path; do
        [ "$hook_path" = "$common/scan-hooks" ] ||
            refuse 'foreign or stale core.hooksPath; install current repository hooks'
    done <"$work/hook-paths"
fi
# Never change refs from a primary checkout with runtime files.
if [ "$common" = "$root/.git" ] && { [ -e .env ] || [ -d secrets ] || [ -d state ] || [ -d data ]; }; then
    refuse 'primary checkout has runtime files; use a separate clean clone'
fi
# A primary checkout with linked worktrees is the integration checkout, not a release clone.
if [ "$common" = "$root/.git" ] && [ "$(git worktree list --porcelain | grep -c '^worktree ')" -gt 1 ]; then
    refuse 'primary checkout has linked worktrees; use a separate clean clone'
fi
[ -f .local/host-values.deny ] || refuse 'local deny list is absent'
awk 'NF && $0 !~ /^[[:space:]]*#/ {found=1} END {exit !found}' .local/host-values.deny ||
    refuse 'local deny list is empty'
rev=$(git rev-parse --verify 'refs/heads/local-dev^{commit}') || error 'local-dev is missing'
[ "$(git rev-parse HEAD)" = "$rev" ] || refuse 'HEAD differs from local-dev'
old=$(git rev-parse --verify --quiet refs/heads/portable || :)
tag=portable-v$version
git show-ref --verify --quiet "refs/tags/$tag" && refuse 'tag already exists'
# Inspect effective push URLs, including insteadOf expansion, without echoing them.
git remote get-url --push --all "$remote" >"$work/urls" 2>/dev/null || error 'remote is missing'
python3 - "$work/urls" "$work/url" "$work/github" <<'PY' || exit 1
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
urls = Path(sys.argv[1]).read_text().splitlines()
if len(urls) != 1:
    sys.exit('promote: refused: remote needs exactly one push URL')
u = urls[0]
if u == 'DISABLED' or '@' in u or any(ord(c) < 33 for c in u) or '?' in u or '#' in u or u.startswith('-'):
    sys.exit('promote: refused: disabled or unsafe push URL')
try:
    p = urlsplit(u)
    port = p.port
except ValueError:
    sys.exit('promote: refused: invalid push URL')
if p.password is not None or p.username is not None:
    sys.exit('promote: refused: user information in push URL')
if p.scheme not in ('', 'file', 'https', 'ssh') or (not p.scheme and ':' in u):
    sys.exit('promote: refused: unsupported push URL')
if p.scheme == 'https' and p.hostname == 'github.com' and port not in (None, 443):
    sys.exit('promote: refused: nonstandard GitHub HTTPS port')
if p.scheme in ('https', 'ssh') and not p.hostname:
    sys.exit('promote: refused: URL has no host')
Path(sys.argv[2]).write_text(u)
if p.hostname == 'github.com':
    repo = p.path.strip('/').removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        sys.exit('promote: refused: invalid GitHub path')
    Path(sys.argv[3]).write_text(repo)
PY
url=$(cat "$work/url")
resolved=$(git ls-remote --get-url "$url" 2>"$work/network-error") || error 'cannot resolve push URL'
[ "$resolved" = "$url" ] || refuse 'push URL rewrites a second time'
# Older Git needs a locally configured probe remote; copy only effective URL rules.
resolved=$(python3 - "$work/push-probe" "$url" 2>"$work/network-error" <<'PY'
import os
import subprocess
import sys
config = subprocess.check_output(['git', 'config', '--null', '--list'])
env = os.environ.copy()
for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_CONFIG',
            'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
    env.pop(key, None)
env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
subprocess.run(['git', 'init', '-q', '--bare', sys.argv[1]], env=env, check=True)
cmd = ['git', '--git-dir=' + sys.argv[1]]
for record in config.split(b'\0'):
    key, _, value = record.partition(b'\n')
    if key.startswith(b'url.') and key.endswith((b'.insteadof', b'.pushinsteadof')):
        subprocess.run(cmd + ['config', '--add', os.fsdecode(key), os.fsdecode(value)], env=env, check=True)
subprocess.run(cmd + ['config', 'remote.chk.url', sys.argv[2]], env=env, check=True)
subprocess.run(cmd + ['remote', 'get-url', '--push', 'chk'], env=env, check=True)
PY
) || error 'cannot resolve push URL'
[ "$resolved" = "$url" ] || refuse 'push URL rewrites a second time'
[ "$(git config --bool --get "remote.$remote.mirror" || :)" != true ] || refuse 'mirror remote'
# Use the inspected URL for every network operation, not an unrelated fetch URL.
remote_refs() {
    git ls-remote "$url" >"$work/refs" 2>"$work/network-error" || error 'cannot list remote refs'
    if ! awk '$2 == "HEAD" || $2 == "refs/heads/portable" || $2 ~ /^refs\/tags\/portable-v/ {next}
        {bad=1} END {exit bad}' "$work/refs"; then
        refuse 'remote contains an undocumented ref; nothing is deleted'
    fi
    if [ "${1:-}" = print ]; then cat "$work/refs"; fi
}
remote_refs
remote_old=$(awk '$2 == "refs/heads/portable" {print $1}' "$work/refs")
if [ "$new_root" = 0 ] && [ -z "$old" ]; then
    if [ -n "$remote_old" ] || grep -q 'refs/tags/portable-v' "$work/refs" ||
        [ -n "$(git tag --list 'portable-v*')" ] ||
        git for-each-ref --format='%(refname)' refs/remotes/ | grep -q '/portable$'; then
        refuse 'no local portable; restore it from the correct remote-tracking ref before chain promotion'
    fi
    echo 'plan: no local portable; chain starts a new root'
fi
# An assertion cannot publish a new root beside any previous reachable history.
if [ "$push" = 1 ] && [ "$new_root" = 1 ] && [ -s "$work/refs" ]; then
    refuse 'a new-root push needs a remote with no refs, including tags'
fi
if [ "$new_root" = 1 ] && [ -n "$remote_old" ] && [ "$new_repo" = 0 ]; then
    refuse '--new-root needs an empty remote or --new-repository'
fi
if [ "$new_root" = 0 ] && [ -n "$remote_old" ]; then
    [ -n "$old" ] && git merge-base --is-ancestor "$remote_old" "$old" 2>/dev/null ||
        refuse 'remote portable is not an ancestor of local portable'
fi
git tag --list 'portable-v*' >"$work/tags"
awk '$2 ~ /^refs\/tags\/portable-v/ {sub(/^refs\/tags\//,"",$2); sub(/\^\{\}$/, "",$2); print $2}' \
    "$work/refs" >>"$work/tags"
python3 - "$version" "$work/tags" <<'PY' || exit 1
from pathlib import Path
import re
import sys
pattern = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
if not re.fullmatch(pattern, sys.argv[1]):
    sys.exit('promote: refused: version must be X.Y.Z without leading zeros')
v = tuple(map(int, sys.argv[1].split('.')))
for tag in Path(sys.argv[2]).read_text().splitlines():
    prior = tag.removeprefix('portable-v')
    if not re.fullmatch(pattern, prior):
        sys.exit('promote: refused: existing portable tag has an invalid version')
    if tuple(map(int, prior.split('.'))) >= v:
        sys.exit('promote: refused: version does not increase')
PY
visibility='Not verified: visibility'
if [ -s "$work/github" ] && command -v gh >/dev/null 2>&1; then
    if gh repo view "$(cat "$work/github")" --json isPrivate >"$work/visibility" 2>/dev/null; then
        visibility=$(python3 - "$work/visibility" <<'PY'
import json
from pathlib import Path
import sys
try:
    value = json.loads(Path(sys.argv[1]).read_text()).get('isPrivate')
    print('private' if value is True else 'public' if value is False else 'Not verified: visibility')
except (ValueError, OSError):
    print('Not verified: visibility')
PY
)
    fi
fi
# A URL path can contain a credential without user information.
python3 - "$work/url" "$remote" "$visibility" <<'PY'
import hashlib
from pathlib import Path
import sys
from urllib.parse import urlsplit
u = Path(sys.argv[1]).read_text()
p = urlsplit(u)
endpoint = (p.scheme + '://' + p.hostname) if p.hostname else 'local target'
print('plan: remote ' + sys.argv[2])
print('plan: URL ' + endpoint + ' sha256:' + hashlib.sha256(u.encode()).hexdigest())
print('plan: visibility ' + sys.argv[3])
PY
[ "$public" = 1 ] || [ "$push" = 0 ] || [ "$visibility" = private ] ||
    refuse '--push needs --public-remote when visibility is public or not verified'
printf 'Promote local-dev %s\n' "$rev" >"$work/commit-message"
# The source commit metadata stays private; check its tree in a neutral candidate.
export GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email"
# Caller dates and local time zones must not enter published metadata.
publish_time=$(date -u '+%s')
export TZ=UTC GIT_AUTHOR_DATE="$publish_time +0000" GIT_COMMITTER_DATE="$publish_time +0000"
if [ "$name" != 'litellm-gateway portable' ] || [ "${email%@*}" != portable ]; then
    echo 'promote: warning: nondefault identity name or email local part; review the checked identity' >&2
fi
# Validate identity before Git normalizes invalid header characters.
python3 - "$name" "$email" "$accept" <<'PY' || exit 1
import re
import sys
name, email, accept = sys.argv[1:]
if not re.fullmatch(r'[^<>\x00-\x1f\x7f]+', name) or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+', email):
    sys.exit('promote: refused: invalid identity')
if not re.fullmatch(r'[^@]+@[^@]+\.(invalid|example)', email, re.I) and accept != '1':
    sys.exit('promote: refused: personal identity needs --accept-identity')
PY
if [ "$new_root" = 0 ] && [ -n "$old" ]; then
    new=$(git -c commit.gpgSign=false commit-tree "$rev^{tree}" -p "$old" -F "$work/commit-message")
else
    new=$(git -c commit.gpgSign=false commit-tree "$rev^{tree}" -F "$work/commit-message")
fi
set -- --level fail --ref "$new" --identity-name "$name" --identity-email "$email" \
    --message-file "$work/tag-message" --commit-message-file "$work/commit-message"
[ "$accept" = 0 ] || set -- "$@" --accept-identity
public_check "$@"
# Every snapshot that is not on the remote leaves too. Check all of those trees
# and identities, not private development ancestry and not merely the tip.
if [ "$new_root" = 0 ] && [ -n "$old" ]; then
    if [ -n "$remote_old" ]; then range=$remote_old..$old; else range=$old; fi
    git rev-list "$range" >"$work/history" || error 'cannot read snapshot history'
    while IFS= read -r commit; do
        echo "plan: check outgoing snapshot $commit"
        set -- --level fail --ref "$commit"
        [ "$accept" = 0 ] || set -- "$@" --accept-identity
        public_check "$@"
    done <"$work/history"
fi
# Create an unsigned annotated tag object without caller tag options or signing.
{
    printf 'object %s\ntype commit\ntag %s\ntagger %s\n\n' "$new" "$tag" "$(git var GIT_COMMITTER_IDENT)"
    cat "$work/tag-message"
} >"$work/tag"
tag_id=$(git hash-object -t tag -w "$work/tag")
printf 'plan: source %s\nplan: old %s\nplan: new %s\n' "$rev" "${old:-none}" "$new"
printf 'plan: history %s\n' "$(if [ "$new_root" = 1 ]; then echo new-root; else echo chain; fi)"
printf 'plan: author %s <%s>\nplan: committer %s <%s>\nplan: tagger %s <%s>\n' "$name" "$email" "$name" "$email" "$name" "$email"
printf 'plan: %s:refs/heads/portable\nplan: %s:refs/tags/%s\n' "$new" "$tag_id" "$tag"
if [ "$apply" = 0 ]; then
    echo 'promote: dry run; no branch or tag changed'
    exit 0
fi
# One transaction protects both refs against concurrent changes and existing tags.
{
    if [ -n "$old" ]; then
        printf 'update refs/heads/portable %s %s\n' "$new" "$old"
    else
        printf 'create refs/heads/portable %s\n' "$new"
    fi
    printf 'create refs/tags/%s %s\n' "$tag" "$tag_id"
} | git update-ref --stdin >/dev/null || error 'ref transaction failed'
echo 'promote: local snapshot and tag created'
if [ "$push" = 1 ]; then
    # Recheck emptiness at both push boundaries; the dry run sends no objects.
    if [ "$new_root" = 1 ]; then
        remote_refs
        [ ! -s "$work/refs" ] || refuse 'a new-root push needs a remote with no refs, including tags'
    fi
    # Disable followTags from configuration. Exact refspecs are the full push set.
    git -c push.followTags=false push --dry-run --atomic "$url" \
        "$new:refs/heads/portable" "$tag_id:refs/tags/$tag" \
        >"$work/push-output" 2>&1 || error 'push dry run failed; local refs remain'
    echo 'promote: push dry run passed'
    if [ "$new_root" = 1 ]; then
        remote_refs
        [ ! -s "$work/refs" ] || refuse 'a new-root push needs a remote with no refs, including tags'
    fi
    git -c push.followTags=false push --atomic "$url" \
        "$new:refs/heads/portable" "$tag_id:refs/tags/$tag" \
        >"$work/push-output" 2>&1 || error 'push failed; local refs remain'
    remote_refs print
    [ "$(awk '$2 == "refs/heads/portable" {print $1}' "$work/refs")" = "$new" ] || error 'remote branch differs'
    [ "$(awk -v ref="refs/tags/$tag" '$2 == ref {print $1}' "$work/refs")" = "$tag_id" ] || error 'remote tag differs'
    echo 'promote: remote refs verified'
fi
