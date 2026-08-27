"""Sequence output enrichment: gateway flags, milestones, season, and pace.

These fields are pure post-processing on the generated sequence — they don't
require Neo4j or external services and never break existing consumers.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.services.course_sequence_service import (
    CourseSequenceService,
    is_gateway_course,
    is_gateway_english,
    is_gateway_math,
)

EMPTY_GRAPH = {"prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {}}


def _course_element(code: str, *, term: int, units: float = 3.0,
                    title: str | None = None, requirement: str = "MAJOR_REQUIRED") -> dict:
    return {
        "name": code,
        "shortDescription": title or code,
        "recommendedOpportunity": {"term": {"termNumber": term}, "minUnits": units},
        "requirement": {"requirementType": requirement},
    }


def _milestone_element(label: str, *, term: int) -> dict:
    return {
        "name": label,
        "shortDescription": label,
        "recommendedOpportunity": {"term": {"termNumber": term}},
        "requirement": {"requirementType": "MILESTONE"},
    }


def _generate(elements: list, *, award_title: str = "Associate of Science",
              max_units: int = 15) -> dict:
    svc = CourseSequenceService("LAMC")
    info = {"title": "Synthetic Program", "awardTitle": award_title}

    async def fake_pm(*_a, **_kw): return info, elements
    async def fake_graph(*_a, **_kw): return deepcopy(EMPTY_GRAPH)
    async def fake_metadata(*_a, **_kw): return {}

    svc._get_program_requirements = fake_pm
    svc._build_full_dependency_graph = fake_graph
    svc._fetch_course_metadata = fake_metadata

    return asyncio.run(svc.generate_sequence("synthetic", "LAMC", max_units_per_term=max_units))


# ── 5a: is_gateway flag ─────────────────────────────────────────────────────

@pytest.mark.parametrize("code, expected", [
    ("ENGLISH 101", True),
    ("ENGL C1000", True),
    ("ENG 28", True),
    ("MATH 261", True),
    ("MATH C1000", True),
    ("STAT 101", True),
    ("STATISTICS 1", True),
    ("STAT C1000", True),
    ("PSYC C1000", False),
    ("CIS 101", False),
    ("WELDG/E 111", False),
    ("ADM JUS 001", False),
])
def test_gateway_predicate_classifies_known_subjects(code, expected):
    assert is_gateway_course(code) is expected, f"is_gateway_course({code!r}) wrong"


def test_english_and_math_predicates_are_orthogonal():
    assert is_gateway_english("ENGL C1000") and not is_gateway_math("ENGL C1000")
    assert is_gateway_math("MATH 261") and not is_gateway_english("MATH 261")
    assert is_gateway_math("STAT 101") and not is_gateway_english("STAT 101")


def test_gateway_flag_appears_in_course_output():
    elements = [
        _course_element("ENGLISH 101", term=1, units=3),
        _course_element("MATH 261", term=1, units=5),
        _course_element("CIS 101", term=1, units=3),
    ]
    out = _generate(elements)
    by_code = {c["code"]: c for term in out["sequence"] for c in term["courses"]}
    assert by_code["ENGLISH 101"]["is_gateway"] is True
    assert by_code["MATH 261"]["is_gateway"] is True
    assert by_code["CIS 101"]["is_gateway"] is False


def test_generic_placeholder_never_marked_gateway():
    elements = [
        # Placeholder that happens to mention "math" in display name — must not
        # be flagged as a gateway course.
        {
            "name": "MATH GE Elective",
            "shortDescription": "MATH GE Elective",
            "recommendedOpportunity": {"term": {"termNumber": 1}, "minUnits": 3},
            "requirement": {"requirementType": "ELECTIVE"},
        },
    ]
    out = _generate(elements)
    course = out["sequence"][0]["courses"][0]
    assert course["is_gateway"] is False, (
        f"placeholder rows must never be is_gateway=True, got {course}"
    )


# ── 5b: milestones ──────────────────────────────────────────────────────────

def test_pm_milestone_rows_surface_in_output():
    elements = [
        _course_element("CS 101", term=1, units=3),
        _milestone_element("Student Educational Plan", term=1),
        _milestone_element("Petition to Graduate", term=4),
    ]
    out = _generate(elements)
    labels = {m["label"] for m in out["milestones"]}
    assert "Student Educational Plan" in labels
    assert "Petition to Graduate" in labels
    pm_milestones = [m for m in out["milestones"] if m["source"] == "pm"]
    assert len(pm_milestones) == 2


def test_milestones_sorted_by_term():
    elements = [
        _course_element("CS 101", term=1, units=3),
        _milestone_element("Petition to Graduate", term=4),
        _milestone_element("Student Educational Plan", term=1),
        _milestone_element("Apply for Transfer", term=3),
    ]
    out = _generate(elements)
    terms = [m["term"] for m in out["milestones"]]
    assert terms == sorted(terms), f"milestones not term-sorted: {out['milestones']}"


def test_derived_gateway_milestone_added_when_english_in_program():
    elements = [
        _course_element("ENGLISH 101", term=2, units=3),
        _course_element("CIS 101", term=1, units=3),
    ]
    out = _generate(elements)
    derived = [m for m in out["milestones"] if m["source"] == "derived"]
    english_milestones = [m for m in derived if "english" in m["label"].lower()]
    assert english_milestones, f"no English gateway milestone in {out['milestones']}"
    assert english_milestones[0]["term"] == 2


def test_derived_math_milestone_uses_first_math_term():
    elements = [
        _course_element("CIS 101", term=1, units=3),
        _course_element("MATH 261", term=1, units=5),
        _course_element("MATH 262", term=2, units=4),
    ]
    out = _generate(elements)
    math_milestones = [m for m in out["milestones"]
                       if m["source"] == "derived" and "math" in m["label"].lower()]
    assert math_milestones, f"no Math milestone in {out['milestones']}"
    # First math course is in term 1, so the milestone term should be 1
    assert math_milestones[0]["term"] == 1


def test_aat_program_gets_transfer_application_milestone():
    elements = [_course_element("PSYC C1000", term=1, units=3)]
    out = _generate(elements, award_title="Associate in Arts for Transfer")
    derived = [m for m in out["milestones"] if m["source"] == "derived"]
    assert any("transfer" in m["label"].lower() for m in derived), (
        f"AA-T should add a transfer reminder; got {out['milestones']}"
    )


def test_non_aat_program_does_not_get_transfer_milestone():
    elements = [_course_element("CIS 101", term=1, units=3)]
    out = _generate(elements, award_title="Certificate of Achievement")
    assert not any("transfer" in m["label"].lower() for m in out["milestones"])


def test_pm_milestone_suppresses_duplicate_derived_marker():
    """If PM authored 'Apply for Transfer', the derived 'Submit transfer
    application' should NOT also fire (they're duplicates from a user POV)."""
    elements = [
        _course_element("PSYC C1000", term=1, units=3),
        _milestone_element("Apply for Transfer (if applicable)", term=4),
    ]
    out = _generate(elements, award_title="Associate in Arts for Transfer")
    transfer_milestones = [m for m in out["milestones"] if "transfer" in m["label"].lower()]
    assert len(transfer_milestones) == 1, (
        f"only PM-authored transfer milestone should appear; got {transfer_milestones}"
    )
    assert transfer_milestones[0]["source"] == "pm"


def test_no_milestones_when_program_has_none():
    elements = [_course_element("WELDG/E 111", term=1, units=6)]
    out = _generate(elements)
    # No PM milestones, no gateway courses → empty milestones list (or only AA-T-derived if applicable)
    assert all(m["source"] == "derived" for m in out["milestones"])
    # Welding has no English/Math gateways and isn't AA-T
    assert out["milestones"] == []


# ── 5c: season + pace ───────────────────────────────────────────────────────

def test_term_includes_season_field():
    elements = [
        _course_element("CS 101", term=1, units=3),
        _course_element("CS 102", term=2, units=3),
    ]
    out = _generate(elements)
    seasons = {t["term_number"]: t["season"] for t in out["sequence"]}
    # Default convention: odd → Fall, even → Spring
    assert seasons.get(1) == "Fall"
    assert seasons.get(2) == "Spring"


def test_season_present_on_generic_term_overflow():
    """If generic placement creates a fresh term beyond the scheduled ones,
    that term must also have the season field."""
    # Force a placeholder to land in a far-future term by giving it pm_term=8
    elements = [
        _course_element("CS 101", term=1, units=3),
        {  # Generic placeholder forced into term 8
            "name": "Senior Capstone Project",
            "shortDescription": "Capstone",
            "recommendedOpportunity": {"term": {"termNumber": 8}, "minUnits": 3},
            "requirement": {"requirementType": "ELECTIVE"},
        },
    ]
    out = _generate(elements)
    for term in out["sequence"]:
        assert "season" in term, f"missing season on term {term['term_number']}"


def test_pace_field_present_with_default_full():
    elements = [_course_element("CS 101", term=1, units=3)]
    out = _generate(elements)
    assert out.get("pace") == "full"


# ── Integration: full payload shape sanity ─────────────────────────────────

def test_track5_fields_all_present_for_typical_program():
    elements = [
        _course_element("ENGLISH 101", term=1, units=3),
        _course_element("MATH 261", term=1, units=5),
        _course_element("CS 101", term=1, units=3),
        _milestone_element("Petition to Graduate", term=4),
    ]
    out = _generate(elements)
    # New top-level fields
    assert "pace" in out
    assert "milestones" in out
    # New term field
    for term in out["sequence"]:
        assert "season" in term
    # New per-course field
    for term in out["sequence"]:
        for course in term["courses"]:
            assert "is_gateway" in course
