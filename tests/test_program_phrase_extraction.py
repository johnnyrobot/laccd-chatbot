"""Award words are matched separately and must not pollute the subject phrase
sent to the program full-text index.
"""

from __future__ import annotations

import pytest

from app.services.chat import ChatOrchestrator


@pytest.fixture
def orchestrator():
    return ChatOrchestrator()


@pytest.mark.parametrize(
    "message,expected",
    [
        ("I want an associate degree in psychology", "psychology"),
        ("I want to get an associate of science in biology", "biology"),
        ("what are the requirements for the nursing associate degree", "nursing"),
        ("I need a bachelor's in biomanufacturing", "biomanufacturing"),
        ("psychology associate in arts for transfer", "psychology"),
    ],
)
def test_award_and_filler_words_are_stripped(orchestrator, message, expected):
    assert orchestrator._extract_program_phrase(message) == expected


def test_a_program_whose_title_contains_an_award_word_is_still_findable(
    orchestrator,
):
    """'Cyber Security Associate' is a real program title. Stripping the award
    word still leaves enough to find it."""
    phrase = orchestrator._extract_program_phrase(
        "show me the cyber security associate program"
    )
    assert "cyber" in phrase and "security" in phrase


def test_subject_only_queries_are_unchanged(orchestrator):
    assert orchestrator._extract_program_phrase("psychology") == "psychology"
