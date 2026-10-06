# Build definition of the gateway image. scripts/build.sh sets the variables
# from Git; docs/image.md describes the targets.
#
#   docker buildx bake              # gateway, host platform, loaded into the local image store
#   docker buildx bake smoke        # start Python in the gateway image, host platform
#   docker buildx bake test         # repo tests inside the image, host platform
#   docker buildx bake release      # linux/amd64 and linux/arm64; add --push or an output

# Registry part of the tag, for example ghcr.io/owner. Empty gives a local name.
variable "REGISTRY" {
  default = ""
}

variable "IMAGE_NAME" {
  default = "litellm-gateway"
}

# LiteLLM version of BASE_IMAGE. Change both together.
variable "LITELLM_VERSION" {
  default = "1.103.0"
}

# Version X.Y.Z of the portable-vX.Y.Z release tag of this repo, for example 0.1.0.
variable "PORTABLE_VERSION" {
  default = "0"
}

variable "TAG" {
  default = "${LITELLM_VERSION}-p${PORTABLE_VERSION}"
}

# Git commit of the build.
variable "REVISION" {
  default = "unknown"
}

# Build time for the created label. scripts/build.sh uses the commit time.
variable "CREATED" {
  default = "1970-01-01T00:00:00Z"
}

# URL of the public repo for the source label. Must not name a private domain.
variable "SOURCE_URL" {
  default = ""
}

# Index digest of LiteLLM 1.103.0. A CI system can give a mirror: <mirror>/litellm@sha256:<digest>.
variable "BASE_IMAGE" {
  default = "ghcr.io/berriai/litellm@sha256:bd089afdcd35b894b14a93f9743cdc8b591f82da1a38dd43a010a7b0c9de5fd7"
}

# Provenance attestation of the release: "false", "min" or "max". An attestation
# holds the build start time and an invocation ID, so the index digest then
# changes on each build. The platform images stay the same.
variable "PROVENANCE" {
  default = "false"
}

# Package index of the pytest stage of the test target. A CI system can give a mirror.
variable "PIP_INDEX_URL" {
  default = "https://pypi.org/simple"
}

variable "PYTHON_IMAGE" {
  default = "docker.io/library/python@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26"
}

function "image_ref" {
  params = [registry, name, tag]
  result = registry == "" ? "${name}:${tag}" : "${registry}/${name}:${tag}"
}

group "default" {
  targets = ["gateway"]
}

target "_common" {
  context    = "."
  dockerfile = "Dockerfile"
  args = {
    BASE_IMAGE       = BASE_IMAGE
    BASE_NAME        = split("@", BASE_IMAGE)[0]
    BASE_DIGEST      = length(split("@", BASE_IMAGE)) > 1 ? split("@", BASE_IMAGE)[1] : ""
    PYTHON_IMAGE     = PYTHON_IMAGE
    PIP_INDEX_URL    = PIP_INDEX_URL
    LITELLM_VERSION  = LITELLM_VERSION
    PORTABLE_VERSION = PORTABLE_VERSION
    REVISION         = REVISION
    CREATED          = CREATED
    SOURCE_URL       = SOURCE_URL
  }
}

# Host platform only (no platforms attribute). Loads into the local image store.
target "gateway" {
  inherits = ["_common"]
  target   = "gateway"
  tags     = [image_ref(REGISTRY, IMAGE_NAME, TAG)]
  output   = ["type=docker"]
}

# Starts Python in the gateway image of the host platform. The hooks stop an
# untested LiteLLM version, so this build fails for it. Nothing is exported.
# scripts/build.sh runs it before gateway and release.
target "smoke" {
  inherits = ["_common"]
  target   = "smoke"
  output   = ["type=cacheonly"]
}

# Runs pytest inside the image during the build. The build fails when a test fails.
# Host platform only. Nothing is exported.
target "test" {
  inherits = ["_common"]
  target   = "test"
  output   = ["type=cacheonly"]
}

# Both platforms. The final stage has no RUN step, so no emulation is needed.
# No output here: scripts/build.sh gives the registry or an OCI archive.
# A multi-platform build needs a docker-container builder or the containerd image store.
target "release" {
  inherits  = ["_common"]
  target    = "gateway"
  platforms = ["linux/amd64", "linux/arm64"]
  tags      = [image_ref(REGISTRY, IMAGE_NAME, TAG)]
  attest    = [PROVENANCE == "false" ? "type=provenance,disabled=true" : "type=provenance,mode=${PROVENANCE}"]
}
