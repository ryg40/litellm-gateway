#!/bin/sh
# Build the gateway image with docker buildx bake. docs/image.md describes it.
#
# Usage: scripts/build.sh [options] [gateway|test|release]
#   gateway   host platform, loaded into the local image store (default)
#   test      run the repo tests inside the image, host platform
#   release   linux/amd64 and linux/arm64; needs --push or --oci
# gateway and release run a smoke step first: the bake target smoke starts
# Python in the image of the host platform, so an untested LiteLLM version
# fails the build.
# Options:
#   --push           push the release to REGISTRY (nothing is pushed without it)
#   --oci FILE       write the release as an OCI archive to FILE
#   --builder NAME   use this buildx builder
#   --print          print the bake definition and stop
#
# Variables from the environment: REGISTRY, IMAGE_NAME, TAG, BASE_IMAGE,
# LITELLM_VERSION, PORTABLE_VERSION, SOURCE_URL, PROVENANCE, SOURCE_DATE_EPOCH,
# PIP_INDEX_URL (package index of the test target), PIP_CA_FILE (CA bundle for
# that index, given to the build as the secret pip-ca).
# Portable POSIX sh; works with the BSD tools of macOS and the GNU tools of Linux.
set -eu

die() {
	printf 'build.sh: %s\n' "$*" >&2
	exit 1
}

target=gateway
push=0
oci=
builder=
print=0
while [ "$#" -gt 0 ]; do
	case "$1" in
	--push) push=1 ;;
	--oci)
		[ "$#" -ge 2 ] || die "--oci needs a file"
		oci=$2
		shift
		;;
	--builder)
		[ "$#" -ge 2 ] || die "--builder needs a name"
		builder=$2
		shift
		;;
	--print) print=1 ;;
	gateway | test | release) target=$1 ;;
	-h | --help)
		sed -n '2,21p' "$0"
		exit 0
		;;
	*) die "unknown argument: $1" ;;
	esac
	shift
done

# A relative PIP_CA_FILE is relative to the directory of the call, not the repo.
case ${PIP_CA_FILE:-} in
'' | /*) ;;
*) PIP_CA_FILE=$PWD/$PIP_CA_FILE ;;
esac
cd "$(dirname "$0")/.."
command -v git >/dev/null 2>&1 || die "git not found"
command -v docker >/dev/null 2>&1 || die "docker not found"

if [ "$target" = release ]; then
	[ "$push" = 1 ] || [ -n "$oci" ] || [ "$print" = 1 ] || die "release needs --push or --oci FILE"
	[ -z "$(git status --porcelain)" ] || die "release refuses a dirty tree; commit or remove the changes"
else
	[ "$push" = 0 ] && [ -z "$oci" ] || die "--push and --oci are for the release target only"
fi

REVISION=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain)" ]; then
	REVISION="$REVISION-dirty"
fi

# The commit time gives the same timestamps in two builds of the same revision.
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct HEAD)}
# GNU date takes -d @N, BSD date takes -r N.
CREATED=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null ||
	date -u -r "$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ) || die "cannot format SOURCE_DATE_EPOCH"

# portable-vX.Y.Z at HEAD gives X.Y.Z. Otherwise git describe gives X.Y.Z-<commits>-g<hash>,
# or 0-g<hash> when no portable tag exists.
if [ -z "${PORTABLE_VERSION:-}" ]; then
	if described=$(git describe --tags --match 'portable-v*' HEAD 2>/dev/null); then
		PORTABLE_VERSION=${described#portable-v}
	else
		PORTABLE_VERSION="0-g$(git rev-parse --short HEAD)"
	fi
fi

export REVISION SOURCE_DATE_EPOCH CREATED PORTABLE_VERSION

# Give the file explicitly: bake also reads compose.yaml when no file is given.
set -- -f docker-bake.hcl
[ -n "$builder" ] && set -- "$@" --builder "$builder"
# Optional CA bundle for the package index of the test target (TLS inspection).
if [ -n "${PIP_CA_FILE:-}" ]; then
	[ -r "$PIP_CA_FILE" ] || die "PIP_CA_FILE is not a readable file: $PIP_CA_FILE"
	set -- "$@" --set "test.secrets=id=pip-ca,src=$PIP_CA_FILE"
fi
if [ "$print" = 1 ]; then
	exec docker buildx bake "$@" --print "$target"
fi
printf 'build.sh: target %s, revision %s, portable version %s, SOURCE_DATE_EPOCH %s\n' \
	"$target" "$REVISION" "$PORTABLE_VERSION" "$SOURCE_DATE_EPOCH" >&2
# Smoke step before gateway and release, without the output settings of the release.
if [ "$target" != test ]; then
	docker buildx bake "$@" smoke ||
		die "smoke step failed: Python does not start in the image; read the message above"
fi
# rewrite-timestamp sets the file times in the layers to SOURCE_DATE_EPOCH.
if [ "$push" = 1 ]; then
	set -- "$@" --set "release.output=type=image,push=true,rewrite-timestamp=true"
elif [ -n "$oci" ]; then
	oci_dir=$(cd "$(dirname "$oci")" && pwd) || die "no directory for $oci"
	set -- "$@" --allow "fs.write=$oci_dir" \
		--set "release.output=type=oci,dest=$oci_dir/$(basename "$oci"),rewrite-timestamp=true"
fi

exec docker buildx bake "$@" "$target"
