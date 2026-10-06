#!/bin/sh
# Fetch one upstream release tag into refs/upstream/tags/<tag>.
#
# The fetch is shallow (--depth 1) and has no file contents
# (--filter=blob:none); Git gets a blob from upstream when a command needs it.
# Upstream tags never enter refs/tags/. See docs/remotes.md.
#
# Usage: scripts/fetch-upstream.sh v1.103.0
# Prints the commit ID of the tag.

set -eu

[ $# -eq 1 ] || {
    echo "usage: $0 v<major>.<minor>.<patch>[suffix]" >&2
    exit 2
}
tag=$1

# v<digits>.<digits>.<digits>, then an optional suffix such as -stable or
# -nightly.1 that starts with '-', '.' or '+'.
if ! printf '%s\n' "$tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+([-.+][A-Za-z0-9][A-Za-z0-9.+-]*)?$'; then
    echo "error: '$tag' is not a release tag name (v<digits>.<digits>.<digits>[suffix])" >&2
    exit 2
fi
# grep tests each line on its own, so refuse white space (a newline) here.
case $tag in
    *..* | *. | *[[:space:]]*) echo "error: '$tag' is not a valid tag name" >&2; exit 2 ;;
esac

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
"$script_dir/setup-remotes.sh" --check >/dev/null || {
    echo "error: remote 'upstream' is not configured; run $script_dir/setup-remotes.sh" >&2
    exit 1
}

ref=refs/upstream/tags/$tag
git fetch --quiet --no-tags --depth 1 --filter=blob:none upstream \
    "+refs/tags/$tag:$ref"
git rev-parse --verify --quiet "$ref^{commit}"
