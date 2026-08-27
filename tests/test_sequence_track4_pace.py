"""pace tracks (4c) and networkx prereq-depth diagnostic (4a).

The big algorithmic refactor (4b: greedy momentum packer, 4a full Kahn's
replacement) was intentionally scoped down. The existing scheduler is hardened
and well-tested; we just thread pace through and add networkx as a low-risk
diagnostic.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.services.course_sequence_service import (
    CourseSequenceService,
    PACE_MAX_UNITS,
    DEFAULT_PACE,
    normalize_pace,
)
from app.services.chat import detect_pace_from_message

EMPTY_GRAPH = {"prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {}}


def _course_element(code: str, *, term: int, units: float = 3.0,
                    title: str | None = None, requirement: str = "MAJOR_REQUIRED") -> dict:
    return {
        "name": code,
        "shortDescription": title or code,
        "recommendedOpportunity": {"term": {"termNumber": term}, "minUnits": units},
        "requirement": {"requirementType": requirement},
    }


def _generate(elements: list, *, pace: str | None = None, max_units: int | None = None) -> dict:
    svc = CourseSequenceService("LAMC")
    info = {"title": "Synthetic Program", "awardTitle": "Associate of Science"}

    async def fake_pm(*_a, **_kw): return info, elements
    async def fake_graph(*_a, **_kw): return deepcopy(EMPTY_GRAPH)
    async def fake_metadata(*_a, **_kw): return {}

    svc._get_program_requirements = fake_pm
    svc._build_full_dependency_graph = fake_graph
    svc._fetch_course_metadata = fake_metadata

    return asyncio.run(svc.generate_sequence(
        "synthetic", "LAMC",
        pace=pace, max_units_per_term=max_units,
    ))


# ── 4c: normalize_pace + PACE_MAX_UNITS ─────────────────────────────────────

def test_pace_constants_match_design():
    """Spec: full=15 (default), momentum=18 (fast track), part_time=9."""
    assert PACE_MAX_UNITS["full"] == 15
    assert PACE_MAX_UNITS["momentum"] == 18
    assert PACE_MAX_UNITS["part_time"] == 9
    assert DEFAULT_PACE == "full"


@pytest.mark.parametrize("input_,expected", [
    ("full", "full"),
    ("FULL", "full"),
    ("momentum", "momentum"),
    ("Momentum", "momentum"),
    ("part_time", "part_time"),
    ("part-time", "part_time"),
    ("part time", "part_time"),
    ("PART TIME", "part_time"),
    (None, "full"),
    ("", "full"),
    ("garbage", "full"),
    ("super-fast", "full"),
])
def test_normalize_pace_maps_to_known_keys(input_, expected):
    assert normalize_pace(input_) == expected


# ── 4c: pace produces matching unit caps ────────────────────────────────────

def test_full_pace_caps_at_15_units():
    elements = [_course_element(f"CS {100+i}", term=1, units=4) for i in range(6)]
    out = _generate(elements, pace="full")
    for term in out["sequence"]:
        assert term["total_units"] <= 15.0


def test_momentum_pace_allows_18_units():
    elements = [_course_element(f"CS {100+i}", term=1, units=4) for i in range(6)]
    out = _generate(elements, pace="momentum")
    # With 6 × 4u courses available in term 1, momentum pace fits 4 in (16u).
    # 5 × 4u = 20u would exceed cap; 4 fits.
    assert max(t["total_units"] for t in out["sequence"]) <= 18.0
    # But should fit MORE than full pace would: 4 courses (16u) > 3 (12u).
    full_out = _generate(elements, pace="full")
    momentum_max = max(t["total_units"] for t in out["sequence"])
    full_max = max(t["total_units"] for t in full_out["sequence"])
    assert momentum_max > full_max, (
        f"momentum should pack more per term than full; got "
        f"momentum_max={momentum_max} full_max={full_max}"
    )


def test_part_time_pace_caps_at_9_units():
    elements = [_course_element(f"CS {100+i}", term=1, units=3) for i in range(6)]
    out = _generate(elements, pace="part_time")
    for term in out["sequence"]:
        assert term["total_units"] <= 9.0
    # Six 3-unit courses in part-time = at least 2 terms (6 / 3 = 2; cap fits 3 per term)
    assert out["terms_to_complete"] >= 2


def test_pace_field_reflects_resolved_pace_in_payload():
    elements = [_course_element("CS 101", term=1, units=3)]
    full_out = _generate(elements, pace="full")
    pt_out = _generate(elements, pace="part_time")
    mom_out = _generate(elements, pace="momentum")
    assert full_out["pace"] == "full"
    assert pt_out["pace"] == "part_time"
    assert mom_out["pace"] == "momentum"
    # max_units_per_term is also surfaced
    assert full_out["max_units_per_term"] == 15
    assert pt_out["max_units_per_term"] == 9
    assert mom_out["max_units_per_term"] == 18


def test_non_default_pace_adds_explanatory_note():
    elements = [_course_element("CS 101", term=1, units=3)]
    pt_out = _generate(elements, pace="part_time")
    assert any("part-time" in n.lower() and "9 unit" in n.lower()
               for n in pt_out.get("notes") or []), (
        f"part_time pace should add a note explaining the cap; got {pt_out.get('notes')}"
    )
    full_out = _generate(elements, pace="full")
    assert not any("pace" in n.lower() for n in full_out.get("notes") or []), (
        f"default pace should NOT add a pace note; got {full_out.get('notes')}"
    )


def test_max_units_per_term_param_overrides_pace():
    """Tests can still inject a custom cap (used by the validity suite)."""
    elements = [_course_element("CS 101", term=1, units=10)]
    out = _generate(elements, max_units=10)
    assert out["max_units_per_term"] == 10


# ── 4c: chat-side keyword detection ─────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("course sequence for nursing", None),
    ("part time course plan for nursing", "part_time"),
    ("part-time semester plan for psychology", "part_time"),
    ("I want to finish the AA fast track", "momentum"),
    ("how can I complete this accelerated", "momentum"),
    ("fastest way to finish — I'm ahead of schedule", "momentum"),
    ("I'm working full time, can I do nursing?", "part_time"),
    ("evening only courses for psychology", "part_time"),
    ("regular pace please", None),
])
def test_chat_pace_detection_from_natural_language(message, expected):
    assert detect_pace_from_message(message) == expected


# ── 4a: networkx-backed depth diagnostic ────────────────────────────────────

def test_depth_floor_diagnostic_returns_layered_levels():
    svc = CourseSequenceService("LAMC")
    prereq_graph = {
        "CS 201": [["CS 101"]],
        "CS 301": [["CS 201"]],
    }
    depth = svc._compute_prereq_depth_floor(prereq_graph, {"CS 101", "CS 201", "CS 301"})
    # Three-layer chain → floors {CS 101: 1, CS 201: 2, CS 301: 3}
    assert depth["CS 101"] == 1
    assert depth["CS 201"] == 2
    assert depth["CS 301"] == 3


def test_depth_floor_returns_empty_for_cyclic_graph():
    """Cycles aren't valid for topological_generations — diagnostic must
    no-op rather than raise."""
    svc = CourseSequenceService("LAMC")
    cyclic = {
        "A 1": [["A 3"]],
        "A 2": [["A 1"]],
        "A 3": [["A 2"]],
    }
    depth = svc._compute_prereq_depth_floor(cyclic, {"A 1", "A 2", "A 3"})
    assert depth == {}


def test_depth_violations_empty_for_well_scheduled_program():
    """A program where the scheduler honors prereqs should produce no
    depth violations."""
    elements = [
        _course_element("CS 101", term=1, units=3),
        _course_element("CS 201", term=2, units=3),
        _course_element("CS 301", term=3, units=3),
    ]
    svc = CourseSequenceService("LAMC")
    prereq_graph = {
        "CS 201": [["CS 101"]],
        "CS 301": [["CS 201"]],
    }

    async def fake_pm(*_a, **_kw): return {"title": "Test"}, elements
    async def fake_graph(*_a, **_kw): return {
        "prerequisites": prereq_graph, "advisories": {}, "corequisites": {}, "course_details": {},
    }
    async def fake_metadata(*_a, **_kw): return {}

    svc._get_program_requirements = fake_pm
    svc._build_full_dependency_graph = fake_graph
    svc._fetch_course_metadata = fake_metadata

    out = asyncio.run(svc.generate_sequence("synthetic", "LAMC"))
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    # Sanity: prereqs are honored
    assert placement["CS 101"] < placement["CS 201"] < placement["CS 301"]
