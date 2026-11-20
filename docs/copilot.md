# GitHub Copilot models

This page describes routes to the models of one GitHub Copilot account.
A separate service, `copilot`, runs the native `github_copilot/` provider of LiteLLM. The gateway calls the service.
The service is off by default: `compose.copilot.yaml` is not in `COMPOSE_FILE`, and the gateway example has no Copilot route.

Tested with LiteLLM 1.103.0, with a stub in place of GitHub and the Copilot API.
Observed with LiteLLM 1.103.0 and a real GitHub Copilot account: login, model listing and a streaming tool call work through the service.
The service runs on the gateway image. The image pins LiteLLM 1.110.2 (`Dockerfile`).
The image suite checks the configuration. Not verified: every configured model with a real Copilot account on LiteLLM 1.110.2.
From LiteLLM 1.110.0 the provider sends the GitHub token of the login to the Copilot API. It makes no token exchange and keeps no `api-key.json` (upstream PR 50126).
Tested with LiteLLM 1.110.2, with a stub in place of the Copilot API (section [Stub test of LiteLLM 1.110.2](#stub-test-of-litellm-11102)).
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

The provider asks `https://api.github.com/copilot_internal/user` for the API address of the account.
It keeps a good answer for 30 minutes for each token. After a failed lookup, the next request asks again.
The call is synchronous: it blocks the process that makes it.
The lookup uses the shared HTTP client of the provider: a connect timeout of 5 seconds, and a read timeout from `request_timeout` of the process when it is set.
These are per-operation timeouts of HTTPX, not one deadline for the whole call. After a timeout or an error the lookup gives no address, and the provider uses `GITHUB_COPILOT_API_BASE` or the default.

- In the service, a stalled call stops the Copilot routes only. `config/copilot.yaml` sets `request_timeout: 540`, so one read can wait up to 9 minutes.
- The gateway continues. Its request to the service can end after 570 seconds with a timeout error, or succeed when the lookup fails first and the fallback address serves the account. Other routes are not affected.
- During the stop the health check of the service can fail, and `docker compose ps` then shows `unhealthy`. Compose does not restart an unhealthy container. `docker compose restart copilot` ends the stop.

Not verified in a run: a stalled lookup in this layout. The source of the LiteLLM 1.110.2 provider is the evidence.

## The token files

| File | Content | Writer |
| --- | --- | --- |
| `access-token` | The GitHub OAuth token of the device flow, as plain text. Scope `read:user`. It has no expiry time in the file. | The login |

The login makes the file with mode 0600. The service only reads it.
The provider reads the directory from `GITHUB_COPILOT_TOKEN_DIR`.
`GITHUB_COPILOT_ACCESS_TOKEN_FILE` changes the file name; this stack keeps the default.

The provider sends `access-token` as the Bearer token of each Copilot request.
The address of the API comes from `endpoints.api` of `https://api.github.com/copilot_internal/user`, then from `GITHUB_COPILOT_API_BASE`, then the default `https://api.githubcopilot.com`.
The provider accepts only an `endpoints.api` address of a Copilot host of `github.com`.
An `api-key.json` from LiteLLM 1.109.1 or earlier stays in the directory. LiteLLM 1.110.2 does not read it, and `GITHUB_COPILOT_API_KEY_URL` and `GITHUB_COPILOT_API_KEY_FILE` have no effect.
Keep `api-key.json` until the upgrade is accepted, as a precaution: a 1.109.1 service reads it as a cache of the Copilot key.
Without the file, the 1.109.1 provider makes a new key. Its real prerequisites are a valid `access-token` and a working token exchange with GitHub.

Warning: `access-token` gives access to the Copilot allowance of the account. Do not copy it into another file, a report or a commit.

## Why the service must not start a device flow

Without an `access-token`, the provider can start a device flow. The flow blocks the process.

- With LiteLLM 1.103.0, at the start of the proxy, each `github_copilot/` entry started a flow of 3 attempts of 60 seconds. The proxy did not listen during that time. With 15 entries the start took up to 45 minutes.
- The device code appears in the log of the container.
- LiteLLM 1.110.2 refuses the flow in an event loop or a worker thread, as in the proxy. A request then fails at once with HTTP 400, "GitHub Copilot device-code login needs a human".

`compose.copilot.yaml` sets `GITHUB_COPILOT_DEVICE_CODE_URL` to a dead local address. A flow then fails at once, and the service starts in its usual time.
The login container does not get this value and uses GitHub.

Warning: do not remove `GITHUB_COPILOT_DEVICE_CODE_URL` from the service. With LiteLLM 1.110.2 it is a second guard. Not verified: a path of the 1.110.2 provider that starts the flow outside an event loop.

With LiteLLM 1.103.0 and no token, the service dropped each entry at the start, and a login needed a recreate of the service.
With LiteLLM 1.110.2 and no token, the service keeps its entries. It reads `access-token` at each request, so a login takes effect at the next request with no recreate.

## Set up

Do the steps in this order. The login comes before the first start of the service.
The `docker compose` commands of this page select the project of the exported `COMPOSE_PROJECT_NAME`, else the `name:` of the Compose files (`litellm`).
When the project name of the deployment differs from that `name:`, export `COMPOSE_PROJECT_NAME` first. The check of step 9 needs it.
If the service starts first, Docker makes `state/copilot/` with the owner `root`, and the login cannot write the token file.

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

   The container prints one line with the URL and the code, then `Token stored` when the login wrote `access-token`.
   It then asks the Copilot models endpoint with the new token. `Entitlement shown` and `Login complete.` follow when the endpoint lists at least one model.
   An HTTP error, an empty list, a list in which each model is disabled by policy, or an answer of another form stops the login with `Entitlement not shown` and the reason.
   The token file stays in that case; the account may have no Copilot seat or no enabled model.
   The provider returns the token also when it cannot write the token file. The script then compares the file with the token, prints neither,
   and stops with `Token not stored` before it asks the models endpoint.
   The last line names the recreate of the service. That command is for a later login: at the first login the service does not exist until step 6.
   With LiteLLM 1.110.2 the recreate is not necessary after a later login, but it does no harm.
   Each code is valid for one minute: the provider polls 12 times with 5 seconds between. It then shows a new code, 3 times in all.
   Enter the newest code. The provider polls only for the newest code, so an older code gives no login.

   As root, the script makes `state/copilot/` with owner `1000:1000` and mode 0700.
   A missing parent directory `state/` gets the usual mode of the shell, not 0700, and the owner of the caller.
   A host user that is not root cannot set that owner. For that user the script stops with exit code 2, unless the user has the uid of `COPILOT_USER` (default `1000:1000`) or the directory has that owner.
   `--dry-run` prints the docker command and changes nothing.
   A second run with an `access-token` in the directory starts no new flow. For a new login, remove `access-token` first.

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

9. Test the API paths of the service. `copilot/sonnet` uses `/chat/completions`. `copilot/codex` uses `/responses`: the service changes the chat request.
   The embedding routes use `/embeddings`.

   Send one streaming request with a tool call to each chat route. This is the form that the stub test passed.
   A Claude id gets no `"tool_choice": "required"`: the Copilot API answers
   `tool_choice: type "tool" and "any" are not supported for this model`. With `tool_choice` absent the model calls the tool.
   The loop sets `tool_choice` for the Codex route only. It reads the address from the published port 4000 of the gateway container (`docker compose port`), not from `.env`.
   The block needs Bash and the exported `COMPOSE_PROJECT_NAME` of the deployment. Without that export, when the lookup of the published port fails,
   or when its answer is not one local address and port, the block prints a `STOP:` line, exits with status 1 and sends no request.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euo pipefail
     name=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$name" ] ||
       { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment; no request sent"; exit 1; }
     port=$(docker compose port gateway 4000) || { echo "STOP: no published port 4000 for the gateway of $name; no request sent"; exit 1; }
     port=${port/#0.0.0.0:/127.0.0.1:}
     [[ $port =~ ^127\.0\.0\.1:[0-9]+$ ]] || { echo "STOP: '$port' is not one local address and port; no request sent"; exit 1; }
     key=$(sed -n 's/^LITELLM_MASTER_KEY=//p' .env); [ -n "$key" ] || { echo "STOP: no LITELLM_MASTER_KEY in .env; no request sent"; exit 1; }
     for model in copilot/sonnet copilot/codex; do
       case $model in
         copilot/codex) choice='"tool_choice": "required",' ;;
         *) choice= ;;
       esac
       if out=$(curl -sS -N -w '\nHTTP %{http_code}\n' "http://$port/v1/chat/completions" \
           -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
           -d '{"model": "'"$model"'", "stream": true, '"$choice"'
                "messages": [{"role": "user", "content": "Call the tool ping."}],
                "tools": [{"type": "function", "function": {"name": "ping",
                  "parameters": {"type": "object", "properties": {}}}}]}'); then curl=ok; else curl=failed; fi
       code=$(printf '%s\n' "$out" | tail -n 1)
       re='"finish_reason": *"tool_calls"'; [[ $out =~ $re ]] && tool=ok || tool=missing
       [ "$(printf '%s\n' "$out" | grep '^data: ' | tail -n 1)" = 'data: [DONE]' ] && end=ok || end=missing
       echo "$model: curl=$curl $code tool=$tool end=$end"
     done
   )
   ```

   Each route must print `curl=ok HTTP 200 tool=ok end=ok`. The four values are separate checks:
   the request ended without a curl error, the status is 200, a chunk has `finish_reason: tool_calls`, and the last `data:` line is `[DONE]`.

   Then run the script. It checks the authentication and sends one short chat request without streaming to each route.
   This block and the embeddings block below take the address in the same way, and stop in the same way before any request.

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euo pipefail
     name=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$name" ] ||
       { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment; no request sent"; exit 1; }
     port=$(docker compose port gateway 4000) || { echo "STOP: no published port 4000 for the gateway of $name; no request sent"; exit 1; }
     port=${port/#0.0.0.0:/127.0.0.1:}
     [[ $port =~ ^127\.0\.0\.1:[0-9]+$ ]] || { echo "STOP: '$port' is not one local address and port; no request sent"; exit 1; }
     python3 scripts/verify.py --base "http://$port" --model copilot/sonnet --model copilot/codex
   )
   ```

   Not verified: a chat request without streaming. The stub test used streaming requests with a tool call.

   Send an embeddings request through the gateway and check the vector:

   ```sh
   (
     [ -n "${BASH_VERSION-}" ] || { echo "STOP: run this block in bash" >&2; exit 1; }
     set -euo pipefail
     name=$(printenv COMPOSE_PROJECT_NAME) && [ -n "$name" ] ||
       { echo "STOP: export COMPOSE_PROJECT_NAME with the project name of the deployment; no request sent"; exit 1; }
     port=$(docker compose port gateway 4000) || { echo "STOP: no published port 4000 for the gateway of $name; no request sent"; exit 1; }
     port=${port/#0.0.0.0:/127.0.0.1:}
     [[ $port =~ ^127\.0\.0\.1:[0-9]+$ ]] || { echo "STOP: '$port' is not one local address and port; no request sent"; exit 1; }
     key=$(sed -n 's/^LITELLM_MASTER_KEY=//p' .env); [ -n "$key" ] || { echo "STOP: no LITELLM_MASTER_KEY in .env; no request sent"; exit 1; }
     curl -fsS -m 60 "http://$port/v1/embeddings" \
       -H "Authorization: Bearer $key" -H "Content-Type: application/json" \
       -d '{"model": "copilot/text-embedding-ada-002", "input": "ping"}' |
       python3 -c 'import json, math, sys
   vector = json.load(sys.stdin)["data"][0]["embedding"]
   assert len(vector) == 1536
   assert all(type(value) in (int, float) and math.isfinite(value) for value in vector)
   print("1536 numbers verified")'
   )
   ```

   The first run verified `text-embedding-ada-002` through the gateway with single and batch inputs.
   Each vector had 1536 values. Not verified: a live request to `text-embedding-3-small`.

After a change of `config/copilot.yaml`, recreate only the service: `docker compose up -d --force-recreate copilot`. The gateway needs no recreate.
After a later login, the service of LiteLLM 1.110.2 uses the new token at the next request.

## Routes and the alias contract

For chat models, the stable name is `copilot/<alias>`. Clients can use a full name to pin a version.
Embedding routes have no short alias. Clients use their pinned full names.

| Gateway route | Gateway target | Service entry | Rule |
| --- | --- | --- | --- |
| `copilot/opus`, `copilot/sonnet`, `copilot/gpt`, `copilot/codex` | `litellm_proxy/<alias>` at `http://copilot:4000` | `<alias>` to the newest allowed `github_copilot/<id>` of the family | The alias moves to a new version in `config/copilot.yaml`. The gateway entry does not change. |
| `copilot/<id>`, for example `copilot/claude-sonnet-4.5` | `litellm_proxy/<id>` at `http://copilot:4000` | `<id>` to `github_copilot/<id>` | The full name stays pinned. |
| `copilot/text-embedding-ada-002` | `litellm_proxy/text-embedding-ada-002` at `http://copilot:4000` | `text-embedding-ada-002` to `github_copilot/text-embedding-ada-002` | Pinned embedding model, 1536 dimensions. |
| `copilot/text-embedding-3-small` | `litellm_proxy/text-embedding-3-small` at `http://copilot:4000` | `text-embedding-3-small` to `github_copilot/text-embedding-3-small` | Pinned embedding model, 1536 dimensions. |

The two examples hold ids from the LiteLLM 1.103.0 cost map for Claude Opus, Claude Sonnet, GPT-5, Codex and embeddings. They are placeholders: the account list of step 3 decides.
The cost map of LiteLLM 1.109.1 does not have 9 of these ids (section [An id that the cost map does not have](#an-id-that-the-cost-map-does-not-have)).
The cost map of LiteLLM 1.110.2 does not have the same 9 ids.
A list can hold ids such as `claude-opus-4.6-fast`. In the example, `opus` points at `claude-opus-4.5`. Decide the target of `opus` after step 3. Not verified: the premium-request cost of a `-fast` id.

Gateway entries:

- An entry has `model: litellm_proxy/<alias>`, `api_base: http://copilot:4000` (no `/v1`), `api_key: os.environ/COPILOT_MASTER_KEY`, and `timeout` and `stream_timeout` 570.
- The provider `litellm_proxy/` sends `tool_choice` and the other OpenAI parameters to the service with no capability check. Chat entries need no `model_info`.
- Embedding entries need `model_info.mode: embedding` and `model_info.output_vector_size: 1536` on both the gateway and the service.
- The gateway does not change a chat request into a Responses request. The service does that for a Codex id.
- The host gateway file needs `router_settings.num_retries: 0`, as `config/gateway.example.yaml` has. Without it the gateway sends a failed request to the service 2 more times. The service itself does not retry: `config/copilot.yaml` sets `num_retries: 0`. Not verified: the premium-request cost of a retried request.

Service entries:

- The `litellm_params` of an entry has `model`, `timeout` and `stream_timeout` only. It has no key and no address. The timeouts are 540, below the 570 of the gateway.
- `extra_headers` is not necessary. The provider sets `Editor-Version`, `Editor-Plugin-Version`, `Copilot-Integration-Id`, `User-Agent` and `X-GitHub-Api-Version`. A value in `extra_headers` replaces the default.
- A Codex id needs `model_info.mode: responses`: LiteLLM then sends the request to `/responses`. Set `model_info.mode: embedding` as the live entry does. Not verified: whether the route works without it.
- Other ids go to `/chat/completions`. A model that is not in the cost map defaults to `/chat/completions` without an explicit mode.
- `disable_copilot_system_to_assistant: true` is in `litellm_settings` of the service. Without it, the provider changes each `system` message of a chat request into an `assistant` message.

### An id that the cost map does not have

The ids of the two examples come from the cost map of LiteLLM 1.103.0.
The cost map of LiteLLM 1.109.1 has no `github_copilot/<id>` entry for 9 of the 13 ids of `config/copilot.yaml`:
`claude-opus-41`, `claude-opus-4.5`, `claude-opus-4.6-fast`, `claude-sonnet-4`, `claude-sonnet-4.5`, `gpt-5`, `gpt-5.1`, `gpt-5.1-codex-max` and `gpt-5.2`.
The image sets `LITELLM_LOCAL_MODEL_COST_MAP=True`, so the service uses this map and does not fetch a newer one.

A stub test with LiteLLM 1.109.1 sent a chat request with a tool and `tool_choice: required` to each chat and Codex entry.
It sent each request with and without streaming.
The stub took the place of the Copilot API. Each request returned HTTP 200 with the tool call.
Each request reached the stub on the expected path with `tool_choice`: `/responses` for the Codex ids, `/chat/completions` for the others.
In this test, the 9 ids differ from an id in the cost map in these points:

- A non-stream request to `/v1/chat/completions` of the service returns the cost 0. A streamed response has no cost header.
  With LiteLLM 1.110.2 (upstream PR 49467) a non-stream response for these ids has no `x-litellm-response-cost` header at all.
  A stub test gave no header for `gpt-5.2` and `claude-sonnet-4.5`, and `3.25e-06` for `gpt-5-mini`, which has a cost-map row.
  An unknown cost gives no header. An entry with explicit zero prices (the `model_info` form below) still gives `x-litellm-response-cost: 0.0`
  on 1.110.2, as on 1.109.1. A stream has no cost header on either version. A rehearsal with a stub provider showed both.
- Not verified: the cost of a streamed request, and of a request to `/v1/messages` or `/v1/responses`. Not verified: how the account counts the Copilot allowance.
- `/v1/model/info` has no `max_input_tokens` and no `max_tokens` for the 9 ids. It has no `mode` for 8 of them; `gpt-5.1-codex-max` has `mode: responses` from the config.
- The service sets no limit from the cost map. Not verified: the service checks no context or output size before the call for these ids.
- Not verified: the gateway counts no cost from the service for these ids, so a key budget of the gateway does not count them.
- The service log has no warning for the entries, and the service drops no entry at the start.

The entries therefore have no `model_info` for the cost map. The form below gave the same requests and the same cost in the same test.
Not verified: whether the Copilot account still serves the 9 ids, and a request to them with a real account.
Step 3 shows the list of the account; remove an id that the account does not have.

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
With LiteLLM 1.109.1 the stub test above passed with the form on the 9 ids: `tool_choice` reached the stub, and `gpt-5.1-codex-max` kept `/responses`.
Not verified: the lookups with LiteLLM 1.109.1 for a GPT id whose bare name is not in the cost map.

An id that LiteLLM cannot map to a provider by its name, for example `gpt-6.1-sol`, can fail the check: `github_copilot does not support parameters: ['tool_choice']`.
With LiteLLM 1.109.1 the stub test sent `tool_choice` to the made-up id `gpt-9-probe` with no `model_info`: HTTP 200 with the tool call.
Not verified: this check with LiteLLM 1.109.1 for `gpt-6.1-sol`.
For such an id add the allowlist to `litellm_params` of each entry that uses it:

```yaml
    litellm_params:
      model: github_copilot/gpt-6.1-sol
      allowed_openai_params: [tool_choice]
```

Not verified: a request with this allowlist. Verified: `get_optional_params` accepts `tool_choice` with it.

## Model list as a source for other tools

The Copilot models endpoint needs the GitHub token of the login. It is an authenticated source.
A tool that reads only public sources cannot read this list. `sh scripts/login-copilot.sh models` prints it for a tool that may use the login.
No script of this repo changes the host gateway file or the service configuration for these routes.

## Limits and terms

The LiteLLM page for the provider (`docs/providers/github_copilot`) says:

- A paid GitHub Copilot subscription is necessary.
- The supported endpoints are `/chat/completions`, `/responses` for Codex models, and `/embeddings`.
- The login is the OAuth device flow, and the credentials stay in local files.

The page says nothing about rate limits and nothing about the GitHub terms of service. Read the terms of the Copilot plan before use.
Not verified: the request limits of the plan, and the response of the Copilot API when the allowance is used up.
Not verified: the premium-request cost of an embeddings request.
Not verified: the full `/health` endpoint of a proxy can send one request for each model, and each can use a premium request. The health check of the containers uses `/health/liveliness`, which sends none. The tracked configurations set no background health check.

## Failures

The gateway lists the `copilot/*` routes in `/v1/models` in each case below: its entries do not depend on the token.
To see the state, send a request. With LiteLLM 1.110.2 the service drops no entry for a token problem; `docker compose logs copilot | grep -c "ignoring and continuing"` counts entries that the service drops for another reason.

| Event | Result | Action |
| --- | --- | --- |
| No `access-token`, at the start or later | The service starts in its usual time and is `healthy`. It keeps its entries. The service answers a request with HTTP 400 at once, "GitHub Copilot device-code login needs a human". Other routes continue. | Run the login. The next request uses the token. |
| GitHub not reachable, token present | The address lookup fails, and the provider uses `GITHUB_COPILOT_API_BASE` or `https://api.githubcopilot.com`. The service keeps its entries. A request works when that address is the address of the account. Each request repeats the lookup until one succeeds. | None for an account on the default address. For another account, set `GITHUB_COPILOT_API_BASE` in the service. |
| `state/copilot/` not readable by `1000:1000` (for example made by Docker with owner `root`) | The provider finds no token: the same result as the first row. | As root: `chown 1000:1000 state/copilot && chmod 700 state/copilot`. |
| `access-token` revoked | The Copilot API refuses the token, and the request fails with the status of the Copilot API. No device flow starts. | Remove `access-token` and run the login. |
| GitHub accepts the address lookup and does not answer | The lookup waits for its HTTPX timeouts. Possible results: the gateway request to `copilot/*` ends with its own timeout after 570 seconds; or the lookup fails first, the provider uses `GITHUB_COPILOT_API_BASE` or the default address, and the request can succeed. While the call waits, the health check of the service can fail. Other routes continue. | `docker compose restart copilot` when the service stays `unhealthy`. |
| Service `copilot` stopped | A request to `copilot/*` fails at the gateway with a connection error. Other routes continue. | `docker compose up -d copilot`. |

Not verified in a run: the rows for a revoked token, a stalled lookup and a stopped service. Not verified: the status that the gateway gives for the HTTP 400 of the service.
The stub test of LiteLLM 1.110.2 checked the first three rows. The repeated lookup and the other rows come from the source of the provider.
The logs of the service and of the gateway show no token in the tested cases.

### Stub test of LiteLLM 1.110.2

The test ran the service on the gateway image of LiteLLM 1.110.2 with `config/copilot.yaml`, the settings of `compose.copilot.yaml`
and a synthetic token, on an internal Docker network with no route to GitHub. A stub took the place of the Copilot API through `GITHUB_COPILOT_API_BASE`.

- With a token: the service started in its usual time and dropped no entry. Streamed and non-streamed chat requests with a tool, a Codex request and an embeddings request each returned HTTP 200.
  Each request reached the stub with the GitHub token as the Bearer token: `/chat/completions`, `/responses` for the Codex id, `/embeddings`. `tool_choice` reached the stub.
  The lookup at `api.github.com` failed, and the provider used `GITHUB_COPILOT_API_BASE`. The service wrote no file into the token directory.
- Without a token, and with a token directory of owner `root` and mode 0700: the service started in its usual time, dropped no entry, started no device flow and answered each request with HTTP 400. No request reached the stub.
- A token written while the service ran: the next chat and Codex requests returned HTTP 200 with no recreate.
- The logs of the service had no token.

## Remove the routes

1. Remove the `copilot/*` entries from the host gateway file.
2. `docker compose up -d --force-recreate gateway`.
3. `docker compose stop copilot && docker compose rm -f copilot`, then remove `compose.copilot.yaml` from `COMPOSE_FILE`.
4. To end the login, remove `state/copilot/access-token` and an old `state/copilot/api-key.json`, and revoke the authorization of "GitHub Copilot Plugin" in the application settings of the GitHub account.
