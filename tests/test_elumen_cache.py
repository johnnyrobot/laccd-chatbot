from __future__ import annotations

import asyncio
import time

from app.services.elumen_service import CAMPUS_TENANTS, MAX_CACHE_ENTRIES, ElumenService


def test_elumen_cache_has_hard_capacity() -> None:
    service = ElumenService()
    for index in range(MAX_CACHE_ENTRIES + 20):
        service._cache_put(service._course_cache, f"course:LAMC:{index}", {"index": index})
    assert len(service._course_cache) == MAX_CACHE_ENTRIES
    assert len(service._cache_timestamps) == MAX_CACHE_ENTRIES


def run(coro):
    return asyncio.run(coro)


def test_elumen_course_and_prereq_caches_are_campus_scoped(monkeypatch):
    service = ElumenService()
    calls: list[tuple[str, str]] = []

    async def fake_search(*, query: str, tenant: str, page: int, page_size: int):
        calls.append((query, tenant))
        title = "Mission Calculus" if tenant == CAMPUS_TENANTS["LAMC"] else "East Calculus"
        prereq = "MATH 260" if tenant == CAMPUS_TENANTS["LAMC"] else "MATH 270"
        return [
            {
                "code": "MATH 261",
                "title": title,
                "prerequisites": [[prereq]],
            }
        ]

    monkeypatch.setattr(service, "_search_courses_live", fake_search)

    lamc = run(service.get_course_details("MATH 261", "LAMC"))
    elac = run(service.get_course_details("MATH 261", "ELAC"))
    lamc_again = run(service.get_course_details("MATH 261", "LAMC"))

    assert lamc["title"] == "Mission Calculus"
    assert elac["title"] == "East Calculus"
    assert lamc_again["title"] == "Mission Calculus"
    assert calls == [
        ("MATH261", CAMPUS_TENANTS["LAMC"]),
        ("MATH261", CAMPUS_TENANTS["ELAC"]),
    ]

    assert run(service.get_prerequisites("MATH 261", "LAMC")) == [["MATH 260"]]
    assert run(service.get_prerequisites("MATH 261", "ELAC")) == [["MATH 270"]]
    assert service.cache_stats("LAMC")["course_entries"] == 1
    assert service.cache_stats("ELAC")["course_entries"] == 1


def test_elumen_negative_cache_avoids_repeated_misses_and_expires(monkeypatch):
    service = ElumenService()
    service.search_cache_ttl = 0
    service.negative_cache_ttl = 60
    calls = 0

    async def fake_search(*, query: str, tenant: str, page: int, page_size: int):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(service, "_search_courses_live", fake_search)

    assert run(service.get_course_details("MATH 999", "LAMC")) is None
    assert run(service.get_course_details("MATH 999", "LAMC")) is None
    assert calls == 1
    assert service.cache_stats("LAMC")["negative_course_entries"] == 1

    course_key = service._campus_cache_key("course", "LAMC", "MATH 999")
    miss_key = service._negative_key(course_key)
    service._cache_timestamps[miss_key] = time.time() - 61

    assert run(service.get_course_details("MATH 999", "LAMC")) is None
    assert calls == 2


def test_elumen_raw_search_cache_is_tenant_scoped_and_ttl_bound(monkeypatch):
    service = ElumenService()
    service.search_cache_ttl = 60
    calls: list[tuple[str, str]] = []

    async def fake_search(*, query: str, tenant: str, page: int, page_size: int):
        calls.append((query, tenant))
        return [{"code": "MATH 261", "title": tenant, "prerequisites": []}]

    monkeypatch.setattr(service, "_search_courses_live", fake_search)

    assert run(service.search_courses("MATH261", tenant=CAMPUS_TENANTS["LAMC"]))[0]["title"] == CAMPUS_TENANTS["LAMC"]
    assert run(service.search_courses("MATH261", tenant=CAMPUS_TENANTS["LAMC"]))[0]["title"] == CAMPUS_TENANTS["LAMC"]
    assert run(service.search_courses("MATH261", tenant=CAMPUS_TENANTS["ELAC"]))[0]["title"] == CAMPUS_TENANTS["ELAC"]
    assert calls == [
        ("MATH261", CAMPUS_TENANTS["LAMC"]),
        ("MATH261", CAMPUS_TENANTS["ELAC"]),
    ]

    search_key = service._search_cache_key(CAMPUS_TENANTS["LAMC"], "MATH261", 1, 100)
    service._cache_timestamps[search_key] = time.time() - 61

    assert run(service.search_courses("MATH261", tenant=CAMPUS_TENANTS["LAMC"]))[0]["title"] == CAMPUS_TENANTS["LAMC"]
    assert calls.count(("MATH261", CAMPUS_TENANTS["LAMC"])) == 2
