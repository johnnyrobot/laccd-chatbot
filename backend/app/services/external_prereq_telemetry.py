"""Categorize and emit telemetry for external prerequisites surfaced by the
course sequencer.

The original ASSIST v1 hook was a pure log-only stub; this module replaces
that with a structured, categorized emission so future production decisions
about an ASSIST resolver can be data-driven.

A single sample-based measurement (scripts/measure_external_prereqs.py) found
that 0/6 observed external-prereq cases would have benefited from ASSIST
articulation lookup. The categories below are the buckets that measurement
identified, plus a real_articulation_candidate bucket reserved for the cases
ASSIST *could* help with if/when they appear in production logs.

Threshold gate (per second-opinion review): do not revisit an ASSIST sequencer
resolver until N=20+ real_articulation_candidate emissions are observed across
M=5+ distinct programs over a window of at least 14 days.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

# Categories ----------------------------------------------------------------

CAT_RENUMBERING = "renumbering"
"""Prereq is a CCN C-form (or alongside one), or a known district renaming
(e.g. CIS↔CS). Should resolve once Neo4j RENUMBERED_TO chains are seeded."""

CAT_ASSUMED_PREP = "assumed_prep"
"""Prereq is a preparatory math/English course typically completed before
program start (intermediate algebra, sub-transfer English). Surfaced to
students as expected prep, not as a missing program element."""

CAT_PLACEHOLDER_MISMATCH = "placeholder_mismatch"
"""Prereq matches an alternative inside an 'X or Y' placeholder row that the
parser used to treat as a single opaque token."""

CAT_REAL_CANDIDATE = "real_articulation_candidate"
"""None of the above. The sequencer can't resolve it locally; ASSIST might.
Watch this bucket — it's the gate for future ASSIST resolver work."""

# Heuristics ---------------------------------------------------------------

# California Common Course Numbering form: SUBJ Cnnn(letter), with or without
# a space between the subject and the C.
_CCN_RE = re.compile(r"^[A-Z]+\s*C\d{3,}[A-Z]?$")

# Preparatory math (sub-transfer): elementary/pre-/intermediate algebra,
# arithmetic, basic stats prep. Codes traditionally numbered 100–199 at LACCD.
_PREP_MATH_CODES = {
    "MATH 105", "MATH 110", "MATH 112", "MATH 115", "MATH 120", "MATH 121",
    "MATH 122", "MATH 123", "MATH 125", "MATH 128", "MATH 134",
}

# Sub-transfer English (developmental writing/reading). LACCD uses 21/28/72
# series for these. ESL prep: low-numbered ESL.
_PREP_ENGLISH_CODES = {
    "ENGLISH 21", "ENGLISH 28", "ENGLISH 31", "ENGLISH 67", "ENGLISH 72",
    "ENGL 21", "ENGL 28", "ENGL 31", "ENGL 67", "ENGL 72",
}

# Known intra-district renamings that should ultimately live in Neo4j as
# RENUMBERED_TO chains; tracked here only for telemetry classification.
_DISTRICT_RENAMINGS: dict[str, set[str]] = {
    "CS 101": {"CIS 101"},
    "CIS 101": {"CS 101"},
}


def _is_ccn(code: str) -> bool:
    return bool(_CCN_RE.match(code.upper().replace(" ", "")))


def _is_prep_course(code: str) -> bool:
    norm = code.upper().strip()
    return norm in _PREP_MATH_CODES or norm in _PREP_ENGLISH_CODES


def _is_district_renaming(prereq: str, program_codes: Iterable[str]) -> bool:
    aliases = _DISTRICT_RENAMINGS.get(prereq.upper().strip())
    if not aliases:
        return False
    program_norm = {c.upper().strip() for c in program_codes}
    return bool(aliases & program_norm)


def classify(
    prereq: str,
    program_codes: Iterable[str],
    all_alternatives: Iterable[str],
) -> str:
    """Bucket a single external prereq into one of the four categories."""
    p = prereq.upper().strip()
    if _is_ccn(p):
        return CAT_RENUMBERING
    if any(_is_ccn(other.upper().strip()) for other in all_alternatives if other != prereq):
        return CAT_RENUMBERING
    if _is_district_renaming(prereq, program_codes):
        return CAT_RENUMBERING
    if _is_prep_course(prereq):
        return CAT_ASSUMED_PREP
    program_set = {c.upper().strip() for c in program_codes}
    if any(other.upper().strip() in program_set for other in all_alternatives if other != prereq):
        return CAT_PLACEHOLDER_MISMATCH
    return CAT_REAL_CANDIDATE


def emit(
    *,
    program_name: str,
    campus_code: str,
    course_code: str,
    prereq: str,
    alternatives: List[str],
    program_codes: Iterable[str],
    award_type: Optional[str] = None,
    resolved: bool = False,
    resolved_to: Optional[str] = None,
) -> str:
    """Categorize a prereq and emit a single structured log line.

    Returns the assigned category so callers can use it for downstream
    decisions (e.g., which warning text to render).

    ``resolved=True`` indicates the renumbering walker mapped the prereq to a
    program-internal code; threshold-gate analysis should subtract these from
    the raw real_articulation_candidate count.
    """
    category = classify(prereq, program_codes, alternatives)
    payload = {
        "event": "external_prereq.candidate",
        "category": category,
        "campus": campus_code,
        "program": program_name,
        "award": award_type or "",
        "course": course_code,
        "prereq": prereq,
        "alternatives": sorted(set(alternatives)) if alternatives else [],
        "resolved": bool(resolved),
    }
    if resolved_to:
        payload["resolved_to"] = resolved_to
    logger.info("external_prereq.candidate %s", json.dumps(payload, separators=(",", ":")))
    return category
