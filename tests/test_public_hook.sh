#!/bin/sh
# Local hook inputs exercise the ref guard and the existing history ranges.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/public-hook-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM
HOME=$tmp GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export HOME GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
git init -q "$tmp/r"
cd "$tmp/r"
mkdir scripts
cat >scripts/scan.sh <<'SH'
#!/bin/sh
printf '%s %s\n' "$SCAN_REF" "$*" >>"$TEST_LOG"
SH
git add . && git -c user.name=Test -c user.email=test@example.invalid commit -qm test
sha=$(git rev-parse HEAD)
zero=$(printf '%040d' 0)
TEST_LOG=$tmp/log; export TEST_LOG
count=0
expect() {
    label=$1 wanted=$2; shift 2
    status=0
    "$@" >"$tmp/out" 2>&1 || status=$?
    if [ "$status" != "$wanted" ]; then echo "FAIL $label ($status)"; cat "$tmp/out"; exit 1; fi
    if [ "$wanted" = 1 ]; then
        case $label in
            'missing tracked scanner fails closed') reason='scripts/scan.sh is not in the working tree' ;;
            'portable deletion refused'|'shared deletion of foreign ref refused'|'installed shared deletion refused'*|'installed URL deletion refused'*) reason='deletion through this remote is refused' ;;
            'mapped development source refused') reason='non-portable source mapped to portable is refused' ;;
            'development object source refused') reason='development object source mapped to portable is refused' ;;
            'malformed tag refused') reason='malformed portable tag is refused' ;;
            *) reason='only portable and portable-v* may leave' ;;
        esac
        grep -qF "$reason" "$tmp/out" || { echo "FAIL $label: wrong reason"; cat "$tmp/out"; exit 1; }
    fi
    count=$((count + 1)); echo "ok $label"
}
hook() {
    printf '%s %s %s %s\n' "$2" "$3" "$4" "$5" | sh "$repo/scripts/git-hooks/pre-push" "$1" unused
}
expect 'shared development ref refused' 1 hook shared refs/heads/local-dev "$sha" refs/heads/local-dev "$zero"
expect 'shared arbitrary tag refused' 1 hook shared refs/tags/v1 "$sha" refs/tags/v1 "$zero"
expect 'shared deletion of foreign ref refused' 1 hook shared '(delete)' "$zero" refs/heads/foreign "$sha"
expect 'origin development ref allowed' 0 hook origin refs/heads/local-dev "$sha" refs/heads/local-dev "$zero"
expect 'new origin range unchanged' 0 grep -F "history $sha --not --remotes=origin" "$TEST_LOG"
expect 'shared portable allowed' 0 hook shared refs/heads/portable "$sha" refs/heads/portable "$zero"
expect 'new portable scans full history' 0 grep -xF "refs/heads/portable history $sha" "$TEST_LOG"
expect 'shared release tag allowed' 0 hook shared refs/tags/portable-v1.0.0 "$sha" refs/tags/portable-v1.0.0 "$zero"
expect 'portable deletion refused' 1 hook shared '(delete)' "$zero" refs/heads/portable "$sha"
grep -qF 'portable deletion through this remote is refused' "$tmp/out"
expect 'mapped development source refused' 1 hook shared refs/heads/local-dev "$sha" refs/heads/portable "$zero"
grep -qF 'non-portable source mapped to portable is refused' "$tmp/out"
expect 'malformed tag refused' 1 hook shared refs/tags/portable-vx/y "$sha" refs/tags/portable-vx/y "$zero"
grep -qF 'malformed portable tag is refused' "$tmp/out"
expect 'checked branch object allowed' 0 hook shared "$sha" "$sha" refs/heads/portable "$zero"
expect 'checked tag object allowed' 0 hook shared "$sha" "$sha" refs/tags/portable-v1.0.0 "$zero"
expect 'existing portable allowed' 0 hook shared refs/heads/portable "$sha" refs/heads/portable "$sha"
expect 'existing range unchanged' 0 grep -F "history $sha..$sha" "$TEST_LOG"
# Refuse the development tip and its ancestor as branch or tag object sources.
git branch local-dev "$sha"
git -c user.name=Test -c user.email=test@example.invalid commit -q --allow-empty -m development
git branch -f local-dev HEAD
for source in "$sha" "$(git rev-parse local-dev)"; do
    for target in refs/heads/portable refs/tags/portable-v1.0.0; do
        expect 'development object source refused' 1 hook shared "$source" "$source" "$target" "$zero"
    done
done
git -c user.name=Test -c user.email=test@example.invalid tag -a portable-v1.1.0 -m example local-dev
dev_tag=$(git rev-parse portable-v1.1.0)
expect 'development object source refused' 1 hook shared "$dev_tag" "$dev_tag" refs/tags/portable-v1.1.0 "$zero"
checked=$(git -c user.name=Test -c user.email=test@example.invalid commit-tree 'HEAD^{tree}' -m portable)
for target in refs/heads/portable refs/tags/portable-v1.0.0; do
    expect 'separate checked object allowed' 0 hook shared "$checked" "$checked" "$target" "$zero"
done
expect 'origin development object allowed' 0 hook origin "$sha" "$sha" refs/heads/portable "$zero"
mv scripts/scan.sh "$tmp/scan-save"
expect 'missing tracked scanner fails closed' 1 hook origin refs/heads/main "$sha" refs/heads/main "$zero"
git rm -q scripts/scan.sh
git -c user.name=Test -c user.email=test@example.invalid commit -qm remove
expect 'no scanner cannot bypass shared ref guard' 1 hook shared refs/heads/main "$sha" refs/heads/main "$zero"
expect 'no scanner preserves origin behavior' 0 hook origin refs/heads/main "$sha" refs/heads/main "$zero"
# Real local pushes use the installed dispatcher, with distinct remote URLs.
mkdir -p scripts/git-hooks
mv "$tmp/scan-save" scripts/scan.sh
cp "$repo/scripts/install-hooks.sh" scripts/
cp "$repo/scripts/git-hooks/dispatch" "$repo/scripts/git-hooks/pre-push" scripts/git-hooks/
sh scripts/install-hooks.sh >"$tmp/install-log"
expect 'installed hooks are current' 0 sh scripts/install-hooks.sh --check
git init -q --bare "$tmp/origin.git"
git init -q --bare "$tmp/shared.git"
git remote add origin "$tmp/origin.git"
git remote add shared "$tmp/shared.git"
git branch portable "$checked"
git -c user.name=Test -c user.email=test@example.invalid tag -a portable-v1.0.0 -m example "$checked"
for remote in origin shared; do
    expect "$remote deletion fixture seeded" 0 git push "$remote" refs/heads/portable:refs/heads/portable refs/tags/portable-v1.0.0:refs/tags/portable-v1.0.0
done
for ref in refs/heads/portable refs/tags/portable-v1.0.0; do
    expect "installed origin deletion allowed: $ref" 0 git push origin --delete "$ref"
    expect "origin deleted ref absent: $ref" 0 test -z "$(git --git-dir="$tmp/origin.git" for-each-ref --format='%(refname)' "$ref")"
    expect "installed shared deletion refused: $ref" 1 git push shared --delete "$ref"
    expect "shared refused ref preserved: $ref" 0 git --git-dir="$tmp/shared.git" show-ref --verify --quiet "$ref"
    expect "installed URL deletion refused: $ref" 1 git push "$tmp/shared.git" --delete "$ref"
    expect "URL refused ref preserved: $ref" 0 git --git-dir="$tmp/shared.git" show-ref --verify --quiet "$ref"
done
printf 'all %s public hook checks passed\n' "$count"
