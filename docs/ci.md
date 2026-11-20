# CI pipeline

`.forgejo/workflows/upstream-daily.yml` runs on private Forgejo at 06:23 UTC each day.
It also runs after a push to `local-dev` and supports `workflow_dispatch`.
The private repository must use `local-dev` as its default branch for schedules.
The public mirror remains portable-only.

The pipeline file holds no build logic. It calls the CI scripts described below.
`docker-bake.hcl` holds the build definition. The same commands run on a Linux host, on a Mac and in CI.

## Daily upstream updates

`scripts/update-upstream.py prepare` reads the latest stable BerriAI GitHub release.
It skips drafts, prereleases, equal versions, and older versions.
A new release must have a valid index digest and both `linux/amd64` and `linux/arm64` images.
The script updates Dockerfile and Bake pins, the tested-version list, and the install documents together.
It builds the architecture page from its source.
The tested-version addition is a candidate until the image suite passes.

`scripts/ci-check.sh` runs the updater tests, architecture tests, all shell suites,
image suite, image checks, Compose checks, and secret and public tree scans.
It scans only the gateway add-on image layers; upstream layers contain third-party test keys.
A failure stops the job before it pushes a branch or creates a pull request.

`scripts/update-upstream.py publish` uses the short-lived job token to push one
`automation/litellm-vX.Y.Z` branch and open one pull request against `local-dev`.
It refuses a public repository, a different default branch, a mismatched origin, or unrelated file changes.
It never force-pushes or pushes tags. It leaves an existing open or closed update request unchanged.
An existing branch without a request stops the job for inspection; a lost create response triggers a read, not a second create.
The pull request links the upstream comparison for hook review.
Merge, portable promotion, container publication, and deployment remain manual.

### Private runner

Register a repository-scoped runner with label `litellm-ci`.
The workflow overrides the job image with a digest-pinned Node 24 Debian image.
`scripts/install-ci-tools.sh` installs hash-checked Docker CLI, Compose, Buildx, and gitleaks binaries.
The runner needs Docker socket access; its shared runner configuration needs no change.
The job uses BuildKit context transfer, `docker run` without checkout mounts, and binary scans.
This avoids the checkout-bind problem of Docker-backed runner workspaces.
The gitleaks wrapper normalizes the official binary's version output for the scanner's exact version check.
The runner and job must use Linux amd64.

Warning: Docker socket access gives trusted workflow code administrator-level access to the host.
Keep the repository private. Do not add untrusted pull-request triggers to this workflow.

The workflow needs no provider token, host `.env`, or long-lived Git write credential.
It checks the current version even when no upgrade exists.
A successful no-update run proves runner operation, but does not prove the live pull-request write path.

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
