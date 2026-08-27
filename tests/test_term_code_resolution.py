"""Term-code resolution must follow the calendar, not a hardcoded literal.

LACCD term codes are ``2`` + last-digit-of-year + season digit
(2=Winter, 4=Spring, 6=Summer, 8=Fall), so Fall 2026 is 2268 and
Spring 2027 is 2274.

The original implementation ended in a bare ``return "2264"`` (Spring 2026)
and enumerated only five terms, so every schedule question that did not name
a season answered from a term that had already ended, and no 2027 term was
reachable at all.

``today`` is injected so these tests keep their meaning as real time passes.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.chat import ChatOrchestrator
from app.services.live_schedule import get_term_name


@pytest.fixture
def orchestrator():
    return ChatOrchestrator()


# (today, expected code, expected human name)
CURRENT_TERM_CASES = [
    (date(2026, 8, 14), "2268", "Fall 2026"),   # the audit date
    (date(2026, 3, 1), "2264", "Spring 2026"),
    (date(2026, 6, 20), "2266", "Summer 2026"),
    (date(2027, 1, 15), "2272", "Winter 2027"),
    (date(2027, 11, 2), "2278", "Fall 2027"),
]


@pytest.mark.parametrize("today,expected,name", CURRENT_TERM_CASES)
def test_message_without_a_season_uses_the_current_term(
    orchestrator, today, expected, name
):
    got = orchestrator._extract_term_code("what math classes are offered", today=today)
    assert got == expected, f"on {today} the current term should be {name} ({expected})"
    assert get_term_name(got) == name


@pytest.mark.parametrize(
    "message,expected,name",
    [
        ("what is offered in spring 2027", "2274", "Spring 2027"),
        ("classes for fall 2027", "2278", "Fall 2027"),
        ("winter 2027 schedule", "2272", "Winter 2027"),
        ("summer 2027 courses", "2276", "Summer 2027"),
        ("fall 2025 sections", "2258", "Fall 2025"),
    ],
)
def test_explicit_season_and_year_is_honored_beyond_the_old_map(
    orchestrator, message, expected, name
):
    got = orchestrator._extract_term_code(message, today=date(2026, 8, 14))
    assert got == expected, f"{message!r} should resolve to {name} ({expected})"
    assert get_term_name(got) == name


@pytest.mark.parametrize(
    "message,expected,name",
    [
        # On 2026-08-14 these seasons have all passed for 2026, so a bare
        # season word means the NEXT one, never a term that already ended.
        ("is it offered in winter", "2272", "Winter 2027"),
        ("spring classes", "2274", "Spring 2027"),
        ("summer classes", "2276", "Summer 2027"),
        # Fall 2026 is in progress, so bare "fall" stays in the current year.
        ("fall classes", "2268", "Fall 2026"),
    ],
)
def test_bare_season_resolves_to_the_next_occurrence(
    orchestrator, message, expected, name
):
    got = orchestrator._extract_term_code(message, today=date(2026, 8, 14))
    assert got == expected, f"{message!r} should resolve to {name} ({expected})"


def test_current_term_is_never_a_term_that_already_ended(orchestrator):
    """Regression guard for the hardcoded Spring 2026 default."""
    today = date(2026, 8, 14)
    got = orchestrator._extract_term_code("what classes are offered", today=today)
    assert got != "2264", (
        "Spring 2026 ended 2026-06-08; it must not be the default in August 2026"
    )
