"""Day / time / building / modality filters on live schedule sections.

Regression cover for the 2026-09-25 "classes in AMP on Saturday" failure: the
schedule search had no way to filter by day or room, so "Saturday" was thrown
away and "AMP" (a building at Mission) was fuzzy-matched to a subject.

The fixture is a trimmed copy of the real LAMC Fall 2026 listing. Pure logic —
no network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.live_schedule import (
    ScheduleFilters,
    building_codes,
    format_sections,
    fuzzy_match_subject,
    room_building,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _listing() -> dict:
    return json.loads((FIXTURES / "lamc_2268_amp_sample.json").read_text())


def _subjects() -> list[dict]:
    return json.loads((FIXTURES / "lamc_2268_subjects.json").read_text())


def _numbers(rows: list[dict]) -> list[str]:
    return [row["class_number"] for row in rows]


# ----- room → building -----


@pytest.mark.parametrize(
    "room,building",
    [
        ("Mission-AMP 302", "AMP"),
        ("Mission-CMS 105", "CMS"),
        ("Mission-Online", "ONLINE"),
        ("Mission-Online Live", "ONLINE"),
        # Trade-Tech's campus prefix itself contains a dash.
        ("Trade-Tech B3-207", "B3"),
        ("Trade-Tech-OFF CAMP", "OFF"),
        ("Trade-Tech -TALLNC-DOM", "TALLNC"),
        ("Trade-Online Live", "ONLINE"),
        # Harbor and Southwest pad or upper-case the prefix.
        ("Harbor - Student Union", "STUDENT"),
        ("Harbor-PE/W 119", "PE/W"),
        ("SOUTHWEST-LOCKE", "LOCKE"),
        ("Valley-M 106", "M"),
        ("", ""),
        ("TBA", ""),
    ],
)
def test_room_building_handles_every_campus_room_format(room, building):
    assert room_building(room) == building


def test_building_codes_come_from_the_listing_itself():
    codes = building_codes(_listing())
    assert {"AMP", "CMS"} <= codes
    # Online is a modality, not a place a student can walk into.
    assert "ONLINE" not in codes


# ----- day filter -----


def test_saturday_filter_keeps_only_sections_meeting_saturday():
    rows = format_sections(_listing(), filters=ScheduleFilters(days=frozenset({"S"})))
    assert sorted(_numbers(rows)) == sorted(
        ["16605", "16606", "16607", "13944", "13945", "16585", "16586", "14297", "14717"]
    )


def test_day_filter_checks_every_meeting_not_just_the_first():
    # ANATOMY 001 #16605 meets Wednesday online and Saturday in CMS 105. The
    # formatter used to read meetings[0] only, which hid the Saturday meeting.
    rows = format_sections(_listing(), filters=ScheduleFilters(days=frozenset({"S"})))
    anatomy = next(row for row in rows if row["class_number"] == "16605")
    assert anatomy["days"] == "S"
    assert anatomy["location"] == "Mission-CMS 105"


# ----- building filter -----


def test_building_filter_keeps_only_sections_meeting_in_that_building():
    rows = format_sections(_listing(), filters=ScheduleFilters(building="AMP"))
    assert rows, "the fixture has AMP sections"
    assert all(row["location"].startswith("Mission-AMP") for row in rows)
    assert "24717" not in _numbers(rows)  # MEDIART 117 is online, not in AMP


def test_saturday_in_amp_is_exactly_the_interior_design_lecture_and_lab():
    """The question from the 2026-09-25 transcript, answered from real data."""
    rows = format_sections(
        _listing(), filters=ScheduleFilters(days=frozenset({"S"}), building="AMP"),
    )
    assert _numbers(rows) == ["13944", "13945"]
    assert {row["course_code"] for row in rows} == {"INTRDGN 116"}


def test_day_and_building_must_hold_in_the_same_meeting():
    # #16605 meets Saturday (CMS 105) and online on Wednesday — never Saturday online.
    rows = format_sections(
        _listing(), filters=ScheduleFilters(days=frozenset({"S"}), building="ONLINE"),
    )
    assert "16605" not in _numbers(rows)


# ----- time of day -----


def test_evening_means_starting_at_5pm_or_later():
    rows = format_sections(_listing(), filters=ScheduleFilters(time_of_day="evening"))
    numbers = _numbers(rows)
    assert "11240" in numbers  # 5:15 PM
    assert "11216" not in numbers  # 7:15 AM
    assert "20430" not in numbers  # 1:50 PM


def test_morning_means_starting_before_noon():
    rows = format_sections(_listing(), filters=ScheduleFilters(time_of_day="morning"))
    numbers = _numbers(rows)
    assert "11216" in numbers
    assert "11240" not in numbers


# ----- modality -----


def test_in_person_excludes_fully_online_sections():
    rows = format_sections(_listing(), filters=ScheduleFilters(modality="in_person"))
    numbers = _numbers(rows)
    assert "24717" not in numbers  # MEDIART 117, online only
    assert "13944" in numbers


def test_online_keeps_only_online_meetings():
    rows = format_sections(_listing(), filters=ScheduleFilters(modality="online"))
    numbers = _numbers(rows)
    assert "24717" in numbers
    assert "13944" not in numbers


def test_no_filters_leaves_the_listing_untouched():
    assert format_sections(_listing(), filters=ScheduleFilters()) == format_sections(_listing())


# ----- subject matcher -----


def test_plural_of_an_exact_subject_name_matches_that_subject():
    # "arts" used to resolve to MEDIART ("Media Arts") because the substring
    # bonus outscored the exact singular match with "Art".
    assert fuzzy_match_subject("arts", _subjects()) == "ART"


def test_exact_subject_matches_still_win():
    assert fuzzy_match_subject("Art", _subjects()) == "ART"
    assert fuzzy_match_subject("media arts", _subjects()) == "MEDIART"
    assert fuzzy_match_subject("MEDIART", _subjects()) == "MEDIART"
