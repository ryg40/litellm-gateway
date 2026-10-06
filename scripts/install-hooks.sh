#!/bin/sh
# Use the hooks in scripts/git-hooks (pre-commit, pre-merge-commit, pre-push)
# in this repository.
#
# The script copies scripts/git-hooks/dispatch to <git-common-dir>/scan-hooks,
# once for each hook, and sets core.hooksPath to that absolute directory. Git
# stores it in the shared configuration, so it applies to the main checkout and
# to each worktree. The dispatcher runs scripts/git-hooks/<hook> of the working
# tree where the hook runs, so each worktree uses the hooks and scripts/scan.sh
# of its own branch. On a branch that never had the scan, the dispatcher runs
# no scan and says why; if the branch has the scan in the index or in HEAD but
# not in the working tree, it refuses. See docs/secret-handling.md.
#
# Usage: scripts/install-hooks.sh [--check|--uninstall]
#   --check      change nothing; exit 0 if the hooks are installed and current, else 1
#   --uninstall  remove core.hooksPath and the dispatcher copies if this script set them

set -eu

HOOKS="pre-commit pre-merge-commit pre-push"
# The value of core.hooksPath that older versions of this script set.
OLD_HOOKS_PATH=scripts/git-hooks

usage() {
    echo "usage: $0 [--check|--uninstall]" >&2
    exit 2
}

mode=install
case $# in
    0) ;;
    1) case $1 in
           --check) mode=check ;;
           --uninstall) mode=uninstall ;;
           *) usage ;;
       esac ;;
    *) usage ;;
esac

git rev-parse --git-dir >/dev/null 2>&1 || {
    echo "error: not inside a Git repository" >&2
    exit 2
}

top=$(git rev-parse --show-toplevel)
# Relative to the top of the working tree in each Git version.
common=$(cd -- "$top" && CDPATH='' cd -- "$(git rev-parse --git-common-dir)" && pwd -P)
dir=$common/scan-hooks
source=$top/scripts/git-hooks/dispatch
current=$(git config --get core.hooksPath || true)

case $mode in
    check)
        if [ "$current" != "$dir" ]; then
            echo "not installed: core.hooksPath is '${current:-unset}', expected $dir" >&2
            exit 1
        fi
        for h in $HOOKS; do
            if [ ! -x "$dir/$h" ] || ! cmp -s "$source" "$dir/$h"; then
                echo "not current: $dir/$h differs from scripts/git-hooks/dispatch; run $0" >&2
                exit 1
            fi
        done
        echo "ok: core.hooksPath is $dir"
        exit 0
        ;;
    uninstall)
        if [ "$current" = "$dir" ] || [ "$current" = "$OLD_HOOKS_PATH" ]; then
            git config --unset core.hooksPath
            echo "removed core.hooksPath"
        else
            echo "core.hooksPath is '${current:-unset}'; nothing to remove"
        fi
        if [ -d "$dir" ]; then
            for h in $HOOKS; do rm -f "$dir/$h"; done
            rmdir "$dir" 2>/dev/null || true
        fi
        ;;
    install)
        if [ -n "$current" ] && [ "$current" != "$dir" ] && [ "$current" != "$OLD_HOOKS_PATH" ]; then
            echo "error: core.hooksPath is already '$current'; remove it first" >&2
            exit 1
        fi
        [ -f "$source" ] || {
            echo "error: scripts/git-hooks/dispatch is missing" >&2
            exit 1
        }
        mkdir -p "$dir"
        for h in $HOOKS; do
            cp "$source" "$dir/$h.tmp"
            chmod 755 "$dir/$h.tmp"
            mv -f "$dir/$h.tmp" "$dir/$h"
        done
        git config core.hooksPath "$dir"
        echo "core.hooksPath = $dir (main checkout and all worktrees)"
        ;;
esac
