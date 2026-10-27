#!/bin/sh
# Install hash-pinned CI tools in a disposable Node 24 Debian job container.
set -eu
[ "$(uname -m)" = x86_64 ] || { echo 'CI tools require Linux amd64' >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM
fetch() {
    curl --fail --silent --show-error --location --retry 3 "$1" -o "$work/$2"
    printf '%s  %s\n' "$3" "$work/$2" | sha256sum -c -
}
fetch https://download.docker.com/linux/static/stable/x86_64/docker-29.7.2.tgz docker.tgz \
    803d433f226db4776e1768fd319fc6c6e4935a456acf84fcc0080818b854bc8f
# The CLI sends contexts to BuildKit; it does not bind the runner checkout.
tar -xzf "$work/docker.tgz" -C "$work" docker/docker
install -m 0755 "$work/docker/docker" /usr/local/bin/docker
mkdir -p "$HOME/.docker/cli-plugins"
fetch https://github.com/docker/compose/releases/download/v5.5.0/docker-compose-linux-x86_64 compose \
    c57ab918abd5b05ca7e7d0f275875dd1330a695074f309dc9eab1b49efafcd4b
install -m 0755 "$work/compose" "$HOME/.docker/cli-plugins/docker-compose"
fetch https://github.com/docker/buildx/releases/download/v0.36.1/buildx-v0.36.1.linux-amd64 buildx \
    48af8a397ebd60178778bf63611dbcebe5f5e7a9be90eb9147b24b9587455778
install -m 0755 "$work/buildx" "$HOME/.docker/cli-plugins/docker-buildx"
fetch https://github.com/gitleaks/gitleaks/releases/download/v8.28.0/gitleaks_8.28.0_linux_x64.tar.gz gitleaks.tgz \
    a65b5253807a68ac0cafa4414031fd740aeb55f54fb7e55f386acb52e6a840eb
tar -xzf "$work/gitleaks.tgz" -C "$work" gitleaks
install -m 0755 "$work/gitleaks" /usr/local/bin/gitleaks-real
# scan.sh expects the release spelling, but the official binary omits its v.
printf '%s\n' '#!/bin/sh' \
    'if [ "$#" = 1 ] && [ "$1" = version ]; then printf "v"; fi' \
    'exec /usr/local/bin/gitleaks-real "$@"' >/usr/local/bin/gitleaks
chmod 0755 /usr/local/bin/gitleaks
sh scripts/check-prereqs.sh
docker info >/dev/null
