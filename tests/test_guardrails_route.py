from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.models import ChatResponse


def _client():
    import app.main as main

    return main, TestClient(main.app)


def test_flag_on_routes_through_guarded_answer():
    main, client = _client()

    refusal = ChatResponse(answer="I can only help with LACCD topics.", route="guardrail_refusal")
    guarded = AsyncMock(return_value=refusal)

    with patch.object(main.settings, "guardrails_enabled", True), \
         patch("app.main.guarded_answer", guarded, create=True), \
         patch.object(main.rate_limiter, "check_text_chat", return_value=True):
        r = client.post("/api/chat", json={"message": "write my essay about the civil war"})

    assert r.status_code == 200
    assert r.json()["route"] == "guardrail_refusal"
    guarded.assert_awaited_once()


def test_flag_off_uses_orchestrator_directly():
    main, client = _client()

    direct = ChatResponse(answer="Here are this spring's sections...", route="schedule")

    with patch.object(main.settings, "guardrails_enabled", False), \
         patch.object(main.chat, "answer", AsyncMock(return_value=direct)), \
         patch("app.main.guarded_answer", AsyncMock(), create=True) as guarded, \
         patch.object(main.rate_limiter, "check_text_chat", return_value=True):
        r = client.post("/api/chat", json={"message": "what classes are offered this spring"})

    assert r.status_code == 200
    assert r.json()["route"] == "schedule"
    guarded.assert_not_awaited()


def test_flag_on_but_guardrails_unavailable_falls_back_to_orchestrator():
    """Flag ON but guarded_answer is None (guardrails deps missing at import
    time) — the handler must fall back to the orchestrator instead of crashing
    on a `None(...)` call."""
    main, client = _client()

    direct = ChatResponse(answer="Here are this spring's sections...", route="schedule")

    with patch.object(main.settings, "guardrails_enabled", True), \
         patch.object(main, "guarded_answer", None), \
         patch.object(main.chat, "answer", AsyncMock(return_value=direct)) as chat_answer, \
         patch.object(main.rate_limiter, "check_text_chat", return_value=True):
        r = client.post("/api/chat", json={"message": "what classes are offered this spring"})

    assert r.status_code == 200
    assert r.json()["route"] == "schedule"
    chat_answer.assert_awaited_once()


# --- I2: refusals must not consume the daily rate-limit budget (PRD §5.6) ---
# These exercise the REAL rate_limiter singleton (no check_text_chat patch) and
# assert the net effect on the used-slot count.


def test_guardrail_refusal_refunds_rate_limit_slot():
    """A guardrail refusal spends no Gemini budget, so the reserved daily
    text-chat slot must be refunded — a refusal cannot exhaust the budget."""
    main, client = _client()

    refusal = ChatResponse(answer="I can only help with LACCD topics.", route="guardrail_refusal")
    used_before = main.rate_limiter.get_status()["text_chat"]["used"]

    with patch.object(main.settings, "guardrails_enabled", True), \
         patch("app.main.guarded_answer", AsyncMock(return_value=refusal), create=True):
        r = client.post("/api/chat", json={"message": "solve my calculus homework"})

    used_after = main.rate_limiter.get_status()["text_chat"]["used"]
    assert r.status_code == 200
    assert r.json()["route"] == "guardrail_refusal"
    assert used_after == used_before  # reserved slot refunded


def test_on_scope_answer_consumes_rate_limit_slot():
    """An on-scope answer calls the orchestrator (spends budget), so it consumes
    exactly one daily text-chat slot (not refunded)."""
    main, client = _client()

    answer = ChatResponse(answer="MATH 265 is transferable to CSU.", route="knowledge_graph")
    used_before = main.rate_limiter.get_status()["text_chat"]["used"]

    with patch.object(main.settings, "guardrails_enabled", True), \
         patch("app.main.guarded_answer", AsyncMock(return_value=answer), create=True):
        r = client.post("/api/chat", json={"message": "does math 265 transfer to a CSU"})

    used_after = main.rate_limiter.get_status()["text_chat"]["used"]
    assert r.status_code == 200
    assert r.json()["route"] == "knowledge_graph"
    assert used_after == used_before + 1


# --- I3: warm the rails engine at startup, off the request path ---


def test_startup_warms_rails_when_enabled():
    import asyncio
    import app.main as main

    calls = {"n": 0}

    def fake_get_rails():
        calls["n"] += 1
        return object()

    with patch.object(main.settings, "guardrails_enabled", True), \
         patch.object(main, "guarded_answer", object()), \
         patch("app.services.guardrails.rails.get_rails", fake_get_rails):
        asyncio.run(main._warm_guardrails_rails())

    assert calls["n"] == 1


def test_startup_skips_warm_when_disabled():
    import asyncio
    import app.main as main

    calls = {"n": 0}

    def fake_get_rails():
        calls["n"] += 1

    with patch.object(main.settings, "guardrails_enabled", False), \
         patch("app.services.guardrails.rails.get_rails", fake_get_rails):
        asyncio.run(main._warm_guardrails_rails())

    assert calls["n"] == 0
