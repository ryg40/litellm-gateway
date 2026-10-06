# Host configuration

The tracked files hold a generic stack. They hold no private address, no private domain, no host path,
no external network and no host model list. Each host keeps its values in `.env` and in `.local/`.
Git ignores both.

## Default: a new clone

1. `python3 scripts/create-env.py`. It makes `.env` from `.env.example` with a new random value for
   `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`, `POSTGRES_PASSWORD`, `UI_PASSWORD`, `CODEX_MASTER_KEY` and `COPILOT_MASTER_KEY`,
   mode 0600. It refuses to replace a `.env` that exists. Then add the provider keys that you use.
   Compose refuses to start while one of the first four is empty (`${VAR:?}` in `compose.yaml`).
   Compose cannot refuse a placeholder value, for example `REPLACE_WITH_...` of an old `.env.example`;
   `scripts/verify.py` stops when `.env` holds `REPLACE_WITH_`.
2. `docker compose up -d --build`. Compose builds the gateway image `litellm-gateway:local` from `Dockerfile`
   and starts two services, `gateway` and `database`, on the project network only.
3. `curl http://127.0.0.1:4321/health/liveliness`. The result is `"I'm alive!"`.

The build needs no other tool than Docker with Compose and BuildKit. It works on Linux.
Not verified: the build and the start on a Mac ([mac.md](mac.md)).

With no host settings, the gateway loads `config/gateway.example.yaml`: one OpenRouter wildcard route
and one local OpenAI-compatible route, `local/example-model`, that reads its address from `LOCAL_API_BASE`.

## Host settings

| Variable (in `.env`) | Default | Use |
| --- | --- | --- |
| `GATEWAY_CONFIG` | `./config/gateway.example.yaml` | Gateway configuration that Compose mounts. A host uses `./.local/config/gateway.yaml`. |
| `COPILOT_CONFIG` | `./config/copilot.yaml` | Configuration of the service `copilot` ([copilot.md](copilot.md)). A host uses `./.local/config/copilot.yaml`. |
| `GATEWAY_PORT` | `127.0.0.1:4321` | Published address of the gateway. `scripts/verify.py` uses the same value. |
| `LOCAL_API_BASE` | none | Address of the example local route. |
| `LOCAL_ENDPOINTS` | none | Endpoints for `scripts/sync-local-models.py`, for example `llamaswap=http://host:9292/v1,vllm=http://host:8001/v1`. |
| `LOCAL_EMBEDDING_MODEL`, `LOCAL_EMBEDDING_BASE` | none | Optional dedicated embedding route. Set both or neither. |
| `LOCAL_EMBEDDING_PROVIDER` | the first endpoint | Endpoint name that gets the embedding route. |
| `GATEWAY_IMAGE` | `litellm-gateway:local` | Image of `gateway`, `codex1`, `codex2`, `codex3`, `codex-router` and `copilot`. A deployment sets a registry name with a digest. |
| `BASE_IMAGE` | the pin in `Dockerfile` | Upstream LiteLLM image of a local build (`--build`). |
| `COMPOSE_FILE` | `compose.yaml` | Compose files. A host adds its override file here. `.env.example` has no such line. |

The scripts read each variable from the environment first, then from `.env`.

## Gateway image

All services except `database` use `${GATEWAY_IMAGE:-litellm-gateway:local}`. `docs/image.md` describes the image.
The hooks and routers are in the image. Compose mounts only the configuration files, `state/` and the database volume.

- Local build: leave `GATEWAY_IMAGE` unset. `docker compose up -d --build` or `docker compose build` builds the
  target `gateway` of `Dockerfile`. Only the service `gateway` has a `build:` section; the Codex services use its image.
- Released image: set `GATEWAY_IMAGE`, for example `ghcr.io/owner/litellm-gateway@sha256:<index digest>`,
  then `docker compose up -d` without `--build`. Compose pulls the image when it is missing.
  With `--build`, Compose builds the local source and tries to give it this name; a name with a digest then fails.

Do not set `PYTHONPATH` in a Compose file. The value of the image, `/opt/litellm-gateway/image/hooks`,
loads `sitecustomize.py`. Another value turns the hooks off without an error.

### Test a changed hook without a new build

Mount the hook directory of the checkout over the image path in an override file under `.local/`.
Do not put this mount in a tracked Compose file.

```yaml
# .local/compose.dev.yaml
services:
  gateway:
    volumes:
      - ./image/hooks:/opt/litellm-gateway/image/hooks:ro
```

```sh
docker compose -f compose.yaml -f .local/compose.dev.yaml up -d --force-recreate gateway
```

The files must be readable for the user `1000:1000`. For the router, mount `./image/routers` over
`/opt/litellm-gateway/image/routers` in `codex-router`. Recreate the service after each change.
Remove the override and build the image before a deployment.

## The host gateway file

`.local/config/gateway.yaml` is the gateway configuration of a host. It uses JSON syntax, which is valid YAML,
because the scripts read and write it with the Python `json` module.

1. `mkdir -p .local/config && cp config/gateway.example.yaml .local/config/gateway.yaml`.
2. Add `GATEWAY_CONFIG=./.local/config/gateway.yaml` to `.env`.
3. Edit the file, or let the scripts fill it:
   - `scripts/sync-local-models.py` replaces the routes of each local endpoint.
   - `scripts/enable-codex.py` adds the Codex routes of `config/gateway.codex.example.yaml`.
   - `scripts/enable-anthropic.py` adds the `anthropic/*` route.
4. `docker compose up -d --force-recreate gateway`. A mounted file needs a recreate, not a restart.

The scripts write only the file that `GATEWAY_CONFIG` names, with `.local/config/gateway.yaml` as the default.
If the file does not exist, they start from `config/gateway.example.yaml`.
They stop if `GATEWAY_CONFIG` points into `config/`, so that they never change a tracked file.

`scripts/sync-local-models.py` with no endpoint stops with exit code 1 and changes no file.

## Host override file

`compose.yaml` has no external network and no fixed container name. A host that serves the gateway through
a reverse proxy adds them in `.local/compose.host.yaml`:

```yaml
services:
  gateway:
    # A fixed name permits only one stack with this file on the host.
    container_name: litellm
    networks:
      proxy:
        aliases: [litellm]

networks:
  proxy:
    external: true
```

Add the file to `.env`, so that each `docker compose` command uses it:

```sh
COMPOSE_FILE=compose.yaml:.local/compose.host.yaml
```

With the Codex services: `COMPOSE_FILE=compose.yaml:compose.codex.yaml:.local/compose.host.yaml`.
With the Copilot service: `COMPOSE_FILE=compose.yaml:compose.copilot.yaml:.local/compose.host.yaml`. It does not need the Codex services.
With both: `COMPOSE_FILE=compose.yaml:compose.codex.yaml:compose.copilot.yaml:.local/compose.host.yaml`.
Keep the host override file last: a later file changes the values of an earlier one.

`docs/examples/Caddyfile.fragment` is an example route for Caddy on the network `proxy`.
Replace `gateway.example.com` with the public host name.

Warning: keep `name: litellm` in `compose.yaml` and do not use `-p` on a host with an existing installation.
The database volume is `litellm_postgres-data`. Another project name makes a new, empty volume.

## Codex services

`compose.codex.yaml` is off by default. Start it with `docker compose -f compose.yaml -f compose.codex.yaml up -d`,
or add it to `COMPOSE_FILE`. Its routes in the gateway are in `config/gateway.codex.example.yaml`.
`scripts/enable-codex.py` copies them into the host gateway file after the account logins.

## Copilot service

`compose.copilot.yaml` is off by default. It adds the service `copilot` for the models of one GitHub Copilot account.
Add it to `COMPOSE_FILE` only after the login: [copilot.md](copilot.md) gives the order.
Its routes in the gateway are in `config/gateway.copilot.example.yaml`.
`COPILOT_CONFIG` in `.env` names a host copy of `config/copilot.yaml`, for example `./.local/config/copilot.yaml`.

## Pass-through routes and the decision router

Pass-through routes and the decision router use services that are not part of this stack.
`docs/decision-routes.md` and `docs/decision-router.md` have generic examples to add to the host files.

## Move a tracked gateway file to `.local/`

If you keep the gateway configuration in `config/gateway.yaml`, move it to `.local/` before the first deploy:

1. Copy the current gateway configuration to `.local/config/gateway.yaml`.
2. Add `GATEWAY_CONFIG=./.local/config/gateway.yaml` and `COMPOSE_FILE=...` (see above) to `.env`.
3. Create `.local/compose.host.yaml` as shown above.
4. Build the image (`docker compose build`) or set `GATEWAY_IMAGE` to a released image.
   The services then run the hooks and the router from the image, not from bind mounts.
5. Compare `docker compose config` with the old output before a recreate.
