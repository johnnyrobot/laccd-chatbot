"""Academic-year fallback for ASSIST agreements.

ASSIST seeds an academic year's Id *before* publishing any articulation
agreements for it. An academic year begins in August, so from August onward
the date-derived "current" year points at a cycle whose agreements do not
exist yet, and ASSIST answers with HTTP 200 and an empty report list rather
than an error. Every campus->university transfer lookup then silently returns
nothing.

These tests are hermetic — ``_get`` is stubbed, so no network is touched and
they keep passing once ASSIST publishes the next cycle.
"""

from __future__ import annotations

import asyncio

from app.services.assist_articulation import AssistService


YEARS = [
    {"Id": 78, "FallYear": 2027, "id": 78, "fallYear": 2027},
    {"Id": 77, "FallYear": 2026, "id": 77, "fallYear": 2026},
    {"Id": 76, "FallYear": 2025, "id": 76, "fallYear": 2025},
    {"Id": 75, "FallYear": 2024, "id": 75, "fallYear": 2024},
]

LAMC_ID = 47
UCLA_ID = 117


def _majors(n: int) -> list[dict]:
    return [{"label": f"MAJOR {i}, B.S.", "key": f"key-{i}"} for i in range(n)]


def _run(coro):
    return asyncio.run(coro)


def _service_with(published: dict[int, list[dict]]) -> tuple[AssistService, list[int]]:
    """An AssistService whose ASSIST responses come from ``published``.

    ``published`` maps academicYearId -> the reports ASSIST would return.
    Years absent from the mapping return an empty list, which is exactly how
    ASSIST behaves for a seeded-but-unpublished cycle.
    """
    svc = AssistService()
    years_requested: list[int] = []

    async def fake_get(path: str, params: dict | None = None):
        if path == "/api/AcademicYears":
            return list(YEARS)
        if path == "/api/agreements":
            year_id = params["academicYearId"]
            years_requested.append(year_id)
            return {"reports": published.get(year_id, [])}
        if path == "/api/agreements/categories":
            year_id = params["academicYearId"]
            has = bool(published.get(year_id))
            return [{"label": "Major", "code": "major", "hasReports": has}]
        raise AssertionError(f"unexpected ASSIST path: {path}")

    svc._get = fake_get  # type: ignore[method-assign]
    return svc, years_requested


def test_unpublished_current_year_falls_back_to_newest_published_year():
    async def work():
        svc, requested = _service_with({76: _majors(12)})
        try:
            # 77 (FallYear 2026) is seeded but unpublished.
            reports = await svc.list_agreements(LAMC_ID, UCLA_ID, 77)
            assert len(reports) == 12, (
                "an unpublished academic year must fall back to the newest "
                f"published one, got {len(reports)} reports"
            )
            assert svc.last_agreement_year_id == 76, (
                "the service must report which academic year it actually used, "
                f"got {svc.last_agreement_year_id!r}"
            )
            assert requested[0] == 77, "the requested year must be tried first"
        finally:
            await svc.aclose()

    _run(work())


def test_published_current_year_is_used_without_fallback():
    async def work():
        svc, requested = _service_with({77: _majors(9), 76: _majors(12)})
        try:
            reports = await svc.list_agreements(LAMC_ID, UCLA_ID, 77)
            assert len(reports) == 9, "a published year must be used as-is"
            assert svc.last_agreement_year_id == 77
            assert requested == [77], (
                f"no earlier year should be consulted, tried {requested}"
            )
        finally:
            await svc.aclose()

    _run(work())


def test_genuinely_empty_pair_stops_after_bounded_lookback():
    """A pair with no agreements in any year must not walk the whole history."""

    async def work():
        svc, requested = _service_with({})
        try:
            reports = await svc.list_agreements(LAMC_ID, UCLA_ID, 78)
            assert reports == []
            # 78 plus a bounded number of earlier years — never all of history.
            assert len(requested) <= 4, f"unbounded lookback: tried {requested}"
        finally:
            await svc.aclose()

    _run(work())
