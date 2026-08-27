"""REST tool endpoints consumed by the mobile app's on-device tool router.

The mobile app runs Gemma 4 E4B and on-device CozoDB locally; for
data the user can't host themselves (live class schedule, live ASSIST articulation,
live eLumen catalog of record), it falls back to these endpoints. The tier ordering is:

  1. App's 24h tool-result cache.
  2. On-device CozoDB (KG / RAG / ASSIST snapshot / programs snapshot).
  3. **These endpoints** for data that's only accurate when fetched live.
  4. Combined into a Gemma ``[CONTEXT]`` block.

Auth uses bearer tokens whose SHA-256 hashes are configured server-side in
``MOBILE_TOOL_TOKEN_HASHES``. Per-client rate limiting is a sliding-window token
bucket — generous enough for an active student chat session (default 60
req/min/client), tight enough to thwart abusive scraping. Daily-budget limits don't
apply: these endpoints don't spend Gemini quota.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from app.config import settings
from app.security import client_identity_from_request, is_allowed_bearer_token
from app.services.assist_articulation import AssistAPIError, get_assist_service
from app.services.course_sequence_service import get_course_sequence_service
from app.services.elumen_service import get_elumen_service
from app.services.live_schedule import search_live_schedule
from app.services.program_mapper import (
    get_program_mapper_client,
    search_programs_all_campuses,
)

logger = logging.getLogger(__name__)


# ── Auth dep ────────────────────────────────────────────────────────────────


async def require_mobile_tool_token(
    authorization: str | None = Header(default=None),
) -> None:
    """Reject requests without a valid mobile tool bearer token.

    When ``MOBILE_TOOL_TOKEN_HASHES`` is unset in the environment, returns 503:
    the endpoint surface is treated as not configured rather than open.
    """
    if not settings.mobile_tool_token_hashes:
        raise HTTPException(status_code=503, detail="Mobile tool API not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    if not is_allowed_bearer_token(token.strip(), settings.mobile_tool_token_hashes):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


# ── Per-client rate limiter ─────────────────────────────────────────────────


class IpRateLimiter:
    """Sliding-window token bucket keyed on trusted client identity.

    Loses state on restart by design (these endpoints are best-effort + idempotent;
    a flush every container restart isn't a security issue). For multi-replica
    deployments swap to Redis later — keep the call sites the same.

    Distinct-key cardinality is bounded (``max_clients``) so a flood of unique
    client identities can't grow ``_buckets`` without limit and exhaust memory.
    When a new key would exceed the cap we first drop fully-expired buckets, then
    (if still full) evict the least-recently-active client.
    """

    def __init__(
        self, max_requests: int, window_seconds: float, *, max_clients: int = 20_000
    ) -> None:
        self.max = max_requests
        self.window = window_seconds
        self.max_clients = max(1, max_clients)
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = Lock()

    def _evict_locked(self, now: float) -> None:
        """Bound ``_buckets`` cardinality. Caller must hold ``self._lock``."""
        # OrderedDict is maintained least- to most-recently accessed, so fully
        # expired buckets and capacity eviction require no full-map scan.
        while self._buckets:
            _, oldest_bucket = next(iter(self._buckets.items()))
            if oldest_bucket and now - oldest_bucket[-1] <= self.window:
                break
            self._buckets.popitem(last=False)
        while len(self._buckets) >= self.max_clients:
            self._buckets.popitem(last=False)

    def check(self, ip: str) -> bool:
        """True if the request is within budget; False if over-limit."""
        now = monotonic()
        with self._lock:
            if ip not in self._buckets and len(self._buckets) >= self.max_clients:
                self._evict_locked(now)
            bucket = self._buckets.setdefault(ip, deque())
            self._buckets.move_to_end(ip)
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            if len(bucket) >= self.max:
                return False
            bucket.append(now)
            return True

    def remaining(self, ip: str) -> int:
        """Used by tests + the /api/tools/health endpoint to surface bucket state."""
        now = monotonic()
        with self._lock:
            bucket = self._buckets.get(ip, deque())
            if ip in self._buckets:
                self._buckets.move_to_end(ip)
            while bucket and now - bucket[0] > self.window:
                bucket.popleft()
            return max(0, self.max - len(bucket))


_ip_rate_limiter = IpRateLimiter(
    max_requests=settings.mobile_tool_rate_per_minute,
    window_seconds=60.0,
)


def _client_ip(request: Request) -> str:
    """Resolve caller identity, honoring forwarded headers only from trusted peers."""
    return client_identity_from_request(request)


async def per_ip_rate_limit(request: Request) -> None:
    ip = _client_ip(request)
    if not _ip_rate_limiter.check(ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({settings.mobile_tool_rate_per_minute} req/min per client)",
        )


# ── Router ──────────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/api/tools",
    tags=["mobile-tools"],
    dependencies=[Depends(require_mobile_tool_token), Depends(per_ip_rate_limit)],
)


@router.get("/schedule")
async def tools_schedule(
    campus: Annotated[str, Query(min_length=2, max_length=5, pattern=r"^[A-Za-z]+$")],
    term: Annotated[str, Query(min_length=4, max_length=12, pattern=r"^[A-Za-z0-9-]+$")],
    subject: Annotated[str | None, Query(max_length=20)] = None,
    course_number: Annotated[str | None, Query(max_length=12)] = None,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
) -> dict:
    """Live class schedule for one campus + term.

    Wraps ``live_schedule.search_live_schedule``; that function has its own 1h
    in-process cache, so this endpoint is implicitly cache-respecting.
    """
    if not campus or not term:
        raise HTTPException(status_code=422, detail="campus and term are required")
    try:
        return await search_live_schedule(
            campus=campus.upper(),
            term=term,
            subject=subject,
            course_number=course_number,
            keyword=keyword,
        )
    except Exception as exc:
        logger.warning("live schedule failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Schedule upstream failed: {exc}") from exc


@router.get("/assist")
async def tools_assist(
    course: Annotated[str, Query(min_length=1, max_length=32)],
    from_institution: Annotated[str, Query(alias="from", max_length=100)] = "",
    to: Annotated[str, Query(max_length=100)] = "",
) -> dict:
    """ASSIST articulation lookup.

    The public query contract is ``?course=&from=&to=``. ``from``
    is a Python keyword, so the parameter is aliased via ``Query(alias="from")``.

    - ``course``: source course code in the form "SUBJ NUM" (e.g., "MATH 261").
    - ``from``: source institution. Either a LACCD campus code (LAMC, ELAC…) or
      free text resolved via ``find_institution``.
    - ``to``: destination institution. UC/CSU short code or free text.

    Returns the underlying transferability list, filtered to entries that mention
    the source course code where possible. The mobile app's tool router does
    additional client-side filtering on the returned ``matches`` list.
    """
    if not course:
        raise HTTPException(status_code=422, detail="course is required")
    src = (from_institution or "").strip()
    dst = (to or "").strip()
    if not src or not dst:
        raise HTTPException(status_code=422, detail="from and to are required")

    service = get_assist_service()
    try:
        year_id = await service.current_academic_year_id()

        # Resolve source institution. LACCD short codes get a dedicated path
        # (the live API uses different ids for them than free-text find_institution).
        src_institution = (
            await service.find_laccd_institution(src.upper())
            if len(src) <= 5
            else await service.find_institution(src)
        )
        dst_institution = await service.find_institution(dst)

        if not src_institution:
            raise HTTPException(status_code=404, detail=f"Unknown source institution: {src}")
        if not dst_institution:
            raise HTTPException(status_code=404, detail=f"Unknown destination institution: {dst}")

        # Determine the right area key for the destination institution. Public-UC
        # destinations get "UCTransferable"; CSU destinations get "CSUTransferable".
        dst_type = (dst_institution.get("type") or "").upper()
        area_key = "UCTransferable" if "UC" in dst_type or "UNIVERSITY OF CALIFORNIA" in dst_type else "CSUTransferable"

        rows = await service.list_transferable_courses(
            institution_id=src_institution["id"],
            year_id=year_id,
            area_key=area_key,
        )

        # Best-effort filter by source course code.
        norm = course.upper().replace("  ", " ").strip()
        filtered = [
            row for row in rows
            if norm in (row.get("code") or "").upper()
            or norm in (row.get("courseCode") or "").upper()
        ]

        return {
            "course": course,
            "from": src,
            "to": dst,
            "area_key": area_key,
            "year_id": year_id,
            "matches": filtered,
            "all_transferable_count": len(rows),
            "filtered_count": len(filtered),
            "source": service.last_source.get("list_transferable_courses", "live"),
        }
    except AssistAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ASSIST upstream failed: {exc}") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("ASSIST endpoint failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"ASSIST lookup failed: {exc}") from exc


@router.get("/programs")
async def tools_programs(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    campus: Annotated[str, Query(max_length=5, pattern=r"^[A-Za-z]*$")] = "",
) -> dict:
    """Program Mapper search.

    When ``campus`` is set, search just that campus. When empty, search all 9
    LACCD campuses. The mobile app routes to one campus most of the time; the
    cross-campus path is for the "where else can I take this program" query.
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="q (search query) is required")

    if campus:
        client = get_program_mapper_client(campus.upper())
        programs = await client.search_programs(q)
        for program in programs:
            program["campus_code"] = campus.upper()
            program["campus_name"] = client.campus_name
        return {
            "query": q,
            "campus": campus.upper(),
            "programs": programs,
            "count": len(programs),
        }

    programs = await search_programs_all_campuses(q)
    return {
        "query": q,
        "campus": None,
        "programs": programs,
        "count": len(programs),
    }


@router.get("/sequence")
async def tools_sequence(
    program_id: Annotated[str, Query(min_length=1, max_length=128)],
    campus: Annotated[str, Query(min_length=2, max_length=5, pattern=r"^[A-Za-z]+$")] = "LAMC",
    pace: Annotated[str, Query(pattern=r"^(full|momentum|part_time)$")] = "full",
) -> dict:
    """Course sequence for a program at a campus, paced by ``full|momentum|part_time``."""
    if not program_id:
        raise HTTPException(status_code=422, detail="program_id is required")
    service = get_course_sequence_service(campus.upper())
    try:
        return await service.generate_sequence(
            program_id=program_id,
            campus_code=campus.upper(),
            pace=pace,
        )
    except Exception as exc:
        logger.warning("sequence generation failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Sequence generation failed: {exc}") from exc


@router.get("/elumen")
async def tools_elumen(
    subject: Annotated[str, Query(min_length=1, max_length=20)],
    number: Annotated[str, Query(min_length=1, max_length=12)],
    campus: Annotated[str, Query(min_length=2, max_length=5, pattern=r"^[A-Za-z]+$")] = "LAMC",
) -> dict:
    """eLumen catalog of record lookup for a single course."""
    if not subject or not number:
        raise HTTPException(status_code=422, detail="subject and number are required")
    course_code = f"{subject.upper()} {number}".strip()
    service = get_elumen_service()
    try:
        details = await service.get_course_details(
            course_code=course_code, campus_code=campus.upper()
        )
        if details is None:
            raise HTTPException(
                status_code=404,
                detail=f"No eLumen record for {course_code} at {campus.upper()}",
            )
        return {
            "subject": subject.upper(),
            "number": number,
            "campus": campus.upper(),
            "course": details,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("eLumen lookup failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"eLumen lookup failed: {exc}") from exc
