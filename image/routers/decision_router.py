"""LiteLLM pre-call hook: route the virtual model "auto" with a decision model.

For a chat request with model "auto", the hook sends the end of the last user message to
the decision service (/v1/systemone) with three questions: route (choice), hard (noul), private (noul).
It then sets data["model"] from the route table in decision-routes.yaml.

Safety rules, in order:
- An image part in the message goes to image_model; no decision call.
- Timeout, error or low route confidence: default_model.
- private >= threshold: never a model with a remote prefix (openrouter/, codex*).
Every decision is appended to a JSONL log for review. Private messages are logged as a hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # unit tests run without litellm
    CustomLogger = object

CONFIG_PATH = os.environ.get("DECISION_ROUTES", "/config/decision-routes.yaml")


def router_enabled() -> bool:
    """DECISION_ROUTER_ENABLED=0|false|off|no turns off decision calls. Unset means on. Read per request."""
    return os.environ.get("DECISION_ROUTER_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")


def load_config(path: str = CONFIG_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text())


def last_user_text(messages: list) -> tuple[str, bool]:
    """Return (text of the last user message, has_image)."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content, False
        texts, image = [], False
        for part in content or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") in ("image_url", "input_image", "image"):
                image = True
        return "\n".join(texts), image
    return "", False


def pick_model(cfg: dict, answers: dict) -> tuple[str, str]:
    """Map decision answers to a model name. Returns (model, reason)."""
    route = answers["route"]
    if route.get("confidence", 0) < cfg["min_confidence"]:
        return cfg["default_model"], "low_confidence"
    hard = answers.get("hard", {}).get("noul", 0.0)
    for rule in cfg["routes"]:
        if rule["route"] != route["choice"]:
            continue
        if "hard_below" in rule and not hard < rule["hard_below"]:
            continue
        if "hard_at_least" in rule and not hard >= rule["hard_at_least"]:
            continue
        return rule["model"], "route"
    return cfg["default_model"], "no_rule"


def enforce_private(cfg: dict, model: str, private: Optional[float]) -> tuple[str, bool]:
    p = cfg["private"]
    if private is not None and private >= p["threshold"] and model.startswith(tuple(p["remote_prefixes"])):
        return p["local_model"], True
    return model, False


class DecisionRouter(CustomLogger):
    def __init__(self, config: Optional[dict] = None, client: Optional[httpx.AsyncClient] = None):
        self.cfg = config or load_config()
        self.client = client or httpx.AsyncClient()
        self.key = os.environ.get(self.cfg["decision"]["key_env"], "")

    async def decide(self, text: str) -> tuple[Optional[dict], Optional[str]]:
        d = self.cfg["decision"]
        body = {"state": {"prompt": text[-d["max_chars"]:]}, "questions": self.cfg["questions"]}
        try:
            r = await self.client.post(d["url"], json=body, timeout=d["timeout_s"],
                                       headers={"Authorization": "Bearer " + self.key})
            r.raise_for_status()
            return r.json()["answers"], None
        except Exception as exc:  # timeout, HTTP error, bad JSON: never block the request
            return None, type(exc).__name__

    async def route(self, data: dict) -> dict:
        """Return the decision record and set data["model"]. No-op for other models."""
        if data.get("model") != self.cfg["virtual_model"]:
            return {}
        started = time.perf_counter()
        text, has_image = last_user_text(data.get("messages", []))
        answers, error, private = None, None, None
        if not router_enabled():
            # Switched off: no decision call; "auto" means the default model.
            model, reason = self.cfg["default_model"], "router_disabled"
        elif has_image:
            model, reason = self.cfg["image_model"], "image_part"
        else:
            answers, error = await self.decide(text)
            if answers is None:
                model, reason = self.cfg["default_model"], f"decision_error:{error}"
            else:
                private = answers.get("private", {}).get("noul")
                model, reason = pick_model(self.cfg, answers)
        model, forced_local = enforce_private(self.cfg, model, private)
        data["model"] = model
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model": model, "reason": reason, "forced_local": forced_local,
            "overhead_ms": round((time.perf_counter() - started) * 1000, 1),
            "route": (answers or {}).get("route", {}).get("choice"),
            "route_confidence": (answers or {}).get("route", {}).get("confidence"),
            "hard": (answers or {}).get("hard", {}).get("noul"),
            "private": private,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16],
            "review": reason != "route" or forced_local,
        }
        if private is None or private < self.cfg["private"]["threshold"]:
            record["excerpt"] = text[: self.cfg["log"]["excerpt_chars"]]
        data.setdefault("metadata", {})["decision_router"] = {k: record[k] for k in ("model", "reason", "route", "route_confidence")}
        self._log(record)
        return record

    def _log(self, record: dict) -> None:
        try:
            with open(self.cfg["log"]["path"], "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass

    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: str):
        if call_type in ("completion", "acompletion"):
            await self.route(data)
        return data


# LiteLLM loads this instance: litellm_settings.callbacks: ["decision_router.proxy_handler_instance"]
proxy_handler_instance = DecisionRouter() if Path(CONFIG_PATH).exists() else None
