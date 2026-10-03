"""Day / time / building / modality filters on the voice schedule tools.

Web chat learned these on 2026-09-25 ("classes in AMP on Saturday"); the voice
tool still took a required subject and nothing else, so a caller asking the
same question got no answer, or a fuzzy-matched subject.

The fixture is the trimmed real LAMC Fall 2026 listing the chat regression
uses. The correct answer to "AMP on Saturday" is INTRDGN 116 lecture #13944
and its lab #13945. No network.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

from app.services import live_schedule
from app.services.live_schedule import ScheduleFilters, filters_from_text
from voice_agent import agent, tools

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "lamc_2268_amp_sample.json").read_text())
SUBJECTS = json.loads((FIXTURES / "lamc_2268_subjects.json").read_text())


@pytest.fixture
def listing_calls(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def fake_listing(campus, term, subjects=None):
        calls.append((campus, term, tuple(subjects or ())))
        if not subjects:
            return LISTING
        wanted = {s.upper() for s in subjects}
        return {**LISTING, "subjects": [s for s in LISTING["subjects"] if s["code"] in wanted]}

    async def fake_subjects(campus, term):
        return SUBJECTS

    monkeypatch.setattr(live_schedule, "get_class_listing", fake_listing)
    monkeypatch.setattr(live_schedule, "get_subjects", fake_subjects)
    monkeypatch.setattr(tools, "get_class_listing", fake_listing)  # building lookup
    return calls


def _search(**kwargs) -> str:
    kwargs.setdefault("campus", "LAMC")
    kwargs.setdefault("term", "Fall 2026")
    return asyncio.run(tools.schedule_search(**kwargs))


# ----- the AMP question, asked by voice -----


def test_building_and_day_without_a_subject_finds_the_saturday_amp_class(listing_calls):
    answer = _search(building="AMP", days="Saturday")

    assert "Found 2 sections for classes meeting in AMP on Saturday" in answer
    assert "INTRDGN 116" in answer
    # The room is what the caller asked about, so it is spoken.
    assert "AMP 302" in answer
    assert "MEDIART" not in answer


def test_a_filtered_search_reads_the_whole_campus_listing_not_a_guessed_subject(listing_calls):
    _search(building="AMP", days="Saturday")

    assert listing_calls and all(subjects == () for _c, _t, subjects in listing_calls)


def test_subject_and_filters_combine(listing_calls):
    answer = _search(subject="Interior Design", days="Saturday", building="AMP")

    assert "Found 2 sections for Interior Design meeting in AMP on Saturday" in answer


def test_zero_matches_under_a_filter_is_an_answer_not_a_miss(listing_calls):
    answer = _search(building="AMP", days="Sunday")

    assert answer.startswith("No sections found for classes meeting in AMP on Sunday")


def test_online_and_time_of_day_filters(listing_calls):
    online = _search(modality="online")
    evening = _search(subject="INTRDGN", time_of_day="evening")

    assert "classes meeting online" in online
    assert "AMP 302" not in online
    assert "in the evening (starting at 5 PM or later)" in evening


def test_unfiltered_subject_search_is_unchanged(listing_calls):
    answer = _search(subject="INTRDGN")

    assert answer.startswith("Found ")
    assert " sections for Interior Design at Los Angeles Mission College for Fall 2026:" in answer
    assert "meeting" not in answer.splitlines()[0]


# ----- what the caller said vs. what the schedule calls it -----


@pytest.mark.parametrize("spoken", ["AMP", "amp", "A M P", "A.M.P.", "the AMP building", "AMP bldg"])
def test_spoken_building_codes_resolve(spoken):
    assert tools._match_building(spoken, {"AMP", "CMS", "PE/W"}) == "AMP"


def test_building_codes_with_punctuation_resolve_when_spoken_without_it():
    assert tools._match_building("PEW", {"PE/W", "CMS"}) == "PE/W"


def test_unknown_building_names_the_codes_instead_of_searching(listing_calls):
    answer = _search(building="the library", days="Saturday")

    assert not answer.startswith("Found")
    assert "library" in answer
    assert "AMP" in answer and "CMS" in answer  # the codes the caller can pick from


@pytest.mark.parametrize(
    "days,expected",
    [
        ("Saturday", {"S"}),
        ("Sat", {"S"}),
        ("Tuesday Thursday", {"T", "Th"}),
        ("Tues/Thurs", {"T", "Th"}),
        ("weekend", {"S", "Su"}),
        ("Mon, Wed", {"M", "W"}),
    ],
)
def test_voice_day_values(days, expected):
    filters, problems = tools._voice_filters(days=days)

    assert problems == []
    assert filters.days == frozenset(expected)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"time_of_day": "evening"}, ScheduleFilters(time_of_day="evening")),
        ({"time_of_day": "night"}, ScheduleFilters(time_of_day="evening")),
        ({"modality": "in person"}, ScheduleFilters(modality="in_person")),
        ({"modality": "in_person"}, ScheduleFilters(modality="in_person")),
        ({"modality": "Online"}, ScheduleFilters(modality="online")),
    ],
)
def test_voice_time_and_modality_values(kwargs, expected):
    filters, problems = tools._voice_filters(**kwargs)

    assert problems == []
    assert filters == expected


def test_unreadable_filters_are_reported_not_silently_dropped(listing_calls):
    answer = _search(subject="INTRDGN", days="someday", time_of_day="noonish", modality="hybrid")

    assert "someday" in answer and "noonish" in answer and "hybrid" in answer
    assert not answer.startswith("Found")
    assert listing_calls == []  # dropping a filter would answer a different question


def test_no_subject_and_no_filter_asks_instead_of_listing_the_whole_campus(listing_calls):
    answer = _search()

    assert not answer.startswith("Found")
    assert listing_calls == []


# ----- all campuses -----


def test_all_campus_search_passes_day_time_and_modality(monkeypatch, listing_calls):
    seen: dict = {}

    async def fake_all(**kwargs):
        seen.update(kwargs)
        return {"total_sections": 0, "term_name": "Fall 2026", "campuses": {}}

    monkeypatch.setattr(tools, "search_all_campuses", fake_all)
    answer = asyncio.run(
        tools.schedule_search_all(subject="INTRDGN", term="Fall 2026", days="Saturday", modality="in person")
    )

    assert seen["filters"] == ScheduleFilters(days=frozenset({"S"}), modality="in_person")
    assert "meeting in person on Saturday" in answer


# ----- the tool the realtime model sees -----


def _params(method) -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(method).parameters)


def test_agent_search_schedule_exposes_the_filters():
    params = _params(agent.CollegeAssistant.search_schedule)

    for name in ("days", "building", "time_of_day", "modality"):
        assert name in params, name
    assert params["subject"].default == ""


def test_agent_all_campus_search_has_no_building_filter():
    params = _params(agent.CollegeAssistant.search_schedule_all_campuses)

    for name in ("days", "time_of_day", "modality"):
        assert name in params, name
    # Building codes are campus-local: "CC" is a hall at City, nothing elsewhere.
    assert "building" not in params


# ----- the shared parser web chat and voice both use -----


def test_shared_text_parser_matches_what_chat_reads():
    assert filters_from_text("classes on saturdays in the evening, in person") == ScheduleFilters(
        days=frozenset({"S"}), time_of_day="evening", modality="in_person",
    )
    # Two times of day, or both formats, is no constraint at all.
    assert filters_from_text("morning or afternoon, online or in person") == ScheduleFilters()
