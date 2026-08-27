""""Which of those courses are offered next semester?" must not overstate itself.

After the bot hands a student a course sequence, the natural follow-up is asking
which of those courses run next term. ``_handle_course_schedule_crossref``
answers it by looking each course up on the live LACCD schedule — but it caps
the lookup at five subjects, and it built its denominator *after* that cap. A
sequence spanning eight subjects therefore reported something like "7 of 10
courses are offered", when ten was never the real total: the courses in the
sixth subject onward were not offered, not listed as missing, and not counted.
They were never looked up at all.

The same silence swallowed any subject whose code failed to resolve — a
``continue`` with no record that it happened.

That is worse than an incomplete answer, because it is indistinguishable from a
complete one. A student reads "not offered this term" and drops the course.

These tests pin the honest accounting:

* every listed course is in the denominator, checked or not;
* "not offered" means **looked up and absent**, never "never looked up";
* anything skipped is stated plainly, with the reason;
* and when everything fits, the answer is exactly as it was — no new noise on
  the common path.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import chat as chat_module
from app.services.chat import ChatOrchestrator


@pytest.fixture
def orchestrator():
    return ChatOrchestrator()


def _sequence_answer(pairs: list[tuple[str, str]]) -> str:
    """An assistant answer shaped like our own sequence output (bolded codes)."""
    lines = ["Here is a suggested course sequence:", ""]
    for subject, number in pairs:
        lines.append(f"- **{subject} {number}**: Some Course Title (3.0 units)")
    return "\n".join(lines)


def _history(pairs: list[tuple[str, str]]) -> list[dict]:
    return [
        {"role": "user", "content": "what order should I take these in?"},
        {"role": "assistant", "content": _sequence_answer(pairs)},
    ]


def _install_schedule(monkeypatch, *, offered: set[str], unresolvable: set[str] = frozenset()):
    """Fake the live schedule: `offered` codes have a section, others do not."""

    async def fake_find_subject_code(subject, campus_code, term):
        if subject in unresolvable:
            return None, None
        return subject, subject

    async def fake_search_live_schedule(campus_code, term=None, subject=None, **kwargs):
        sections = [
            {
                "course_code": code,
                "course_title": f"{code} Title",
                "days": "MW",
                "time": "9:00 AM",
                "instructor": "Staff",
                "location": "TBA",
                "section": "1234",
                "class_number": "1234",
                "dates": "08/31/26 - 12/20/26",
            }
            for code in offered
            if code.split()[0] == subject
        ]
        return {"sections": sections, "campus_name": "Los Angeles Mission College"}

    monkeypatch.setattr(chat_module, "find_subject_code", fake_find_subject_code)
    monkeypatch.setattr(chat_module, "search_live_schedule", fake_search_live_schedule)


def _ask(orchestrator, history):
    return asyncio.run(
        orchestrator._handle_course_schedule_crossref(
            "which of those are offered next semester?", history, "LAMC",
        )
    ).answer


# Eight subjects — a realistic transfer-degree spread, and three past the cap.
EIGHT_SUBJECTS = [
    ("ENGL", "101"), ("MATH", "261"), ("PSYC", "001"), ("BIOL", "003"),
    ("HIST", "011"), ("COMM", "101"), ("ART", "101"), ("CHEM", "101"),
]


def test_every_listed_course_is_in_the_denominator(orchestrator, monkeypatch):
    """"N of M" must count the courses the student was given, not the ones we
    happened to look up."""
    _install_schedule(monkeypatch, offered={"ENGL 101", "MATH 261"})

    answer = _ask(orchestrator, _history(EIGHT_SUBJECTS))

    assert "of **8**" in answer, f"denominator must be 8, got:\n{answer}"


def test_unchecked_courses_are_not_called_not_offered(orchestrator, monkeypatch):
    """The bug that matters: a course we never looked up must not be reported as
    absent from the schedule. A student drops a course over that sentence."""
    _install_schedule(monkeypatch, offered={"ENGL 101", "MATH 261"})

    answer = _ask(orchestrator, _history(EIGHT_SUBJECTS))

    not_offered_line = next(
        (line for line in answer.splitlines() if "Not offered" in line), "",
    )
    for unchecked in ("ART 101", "CHEM 101"):
        assert unchecked not in not_offered_line, (
            f"{unchecked} was never looked up; it cannot be reported as not offered.\n{answer}"
        )


def test_skipped_subjects_are_stated_plainly(orchestrator, monkeypatch):
    _install_schedule(monkeypatch, offered={"ENGL 101", "MATH 261"})

    answer = _ask(orchestrator, _history(EIGHT_SUBJECTS))

    assert "couldn't check" in answer.lower() or "not checked" in answer.lower(), (
        f"the answer must admit the courses it skipped:\n{answer}"
    )
    assert "ART 101" in answer and "CHEM 101" in answer


def test_an_unresolvable_subject_is_reported_as_unchecked(orchestrator, monkeypatch):
    """A subject whose code won't resolve used to `continue` in silence."""
    _install_schedule(
        monkeypatch, offered={"ENGL 101"}, unresolvable={"MATH"},
    )

    answer = _ask(orchestrator, _history([("ENGL", "101"), ("MATH", "261")]))

    assert "of **2**" in answer
    not_offered_line = next(
        (line for line in answer.splitlines() if "Not offered" in line), "",
    )
    assert "MATH 261" not in not_offered_line, f"never looked up:\n{answer}"
    assert "MATH 261" in answer


def test_a_course_genuinely_absent_is_still_reported_as_not_offered(orchestrator, monkeypatch):
    """The honest case must keep working — this is not about softening real answers."""
    _install_schedule(monkeypatch, offered={"ENGL 101"})

    answer = _ask(orchestrator, _history([("ENGL", "101"), ("MATH", "261")]))

    not_offered_line = next(
        (line for line in answer.splitlines() if "Not offered" in line), "",
    )
    assert "MATH 261" in not_offered_line, f"MATH was checked and absent:\n{answer}"


def test_the_common_path_gains_no_new_noise(orchestrator, monkeypatch):
    """Sequences under the cap are the normal case; they must read exactly as before."""
    _install_schedule(monkeypatch, offered={"ENGL 101", "MATH 261"})

    answer = _ask(orchestrator, _history([("ENGL", "101"), ("MATH", "261")]))

    assert "of **2**" in answer
    assert "couldn't check" not in answer.lower()
    assert "not checked" not in answer.lower()


def test_nothing_offered_still_counts_every_listed_course(orchestrator, monkeypatch):
    """The early-return path had its own copy of the same wrong total."""
    _install_schedule(monkeypatch, offered=set())

    answer = _ask(orchestrator, _history(EIGHT_SUBJECTS))

    assert "8" in answer, f"the no-results path must also count all 8:\n{answer}"
    assert "ART 101" in answer and "CHEM 101" in answer
