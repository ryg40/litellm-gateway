# Gateway image

The gateway image is the upstream LiteLLM image at a pinned index digest plus
the hooks and routers of this repo. `Dockerfile` defines it, `docker-bake.hcl`
defines the targets, and `scripts/build.sh` starts a build.

## Content

| Path in the image | Source in the repo | Use |
| --- | --- | --- |
| `/opt/litellm-gateway/image/hooks/` | `image/hooks/` | start-up hooks; `sitecustomize.py` loads at each Python start |
| `/opt/litellm-gateway/image/routers/` | `image/routers/` | `quota_router.py`, `decision_router.py` |

- `PYTHONPATH=/opt/litellm-gateway/image/hooks`. Python imports `sitecustomize` from it at start.
- `PYTHONDONTWRITEBYTECODE=1`. The add-on directories are read-only.
- `LITELLM_LOCAL_MODEL_COST_MAP=True` and `LITELLM_TELEMETRY=False`. LiteLLM uses the cost map of the
  installed version and sends no telemetry, also with `docker run` or a CI job without Compose.
- The files are owned by `root:root` with mode `0444`; the runtime user cannot change them.
- The runtime user is `65532:65532` (`nonroot` of the Wolfi base). A Compose `user:` setting replaces it.
- The layout under `/opt/litellm-gateway` mirrors the repo. `tests/conftest.py` finds
  the hooks at `<root>/image/hooks`, so the `test` target runs the unchanged tests against the image paths.

The image does not contain route configuration, `.env`, `secrets/`, `state/`,
`.local/`, Git data, tests or documents. `.dockerignore` is an allowlist: it
sends only `image/hooks/`, `image/routers/` and, for the `test` target,
`tests/`, `config/`, `scripts/`, `compose.yaml`, `compose.codex.yaml` and `compose.copilot.yaml` to the builder.

The final stage `gateway` has `COPY`, `ENV`, `LABEL`, `USER` and `ARG` steps only. The
stage `addons` sets owner, mode and file times; it runs on the build platform
(`FROM --platform=$BUILDPLATFORM`). A build for another platform therefore
needs no emulation (QEMU).

## Hooks

`sitecustomize.py` loads the hooks at each Python start. Each hook changes LiteLLM in the process and has a reason to exist only until upstream changes.

| File in `image/hooks/` | What it does | Remove it when |
| --- | --- | --- |
| `sitecustomize.py` | Stops an untested LiteLLM version. Registers the lookup keys of `gpt-6-luna`, `gpt-6-sol` and `responses/gpt-6-astra`. Installs the hooks below. | Upstream has the lookup keys: remove the static entries. The file stays while one hook stays. |
| `litellm_versions.py` | The list `TESTED_VERSIONS` for all hooks. | No hook stays. |
| `responses_tool_finish.py` | Gives a streamed chat answer with tool calls the finish reason `tool_calls`. | The pinned upstream bridge passes `tests/test_responses_tool_finish.py` without the hook. |
| `chatgpt_session_id.py` | Gives a request for a `chatgpt` deployment a stable session id ([codex-services.md](codex-services.md), section "Stable session id"). `quota_router.py` imports `cache_session_id` from it. | Upstream gives a conversation without a client id a stable `session_id` header. Then keep `cache_session_id` for the router, or move it back into `quota_router.py`. |
| `chatgpt_auth_file.py` | One ChatGPT account for each deployment. Off by default ([codex-accounts.md](codex-accounts.md)). | The pinned upstream has the key `chatgpt_auth_file`. The hook stops the start when it finds the key upstream. |

When you remove a hook, remove its file, its lines in `sitecustomize.py`, its test module in `tests/` and the name of that module in `HOOK_TESTS` of `tests/conftest.py`.
`quota_router.py` needs `image/hooks` on `PYTHONPATH`, as the image sets it.

## Run

The entry point (`docker/prod_entrypoint.sh`) and the command of the base image stay.

Gateway, with the usual LiteLLM arguments:

```
docker run ... litellm-gateway:1.103.0-p0.1.0 --config /config/config.yaml --host 0.0.0.0 --port 4000
```

Router (Compose: `entrypoint: ["python", "-m", "uvicorn"]`):

```
docker run ... --entrypoint python litellm-gateway:1.103.0-p0.1.0 \
  -m uvicorn quota_router:app --app-dir /opt/litellm-gateway/image/routers --host 0.0.0.0 --port 4000
```

Do not set `PYTHONPATH` to another value in Compose, or keep
`/opt/litellm-gateway/image/hooks` as its first entry. Without it the hooks do not load.

## Compose

`compose.yaml`, `compose.codex.yaml` and `compose.copilot.yaml` use this image for `gateway`, `codex1`, `codex2`,
`codex3`, `codex-router` and `copilot`: `image: ${GATEWAY_IMAGE:-litellm-gateway:local}`. They mount no hook or router file
and set no `PYTHONPATH`. `codex-router` runs `quota_router:app` with
`--app-dir /opt/litellm-gateway/image/routers`.

The service `gateway` has a `build:` section for the target `gateway`. `docker compose build` and
`docker compose up -d --build` therefore build `litellm-gateway:local` with the defaults of `Dockerfile`.
`BASE_IMAGE` comes from the environment or `.env` when it is set. This build sets no revision,
version or time labels: use `scripts/build.sh` for an image that you publish.

A deployment sets `GATEWAY_IMAGE` to a released image with its index digest, for example
`ghcr.io/owner/litellm-gateway@sha256:<digest>`, and runs `docker compose up -d` without `--build`.
Compose pulls the image when it is missing. With `--build` and a digest name the build fails:
`refusing to create a tag with a digest reference`.

`docs/host-configuration.md` has the settings and a method to test a changed hook without a new build.

## A new model

A new model needs no image change: add `model_list` entries with `model_info` as in
`config/model-info.example.yaml`, and restart the proxy.

LiteLLM checks the parameters of a `gpt-5*` or `gpt-6*` request against its cost map, by the bare model
name. A model that the map of the image lacks, for example `gpt-6.1-sol`, gets HTTP 400 for
`reasoning_effort: xhigh` and for `tool_choice` on an `openai/` or a `chatgpt/` route. A plain request works.
The router registers the deployment id of a `model_list` entry as a cost-map key. An entry with
`model_info.id` equal to the bare model name therefore passes the check.

| Key in `model_info` | Value |
| --- | --- |
| `id` | The bare model name, for example `gpt-6.1-sol`. No provider prefix, no `responses/`. |
| `litellm_provider` | `openai`. Without it the check fails. |
| `mode` | `chat` in the gateway. |
| `input_cost_per_token`, `output_cost_per_token`, `cache_read_input_token_cost`, `cache_creation_input_token_cost` | USD per token. API-equivalent estimates, not subscription charges. |
| `supports_reasoning`, `supports_xhigh_reasoning_effort`, `supports_function_calling`, `supports_tool_choice` | `true`. |

- Only one entry of a proxy has the id. Other entries for the same model (an alias, a second account)
  have no `model_info` and use the registered key.
- The entry with the id stands before the other entries for the same model. With another entry first,
  the proxy reports cost 0.0 for the model.
- `router.get_model_ids()` returns the id. Not verified: the id in `/model/info` and in the spend logs.
- The form covers explicit `model_list` entries. It does not cover a wildcard route.
- The account service (`chatgpt/responses/<model>`) uses two entries, as the section `account` of the example shows.
  The route entry has `mode: responses`, `supports_native_streaming: true` and the flags, with no `id` and no
  `litellm_provider`. A second entry, which is not a route, has `id: responses/<model>`, `litellm_provider: openai`,
  `mode: responses` and the flags; its `litellm_params.model` must not start with `chatgpt/`.
  With `litellm_provider: openai` on the route entry, LiteLLM sends the backend request without `stream` and
  Codex answers HTTP 400 "Stream must be set to true".
- A hop with `litellm_proxy/<model>` has no parameter check at the gateway. There `model_info` gives the prices only.
- The gateway reports the cost from these prices in the header `x-litellm-response-cost` and in the usage log.
  An account service reports no cost for a `chatgpt/` route; this is the same for the models of the image.
- The registration stays after a reload of the cost map: the router registers its deployments again.
- The comments of `config/model-info.example.yaml` give the example prices per million tokens and their tier.

The static entries for `gpt-6-luna`, `gpt-6-sol` and `responses/gpt-6-astra` stay in `image/hooks/sitecustomize.py`.

Warning: a gateway entry that gets its prices only from the static entries (`gpt-6-luna`, `gpt-6-sol`)
reports cost 0.0 after a reload of the cost map. The request still returns 200.
`config/gateway.codex.example.yaml` has the prices in `model_info` of `codex1/gpt-6-luna` and `codex1/gpt-6-sol`.
A host file without these two `model_info` blocks must not schedule or trigger `/reload/model_cost_map`.

Tested in isolation with LiteLLM 1.103.0 and 1.101.0.

## Build

```
scripts/build.sh                          # gateway, host platform, loaded into the local image store
scripts/build.sh test                     # repo tests inside the image
scripts/build.sh --oci /tmp/gw.tar release  # linux/amd64 and linux/arm64 as an OCI archive
REGISTRY=ghcr.io/owner scripts/build.sh --push release
```

`scripts/build.sh` gets the revision and the commit time from Git, refuses a
dirty tree for `release`, and pushes only with `--push`. It gives
`-f docker-bake.hcl` to bake. Without `-f`, bake also reads `compose.yaml` and
merges the service `gateway` into the target `gateway`. `docker-bake.hcl` is read
last, so its values win: with buildx 0.36.1, `docker buildx bake --print` gives
the same definition for `gateway`, `test` and `release` with and without `-f`.
Give `-f docker-bake.hcl` anyway when you call bake directly, so that a later
change of `compose.yaml` cannot change the build.

| Target | Platforms | Output |
| --- | --- | --- |
| `gateway` (default) | host | local image store |
| `smoke` | host | none; the build fails when Python does not start in the gateway image |
| `test` | host | none; the build fails when a test fails |
| `release` | `linux/amd64`, `linux/arm64` | `--push` or `--oci FILE` |

Before `gateway` and `release`, `scripts/build.sh` builds the target `smoke`.
The stage `smoke` starts `python -c pass` on top of the gateway image of the host platform,
with the hooks as at run time. The hooks stop a LiteLLM version that is not in
`TESTED_VERSIONS` (`image/hooks/litellm_versions.py`), so a build on an untested
`BASE_IMAGE` fails at build time, with the message of the hook. The stage is not
part of the gateway image, so the final stage keeps no `RUN` step.
A direct `docker buildx bake gateway` or `release` runs no smoke step.

The `release` target needs a builder with the `docker-container` driver, or
Docker with the containerd image store. The classic `docker` driver cannot
export two platforms:

```
docker buildx create --name gateway-builder --driver docker-container
scripts/build.sh --builder gateway-builder --oci /tmp/gw.tar release
```

### Variables

| Variable | Default | Use |
| --- | --- | --- |
| `REGISTRY` | empty | registry part of the tag |
| `IMAGE_NAME` | `litellm-gateway` | image name |
| `LITELLM_VERSION` | `1.103.0` | tag and label; change with `BASE_IMAGE` |
| `PORTABLE_VERSION` | `X.Y.Z` of the `portable-vX.Y.Z` tag | tag and label |
| `TAG` | `<LITELLM_VERSION>-p<PORTABLE_VERSION>` | image tag |
| `BASE_IMAGE` | `ghcr.io/berriai/litellm@sha256:bd089afd…` | upstream image; a mirror of the same digest is permitted |
| `PYTHON_IMAGE` | `python:3.13-slim-bookworm` by digest | only for pytest in the `test` target |
| `PIP_INDEX_URL` | `https://pypi.org/simple` | package index of the `test` target; a mirror is permitted |
| `PIP_CA_FILE` | empty | `scripts/build.sh` only: CA bundle for the package index, given as the BuildKit secret `pip-ca` |
| `SOURCE_URL` | empty | `org.opencontainers.image.source`; give the URL of the public repo |
| `PROVENANCE` | `false` | provenance attestation of `release`: `false`, `min`, `max` |
| `SOURCE_DATE_EPOCH` | commit time | file times and `created` label |

Tag format: `<registry>/litellm-gateway:<litellm version>-p<portable version>`,
for example `ghcr.io/owner/litellm-gateway:1.103.0-p0.1.0` for the tag `portable-v0.1.0`.
Without a `portable-vX.Y.Z` tag at HEAD, the portable version is the `git describe` form,
for example `0.1.0-3-gabc1234`, or `0-gabc1234` when no such tag exists.

### Labels

`org.opencontainers.image.source`, `.revision`, `.version`, `.created`,
`.base.name`, `.base.digest`, `.title`, `.description` and `litellm.version`.
No label names a host. Other labels (`dev.chainguard.*`, `.vendor`, `.url`,
`.authors`) come from the base image unchanged.

### Reproducible builds

Two `release` builds of the same revision with the same `SOURCE_DATE_EPOCH`
give the same index digest. The stage `addons` sets the file times of the
add-on files to `SOURCE_DATE_EPOCH`, and `scripts/build.sh` sets
`rewrite-timestamp=true` for the directories that the build creates. The
layers therefore do not depend on the checkout time. With
`PROVENANCE=min` or `max` the platform images stay the same, but the index
digest changes: the attestation holds the build start time and an invocation ID.

## Tests

- `scripts/build.sh test` runs `pytest` on `tests/` inside the image. A stage
  from `PYTHON_IMAGE` installs `pytest`, `pluggy` and `iniconfig` with
  `--require-hashes`; the base image has no `pip`.
- `sh tests/test_image.sh IMAGE` checks a built image: hook loaded, stream
  iterator patched, session id step installed, routers importable and the router starts, user not
  `root`, file owner and mode, content, entry point, labels.
- `tests/test_model_info.py` loads `config/model-info.example.yaml` into a `Router` and checks the
  parameter check and the prices for `gpt-6.1-sol`, with and without the required `model_info` keys.

## Update the base image

1. Set `BASE_IMAGE` (index digest) and `LITELLM_VERSION` in `Dockerfile` and `docker-bake.hcl`.
2. Add the version to `TESTED_VERSIONS` in `image/hooks/litellm_versions.py` after a review of the upstream change.
   The list is the same for all hooks.
   [operations.md](operations.md) has the full procedure.
3. Run `scripts/build.sh test`. All tests must pass.
