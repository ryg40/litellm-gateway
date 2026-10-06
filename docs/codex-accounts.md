# More than one ChatGPT account in one gateway process (prototype)

Status: prototype with offline tests. Off by default.
Not verified: requests to ChatGPT with this prototype.

This page is the second of two designs. The supported design runs one LiteLLM process for each account:
the services `codex1`, `codex2`, `codex3` and `codex-router` of `compose.codex.yaml` ([codex-services.md](codex-services.md)).
This prototype is off by default and changes nothing in the supported design.

LiteLLM 1.101.0 and 1.103.0 support one ChatGPT account for each process.
The hook `image/hooks/chatgpt_auth_file.py` lets each `chatgpt` deployment name its own auth file.
The key `chatgpt_auth_file` in `litellm_params` and its behavior follow open upstream pull request #41928.
Remove the hook when the pinned LiteLLM has the key. The hook stops the start when it finds the key upstream.

## Switch

The hook is off by default. It changes LiteLLM only when the environment of the process sets
`CHATGPT_AUTH_FILE_HOOK` to `on`, `1`, `true` or `yes`.
With the hook off, `install()` returns before the version check and replaces no upstream object.
Each process of the image loads `sitecustomize.py`: the gateway, `codex1`, `codex2`, `codex3` and the login container.
Only the gateway that uses the key needs the switch on.

The tracked Compose files do not set the switch. A test stack sets it in an override file under `.local/`.
The Compose service sets the switch. `environment_variables` in the proxy configuration is too late:
LiteLLM applies that section when it loads the configuration, after `sitecustomize.py` has run.
`CHATGPT_AUTH_FILE_REQUIRED` is read for each request, so `environment_variables` would work for it.
With the hook on, it is `true` when it is not set. Set both in Compose, in one place:

```yaml
# .local/compose.accounts.yaml, only for a test stack
services:
  gateway:
    environment:
      CHATGPT_AUTH_FILE_HOOK: "on"
      CHATGPT_AUTH_FILE_REQUIRED: "true"
      CHATGPT_TOKEN_DIR: /tokens
    volumes:
      - ./state/codex-accounts:/tokens
```

Start the test stack with its own project name, so that it gets its own volume:

```sh
docker compose -p litellm-accounts-test -f compose.yaml -f .local/compose.accounts.yaml up -d --build
```

The gateway configuration of the test stack must name the `chatgpt` deployments with the key
(`config/gateway.codex-accounts.example.yaml`). Set `GATEWAY_CONFIG` and `GATEWAY_PORT` for it.

### Guard with the hook off

With the hook off, upstream LiteLLM ignores `chatgpt_auth_file` and uses the process account.
It can then start the device-code login in the gateway.
Guard: with the hook off, `sitecustomize.py` reads the configuration file of a proxy start
(`--config`, `-c` or `CONFIG_FILE_PATH`) and its `include` files.
When a deployment in `model_list` has the key, the interpreter stops at start with a message that names the switch.
The guard replaces no upstream function.

Cost and limits of the guard:

- One read and one YAML parse of the configuration at each start of a process with `--config`. Other processes, for example the health check, read nothing.
- It does not see models from the database or the UI (`store_model_in_db`), models added through the API while the proxy runs, or a configuration from a bucket.
- A file that it cannot read or parse does not stop the start; LiteLLM reports that file itself.

Warning: a `chatgpt` deployment with the key that comes from the database or the API is not guarded.
With the hook off, it uses the process account.

## How the hook works

- `chatgpt_auth_file` is a path relative to the allowed directory, for example `codex1/auth.json`.
  An absolute path is permitted when it is inside the allowed directory.
- The allowed directory is `CHATGPT_AUTH_FILE_ROOT`. When it is not set, it is `CHATGPT_TOKEN_DIR`.
  The hook refuses a path outside it, a path with `..`, and a symlink that leaves it.
- The hook keeps one authenticator for each auth file. A refresh writes only that file, atomically, with mode 0600.
- `get_llm_provider()` reads no token. The token comes from the deployment in `validate_environment()`.
  Thus the gateway needs no process account.
- The hook never starts the device-code login. A missing, invalid or unrefreshable auth file fails the request at once.
  The error for the client names the deployment and the account, for example
  `chatgpt deployment gpt-6-luna (id x), account codex1: auth file is missing or not valid`.
  It names no token and no path. The warning in the gateway log also names the path of the auth file.
  The account is the directory of the auth file (`codex1` for `codex1/auth.json`).
- `CHATGPT_AUTH_FILE_REQUIRED` is on by default when the hook is on: a `chatgpt` deployment without the key fails.
  `CHATGPT_AUTH_FILE_REQUIRED=false` (also `0`, `no`, `off`) lets such a deployment use the upstream process account.
  The account proxies `codex1`, `codex2` and `codex3` run with the hook off, so the setting does not apply to them.
- The account headers `Authorization` and `ChatGPT-Account-Id` always win over client headers.
  LiteLLM merges `extra_headers` and `headers` of the request body into the request headers
  after `validate_environment()`. The hook returns a header object that sets the account values again after each merge,
  and `sign_request()` sets them once more as the last step before the request goes out.
  When the header object is lost, `sign_request()` fails the request.
- The proxy rejects a client request that has `chatgpt_auth_file` anywhere in the body with HTTP 400.
  `allow_client_side_credentials` does not change this.
  The check also reads a string as JSON text, because LiteLLM reads some fields (`metadata`, `extra_body`) from JSON text.
  Exception: the prompt fields at the top level of the body (`messages`, `input`, `prompt`, `instructions`, `system`).
  LiteLLM sends them to the model and does not read them as parameters, so a prompt that quotes the key passes.
  A key name in a dictionary is refused in the prompt fields too.
- A value that nests deeper than 32 levels, or a JSON text that is too deep for the parser, gives HTTP 400.
  The check cannot read such a value, so it refuses it.
- On a LiteLLM version that is not in `TESTED_VERSIONS` of `image/hooks/litellm_versions.py` (1.101.0 and 1.103.0),
  `sitecustomize.py` stops the Python interpreter at start, with the switch on or off.
  `chatgpt_auth_file.install()` checks the same list.

Warning: do not set `CHATGPT_AUTH_FILE_REQUIRED=false` in a gateway that uses the key.
Then a code path that loses the key uses the process account and can start the device-code login.

## Deployment requirements (not done in this branch)

1. Use the gateway image (`docs/image.md`). It contains `chatgpt_auth_file.py` next to `sitecustomize.py`,
   and the Compose services use it. No hook file is mounted.
2. Set the switch, `CHATGPT_AUTH_FILE_REQUIRED`, `CHATGPT_TOKEN_DIR` and the mount of `./state/codex-accounts`
   in the gateway service (section "Switch").
3. Use `config/gateway.codex-accounts.example.yaml` as the model for the `chatgpt` deployments.

## Login of one account

```
sh scripts/login-codex-account.sh codex1
```

The script starts one short-lived container. The image is `LITELLM_IMAGE`, when it is set.
Otherwise it is the image of the service `gateway` in
`docker compose -f compose.yaml config --no-env-resolution --format json`.
This gives the image after Compose interpolation, whatever its name: `GATEWAY_IMAGE`, or `litellm-gateway:local`
when it is not set. Build that image first (`docker compose build`) or set `GATEWAY_IMAGE` to a released image.
For interpolation, Compose can read the project `.env`; it does not load the env files of the services.
Without a `.env` with its secrets, Compose refuses to render `compose.yaml` (`${VAR:?}`); then set `LITELLM_IMAGE`.
The script uses only the image name from the output.
The script needs `python3` on the host for this step.
The container has no env file and no project network. It sets `CHATGPT_AUTH_FILE_HOOK=off`.
The image may load the hooks; the login script stops only when the hook is active.
It runs `scripts/login-codex-account.py`, which does the upstream device-code login.
The auth file goes to `state/codex-accounts/<account>/auth.json` with mode 0600.
The directory gets mode 0700. As root, the script gives the directory to `1000:1000`, the user of the proxies.
`CODEX_ACCOUNTS_DIR`, `CODEX_ACCOUNT_USER`, `LITELLM_IMAGE` and `DOCKER` change the defaults.

## Routing with native LiteLLM features

The example has two deployments in the model group `gpt-6-luna`: `codex2` with `order: 1` and `codex1` with `order: 2`.

- The router sends a request to `order: 1` first.
- On a failure, HTTP 429 included, it sends the same request to `order: 2`. This works with `num_retries: 0`.
- A 429 puts the deployment into cooldown at once, because the group has two deployments.
  The cooldown time is fixed (`cooldown_time`, 60 s in the example, 5 s by default).
  During the cooldown, requests go to `order: 2` directly.

Functions of `image/routers/quota_router.py` with no native replacement:

| Function | What LiteLLM does instead |
|---|---|
| `Quota.usable()` with `exhausted_until()`: asks `wham/usage` before a request and skips an account until its quota window resets | Nothing. The router learns about a limit only from a failed request. |
| `Quota.rate_limited()`: blocks the account until `resets_at` or `Retry-After` of the 429, bounded by its usage windows | A fixed `cooldown_time`. The router does not read the reset time. |
| The 409 answer in `proxy()` for `previous_response_id` and `conversation` | Partly: the optional pre-call check `DeploymentAffinityCheck` with Responses API affinity sends a `previous_response_id` to the deployment that made the response. Not verified: what it does when that deployment is in cooldown, and for `conversation`. |

## Tests

`tests/test_chatgpt_auth_file.py` runs in the LiteLLM image with the command in `tests/README.md`.
