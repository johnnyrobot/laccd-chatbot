"""Heuristic soft edges must not contradict the college's own term assignment.

`CourseOrderingAnalyzer` infers ordering from titles and descriptions. Those
signals are guesses; `recommendedOpportunity.term.termNumber` is what the
college published. When they disagree, the college wins.

The case that motivated this: LATTC's Welding AS lists
WELDG/E 113 "Applied Mathematics I" in term 1. Its catalog description
mentions welding, and `_signal_description_skills` reads that as "113 uses a
skill that the welding courses teach", so it emitted eight edges at weight
0.7 -- above HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT, where a soft hint becomes a
hard scheduling gate -- three of them from term-4 courses. The packer then
put a first-term course dead last.

The same signal fires within a single term: WELDG/E 124 "Blueprint Reading I"
was gated behind WELDG/E 121, which the college schedules alongside it.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.course_sequence_service import (
    CourseSequenceService,
    HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sequences"
PACES = ("full", "momentum", "part_time")


def _fixtures():
    return sorted(FIXTURE_DIR.glob("*.json"))


def _generate(fixture: dict, pace: str) -> dict:
    svc = CourseSequenceService(fixture["campus_code"])
    program_info = deepcopy(fixture.get("program_info") or {})
    elements = deepcopy(fixture.get("elements") or [])
    graph = deepcopy(fixture.get("graph") or {
        "prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {},
    })

    async def fake_pm(program_id, campus_code):
        return program_info, elements

    async def fake_graph(course_codes, campus_code):
        return graph

    svc._get_program_requirements = fake_pm       # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_graph  # type: ignore[assignment]
    return asyncio.run(svc.generate_sequence(
        fixture["program_id"], fixture["campus_code"], pace=pace,
    ))


def _college_terms(fixture: dict) -> dict[str, int]:
    svc = CourseSequenceService(fixture["campus_code"])
    items, _skipped = svc._parse_pathway_elements(fixture["elements"])
    return {i["code"]: i["pm_term"] for i in items if not i["is_generic"]}


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
@pytest.mark.parametrize("pace", PACES)
def test_college_ordering_survives_at_every_pace(path, pace):
    """A course the college placed earlier is never scheduled after a later one.

    The validity suite asserts this too, but only at the default pace, where
    #63 hands the whole plan to the vendor grouping and the packer never runs.
    Every ordering defect therefore lives in the paces nothing covered.
    """
    fixture = json.loads(path.read_text())
    college = _college_terms(fixture)
    result = _generate(fixture, pace)
    placement = {
        course["code"]: term["term_number"]
        for term in result["sequence"] for course in term["courses"]
    }
    shared = [(c, t) for c, t in college.items() if c in placement]
    inversions = [
        f"{a} (college term {ta}) in term {placement[a]}, after "
        f"{b} (college term {tb}) in term {placement[b]}"
        for a, ta in shared for b, tb in shared
        if ta < tb and placement[a] > placement[b]
    ]
    assert not inversions, (
        f"{fixture['slug']} at {pace} pace ({result['term_source']}): "
        f"{len(inversions)} inversion(s) of the college's order; first: {inversions[0]}"
    )


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_no_soft_edge_points_backwards_through_the_college_plan(path):
    """No inferred predecessor is a course the college schedules later."""
    fixture = json.loads(path.read_text())
    college = _college_terms(fixture)
    if len(set(college.values())) < 2:
        pytest.skip("college published no usable multi-term assignment")

    svc = CourseSequenceService(fixture["campus_code"])
    items, _skipped = svc._parse_pathway_elements(fixture["elements"])
    codes = [i["code"] for i in items if not i["is_generic"]]
    pm_titles = {i["code"]: i["title"] for i in items
                 if not i["is_generic"] and i.get("title")}
    graph = fixture["graph"]
    soft_order, _coreqs, _levels = svc.analyzer.analyze(
        course_codes=codes,
        advisories=graph["advisories"],
        corequisites=graph["corequisites"],
        course_details=graph["course_details"],
        pm_titles=pm_titles,
    )
    soft_order = svc._reconcile_soft_order_with_college_terms(soft_order, college)

    backwards = [
        f"{dependent} (college term {college[dependent]}) <- "
        f"{predecessor} (college term {college[predecessor]}) at {weight}"
        for dependent, preds in soft_order.items()
        for predecessor, weight in preds.items()
        if dependent in college and predecessor in college
        and college[predecessor] > college[dependent]
    ]
    assert not backwards, (
        f"{fixture['slug']}: {len(backwards)} soft edge(s) contradict the "
        f"college's own order; first: {backwards[0]}"
    )


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_same_term_courses_are_never_hard_gated_against_each_other(path):
    """Two courses the college puts in one term must not be forced apart.

    Below the high-confidence threshold an edge only breaks ties, which is
    harmless within a term. At or above it, the packer refuses to schedule the
    dependent until the predecessor has been taken -- so an inferred edge
    between two concurrent courses adds a term the college did not ask for.
    """
    fixture = json.loads(path.read_text())
    college = _college_terms(fixture)
    if len(set(college.values())) < 2:
        pytest.skip("college published no usable multi-term assignment")

    svc = CourseSequenceService(fixture["campus_code"])
    items, _skipped = svc._parse_pathway_elements(fixture["elements"])
    codes = [i["code"] for i in items if not i["is_generic"]]
    pm_titles = {i["code"]: i["title"] for i in items
                 if not i["is_generic"] and i.get("title")}
    graph = fixture["graph"]
    soft_order, _coreqs, _levels = svc.analyzer.analyze(
        course_codes=codes,
        advisories=graph["advisories"],
        corequisites=graph["corequisites"],
        course_details=graph["course_details"],
        pm_titles=pm_titles,
    )
    soft_order = svc._reconcile_soft_order_with_college_terms(soft_order, college)

    gated = [
        f"{dependent} <- {predecessor}, both in college term {college[dependent]}, at {weight}"
        for dependent, preds in soft_order.items()
        for predecessor, weight in preds.items()
        if dependent in college and predecessor in college
        and college[predecessor] == college[dependent]
        and weight >= HIGH_CONFIDENCE_SOFT_ORDER_WEIGHT
    ]
    assert not gated, (
        f"{fixture['slug']}: {len(gated)} same-term pair(s) hard-gated; first: {gated[0]}"
    )


def test_reconciliation_keeps_heuristics_when_the_college_published_no_terms():
    """With every row in term 1, the term data carries no ordering to defer to.

    Dropping the heuristics there would leave nothing at all, so the edges
    stand -- these are exactly the programs that need them most.
    """
    svc = CourseSequenceService("LAMC")
    soft_order = {"ART 200": {"ART 100": 0.85}}
    flat = {"ART 100": 1, "ART 200": 1}
    assert svc._reconcile_soft_order_with_college_terms(soft_order, flat) == soft_order


def test_reconciliation_keeps_edges_that_agree_with_the_college():
    svc = CourseSequenceService("LAMC")
    soft_order = {"ART 200": {"ART 100": 0.85}}
    terms = {"ART 100": 1, "ART 200": 2, "ART 300": 3}
    assert svc._reconcile_soft_order_with_college_terms(soft_order, terms) == soft_order
