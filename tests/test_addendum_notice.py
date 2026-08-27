"""Catalog data goes stale when a campus publishes an addendum.

Our course data comes from the base catalog PDF. Four campuses have published
an addendum against the edition we parse (ELAC, LACC, LAPC, LAVC), so for any
course those documents touch, the units and prerequisites we hold may already
be wrong. LAPC's Addendum B, for one, moves nine KIN ATH courses from 3 units
to 4.

The deliberate choice here is to flag rather than parse — see the module
docstring in `shared/addenda.py`. These tests pin the two properties that make
that choice safe: the caveat appears for courses an addendum names, and it does
not appear for campuses or courses it does not.
"""

from __future__ import annotations

import pytest

from shared import addenda


# LAPC Addendum B: "KIN ATH 503, 504, 506, 508, 511, 512, 513, 514, and 516:
# Units increased from 3 to 4" -- the concrete case this whole feature exists for.
KIN_ATH = [f"KIN ATH {n}" for n in (503, 504, 506, 508, 511, 512, 513, 514, 516)]


@pytest.mark.parametrize("code", KIN_ATH)
def test_a_course_whose_units_changed_is_flagged(code):
    note = addenda.notice_for("LAPC", code)
    assert note is not None, f"{code} had its units changed by Addendum B"
    assert "addendum" in note.lower()
    assert "counselor" in note.lower()


def test_the_notice_names_the_newest_addendum_for_the_campus():
    """LAPC has two; the caveat should cite B (2026-05), not A (2026-02)."""
    note = addenda.notice_for("LAPC", "KIN ATH 503")
    assert "2026-05" in note
    assert "Summer 2026" in note


@pytest.mark.parametrize("campus", ["LAMC", "WLAC", "LAHC", "LASC", "LATTC"])
def test_campuses_with_no_live_addendum_are_never_flagged(campus):
    """LAMC/WLAC/LAHC moved to 2026-2027 catalogs, which their old addenda
    predate; LASC and LATTC published none. Flagging them would be noise."""
    assert not addenda.has_addendum(campus)
    assert addenda.notice_for(campus, "ACCTG 001") is None
    assert addenda.notice_for(campus) is None


def test_a_course_no_addendum_mentions_is_not_flagged():
    """The caveat has to be selective or it is just a banner."""
    assert addenda.notice_for("LAPC", "ZZZZ 999") is None


def test_campus_level_notice_needs_no_course():
    """Program- and plan-shaped answers have no single course in view."""
    note = addenda.notice_for("LAVC")
    assert note is not None and "addendum" in note.lower()
    assert addenda.notice_for("LASC") is None


def test_unknown_and_empty_inputs_are_safe():
    assert addenda.notice_for("", "ACCTG 001") is None
    assert addenda.notice_for("NOPE", "ACCTG 001") is None
    assert addenda.notice_for("LAPC", None) is not None  # campus-level
    assert not addenda.is_affected("LAPC", None)


def test_case_and_whitespace_do_not_defeat_the_match():
    assert addenda.is_affected("lapc", "  kin ath 503 ")


def test_every_declared_addendum_has_a_safe_pdf_filename():
    """External sources stay flat and cannot escape the selected directory."""
    from pathlib import Path

    for campus, entries in addenda.ADDENDA.items():
        for entry in entries:
            filename = Path(entry.filename)
            assert filename.name == entry.filename, f"{campus}: unsafe path {entry.filename}"
            assert filename.suffix.lower() == ".pdf", f"{campus}: expected a PDF filename"


def test_the_index_covers_every_declared_campus():
    """If the index is missing or stale, notice_for silently returns None for
    every course -- the failure mode this project keeps hitting."""
    for campus in addenda.ADDENDA:
        assert addenda.affected_courses(campus), (
            f"{campus} has an addendum but no indexed courses; "
            f"re-run scripts/index_addenda.py"
        )


def test_a_course_named_only_in_a_program_requirement_table_is_not_flagged():
    """LAPC Addendum B lists `MATH 261* Calculus I .......... 5` inside the new
    Computer Science AS-T requirements. Calculus I did not change; it is named
    because a new degree requires it. Flagging it would put a stale-data caveat
    on one of the most-asked-about courses in the district.

    Measured across the four live addenda, these rows are 26% of all mentions
    and 66% of LACC's, so this distinction is the difference between a useful
    caveat and a banner.
    """
    assert not addenda.is_affected("LAPC", "MATH 261")
    assert addenda.notice_for("LAPC", "MATH 261") is None
    # ...while a course the addendum actually discusses still is flagged.
    assert addenda.is_affected("LAPC", "KIN ATH 503")


def test_the_notice_does_not_claim_the_course_changed():
    """The index knows a course is *named*, not that it changed."""
    note = addenda.notice_for("LAPC", "KIN ATH 503")
    assert "appears in" in note
    assert "may not reflect" in note
