"""Voice schedule output must disclose coverage beyond its detail cap."""

from __future__ import annotations

from voice_agent.tools import _fmt_sections_voice


def _components(course_count: int, component_count: int) -> list[dict]:
    """Build ordered components with a recognizable detail on each row."""
    return [
        {
            "course_code": f"MULTIMD {100 + index % course_count}",
            "course_title": f"Detail {index + 1}",
            "days": "M",
            "time": "9:00 AM - 10:00 AM",
            "instructor": "Instructor",
        }
        for index in range(component_count)
    ]


def test_voice_schedule_discloses_all_courses_and_components_beyond_detail_cap():
    """Missing coverage disclosures can make eight spoken details look complete."""
    sections = _components(course_count=10, component_count=26)

    output = _fmt_sections_voice(sections)

    assert "10 courses and 26 components" in output
    for course_number in range(100, 110):
        assert f"MULTIMD {course_number}" in output
    detail_lines = [line for line in output.splitlines() if "Detail " in line]
    assert len(detail_lines) == 8
    assert "Plus 18 additional components." in output


def test_voice_schedule_coverage_shows_first_twelve_courses_and_reports_the_rest():
    """Course lists over the speech-safe cap preserve their input order and remainder."""
    sections = _components(course_count=13, component_count=13)

    output = _fmt_sections_voice(sections)

    expected_shown = ", ".join(f"MULTIMD {number}" for number in range(100, 112))
    assert f"13 courses and 13 components: {expected_shown}" in output
    assert "1 additional course" in output
    assert "MULTIMD 112" not in output
