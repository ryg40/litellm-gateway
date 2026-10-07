# LiteLLM gateway

This repo builds and runs an OpenAI-compatible gateway with LiteLLM.
The gateway image is a local build on the LiteLLM 1.103.0 image, pinned by index digest.
The image adds the hooks and routers of this repo (`image/hooks/`, `image/routers/`).
The stack has two services: `gateway` and a Postgres `database`.
Two sets of optional services are off by default: Codex account services in `compose.codex.yaml`
([docs/codex-services.md](docs/codex-services.md)) and a GitHub Copilot service in `compose.copilot.yaml` ([docs/copilot.md](docs/copilot.md)).

Each host keeps its own values in `.env` and `.local/`. Git ignores both.
The tracked files hold no secret and no host value.

## Quick start

[EXPLAINER.md](EXPLAINER.md) explains each step below and lists each file, container and port that the step touches.

You need Docker with Compose v2 and Buildx, `git`, `sh`, `python3` (3.9 or later) and Node.js 24.
On a Mac, Podman replaces Docker Desktop ([docs/mac.md](docs/mac.md)); the other hosts use Docker.
`sh scripts/check-prereqs.sh` checks each tool and stops with exit code 1 when a hard requirement is missing.

| Tool | Minimum | Command that needs it |
| --- | --- | --- |
| Git | 2.24 | The `pre-merge-commit` hook that `scripts/install-hooks.sh` installs; `scripts/scan.sh` alone needs 2.5 |
| Docker Compose | 2.35.0 | `docker compose config --no-env-resolution` in `scripts/login-codex-account.sh` |
| Buildx | 0.19 | `docker buildx bake --allow fs.write=...` in `scripts/build.sh --oci` |
| Node.js | major version 24 | The build pipelines run on Node 24 ([docs/ci.md](docs/ci.md)). `scripts/check-prereqs.sh` refuses another major version. |

Tested with Git 2.39.5, Compose 5.5.0 and Buildx 0.36.1. Not verified: the minimum versions themselves;
they come from the upstream release where each option first appears.
On a Mac with Apple silicon, [docs/mac.md](docs/mac.md) gives the runtime steps. Each Homebrew formula that it names has an `arm64` bottle.

1. Clone the repo.

   ```sh
   git clone <url of the repo> litellm
   cd litellm
   ```

2. Create the fetch-only remote `upstream`. A clone does not copy the remote configuration.

   ```sh
   scripts/setup-remotes.sh
   ```

3. Create `.env`. The script gives the master key, the salt key, the database password, the UI password,
   `CODEX_MASTER_KEY` and `COPILOT_MASTER_KEY` a new random value, and writes mode 0600. It does not replace a `.env` that exists.

   ```sh
   python3 scripts/create-env.py
   ```

   Add the provider keys that you use, for example `OPENROUTER_API_KEY`.
   Do not copy `.env.example` to `.env`: Compose refuses to start with its empty secrets.

4. Build the image and start the stack.

   ```sh
   docker compose up -d --build
   ```

5. Check the gateway. The result is `"I'm alive!"`.

   ```sh
   curl http://127.0.0.1:4321/health/liveliness
   ```

6. Check the authentication and the model list with the key from `.env`.

   ```sh
   python3 scripts/verify.py
   ```

7. Run the tests and the scan.

   ```sh
   scripts/build.sh test
   scripts/scan.sh tree
   ```

The default configuration is `config/gateway.example.yaml`.
A public address, for example `https://gateway.example.com`, needs a reverse proxy ([docs/host-configuration.md](docs/host-configuration.md)).

## Branches

- `upstream` is upstream LiteLLM. It is fetch only.
- `local-dev` is the private integration branch of a host. It never goes to a shared remote.
- `topic/*` branches hold one change each and merge into `local-dev`.
- `portable` has no shared history with `local-dev`. Each promotion is one snapshot commit.
- Tags `portable-v*` mark releases of `portable`. Images build from these tags.

## Documents

| Document | Content |
| --- | --- |
| [EXPLAINER.md](EXPLAINER.md) | Installation explainer: each install step with a drill-down, the components, how to remove the stack |
| [docs/branches.md](docs/branches.md) | Branches, tags and which remote gets which branch |
| [docs/remotes.md](docs/remotes.md) | The remote `upstream` and a comparison of upstream releases |
| [docs/host-configuration.md](docs/host-configuration.md) | `.env` settings, the host gateway file, the host override file |
| [docs/image.md](docs/image.md) | Image content, build targets, variables, tags, `model_info` entries for a new model |
| [docs/secret-handling.md](docs/secret-handling.md) | The scan for secrets and host values, Git hooks |
| [docs/mac.md](docs/mac.md) | The stack on a Mac: Podman as the Docker Desktop replacement, Colima as the alternative, TLS inspection |
| [docs/ci.md](docs/ci.md) | What a CI pipeline does |
| [docs/operations.md](docs/operations.md) | Secrets, backup and recovery, base-image update |
| [docs/codex-services.md](docs/codex-services.md) | Up to three Codex account services, logins, a new login with `scripts/reauth-codex.sh`, `codex-router` (supported design) |
| [docs/codex-accounts.md](docs/codex-accounts.md) | More than one ChatGPT account in one process (prototype, off by default) |
| [docs/anthropic.md](docs/anthropic.md) | The separate Anthropic credential |
| [docs/copilot.md](docs/copilot.md) | GitHub Copilot models: the service `copilot`, login, token directory, `copilot/*` routes |
| [docs/decision-routes.md](docs/decision-routes.md) | Pass-through routes to decision services |
| [docs/decision-router.md](docs/decision-router.md) | The prototype router for the virtual model `auto` |
| [tests/README.md](tests/README.md) | Test commands |

Warning: `.env`, `secrets/` and `state/` hold credentials and tokens. Do not add them to Git. Back them up separately ([docs/operations.md](docs/operations.md)).
