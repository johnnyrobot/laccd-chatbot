"""Multi-subject schedule payloads include every key required by the formatter.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

from app.services.chat import ChatOrchestrator
from app.services import chat as chat_module
from app.services.live_schedule import (
    MAX_GE_SUBJECTS,
    MAX_PUBLIC_SECTIONS,
    format_sections,
    search_live_schedule,
    search_ztc_sections,
)


REQUIRED_KEYS_FOR_FORMATTER = {"campus", "campus_name", "term_name", "sections", "total_sections"}


def _multimd_listing() -> dict:
    fixture_path = Path(__file__).parent / "fixtures/lamc_2268_multimd_sample.json"
    return json.loads(fixture_path.read_text())


def test_format_sections_includes_related_components_with_component_metadata():
    sections = format_sections(_multimd_listing())

    assert [section["class_number"] for section in sections] == ["13191", "13192"]
    assert sections[0]["component"] == "LEC"
    assert sections[0]["related_to"] == ""
    assert sections[1]["component"] == "LAB"
    assert sections[1]["related_to"] == "13191"


def test_format_sections_filters_courses_without_dropping_related_components():
    listing = _multimd_listing()

    assert [section["class_number"] for section in format_sections(listing, course_number="100")] == ["13191", "13192"]
    assert [section["class_number"] for section in format_sections(listing, keyword="multimedia")] == ["13191", "13192"]


def test_schedule_totals_and_ztc_tags_include_related_components(monkeypatch):
    listing = _multimd_listing()

    async def get_fixture_listing(*_args, **_kwargs):
        return listing

    monkeypatch.setattr("app.services.live_schedule.get_class_listing", get_fixture_listing)

    schedule = asyncio.run(search_live_schedule("LAMC", "2268", subject="MULTIMD", course_number="100"))
    ztc = asyncio.run(search_ztc_sections("LAMC", "2268", subject="MULTIMD"))

    assert schedule["total_sections"] == 2
    assert [section["class_number"] for section in schedule["sections"]] == ["13191", "13192"]
    assert ztc["total_sections"] == 1
    assert [section["class_number"] for section in ztc["sections"]] == ["13192"]


def test_public_schedule_payload_is_bounded_but_total_is_truthful(monkeypatch):
    listing = deepcopy(_multimd_listing())
    course = listing["subjects"][0]["courses"][0]
    template = deepcopy(course["sections"][0])
    template["relsections"] = []
    course["sections"] = []
    for index in range(MAX_PUBLIC_SECTIONS + 25):
        section = deepcopy(template)
        section["classNbr"] = str(20_000 + index)
        course["sections"].append(section)

    async def get_fixture_listing(*_args, **_kwargs):
        return listing

    monkeypatch.setattr("app.services.live_schedule.get_class_listing", get_fixture_listing)

    result = asyncio.run(search_live_schedule("LAMC", "2268", subject="MULTIMD"))

    assert result["total_sections"] == MAX_PUBLIC_SECTIONS + 25
    assert result["returned_sections"] == MAX_PUBLIC_SECTIONS
    assert result["truncated"] is True
    assert len(result["sections"]) == MAX_PUBLIC_SECTIONS


def test_ge_schedule_batches_a_bounded_subject_list(monkeypatch):
    subjects = [f"SUBJ{index}" for index in range(MAX_GE_SUBJECTS + 7)]
    ge_courses = [
        {"course_code": f"{subject} 001", "subject_code": subject}
        for subject in subjects
    ]
    calls: list[tuple[str, ...]] = []

    async def fake_ge_courses(*_args, **_kwargs):
        return ge_courses

    async def fake_listing(_campus, _term, selected_subjects=None):
        calls.append(tuple(selected_subjects or ()))
        return {"campusname": "Los Angeles Mission College", "termname": "Fall 2026", "subjects": []}

    async def unexpected_per_subject(*_args, **_kwargs):
        raise AssertionError("GE schedule must use one batched listing per campus")

    monkeypatch.setattr(chat_module.neo4j_service, "get_ge_courses", fake_ge_courses)
    monkeypatch.setattr(chat_module, "get_class_listing", fake_listing)
    monkeypatch.setattr(chat_module, "search_live_schedule", unexpected_per_subject)

    response = asyncio.run(
        chat_module.ChatOrchestrator()._handle_ge_schedule_query(
            "Which IGETC Area 1A classes are offered?",
            "LAMC",
        )
    )

    assert response.route == "ge_schedule_cross_ref"
    assert calls == [tuple(sorted(subjects)[:MAX_GE_SUBJECTS])]


def test_format_sections_extracts_class_number_and_dates():
    listing = {
        "subjects": [
            {
                "courses": [
                    {
                        "subject": "MATH",
                        "catalogNbr": "261",
                        "descr": "Calculus I",
                        "sections": [
                            {
                                "classNbr": "11747 (LEC)",
                                "dates": "02/09/26 - 06/01/26",
                                "meetings": [
                                    {
                                        "days": "M W",
                                        "times": "9:00 AM - 11:30 AM",
                                        "room": "Mission-CMS 128",
                                        "instr": "Lan Xu",
                                    }
                                ],
                                "classType": [],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    sections = format_sections(listing, course_number="261")

    assert sections[0]["class_number"] == "11747"
    assert sections[0]["section"] == "11747 (LEC)"
    assert sections[0]["dates"] == "02/09/26 - 06/01/26"


def test_format_live_schedule_requires_campus_key():
    """_format_live_schedule reads payload['campus'] — exercise the contract."""
    orch = ChatOrchestrator()
    payload = {
        "campus": "WLAC",
        "campus_name": "West Los Angeles College",
        "term_name": "Spring 2026",
        "sections": [],
        "total_sections": 0,
    }
    # Empty sections short-circuits to a "no matches" message — but the
    # campus_name read at the top of the formatter happens before that
    # branch, so this exercises the bug path even with no sections.
    out = orch._format_live_schedule(payload, "MATH, STAT")
    assert "West Los Angeles College" in out
    assert "Spring 2026" in out


def test_format_live_schedule_includes_class_numbers_and_meeting_dates():
    """Live schedule answers should show enrollment class numbers and dates."""
    orch = ChatOrchestrator()
    payload = {
        "campus": "LAMC",
        "campus_name": "Los Angeles Mission College",
        "term_name": "Spring 2026",
        "sections": [
            {
                "course_code": "MATH 261",
                "course_title": "Calculus I",
                "class_number": "11747",
                "section": "11747 (LEC)",
                "days": "M W",
                "time": "9:00 AM - 11:30 AM",
                "location": "Mission-CMS 128",
                "instructor": "Lan Xu",
                "dates": "02/09/26 - 06/01/26",
                "class_type": [],
            },
            {
                "course_code": "MATH 261",
                "course_title": "Calculus I",
                "class_number": "11748",
                "section": "11748 (LEC)",
                "days": "TBA",
                "time": "TBA",
                "location": "Mission-Online",
                "instructor": "TBA",
                "dates": "02/09/26 - 06/01/26",
                "class_type": ["ONLINE"],
            },
            {
                "course_code": "MATH 261",
                "course_title": "Calculus I",
                "section": "13698 (LEC)",
                "days": "F",
                "time": "1:00 PM - 3:30 PM",
                "location": "Mission-CMS 121",
                "instructor": "Staff",
                "dates": "04/13/26 - 06/01/26",
                "class_type": [],
            },
        ],
        "total_sections": 3,
    }

    out = orch._format_live_schedule(payload, "Mathematics")

    assert "Class #11747" in out
    assert "Class #11748" in out
    assert "Class #13698" in out
    assert "Dates: 02/09/26 - 06/01/26" in out
    assert "Dates: 04/13/26 - 06/01/26" in out


def test_format_live_schedule_raises_when_campus_missing():
    """Sanity check: removing the key restores the pre-fix failure mode.

    This locks in the regression — if a future refactor drops the campus
    key from the multi-subject combined_payload, this test catches it via
    the companion presence test below.
    """
    orch = ChatOrchestrator()
    payload = {
        # No "campus" key
        "campus_name": "West Los Angeles College",
        "term_name": "Spring 2026",
        "sections": [
            {"subject": "MATH", "course_number": "261", "title": "Calculus I",
             "instructor": "Lan Xu", "meeting_pattern": "MWF 9-10",
             "campus_code": "WLAC", "is_ztc": False},
        ],
        "total_sections": 1,
    }
    try:
        orch._format_live_schedule(payload, "MATH, STAT")
    except KeyError as exc:
        assert "campus" in str(exc)
        return
    raise AssertionError("expected KeyError when 'campus' key missing")


def test_combined_payload_construction_in_chat_source():
    """Static check that chat.py:1929-area constructs combined_payload
    with the campus key. Defends against silent refactors that drop it.

    Reads the raw source and asserts both the construction site and the
    consumer use the same key. This is cheap, accurate, and survives
    minor formatting changes.
    """
    import re
    from pathlib import Path

    chat_src = (Path(__file__).resolve().parents[1] / "backend/app/services/chat.py").read_text()
    # The construction site uses kwargs-style dict literal across lines.
    # Look for the multi-subject branch's combined_payload definition and
    # the presence of "campus": within the immediately following dict.
    construction = re.search(
        r"combined_payload\s*=\s*\{([^}]+)\}",
        chat_src,
        re.MULTILINE | re.DOTALL,
    )
    assert construction is not None, "combined_payload literal not found"
    body = construction.group(1)
    assert '"campus":' in body, "combined_payload missing 'campus' key — see chat.py:1929"
    assert '"campus_name":' in body, "combined_payload missing 'campus_name' key"
