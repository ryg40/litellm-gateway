#!/usr/bin/env python3
"""Refresh local model IDs from live endpoints. Keep cloud routes unchanged.

Endpoints come from arguments or from LOCAL_ENDPOINTS (environment or .env),
for example LOCAL_ENDPOINTS=llamaswap=http://host:9292/v1,vllm=http://host:8001/v1.
The optional dedicated embedding route needs LOCAL_EMBEDDING_MODEL and
LOCAL_EMBEDDING_BASE; it goes to LOCAL_EMBEDDING_PROVIDER (default: the first endpoint).
The script writes the host file only (see scripts/gateway_config.py).
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.request

# Import the helper without a __pycache__ directory in the checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gateway_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def parse_endpoints(values):
    endpoints = {}
    for value in values:
        for item in filter(None, (part.strip() for part in value.split(","))):
            name, sep, base = item.partition("=")
            if not sep or not name or not base:
                raise SystemExit(f"Endpoint {item!r} is not in the form name=url")
            endpoints[name] = base.rstrip("/")
    return endpoints


def options(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", action="append", default=[], metavar="NAME=URL",
                        help="OpenAI-compatible base URL, for example llamaswap=http://host:9292/v1")
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-base")
    parser.add_argument("--embedding-provider")
    args = parser.parse_args(argv)
    endpoints = parse_endpoints(args.endpoint or [gateway_config.setting(ROOT, "LOCAL_ENDPOINTS") or ""])
    if not endpoints:
        raise SystemExit("No local endpoint is set. Give --endpoint NAME=URL or set LOCAL_ENDPOINTS. "
                         "No file was changed.")
    model = args.embedding_model or gateway_config.setting(ROOT, "LOCAL_EMBEDDING_MODEL")
    base = args.embedding_base or gateway_config.setting(ROOT, "LOCAL_EMBEDDING_BASE")
    if bool(model) != bool(base):
        raise SystemExit("Set both the embedding model and the embedding base, or neither. No file was changed.")
    provider = (args.embedding_provider or gateway_config.setting(ROOT, "LOCAL_EMBEDDING_PROVIDER")
                or next(iter(endpoints)))
    if model and provider not in endpoints:
        raise SystemExit(f"Embedding provider {provider!r} is not an endpoint. No file was changed.")
    embedding = (provider, model, base.rstrip("/")) if model else None
    return endpoints, embedding


def main(argv=None):
    endpoints, embedding = options(argv)
    path, config = gateway_config.load(ROOT)
    for provider, base in endpoints.items():
        # Fail before writing when any endpoint is unavailable.
        with urllib.request.urlopen(base + "/models", timeout=15) as response:
            models = json.load(response)["data"]
        if not models:
            raise RuntimeError(f"{provider} returned no models; keep current configuration")
        embedding_model = embedding_base = None
        if embedding and embedding[0] == provider:
            _, embedding_model, embedding_base = embedding
            with urllib.request.urlopen(embedding_base + "/models", timeout=15) as response:
                dedicated_models = json.load(response)["data"]
            if embedding_model not in {model["id"] for model in dedicated_models}:
                raise RuntimeError("Dedicated embedding model is unavailable; keep current configuration")
            models = [model for model in models if model["id"] != embedding_model]
            models.append({"id": embedding_model})
        config["model_list"] = [m for m in config["model_list"]
                                if not m["model_name"].startswith(provider + "/")]
        for model in sorted(models, key=lambda m: m["id"]):
            model_id = model["id"]
            entry = {
                "model_name": provider + "/" + model_id,
                "litellm_params": {
                    "model": "openai/" + model_id,
                    "api_base": embedding_base if model_id == embedding_model else base,
                    "api_key": "os.environ/LOCAL_API_KEY",
                },
            }
            if "embedding" in model_id.lower():
                entry["model_info"] = {"mode": "embedding"}
            config["model_list"].append(entry)
        print(f"{provider}: {len(models)} models")
    gateway_config.save(ROOT, path, config)
    print("Run docker compose up -d --force-recreate gateway to load the new file.")


if __name__ == "__main__":
    main()
