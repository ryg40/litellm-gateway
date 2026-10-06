# Codex account services

This page describes the supported design for up to three ChatGPT (Codex) accounts: one LiteLLM process for each account.
`compose.codex.yaml` defines the services. It is off by default.

## Two designs

| Design | State | Document |
| --- | --- | --- |
| One process for each account: `codex1`, `codex2`, `codex3`, optional `codex-router` | Supported design | This page |
| One gateway process with the key `chatgpt_auth_file` for each deployment | Prototype. The hook is off by default. | [codex-accounts.md](codex-accounts.md) |

Upstream LiteLLM supports one ChatGPT account for each process. The services give each account its own process.
The prototype hook would let one process use more than one account. Use this page unless you test the prototype.

## Services

| Service | Use |
| --- | --- |
| `codex1` | LiteLLM proxy for account 1. Token directory `state/codex1/`. |
| `codex2` | LiteLLM proxy for account 2. Token directory `state/codex2/`. |
| `codex3` | LiteLLM proxy for account 3. Token directory `state/codex3/`. |
| `codex-router` | Optional quota-aware router (`image/routers/quota_router.py`). See "The router `codex-router`". |

The services use the gateway image and `config/codex.yaml`. They use only the project network and publish no host port.
Each service refreshes only the tokens of its own account.

## Set up the accounts

Warning: do not copy the refresh tokens of another client (for example a Codex CLI login) into these directories. Concurrent refresh can break the original login.

LiteLLM uses its native `chatgpt/` provider and the device authorization flow.
If the account requires it, enable device-code authorization in the security settings of the account.

1. Create `secrets/codex.env` with the same `CODEX_MASTER_KEY` as in `.env`. The three services read it; the gateway uses the key for the Codex routes.

   ```sh
   mkdir -p secrets
   grep '^CODEX_MASTER_KEY=' .env > secrets/codex.env
   chmod 600 secrets/codex.env
   ```

2. Start the login of the first account. Open the displayed URL with the first ChatGPT account and enter the code.

   ```sh
   sh scripts/login-codex.sh 1
   ```

3. Start the login of the second account. Use the second ChatGPT account, preferably in a separate browser profile.

   ```sh
   sh scripts/login-codex.sh 2
   ```

   Start the login of the third account in the same way, with `3`.
   The tokens go to `state/codex1/`, `state/codex2/` and `state/codex3/`. The directories must be writable for the user `1000:1000`.
   Not verified: the directory owner on a new clone and on a Mac.

4. Add the Codex routes to the host gateway file. The script stops when a token file of an account is missing.

   ```sh
   python3 scripts/enable-codex.py
   ```

   Without `--accounts`, the script counts the complete logins from `codex1` up. With two logins it adds no `codex3/` route and no `codex3/` fallback. `--accounts 2` or `--accounts 3` sets the number.

5. Start the account services and recreate the gateway.

   ```sh
   docker compose -f compose.yaml -f compose.codex.yaml up -d --force-recreate
   ```

6. Test an entitled model on each account. Replace `MODEL_ID` with the upstream model ID.

   ```sh
   python3 scripts/verify.py --model codex1/MODEL_ID --model codex2/MODEL_ID --model codex3/MODEL_ID
   ```

To include the services in each `docker compose` command, add `compose.codex.yaml` to `COMPOSE_FILE` ([host-configuration.md](host-configuration.md)).

### One, two or three accounts

`compose.codex.yaml` always defines `codex1`, `codex2`, `codex3` and `codex-router`. The number of logins decides what you start and which routes the gateway gets.

| Accounts | Routes | Services |
| --- | --- | --- |
| Three | `python3 scripts/enable-codex.py` (or `--accounts 3`): `codex1/`, `codex2/`, `codex3/` and `codex-auto/` with the fallbacks `codex3`, then `codex1`. | Start all services (step 5). |
| Two | `python3 scripts/enable-codex.py` (or `--accounts 2`): no `codex3/` route, and `codex-auto/` falls back to `codex1` only. | Start all services, then `docker compose -f compose.yaml -f compose.codex.yaml stop codex3`. Without a login, `codex3` stays unhealthy and prints a device code again and again. |
| One | The script needs at least two logins and stops. Copy the `codex1/` entries of `config/gateway.codex.example.yaml` into the host gateway file by hand. | `docker compose -f compose.yaml -f compose.codex.yaml up -d codex1`, then `docker compose up -d --force-recreate gateway`. Do not start `codex-router`: it waits for a healthy `codex2`. |

To add an account later, run its login, run `scripts/enable-codex.py` again, and recreate the services (step 5). The script replaces the `codex1/`, `codex2/`, `codex3/` and `codex-auto/` entries of the host gateway file and keeps the other entries.
Not verified: a stack with one account.

## Reauthorize an account

`scripts/reauth-codex.sh` makes a new device-code login for one account service. Run it in the checkout that runs the services, as root or as the owner of `state/`.

```sh
sh scripts/reauth-codex.sh 3            # one account
sh scripts/reauth-codex.sh all          # codex1, codex2, codex3, one at a time
sh scripts/reauth-codex.sh all --check  # change nothing; print the state
```

Warning: do not copy the refresh tokens of another client (for example a Codex CLI login) into the token directories. Concurrent refresh can break the original login. The script makes a new login; it never imports a token.

What the script does for each account:

1. It prints the plan and asks for confirmation. `--yes` skips the question.
2. It stops the service `codexN`. A service refreshes its own token file while it runs, so a login beside a running service can race with that refresh.
3. It moves `state/codexN/auth.json` to `auth.json.before-reauth-<UTC time>` in the same directory, with mode 0600. The time has the form `20260101T000000Z`. With no token file, the login always asks for a device code.
   Not verified: whether the login asks for a new device code while a valid token file is present.
4. It runs `sh scripts/login-codex.sh N`. The login prints a URL and a device code. Open the URL with the ChatGPT account of this service, preferably in a separate browser profile, and enter the code.
5. It checks the new file: mode 0600, the owner of the old file, and non-empty `access_token` and `account_id`. It prints no value.
6. It starts the service and waits until the container is healthy. `REAUTH_HEALTH_TIMEOUT` sets the limit in seconds (default 120). `REAUTH_POLL_SECONDS` sets the time between two checks (default 5, a positive integer).
7. It removes old backups: the backup of this run and the 2 newest other backups stay. Only names of the exact form of step 3 count; a file with another name stays.
8. It reads the log of `codex-router` since the login and prints one result line, for example `codex3: OK: new login, service healthy after 35 s; no skip line in the router log since the login. Not a proof: ...`.

The script uses `scripts/login-codex.sh`, not `scripts/login-codex-account.sh`. `login-codex.sh` runs the login in a container of the service itself, so the token goes to the mount `./state/codexN:/tokens` with the user and the image of the service. `login-codex-account.sh` writes to `state/codex-accounts/<account>/` for the prototype of [codex-accounts.md](codex-accounts.md); no account service mounts that directory.

What a failure or a signal (Ctrl-C, a closed terminal, `kill`) does depends on the step:

| When | Token file | Service |
| --- | --- | --- |
| The stop fails | Not changed | Started again |
| After the move, before the new file passes step 5 | The old file goes back to `auth.json`. The script ends a login that still runs. | Started again. A service that was stopped before the run (`exited`, `created`, `dead`, no container) stays stopped. |
| The old file cannot go back | The backup stays in the directory. The script prints the `mv` command. | Stays stopped |
| After the new file passed step 5 (start fails, not healthy in time, signal) | The new file stays. The backup stays in the directory. | The script sends the start one more time and prints the command when it fails. |

In each of these cases the exit status is not 0. The script never puts the old file over a complete new login.

Limits:

- The service is away from the stop until it is healthy again: the time of the login in the browser, and then the start of the container. During this time a `codexN/<model>` route fails. The router uses the next account; on a request it logs `codexN skipped source=no-login`, at most once a minute.
- `all` works on one account at a time, in the order 1, 2, 3, and stops at the first failure. It never stops two account services at the same time.
- The router check is not a proof. The router logs a skip only when a request arrives. It never checks the last account of `CODEX_ACCOUNT_ORDER` (`codex1` by default), so it logs no skip for that account. No skip line means only that the log has no such line.
- A skip line of the account after the login gives a `WARNING` line and exit status 1. The new file stays.
- To prove a login, send a request to the account: `python3 scripts/verify.py --model codexN/MODEL_ID`.

`--check` prints one line for each account and exits with 0 only when each login is complete:

```
codex1: service=running health=healthy file=yes mode=600 login=complete expires_in=212.4h router_skip_line_2m=none
```

`login` is `complete`, `incomplete` (a key is empty or missing), `invalid` (not JSON), `unreadable` or `missing`. `expires_in` is the time to `expires_at` of the file, or `-` when the file has no such key. `router_skip_line_2m` is `yes` when the router log of the last 2 minutes has a skip line of the account, `none` when it has no such line, and `unknown` when the log was not available. `none` is not a proof (see "Limits"); the last line of the output says so.

`DOCKER` names the docker command of `reauth-codex.sh`. `scripts/login-codex.sh` always uses `docker` from `PATH`.
`tests/test_reauth_codex.sh` tests the script with a fake docker command.

Not verified: a run of `scripts/reauth-codex.sh` on a live account, and that a signal ends the login container. The script ends the login processes. Then it removes with `docker rm -f` each one-off container of the service in this checkout that did not exist before the login. Limit: it also removes another `docker compose run` of the same service that is started during the login. When stdin is a terminal, the script saves the terminal settings before the login and restores them after a signal; this is not verified on a terminal.
Not verified: the exact text of the URL and of the device code, and how long a device code stays valid.
Not verified: the time until the container is healthy after the start. The health check has a 30 s interval and a 90 s start period.
Not verified: `expires_at` in the token file of a live account, and its unit. The script accepts seconds, milliseconds and an ISO time.
Not verified: whether the old login stays valid at ChatGPT after a new login, so a restored old file can need a new login.

## Routes

`scripts/enable-codex.py` copies the routes of `config/gateway.codex.example.yaml`:

| Route | Target |
| --- | --- |
| `codex1/<model>` | Account 1 |
| `codex2/<model>` | Account 2 |
| `codex3/<model>` | Account 3 |
| `codex-auto/<model>` | Account 2 first, then account 3, account 1 last |

Each route exists for `luna`, `sol` and `astra`, and for the full upstream IDs `gpt-6-luna`, `gpt-6-sol`, `gpt-6.1-sol` and `gpt-6-astra`.
The short name `sol` targets `gpt-6.1-sol`. A full ID is a pinned name and does not move.
`gpt-6-terra` is not configured: ChatGPT Codex rejected it for subscription accounts. Check model availability for each account before you add a route.

`codex-auto/*` uses the native fallback of LiteLLM. The route goes to `codex2`, and `router_settings.fallbacks` sends a failed request to `codex3`, then to `codex1` (`max_fallbacks: 2`).
`scripts/enable-codex.py` sets `router_settings.max_fallbacks` of the host gateway file to the number of fallback accounts (1 for two accounts, 2 for three) when the file has no value or a lower one. It keeps a higher value.
Each `codex-auto` entry has `cooldown_time: 60` and `RateLimitErrorAllowedFails: 0`.
[codex-accounts.md](codex-accounts.md) compares this fallback with `codex-router`.

Warning: a fallback cannot give allowance after all accounts reach their limits.

### Stable session id

ChatGPT Codex selects a prompt-cache server by the `session_id` header of a request. A conversation keeps its cache only when each of its requests has the same id.
The hook `image/hooks/chatgpt_session_id.py` sets that id in each proxy that has a `chatgpt` deployment. It applies to `codex-auto/*`, to the explicit routes `codex1/*`, `codex2/*` and `codex3/*`, and to the prototype of [codex-accounts.md](codex-accounts.md). It needs no setting.

- Without a session id from the client, the id is `prefix-<hash>`. The hash covers the model, the system and developer messages (or `instructions`) and the first user message. A conversation that grows keeps its id. Another first user message gives another id.
- A client id applies when the client sends it to the proxy that has the `chatgpt` deployment: an account service, or the gateway of the prototype. There the client sets its own id with `litellm_session_id` in the request body. The hook also honors `litellm_session_id` in `extra_body`, `session_id`, `metadata.session_id` and the header `x-litellm-session-id`. That proxy sends the client id to ChatGPT as it is.
- Through the gateway, the routes `codexN/*` and `codex-auto/*` are `openai/` deployments. Such a deployment does not forward `litellm_session_id` or `metadata.session_id` of the client. The account service (or `codex-router`) then derives the id, so the conversation still has a stable id, but not the id of the client.
- Do not set `general_settings.missing_session_id` in a proxy that has a `chatgpt` deployment: it gives each request a random id before the hook runs.
- Limit: a Responses client that sends only the new turn with `previous_response_id` and no session id gets another id for each turn. Such a client must send its own id.
- The hook acts on `/v1/chat/completions` and `/v1/responses`, and only when each deployment of the model group is a `chatgpt` deployment. A request for another provider does not change.
- The session id is not in the request body that ChatGPT receives. It is the value of `litellm_session_id` inside the proxy, so the logs of that proxy show it as the session of the request.
- Two conversations with the same model, the same system text and the same first user message share one id. This is intended: they share the same cached prefix.
- `codex-router` derives the id with the same function and sends it to the account service as `litellm_session_id`.

### Reasoning effort and tool calls

Luna and Sol requests keep `reasoning_effort=xhigh` and tool-choice support through the gateway and the account proxies.
The hook `image/hooks/sitecustomize.py` registers the lookup keys `gpt-6-luna`, `responses/gpt-6-luna`, `gpt-6-sol` and `responses/gpt-6-sol` until upstream has them.
`config/codex.yaml` also declares the capabilities of the three models.
`gpt-6.1-sol` is not in the hook: `config/codex.yaml` and the gateway file have entries with `model_info` as in `config/model-info.example.yaml`. A later model needs no image change in the same way ([image.md](image.md), section "A new model").

Do not use `drop_params` or lower the reasoning effort to get past a validation error.
After an image update, test xhigh chat and streaming tool calls before you remove the hook.

Codex requires upstream streaming. `supports_native_streaming` in `config/codex.yaml` turns it on for the four model IDs.
`gpt-6.1-sol-capabilities` in `config/codex.yaml` is not a route: it registers the lookup key `responses/gpt-6.1-sol`.

Not verified: non-streaming requests through `codex1/*` and `codex2/*`. `codex-router` builds the response itself (see below).

## The router `codex-router`

`compose.codex.yaml` starts `codex-router`. The routes of `config/gateway.codex.example.yaml` do not use it: they use the native fallback.
The router stays in the image as an alternative for `codex-auto/*`. To use it, a host gateway file points the `codex-auto` entries at `http://codex-router:4000/v1` and has no fallbacks for them.

`CODEX_ACCOUNT_ORDER` in `compose.codex.yaml` lists the account services in preference order. The default is `codex2,codex3,codex1`.
The first name is the preferred account. The last name is the final fallback: the router never asks for its usage and mounts no token of it.
Each other account has a read-only token mount and its own block state.

Another order needs two settings in one override file: `CODEX_ACCOUNT_ORDER`, and a read-only mount `./state/<name>:/tokens/<name>:ro` for each account except the last.
`compose.codex.yaml` mounts only `state/codex2` and `state/codex3`. The router reads `/tokens/<name>/auth.json`; without the mount it finds no login and skips the account (`skipped source=no-login`).

```yaml
# .local/compose.host.yaml: codex1 first, codex3 last
services:
  codex-router:
    environment:
      CODEX_ACCOUNT_ORDER: "codex1,codex2,codex3"
    volumes:
      - ./state/codex1:/tokens/codex1:ro
```

Compose adds the mount to the two mounts of `compose.codex.yaml`. Not verified: a run with another order.

What the router does:

- Before a request, it asks the usage endpoint of each account in the order, except the last. It caches an available result for 60 seconds.
- An account without a complete token file (`access_token` and `account_id`) has no login. The router skips it. It logs `skipped source=no-login` on a request, at most once a minute. It is not a block.
- A service without a login waits for a device code at start and stays unhealthy. `codex-router` waits for `codex1` and `codex2` to be healthy and only for `codex3` to be started. Stop `codex3` with `docker compose stop codex3` until its login when the repeated device codes are unwanted.
- It uses the first account that has a login and is not blocked. An account is blocked while its primary or secondary window is exhausted.
- It reads the reset time of each window. It does not assume a fixed length for the primary window.
- While an account is blocked, the router asks its usage endpoint again every 60 seconds and clears the block when the account is allowed and no window is at 100 percent.
- An HTTP 429 from an account moves the request to the next candidate, before any response bytes reach the client. The answer of the last candidate goes back as it is. No retry loop runs.
- A 429 blocks the account. It honors a numeric `Retry-After` and a reset timestamp of the error, with a minimum of 60 seconds. A fresh usage check bounds the block: it never runs past the reset time of an exhausted window, and with no exhausted window it lasts at most 60 seconds.
- It does not switch accounts for authentication errors, invalid requests, network failures or server errors.
- If a usage check fails for an account with a login, the router tries that account. It does not spend the allowance of a later account on a guess.
- It sends each request upstream as a stream. For a non-streaming client, it builds one response from the stream, tool-call chunks included.
- It never retries a stream that it has partly sent. Clients must handle a failure in the middle of a stream.
- Each request must hold the full conversation. `previous_response_id` and `conversation` get HTTP 409: these continuations belong to one account. Use `codex1/`, `codex2/` or `codex3/` for them.
- `CODEX_REQUEST_TIMEOUT_SECONDS` sets the upstream deadline. `compose.codex.yaml` sets 555 seconds.
- Every block, kept block, unblock and skip is logged with its source, the HTTP status and the usage window fields. No token, account id or prompt content.
  The log is the standard error of the container: `docker compose -f compose.yaml -f compose.codex.yaml logs codex-router`. At a start the router logs `router order=...` with the order in use.

The router mounts the token directories read-only and never refreshes tokens. It reads the current token after a refresh by the account service.
The account services are the only token writers.
The block state is in memory. After a restart, the router asks the usage endpoints again.

Warning: the usage endpoint is not a stable public API. A change of its schema or access reduces the routing to the fallback after HTTP 429.

No quota-based routing can reserve allowance against other clients, for example CLI sessions, that use the same account.

Not verified: native Responses API requests through the full automatic route.

`tests/test_quota_router.py` tests the order, quota exhaustion, reset, the no-login skip, the 429 cascade through three accounts, streaming and aggregation with simulated responses.

Show the routing state of the router:

```sh
docker compose -f compose.yaml -f compose.codex.yaml exec -T codex-router python -c \
  'import os,json,urllib.request; r=urllib.request.Request("http://127.0.0.1:4000/routing/status",headers={"Authorization":"Bearer "+os.environ["CODEX_MASTER_KEY"]}); print(json.load(urllib.request.urlopen(r)))'
```

The answer has `order`, `preferred_account`, `selected_account` and one `accounts` entry for each name with `blocked_until`, `block_reason`, `blocked_since` and `logged_in`. The last account has `usage_checked: false`.
