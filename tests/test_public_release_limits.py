from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.main import app
from app.models import ChatRequest, ChatResponse
from app.services import enrollment_planner
from app.services.assist_articulation import MAX_CACHE_ENTRIES as ASSIST_CACHE_MAX, AssistService
from voice_agent import token_endpoint


def test_chat_request_rejects_oversized_fields_and_history() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="x" * 4_001)
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", conversation_id="x" * 129)
    with pytest.raises(ValidationError):
        ChatRequest(
            message="hello",
            history=[{"role": "user", "content": "ok"}] * 11,
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            message="hello",
            history=[{"role": "user", "content": "x" * 1_501}],
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            message="hello",
            history=[{"role": "system", "content": "not allowed"}],
        )


def test_assist_cache_has_hard_capacity() -> None:
    service = AssistService()
    for index in range(ASSIST_CACHE_MAX + 20):
        service._cache_put(f"test:{index}", {"index": index})
    assert len(service._cache) == ASSIST_CACHE_MAX


def test_chat_body_limit_runs_before_orchestrator(monkeypatch) -> None:
    called = False

    async def fake_answer(*_args, **_kwargs):
        nonlocal called
        called = True
        return ChatResponse(answer="unexpected", route="test")

    monkeypatch.setattr("app.main.chat.answer", fake_answer)

    response = TestClient(app).post("/api/chat", json={"message": "x" * 70_000})

    assert response.status_code == 413
    assert called is False


def test_frontend_uses_local_scripts_and_security_headers() -> None:
    response = TestClient(app).get("/", headers={"host": "lamc.johnnyrobot.ai"})

    assert response.status_code == 200
    assert "unpkg.com" not in response.text
    assert "/vendor/livekit-client-2.9.5.umd.js" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_enrollment_plan_rejects_unrealistic_target_before_schedule_lookup(monkeypatch) -> None:
    called = False

    async def fake_schedule(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"sections": []}

    monkeypatch.setattr(enrollment_planner, "search_live_schedule", fake_schedule)

    with pytest.raises(ValueError, match="between 1 and 21"):
        asyncio.run(
            enrollment_planner.build_candidate_enrollment_plan(
                campus_code="LAMC",
                term="2268",
                target_units=99,
            )
        )

    assert called is False


def test_public_neo4j_diagnostics_require_admin_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    client = TestClient(app)

    for path in (
        "/api/neo4j/status",
        "/api/neo4j/renumberings",
        "/api/neo4j/ge-courses?ge_code=1A",
        "/api/neo4j/course-info?subject=MATH&number=261",
    ):
        assert client.get(path).status_code == 401


def test_voice_token_has_explicit_short_ttl(monkeypatch) -> None:
    livekit_api = pytest.importorskip("livekit.api")
    class FakeRoom:
        async def create_room(self, _request):
            return None

    class FakeDispatch:
        async def create_dispatch(self, _request):
            return None

    class FakeLiveKitAPI:
        def __init__(self, **_kwargs):
            self.room = FakeRoom()
            self.agent_dispatch = FakeDispatch()

        async def aclose(self):
            return None

    monkeypatch.setattr(token_endpoint, "LIVEKIT_URL", "wss://voice.example")
    monkeypatch.setattr(token_endpoint, "LIVEKIT_API_KEY", "test-key")
    test_secret = "test-secret-that-is-at-least-32-bytes-long"
    monkeypatch.setattr(token_endpoint, "LIVEKIT_API_SECRET", test_secret)
    monkeypatch.setattr(livekit_api, "LiveKitAPI", FakeLiveKitAPI)

    result = asyncio.run(token_endpoint.create_voice_token("LAMC"))
    claims = jwt.decode(
        result["token"],
        test_secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )

    assert 0 < claims["exp"] - claims["nbf"] <= 10 * 60
    assert datetime.fromtimestamp(claims["exp"], tz=timezone.utc) > datetime.now(timezone.utc)
