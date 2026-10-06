import asyncio
import json
from pathlib import Path

import httpx
import yaml

from decision_router import DecisionRouter, last_user_text

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/decision-routes.example.yaml").read_text())


def router(tmp_path, answers=None, status=200, delay=None, cfg_patch=None):
    cfg = json.loads(json.dumps(CFG))
    cfg["log"]["path"] = str(tmp_path / "log.jsonl")
    for k, v in (cfg_patch or {}).items():
        cfg[k] = v
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        if delay:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(status, json={"answers": answers or {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DecisionRouter(cfg, client), calls


def ans(route, conf=0.9, hard=0.1, private=0.05):
    return {"route": {"choice": route, "confidence": conf}, "hard": {"noul": hard}, "private": {"noul": private}}


def run(r, data):
    return asyncio.run(r.route(data))


def req(text, model="auto"):
    return {"model": model, "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]}


def test_routes_easy_code_to_coder(tmp_path):
    r, calls = router(tmp_path, ans("coder", hard=0.2))
    data = req("rename this variable")
    rec = run(r, data)
    assert data["model"] == "local/coder-model"
    assert rec["reason"] == "route"
    assert set(calls[0]["questions"]) == {"route", "hard", "private"}


def test_hard_code_goes_to_general_coding(tmp_path):
    r, _ = router(tmp_path, ans("coder", hard=0.8))
    data = req("find the race")
    run(r, data)
    assert data["model"] == "local/general-model-coding"


def test_other_models_untouched(tmp_path):
    r, calls = router(tmp_path, ans("coder"))
    data = req("hi", model="local/small-chat-model")
    assert run(r, data) == {}
    assert data["model"] == "local/small-chat-model" and calls == []


def test_low_confidence_uses_default_and_flags_review(tmp_path):
    r, _ = router(tmp_path, ans("chat", conf=0.3))
    data = req("hmm")
    rec = run(r, data)
    assert data["model"] == CFG["default_model"]
    assert rec["reason"] == "low_confidence" and rec["review"]


def test_timeout_uses_default(tmp_path):
    r, _ = router(tmp_path, delay=True)
    data = req("anything")
    rec = run(r, data)
    assert data["model"] == CFG["default_model"]
    assert rec["reason"].startswith("decision_error")


def test_private_never_goes_remote(tmp_path):
    routes = [{"route": "chat", "model": "openrouter/example/remote-model"}]
    r, _ = router(tmp_path, ans("chat", private=0.9), cfg_patch={"routes": routes})
    data = req("my blood pressure is 150/95")
    rec = run(r, data)
    assert data["model"] == CFG["private"]["local_model"]
    assert rec["forced_local"] and "excerpt" not in rec


def test_public_may_go_remote(tmp_path):
    routes = [{"route": "chat", "model": "openrouter/example/remote-model"}]
    r, _ = router(tmp_path, ans("chat", private=0.1), cfg_patch={"routes": routes})
    data = req("explain TCP")
    run(r, data)
    assert data["model"] == "openrouter/example/remote-model"


def test_image_part_skips_decision(tmp_path):
    r, calls = router(tmp_path, ans("coder"))
    data = {"model": "auto", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "what is this"}, {"type": "image_url", "image_url": {"url": "data:,"}}]}]}
    run(r, data)
    assert data["model"] == CFG["image_model"] and calls == []


def test_log_line_written(tmp_path):
    r, _ = router(tmp_path, ans("chat"))
    run(r, req("hello"))
    line = json.loads((tmp_path / "log.jsonl").read_text().splitlines()[0])
    assert line["route"] == "chat" and line["excerpt"] == "hello"


def test_last_user_text_takes_last_user_turn():
    msgs = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "x"}, {"role": "user", "content": "second"}]
    assert last_user_text(msgs) == ("second", False)


def test_env_flag_off_skips_decision_call(tmp_path, monkeypatch):
    r, calls = router(tmp_path, answers=ans("coder"))
    monkeypatch.setenv("DECISION_ROUTER_ENABLED", "0")
    data = req("fix this bug")
    rec = run(r, data)
    assert calls == []
    assert data["model"] == CFG["default_model"]
    assert rec["reason"] == "router_disabled"


def test_env_flag_on_or_unset_keeps_routing(tmp_path, monkeypatch):
    for value in (None, "1", "on"):
        if value is None:
            monkeypatch.delenv("DECISION_ROUTER_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DECISION_ROUTER_ENABLED", value)
        r, calls = router(tmp_path, answers=ans("coder"))
        run(r, req("fix this bug"))
        assert len(calls) == 1
