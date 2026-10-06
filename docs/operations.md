# Operations

This page covers secrets, backup and recovery, and the update of the LiteLLM base image.

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

A dump of the database while the stack runs:

```sh
docker compose exec -T database pg_dump -U litellm -d litellm > litellm.sql
```

Not verified: this command in the current layout, and a restore of its output.
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

2. Verify the signature of the release with the cosign key of upstream, when cosign is available.
   Not verified: the signature of the digest that is pinned now.
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

7. Build the image and start the stack. Test the authentication and one inference on each provider that you use.

   ```sh
   docker compose up -d --build
   python3 scripts/verify.py --model MODEL_ID
   ```

8. For the Codex routes, test xhigh chat and streaming tool calls ([codex-services.md](codex-services.md)).

A deployment that uses a released image sets `GATEWAY_IMAGE` to the new digest and runs `docker compose up -d` ([host-configuration.md](host-configuration.md)).

Warning: a downgrade of LiteLLM can meet a database that a newer version migrated. Not verified: the database schema of two versions is compatible.
