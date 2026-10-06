#!/bin/sh
# Scan for secrets and host values before they go to a remote or into an image.
# See docs/secret-handling.md for the rules, the levels and the hooks.
#
# Usage: scripts/scan.sh [--level warn|fail] <mode> [<argument>]
#   tree               tracked files (working tree) and the staged content,
#                      the content of archives, the target text of symlinks
#                      and the names of the tracked files
#   staged             the staged changes and their file names (the hooks
#                      pre-commit and pre-merge-commit use this)
#   history [<range>]  the commits in <range> (git log syntax, default HEAD),
#                      their messages and the names of the files they change
#   image <reference> [<base>]
#                      the file system of a local image; with <base>, only the
#                      layers that <reference> adds on top of the image <base>
#   all                tree and history
#   selftest           negative control: a synthetic token must give a finding
#
# A secret finding always fails. A host-value finding (scripts/host-values.deny,
# the untracked .local/host-values.deny when it exists, scripts/host-values.regex)
# fails at level "fail" and only prints at level "warn". The default level is
# "fail" for the branch portable, for a tag portable-v* and for the mode image;
# "warn" otherwise.
# A policy finding always fails (modes tree, staged, history, all): a tracked
# file named .gitleaksignore or .gitleaksbaseline, a tracked file in .local/,
# or a tracked archive, database dump or Git bundle, known by the extension or
# by the first bytes of the content (list in classify below).
# Content does not switch the scan off: a "gitleaks:allow" comment does not
# hide a finding, and gitleaks reads no .gitleaksignore of the repository.
#
# Exit: 0 clean, 1 finding, 2 usage or tool error.
#
# Needs: a POSIX shell with od and readlink, Git 2.5 or later (tested with
# 2.39.5), and docker or the gitleaks binary.
#
# Environment:
#   GITLEAKS_IMAGE    replaces the pinned scanner image (for a mirror)
#   SCAN_ENGINE       auto (default), docker or binary
#   SCAN_REF          the ref that sets the default level, for example
#                     refs/tags/portable-v1 (default: the current branch and tags)
#   SCAN_NAME_PREFIX  name prefix of the helper containers (default scan-)

set -eu

GITLEAKS_VERSION=v8.28.0
GITLEAKS_IMAGE=${GITLEAKS_IMAGE:-zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854}
SCAN_ENGINE=${SCAN_ENGINE:-auto}
SCAN_NAME_PREFIX=${SCAN_NAME_PREFIX:-scan-}
# Paths of the deny lists, relative to the repository root. A deny list contains
# the patterns that it forbids, so host-value findings in these two files are
# dropped. The tracked list holds generic patterns only; the untracked local
# list holds the values of one host and is optional (a clone has none).
DENY_REL=scripts/host-values.deny
LOCAL_DENY_REL=.local/host-values.deny
# Regular expressions for host values. The file contains no address, so it has
# no exception.
REGEX_REL=scripts/host-values.regex

script_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
base_config=$script_root/.gitleaks.toml
deny_file=$script_root/$DENY_REL
local_deny_file=$script_root/$LOCAL_DENY_REL
regex_file=$script_root/$REGEX_REL

usage() {
    echo "usage: $0 [--level warn|fail] tree|staged|history [<range>]|image <reference> [<base>]|all|selftest" >&2
    exit 2
}
die() {
    echo "scan: error: $*" >&2
    exit 2
}

level=
while [ $# -gt 0 ]; do
    case $1 in
        --level) [ $# -ge 2 ] || usage; level=$2; shift 2 ;;
        --level=*) level=${1#--level=}; shift ;;
        -h|--help) usage ;;
        -*) usage ;;
        *) break ;;
    esac
done
[ $# -ge 1 ] || usage
mode=$1; shift
arg=
base=
case $mode in
    tree|staged|all|selftest) [ $# -eq 0 ] || usage ;;
    history) [ $# -le 1 ] || usage; arg=${1:-HEAD} ;;
    image) [ $# -ge 1 ] && [ $# -le 2 ] || usage; arg=$1; base=${2:-} ;;
    *) usage ;;
esac
case $level in
    ''|warn|fail) ;;
    *) usage ;;
esac

work=$(mktemp -d "${TMPDIR:-/tmp}/scan.XXXXXX")
# Docker Desktop and some CI systems share only resolved paths.
work=$(CDPATH='' cd -- "$work" && pwd -P)
export_cid=
cleanup() {
    if [ -n "$export_cid" ]; then docker rm -f "$export_cid" >/dev/null 2>&1 || true; fi
    rm -rf "$work"
}
trap cleanup EXIT
# In sh, an INT or TERM handler does not end the script; exit runs the EXIT trap.
trap 'exit 130' INT TERM

# --- scanner engine ---------------------------------------------------------

engine=
select_engine() {
    case $SCAN_ENGINE in
        docker|auto)
            if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
                engine=docker
                docker image inspect "$GITLEAKS_IMAGE" >/dev/null 2>&1 ||
                    die "scanner image $GITLEAKS_IMAGE is not present; run: docker pull $GITLEAKS_IMAGE"
                return
            fi
            [ "$SCAN_ENGINE" = auto ] || die "docker is not available"
            ;;
        binary) ;;
        *) die "SCAN_ENGINE must be auto, docker or binary" ;;
    esac
    command -v gitleaks >/dev/null 2>&1 || die "no docker and no gitleaks binary"
    v=$(gitleaks version 2>/dev/null || true)
    [ "$v" = "$GITLEAKS_VERSION" ] || die "gitleaks binary is ${v:-unknown}, need $GITLEAKS_VERSION"
    engine=binary
}

run_count=0
# bind_mount <path>: a read-only --mount value for <path> at the same path.
# The value is CSV and each field is quoted, so a path may hold a blank, a
# comma or a colon.
bind_mount() {
    q=$(printf '%s' "$1" | sed 's/"/""/g')
    printf 'type=bind,"source=%s","target=%s",readonly' "$q" "$q"
}
# run_gitleaks <mounts> <gitleaks-argument>...
# <mounts>: none, repo (the repository) or image=<reference>. Paths are mounted
# read-only at the same path, so the arguments are the same for the container
# and for the binary. The report goes to stdout.
run_gitleaks() {
    mounts=$1; shift
    if [ "$engine" = binary ]; then
        gitleaks "$@"
        return
    fi
    run_count=$((run_count + 1))
    set -- "$GITLEAKS_IMAGE" "$@"
    case $mounts in
        repo)
            # The Git directory is inside the working tree or inside the common directory.
            case $common/ in
                "$repo_top"/*) ;;
                *) set -- --mount "$(bind_mount "$common")" "$@" ;;
            esac
            case $git_dir/ in
                "$repo_top"/*|"$common"/*) ;;
                *) set -- --mount "$(bind_mount "$git_dir")" "$@" ;;
            esac
            set -- --mount "$(bind_mount "$repo_top")" "$@"
            # The index file is inside the Git directory.
            if [ -n "${GIT_INDEX_FILE:-}" ]; then set -- -e GIT_INDEX_FILE "$@"; fi
            ;;
        image=*) set -- --mount "type=image,source=${mounts#image=},target=$image_root" "$@" ;;
    esac
    docker run --rm --network none --name "${SCAN_NAME_PREFIX}gitleaks-$$-$run_count" \
        --mount "$(bind_mount "$work")" \
        -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
        "$@"
}
# gl <mounts> <gitleaks-command> <argument>...: gitleaks with the common options.
# --ignore-gitleaks-allow: a "gitleaks:allow" comment does not hide a finding.
# --gitleaks-ignore-path: an empty directory, so that no .gitleaksignore of the
# current directory applies. gitleaks also reads <source>/.gitleaksignore; in
# git mode the source is the Git directory, not the working tree (see
# require_repo), and in dir mode it is a directory of this script.
gl() {
    m=$1 c=$2; shift 2
    run_gitleaks "$m" "$c" --no-banner --no-color --log-level error --redact --exit-code 0 \
        --ignore-gitleaks-allow --gitleaks-ignore-path "$work/no-ignore" \
        --config "$work/config.toml" --report-format template \
        --report-template "$work/report.tmpl" --report-path - "$@"
}

# --- configuration ------------------------------------------------------------

build_config() {
    [ -f "$base_config" ] || die "missing $base_config"
    [ -f "$deny_file" ] || die "missing $deny_file"
    [ -f "$regex_file" ] || die "missing $regex_file"
    # The allowlists of .gitleaks.toml are for secrets. Limit each top-level
    # allowlist to the secret rules, so that no allowlist hides a host value.
    ! grep -q '^[[:space:]]*targetRules' "$base_config" ||
        die ".gitleaks.toml: do not set targetRules; scripts/scan.sh sets it"
    ids=$(awk '/^\[\[rules\]\]/ { r = 1; next } /^\[/ { r = 0 }
        r && /^id = / { sub(/^id = /, ""); printf "%s%s", sep, $0; sep = ", " }' "$base_config")
    [ -n "$ids" ] || die ".gitleaks.toml has no rule"
    awk -v ids="$ids" '{ print } /^\[\[allowlists\]\][[:space:]]*$/ { print "targetRules = [" ids "]" }' \
        "$base_config" >"$work/config.toml"
    : >"$work/patterns"
    # The rule ids: host-value-<n> for the tracked list, host-value-local-<n>
    # for the local list.
    add_deny "$deny_file" "$DENY_REL" host-value-
    if [ -f "$local_deny_file" ]; then
        add_deny "$local_deny_file" "$LOCAL_DENY_REL" host-value-local-
    fi
    printf '\n# Generated from %s\n' "$REGEX_REL" >>"$work/config.toml"
    # One line for each rule: <name> <sample> <regex> (see the file).
    : >"$work/samples"
    while read -r name sample re rest; do
        case $name in ''|'#'*) continue ;; esac
        [ -n "$re" ] && [ -z "$rest" ] || die "$REGEX_REL: rule $name needs three fields"
        case $name in *[!A-Za-z0-9-]*|local-*) die "$REGEX_REL: bad rule name $name" ;; esac
        case $re in *"'''"*) die "$REGEX_REL: the regex of $name contains three quotes" ;; esac
        printf 'host-value-%s\t%s (regex)\n' "$name" "$name" >>"$work/patterns"
        printf 'host-value-%s\t%s\n' "$name" "$(printf '%s' "$sample" | tr '_-' '.:')" >>"$work/samples"
        printf "\n[[rules]]\nid = \"host-value-%s\"\ndescription = \"host value: %s\"\nregex = '''%s'''\nsecretGroup = 1\n" \
            "$name" "$name" "$re" >>"$work/config.toml"
    done <"$regex_file"
    # The deny lists and the regex list together must give at least one rule.
    [ -s "$work/patterns" ] || die "no host-value rule in $DENY_REL, $LOCAL_DENY_REL and $REGEX_REL"
    # One line for each finding: rule, file, line, commit (tab separated).
    printf '%s\n' '{{- range . }}{{ .RuleID }}	{{ .File }}	{{ .StartLine }}	{{ .Commit }}{{ "\n" }}{{- end }}' \
        >"$work/report.tmpl"
    mkdir "$work/no-ignore"
}

# add_deny <file> <relative path> <rule id prefix>: one rule for each pattern.
add_deny() {
    printf '\n# Generated from %s\n' "$2" >>"$work/config.toml"
    n=0
    # Strip leading and trailing blanks; skip comments and empty lines.
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" >"$work/deny"
    while IFS= read -r p; do
        case $p in ''|'#'*) continue ;; esac
        case $p in *[\'\"\\]*) die "$2: a pattern contains a quote or a backslash" ;; esac
        n=$((n + 1))
        re=$(printf '%s\n' "$p" | sed 's/[].*^$+?(){}|[]/\\&/g')
        printf '%s%s\t%s\n' "$3" "$n" "$p" >>"$work/patterns"
        printf "\n[[rules]]\nid = \"%s%s\"\ndescription = \"host value: %s\"\nregex = '''(?i)%s'''\n" \
            "$3" "$n" "$p" "$re" >>"$work/config.toml"
    done <"$work/deny"
}

# --- repository helpers ---------------------------------------------------------

repo_top=
git_dir=
common=
image_root=/scan-image-root
require_repo() {
    [ -z "$repo_top" ] || return 0
    repo_top=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a Git working tree"
    repo_top=$(CDPATH='' cd -- "$repo_top" && pwd -P)
    # A hook gets the index of the commit in GIT_INDEX_FILE: a relative
    # ".git/index", or a temporary index for "git commit -a" and
    # "git commit <path>". Make it absolute before the cd below; run_gitleaks
    # passes it to the container.
    if [ -n "${GIT_INDEX_FILE:-}" ]; then
        case $GIT_INDEX_FILE in
            /*) ;;
            *) GIT_INDEX_FILE=$(pwd -P)/$GIT_INDEX_FILE ;;
        esac
        export GIT_INDEX_FILE
    fi
    # The Git commands below run at the top of the working tree. There a
    # relative --git-dir or --git-common-dir is relative to the current
    # directory in each Git version, so --path-format=absolute (Git 2.31) is
    # not needed.
    cd -- "$repo_top"
    common=$(git rev-parse --git-common-dir)
    common=$(CDPATH='' cd -- "$common" && pwd -P)
    # gitleaks git runs on the Git directory of this working tree (its HEAD
    # and index). A .gitleaksignore in the working tree then does not apply.
    git_dir=$(git rev-parse --git-dir)
    git_dir=$(CDPATH='' cd -- "$git_dir" && pwd -P)
}

default_level() {
    [ "$mode" = image ] && { echo fail; return; }
    if [ -n "${SCAN_REF:-}" ]; then
        refs=$SCAN_REF
    else
        refs=$(git symbolic-ref -q --short HEAD 2>/dev/null || true)
        refs="$refs $(git tag --points-at HEAD 2>/dev/null | tr '\n' ' ')"
    fi
    for r in $refs; do
        case $r in
            portable|refs/heads/portable|portable-v*|refs/tags/portable-v*) echo fail; return ;;
        esac
    done
    echo warn
}

# --- scans --------------------------------------------------------------------
# Each scan appends "rule<TAB>file<TAB>line<TAB>commit" lines to $work/findings,
# with file relative to the scanned root.

# strip_prefix <prefix>: remove <prefix> from the file column.
strip_prefix() {
    awk -F '\t' -v OFS='\t' -v p="$1" 'index($2, p) == 1 { $2 = substr($2, length(p) + 1) } { print }'
}
# strip_archive: remove "<archive>!" from the file column.
strip_archive() {
    awk -F '\t' -v OFS='\t' '{ sub(/^[^!]*!/, "", $2); print }'
}
# map_meta: a finding in a helper file of a meta/ directory names the commit
# message or the file names, not the helper file. "(file names):N" is line N
# of the name list: git ls-files (tree), git diff --cached --name-only
# (staged), or the files that the commit changes (history).
map_meta() {
    awk -F '\t' -v OFS='\t' '
        $2 ~ /^meta\/msg-/ { $4 = substr($2, 10); $2 = "(commit message)" }
        $2 ~ /^meta\/names-/ { c = substr($2, 12); $2 = "(file names)"; $4 = (c ~ /^[0-9a-f]+$/) ? c : "" }
        { print }'
}

# hexdump_head: the first 264 bytes of stdin as " xx xx ...".
hexdump_head() {
    od -An -tx1 -v -N 264 | tr '\n' ' ' | tr -s ' '
}

# classify: read "path<TAB>commit<TAB>hex" lines and write policy findings.
# Ignore files: .gitleaksignore, .gitleaksbaseline (any directory, any case).
# Local files: a path in the directory .local/ at the root. It holds the values
# of one host (.local/host-values.deny) and is not tracked.
# Extensions: zip jar war ear whl egg apk aab ipa nupkg tar tgz taz tbz tbz2
#   tb2 txz tlz tzst gz bz2 xz zst lz lz4 lzma lzo z 7z rar cab cpio ar deb rpm
#   iso dmg sql dump pgdump backup db sqlite sqlite3 db3 mdb accdb rdb bundle pack
# Content: gzip, compress, zip, bzip2, xz, zstd, 7z, rar, ar/deb, lz4, lzip,
#   rpm, cab, tar (ustar at byte 257), PostgreSQL custom dump (PGDMP), SQLite,
#   Git bundle v2 and v3, Git pack, Redis dump, and a text dump with the header
#   of pg_dump, mysqldump or mariadb-dump in the first 264 bytes.
classify() {
    awk -F '\t' -v OFS='\t' '
    function h(s,   r, i) { r = ""; for (i = 1; i < length(s); i += 2) r = r " " substr(s, i, 2); return r }
    BEGIN {
        n = split("1f8b 1f9d 504b0304 504b0506 504b0708 425a68 fd377a585a00 28b52ffd " \
            "377abcaf271c 526172211a07 213c617263683e0a 04224d18 4c5a4950 edabeedb " \
            "4d534346 5047444d50 53514c69746520666f726d6174203300 " \
            "23207632206769742062756e646c65 23207633206769742062756e646c65 " \
            "5041434b00000002 5041434b00000003 524544495330", m, " ")
        for (i = 1; i <= n; i++) magic[i] = h(m[i])
        tar = h("7573746172")
        k = split("506f737467726553514c2064617461626173652064756d70 4d7953514c2064756d70 " \
            "4d6172696144422064756d70", t, " ")
        for (i = 1; i <= k; i++) text[i] = h(t[i])
        ext = "\\.(zip|jar|war|ear|whl|egg|apk|aab|ipa|nupkg|tar|tgz|taz|tbz|tbz2|tb2|txz|tlz|tzst|" \
            "gz|bz2|xz|zst|lz|lz4|lzma|lzo|z|7z|rar|cab|cpio|ar|deb|rpm|iso|dmg|" \
            "sql|dump|pgdump|backup|db|sqlite|sqlite3|db3|mdb|accdb|rdb|bundle|pack)$"
    }
    {
        path = $1; name = path; sub(/.*\//, "", name); name = tolower(name)
        if (name == ".gitleaksignore" || name == ".gitleaksbaseline") {
            print "policy-ignore-file", path, "", $2
            next
        }
        if (index(path, ".local/") == 1) {
            print "policy-local-file", path, "", $2
            next
        }
        hit = (name ~ ext)
        x = " " $3
        gsub(/  +/, " ", x)
        for (i = 1; !hit && i <= n; i++) hit = (index(x, magic[i]) == 1)
        if (!hit) hit = (substr(x, 257 * 3 + 1, length(tar)) == tar)
        for (i = 1; !hit && i <= k; i++) hit = (index(x, text[i]) > 0)
        if (hit) print "policy-archive", path, "", $2
    }'
}

# classify_blobs <file>: <file> has "blob<TAB>path<TAB>commit" lines.
classify_blobs() {
    awk -F '\t' '!seen[$1]++ { print $1 }' "$1" | while IFS= read -r id; do
        printf '%s\t%s\n' "$id" "$(git cat-file blob "$id" | hexdump_head)"
    done >"$work/hex"
    awk -F '\t' -v OFS='\t' -v hexf="$work/hex" '
        BEGIN { while ((getline l < hexf) > 0) { split(l, a, "\t"); hex[a[1]] = a[2] } }
        { print $2, $3, hex[$1] }' "$1" | classify >>"$work/findings"
}

scan_tree() {
    require_repo
    rm -rf "$work/tree"
    mkdir -p "$work/tree/worktree" "$work/tree/index" "$work/tree/meta"
    # Tracked files as they are in the working tree (untracked and ignored files stay out).
    # shellcheck disable=SC2016 # the inner sh expands $f
    git ls-files -z | xargs -0 sh -c \
        'for f do { [ -e "$f" ] || [ -L "$f" ]; } && printf "%s\0" "$f"; done; :' sh |
        tar --null -T - -cf - | tar -xf - -C "$work/tree/worktree"
    # The staged content of each tracked file.
    git checkout-index --all --prefix="$work/tree/index/"
    # gitleaks does not read a symlink. Replace each symlink with a file that
    # holds its target text, as the modes staged and history see it.
    find "$work/tree/worktree" "$work/tree/index" -type l | while IFS= read -r l; do
        t=$(readlink -- "$l")
        rm -f -- "$l"
        printf '%s\n' "$t" >"$l"
    done
    git ls-files -z | tr '\0' '\n' >"$work/tree/meta/names-tree"
    (cd "$work/tree" && find worktree index -type f) | while IFS= read -r f; do
        printf '%s\t\t%s\n' "${f#*/}" "$(hexdump_head <"$work/tree/$f")"
    done | classify >>"$work/findings"
    gl none dir --max-archive-depth 3 "$work/tree" >"$work/raw" || die "gitleaks failed"
    strip_prefix "$work/tree/" <"$work/raw" | strip_prefix worktree/ | strip_prefix index/ |
        map_meta | sort -u >>"$work/findings"
}

scan_staged() {
    require_repo
    gl repo git --pre-commit --staged "$git_dir" >>"$work/findings" || die "gitleaks failed"
    # Each tracked file, not only the staged ones: the index is what the commit holds.
    git ls-files -s -z | tr '\0' '\n' | while IFS='	' read -r meta path; do
        # shellcheck disable=SC2086 # <mode> <blob> <stage>
        set -- $meta
        [ "$1" = 160000 ] || printf '%s\t%s\t\n' "$2" "$path"
    done >"$work/blobs"
    classify_blobs "$work/blobs"
    rm -rf "$work/staged"
    mkdir -p "$work/staged/meta"
    git diff --cached --name-only -z | tr '\0' '\n' >"$work/staged/meta/names-staged"
    gl none dir "$work/staged/meta" >"$work/raw" || die "gitleaks failed"
    strip_prefix "$work/staged/" <"$work/raw" | map_meta >>"$work/findings"
}

# scan_commits <range>: the messages, the file names and the new blobs of the
# commits in <range>. gitleaks reads neither messages nor names in git mode.
scan_commits() {
    rm -rf "$work/hist"
    mkdir -p "$work/hist/meta"
    # <range> is a list of git log arguments, for example "<sha> --not --remotes=origin".
    set -f
    # shellcheck disable=SC2086
    git rev-list $1 >"$work/commits" || die "git rev-list $1 failed"
    set +f
    : >"$work/blobs"
    while IFS= read -r c; do
        git log -1 --format=%B "$c" >"$work/hist/meta/msg-$c"
        # :<old mode> <new mode> <old blob> <new blob> <status><TAB><path>
        git -c core.quotePath=false diff-tree -r -m --root --no-commit-id --no-abbrev "$c" >"$work/diff"
        cut -f 2- "$work/diff" >"$work/hist/meta/names-$c"
        awk -F '\t' -v OFS='\t' -v c="$c" '{ split($1, a, " ") }
            a[5] != "D" && a[2] != "160000" { print a[4], $2, c }' "$work/diff" >>"$work/blobs"
    done <"$work/commits"
    classify_blobs "$work/blobs"
    gl none dir "$work/hist/meta" >"$work/raw" || die "gitleaks failed"
    strip_prefix "$work/hist/" <"$work/raw" | map_meta >>"$work/findings"
}

scan_history() {
    require_repo
    gl repo git --log-opts="$1" "$git_dir" >>"$work/findings" || die "gitleaks failed"
    scan_commits "$1"
}

scan_image() {
    command -v docker >/dev/null 2>&1 || die "mode image needs docker"
    docker image inspect "$1" >/dev/null 2>&1 || die "image $1 is not present locally"
    if [ "$engine" = docker ] && [ "${SCAN_IMAGE_METHOD:-mount}" = mount ]; then
        # Docker 28 and later mount an image read-only, with no container of it.
        if gl "image=$1" dir "$image_root" >"$work/raw" 2>"$work/err"; then
            strip_prefix "$image_root/" <"$work/raw" >>"$work/findings"
            return
        fi
        echo "scan: image mount failed, using docker export" >&2
    fi
    export_cid=$(docker create --network none --name "${SCAN_NAME_PREFIX}export-$$" "$1" /scan-no-command) ||
        die "cannot create a container of $1"
    docker export -o "$work/rootfs.tar" "$export_cid"
    docker rm -f "$export_cid" >/dev/null
    export_cid=
    gl none dir --max-archive-depth 1 "$work/rootfs.tar" >"$work/raw" || die "gitleaks failed"
    strip_archive <"$work/raw" >>"$work/findings"
}

# The layers of <reference> after the layers of <base>. Each layer is a tar
# file; gitleaks reads it as an archive, so no file of the image is unpacked.
scan_layers() {
    command -v docker >/dev/null 2>&1 || die "mode image needs docker"
    for i in "$1" "$2"; do
        docker image inspect "$i" >/dev/null 2>&1 || die "image $i is not present locally"
    done
    fmt='{{range .RootFS.Layers}}{{println .}}{{end}}'
    docker image inspect -f "$fmt" "$1" | sed '/^$/d' >"$work/layers.image"
    docker image inspect -f "$fmt" "$2" | sed '/^$/d' >"$work/layers.base"
    nbase=$(wc -l <"$work/layers.base" | tr -d ' ')
    head -n "$nbase" "$work/layers.image" | cmp -s - "$work/layers.base" ||
        die "$2 is not a base image of $1"
    docker save -o "$work/image.tar" "$1"
    tar -xf "$work/image.tar" -C "$work" manifest.json
    # manifest.json: [{"Config":...,"Layers":["<path>",...],...}], in RootFS order.
    tr -d '\n' <"$work/manifest.json" | sed 's/.*"Layers":\[\([^]]*\)\].*/\1/' |
        tr ',' '\n' | tr -d '" ' | awk 'NF' >"$work/layer.paths"
    mkdir "$work/layers"
    n=0
    while IFS= read -r path; do
        n=$((n + 1))
        [ "$n" -gt "$nbase" ] || continue
        tar -xOf "$work/image.tar" "$path" >"$work/layers/layer-$n.tar"
    done <"$work/layer.paths"
    rm -f "$work/image.tar"
    echo "scan: $((n - nbase)) add-on layer(s) of $n" >&2
    gl none dir --max-archive-depth 1 "$work/layers" >"$work/raw" || die "gitleaks failed"
    strip_archive <"$work/raw" >>"$work/findings"
}

selftest() {
    # The tokens are built at run time, so no tracked file contains them.
    mkdir -p "$work/selftest"
    tok=$(printf '%s%s%s%s' ghp _ Zx9QwEr7TyUi3OpA s5DfGh1JkLz2XcVbNm4Q)
    # Line 2: a token that contains "false" (the anchored allowlist of .gitleaks.toml).
    # Line 3: a "gitleaks:allow" comment does not hide a finding.
    tokf=$(printf '%s%s%s%s' ghp _ falseZx9QwEr7TyUi3O pAs5DfGh1JkLz2XcV)
    printf 'token = "%s"\ntoken = "%s"\ntoken = "%s" # gitleaks:%s\n' "$tok" "$tokf" "$tok" allow \
        >"$work/selftest/token.txt"
    # One line for each deny pattern and each regex sample; each must give a finding.
    { grep -v ' (regex)$' "$work/patterns" | cut -f 2; cut -f 2 "$work/samples"; } |
        sed 's/^/host = /' >"$work/selftest/host.txt"
    # No finding: version pins, loopback and documentation addresses.
    v=$(printf '%s.%s' 10 2.3.4)
    {
        printf 'nothing here\n'
        printf 'pin = "pkg==%s" "pkg>=%s" pkg-%s pkg:%s v%s %s.dev0\n' "$v" "$v" "$v" "$v" "$v" "$v"
        printf 'loopback = %s.0.0.1 ::1\n' 127
        printf 'docs = %s.0.2.10 %s.51.100.7 %s.0.113.9 %s:db8::1\n' 192 198 203 2001
    } >"$work/selftest/clean.txt"
    gl none dir "$work/selftest" >"$work/raw0" || die "gitleaks failed"
    strip_prefix "$work/selftest/" <"$work/raw0" >"$work/raw"
    ok=0
    for line in 1 2 3; do
        grep -q "^github-pat	token.txt	$line	" "$work/raw" ||
            { echo "selftest: FAIL - synthetic token on line $line not found" >&2; ok=1; }
    done
    while IFS='	' read -r id p; do
        grep -q "^$id	host.txt	" "$work/raw" || { echo "selftest: FAIL - host value $id ($p) not found" >&2; ok=1; }
    done <"$work/patterns"
    ! grep -q "	clean.txt	" "$work/raw" || { echo "selftest: FAIL - finding in a clean file" >&2; ok=1; }
    [ "$ok" -eq 0 ] && echo "selftest: ok - the rules find the synthetic tokens and each host value"
    return "$ok"
}

# --- main -----------------------------------------------------------------------

select_engine
build_config
: >"$work/findings"

if [ "$mode" = selftest ]; then
    selftest
    exit $?
fi

[ -n "$level" ] || level=$(default_level)

case $mode in
    tree) scan_tree ;;
    staged) scan_staged ;;
    history) scan_history "$arg" ;;
    image) if [ -n "$base" ]; then scan_layers "$arg" "$base"; else scan_image "$arg"; fi ;;
    all) scan_tree; scan_history HEAD ;;
esac

# Drop host-value findings in the two deny lists (repository modes only).
# Secret rules still scan these files, and no other file gets this exception.
# The local list is not tracked, so only a tracked copy can reach a scan, and
# a tracked file in .local/ is a policy finding.
if [ "$mode" = image ]; then
    cp "$work/findings" "$work/kept"
else
    awk -F '\t' -v f="$DENY_REL" -v l="$LOCAL_DENY_REL" \
        '!($1 ~ /^host-value-/ && ($2 == f || $2 == l))' "$work/findings" >"$work/kept"
fi
tab=$(printf '\t')
sort -t "$tab" -u -k2,2 -k3,3n -k1,1 -k4,4 "$work/kept" >"$work/sorted"

awk -F '\t' -v level="$level" -v pfile="$work/patterns" '
BEGIN {
    while ((getline l < pfile) > 0) { split(l, a, "\t"); pat[a[1]] = a[2] }
    pat["policy-ignore-file"] = "gitleaks ignore or baseline file"
    pat["policy-archive"] = "archive, database dump or Git bundle"
    pat["policy-local-file"] = "tracked file in .local/"
}
NF < 2 { next }
{
    where = ($3 != "") ? $2 ":" $3 : $2
    at = ($4 != "") ? "  commit " substr($4, 1, 12) : ""
    if ($1 ~ /^host-value-/) {
        hosts++
        printf "%s  host value  %s  rule %s (%s)%s\n", (level == "fail" ? "FAIL" : "warn"), where, $1, pat[$1], at
    } else if ($1 ~ /^policy-/) {
        policy++
        printf "FAIL  policy  %s  rule %s (%s)%s\n", where, $1, pat[$1], at
    } else {
        secrets++
        printf "FAIL  secret  %s  rule %s%s\n", where, $1, at
    }
}
END {
    printf "scan: %d secret finding(s), %d host-value finding(s), %d policy finding(s), level %s\n",
        secrets, hosts, policy, level
    exit (secrets > 0 || policy > 0 || (hosts > 0 && level == "fail")) ? 1 : 0
}' "$work/sorted"
