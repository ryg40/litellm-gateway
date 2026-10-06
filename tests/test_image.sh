#!/bin/sh
# Check a built gateway image. Each container runs with --rm and --network none.
# Usage: sh tests/test_image.sh IMAGE
# Example: IMAGE_NAME=litellm-gateway TAG=local scripts/build.sh && sh tests/test_image.sh litellm-gateway:local
set -eu

image=${1:?usage: sh tests/test_image.sh IMAGE}
root=/opt/litellm-gateway/image
# Container names: <prefix>-<pid>-<n>. Set TEST_NAME_PREFIX to mark the containers of a run.
prefix=${TEST_NAME_PREFIX:-litellm-image-test}-$$
n=0
failed=0

check() {
	name=$1
	shift
	n=$((n + 1))
	cname=$prefix-$n
	if out=$("$@" 2>&1); then
		printf 'ok   %s\n' "$name"
	else
		printf 'FAIL %s\n%s\n' "$name" "$out"
		failed=1
	fi
}

py() {
	# No -e: the checks see the environment of the image.
	docker run --rm --name "$cname" --network none --entrypoint python "$image" -c "$1"
}

check "sitecustomize loads from $root/hooks" py "
import sys
hook = sys.modules.get('sitecustomize')
assert hook is not None, 'sitecustomize not loaded'
assert hook.__file__ == '$root/hooks/sitecustomize.py', hook.__file__
"

check "stream iterator is patched" py "
from litellm.completion_extras.litellm_responses_transformation.transformation import (
    OpenAiResponsesToChatCompletionStreamIterator as Iterator,
)
assert getattr(Iterator, '_gateway_tool_finish', False), 'no _gateway_tool_finish'
"

check "proxy pre-call step sets the ChatGPT session id" py "
from litellm.proxy.utils import ProxyLogging
assert getattr(ProxyLogging, '_gateway_session_id', False), 'no _gateway_session_id'
"

check "routers import from $root/routers" py "
import sys
sys.path.insert(0, '$root/routers')
import quota_router, decision_router
assert quota_router.__file__ == '$root/routers/quota_router.py', quota_router.__file__
import chatgpt_session_id
assert quota_router.cache_session_id is chatgpt_session_id.cache_session_id, 'router has another derivation'
"

check "image sets the local cost map and no telemetry" py "
import os
for key, value in {'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'LITELLM_TELEMETRY': 'False',
                   'PYTHONPATH': '$root/hooks', 'PYTHONDONTWRITEBYTECODE': '1'}.items():
    assert os.environ.get(key) == value, (key, os.environ.get(key))
"

check "runtime user is not root" py "
import os
assert os.getuid() != 0 and os.getgid() != 0, (os.getuid(), os.getgid())
"

check "add-on files are owned by root and read-only" py "
import os
for base, dirs, files in os.walk('/opt/litellm-gateway'):
    for name in dirs + files:
        path = os.path.join(base, name)
        st = os.stat(path)
        assert st.st_uid == 0 and st.st_gid == 0, path
        assert not os.access(path, os.W_OK), path + ' is writable'
"

check "only hooks and routers under /opt/litellm-gateway" py "
import os
found = sorted(os.path.relpath(os.path.join(b, f), '/opt/litellm-gateway')
               for b, _, fs in os.walk('/opt/litellm-gateway') for f in fs)
extra = [f for f in found if not f.startswith(('image/hooks/', 'image/routers/'))]
assert not extra, extra
assert 'image/hooks/sitecustomize.py' in found, found
"

# The health check command of compose.yaml: python -S loads no hook and no LiteLLM.
# With no gateway on the port it must fail.
healthcheck() {
	docker run --rm --name "$cname" --network none --entrypoint python "$image" -S -c "
import sys, urllib.request
assert 'sitecustomize' not in sys.modules and 'litellm' not in sys.modules, sorted(sys.modules)
try:
    urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=5)
except OSError:
    sys.exit(0)
sys.exit('health check passed without a gateway')
"
}

label() {
	docker image inspect --format "{{index .Config.Labels \"$1\"}}" "$image"
}

entrypoint() {
	[ "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$image")" = '["docker/prod_entrypoint.sh"]' ]
}

# Expected label values. EXPECT_REVISION, EXPECT_BASE_NAME and EXPECT_BASE_DIGEST
# give exact values; without them the check tests the format.
matches() {
	printf '%s\n' "$2" | grep -Eqx "$3" || { echo "label $1 is '$2', expected /$3/"; return 1; }
}

labels() {
	oci=org.opencontainers.image
	revision_re='[0-9a-f]{40}(-dirty)?'
	base_name_re='[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._-]+)+'
	base_digest_re='sha256:[0-9a-f]{64}'
	[ -z "${EXPECT_REVISION:-}" ] || revision_re=$EXPECT_REVISION
	[ -z "${EXPECT_BASE_NAME:-}" ] || base_name_re=$EXPECT_BASE_NAME
	[ -z "${EXPECT_BASE_DIGEST:-}" ] || base_digest_re=$EXPECT_BASE_DIGEST
	# The LiteLLM version of the image itself, read without the hooks.
	lv=$(docker run --rm --name "$cname" --network none -e PYTHONPATH= --entrypoint python "$image" \
		-c "from importlib.metadata import version; print(version('litellm'))") || return 1
	lv_re=$(printf '%s' "$lv" | sed 's/[.]/[.]/g')
	matches litellm.version "$(label litellm.version)" "$lv_re" || return 1
	matches title "$(label $oci.title)" 'litellm-gateway' || return 1
	matches description "$(label $oci.description)" 'LiteLLM with the hooks and routers of this repo' || return 1
	matches version "$(label $oci.version)" "$lv_re-p[0-9][0-9A-Za-z.-]*" || return 1
	matches revision "$(label $oci.revision)" "$revision_re" || return 1
	matches created "$(label $oci.created)" '(19[7-9][0-9]|2[0-9]{3})-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z' || return 1
	[ "$(label $oci.created)" != 1970-01-01T00:00:00Z ] || { echo "label created has the default value"; return 1; }
	matches base.name "$(label $oci.base.name)" "$base_name_re" || return 1
	matches base.digest "$(label $oci.base.digest)" "$base_digest_re" || return 1
	# source stays empty unless the build gives SOURCE_URL.
	matches source "$(label $oci.source)" '(https://[^ ]+)?' || return 1
}

# The router command of docs/image.md; the placeholder key is not a credential.
router() {
	docker run --rm --name "$cname" --network none -e CODEX_MASTER_KEY=placeholder --entrypoint timeout "$image" \
		10 python -m uvicorn quota_router:app --app-dir "$root/routers" --host 127.0.0.1 --port 4000 2>&1 |
		grep 'Application startup complete'
}

check "health check (python -S) loads no hook and fails without a gateway" healthcheck
check "entry point of the base image stays" entrypoint
check "labels have the expected values" labels
check "router starts with uvicorn --app-dir" router

if [ "$failed" = 0 ]; then
	echo "all image checks passed"
else
	echo "image checks failed"
	exit 1
fi
