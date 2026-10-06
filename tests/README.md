# Tests

Run the tests inside the LiteLLM image that the proxies use.
`tests/conftest.py` stops the run when it collects a hook test and
`image/hooks` is not on `PYTHONPATH`, or `image/hooks/sitecustomize.py` did
not load. The hook tests need the hook as the proxies load it at start-up.
`HOOK_TESTS` in `tests/conftest.py` lists them. The other tests need only the
standard library, for example `python3 -m pytest tests/test_sync_local_models.py`
on the host.
`tests/conftest.py` puts `image/routers` on `sys.path`, so the router tests
import `quota_router` and `decision_router` without other settings.
`quota_router` imports `chatgpt_session_id` from `image/hooks`, so
`tests/test_quota_router.py` is in `HOOK_TESTS` too.

The image does not contain `pytest`. Install it once into a separate directory:

```
mkdir -p /tmp/litellm-pytest
docker run --rm -v /tmp/litellm-pytest:/pt python:3.13-slim-bookworm \
  pip install --no-cache-dir --target /pt pytest
```

Run the commands from the repository root.

## LiteLLM 1.103.0 (pinned in `Dockerfile` and `docker-bake.hcl`)

```
docker run --rm --network none --entrypoint python \
  -v "$PWD":/w:ro -v /tmp/litellm-pytest:/pt:ro \
  -e PYTHONPATH=/pt:/w/image/hooks -e PYTHONDONTWRITEBYTECODE=1 \
  -e LITELLM_LOCAL_MODEL_COST_MAP=True -w /w \
  ghcr.io/berriai/litellm@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7 \
  -m pytest -p no:cacheprovider -q tests
```

## LiteLLM 1.101.0 (the previous pin)

```
docker run --rm --network none --entrypoint python \
  -v "$PWD":/w:ro -v /tmp/litellm-pytest:/pt:ro \
  -e PYTHONPATH=/pt:/w/image/hooks -e PYTHONDONTWRITEBYTECODE=1 \
  -e LITELLM_LOCAL_MODEL_COST_MAP=True -w /w \
  ghcr.io/berriai/litellm@sha256:d295634e09c648dcdb72c4cc2dd226f5fb87823a73e88cbbed6f205e4deb044b \
  -m pytest -p no:cacheprovider -q tests
```

The hooks permit only the versions in `TESTED_VERSIONS` of
`image/hooks/litellm_versions.py`. Before you pin another LiteLLM version, add
the version there, run these tests in the new image, and keep the version only
when the tests pass.

## In the gateway image

`scripts/build.sh test` builds the `test` target of `Dockerfile`. It runs
`pytest` on `tests/` inside the gateway image, with the hooks at their image
path `/opt/litellm-gateway/image/hooks`. The build installs a hash-pinned
`pytest` and needs no directory from above. The build fails when a test fails.
To test another base image, set `BASE_IMAGE` and `LITELLM_VERSION`:

```
BASE_IMAGE=ghcr.io/berriai/litellm@sha256:d295634e09c648dcdb72c4cc2dd226f5fb87823a73e88cbbed6f205e4deb044b \
  LITELLM_VERSION=1.101.0 scripts/build.sh test
```

`tests/test_image.sh` checks a built gateway image: hook loaded, stream
iterator patched, session id step installed, routers importable and the router starts, user not `root`,
file owner and mode, image content, entry point, health check command and the
value of each label. `litellm.version` must equal the LiteLLM version in the
image. `EXPECT_REVISION`, `EXPECT_BASE_NAME` and `EXPECT_BASE_DIGEST` give exact
values; without them the check tests the format.
Each container runs with `--rm` and `--network none`.

```
IMAGE_NAME=litellm-gateway TAG=local scripts/build.sh
sh tests/test_image.sh litellm-gateway:local
```

## Compose configuration

`tests/test_compose.sh` checks the rendered Compose configuration. It runs only
`docker compose config` on a temporary copy, with `.env` from `scripts/create-env.py`.
It starts no container. It needs `docker compose` and `python3`.

```
sh tests/test_compose.sh
```

## GitHub Copilot

`tests/test_login_copilot.sh` checks `scripts/login-copilot.sh` with `--dry-run` and a
fake docker command. It starts no container. The checks differ for root and for another user.
`tests/test_copilot_config.py` checks `config/copilot.yaml`, the configuration of the service `copilot`,
and `config/gateway.copilot.example.yaml`, the gateway routes to the service.
`COPILOT_PROPOSAL` names a host proposal file to check with the rules of the gateway routes.
The two tests need `sh` and `python3` only; `unittest` is in the standard library.
`tests/test_compose.sh` renders `compose.copilot.yaml`.

```
sh tests/test_login_copilot.sh
python3 -m unittest tests.test_copilot_config
COPILOT_PROPOSAL=path/to/copilot-routes.json python3 -m unittest tests.test_copilot_config
```

## Codex reauthorization

`tests/test_reauth_codex.sh` checks `scripts/reauth-codex.sh` on a temporary copy of the tree with a fake docker command on `PATH`.
It starts no container and reads no token of the host. It needs `sh` and `python3`.

```
sh tests/test_reauth_codex.sh
```

## Promotion commands

These tests use temporary repositories under `$TMPDIR` (default `/tmp`).
They need Git, POSIX `sh` and Python 3.9 or later.
The promotion tests use local bare remotes and a fake `gh`; they contact no Git server.
The unit tests replace the secret scanner with a test double. Run the real scanner gates separately.
The public hook tests check source refs, valid version tags, deletion restrictions and history ranges.
The promotion suite moves branch and tag refs between checks and both push calls.
It also checks URL rewrites, redacted paths, installed hook invocation and the documented clean-clone procedure.
Refusal tests check the reason as well as the exit code.
The public suite checks committed blobs against different index and working-tree content.
It covers encoded text, binary flags, submodules, symlinks, private filenames and path-bound line exceptions.
`test_promotion_scan.sh` uses the real scanner in a scratch clone with `--no-local` and a synthetic local deny list.
It checks UTF-16 secret payloads and a real installed pre-push hook, using only local bare remotes.
Set `TMPDIR` to a scratch root you create to keep every test directory under that root.
Not verified: real GitHub, macOS execution, malicious concurrent policy edits or every unsupported encoding.

```sh
sh tests/test_promotion.sh
sh tests/test_public_check.sh
sh tests/test_public_hook.sh
sh tests/test_promotion_scan.sh
scripts/public_check.sh selftest
scripts/scan.sh selftest
scripts/scan.sh --level fail tree
scripts/public_check.sh --level fail tree
```

The package index of the `test` target is `PIP_INDEX_URL`, and `PIP_CA_FILE`
gives a CA bundle for it (`docs/ci.md`).
