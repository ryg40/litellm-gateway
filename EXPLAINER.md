# LiteLLM gateway: installation explainer

This file explains a first installation of this gateway, one step at a time.
It is for a developer who wants to know what each command does to the computer.
[README.md](README.md) has the same steps in a short form.

How to read this file:

- Each step gives the command, the result that you see, and the reason.
- Each step has a "Drill-down" block. Open it to see each file, container, port and host that the step touches.
- The scripts in `scripts/` are the source of truth. The documents in `docs/` have more detail.

`gateway.example.com` and `local-llm.example` are placeholders. Replace them with your values.
The base steps ran on Linux amd64 without a provider key.
Not verified: these steps on a Mac ([docs/mac.md](docs/mac.md)).

Sections: [Overview](#overview), [Prerequisites](#prerequisites), [Install](#install), [Optional components](#optional-components),
[Mac specifics](#mac-specifics), [Components](#components), [How to remove it](#how-to-remove-it), [Keep this file current](#keep-this-file-current).

## Overview

The stack is an OpenAI-compatible gateway. A client sends a request to the gateway, and the gateway sends it to a provider.
The gateway is LiteLLM 1.103.0 with the hooks and routers of this repo. Docker Compose runs it.
The base install has two services: `gateway` and `database`. The other services are optional and off by default.

```text
 host                            |  Compose project "litellm", network litellm_default
                                 |
 client --> 127.0.0.1:4321 ---------> gateway :4000 ---> providers (HTTPS)
            (the only published  |       |
             port)               |       +--> database :5432            volume litellm_postgres-data
                                 |       +--> codex1, codex2, codex3    optional, :4000 each
                                 |       +--> codex-router              optional, :4000
                                 |       +--> copilot                   optional, :4000
```

### Footprint of the base install

| Kind | What the install makes | Step |
| --- | --- | --- |
| Containers | `litellm-gateway-1`, `litellm-database-1` | 6 |
| Images | `litellm-gateway:local` (about 1.2 GB on `linux/amd64`), the LiteLLM base image, the Postgres image | 6 |
| More images | The scanner image `zricethezav/gitleaks:v8.28.0`. The tests also pull a Python image for one build stage. | 9 |
| Published port | `127.0.0.1:4321`, to port 4000 of `gateway` | 6 |
| Network | `litellm_default` | 6 |
| Volume | `litellm_postgres-data`, the database | 6 |
| Files in the checkout | `.env`, mode 0600 | 4 |
| Git configuration | The remote `upstream`, fetch only | 2 |
| Git hooks (optional) | `core.hooksPath` and three files in `.git/scan-hooks/` | 3 |

The scripts write nothing outside the checkout. The exceptions are Docker objects (the table above and the build cache) and temporary files under `$TMPDIR`.

### Footprint of the optional components

| Component | Containers that it adds | Files that it adds in the checkout |
| --- | --- | --- |
| Host gateway file | none | `.local/config/gateway.yaml` |
| Host override file | none | `.local/compose.host.yaml` |
| Anthropic credential | none | `ANTHROPIC_API_KEY` in `.env`, one route in the host gateway file |
| Codex account services | `litellm-codex1-1`, `litellm-codex2-1`, `litellm-codex3-1`, `litellm-codex-router-1` | `secrets/codex.env`, `state/codex1/`, `state/codex2/`, `state/codex3/` |
| GitHub Copilot service | `litellm-copilot-1` | `secrets/copilot.env`, `state/copilot/`, `.local/config/copilot.yaml` |
| Local models | none | Routes in the host gateway file |

## Prerequisites

| Tool | Minimum | Why |
| --- | --- | --- |
| Docker with Compose v2 | Compose 2.35.0 | `compose.yaml` starts the stack. Two login scripts use `docker compose config --no-env-resolution`. |
| Docker Buildx with BuildKit | Buildx 0.19 | `Dockerfile` needs BuildKit. `scripts/build.sh` uses `docker buildx bake`. |
| Git | 2.24 | The `pre-merge-commit` hook of `scripts/install-hooks.sh`. `scripts/scan.sh` alone needs 2.5. |
| `python3` | 3.9 | The scripts on the host use the standard library only. |
| Node.js | major version 24 | A hard requirement of the build pipelines ([docs/ci.md](docs/ci.md)). No script of this repo runs Node; the pipeline runtime does. |
| `sh`, `curl` | none | The shell scripts and the health check. |

Tested with Git 2.39.5, Compose 5.5.0 and Buildx 0.36.1. Not verified: the minimum versions themselves;
they come from the upstream release where each option first appears.
`sh scripts/check-prereqs.sh` checks each tool. It stops with exit code 1 when a hard requirement is missing, for example Node 22 instead of Node 24.
Run it before Step 1. On a Mac the runtime is Podman, not Docker ([Mac specifics](#mac-specifics)); there, run it with `--runtime podman`.
The single commands are `git --version`, `docker compose version`, `docker buildx version`, `python3 --version` and `node --version`.

The install contacts these hosts. A network with a registry proxy or TLS inspection needs the settings of [Mac specifics](#mac-specifics).
The optional Codex and Copilot logins contact more hosts; their drill-down blocks name them.

| Host | Step | Why |
| --- | --- | --- |
| Your Git server | 1 | The clone |
| `ghcr.io` | 6, 9 | The LiteLLM base image |
| Docker Hub (`docker.io`) | 6, 9 | The Postgres image, the Python image of the tests, the scanner image |
| `pypi.org` | 9 | `pytest` for the tests in the image |
| The providers that you use | each request | For example OpenRouter |

## Install

Run each command in the root of the checkout.

### Step 1: Clone the repo

```sh
git clone <url of the repo> litellm
cd litellm
```

You see the usual output of `git clone`. The directory `litellm` now holds the checkout.
A release is on the branch `portable`, with a tag `portable-v*` for each release.

<details><summary>Drill-down: what the clone touches</summary>

| Piece | Detail |
| --- | --- |
| Files created | The directory `litellm/` with the tracked files and `.git/`. |
| Files that a clone does not have | `.env`, `.local/`, `secrets/` and `state/`. Later steps make them. |
| External hosts | Your Git server. A clone from a Git bundle contacts no host: `git clone -b portable litellm-portable.bundle litellm` ([docs/remotes.md](docs/remotes.md)). |
| Compose project name | `compose.yaml` sets `name: litellm`. The name of the directory does not change it. |
| Not touched | Docker. No container, image, network or volume exists after this step. |

</details>

### Step 2: Create the remote upstream

```sh
scripts/setup-remotes.sh
scripts/setup-remotes.sh --check
```

The first command prints two lines. The second command prints `upstream: configuration is correct`.

```text
origin: <url of your Git server> (not changed)
upstream: https://github.com/BerriAI/litellm.git (fetch only, push DISABLED, tagOpt --no-tags)
```

A clone does not copy the remote configuration, so each new clone needs this step once.
The remote lets you compare a file with an upstream LiteLLM release. The gateway runs without it.

<details><summary>Drill-down: what <code>scripts/setup-remotes.sh</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Files changed | `.git/config` only: the section of the remote `upstream`. |
| Settings written | `remote.upstream.url` is `https://github.com/BerriAI/litellm.git`. `remote.upstream.pushurl` is `DISABLED`. `remote.upstream.tagOpt` is `--no-tags`. `remote.upstream.fetch` is a refspec that matches no upstream ref. The script removes `remote.upstream.mirror`. |
| Result | A push to `upstream` fails, and a plain `git fetch upstream` transfers nothing ([docs/remotes.md](docs/remotes.md)). |
| External hosts | None. The script fetches nothing. |
| Options | `--check` changes nothing: exit 0 for a correct configuration, exit 1 for a wrong one. `UPSTREAM_URL` in the environment names a mirror. |
| Not touched | The remote `origin`. The script prints its URL without credentials. |
| Later use | `scripts/fetch-upstream.sh v1.103.0` fetches one release tag from `github.com` into `refs/upstream/tags/`. |

</details>

### Step 3: Install the Git hooks (optional)

Do this step if you commit or push from this clone. Skip it if you only run the gateway.

```sh
scripts/install-hooks.sh
scripts/install-hooks.sh --check
```

You see `core.hooksPath = <path of the checkout>/.git/scan-hooks (main checkout and all worktrees)`. The second command prints a line that starts with `ok:`.
The hooks scan each commit, merge and push for secrets and for values of one host.
A hook fails closed: without Docker or the scanner image, the commit or the push stops. Pull the scanner image of step 9 first.

<details><summary>Drill-down: what <code>scripts/install-hooks.sh</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Files created | `pre-commit`, `pre-merge-commit` and `pre-push` in `.git/scan-hooks/`, mode 0755. Each is a copy of `scripts/git-hooks/dispatch`. |
| Settings written | `core.hooksPath` in `.git/config`, with the absolute path of that directory. It applies to the main checkout and to each worktree. |
| What a hook runs | The copy runs `scripts/git-hooks/<hook>` of the working tree. `pre-commit` and `pre-merge-commit` run `scripts/scan.sh staged`. `pre-push` runs `scripts/scan.sh history` for the pushed commits. |
| Containers and hosts | Each scan starts short-lived scanner containers with `--rm` and `--network none` (step 9). It contacts no external host. |
| Not touched | The tracked files and `.git/hooks/`. With `core.hooksPath` set, Git runs no hook from `.git/hooks/`. |
| Refusal and removal | The script stops if `core.hooksPath` already has another value. `scripts/install-hooks.sh --uninstall` removes the setting and the three files. |
| Skipped hooks | `git commit --no-verify` and `git push --no-verify` skip the hooks. |

Run the script again after the checkout moves to another path. [docs/secret-handling.md](docs/secret-handling.md) describes the scan.

</details>

### Step 4: Create the file .env

```sh
python3 scripts/create-env.py
```

You see one line:

```text
create-env: wrote <path of the checkout>/.env (mode 0600) with new values for LITELLM_MASTER_KEY, CODEX_MASTER_KEY, COPILOT_MASTER_KEY, UI_PASSWORD, POSTGRES_PASSWORD, LITELLM_SALT_KEY
```

Compose reads the secrets of the stack from `.env`. The script makes a new random value for each secret.
It prints key names only, never a value.

Warning: do not copy `.env.example` to `.env`. Compose refuses to start with its empty secrets.

Warning: keep `LITELLM_SALT_KEY` unchanged after the first start. The database encrypts stored credentials with it.

<details><summary>Drill-down: what <code>scripts/create-env.py</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Files read | `.env.example`. |
| Files created | `.env`, mode 0600. |
| External hosts | None. The script uses the Python standard library only. |
| Argument | An optional directory that holds `.env.example` and gets `.env`. The default is the root of the repo. |
| Keys copied unchanged | `UI_USERNAME=admin`, `OPENROUTER_API_KEY` (empty), `LOCAL_API_KEY=local-no-key`, `LOCAL_API_BASE`, and the comment lines with the optional host settings. |
| Not touched | A `.env` that exists: the script stops and does not replace it. Docker. Each other file. |

Keys with a new random value:

| Key | Use |
| --- | --- |
| `LITELLM_MASTER_KEY` | The master key of the gateway. It gives administrator access. It starts with `sk-`. |
| `LITELLM_SALT_KEY` | The key that encrypts the credentials in the database. |
| `POSTGRES_PASSWORD` | The password of the database user `litellm`. Compose puts it into `DATABASE_URL`. |
| `UI_PASSWORD` | The password of the LiteLLM admin UI at `/ui`, with the user name `UI_USERNAME`. Not verified: a login to the UI. |
| `CODEX_MASTER_KEY` | The key between the gateway and the Codex account services. |
| `COPILOT_MASTER_KEY` | The key between the gateway and the service `copilot`. |

</details>

### Step 5: Add the provider keys to .env

Open `.env` in an editor. Set the key of each provider that you use, for example `OPENROUTER_API_KEY=<your key>`.

You see no output: this step is an edit.
The default configuration `config/gateway.example.yaml` has two routes. The route `openrouter/*` reads `OPENROUTER_API_KEY`.
The route `local/example-model` reads `LOCAL_API_BASE` and `LOCAL_API_KEY`.
The gateway also starts with an empty `OPENROUTER_API_KEY`. A route works only with its key.

Warning: `.env` holds credentials. Do not add it to Git, and do not paste a key into a chat.

<details><summary>Drill-down: who reads <code>.env</code></summary>

| Piece | Detail |
| --- | --- |
| Files changed | `.env`, by you. Keep mode 0600. |
| Compose | Compose puts the values into the `${VAR}` places of the Compose files. The service `gateway` gets each key of `.env` as an environment variable (`env_file`). |
| Scripts | The scripts read a setting from the environment first, then from `.env` (`scripts/gateway_config.py`). |
| Optional host settings ([docs/host-configuration.md](docs/host-configuration.md)) | `GATEWAY_CONFIG`, `COPILOT_CONFIG`, `GATEWAY_PORT`, `LOCAL_ENDPOINTS`, `LOCAL_EMBEDDING_MODEL`, `LOCAL_EMBEDDING_BASE`, `LOCAL_EMBEDDING_PROVIDER`, `GATEWAY_IMAGE`, `BASE_IMAGE`, `COMPOSE_FILE`. |
| When a change applies | At the start of the container. After an edit: `docker compose up -d --force-recreate gateway`. |
| Not touched | The service `database` gets only `POSTGRES_PASSWORD`. The Codex account services and `copilot` do not read `.env`: they read `secrets/codex.env` and `secrets/copilot.env`. |

</details>

### Step 6: Build the image and start the stack

```sh
docker compose up -d --build
docker compose ps
```

The first command prints the build steps. Its last lines name the objects that Compose makes:

```text
 Image litellm-gateway:local Built
 Network litellm_default Created
 Volume litellm_postgres-data Created
 Container litellm-database-1 Healthy
 Container litellm-gateway-1 Started
```

`docker compose ps` shows the two services. After the start period each one shows `(healthy)`.
The line of `gateway` shows `127.0.0.1:4321->4000/tcp`. The first start takes up to 90 seconds.
One command builds the gateway image from `Dockerfile` and starts the two services of `compose.yaml`.

<details><summary>Drill-down: what <code>docker compose up -d --build</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Compose file and services | `compose.yaml`: `gateway` and `database`. |
| Files read | `compose.yaml`, `.env`, `Dockerfile`, `.dockerignore`, `image/hooks/`, `image/routers/`. |
| Image built | `litellm-gateway:local`, the target `gateway` of `Dockerfile`: the LiteLLM base image plus `image/hooks/` and `image/routers/`. |
| Images pulled | `ghcr.io/berriai/litellm@sha256:bd089afd...` (LiteLLM 1.103.0) and `postgres@sha256:a3b7f434...` (Postgres 16, from Docker Hub). Both are pinned by index digest. |
| Containers | `litellm-gateway-1` and `litellm-database-1`. The gateway runs as the user `1000:1000`, with no capability and with `no-new-privileges`. Each container has `restart: unless-stopped` and keeps at most 3 log files of 10 MB. |
| Network and volume | The network `litellm_default`: both services use only this network. The volume `litellm_postgres-data`, at `/var/lib/postgresql/data` of `database`. |
| Ports | `gateway` listens on 4000 in the container. Compose publishes it on `127.0.0.1:4321`; `GATEWAY_PORT` changes the address. `database` listens on 5432 and publishes no port. |
| Mounts | `config/gateway.example.yaml` at `/config/config.yaml`, read-only. `GATEWAY_CONFIG` names another file. |
| Environment of `gateway` | Each key of `.env`, and `DATABASE_URL`, `HOME=/tmp`, `LITELLM_LOCAL_MODEL_COST_MAP=True` and `LITELLM_TELEMETRY=False`. |
| Health checks | `gateway`: a request to `/health/liveliness` every 30 seconds, with a start period of 90 seconds. `database`: `pg_isready` every 5 seconds. `gateway` starts after `database` is healthy. |
| External hosts | `ghcr.io` and Docker Hub, for the first build. LiteLLM sends no telemetry and uses the cost map of the image. |
| Not touched | `.env`. The start makes no file in the checkout. The hooks and routers are in the image; Compose does not mount them. |

Two other ways to get the image ([docs/image.md](docs/image.md)):

- `scripts/build.sh` builds the same target with `docker buildx bake -f docker-bake.hcl`. It first runs the target `smoke`, which starts Python in the image.
  It sets the labels for the revision, the version and the time from Git. The tag is `litellm-gateway:1.103.0-p<portable version>`.
  Compose uses that image only when `GATEWAY_IMAGE` in `.env` names it. Nothing is pushed without `--push`.
- A released image: set `GATEWAY_IMAGE` in `.env` to the registry name with its index digest. Then run `docker compose up -d` without `--build`.

</details>

### Step 7: Check the health of the gateway

```sh
curl http://127.0.0.1:4321/health/liveliness
```

You see `"I'm alive!"`.
The answer shows that the gateway process runs and that the published port works. The request needs no key.

<details><summary>Drill-down: what the health check touches</summary>

| Piece | Detail |
| --- | --- |
| Request | One HTTP GET to the published address of `gateway`. Use the address of `GATEWAY_PORT` if you changed it. |
| Provider requests | None. `/health/liveliness` sends no request to a provider. The health check of the containers uses the same path. |
| Other health path | Not verified: the full `/health` endpoint of a proxy can send one request for each model. Do not use it for this check. |
| No answer | Wait for the start period. Then read `docker compose ps` and `docker compose logs gateway`. |

</details>

### Step 8: Check the authentication and the model list

```sh
python3 scripts/verify.py
```

You see `Authentication passed; <n> model entries`. With the default configuration, the route `openrouter/*` gives more than 400 entries.
The script shows that the gateway refuses a request without a valid key and accepts the master key.
To test one model, run `python3 scripts/verify.py --model <model name>`. This sends one short chat request, which uses the allowance of the provider.

<details><summary>Drill-down: what <code>scripts/verify.py</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Files read | `.env`: `LITELLM_MASTER_KEY` and `GATEWAY_PORT`. |
| Requests | `GET /v1/models` with no key and with the key `sk-invalid-test`: each must give HTTP 401 or 403. Then `GET /v1/models` with the master key: it must give HTTP 200 and a list. |
| With `--model` | One `POST /v1/chat/completions` for each model, with the text `Reply only OK.` and `max_tokens` 128. |
| Address | `--base`, or `http://` plus `GATEWAY_PORT`, or `http://127.0.0.1:4321`. |
| Early stop | The script stops before a request if `.env` holds `REPLACE_WITH_` or an empty master key. |
| External hosts | None without `--model`. With `--model`, the gateway calls the provider of the model. |
| Not touched | Each file. The script prints no key and no error text of a provider. |

</details>

### Step 9: Run the tests and the scan

```sh
scripts/build.sh test
docker pull zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854
scripts/scan.sh tree
```

The first command ends without an error when each test passes. The scan ends with exit code 0 when it has no finding.
The tests show that the hooks work with the pinned LiteLLM version. The scan shows that the tracked files hold no secret.
`scripts/scan.sh` stops with exit code 2 while the scanner image is absent, so pull the image once.

<details><summary>Drill-down: what <code>scripts/build.sh test</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Command that it runs | `docker buildx bake -f docker-bake.hcl test`, the target `test` of `Dockerfile`. |
| What runs | `python -m pytest` on `tests/` inside the gateway image, with no network in that build step. The build fails when a test fails. |
| Files read | `Dockerfile`, `docker-bake.hcl`, `image/`, `tests/`, `config/`, `scripts/` and the three Compose files. The script also reads the revision from Git. |
| Images pulled | The LiteLLM base image, and `python:3.13-slim-bookworm` by digest for the install of `pytest`. |
| External hosts | `ghcr.io`, Docker Hub and `pypi.org`. pip checks the hash of each package. `PIP_INDEX_URL` names a mirror; `PIP_CA_FILE` names a CA bundle. |
| Output | None. The target exports no image and leaves no container. It adds only build cache. |
| Not touched | The running stack, `.env`, `secrets/` and `state/`. |
| Other tests | [tests/README.md](tests/README.md) has the other test commands, for example `sh tests/test_compose.sh`. |

</details>

<details><summary>Drill-down: what <code>scripts/scan.sh tree</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Scanner | gitleaks v8.28.0, in an image that `scripts/scan.sh` pins by digest. `GITLEAKS_IMAGE` names a mirror. |
| What it reads | The tracked files, the staged content and the names of the tracked files. |
| Rules | `.gitleaks.toml`, `scripts/host-values.deny`, `scripts/host-values.regex`, and `.local/host-values.deny` if that file exists. |
| Containers and temporary files | Short-lived containers with the name prefix `scan-gitleaks-`, with `--rm`, `--network none` and read-only mounts. One directory `scan.XXXXXX` under `$TMPDIR` (default `/tmp`), which the script removes at the end. |
| Result | Exit code 0: clean. Exit code 1: a finding. Exit code 2: a usage or tool error. The output never shows a secret value. |
| Not touched | `.env`, `secrets/` and `state/`. Git does not track them, so the mode `tree` does not read them. |

A secret always fails the scan. A value of one host fails it on the branch `portable` and prints a warning on other branches.

</details>

## Optional components

Each component below is off by default. Do steps 1 to 8 first.
Most components write routes into the host gateway file. Thus do steps 1 and 2 of the next section first.

### Host gateway file and host override file

`config/gateway.example.yaml` is a tracked example. Your own routes go into `.local/config/gateway.yaml`, the host gateway file.

1. Run `mkdir -p .local/config`, then `cp config/gateway.example.yaml .local/config/gateway.yaml`. Git ignores `.local/`, so an update of the repo does not change your file.
2. Add the line `GATEWAY_CONFIG=./.local/config/gateway.yaml` to `.env`. Compose then mounts your file in place of the example.
3. Edit the file, or let a script of the next sections fill it. Keep the JSON syntax: the scripts read the file as JSON.
4. Run `docker compose up -d --force-recreate gateway`. A mounted file needs a recreate, not a restart.

The host override file `.local/compose.host.yaml` is necessary only for a public address, for example `https://gateway.example.com`.
It gives the gateway a fixed container name and an external network for a reverse proxy.
[docs/host-configuration.md](docs/host-configuration.md) has the file and the `COMPOSE_FILE` line for `.env`.

<details><summary>Drill-down: the host files and the scripts that write them</summary>

| Piece | Detail |
| --- | --- |
| Files created | `.local/config/gateway.yaml` and, for a reverse proxy, `.local/compose.host.yaml`. |
| `.env` keys | `GATEWAY_CONFIG` names the gateway file. `COMPOSE_FILE` lists the Compose files, with the host override file last. |
| Scripts that write the gateway file | `scripts/enable-anthropic.py`, `scripts/enable-codex.py` and `scripts/sync-local-models.py`, through `scripts/gateway_config.py`. |
| How they write | They write a temporary file with mode 0644 and then replace the target. Without a host file they start from the example. |
| What they do not write | The line `GATEWAY_CONFIG` in `.env`. They print the line when `.env` does not have it. Add it yourself (step 2). |
| Guard | A script stops if `GATEWAY_CONFIG` points into `config/`. Thus no script changes a tracked file. |
| Host override file | It adds `container_name: litellm` and the external network `proxy`, which must exist. `docs/examples/Caddyfile.fragment` is an example route for Caddy. |

Warning: keep `name: litellm` in `compose.yaml` and do not use `-p`. Another project name makes a new, empty database volume.

</details>

### Anthropic credential

```sh
python3 scripts/enable-anthropic.py
docker compose up -d --force-recreate gateway
python3 scripts/verify.py --model anthropic/MODEL_ID
```

The first command shows the hidden prompt `Separate Anthropic API key or OAuth token:`. Paste the credential and press Enter.
It then prints the path of the host gateway file and the recreate command. Replace `MODEL_ID` with an Anthropic model ID.
The route `anthropic/*` uses its own credential. The hidden prompt keeps the credential out of the shell history.

Warning: do not paste the credential into a shell argument or a chat.

<details><summary>Drill-down: what <code>scripts/enable-anthropic.py</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Files read | The host gateway file, or `config/gateway.example.yaml` if no host file exists. |
| Files changed | `.env`: the script removes each `ANTHROPIC_API_KEY` line, adds the new one and sets mode 0600. The host gateway file: the script replaces the entry `anthropic/*`, with `api_key` set to `os.environ/ANTHROPIC_API_KEY`. |
| Check of the input | The credential must start with `sk-ant-` and have no white space. |
| Order | The script finds the host file first. A wrong `GATEWAY_CONFIG` thus stops it before `.env` changes. |
| External hosts | None. The gateway contacts Anthropic only at a request. |
| Not touched | The credentials of other tools, for example a Claude Code login. The script reads none and stores no refresh token. |
| OAuth token | It does not refresh here: replace it before it expires. |

Not verified: inference with a subscription token, and the permission of the provider. [docs/anthropic.md](docs/anthropic.md) has the details.

</details>

### Codex account services

`compose.codex.yaml` adds one LiteLLM process for each ChatGPT (Codex) account: `codex1`, `codex2` and `codex3`.
The script for the routes needs two logins or more.

1. Make the key file of the services. The services read `CODEX_MASTER_KEY` from it, not from `.env`.

   ```sh
   mkdir -p secrets
   grep '^CODEX_MASTER_KEY=' .env > secrets/codex.env
   chmod 600 secrets/codex.env
   ```

2. Make the token directories for the user of the containers. Omit `chown` if your user id is 1000.

   ```sh
   mkdir -p state/codex1 state/codex2 state/codex3
   sudo chown 1000:1000 state/codex1 state/codex2 state/codex3
   ```

   The directories must be writable for the user `1000:1000`. On Linux, Docker makes a missing directory with the owner `root`.
   The login then cannot write the token. Not verified: the directory owner on a Mac.
3. Run `sh scripts/login-codex.sh 1`, then `sh scripts/login-codex.sh 2`. Each command shows a URL and a device code.
   Open the URL in a browser with the ChatGPT account of that number and enter the code. Use a separate browser profile for each account.
   The last line is `Login complete. Credentials stay in this account's token directory.`
   Not verified: the exact text of the URL and of the device code, and how long a device code stays valid.
4. Run `python3 scripts/enable-codex.py`. It adds the Codex routes to the host gateway file and stops when a token file is missing.
5. Run `docker compose -f compose.yaml -f compose.codex.yaml up -d --force-recreate`. It starts the account services and recreates the gateway.
6. Run `python3 scripts/verify.py --model codex1/MODEL_ID --model codex2/MODEL_ID`. `MODEL_ID` is a model name of `config/gateway.codex.example.yaml`.

With two logins, stop the third service: `docker compose -f compose.yaml -f compose.codex.yaml stop codex3`.
Without a login it stays unhealthy and prints a device code again and again.
Add `compose.codex.yaml` to `COMPOSE_FILE` in `.env` to include the services in each `docker compose` command.

Warning: do not copy the refresh tokens of another client, for example a Codex CLI login, into `state/`. Concurrent refresh can break the original login.

<details><summary>Drill-down: what the Codex scripts and services touch</summary>

| Piece | Detail |
| --- | --- |
| Compose file and services | `compose.codex.yaml`: `codex1`, `codex2`, `codex3` and `codex-router`. They use the gateway image and do not build it. Each one listens on 4000 on the project network and publishes no port. |
| Files that you create | `secrets/codex.env`, mode 0600, with the one key `CODEX_MASTER_KEY`. |
| Login container | `scripts/login-codex.sh N` runs `docker compose run --rm --no-deps` for the service `codexN`. It mounts `scripts/login-codex.py` read-only. Docker removes the container at the end. |
| Token file | `state/codexN/auth.json`, mode 0600. Only the service `codexN` refreshes it. |
| External hosts | `auth.openai.com` for the login, `chatgpt.com` for the requests and for the usage check of `codex-router`. |
| `scripts/enable-codex.py` | It reads `state/codexN/auth.json` and `config/gateway.codex.example.yaml`. It writes the host gateway file only. `--accounts 2` or `--accounts 3` sets the number of accounts. |
| Routes written | `codex1/`, `codex2/`, `codex3/` and `codex-auto/` entries, and `router_settings.fallbacks` and `max_fallbacks`. The script keeps the other entries. `codexN/<model>` goes to `http://codexN:4000/v1` with `CODEX_MASTER_KEY`. `codex-auto/<model>` goes to `codex2`, then to `codex3`, then to `codex1`. |
| `codex-router` | An optional quota-aware router. It mounts `state/codex2` and `state/codex3` read-only. The default routes do not use it. |
| Not touched | `.env`. The logins of other clients. The token directory of another account. |

A later login for an account that exists: `sh scripts/reauth-codex.sh <1|2|3|all>`. `--check` prints the state and changes nothing.
The script stops one service, moves `auth.json` to a backup with mode 0600, runs the login and starts the service again.
`scripts/login-codex-account.sh` is for the prototype of [docs/codex-accounts.md](docs/codex-accounts.md). The account services do not use it.
[docs/codex-services.md](docs/codex-services.md) covers one account, the router and the limits.

</details>

### GitHub Copilot service

`compose.copilot.yaml` adds the service `copilot` for the models of one GitHub Copilot account.
Do the steps in this order: the login comes before the first start of the service.

1. Make the key file of the service. The service reads `COPILOT_MASTER_KEY` from it, not from `.env`.

   ```sh
   mkdir -p secrets
   grep '^COPILOT_MASTER_KEY=' .env > secrets/copilot.env
   chmod 600 secrets/copilot.env
   ```

2. Run `sh scripts/login-copilot.sh` as root, or as a user with the user id 1000. Another user gets exit code 2: only root can give the directory to the user of the service.
   The container prints a URL and a code. Open the URL in a browser with the GitHub account and enter the newest code.
   Each code is valid for one minute. The login ends with `Login complete. The token files stay in the token directory.`
3. Run `sh scripts/login-copilot.sh models --endpoints`. It prints the model ids of the account and the policy state of each id.
4. Copy `config/copilot.yaml` to `.local/config/copilot.yaml` and keep only the ids of step 3. The tracked file holds placeholder ids.
   Then add `COPILOT_CONFIG=./.local/config/copilot.yaml` to `.env`.
5. Add `compose.copilot.yaml` to `COMPOSE_FILE` in `.env`: `COMPOSE_FILE=compose.yaml:compose.copilot.yaml`. Keep a host override file last in the list.
6. Run `docker compose up -d copilot`, then `docker compose ps copilot`. The status must be `healthy`.
   `docker compose logs copilot | grep -c "ignoring and continuing"` must print 0: each counted line is an entry that the service dropped.
7. Copy the `model_list` entries of `config/gateway.copilot.example.yaml` into the host gateway file. Keep the aliases that the service has.
8. Run `docker compose up -d --force-recreate gateway`. The gateway reads `COPILOT_MASTER_KEY` and the new routes at a recreate only.
9. Run `python3 scripts/verify.py --model copilot/sonnet --model copilot/codex`. Not verified: a request without streaming, which this script sends.

Warning: do not start the service before the login. Docker then makes `state/copilot/` with the owner `root`, and the service cannot write the key file.

Warning: the file `access-token` gives access to the Copilot allowance of the account. Do not copy it.

<details><summary>Drill-down: what the Copilot script and service touch</summary>

| Piece | Detail |
| --- | --- |
| Compose file and service | `compose.copilot.yaml`: `copilot`, on the gateway image. It listens on 4000 on the project network and publishes no port. |
| Files that you create | `secrets/copilot.env`, mode 0600, and `.local/config/copilot.yaml`. |
| `.env` keys | `COPILOT_MASTER_KEY` for the gateway, `COPILOT_CONFIG` and `COMPOSE_FILE`. No script writes the last two. |
| Login container | `docker run --rm --name litellm-copilot-login`, as the user `1000:1000`. It gets no env file, no master key and no project network, and the hooks of the image are off. It uses the image of the service `gateway` in `compose.yaml`, or `LITELLM_IMAGE`: build the image first (step 6 of the install). |
| Token directory | `state/copilot/`, owner `1000:1000`, mode 0700. The script makes it. `COPILOT_TOKEN_DIR` and `COPILOT_USER` change the path and the user. |
| Token files | `access-token`, the GitHub OAuth token, and `api-key.json`, the short-lived Copilot key. The login makes both with mode 0600. The service writes a new `api-key.json` when the old one expires. |
| External hosts | `github.com` for the device login, `api.github.com` for the key exchange, `api.githubcopilot.com` for the models and the requests. |
| Service settings | `GITHUB_COPILOT_TOKEN_DIR=/state/copilot`. `GITHUB_COPILOT_DEVICE_CODE_URL` is a dead local address, so the service never starts a login. Do not remove it. |
| Gateway routes | `copilot/<alias>` goes to `http://copilot:4000` with `COPILOT_MASTER_KEY`. No script writes these routes. |
| Options | `--dry-run` prints the `docker` command and changes nothing. `models` lists the model ids; `--endpoints` adds the API paths and the policy state. |
| Not touched | The gateway gets no token directory. The login does not read `.env`. |

After a later login or a change of the service configuration: `docker compose up -d --force-recreate copilot`.
[docs/copilot.md](docs/copilot.md) has the streaming test, the alias contract, the failure table and the steps to end the login.

</details>

### Local models

```sh
python3 scripts/sync-local-models.py --endpoint vllm=http://local-llm.example:8001/v1
docker compose up -d --force-recreate gateway
```

The first command prints `vllm: <n> models` and the path of the host gateway file.
It reads the model list of an OpenAI-compatible endpoint and writes one route for each model: `vllm/<model id>`.
Use an address that the host and the container `gateway` can both reach.

<details><summary>Drill-down: what <code>scripts/sync-local-models.py</code> touches</summary>

| Piece | Detail |
| --- | --- |
| Endpoints | `--endpoint NAME=URL`, more than one time if necessary. Without the option, `LOCAL_ENDPOINTS` from the environment or from `.env`. |
| External hosts | One `GET <URL>/models` for each endpoint, with a timeout of 15 seconds. The script sends no key. |
| Files changed | The host gateway file only. The script removes each route that starts with `NAME/` and adds one route for each model: `NAME/<model id>`, with `model: openai/<model id>`, the URL as `api_base`, and `api_key` set to `os.environ/LOCAL_API_KEY`. |
| Embedding | `--embedding-model` and `--embedding-base`, or `LOCAL_EMBEDDING_MODEL` and `LOCAL_EMBEDDING_BASE`, add one dedicated embedding route. Set both or neither. |
| Safe stop | The script writes nothing if an endpoint does not answer or gives no model. With no endpoint it stops with exit code 1. |
| Not touched | The routes of other providers, `.env` and each container. |

</details>

## Mac specifics

Not verified: these steps on a Mac. [docs/mac.md](docs/mac.md) has the full steps and the sources.
The steps are the same as on Linux. The table gives the differences.

| Step | Difference on a Mac |
| --- | --- |
| Prerequisites | The Mac has Apple silicon (`arm64`). You need a container runtime with a Linux `arm64` VM. Podman is the recommended replacement for Docker Desktop. Colima is an alternative; test it before you use it. Each Homebrew formula that [docs/mac.md](docs/mac.md) names has an `arm64` bottle. |
| Prerequisites | With Homebrew: `brew install podman docker docker-compose node@24`. Add `cliPluginsExtraDirs` to `~/.docker/config.json`, so that the Docker CLI finds the Compose plugin. Put `$(brew --prefix node@24)/bin` first on `PATH`, then run `sh scripts/check-prereqs.sh --runtime podman`. |
| Prerequisites | Start the VM with `podman machine init --cpus 4 --memory 8192 --disk-size 60` and `podman machine start`. Set `DOCKER_HOST` to the Podman socket, so that `docker compose` and the scripts talk to Podman. |
| 1 | Keep the checkout under `$HOME`. The Podman VM shares `/Users`, `/private` and `/var/folders` by default. Colima shares only `$HOME`. |
| 6 | Build with `podman build --target gateway -t litellm-gateway:local .`, then run `docker compose up -d` without `--build`. `scripts/build.sh` and `docker compose build` need `docker buildx`, which Podman does not have. The build makes a `linux/arm64` image. Not verified: the `arm64` gateway image at run time. |
| 9 | Run the tests with `podman run` as [tests/README.md](tests/README.md) says, instead of `scripts/build.sh test`. `scripts/scan.sh` runs through `docker run` on `DOCKER_HOST`. With Colima, the macOS `$TMPDIR` is under `/var/folders`, which the Colima VM does not see; set `TMPDIR` to a directory under `$HOME` first. |
| Codex and Copilot | The services write to `state/` as the user `1000:1000`. Not verified: the directory owner on a Mac. |

A work network can need more settings. Not verified: a run with a registry proxy or with TLS inspection.

| Case | Setting |
| --- | --- |
| Registry proxy for the images | `BASE_IMAGE` in `.env`, `PYTHON_IMAGE` and `GITLEAKS_IMAGE` in the environment. Keep each digest. |
| Package proxy for the tests | `PIP_INDEX_URL` in the environment of `scripts/build.sh`. |
| TLS inspection: image pulls | With Podman: the CA in the trust store of the VM, through `podman machine ssh`. With Colima: the CA in `~/.docker/certs.d/`, then a restart of Colima. |
| TLS inspection: the tests | `PIP_CA_FILE` with a CA bundle, for example `PIP_CA_FILE=.local/ca-bundle.pem scripts/build.sh test`. |
| TLS inspection: the gateway at run time | `SSL_CERT_FILE` and a mount of the CA bundle, in an override file under `.local/`. |

## Components

### Services

| Service | Compose file | What it is | Listens on | Published to the host |
| --- | --- | --- | --- | --- |
| `gateway` | `compose.yaml` | The LiteLLM proxy that clients call. It holds the routes. | 4000 | `127.0.0.1:4321` |
| `database` | `compose.yaml` | Postgres. It holds virtual keys, spend and the settings from the UI. | 5432 | no |
| `codex1`, `codex2`, `codex3` | `compose.codex.yaml` | One LiteLLM proxy for each ChatGPT account, with `config/codex.yaml`. | 4000 | no |
| `codex-router` | `compose.codex.yaml` | `image/routers/quota_router.py`, an optional router for the Codex accounts. | 4000 | no |
| `copilot` | `compose.copilot.yaml` | A LiteLLM proxy with the `github_copilot/` provider, with `config/copilot.yaml`. | 4000 | no |

All services except `database` use one image: `GATEWAY_IMAGE`, or `litellm-gateway:local`.
They run as the user `1000:1000` with no capability. They reach each other by service name on the project network.

### Image layers

| Layer | Content |
| --- | --- |
| Base image | The upstream LiteLLM 1.103.0 image, pinned by index digest in `Dockerfile` and `docker-bake.hcl`. `BASE_IMAGE` names a mirror. |
| `image/hooks/` | Copied to `/opt/litellm-gateway/image/hooks/`. `PYTHONPATH` names this directory. |
| `image/routers/` | Copied to `/opt/litellm-gateway/image/routers/`: `quota_router.py` and `decision_router.py`. |

Python imports `sitecustomize.py` from `PYTHONPATH` at each start, and that file installs the hooks.
The add-on files have the owner `root` and are read-only. The image holds no route, no `.env`, no `secrets/` and no `state/`.

Warning: do not set `PYTHONPATH` in a Compose file. Another value turns the hooks off without an error.

| Hook in `image/hooks/` | What it does |
| --- | --- |
| `sitecustomize.py` | It stops an untested LiteLLM version, registers some model lookup keys and installs the hooks below. |
| `litellm_versions.py` | It holds `TESTED_VERSIONS`, the list of the tested LiteLLM versions for all hooks. |
| `responses_tool_finish.py` | It preserves completion-only function arguments and gives a completed streamed chat answer with tool calls the finish reason `tool_calls`. |
| `chatgpt_session_id.py` | It gives each request for a `chatgpt` deployment a stable session id, for the prompt cache. |
| `chatgpt_auth_file.py` | It permits one ChatGPT account for each deployment. It is a prototype and off by default. |

The Responses-to-Chat bridge can drop function arguments that arrive only in completion events, observed with parallel Codex tool calls.
`responses_tool_finish.py` emits those arguments once, from `response.function_call_arguments.done` or `response.output_item.done`.
It keeps the call id, function name and sequential chat tool index. Calls that already delivered arguments stay unchanged.
State belongs to each iterator and each call. Incomplete, failed and cancelled events stay unchanged; completion events never end the stream early.
The hook is tested with LiteLLM 1.101.0 and 1.103.0. Remove it when the pinned upstream passes the regression tests without it.
`tests/test_responses_tool_finish.py` checks synthetic event sequences; [tests/README.md](tests/README.md) gives the pinned-image test commands.
Not verified: live parallel tool calls with this argument recovery hook.

`decision_router.py` is a prototype for the virtual model `auto` ([docs/decision-router.md](docs/decision-router.md)). The default configuration does not load it.

### Configuration files

| File in `config/` | Use |
| --- | --- |
| `gateway.example.yaml` | The default configuration of `gateway`: the routes `openrouter/*` and `local/example-model`. |
| `gateway.codex.example.yaml` | The Codex routes that `scripts/enable-codex.py` copies into the host gateway file. |
| `gateway.copilot.example.yaml` | The Copilot routes that you copy into the host gateway file. |
| `codex.yaml` | The configuration of `codex1`, `codex2` and `codex3`. |
| `copilot.yaml` | The configuration of `copilot`, with placeholder model ids. |
| `model-info.example.yaml` | A template for a model that the cost map of the image lacks ([docs/image.md](docs/image.md)). |
| `gateway.codex-accounts.example.yaml`, `decision-routes.example.yaml` | Examples for the two prototypes. |

### Directories of one host

Git ignores each of these paths. Back them up separately ([docs/operations.md](docs/operations.md)).

| Path | Content | Writer |
| --- | --- | --- |
| `.env` | The keys, the passwords and the host settings. Mode 0600. | `scripts/create-env.py`, you, `scripts/enable-anthropic.py` |
| `.local/` | The host gateway file, the host override file, a host copy of `copilot.yaml`, an optional deny list for the scan. | You and the scripts that write routes |
| `secrets/` | `codex.env` and `copilot.env`: the key of each optional service. Mode 0600. | You |
| `state/` | The login tokens: `state/codexN/auth.json` and `state/copilot/`. | The login containers and the services |

## How to remove it

Do the steps in this order. Stop at the step that fits your purpose.

1. Run `docker compose down`. Compose removes the containers and the network and keeps the database volume.
   Without the optional files in `COMPOSE_FILE`, name them: `docker compose -f compose.yaml -f compose.codex.yaml -f compose.copilot.yaml down`.

Warning: step 2 deletes the database: the virtual keys, the spend data and the settings from the UI.

2. Run `docker compose down -v`. Compose also removes the volume `litellm_postgres-data`.
3. Run `docker image rm litellm-gateway:local`. `docker image ls` shows the names of the other images of the table "Footprint of the base install".
   Delete the LiteLLM base image, the Postgres image and the scanner image in the same way if no other project uses them.
   `docker buildx prune` deletes the unused build cache of all projects, not only of this one.
4. Run `scripts/install-hooks.sh --uninstall` and `git remote remove upstream`, if the clone stays. This removes the Git hooks and the remote.

Warning: step 5 deletes the login tokens. Each account then needs a new login.

5. Run `rm -rf state` to delete the login tokens. The user `1000:1000` owns the token files, so the command can need `sudo`.
   For Copilot, also revoke the authorization of "GitHub Copilot Plugin" in the settings of the GitHub account.

Warning: step 6 deletes the keys of `.env` and your host configuration. Without a backup they are gone.

6. Run `rm -rf .env secrets .local` to delete the host files.

After step 6 the checkout has only tracked files. Delete the directory `litellm` to remove the last part.

## Keep this file current

Check these parts for each release of `portable`. The right column names the source of truth.

| Part of this file | Source |
| --- | --- |
| LiteLLM version, the digest of the base image and the size of the gateway image | `Dockerfile`, `docker-bake.hcl`, `docker image ls litellm-gateway:local` |
| Postgres digest and major version | `compose.yaml` |
| Scanner version and digest | `scripts/scan.sh` |
| Minimum tool versions | [README.md](README.md), section "Quick start" |
| Script names, options and the output lines that this file quotes | `scripts/` |
| Keys of `.env` and the list of generated keys | `.env.example`, `GENERATED` in `scripts/create-env.py` |
| Services, ports, mounts, network and volume | `compose.yaml`, `compose.codex.yaml`, `compose.copilot.yaml` |
| Lists of the hooks, the routers and the configuration files | `image/hooks/`, `image/routers/`, `config/`, [docs/image.md](docs/image.md) |
| External hosts of the logins | The provider source of the pinned LiteLLM image |
| Each line that starts with "Not verified" | Remove it when a run verifies the statement, for example a run on a Mac. |

After an edit, run `scripts/scan.sh --level fail tree` and check that each relative link points to a file that exists.
Keep the file generic: placeholders only, no host name, no account name and no private path.
