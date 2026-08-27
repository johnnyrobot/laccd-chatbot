from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models import ChatResponse, Citation


def test_guardrails_flags_default_off_and_fail_open():
    # Defaults chosen for dev MVP: disabled until explicitly enabled; fail-open so a
    # broken rails config never wedges the chat endpoint.
    assert settings.guardrails_enabled is False
    assert settings.guardrails_fail_open is True


def test_get_orchestrator_is_singleton():
    from app.services.chat import get_orchestrator

    a = get_orchestrator()
    b = get_orchestrator()
    assert a is b


def test_answer_laccd_delegates_to_orchestrator():
    from app.services.guardrails.actions import answer_laccd

    fake = ChatResponse(
        answer="MATH 265 transfers to CSU.",
        route="knowledge_graph",
        citations=[Citation(title="ASSIST")],
        detected_lang="en",
    )
    mock_orch = AsyncMock()
    mock_orch.answer.return_value = fake

    context = {
        "last_user_message": "does math 265 transfer to a CSU",
        "campus_code": "LAMC",
        "history": [],
        "use_live_crawl": False,
    }

    with patch("app.services.chat.get_orchestrator", return_value=mock_orch):
        result = asyncio.run(answer_laccd(context=context))

    mock_orch.answer.assert_awaited_once_with(
        "does math 265 transfer to a CSU",
        history=[],
        campus="LAMC",
        use_live_crawl=False,
        resolved=None,
    )
    assert result["answer"] == "MATH 265 transfers to CSU."
    assert result["route"] == "knowledge_graph"
    assert result["citations"][0]["title"] == "ASSIST"


# --- Context rewrite is hoisted ABOVE the rails ---
#
# The rails classify user intent from the latest message alone (embeddings_only).
# A terse reply to our own clarify-back ("UCLA", "MATH 31A") carries no scope
# signal, so it was matched to `express greeting` and answered with the static
# greeting — the orchestrator never ran. Resolving the standalone query BEFORE
# the rails means they classify "Is STAT C1000 transferable to UCLA?" instead.


def test_answer_laccd_reuses_resolved_query_instead_of_rewriting_twice():
    """The rewrite already happened in guarded_answer; answer() must be handed the
    result rather than paying for (and re-deriving) it a second time."""
    from app.services.chat import RewriteResult
    from app.services.guardrails.actions import answer_laccd

    mock_orch = AsyncMock()
    mock_orch.answer.return_value = ChatResponse(answer="ok", route="assist_articulation")

    context = {
        # What the rails saw and classified:
        "last_user_message": "Is STAT C1000 transferable to UCLA?",
        # What the student actually typed, plus the pre-resolved rewrite:
        "original_message": "UCLA",
        "resolved_query": "Is STAT C1000 transferable to UCLA?",
        "detected_lang": "en",
        "campus_code": "LAMC",
        "history": [{"role": "user", "content": "Is STAT C1000 transferable?"}],
        "use_live_crawl": False,
    }

    with patch("app.services.chat.get_orchestrator", return_value=mock_orch):
        asyncio.run(answer_laccd(context=context))

    kwargs = mock_orch.answer.await_args.kwargs
    args = mock_orch.answer.await_args.args
    assert args[0] == "UCLA"  # original message preserved for the orchestrator
    assert kwargs["resolved"] == RewriteResult(
        english_query="Is STAT C1000 transferable to UCLA?", detected_lang="en"
    )


def test_guarded_answer_classifies_the_resolved_query_not_the_terse_turn():
    """Regression: a bare 'MATH 31A' reply must reach the rails as the resolved
    standalone question, or embeddings match it to `express greeting`."""
    import app.services.guardrails.rails as rails_mod
    from app.services.chat import RewriteResult

    mock_orch = AsyncMock()
    mock_orch.resolve_query = lambda message, history=None: RewriteResult(
        english_query="Is STAT C1000 transferable to UCLA for the Mathematics major?",
        detected_lang="en",
    )

    captured: dict = {}

    class FakeRails:
        async def generate_async(self, messages=None, options=None):
            captured["messages"] = messages
            return type(
                "R",
                (),
                {
                    "response": [{"role": "assistant", "content": "ok"}],
                    "output_data": {"laccd": {"answer": "ok", "route": "assist_articulation"}},
                },
            )()

    history = [{"role": "user", "content": "Is STAT C1000 transferable to UC and CSU?"}]

    with patch.object(rails_mod, "get_rails", lambda: FakeRails()), \
         patch("app.services.chat.get_orchestrator", return_value=mock_orch):
        resp = asyncio.run(rails_mod.guarded_answer("MATH 31A", history=history, campus="LAMC"))

    last_user = captured["messages"][-1]
    assert last_user["role"] == "user"
    assert last_user["content"] == "Is STAT C1000 transferable to UCLA for the Mathematics major?"

    ctx = captured["messages"][0]["content"]
    assert ctx["original_message"] == "MATH 31A"
    assert ctx["resolved_query"] == "Is STAT C1000 transferable to UCLA for the Mathematics major?"
    assert ctx["detected_lang"] == "en"

    assert resp.route == "assist_articulation"
