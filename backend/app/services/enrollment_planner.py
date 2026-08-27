"""Candidate enrollment-plan builder for public chat and voice surfaces.

The service deliberately does not claim access to student records or real-time
seat counts.  It combines the published term schedule with verified course
units, prerequisite, and corequisite records, then proposes a conflict-free
candidate set of offered sections.  Students must still confirm seat
availability and personal eligibility before registering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.live_schedule import get_term_name, search_live_schedule
from app.services.neo4j_service import neo4j_service


_DAY_TOKENS = ("M", "T", "W", "Th", "F", "Sa", "Su")
MIN_TARGET_UNITS = 1.0
MAX_TARGET_UNITS = 21.0
MAX_SEARCH_STATES = 20_000


class PlanSearchBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateSection:
    course_code: str
    title: str
    units: float
    section: dict[str, Any]
    prerequisites: tuple[str, ...]
    corequisites: tuple[str, ...]

    @property
    def cost_label(self) -> str:
        class_type = self.section.get("class_type") or []
        if "OER" in class_type:
            return "ZTC"
        if "LCT" in class_type:
            return "Low Cost"
        return "Standard materials"


def _parse_days(value: str | None) -> set[str]:
    if not value or value in {"TBA", "Online"}:
        return set()
    normalized = value.replace("TH", "Th").replace("TU", "T").replace("SA", "Sa")
    return {token for token in _DAY_TOKENS if re.search(rf"(?<![A-Za-z]){token}(?![A-Za-z])", normalized)}


def _parse_time(value: str | None) -> tuple[int, int] | None:
    if not value or value == "TBA" or " - " not in value:
        return None
    start_raw, end_raw = value.split(" - ", 1)
    try:
        start = datetime.strptime(start_raw.strip(), "%I:%M %p")
        end = datetime.strptime(end_raw.strip(), "%I:%M %p")
    except ValueError:
        return None
    return start.hour * 60 + start.minute, end.hour * 60 + end.minute


def _conflicts(left: CandidateSection, right: CandidateSection) -> bool:
    left_days = _parse_days(left.section.get("days"))
    right_days = _parse_days(right.section.get("days"))
    if not left_days or not right_days or left_days.isdisjoint(right_days):
        return False
    left_time = _parse_time(left.section.get("time"))
    right_time = _parse_time(right.section.get("time"))
    if not left_time or not right_time:
        return False
    return left_time[0] < right_time[1] and right_time[0] < left_time[1]


def _cost_rank(section: dict[str, Any]) -> int:
    class_type = section.get("class_type") or []
    if "OER" in class_type:
        return 0
    if "LCT" in class_type:
        return 1
    return 2


def _choose_plan(
    candidates: list[CandidateSection],
    target_units: float,
    *,
    max_states: int = MAX_SEARCH_STATES,
) -> list[CandidateSection]:
    """Find an exact-unit, conflict-free combination, preferring lower cost."""

    target_half_units = round(target_units * 2)
    best: list[CandidateSection] | None = None
    best_cost = 10**9
    states_visited = 0

    def visit(
        start: int,
        selected: list[CandidateSection],
        used_codes: set[str],
        unit_total: int,
        cost_total: int,
    ) -> None:
        nonlocal best, best_cost, states_visited
        states_visited += 1
        if states_visited > max_states:
            raise PlanSearchBudgetExceeded("candidate-plan search budget exhausted")
        if unit_total == target_half_units:
            if cost_total < best_cost:
                best = list(selected)
                best_cost = cost_total
            return
        if unit_total > target_half_units or len(selected) >= 6 or cost_total >= best_cost:
            return
        for index in range(start, len(candidates)):
            candidate = candidates[index]
            if candidate.course_code in used_codes:
                continue
            candidate_units = round(candidate.units * 2)
            if candidate_units <= 0 or unit_total + candidate_units > target_half_units:
                continue
            if any(_conflicts(candidate, existing) for existing in selected):
                continue
            visit(
                index + 1,
                [*selected, candidate],
                {*used_codes, candidate.course_code},
                unit_total + candidate_units,
                cost_total + _cost_rank(candidate.section),
            )

    visit(0, [], set(), 0, 0)
    return best or []


async def build_candidate_enrollment_plan(
    campus_code: str,
    term: str,
    target_units: float = 12,
) -> dict[str, Any]:
    """Build a candidate plan from sections without listed prerequisites/corequisites."""

    doubled = target_units * 2
    if (
        target_units < MIN_TARGET_UNITS
        or target_units > MAX_TARGET_UNITS
        or abs(doubled - round(doubled)) > 1e-9
    ):
        raise ValueError("target_units must be between 1 and 21 in half-unit increments")

    campus_code = campus_code.upper()
    schedule = await search_live_schedule(campus_code, term=term)
    sections = schedule.get("sections") or []
    course_codes = sorted({section.get("course_code", "") for section in sections if section.get("course_code")})
    metadata = await neo4j_service.get_course_metadata_bulk(course_codes, campus_code)

    candidates: list[CandidateSection] = []
    for section in sections:
        code = section.get("course_code", "")
        info = metadata.get(code) or {}
        units = info.get("units_min")
        prerequisites = tuple(info.get("prerequisites") or ())
        catalog_prereq = (info.get("catalog_prereq_text") or "").strip()
        corequisites = tuple(info.get("corequisites") or ())
        catalog_coreq = (info.get("catalog_coreq_text") or "").strip()
        if not isinstance(units, (int, float)) or units <= 0:
            continue
        if prerequisites or catalog_prereq or corequisites or catalog_coreq:
            continue
        candidates.append(
            CandidateSection(
                course_code=code,
                title=section.get("course_title") or info.get("title") or "",
                units=float(units),
                section=section,
                prerequisites=prerequisites,
                corequisites=corequisites,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            _cost_rank(candidate.section),
            candidate.course_code,
            candidate.section.get("class_number", ""),
        )
    )
    # Forty distinct candidates are enough to find a four-course plan while
    # keeping the bounded backtracking search inexpensive.
    search_exhausted = False
    try:
        plan = _choose_plan(candidates[:40], target_units)
    except PlanSearchBudgetExceeded:
        plan = []
        search_exhausted = True
    return {
        "campus": campus_code,
        "campus_name": schedule.get("campus_name", campus_code),
        "term": term,
        "term_name": schedule.get("term_name", get_term_name(term)),
        "target_units": target_units,
        "total_units": sum(item.units for item in plan),
        "sections": [
            {
                **item.section,
                "course_code": item.course_code,
                "course_title": item.title,
                "units": item.units,
                "cost_label": item.cost_label,
                "prerequisites": list(item.prerequisites),
                "corequisites": list(item.corequisites),
            }
            for item in plan
        ],
        "candidate_count": len(candidates),
        "seat_status_available": False,
        "search_exhausted": search_exhausted,
    }


def format_candidate_enrollment_plan(payload: dict[str, Any]) -> str:
    """Format a candidate plan with explicit scope and safety boundaries."""

    campus_name = payload.get("campus_name") or payload.get("campus")
    term_name = payload.get("term_name") or payload.get("term")
    target_units = payload.get("target_units", 12)
    sections = payload.get("sections") or []
    if payload.get("search_exhausted"):
        return (
            "That request produced too many candidate combinations to evaluate safely. "
            "Choose a subject or program to narrow the plan, or request fewer units."
        )
    if not sections:
        return (
            f"I couldn't build an exact {target_units:g}-unit candidate plan from the "
            f"currently published {term_name} sections at {campus_name}. Tell me a subject "
            "or program you want to prioritize and I can narrow the search."
        )

    lines = [
        f"**Candidate {target_units:g}-unit enrollment plan — {campus_name}, {term_name}**",
        "",
        "I selected offered courses with no listed prerequisites or corequisites and avoided published meeting-time conflicts:",
    ]
    for section in sections:
        badge = section.get("cost_label", "Standard materials")
        class_number = section.get("class_number") or "not listed"
        days = section.get("days") or "TBA"
        time = section.get("time") or "TBA"
        lines.append(
            f"- **{section['course_code']}** — {section.get('course_title', '')} "
            f"({section.get('units', 0):g} units) — Class #{class_number} — "
            f"{days} {time} — **{badge}**"
        )
    lines.extend(
        [
            "",
            f"**Total: {payload.get('total_units', 0):g} units.**",
            "",
            "Tell me a program or subject to prioritize if you want a more relevant candidate set.",
            "",
            "*This is a planning candidate, not a registration or financial-aid determination. "
            "The public schedule does not provide reliable live seat counts here, so confirm that "
            "each section is still open and review placement, program, and counselor requirements before enrolling.*",
        ]
    )
    return "\n".join(lines)
