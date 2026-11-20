"""Preserve explicit GPT capability ids across the 1.109 router registration change.

From 1.109 the router registers a deployment id with add_to_provider_model_sets=False.
1.110.2 keeps this in register_deployment_in_model_cost() of
litellm/router_rust/surface/registry.py, which looks up litellm.register_model at call time.
"""
from functools import wraps
from importlib.metadata import version

# The versions with that registration whose model-info tests need this hook.
VERSIONS = ("1.109.1", "1.110.2")


def install():
    if version("litellm") not in VERSIONS:
        return
    import litellm

    original = litellm.register_model
    if getattr(original, "_gateway_capability_ids", False):
        return

    @wraps(original)
    def register_model(model_cost, **kwargs):
        result = original(model_cost, **kwargs)
        if kwargs.get("add_to_provider_model_sets") is False and isinstance(model_cost, dict):
            # These existing configurations use a model name as the explicit id.
            # Never register opaque ids, or move deployment pricing to a shared key.
            named = {
                key: info for key, info in model_cost.items()
                if isinstance(key, str) and key.startswith(("gpt-", "responses/gpt-"))
                and isinstance(info, dict) and info.get("id") == key
                and info.get("litellm_provider") == "openai"
                and info.get("supports_reasoning") is True
            }
            if named:
                litellm.add_known_models(named)
                for key, info in named.items():
                    if not key.startswith("responses/"):
                        continue
                    bare = key.removeprefix("responses/")
                    if bare in litellm.model_cost:
                        continue
                    # The new chat bridge checks the bare name before Responses.
                    # Copy capabilities only; routing and prices stay unchanged.
                    capabilities = {k: v for k, v in info.items() if k.startswith("supports_")}
                    original({bare: {**capabilities, "litellm_provider": "openai", "mode": "chat"}},
                             persist_across_reloads=False)
        return result

    register_model._gateway_capability_ids = True
    litellm.register_model = register_model
