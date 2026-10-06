#!/bin/sh
# Check the rendered Compose configuration. Runs only `docker compose config`
# on a temporary copy of the tracked Compose files, with .env that
# scripts/create-env.py makes from .env.example.
# It starts no container and reads no .env, secrets/ or state/ of the checkout.
# Usage: sh tests/test_compose.sh
# Needs docker compose and python3.
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d "${TMPDIR:-/tmp}/litellm-compose-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT INT TERM
failed=0

cp "$repo/compose.yaml" "$repo/compose.codex.yaml" "$repo/compose.copilot.yaml" "$repo/.env.example" "$tmp/"
mkdir -p "$tmp/config" "$tmp/secrets"
: >"$tmp/secrets/codex.env"
: >"$tmp/secrets/copilot.env"

# The environment of the caller must not change the result.
unset COMPOSE_FILE COMPOSE_PROJECT_NAME GATEWAY_IMAGE BASE_IMAGE GATEWAY_CONFIG GATEWAY_PORT COPILOT_CONFIG
unset LITELLM_MASTER_KEY LITELLM_SALT_KEY POSTGRES_PASSWORD UI_PASSWORD

# An unchanged copy of .env.example has empty secrets: Compose must refuse it.
cp "$repo/.env.example" "$tmp/.env"
if out=$(cd "$tmp" && docker compose config --format json 2>&1); then
	printf 'FAIL Compose refuses .env.example with empty secrets\n'
	failed=1
elif printf '%s' "$out" | grep -q 'scripts/create-env.py'; then
	printf 'ok   Compose refuses .env.example with empty secrets\n'
else
	printf 'FAIL Compose refuses .env.example with empty secrets\n%s\n' "$out"
	failed=1
fi
rm "$tmp/.env"
python3 "$repo/scripts/create-env.py" "$tmp" >/dev/null

# Arguments: optional VAR=value pairs, then "docker compose" and its file options.
render() {
	(cd "$tmp" && env "$@" config --format json)
}

check() {
	name=$1
	json=$2
	code=$3
	if out=$(printf '%s' "$json" | python3 -c "
import json, sys
c = json.load(sys.stdin)
s = c['services']
$code
" 2>&1); then
		printf 'ok   %s\n' "$name"
	else
		printf 'FAIL %s\n%s\n' "$name" "$out"
		failed=1
	fi
}

base=$(render docker compose)
both=$(render docker compose -f compose.yaml -f compose.codex.yaml)
digest=registry.example/litellm-gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000
pinned=$(render GATEWAY_IMAGE="$digest" BASE_IMAGE=mirror.example/litellm:1 docker compose)
both_pinned=$(render GATEWAY_IMAGE="$digest" docker compose -f compose.yaml -f compose.codex.yaml)
# The form of a host: COMPOSE_FILE names the files.
all_files=compose.yaml:compose.codex.yaml:compose.copilot.yaml
with_copilot=$(render COMPOSE_FILE="$all_files" docker compose)
copilot_only=$(render COMPOSE_FILE=compose.yaml:compose.copilot.yaml docker compose)
copilot_pinned=$(render COMPOSE_FILE="$all_files" GATEWAY_IMAGE="$digest" docker compose)
copilot_host=$(render COMPOSE_FILE="$all_files" COPILOT_CONFIG=./.local/config/copilot.yaml docker compose)

# Checks for each rendering with the Codex services.
common='
image_services = ["gateway", "codex1", "codex2", "codex3", "codex-router", "copilot"]
for name in [n for n in image_services if n in s]:
    svc = s[name]
    assert "PYTHONPATH" not in svc.get("environment", {}), name + " sets PYTHONPATH"
    for v in svc.get("volumes", []):
        t = v["target"]
        assert not t.startswith(("/config/proxy-python", "/router", "/opt/litellm-gateway")), name + " mounts " + t
        assert "image/hooks" not in v.get("source", "") and "image/routers" not in v.get("source", ""), name + " mounts " + v["source"]
    env = svc.get("environment", {})
    assert env.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True", name
    assert "CHATGPT_AUTH_FILE_HOOK" not in env, name + " sets CHATGPT_AUTH_FILE_HOOK"
    assert svc.get("user") == "1000:1000", name
    assert svc.get("cap_drop") == ["ALL"], name
    assert svc.get("security_opt") == ["no-new-privileges:true"], name
    assert svc["logging"]["options"] == {"max-size": "10m", "max-file": "3"}, name
    if name != "codex-router":
        assert "healthcheck" in svc, name
        # python -S: the health check loads no hook and no LiteLLM.
        assert svc["healthcheck"]["test"][:4] == ["CMD", "python", "-S", "-c"], (name, svc["healthcheck"]["test"])
assert c["name"] == "litellm", c["name"]
assert [v["name"] for v in c["volumes"].values()] == ["litellm_postgres-data"], c["volumes"]
'

check "compose.yaml has gateway and database" "$base" '
assert sorted(s) == ["database", "gateway"], sorted(s)
'
check "gateway uses litellm-gateway:local and builds the target gateway" "$base" '
g = s["gateway"]
assert g["image"] == "litellm-gateway:local", g["image"]
assert g["build"]["target"] == "gateway", g["build"]
assert g["build"]["dockerfile"] == "Dockerfile", g["build"]
assert "args" not in g["build"] or "BASE_IMAGE" not in g["build"]["args"], g["build"]
'
check "gateway keeps the configuration mount, no hook mount, no PYTHONPATH" "$base" "
assert [v['target'] for v in s['gateway']['volumes']] == ['/config/config.yaml'], s['gateway']['volumes']
$common"
check "GATEWAY_IMAGE and BASE_IMAGE come from the environment" "$pinned" "
assert s['gateway']['image'] == '$digest', s['gateway']['image']
assert s['gateway']['build']['args'] == {'BASE_IMAGE': 'mirror.example/litellm:1'}, s['gateway']['build']
"
check "with compose.codex.yaml: six services" "$both" '
assert sorted(s) == ["codex-router", "codex1", "codex2", "codex3", "database", "gateway"], sorted(s)
'
check "Codex services use the gateway image and do not build" "$both" "
for name in ('codex1', 'codex2', 'codex3', 'codex-router'):
    assert s[name]['image'] == 'litellm-gateway:local', (name, s[name]['image'])
    assert 'build' not in s[name], name
$common"
check "codex-router starts quota_router from the image path" "$both" '
r = s["codex-router"]
assert r["entrypoint"] == ["python", "-m", "uvicorn"], r["entrypoint"]
cmd = r["command"]
assert cmd[0] == "quota_router:app", cmd
assert cmd[cmd.index("--app-dir") + 1] == "/opt/litellm-gateway/image/routers", cmd
'
check "codex-router has the default order, the token mounts of that order and its start conditions" "$both" '
r = s["codex-router"]
order = r["environment"]["CODEX_ACCOUNT_ORDER"]
assert order == "codex2,codex3,codex1", order
# One read-only token mount for each account of the order except the last.
mounts = {v["target"]: v for v in r["volumes"]}
assert sorted(mounts) == ["/tokens/" + name for name in sorted(order.split(",")[:-1])], sorted(mounts)
for target, v in mounts.items():
    name = target.rsplit("/", 1)[1]
    assert v["source"].endswith("/state/" + name), (target, v["source"])
    assert v.get("read_only") is True, target
assert not any(v["source"].endswith("/state/codex1") for v in r["volumes"]), r["volumes"]
conditions = {name: d["condition"] for name, d in r["depends_on"].items()}
assert conditions == {"codex1": "service_healthy", "codex2": "service_healthy",
                      "codex3": "service_started"}, conditions
'
check "Codex services use GATEWAY_IMAGE" "$both_pinned" "
for name in ('gateway', 'codex1', 'codex2', 'codex3', 'codex-router'):
    assert s[name]['image'] == '$digest', (name, s[name]['image'])
"
check "with compose.copilot.yaml in COMPOSE_FILE: seven services" "$with_copilot" "
assert sorted(s) == ['codex-router', 'codex1', 'codex2', 'codex3', 'copilot', 'database', 'gateway'], sorted(s)
$common"
check "compose.yaml and compose.copilot.yaml: three services" "$copilot_only" "
assert sorted(s) == ['copilot', 'database', 'gateway'], sorted(s)
$common"
check "copilot uses the gateway image, does not build and publishes no port" "$with_copilot" "
k = s['copilot']
assert k['image'] == 'litellm-gateway:local', k['image']
assert 'build' not in k, k
assert not k.get('ports'), k.get('ports')
assert sorted(k['networks']) == ['default'], k['networks']
assert k['restart'] == 'unless-stopped', k['restart']
assert k['command'] == s['gateway']['command'], k['command']
assert k['healthcheck'] == s['codex1']['healthcheck'], k['healthcheck']
"
check "copilot uses GATEWAY_IMAGE" "$copilot_pinned" "
assert s['copilot']['image'] == '$digest', s['copilot']['image']
"
check "copilot mounts its configuration read-only and the token directory read-write" "$with_copilot" "
mounts = {v['target']: v for v in s['copilot']['volumes']}
assert sorted(mounts) == ['/config/config.yaml', '/state/copilot'], sorted(mounts)
assert mounts['/config/config.yaml']['source'].endswith('/config/copilot.yaml'), mounts
assert mounts['/config/config.yaml'].get('read_only') is True, mounts
assert mounts['/state/copilot']['source'].endswith('/state/copilot'), mounts
assert not mounts['/state/copilot'].get('read_only'), mounts
"
check "COPILOT_CONFIG names the configuration of the service copilot" "$copilot_host" "
mounts = {v['target']: v for v in s['copilot']['volumes']}
assert mounts['/config/config.yaml']['source'].endswith('/.local/config/copilot.yaml'), mounts
assert mounts['/config/config.yaml'].get('read_only') is True, mounts
"
check "copilot has the token directory and a disabled device flow, and no other key" "$with_copilot" "
env = s['copilot']['environment']
assert env['GITHUB_COPILOT_TOKEN_DIR'] == '/state/copilot', env
url = env['GITHUB_COPILOT_DEVICE_CODE_URL']
assert url.startswith('http://127.0.0.1:9/'), url
for key in ('CHATGPT_TOKEN_DIR', 'LITELLM_MASTER_KEY', 'LITELLM_SALT_KEY', 'DATABASE_URL', 'UI_PASSWORD', 'CODEX_MASTER_KEY'):
    assert key not in env, key
"
check "the gateway and the Codex services do not get the Copilot token directory" "$with_copilot" "
for name in ('gateway', 'codex1', 'codex2', 'codex3', 'codex-router'):
    assert 'GITHUB_COPILOT_TOKEN_DIR' not in s[name]['environment'], name
    assert all(v['target'] != '/state/copilot' for v in s[name].get('volumes', [])), name
"
if grep -q '^    env_file: \[secrets/copilot.env\]$' "$repo/compose.copilot.yaml"; then
	printf 'ok   copilot reads secrets/copilot.env\n'
else
	printf 'FAIL copilot reads secrets/copilot.env\n'
	failed=1
fi

if grep -q '^[[:space:]]*#*[[:space:]]*COMPOSE_FILE=' "$repo/.env.example"; then
	printf 'FAIL .env.example has no COMPOSE_FILE line\n'
	failed=1
else
	printf 'ok   .env.example has no COMPOSE_FILE line\n'
fi

if [ "$failed" = 0 ]; then
	echo "all compose checks passed"
else
	echo "compose checks failed"
	exit 1
fi
