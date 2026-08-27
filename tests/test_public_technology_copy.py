from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.chat import ChatOrchestrator
from app.services.neo4j_service import neo4j_service
from shared.campus_config import CAMPUSES


FORBIDDEN_PUBLIC_TECHNOLOGY_TERMS = (
    "knowledge graph",
    "neo4j",
    "cypher",
)
EXPECTED_CRAWLER_DIRECTIVES = {
    "noindex",
    "nofollow",
    "noarchive",
    "nosnippet",
}
EXPECTED_CAMPUS_CHAT_LINKS = {
    "East Los Angeles College": "https://elac.chat/",
    "Los Angeles City College": "https://lacc.chat/",
    "Los Angeles Harbor College": "https://lahc.chat/",
    "Los Angeles Mission College": "https://lamc.chat/",
    "Los Angeles Pierce College": "https://lapc.chat/",
    "Los Angeles Southwest College": "https://lasc.chat/",
    "Los Angeles Trade-Technical College": "https://lattc.chat/",
    "Los Angeles Valley College": "https://lavc.chat/",
    "West Los Angeles College": "https://wlac.chat/",
}


def _directives(value: str) -> set[str]:
    return {directive.strip().lower() for directive in value.split(",") if directive.strip()}


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
@pytest.mark.parametrize("path", ("/", "/how-it-works"))
def test_public_campus_pages_do_not_disclose_internal_data_technology(
    campus_code: str,
    path: str,
) -> None:
    response = TestClient(app).get(path, headers={"host": f"{campus_code.lower()}.chat"})

    assert response.status_code == 200
    page = response.text.lower()
    for term in FORBIDDEN_PUBLIC_TECHNOLOGY_TERMS:
        assert term not in page
    assert re.search(r"\bgraph\b", page) is None


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
@pytest.mark.parametrize("path", ("/", "/how-it-works"))
def test_every_rendered_page_has_complete_robots_metadata(
    campus_code: str,
    path: str,
) -> None:
    response = TestClient(app).get(
        path,
        headers={"host": f"{campus_code.lower()}.chat"},
    )

    assert response.status_code == 200
    match = re.search(
        r'<meta\s+name="robots"\s+content="([^"]+)"',
        response.text,
        flags=re.IGNORECASE,
    )
    assert match is not None
    assert _directives(match.group(1)) == EXPECTED_CRAWLER_DIRECTIVES


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_how_it_works_links_to_every_campus_chatbot(campus_code: str) -> None:
    response = TestClient(app).get(
        "/how-it-works",
        headers={"host": f"{campus_code.lower()}.chat"},
    )

    assert response.status_code == 200
    assert response.text.count('class="campus-chat-link"') == len(
        EXPECTED_CAMPUS_CHAT_LINKS
    )
    for campus_name, url in EXPECTED_CAMPUS_CHAT_LINKS.items():
        assert f'href="{url}"' in response.text
        assert f'aria-label="Open the {campus_name} chatbot"' in response.text


@pytest.mark.parametrize(
    "path",
    ("/", "/how-it-works", "/styles.css", "/api/health", "/not-a-real-page"),
)
def test_every_http_response_has_complete_x_robots_tag(path: str) -> None:
    response = TestClient(app).get(path, headers={"host": "lamc.chat"})

    assert _directives(response.headers["x-robots-tag"]) == EXPECTED_CRAWLER_DIRECTIVES


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_every_campus_explains_rag_limits_and_advanced_capabilities(
    campus_code: str,
) -> None:
    client = TestClient(app)
    root = client.get(
        "/",
        headers={"host": f"{campus_code.lower()}.chat"},
    )
    how_it_works = client.get(
        "/how-it-works",
        headers={"host": f"{campus_code.lower()}.chat"},
    )

    assert "Typical higher-ed RAG chatbot" in root.text
    assert "Admissions, enrollment &amp; financial aid" in root.text
    assert "Explains applications and aid procedures" in root.text
    assert "Builds a candidate enrollment plan" in root.text
    assert "does not determine aid eligibility, access a student account, or guarantee that a seat is still open" in root.text
    assert "Counseling &amp; transfer" in root.text
    assert "Registration &amp; class schedules" in root.text
    assert "Academic programs &amp; degree planning" in root.text
    assert "RAG alone cannot reliably reconcile separate records" in root.text
    assert "Checks how a specific course applies" in root.text
    assert "Turns requirements into an ordered plan" in root.text
    assert "What this assistant can answer today" not in root.text
    assert "advanced-features" not in root.text

    assert "Traditional chatbots often use retrieval-augmented generation" in how_it_works.text
    assert "<strong>Admissions &amp; financial aid:</strong>" in how_it_works.text
    assert "<strong>Enrollment planning:</strong>" in how_it_works.text
    assert "does not determine aid eligibility or guarantee an open seat" in how_it_works.text
    assert "Is BIOLOGY 005 UC-transferable?" in how_it_works.text
    assert "BIOLOGY C100" not in how_it_works.text
    assert "This assistant" in how_it_works.text
    assert "verified academic facts" in how_it_works.text


def test_missing_academic_data_response_uses_neutral_public_language(
    monkeypatch,
) -> None:
    async def _no_similar_courses(*_args, **_kwargs):
        return []

    monkeypatch.setattr(neo4j_service, "find_similar_courses", _no_similar_courses)

    response = asyncio.run(
        ChatOrchestrator()._kg_not_found(
            "MATH",
            "999",
            "LAMC",
            "en",
            "course data",
        )
    )

    assert "verified academic records" in response.answer
    assert "knowledge graph" not in response.answer.lower()


def test_schedule_enrichment_uses_neutral_public_label(monkeypatch) -> None:
    async def _academic_details(*_args, **_kwargs):
        return {
            "uc_transferable": True,
            "csu_transferable": True,
            "ge_areas": [{"ge_system": "IGETC", "ge_code": "2A"}],
        }

    monkeypatch.setattr(neo4j_service, "get_course_enrichment", _academic_details)

    enrichment = asyncio.run(
        ChatOrchestrator()._get_schedule_enrichment("MATH", "261", "LAMC")
    )

    assert enrichment is not None
    assert "Verified academic details" in enrichment
    assert "knowledge graph" not in enrichment.lower()
