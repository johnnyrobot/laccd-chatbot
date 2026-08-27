"""Renumbering detection requires a retiring code and a newly appearing code.

Lifecycle continuity and one-to-one matching prevent transition-term
cross-products from becoming false successor edges.
"""

from __future__ import annotations

from scripts.load_assist_data import AssistCourse, detect_renumberings


ELAC = "004296"
LAHC = "004395"
LAPC = "004398"
LATTC = "004400"
WLAC = "004964"


def _course(
    institution: str,
    subject: str,
    number: str,
    title: str,
    begin: str,
    end: str = "999999",
    units: float = 3.0,
) -> AssistCourse:
    return AssistCourse(
        institution_code=institution,
        subject_code=subject,
        number=number,
        title=title,
        units_min=units,
        units_max=units,
        unit_type="S",
        begin_term=begin,
        end_term=end,
    )


def _successors(pairs, old_code: str) -> set[str]:
    return {new.course_code for old, new in pairs if old.course_code == old_code}


def _keyed(courses: list[AssistCourse]) -> dict[str, AssistCourse]:
    return {
        f"{c.institution_code}|{c.subject_code}|{c.number}|{c.begin_term}": c
        for c in courses
    }


def test_a_bulk_subject_rename_does_not_cross_product():
    """WLAC OFF ADM -> CAOT, 200807. Three 1-unit records share the title
    "Keyboarding/Document Processing II" on each side of the term, so detection
    emitted 2A -> B2, 2A -> C2, B2 -> 2A, B2 -> C2, C2 -> 2A, C2 -> B2 -- every
    combination except the one that is right, which the cross-listing guard rejects
    because the number is unchanged."""
    courses = _keyed([
        _course(WLAC, "OFF ADM", n, "Keyboarding/Document Processing II", "200004", "200807", 1.0)
        for n in ("2A", "B2", "C2")
    ] + [
        _course(WLAC, "CAOT", n, "Keyboarding/Document Processing II", "200807", units=1.0)
        for n in ("2A", "B2", "C2")
    ])
    pairs = detect_renumberings(courses)
    for old in ("OFF ADM 2A", "OFF ADM B2", "OFF ADM C2"):
        assert _successors(pairs, old) == set(), (
            "nothing distinguishes the three records, so no mapping is inferable; "
            f"{old} got {sorted(_successors(pairs, old))}"
        )


def test_two_courses_that_normalize_to_one_title_keep_only_their_exact_match():
    """LATTC NUR SCI 101/102 -> REGNRSG 121/122, 201210. `_normalize_title` strips
    both "introduction to " and "fundamentals of ", so all four collapse to "nursing"
    in the fuzzy pass and each old course picked up both new ones. The exact-title
    pass already matched them correctly and runs first, so the right answer survives
    and only the fuzzy cross-product is dropped."""
    courses = _keyed([
        _course(LATTC, "NUR SCI", "101", "Introduction to Nursing", "200910", "201210"),
        _course(LATTC, "NUR SCI", "102", "Fundamentals of Nursing", "200910", "201210"),
        _course(LATTC, "REGNRSG", "121", "Introduction to Nursing", "201210", "201310"),
        _course(LATTC, "REGNRSG", "122", "Fundamentals of Nursing", "201210", "201310"),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "NUR SCI 101") == {"REGNRSG 121"}, (
        "Introduction to Nursing and Fundamentals of Nursing are different courses; "
        f"got {sorted(_successors(pairs, 'NUR SCI 101'))}"
    )
    assert _successors(pairs, "NUR SCI 102") == {"REGNRSG 122"}


def test_identically_titled_courses_retiring_together_map_to_nothing():
    """LAHC PE 230/690 -> KIN 250/350, 201310. Four records, one title
    ("Weight Training Skills"), one unit each: there is no evidence at all for
    230 -> 250 over 230 -> 350, so detection must return neither."""
    courses = _keyed([
        _course(LAHC, "PE", "230", "Weight Training Skills", "199510", "201310", 1.0),
        _course(LAHC, "PE", "690", "Weight Training Skills", "199510", "201310", 1.0),
        _course(LAHC, "KIN", "250", "Weight Training Skills", "201310", "201404", 1.0),
        _course(LAHC, "KIN", "350", "Weight Training Skills", "201310", units=1.0),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "PE 230") == set(), f"got {sorted(_successors(pairs, 'PE 230'))}"
    assert _successors(pairs, "PE 690") == set(), f"got {sorted(_successors(pairs, 'PE 690'))}"


def test_a_course_that_kept_running_was_not_renumbered():
    """ELAC PHYS ED 663 -> PHYS ED 627, 199610. 663 gave up the title "Activities"
    and carried on as "Baseball Skills Activity" until 201310, when it really did
    become KIN 386. A course that has a record beginning at the transition term did
    not retire in it."""
    courses = _keyed([
        _course(ELAC, "PHYS ED", "663", "Activities", "199510", "199610", 1.0),
        _course(ELAC, "PHYS ED", "663", "Baseball Skills Activity", "199610", "201310", 1.0),
        _course(ELAC, "PHYS ED", "627", "Backpacking", "199510", "199610", 1.0),
        _course(ELAC, "PHYS ED", "627", "Activities", "199610", "200104", 1.0),
        _course(ELAC, "KIN", "386", "Baseball Skills Activity", "201310", "201507", 1.0),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "PHYS ED 663") == {"KIN 386"}, (
        "a title handed to a course that already existed is not a renumbering; got "
        f"{sorted(_successors(pairs, 'PHYS ED 663'))}"
    )


def test_a_course_that_already_existed_is_not_a_successor():
    """LAPC MUSIC 99V -> THEATER 279, 200410. THEATER 279 has run since 200110; ASSIST
    just issued it a fresh record at 200410, the term MUSIC 99V retired. The real
    successor, MUSIC 776, appears for the first time in that term."""
    courses = _keyed([
        _course(LAPC, "MUSIC", "99V", "Musical Theater", "200404", "200410", 2.0),
        _course(LAPC, "MUSIC", "776", "Musical Theater", "200410", "201410", 2.0),
        _course(LAPC, "THEATER", "279", "Musical Theater", "200110", "200410", 2.0),
        _course(LAPC, "THEATER", "279", "Musical Theater", "200410", units=2.0),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MUSIC 99V") == {"MUSIC 776"}, (
        f"got {sorted(_successors(pairs, 'MUSIC 99V'))}"
    )


def test_the_ccn_split_survives_a_support_variant_retiring_beside_it():
    """The edge this whole filter exists to protect, on all nine campuses.

    MATH 227 -> STAT C1000 *and* STAT C1000E is a genuine 1->2 CCN mapping. MATH 227S
    retires in the same term, so the transition holds two old course codes and two
    new ones and any rule that counts codes rejects it. Counted as *families* --
    227/227S share the stem 227, C1000/C1000E share C1000 -- it is 1x1 and untouched.
    """
    courses = _keyed([
        _course(ELAC, "MATH", "227", "Statistics", "199510", "202510", 4.0),
        _course(ELAC, "MATH", "227S", "Statistics with Support", "201810", "202510", 4.0),
        _course(ELAC, "STAT", "C1000", "Introduction to Statistics", "202510", units=4.0),
        _course(ELAC, "STAT", "C1000E", "Introduction to Statistics", "202510", units=4.0),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "MATH 227") == {"STAT C1000", "STAT C1000E"}, (
        "the embedded-support variant is part of the split, not a cross-product; got "
        f"{sorted(_successors(pairs, 'MATH 227'))}"
    )


def test_an_exact_title_match_is_never_treated_as_a_cross_product():
    """LAPC CO SCI 546 -> CS 230, 202004. Two courses renumber in parallel here
    ("Beginning" and "Advanced Computer Architecture and Organization"), but the
    exact-title pass sees the Advanced pair alone -- one course ending, one
    beginning -- and must keep it whatever the fuzzy pass later decides about the
    group both halves fall into."""
    courses = _keyed([
        _course(LAPC, "CO SCI", "516", "Beginning Computer Architecture and Organization",
                "201804", "202004"),
        _course(LAPC, "CO SCI", "546", "Advanced Computer Architecture and Organization",
                "201804", "202004"),
        _course(LAPC, "CS", "130", "Introduction to Computer Architecture and Organization",
                "202004"),
        _course(LAPC, "CS", "230", "Advanced Computer Architecture and Organization", "202004"),
    ])
    pairs = detect_renumberings(courses)
    assert _successors(pairs, "CO SCI 546") == {"CS 230"}, (
        f"got {sorted(_successors(pairs, 'CO SCI 546'))}"
    )
