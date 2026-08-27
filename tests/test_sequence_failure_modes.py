"""Failure-mode scenarios for CourseSequenceService.generate_sequence().

Each test crafts a small synthetic (elements, graph) pair that drives a specific
edge-case code path:

  - Empty pathwayElements (Neo4j fallback returns nothing)
  - Pathway present but eLumen / Neo4j returned no prereqs (graph all-empty)
  - Circular prereq chain (cycle break path in _topological_sort_with_terms)
  - Course code that is renumbered in PM (CCN-style: STAT C1000)

These complement tests/test_sequence_validity.py which runs against
recorded real-world fixtures.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.services.course_sequence_service import CourseSequenceService

EMPTY_GRAPH = {"prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {}}


def _course_element(code: str, *, term: int, units: float = 3.0, title: str | None = None,
                    requirement: str = "MAJOR_REQUIRED") -> dict:
    return {
        "name": code,
        "shortDescription": title or code,
        "recommendedOpportunity": {
            "term": {"termNumber": term},
            "minUnits": units,
        },
        "requirement": {"requirementType": requirement},
    }


def _generate(elements: list, graph: dict, *, campus: str = "LAMC",
              program_id: str = "synthetic", program_info: dict | None = None,
              max_units: int = 15) -> dict:
    svc = CourseSequenceService(campus)
    info = program_info or {"title": "Synthetic Program", "awardTitle": "Test"}

    async def fake_pm(*_a, **_kw):
        return info, elements

    async def fake_graph(*_a, **_kw):
        return graph

    svc._get_program_requirements = fake_pm  # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_graph  # type: ignore[assignment]

    return asyncio.run(svc.generate_sequence(program_id, campus, max_units_per_term=max_units))


# ── Failure modes ───────────────────────────────────────────────────────────

def test_empty_pathway_elements_returns_clean_empty_sequence():
    """When PM returns no pathwayElements, generator must NOT crash."""
    out = _generate([], deepcopy(EMPTY_GRAPH))
    assert out["sequence"] == []
    assert out["terms_to_complete"] == 0
    assert out["total_units"] == 0
    # Should still return a well-formed payload with diagnostics
    assert "sequence_status" in out
    assert "warnings" in out


def test_graph_unavailable_falls_back_to_pm_order():
    """When eLumen + Neo4j return nothing, the scheduler should still produce
    a sequence following PM term order alone."""
    elements = [
        _course_element("CS 101", term=1, units=3),
        _course_element("CS 113", term=2, units=3),
        _course_element("CS 213", term=3, units=3),
    ]
    out = _generate(elements, deepcopy(EMPTY_GRAPH))
    assert out["sequence"], "expected a sequence even without prereq graph data"
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    # PM term order is the only signal — all three should land at or after their pm_term
    assert placement["CS 101"] >= 1
    assert placement["CS 113"] >= 2
    assert placement["CS 213"] >= 3
    # Each course's prerequisites list should be empty (no graph data)
    for term in out["sequence"]:
        for course in term["courses"]:
            assert course["prerequisites"] == []


def test_circular_prereq_chain_does_not_loop_forever():
    """Cycle in prereq graph (A→B→C→A) must be broken; all three must be scheduled."""
    elements = [
        _course_element("CS 101", term=1, units=3),
        _course_element("CS 102", term=1, units=3),
        _course_element("CS 103", term=1, units=3),
    ]
    cyclic_graph = {
        "prerequisites": {
            "CS 101": [["CS 103"]],
            "CS 102": [["CS 101"]],
            "CS 103": [["CS 102"]],
        },
        "advisories": {},
        "corequisites": {},
        "course_details": {},
    }
    out = _generate(elements, cyclic_graph)
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert {"CS 101", "CS 102", "CS 103"}.issubset(placement.keys()), (
        f"cycle break dropped courses; placed: {placement}"
    )


def test_renumbered_ccn_course_code_is_scheduled():
    """A C-ID / Common Course Numbering code like STAT C1000 must be parsed
    as a real course (not a generic placeholder) and placed in the schedule."""
    elements = [
        _course_element("STAT C1000", term=1, units=4, title="Introduction to Statistics"),
        _course_element("PSYC C1000", term=1, units=3, title="Introduction to Psychology"),
        _course_element("ENGL C1000", term=1, units=3, title="College Composition"),
    ]
    out = _generate(elements, deepcopy(EMPTY_GRAPH))
    codes = {c["code"] for term in out["sequence"] for c in term["courses"]}
    assert "STAT C1000" in codes, f"CCN code not recognized as a course: {codes}"
    assert "PSYC C1000" in codes
    assert "ENGL C1000" in codes


def test_external_prereq_does_not_block_scheduling():
    """A course whose only prereq is OUTSIDE the program should still get
    scheduled, with a warning + prerequisites_met=False on the course."""
    elements = [
        _course_element("CIS 201", term=1, units=3),
    ]
    graph = {
        "prerequisites": {"CIS 201": [["MATH 125"]]},  # MATH 125 not in program
        "advisories": {},
        "corequisites": {},
        "course_details": {},
    }
    out = _generate(elements, graph)
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert "CIS 201" in placement, "course was dropped because of an external prereq"
    course = next(c for t in out["sequence"] for c in t["courses"] if c["code"] == "CIS 201")
    assert course["prerequisites_met"] is False
    assert "MATH 125" in (out.get("external_prerequisites") or {}).get("CIS 201", [])
    assert any("MATH 125" in w for w in (out.get("warnings") or [])), (
        f"expected a warning naming MATH 125; warnings={out.get('warnings')}"
    )


def test_corequisite_pair_placed_in_same_term():
    """Two courses with bidirectional COREQUISITE_OF land in the same term."""
    elements = [
        _course_element("NURSING 090", term=1, units=1, title="Skills Lab"),
        _course_element("NURSING 091", term=1, units=10, title="Patient Care"),
    ]
    graph = {
        "prerequisites": {},
        "advisories": {},
        "corequisites": {
            "NURSING 090": [["NURSING 091"]],
            "NURSING 091": [["NURSING 090"]],
        },
        "course_details": {},
    }
    out = _generate(elements, graph, max_units=15)
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert placement["NURSING 090"] == placement["NURSING 091"], (
        f"corequisites split: {placement}"
    )


def test_unit_range_string_does_not_crash_parser():
    """PM minUnits like '3-4' must parse without exception (Codex covered this
    in unit tests; we verify it through generate_sequence end-to-end)."""
    elements = [
        {
            "name": "MATH 261",
            "shortDescription": "Calculus I",
            "recommendedOpportunity": {"term": {"termNumber": 1}, "minUnits": "3-4"},
            "requirement": {"requirementType": "MAJOR_REQUIRED"},
        },
    ]
    out = _generate(elements, deepcopy(EMPTY_GRAPH))
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert "MATH 261" in placement


def test_blank_and_nonobject_rows_are_skipped_not_crash():
    """Whitespace-only / non-dict pathway rows are reported in skipped_elements
    but never raise."""
    elements = [
        _course_element("CS 101", term=1),
        {"name": "", "shortDescription": "", "recommendedOpportunity": None},
        "not an object at all",
        {"name": "  ", "recommendedOpportunity": {}},
    ]
    out = _generate(elements, deepcopy(EMPTY_GRAPH))
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert "CS 101" in placement
    # Non-empty skipped_elements is fine; what matters is no crash + the real
    # course made it through.
