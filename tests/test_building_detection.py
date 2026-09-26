"""Which words in a question name a campus building.

Measured against every building in the Fall 2026 district schedule (464 codes,
9 colleges), the first version missed 57 when typed in capitals. Two rules
caused almost all of the misses:

1. Codes shorter than 3 letters were ignored. That dropped LACC's main halls
   (FH, DH, JH, CC) and Valley's lettered buildings (A, E, H, M).
2. A code that is also a subject was never a building, even when the student
   said "building". "the CHEM building" searched Chemistry classes.

The fixture is synthetic: a City College-shaped listing with exactly those
collisions. No network.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import chat as chat_module
from app.services.chat import ChatOrchestrator


def _section(class_nbr: str, days: str, room: str) -> dict:
    return {
        "classNbr": f"{class_nbr} (LEC)",
        "dates": "08/31/26 - 12/20/26",
        "meetings": [{"days": days, "times": "9:00 AM - 10:25 AM", "room": room, "instr": "Staff"}],
        "relsections": [],
        "classType": [],
    }


LISTING = {
    "campuscode": "LACC",
    "campusname": "City College",
    "termcode": "2268",
    "termname": "Fall 2026",
    "subjects": [
        {"code": "CHEM", "name": "Chemistry", "courses": [
            {"subject": "CHEM", "catalogNbr": "101", "descr": "General Chemistry I", "sections": [
                _section("10001", "M W", "City-SCI 110"),  # Chemistry, not in the CHEM building
            ]},
        ]},
        {"code": "BIOLOGY", "name": "Biology", "courses": [
            {"subject": "BIOLOGY", "catalogNbr": "3", "descr": "Intro To Biology", "sections": [
                _section("20001", "M", "City-CHEM 201"),  # in the CHEM building, not Chemistry
                _section("20002", "T", "City-FH 105"),
                _section("20003", "M", "City-CC 12"),
                _section("20004", "M", "City-LA 3"),
                _section("20005", "M", "City-M 106"),
            ]},
        ]},
    ],
}
SUBJECTS = [{"code": "CHEM", "name": "Chemistry"}, {"code": "BIOLOGY", "name": "Biology"}]


@pytest.fixture
def orch(monkeypatch):
    async def fake_listing(campus, term, subjects=None):
        if not subjects:
            return LISTING
        return {**LISTING, "subjects": [s for s in LISTING["subjects"] if s["code"] in subjects]}

    async def fake_subjects(campus, term):
        return SUBJECTS

    from app.services import live_schedule

    for module in (live_schedule, chat_module):
        monkeypatch.setattr(module, "get_class_listing", fake_listing)
        monkeypatch.setattr(module, "get_subjects", fake_subjects)
    monkeypatch.setattr(chat_module.neo4j_service, "is_configured", lambda: False)
    return ChatOrchestrator()


def detect(orch, text: str) -> str | None:
    return asyncio.run(orch._detect_building([text], "LACC", "2268"))


# ----- fix 1: short codes after "in" / "at" -----


@pytest.mark.parametrize(
    "text,building",
    [
        ("classes in FH on Monday", "FH"),
        ("any classes at CC this fall?", "CC"),
        ("what meets in the FH on Tuesday", "FH"),
        ("classes in M for fall 2026", "M"),
    ],
)
def test_short_building_codes_after_in_or_at_are_buildings(orch, text, building):
    assert detect(orch, text) == building


@pytest.mark.parametrize(
    "text",
    [
        # Lower case "in fh" is too close to ordinary English to trust.
        "classes in fh on Monday",
        # A bare short code with no "in"/"at" in front is not enough.
        "FH classes on Monday",
        # LA is a building at City and East, and also "Los Angeles".
        "are there classes in LA this fall?",
        # Times, not places.
        "classes at 9 AM on Monday",
    ],
)
def test_short_codes_without_that_context_are_not_buildings(orch, text):
    assert detect(orch, text) is None


# ----- fix 2: "building" beats the subject reading -----


def test_the_word_building_makes_a_subject_code_a_building(orch):
    assert detect(orch, "classes in the CHEM building on Monday") == "CHEM"
    assert detect(orch, "classes in the chem building") == "CHEM"


@pytest.mark.parametrize("text", ["CHEM classes on Monday", "classes in CHEM this fall"])
def test_without_building_a_subject_code_stays_a_subject(orch, text):
    assert detect(orch, text) is None


def test_chem_building_search_lists_what_meets_there_not_chemistry_classes(orch):
    resp = asyncio.run(
        orch._handle_schedule_query("classes in the CHEM building for fall 2026", "LACC")
    )
    assert [s["class_number"] for s in resp.tool_payload["sections"]] == ["20001"]


def test_chem_classes_still_search_chemistry(orch):
    resp = asyncio.run(orch._handle_schedule_query("CHEM classes for fall 2026", "LACC"))
    assert [s["class_number"] for s in resp.tool_payload["sections"]] == ["10001"]


# ----- the original rules still hold -----


def test_long_caps_codes_still_work_without_in_or_at(orch):
    assert detect(orch, "SCI classes on Monday") == "SCI"


def test_campus_codes_are_still_never_buildings(orch):
    assert detect(orch, "classes at LACC in fall 2026") is None


def test_saying_building_unlocks_la(orch):
    # Bare "in LA" means Los Angeles; "the LA building" can only mean the place.
    assert detect(orch, "classes in the LA building") == "LA"
