"""Synthetic measurement: classify external_prerequisites across recorded
sequence fixtures.

Runs CourseSequenceService.generate_sequence against each fixture in
tests/fixtures/sequences/ (PM/eLumen stubbed from fixture data) and prints
external_prerequisites + a heuristic category label per prereq.

Usage:
    PYTHONPATH="$PWD:$PWD/backend" python scripts/measure_external_prereqs.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.services.course_sequence_service import CourseSequenceService
from app.services.external_prereq_telemetry import classify as _telemetry_classify

FIXTURE_DIR = ROOT / "tests" / "fixtures" / "sequences"


def _generate(fixture: dict, max_units: int = 15) -> dict:
    svc = CourseSequenceService(fixture["campus_code"])
    program_info = deepcopy(fixture.get("program_info") or {})
    elements = deepcopy(fixture.get("elements") or [])
    graph = deepcopy(fixture.get("graph") or {
        "prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {},
    })

    async def fake_get_program_requirements(program_id, campus_code):
        return program_info, elements

    async def fake_build_full_dependency_graph(course_codes, campus_code):
        return graph

    svc._get_program_requirements = fake_get_program_requirements  # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_build_full_dependency_graph  # type: ignore[assignment]

    return asyncio.run(svc.generate_sequence(
        fixture["program_id"], fixture["campus_code"], max_units_per_term=max_units,
    ))


def _classify_prereq(prereq: str, program_codes: set[str], all_prereqs_for_course: list[str]) -> str:
    """Delegate to the production telemetry classifier so this script mirrors
    what prod logs will look like."""
    return _telemetry_classify(prereq, program_codes, all_prereqs_for_course)


def main() -> None:
    fixture_paths = sorted(FIXTURE_DIR.glob("*.json"))
    summary: dict[str, int] = {}

    print(f"Measuring {len(fixture_paths)} fixtures\n")

    for path in fixture_paths:
        fixture = json.loads(path.read_text())
        slug = fixture["slug"]
        award = fixture.get("program_info", {}).get("awardTitle", "?")
        is_transfer = "Transfer" in award

        try:
            result = _generate(fixture)
        except Exception as e:
            print(f"=== {slug} ({award}) ===")
            print(f"  ERROR: {e}\n")
            continue

        ext = result.get("external_prerequisites", {})
        program_codes = {c["code"] for term in result["sequence"] for c in term["courses"]}

        print(f"=== {slug} ({award}) {'[TRANSFER]' if is_transfer else ''} ===")
        print(f"  program_codes ({len(program_codes)}): {sorted(program_codes)[:8]}{'...' if len(program_codes) > 8 else ''}")
        if not ext:
            print("  external_prerequisites: (none)")
            print()
            continue

        prereq_graph = fixture.get("graph", {}).get("prerequisites", {})
        for course, prereqs in sorted(ext.items()):
            print(f"  {course}: {prereqs}")
            # Find the alternative prereq branches for richer classification
            all_branches = prereq_graph.get(course, [])
            all_alternatives = sorted({p for branch in all_branches for p in branch})
            for prereq in prereqs:
                cat = _classify_prereq(prereq, program_codes, all_alternatives)
                summary[cat] = summary.get(cat, 0) + 1
                print(f"    └ {prereq:<20} → {cat} (alts: {all_alternatives})")
        print()

    print("─" * 60)
    print("Summary:")
    total = sum(summary.values())
    for cat, count in summary.items():
        pct = (100 * count / total) if total else 0
        print(f"  {cat}: {count}  ({pct:.0f}%)")
    print(f"  total external prereqs: {total}")


if __name__ == "__main__":
    main()
