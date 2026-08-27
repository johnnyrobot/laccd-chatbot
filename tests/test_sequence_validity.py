"""Schedule-validity properties for CourseSequenceService.generate_sequence().

Tests run offline against a deliberately synthetic fixture. No provider payloads
or personal data are included in the public repository.

Each fixture supplies the inputs to the two seams:
  - _get_program_requirements  → (program_info, elements)
  - _build_full_dependency_graph → {prerequisites, advisories, corequisites, course_details}

Those two are the only stubs: they are the network seams. Nothing in the
ordering path is stubbed, so this suite exercises the real scheduler.

Properties asserted (per the Track-2 plan):
  1. No phantom courses — every code in sequence traces back to a pathwayElements row.
  2. Hard-prereq satisfaction — placement[course] > placement[prereq], or prereq is external.
  3. Corequisite atomicity — every coreq group lands in the same term.
  4. Term load bounds — no term exceeds max_units_per_term.
  5. PM term-floor preserved — term[course] >= pm_term[course].
  6. Total-units sanity — total_units == sum(term.total_units).
  7. Idempotence — two runs return identical sequences.
  8. Gateway-first heuristic — lower-division English/Math, when present, in term ≤ 2.
  9. Momentum target — programs ≥ 30 units fit in ≤ 5 terms at 15 u/term, else warning.
 10. Unknown-code grace — fixtures with malformed rows still produce a sequence.
"""
from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.services.course_sequence_service import CourseSequenceService

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sequences"
GATEWAY_SUBJECT_PATTERNS = (
    re.compile(r"^ENGLISH\b", re.I),
    re.compile(r"^ENG\b", re.I),
    re.compile(r"^MATH\b", re.I),
    re.compile(r"^STAT\b", re.I),
    # CCN forms
    re.compile(r"^ENGL\s*C\d", re.I),
    re.compile(r"^MATH\s*C\d", re.I),
    re.compile(r"^STAT\s*C\d", re.I),
)


# ── Fixture loading ─────────────────────────────────────────────────────────

def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _generate(fixture: dict, max_units: int = 15) -> dict:
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
        fixture["program_id"], fixture["campus_code"], max_units_per_term=max_units,
    ))


@pytest.fixture(params=_fixture_paths(), ids=lambda p: p.stem)
def fixture(request) -> dict:
    return _load(request.param)


@pytest.fixture
def result(fixture) -> dict:
    return _generate(fixture)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _placement(seq: list[dict]) -> dict[str, int]:
    """Map course code → term_number from a generated sequence."""
    return {c["code"]: term["term_number"] for term in seq for c in term["courses"]}


def _pm_codes(elements: list[dict]) -> set[str]:
    """Codes the parser keeps as real courses, plus generic placeholders."""
    svc = CourseSequenceService()  # only uses pure parse helpers
    items, _skipped = svc._parse_pathway_elements(elements)
    return {item["code"] for item in items}


def _pm_term_map(elements: list[dict]) -> dict[str, int]:
    """PM term floor for *real* courses only (generic placeholders can repeat
    with the same code at different terms, so a flat code→term map is lossy)."""
    svc = CourseSequenceService()
    items, _skipped = svc._parse_pathway_elements(elements)
    out: dict[str, int] = {}
    for item in items:
        if item.get("is_generic"):
            continue
        # Multiple PM rows for the same real course are unusual; keep the
        # earliest as the floor (most generous to the scheduler).
        existing = out.get(item["code"])
        out[item["code"]] = min(existing, item["pm_term"]) if existing else item["pm_term"]
    return out


def _vendor_term_loads(elements: list[dict]) -> dict[int, float]:
    """Units per term as the college assigned them, straight from the rows."""
    svc = CourseSequenceService()
    items, _skipped = svc._parse_pathway_elements(elements)
    loads: dict[int, float] = {}
    for item in items:
        loads[item["pm_term"]] = loads.get(item["pm_term"], 0.0) + item["units"]
    return loads


def _squash(code: str) -> str:
    """Space-insensitive course-code key: "PSYC C1000" and "PSYCC 1000" both
    collapse to "PSYCC1000"."""
    return re.sub(r"\s+", "", (code or "").upper())


def _all_courses(seq: list[dict]) -> Iterator[dict]:
    for term in seq:
        yield from term["courses"]


# ── Properties ──────────────────────────────────────────────────────────────

def test_sequence_is_nonempty(result, fixture):
    assert result["sequence"], f"{fixture['slug']}: generator produced an empty sequence"
    assert result["terms_to_complete"] == len(result["sequence"])


def test_no_phantom_courses(result, fixture):
    """Every scheduled course traces back to a pathwayElements row."""
    expected = _pm_codes(fixture["elements"])
    for course in _all_courses(result["sequence"]):
        assert course["code"] in expected, (
            f"{fixture['slug']}: phantom course {course['code']!r} is not in pathwayElements"
        )


def test_hard_prereq_satisfaction(result, fixture):
    """Every hard prereq is in an earlier term, OR is flagged external_prerequisites."""
    placement = _placement(result["sequence"])
    external = result.get("external_prerequisites") or {}
    for course in _all_courses(result["sequence"]):
        code = course["code"]
        for prereq in course.get("prerequisites") or []:
            if prereq in placement:
                assert placement[prereq] < placement[code], (
                    f"{fixture['slug']}: {code} (term {placement[code]}) "
                    f"placed before its prereq {prereq} (term {placement[prereq]})"
                )
            else:
                assert prereq in (external.get(code) or []), (
                    f"{fixture['slug']}: prereq {prereq!r} of {code} neither scheduled "
                    f"nor flagged in external_prerequisites"
                )


def test_in_program_prereqs_are_not_flagged_external(result, fixture):
    """A prereq the program already contains must not be excused as external.

    Codes reach us with the space in different places — Program Mapper writes
    "PSYC C1000" while the prereq graph writes "PSYCC 1000". Comparing them
    literally makes an in-program course look external, which silently excuses
    it from test_hard_prereq_satisfaction and hides real ordering bugs.
    """
    scheduled = {_squash(c["code"]) for c in _all_courses(result["sequence"])}
    for code, prereqs in (result.get("external_prerequisites") or {}).items():
        for prereq in prereqs or []:
            assert _squash(prereq) not in scheduled, (
                f"{fixture['slug']}: prereq {prereq!r} of {code} is flagged external "
                f"but the program schedules it"
            )


def test_corequisite_atomicity(result, fixture):
    """Every corequisite group lands in the same term."""
    coreqs = (fixture.get("graph") or {}).get("corequisites") or {}
    placement = _placement(result["sequence"])
    for code, groups in coreqs.items():
        if code not in placement:
            continue
        for group in groups or []:
            members = [c for c in (group or []) if c in placement]
            if len(members) < 2:
                continue
            terms = {placement[code], *(placement[m] for m in members)}
            assert len(terms) == 1, (
                f"{fixture['slug']}: corequisite group {[code, *members]} split across terms {terms}"
            )


def test_term_load_within_max_units(result, fixture):
    """Term loads are bounded by whichever authority produced the grouping.

    A packed plan is ours, so the 15-unit cap governs. A vendor plan is the
    college's, and its load governs instead — LAMC Psychology's term 2 is 16
    units because that is what the college published. Rather than drop the
    invariant for vendor plans, pin them to the vendor's own numbers so the
    grouping cannot drift, under a ceiling that still catches nonsense.
    """
    if result.get("term_source") == "vendor":
        expected = _vendor_term_loads(fixture["elements"])
        actual = {t["term_number"]: t["total_units"] for t in result["sequence"]}
        assert actual == pytest.approx(expected), (
            f"{fixture['slug']}: vendor plan drifted from the college's own "
            f"term loads: {actual} != {expected}"
        )
        for term_no, units in actual.items():
            assert units <= 18.0 + 0.001, (
                f"{fixture['slug']}: vendor term {term_no} has {units} units, "
                f"beyond any plausible full-time load"
            )
        return

    for term in result["sequence"]:
        assert term["total_units"] <= 15.0 + 0.001, (
            f"{fixture['slug']}: term {term['term_number']} has "
            f"{term['total_units']} units (>15)"
        )


def test_pm_term_floor_preserved(result, fixture):
    """The college's term ordering survives, in whichever form applies.

    A vendor plan is pinned to the exact term. A packed plan renumbers its
    terms contiguously for display, so absolute numbers no longer line up with
    the vendor's — what must survive is the *ordering*: a course the college
    put earlier is never scheduled after one it put later.
    """
    pm_term = _pm_term_map(fixture["elements"])
    placement = {
        c["code"]: t["term_number"]
        for t in result["sequence"] for c in t["courses"]
    }

    if result.get("term_source") == "vendor":
        for code, floor in pm_term.items():
            if code in placement:
                assert placement[code] == floor, (
                    f"{fixture['slug']}: {code} is in term {placement[code]} "
                    f"but the college assigned term {floor}"
                )
        return

    shared = [(code, floor) for code, floor in pm_term.items() if code in placement]
    for code_a, floor_a in shared:
        for code_b, floor_b in shared:
            if floor_a < floor_b:
                assert placement[code_a] <= placement[code_b], (
                    f"{fixture['slug']}: {code_a} (college term {floor_a}) is "
                    f"scheduled in term {placement[code_a]}, after {code_b} "
                    f"(college term {floor_b}) in term {placement[code_b]}"
                )


def test_total_units_consistency(result, fixture):
    """Top-level total_units matches sum of per-term units."""
    summed = sum(t["total_units"] for t in result["sequence"])
    assert abs(result["total_units"] - summed) < 0.01, (
        f"{fixture['slug']}: total_units={result['total_units']} but per-term sum={summed}"
    )


def test_idempotence(fixture):
    """Two consecutive runs produce identical sequences."""
    a = _generate(fixture)
    b = _generate(fixture)
    # Compare a stable projection (term order + course codes per term)
    proj_a = [(t["term_number"], [c["code"] for c in t["courses"]]) for t in a["sequence"]]
    proj_b = [(t["term_number"], [c["code"] for c in t["courses"]]) for t in b["sequence"]]
    assert proj_a == proj_b, f"{fixture['slug']}: non-deterministic output"


def test_gateway_courses_in_first_two_terms(result, fixture):
    """If a gateway English/Math course is in the program, it lands in term 1 or 2.

    Permissive: only enforced when at least one such gateway course is recognized
    AND its PM term floor is also ≤ 2 (so we don't punish programs that
    deliberately schedule them later for placement reasons).
    """
    placement = _placement(result["sequence"])
    pm_term = _pm_term_map(fixture["elements"])
    gateways = [
        code for code in placement
        if any(p.match(code) for p in GATEWAY_SUBJECT_PATTERNS)
    ]
    if not gateways:
        pytest.skip("no gateway English/Math courses in program")
    flexible = [g for g in gateways if pm_term.get(g, 1) <= 2]
    if not flexible:
        pytest.skip("gateway courses have PM term-floor > 2")
    earliest = min(placement[g] for g in flexible)
    assert earliest <= 2, (
        f"{fixture['slug']}: gateway courses {flexible} all placed in term ≥ 3 "
        f"(earliest={earliest})"
    )


def test_momentum_target_for_full_programs(result, fixture):
    """Programs ≥ 30 units finish in ≤ 5 terms at 15-unit pace, else warning surfaced."""
    if result["total_units"] < 30:
        pytest.skip("program is sub-30 units; momentum target N/A")
    if result["terms_to_complete"] <= 5:
        return
    # Allowed to exceed only if a warning explains why
    assert result.get("warnings"), (
        f"{fixture['slug']}: {result['total_units']:.1f}u took "
        f"{result['terms_to_complete']} terms at 15u/term and emitted no warnings"
    )


def test_unknown_code_grace(fixture):
    """Generator never raises on this fixture's data, even when malformed rows present."""
    # Drop into one element with shape that exercises the parser's defensive paths.
    poisoned = deepcopy(fixture)
    poisoned["elements"] = list(poisoned["elements"]) + [
        {"name": "", "shortDescription": "", "recommendedOpportunity": None},
        {"name": "BAD CODE WITH SPACES NOT VALID", "recommendedOpportunity": {"term": None, "minUnits": "3-4"}},
        "not-an-object",
    ]
    out = _generate(poisoned)
    assert out["sequence"], f"{fixture['slug']}: malformed-row injection produced empty sequence"
