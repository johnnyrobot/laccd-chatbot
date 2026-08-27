"""Program questions fall back to the recorded Neo4j program graph when the
external Program Mapper host is unreachable.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services import neo4j_service as neo4j_module
from app.services.program_mapper import (
    ProgramMapperClient,
    ProgramMapperUnavailable,
    egress_breaker,
)


@pytest.fixture(autouse=True)
def _clean_breaker():
    egress_breaker().reset()
    yield
    egress_breaker().reset()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://b.api.programmapper.com/x")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


PATHWAY_ELEMENTS = [
    {
        "name": "MATH 261",
        "shortDescription": "Calculus I",
        "recommendedOpportunity": {"minUnits": 5},
    },
    {
        "name": "CS 101",
        "shortDescription": "Intro to Computer Science",
        "recommendedOpportunity": {"minUnits": 3},
    },
]

GRAPH_PROGRAMS = [
    {
        "master_record_id": "prog-cs",
        "title": "Computer Science",
        "award_type": "AS-T",
        "description": "Transfer preparation for computer science majors.",
        "catalog_url": "https://example.edu/cs",
        "program_map_id": "map-cs",
        "campus_code": "LAMC",
        "group_title": "STEM",
    },
    {
        "master_record_id": "prog-nursing",
        "title": "Registered Nursing",
        "award_type": "AS",
        "description": "Clinical nursing preparation.",
        "catalog_url": "https://example.edu/rn",
        "program_map_id": "map-rn",
        "campus_code": "LAMC",
        "group_title": "Health",
    },
]

GRAPH_BUNDLE_ROW = {
    "master_record_id": "prog-cs",
    "title": "Computer Science",
    "award_type": "AS-T",
    "description": "Transfer preparation for computer science majors.",
    "catalog_url": "https://example.edu/cs",
    "program_map_id": "map-cs",
    "campus_code": "LAMC",
    "pathway_elements": json.dumps(PATHWAY_ELEMENTS),
    "careers": [
        {"title": "Software Developer", "avg_salary": 120, "soc_code": "15-1252"},
        {"title": "Web Developer", "avg_salary": 85, "soc_code": "15-1254"},
    ],
}


def _stub_graph(monkeypatch, *, programs=None, bundle_row=None, calls=None):
    """Answer the two fallback queries without a live Neo4j."""

    async def fake_run_query(cypher: str, params: dict | None = None) -> list[dict]:
        if calls is not None:
            calls.append(cypher)
        if "LEADS_TO_CAREER" in cypher:
            if bundle_row is None:
                return []
            if params and params.get("id") != bundle_row["master_record_id"]:
                return []
            return [bundle_row]
        return list(programs or [])

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", fake_run_query)


def _refuse_the_host(monkeypatch):
    """Make every outbound Program Mapper call fail the way the VPS does."""

    async def boom(*args, **kwargs):
        raise _status_error(403)

    monkeypatch.setattr(ProgramMapperClient, "_get", boom)
    monkeypatch.setattr(ProgramMapperClient, "_post", boom)


def _fresh_client() -> ProgramMapperClient:
    """A client with an empty cache, so a test never reads another's result."""
    return ProgramMapperClient("LAMC")


def test_a_refused_host_serves_search_from_the_graph(monkeypatch):
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS)

    matches = asyncio.run(_fresh_client().search_programs("computer science"))

    assert [match["masterRecordId"] for match in matches] == ["prog-cs"]
    assert matches[0]["awardShortTitle"] == "AS-T"
    assert matches[0]["title"] == "Computer Science"


def test_the_graph_search_matches_the_same_way_the_live_path_does(monkeypatch):
    """One matcher, not two. A second copy is how this codebase has drifted before."""
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS)
    client = _fresh_client()

    # compact match: punctuation and spacing removed on both sides
    assert asyncio.run(client.search_programs("computerscience"))
    # term-subset match, out of order
    assert asyncio.run(_fresh_client().search_programs("nursing registered"))
    # a genuine non-match stays a non-match
    assert asyncio.run(_fresh_client().search_programs("underwater basket weaving")) == []


def test_a_refused_host_serves_the_bundle_from_the_graph(monkeypatch):
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, bundle_row=GRAPH_BUNDLE_ROW)

    bundle = asyncio.run(_fresh_client().get_program_bundle("prog-cs"))

    assert bundle["campus_code"] == "LAMC"
    assert bundle["campus_name"] == "Los Angeles Mission College"
    assert bundle["program"]["title"] == "Computer Science"
    assert bundle["program"]["awardShortTitle"] == "AS-T"
    assert bundle["program"]["catalogUrl"] == "https://example.edu/cs"
    assert bundle["requirements"]["pathwayElements"] == PATHWAY_ELEMENTS
    assert bundle["careers"][0]["title"] == "Software Developer"
    assert bundle["careers"][0]["averageSalaryK"] == 120


def test_the_graph_bundle_renders_through_the_real_formatter(monkeypatch):
    """The shape is only right if the answer students see is right."""
    from app.services.chat import ChatOrchestrator

    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, bundle_row=GRAPH_BUNDLE_ROW)

    bundle = asyncio.run(_fresh_client().get_program_bundle("prog-cs"))
    rendered = ChatOrchestrator._format_program_bundle(ChatOrchestrator, bundle)

    assert "Computer Science — AS-T (Los Angeles Mission College)" in rendered
    assert "MATH 261: Calculus I (5 units)" in rendered
    assert "Software Developer: avg $120K" in rendered
    assert "https://example.edu/cs" in rendered


def test_a_missing_record_is_not_answered_from_the_graph(monkeypatch):
    """A 404 means that program does not exist, not that the host is unreachable.

    Silently substituting graph data here would turn a real "no such program"
    into a stale answer with no way to tell the difference.
    """

    async def not_found(*args, **kwargs):
        raise _status_error(404)

    monkeypatch.setattr(ProgramMapperClient, "_get", not_found)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS, bundle_row=GRAPH_BUNDLE_ROW)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(_fresh_client().get_program_bundle("prog-cs"))


def test_an_empty_graph_re_raises_so_callers_still_reach_rag(monkeypatch):
    """Without seeded data the old behaviour must survive exactly."""
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, programs=[], bundle_row=None)

    with pytest.raises(Exception):
        asyncio.run(_fresh_client().search_programs("computer science"))
    with pytest.raises(Exception):
        asyncio.run(_fresh_client().get_program_bundle("prog-cs"))


def test_an_open_breaker_reaches_the_graph_without_a_network_call(monkeypatch):
    """In prod the breaker opens on the first 403, so this is the steady state."""
    egress_breaker().record_failure(_status_error(403))

    attempted = False

    class _Boom:
        def __init__(self, *a, **k):
            nonlocal attempted
            attempted = True

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS)

    matches = asyncio.run(_fresh_client().search_programs("computer science"))

    assert [match["masterRecordId"] for match in matches] == ["prog-cs"]
    assert not attempted, "an open breaker must not construct an HTTP client"


def test_a_reachable_host_is_never_second_guessed_by_the_graph(monkeypatch):
    """Dev and the laptop are not blocked; they must keep getting live data."""
    live_programs = [{"masterRecordId": "live-cs", "title": "Computer Science", "awardShortTitle": "AS-T"}]

    async def live_get(self, path: str) -> dict:
        if "home-page-content" in path:
            return {"programGroups": [{"masterRecordId": "g1", "title": "STEM"}]}
        return {"programs": live_programs}

    monkeypatch.setattr(ProgramMapperClient, "_get", live_get)
    calls: list[str] = []
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS, calls=calls)

    matches = asyncio.run(_fresh_client().search_programs("computer science"))

    assert [match["masterRecordId"] for match in matches] == ["live-cs"]
    assert calls == [], "the graph must not be queried while the host answers"


def test_graph_results_are_not_cached_as_if_they_were_live(monkeypatch):
    """The vendor may allowlist us at any moment; a 7-day cache of fallback data
    would keep serving the graph long after the live API came back."""
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS)
    client = _fresh_client()

    asyncio.run(client.search_programs("computer science"))

    assert client.cache_stats()["entries"] == 0


def test_the_fallback_is_reported_on_the_payload(monkeypatch):
    """Nothing surfaced the 403 for months. A caller must be able to tell that
    an answer came from the weekly snapshot rather than the live vendor."""
    _refuse_the_host(monkeypatch)
    _stub_graph(monkeypatch, programs=GRAPH_PROGRAMS, bundle_row=GRAPH_BUNDLE_ROW)

    matches = asyncio.run(_fresh_client().search_programs("computer science"))
    bundle = asyncio.run(_fresh_client().get_program_bundle("prog-cs"))

    assert matches[0]["source"] == "neo4j_snapshot"
    assert bundle["source"] == "neo4j_snapshot"


def test_unavailable_is_still_raised_when_the_graph_cannot_answer(monkeypatch):
    """A breaker-skipped call with no graph data must look exactly like before."""
    egress_breaker().record_failure(_status_error(403))
    _stub_graph(monkeypatch, programs=[], bundle_row=None)

    with pytest.raises(ProgramMapperUnavailable):
        asyncio.run(_fresh_client().search_programs("computer science"))
