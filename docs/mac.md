# Run the stack on a Mac

This page starts the stack on a Mac with Apple silicon (`arm64`).
The steps are the same as on Linux. The differences are the container runtime, the shared paths and TLS inspection.

Not verified: these steps on a Mac. Each step has a verification note.
The steps come from a run on Linux amd64, except where a step says otherwise.
The statements about Colima, Podman and Buildx come from their documentation and source code (section "Sources").

## What the repo needs

| Need | Why |
| --- | --- |
| A container runtime with the Docker CLI and a Linux `arm64` VM | All commands use `docker`. |
| Docker Compose v2 (`docker compose`) | `compose.yaml` starts the stack. |
| Buildx with BuildKit | `Dockerfile` uses `RUN --network=none` and `FROM --platform=$BUILDPLATFORM`. `scripts/build.sh` uses `docker buildx bake`. |
| File sharing for the repository and `$TMPDIR` | Compose mounts files from the checkout. `scripts/scan.sh` mounts temporary files. |
| `git`, `sh`, `python3` | Scripts and tests. macOS has `python3` (3.9) with the Command Line Tools. |
| Git 2.24 or later | Git runs the `pre-merge-commit` hook of `scripts/install-hooks.sh` from 2.24. `core.hooksPath` needs 2.9, `scripts/scan.sh` needs 2.5. |
| Docker Compose 2.35.0 or later | `scripts/login-codex-account.sh` uses `docker compose config --no-env-resolution`. |
| Buildx 0.19 or later | `scripts/build.sh --oci` uses `docker buildx bake --allow fs.write=...`. |

The minimum versions come from the upstream release where each option first appears. Not verified: a run with these versions.
Check the installed versions with `git --version`, `docker compose version` and `docker buildx version`.

The base images have an `arm64` variant: the LiteLLM image and the Postgres image are pinned by index digest with `linux/amd64` and `linux/arm64`.
Not verified: the `arm64` gateway image at run time.

## Container runtime

This page uses Colima with the Docker runtime. Colima runs the Docker Engine in a Linux VM, and the Homebrew
Docker CLI, Compose and Buildx talk to it. Docker Desktop, OrbStack and Rancher Desktop also give a Docker Engine;
the repo does not need Colima in particular.

### Colima and Podman compared

| Need of the repo | Colima (runtime `docker`) | Podman on macOS |
| --- | --- | --- |
| Docker CLI | Talks to the Docker Engine in the VM. | Talks to the Docker-compatible API of `podman system service` (Docker API v1.40). |
| BuildKit for `docker build`, `docker compose build` and the Buildx driver `docker` | Yes. The Docker Engine has BuildKit, and Colima turns it on by default. | No. The compatible API has no BuildKit endpoint (Podman issue 17836, open). |
| `docker buildx bake` (`scripts/build.sh`) | Yes, with the Homebrew plugin `docker-buildx`. | Podman and Buildah have no `bake` command (Buildah issue 4796, open). A Buildx `docker-container` builder on the Podman API is not documented. Not verified. |
| `RUN --network=none`, `RUN --mount=type=secret` | Yes (BuildKit). | `podman build` documents `--network none` and `--secret`. `scripts/build.sh` does not call `podman build`. |
| `docker compose` v2 | Yes, with the Homebrew plugin `docker-compose`. | Podman documents `DOCKER_HOST` for Compose. Not verified with this repo. |
| Two platforms for `release` | Yes, with a Buildx builder of the `docker-container` driver. | Not verified. |

Result: with Podman, `scripts/build.sh` and the bake targets `test` and `release` do not work as documented here.

Not verified: Podman can start the stack from a pre-built image. Set `GATEWAY_IMAGE` to a released image
with its index digest, point `DOCKER_HOST` at the Podman socket, and run `docker compose up -d` without `--build`.
`scripts/scan.sh` starts the scanner with `docker run` and can also work on Podman. Not verified.

### Install Colima

Not verified on a Mac.

1. Install Colima, the Docker CLI and the two CLI plugins.

   ```sh
   brew install colima docker docker-compose docker-buildx
   ```

2. Tell the Docker CLI where Homebrew puts the plugins. The Homebrew formulae of `docker-compose` and `docker-buildx`
   give this setting. Add the key to `~/.docker/config.json`; keep the other keys of the file.

   ```json
   {
     "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
   }
   ```

   `/opt/homebrew` is the Homebrew prefix on Apple silicon. `brew --prefix` prints it.
3. Start the VM. The values of `--arch`, `--vm-type` and `--mount-type` cannot change after the first start.

   ```sh
   colima start --cpus 4 --memory 8 --arch aarch64 --vm-type vz --mount-type virtiofs
   ```

   The defaults are 2 CPUs and 2 GiB of memory; the test build and the `release` build need more.
   `vz` needs macOS 13 or later; `virtiofs` needs `vz`.
   Colima makes its Docker context active. `docker context ls` marks `colima` with `*`.
4. Check the tools.

   ```sh
   docker version
   docker compose version
   docker buildx version
   ```

5. Keep the checkout and the temporary files in a shared path. Colima mounts only `$HOME` by default, writable.
   The macOS `$TMPDIR` is under `/var/folders`, which the VM does not see. Set `TMPDIR` to a directory under `$HOME`
   in the shell that runs `scripts/scan.sh` and the tests.

   ```sh
   mkdir -p "$HOME/.cache/litellm-tmp"
   export TMPDIR="$HOME/.cache/litellm-tmp"
   ```

   For a checkout outside `$HOME`, give each mount with `--mount`. A `--mount` option replaces the default mount of `$HOME`.

   ```sh
   colima start --mount "$HOME:w" --mount /path/of/the/checkout:w
   ```

6. Create a builder for the `release` target. The Buildx driver `docker` cannot export two platforms.

   ```sh
   docker buildx create --name gateway-builder --driver docker-container
   ```

   `scripts/build.sh --builder gateway-builder release` then uses it ([image.md](image.md)).

   With TLS inspection, create the builder as the section "The `release` builder" says.

## Steps

1. Get the source. Clone the shared Git server, or clone a Git bundle. Not verified on a Mac.

   ```sh
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

3. Create `.env`. Not verified on a Mac. The script gives each secret of the stack a new random value
   and writes mode 0600. It does not replace a `.env` that exists.

   ```sh
   python3 scripts/create-env.py
   ```

   Add the provider keys that you use, for example `OPENROUTER_API_KEY`.

   [host-configuration.md](host-configuration.md) lists the other settings.

4. Build the image and start the stack. Not verified on a Mac. Compose builds `litellm-gateway:local` and starts `gateway` and `database`.

   ```sh
   docker compose up -d --build
   ```

5. Check the gateway. Not verified on a Mac. The result is `"I'm alive!"`.

   ```sh
   curl http://127.0.0.1:4321/health/liveliness
   ```

   The first start takes up to 90 seconds.

6. Run the tests. Not verified on a Mac. The build of the bake target `test` fails when a test fails.

   ```sh
   scripts/build.sh test
   sh tests/test_compose.sh
   ```

   [tests/README.md](../tests/README.md) has the other test commands.

7. Run the scan. Not verified on a Mac. Exit code 0 means no finding.

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

## Registry proxy and package proxy (optional)

The base images come from `ghcr.io` and `docker.io`, and the `test` target installs packages from PyPI.
When these hosts are reachable, no proxy is necessary. A network can instead give a registry proxy and a
package proxy, for example Artifactory. Not verified: these steps with a registry proxy or package proxy.

| Image or index | Setting | Example |
| --- | --- | --- |
| LiteLLM base image | `BASE_IMAGE` in `.env` (Compose build) or in the environment (`scripts/build.sh`). Keep the same index digest. | `registry-proxy.example.com/ghcr/berriai/litellm@sha256:<digest>` |
| Python image of the `test` target | `PYTHON_IMAGE` in the environment of `scripts/build.sh`. Keep the digest. | `registry-proxy.example.com/docker/library/python@sha256:<digest>` |
| Scanner image | `GITLEAKS_IMAGE` in the environment of `scripts/scan.sh`. Keep the digest. | `registry-proxy.example.com/docker/zricethezav/gitleaks:v8.28.0@sha256:<digest>` |
| Package index of the `test` target | `PIP_INDEX_URL` in the environment of `scripts/build.sh`. pip checks the hash of each file. | `https://registry-proxy.example.com/api/pypi/pypi/simple` |
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
| Docker Engine in the Colima VM (image pulls, builds with the default builder) | The VM | `~/.docker/certs.d/` |
| Buildx builder `gateway-builder` (`release`) | The BuildKit container | `--buildkitd-config` |
| pip in the `test` target | pip | `PIP_CA_FILE` |
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

### The `release` builder

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

### The `test` target

The bake target `test` installs `pytest` from PyPI in a build stage.
Set `PIP_CA_FILE` to the combined bundle for that stage, and `PIP_INDEX_URL` for a package proxy ([ci.md](ci.md)).
`scripts/build.sh` gives the file to the build as the BuildKit secret `pip-ca`; the file does not go into a layer.

```sh
PIP_CA_FILE=.local/ca-bundle.pem scripts/build.sh test
```

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
| Colima defaults (`cpu`, `memory`, `arch`, `vmType`, `mountType`, `$HOME` mount, BuildKit on) | `embedded/defaults/colima.yaml` in `github.com/abiosoft/colima` (release v0.10.3) |
| A `--mount` option replaces the default `$HOME` mount | `environment/vm/lima/yaml.go` in `github.com/abiosoft/colima` |
| Colima copies `~/.docker/certs.d` to `/etc/docker/certs.d` and `/etc/ssl/certs` | `environment/vm/lima/certs.go` in `github.com/abiosoft/colima` |
| `cliPluginsExtraDirs` for the plugins | Caveats of the Homebrew formulae `docker-compose` and `docker-buildx` (`formulae.brew.sh`) |
| The Buildx driver `docker` cannot build two platforms; `docker-container` can | `docs.docker.com/build/builders/drivers/` |
| `ca` in `buildkitd.toml`, `--buildkitd-config`, the copy into the builder | `docs.docker.com/build/buildkit/configure/` |
| Podman gives a Docker v1.40 compatible API | `docs.podman.io`, `podman-system-service(1)` |
| No BuildKit through the Podman API | `github.com/podman-container-tools/podman/issues/17836` (open) |
| No `bake` in Buildah | `github.com/podman-container-tools/buildah/issues/4796` (open) |
| `podman build` supports `--network none` and `--secret` | `docs.podman.io`, `podman-build(1)` |
