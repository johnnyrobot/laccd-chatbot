"""Chat and voice share calendar-derived LACCD term resolution.

Injected dates keep current-term and future-term behavior deterministic.
"""

from __future__ import annotations

from datetime import date

import pytest

from shared.terms import current_term_code, resolve_term_code, term_code
from voice_agent.tools import resolve_term


@pytest.mark.parametrize(
    "spoken,expected,name",
    [
        ("spring 2027", "2274", "Spring 2027"),
        ("Spring 2027", "2274", "Spring 2027"),
        ("winter 2027", "2272", "Winter 2027"),
        ("summer 2027", "2276", "Summer 2027"),
        ("fall 2027", "2278", "Fall 2027"),
        ("fall 2025", "2258", "Fall 2025"),
        ("spring 2026", "2264", "Spring 2026"),
    ],
)
def test_voice_reaches_terms_beyond_the_old_table(spoken, expected, name):
    assert resolve_term(spoken) == expected, f"{spoken!r} should resolve to {name}"


def test_spring_2027_is_not_silently_answered_as_spring_2026():
    """The exact reported symptom: the old code returned DEFAULT_TERM here."""
    assert resolve_term("spring 2027") != "2264"
    assert resolve_term("spring 2027") == "2274"


def test_winter_2027_is_reachable():
    """Live in the SIS at every campus, and unreachable before this fix."""
    assert resolve_term("winter 2027") == "2272"


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 8, 14), "2268"),   # the day this was reported: Fall 2026
        (date(2026, 3, 1), "2264"),
        (date(2027, 1, 15), "2272"),
        (date(2027, 11, 2), "2278"),
    ],
)
def test_no_term_named_means_the_term_running_today(today, expected):
    assert resolve_term_code(None, today=today) == expected
    assert current_term_code(today) == expected


def test_a_bare_season_never_means_a_term_that_already_ended():
    # Asked in August 2026, "spring" must mean Spring 2027, not Spring 2026.
    assert resolve_term_code("spring classes", today=date(2026, 8, 14)) == "2274"


def test_term_code_construction():
    assert term_code("spring", 2027) == "2274"
    assert term_code("fall", 2026) == "2268"
    assert term_code("winter", 2027) == "2272"


def test_chat_and_voice_agree():
    """Two copies of this rule is what caused the bug; they must not diverge."""
    from app.services.chat import ChatOrchestrator

    orchestrator = ChatOrchestrator()
    for phrase in ["spring 2027", "winter 2027", "fall 2025", "summer 2027"]:
        assert orchestrator._extract_term_code(phrase) == resolve_term(phrase), phrase
