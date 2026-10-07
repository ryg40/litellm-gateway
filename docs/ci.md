# CI pipeline

This page says what a pipeline for this repo does. It gives no pipeline file for one CI product.
Choose a CI system. See "Decisions for your environment".

The pipeline file holds no build logic. It calls `scripts/scan.sh` and `scripts/build.sh`.
`docker-bake.hcl` holds the build definition. The same commands run on a Linux host, on a Mac and in CI.

## On each push

0. Check the tools of the runner. The check stops with exit code 1 when a hard requirement is missing.

   ```sh
   sh scripts/check-prereqs.sh
   ```

   Node 24 is a hard requirement: the pipeline runtime runs on Node 24, and the check refuses another major version.
1. Check out the full history. `scripts/scan.sh history` needs all commits of the scanned range.
2. Get the scanner image. The scan needs no network after this step.

   ```sh
   docker pull zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854
   ```

3. Scan the tracked files and the history. Exit code 1 means a finding.

   ```sh
   SCAN_REF=<pushed ref> scripts/scan.sh all
   ```

   `SCAN_REF` sets the level of host values: `fail` for `portable` and `portable-v*`, `warn` for other refs.
4. Run the tests in the image. The build fails when a test fails.

   ```sh
   scripts/build.sh test
   ```

## On a tag `portable-vX.Y.Z`

The release tag has the form `portable-vX.Y.Z`, for example `portable-v0.1.0`.
The image tag is then `<litellm version>-pX.Y.Z`, for example `1.103.0-p0.1.0`.

1. Do the steps of "On each push" with `SCAN_REF=refs/tags/<tag>`.
2. Create a builder with the `docker-container` driver. The classic `docker` driver cannot export two platforms.

   ```sh
   docker buildx create --name gateway-builder --driver docker-container
   ```

3. Build `linux/amd64` and `linux/arm64` and push them. `scripts/build.sh` refuses a dirty tree for `release`.

   ```sh
   REGISTRY=<registry>/<namespace> SOURCE_URL=<URL of the repo> \
     scripts/build.sh --builder gateway-builder --push release
   ```

4. Scan the layers that the build adds to the base image. The base image contains test keys of third parties.

   ```sh
   scripts/scan.sh image <built image> <base image>
   ```

   Not verified: this command with an image that is only in a registry. The script reads a local image.
5. Record the index digest of the pushed image. A deployment uses the digest, not the tag.

   ```sh
   docker buildx imagetools inspect <registry>/<namespace>/litellm-gateway:<tag>
   ```

   Keep the digest as a job output or a release note. [image.md](image.md) describes the tag format.

Warning: a push must not start from a branch other than `portable` or a `portable-v*` tag. Other branches can contain host values.

## Variables

| Variable | Use | Example |
| --- | --- | --- |
| `REGISTRY` | Registry and namespace of the image | `registry.example.com/team` |
| `SOURCE_URL` | URL of the repo for the label `org.opencontainers.image.source` | `https://git.example.com/team/litellm` |
| `BASE_IMAGE` | A mirror of the LiteLLM base image. Keep the same index digest. | `mirror.example.com/berriai/litellm@sha256:<digest>` |
| `GITLEAKS_IMAGE` | A mirror of the scanner image. Pin it by digest. | `mirror.example.com/gitleaks:v8.28.0@sha256:<digest>` |
| `PROVENANCE` | Provenance attestation of `release`: `false`, `min` or `max` | `false` |
| `PYTHON_IMAGE` | A mirror of the Python image of the `test` target. Pin it by digest. | `mirror.example.com/library/python@sha256:<digest>` |
| `PIP_INDEX_URL` | A mirror of the package index for the `test` target | `https://pypi.example.com/simple` |
| `PIP_CA_FILE` | A CA bundle for the package index, for TLS inspection | `/etc/ssl/certs/ca-bundle.pem` |

With `PROVENANCE=min` or `max` the index digest changes on each build. [image.md](image.md) gives the reason.
`SCAN_REF` is not a pipeline setting. The job sets it from the ref of the run.

## Runner needs

- Node.js, major version 24. The pipeline runtime of the CI system runs on it, for example the JavaScript actions of a hosted CI. `scripts/check-prereqs.sh` refuses another major version. No script of this repo runs Node.
- Docker with Buildx. The `release` target needs the `docker-container` driver. Podman has no `bake` command, so the pipeline needs Docker.
- Access to `ghcr.io` and `docker.io`, or mirrors through `BASE_IMAGE` and `GITLEAKS_IMAGE`.
- The bake target `test` installs `pytest`, `pluggy` and `iniconfig` from PyPI, or from `PIP_INDEX_URL`. pip checks the hash of each file, also from a mirror.
- With Docker-in-Docker, the checkout and `$TMPDIR` must have the same path for the job and for the Docker daemon. `scripts/scan.sh` mounts them.
- The registry credential comes from the secret store of the CI system. It is not in the repo.

## Registry proxy and package proxy (optional)

A runner that reaches `ghcr.io`, `docker.io` and PyPI needs no proxy. A runner can instead pull through a
registry proxy and a package proxy, for example Artifactory. The variables of the table in "Variables" do this:

| Source | Variable | Note |
| --- | --- | --- |
| LiteLLM base image | `BASE_IMAGE` | Keep the index digest of `Dockerfile`. `docker-bake.hcl` puts the name of the proxy into the label `org.opencontainers.image.base.name`. |
| Python image of `test` | `PYTHON_IMAGE` | Keep the digest of `Dockerfile`. |
| Scanner image | `GITLEAKS_IMAGE` | Keep the digest of `scripts/scan.sh`. |
| Package index of `test` | `PIP_INDEX_URL` | pip checks the hash of each file, also from a proxy. |

The pipeline does not pull the Postgres image: `tests/test_compose.sh` reads the Compose configuration only.
A host that starts the stack sets the Postgres image in an override file ([mac.md](mac.md)); `compose.yaml` has no variable for it.
Not verified: a build through a proxy.

## TLS inspection

A network with TLS inspection needs its CA in each client that makes an HTTPS request.

- Image pulls: the Docker daemon of the runner needs the CA. This is a setting of the runner, not of the repo.
- The `test` target: set `PIP_CA_FILE` to a PEM file that holds the public CAs and the CA of the network.
  `scripts/build.sh` gives the file to the build as the BuildKit secret `pip-ca`. The file does not go into a layer.

  ```sh
  PIP_CA_FILE=/etc/ssl/certs/ca-bundle.pem scripts/build.sh test
  ```

  Without `PIP_CA_FILE`, pip uses its own CA bundle.
  A direct call of `docker buildx bake` gives the secret with `--set test.secrets=id=pip-ca,src=<file>`.

## Decisions for your environment

- Which CI system runs the pipeline.
- Which registry gets the image, and which namespace.
- Which shared Git server holds `portable`.
- Whether the runners reach `ghcr.io`, `docker.io`, `github.com` and PyPI, or need mirrors.
- Whether your network uses TLS inspection. The runner then needs the CA of that network (see "TLS inspection").
- Whether the runners give Docker with Buildx, or another builder.
- Where the pipeline records the digest.
