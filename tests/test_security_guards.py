from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.main import _resolve_cors_origins, app
from app.rate_limiter import DailyRateLimiter
from shared.campus_config import CAMPUSES
from app.security import (
    client_identity_from_request,
    is_trusted_browser_origin,
    resolve_under,
)
from app.services.corpus import is_lamission_url, normalize_lamission_url
from voice_agent.tools import _is_laccd_url


@pytest.fixture
def test_client() -> TestClient:
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_endpoint_requires_configured_admin_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", None)

    resp = test_client.post("/api/ingest/purge-old")

    assert resp.status_code == 503
    assert "admin api is not configured" in resp.json()["detail"].lower()


def test_admin_endpoint_rejects_missing_or_wrong_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")

    missing = test_client.post("/api/ingest/purge-old")
    wrong = test_client.post(
        "/api/ingest/purge-old",
        headers={"Authorization": "Bearer wrong"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_admin_endpoint_accepts_bearer_key(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    monkeypatch.setattr("app.main.file_search.delete_old_documents", lambda min_year: 0)

    resp = test_client.post(
        "/api/ingest/purge-old",
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0


def test_voice_token_rejects_untrusted_origin(test_client):
    resp = test_client.post(
        "/api/voice/token", headers={"Origin": "https://evil.example.com"}
    )

    assert resp.status_code == 403
    assert "origin not allowed" in resp.json()["detail"].lower()


def test_voice_token_rejects_missing_origin(test_client):
    resp = test_client.post("/api/voice/token")

    assert resp.status_code == 403


def test_voice_token_enforces_per_client_limit(test_client, monkeypatch):
    # M2: a single client that clears the origin guard is still capped per-client
    # (here: 1/day), so it can't drain the global LiveKit token budget.
    monkeypatch.setattr("app.main.voice_configured", lambda: True)

    async def _fake_token(campus_code):
        return {"token": "t", "serverUrl": "wss://example"}

    monkeypatch.setattr("app.main.create_voice_token", _fake_token)
    monkeypatch.setattr(
        "app.main.rate_limiter",
        DailyRateLimiter(voice_daily_limit=100, voice_per_client_daily_limit=1),
    )

    headers = {"Origin": "http://testserver"}
    first = test_client.post("/api/voice/token", headers=headers)
    second = test_client.post("/api/voice/token", headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429


def test_voice_token_allows_same_origin(test_client, monkeypatch):
    # A first-party (same Host) request clears the origin guard; it then fails
    # downstream on voice not being configured (503), NOT on the guard (403).
    monkeypatch.setattr("app.main.voice_configured", lambda: False)

    resp = test_client.post(
        "/api/voice/token", headers={"Origin": "http://testserver"}
    )

    assert resp.status_code == 503
    assert "voice agent is not configured" in resp.json()["detail"].lower()


def test_is_trusted_browser_origin_accepts_same_host_rejects_others():
    class _SameOrigin:
        headers = {
            "host": "lamc-chat.johnnyrobot.ai",
            "origin": "https://lamc-chat.johnnyrobot.ai",
        }

    class _ForeignOrigin:
        headers = {
            "host": "lamc-chat.johnnyrobot.ai",
            "origin": "https://evil.example.com",
        }

    class _NoOrigin:
        headers = {"host": "lamc-chat.johnnyrobot.ai"}

    assert is_trusted_browser_origin(_SameOrigin()) is True
    assert is_trusted_browser_origin(_ForeignOrigin()) is False
    assert is_trusted_browser_origin(_NoOrigin()) is False


def test_bootstrap_rejects_corpus_dir_outside_configured_root(
    test_client, monkeypatch, tmp_path
):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    monkeypatch.setattr(settings, "corpus_dir", allowed)

    resp = test_client.post(
        "/api/ingest/bootstrap",
        json={"corpus_dir": str(outside)},
        headers={"X-Admin-API-Key": "admin-secret"},
    )

    assert resp.status_code == 400
    assert "corpus_dir" in resp.json()["detail"]


def test_resolve_under_rejects_sibling_prefix(tmp_path):
    root = tmp_path / "lamission"
    sibling = tmp_path / "lamission.edu.evil"
    root.mkdir()
    sibling.mkdir()

    with pytest.raises(HTTPException) as exc:
        resolve_under(root, sibling)

    assert exc.value.status_code == 400


def test_lamission_url_validation_rejects_lookalike_and_non_http_schemes():
    assert is_lamission_url("https://www.lamission.edu/admissions")
    assert is_lamission_url("https://catalog.lamission.edu/programs")
    assert not is_lamission_url("https://evil-lamission.edu/path")
    assert not is_lamission_url("file://www.lamission.edu/etc/passwd")
    assert normalize_lamission_url("https://evil-lamission.edu/path") is None


def test_voice_laccd_url_validation_requires_http_scheme():
    assert _is_laccd_url("https://www.lamission.edu/admissions")
    assert _is_laccd_url("https://www.elac.edu/academics")
    assert not _is_laccd_url("http://www.elac.edu/academics")
    assert not _is_laccd_url("file://www.lamission.edu/etc/passwd")
    assert not _is_laccd_url("ftp://www.lamission.edu/path")
    assert not _is_laccd_url("https://[invalid-host/admissions")


def test_client_identity_ignores_forwarded_for_from_untrusted_peer():
    class _Request:
        headers = {"x-forwarded-for": "203.0.113.5"}
        client = type("Client", (), {"host": "198.51.100.9"})()

    assert (
        client_identity_from_request(_Request(), trusted_proxy_hosts={"127.0.0.1"})
        == "198.51.100.9"
    )


def test_client_identity_uses_forwarded_for_from_trusted_peer():
    # Caddy (the trusted peer) APPENDS the real client IP as the rightmost hop.
    # The client we must bill is the rightmost element, not the leftmost.
    class _Request:
        headers = {"x-forwarded-for": "203.0.113.5, 198.51.100.9"}
        client = type("Client", (), {"host": "127.0.0.1"})()

    assert (
        client_identity_from_request(_Request(), trusted_proxy_hosts={"127.0.0.1"})
        == "198.51.100.9"
    )


def test_client_identity_ignores_spoofed_leftmost_forwarded_for():
    # An attacker prepends a bogus hop; Caddy appends the true client IP. We
    # must resolve to the real client (rightmost), never the spoofed value.
    class _Request:
        headers = {"x-forwarded-for": "spoofed, 203.0.113.7"}
        client = type("Client", (), {"host": "127.0.0.1"})()

    assert (
        client_identity_from_request(_Request(), trusted_proxy_hosts={"127.0.0.1"})
        == "203.0.113.7"
    )


def test_client_identity_skips_trailing_trusted_proxy_hops():
    # Multiple trusted proxies in the chain: skip them right-to-left and land
    # on the first non-trusted hop (the real client).
    class _Request:
        headers = {"x-forwarded-for": "203.0.113.9, 172.18.0.4"}
        client = type("Client", (), {"host": "172.18.0.4"})()

    assert (
        client_identity_from_request(
            _Request(), trusted_proxy_hosts={"172.18.0.4"}
        )
        == "203.0.113.9"
    )


def test_client_identity_falls_back_to_peer_on_non_ip_client_hop():
    # If the rightmost non-trusted hop isn't a valid IP, don't trust it —
    # fall back to the (trusted) proxy peer.
    class _Request:
        headers = {"x-forwarded-for": "not-an-ip"}
        client = type("Client", (), {"host": "127.0.0.1"})()

    assert (
        client_identity_from_request(_Request(), trusted_proxy_hosts={"127.0.0.1"})
        == "127.0.0.1"
    )


def test_chat_daily_limit_is_per_client():
    limiter = DailyRateLimiter(
        text_daily_limit=10,
        voice_daily_limit=10,
        fallback_daily_limit=10,
        chat_per_client_daily_limit=1,
        chat_per_client_minute_limit=10,
    )

    assert limiter.check_text_chat("client-a")
    assert not limiter.check_text_chat("client-a")
    assert limiter.check_text_chat("client-b")


def test_refund_restores_global_daily_and_minute_client_slots():
    limiter = DailyRateLimiter(
        text_daily_limit=1,
        chat_per_client_daily_limit=1,
        chat_per_client_minute_limit=1,
    )

    assert limiter.check_text_chat("client-a")
    limiter.refund_text_chat("client-a")

    assert limiter.get_status()["text_chat"]["used"] == 0
    assert limiter.check_text_chat("client-a")


def test_chat_per_client_maps_are_bounded():
    # A flood of distinct client keys must not grow the per-client maps without
    # bound (memory-exhaustion DoS, M3). Oldest-seen keys are FIFO-evicted.
    limiter = DailyRateLimiter(
        text_daily_limit=100_000,
        chat_per_client_daily_limit=5,
        chat_per_client_minute_limit=5,
        chat_client_max_keys=10,
    )
    for i in range(1000):
        assert limiter.check_text_chat(f"client-{i}")
    assert len(limiter._text_client_counts) <= 10
    assert len(limiter._text_client_windows) <= 10


def test_voice_call_is_per_client():
    # M2: a per-client daily cap on voice-token mints, on top of the global cap.
    limiter = DailyRateLimiter(voice_daily_limit=100, voice_per_client_daily_limit=2)

    assert limiter.check_voice_call("client-a")
    assert limiter.check_voice_call("client-a")
    assert not limiter.check_voice_call("client-a")  # per-client cap reached
    assert limiter.check_voice_call("client-b")  # a different client is unaffected


def test_voice_call_global_cap_applies_across_clients():
    # The global daily cap still bounds total voice mints even when each client
    # is under its own per-client cap.
    limiter = DailyRateLimiter(voice_daily_limit=2, voice_per_client_daily_limit=5)

    assert limiter.check_voice_call("client-a")
    assert limiter.check_voice_call("client-b")
    assert not limiter.check_voice_call("client-c")  # global cap of 2 reached


# ── L1: /api/neo4j/status must not leak the internal Bolt URI ────────────────


def test_neo4j_status_does_not_leak_uri(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "test-admin-key")
    monkeypatch.setattr("app.main.neo4j_service.is_configured", lambda: True)

    async def _unreachable():
        return False

    monkeypatch.setattr("app.main.neo4j_service.verify_connectivity", _unreachable)

    resp = test_client.get(
        "/api/neo4j/status", headers={"X-Admin-API-Key": "test-admin-key"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unreachable"
    assert "uri" not in body  # internal topology must not be disclosed


# ── L2: CORS defaults to the campus domains in production when unset ─────────


def test_cors_defaults_to_campus_domains_in_production_when_unset(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.setattr(settings, "app_env", "production")

    origins = _resolve_cors_origins()

    # Both the short <campus>.chat hosts and the legacy *-chat.johnnyrobot.ai
    # hosts are allowed, so the allowlist stays correct across the DNS migration.
    # Compare the whole set rather than a count plus samples, so that a missing
    # campus or a duplicate cannot pass.
    expected = {
        origin
        for campus in CAMPUSES.values()
        for origin in (
            f"https://{campus.code.lower()}.chat",
            f"https://{campus.subdomain}.johnnyrobot.ai",
        )
    }
    assert set(origins) == expected
    assert len(origins) == len(expected)  # no duplicates
    assert "https://lamc.chat" in origins
    assert "https://lamc-chat.johnnyrobot.ai" in origins
    assert "*" not in origins


def test_cors_explicit_override_is_respected(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.example,https://b.example")
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "cors_allow_origins", "https://a.example,https://b.example")

    origins = _resolve_cors_origins()

    assert origins == ["https://a.example", "https://b.example"]


# ── L6: /api/ingest/refresh rejects non-LACCD crawl targets (SSRF guard) ─────


def test_ingest_refresh_rejects_non_laccd_urls(test_client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    monkeypatch.setattr("app.main.crawler.is_available", lambda: True)

    resp = test_client.post(
        "/api/ingest/refresh",
        json={"urls": ["https://evil.example.com/internal"]},
        headers={"X-Admin-API-Key": "admin-secret"},
    )

    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"].lower()
