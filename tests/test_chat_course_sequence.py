from __future__ import annotations

import asyncio

from app.models import ChatResponse
from app.services import chat as chat_module
from app.services.chat import ChatOrchestrator


def _orchestrator() -> ChatOrchestrator:
    return object.__new__(ChatOrchestrator)


def test_sequence_query_detects_casual_student_wording():
    orchestrator = _orchestrator()

    assert orchestrator._is_sequence_query("how do i finish the nursing aa?")
    assert orchestrator._is_sequence_query("how should i complete the cyber security certificate?")
    assert orchestrator._is_sequence_query("can you map out my path for nursing?")
    assert orchestrator._is_sequence_query("in what order do i take cis classes?")
    assert orchestrator._is_sequence_query("what classes should i take first for the computer science degree?")


def test_program_phrase_extraction_removes_sequence_filler_words():
    orchestrator = _orchestrator()

    assert orchestrator._extract_program_phrase("How do I finish the nursing AA?") == "nursing aa"
    assert orchestrator._extract_program_phrase("How should I complete the Cyber Security certificate?") == "cyber security"
    assert orchestrator._extract_program_phrase("Can you map out my path for nursing?") == "nursing"
    assert orchestrator._extract_program_phrase("In what order do I take CIS classes?") == "cis"


def test_sequence_generation_failure_keeps_course_sequence_route(monkeypatch):
    orchestrator = _orchestrator()

    class FakeProgramMapperClient:
        async def search_programs(self, query: str) -> list[dict]:
            return [{
                "masterRecordId": "program-1",
                "title": "Cyber Security Associate",
                "awardShortTitle": "Certificate",
            }]

    class FakeSequenceService:
        async def generate_sequence(self, program_id: str, campus_code: str) -> dict:
            raise RuntimeError("boom")

    async def fake_rag(message: str, campus_code: str = "LAMC", lang: str = "en", use_live_crawl: bool = False) -> ChatResponse:
        return ChatResponse(answer="Fallback program information.", route="gemini_file_search")

    monkeypatch.setattr(chat_module, "get_program_mapper_client", lambda campus_code: FakeProgramMapperClient())
    monkeypatch.setattr(chat_module, "get_course_sequence_service", lambda campus_code: FakeSequenceService())
    monkeypatch.setattr(orchestrator, "_handle_rag_query", fake_rag)

    response = asyncio.run(
        orchestrator._handle_sequence_query(
            "course sequence for Cyber Security Associate",
            "LAMC",
            lang="en",
        )
    )

    assert response.route == "course_sequence"
    assert response.fallback_mode is True
    assert response.tool_payload["sequence_status"] == "generation_failed"
    assert response.tool_payload["fallback_route"] == "gemini_file_search"
    assert "couldn't build the semester-by-semester course sequence" in response.answer
    assert "Fallback program information." in response.answer
