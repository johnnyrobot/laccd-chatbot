"""Reject false renumberings caused by concurrent variants, level mismatches,
and bidirectional cross-listing shapes.
"""

from __future__ import annotations

from scripts.load_assist_data import AssistCourse, detect_renumberings


ELAC = "000934"


def _course(
    subject: str, number: str, title: str, begin: str, end: str = "999999"
) -> AssistCourse:
    return AssistCourse(
        institution_code=ELAC,
        subject_code=subject,
        number=number,
        title=title,
        units_min=3.0,
        units_max=3.0,
        unit_type="S",
        begin_term=begin,
        end_term=end,
    )


def _successors(pairs, old_code: str) -> set[str]:
    return {new.course_code for old, new in pairs if old.course_code == old_code}


def test_an_honors_variant_is_not_a_renumbering():
    """ELAC's largest class: 13 of the 34 bidirectional pairs are X <-> XH."""
    courses = {
        "plain": _course("ENGLISH", "101", "College Reading and Composition I", "199810", "202010"),
        "honors": _course("ENGLISH", "101H", "College Reading and Composition I", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "ENGLISH 101") == set(), (
        "an Honors section is a concurrent offering, not a successor; got "
        f"{sorted(_successors(pairs, 'ENGLISH 101'))}"
    )


def test_a_support_variant_is_not_a_renumbering():
    """LAVC MATH 227 -> MATH 227S. The S is an AB 705 corequisite support section;
    presenting it as a renumbering pushes a student into a course they may not need."""
    courses = {
        "plain": _course("MATH", "227", "Statistics", "199810", "202010"),
        "support": _course("MATH", "227S", "Statistics with Support", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 227") == set(), (
        f"got {sorted(_successors(pairs, 'MATH 227'))}"
    )


def test_a_bare_title_does_not_renumber_to_its_advanced_course():
    """The asymmetric level case, live in prod on LACC and LAMC."""
    courses = {
        "plain": _course("CS", "113", "Programming in JAVA", "199810", "202010"),
        "advanced": _course("CS", "213", "Advanced Programming in JAVA", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "CS 113") == set(), (
        "Advanced X is a higher course, not a renumbering of X; got "
        f"{sorted(_successors(pairs, 'CS 113'))}"
    )


def test_a_renumbering_is_never_bidirectional():
    """WLAC HISTORY 44 <-> CHICANO 8: one class cross-listed under two subjects.
    Direction is unknowable, so neither edge survives.

    These are the real ASSIST records. Both codes have a record ending at 201510 and
    another beginning there, which is what produced an edge in each direction — and
    also what tells you neither retired. The fixture this test shipped with in #24
    compressed that into two records by giving CHICANO 8 an end_term *earlier* than
    its begin_term; the lifecycle rule added in #25 reads those terms, so the shape
    now has to be spelled out honestly.
    """
    title = "The Mexican American in the History of the United States II"
    courses = {
        "chic_old": _course("CHICANO", "8", title, "201110", "201510"),
        "chic_new": _course("CHICANO", "8", title, "201510", "201710"),
        "hist_old": _course("HISTORY", "44", title, "201410", "201510"),
        "hist_new": _course("HISTORY", "44", title, "201510", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "HISTORY 44") == set()
    assert _successors(pairs, "CHICANO 8") == set(), (
        "when both directions are detected neither can be trusted; got "
        f"{sorted(_successors(pairs, 'CHICANO 8'))}"
    )


def test_the_genuine_ccn_renumbering_still_survives():
    """The one the inference exists for, and the one prod gets right on all nine
    campuses: MATH 227 Statistics -> STAT C1000."""
    courses = {
        "old": _course("MATH", "227", "Introduction to Statistics", "199810", "202210"),
        "new": _course("STAT", "C1000", "Statistics", "202210"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 227") == {"STAT C1000"}


def test_a_genuine_same_subject_renumbering_still_survives():
    """LAMC MATH 265 -> MATH 261 must not be collateral damage of the level rule:
    neither title declares a level, so the ranks match."""
    courses = {
        "old": _course("MATH", "265", "Calculus with Analytical Geometry I", "199810", "202210"),
        "new": _course("MATH", "261", "Calculus I", "202210"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 265") == {"MATH 261"}
