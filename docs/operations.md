# Operations

This page covers secrets, backup and recovery, and the update of the LiteLLM base image.
The command blocks of the backup, the upgrade and the rollback need Bash.

## Secrets

Git ignores `.env`, `secrets/` and `state/`. They hold the credentials and the Codex and Copilot tokens.
`.env`, `secrets/codex.env` and `secrets/copilot.env` use mode 0600.
The repository holds only configuration, scripts, tests and documents. `scripts/scan.sh` checks it ([secret-handling.md](secret-handling.md)).

Protect the administrator access to Docker. An administrator can read the environment variables of a container.
The master key of the gateway (`LITELLM_MASTER_KEY`) gives administrator access. Do not give it to untrusted users.

Warning: keep `LITELLM_SALT_KEY` unchanged after the first start of the database. The database encrypts stored credentials with it.

## Backup and recovery

Back up these items separately, with encryption and restricted access:

| Item | Content |
| --- | --- |
| `.env` | Keys and passwords |
| `secrets/` | `codex.env`, `copilot.env`, separate login directories |
| `state/` | Codex OAuth tokens, the Copilot token directory `state/copilot/` |
| Volume `<project>_postgres-data` (`litellm_postgres-data` with the project name `litellm`) | The database: virtual keys, spend, settings from the UI |

A persistent directory or volume is not a backup.

The rules of the blocks of the backup, the upgrade and the rollback:

- Each block needs Bash. Under another shell, the block stops at its first command with a `STOP:` line, and does nothing more.
- Each block runs its commands in a subshell with `set -euCo pipefail`: the first failed command ends it, and `-C` refuses to overwrite a file.
- Export `COMPOSE_PROJECT_NAME` with the project name of the deployment (label `com.docker.compose.project`) before the first block.
  Then each `docker compose` command selects the deployment, also when the `name:` of the Compose files (`litellm`) differs. Each block checks the exported value;
  the restore drill sets its own project only inside its subshell.
- A block passed when its exit status is 0 (`echo $?`) and its last line starts with `ok:`. Messages before that line, for example the progress of `docker compose`, do not count.
  A `STOP:` line, or a last line without `ok:`, means: stop the procedure. The readiness loops retry without messages; only their limit prints a `STOP:` line.
- Do not join a block to another command with `&&` or `||`: in such a list, bash ignores `set -e` inside the subshell.
- Each run writes into a new directory from `mktemp -d` in a private directory outside the checkout.

The other commands of this page, for example in "Update the base image", are single commands: a nonzero exit status or an error message is a stop.

A dump of the database while the stack runs, into a new directory under `$HOME/litellm-backups`:

```sh
backup=$(
  [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
  set -euCo pipefail; umask 077
  project=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$project" ] ||
    { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment" >&2; exit 1; }
  mkdir -p "$HOME/litellm-backups"; chmod 700 "$HOME/litellm-backups"
  dir=$(mktemp -d "$HOME/litellm-backups/dump-XXXXXXXX")
  id=$(docker compose ps -q database)
  case $id in ''|*[!0-9a-f]*) echo "STOP: not one running database container" >&2; exit 1 ;; esac
  echo "$id" > "$dir/database-id"
  docker compose exec -T database pg_dump -U litellm -d litellm > "$dir/litellm.sql.part"
  test -s "$dir/litellm.sql.part" || { echo "STOP: the dump is empty" >&2; exit 1; }
  mv "$dir/litellm.sql.part" "$dir/litellm.sql"
  echo "$dir")
echo "ok: dump ${backup:?STOP: no dump}/litellm.sql"
```

A second run, also in the same second, writes a new directory and does not touch an earlier one. A failed `pg_dump` leaves only `litellm.sql.part`.
`database-id` names the database container of the dump. Keep the last verified backup until a new one passes the restore drill.

The restore drill puts the dump into a new Compose project with its own name, port and volume, with the image ID of the running gateway,
and checks a virtual key of the dump on that port. The project name comes from the name of the backup directory, in lowercase:
a Compose project name has lowercase letters, digits, `-` and `_` only.
Before the block arms the trap that removes the drill project, it checks that no container, volume or network of that name exists.
On a collision, it stops and changes nothing. `COMPOSE_FILE=compose.yaml` keeps a host override file with a fixed `container_name` out of the drill.

```sh
(
  [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
  set -euCo pipefail; umask 077
  : "${backup:?set backup to the directory of the dump}" "${VIRTUAL_KEY:?set VIRTUAL_KEY to a virtual key of the dump}"
  deployment=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$deployment" ] ||   # the source side selects the deployment
    { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment"; exit 1; }
  test -s "$backup/litellm.sql"
  test ! -e "$backup/restore-verified" || { echo "STOP: this dump has a drill result already"; exit 1; }
  gateway=$(docker compose ps -q gateway); database=$(docker compose ps -q database)
  for id in "$gateway" "$database"; do
    case $id in ''|*[!0-9a-f]*) echo "STOP: not one running gateway and one running database"; exit 1 ;; esac
  done
  image=$(docker inspect -f '{{.Image}}' "$gateway")
  case $image in sha256:*) ;; *) echo "STOP: no image ID for the gateway"; exit 1 ;; esac
  before=$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' "$gateway" "$database")
  project=litellm-restore-$(basename "$backup" | tr '[:upper:]' '[:lower:]')
  containers=$(docker ps -a -q -f "label=com.docker.compose.project=$project")
  volumes=$(docker volume ls -q -f "name=$project"); networks=$(docker network ls -q -f "name=$project")
  test -z "$containers$volumes$networks" || { echo "STOP: $project exists; the drill changed nothing"; exit 1; }
  export COMPOSE_PROJECT_NAME=$project COMPOSE_FILE=compose.yaml GATEWAY_PORT=127.0.0.1:4393 GATEWAY_IMAGE=$image
  trap 'docker compose down -v --timeout 30' EXIT
  docker compose up -d --no-build database
  i=0; until docker compose exec -T database pg_isready -q -h 127.0.0.1 -U litellm -d litellm; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "STOP: the drill database is not ready"; exit 1; }; sleep 2
  done
  docker compose exec -T database psql -q -U litellm -d litellm -v ON_ERROR_STOP=1 < "$backup/litellm.sql"
  docker compose up -d --no-build --no-deps gateway
  drill=$(docker compose ps -q gateway); used=$(docker inspect -f '{{.Image}}' "$drill")
  test "$used" = "$image" || { echo "STOP: the drill gateway runs $used"; exit 1; }
  i=0; until curl -fs -m 5 -o /dev/null "http://$GATEWAY_PORT/health/liveliness"; do
    i=$((i + 1)); [ "$i" -lt 60 ] || { echo "STOP: the drill gateway is not ready"; exit 1; }; sleep 2
  done
  curl -fsS -m 30 -o /dev/null "http://$GATEWAY_PORT/v1/models" -H "Authorization: Bearer $VIRTUAL_KEY"
  after=$(docker inspect -f '{{.Id}} {{.State.StartedAt}}' "$gateway" "$database")
  test "$after" = "$before" || { echo "STOP: the running gateway or database changed"; exit 1; }
  docker compose down -v --timeout 30; trap - EXIT
  echo "$image $gateway" > "$backup/restore-verified"
  echo "ok: restore verified in $project with the image $image of the gateway $gateway"
)
```

The backup is verified when the block prints its `ok:` line. `restore-verified` then holds the image ID and the container ID of the gateway; the upgrade compares them with its pin.
The drill waits for the database on TCP (`-h 127.0.0.1`): at the first start of an empty volume, Postgres runs a temporary server on the socket only,
and a restore into it fails with `database "litellm" does not exist`.
A scratch drill ran these blocks as written (end of "Roll the gateway back"). Earlier, a documentation test on a clone of release 0.9.0 (LiteLLM 1.108.2) restored a dump into a second project.
Not verified: a restore of `.env`, `secrets/` and `state/`.

A new clone needs new secrets and new Codex logins ([codex-services.md](codex-services.md)), or a verified secure restore.
A new login for an account that exists: `sh scripts/reauth-codex.sh <1|2|3|all>` ([codex-services.md](codex-services.md), section "Reauthorize an account"). `--check` prints the state of each login and changes nothing.

Warning: `docker compose down -v` deletes the database volume. Another project name (`-p`) uses another, empty volume.

## Update the base image

The image pins the LiteLLM base image by index digest ([image.md](image.md)).

1. Select an official LiteLLM release. Read the index digest of its image.

   ```sh
   docker buildx imagetools inspect ghcr.io/berriai/litellm:<version>
   ```

2. Verify the signature of the release with the cosign command of the upstream release notes, when cosign is available.
   The signature of the pinned 1.110.2 digest passed this check (cosign from its container image, with the identity of the release notes).
3. Set `BASE_IMAGE` (with the index digest) and `LITELLM_VERSION` in `Dockerfile` and in `docker-bake.hcl`.
4. Read the upstream change of the files that the hooks patch. `scripts/fetch-upstream.sh` gets the release for a comparison ([remotes.md](remotes.md)).
5. Add the version to `TESTED_VERSIONS` in `image/hooks/litellm_versions.py`. The list is the same for all hooks.
   `sitecustomize.py` stops the start of each process on a version that is not in the list, before it imports LiteLLM.
   The message names the version that it found, the tested versions and this procedure.
   `chatgpt_auth_file.py` uses the same list when its switch is on ([codex-accounts.md](codex-accounts.md)).
6. Run the tests in the new image. All tests must pass.

   ```sh
   scripts/build.sh test
   ```

7. A new stack only (no running service, no database of an older version): build the image and start the stack.
   Test the authentication and one inference on each provider that you use.

   ```sh
   docker compose up -d --build
   python3 scripts/verify.py --model MODEL_ID
   ```

   For a running stack, also with a local build, do not run this command. Use "Upgrade a running stack" below.
8. For the Codex routes, test xhigh chat and streaming tool calls ([codex-services.md](codex-services.md)).

A deployment that uses a released image sets `GATEWAY_IMAGE` to the new digest ([host-configuration.md](host-configuration.md)) and recreates the services in the order of "Upgrade a running stack".

### Upgrade a running stack

`compose.yaml`, `compose.codex.yaml` and `compose.copilot.yaml` use one `GATEWAY_IMAGE` for all six services.
A plain `docker compose up -d` (with or without `--build`) after a change of the image recreates all of them at once.
It stops the gateway with its `stop_grace_period` of 60 seconds (`compose.yaml`), and each other service with the default of Docker: SIGTERM, then SIGKILL after 10 seconds.
Recreate one service at a time instead, in the order below.

Run all blocks of the upgrade in one shell, in the directory of the deployment. Set `COMPOSE_FILE` (in the environment or in `.env`)
to the Compose file list of the running containers (label `com.docker.compose.project.config_files`),
and export `COMPOSE_PROJECT_NAME` with their project name (label `com.docker.compose.project`). The blocks follow the rules of "Backup and recovery".
First define the helpers. This block defines functions only, changes nothing, and prints `ok: helpers defined`:

```sh
if [ -n "${BASH_VERSION-}" ]; then
endpoint() (      # endpoint CONTAINER: the one local address of its published port 4000
  set -euo pipefail
  bindings=$(docker port "$1" 4000/tcp)
  addr=$(printf '%s\n' "$bindings" | sed -n -e 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/127.0.0.1:\1/p' \
    -e 's/^0\.0\.0\.0:\([0-9][0-9]*\)$/127.0.0.1:\1/p' | sort -u)
  case $addr in ''|*[!0-9.:]*) echo "STOP: not one local address for port 4000 of $1: $bindings" >&2; exit 1 ;; esac
  echo "http://$addr"
)
start_run() (     # start_run KIND: a new run directory with the pin of the deployment; prints its path
  set -euCo pipefail; umask 077
  project=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$project" ] ||
    { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment" >&2; exit 1; }
  mkdir -p "$HOME/litellm-runs"; chmod 700 "$HOME/litellm-runs"
  dir=$(mktemp -d "$HOME/litellm-runs/$1-XXXXXXXX")
  gateway=$(docker compose ps -q gateway); database=$(docker compose ps -q database)
  for id in "$gateway" "$database"; do
    case $id in ''|*[!0-9a-f]*) echo "STOP: not one running gateway and one running database" >&2; exit 1 ;; esac
    labels=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$id")
    test "$labels" = "$project $PWD" || { echo "STOP: $id is not of the project $project in $PWD" >&2; exit 1; }
  done
  base=$(endpoint "$gateway")
  curl -fsS -m 5 -o /dev/null "$base/health/liveliness" || { echo "STOP: the gateway does not answer on $base" >&2; exit 1; }
  echo "$project" > "$dir/project"; echo "$gateway" > "$dir/gateway-id"; echo "$database" > "$dir/database-id"
  docker inspect -f '{{.Image}}' "$gateway" > "$dir/gateway-image"; echo "$base" > "$dir/base"
  echo "$dir"
)
value() (         # value FILE: the text of FILE without final newlines; fails when it is missing or empty, or has two lines of text or more
  set -euo pipefail
  v=$(cat "$1")
  case $v in ''|*$'\n'*) echo "STOP: $1 is empty or has more than one line" >&2; exit 1 ;; esac
  printf '%s\n' "$v"
)
in_project() (    # in_project: fails unless the exported COMPOSE_PROJECT_NAME is the project of the run
  set -euo pipefail
  selected=$(printenv COMPOSE_PROJECT_NAME) || selected=
  pinned=$(value "$run/project")
  test "$selected" = "$pinned" || { echo "STOP: the exported COMPOSE_PROJECT_NAME is '$selected', not $pinned of the run" >&2; exit 1; }
)
sql() { docker compose exec -T database psql -U litellm -d litellm -At -v ON_ERROR_STOP=1 -c "$1"; }
backlog() { local base key; base=$(value "$run/base") && key=$(sed -n 's/^LITELLM_MASTER_KEY=//p' .env) && [ -n "$key" ] &&
  curl -fsS -m 10 "$base/health/backlog" -H "Authorization: Bearer $key"; }
wait_idle() (     # until in_flight_requests is 1 (the check itself) for 30 s in a row, then 40 s more; at most 200 samples
  set -euo pipefail
  since=
  for i in $(seq 200); do
    now=$(date +%s); inflight=$(backlog | python3 -c 'import json, sys; print(json.load(sys.stdin)["in_flight_requests"])')
    if [ "$inflight" = 1 ]; then since=${since:-$now}; else since=; fi
    if [ -n "$since" ] && [ $((now - since)) -ge 30 ]; then sleep 40; exit 0; fi
    sleep 3
  done
  echo "STOP: the gateway was not idle for 30 s in 200 samples" >&2; exit 1
)
spend() (         # key spend|daily requests|daily spend of the canary key; no line without the key
  set -euo pipefail
  canary_alias=$(value "$run/alias")
  sql "select t.spend || '|' || coalesce(sum(d.api_requests), 0) || '|' || coalesce(sum(d.spend), 0)
    from \"LiteLLM_VerificationToken\" t left join \"LiteLLM_DailyUserSpend\" d on d.api_key = t.token
    where t.key_alias = '$canary_alias' group by t.token, t.spend"
)
priced_request() (  # priced_request HEADERS: one canary request; prints its cost
  set -euo pipefail
  base=$(value "$run/base"); canary=$(value "$run/canary-key"); route=$(value "$run/route")
  code=$(curl -sS -m 120 -o /dev/null -D "$1" -w '%{http_code}' "$base/v1/chat/completions" \
    -H "Authorization: Bearer $canary" -H "Content-Type: application/json" \
    -d '{"model": "'"$route"'", "messages": [{"role": "user", "content": "Say OK."}]}')
  test "$code" = 200 || { echo "STOP: HTTP $code" >&2; exit 1; }
  cost=$(awk -F': *' 'tolower($1) == "x-litellm-response-cost" { n++; c = $2; sub(/\r$/, "", c) } END { if (n == 1) print c }' "$1")
  awk -v c="$cost" 'BEGIN { exit !(c ~ /^[0-9]+([.][0-9]+)?(e[-+]?[0-9]+)?$/ && c + 0 > 0) }' ||
    { echo "STOP: the cost is not one positive number: '$cost'" >&2; exit 1; }
  echo "$cost"
)
delta() {         # delta BEFORE AFTER REQUESTS COST: the increase, then match or DIFFERENT; fails on malformed input
  paste -d '|' "$1" "$2" | awk -F'|' -v n="$3" -v c="$4" '
    function num(x) { return x ~ /^[0-9]+([.][0-9]+)?(e[-+]?[0-9]+)?$/ }
    function near(x) { return (x - c) ^ 2 <= (c * 1e-6) ^ 2 }
    NR > 1 || NF != 6 || !num($1) || !num($3) || !num($4) || !num($6) || $2 !~ /^[0-9]+$/ || $5 !~ /^[0-9]+$/ || !num(c) || c + 0 <= 0 { bad = 1; exit }
    { dk = $4 - $1; dr = $5 - $2; ds = $6 - $3; ok = dr == n && dk > 0 && ds > 0 && near(dk) && near(ds)
      printf "key +%.12g daily +%d +%.12g expected +%d +%.12g: %s\n", dk, dr, ds, n, c, ok ? "match" : "DIFFERENT" }
    END { if (bad || NR != 1) { print "STOP: malformed input" > "/dev/stderr"; exit 1 } }'
}
echo "ok: helpers defined"
else
  echo "STOP: run the blocks in bash" >&2
fi
```

`start_run` pins the deployment. The running gateway and database must be one container each, with the labels of the exported `COMPOSE_PROJECT_NAME` and of this directory.
It writes the project, their container IDs and the image ID of the gateway into a new run directory under `$HOME/litellm-runs`.
The address of the gateway comes from the published port 4000 of the pinned container, not from `.env`: there must be exactly one local address,
and the gateway must answer there before a block sends a key. Before a block stops a container, it compares the running containers with this pin.
`in_project` stops a block when the exported `COMPOSE_PROJECT_NAME` is not the project of the run.
Call the helpers as commands of their own, as the blocks do: in an `if`, `&&` or `||` list, bash ignores their `set -e`.

`NEW_IMAGE` is the verified reference of the new image: the index digest of a release, or the image ID of a local build.
With a local build, build without a recreate and take the image ID of the build:

```sh
NEW_IMAGE=$(
  [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
  set -euCo pipefail
  project=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$project" ] ||
    { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment" >&2; exit 1; }
  docker compose build gateway >&2
  name=$(docker compose config --format json | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["gateway"]["image"])')
  docker image inspect -f '{{.Id}}' "$name")
echo "ok: NEW_IMAGE=${NEW_IMAGE:?STOP: no build}"
```

Do not run `docker compose build` while `GATEWAY_IMAGE` names a released image: the build gives that name to the new build.
Do not remove unused images until the upgrade is accepted: the older image must stay.

Start the run. The image ID of the running gateway in the pin is the older image of a rollback. No block reads `OLD_IMAGE`.

```sh
run=$(start_run upgrade)
(
  [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
  set -euCo pipefail; umask 077
  : "${run:?STOP: no run directory}" "${NEW_IMAGE:?set NEW_IMAGE to the verified reference of the new image}"
  in_project
  old=$(value "$run/gateway-image"); new=$(docker image inspect -f '{{.Id}}' "$NEW_IMAGE")
  case $new in sha256:*) ;; *) echo "STOP: no image ID for NEW_IMAGE"; exit 1 ;; esac
  test "$new" != "$old" || { echo "STOP: NEW_IMAGE is the image of the running gateway"; exit 1; }
  echo "$NEW_IMAGE" > "$run/new-image"; echo "$new" > "$run/new-image-id"
  echo "ok: run $run; older image $old, new image $new"
)
```

Keep the path of the run directory: a rollback needs it.
Recreate each service with its name in the block below (`service=`). The block uses the project and the new image of the run, `--no-deps`, `--no-build` and `--timeout 60`.
`--timeout 60` gives each service 60 seconds to stop. The gateway has that time also without the option, from its `stop_grace_period`;
the option keeps the time with a Compose file that does not set it.

```sh
(
  [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
  set -euCo pipefail
  : "${run:?}"; service=codex1                 # the service of this step
  in_project
  image=$(value "$run/new-image")
  GATEWAY_IMAGE="$image" docker compose up -d --no-deps --no-build --timeout 60 "$service"
  echo "ok: $service recreated with $image"
)
```

1. The account services: `codex1` first (the final fallback), then `codex2`, then `codex3`. The preferred account comes last, so that the router keeps an older first choice until the end.
   After each one, check its health and one request on its explicit route. A stop cuts the streams that run through the service: choose a quiet minute.
2. `codex-router`. Check `GET /routing/status` with the service key, and one request on a `codex-auto/*` route.
3. `copilot`. Do the stream check of [copilot.md](copilot.md), step 9. A 401 or 403 from the Copilot API: roll back only `copilot` (end of "Roll the gateway back").
4. The gateway last. The gateway step migrates the database: first make the dump and the restore drill of "Backup and recovery" in this shell.
   The stop block refuses to stop the older gateway without a drill of a dump of the pinned database, with the pinned gateway and its image.
   Stop new requests at the entry point, and wait until the gateway is idle. The block waits until `in_flight_requests` is 1 (the check itself)
   for 30 seconds in a row, then 40 seconds more. It stops after 200 samples without such an interval; each sample waits up to 10 seconds for its answer, then 3 seconds.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail
     : "${run:?}"
     in_project
     wait_idle
     echo "ok: idle for 30 seconds, then 40 seconds more"
   )
   ```

   Then stop the older gateway. The block writes the status of each step into `$run/stop-status`.
   A 1.109.1 gateway has no delivery rows, so this stop has no delivery query.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail; umask 077
     : "${run:?}" "${backup:?set backup to the directory of the verified dump}"
     in_project
     gateway=$(value "$run/gateway-id"); database=$(value "$run/database-id"); image=$(value "$run/gateway-image")
     verified=$(cat "$backup/restore-verified" 2> /dev/null) || verified=none
     test "$verified" = "$image $gateway" || { echo "STOP: no restore drill with the pinned gateway and its image"; exit 1; }
     dumped=$(value "$backup/database-id")
     test "$dumped" = "$database" || { echo "STOP: the dump is not of the pinned database"; exit 1; }
     now_gateway=$(docker compose ps -q gateway); now_database=$(docker compose ps -q database)
     test "$now_gateway $now_database" = "$gateway $database" || { echo "STOP: the running gateway or database is not the pinned one"; exit 1; }
     echo "$backup" > "$run/backup"; : > "$run/stop-status"
     stopped_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
     rc=0; docker compose stop --timeout 60 gateway || rc=$?; echo "stop=$rc" >> "$run/stop-status"
     code=$(docker inspect -f '{{.State.ExitCode}}' "$gateway") || code=unknown; echo "exit=$code" >> "$run/stop-status"
     rc=0; docker compose logs --since "$stopped_at" gateway > "$run/gateway-stop.log" 2>&1 || rc=$?; echo "log=$rc" >> "$run/stop-status"
     rc=0; grep -q 'Application shutdown complete' "$run/gateway-stop.log" || rc=$?; echo "marker=$rc" >> "$run/stop-status"
     stops=$(cat "$run/stop-status"); echo "$stops"
     test "$stops" = "$(printf 'stop=0\nexit=0\nlog=0\nmarker=0')" || { echo "STOP: the older gateway did not stop cleanly"; exit 1; }
     echo "ok: the older gateway stopped cleanly"
   )
   ```

   After its `ok:` line, start the new gateway: the block needs the clean stop of this run, and checks the image and the address of the new gateway.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail
     : "${run:?}"
     in_project
     stops=$(cat "$run/stop-status")
     test "$stops" = "$(printf 'stop=0\nexit=0\nlog=0\nmarker=0')" || { echo "STOP: no clean stop of the older gateway in this run"; exit 1; }
     image=$(value "$run/new-image"); expected=$(value "$run/new-image-id"); base=$(value "$run/base")
     GATEWAY_IMAGE="$image" docker compose up -d --no-deps --no-build --timeout 60 gateway
     i=0; until curl -fs -m 5 -o /dev/null "$base/health/liveliness"; do
       i=$((i + 1)); [ "$i" -lt 60 ] || { echo "STOP: the new gateway is not ready after 60 tries"; exit 1; }; sleep 2
     done
     gateway=$(docker compose ps -q gateway); used=$(docker inspect -f '{{.Image}}' "$gateway"); now=$(endpoint "$gateway")
     test "$used" = "$expected" || { echo "STOP: the gateway runs $used, not the new image"; exit 1; }
     test "$now" = "$base" || { echo "STOP: the gateway answers on $now, not on $base"; exit 1; }
     echo "ok: the new gateway runs $used and answers on $base"
   )
   ```

Reason for the order: the services hold no database state, and each one rolls back alone. The gateway step has the migration, the change of the spend path and the change of the Responses ids ([codex-services.md](codex-services.md)).
A rehearsal of 1.109.1 to 1.110.2 gave the same request results in both mixed orders (newer gateway with older services, and the reverse), so the order follows the risk.
With the services first, a rollback of the gateway returns to a rehearsed pair: older gateway, newer services.

Warning: a downgrade of LiteLLM can meet a database that a newer version migrated. Use the steps of "Roll the gateway back" below. The verified backup is the last method: a restore loses each write after the dump.

A rehearsal of 1.109.1 to 1.110.2 on a copy of a database schema, with synthetic keys and rows, showed:

- 1.110.2 applies 8 new migrations (225 to 233). They touch only new or empty tables and drop the unused accounting event tables (upstream PR 49923).
- 1.109.1 starts on the 1.110.2 schema. Its log names 8 migrations that are newer than its own, and it applies none. Its schema dump before and after the start is equal. Keys made by 1.110.2 work on it.
- 1.110.2 writes spend through `LiteLLM_SpendLogDelivery` rows; 1.109.1 has no code for them. A row that is not `applied` when 1.109.1 takes over stays unapplied until a 1.110.x gateway runs again.
  1.110.2 writes spend in two steps: a delivery row, and its application one spend cycle later (each cycle about 10 to 15 seconds).
  Docker and Compose send SIGKILL 10 seconds after SIGTERM, unless `stop_grace_period` or `--timeout` gives a longer time. LiteLLM waits up to `GRACEFUL_SHUTDOWN_TIMEOUT` (default 30 seconds) for open requests.
  `compose.yaml` therefore sets `stop_grace_period: 60s` for the gateway; `tests/test_compose.sh` fails without it.
  A stop that ends with SIGKILL loses from the rollups the spend of the requests that finished in the last 30 seconds or so.
  Some of them leave `pending` rows, which 1.109.1 never applies and a later 1.110.x gateway applies. Others leave no row at all: a missing row does not prove that no spend is lost.
  With a backlog of open requests, the default stop of 10 seconds killed the 1.110.2 gateway (exit code 137). A stop with `--timeout 60` drained the requests and exited with code 0, but cut the streams that still needed more than about 30 seconds after SIGTERM.
  A stop after the gateway was idle and two spend cycles had passed exited with code 0 in 2 seconds and applied all rows.
  A row becomes `parked` only for an undecodable payload or after 5 failed attempts, and no code moves it back to `pending`.
  The 60 seconds fit `GRACEFUL_SHUTDOWN_TIMEOUT`. At SIGTERM, uvicorn waits up to 30 seconds for open requests and cancels the rest.
  The teardown then drains the callbacks within the rest of the same 30 seconds and flushes the spend (`proxy/shutdown/graceful_shutdown_manager.py`, `proxy/proxy_server.py`).
  Under a backlog, such stops ended after about 33 seconds, so 60 seconds leave a margin for the flush.
  The limit is the time that a request still needs after SIGTERM, not its total time: a long request near its end can finish, and one that needs more than 30 seconds more is cut.
  The 60 seconds of `stop_grace_period` are the limit of Docker before SIGKILL, not 60 seconds of drain.
  A longer `GRACEFUL_SHUTDOWN_TIMEOUT` must stay below the grace period minus the flush time; no test showed a need, so the stack keeps 30 seconds.
- With `stop_grace_period: 60s`, a scratch test of the 1.110.2 gateway image ran a backlog of 30 non-stream requests (10 of 0, 10 of 5, 8 of 20 and 2 of 45 seconds) with a fresh key:
  - a plain `docker compose stop gateway` exited with code 0 after 33 seconds;
  - a plain `docker compose up -d` after a change of `.env` recreated only the gateway, and the old container exited with code 0.
  In both cases the 28 requests that finished for the client (HTTP 200 with a cost) were all in the rollups: 28 successful requests and the sum of their costs, in the key spend and in the daily spend.
  The 2 requests of 45 seconds got HTTP 500. A control without the setting: the same plain stop killed the gateway after 10 seconds (exit code 137);
  of 20 finished requests only 10 reached the rollups, and no `pending` row was left for the others.
- The dropped tables do not come back with an image rollback.

Not verified: these steps on a database with real rows.

### Roll the gateway back

No delivery row other than `applied` is necessary, but it does not prove that no spend is lost: a kill can leave `pending` rows, and it can also lose spend that has no row yet.
The steps below therefore reconcile a fresh canary key with records of the client.
This check covers the canary requests only. They take the spend path of all keys, after new requests have stopped.
For the requests of other keys before step 1, the evidence is the idle wait, the clean stop and the delivery rows, not a reconciliation for each key.
A rehearsal on a copy of a schema with synthetic keys and a stub provider gave the waits. They are measurements, not guarantees.
Run the blocks in one shell, with the helpers, `COMPOSE_FILE` and the exported `COMPOSE_PROJECT_NAME` of "Upgrade a running stack",
and with `UPGRADE_RUN` set to the run directory of the upgrade. Each block writes into the new run directory of the rollback.

1. Stop new requests at the entry point: stop the clients, or the route of the reverse proxy to the gateway. From here on only the canary calls the gateway.
2. Start the run: pin the newer gateway, make a fresh canary key with an alias from the run directory, read its baselines, and send 3 requests with it.
   Before it makes the key, the block compares `UPGRADE_RUN` with the running stack: its database must be the running one, and its new image the image of the running gateway.
   The block stops unless the baselines read `0|0|0` and each request gave HTTP 200 and one positive cost. A next attempt is a new run of this block.

   ```sh
   run=$(start_run rollback)
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail; umask 077
     : "${run:?STOP: no run directory}" "${UPGRADE_RUN:?set UPGRADE_RUN to the run directory of the upgrade}"
     in_project
     older=$(value "$UPGRADE_RUN/gateway-image"); expected=$(value "$UPGRADE_RUN/new-image-id"); upgraded=$(value "$UPGRADE_RUN/database-id")
     dumped=$(value "$UPGRADE_RUN/backup"); current=$(value "$run/gateway-image"); database=$(value "$run/database-id")
     case $older in sha256:*) ;; *) echo "STOP: no image ID for the older image"; exit 1 ;; esac
     test "$database" = "$upgraded" || { echo "STOP: UPGRADE_RUN is not of the running database"; exit 1; }
     test "$current" = "$expected" || { echo "STOP: the running gateway does not run the new image of UPGRADE_RUN"; exit 1; }
     test "$current" != "$older" || { echo "STOP: the running gateway has the older image"; exit 1; }
     docker image inspect "$older" > /dev/null
     echo "$older" > "$run/older-image"
     echo codex1/gpt-6.2-sol > "$run/route"           # a route with prices in model_info
     echo "rollback-canary-${run##*-}" > "$run/alias"  # a new alias for each run
     key=$(sed -n 's/^LITELLM_MASTER_KEY=//p' .env); base=$(value "$run/base"); route=$(value "$run/route"); canary_alias=$(value "$run/alias")
     test -n "$key" || { echo "STOP: no LITELLM_MASTER_KEY in .env"; exit 1; }
     curl -fsS -m 30 "$base/key/generate" -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
       -d '{"key_alias": "'"$canary_alias"'", "models": ["'"$route"'"]}' |
       python3 -c 'import json, sys; print(json.load(sys.stdin)["key"])' > "$run/canary-key"
     spend > "$run/spend-before"
     baseline=$(cat "$run/spend-before"); test "$baseline" = "0|0|0" || { echo "STOP: the baselines of the new key read '$baseline'"; exit 1; }
     : > "$run/canary-costs"
     for n in 1 2 3; do priced_request "$run/canary-$n.headers" >> "$run/canary-costs"; done
     awk '{ s += $1 } END { if (NR != 3) exit 1; printf "%.12g\n", s }' "$run/canary-costs" > "$run/canary-sum"
     sum=$(value "$run/canary-sum")
     echo "ok: run $run; canary $canary_alias, baselines 0|0|0, 3 priced requests, cost $sum"
   )
   ```

3. Wait until the gateway is idle: the block waits until `in_flight_requests` is 1 (the check itself) for 30 seconds in a row,
   then 40 seconds more, in which two spend cycles apply the last delivery rows. Then it shows the delivery rows. It stops after 200 samples without such an interval.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail
     : "${run:?}"
     in_project
     wait_idle
     sql "select state, count(*), min(created_at) from \"LiteLLM_SpendLogDelivery\" group by state"
     echo "ok: idle for 30 seconds, then 40 seconds more"
   )
   ```

4. Stop the newer gateway, and keep the evidence before any other step. The block compares the running containers with the pin first.
   It writes the status of each step into `$run/stop-status`. The log starts at the time of the stop, so its shutdown line comes from this stop.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail; umask 077
     : "${run:?}"
     in_project
     gateway=$(value "$run/gateway-id"); database=$(value "$run/database-id")
     now_gateway=$(docker compose ps -q gateway); now_database=$(docker compose ps -q database)
     test "$now_gateway $now_database" = "$gateway $database" || { echo "STOP: the running gateway or database is not the pinned one"; exit 1; }
     : > "$run/stop-status"
     stopped_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
     rc=0; docker compose stop --timeout 60 gateway || rc=$?; echo "stop=$rc" >> "$run/stop-status"
     code=$(docker inspect -f '{{.State.ExitCode}}' "$gateway") || code=unknown; echo "exit=$code" >> "$run/stop-status"
     rc=0; docker compose logs --since "$stopped_at" gateway > "$run/gateway-stop.log" 2>&1 || rc=$?; echo "log=$rc" >> "$run/stop-status"
     rc=0; grep -q 'Application shutdown complete' "$run/gateway-stop.log" || rc=$?; echo "marker=$rc" >> "$run/stop-status"
     rc=0; sql "select delivery_id, state, attempts, created_at from \"LiteLLM_SpendLogDelivery\" where state <> 'applied'" \
       > "$run/deliveries-not-applied" || rc=$?; echo "export=$rc" >> "$run/stop-status"
     rc=0; sql "select state, count(*) from \"LiteLLM_SpendLogDelivery\" group by state" > "$run/delivery-counts" || rc=$?
     echo "counts=$rc" >> "$run/stop-status"
     stops=$(cat "$run/stop-status"); echo "$stops"; cat "$run/delivery-counts"
     test "$stops" = "$(printf 'stop=0\nexit=0\nlog=0\nmarker=0\nexport=0\ncounts=0')" ||
       { echo "STOP: the stop is not clean; the evidence is in $run"; exit 1; }
     echo "ok: the newer gateway stopped cleanly; the evidence is in $run, and step 5 decides"
   )
   ```

5. Read the totals again and decide. The block prints `GO` only when all of these hold:
   - all six status values are 0;
   - the export of the rows that are not `applied` exists and is empty;
   - the counts have at least one `applied` row and no other state;
   - the increase since step 2 equals the records of step 2: 3 requests, and their cost in the key spend and in the daily spend.

   The canary requests of step 2 make delivery rows, and LiteLLM keeps `applied` rows for at least one day (`cleanup_applied`, `retention_days=1`):
   counts without an `applied` row contradict the canary. A cost matches when the increase is positive and within one millionth of the expected cost.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail; umask 077
     : "${run:?}"
     in_project
     sum=$(value "$run/canary-sum"); stops=$(cat "$run/stop-status"); counts=$(cat "$run/delivery-counts")
     spend > "$run/spend-after"
     result=$(delta "$run/spend-before" "$run/spend-after" 3 "$sum"); echo "$result" > "$run/reconcile"; echo "$result"
     test "$stops" = "$(printf 'stop=0\nexit=0\nlog=0\nmarker=0\nexport=0\ncounts=0')" || { echo "STOP: the stop is not clean (stop-status)"; exit 1; }
     test -f "$run/deliveries-not-applied" || { echo "STOP: no export of the rows that are not applied"; exit 1; }
     test ! -s "$run/deliveries-not-applied" || { echo "STOP: the export holds rows that are not applied"; exit 1; }
     printf '%s\n' "$counts" | awk -F'|' 'NF == 0 { next } NF != 2 || $2 !~ /^[0-9]+$/ { bad = 1 } $1 == "applied" && $2 > 0 { seen = 1 }
       END { exit bad || !seen }' || { echo "STOP: the delivery counts are malformed or have no applied row"; exit 1; }
     others=$(printf '%s\n' "$counts" | awk -F'|' 'NF && $1 != "applied" { printf "%s %s; ", $2, $1 }')
     test -z "$others" || { echo "STOP: delivery rows that are not applied: $others"; exit 1; }
     case $result in *': match') ;; *) echo "STOP: the canary spend is DIFFERENT"; exit 1 ;; esac
     echo GO > "$run/decision"
     echo "ok: GO; start the older gateway (step 6)"
   )
   ```

   Each `STOP` ends the rollback. Do not delete or change delivery rows, and find the cause before a next step. The states:

   - The stop is not clean: `exit=137` is a kill, `exit=unknown` a missing container, `marker=1` no shutdown line from this stop,
     and `export` or `counts` other than 0 no reading of the delivery rows.
   - Rows are `pending`: only a 1.110.x gateway applies them. Rows are `parked`: no code moves them back to `pending`.
   - The export holds rows, or the counts have no `applied` row: the two readings contradict each other or the canary.
   - The canary spend is `DIFFERENT`, also with `pending` rows: rows of other keys do not explain the canary.
   - A file is missing or malformed, or the totals have no reading: a missing reading is not a zero.

   After a `STOP`, the newer gateway is stopped. Start it again with the image of `$UPGRADE_RUN/new-image` when the stack must serve.
   A next attempt of the rollback is a new run from step 2.
6. Start the older gateway with the image of the pin of the upgrade, and accept it. The block needs the `GO` of this run.
   It stops unless the gateway is ready within 60 tries (each try waits up to 5 seconds), runs the older image, answers on the address of the pin,
   logs both migration lines, and charges one more canary request (HTTP 200, one positive cost) in the key spend and in the daily spend within 24 polls of 5 seconds.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euCo pipefail; umask 077
     : "${run:?}"
     in_project
     decision=$(cat "$run/decision" 2> /dev/null) || decision=none
     test "$decision" = GO || { echo "STOP: no GO in this run"; exit 1; }
     older=$(value "$run/older-image"); base=$(value "$run/base")
     case $older in sha256:*) ;; *) echo "STOP: no image ID for the older image"; exit 1 ;; esac
     started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
     GATEWAY_IMAGE="$older" docker compose up -d --no-deps --no-build --timeout 60 gateway
     i=0; until curl -fs -m 5 -o /dev/null "$base/health/liveliness"; do
       i=$((i + 1)); [ "$i" -lt 60 ] || { echo "STOP: the older gateway is not ready after 60 tries"; exit 1; }; sleep 2
     done
     gateway=$(docker compose ps -q gateway); image=$(docker inspect -f '{{.Image}}' "$gateway"); now=$(endpoint "$gateway")
     test "$image" = "$older" || { echo "STOP: the gateway runs $image, not the older image"; exit 1; }
     test "$now" = "$base" || { echo "STOP: the gateway answers on $now, not on $base"; exit 1; }
     docker compose logs --since "$started_at" gateway > "$run/older-start.log" 2>&1
     grep -q 'NEWER than any migration' "$run/older-start.log" && grep -q 'No pending migrations to apply' "$run/older-start.log" ||
       { echo "STOP: the older gateway did not log both migration lines"; exit 1; }
     spend > "$run/spend-older-before"
     cost=$(priced_request "$run/canary-older.headers")
     for i in $(seq 24); do
       sleep 5; spend > "$run/spend-older-after-$i"
       result=$(delta "$run/spend-older-before" "$run/spend-older-after-$i" 1 "$cost")
       case $result in *': match') echo "$result"; echo "ok: the older gateway charged the canary request (poll $i)"; exit 0 ;; esac
     done
     echo "STOP: no match after 24 polls: $result"; exit 1
   )
   ```

7. Tell the owners of keys without a user that the older gateway refuses the Responses ids that 1.110.2 made for them. Then delete the canary key.

The other services have no database step: recreate each one with the image of `$UPGRADE_RUN/gateway-image`, the exported `COMPOSE_PROJECT_NAME` and `--no-deps --no-build --timeout 60`.
For `copilot`, keep `state/copilot/api-key.json` as a precaution. The 1.109.1 provider reads it as a cache of the Copilot key; without the file, it makes a new key.
The real prerequisites of a 1.109.1 Copilot service are a valid `access-token` and a working token exchange with GitHub when the cached key is missing or expired.

Warning: the older gateway does not bring back the dropped accounting tables and does not serve Skills Hub rows. A dump restore loses each later write.

The 14 blocks of the backup, the upgrade and the rollback, and the stream check of [copilot.md](copilot.md), step 9, ran as written in a scratch Compose project:
its own project name, port, volume and run directories, a stub in place of ChatGPT and the Copilot API,
synthetic tokens, the 1.109.1 gateway image as the older image and the 1.110.2 gateway image as `NEW_IMAGE`.
The drill set `.env`, the gateway file (a priced canary route and the Copilot routes to the stub), `HOME`, `NEW_IMAGE`, `VIRTUAL_KEY`, `UPGRADE_RUN`,
the exported `COMPOSE_PROJECT_NAME`, and a stale `OLD_IMAGE` (the image ID of the new image).
The `name:` of its Compose files was another project, and a decoy stack with a gateway and a database ran under that default name.
`GATEWAY_PORT` in the environment published the gateway on another port than the one of `.env`, and a decoy service listened on the port of `.env`. The drill recreated `codex2`, `codex3`, `codex-router` and `copilot` with the recreate block and their names in `service=`.
The local build ran with `GATEWAY_IMAGE` set to a scratch name. The comparison of the printed blocks with this page covers these paths only. The drill ran in bash.

- Upgrade: each block printed its `ok:` line. The pin held the image ID of the 1.109.1 gateway and its published address, not the stale `OLD_IMAGE` and not the port of `.env`.
  The Copilot check gave `curl=ok HTTP 200 tool=ok end=ok` for both routes, in front of the older and of the newer gateway.
  The restore drill left no drill project and no volume. The stop of the 1.109.1 gateway read `stop=0 exit=0 log=0 marker=0`. The 1.110.2 gateway applied 8 migrations.
- Rollback: a copy of the upgrade run with another database ID stopped step 2 before it made a key.
  Then: baselines `0|0|0`, 3 priced canary requests, all six stop values 0, `match` with +3 requests and +0.00006, and `GO`.
  In step 6, the canary charge of the older gateway matched at the second poll.
- Step 6 with `general_settings.disable_spend_updates` on the older gateway (a positive cost header and no spend write) stopped after 24 polls with `DIFFERENT`.
- The decoy service on the port of `.env` got no request. The decoy default stack got no request and no `exec` call, so no dump; a control `exec` counted 1.
- The readiness loops of the doc blocks printed no curl message.
- A self-review ran each of the 14 blocks and the three blocks of [copilot.md](copilot.md), step 9, as written, with stub functions for `docker`, `curl`, `sleep`, `date` and `scripts/verify.py`.
  The stub selects the Compose project like Compose, with a decoy as the default project. 38 cases had to pass and passed, with no call to the decoy.
  309 cases each changed one input that a block reads (missing, empty, malformed, contradictory, or a project that is not exported), and each ended without its result line.
  In the three Copilot blocks, each case that fails before a request (selector, address or key) made no curl or `verify.py` call and exited with status 1.
  The cases with a bad answer, for example HTTP 500, made their requests and gave no passing result line.
  Under zsh and under dash, each block stopped at its first command and called neither `docker` nor `curl`.
- The three Copilot blocks then ran as written on the scratch project with the 1.110.2 image: both stream routes gave `curl=ok HTTP 200 tool=ok end=ok`,
  `verify.py` passed both completions, and the embeddings block printed `1536 numbers verified`. With the name unset, not exported or empty, each block stopped with status 1, and neither decoy got a request.
- Earlier scratch drills ran the drill collision, the stopped gateway, the runs in the same second, the cost 0, the HTTP 500 and the readiness limit against earlier forms of these blocks.
  They also found errors that these blocks no longer have: an invalid Compose project name, `set -e` without effect in a `&&` list,
  a database wait that the temporary Postgres server passed, a two-line image list of `config --images`, and an exit value on two lines.

Not run: these steps on the live database with real traffic, a real `parked` or `pending` row in step 5, a kill or a removed container before the stop outside the stub test,
the real ChatGPT and Copilot APIs, and the recreate of a local-build stack.
