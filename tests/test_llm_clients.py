"""Provider client tests with mocked SDKs.

Each test monkeypatches the client factory (`_client`) so we never touch a
real API. The retry decorator is exercised by raising RateLimitError on the
first call(s) and a real response on a later call.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes.llm import anthropic_client, deepseek_client, google_client
from hermes.llm.types import AuthError, ProviderError, RateLimitError

# ---------- Anthropic ----------


def _anthropic_response(text: str = "ok", in_t: int = 100, out_t: int = 50):
    return SimpleNamespace(
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(input_tokens=in_t, output_tokens=out_t),
    )


async def test_anthropic_success(monkeypatch):
    mock = SimpleNamespace(
        messages=SimpleNamespace(
            create=AsyncMock(return_value=_anthropic_response("hi", 10, 5))
        )
    )
    monkeypatch.setattr(anthropic_client, "_client", lambda: mock)

    resp = await anthropic_client.complete("hello")

    assert resp.text == "hi"
    assert resp.provider == "anthropic"
    assert resp.input_tokens == 10
    assert resp.output_tokens == 5
    assert resp.cost_usd > 0


async def test_anthropic_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_call_once(client, *, model, prompt, max_tokens):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RateLimitError("first")
        return _anthropic_response("ok2", 1, 1)

    monkeypatch.setattr(anthropic_client, "_call_once", fake_call_once)
    monkeypatch.setattr(anthropic_client, "_client", lambda: SimpleNamespace())

    resp = await anthropic_client.complete("x")
    assert resp.text == "ok2"
    assert calls["n"] == 2


async def test_anthropic_auth_error_does_not_retry(monkeypatch):
    calls = {"n": 0}

    async def fake_call_once(client, *, model, prompt, max_tokens):
        calls["n"] += 1
        raise AuthError("bad key")

    monkeypatch.setattr(anthropic_client, "_call_once", fake_call_once)
    monkeypatch.setattr(anthropic_client, "_client", lambda: SimpleNamespace())

    with pytest.raises(AuthError):
        await anthropic_client.complete("x")
    assert calls["n"] == 1  # no retry


async def test_anthropic_exhausted_retries_raises_provider_error(monkeypatch):
    async def fake_call_once(client, *, model, prompt, max_tokens):
        raise ProviderError("5xx everytime")

    monkeypatch.setattr(anthropic_client, "_call_once", fake_call_once)
    monkeypatch.setattr(anthropic_client, "_client", lambda: SimpleNamespace())

    with pytest.raises(ProviderError):
        await anthropic_client.complete("x")


# ---------- DeepSeek ----------


def _deepseek_response(text: str = "ok", in_t: int = 10, out_t: int = 5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=in_t, completion_tokens=out_t),
    )


async def test_deepseek_success(monkeypatch):
    async def fake_call_once(client, *, model, prompt, max_tokens):
        return _deepseek_response("hi", 100, 50)

    monkeypatch.setattr(deepseek_client, "_call_once", fake_call_once)
    monkeypatch.setattr(deepseek_client, "_client", lambda: SimpleNamespace())

    resp = await deepseek_client.complete("hello", model="deepseek-v4-flash")
    assert resp.text == "hi"
    assert resp.provider == "deepseek"
    assert resp.model == "deepseek-v4-flash"
    assert resp.input_tokens == 100
    assert resp.cost_usd > 0


async def test_deepseek_rate_limit_then_success(monkeypatch):
    calls = {"n": 0}

    async def fake_call_once(client, *, model, prompt, max_tokens):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RateLimitError("limited")
        return _deepseek_response("done", 1, 1)

    monkeypatch.setattr(deepseek_client, "_call_once", fake_call_once)
    monkeypatch.setattr(deepseek_client, "_client", lambda: SimpleNamespace())

    resp = await deepseek_client.complete("x", model="deepseek-v4-pro")
    assert resp.text == "done"
    assert calls["n"] == 2


# ---------- Google ----------


def _google_response(text: str = "ok", in_t: int = 10, out_t: int = 5):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=in_t, candidates_token_count=out_t),
    )


async def test_google_success(monkeypatch):
    async def fake_call_once(client, *, model, prompt):
        return _google_response("hi", 20, 10)

    monkeypatch.setattr(google_client, "_call_once", fake_call_once)
    monkeypatch.setattr(google_client, "_client", lambda: SimpleNamespace())

    resp = await google_client.complete("hello")
    assert resp.text == "hi"
    assert resp.provider == "google"
    assert resp.input_tokens == 20
    assert resp.output_tokens == 10


async def test_google_provider_error_retries_and_fails(monkeypatch):
    calls = {"n": 0}

    async def fake_call_once(client, *, model, prompt):
        calls["n"] += 1
        raise ProviderError("500")

    monkeypatch.setattr(google_client, "_call_once", fake_call_once)
    monkeypatch.setattr(google_client, "_client", lambda: SimpleNamespace())

    with pytest.raises(ProviderError):
        await google_client.complete("x")
    assert calls["n"] == 3  # exhausted 3 attempts
