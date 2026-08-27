"""Tests for the Android app's `/api/tools/*` endpoints.

These tests use FastAPI's TestClient + dependency_overrides to bypass the
auth header check / rate limiter where they're not under test. The 5
endpoints are exercised with monkeypatched service functions so the live
APIs (LACCD class schedule, ASSIST.org, Program Mapper, eLumen) are not
hit during ``pytest``.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routes.mobile_tools import (
    IpRateLimiter,
    _client_ip,
    _ip_rate_limiter,
    per_ip_rate_limit,
    require_mobile_tool_token,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """TestClient with auth + rate-limit deps short-circuited.

    Per-test overrides re-enable the real deps to exercise auth / rate-limit
    behavior specifically.
    """
    app.dependency_overrides[require_mobile_tool_token] = lambda: None
    app.dependency_overrides[per_ip_rate_limit] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client() -> TestClient:
    """TestClient with the real auth dep but rate-limit short-circuited."""
    app.dependency_overrides[per_ip_rate_limit] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Auth ────────────────────────────────────────────────────────────────────


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_auth_503_when_token_hashes_unset(auth_client, monkeypatch):
    """No MOBILE_TOOL_TOKEN_HASHES in env → endpoint surface is treated as not configured."""
    monkeypatch.setattr(settings, "mobile_tool_token_hashes", tuple())
    resp = auth_client.get("/api/tools/programs", params={"q": "nursing"})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


def test_auth_401_when_bearer_token_wrong(auth_client, monkeypatch):
    """Wrong bearer token → 401 (not 503)."""
    monkeypatch.setattr(settings, "mobile_tool_token_hashes", (_sha256("correct-secret"),))
    resp = auth_client.get(
        "/api/tools/programs",
        params={"q": "nursing"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_auth_200_when_bearer_token_hash_matches(auth_client, monkeypatch):
    """Correct bearer token → endpoint runs."""
    monkeypatch.setattr(settings, "mobile_tool_token_hashes", (_sha256("correct-secret"),))
    monkeypatch.setattr(
        "app.routes.mobile_tools.search_programs_all_campuses",
        _async_returning([]),
    )
    resp = auth_client.get(
        "/api/tools/programs",
        params={"q": "nursing"},
        headers={"Authorization": "Bearer correct-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["query"] == "nursing"


# ── Per-IP rate limiter ─────────────────────────────────────────────────────


def test_ip_rate_limiter_allows_within_budget():
    limiter = IpRateLimiter(max_requests=3, window_seconds=60.0)
    assert limiter.check("1.1.1.1")
    assert limiter.check("1.1.1.1")
    assert limiter.check("1.1.1.1")


def test_ip_rate_limiter_rejects_over_budget():
    limiter = IpRateLimiter(max_requests=2, window_seconds=60.0)
    assert limiter.check("2.2.2.2")
    assert limiter.check("2.2.2.2")
    assert not limiter.check("2.2.2.2")


def test_ip_rate_limiter_isolates_ips():
    limiter = IpRateLimiter(max_requests=1, window_seconds=60.0)
    assert limiter.check("3.3.3.3")
    # Same IP exhausted, but a different IP still has full budget.
    assert not limiter.check("3.3.3.3")
    assert limiter.check("4.4.4.4")


def test_ip_rate_limiter_bounds_distinct_key_cardinality():
    # A flood of distinct client keys must NOT grow _buckets without bound
    # (memory-exhaustion DoS). Cap at max_clients; oldest keys get evicted.
    limiter = IpRateLimiter(max_requests=5, window_seconds=60.0, max_clients=10)
    for i in range(1000):
        assert limiter.check(f"10.0.0.{i}")
    assert len(limiter._buckets) <= 10


def test_ip_rate_limiter_evicts_expired_buckets_before_capping():
    # Expired buckets are dropped before any live-bucket eviction. A key whose
    # only hit has aged past the window should be reclaimed when space is needed.
    limiter = IpRateLimiter(max_requests=5, window_seconds=0.0, max_clients=2)
    assert limiter.check("a")  # its single hit is immediately "expired" (window=0)
    assert limiter.check("b")
    assert limiter.check("c")  # forces eviction; stays within the cap
    assert len(limiter._buckets) <= 2


def test_mobile_tool_client_ip_ignores_forwarded_for_without_trusted_proxy(monkeypatch):
    class _Request:
        headers = {"x-forwarded-for": "203.0.113.25"}
        client = type("Client", (), {"host": "198.51.100.10"})()

    monkeypatch.setattr(settings, "trusted_proxy_hosts", tuple())

    assert _client_ip(_Request()) == "198.51.100.10"


def test_mobile_tool_client_ip_uses_forwarded_for_from_trusted_proxy(monkeypatch):
    # The trusted proxy appends the real client as the rightmost hop; a leading
    # (leftmost) value is attacker-controlled and must be ignored.
    class _Request:
        headers = {"x-forwarded-for": "203.0.113.25, 198.51.100.10"}
        client = type("Client", (), {"host": "127.0.0.1"})()

    monkeypatch.setattr(settings, "trusted_proxy_hosts", ("127.0.0.1",))

    assert _client_ip(_Request()) == "198.51.100.10"


# ── Endpoint shape: schedule ────────────────────────────────────────────────


def test_schedule_returns_payload(client, monkeypatch):
    monkeypatch.setattr(
        "app.routes.mobile_tools.search_live_schedule",
        _async_returning(
            {
                "campus": "LAMC",
                "campus_name": "Los Angeles Mission College",
                "term": "2264",
                "term_name": "Spring 2026",
                "subject": "MATH",
                "course_number": "261",
                "keyword": None,
                "sections": [{"course_code": "MATH 261", "section": "0001"}],
                "total_sections": 1,
            }
        ),
    )
    resp = client.get(
        "/api/tools/schedule",
        params={"campus": "lamc", "term": "2264", "subject": "MATH", "course_number": "261"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["campus"] == "LAMC"
    assert data["total_sections"] == 1


def test_schedule_422_when_required_missing(client):
    resp = client.get("/api/tools/schedule", params={"campus": "LAMC"})
    # FastAPI auto-422 for missing required `term` query param.
    assert resp.status_code == 422


# ── Endpoint shape: programs ────────────────────────────────────────────────


def test_programs_per_campus(client, monkeypatch):
    class _FakeClient:
        campus_name = "Los Angeles Mission College"

        async def search_programs(self, q):
            return [{"masterRecordId": "p1", "title": "Nursing"}]

    monkeypatch.setattr(
        "app.routes.mobile_tools.get_program_mapper_client",
        lambda code: _FakeClient(),
    )
    resp = client.get("/api/tools/programs", params={"q": "nursing", "campus": "LAMC"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["campus"] == "LAMC"
    assert data["programs"][0]["campus_code"] == "LAMC"


def test_programs_all_campuses(client, monkeypatch):
    monkeypatch.setattr(
        "app.routes.mobile_tools.search_programs_all_campuses",
        _async_returning(
            [
                {"campus_code": "LAMC", "title": "Nursing"},
                {"campus_code": "ELAC", "title": "Nursing"},
            ]
        ),
    )
    resp = client.get("/api/tools/programs", params={"q": "nursing"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2


def test_programs_422_when_query_missing(client):
    resp = client.get("/api/tools/programs")
    assert resp.status_code == 422


# ── Endpoint shape: assist ──────────────────────────────────────────────────


def test_assist_requires_from_and_to(client):
    resp = client.get("/api/tools/assist", params={"course": "MATH 261"})
    assert resp.status_code == 422


def test_assist_returns_filtered_matches(client, monkeypatch):
    class _FakeAssist:
        last_source = {"list_transferable_courses": "live"}

        async def current_academic_year_id(self):
            return 75

        async def find_laccd_institution(self, code):
            return {"id": 124, "name": "Mission", "type": "CCC"}

        async def find_institution(self, query):
            return {"id": 7, "name": "UCLA", "type": "University of California"}

        async def list_transferable_courses(self, institution_id, year_id, area_key):
            return [
                {"code": "MATH 261", "title": "Calculus I"},
                {"code": "ENG 101", "title": "Composition"},
            ]

    monkeypatch.setattr(
        "app.routes.mobile_tools.get_assist_service", lambda: _FakeAssist()
    )
    resp = client.get(
        "/api/tools/assist",
        params={"course": "MATH 261", "from": "LAMC", "to": "UCLA"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["area_key"] == "UCTransferable"
    assert data["filtered_count"] == 1
    assert data["matches"][0]["code"] == "MATH 261"


# ── Endpoint shape: sequence ────────────────────────────────────────────────


def test_sequence_passes_pace_through(client, monkeypatch):
    captured = {}

    class _FakeService:
        async def generate_sequence(self, *, program_id, campus_code, pace):
            captured["program_id"] = program_id
            captured["campus_code"] = campus_code
            captured["pace"] = pace
            return {
                "program_name": "Cyber Security",
                "pace": pace,
                "terms_to_complete": 4,
                "sequence": [],
            }

    monkeypatch.setattr(
        "app.routes.mobile_tools.get_course_sequence_service",
        lambda code: _FakeService(),
    )
    resp = client.get(
        "/api/tools/sequence",
        params={"program_id": "abc-123", "campus": "lamc", "pace": "momentum"},
    )
    assert resp.status_code == 200
    assert resp.json()["pace"] == "momentum"
    assert captured == {"program_id": "abc-123", "campus_code": "LAMC", "pace": "momentum"}


def test_sequence_422_when_program_id_missing(client):
    resp = client.get("/api/tools/sequence")
    assert resp.status_code == 422


# ── Endpoint shape: elumen ──────────────────────────────────────────────────


def test_elumen_returns_course(client, monkeypatch):
    class _FakeElumen:
        async def get_course_details(self, course_code, campus_code):
            return {"code": course_code, "title": "Calculus I", "units": 5.0}

    monkeypatch.setattr(
        "app.routes.mobile_tools.get_elumen_service", lambda: _FakeElumen()
    )
    resp = client.get(
        "/api/tools/elumen",
        params={"subject": "math", "number": "261", "campus": "lamc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["subject"] == "MATH"
    assert data["course"]["title"] == "Calculus I"


def test_elumen_404_when_no_record(client, monkeypatch):
    class _FakeElumen:
        async def get_course_details(self, course_code, campus_code):
            return None

    monkeypatch.setattr(
        "app.routes.mobile_tools.get_elumen_service", lambda: _FakeElumen()
    )
    resp = client.get(
        "/api/tools/elumen",
        params={"subject": "math", "number": "9999", "campus": "lamc"},
    )
    assert resp.status_code == 404


def test_elumen_422_when_required_missing(client):
    resp = client.get("/api/tools/elumen", params={"subject": "math"})
    assert resp.status_code == 422


# ── Helpers ─────────────────────────────────────────────────────────────────


def _async_returning(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro
