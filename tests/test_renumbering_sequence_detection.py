"""Course-sequence detection must preserve sequence markers.

Roman numerals distinguish adjacent courses while normalized stems still allow
legitimate old-to-new title matching.
"""

from __future__ import annotations

from scripts.load_assist_data import AssistCourse, detect_renumberings


LAMC = "000937"


def _course(number: str, title: str, begin: str, end: str = "999999") -> AssistCourse:
    return AssistCourse(
        institution_code=LAMC,
        subject_code="MATH",
        number=number,
        title=title,
        units_min=5.0,
        units_max=5.0,
        unit_type="S",
        begin_term=begin,
        end_term=end,
    )


def _calculus_sequence() -> dict[str, AssistCourse]:
    """The real LAMC shape: an old Calc I retired at 202210, the new sequence begins."""
    return {
        "old1": _course("265", "Calculus with Analytical Geometry I", "199810", "202210"),
        "new1": _course("261", "Calculus I", "202210"),
        "new2": _course("262", "Calculus II", "202210"),
        "new3": _course("263", "Calculus III", "202210"),
    }


def _successors(pairs, old_code: str) -> set[str]:
    return {new.course_code for old, new in pairs if old.course_code == old_code}


def test_a_course_does_not_renumber_to_several_courses_at_once():
    pairs = detect_renumberings(_calculus_sequence())
    targets = _successors(pairs, "MATH 265")
    assert len(targets) <= 1, (
        f"MATH 265 cannot renumber to {len(targets)} courses in one term: {sorted(targets)}"
    )


def test_the_genuine_renumbering_is_still_found():
    """Calculus I -> Calculus I is a real rename; only the roman numeral matches."""
    pairs = detect_renumberings(_calculus_sequence())
    assert _successors(pairs, "MATH 265") == {"MATH 261"}


def test_later_sequence_positions_are_never_matched_to_earlier_ones():
    pairs = detect_renumberings(_calculus_sequence())
    targets = _successors(pairs, "MATH 265")
    assert "MATH 262" not in targets, "Calculus II is not a renumbering of Calculus I"
    assert "MATH 263" not in targets, "Calculus III is not a renumbering of Calculus I"


def test_intro_and_advanced_are_not_the_same_course():
    """_TITLE_STRIP_PREFIXES strips "introduction to" AND "advanced", so both
    titles normalised to "comic book design" and ELAC ART 99O was recorded as
    renumbering to the Advanced course as well as the Intro one."""
    def art(number, title, begin, end="999999"):
        c = _course(number, title, begin, end)
        c.subject_code = "ART"
        return c

    courses = {
        "intro_old": art("99O", "Introduction to Comic Book Design", "199810", "202010"),
        "adv_old": art("99P", "Advanced Comic Book Design", "199810", "202010"),
        "intro_new": art("661", "Introduction to Comic Book Design", "202010"),
        "adv_new": art("662", "Advanced Comic Book Design", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "ART 99O") == {"ART 661"}, (
        f"intro must not map to the advanced course; got {sorted(_successors(pairs, 'ART 99O'))}"
    )
    assert _successors(pairs, "ART 99P") == {"ART 662"}


def test_an_intro_prefix_still_matches_a_title_that_dropped_it():
    """The case the prefix stripping exists for: only a MISMATCH of levels is
    rejected, an absent level still matches."""
    courses = {
        "old": _course("227", "Introduction to Statistics", "199810", "202210"),
        "new": _course("C1000", "Statistics", "202210"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 227") == {"MATH C1000"}


def test_zero_padded_variants_of_one_target_count_once():
    """ELAC ENV SCI 17 -> GEOG 14 and -> GEOG 014 is one renumbering, not two."""
    def g(subject, number, title, begin, end="999999"):
        c = _course(number, title, begin, end)
        c.subject_code = subject
        return c

    courses = {
        "old": g("ENV SCI", "17", "Geography of California", "199810", "202010"),
        "new_padded": g("GEOG", "014", "Geography of California", "202010"),
        "new_bare": g("GEOG", "14", "Geography of California", "202010"),
    }
    pairs = detect_renumberings(courses)
    targets = _successors(pairs, "ENV SCI 17")
    assert len(targets) == 1, f"one target expected, got {sorted(targets)}"


def test_courses_without_a_sequence_marker_still_match():
    """The fix must not break the ordinary case it was written for."""
    courses = {
        "old": _course("227", "Introduction to Statistics", "199810", "202210"),
        "new": _course("C1000", "Elementary Statistics", "202210"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 227") == {"MATH C1000"}


def test_a_sequence_encoded_in_the_course_number_is_also_respected():
    """PERSDEV 40A/40B/40C share one title, so the sequence lives in the NUMBER.
    40A may become COUNSEL 40A; it must not become COUNSEL 40B."""
    def pd(subject, number, begin, end="999999"):
        c = _course(number, "Personal Development", begin, end)
        c.subject_code = subject
        return c

    courses = {
        "a_old": pd("PERSDEV", "40A", "199810", "202010"),
        "b_old": pd("PERSDEV", "40B", "199810", "202010"),
        "a_new": pd("COUNSEL", "40A", "202010"),
        "b_new": pd("COUNSEL", "40B", "202010"),
    }
    pairs = detect_renumberings(courses)
    # A mismatched position is always wrong. Same-number-different-subject is
    # genuinely ambiguous (department rename vs. the MATH 485 -> REAL ES 485
    # cross-listing the existing guard rejects), so we prefer a miss to a wrong
    # answer and only assert the unambiguous half here.
    assert "COUNSEL 40B" not in _successors(pairs, "PERSDEV 40A"), (
        f"40A must not renumber to 40B; got {sorted(_successors(pairs, 'PERSDEV 40A'))}"
    )
    assert "COUNSEL 40A" not in _successors(pairs, "PERSDEV 40B")
    assert "PERSDEV 40B" not in _successors(pairs, "PERSDEV 40A")


def test_arabic_and_roman_sequence_markers_are_treated_as_the_same_position():
    """"Spanish 1" and "Spanish I" are the same position, not a renumbering pair
    with "Spanish 2"."""
    def sp(number, title, begin, end="999999"):
        c = _course(number, title, begin, end)
        c.subject_code = "SPANISH"
        return c

    courses = {
        "old": sp("001", "Spanish 1", "199810", "202010"),
        "new1": sp("C1000", "Spanish I", "202010"),
        "new2": sp("C1001", "Spanish II", "202010"),
    }
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "SPANISH 001") == {"SPANISH C1000"}
