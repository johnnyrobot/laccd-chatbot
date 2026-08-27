from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.services import program_mapper as pm
from app.services.program_mapper import ProgramMapperClient


def test_program_search_rejects_blank_query() -> None:
    client = ProgramMapperClient("LAMC")
    try:
        run(client.search_programs("   "))
    except ValueError as exc:
        assert "blank" in str(exc)
    else:
        raise AssertionError("blank query was accepted")


def run(coro):
    return asyncio.run(coro)


class _FakeLangfuse:
    def update_current_span(self, *args, **kwargs):
        return None


def test_program_mapper_caches_collections_search_and_returns_copies(monkeypatch):
    client = ProgramMapperClient("LAMC")
    calls: list[str] = []

    monkeypatch.setattr(pm, "get_client", lambda: _FakeLangfuse())

    async def fake_get(path: str):
        calls.append(path)
        if path.endswith("/home-page-content"):
            return {"programGroups": [{"masterRecordId": "g1", "title": "Health"}]}
        if "/program-groups/" in path:
            return {
                "programs": [
                    {
                        "masterRecordId": "p1",
                        "title": "Nursing",
                        "awardShortTitle": "AS",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(client, "_get", fake_get)

    first = run(client.search_programs("nursing"))
    first[0]["title"] = "mutated by caller"
    second = run(client.search_programs("nursing"))

    assert second[0]["title"] == "Nursing"
    assert second[0]["group_title"] == "Health"
    assert calls == [
        f"/site-contents/{client.site_content_id}/home-page-content",
        f"/site-contents/{client.site_content_id}/program-groups/g1",
    ]


def test_program_mapper_caches_details_requirements_and_careers_with_ttl(monkeypatch):
    client = ProgramMapperClient("LAMC")
    get_calls: list[str] = []
    post_calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake_get(path: str):
        get_calls.append(path)
        if "/programs/" in path:
            return {"masterRecordId": "p1", "title": "Nursing"}
        if "/program-maps/" in path:
            return {"programMapId": "m1", "requirements": []}
        raise AssertionError(path)

    async def fake_post(path: str, payload: dict):
        post_calls.append((path, tuple(payload["standardOccupationalCodes"])))
        return {"careers": [{"code": "29-1141", "title": "Registered Nurses"}]}

    monkeypatch.setattr(client, "_get", fake_get)
    monkeypatch.setattr(client, "_post", fake_post)

    assert run(client.get_program_details("p1"))["title"] == "Nursing"
    assert run(client.get_program_details("p1"))["title"] == "Nursing"
    assert run(client.get_program_requirements("m1"))["programMapId"] == "m1"
    assert run(client.get_program_requirements("m1"))["programMapId"] == "m1"
    assert run(client.get_careers(["29-1141"]))[0]["title"] == "Registered Nurses"
    assert run(client.get_careers(["29-1141"]))[0]["title"] == "Registered Nurses"

    assert get_calls == [
        f"/site-contents/{client.site_content_id}/programs/p1",
        f"/site-contents/{client.site_content_id}/program-maps/m1",
    ]
    assert post_calls == [("/standard-occupations/batch", ("29-1141",))]

    key = client._cache_key("program_details", "p1")
    value, _cached_at = client._cache[key]
    client._cache[key] = (value, datetime.now() - client.cache_ttl - timedelta(seconds=1))

    assert run(client.get_program_details("p1"))["title"] == "Nursing"
    assert get_calls.count(f"/site-contents/{client.site_content_id}/programs/p1") == 2


def test_program_mapper_cache_has_a_hard_capacity():
    client = ProgramMapperClient("LAMC")

    for index in range(300):
        client._set_cached(f"query:{index}", {"index": index})

    assert len(client._cache) <= 256
    assert "query:0" not in client._cache
    assert client._get_cached("query:299") == {"index": 299}


def test_program_mapper_all_campus_searches_are_campus_scoped_and_cached(monkeypatch):
    configs = {code: pm.COLLEGE_CONFIGS[code] for code in ("LAMC", "ELAC")}
    monkeypatch.setattr(pm, "COLLEGE_CONFIGS", configs)
    monkeypatch.setattr(pm, "_clients", {})
    monkeypatch.setattr(pm, "get_client", lambda: _FakeLangfuse())
    calls: list[tuple[str, str]] = []

    async def fake_get(self: ProgramMapperClient, path: str):
        calls.append((self.campus_code, path))
        if path.endswith("/home-page-content"):
            return {
                "programGroups": [
                    {"masterRecordId": f"{self.campus_code}-g1", "title": "Health"}
                ]
            }
        if "/program-groups/" in path:
            return {
                "programs": [
                    {
                        "masterRecordId": f"{self.campus_code}-p1",
                        "title": f"Nursing {self.campus_code}",
                        "awardShortTitle": "AS",
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(pm.ProgramMapperClient, "_get", fake_get)

    first = run(pm.search_programs_all_campuses("nursing"))
    second = run(pm.search_programs_all_campuses("nursing"))

    assert {(item["campus_code"], item["title"]) for item in first} == {
        ("LAMC", "Nursing LAMC"),
        ("ELAC", "Nursing ELAC"),
    }
    assert second == first
    assert len(calls) == 4
    assert {campus for campus, _path in calls} == {"LAMC", "ELAC"}
