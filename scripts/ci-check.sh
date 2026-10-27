#!/bin/sh
# Validate a checkout without host secrets or checkout bind mounts.
set -eu
cd "$(dirname "$0")/.."
export SCAN_ENGINE=binary
sh scripts/check-prereqs.sh
python3 -m unittest discover -s tests -p test_update_upstream.py
python3 -m unittest discover -s tests -p test_architecture_html.py
sh scripts/scan.sh selftest
sh scripts/public_check.sh selftest
sh scripts/scan.sh --level fail tree
sh scripts/public_check.sh --level fail tree
for test in tests/test_*.sh; do
    case "$test" in tests/test_image.sh) continue ;; esac
    sh "$test"
done
sh scripts/build.sh test
# A unique local tag cannot replace an image used by a running service.
tag="ci-$(git rev-parse --short HEAD)-$$"
image="litellm-gateway:$tag"
cleanup() { docker image rm "$image" >/dev/null 2>&1 || :; }
trap cleanup EXIT
trap 'exit 130' INT TERM
TAG=$tag sh scripts/build.sh
sh tests/test_image.sh "$image"
base=$(awk -F= '/^ARG BASE_IMAGE=/ {print $2; exit}' Dockerfile)
docker pull "$base" >/dev/null
sh scripts/scan.sh --level fail image "$image" "$base"
