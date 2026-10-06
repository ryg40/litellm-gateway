#!/bin/sh
# All remotes are local bare repositories. GitHub and gh are simulated.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/promotion-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM
HOME=$tmp GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=test@example.invalid
GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=test@example.invalid
export HOME GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
real_git=$(command -v git)
export REAL_GIT=$real_git TEST_REMOTE=$tmp/remote.git
mkdir "$tmp/bin"
cat >"$tmp/bin/git" <<'SH'
#!/bin/sh
# Translate a fake HTTPS endpoint to the bare repository; reject all others.
exec python3 -c '
import os
import sys
args = sys.argv[1:]
if "push" in args and os.environ.get("TEST_PUSH_LOG"):
    with open(os.environ["TEST_PUSH_LOG"], "a") as log:
        log.write("push\n")
if "push" in args and "--dry-run" in args and os.environ.get("TEST_REMOTE_TAG_RACE"):
    import subprocess
    status = subprocess.call([os.environ["REAL_GIT"], *args])
    target = os.environ["TEST_REMOTE_TAG_RACE"]
    subprocess.check_call([os.environ["REAL_GIT"], "--git-dir=" + target, "fetch", "-q", os.getcwd(), "portable"])
    sha = subprocess.check_output([os.environ["REAL_GIT"], "rev-parse", "portable"]).decode().strip()
    subprocess.check_call([os.environ["REAL_GIT"], "--git-dir=" + target, "update-ref", "refs/tags/portable-v0.0.1", sha])
    sys.exit(status)
if "push" in args and os.environ.get("TEST_RACE"):
    import subprocess
    sha = subprocess.check_output([os.environ["REAL_GIT"], "rev-parse", "local-dev"]).decode().strip()
    if os.environ["TEST_RACE"] == "branch":
        subprocess.check_call([os.environ["REAL_GIT"], "update-ref", "refs/heads/portable", sha])
    else:
        dest = next(x.split(":", 1)[1] for x in args if ":refs/tags/" in x)
        subprocess.check_call([os.environ["REAL_GIT"], "update-ref", dest, sha])
    if any(x.startswith("refs/heads/portable:") or x.startswith("refs/tags/portable-v") for x in args):
        sys.exit("test: push uses movable refs")
for i, arg in enumerate(args):
    if arg == "https://github.com/example/gateway.git":
        if ("ls-remote" in args or "push" in args) and "--get-url" not in args:
            args[i] = os.environ["TEST_REMOTE"]
    elif "://" in arg and ("ls-remote" in args or "push" in args) and "--get-url" not in args:
        sys.exit("network disabled in tests")
os.execv(os.environ["REAL_GIT"], ["git", *args])
' "$@"
SH
cat >"$tmp/bin/gh" <<'SH'
#!/bin/sh
[ "$*" = 'repo view example/gateway --json isPrivate' ] || exit 2
printf '{"isPrivate":%s}\n' "${TEST_PRIVATE:-true}"
SH
chmod +x "$tmp/bin/git" "$tmp/bin/gh"
PATH=$tmp/bin:$PATH
export PATH
git init -q --bare "$TEST_REMOTE"
git init -q "$tmp/r"
cd "$tmp/r"
git symbolic-ref HEAD refs/heads/local-dev
mkdir scripts .local
cp "$repo/scripts/promote.sh" "$repo/scripts/public_check.sh" "$repo/scripts/public-patterns.tsv" "$repo/scripts/public-allow.tsv" "$repo/scripts/install-hooks.sh" scripts/
mkdir scripts/git-hooks
cp "$repo/scripts/git-hooks/dispatch" "$repo/scripts/git-hooks/pre-push" scripts/git-hooks/
# Unit tests use a scan double. The real scanner has its own integration gate.
cat >scripts/scan.sh <<'SH'
#!/bin/sh
if [ "${1:-}" = history ]; then
    printf '%s %s\n' "$SCAN_REF" "$*" >>"$TEST_HOOK_LOG"
else
    [ "$*" = '--level fail tree' ] || [ "$*" = selftest ] || exit 2
fi
exit "${TEST_SCAN_STATUS:-0}"
SH
printf '.local/\n' >.gitignore
printf 'synthetic-host-only\n' >.local/host-values.deny
printf 'Release example.\n' >"$tmp/message"
printf 'a\n' >a.txt
printf 'b\n' >b.txt
mkdir d && printf 'c\n' >d/c.txt
git add . && git commit -qm first
git commit -q --allow-empty -m second
git remote add shared "$TEST_REMOTE"
count=0
expect() {
    label=$1 wanted=$2; shift 2
    status=0
    "$@" >"$tmp/next-out" 2>&1 || status=$?
    mv "$tmp/next-out" "$tmp/out"
    if [ "$status" != "$wanted" ]; then
        printf 'FAIL %s: expected %s, got %s\n' "$label" "$wanted" "$status"
        cat "$tmp/out"
        exit 1
    fi
    reason=
    case $label in
        'existing version') reason='tag already exists' ;;
        'decreasing version') reason='version does not increase' ;;
        'leading-zero version'|'invalid version') reason='version must be X.Y.Z' ;;
        'dirty tree refused') reason='dirty tree' ;;
        'personal identity refused') reason='personal identity needs --accept-identity' ;;
        'missing deny list') reason='local deny list is absent' ;;
        'empty deny list') reason='local deny list is empty' ;;
        'hook override refused') reason='foreign or stale core.hooksPath' ;;
        'primary runtime checkout refused') reason='primary checkout has runtime files' ;;
        'disabled remote'|'userinfo remote') reason='disabled or unsafe push URL' ;;
        'upstream refused') reason='upstream is fetch-only' ;;
        'scan finding stops promotion'|'tag wording checked') reason='public check failed' ;;
        'scan tool error stays distinct') reason='public check tool failed' ;;
        'portable in another checkout refused') reason='portable is checked out in another worktree' ;;
        'shallow repository refused') reason='shallow repository' ;;
        'unknown visibility push refused'|'public remote push refused'|'unavailable visibility refused') reason='--push needs --public-remote' ;;
        'new root refuses populated remote') reason='--new-root needs an empty remote' ;;
        'assertion never permits force push') reason='a new-root push needs a remote with no refs' ;;
        'foreign remote ref refused'|'post-push foreign ref fails') reason='remote contains an undocumented ref' ;;
        'portable checkout refused') reason='portable is checked out' ;;
    esac
    if [ -n "$reason" ] && ! grep -qF -- "$reason" "$tmp/out"; then
        echo "FAIL $label: wrong refusal reason"; cat "$tmp/out"; exit 1
    fi
    count=$((count + 1)); printf 'ok %s\n' "$label"
}
reject() {
    label=$1 reason=$2; shift 2
    expect "$label" 1 "$@"
    grep -qF -- "$reason" "$tmp/out" || { echo "FAIL $label: wrong reason"; cat "$tmp/out"; exit 1; }
}
eq() { [ "$1" = "$2" ]; }
run() { sh scripts/promote.sh --remote shared --version "$1" --message-file "$tmp/message" ${2+"$2"} ${3+"$3"}; }
expect 'default is a dry run' 0 run 0.1.0
expect 'dry run keeps refs absent' 1 git show-ref --verify --quiet refs/heads/portable
git config commit.gpgSign true
git config tag.gpgSign true
expect 'first snapshot ignores signing settings' 0 env TZ=Pacific/Honolulu GIT_AUTHOR_DATE='946684800 -1000' GIT_COMMITTER_DATE='946684800 -1000' sh scripts/promote.sh --remote shared --version 0.1.0 --message-file "$tmp/message" --apply
git config --unset commit.gpgSign
git config --unset tag.gpgSign
first=$(git rev-parse portable)
expect 'root has one commit' 0 eq "$(git rev-list --count portable)" 1
expect 'snapshot equals source tree' 0 eq "$(git rev-parse 'portable^{tree}')" "$(git rev-parse 'local-dev^{tree}')"
expect 'history is separate' 1 git merge-base portable local-dev
expect 'message identifies source' 0 eq "$(git log -1 --format=%s portable)" "Promote local-dev $(git rev-parse local-dev)"
expect 'neutral author and committer' 0 eq "$(git log -1 --format='%ae %ce' portable)" 'portable@litellm-gateway.invalid portable@litellm-gateway.invalid'
expect 'annotated neutral tag' 0 eq "$(git for-each-ref --format='%(objecttype) %(taggeremail)' refs/tags/portable-v0.1.0)" 'tag <portable@litellm-gateway.invalid>'
expect 'unsigned tag' 1 sh -c 'git cat-file tag portable-v0.1.0 | grep -q "BEGIN PGP"'
expect 'existing version' 1 run 0.1.0
expect 'decreasing version' 1 run 0.0.9
expect 'leading-zero version' 1 run 00.2.0
expect 'invalid version' 1 run invalid
printf 'a2\n' >a.txt
git rm -q b.txt d/c.txt
printf 'new\n' >e.txt
git add . && git commit -qm change
expect 'chain snapshot' 0 run 0.2.0 --apply
expect 'one parent' 0 eq "$(git rev-list --parents -n 1 portable)" "$(git rev-parse portable) $first"
expect 'deleted file stays absent' 128 git cat-file -e portable:b.txt
expect 'deleted directory stays absent' 128 git cat-file -e portable:d
expect 'changed content' 0 eq "$(git show portable:a.txt)" a2
expect 'new content' 0 eq "$(git show portable:e.txt)" new
expect 'new root snapshot' 0 run 0.3.0 --apply --new-root
expect 'new root has no parent' 0 eq "$(git rev-list --count portable)" 1
expect 'checkout remains clean' 0 eq "$(git status --porcelain)" ''
expect 'checkout remains local-dev' 0 eq "$(git symbolic-ref --short HEAD)" local-dev
printf 'dirty\n' >>a.txt
expect 'dirty tree refused' 1 run 0.4.0
git show HEAD:a.txt >a.txt
expect 'personal identity refused' 1 env PORTABLE_PUBLISH_EMAIL=person@example.com sh scripts/promote.sh --remote shared --version 0.4.0 --message-file "$tmp/message"
expect 'explicit identity acceptance' 0 sh scripts/promote.sh --remote shared --version 0.4.0 --message-file "$tmp/message" --email person@example.com --accept-identity
expect 'neutral identity override' 0 env PORTABLE_PUBLISH_NAME=Example PORTABLE_PUBLISH_EMAIL=release@project.example sh scripts/promote.sh --remote shared --version 0.4.0 --message-file "$tmp/message"
mv .local/host-values.deny .local/deny-save
expect 'missing deny list' 1 run 0.4.0
printf '# comment only\n' >.local/host-values.deny
expect 'empty deny list' 1 run 0.4.0
mv .local/deny-save .local/host-values.deny
git config core.hooksPath /unused
expect 'hook override refused' 1 run 0.4.0
git config --unset core.hooksPath
printf 'runtime\n' >.env
printf '.env\n' >>.git/info/exclude
expect 'primary runtime checkout refused' 1 run 0.4.0
rm .env
git remote set-url shared DISABLED
expect 'disabled remote' 1 run 0.4.0
git remote set-url shared https://person@example.invalid/repo.git
expect 'userinfo remote' 1 run 0.4.0
expect 'userinfo is not printed' 1 grep -F person@ "$tmp/out"
git remote set-url shared "$TEST_REMOTE"
expect 'upstream refused' 1 sh scripts/promote.sh --remote upstream --version 0.4.0 --message-file "$tmp/message"
expect 'scan finding stops promotion' 1 env TEST_SCAN_STATUS=1 sh scripts/promote.sh --remote shared --version 0.4.0 --message-file "$tmp/message"
expect 'scan tool error stays distinct' 2 env TEST_SCAN_STATUS=2 sh scripts/promote.sh --remote shared --version 0.4.0 --message-file "$tmp/message"
printf 'iss%s 123\n' ue >"$tmp/message"
expect 'tag wording checked' 1 run 0.4.0
printf 'Release example.\n' >"$tmp/message"
git worktree add -q "$tmp/portable-checkout" portable
expect 'portable in another checkout refused' 1 run 0.4.0
git worktree remove "$tmp/portable-checkout"
printf '%s\n' "$(git rev-parse HEAD)" >.git/shallow
expect 'shallow repository refused' 1 run 0.4.0
rm .git/shallow
expect 'unknown visibility push refused' 1 run 0.4.0 --push
git remote set-url shared https://github.com/example/gateway.git
TEST_PRIVATE=false; export TEST_PRIVATE
expect 'public remote push refused' 1 run 0.4.0 --push
expect 'public visibility in plan' 0 grep -F 'plan: visibility public' "$tmp/out"
expect 'public dry run allowed' 0 run 0.4.0
mv "$tmp/bin/gh" "$tmp/bin/gh-save"
# A failing gh has the same fail-closed result as an absent gh.
printf '#!/bin/sh\nexit 1\n' >"$tmp/bin/gh"
chmod +x "$tmp/bin/gh"
expect 'unavailable visibility refused' 1 run 0.4.0 --push
mv "$tmp/bin/gh-save" "$tmp/bin/gh"
TEST_PRIVATE=true; export TEST_PRIVATE
# Disable follow-tags from caller configuration and test a real local push.
git config push.followTags true
expect 'private push with two explicit refs' 0 run 0.4.0 --push
expect 'only one branch and one tag leave' 0 eq "$(git --git-dir="$TEST_REMOTE" for-each-ref --format='%(refname)' | wc -l | tr -d ' ')" 2
expect 'new root refuses populated remote' 1 run 0.5.0 --new-root
expect 'new-repository assertion permits root preview' 0 sh scripts/promote.sh --remote shared --version 0.5.0 --message-file "$tmp/message" --new-root --new-repository
expect 'assertion never permits force push' 1 sh scripts/promote.sh --remote shared --version 0.5.0 --message-file "$tmp/message" --new-root --new-repository --push
git --git-dir="$TEST_REMOTE" update-ref refs/heads/foreign "$(git rev-parse portable)"
expect 'foreign remote ref refused' 1 run 0.5.0
git --git-dir="$TEST_REMOTE" update-ref -d refs/heads/foreign
TEST_PRIVATE=false; export TEST_PRIVATE
expect 'explicit public push' 0 run 0.5.0 --push --public-remote
# A remote hook creates an extra ref after receive. The post-push check must fail.
cat >"$TEST_REMOTE/hooks/post-receive" <<'SH'
#!/bin/sh
git update-ref refs/heads/foreign refs/heads/portable
SH
chmod +x "$TEST_REMOTE/hooks/post-receive"
expect 'post-push foreign ref fails' 1 run 0.6.0 --push --public-remote
expect 'foreign ref was not deleted' 0 git --git-dir="$TEST_REMOTE" show-ref --verify refs/heads/foreign
git --git-dir="$TEST_REMOTE" update-ref -d refs/heads/foreign
rm "$TEST_REMOTE/hooks/post-receive"
# Both push calls use checked objects even after local refs move.
for race in branch tag; do
    git init -q --bare "$tmp/race-$race.git"
    git remote set-url shared "$tmp/race-$race.git"
    v=1.0.0; [ "$race" = branch ] || v=1.1.0
    expect "$race ref race keeps checked objects" 0 env TEST_RACE=$race sh scripts/promote.sh --remote shared --version "$v" --message-file "$tmp/message" --new-root --push --public-remote
    checked=$(awk '/plan: new / {print $3}' "$tmp/out")
    checked_tag=$(awk '/plan: .*:refs\/tags\// {split($2, a, ":"); print a[1]}' "$tmp/out")
    expect "$race race sends checked branch" 0 eq "$(git --git-dir="$tmp/race-$race.git" rev-parse portable)" "$checked"
    expect "$race race sends checked tag" 0 eq "$(git --git-dir="$tmp/race-$race.git" rev-parse "portable-v$v^{commit}")" "$checked"
    expect "$race race sends checked annotated object" 0 eq "$(git --git-dir="$tmp/race-$race.git" rev-parse "portable-v$v")" "$checked_tag"
    expect "$race race excludes development ancestry" 1 git --git-dir="$tmp/race-$race.git" cat-file -e "$(git rev-parse local-dev)"
done
# Test a single ordinary rewrite and a forbidden second expansion.
git init -q --bare "$tmp/rewrite.git"
git remote set-url shared publish-target
git config "url.$tmp/rewrite.git.insteadOf" publish-target
expect 'single URL rewrite' 0 run 2.0.0 --new-root
git config "url.$tmp/final.git.insteadOf" "$tmp/rewrite.git"
reject 'chained URL rewrite' 'push URL rewrites a second time' run 2.0.0 --new-root
git config --unset "url.$tmp/final.git.insteadOf"
git config --unset "url.$tmp/rewrite.git.insteadOf"
git config "url.$tmp/rewrite.git.pushInsteadOf" push-target
git remote set-url shared push-target
expect 'ordinary pushInsteadOf' 0 run 2.0.0 --new-root
git init -q --bare "$tmp/final.git"
git config "url.$tmp/final.git.pushInsteadOf" "$tmp/rewrite.git"
reject 'chained pushInsteadOf rewrite' 'push URL rewrites a second time' env TEST_PUSH_LOG="$tmp/chained-push-log" sh scripts/promote.sh --remote shared --version 2.0.0 --message-file "$tmp/message" --new-root --push --public-remote
expect 'chained push rewrite stops before push' 1 test -e "$tmp/chained-push-log"
expect 'chained push rewrite leaves final remote empty' 0 eq "$(git --git-dir="$tmp/final.git" for-each-ref --format='%(refname)')" ''
git config --unset "url.$tmp/final.git.pushInsteadOf"
git config --unset "url.$tmp/rewrite.git.pushInsteadOf"
git remote set-url shared "$tmp/rewrite.git"
# Remote configuration, versions and unrecognized refs refuse for the right reasons.
git config remote.shared.mirror true
reject 'mirror remote refused' 'mirror remote' run 2.0.0 --new-root
git config --unset remote.shared.mirror
git config --add remote.shared.pushurl "$tmp/rewrite.git"
git config --add remote.shared.pushurl "$tmp/other.git"
reject 'multiple push URLs refused' 'exactly one push URL' run 2.0.0 --new-root
git config --unset-all remote.shared.pushurl
for unsafe in 'https://example.invalid/path?token=value' 'https://example.invalid/path#token' 'git@github.com:example/gateway.git' 'ssh://git@github.com/example/gateway.git' 'https://github.com:444/example/gateway.git' 'https://github.com/not/a/repository.git'; do
    git remote set-url shared "$unsafe"
    case $unsafe in
        *:444/*) reason='nonstandard GitHub HTTPS port' ;;
        *not/a/*) reason='invalid GitHub path' ;;
        *) reason='disabled or unsafe push URL' ;;
    esac
    reject 'unsafe URL refused' "$reason" run 2.0.0 --new-root
done
git remote set-url shared "$tmp/rewrite.git"
git --git-dir="$tmp/rewrite.git" fetch -q "$tmp/r" local-dev:refs/heads/portable
reject 'divergent remote portable refused' 'remote portable is not an ancestor' run 2.0.0
git --git-dir="$tmp/rewrite.git" fetch -q "$tmp/r" portable
git --git-dir="$tmp/rewrite.git" update-ref -d refs/heads/portable
git --git-dir="$tmp/rewrite.git" update-ref refs/tags/portable-v9.0.0 "$(git rev-parse portable)"
reject 'remote-only higher version refused' 'version does not increase' run 2.0.0 --new-root
git --git-dir="$tmp/rewrite.git" update-ref -d refs/tags/portable-v9.0.0
git --git-dir="$tmp/rewrite.git" update-ref refs/tags/portable-vx/y "$(git rev-parse portable)"
reject 'remote malformed version refused' 'existing portable tag has an invalid version' run 2.0.0 --new-root
git --git-dir="$tmp/rewrite.git" update-ref -d refs/tags/portable-vx/y
git --git-dir="$tmp/rewrite.git" update-ref refs/pull/1/head "$(git rev-parse portable)"
reject 'pull refs refused' 'remote contains an undocumented ref' run 2.0.0 --new-root
git --git-dir="$tmp/rewrite.git" update-ref -d refs/pull/1/head
# A URL path is never printed, including on network failure.
git remote set-url shared "https://example.invalid/t/path-token-control/repo.git"
status=0
run 2.0.0 >"$tmp/url-error" 2>&1 || status=$?
expect 'network error is redacted' 0 eq "$status" 2
expect 'path token stays hidden' 1 grep -F path-token-control "$tmp/url-error"
grep -qF 'cannot list remote refs' "$tmp/url-error"
git init -q --bare "$tmp/path-token-control.git"
git remote set-url shared "$tmp/path-token-control.git"
expect 'successful path target preview' 0 run 2.0.0 --new-root
cp "$tmp/out" "$tmp/path-plan"
expect 'successful plan hides path token' 1 grep -F path-token-control "$tmp/path-plan"
git remote set-url shared "$tmp/rewrite.git"
git init -q --bare "$tmp/tag-race.git"
git remote set-url shared "$tmp/tag-race.git"
reject 'tag appearing after dry run blocks root push' 'a new-root push needs a remote with no refs' env TEST_REMOTE_TAG_RACE="$tmp/tag-race.git" sh scripts/promote.sh --remote shared --version 1.2.0 --message-file "$tmp/message" --new-root --push --public-remote
expect 'tag race sends no branch' 1 git --git-dir="$tmp/tag-race.git" show-ref --verify --quiet refs/heads/portable
git remote set-url shared "$tmp/rewrite.git"
# A tags-only remote is not empty for a new-root push.
git --git-dir="$tmp/rewrite.git" fetch -q "$tmp/r" refs/tags/portable-v0.4.0:refs/tags/portable-v0.4.0
reject 'tags-only new-root push' 'a new-root push needs a remote with no refs' run 2.0.0 --new-root --push
reject 'tags-only assertion push' 'a new-root push needs a remote with no refs' sh scripts/promote.sh --remote shared --version 2.0.0 --message-file "$tmp/message" --new-root --new-repository --push
expect 'tags-only new-root preview' 0 run 2.0.0 --new-root
# Test real dispatcher invocation and stale/global/system overrides.
TEST_HOOK_LOG=$tmp/hook-log; export TEST_HOOK_LOG
sh scripts/install-hooks.sh >"$tmp/install-log"
expect 'installed hook path accepted' 0 run 2.0.0 --new-root
cp .git/scan-hooks/pre-push "$tmp/current-hook"
printf '# stale\n' >>.git/scan-hooks/pre-push
reject 'stale hooks refused' 'foreign or stale core.hooksPath' run 2.0.0 --new-root
cp "$tmp/current-hook" .git/scan-hooks/pre-push
printf '[core]\n hooksPath = /foreign-global\n' >"$tmp/global"
reject 'shadowed global hooks refused' 'foreign or stale core.hooksPath' env GIT_CONFIG_GLOBAL="$tmp/global" sh scripts/promote.sh --remote shared --version 2.0.0 --message-file "$tmp/message" --new-root
reject 'system hooks refused' 'foreign or stale core.hooksPath' env GIT_CONFIG_NOSYSTEM=0 GIT_CONFIG_SYSTEM="$tmp/global" sh scripts/promote.sh --remote shared --version 2.0.0 --message-file "$tmp/message" --new-root
git init -q --bare "$tmp/hooks.git"
git remote set-url shared "$tmp/hooks.git"
expect 'push invokes installed hook' 0 sh scripts/promote.sh --remote shared --version 2.0.0 --message-file "$tmp/message" --new-root --push --public-remote
expect 'installed hook scans branch' 0 grep -F 'refs/heads/portable history' "$TEST_HOOK_LOG"
expect 'installed hook scans tag' 0 grep -F 'refs/tags/portable-v2.0.0 history' "$TEST_HOOK_LOG"
expect 'UTC commit timestamp' 0 sh -c 'git cat-file commit portable | grep -Eq "^author .* [0-9]+ [+]0000$"'
expect 'UTC tag timestamp' 0 sh -c 'git cat-file tag portable-v2.0.0 | grep -Eq "^tagger .* [0-9]+ [+]0000$"'
reject 'deny list checks custom name' 'public check failed' sh scripts/promote.sh --remote shared --version 2.1.0 --message-file "$tmp/message" --name synthetic-host-only
reject 'deny list checks custom email' 'public check failed' sh scripts/promote.sh --remote shared --version 2.1.0 --message-file "$tmp/message" --email synthetic-host-only@project.invalid
expect 'nondefault identity warning' 0 sh scripts/promote.sh --remote shared --version 2.1.0 --message-file "$tmp/message" --name Example --email release@project.invalid
expect 'identity warning is visible' 0 grep -F 'nondefault identity name or email local part' "$tmp/out"
# The clone gets publication history from github, not automatically from origin.
git clone -q --no-local --no-tags --branch local-dev "$tmp/r" "$tmp/clone"
cd "$tmp/clone"
{
    git remote add github "$tmp/hooks.git"
    git config remote.github.fetch '+refs/heads/portable:refs/remotes/github/portable'
    git fetch -q --no-tags github
    mkdir .local
    cp "$tmp/r/.local/host-values.deny" .local/host-values.deny
    reject 'missing clone portable diagnosed' 'no local portable; restore it' sh scripts/promote.sh --version 2.1.0 --message-file "$tmp/message"
    git branch portable refs/remotes/github/portable
    sh scripts/install-hooks.sh >"$tmp/clone-hooks"
    expect 'documented clean clone preview' 0 sh scripts/promote.sh --version 2.1.0 --message-file "$tmp/message"
    git remote set-url github "$tmp/rewrite.git"
    git update-ref -d refs/heads/portable
    reject 'cached portable prevents silent root' 'no local portable; restore it' sh scripts/promote.sh --version 2.1.0 --message-file "$tmp/message"
    git update-ref -d refs/remotes/github/portable
    git update-ref -d refs/remotes/origin/portable
    reject 'remote tags prevent silent root' 'no local portable; restore it' sh scripts/promote.sh --version 2.1.0 --message-file "$tmp/message"
}
cd "$tmp/r"
# Check a portable checkout only after the local operations.
git checkout -q portable
expect 'portable checkout refused' 1 run 0.7.0
printf 'all %s promotion checks passed\n' "$count"
