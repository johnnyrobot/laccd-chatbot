"""The college's own term assignment is the default grouping.

Program Mapper pathways carry `recommendedOpportunity.term.termNumber` — a
term assignment built by counselors who know the catalog and the prerequisite
chains. Re-deriving the grouping from unit caps produced plans that were
longer than the official one and could be prerequisite-invalid.

At the default "full" pace the vendor grouping is used verbatim. Unit-cap
repacking is an override for a chosen pace track, not the other way round.
"""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.course_sequence_service import CourseSequenceService

FIXTURE = Path(__file__).parent / "fixtures" / "sequences" / "synthetic_program.json"


def _load() -> dict:
    return json.loads(FIXTURE.read_text())


def _generate(fixture: dict, **kwargs) -> dict:
    """Run generate_sequence with PM/eLumen/Gemini stubbed from the fixture."""
    svc = CourseSequenceService(fixture["campus_code"])

    program_info = deepcopy(fixture.get("program_info") or {})
    elements = deepcopy(fixture.get("elements") or [])
    graph = deepcopy(fixture.get("graph") or {
        "prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {},
    })

    async def fake_get_program_requirements(program_id: str, campus_code: str):
        return program_info, elements

    async def fake_build_full_dependency_graph(course_codes, campus_code):
        return graph

    svc._get_program_requirements = fake_get_program_requirements   # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_build_full_dependency_graph  # type: ignore[assignment]

    return asyncio.run(svc.generate_sequence(
        fixture["program_id"], fixture["campus_code"], **kwargs,
    ))


def _vendor_terms(fixture: dict) -> dict[int, list[str]]:
    """The college's own grouping, straight from the pathway rows."""
    svc = CourseSequenceService()
    items, _skipped = svc._parse_pathway_elements(fixture["elements"])
    grouped: dict[int, list[str]] = {}
    for item in items:
        grouped.setdefault(item["pm_term"], []).append(item["code"])
    return grouped


@pytest.fixture
def fixture() -> dict:
    return _load()


def test_default_pace_uses_the_vendor_term_count(fixture):
    """The synthetic vendor plan keeps its declared term count by default."""
    result = _generate(fixture)
    expected = len(_vendor_terms(fixture))
    assert len(result["sequence"]) == expected, (
        f"vendor supplies {expected} terms; generator produced "
        f"{len(result['sequence'])}"
    )


@pytest.mark.parametrize("pace", ["full", "momentum", "part_time"])
def test_term_numbers_are_contiguous_from_one(fixture, pace):
    """No skipped Semester 2, on any pace.

    The packer advances its term counter while waiting for a pm_term floor and
    appends nothing, so the label sequence used to read "1, 3, 4, 5" — a hole
    a student has no way to interpret.
    """
    numbers = [term["term_number"] for term in _generate(fixture, pace=pace)["sequence"]]
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"{pace}: term numbers are not contiguous: {numbers}"
    )


def test_default_pace_places_every_course_in_its_vendor_term(fixture):
    """Each term holds exactly the rows the college assigned to it.

    Compared as per-term multisets, not a code->term map: generic rows repeat
    ("CSU Transferable Electives" appears four times across three terms), and
    a map would silently keep only the last one.
    """
    result = _generate(fixture)
    rendered = {
        term["term_number"]: sorted(c["code"] for c in term["courses"])
        for term in result["sequence"]
    }
    expected = {term: sorted(codes) for term, codes in _vendor_terms(fixture).items()}
    assert rendered == expected


def test_prerequisite_is_scheduled_before_the_course_it_unlocks(fixture):
    """A declared prerequisite must remain earlier in the vendor plan."""
    result = _generate(fixture)
    placement = {
        course["code"]: term["term_number"]
        for term in result["sequence"] for course in term["courses"]
    }
    assert placement["MATH 100"] < placement["TEST 201"], (
        f"MATH 100 (term {placement['MATH 100']}) must precede TEST 201 "
        f"(term {placement['TEST 201']}), which requires it"
    )


def test_vendor_grouping_is_reported_as_the_source(fixture):
    """The plan says how it was grouped, so tests and callers can tell."""
    result = _generate(fixture)
    assert result.get("term_source") == "vendor"


@pytest.mark.parametrize("pace", ["momentum", "part_time"])
def test_repacked_plans_keep_prerequisite_order(fixture, pace):
    """The ordering rule survives repacking, not just vendor grouping."""
    result = _generate(fixture, pace=pace)
    assert result["term_source"] == "packed"
    placement = {
        course["code"]: term["term_number"]
        for term in result["sequence"] for course in term["courses"]
    }
    assert placement["MATH 100"] < placement["TEST 201"], (
        f"{pace}: MATH 100 (term {placement['MATH 100']}) must precede "
        f"TEST 201 (term {placement['TEST 201']}), which requires it"
    )


def test_part_time_pace_repacks_below_the_vendor_load(fixture):
    """A chosen pace track overrides the vendor grouping, as designed."""
    result = _generate(fixture, pace="part_time")
    assert result.get("term_source") == "packed"
    assert len(result["sequence"]) > len(_vendor_terms(fixture))
    for term in result["sequence"]:
        assert term["total_units"] <= 9.0 + 0.001, (
            f"part-time term {term['term_number']} has {term['total_units']} units (>9)"
        )
