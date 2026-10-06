# GitHub Copilot models

This page describes routes to the models of one GitHub Copilot account.
A separate service, `copilot`, runs the native `github_copilot/` provider of LiteLLM. The gateway calls the service.
The service is off by default: `compose.copilot.yaml` is not in `COMPOSE_FILE`, and the gateway example has no Copilot route.

Tested with LiteLLM 1.103.0, with a stub in place of GitHub and the Copilot API.
Observed with LiteLLM 1.103.0 and a real GitHub Copilot account: login, model listing and a streaming tool call work through the service.
Not verified: model availability and entitlement for your account.

## Design

| Part | Place |
| --- | --- |
| Service | `copilot` in `compose.copilot.yaml`, on the gateway image. No published port; the project network only. |
| Service configuration | `config/copilot.yaml`: one `github_copilot/<id>` entry for each alias. `COPILOT_CONFIG` in `.env` names a host copy. |
| Token directory | `state/copilot/`, owner `1000:1000`, mode 0700, mounted read-write at `/state/copilot` of the service. Git ignores `state/`. |
| Login | `scripts/login-copilot.sh`, a one-off container. The service and the gateway never run a login. |
| Key of the service | `COPILOT_MASTER_KEY`, in `.env` for the gateway and in `secrets/copilot.env` for the service. |
| Gateway routes | `copilot/<alias>` entries in the host gateway file. `config/gateway.copilot.example.yaml` is the example. |

The service has the hardening of the other services: user `1000:1000`, no capability, `no-new-privileges`.
The gateway gets no token directory and no Copilot variable. It has only the key and the address of the service.

### Why a separate service

The provider gets a new API key about every 30 minutes. The call is synchronous: it blocks the process that makes it.
If GitHub accepts the connection and does not answer, the provider tries 3 times. Each try waits for `request_timeout` of the process.

- In the service, a stalled call stops the Copilot routes only. `config/copilot.yaml` sets `request_timeout: 540`, so the stop can last 27 minutes.
- The gateway continues. Its request to the service ends after 570 seconds with a timeout error. Other routes are not affected.
- During the stop the health check of the service fails and `docker compose ps` shows `unhealthy`. Compose does not restart an unhealthy container. `docker compose restart copilot` ends the stop.

Not verified in a run: a stalled key refresh in this layout. The source of the provider and a probe in one process are the evidence.

## The token files

| File | Content | Writer |
| --- | --- | --- |
| `access-token` | The GitHub OAuth token of the device flow, as plain text. Scope `read:user`. It has no expiry time in the file. | The login |
| `api-key.json` | The short-lived Copilot API key: `token`, `expires_at`, and `endpoints.api`, the address of the Copilot API for the account. | The login and the service |

The login makes both files with mode 0600. The service keeps the mode of a file that exists.
If the service makes a new `api-key.json`, the file gets mode 0644. The directory mode 0700 protects it: no other user can open the directory.
The provider reads the directory from `GITHUB_COPILOT_TOKEN_DIR`.
`GITHUB_COPILOT_ACCESS_TOKEN_FILE` and `GITHUB_COPILOT_API_KEY_FILE` change the two file names; this stack keeps the defaults.

When `api-key.json` is expired, the provider sends `access-token` to `https://api.github.com/copilot_internal/v2/token` and writes the new key.
The address of the API comes from `endpoints.api` of `api-key.json`, then from `GITHUB_COPILOT_API_BASE`, then the default `https://api.githubcopilot.com`.

Warning: `access-token` gives access to the Copilot allowance of the account. Do not copy it into another file, a report or a commit.

## Why the service must not start a device flow

The provider starts a device flow each time it finds no `access-token`. The flow blocks the process.

- At the start of the proxy, each `github_copilot/` entry starts a flow of 3 attempts of 60 seconds. The proxy does not listen during that time. With 15 entries the start takes up to 45 minutes.
- The device code appears in the log of the container.

`compose.copilot.yaml` sets `GITHUB_COPILOT_DEVICE_CODE_URL` to a dead local address. The provider then fails at once, and the service starts in its usual time.
The login container does not get this value and uses GitHub.

Warning: do not remove `GITHUB_COPILOT_DEVICE_CODE_URL` from the service.

With no token, the service drops each entry at the start. A login does not bring the entries back: the service needs a recreate after the login.

## Set up

Do the steps in this order. The login comes before the first start of the service.
If the service starts first, Docker makes `state/copilot/` with the owner `root`, and the service cannot write the key file.

1. Make the key of the service. The gateway reads it from `.env`; the service reads it from `secrets/copilot.env`.

   A new `.env` from `scripts/create-env.py` has `COPILOT_MASTER_KEY`. For a `.env` without the line, add it:

   ```sh
   printf '\nCOPILOT_MASTER_KEY=sk-%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
   ```

   The first `\n` keeps the key on its own line when `.env` has no final newline.

   Then copy the line to the file of the service:

   ```sh
   mkdir -p secrets
   grep '^COPILOT_MASTER_KEY=' .env > secrets/copilot.env
   chmod 600 secrets/copilot.env
   ```

2. Start the login as root. Open the displayed URL with the GitHub account and enter the code.

   ```sh
   sh scripts/login-copilot.sh
   ```

   The container prints one line with the URL and the code, then `Login complete.`
   The last line names the recreate of the service. That command is for a later login: at the first login the service does not exist until step 6.
   Each code is valid for one minute: the provider polls 12 times with 5 seconds between. It then shows a new code, 3 times in all.
   Enter the newest code. The provider polls only for the newest code, so an older code gives no login.

   As root, the script makes `state/copilot/` with owner `1000:1000` and mode 0700.
   A missing parent directory `state/` gets the usual mode of the shell, not 0700, and the owner of the caller.
   A host user that is not root cannot set that owner. For that user the script stops with exit code 2, unless the user has the uid of `COPILOT_USER` (default `1000:1000`) or the directory has that owner.
   `--dry-run` prints the docker command and changes nothing.
   A second run with an `access-token` in the directory starts no new flow. For a new login, remove the two files first.

3. List the models of the account. The command prints model ids only.

   ```sh
   sh scripts/login-copilot.sh models
   sh scripts/login-copilot.sh models --endpoints
   ```

   `--endpoints` adds the API paths that the Copilot API states for each model, and the policy state.
   A model with `policy=disabled` is in the list, but a request to it gets HTTP 400, "The requested model is not supported".
   Enable the model in the Copilot settings of the GitHub account (Features, Models), then send the request again. The service needs no restart.
   `policy=none` means the model has no policy and is available.
   The LiteLLM cost map is not the entitlement: the provider does not read the models endpoint. This list is the source for the entries.

4. Set the entries of the service. `config/copilot.yaml` holds placeholder ids.
   Copy it to `.local/config/copilot.yaml`, keep only the ids of step 3, and add `COPILOT_CONFIG=./.local/config/copilot.yaml` to `.env`.
   An entry for an id that the account does not have does no harm: only a request to it fails.

5. Add `compose.copilot.yaml` to `COMPOSE_FILE` in `.env` ([host-configuration.md](host-configuration.md)). Keep the host override file last.

   ```sh
   COMPOSE_FILE=compose.yaml:compose.copilot.yaml:.local/compose.host.yaml
   ```

   The Copilot service does not need the Codex services. If the host uses them, keep `compose.codex.yaml` in the list:
   `COMPOSE_FILE=compose.yaml:compose.codex.yaml:compose.copilot.yaml:.local/compose.host.yaml`.

6. Start the service. The command does not change the gateway.

   ```sh
   docker compose up -d copilot
   docker compose ps copilot
   docker compose logs copilot | grep -c "ignoring and continuing"
   ```

   The status must be `healthy` and the count must be 0. Each counted line is an entry that the service dropped.

7. Add the routes to the host gateway file. Copy the entries of `model_list` from `config/gateway.copilot.example.yaml` and keep the aliases that the service has.

8. Recreate the gateway. It reads `COPILOT_MASTER_KEY` and the new routes at a recreate only.

   ```sh
   docker compose up -d --force-recreate gateway
   ```

9. Test the two API paths of the service. `copilot/sonnet` uses `/chat/completions`. `copilot/codex` uses `/responses`: the service changes the chat request.

   Send one streaming request with a tool call to each route. This is the form that the stub test passed.
   For a Claude id, remove `"tool_choice": "required"` from the request: the Copilot API answers
   `tool_choice: type "tool" and "any" are not supported for this model`. With `tool_choice` absent the model calls the tool.

   ```sh
   key=$(sed -n 's/^LITELLM_MASTER_KEY=//p' .env)
   for model in copilot/sonnet copilot/codex; do
     curl -sS -N http://127.0.0.1:4321/v1/chat/completions \
       -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
       -d '{"model": "'"$model"'", "stream": true, "tool_choice": "required",
            "messages": [{"role": "user", "content": "Call the tool ping."}],
            "tools": [{"type": "function", "function": {"name": "ping",
              "parameters": {"type": "object", "properties": {}}}}]}' |
       grep -c -e '"finish_reason": *"tool_calls"' -e '^data: \[DONE\]'
   done
   ```

   Each count must be 2: one line with `finish_reason: tool_calls` and the `[DONE]` line. Use the address of `GATEWAY_PORT` if the host changes it.

   Then run the script. It checks the authentication and sends one short chat request without streaming to each route.

   ```sh
   python3 scripts/verify.py --model copilot/sonnet --model copilot/codex
   ```

   Not verified: a request without streaming. The stub test used streaming requests with a tool call.

After a later login, or after a change of `config/copilot.yaml`, recreate only the service: `docker compose up -d --force-recreate copilot`. The gateway needs no recreate.

## Routes and the alias contract

The stable name is `copilot/<alias>`. Clients use the alias only.

| Gateway route | Gateway target | Service entry | Rule |
| --- | --- | --- | --- |
| `copilot/opus`, `copilot/sonnet`, `copilot/gpt`, `copilot/codex` | `litellm_proxy/<alias>` at `http://copilot:4000` | `<alias>` to the newest allowed `github_copilot/<id>` of the family | The alias moves to a new version in `config/copilot.yaml`. The gateway entry does not change. |
| `copilot/<id>`, for example `copilot/claude-sonnet-4.5` | `litellm_proxy/<id>` at `http://copilot:4000` | `<id>` to `github_copilot/<id>` | The full name stays pinned. |

The two examples hold the ids that the LiteLLM 1.103.0 cost map knows for Claude Opus, Claude Sonnet, GPT-5 and Codex. They are placeholders: the account list of step 3 decides.
A list can hold ids such as `claude-opus-4.6-fast`. In the example, `opus` points at `claude-opus-4.5`. Decide the target of `opus` after step 3. Not verified: the premium-request cost of a `-fast` id.

Gateway entries:

- An entry has `model: litellm_proxy/<alias>`, `api_base: http://copilot:4000` (no `/v1`), `api_key: os.environ/COPILOT_MASTER_KEY`, and `timeout` and `stream_timeout` 570.
- The provider `litellm_proxy/` sends `tool_choice` and the other OpenAI parameters to the service with no capability check. The entries need no `model_info`.
- The gateway does not change a chat request into a Responses request. The service does that for a Codex id.
- The host gateway file needs `router_settings.num_retries: 0`, as `config/gateway.example.yaml` has. Without it the gateway sends a failed request to the service 2 more times. The service itself does not retry: `config/copilot.yaml` sets `num_retries: 0`. Not verified: the premium-request cost of a retried request.

Service entries:

- An entry has `model`, `timeout` and `stream_timeout` only. It has no key and no address. The timeouts are 540, below the 570 of the gateway.
- `extra_headers` is not necessary. The provider sets `Editor-Version`, `Editor-Plugin-Version`, `Copilot-Integration-Id`, `User-Agent` and `X-GitHub-Api-Version`. A value in `extra_headers` replaces the default.
- A Codex id needs `model_info.mode: responses`: LiteLLM then sends the request to `/responses`. Other ids go to `/chat/completions`. A model that is not in the cost map goes to `/chat/completions`.
- `disable_copilot_system_to_assistant: true` is in `litellm_settings` of the service. Without it, the provider changes each `system` message of a chat request into an `assistant` message.

### An id that the cost map does not have

All ids of the two examples are in the cost map of LiteLLM 1.103.0, so no entry has `model_info` with an `id`.
A new id of the account can be missing from the cost map. If the service refuses a parameter for such an id, add this form to its entry in `config/copilot.yaml`:

```yaml
    model_info:
      id: NEW_MODEL_ID
      mode: chat
      input_cost_per_token: 0
      output_cost_per_token: 0
      supports_function_calling: true
      supports_tool_choice: true
```

`id` is the bare model id, and one entry only can have it. Use `mode: responses` for an id that the account list gives on `/responses` only. Add `supports_reasoning: true` for a reasoning model.

The form has no `litellm_provider`. The GPT parameter check of LiteLLM reads the bare id with no provider; a value `github_copilot` then does not match, and the check refuses `tool_choice`.
A value `openai` passes that check, but then the lookup of `mode` under the provider `github_copilot` fails, and the request goes to `/chat/completions`.
With no value, both lookups pass. Verified with LiteLLM 1.103.0 for `gpt-5.5`, `gpt-6-astra`, `gpt-6-luna`, `gpt-6-sol` and `grok-4.7`.

An id that LiteLLM cannot map to a provider by its name, for example `gpt-6.1-sol`, still fails the check: `github_copilot does not support parameters: ['tool_choice']`.
For such an id add the allowlist to `litellm_params` of each entry that uses it:

```yaml
    litellm_params:
      model: github_copilot/gpt-6.1-sol
      allowed_openai_params: [tool_choice]
```

Not verified: a request with this allowlist. Verified: `get_optional_params` accepts `tool_choice` with it.

## Model list as a source for other tools

The Copilot models endpoint needs the Copilot API key. It is an authenticated source.
A tool that reads only public sources cannot read this list. `sh scripts/login-copilot.sh models` prints it for a tool that may use the login.
No script of this repo changes the host gateway file or the service configuration for these routes.

## Limits and terms

The LiteLLM page for the provider (`docs/providers/github_copilot`) says:

- A paid GitHub Copilot subscription is necessary.
- The supported endpoints are `/chat/completions`, `/responses` for Codex models, and `/embeddings`.
- The login is the OAuth device flow, and the credentials stay in local files.

The page says nothing about rate limits and nothing about the GitHub terms of service. Read the terms of the Copilot plan before use.
Not verified: the request limits of the plan, and the response of the Copilot API when the allowance is used up.
Not verified: the full `/health` endpoint of a proxy can send one request for each model, and each can use a premium request. The health check of the containers uses `/health/liveliness`, which sends none. The tracked configurations set no background health check.

## Failures

The gateway lists the `copilot/*` routes in `/v1/models` in each case below: its entries do not depend on the token.
To see the state, send a request, or count the dropped entries of the service: `docker compose logs copilot | grep -c "ignoring and continuing"`.

| Event | Result | Action |
| --- | --- | --- |
| No token at the start of the service | The service starts in its usual time and is `healthy`. It drops its entries. A request to `copilot/<alias>` gets HTTP 400 at once, "There are no healthy deployments for this model". Other routes continue. | Run the login, then `docker compose up -d --force-recreate copilot`. |
| GitHub not reachable at the start of the service, token present | `api-key.json` is usually expired at a start, so the service asks for a new key. The request fails, and the service drops its entries. The result is the same HTTP 400. The entries do not come back by themselves. | When GitHub is reachable again: `docker compose restart copilot`. Send one request after each recreate of the service. |
| `state/copilot/` not writable by `1000:1000` (for example made by Docker with owner `root`) | No restart loop. Each request fails with "Failed to save API key: Permission denied", and each request repeats the key exchange with GitHub. At a start, the service drops its entries. | As root: `chown 1000:1000 state/copilot && chmod 700 state/copilot`, then recreate the service. |
| `api-key.json` missing or expired and no `access-token`, service running | HTTP 400, "Failed to get access token after 3 attempts", at once. | Run the login. Recreate the service. |
| `access-token` revoked | The key exchange fails 3 times, then the request fails. No device flow starts. | Remove the two files, run the login, recreate the service. |
| GitHub accepts the key request and does not answer | The service stops for up to 27 minutes and is `unhealthy`. Requests to `copilot/*` end with a timeout at the gateway after 570 seconds. Other routes continue. | `docker compose restart copilot`. |
| Service `copilot` stopped | A request to `copilot/*` fails at the gateway with a connection error. Other routes continue. | `docker compose up -d copilot`. |

Not verified in a run: the last two rows. The rows for a directory that is not writable and for GitHub not reachable come from a test of the provider in one process.
The logs of the service and of the gateway show no token in the tested cases.

## Remove the routes

1. Remove the `copilot/*` entries from the host gateway file.
2. `docker compose up -d --force-recreate gateway`.
3. `docker compose stop copilot && docker compose rm -f copilot`, then remove `compose.copilot.yaml` from `COMPOSE_FILE`.
4. To end the login, remove `state/copilot/access-token` and `state/copilot/api-key.json`, and revoke the authorization of "GitHub Copilot Plugin" in the application settings of the GitHub account.
