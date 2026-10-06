# Anthropic credential

The route `anthropic/*` sends requests to Anthropic with a separate credential.
The credential is an Anthropic API key or a separate OAuth token of a subscription.
`scripts/enable-anthropic.py` installs it.

## Permission

LiteLLM 1.101.0 recognizes the prefixes of Anthropic OAuth tokens and sets a bearer authorization header.
Not verified: the same behavior in LiteLLM 1.103.0, the version of the image.
This technical support does not give permission to use a subscription through a third-party tool.
Use an Anthropic API key if your subscription does not permit this use.

## Install the credential

Do not copy an existing Claude Code OAuth token. The installer needs no refresh token.

1. For a separate subscription token, run `claude setup-token` with its own configuration directory. For an API key, skip this step.

   ```sh
   mkdir -p secrets/claude-login
   chmod 700 secrets/claude-login
   CLAUDE_CONFIG_DIR="$PWD/secrets/claude-login" claude setup-token
   ```

2. Install the credential. The script reads it through a hidden prompt, writes `ANTHROPIC_API_KEY` to `.env` with mode 0600 and adds the route `anthropic/*` to the host gateway file.

   ```sh
   python3 scripts/enable-anthropic.py
   ```

   Warning: do not paste the credential into a shell argument or a chat.

3. Recreate the gateway. The gateway reads `.env` only when the container starts.

   ```sh
   docker compose up -d --force-recreate gateway
   ```

4. Test an available model. Replace `MODEL_ID` with an Anthropic model ID.

   ```sh
   python3 scripts/verify.py --model anthropic/MODEL_ID
   ```

The script accepts only a credential that starts with `sk-ant-` and has no white space.

Not verified: inference with a subscription token, and the permission of the provider.
The replacement of an OAuth token is manual. No refresh token is shared.
