# Decision-model routes

A pass-through route (`general_settings.pass_through_endpoints`) exposes a backend behind the normal
LiteLLM key. Clients never hold the backend key.

The routes are not in `config/gateway.example.yaml`. Route configuration belongs to the host.
A host adds its routes to its gateway file (`GATEWAY_CONFIG`, see `docs/host-configuration.md`).
A private backend must be on a network that the gateway joins, for example a network that
`.local/compose.host.yaml` adds to the gateway.

This document shows one generic example with two routes:

| Route | Target | Backend key (in `.env`) |
|---|---|---|
| `/decision/*` | `http://decision-service:8000` (a private decision service of the host) | `DECISION_API_KEY` |
| `/public-llm/*` | `https://api.example.com` (a paid public service, leaves the network) | `PUBLIC_LLM_API_KEY` |

Both routes use `auth: true` (LiteLLM key required) and `include_subpath: true`.
The client `Authorization` header is not forwarded; LiteLLM sets the backend key.

```bash
curl -s https://gateway.example.com/decision/v1/systemone \
  -H "Authorization: Bearer $LITELLM_KEY" -H 'Content-Type: application/json' \
  -d '{"state":"Disk /var/log at 98%","questions":{"act":{"type":"noul","instructions":"Needs intervention?"}}}'
```

Example for `general_settings` of the host gateway file. Add only the routes whose backends exist:

```json
"pass_through_endpoints": [
  {"path": "/decision", "target": "http://decision-service:8000", "include_subpath": true, "auth": true, "timeout": 30,
   "headers": {"Authorization": "Bearer os.environ/DECISION_API_KEY", "Content-Type": "application/json"}},
  {"path": "/public-llm", "target": "https://api.example.com", "include_subpath": true, "auth": true, "timeout": 30,
   "headers": {"Authorization": "Bearer os.environ/PUBLIC_LLM_API_KEY", "Content-Type": "application/json"}}
]
```

Warning: `/public-llm` sends the request to a public service outside the network. Do not send private data to it.

Notes:

- The decision routes are pass-throughs, not `model_list` entries. `/v1/chat/completions` cannot call them.
- Not verified: LiteLLM spend tracking for generic pass-through routes. Track the spend of a paid service in its own console.
- Changing a key in `.env` needs `docker compose up -d gateway` (recreate), not a restart.
