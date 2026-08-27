"""Precision tests for the schedule-routing predicate.

Regression cover for #47: "I have AP exam credit. Do I still need to take the
general education course it covers?" routed to ``live_schedule`` at every campus
and returned class listings, because ``"take"`` was a bare substring in
``SCHEDULE_TERMS`` and routed on its own.

Two things were wrong and both are covered here:

1. **No word boundaries.** ``"take" in "mistake"`` is True. So were
   ``"time"`` in "sometimes", ``"class"`` in "classic", ``"section"`` in
   "intersection".
2. **Weak terms routed alone.** "take" and "time" are ordinary English that
   co-occur with schedule questions rather than naming them, so they now need a
   strong term, a course code, or a term reference to count.

Pure logic — no network, no Neo4j.
"""

from __future__ import annotations

import pytest

from app.services.chat import (
    SCHEDULE_TERMS,
    SCHEDULE_TERMS_STRONG,
    SCHEDULE_TERMS_WEAK,
    ChatOrchestrator,
    _word_in,
)


@pytest.fixture
def orch():
    # ChatOrchestrator makes no network calls during construction.
    return ChatOrchestrator()


def matched(orch, q: str) -> bool:
    return bool(orch._schedule_terms_matched(q, q.lower()))


# ----- #47 itself -----


def test_the_exact_question_from_issue_47_is_not_a_schedule_query(orch):
    q = "I have AP exam credit. Do I still need to take the general education course it covers?"
    assert not matched(orch, q)


def test_the_advising_phrasing_without_ap_is_also_not_a_schedule_query(orch):
    assert not matched(orch, "Do I still need to take the general education course?")


# ----- word-boundary regressions -----


@pytest.mark.parametrize(
    "question,substring_of",
    [
        ("that was a mistake", "take"),
        ("have you taken the placement test", "take"),
        ("I want to undertake research", "take"),
        ("sometimes I get confused", "time"),
        ("I am a part-timer", "time"),
        ("classic literature interests me", "class"),
        ("what is the classification of this course", "class"),
        ("the intersection of math and art", "section"),
    ],
)
def test_substring_matches_no_longer_route_to_schedule(orch, question, substring_of):
    assert substring_of in question.lower().replace("-", "")  # the trap is real
    assert not matched(orch, question)


def test_word_in_helper_respects_boundaries():
    assert _word_in("take", "can I take this")
    assert not _word_in("take", "that was a mistake")
    assert not _word_in("take", "have you taken it")
    assert _word_in("class", "which class")
    assert not _word_in("class", "classic novel")


# ----- weak terms need corroboration -----


@pytest.mark.parametrize(
    "question",
    [
        "do I need to take that",
        "is now a good time",
        "I do not have time to take more",
    ],
)
def test_weak_terms_alone_do_not_route(orch, question):
    assert not matched(orch, question)


@pytest.mark.parametrize(
    "question",
    [
        "can I take MATH 101 online",  # course code corroborates
        "can I take it in Fall 2026",  # term reference corroborates
        "what time is it offered",  # strong term corroborates
    ],
)
def test_weak_terms_route_when_corroborated(orch, question):
    assert matched(orch, question)


# ----- genuine schedule queries still route (no regression) -----


@pytest.mark.parametrize(
    "question",
    [
        "what classes are offered in Fall 2026",
        "show me the schedule",
        "what sections are available",
        "who is the instructor for that",
        "is there an evening class",
        "which sections are still available",
    ],
)
def test_real_schedule_queries_still_route(orch, question):
    assert matched(orch, question)


# ----- the predicate is the single source of truth -----


def test_matches_any_intent_uses_the_same_predicate(orch):
    """The ladder and its mirror must agree, or clarification abandonment drifts."""
    q = "Do I still need to take the general education course?"
    lowered = q.lower()
    # Not a schedule query, so schedule must not be what makes this a topic change.
    assert not orch._schedule_terms_matched(q, lowered)


def test_strong_and_weak_partition_the_vocabulary():
    assert SCHEDULE_TERMS == SCHEDULE_TERMS_STRONG | SCHEDULE_TERMS_WEAK
    assert not (SCHEDULE_TERMS_STRONG & SCHEDULE_TERMS_WEAK)


def test_matched_terms_are_returned_for_telemetry(orch):
    """Route telemetry reports which terms fired; it must not come back empty."""
    q = "what sections are available in Fall 2026"
    terms = orch._schedule_terms_matched(q, q.lower())
    assert "sections" in terms and "available" in terms
