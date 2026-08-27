"""Monitoring and observability tests.

Covers:
  * ``GET /api/data-health`` is admin-guarded (503 unset / 401 wrong / 200 key);
  * every ``/api/chat`` exit path emits exactly one structured ``event=chat_turn``
    INFO line with the stable schema (client id hashed, campus, tier, route,
    rate_limited, guardrail_refusal) and NO message content — in particular the
    previously-silent rate-limit rejection.

All external calls are stubbed — NO real network.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import _hash_client_id, app
from app.models import ChatResponse


@pytest.fixture
def test_client() -> TestClient:
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── /api/data-health admin guard ─────────────────────────────────────────────


def test_data_health_requires_configured_admin_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", None)

    resp = test_client.get("/api/data-health")

    assert resp.status_code == 503
    assert "admin api is not configured" in resp.json()["detail"].lower()


def test_data_health_rejects_missing_or_wrong_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")

    missing = test_client.get("/api/data-health")
    wrong = test_client.get("/api/data-health", headers={"Authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_data_health_returns_coverage_with_admin_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")

    resp = test_client.get("/api/data-health", headers={"X-Admin-API-Key": "admin-secret"})

    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert "campuses" in body
    assert body["summary"]["campus_count"] == len(body["campuses"])


# ── structured per-request chat logging ──────────────────────────────────────


def _chat_turn_lines(caplog) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "app.main" and "event=chat_turn" in rec.getMessage()
    ]


def test_rate_limited_chat_turn_emits_structured_log(test_client, monkeypatch, caplog):
    # The previously-silent rejection path must now emit one structured line.
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: False)

    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = test_client.post(
            "/api/chat",
            json={"message": "What math classes are offered?", "campus": "LAMC"},
        )

    assert resp.status_code == 200
    lines = _chat_turn_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "tier=graceful" in line
    assert "route=clarifying_fallback" in line
    assert "rate_limited=true" in line
    assert "guardrail_refusal=false" in line
    assert "campus=LAMC" in line
    # Raw client identity must never appear; only the hashed prefix.
    assert "client_id=" in line
    assert "testclient" not in line  # TestClient peer host, must be hashed away


def test_normal_chat_turn_logs_gemini_tier(test_client, monkeypatch, caplog):
    monkeypatch.setattr("app.main.rate_limiter.check_text_chat", lambda client_id=None: True)

    async def _answer(message, history=None, campus=None, use_live_crawl=False, pending=None):
        return ChatResponse(answer="here you go", route="knowledge_graph")

    monkeypatch.setattr("app.main.chat.answer", _answer)

    with caplog.at_level(logging.INFO, logger="app.main"):
        resp = test_client.post("/api/chat", json={"message": "Is MATH 261 UC transferable?", "campus": "WLAC"})

    assert resp.status_code == 200
    lines = _chat_turn_lines(caplog)
    assert len(lines) == 1, lines
    assert "tier=gemini" in lines[0]
    assert "route=knowledge_graph" in lines[0]
    assert "rate_limited=false" in lines[0]
    assert "campus=WLAC" in lines[0]


def test_hash_client_id_is_stable_and_does_not_echo_the_raw_identity():
    # Deterministic, keyed 12-hex-character pseudonym for this process.
    assert _hash_client_id("203.0.113.7") == _hash_client_id("203.0.113.7")
    digest = _hash_client_id("203.0.113.7")
    assert len(digest) == 12
    assert "203.0.113.7" not in digest
