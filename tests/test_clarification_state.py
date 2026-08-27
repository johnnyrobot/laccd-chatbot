"""Clarification state machine — deterministic slot filling for clarify-backs.

The bot already knows what it asked for; it used to throw that away and ask an
LLM to reconstruct the question from a truncated transcript. These tests pin the
replacement: the pending slot is filled from the raw reply, with no rewriter, no
history window, and no LLM.

"""

from __future__ import annotations

import asyncio

import pytest

from app.services.clarification import Clarification, parse_pending


@pytest.fixture
def orchestrator():
    from app.services.chat import ChatOrchestrator

    return ChatOrchestrator()


# ----- the trust boundary: `pending` comes from the client -----


def test_parse_pending_accepts_a_well_formed_object():
    p = parse_pending({
        "intent": "assist",
        "needs": "major",
        "slots": {"course": "STAT C1000", "campus": "UCLA"},
    })
    assert p == Clarification(
        intent="assist", needs="major", slots={"course": "STAT C1000", "campus": "UCLA"}
    )


@pytest.mark.parametrize("bad", [
    None,
    {},
    "not-a-dict",
    {"intent": "assist"},                                     # no `needs`
    {"intent": "evil", "needs": "major", "slots": {}},        # intent not allowlisted
    {"intent": "assist", "needs": "root", "slots": {}},       # needs not allowlisted
    {"intent": "assist", "needs": "major", "slots": {"campus_code": "ELAC"}},  # slot key not allowlisted
    {"intent": "assist", "needs": "major", "slots": {"course": {"$ne": None}}},  # non-string value
    {"intent": "assist", "needs": "major", "slots": {"course": "X" * 5000}},     # length cap
])
def test_parse_pending_rejects_hostile_or_malformed_input(bad):
    """Malformed pending is IGNORED (-> None), never an error: a stale browser
    tab must degrade, not 422. And it must never smuggle in a campus."""
    assert parse_pending(bad) is None


@pytest.mark.parametrize("bad", [
    {"intent": ["assist"], "needs": "major"},        # unhashable -> `in frozenset` raised TypeError
    {"intent": {"a": 1}, "needs": "major"},
    {"intent": "assist", "needs": ["major"]},
    {"intent": None, "needs": None},
])
def test_parse_pending_survives_unhashable_values(bad):
    """`x in frozenset(...)` raises TypeError for unhashable x, so a client could
    500 the chat endpoint with `pending: {"intent": []}`. Type-check first."""
    assert parse_pending(bad) is None


@pytest.mark.parametrize("contradictory", [
    # We only ask for a slot we don't have. A client claiming both is not a client
    # we understand -- and it would let them skip the clarify-back entirely.
    {"intent": "assist", "needs": "major", "slots": {"major": "MATHEMATICS, B.S."}},
    {"intent": "assist", "needs": "receiving_institution", "slots": {"campus": "ucla"}},
])
def test_parse_pending_rejects_a_needs_slot_it_already_claims_to_have(contradictory):
    assert parse_pending(contradictory) is None


def test_malformed_pending_is_ignored_by_the_api_not_a_422():
    """The spec says a stale browser tab must DEGRADE, not error. Typing the field
    as dict[str, Any] made Pydantic 422 before parse_pending ever ran."""
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from app.models import ChatResponse
    import app.main as main

    client = TestClient(main.app)
    answer = ChatResponse(answer="ok", route="knowledge_graph")
    with patch.object(main.chat, "answer", AsyncMock(return_value=answer)), \
         patch.object(main.rate_limiter, "check_text_chat", return_value=True):
        for junk in ["stale-garbage", 123, ["a"], {"intent": []}]:
            r = client.post("/api/chat", json={"message": "hi", "pending": junk})
            assert r.status_code == 200, f"{junk!r} -> {r.status_code}"


def test_pending_cannot_override_campus():
    """Campus is resolved from the Host header. `campus_code` is not an
    allowlisted slot key, so a client cannot cross-campus itself via pending."""
    assert parse_pending({
        "intent": "assist", "needs": "major",
        "slots": {"course": "STAT C1000", "campus_code": "ELAC"},
    }) is None


# ----- slot filling: the terse reply fills exactly the missing slot -----


def test_fills_campus_from_a_bare_campus_name(orchestrator):
    pending = Clarification(intent="assist", needs="receiving_institution",
                            slots={"course": "STAT C1000"})
    filled = orchestrator._fill_clarification(pending, "UCLA")
    assert filled is not None
    # The canonical alias, not the raw text or the display name — it has to
    # re-resolve on the next turn.
    assert filled.slots["campus"] == "ucla"
    assert filled.slots["course"] == "STAT C1000"   # carried, not re-derived


def test_fills_major_from_a_bare_major_name(orchestrator):
    majors = [{"label": "MATHEMATICS, B.S.", "key": "k1"},
              {"label": "COMPUTER SCIENCE, B.S.", "key": "k2"}]
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    filled = orchestrator._fill_clarification(pending, "Mathematics", majors=majors)
    assert filled is not None
    assert filled.slots["major"] == "MATHEMATICS, B.S."


def test_a_course_code_does_not_fill_the_major_slot(orchestrator):
    """The bug from the screenshot: 'MATH 31A' answered 'which major?'. It is not
    a major, so the slot must stay unfilled (-> we re-ask), never guess."""
    majors = [{"label": "MATHEMATICS, B.S.", "key": "k1"}]
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    assert orchestrator._fill_clarification(pending, "MATH 31A", majors=majors) is None


def test_both_does_not_fill_the_campus_slot(orchestrator):
    """'both' is not a campus and never will be. It must not fill the slot --
    the system-level question is answered by the KG, not by ASSIST."""
    pending = Clarification(intent="assist", needs="receiving_institution",
                            slots={"course": "STAT C1000"})
    assert orchestrator._fill_clarification(pending, "both") is None


# ----- the roach-motel rule: a pending clarification must not trap the user -----


def test_topic_change_abandons_the_pending_clarification(orchestrator):
    """Student gives up on the transfer question and asks something else. We must
    answer the NEW question, not re-ask 'which major?' forever."""
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    assert orchestrator._should_abandon_clarification(pending, "how much does parking cost?")


def test_a_terse_non_answer_keeps_the_clarification_alive(orchestrator):
    """'idk' matches no other intent -- re-ask rather than dropping context."""
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    assert not orchestrator._should_abandon_clarification(pending, "idk")


def test_a_valid_slot_answer_is_not_treated_as_a_topic_change(orchestrator):
    pending = Clarification(intent="assist", needs="receiving_institution",
                            slots={"course": "STAT C1000"})
    assert not orchestrator._should_abandon_clarification(pending, "UCLA")


# ----- regression: the three conversations from the production screenshots -----


MAJORS = [
    {"label": "MATHEMATICS, B.S.", "key": "key-math"},
    {"label": "COMPUTER SCIENCE, B.S.", "key": "key-cs"},
    {"label": "ENGLISH, B.A.", "key": "key-eng"},
]


@pytest.fixture
def assist_orch(orchestrator):
    """Orchestrator with the ASSIST + Gemini clients stubbed. The clarification
    logic under test is real; only the network is faked."""
    from unittest.mock import AsyncMock, MagicMock

    assist = AsyncMock()
    assist.find_laccd_institution.return_value = {"id": 1, "code": "LAMC",
                                                  "names": [{"name": "Los Angeles Mission College"}]}
    assist.find_institution.return_value = {"id": 117, "code": "UCLA",
                                            "names": [{"name": "University of California, Los Angeles"}]}
    assist.current_academic_year_id.return_value = 75
    assist.list_agreements.return_value = MAJORS
    assist.get_articulation.return_value = {"articulation": "stub"}
    assist.last_source = {"list_agreements": "live"}
    orchestrator.tools.assist = assist

    # Echo the prompt back so tests can assert which major we asked about.
    gemini = MagicMock()
    gemini.generate.side_effect = lambda prompt: f"ANSWER_FOR_PROMPT::{prompt}"
    orchestrator.tools.gemini_file_search = gemini
    return orchestrator


def test_clarify_back_carries_the_course_and_campus_forward(assist_orch):
    """Turn 1: we can't pin the major, so we ask — and hand back the slots we
    already know, so the next turn needn't reconstruct them from the transcript."""
    resp = asyncio.run(assist_orch._handle_assist_query(
        "Is STAT C1000 transferable to UCLA?", "LAMC",
    ))
    assert resp.pending is not None
    assert resp.pending["needs"] == "major"
    assert resp.pending["slots"]["course"] == "STAT C1000"
    assert resp.pending["slots"]["campus"] == "ucla"   # re-resolvable alias


def test_terse_major_reply_answers_for_that_major(assist_orch):
    """Screenshot 1 fixed: 'Mathematics' answers for Mathematics — and the whole
    turn happens with no rewriter, no history, and no LLM reconstruction."""
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    resp = asyncio.run(assist_orch._handle_assist_query(
        "Mathematics", "LAMC", pending=pending,
    ))
    assert resp.route == "assist_articulation"
    assert resp.pending is None                      # answered; nothing outstanding
    assert "MATHEMATICS, B.S." in resp.answer        # the major the student named
    assert "COMPUTER SCIENCE" not in resp.answer     # never the invented one
    assert "STAT C1000" in resp.answer               # the carried course


def test_course_code_reply_re_asks_instead_of_guessing(assist_orch):
    """Screenshot 1's actual bug: 'MATH 31A' answered 'which major?'. We must
    re-ask — not silently answer for Computer Science."""
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "UCLA"})
    resp = asyncio.run(assist_orch._handle_assist_query(
        "MATH 31A", "LAMC", pending=pending,
    ))
    assert resp.pending is not None                  # still waiting on the major
    assert resp.pending["slots"]["course"] == "STAT C1000"   # context NOT lost
    assert "course code isn't a major" in resp.answer.lower()


def test_carried_campus_slot_is_re_resolvable_on_the_next_turn(assist_orch):
    """Caught only by replaying against prod: the campus slot stored ASSIST's
    DISPLAY name ("University of California, Los Angeles"), which contains none
    of the aliases the extractor matches on. Re-resolving it found nothing, so
    the bot asked for the campus it was already holding. The slot must store the
    alias ("ucla")."""
    # Turn 1 emits the slots...
    first = asyncio.run(assist_orch._handle_assist_query(
        "Is STAT C1000 transferable to UCLA?", "LAMC",
    ))
    campus_slot = first.pending["slots"]["campus"]

    # ...turn 2 must be able to resolve them again, and keep asking for the MAJOR
    # (not regress to asking for the campus).
    carried = parse_pending(first.pending)
    assert carried is not None, "our own pending must survive its own validator"
    second = asyncio.run(assist_orch._handle_assist_query(
        "MATH 31A", "LAMC", pending=carried,
    ))
    assert second.pending["needs"] == "major", (
        f"regressed to asking for the campus; slot was {campus_slot!r}"
    )
    assert "course code isn't a major" in second.answer.lower()


def test_student_can_switch_campus_mid_clarification(assist_orch):
    """'actually, CSUN' — this turn's campus wins over the carried slot."""
    pending = Clarification(intent="assist", needs="major",
                            slots={"course": "STAT C1000", "campus": "ucla"})
    resp = asyncio.run(assist_orch._handle_assist_query("CSUN", "LAMC", pending=pending))
    assert resp.pending["slots"]["campus"] == "csun"


def test_campus_reply_after_a_clarify_does_not_lose_the_course(assist_orch):
    """Screenshot 3 fixed: replying 'CSUN' no longer strands the course. The
    course comes from the slot, so no history window can drop it."""
    pending = Clarification(intent="assist", needs="receiving_institution",
                            slots={"course": "STAT C1000"})
    resp = asyncio.run(assist_orch._handle_assist_query(
        "CSUN", "LAMC", pending=pending,
    ))
    # Campus resolved -> we advance to the major question, still holding the course.
    assert resp.pending is not None
    assert resp.pending["needs"] == "major"
    assert resp.pending["slots"]["course"] == "STAT C1000"
