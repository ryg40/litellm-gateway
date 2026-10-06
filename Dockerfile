# Gateway image: the upstream LiteLLM image plus the hooks and routers of this repo.
# docs/image.md describes the paths, the targets and the build command.
# The final stage has no RUN step, so a multi-platform build needs no emulation.

# LiteLLM 1.103.0, index digest for linux/amd64 and linux/arm64.
# A CI system can give a mirror of the same digest.
ARG BASE_IMAGE=ghcr.io/berriai/litellm@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7
# python:3.13-slim-bookworm, index digest. Used only to install pytest for the test target.
ARG PYTHON_IMAGE=docker.io/library/python@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26

# Owner, mode and file times of the add-on files. This stage runs on the build
# platform, so the RUN step needs no emulation. Owner root, read-only for all.
# The file times become SOURCE_DATE_EPOCH: the layer then does not depend on
# the checkout time. rewrite-timestamp only lowers newer times.
FROM --platform=$BUILDPLATFORM ${BASE_IMAGE} AS addons
ARG SOURCE_DATE_EPOCH=0
COPY image/hooks/ /out/image/hooks/
COPY image/routers/ /out/image/routers/
RUN --network=none chown -R 0:0 /out \
    && chmod -R a=rX /out \
    && find /out -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

FROM ${BASE_IMAGE} AS gateway
ARG BASE_IMAGE
ARG LITELLM_VERSION=1.103.0
ARG PORTABLE_VERSION=0
ARG REVISION=unknown
ARG CREATED=1970-01-01T00:00:00Z
ARG SOURCE_URL=
# BASE_NAME and BASE_DIGEST split BASE_IMAGE; docker-bake.hcl sets them.
ARG BASE_NAME=ghcr.io/berriai/litellm
ARG BASE_DIGEST=sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7

# The layout under /opt/litellm-gateway mirrors the repo, so tests/conftest.py
# finds the hooks at <root>/image/hooks in the test target.
COPY --from=addons /out/ /opt/litellm-gateway/

# Python imports sitecustomize from the first PYTHONPATH entry at start.
# The hook directory is read-only, so Python cannot write bytecode there.
# The local model cost map and no telemetry: a start does not depend on the
# network or the day, also with docker run and without Compose.
ENV PYTHONPATH=/opt/litellm-gateway/image/hooks \
    PYTHONDONTWRITEBYTECODE=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_TELEMETRY=False

LABEL org.opencontainers.image.title="litellm-gateway" \
      org.opencontainers.image.description="LiteLLM with the hooks and routers of this repo" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.version="${LITELLM_VERSION}-p${PORTABLE_VERSION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.base.name="${BASE_NAME}" \
      org.opencontainers.image.base.digest="${BASE_DIGEST}" \
      litellm.version="${LITELLM_VERSION}"

# nonroot user of the Wolfi base. Entry point and command stay from the base image.
USER 65532:65532

# Smoke step: start Python in the gateway image of the build host, with the
# hooks as at run time. The hooks stop an untested LiteLLM version, so the
# build fails here and not at run time. scripts/build.sh builds this stage
# before the targets gateway and release. It is not part of the gateway image.
FROM gateway AS smoke
RUN --network=none python -c pass

# pytest and its two dependencies that the base image lacks. The base image
# has packaging and pygments. --require-hashes pins each file.
FROM ${PYTHON_IMAGE} AS pytest
COPY <<EOF /tmp/requirements.txt
pytest==9.1.1 --hash=sha256:37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c
pluggy==1.6.0 --hash=sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746
iniconfig==2.3.0 --hash=sha256:f631c04d2c48c52b84d0d0549c99ff3859c98df65b3101406327ecc7d53fbf12
EOF
# Package index: the public index, or a mirror (docs/ci.md). pip reads PIP_INDEX_URL.
ARG PIP_INDEX_URL=https://pypi.org/simple
# Optional CA bundle for a network with TLS inspection: the BuildKit secret pip-ca.
# Without the secret, pip uses its own CA bundle. The hashes are checked in both cases.
RUN --mount=type=secret,id=pip-ca,required=false \
    if [ -s /run/secrets/pip-ca ]; then export PIP_CERT=/run/secrets/pip-ca; fi \
    && pip install --no-cache-dir --no-deps --only-binary=:all: --require-hashes \
        --target /opt/pytest -r /tmp/requirements.txt

# Runs the repo tests with the hooks at their image paths. The build fails when a test fails.
FROM gateway AS test
COPY --from=pytest --chown=0:0 --chmod=a=rX /opt/pytest/ /opt/pytest/
COPY --chown=0:0 --chmod=a=rX tests/ /opt/litellm-gateway/tests/
COPY --chown=0:0 --chmod=a=rX config/ /opt/litellm-gateway/config/
COPY --chown=0:0 --chmod=a=rX scripts/ /opt/litellm-gateway/scripts/
# tests/test_host_separation.py reads the Compose files.
COPY --chown=0:0 --chmod=a=rX compose.yaml compose.codex.yaml compose.copilot.yaml /opt/litellm-gateway/
ENV PYTHONPATH=/opt/litellm-gateway/image/hooks:/opt/pytest \
    HOME=/tmp
WORKDIR /opt/litellm-gateway
RUN --network=none python -m pytest -p no:cacheprovider -q tests
