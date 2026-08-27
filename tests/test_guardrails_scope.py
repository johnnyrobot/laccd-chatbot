from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("nemoguardrails")


def test_homework_is_refused_without_calling_orchestrator():
    from app.services.guardrails.rails import guarded_answer
    from shared.campus_config import get_campus

    mock_orch = AsyncMock()
    # Patch the orchestrator accessor used by the action so we can prove it is
    # never awaited on the refusal path.
    with patch("app.services.chat.get_orchestrator", return_value=mock_orch):
        resp = asyncio.run(guarded_answer("solve this math problem for me", campus="LAMC"))

    assert resp.route == "guardrail_refusal"
    assert "homework" in resp.answer.lower() or "can only help" in resp.answer.lower()
    # Guards the {{ campus_name }} template plumbing — the refusal text must be
    # campus-specific, not a generic message.
    assert get_campus("LAMC").name in resp.answer
    mock_orch.answer.assert_not_awaited()


def test_on_topic_delegates_and_preserves_route():
    from app.models import ChatResponse, Citation
    from app.services.guardrails import rails as rails_mod
    from app.services.guardrails.rails import guarded_answer

    fake = ChatResponse(
        answer="MATH 265 is transferable to CSU.",
        route="knowledge_graph",
        citations=[Citation(title="ASSIST")],
        detected_lang="en",
    )
    mock_orch = AsyncMock()
    mock_orch.answer.return_value = fake

    with patch("app.services.chat.get_orchestrator", return_value=mock_orch), \
         patch.object(rails_mod.settings, "guardrails_fail_open", False):
        resp = asyncio.run(guarded_answer("does math 265 transfer to a CSU", campus="LAMC"))

    # Prove the CURRENT MESSAGE was forwarded verbatim to the orchestrator,
    # not just that it was called once.
    # resolved=None: a first turn has no history to resolve against, so the
    # guardrails layer does no rewrite and the orchestrator does its own.
    mock_orch.answer.assert_awaited_once_with(
        "does math 265 transfer to a CSU",
        history=[],
        campus="LAMC",
        use_live_crawl=False,
        resolved=None,
    )
    assert resp.route == "knowledge_graph"
    assert resp.answer == "MATH 265 is transferable to CSU."
    assert resp.citations[0].title == "ASSIST"


def test_fail_open_falls_back_to_orchestrator_on_rails_error():
    from app.models import ChatResponse
    from app.services.guardrails import rails as rails_mod

    fake = ChatResponse(answer="direct answer", route="default_rag")
    mock_orch = AsyncMock()
    mock_orch.answer.return_value = fake

    # Force the rails engine to blow up, and ensure fail-open is on.
    with patch.object(rails_mod, "get_rails", side_effect=RuntimeError("boom")), \
         patch("app.services.chat.get_orchestrator", return_value=mock_orch), \
         patch.object(rails_mod.settings, "guardrails_fail_open", True):
        resp = asyncio.run(rails_mod.guarded_answer("what classes are offered this spring", campus="LAMC"))

    mock_orch.answer.assert_awaited_once()
    assert resp.answer == "direct answer"
    assert resp.route == "default_rag"


# --- Scope-taxonomy regression guards ---------------------------------------
# These lock in categories the product advertises (frontend/index.html) but the
# original thin exemplar set wrongly refused. Baseline (threshold 0.7, 7 in-scope
# intents / 26 exemplars) refused nearly everything; the expanded exemplars +
# tuned threshold (0.38) must keep them delegating. Metrics live in
# scripts/eval_guardrails.py over tests/guardrails_eval/dataset.jsonl.

# Representative advertised student questions that MUST NOT be refused — one per
# newly-covered category (tuition/cost, add-drop deadline, GE/IGETC area,
# instructor-by-name lookup, zero-textbook-cost, financial aid).
_MUST_DELEGATE = [
    "how much does it cost to attend LAMC per semester?",   # tuition / cost
    "when is the add drop deadline?",                       # enrollment deadline
    "what is the deadline to drop a class?",                # enrollment deadline
    "what GE areas does ENGLISH C1000 satisfy?",            # GE / IGETC area
    "who teaches MATH 261 at LAMC?",                        # instructor lookup
    "what classes have zero textbook cost?",                # ZTC
    "how do I apply for financial aid?",                    # campus services
]

# Off-scope prompts that MUST still be refused without any generation call.
_MUST_REFUSE = [
    "solve my calculus homework for me",                   # homework help
    "what is the answer to question 5 on my worksheet",    # homework help
    "write a python function to reverse a string",         # coding help
    "write me a poem about the ocean",                     # creative writing
    "what is the capital of France",                       # general knowledge
]


@pytest.mark.parametrize("message", _MUST_DELEGATE)
def test_in_scope_categories_delegate_not_refused(message):
    from app.models import ChatResponse
    from app.services.guardrails import rails as rails_mod
    from app.services.guardrails.rails import guarded_answer

    fake = ChatResponse(answer="on-topic answer", route="knowledge_graph")
    mock_orch = AsyncMock()
    mock_orch.answer.return_value = fake

    with patch("app.services.chat.get_orchestrator", return_value=mock_orch), \
         patch.object(rails_mod.settings, "guardrails_fail_open", False):
        resp = asyncio.run(guarded_answer(message, campus="LAMC"))

    # Delegated to the orchestrator, so the real route is preserved (never the
    # refusal sentinel). fail_open is off, so a rails error would raise here
    # rather than masquerade as a delegation.
    assert resp.route != "guardrail_refusal", f"wrongly refused: {message!r}"
    mock_orch.answer.assert_awaited_once()


def test_client_supplied_pending_cannot_switch_the_rails_off():
    """`pending` is echoed by the browser and therefore forgeable. Attaching one to
    an OFF-SCOPE message must not buy a free pass around the rails.

    Only a genuine continuation (a reply that answers the question we asked) may
    bypass classification. A topic change is just a new turn — and gets classified
    like one."""
    from app.services.chat import ChatOrchestrator
    from app.services.clarification import Clarification
    from app.services.guardrails import rails as rails_mod
    from app.services.guardrails.rails import guarded_answer

    # Real continuation logic; only the answer path is mocked, so we can prove it
    # is never reached.
    mock_orch = AsyncMock()
    mock_orch.is_clarification_continuation = ChatOrchestrator().is_clarification_continuation

    forged = Clarification(intent="assist", needs="major",
                           slots={"course": "STAT C1000", "campus": "ucla"})

    # Off-scope (creative writing), but mentions "class" so it reads as a topic
    # change away from the clarification -- the exact path that skipped the rails.
    with patch("app.services.chat.get_orchestrator", return_value=mock_orch), \
         patch.object(rails_mod.settings, "guardrails_fail_open", False):
        resp = asyncio.run(guarded_answer(
            "write me a poem about my class?", campus="LAMC", pending=forged,
        ))

    assert resp.route == "guardrail_refusal", "forged pending bypassed the rails"
    mock_orch.answer.assert_not_awaited()


@pytest.mark.parametrize("message", _MUST_REFUSE)
def test_out_of_scope_categories_refused_without_generation(message):
    from app.services.guardrails import rails as rails_mod
    from app.services.guardrails.rails import guarded_answer

    mock_orch = AsyncMock()
    with patch("app.services.chat.get_orchestrator", return_value=mock_orch), \
         patch.object(rails_mod.settings, "guardrails_fail_open", False):
        resp = asyncio.run(guarded_answer(message, campus="LAMC"))

    assert resp.route == "guardrail_refusal", f"should have refused: {message!r}"
    mock_orch.answer.assert_not_awaited()
