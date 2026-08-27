"""Unit tests for district-wide regulatory facts injected into the RAG prompt.

Pure-logic tests — no network, no Gemini. Campus corpora returned incompatible
answers to the same district-wide Title 5 question, so these tests pin the
reviewed rule and its effective-date boundary.

The date-boundary tests pass an explicit ``today`` rather than reading the clock,
so they keep asserting the same thing on both sides of the change date.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shared.district_facts import (
    CPL_RESIDENCY_CHANGE_DATE,
    DISTRICT_FACTS,
    district_facts_for,
)

BEFORE = CPL_RESIDENCY_CHANGE_DATE - timedelta(days=1)
ON = CPL_RESIDENCY_CHANGE_DATE
AFTER = CPL_RESIDENCY_CHANGE_DATE + timedelta(days=90)

# The exact question that produced contradictory answers across nine campuses.
CPL_QUESTION = (
    "Can credit for prior learning count toward the 12 units of residence "
    "I need for my associate degree?"
)


# ----- firing -----


def test_fires_on_the_question_that_broke_prod():
    assert district_facts_for(CPL_QUESTION, today=BEFORE) != ""


@pytest.mark.parametrize(
    "question",
    [
        "does CPL count toward residency",
        "will my military training count toward the 12 units in residence",
        "can credit by examination satisfy the residence requirement",
        "Does credit for prior learning apply to resident credit?",
    ],
)
def test_fires_on_phrasing_variants(question):
    assert district_facts_for(question, today=BEFORE) != ""


# ----- not firing (precision) -----


def test_does_not_fire_on_california_residency_for_tuition():
    """"Residency" also means tuition residency — a completely different rule."""
    q = "Am I a California resident for tuition purposes if I moved here last year?"
    assert district_facts_for(q, today=BEFORE) == ""


def test_does_not_fire_on_prior_learning_without_residency():
    q = "How do I get credit for prior learning from my industry certification?"
    assert district_facts_for(q, today=BEFORE) == ""


@pytest.mark.parametrize(
    "question",
    [
        "what are the discipline requirements for the major",  # contains 'cpl' as substring
        "can I duplicate a course",
        "when does the fall term start",
        "",
    ],
)
def test_does_not_fire_on_unrelated_questions(question):
    assert district_facts_for(question, today=BEFORE) == ""


# ----- correctness across the date boundary -----


def test_before_the_change_states_the_old_rule():
    out = district_facts_for(CPL_QUESTION, today=BEFORE)
    assert "shall NOT be counted" in out
    assert "55050(h)" in out
    assert "REVERSES" in out


def test_on_the_change_date_states_the_new_rule():
    """The boundary is inclusive: the new rule applies ON 2026-09-09."""
    out = district_facts_for(CPL_QUESTION, today=ON)
    assert "MAY be counted" in out
    assert "55050(d)" in out
    assert "shall NOT be counted" not in out


def test_after_the_change_states_the_new_rule_and_the_history():
    out = district_facts_for(CPL_QUESTION, today=AFTER)
    assert "MAY be counted" in out
    assert "CHANGED" in out
    # A student denied under the old rule needs to know it moved.
    assert "counselor" in out


@pytest.mark.parametrize("today", [BEFORE, ON, AFTER])
def test_effective_date_is_always_stated(today):
    """spec_class_advisement.md §4.4 — every factual line carries its vintage.

    The prod failure was nine confident answers, none carrying a date.
    """
    out = district_facts_for(CPL_QUESTION, today=today)
    assert "2026" in out
    assert "September" in out


@pytest.mark.parametrize("today", [BEFORE, ON, AFTER])
def test_never_predicts_the_outcome_of_a_petition(today):
    """Stating the criteria is advising; predicting the award is a determination."""
    out = district_facts_for(CPL_QUESTION, today=today)
    assert "counselor" in out
    assert "Do not predict" in out


@pytest.mark.parametrize("today", [BEFORE, ON, AFTER])
def test_tells_the_model_to_override_a_conflicting_campus_page(today):
    """The whole point: nine campus sites disagree and must not win."""
    out = district_facts_for(CPL_QUESTION, today=today)
    assert "OVERRIDE" in out
    assert "same at all nine" in out.lower()


# ----- registry hygiene -----


def test_every_fact_requires_at_least_two_term_groups():
    """One group is a substring match waiting to misfire; two keeps precision up."""
    for fact in DISTRICT_FACTS:
        assert len(fact.term_groups) >= 2, fact.name
        assert all(group for group in fact.term_groups), fact.name


def test_fact_names_are_unique():
    names = [f.name for f in DISTRICT_FACTS]
    assert len(names) == len(set(names))


def test_block_is_prefixed_so_it_concatenates_onto_a_system_prompt():
    """It is appended to an existing prompt, so it must open with separation."""
    out = district_facts_for(CPL_QUESTION, today=BEFORE)
    assert out.startswith("\n\n")


def test_default_today_uses_the_clock_and_still_returns_a_fact():
    """No explicit date — should still fire, whichever side of the boundary we run on."""
    out = district_facts_for(CPL_QUESTION)
    assert "55050" in out
    # Both framings appear on purpose before the change date (the rule in force,
    # plus the reversal). What must be unambiguous is which one is current.
    assert out.count("This is the rule in force today.") == 1


@pytest.mark.parametrize("today", [BEFORE, ON, AFTER])
def test_exactly_one_rule_is_marked_as_currently_in_force(today):
    """The pre-change text names both rules; only one may be flagged as current."""
    out = district_facts_for(CPL_QUESTION, today=today)
    assert out.count("This is the rule in force today.") == 1


# ----- integration: the block actually reaches the model prompt -----


def test_answer_injects_district_facts_into_the_prompt(monkeypatch):
    """The helper being correct is worthless if answer() never sends it.

    Stubs the Gemini client and captures the prompt that would have been sent.
    """
    from app.services import gemini_file_search as gfs

    captured = {}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            captured["prompt"] = contents

            class _R:
                text = "ok"
                candidates = []

            return _R()

    class _FakeClient:
        models = _FakeModels()

    svc = gfs.GeminiFileSearchService()
    monkeypatch.setattr(type(svc), "client", property(lambda self: _FakeClient()))
    monkeypatch.setattr(svc, "get_store_for_campus", lambda code: "stores/fake")
    monkeypatch.setattr(gfs, "types", _StubTypes())
    monkeypatch.setattr(gfs.get_client(), "update_current_generation", lambda **kw: None)

    svc.answer(CPL_QUESTION, system_prompt="SYSTEM.", campus_code="LAMC")

    assert "AUTHORITATIVE DISTRICT-WIDE FACTS" in captured["prompt"]
    assert "55050" in captured["prompt"]
    assert captured["prompt"].startswith("SYSTEM.")


def test_answer_leaves_unrelated_questions_untouched(monkeypatch):
    """No fact matched -> the prompt is exactly what it was before this change."""
    from app.services import gemini_file_search as gfs

    captured = {}

    class _FakeModels:
        def generate_content(self, *, model, contents, config):
            captured["prompt"] = contents

            class _R:
                text = "ok"
                candidates = []

            return _R()

    class _FakeClient:
        models = _FakeModels()

    svc = gfs.GeminiFileSearchService()
    monkeypatch.setattr(type(svc), "client", property(lambda self: _FakeClient()))
    monkeypatch.setattr(svc, "get_store_for_campus", lambda code: "stores/fake")
    monkeypatch.setattr(gfs, "types", _StubTypes())
    monkeypatch.setattr(gfs.get_client(), "update_current_generation", lambda **kw: None)

    svc.answer("when does the fall term start", system_prompt="SYSTEM.", campus_code="LAMC")

    assert captured["prompt"] == "SYSTEM.\n\nQuestion:\nwhen does the fall term start"


class _StubTypes:
    """Minimal stand-in for google.genai.types so answer() can build its config."""

    class Tool:
        def __init__(self, **kw):
            pass

    class FileSearch:
        def __init__(self, **kw):
            pass

    class ThinkingConfig:
        def __init__(self, **kw):
            pass

    class GenerateContentConfig:
        def __init__(self, **kw):
            pass


# ----- exam credit fact (#49) -----

from shared.district_facts import EXAM_CREDIT_CHANGE_DATE  # noqa: E402

EXAM_BEFORE = EXAM_CREDIT_CHANGE_DATE - timedelta(days=1)
EXAM_ON = EXAM_CREDIT_CHANGE_DATE

# The question LAMC answered with 0 citations and a pure deflection.
EXAM_QUESTION = "If I passed an AP exam in high school, will the college give me credit for it?"


def test_exam_fact_fires_on_the_question_lamc_could_not_answer():
    assert "55052" in district_facts_for(EXAM_QUESTION, today=EXAM_BEFORE)


@pytest.mark.parametrize(
    "question",
    [
        "do my AP scores count toward general education",
        "will an International Baccalaureate exam satisfy a requirement",
        "does CLEP credit transfer",
        "I have advanced placement credit, can I skip the course",
    ],
)
def test_exam_fact_fires_on_variants(question):
    assert "55052" in district_facts_for(question, today=EXAM_BEFORE)


@pytest.mark.parametrize(
    "question",
    [
        "when do applications open",
        "who is my counselor",
        "how many units is the AA degree",
    ],
)
def test_exam_fact_does_not_fire_on_unrelated(question):
    assert "55052" not in district_facts_for(question, today=EXAM_BEFORE)


def test_exam_fact_is_permissive_before_and_mandatory_after():
    before = district_facts_for(EXAM_QUESTION, today=EXAM_BEFORE)
    after = district_facts_for(EXAM_QUESTION, today=EXAM_ON)
    assert "**may** adopt" in before
    assert "**shall** adopt" in after
    assert "shall award credit" in after


@pytest.mark.parametrize("today", [EXAM_BEFORE, EXAM_ON])
def test_exam_fact_never_states_a_score_or_promises_a_waiver(today):
    """Score thresholds and equivalencies are the college's determination."""
    out = district_facts_for(EXAM_QUESTION, today=today)
    assert "Do NOT state a specific minimum score" in out
    assert "determination" in out
    assert "Admissions and Records or a counselor" in out


@pytest.mark.parametrize("today", [EXAM_BEFORE, EXAM_ON])
def test_exam_fact_carries_its_effective_date(today):
    out = district_facts_for(EXAM_QUESTION, today=today)
    assert "September" in out and "2026" in out


def test_both_facts_can_fire_together_without_colliding():
    """A question spanning both rules should get both blocks, once each."""
    q = "Can AP exam credit or credit for prior learning count toward the 12 units of residence?"
    out = district_facts_for(q, today=EXAM_BEFORE)
    assert "55050" in out and "55052" in out
    assert out.count("AUTHORITATIVE DISTRICT-WIDE FACTS") == 1
