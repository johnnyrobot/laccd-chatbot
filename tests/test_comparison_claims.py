from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.models import ChatResponse
from app.services import chat as chat_module
from app.services import enrollment_planner as planner_module
from app.services.chat import ChatOrchestrator
from shared.program_matching import select_explicit_program
from voice_agent import prompts as voice_prompts
from voice_agent import tools as voice_tools


def run(coro):
    return asyncio.run(coro)


def test_explicit_program_selection_works_inside_a_longer_rewritten_question():
    matches = [
        {
            "masterRecordId": "data-science",
            "title": "Data Science",
            "awardShortTitle": "Certificate of Achievement",
        },
        {
            "masterRecordId": "data-analytics",
            "title": "Data Analytics",
            "awardShortTitle": "Certificate of Achievement",
        },
        {
            "masterRecordId": "health-science",
            "title": "Health Science",
            "awardShortTitle": "Associate of Science",
        },
    ]

    selected = select_explicit_program(
        "What is the complete prerequisite path for the LAMC Data Science "
        "(Certificate of Achievement), and how should I sequence the courses "
        "at 9 units per term?",
        matches,
    )

    assert [match["masterRecordId"] for match in selected] == ["data-science"]


def test_sequence_handler_uses_explicit_program_instead_of_repeating_choices(monkeypatch):
    orchestrator = ChatOrchestrator()

    class FakeProgramMapperClient:
        async def search_programs(self, query: str) -> list[dict]:
            return [
                {
                    "masterRecordId": "data-science",
                    "title": "Data Science",
                    "awardShortTitle": "Certificate of Achievement",
                    "campus_code": "LAMC",
                },
                {
                    "masterRecordId": "data-analytics",
                    "title": "Data Analytics",
                    "awardShortTitle": "Certificate of Achievement",
                    "campus_code": "LAMC",
                },
                {
                    "masterRecordId": "health-science",
                    "title": "Health Science",
                    "awardShortTitle": "Associate of Science",
                    "campus_code": "LAMC",
                },
                {
                    "masterRecordId": "political-science",
                    "title": "Political Science",
                    "awardShortTitle": "Associate in Arts for Transfer",
                    "campus_code": "LAMC",
                },
            ]

    class FakeSequenceService:
        async def generate_sequence(self, program_id, campus_code, **kwargs):
            assert program_id == "data-science"
            assert kwargs["pace"] == "part_time"
            return {
                "program_name": "Data Science",
                "award_type": "Certificate of Achievement",
                "total_units": 18,
                "terms_to_complete": 2,
                "sequence": [],
                "prerequisite_chains": [],
            }

    monkeypatch.setattr(
        chat_module,
        "get_program_mapper_client",
        lambda campus_code: FakeProgramMapperClient(),
    )
    monkeypatch.setattr(
        chat_module,
        "get_course_sequence_service",
        lambda campus_code: FakeSequenceService(),
    )
    monkeypatch.setattr(
        orchestrator.tools.gemini_file_search,
        "is_configured",
        lambda: False,
    )

    response = run(
        orchestrator._handle_sequence_query(
            "What is the complete prerequisite path for the LAMC Data Science "
            "(Certificate of Achievement), and how should I sequence the courses "
            "at 9 units per term?",
            "LAMC",
        )
    )

    assert response.route == "course_sequence"
    assert "COURSE SEQUENCE PLAN: Data Science" in response.answer
    assert "Which one do you want" not in response.answer


def test_combined_course_question_uses_comprehensive_course_handler(monkeypatch):
    orchestrator = object.__new__(ChatOrchestrator)
    monkeypatch.setattr(
        chat_module.neo4j_service,
        "resolve_course_subject",
        AsyncMock(return_value="MATH"),
    )
    comprehensive = AsyncMock(
        return_value=ChatResponse(
            answer="MATH 227 became STAT C1000 and is UC/CSU transferable.",
            route="knowledge_graph",
        )
    )
    monkeypatch.setattr(orchestrator, "_kg_course_info", comprehensive)

    response = run(
        orchestrator._handle_kg_query(
            "Is MATH 227 equivalent to STAT C1000, is it UC/CSU transferable, "
            "and which GE areas does it satisfy?",
            "LAMC",
        )
    )

    assert "became STAT C1000" in response.answer
    comprehensive.assert_awaited_once_with("MATH", "227", "LAMC", "en")


def test_candidate_enrollment_plan_combines_units_prereqs_cost_and_times(monkeypatch):
    sections = [
        {
            "course_code": "ENGL C1000",
            "course_title": "Academic Reading and Writing",
            "class_number": "10001",
            "days": "M W",
            "time": "9:00 AM - 10:25 AM",
            "class_type": ["OER"],
        },
        {
            "course_code": "COMM C1000",
            "course_title": "Public Speaking",
            "class_number": "10002",
            "days": "T Th",
            "time": "9:00 AM - 10:25 AM",
            "class_type": ["OER"],
        },
        {
            "course_code": "ART 101",
            "course_title": "Survey of Art History",
            "class_number": "10003",
            "days": "M W",
            "time": "10:30 AM - 11:55 AM",
            "class_type": ["LCT"],
        },
        {
            "course_code": "SOC 001",
            "course_title": "Introduction to Sociology",
            "class_number": "10004",
            "days": "T Th",
            "time": "10:30 AM - 11:55 AM",
            "class_type": [],
        },
        {
            "course_code": "MATH 261",
            "course_title": "Calculus I",
            "class_number": "10005",
            "days": "M W",
            "time": "9:00 AM - 10:25 AM",
            "class_type": ["OER"],
        },
        {
            "course_code": "BIOLOGY 005",
            "course_title": "Introduction to Human Biology",
            "class_number": "10006",
            "days": "F",
            "time": "9:00 AM - 11:55 AM",
            "class_type": ["OER"],
        },
    ]

    async def fake_schedule(campus, term):
        return {
            "campus": campus,
            "campus_name": "Los Angeles Mission College",
            "term": term,
            "term_name": "Spring 2026",
            "sections": sections,
        }

    metadata = {
        code: {
            "title": section["course_title"],
            "units_min": 3,
            "prerequisites": ["MATH 260"] if code == "MATH 261" else [],
            "catalog_prereq_text": "",
            "corequisites": ["BIOLOGY 006"] if code == "BIOLOGY 005" else [],
            "catalog_coreq_text": "",
        }
        for code, section in ((section["course_code"], section) for section in sections)
    }

    monkeypatch.setattr(planner_module, "search_live_schedule", fake_schedule)
    monkeypatch.setattr(
        planner_module.neo4j_service,
        "get_course_metadata_bulk",
        AsyncMock(return_value=metadata),
    )

    payload = run(
        planner_module.build_candidate_enrollment_plan(
            "LAMC",
            "2264",
            target_units=12,
        )
    )

    assert payload["total_units"] == 12
    assert len(payload["sections"]) == 4
    assert "MATH 261" not in {section["course_code"] for section in payload["sections"]}
    assert "BIOLOGY 005" not in {section["course_code"] for section in payload["sections"]}
    assert any(section["cost_label"] == "ZTC" for section in payload["sections"])
    assert payload["seat_status_available"] is False


def test_exact_enrollment_claim_routes_to_candidate_planner():
    orchestrator = object.__new__(ChatOrchestrator)

    assert orchestrator._is_enrollment_plan_query(
        "i need 12 units. which offered courses have no listed prerequisites, "
        "and which have zero-cost materials?"
    )
    assert orchestrator._is_enrollment_plan_query(
        "build a 12-unit plan from offered courses with no listed prerequisites "
        "and identify ztc sections."
    )


def test_program_sequence_with_unit_pace_is_not_generic_enrollment_plan():
    orchestrator = object.__new__(ChatOrchestrator)

    assert not orchestrator._is_enrollment_plan_query(
        "what is the complete prerequisite path for the lamc data science "
        "(certificate of achievement), and how should i sequence the courses "
        "at 9 units per term?"
    )


def test_cross_campus_ge_schedule_returns_each_campus_and_cost_markers(monkeypatch):
    orchestrator = object.__new__(ChatOrchestrator)
    monkeypatch.setattr(chat_module, "ALL_CAMPUSES", ["LAMC", "ELAC"])
    monkeypatch.setattr(
        chat_module,
        "CAMPUS_NAMES",
        {
            "LAMC": "Los Angeles Mission College",
            "ELAC": "East Los Angeles College",
        },
    )

    async def fake_ge_courses(ge_code, system, campus_code):
        return [
            {
                "course_code": "MATH 230",
                "title": "Mathematics for Liberal Arts Students",
                "subject_code": "MATH",
            }
        ]

    async def fake_schedule(campus_code, term, subjects):
        return {
            "subjects": [
                {"courses": [{
                    "subject": "MATH",
                    "catalogNbr": "230",
                    "descr": "Mathematics for Liberal Arts Students",
                    "sections": [{
                        "classNbr": f"{campus_code}-1 (LEC)",
                        "meetings": [{"room": "Online", "instr": "Staff"}],
                        "classType": ["OER"] if campus_code == "LAMC" else ["LCT"],
                        "relsections": [],
                    }],
                }]}
            ]
        }

    monkeypatch.setattr(
        chat_module.neo4j_service,
        "get_ge_courses",
        fake_ge_courses,
    )
    monkeypatch.setattr(chat_module, "get_class_listing", fake_schedule)

    response = run(
        orchestrator._handle_ge_schedule_query(
            "Which IGETC Area 2A courses are offered this term across LACCD, "
            "and which have zero-cost materials?",
            "LAMC",
        )
    )

    assert response.tool_payload["scope"] == "district"
    assert response.tool_payload["cost_sections"] == 2
    assert "Los Angeles Mission College" in response.answer
    assert "East Los Angeles College" in response.answer
    assert "ZTC" in response.answer
    assert "Low Cost" in response.answer
    assert "does not expose reliable live seat counts" in response.answer


def test_voice_multi_fact_tool_uses_same_chat_orchestrator(monkeypatch):
    calls = []

    class FakeOrchestrator:
        async def answer(self, question, campus):
            calls.append((question, campus))
            return ChatResponse(
                answer="**MATH 227** was renumbered to **STAT C1000**.",
                route="knowledge_graph",
            )

    monkeypatch.setattr(
        voice_tools,
        "get_chat_orchestrator",
        lambda: FakeOrchestrator(),
    )

    answer = run(
        voice_tools.academic_answer(
            "Is MATH 227 now STAT C1000 and is it transferable?",
            "LAMC",
        )
    )

    assert calls == [
        ("Is MATH 227 now STAT C1000 and is it transferable?", "LAMC")
    ]
    assert answer == "MATH 227 was renumbered to STAT C1000."


def test_voice_district_ge_summary_keeps_every_college_and_cost_examples():
    answer = voice_tools._voice_district_ge_summary(
        {
            "system": "IGETC",
            "ge_code": "2A",
            "offered_sections": 3,
            "cost_sections": 2,
            "campuses": {
                "LAMC": {
                    "campus_name": "Los Angeles Mission College",
                    "offered_sections": [
                        {"course_code": "MATH 230", "class_type": ["OER"]},
                        {"course_code": "STAT C1000", "class_type": []},
                    ],
                },
                "ELAC": {
                    "campus_name": "East Los Angeles College",
                    "offered_sections": [
                        {"course_code": "MATH 260", "class_type": ["LCT"]},
                    ],
                },
            },
        }
    )

    assert "across 2 colleges" in answer
    assert "Los Angeles Mission College" in answer
    assert "East Los Angeles College" in answer
    assert "MATH 230" in answer
    assert "MATH 260" in answer
    assert "not reliable live seat counts" in answer


def test_voice_prompt_requires_one_shared_tool_for_combined_claims():
    prompt = voice_prompts.build_voice_prompt(
        __import__("shared.campus_config", fromlist=["CAMPUSES"]).CAMPUSES["LAMC"]
    )

    assert "ask_verified_academic_question exactly once" in prompt
    assert "Do not split a combined question across separate tools" in prompt
    assert "question argument MUST be a verbatim copy" in prompt
    assert "I need 12 units" in prompt
