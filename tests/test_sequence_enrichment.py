"""Sequence enrichment: GE, transferability, and renumber-chain metadata.

These tests cover the Neo4j-backed enrichment that decorates each course in the
generated sequence with ge_areas, transferable, and renumbered_from, plus the
program-wide ge_progress rollup and the AA-T transferability warning.

Neo4j is stubbed via _fetch_course_metadata so the suite stays deterministic
and runs without network or a live database.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.services.course_sequence_service import CourseSequenceService

EMPTY_GRAPH = {"prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {}}


def _course_element(code: str, *, term: int, units: float = 3.0,
                    title: str | None = None, requirement: str = "MAJOR_REQUIRED") -> dict:
    return {
        "name": code,
        "shortDescription": title or code,
        "recommendedOpportunity": {"term": {"termNumber": term}, "minUnits": units},
        "requirement": {"requirementType": requirement},
    }


def _generate(elements: list, graph: dict, metadata: dict, *,
              campus: str = "LAMC", program_id: str = "synthetic",
              award_title: str = "Associate of Science",
              max_units: int = 15) -> dict:
    """Run generate_sequence with PM, eLumen, Gemini, AND Neo4j metadata stubbed."""
    svc = CourseSequenceService(campus)
    info = {"title": "Synthetic Program", "awardTitle": award_title}

    async def fake_pm(*_a, **_kw):
        return info, elements

    async def fake_graph(*_a, **_kw):
        return graph

    async def fake_metadata(course_codes, campus_code):
        # Only return metadata for codes the test actually provided
        return {c: metadata[c] for c in course_codes if c in metadata}

    svc._get_program_requirements = fake_pm  # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_graph  # type: ignore[assignment]
    svc._fetch_course_metadata = fake_metadata  # type: ignore[assignment]

    return asyncio.run(svc.generate_sequence(program_id, campus, max_units_per_term=max_units))


# ── 3c: GE-area enrichment ──────────────────────────────────────────────────

def test_courses_get_ge_areas_and_transferable_flags():
    elements = [
        _course_element("MATH 261", term=1, units=5),
        _course_element("ENGLISH 101", term=1, units=3),
    ]
    metadata = {
        "MATH 261": {
            "code": "MATH 261", "title": "Calculus I",
            "units_min": 5, "units_max": 5,
            "uc_transferable": True, "csu_transferable": True,
            "ge_areas": [
                {"code": "2A", "system": "IGETC", "name": "Math"},
                {"code": "B4", "system": "CSU-GE", "name": "Math"},
            ],
            "renumbered_from": None,
        },
        "ENGLISH 101": {
            "code": "ENGLISH 101", "title": "College Composition",
            "units_min": 3, "units_max": 3,
            "uc_transferable": True, "csu_transferable": True,
            "ge_areas": [{"code": "1A", "system": "IGETC", "name": "English"}],
            "renumbered_from": None,
        },
    }
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata)
    by_code = {c["code"]: c for term in out["sequence"] for c in term["courses"]}
    assert {a["code"] for a in by_code["MATH 261"]["ge_areas"]} == {"2A", "B4"}
    assert by_code["MATH 261"]["transferable"] == ["UC", "CSU"]
    assert "IGETC 2A" in by_code["MATH 261"]["ge_summary"]
    assert by_code["ENGLISH 101"]["transferable"] == ["UC", "CSU"]


def test_term_lists_its_ge_areas_covered():
    elements = [
        _course_element("MATH 261", term=1, units=5),
        _course_element("ENGLISH 101", term=1, units=3),
    ]
    metadata = {
        "MATH 261": {
            "ge_areas": [{"code": "2A", "system": "IGETC", "name": "Math"}],
            "uc_transferable": True, "csu_transferable": True,
            "renumbered_from": None,
        },
        "ENGLISH 101": {
            "ge_areas": [{"code": "1A", "system": "IGETC", "name": "English"}],
            "uc_transferable": True, "csu_transferable": True,
            "renumbered_from": None,
        },
    }
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata)
    term1 = out["sequence"][0]
    assert sorted(term1["ge_areas_covered"]) == ["1A", "2A"]


def test_program_wide_ge_progress_rollup():
    elements = [
        _course_element("MATH 261", term=1, units=5),
        _course_element("ENGLISH 101", term=1, units=3),
        _course_element("HIST 011", term=2, units=3),
    ]
    metadata = {
        "MATH 261": {
            "ge_areas": [
                {"code": "2A", "system": "IGETC", "name": "Math"},
                {"code": "B4", "system": "CSU-GE", "name": "Math"},
            ],
            "uc_transferable": True, "csu_transferable": True, "renumbered_from": None,
        },
        "ENGLISH 101": {
            "ge_areas": [{"code": "1A", "system": "IGETC", "name": "English"}],
            "uc_transferable": True, "csu_transferable": True, "renumbered_from": None,
        },
        "HIST 011": {
            "ge_areas": [
                {"code": "4F", "system": "IGETC", "name": "History"},
                {"code": "D6", "system": "CSU-GE", "name": "History"},
            ],
            "uc_transferable": True, "csu_transferable": True, "renumbered_from": None,
        },
    }
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata)
    progress = out["ge_progress"]
    assert progress == {"2A": 1, "B4": 1, "1A": 1, "4F": 1, "D6": 1}


# ── 3b: RENUMBERED_TO chain ─────────────────────────────────────────────────

def test_renumbered_from_surfaces_in_course_dict():
    elements = [_course_element("MATH 227", term=1, units=4)]
    metadata = {
        "MATH 227": {
            "code": "STAT C1000E",  # current code (terminal of chain)
            "title": "Introduction to Statistics",
            "ge_areas": [{"code": "2A", "system": "IGETC", "name": "Math"}],
            "uc_transferable": True, "csu_transferable": True,
            "renumbered_from": "MATH 227",
        },
    }
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata)
    course = out["sequence"][0]["courses"][0]
    assert course["code"] == "MATH 227"  # display stays as PM canonical
    assert course["renumbered_from"] == "MATH 227"  # provenance preserved
    assert "2A" in [g["code"] for g in course["ge_areas"]]


# ── 3d: AA-T transferability warning ────────────────────────────────────────

def test_aat_program_warns_when_course_lacks_csu_transferability():
    elements = [
        _course_element("PSYC C1000", term=1, units=3),
        _course_element("PSYCH 075", term=2, units=3),  # imagine local-only
    ]
    metadata = {
        "PSYC C1000": {
            "ge_areas": [{"code": "D9", "system": "CSU-GE"}],
            "uc_transferable": True, "csu_transferable": True, "renumbered_from": None,
        },
        "PSYCH 075": {
            "ge_areas": [],
            "uc_transferable": False, "csu_transferable": False, "renumbered_from": None,
        },
    }
    out = _generate(
        elements, deepcopy(EMPTY_GRAPH), metadata,
        award_title="Associate in Arts for Transfer",
    )
    warnings = out["warnings"]
    assert any("PSYCH 075" in w and "CSU" in w for w in warnings), (
        f"expected an AA-T transferability warning naming PSYCH 075; got {warnings}"
    )


def test_non_aat_program_does_not_warn_about_transferability():
    elements = [_course_element("CIS 101", term=1, units=3)]
    metadata = {
        "CIS 101": {
            "ge_areas": [],
            "uc_transferable": False, "csu_transferable": False, "renumbered_from": None,
        },
    }
    out = _generate(
        elements, deepcopy(EMPTY_GRAPH), metadata,
        award_title="Certificate of Achievement",
    )
    assert not any("CSU" in w for w in (out.get("warnings") or [])), (
        f"non-transfer programs must not get AA-T warnings; got {out.get('warnings')}"
    )


# ── Graceful degradation when Neo4j is unconfigured ─────────────────────────

def test_missing_neo4j_metadata_yields_empty_ge_progress_no_crash():
    """When _fetch_course_metadata returns {} (Neo4j unconfigured), the
    sequencer must still produce a valid sequence with empty ge_progress."""
    elements = [_course_element("CS 101", term=1, units=3)]
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata={})
    assert out["sequence"], "expected non-empty sequence even without Neo4j metadata"
    assert out["ge_progress"] == {}
    course = out["sequence"][0]["courses"][0]
    # Enrichment fields should be absent (not None) when no metadata available
    assert "ge_areas" not in course
    assert "transferable" not in course


def test_partial_metadata_only_enriches_known_courses():
    elements = [
        _course_element("CS 101", term=1, units=3),
        _course_element("MATH 261", term=1, units=5),
    ]
    metadata = {
        "MATH 261": {
            "ge_areas": [{"code": "2A", "system": "IGETC"}],
            "uc_transferable": True, "csu_transferable": True, "renumbered_from": None,
        },
        # No entry for CS 101
    }
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata)
    by_code = {c["code"]: c for t in out["sequence"] for c in t["courses"]}
    assert "ge_areas" in by_code["MATH 261"]
    assert "ge_areas" not in by_code["CS 101"]


# ── Slash-prefix course codes (LATTC Welding regression) ────────────────────

def test_slash_prefix_subject_treated_as_real_course():
    """LATTC's WELDG/E 111 must parse as a real course (not a generic placeholder).

    Before the regex extension, slash-prefixed subjects fell into the
    `is_generic=True` branch and ended up in the leftover-elective placement
    loop instead of the prereq-aware schedule.
    """
    elements = [
        _course_element("WELDG/E 111", term=1, units=6, title="Beginning Welding"),
        _course_element("WELDG/E 121", term=2, units=5),
    ]
    out = _generate(elements, deepcopy(EMPTY_GRAPH), metadata={})
    placement = {c["code"]: t["term_number"] for t in out["sequence"] for c in t["courses"]}
    assert "WELDG/E 111" in placement
    assert "WELDG/E 121" in placement
    # Real courses keep their MAJOR_REQUIRED requirement_type, not "Elective"
    course = next(c for t in out["sequence"] for c in t["courses"] if c["code"] == "WELDG/E 111")
    assert course["requirement_type"] != "Elective", (
        f"WELDG/E 111 must be classified as a real course, not an elective placeholder"
    )
