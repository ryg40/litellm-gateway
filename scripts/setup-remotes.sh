#!/bin/sh
# Create the fetch-only remote "upstream" (BerriAI/litellm) in this clone.
#
# A clone does not copy remote configuration, so run this once per clone.
# See docs/remotes.md for the reasons behind each setting.
#
# Usage: scripts/setup-remotes.sh [--check]
#   --check  change nothing; exit 0 if the configuration is correct,
#            exit 1 with a message for each wrong setting.
# Environment:
#   UPSTREAM_URL  replaces https://github.com/BerriAI/litellm.git (for a mirror)

set -eu

UPSTREAM_URL=${UPSTREAM_URL:-https://github.com/BerriAI/litellm.git}
PUSH_URL=DISABLED
TAG_OPT=--no-tags
# A wildcard refspec that matches no upstream ref: a plain "git fetch upstream"
# then transfers nothing. Use scripts/fetch-upstream.sh to fetch one tag.
FETCH_REFSPEC='+refs/upstream-none/*:refs/upstream/none/*'

usage() {
    echo "usage: $0 [--check]" >&2
    exit 2
}

mode=setup
case $# in
    0) ;;
    1) [ "$1" = --check ] || usage; mode=check ;;
    *) usage ;;
esac

git rev-parse --git-dir >/dev/null 2>&1 || {
    echo "error: not inside a Git repository" >&2
    exit 2
}

# Print a URL without user name, password or token.
strip_credentials() {
    printf '%s\n' "$1" | sed 's#://[^/@]*@#://#'
}

# Print the values of a multi-value config key, or nothing.
config_all() {
    git config --get-all "$1" 2>/dev/null || true
}

report_origin() {
    origin_url=$(git config --get remote.origin.url 2>/dev/null || true)
    if [ -n "$origin_url" ]; then
        echo "origin: $(strip_credentials "$origin_url") (not changed)"
    else
        echo "warning: remote 'origin' is absent" >&2
    fi
}

check() {
    bad=0
    if ! git config --get remote.upstream.url >/dev/null 2>&1; then
        echo "check: remote 'upstream' is absent" >&2
        return 1
    fi
    url=$(config_all remote.upstream.url)
    [ "$url" = "$UPSTREAM_URL" ] || {
        echo "check: remote.upstream.url is '$(strip_credentials "$url")', expected '$(strip_credentials "$UPSTREAM_URL")'" >&2
        bad=1
    }
    pushurl=$(config_all remote.upstream.pushurl)
    [ "$pushurl" = "$PUSH_URL" ] || {
        echo "check: remote.upstream.pushurl is '$(strip_credentials "$pushurl")', expected '$PUSH_URL'" >&2
        bad=1
    }
    tagopt=$(config_all remote.upstream.tagOpt)
    [ "$tagopt" = "$TAG_OPT" ] || {
        echo "check: remote.upstream.tagOpt is '$tagopt', expected '$TAG_OPT'" >&2
        bad=1
    }
    fetch=$(config_all remote.upstream.fetch)
    [ "$fetch" = "$FETCH_REFSPEC" ] || {
        echo "check: remote.upstream.fetch is '$fetch', expected '$FETCH_REFSPEC'" >&2
        bad=1
    }
    mirror=$(config_all remote.upstream.mirror)
    case $mirror in
        '' | false) ;;
        *)
            echo "check: remote.upstream.mirror is '$mirror', expected unset" >&2
            bad=1
            ;;
    esac
    return $bad
}

if [ "$mode" = check ]; then
    report_origin
    if check; then
        echo "upstream: configuration is correct"
        exit 0
    fi
    echo "upstream: configuration is wrong; run $0 to fix it" >&2
    exit 1
fi

if git config --get remote.upstream.url >/dev/null 2>&1; then
    git remote set-url upstream "$UPSTREAM_URL"
else
    # --no-tags writes tagOpt; the fetch refspec is replaced below.
    git remote add --no-tags upstream "$UPSTREAM_URL"
fi
# Remove every push URL, then set the one disabled value.
git config --unset-all remote.upstream.pushurl 2>/dev/null || true
git remote set-url --push upstream "$PUSH_URL"
git config remote.upstream.tagOpt "$TAG_OPT"
git config --unset-all remote.upstream.fetch 2>/dev/null || true
git config remote.upstream.fetch "$FETCH_REFSPEC"
git config --unset-all remote.upstream.mirror 2>/dev/null || true

report_origin
check
echo "upstream: $(strip_credentials "$UPSTREAM_URL") (fetch only, push $PUSH_URL, tagOpt $TAG_OPT)"
