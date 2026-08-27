"""Integration tests for AssistService — these hit the live assist.org API.

Marked ``integration`` so CI can skip when assist.org is unreachable. Run
locally with::

    PYTHONPATH="$PWD:$PWD/backend" pytest tests/test_assist_articulation.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.assist_articulation import (
    CATEGORY_CCC,
    CATEGORY_UC,
    LACCD_TO_ASSIST_CODE,
    AssistService,
)


pytestmark = pytest.mark.integration


def _run(coro):
    """Run an async coroutine in a fresh event loop. Each test gets a fresh
    AssistService too — no cache state leaks between tests."""
    return asyncio.run(coro)


async def _with_service(work):
    svc = AssistService()
    try:
        return await work(svc)
    finally:
        await svc.aclose()


def test_bootstrap_token():
    async def work(svc: AssistService):
        await svc._ensure_client()
        assert svc._xsrf, "XSRF token should be populated after bootstrap"
        assert len(svc._xsrf) > 50, "Expected long ASP.NET CSRF blob"

    _run(_with_service(work))


def test_list_academic_years():
    async def work(svc: AssistService):
        years = await svc.list_academic_years()
        assert isinstance(years, list)
        assert len(years) > 5
        fall_years = [y["FallYear"] for y in years]
        assert 2025 in fall_years
        current = await svc.current_academic_year_id()
        assert isinstance(current, int)
        assert current > 70  # Id grows over time; 76 was 2025-26
        # Resolved year must have a non-future FallYear (today is 2026-05-04)
        resolved = next(y for y in years if y["Id"] == current)
        assert resolved["FallYear"] <= 2026

    _run(_with_service(work))


def test_find_lamc():
    async def work(svc: AssistService):
        lamc = await svc.find_laccd_institution("LAMC")
        assert lamc is not None, "LAMC must resolve via LACCD_TO_ASSIST_CODE"
        assert lamc["category"] == CATEGORY_CCC
        names = [n.get("name", "") for n in lamc.get("names", [])]
        assert any("Mission" in n for n in names)
        # Must NOT match "Mission College" (the unrelated school at id=32)
        assert lamc["id"] != 32

    _run(_with_service(work))


def test_find_all_laccd_campuses():
    async def work(svc: AssistService):
        for campus_code in LACCD_TO_ASSIST_CODE:
            inst = await svc.find_laccd_institution(campus_code)
            assert inst is not None, f"{campus_code} did not resolve"
            assert inst["category"] == CATEGORY_CCC, f"{campus_code} is not a CCC"

    _run(_with_service(work))


def test_find_uc_csu():
    async def work(svc: AssistService):
        ucla = await svc.find_institution("UCLA")
        assert ucla is not None
        assert ucla["category"] == CATEGORY_UC
        names = [n.get("name", "") for n in ucla.get("names", [])]
        assert any("University of California, Los Angeles" in n for n in names)

    _run(_with_service(work))


def test_list_agreements_lamc_to_ucla():
    async def work(svc: AssistService):
        lamc = await svc.find_laccd_institution("LAMC")
        ucla = await svc.find_institution("UCLA")
        year_id = await svc.current_academic_year_id()
        reports = await svc.list_agreements(lamc["id"], ucla["id"], year_id)
        assert isinstance(reports, list)
        assert len(reports) > 10, "LAMC→UCLA should have many major agreements"
        sample = reports[0]
        assert "label" in sample
        assert "key" in sample

    _run(_with_service(work))


def test_get_articulation_parses_inner_json():
    """receivingInstitution / sendingInstitution arrive as JSON-stringified
    inside the outer JSON. They must be re-parsed to dicts."""
    async def work(svc: AssistService):
        lamc = await svc.find_laccd_institution("LAMC")
        ucla = await svc.find_institution("UCLA")
        year_id = await svc.current_academic_year_id()
        reports = await svc.list_agreements(lamc["id"], ucla["id"], year_id)
        assert reports
        doc = await svc.get_articulation(reports[0]["key"])
        assert isinstance(doc, dict)
        if "receivingInstitution" in doc:
            assert isinstance(doc["receivingInstitution"], dict)
        if "sendingInstitution" in doc:
            assert isinstance(doc["sendingInstitution"], dict)

    _run(_with_service(work))


def test_cache_hit_avoids_network():
    async def work(svc: AssistService):
        years_1 = await svc.list_academic_years()
        original = svc._get
        calls = [0]

        async def counted(path, params=None):
            calls[0] += 1
            return await original(path, params)

        svc._get = counted  # type: ignore[method-assign]
        years_2 = await svc.list_academic_years()
        assert years_2 == years_1
        assert calls[0] == 0, "Second call must hit cache, not the network"

    _run(_with_service(work))


def test_unknown_campus_returns_none():
    async def work(svc: AssistService):
        result = await svc.find_laccd_institution("ZZZZ")
        assert result is None

    _run(_with_service(work))
