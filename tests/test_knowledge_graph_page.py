from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.services import knowledge_graph_view as view
from shared.campus_config import CAMPUSES


def _counts(courses: int) -> dict[str, Any]:
    return {
        "Course": courses,
        "Program": 100,
        "Instructor": 200,
        "Subject": 50,
        "careers": 300,
        "ge_areas": 40,
        "books": 41,
        "edges": {"PREREQUISITE_OF": 12, "RENUMBERED_TO": 7, "TEACHES": 9},
        "connections": 1234,
    }


def _walk(**overrides: Any) -> dict[str, Any]:
    walk = {
        "code": "MATH 262",
        "title": "Calculus II",
        "uc": True,
        "csu": True,
        "old": "MATH 266",
        "olds_total": 1,
        "chain": [
            {"code": "MATH 260", "title": "Pre-Calculus"},
            {"code": "MATH 261", "title": "Calculus I"},
        ],
        "unlocks": [{"code": "MATH 263", "title": "Calculus III"}],
        "unlocks_total": 1,
        "ge": [{"system": "IGETC", "code": "2A", "name": "Mathematical Concepts"}],
        "ge_total": 1,
        "programs": ["Mathematics 2.0"],
        "programs_total": 1,
        "careers": ["Statisticians"],
        "careers_total": 1,
        "links": 6,
    }
    walk.update(overrides)
    return walk


def _snapshot(walk: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "campuses": {code: _counts(1000 + i) for i, code in enumerate(sorted(CAMPUSES))},
        "walks": {code: walk for code in CAMPUSES},
        "district": {"courses": 9036, "programs": 900, "connections": 11106},
        "read_at": "2026-11-06T20:00:00+00:00",
    }


def _get(monkeypatch, snapshot: dict[str, Any] | None, campus: str = "LAMC"):
    async def fake_snapshot():
        return snapshot

    monkeypatch.setattr(main_module, "get_knowledge_graph_snapshot", fake_snapshot)
    return TestClient(app).get(
        "/knowledge-graph", headers={"host": f"{campus.lower()}.chat"}
    )


def _step_keys(html: str) -> list[str]:
    return re.findall(r'class="kg-step" data-key="(\w+)"', html)


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_every_campus_gets_its_own_page(monkeypatch, campus_code: str) -> None:
    response = _get(monkeypatch, _snapshot(_walk()), campus_code)

    assert response.status_code == 200
    assert CAMPUSES[campus_code].name in response.text
    assert CAMPUSES[campus_code].accent_hover in response.text
    embedded = re.search(
        r'<script type="application/json" id="kg-data">(.*?)</script>',
        response.text,
        flags=re.DOTALL,
    )
    assert json.loads(embedded.group(1))["campus"] == campus_code


def test_page_keeps_crawlers_out_and_never_names_the_database(monkeypatch) -> None:
    response = _get(monkeypatch, _snapshot(_walk()))
    page = response.text.lower()

    assert 'content="noindex, nofollow, noarchive, nosnippet"' in page
    assert "neo4j" not in page
    assert "cypher" not in page


def test_full_tour_runs_from_district_to_a_real_question(monkeypatch) -> None:
    response = _get(monkeypatch, _snapshot(_walk()))

    assert _step_keys(response.text) == [
        "district", "campus", "course", "renumber", "path", "counts", "answer",
    ]
    assert "MATH 260 to MATH 261 to MATH 262" in response.text
    assert "What are the prerequisites for MATH 262, and is it transferable?" in response.text


def test_tour_drops_steps_the_course_has_nothing_to_show_for(monkeypatch) -> None:
    sparse = _walk(old=None, chain=[], unlocks=[], unlocks_total=0)

    response = _get(monkeypatch, _snapshot(sparse))

    assert _step_keys(response.text) == ["district", "campus", "course", "counts", "answer"]
    assert "Is MATH 262 transferable, and which general education areas" in response.text


def test_final_step_only_claims_the_facts_this_course_has(monkeypatch) -> None:
    html = _get(monkeypatch, _snapshot(_walk(ge=[], ge_total=0))).text

    assert "reads prerequisites and transfer status straight from the map" in html
    assert "<code>FULFILLS_GE</code> <code>REQUIRES_COURSE</code>" not in html


def test_page_still_renders_when_the_graph_has_never_answered(monkeypatch) -> None:
    response = _get(monkeypatch, None)

    assert response.status_code == 200
    assert _step_keys(response.text) == ["district", "campus"]
    assert "Live numbers are unavailable right now" in response.text
    assert "What stays out of the map" in response.text


def test_connection_table_adds_up_to_the_stated_total(monkeypatch) -> None:
    snapshot = _snapshot(_walk())
    kinds = (
        "HAS_COURSE", "FULFILLS_GE", "REQUIRES_COURSE", "LEADS_TO_CAREER", "TEACHES",
        "PREREQUISITE_OF", "ADVISORY_FOR", "COREQUISITE_OF", "RENUMBERED_TO", "IN_GROUP",
        "HAS_OPENSTAX_BOOK", "AT_CAMPUS", "TEACHES_AT", "OFFERS_SUBJECT",
    )
    counts = snapshot["campuses"]["LAMC"]
    counts["edges"] = {kind: 1000 + i for i, kind in enumerate(kinds)}
    counts["connections"] = sum(counts["edges"].values())

    html = _get(monkeypatch, snapshot).text

    table = html[html.index("kg-table"):html.index("Nine colleges side by side")]
    shown = re.findall(r'<td class="kg-num">([\d,]+)</td>\s*<td class="kg-narrow-hide"><code>', table)
    assert sum(int(n.replace(",", "")) for n in shown) == counts["connections"]
    assert f'{counts["connections"]:,}' in table


def test_comparison_bars_stay_proportional_and_largest_first(monkeypatch) -> None:
    snapshot = _snapshot(_walk())
    snapshot["campuses"]["LACC"]["Course"] = 2785
    snapshot["campuses"]["LAPC"]["Course"] = 2862
    snapshot["campuses"]["ELAC"]["Course"] = 3049

    html = _get(monkeypatch, snapshot).text

    widths = re.findall(r'kg-bar-fill" style="width: ([\d.]+)%"', html)
    assert widths[:3] == ["100.0", "93.9", "91.3"]
    assert [float(w) for w in widths] == sorted(float(w) for w in widths)[::-1]


def test_page_data_cannot_break_out_of_its_script_block(monkeypatch) -> None:
    hostile = _walk(title="</script><script>alert(1)</script>")

    response = _get(monkeypatch, _snapshot(hostile))

    assert "<script>alert(1)" not in response.text


def test_chat_page_does_not_link_to_this_page() -> None:
    response = TestClient(app).get("/", headers={"host": "lamc.chat"})

    assert "knowledge-graph" not in response.text


@pytest.mark.parametrize("campus_code", sorted(CAMPUSES))
def test_how_it_works_links_to_this_page_once(campus_code: str) -> None:
    response = TestClient(app).get(
        "/how-it-works", headers={"host": f"{campus_code.lower()}.chat"}
    )

    assert response.text.count('href="/knowledge-graph"') == 1
    assert f"See how {campus_code}’s records connect" in response.text


def test_stage_color_stays_readable_on_a_projector() -> None:
    def luminance(hex_color: str) -> float:
        channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [
            c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            for c in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    for campus in CAMPUSES.values():
        assert 1.05 / (luminance(campus.accent_hover) + 0.05) >= 7, campus.code


def _candidate(code: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "code": code, "renumbered": 1, "uc": True,
        "before": 1, "after": 1, "ge": 1, "programs": 1,
    }
    row.update(overrides)
    return row


def test_example_course_is_the_one_with_the_most_to_show() -> None:
    thin = _candidate("AAA 100", before=0, ge=0, programs=0)
    rich = _candidate("ZZZ 100")

    assert min([thin, rich], key=view.candidate_rank)["code"] == "ZZZ 100"


def test_a_campus_without_renumberings_still_gets_an_example_course() -> None:
    rows = [_candidate("BIOL 101", renumbered=0)]

    assert min(rows, key=view.candidate_rank)["code"] == "BIOL 101"


def test_full_course_scan_runs_only_for_a_campus_with_no_renumberings(monkeypatch) -> None:
    queries: list[str] = []

    async def query(cypher: str, _params: dict[str, Any]):
        queries.append(cypher)
        if cypher is view._WALK_DETAIL:
            return []
        if cypher is view._RENUMBERED_CANDIDATES:
            return [] if campus_has_none else [_candidate("ZZZ 100")]
        return [_candidate("BIOL 101", renumbered=0)]

    monkeypatch.setattr(view.neo4j_service, "run_query", query)

    campus_has_none = False
    asyncio.run(view._load_walk("LAMC"))
    assert view._ALL_CANDIDATES not in queries

    campus_has_none = True
    asyncio.run(view._load_walk("LAMC"))
    assert view._ALL_CANDIDATES in queries


@pytest.mark.parametrize(
    ("code", "readable"),
    (
        ("MATH 262", True),
        ("STAT C1000", True),
        ("PHYSICS 102", True),
        ("ADM JUS 165", True),
        ("MUSIC 216-2", False),
    ),
)
def test_example_course_must_be_one_the_chatbot_reads_whole(code: str, readable: bool) -> None:
    assert view.chatbot_reads_code(code) is readable


def test_unreadable_course_is_never_chosen_as_the_example(monkeypatch) -> None:
    detail_codes: list[str] = []

    async def query(cypher: str, params: dict[str, Any]):
        if cypher is view._WALK_DETAIL:
            detail_codes.append(params["code"])
            return []
        return [
            _candidate("MUSIC 216-2", before=5, programs=9),
            _candidate("MUSIC 101", before=0),
        ]

    monkeypatch.setattr(view.neo4j_service, "run_query", query)

    asyncio.run(view._load_walk("LAHC"))

    assert detail_codes == ["MUSIC 101"]


def test_a_renumbered_course_is_preferred_as_the_example() -> None:
    plain = _candidate("AAA 100", renumbered=0)
    renumbered = _candidate("ZZZ 100", renumbered=1)

    assert min([plain, renumbered], key=view.candidate_rank)["code"] == "ZZZ 100"


def test_example_course_choice_is_repeatable_on_ties() -> None:
    rows = [_candidate("MATH 262"), _candidate("CHEM 101"), _candidate("PHYS 101")]

    assert min(rows, key=view.candidate_rank)["code"] == "CHEM 101"
    assert min(reversed(rows), key=view.candidate_rank)["code"] == "CHEM 101"


def test_prerequisite_chain_is_longest_path_without_the_course_itself() -> None:
    step = lambda code: {"code": code, "title": code}  # noqa: E731
    chains = [
        [step("MATH 261"), step("MATH 262")],
        [step("MATH 260"), step("MATH 261"), step("MATH 262")],
    ]

    assert [s["code"] for s in view.best_chain(chains)] == ["MATH 260", "MATH 261"]


def test_prerequisite_chain_ignores_loops_in_the_data() -> None:
    step = lambda code: {"code": code, "title": code}  # noqa: E731
    looped = [step("MATH 262"), step("MATH 263"), step("MATH 262")]
    honest = [step("MATH 261"), step("MATH 262")]

    assert view.best_chain([looped]) == []
    assert [s["code"] for s in view.best_chain([looped, honest])] == ["MATH 261"]


def test_prerequisite_chain_prefers_the_main_course_over_its_support_section() -> None:
    step = lambda code: {"code": code, "title": code}  # noqa: E731
    chains = [[step("MATH 261S"), step("MATH 262")], [step("MATH 261"), step("MATH 262")]]

    assert [s["code"] for s in view.best_chain(chains)] == ["MATH 261"]


def test_programs_with_the_same_title_appear_once_but_all_count() -> None:
    row = {
        "code": "CS 136", "title": "Data Structures", "uc": True, "csu": True,
        "olds": [], "before": 0, "ge": [], "unlocks": [], "chains": [], "careers": [],
        "programs": [
            {"id": "1", "title": "Computer Science"},
            {"id": "2", "title": "Computer Science"},
            {"id": "3", "title": "Programming"},
        ],
    }

    walk = view.build_walk(row)

    assert walk["programs"] == ["Computer Science", "Programming"]
    assert walk["programs_total"] == 3


def test_walk_caps_lists_but_keeps_true_totals() -> None:
    row = {
        "code": "STAT C1000",
        "title": "Introduction to Statistics",
        "uc": True,
        "csu": True,
        "olds": ["MATH 227S", "MATH 227"],
        "before": 3,
        "ge": [{"system": "IGETC", "code": str(i), "name": "x"} for i in range(6)],
        "unlocks": [{"code": f"PSYCH {i}", "title": "x"} for i in range(5)],
        "chains": [],
        "programs": [{"id": str(i), "title": f"Program {i}"} for i in range(25)],
        "careers": [f"Career {i}" for i in range(9)],
    }

    walk = view.build_walk(row)

    assert walk["old"] == "MATH 227"
    assert len(walk["programs"]) == view.MAX_PROGRAMS and walk["programs_total"] == 25
    assert len(walk["careers"]) == view.MAX_CAREERS and walk["careers_total"] == 9
    assert len(walk["ge"]) == view.MAX_GE and walk["ge_total"] == 6
    assert walk["links"] == 2 + 3 + 6 + 5 + 25


@pytest.fixture
def cache(monkeypatch) -> view._SnapshotCache:
    monkeypatch.setattr(view.neo4j_service, "is_configured", lambda: True)
    return view._SnapshotCache()


def test_summary_is_reused_instead_of_requeried(monkeypatch, cache) -> None:
    calls = []

    async def build():
        calls.append(1)
        return {"read_at": "now"}

    monkeypatch.setattr(view, "_build_snapshot", build)

    async def twice():
        return await cache.get(), await cache.get()

    first, second = asyncio.run(twice())

    assert first == second == {"read_at": "now"}
    assert len(calls) == 1


def test_failed_refresh_keeps_serving_the_last_good_numbers(monkeypatch, cache) -> None:
    async def broken():
        raise RuntimeError("database restarting")

    monkeypatch.setattr(view, "_build_snapshot", broken)
    cache.value = {"read_at": "earlier"}

    assert asyncio.run(cache.get()) == {"read_at": "earlier"}


def test_failed_refresh_waits_before_trying_the_database_again(monkeypatch, cache) -> None:
    calls = []

    async def broken():
        calls.append(1)
        raise RuntimeError("database restarting")

    monkeypatch.setattr(view, "_build_snapshot", broken)

    async def twice():
        await cache.get()
        await cache.get()

    asyncio.run(twice())

    assert len(calls) == 1


def test_hung_database_cannot_hang_the_page(monkeypatch, cache) -> None:
    async def hang():
        await asyncio.sleep(60)

    monkeypatch.setattr(view, "_build_snapshot", hang)
    monkeypatch.setattr(view, "BUILD_TIMEOUT_SECONDS", 0.01)

    assert asyncio.run(cache.get()) is None


def test_unconfigured_database_means_no_numbers_rather_than_an_error(monkeypatch) -> None:
    monkeypatch.setattr(view.neo4j_service, "is_configured", lambda: False)

    assert asyncio.run(view._SnapshotCache().get()) is None
