"""Room numbers must not be mistaken for course codes.

Schedule answers end each section with its room ("— INST 2016", "— CMS 029"),
and a room is indistinguishable from a course code by shape alone: both are
letters followed by 2-4 digits. When a follow-up question cross-referenced the
previous answer, rooms were picked up as courses and reported back to the
student as "Not offered this term: INST 2014, INST 2016, ...".

Our own answers bold the course code and leave the room bare, so bolding is
the discriminator.
"""

from __future__ import annotations

from app.services.chat import (
    _extract_all_course_codes,
    _extract_course_codes_from_answer,
)


# Verbatim shape of a live_schedule answer, rooms included.
SCHEDULE_ANSWER = """Found **4** **Nursing** sections at Los Angeles Mission College for Fall 2026.

- **NURSING 094** Intermediate Voc Nurs Skil Lab — Class #19580 — M W 10:45 AM - 12:10 PM — Tahereh Khosrowabadi — INST 2016
- **NURSING 095** Intermediate Vocation Nursing — Class #19582 — M W 8:00 AM - 10:35 AM — Candra Lok — INST 2014
- **MATH 240** Trigonometry — Class #14370 — S 8:00 AM - 11:15 AM — Jose Avendano — CMS 029
"""


def test_rooms_are_not_extracted_as_course_codes():
    codes = _extract_course_codes_from_answer(SCHEDULE_ANSWER)
    subjects = {subject for subject, _ in codes}
    assert "INST" not in subjects, f"room INST leaked in as a course: {codes}"
    assert "CMS" not in subjects, f"room CMS leaked in as a course: {codes}"


def test_real_course_codes_are_still_extracted():
    codes = _extract_course_codes_from_answer(SCHEDULE_ANSWER)
    assert ("NURSING", "094") in codes
    assert ("NURSING", "095") in codes
    assert ("MATH", "240") in codes


def test_plain_prose_without_bolding_still_works():
    """Not every answer bolds — unbolded text must still yield its codes."""
    codes = _extract_course_codes_from_answer(
        "You should take MATH 261 and then MATH 262."
    )
    assert ("MATH", "261") in codes
    assert ("MATH", "262") in codes


def test_the_unbolded_extractor_is_unchanged_for_user_messages():
    """Students don't bold, so the plain extractor keeps its old behavior."""
    codes = _extract_all_course_codes("what about MATH 261 and CHEM C211?")
    assert ("MATH", "261") in codes
    assert ("CHEM", "C211") in codes
