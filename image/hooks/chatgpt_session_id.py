"""LiteLLM 1.101.0 and 1.103.0: a stable ChatGPT session id for prompt-cache affinity.

Codex routes a request to a prompt-cache server by its session_id header.
Upstream sets the header from litellm_session_id, session_id or
metadata.session_id, then from litellm_trace_id or litellm_call_id. The proxy
makes the last two new for each request, so a conversation without a client
id goes to another cache server with each request.

The hook adds one step before the proxy pre-call hooks. It acts only when
each deployment of the requested model group is a chatgpt deployment, and
only on /v1/chat/completions and /v1/responses. A request for another
provider stays as it is. The step sets litellm_session_id, which LiteLLM
does not send in the provider request body:
- to the session id of the client, when the client sent one in a key that
  upstream does not read on this endpoint (SESSION_KEYS);
- else to the id that cache_session_id() derives from the conversation prefix.
A request with litellm_session_id stays as it is. The step never fails a
request: after an error in it the request goes on unchanged. The hook reads no
token and logs nothing. Remove it when upstream gives a conversation a stable id.
"""
import hashlib
import json

from litellm_versions import TESTED_VERSIONS, untested  # noqa: F401  (TESTED_VERSIONS for the tests)

CALL_TYPES = ("acompletion", "aresponses")
# Where a client can put its session id, after litellm_session_id: (holder, key).
SESSION_KEYS = (("extra_body", "litellm_session_id"), (None, "session_id"),
                ("metadata", "session_id"), ("litellm_metadata", "session_id"))


def cache_session_id(payload):
    """Stable ChatGPT session id for prompt-cache affinity.

    Codex routes a request to a prompt-cache server by its session_id header. The
    account proxies set that header from litellm_session_id and otherwise use a
    random id per request, which spreads one conversation over many servers and
    lowers the cache-read rate. Keep a client id (top level or in extra_body);
    derive one from the stable conversation prefix when the client sends none.
    """
    explicit = payload.get("litellm_session_id")
    extra = payload.get("extra_body")
    if not explicit and isinstance(extra, dict):
        explicit = extra.get("litellm_session_id")
    if explicit:
        return str(explicit)
    prefix = [payload.get("model")]
    messages = payload.get("messages")
    if isinstance(messages, list):
        prefix.append([m for m in messages if isinstance(m, dict) and m.get("role") in ("system", "developer")])
        prefix.append(next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"), None))
    else:
        prefix.append(payload.get("instructions"))
        items = payload.get("input")
        if isinstance(items, list):
            prefix.append(next((i for i in items if isinstance(i, dict) and i.get("role") == "user"), None))
        else:
            prefix.append(items)
    digest = hashlib.sha256(json.dumps(prefix, sort_keys=True, default=str).encode()).hexdigest()
    return "prefix-" + digest[:32]


def client_session_id(data):
    """The session id that the client sent outside litellm_session_id, or None."""
    for holder, key in SESSION_KEYS:
        values = data if holder is None else data.get(holder)
        if isinstance(values, dict) and values.get(key):
            return str(values[key])
    return None


def is_chatgpt_group(llm_router, model):
    """True when the model group has deployments and each one is a chatgpt deployment."""
    if llm_router is None or not isinstance(model, str):
        return False
    try:
        deployments = llm_router.get_model_list(model_name=model)
    except Exception:
        return False  # The step must not fail a request; the request stays as it is.
    if not deployments:
        return False
    for deployment in deployments:
        params = deployment.get("litellm_params") or {}
        name = params.get("model")
        if params.get("custom_llm_provider") != "chatgpt" and not (
                isinstance(name, str) and name.startswith("chatgpt/")):
            return False
    return True


def set_session_id(data, llm_router):
    """Give a request for a chatgpt model group its session id. Return True when data changed."""
    if data.get("litellm_session_id") or not is_chatgpt_group(llm_router, data.get("model")):
        return False
    try:
        session_id = client_session_id(data) or cache_session_id(data)
    except Exception:
        return False  # As above: the request goes on without the id.
    data["litellm_session_id"] = session_id
    return True


def install():
    problem = untested()
    if problem:
        raise SystemExit(problem)
    from litellm.proxy.utils import ProxyLogging

    if getattr(ProxyLogging, "_gateway_session_id", False):
        return
    original = ProxyLogging.pre_call_hook

    # pre_call_hook runs after the proxy has read the session headers of the
    # client into the body, and before the router makes litellm_trace_id.
    async def pre_call_hook(self, user_api_key_dict, data, call_type, *args, **kwargs):
        if isinstance(data, dict) and call_type in CALL_TYPES:
            from litellm.proxy import proxy_server

            set_session_id(data, proxy_server.llm_router)
        return await original(self, user_api_key_dict, data, call_type, *args, **kwargs)

    ProxyLogging.pre_call_hook = pre_call_hook
    ProxyLogging._gateway_session_id = True
