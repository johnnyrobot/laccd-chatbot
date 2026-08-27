"""ASSIST.org articulation lookup service.

Thin live wrapper around the (undocumented but public) assist.org REST API at
``https://assist.org/api/*``. The official ASSIST API requiring an ``API-Key``
header is still listed as "Future Release" in the V3 spec (8/20/24); this
service uses the website's own backend, which only requires a CSRF token
fetched in one round-trip.

Auth flow:
1. ``GET https://assist.org/`` once → server sets ``X-XSRF-TOKEN`` cookie
2. Every ``/api/*`` request sends ``X-XSRF-TOKEN: <cookie value>`` header
3. On 400 (token expired), refresh by re-hitting ``/``

Note: the cookie value to forward in the header lives in the cookie literally
named ``X-XSRF-TOKEN`` (non-HttpOnly), NOT in ``XSRF-TOKEN`` (HttpOnly). The
two are distinct values; using the wrong one returns 400.

Fallback to Neo4j is wired in elsewhere — this module is live-only. Major
articulation has no flat-file fallback (Future Release per ASSIST V3 spec);
institution lookup and transferable-course lookup do.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import time
from typing import Any

import httpx
from langfuse import observe

logger = logging.getLogger(__name__)

API_BASE = "https://assist.org"
API_TIMEOUT = 30.0
CACHE_TTL_SECONDS = 24 * 60 * 60

# How many earlier academic years to try when the requested year has no
# published agreements. ASSIST publishes a cycle within a year of seeding it,
# so 3 covers the real gap while keeping a genuinely empty pair cheap.
MAX_YEAR_LOOKBACK = 3
USER_AGENT = "laccd-chatbot/1.0 (educational, lamission.edu)"

# LACCD campus_code → ASSIST website API short code. The website API uses
# different codes than the ETS codes in neo4j_service.ASSIST_INSTITUTION_CODES.
LACCD_TO_ASSIST_CODE: dict[str, str] = {
    "LACC": "LACC",
    "LAHC": "LAHC",
    "LAPC": "LAPC",
    "LATTC": "LATT",
    "LAMC": "LAMC",
    "LASC": "LASC",
    "ELAC": "LAEC",
    "WLAC": "LAWC",
    "LAVC": "LAVC",
}

# ASSIST institution category enum (observed values; not all may be in spec).
CATEGORY_CSU = 0
CATEGORY_UC = 1
CATEGORY_CCC = 2
CATEGORY_INDEPENDENT = 5
MAX_CACHE_ENTRIES = 512
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024


class AssistAPIError(RuntimeError):
    """Raised when the live ASSIST API is unreachable or returns an error."""


class AssistService:
    """Live ASSIST.org articulation client.

    Single shared async client with a persistent cookie jar (XSRF cookie
    survives across calls). All public methods are cached in-process for
    24h. Caller-side fallback to Neo4j is wired separately.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._xsrf: str | None = None
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._cache_sizes: dict[str, int] = {}
        self._cache_bytes = 0
        self._sem = asyncio.Semaphore(4)
        self._auth_lock = asyncio.Lock()
        # The most recently used data tier per method name. Callers
        # (chat handler) read this to surface ``tool_payload.source``.
        # Values: "live", "neo4j_fallback", "cache", "live_year_fallback".
        self.last_source: dict[str, str] = {}
        # The academicYearId ``list_agreements`` actually served results from.
        # Differs from the requested year when the requested cycle is seeded
        # but unpublished, so callers can cite the correct catalog year.
        self.last_agreement_year_id: int | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=API_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        if not self._xsrf:
            await self._bootstrap_token()
        return self._client

    async def _bootstrap_token(self) -> None:
        async with self._auth_lock:
            if self._xsrf:
                return
            assert self._client is not None
            async with self._client.stream("GET", "/") as response:
                response.raise_for_status()
                consumed = 0
                async for chunk in response.aiter_bytes():
                    consumed += len(chunk)
                    if consumed > MAX_RESPONSE_BYTES:
                        raise AssistAPIError("ASSIST bootstrap response exceeds byte limit")
            # The header value lives in the *non-HttpOnly* cookie named
            # ``X-XSRF-TOKEN``. There's also an ``XSRF-TOKEN`` cookie, but
            # that's HttpOnly and a different value — sending it yields 400.
            token = self._client.cookies.get("X-XSRF-TOKEN")
            if not token:
                raise AssistAPIError("assist.org did not set X-XSRF-TOKEN cookie")
            self._xsrf = token

    def _auth_headers(self) -> dict[str, str]:
        """Auth headers for /api/* requests. Wraps the CSRF token so swapping
        to the official API-Key header later is a one-method change."""
        return {"X-XSRF-TOKEN": self._xsrf or ""}

    async def _get(self, path: str, params: dict | None = None) -> Any:
        client = await self._ensure_client()
        async with self._sem:
            for attempt in range(2):
                async with client.stream(
                    "GET", path, params=params, headers=self._auth_headers()
                ) as response:
                    if response.status_code == 400 and attempt == 0:
                        self._xsrf = None
                        await self._bootstrap_token()
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise AssistAPIError("ASSIST response exceeds byte limit")
                return json.loads(body)

    def _cache_get(self, key: str) -> Any | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        timestamp, value = hit
        if time.time() - timestamp > CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            self._cache_bytes -= self._cache_sizes.pop(key, 0)
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: str, value: Any) -> None:
        encoded_size = len(json.dumps(value, default=str).encode("utf-8"))
        if encoded_size > MAX_CACHE_BYTES:
            return
        self._cache_bytes -= self._cache_sizes.pop(key, 0)
        self._cache[key] = (time.time(), value)
        self._cache_sizes[key] = encoded_size
        self._cache_bytes += encoded_size
        self._cache.move_to_end(key)
        while len(self._cache) > MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
            evicted, _ = self._cache.popitem(last=False)
            self._cache_bytes -= self._cache_sizes.pop(evicted, 0)

    @observe(name="assist.list_academic_years")
    async def list_academic_years(self) -> list[dict]:
        cached = self._cache_get("years")
        if cached is not None:
            return cached
        data = await self._get("/api/AcademicYears")
        if isinstance(data, list):
            data = [self._normalize_academic_year(year) for year in data]
        self._cache_put("years", data)
        return data

    @staticmethod
    def _normalize_academic_year(year: Any) -> dict:
        """Preserve ASSIST's historical academic-year key casing.

        ASSIST has returned both ``Id``/``FallYear`` and ``id``/``fallYear``
        over time. Keep the legacy keys populated because callers and tests
        use them as the local service contract.
        """
        if not isinstance(year, dict):
            return {}
        normalized = dict(year)
        if "Id" not in normalized and "id" in normalized:
            normalized["Id"] = normalized["id"]
        if "FallYear" not in normalized and "fallYear" in normalized:
            normalized["FallYear"] = normalized["fallYear"]
        return normalized

    async def current_academic_year_id(self) -> int:
        """The Id for the academic year currently in progress.

        ASSIST seeds future-year Ids before publishing articulations for them
        (e.g., FallYear=2027 may exist with no reports). We want the most
        recent year that has *already started*: an academic year begins in
        August, so today 2026-05-04 falls inside the 2025-26 cycle (FallYear
        2025), not 2026-27. Callers who want literal max-FallYear can call
        ``list_academic_years()`` directly.
        """
        from datetime import datetime

        today = datetime.now()
        active_fall = today.year if today.month >= 8 else today.year - 1
        years = await self.list_academic_years()
        for year in sorted(years, key=lambda y: y.get("FallYear", 0), reverse=True):
            if year.get("FallYear", 0) <= active_fall:
                return year["Id"]
        # Fallback: max FallYear (shouldn't happen unless ASSIST is empty)
        return max(years, key=lambda y: y.get("FallYear", 0))["Id"]

    @observe(name="assist.list_institutions")
    async def list_institutions(self) -> list[dict]:
        cached = self._cache_get("institutions")
        if cached is not None:
            self.last_source["list_institutions"] = "cache"
            return cached
        try:
            data = await self._get("/api/institutions")
            self._cache_put("institutions", data)
            self.last_source["list_institutions"] = "live"
            return data
        except (httpx.HTTPError, AssistAPIError) as exc:
            logger.warning("ASSIST live API failed; using Neo4j fallback: %s", exc)
            data = await self._neo4j_list_institutions()
            self.last_source["list_institutions"] = "neo4j_fallback"
            return data

    async def _neo4j_list_institutions(self) -> list[dict]:
        """Fallback: query (:Institution) nodes loaded from insts.txt.

        Returns entries shaped to match the live API's contract closely
        enough that ``find_institution`` can iterate over both
        interchangeably. Live API ``id`` integers are unavailable from the
        flat file — fallback entries omit ``id`` and callers downstream
        (e.g. ``list_agreements``) will fail gracefully when the live API
        is also down.
        """
        from app.services.neo4j_service import neo4j_service

        rows = await neo4j_service.run_query(
            """
            MATCH (i:Institution)
            RETURN i.ets_code AS ets_code,
                   i.short_code AS short_code,
                   i.name AS name,
                   i.system AS system,
                   i.unit_type AS unit_type
            """
        )
        category_map = {"C": CATEGORY_CCC, "S": CATEGORY_CSU, "U": CATEGORY_UC, "I": CATEGORY_INDEPENDENT}
        return [
            {
                "id": None,  # not available from flat-file
                "ets_code": row["ets_code"],
                "code": (row["short_code"] or "").ljust(8),  # match live API trailing-space style
                "names": [{"name": row["name"] or ""}],
                "category": category_map.get(row["system"], None),
                "isCommunityCollege": row["system"] == "C",
            }
            for row in rows
        ]

    @observe(name="assist.find_institution")
    async def find_institution(self, query: str) -> dict | None:
        """Fuzzy-match an institution by short code or name.

        Strips trailing whitespace from the API's ``code`` field (e.g.
        ``'CSUMA   '``). Tries exact code match first to avoid name collisions
        like 'Mission College' vs 'Los Angeles Mission College'.
        """
        if not query:
            return None
        q = query.strip()
        q_lower = q.lower()
        institutions = await self.list_institutions()
        # 1) Exact match on stripped code (case-insensitive)
        for inst in institutions:
            code = (inst.get("code") or "").strip().lower()
            if code == q_lower:
                return inst
        # 2) Substring match on name
        for inst in institutions:
            for name_entry in inst.get("names", []):
                if q_lower in (name_entry.get("name") or "").lower():
                    return inst
        # 3) Substring match on code
        for inst in institutions:
            code = (inst.get("code") or "").strip().lower()
            if q_lower in code:
                return inst
        return None

    async def find_laccd_institution(self, campus_code: str) -> dict | None:
        """Resolve a LACCD campus_code to its ASSIST institution entry.

        Uses the hardcoded LACCD_TO_ASSIST_CODE mapping for stability — avoids
        accidentally matching 'Mission College' (a different school) when
        looking up LAMC.
        """
        assist_code = LACCD_TO_ASSIST_CODE.get(campus_code.upper())
        if not assist_code:
            return None
        return await self.find_institution(assist_code)

    @observe(name="assist.list_agreement_categories")
    async def list_agreement_categories(
        self, sending_id: int, receiving_id: int, year_id: int
    ) -> list[dict]:
        """Categories of agreement available for a sending/receiving pair
        (e.g. ``[{"code": "major", "label": "Majors", "hasReports": true}, ...]``)."""
        cache_key = f"cats:{sending_id}:{receiving_id}:{year_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(
            "/api/agreements/categories",
            params={
                "receivingInstitutionId": receiving_id,
                "sendingInstitutionId": sending_id,
                "academicYearId": year_id,
            },
        )
        if not isinstance(data, list):
            data = []
        self._cache_put(cache_key, data)
        return data

    @observe(name="assist.list_agreements")
    async def list_agreements(
        self,
        sending_id: int,
        receiving_id: int,
        year_id: int,
        category: str = "major",
        *,
        allow_year_fallback: bool = True,
    ) -> list[dict]:
        """Reports under a category for a sending/receiving pair.

        Each entry has ``label`` (e.g. 'COMPUTER SCIENCE, B.S.'), ``key``
        (used to fetch the full articulation), and ``ownerInstitutionId``.

        ASSIST seeds an academic year's Id before publishing its agreements,
        and answers with HTTP 200 + an empty report list for the unpublished
        cycle rather than an error. From August onward the date-derived
        "current" year is exactly that unpublished cycle, which silently
        emptied every transfer lookup. So an empty result walks back to the
        newest *published* year (bounded by ``MAX_YEAR_LOOKBACK``) and records
        the year actually used in ``last_agreement_year_id``. Pass
        ``allow_year_fallback=False`` for a strict single-year query.

        No flat-file fallback exists for major-level articulation (per
        ASSIST V3 spec, ``File #7: CSU and UC Courses Referenced in
        Articulation Agreements`` is still Future Release). Live-only;
        ``AssistAPIError`` propagates to the caller on outage.
        """
        cache_key = f"agree:{sending_id}:{receiving_id}:{year_id}:{category}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.last_source["list_agreements"] = "cache"
            self.last_agreement_year_id = year_id
            if cached or not allow_year_fallback:
                return cached
            return await self._agreements_from_earlier_year(
                sending_id, receiving_id, year_id, category
            )
        try:
            data = await self._get(
                "/api/agreements",
                params={
                    "receivingInstitutionId": receiving_id,
                    "sendingInstitutionId": sending_id,
                    "academicYearId": year_id,
                    "categoryCode": category,
                },
            )
        except httpx.HTTPError as exc:
            self.last_source["list_agreements"] = "unavailable"
            raise AssistAPIError(
                "ASSIST live API unavailable; major-level articulation has no fallback"
            ) from exc
        reports = data.get("reports", []) if isinstance(data, dict) else []
        self._cache_put(cache_key, reports)
        self.last_source["list_agreements"] = "live"
        self.last_agreement_year_id = year_id
        if reports or not allow_year_fallback:
            return reports
        return await self._agreements_from_earlier_year(
            sending_id, receiving_id, year_id, category
        )

    async def _agreements_from_earlier_year(
        self, sending_id: int, receiving_id: int, year_id: int, category: str
    ) -> list[dict]:
        """Newest published agreements at or before ``year_id``, or ``[]``.

        Bounded so a pair that genuinely has no agreements in any year costs a
        handful of requests instead of walking ASSIST's history back to 1950.
        """
        years = await self.list_academic_years()
        requested = next(
            (y for y in years if y.get("Id") == year_id), None
        )
        if requested is None:
            return []
        requested_fall = requested.get("FallYear", 0)
        earlier = sorted(
            (y for y in years if y.get("FallYear", 0) < requested_fall),
            key=lambda y: y.get("FallYear", 0),
            reverse=True,
        )[:MAX_YEAR_LOOKBACK]
        for year in earlier:
            reports = await self.list_agreements(
                sending_id, receiving_id, year["Id"], category,
                allow_year_fallback=False,
            )
            if reports:
                logger.warning(
                    "ASSIST academic year %s (FallYear %s) has no published %s "
                    "agreements for %s->%s; serving FallYear %s instead",
                    year_id, requested_fall, category, sending_id, receiving_id,
                    year.get("FallYear"),
                )
                self.last_source["list_agreements"] = "live_year_fallback"
                self.last_agreement_year_id = year["Id"]
                return reports
        return []

    @observe(name="assist.get_articulation")
    async def get_articulation(self, agreement_key: str) -> dict:
        """Full articulation document for one agreement key.

        Re-parses the ``receivingInstitution`` and ``sendingInstitution``
        fields, which arrive as JSON-stringified inside the outer JSON.

        Live-only (no flat-file fallback exists; see ``list_agreements``).
        """
        cache_key = f"art:{agreement_key}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.last_source["get_articulation"] = "cache"
            return cached
        try:
            raw = await self._get("/api/articulation/Agreements", params={"Key": agreement_key})
        except httpx.HTTPError as exc:
            self.last_source["get_articulation"] = "unavailable"
            raise AssistAPIError(
                "ASSIST live API unavailable; articulation document has no fallback"
            ) from exc
        result = raw.get("result", raw) if isinstance(raw, dict) else raw
        if isinstance(result, dict):
            for field in ("receivingInstitution", "sendingInstitution"):
                value = result.get(field)
                if isinstance(value, str):
                    try:
                        result[field] = json.loads(value)
                    except json.JSONDecodeError:
                        logger.warning(
                            "assist: could not re-parse %s in %s", field, agreement_key
                        )
        self._cache_put(cache_key, result)
        self.last_source["get_articulation"] = "live"
        return result

    @observe(name="assist.list_transferable_courses")
    async def list_transferable_courses(
        self, institution_id: int, year_id: int, area_key: str = "CSUTransferable"
    ) -> list[dict]:
        """Courses at a CCC that satisfy a CSU/UC transferability area.

        On live-API failure falls back to the existing Course nodes in
        Neo4j (loaded by ``scripts/load_assist_data.py``), which carry
        ``csu_transferable`` / ``uc_transferable`` flags. Only the
        ``CSUTransferable`` and ``UCTransferable`` area keys have flat-file
        equivalents; other areas raise ``AssistAPIError`` on outage.
        """
        cache_key = f"transfer:{institution_id}:{year_id}:{area_key}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.last_source["list_transferable_courses"] = "cache"
            return cached
        try:
            data = await self._get(
                "/api/transferability/courses",
                params={
                    "institutionId": institution_id,
                    "academicYearId": year_id,
                    "areaTypeKey": area_key,
                },
            )
            self._cache_put(cache_key, data)
            self.last_source["list_transferable_courses"] = "live"
            return data
        except httpx.HTTPError as exc:
            logger.warning("ASSIST transferability live failed; trying fallback: %s", exc)
            data = await self._neo4j_list_transferable_courses(institution_id, area_key)
            if data is None:
                self.last_source["list_transferable_courses"] = "unavailable"
                raise AssistAPIError(
                    f"No flat-file fallback for area_key={area_key}"
                ) from exc
            self.last_source["list_transferable_courses"] = "neo4j_fallback"
            return data

    async def _neo4j_list_transferable_courses(
        self, institution_id: int, area_key: str
    ) -> list[dict] | None:
        """Fallback via existing csu_transferable/uc_transferable Course flags."""
        from app.services.neo4j_service import neo4j_service

        flag_field = {
            "CSUTransferable": "csu_transferable",
            "UCTransferable": "uc_transferable",
        }.get(area_key)
        if not flag_field:
            return None
        # Resolve institution_id → campus_code via Institution.short_code
        # → LACCD_TO_ASSIST_CODE inverse. Only LACCD campuses are in our graph.
        institutions = await self._neo4j_list_institutions()
        match = next((i for i in institutions if i.get("ets_code")), None)
        # Best-effort: look up the institution by its live id from the cached
        # live institution list; if the live cache is empty we can't map id → campus.
        live_institutions = self._cache_get("institutions") or []
        live_match = next(
            (i for i in live_institutions if i.get("id") == institution_id), None
        )
        if not live_match:
            return None
        short_code = (live_match.get("code") or "").strip()
        campus_code = next(
            (k for k, v in LACCD_TO_ASSIST_CODE.items() if v == short_code), None
        )
        if not campus_code:
            return None
        rows = await neo4j_service.run_query(
            f"""
            MATCH (c:Course {{campus_code: $campus_code}})
            WHERE c.{flag_field} = true AND c.is_current = true
            RETURN c.code AS code, c.title AS title, c.units_min AS units_min,
                   c.units_max AS units_max
            ORDER BY c.code
            """,
            {"campus_code": campus_code},
        )
        return [dict(row) for row in rows]

    async def aclose(self) -> None:
        """Close the underlying httpx client (for tests/teardown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._xsrf = None


_singleton: AssistService | None = None


def get_assist_service() -> AssistService:
    """Module-level singleton. The httpx client + XSRF cookie persist across
    calls within the process — same pattern as program_mapper's per-campus
    clients."""
    global _singleton
    if _singleton is None:
        _singleton = AssistService()
    return _singleton
