"""Ordered Codex account router. Only account workers own OAuth refresh tokens.

CODEX_ACCOUNT_ORDER lists the account services in preference order, for example
codex2,codex3,codex1. Each account but the last has a usage check and a block
state. The last account is the final fallback: the router never asks for its
usage and never spends its allowance on a guess.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sys
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

# image/hooks is on PYTHONPATH in the image: one derivation for the router and the account proxies.
from chatgpt_session_id import cache_session_id

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CACHE_SECONDS = 60
DEFAULT_ORDER = "codex2,codex3,codex1"
ACCOUNT_NAME = re.compile(r"codex[1-9][0-9]*\Z")

log = logging.getLogger("quota_router")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def account_order(value=None):
    """The account names of CODEX_ACCOUNT_ORDER, first preferred, last the final fallback."""
    text = DEFAULT_ORDER if value is None else value
    names = [name.strip() for name in text.split(",") if name.strip()]
    if len(names) < 2 or len(set(names)) != len(names) or not all(ACCOUNT_NAME.fullmatch(n) for n in names):
        raise RuntimeError("CODEX_ACCOUNT_ORDER must list two or more distinct names of the form codexN")
    return names


def iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return repr(ts)


def usage_fields(payload):
    """Window fields for the log. No token, account id or prompt content."""
    rate = payload.get("rate_limit") if isinstance(payload, dict) else None
    if not isinstance(rate, dict):
        return "rate_limit=missing"
    parts = [f"allowed={rate.get('allowed')}", f"limit_reached={rate.get('limit_reached')}"]
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if isinstance(window, dict):
            parts.append(f"{name}.used_percent={window.get('used_percent')}")
            parts.append(f"{name}.reset_at={iso(window.get('reset_at'))}")
        else:
            parts.append(f"{name}=none")
    return " ".join(parts)


def exhausted_until(payload, now):
    """Honor either account-wide window. Never assume primary means five hours."""
    rate = payload.get("rate_limit")
    if not isinstance(rate, dict):
        raise ValueError("Missing rate limit data")
    resets = []
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if not isinstance(window, dict):
            continue
        if float(window.get("used_percent", 0)) >= 100:
            reset = window.get("reset_at")
            if reset is None:
                reset = now + float(window.get("reset_after_seconds", CACHE_SECONDS))
            resets.append(max(now + 1, float(reset)))
    if resets:
        return max(resets)
    if rate.get("allowed") is False or rate.get("limit_reached") is True:
        return now + CACHE_SECONDS
    return 0


class Quota:
    """Usage state of one account with a mounted, read-only token file."""

    def __init__(self, client, name, token_path, clock=time.time):
        self.client = client
        self.name = name
        self.token_path = Path(token_path)
        self.clock = clock
        self.checked = 0
        self.blocked_until = 0
        self.block_reason = None
        self.blocked_since = None
        self.available = None
        self.missing_logged = None
        self.lock = asyncio.Lock()

    def logged_in(self):
        """A complete token file exists. The account worker writes it at login; the router never does.

        A worker without a login writes a placeholder with only device_code_requested_at.
        """
        try:
            token = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return False
        return isinstance(token, dict) and bool(token.get("access_token")) and bool(token.get("account_id"))

    async def usage(self):
        # Re-read after worker refresh. Never refresh or rewrite this file.
        token = json.loads(self.token_path.read_text())
        response = await self.client.get(USAGE_URL, headers={
            "Authorization": "Bearer " + token["access_token"],
            "ChatGPT-Account-Id": token["account_id"],
        }, timeout=10)
        response.raise_for_status()
        return response.json()

    def block(self, until, reason, now, fields):
        if self.blocked_until <= now:
            self.blocked_since = now
        self.blocked_until = until
        self.block_reason = reason
        self.available = False
        log.info("%s block source=%s blocked_until=%s %s", self.name, reason, iso(until), fields)

    def unblock(self, why, now, fields=""):
        log.info("%s unblock source=%s was=%s blocked_since=%s blocked_until=%s %s", self.name,
                 why, self.block_reason, iso(self.blocked_since), iso(self.blocked_until), fields)
        self.blocked_until = 0
        self.block_reason = None
        self.blocked_since = None

    async def usable(self):
        """True when the account has a login and is not blocked."""
        async with self.lock:
            now = self.clock()
            if not self.logged_in():
                # An account without a login cannot serve. Skip it; do not count it as a block.
                if self.missing_logged is None or now - self.missing_logged >= CACHE_SECONDS:
                    log.info("%s skipped source=no-login", self.name)
                    self.missing_logged = now
                return False
            if self.blocked_until > now:
                if now - self.checked < CACHE_SECONDS:
                    return False
                # A block must not outlive the account state it came from.
                self.checked = now
                try:
                    payload = await self.usage()
                    until = exhausted_until(payload, now)
                except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
                    log.info("%s block kept source=usage-check-failed error=%s blocked_until=%s",
                             self.name, type(error).__name__, iso(self.blocked_until))
                    return False
                if until == 0 and payload["rate_limit"].get("allowed") is True:
                    self.unblock("usage", now, usage_fields(payload))
                    self.available = True
                    return True
                if until > now:
                    self.block(until, "usage", now, usage_fields(payload))
                else:
                    log.info("%s block kept source=usage blocked_until=%s %s",
                             self.name, iso(self.blocked_until), usage_fields(payload))
                return False
            if self.block_reason is not None:
                self.unblock("expired", now)
            if self.available is True and now - self.checked < CACHE_SECONDS:
                return True
            try:
                payload = await self.usage()
                until = exhausted_until(payload, now)
                if until > now:
                    self.block(until, "usage", now, usage_fields(payload))
                else:
                    self.available = True
            except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
                # Unknown quota must not consume the allowance of a later account speculatively.
                # The worker can refresh its own token and return a real limit error.
                self.available = True
            self.checked = now
            return self.available

    async def rate_limited(self, status, headers, body):
        async with self.lock:
            now = self.clock()
            reset = now + CACHE_SECONDS
            resets_at = retry_after = None
            try:
                error = json.loads(body).get("error", {})
                # Native upstream payloads can carry an absolute reset timestamp.
                if isinstance(error, dict) and error.get("resets_at"):
                    resets_at = float(error["resets_at"])
                    reset = max(reset, resets_at)
            except (ValueError, TypeError, AttributeError):
                pass
            try:
                retry_after = headers.get("retry-after")
                if retry_after is not None:
                    reset = max(reset, now + float(retry_after))
            except (ValueError, TypeError):
                pass
            # A 429 alone cannot prove the account is exhausted. A proxy cooldown or a
            # per-model limit must not block a healthy account for days. Bound the
            # block by the account's own usage windows.
            try:
                payload = await self.usage()
                until = exhausted_until(payload, now)
                fields = usage_fields(payload)
            except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as error:
                until = 0
                fields = "usage=" + type(error).__name__
            limit = until if until > now else now + CACHE_SECONDS
            reset = min(reset, limit)
            self.checked = now
            self.block(max(self.blocked_until, reset), "429", now,
                       f"status={status} resets_at={iso(resets_at)} retry-after={retry_after} {fields}")

    def state(self):
        return {"blocked_until": self.blocked_until, "block_reason": self.block_reason,
                "blocked_since": iso(self.blocked_since), "logged_in": self.logged_in(), "usage_checked": True}


class Router:
    """The accounts in preference order. Every account but the last has a Quota."""

    def __init__(self, client, order, token_dir="/tokens", clock=time.time):
        self.order = list(order)
        self.quotas = {name: Quota(client, name, Path(token_dir) / name / "auth.json", clock)
                       for name in self.order[:-1]}

    async def candidates(self):
        """The accounts to try for one request, in order. The last account is always included."""
        names = [name for name in self.order[:-1] if await self.quotas[name].usable()]
        return names + [self.order[-1]]

    async def status(self):
        preferred = self.order[0]
        selected = (await self.candidates())[0]
        accounts = {name: quota.state() for name, quota in self.quotas.items()}
        accounts[self.order[-1]] = {"blocked_until": 0, "block_reason": None, "blocked_since": None,
                                    "logged_in": None, "usage_checked": False}
        first = self.quotas[preferred]
        return {"preferred_account": preferred, "selected_account": selected, "order": self.order,
                "accounts": accounts,
                f"{preferred}_blocked_until": first.blocked_until,
                "block_reason": first.block_reason, "blocked_since": iso(first.blocked_since),
                "quota_cache_seconds": CACHE_SECONDS}


def aggregate_sse(content, path):
    events = []
    done = False
    for line in content.decode().splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            continue
        event = json.loads(data)
        if event.get("error") or event.get("type") in ("error", "response.failed"):
            raise ValueError("Upstream stream error")
        events.append(event)
    if path == "responses":
        for event in reversed(events):
            if event.get("type") == "response.completed":
                return event["response"]
        raise ValueError("Missing response.completed")
    if not done or not events:
        raise ValueError("Missing stream terminator")
    from litellm import stream_chunk_builder
    from litellm.types.utils import ModelResponseStream
    result = stream_chunk_builder([ModelResponseStream(**event) for event in events])
    if result is None:
        raise ValueError("Empty completion")
    return result.model_dump(exclude_none=True)


@asynccontextmanager
async def lifespan(app):
    key = os.environ["CODEX_MASTER_KEY"]
    if not key:
        raise RuntimeError("CODEX_MASTER_KEY must not be empty")
    app.state.key = key
    timeout = float(os.environ.get("CODEX_REQUEST_TIMEOUT_SECONDS", "180"))
    if not 1 <= timeout <= 3600:
        raise RuntimeError("CODEX_REQUEST_TIMEOUT_SECONDS must be between 1 and 3600")
    order = account_order(os.environ.get("CODEX_ACCOUNT_ORDER"))
    token_dir = os.environ.get("CODEX_TOKEN_DIR", "/tokens")
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False) as client:
        app.state.client = client
        app.state.router = Router(client, order, token_dir)
        log.info("router order=%s", ",".join(order))
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health/liveliness")
async def health():
    return {"status": "ok"}


def authorized(request):
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied, "Bearer " + request.app.state.key)


@app.get("/routing/status")
async def status(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await request.app.state.router.status()


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    if not authorized(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if path not in ("chat/completions", "responses"):
        return JSONResponse({"error": "Unsupported endpoint"}, status_code=404)
    body = await request.body()
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError()
    except ValueError:
        return JSONResponse({"error": "Invalid JSON object"}, status_code=400)
    # Account-scoped continuation IDs cannot safely follow an automatic switch.
    if payload.get("previous_response_id") or payload.get("conversation"):
        return JSONResponse({"error": "Use an explicit codexN/ route for account-scoped continuations; codex-auto requires full input history."}, status_code=409)
    router = request.app.state.router
    names = await router.candidates()
    client = request.app.state.client
    # Codex requires streaming. Aggregate locally for non-streaming API clients.
    upstream_payload = {**payload, "stream": True, "litellm_session_id": cache_session_id(payload)}
    body = json.dumps(upstream_payload).encode()

    async def send(selected):
        req = client.build_request("POST", f"http://{selected}:4000/v1/{path}",
                                   content=body, headers={
                                       "Authorization": "Bearer " + request.app.state.key,
                                       "Content-Type": "application/json",
                                   })
        return await client.send(req, stream=True)

    try:
        for index, account in enumerate(names):
            response = await send(account)
            # A 429 moves to the next candidate, before any response bytes reach the
            # caller. The last candidate's answer goes back as it is: no retry loop.
            if response.status_code == 429 and index + 1 < len(names):
                error = await response.aread()
                await router.quotas[account].rate_limited(response.status_code, response.headers, error)
                await response.aclose()
                continue
            break
    except httpx.HTTPError:
        # Network failures do not justify spending the allowance of a later account.
        return JSONResponse({"error": "Codex account upstream unavailable"}, status_code=502)
    headers = {"X-Codex-Account": account}
    for name in ("content-type", "retry-after"):
        if name in response.headers:
            headers[name] = response.headers[name]
    if response.status_code >= 400 or not payload.get("stream"):
        content = await response.aread()
        await response.aclose()
        if response.status_code < 400 and "text/event-stream" in response.headers.get("content-type", ""):
            try:
                result = aggregate_sse(content, path)
            except (ValueError, KeyError, TypeError):
                return JSONResponse({"error": "Incomplete Codex stream; request was not retried"}, status_code=502)
            return JSONResponse(result, headers={"X-Codex-Account": account})
        return Response(content, status_code=response.status_code, headers=headers)
    return StreamingResponse(response.aiter_bytes(), status_code=response.status_code,
                             headers=headers, background=BackgroundTask(response.aclose))
