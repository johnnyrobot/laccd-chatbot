"""Graceful-degradation tests for the `/api/chat` tiered fallback.

Covers the paths that must never surface a bare 500 to a student when the
budget is exhausted or an upstream model hiccups:

  * a per/global rate-limit rejection returns the graceful clarifying response
    (route="clarifying_fallback", rate_limited=True), not an error;
  * a transient Tier-1 (Gemini) failure falls through to Tier-2 (OpenRouter),
    and an ``openrouter_fallback_error`` from Tier-2 falls through to Tier-3
    (the graceful clarifying response);
  * ``_is_transient_upstream_error`` classifies upstream API/network blips as
    transient (fall back) and programmer errors as non-transient (re-raise).

All external calls are stubbed — NO real network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import _is_transient_upstream_error, app
from app.models import ChatResponse


@pytest.fixture
def test_client() -> TestClient:
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── (a) rate-limit rejection → graceful clarifying, never a 500 ──────────────


def test_rate_limited_chat_returns_graceful_response_not_500(test_client, monkeypatch):
    # Simulate the limiter rejecting the turn (budget/per-client exhausted).
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: False)

    resp = test_client.post("/api/chat", json={"message": "What math classes are offered?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["rate_limited"] is True
    assert body["route"] == "clarifying_fallback"
    assert body["answer"]  # a real, helpful message — not an error string


# ── (b) Tier-1 transient error → Tier-2 → Tier-3 ─────────────────────────────


class _ConnectError(Exception):
    """Name matches the httpx-style transient set _is_transient_upstream_error keys on."""


_ConnectError.__name__ = "ConnectError"


def _raise_transient(*args, **kwargs):
    raise _ConnectError("simulated upstream blip")


def test_transient_tier1_error_reaches_openrouter_tier2(test_client, monkeypatch):
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: True)
    monkeypatch.setattr("app.main.rate_limiter.check_fallback", lambda: True)

    async def _boom(message, history=None, campus=None, use_live_crawl=False, pending=None):
        _raise_transient()

    monkeypatch.setattr("app.main.chat.answer", _boom)

    def _fake_openrouter(message, campus_code="LAMC", history=None):
        return ChatResponse(answer="general-knowledge answer", route="openrouter_fallback", fallback_mode=True)

    monkeypatch.setattr("app.main.openrouter_fallback.answer", _fake_openrouter)

    resp = test_client.post("/api/chat", json={"message": "Who was Ada Lovelace?"})

    assert resp.status_code == 200
    assert resp.json()["route"] == "openrouter_fallback"


def test_openrouter_error_falls_through_to_tier3_graceful(test_client, monkeypatch):
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: True)
    monkeypatch.setattr("app.main.rate_limiter.check_fallback", lambda: True)

    async def _boom(message, history=None, campus=None, use_live_crawl=False, pending=None):
        _raise_transient()

    monkeypatch.setattr("app.main.chat.answer", _boom)

    def _fake_openrouter_error(message, campus_code="LAMC", history=None):
        # Tier-2 itself fails: the route the orchestrator uses to signal a
        # non-answer must fall through to Tier-3, not be returned as-is.
        return ChatResponse(
            answer="temporarily unavailable", route="openrouter_fallback_error", rate_limited=True
        )

    monkeypatch.setattr("app.main.openrouter_fallback.answer", _fake_openrouter_error)

    resp = test_client.post("/api/chat", json={"message": "Is MATH 261 UC transferable?"})

    assert resp.status_code == 200
    assert resp.json()["route"] == "clarifying_fallback"


def test_non_transient_tier1_error_reraises_as_500(test_client, monkeypatch):
    # A programmer error is NOT transient — it must propagate (surface as 500)
    # so we get a real stack trace, not a silently-swallowed fallback.
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: True)

    async def _bug(message, history=None, campus=None, use_live_crawl=False, pending=None):
        raise KeyError("programmer error")

    monkeypatch.setattr("app.main.chat.answer", _bug)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 500


# ── (c) _is_transient_upstream_error classification ──────────────────────────


def test_transient_classifier_flags_httpx_network_blips():
    for name in ("ConnectError", "ReadTimeout", "ConnectTimeout", "RemoteProtocolError"):
        exc = type(name, (Exception,), {})()
        assert _is_transient_upstream_error(exc) is True, name


def test_transient_classifier_flags_gemini_api_error():
    try:
        from google.genai.errors import APIError as GenaiAPIError
    except Exception:
        pytest.skip("google-genai not installed")
    # Build an instance without invoking the real __init__ (it needs a live
    # response object); isinstance is all the classifier checks.
    err = GenaiAPIError.__new__(GenaiAPIError)
    assert _is_transient_upstream_error(err) is True


def test_transient_classifier_rejects_programmer_errors():
    assert _is_transient_upstream_error(KeyError("x")) is False
    assert _is_transient_upstream_error(ValueError("x")) is False
    assert _is_transient_upstream_error(RuntimeError("x")) is False
