"""Tests for the clarifying-question fallback used when both Gemini and
OpenRouter are unavailable.

The fallback's contract: NEVER return a bare error. Always offer the
student a way forward, tailored to the apparent intent of their query.
"""
from __future__ import annotations

from app.services.graceful_fallback import (
    _detect_intent,
    graceful_clarifying_response,
)


def test_detect_intent_recognizes_course_code():
    assert _detect_intent("Is CHEM C211 transferable?") == "course_code"
    assert _detect_intent("MATH 261 prereqs") == "course_code"


def test_detect_intent_recognizes_transfer():
    assert _detect_intent("How do I transfer to UCLA?") == "transfer"
    assert _detect_intent("articulation for psychology") == "transfer"


def test_detect_intent_recognizes_program():
    assert _detect_intent("What are the requirements for the Computer Science degree?") == "program"
    assert _detect_intent("paralegal certificate at WLAC") == "program"


def test_detect_intent_recognizes_schedule():
    assert _detect_intent("What classes are offered Spring 2026?") == "schedule"
    assert _detect_intent("Find me a section of MATH") == "schedule"


def test_detect_intent_falls_back_to_generic():
    # Generic "hi" / off-topic: capability-listing response is more useful
    # than a forced specialization.
    assert _detect_intent("hi") == "generic"
    assert _detect_intent("") == "generic"


def test_response_route_is_clarifying_fallback_not_error():
    """The contract: route is 'clarifying_fallback', not an error route.
    Frontend treats this as a normal assistant turn — not a 500."""
    r = graceful_clarifying_response("anything at all", "WLAC")
    assert r.route == "clarifying_fallback"
    assert r.fallback_mode is True
    assert r.answer  # non-empty


def test_response_for_course_code_asks_about_transfer_ge_prereqs_schedule():
    """Course-code intents should surface the 4 things the chatbot can do
    with a course code, so the student can pick what they actually want."""
    r = graceful_clarifying_response("Is CHEM C211 transferable?", "WLAC")
    body = r.answer.lower()
    for keyword in ("transfer", "ge", "prerequisite", "schedule"):
        assert keyword in body, f"course-code clarifier missing {keyword!r}"


def test_response_for_program_intent_mentions_award_types_and_sequence():
    r = graceful_clarifying_response(
        "What are the requirements for the Computer Science degree at West LA?",
        "WLAC",
    )
    body = r.answer.lower()
    # Award-type disambiguation is the actual ask here — the original 500
    # was on this exact query, and the fallback should now steer toward
    # AA/AS/AA-T disambiguation.
    assert "course sequence" in body
    assert any(award in body for award in ("aa", "as", "aa-t", "as-t", "certificate"))


def test_response_uses_campus_full_name_not_code():
    r = graceful_clarifying_response("hi", "WLAC")
    assert "West Los Angeles College" in r.answer
    assert "WLAC" not in r.answer  # the bare code shouldn't leak into UI copy


def test_unknown_campus_does_not_raise():
    """Unknown campus codes shouldn't crash — fall back to generic phrasing."""
    r = graceful_clarifying_response("hi", "ZZZZ")
    assert r.route == "clarifying_fallback"
    assert r.answer
