"""Replay of the 2026-09-25 "classes in AMP on Saturday" conversation.

What prod did (Langfuse traces, LAMC, Fall 2026):

1. "are there classes in AMP on Saturday's for fall 2026?" — the welcome banner
   was sent as history, so the first turn went through the LLM rewriter, which
   wrote "in the AMP program". "program" routed to Program Mapper → RAG → "no
   information".
2. "Can you look up the scehdules for all AMP classes for fall 2026?" — rewritten
   to "Arts, Media, and Performance (AMP)"; "arts" and "media" both fuzzy-matched
   MEDIART, the listing was fetched twice, the same section shown twice.
3/4. "those do not belong to LAMC" / "this is not correct" — rewritten into a
   restatement of the last query, which re-ran it and returned the same answer.

AMP is a building at Mission (rooms "Mission-AMP 302"). The correct answer to
turn 1 is INTRDGN 116 lecture #13944 and its lab #13945.

No network: the district schedule is served from a trimmed real fixture and the
LLM is a fake that records its prompts.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services import chat as chat_module
from app.services import live_schedule
from app.services.chat import ChatOrchestrator

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "lamc_2268_amp_sample.json").read_text())
SUBJECTS = json.loads((FIXTURES / "lamc_2268_subjects.json").read_text())

WELCOME = (
    "Welcome! I can help with:Class schedules and section availabilityDegree and "
    "certificate program infoCounseling and student servicesCampus info, "
    "enrollment, and transfersType a question or click a sample on the left."
)
TURN_1 = "are there classes in AMP on Saturday's for fall 2026?"
TURN_2 = "Can you look up the scehdules for all AMP classes for fall 2026?"
TURN_2_PROD_REWRITE = (
    "Can you look up the schedules for all Arts, Media, and Performance (AMP) "
    "classes for Fall 2026?"
)
PROD_TURN_2_ANSWER = (
    "Found 2 Media Arts, Media Arts sections at Los Angeles Mission College for "
    "Fall 2026.MEDIART 117 Intro Social Media — Class #24717 — Dates: 08/31/26 - "
    "12/20/26 — Online / TBA — Morgan KeithMEDIART 117 Intro Social Media — Class "
    "#24717 — Dates: 08/31/26 - 12/20/26 — Online / TBA — Morgan Keith"
)


class FakeLLM:
    """Stands in for GeminiFileSearchService.generate; records every prompt."""

    def __init__(self, reply: str | None = None):
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str, *args, **kwargs):
        self.prompts.append(prompt)
        return self.reply


def _filtered_listing(subjects: list[str] | None) -> dict:
    if not subjects:
        return LISTING
    wanted = {s.upper() for s in subjects}
    return {**LISTING, "subjects": [s for s in LISTING["subjects"] if s["code"] in wanted]}


@pytest.fixture
def orch(monkeypatch):
    listing_calls: list[tuple] = []

    async def fake_listing(campus, term, subjects=None):
        listing_calls.append((campus, term, tuple(subjects or ())))
        return _filtered_listing(subjects)

    async def fake_subjects(campus, term):
        return SUBJECTS

    for module in (live_schedule, chat_module):
        monkeypatch.setattr(module, "get_class_listing", fake_listing)
        monkeypatch.setattr(module, "get_subjects", fake_subjects)
    monkeypatch.setattr(chat_module.neo4j_service, "is_configured", lambda: False)

    o = ChatOrchestrator()
    o.listing_calls = listing_calls
    o.llm = FakeLLM()
    monkeypatch.setattr(o.tools.gemini_file_search, "generate", o.llm)

    async def no_rag(*_args, **_kwargs):
        raise AssertionError("this turn must not fall through to website RAG")

    monkeypatch.setattr(o, "_handle_rag_query", no_rag)
    return o


def _ask(orch, message, history=None):
    return asyncio.run(orch.answer(message, history=history, campus="LAMC"))


# ----- turn 1 -----


def test_turn_1_answers_the_saturday_amp_question_from_the_live_schedule(orch):
    resp = _ask(orch, TURN_1, history=[{"role": "assistant", "content": WELCOME}])

    assert resp.route == "live_schedule"
    assert "INTRDGN 116" in resp.answer
    assert "#13944" in resp.answer and "#13945" in resp.answer
    # Saturday, but not in AMP:
    assert "MATH 240" not in resp.answer
    assert "#16585" not in resp.answer  # the Online Live twin of the same course
    # In AMP, but not Saturday:
    assert "ART " not in resp.answer
    assert "AMP" in resp.answer and "Saturday" in resp.answer


def test_the_welcome_banner_does_not_trigger_the_llm_rewriter(orch):
    _ask(orch, TURN_1, history=[{"role": "assistant", "content": WELCOME}])
    assert orch.llm.prompts == [], "a first turn has no conversation to resolve"


def test_empty_saturday_in_amp_result_says_what_was_searched(orch, monkeypatch):
    resp = _ask(orch, "are there classes in AMP on Sunday for fall 2026?")
    assert resp.route == "live_schedule"
    assert "AMP" in resp.answer and "Sunday" in resp.answer
    assert "#" not in resp.answer


# ----- turn 2 -----


def _turn_2_history():
    return [
        {"role": "user", "content": TURN_1},
        {"role": "assistant", "content": "(turn 1 answer)"},
    ]


def test_turn_2_lists_amp_building_sections_even_when_the_rewriter_expands_amp(orch):
    orch.llm.reply = json.dumps({"english": TURN_2_PROD_REWRITE, "lang": "en"})

    resp = _ask(orch, TURN_2, history=_turn_2_history())

    assert resp.route == "live_schedule"
    assert "MEDIART" not in resp.answer  # "Media Arts" was never asked for
    numbers = [s["class_number"] for s in resp.tool_payload["sections"]]
    assert len(numbers) == len(set(numbers)), "no section may be listed twice"
    assert "13944" in numbers
    assert all(s["location"].startswith("Mission-AMP") for s in resp.tool_payload["sections"])


def test_turn_2_survives_a_rewrite_that_drops_the_acronym(orch):
    orch.llm.reply = json.dumps(
        {"english": "Can you look up the schedules for all Arts, Media, and Performance classes for Fall 2026?",
         "lang": "en"}
    )
    resp = _ask(orch, TURN_2, history=_turn_2_history())
    assert "MEDIART" not in resp.answer
    assert all(s["location"].startswith("Mission-AMP") for s in resp.tool_payload["sections"])


def test_rewriter_is_told_not_to_expand_acronyms_or_add_intent_words(orch):
    orch.llm.reply = json.dumps({"english": TURN_2, "lang": "en"})
    _ask(orch, TURN_2, history=_turn_2_history())
    prompt = orch.llm.prompts[0].lower()
    assert "acronym" in prompt
    assert "program" in prompt and "did not" in prompt


def test_a_rewrite_that_invents_program_does_not_hijack_routing(orch):
    # Turn 1 as prod rewrote it. The student never said "program".
    orch.llm.reply = json.dumps(
        {"english": "Are there classes in the AMP program on Saturdays for Fall 2026?", "lang": "en"}
    )
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"}]
    resp = _ask(orch, TURN_1, history=history)
    assert resp.route == "live_schedule"
    assert "#13944" in resp.answer


def test_a_program_word_the_student_did_say_is_kept(orch):
    orch.llm.reply = json.dumps(
        {"english": "What are the requirements for the Art program?", "lang": "en"}
    )
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Hello!"}]
    result = orch.resolve_query("what's needed for the art program", history)
    assert "program" in result.english_query


def test_duplicate_subject_matches_are_fetched_and_listed_once(orch, monkeypatch):
    async def always_mediart(query, campus, term):
        return "MEDIART", "Media Arts"

    monkeypatch.setattr(chat_module, "find_subject_code", always_mediart)
    resp = asyncio.run(
        orch._handle_schedule_query("media arts multimedia classes for fall 2026", "LAMC")
    )
    numbers = [s["class_number"] for s in resp.tool_payload["sections"]]
    assert numbers.count("24717") == 1
    assert "Media Arts, Media Arts" not in resp.answer


# ----- building detection must not steal subjects or ordinary words -----


def test_a_subject_code_is_never_read_as_a_building(orch, monkeypatch):
    # ART is both a subject everywhere and a building at Valley and Pierce.
    # Give the fixture an ART building so the collision is real.
    listing = json.loads(json.dumps(LISTING))
    listing["subjects"][0]["courses"][0]["sections"][0]["meetings"][0]["room"] = "Mission-ART 101"

    async def listing_with_art_building(campus, term, subjects=None):
        return listing

    monkeypatch.setattr(chat_module, "get_class_listing", listing_with_art_building)
    assert "ART" in live_schedule.building_codes(listing)

    building = asyncio.run(orch._detect_building(["ART classes for fall 2026"], "LAMC", "2268"))
    assert building is None


def test_lowercase_building_needs_the_word_building(orch):
    resp = asyncio.run(
        orch._handle_schedule_query("classes in the amp building on saturday fall 2026", "LAMC")
    )
    assert [s["class_number"] for s in resp.tool_payload["sections"]] == ["13944", "13945"]


def test_campus_codes_are_not_buildings(orch):
    building = asyncio.run(orch._detect_building(["LAMC classes for fall 2026"], "LAMC", "2268"))
    assert building is None


# ----- turns 3 and 4: corrections -----


def _after_turn_2():
    return [
        {"role": "user", "content": TURN_1},
        {"role": "assistant", "content": "(turn 1 answer)"},
        {"role": "user", "content": TURN_2},
        {"role": "assistant", "content": PROD_TURN_2_ANSWER},
    ]


@pytest.mark.parametrize(
    "message",
    ["those do not belong to LAMC", "this is not correct", "that's wrong", "Not what I asked for"],
)
def test_a_correction_is_acknowledged_not_answered_with_the_same_search(orch, monkeypatch, message):
    async def must_not_search(*_a, **_k):
        raise AssertionError("a correction must not re-run the previous search")

    monkeypatch.setattr(orch, "_handle_schedule_query", must_not_search)

    resp = _ask(orch, message, history=_after_turn_2())

    assert resp.route == "user_correction"
    assert "Found" not in resp.answer
    assert "classsearchguest" in resp.answer  # the official Class Search link
    assert orch.llm.prompts == [], "no LLM call is needed to acknowledge a correction"


def test_correction_after_a_schedule_answer_names_the_data_source(orch):
    resp = _ask(orch, "those do not belong to LAMC", history=_after_turn_2())
    assert "district" in resp.answer.lower()
    assert "Los Angeles Mission College" in resp.answer


@pytest.mark.parametrize(
    "message",
    [
        # A real question that happens to contain "wrong".
        "what happens if I register for the wrong class section?",
        # A new request, not a complaint.
        "are there ART classes on Saturday for fall 2026?",
    ],
)
def test_questions_containing_correction_words_are_not_corrections(orch, message):
    assert not orch._is_user_correction(message, _after_turn_2())


def test_a_correction_with_no_previous_answer_is_not_a_correction(orch):
    assert not orch._is_user_correction("that's wrong", [{"role": "assistant", "content": WELCOME}])


# ----- small formatting fixes -----


def test_verified_details_footer_has_no_leading_pipe(orch, monkeypatch):
    async def enrichment(*_a, **_k):
        return {"uc_transferable": True, "csu_transferable": True, "ge_areas": []}

    monkeypatch.setattr(chat_module.neo4j_service, "get_course_enrichment", enrichment)
    text = asyncio.run(orch._get_schedule_enrichment("MEDIART", "117", "LAMC"))
    assert "*Verified academic details:* |" not in text
    assert "Transferable to: **UC, CSU**" in text


def test_program_route_with_no_program_match_falls_back_to_the_schedule(orch, monkeypatch):
    class NoPrograms:
        campus_name = "Los Angeles Mission College"

        async def search_programs(self, query):
            return []

    monkeypatch.setattr(chat_module, "get_program_mapper_client", lambda campus: NoPrograms())
    resp = asyncio.run(
        orch._handle_program_query(
            "Are there classes in the AMP program on Saturdays for Fall 2026?", "LAMC"
        )
    )
    assert resp.route == "live_schedule"
    assert "#13944" in resp.answer


def test_frontend_history_skips_the_welcome_banner():
    """The browser scraped the welcome bubble into `history`, so every first turn
    looked like a follow-up and was paid for (and mangled) by the rewriter. The
    backend now drops it too; this keeps the client honest."""
    app_js = (Path(__file__).resolve().parents[1] / "frontend/app.js").read_text()
    get_history = app_js.split("function getHistory()", 1)[1].split("\n}\n", 1)[0]
    assert "welcome-bubble" in get_history
