"""Unit tests for ASSIST query classifier + helper methods on ChatOrchestrator.

Pure-logic tests — no network, no Neo4j. Integration tests live in
``tests/test_assist_articulation.py`` (marked @pytest.mark.integration).
"""

from __future__ import annotations

import pytest

from app.services.chat import ChatOrchestrator


@pytest.fixture
def orchestrator():
    # ChatOrchestrator does not make network calls during construction
    return ChatOrchestrator()


# ----- _is_assist_query -----


def test_explicit_trigger_articulation(orchestrator):
    q = "show me the articulation for cs"
    assert orchestrator._is_assist_query(q, q.lower())


def test_explicit_trigger_assist_org(orchestrator):
    q = "what does assist.org say about MATH 261"
    assert orchestrator._is_assist_query(q, q.lower())


def test_target_institution_with_transfer_verb(orchestrator):
    q = "does MATH 261 transfer to UCLA"
    assert orchestrator._is_assist_query(q, q.lower())


def test_target_institution_alone_does_not_trigger(orchestrator):
    """Bare 'UCLA' without a transfer verb should NOT route to ASSIST."""
    q = "what is UCLA's address"
    assert not orchestrator._is_assist_query(q, q.lower())


def test_unrelated_query_does_not_trigger(orchestrator):
    q = "what is the math 261 prerequisite"
    assert not orchestrator._is_assist_query(q, q.lower())


def test_csun_with_transfer(orchestrator):
    q = "i want to transfer to csun"
    assert orchestrator._is_assist_query(q, q.lower())


# ----- system-level transferability belongs to the KG, not ASSIST -----
#
# "Is STAT C1000 transferable to UC and CSU?" is advanced sample A1 on the page
# (an `advanced-btn`). ASSIST was stealing it because "transferable to" is an explicit
# trigger — then demanding a receiving campus the student can't name, because
# they meant the SYSTEMS. Answering "both" looped forever: no campus name, so
# the same clarify-back came back verbatim, with no exit.
#
# UC/CSU transferability is a course attribute (uc_transferable/csu_transferable)
# the knowledge graph answers outright, with no campus and no clarify at all.
# ASSIST is only for major-specific articulation to a NAMED campus.


@pytest.mark.parametrize("q", [
    "Is STAT C1000 transferable to UC and CSU?",   # sample A1
    "Does MATH 265 transfer to CSU?",              # sample A5
    "is MATH 261 transferable to a UC",
    "does ENGLISH C1000 transfer to UC and CSU",
    "Is MATH 261 UC transferable?",
    "Is ENGLISH C1000 CSU transferable?",
])
def test_system_level_transfer_goes_to_kg_not_assist(orchestrator, q):
    lowered = q.lower()
    assert orchestrator._is_system_level_transfer_query(q, lowered), q
    assert not orchestrator._is_assist_query(q, lowered), f"ASSIST stole a KG question: {q!r}"


@pytest.mark.parametrize("q", [
    "does MATH 261 transfer to UCLA",
    "is STAT C1000 transferable to CSUN",
    "show me the articulation for computer science",   # no campus yet — ASSIST asks
])
def test_named_campus_or_articulation_still_routes_to_assist(orchestrator, q):
    lowered = q.lower()
    assert not orchestrator._is_system_level_transfer_query(q, lowered), q
    assert orchestrator._is_assist_query(q, lowered), q


def test_system_level_requires_a_course_code(orchestrator):
    """'How do I transfer to a CSU?' is general advice, not a course lookup —
    it must not be forced into the KG's course-transferability handler."""
    q = "how do I transfer to a CSU or UC"
    assert not orchestrator._is_system_level_transfer_query(q, q.lower())


# ----- _detect_assist_subintent -----


def test_subintent_course_transfer_check(orchestrator):
    q = "does MATH 261 transfer to UCLA"
    assert orchestrator._detect_assist_subintent(q, q.lower()) == "course_transfer_check"


def test_subintent_list_majors(orchestrator):
    q = "what majors transfer to UCLA"
    assert orchestrator._detect_assist_subintent(q, q.lower()) == "list_majors"


def test_subintent_articulation_for_major(orchestrator):
    q = "show me the articulation for computer science at UCLA"
    assert orchestrator._detect_assist_subintent(q, q.lower()) == "articulation_for_major"


def test_subintent_transfer_program_lists_majors(orchestrator):
    """'Does LAMC have a transfer program to UCLA?' (sample #103) asks WHETHER
    agreements exist — answer by listing them, don't demand a major first."""
    q = "Does LAMC have a transfer program to UCLA?"
    assert orchestrator._detect_assist_subintent(q, q.lower()) == "list_majors"


def test_subintent_course_wins_over_list(orchestrator):
    """If a course code is present, course_transfer_check wins over list_majors."""
    q = "what majors does MATH 261 fit at UCLA"
    assert orchestrator._detect_assist_subintent(q, q.lower()) == "course_transfer_check"


# ----- _fuzzy_match_major -----


def _major(label: str) -> dict:
    return {"label": label, "key": f"k-{label}"}


def test_fuzzy_match_finds_cs(orchestrator):
    majors = [
        _major("COMPUTER SCIENCE, B.S."),
        _major("ENGLISH, B.A."),
        _major("PSYCHOLOGY, B.A."),
    ]
    match = orchestrator._fuzzy_match_major("show me computer science articulation", majors)
    assert match is not None
    assert match["label"] == "COMPUTER SCIENCE, B.S."


def test_fuzzy_match_strips_degree_code(orchestrator):
    majors = [_major("ANIMAL SCIENCE, B.S.")]
    match = orchestrator._fuzzy_match_major("animal science articulation", majors)
    assert match is not None


def test_fuzzy_match_returns_none_below_threshold(orchestrator):
    majors = [_major("COMPUTER SCIENCE, B.S.")]
    # No tokens overlap with "computer science"
    assert orchestrator._fuzzy_match_major("show me majors", majors) is None


def test_fuzzy_match_psychology_among_many(orchestrator):
    majors = [
        _major("COMPUTER SCIENCE, B.S."),
        _major("PSYCHOLOGY, B.A."),
        _major("BUSINESS ADMINISTRATION, B.S."),
    ]
    match = orchestrator._fuzzy_match_major("psychology to ucla", majors)
    assert match is not None
    assert match["label"] == "PSYCHOLOGY, B.A."


def test_fuzzy_match_ignores_stop_words(orchestrator):
    """Stop words like 'major', 'transfer', 'ucla' must not contribute to scoring."""
    majors = [_major("MATHEMATICS, B.S.")]
    # Without stop-word filtering, 'major' might match nothing useful — but
    # 'mathematics' should still trigger
    match = orchestrator._fuzzy_match_major("mathematics major to UCLA", majors)
    assert match is not None


# ----- _format_major_clarify -----
#
# The clarify-back used to name a hypothetical major ("...may articulate as
# MATH 31A for Computer Science but not for English"). On the next turn the
# context rewriter folded that example into the standalone query and the
# orchestrator answered for Computer Science — a major the student never chose.
# The clarify-back must offer only REAL majors from the live agreement list.

_UCLA_MAJORS = [
    _major("MATHEMATICS, B.S."),
    _major("COMPUTER SCIENCE, B.S."),
    _major("ENGLISH, B.A."),
    _major("PSYCHOLOGY, B.A."),
]


def test_major_clarify_lists_real_majors(orchestrator):
    msg = orchestrator._format_major_clarify("Is STAT C1000 transferable to UCLA?", _UCLA_MAJORS)
    assert "MATHEMATICS, B.S." in msg
    assert "PSYCHOLOGY, B.A." in msg


def test_major_clarify_has_no_hypothetical_example(orchestrator):
    """No invented major/course example — that is what leaked into the rewrite."""
    msg = orchestrator._format_major_clarify("Is STAT C1000 transferable to UCLA?", _UCLA_MAJORS)
    assert "MATH 261" not in msg
    assert "but not for" not in msg


def test_major_clarify_flags_a_course_code_reply(orchestrator):
    """Student answered 'which major?' with a course code. Saying the identical
    thing again just loops; call out that a course code is not a major."""
    # Two course codes = the original course + the course given where a major belongs.
    msg = orchestrator._format_major_clarify(
        "Is STAT C1000 transferable to UCLA for MATH 31A?", _UCLA_MAJORS
    )
    assert "course code isn't a major" in msg.lower()


def test_major_clarify_does_not_scold_on_first_ask(orchestrator):
    """A single course code is the normal first clarify — don't accuse the
    student of naming a course when they simply haven't answered yet."""
    msg = orchestrator._format_major_clarify("Is STAT C1000 transferable to UCLA?", _UCLA_MAJORS)
    assert "course code isn't a major" not in msg.lower()


def test_major_clarify_survives_empty_major_list(orchestrator):
    msg = orchestrator._format_major_clarify("Is STAT C1000 transferable to UCLA?", [])
    assert "major" in msg.lower()
