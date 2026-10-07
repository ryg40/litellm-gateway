# Run the stack on a Mac

This page starts the stack on a Mac with Apple silicon (`arm64`).
The steps are the same as on Linux. The differences are the container runtime, the shared paths, the shell and TLS inspection.

The shell on a Mac is zsh, and the shell profile is `~/.zshrc`. Each line that this page adds to the profile goes into `~/.zshrc`, not into `~/.bashrc`. The scripts of the repo run with `sh` and need no change.

On a Mac, Podman replaces Docker Desktop. Colima is an alternative; test it before you use it (section "Colima, the alternative").
Linux hosts and CI runners use Docker ([ci.md](ci.md)). Each Homebrew formula on this page has an `arm64` bottle.

Not verified: these steps on a Mac. Each step has a verification note.
The steps come from a run on Linux amd64, except where a step says otherwise.
The statements about Podman, Colima and Buildx come from their documentation and source code (section "Sources").

## What the repo needs

| Need | Why |
| --- | --- |
| A container runtime with a Linux `arm64` VM: Podman | All commands use the Docker CLI against the Podman socket. |
| The Docker CLI with the Compose plugin (`docker compose`), Compose 2.35.0 or later | `compose.yaml` starts the stack. `scripts/login-codex-account.sh` uses `docker compose config --no-env-resolution`. |
| Node.js, major version 24 | A hard requirement of the build pipelines ([ci.md](ci.md)). `scripts/check-prereqs.sh` refuses another major version. No script of this repo runs Node. |
| File sharing for the repository and `$TMPDIR` | Compose mounts files from the checkout. `scripts/scan.sh` mounts temporary files. |
| `git` 2.24 or later, `sh`, `python3` 3.9 or later | Scripts and tests. macOS has `python3` with the Command Line Tools. |

Not needed on a Mac: Buildx. `scripts/build.sh` uses `docker buildx bake`, which Podman does not have. `podman build` builds the same `Dockerfile` (section "Steps", step 5).

The minimum versions come from the upstream release where each option first appears. Not verified: a run with these versions.
`sh scripts/check-prereqs.sh --runtime podman` checks each tool and stops with exit code 1 when a hard requirement is missing.

The base images have an `arm64` variant: the LiteLLM image and the Postgres image are pinned by index digest with `linux/amd64` and `linux/arm64`.
Not verified: the `arm64` gateway image at run time.

## Container runtime: Podman

Podman runs a Linux VM (`podman machine`) with the Apple Virtualization framework. The Homebrew Docker CLI and the
Compose plugin talk to the Podman socket through `DOCKER_HOST`. Podman is installed and confirmed on the target Mac;
the stack on it is not verified.

### Install Podman and the tools

Not verified on a Mac.

1. Install Podman, the Docker CLI, the Compose plugin and Node 24. Each formula has an `arm64` bottle.

   ```sh
   brew install podman docker docker-compose node@24
   ```

2. Tell the Docker CLI where Homebrew puts the Compose plugin. The Homebrew formula of `docker-compose`
   gives this setting. Add the key to `~/.docker/config.json`; keep the other keys of the file.

   ```json
   {
     "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
   }
   ```

   `/opt/homebrew` is the Homebrew prefix on Apple silicon. `brew --prefix` prints it.
3. Put Node 24 on `PATH`. The formula `node@24` is keg-only when another Node version is the current `node` formula.
   Add the line to `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`.

   ```sh
   export PATH="$(brew --prefix node@24)/bin:$PATH"
   ```

   `node --version` then prints `v24.` and a minor version.
4. Create and start the VM. The defaults are small; the image build and the tests need more.
   `--cpus`, `--memory` and `--disk-size` change later with `podman machine set`.

   ```sh
   podman machine init --cpus 4 --memory 8192 --disk-size 60
   podman machine start
   ```

   The VM is `arm64` on Apple silicon. It shares `/Users`, `/private` and `/var/folders` by default, so the
   checkout under `$HOME` and the macOS `$TMPDIR` are visible in the VM.
5. Point the Docker CLI at the Podman socket. Add the line to `~/.zshrc`, then open a new terminal or run `source ~/.zshrc`. `podman machine start` prints the same path.

   ```sh
   export DOCKER_HOST="unix://$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')"
   ```

6. Check the tools. The last command prints `check-prereqs: ok (podman)`.

   ```sh
   podman --version
   docker version
   docker compose version
   node --version
   sh scripts/check-prereqs.sh --runtime podman
   ```

   `docker version` shows a Podman server. The Docker API version of Podman is 1.40 or later.

### What does not work on Podman

| Command | Reason | What to do instead |
| --- | --- | --- |
| `scripts/build.sh` (targets `gateway`, `test`, `release`) | `docker buildx bake` needs BuildKit in the daemon. The Podman API has no BuildKit endpoint (`containers/podman#17836`, open), and Buildah has no `bake` command (`containers/buildah#4796`, open). | `podman build` for the image (step 5). `podman run` for the tests ([tests/README.md](../tests/README.md)). A Linux host or CI runner with Docker for `release`. |
| `docker compose up -d --build`, `docker compose build` | Compose v2 builds through Buildx. | Build with `podman build`, then `docker compose up -d` without `--build`. |

`Dockerfile` uses `RUN --network=none`, `RUN --mount=type=secret` and `FROM --platform=$BUILDPLATFORM`.
`podman build` documents `--network`, `--secret` and the platform build arguments. Not verified: `podman build` with this `Dockerfile`.

## Steps

1. Get the source. Clone the shared Git server, or clone a Git bundle. Keep the checkout under `$HOME`. Not verified on a Mac.

   ```sh
   cd ~
   git clone <url of the shared Git server> litellm
   ```

   With a bundle, clone the branch `portable`. [remotes.md](remotes.md) says how to point `origin` at a Git server after that.

   ```sh
   git clone -b portable litellm-portable.bundle litellm
   ```

2. Create the remote `upstream`. Not verified on a Mac. A clone does not copy the remote configuration.

   ```sh
   cd litellm
   scripts/setup-remotes.sh
   scripts/setup-remotes.sh --check
   ```

3. Check the tools. Not verified on a Mac. The command stops with exit code 1 when Node 24 or another hard requirement is missing.

   ```sh
   sh scripts/check-prereqs.sh --runtime podman
   ```

4. Create `.env`. Not verified on a Mac. The script gives each secret of the stack a new random value
   and writes mode 0600. It does not replace a `.env` that exists.

   ```sh
   python3 scripts/create-env.py
   ```

   Add the provider keys that you use, for example `OPENROUTER_API_KEY`.

   [host-configuration.md](host-configuration.md) lists the other settings.

5. Build the image with Podman. Not verified on a Mac. The build makes `litellm-gateway:local` for `linux/arm64`.

   ```sh
   podman build --target gateway -t litellm-gateway:local .
   podman image ls litellm-gateway
   ```

   The image has no revision label. `scripts/build.sh` sets the labels; it needs Docker.

6. Start the stack. Not verified on a Mac. Compose starts `gateway` and `database` from the image of step 5.

   ```sh
   docker compose up -d
   docker compose ps
   ```

   Do not add `--build`. `docker compose ps` shows the two services; after the start period each one shows `(healthy)`.

7. Check the gateway. Not verified on a Mac. The result is `"I'm alive!"`.

   ```sh
   curl http://127.0.0.1:4321/health/liveliness
   ```

   The first start takes up to 90 seconds.

8. Check the authentication and the model list. Not verified on a Mac. The script reads the master key from `.env`.

   ```sh
   python3 scripts/verify.py
   ```

9. Run the tests. Not verified on a Mac. Install `pytest` once into a separate directory, then run the tests inside
   the pinned LiteLLM image. [tests/README.md](../tests/README.md) has the commands with the image digest; replace `docker run` with `podman run`.
   The run ends without an error when each test passes.

   ```sh
   mkdir -p "$HOME/.cache/litellm-pytest"
   podman run --rm -v "$HOME/.cache/litellm-pytest":/pt python:3.13-slim-bookworm \
     pip install --no-cache-dir --target /pt pytest
   ```

   `sh tests/test_compose.sh` and `sh tests/test_check_prereqs.sh` run on the host.

10. Run the scan. Not verified on a Mac. `scripts/scan.sh` starts the scanner with `docker run`, which goes to Podman through `DOCKER_HOST`. Exit code 0 means no finding.

    ```sh
    docker pull zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854
    scripts/scan.sh tree
    sh tests/test_scan.sh
    ```

    [secret-handling.md](secret-handling.md) describes the modes and the levels.

To stop the stack:

```sh
docker compose down
```

Warning: `docker compose down -v` also deletes the database volume.

## Colima, the alternative

Colima runs the Docker Engine in a Linux VM, and the Homebrew Docker CLI, Compose and Buildx talk to it.
With Colima, `scripts/build.sh` and `docker compose up -d --build` work as on Linux, because the engine has BuildKit.
Colima is optional. Test it before you use it; Podman is the runtime of this page.

Not verified on a Mac: all steps of this section.

1. Install Colima and Buildx. The Docker CLI and the Compose plugin are the ones of section "Install Podman and the tools".

   ```sh
   brew install colima docker-buildx
   ```

   Add `/opt/homebrew/lib/docker/cli-plugins` to `cliPluginsExtraDirs` when it is not there yet.
2. Start the VM. The values of `--arch`, `--vm-type` and `--mount-type` cannot change after the first start.

   ```sh
   colima start --cpus 4 --memory 8 --arch aarch64 --vm-type vz --mount-type virtiofs
   ```

   The defaults are 2 CPUs and 2 GiB of memory; the test build and the `release` build need more.
   `vz` needs macOS 13 or later; `virtiofs` needs `vz`.
   Colima makes its Docker context active. `docker context ls` marks `colima` with `*`.
   Unset `DOCKER_HOST` first: the variable has priority over the context.
3. Check the tools. `sh scripts/check-prereqs.sh --runtime docker` prints `check-prereqs: ok (docker)`.
4. Keep the checkout and the temporary files in a shared path. Colima mounts only `$HOME` by default, writable.
   The macOS `$TMPDIR` is under `/var/folders`, which the VM does not see. Set `TMPDIR` to a directory under `$HOME`
   in the zsh session that runs `scripts/scan.sh` and the tests, or in `~/.zshrc`.

   ```sh
   mkdir -p "$HOME/.cache/litellm-tmp"
   export TMPDIR="$HOME/.cache/litellm-tmp"
   ```

   For a checkout outside `$HOME`, give each mount with `--mount`. A `--mount` option replaces the default mount of `$HOME`.
5. Build and start as on Linux: `docker compose up -d --build`, `scripts/build.sh test`.
6. For the `release` target, create a builder of the `docker-container` driver. The Buildx driver `docker` cannot export two platforms.

   ```sh
   docker buildx create --name gateway-builder --driver docker-container
   ```

   `scripts/build.sh --builder gateway-builder release` then uses it ([image.md](image.md)).
   With TLS inspection, create the builder as the section "The `release` builder" says.

### Podman and Colima compared

| Need of the repo | Podman | Colima (runtime `docker`) |
| --- | --- | --- |
| Docker CLI | Talks to the Docker-compatible API of the Podman socket (Docker API v1.40). | Talks to the Docker Engine in the VM. |
| BuildKit for `docker compose build` and `scripts/build.sh` | No. The compatible API has no BuildKit endpoint (`containers/podman#17836`, open). `podman build` builds the `Dockerfile` instead. | Yes. The Docker Engine has BuildKit, and Colima turns it on by default. |
| `docker buildx bake` | No. Podman and Buildah have no `bake` command (`containers/buildah#4796`, open). | Yes, with the Homebrew plugin `docker-buildx`. |
| `RUN --network=none`, `RUN --mount=type=secret` | `podman build` documents `--network none` and `--secret`. Not verified with this `Dockerfile`. | Yes (BuildKit). |
| `docker compose` v2 | Yes, through `DOCKER_HOST`. Not verified with this repo. | Yes. |
| Two platforms for `release` | No. Use a Linux host or a CI runner with Docker. | Yes, with a Buildx builder of the `docker-container` driver. |
| Shared paths | `/Users`, `/private`, `/var/folders` by default. | `$HOME` by default. |

Do not run both VMs against one shell: `DOCKER_HOST` selects Podman, the Docker context selects Colima.

## Registry proxy and package proxy (optional)

The base images come from `ghcr.io` and `docker.io`, and the `test` target installs packages from PyPI.
When these hosts are reachable, no proxy is necessary. A network can instead give a registry proxy and a
package proxy, for example Artifactory. Not verified: these steps with a registry proxy or package proxy.

| Image or index | Setting | Example |
| --- | --- | --- |
| LiteLLM base image | `--build-arg BASE_IMAGE=...` of `podman build`, or `BASE_IMAGE` in the environment of `scripts/build.sh` (Colima). Keep the same index digest. | `registry-proxy.example.com/ghcr/berriai/litellm@sha256:<digest>` |
| Python image of the `test` target | `PYTHON_IMAGE` in the environment of `scripts/build.sh` (Colima). Keep the digest. | `registry-proxy.example.com/docker/library/python@sha256:<digest>` |
| Scanner image | `GITLEAKS_IMAGE` in the environment of `scripts/scan.sh`. Keep the digest. | `registry-proxy.example.com/docker/zricethezav/gitleaks:v8.28.0@sha256:<digest>` |
| Package index of the `test` target | `PIP_INDEX_URL` in the environment of `scripts/build.sh` (Colima). pip checks the hash of each file. | `https://registry-proxy.example.com/api/pypi/pypi/simple` |
| Postgres image | No variable. Set the image in an override file under `.local/`. Keep the digest. | See below |

The digests are in `Dockerfile` (`BASE_IMAGE`, `PYTHON_IMAGE`), `scripts/scan.sh` (`GITLEAKS_IMAGE`) and `compose.yaml` (`database`).
The path of an image on the proxy depends on the proxy configuration.

```yaml
# .local/compose.proxy.yaml
services:
  database:
    image: registry-proxy.example.com/docker/library/postgres@sha256:<digest of compose.yaml>
```

Add the file to `COMPOSE_FILE` in `.env`, for example `COMPOSE_FILE=compose.yaml:.local/compose.proxy.yaml`.
Log in to the proxy with `docker login registry-proxy.example.com` when it needs a credential.

## TLS inspection

A network with TLS inspection replaces the server certificates with certificates of its own CA.
Then each client needs that CA in its trust store. Not verified: these steps on a network with TLS inspection.

| Client | Trust store | Setting |
| --- | --- | --- |
| Podman VM (image pulls, `podman build`) | The VM | `podman machine ssh`, section "Image pulls in the Podman VM" |
| Docker Engine in the Colima VM (image pulls, builds with the default builder) | The VM | `~/.docker/certs.d/` |
| Buildx builder `gateway-builder` (`release`, Colima) | The BuildKit container | `--buildkitd-config` |
| pip in the `test` target (Colima) | pip | `PIP_CA_FILE` |
| The gateway, the Codex services and the Copilot service at run time | Python | `SSL_CERT_FILE` |

### Export the CA of the network

1. Find the name of the CA in Keychain Access, in the keychain "System", category "Certificates".
2. Export the CA as a PEM file. `-a` exports each certificate with that name.

   ```sh
   mkdir -p .local
   security find-certificate -a -c "<name of the CA>" -p /Library/Keychains/System.keychain > .local/network-ca.pem
   ```

   If the command prints nothing, the CA is possibly in the login keychain of the user. Then search the login keychain:
   omit the keychain path at the end of the command.

3. Check the file. The subject must be the CA of the network, and the end date must be in the future.

   ```sh
   openssl x509 -in .local/network-ca.pem -noout -subject -enddate
   ```

4. Create the combined bundle: the public CAs of macOS and the CA of the network.
   `PIP_CA_FILE` and `SSL_CERT_FILE` replace the default bundle, so the file must hold both.

   ```sh
   cat /etc/ssl/cert.pem .local/network-ca.pem > .local/ca-bundle.pem
   ```

   Not verified: `/etc/ssl/cert.pem` holds the current public CAs on each macOS version. The alternative is
   `security find-certificate -a -p /System/Library/Keychains/SystemRootCertificates.keychain`.

`.local/` is not tracked. The files hold public certificates only, but they identify the network.

### Image pulls in the Podman VM

The Podman VM runs Fedora CoreOS. Its trust store takes a CA from `/etc/pki/ca-trust/source/anchors/`.
Not verified: these steps.

1. Copy the CA into the VM and update the trust store. The VM shares `/Users`, so the file of the checkout is visible.

   ```sh
   podman machine ssh -- sudo cp "$PWD/.local/network-ca.pem" /etc/pki/ca-trust/source/anchors/network-ca.pem
   podman machine ssh -- sudo update-ca-trust
   ```

2. Restart the VM, so that the Podman service reads the trust store again.

   ```sh
   podman machine stop
   podman machine start
   ```

3. Pull one base image. The pull must not fail with `x509: certificate signed by unknown authority`.

   ```sh
   docker pull postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
   ```

### Image pulls in the Colima VM

At each start, Colima copies the content of `~/.docker/certs.d/` on the Mac to `/etc/docker/certs.d/` and to
`/etc/ssl/certs/` in the VM. A PEM file at the top of `~/.docker/certs.d/` thus goes into the certificate
directory of the VM.

1. Copy the CA to that directory.

   ```sh
   mkdir -p ~/.docker/certs.d
   cp .local/network-ca.pem ~/.docker/certs.d/network-ca.crt
   ```

2. Restart Colima. The copy runs at start. Check that the file is in the VM.

   ```sh
   colima restart
   colima ssh -- ls -l /etc/ssl/certs/network-ca.crt
   ```

3. Restart the Docker Engine in the VM. Colima copies the file after the engine starts, and the engine
   reads the system certificates once. Not verified: the engine needs this restart.

   ```sh
   colima ssh -- sudo systemctl restart docker
   ```

4. Pull one base image. The pull must not fail with `x509: certificate signed by unknown authority`.

   ```sh
   docker pull postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6
   ```

### The `release` builder (Colima)

A builder of the `docker-container` driver runs BuildKit in its own container, with its own trust store.
Give it the CA in a BuildKit configuration file. Buildx copies the files of `ca` into the builder.

```toml
# .local/buildkitd.toml
[registry."ghcr.io"]
  ca = ["/Users/<user>/litellm/.local/network-ca.pem"]

[registry."docker.io"]
  ca = ["/Users/<user>/litellm/.local/network-ca.pem"]
```

Use absolute paths. Create the builder with the file. When the builder exists, remove it first with `docker buildx rm gateway-builder`.

```sh
docker buildx create --name gateway-builder --driver docker-container --buildkitd-config .local/buildkitd.toml
```

With a registry proxy, add a section for the host of the proxy.

A registry can send the image layers from another host (a blob host), for example a CDN host.
Not verified: a section for a registry applies to its blob host. Check the builder before the first release:

```sh
scripts/build.sh --builder gateway-builder --oci "$TMPDIR/gw.tar" release
```

If the build fails with `x509: certificate signed by unknown authority`, the error names the host of the request.
Add a section for that host to `.local/buildkitd.toml`, then remove and create the builder again.
The host name below is an example; use the name from the error.

```toml
[registry."pkg-containers.githubusercontent.com"]
  ca = ["/Users/<user>/litellm/.local/network-ca.pem"]
```

### The `test` target (Colima)

The bake target `test` installs `pytest` from PyPI in a build stage.
Set `PIP_CA_FILE` to the combined bundle for that stage, and `PIP_INDEX_URL` for a package proxy ([ci.md](ci.md)).
`scripts/build.sh` gives the file to the build as the BuildKit secret `pip-ca`; the file does not go into a layer.

```sh
PIP_CA_FILE=.local/ca-bundle.pem scripts/build.sh test
```

With Podman, the tests run with `podman run` in the pinned image; `pip install` of `pytest` on the host side of that command
needs `PIP_CERT=.local/ca-bundle.pem` in the environment of the container. Not verified.

### The gateway at run time

The gateway calls providers over HTTPS. Python reads a CA bundle from `SSL_CERT_FILE`.

1. Create the combined bundle `.local/ca-bundle.pem` (section "Export the CA of the network").
2. Mount the file and set the variable in an override file under `.local/`:

   ```yaml
   # .local/compose.tls.yaml
   services:
     gateway:
       volumes:
         - ./.local/ca-bundle.pem:/etc/ssl/certs/ca-bundle.pem:ro
       environment:
         SSL_CERT_FILE: /etc/ssl/certs/ca-bundle.pem
   ```

3. Add the file to `COMPOSE_FILE` in `.env`:

   ```sh
   COMPOSE_FILE=compose.yaml:.local/compose.tls.yaml
   ```

4. Recreate the gateway:

   ```sh
   docker compose up -d --force-recreate gateway
   ```

The Codex services and the service `copilot` need the same mount and variable when you use `compose.codex.yaml` or `compose.copilot.yaml`.
The file must be readable for the user `1000:1000`.

## Sources

| Statement | Source |
| --- | --- |
| `podman machine init` options, the default shared directories `/Users`, `/private`, `/var/folders` on macOS | `docs.podman.io`, `podman-machine-init(1)` |
| The Podman socket path in `podman machine inspect` (`ConnectionInfo.PodmanSocket.Path`) | `docs.podman.io`, `podman-machine-inspect(1)` |
| Podman gives a Docker v1.40 compatible API | `docs.podman.io`, `podman-system-service(1)` |
| No BuildKit through the Podman API | `github.com/containers/podman/issues/17836` (open) |
| No `bake` in Buildah | `github.com/containers/buildah/issues/4796` (open) |
| `podman build` supports `--network none`, `--secret` and the platform build arguments | `docs.podman.io`, `podman-build(1)` |
| The Podman VM is Fedora CoreOS; the trust store reads `/etc/pki/ca-trust/source/anchors/` | `docs.podman.io`, `podman-machine(1)`; `update-ca-trust(8)` |
| `node@24` is keg-only; the Homebrew formulae of this page have `arm64` bottles | `formulae.brew.sh` |
| Colima defaults (`cpu`, `memory`, `arch`, `vmType`, `mountType`, `$HOME` mount, BuildKit on) | `embedded/defaults/colima.yaml` in `github.com/abiosoft/colima` (release v0.10.3) |
| A `--mount` option replaces the default `$HOME` mount | `environment/vm/lima/yaml.go` in `github.com/abiosoft/colima` |
| Colima copies `~/.docker/certs.d` to `/etc/docker/certs.d` and `/etc/ssl/certs` | `environment/vm/lima/certs.go` in `github.com/abiosoft/colima` |
| `cliPluginsExtraDirs` for the plugins | Caveats of the Homebrew formulae `docker-compose` and `docker-buildx` (`formulae.brew.sh`) |
| The Buildx driver `docker` cannot build two platforms; `docker-container` can | `docs.docker.com/build/builders/drivers/` |
| `ca` in `buildkitd.toml`, `--buildkitd-config`, the copy into the builder | `docs.docker.com/build/buildkit/configure/` |
| `DOCKER_HOST` has priority over the Docker context | `docs.docker.com/engine/manage-resources/contexts/` |
