"""A course must never be presented as its own renumbering.

``find_course_cross_refs`` returns edges in BOTH directions. Forward rows
describe "the course you asked about became X"; reverse rows describe "Y
became the course you asked about", and carry the queried course in
``new_code``. The formatter rendered every row as a target, so asking
"What happened to MATH 265?" answered:

    **MATH 265** has been renumbered:
    - **MATH 262** - Calculus II
    ...
    - **MATH 265** - Calculus with Analytical Geometry I
    - **MATH 265** - Calculus with Analytical Geometry I

listing the course as its own replacement, once per reverse edge.
"""

from __future__ import annotations

from app.services.chat import ChatOrchestrator


# Representative shape returned by neo4j_service.find_course_cross_refs.
REFS = [
    {"new_code": "MATH 262", "new_subject": "MATH", "new_number": "262",
     "title": "Calculus II", "old_code": "MATH 265"},
    {"new_code": "MATH 261", "new_subject": "MATH", "new_number": "261",
     "title": "Calculus I", "old_code": "MATH 265"},
    # Reverse rows carry the ORIGIN's title, so the course that became
    # MATH 265 is described by its own name rather than MATH 265's.
    {"new_code": "MATH 265", "new_subject": "MATH", "new_number": "265",
     "title": "Calculus with Analytical Geometry III", "old_code": "MATH 267"},
    {"new_code": "MATH 265", "new_subject": "MATH", "new_number": "265",
     "title": "Calculus with Analytical Geometry II", "old_code": "MATH 266"},
]


def _render(queried: str = "MATH 265") -> str:
    return "\n".join(ChatOrchestrator._format_renumbering_lines(queried, REFS))


def test_course_is_not_listed_as_its_own_replacement():
    body = _render()
    replacements = [
        line for line in body.splitlines()
        if line.startswith("- ") and "MATH 265" in line
    ]
    assert not replacements, (
        f"MATH 265 presented as its own renumbering target:\n{body}"
    )


def test_forward_renumberings_are_still_reported():
    body = _render()
    assert "MATH 261" in body
    assert "MATH 262" in body


def test_reverse_renumberings_are_reported_in_the_right_direction():
    """The courses that BECAME MATH 265 are useful — just not as targets."""
    body = _render()
    assert "MATH 266" in body and "MATH 267" in body, (
        f"reverse renumberings dropped entirely:\n{body}"
    )


def test_no_duplicate_lines():
    lines = [line for line in _render().splitlines() if line.startswith("- ")]
    assert len(lines) == len(set(lines)), f"duplicate rows: {lines}"


def test_reverse_rows_use_the_origin_course_title():
    """MATH 267 must be described as Geometry III, not as MATH 265's title."""
    body = _render()
    assert "**MATH 267** — Calculus with Analytical Geometry III" in body, body
    assert "**MATH 266** — Calculus with Analytical Geometry II" in body, body
