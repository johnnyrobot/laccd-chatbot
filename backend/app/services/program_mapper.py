from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List

import httpx
from langfuse import get_client, observe

from app.config import settings
from app.services.neo4j_service import neo4j_service

logger = logging.getLogger(__name__)

API_BASE = "https://b.api.programmapper.com"
API_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PROGRAM_GROUPS = 64
MAX_PROGRAMS = 2_000
MAX_MATCHES = 50
MAX_CACHE_BYTES = 16 * 1024 * 1024
_PROVIDER_SEMAPHORE = asyncio.Semaphore(6)
# The vendor rejects non-browser clients with a bare 403 (no body, no hint) —
# httpx's own User-Agent fails every request. Verified with curl: 403 without
# this header, 200 with it, on every campus origin.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_CAMPUS = "LAMC"
COLLEGE_CONFIGS = {
    "LAMC": {
        "name": "Los Angeles Mission College",
        "origin": "https://la-mission.programmapper.ws",
        "site_content_id": "0055f609-1a83-4937-8356-c67ec89cb496",
    },
    "LAVC": {
        "name": "Los Angeles Valley College",
        "origin": "https://programmap.lavc.edu",
        "site_content_id": "b42b1741-63ac-4bcf-95b6-48288af8733d",
    },
    "LAPC": {
        "name": "Los Angeles Pierce College",
        "origin": "https://programmapper.piercecollege.edu",
        "site_content_id": "a10412a2-4b0f-493e-a7d0-2d8c4b1af0e2",
    },
    "LAHC": {
        "name": "Los Angeles Harbor College",
        "origin": "https://la-harbor.programmapper.com",
        "site_content_id": "170b2c8d-6880-48fe-aea2-d2017ffabe27",
    },
    "LATTC": {
        "name": "Los Angeles Trade-Tech College",
        "origin": "https://la-trade-tech.programmapper.ws",
        "site_content_id": "3973c13e-2554-42a2-aede-02f223d887d0",
    },
    "LACC": {
        "name": "Los Angeles City College",
        "origin": "https://la-city.programmapper.ws",
        "site_content_id": "82f8d72b-b23d-4f3b-8c4e-efc491c536ff",
    },
    "ELAC": {
        "name": "East Los Angeles College",
        "origin": "https://east-la.programmapper.com",
        "site_content_id": "679f91e9-a94b-45f3-b0d5-4bae183a3f91",
    },
    "LASC": {
        "name": "Los Angeles Southwest College",
        "origin": "https://la-southwest.programmapper.ws",
        "site_content_id": "c412a3e5-ac95-4de6-9def-f17f44deedfc",
    },
    "WLAC": {
        "name": "West Los Angeles College",
        "origin": "https://west-la.programmapper.ws",
        "site_content_id": "b72f9ee4-f902-4c14-9088-f4298008f569",
    },
}

_CACHE_MISS = object()


def build_request_headers(campus_code: str) -> dict[str, str]:
    """Headers every Program Mapper request must carry, for any caller.

    This is the single source of truth on purpose: the seed pipeline
    (``scripts/sync_programs_to_neo4j.py``) previously hand-rolled its own
    header dicts, so adding the User-Agent here did not reach it and the
    career half of the graph stayed empty. New callers import this, they do
    not rebuild it.
    """
    config = COLLEGE_CONFIGS[campus_code]
    return {
        "Origin": config["origin"],
        "Accept": "application/json",
        "Referer": f"{config['origin']}/",
        "User-Agent": BROWSER_USER_AGENT,
    }


class ProgramMapperUnavailable(RuntimeError):
    """Raised instead of re-attempting a call this machine cannot complete.

    Callers already wrap Program Mapper in ``try/except`` and fall back to
    Neo4j, so this deliberately subclasses nothing they special-case: the
    fallback path is identical, it just starts ~100ms sooner.
    """


class _EgressBreaker:
    """Stops calling a host that is refusing this machine outright.

    Some hosting networks are refused by the vendor before the application is
    reached, while the same request succeeds elsewhere. Only successful
    responses are cached, so repeated failures would otherwise add latency
    before the Neo4j fallback.

    The block is by IP, so this is deliberately **process-wide** rather than
    per-campus: one campus proving the host unreachable proves it for all nine.

    Recovery is automatic. After the cooldown the next call is allowed through
    as a probe; if the vendor allows the host it succeeds and normal service
    resumes without a restart. Only failures that mean "this machine cannot reach
    the vendor" trip it — a 404 for one missing record must not.
    """

    def __init__(self) -> None:
        self._opened_at: datetime | None = None
        self._reason: str = ""

    @property
    def _cooldown(self) -> timedelta:
        return timedelta(seconds=settings.program_mapper_breaker_cooldown_seconds)

    def is_open(self) -> bool:
        if self._opened_at is None or self._cooldown <= timedelta(0):
            return False
        if datetime.now() - self._opened_at >= self._cooldown:
            # Cooldown elapsed: let the next call through as a probe.
            self._opened_at = None
            logger.info("Program Mapper breaker cooling down; probing the host again")
            return False
        return True

    @property
    def reason(self) -> str:
        return self._reason

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("Program Mapper reachable again; breaker closed")
        self._opened_at = None
        self._reason = ""

    def record_failure(self, exc: Exception) -> None:
        if not _is_egress_failure(exc):
            return
        if self._opened_at is None:
            logger.warning(
                "Program Mapper refused this host (%s); skipping calls for %ss "
                "and answering from Neo4j instead",
                exc.__class__.__name__,
                settings.program_mapper_breaker_cooldown_seconds,
            )
        self._opened_at = datetime.now()
        self._reason = f"{exc.__class__.__name__}: {exc}"

    def reset(self) -> None:
        self._opened_at = None
        self._reason = ""


def _is_egress_failure(exc: Exception) -> bool:
    """Whether an error means the host is unreachable, not that a record is."""
    if isinstance(exc, httpx.HTTPStatusError):
        # 403 is the WAF refusing this IP. A 404 is a missing record and says
        # nothing about reachability, so it must not trip the breaker.
        return exc.response.status_code in (401, 403, 429)
    return isinstance(exc, (httpx.TransportError, asyncio.TimeoutError))


_breaker = _EgressBreaker()


def egress_breaker() -> _EgressBreaker:
    """The process-wide breaker, exposed for tests and health reporting."""
    return _breaker


def _is_unreachable(exc: Exception) -> bool:
    """Whether the vendor could not be reached, as opposed to said no.

    Only these failures may be answered from the local snapshot. A 404 or a 500
    is the vendor speaking about a record, and substituting seeded data for it
    would hide a real answer behind a stale one.
    """
    return isinstance(exc, ProgramMapperUnavailable) or _is_egress_failure(exc)


# Marks any payload assembled from the seeded graph rather than the live API.
# Nothing surfaced the 403 for months; an answer that silently came from last
# Sunday's snapshot must at least say so on the payload it returns.
GRAPH_SOURCE = "neo4j_snapshot"


def _match_programs(query: str, programs: List[dict]) -> List[dict]:
    """Select the programs a query names, deduplicated by master record.

    Shared by the live and snapshot paths on purpose. The same program list is
    matched the same way whichever side it came from, so a student cannot get
    different results from the two — a second copy of this logic is how the
    Program Mapper header handling drifted into three dicts and one silent bug.
    """
    query_lower = query.strip().lower()
    if not query_lower:
        return []
    query_compact = re.sub(r"[^a-z0-9]+", "", query_lower)
    query_terms = {term for term in re.findall(r"[a-z0-9]+", query_lower) if len(term) > 2}
    matches = []
    for program in programs:
        title = (program.get("title") or "").lower()
        award = (program.get("awardShortTitle") or "").lower()
        title_compact = re.sub(r"[^a-z0-9]+", "", title)
        award_compact = re.sub(r"[^a-z0-9]+", "", award)
        text_terms = set(re.findall(r"[a-z0-9]+", f"{title} {award}"))
        if (
            query_lower in title
            or query_lower in award
            or query_compact and (query_compact in title_compact or query_compact in award_compact)
            or (query_terms and query_terms.issubset(text_terms))
        ):
            matches.append(program)
    # Deduplicate by masterRecordId (same program can appear in multiple groups)
    seen: set[str] = set()
    unique: List[dict] = []
    for match in matches:
        rid = match.get("masterRecordId", "")
        if rid not in seen:
            seen.add(rid)
            unique.append(match)
    return unique[:MAX_MATCHES]


def _program_from_graph(row: dict) -> dict:
    """Translate a Program node into the vendor's JSON shape.

    Callers, formatters, and the mobile and voice surfaces are all written
    against the vendor's camelCase keys, so the translation happens here rather
    than asking every one of them to learn a second shape.
    """
    return {
        "masterRecordId": row.get("master_record_id") or "",
        "title": row.get("title") or "",
        "awardShortTitle": row.get("award_type") or "",
        "description": row.get("description") or "",
        "catalogUrl": row.get("catalog_url") or "",
        "programMapId": row.get("program_map_id") or "",
        "group_title": row.get("group_title") or "",
        "campus_code": row.get("campus_code") or "",
        "source": GRAPH_SOURCE,
    }


def _career_from_graph(row: dict) -> dict:
    return {
        "title": row.get("title") or "",
        "averageSalaryK": row.get("avg_salary"),
        "standardOccupationalCode": row.get("soc_code") or "",
    }


def _pathway_elements_from_graph(raw: Any) -> List[dict]:
    """Seeded ``pathwayElements`` come back as the JSON they were stored as."""
    if not raw:
        return []
    try:
        elements = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Program snapshot has unreadable pathway_elements; treating as empty")
        return []
    return elements if isinstance(elements, list) else []


class ProgramMapperClient:
    CACHE_TTL = timedelta(seconds=settings.program_mapper_cache_ttl_seconds)
    MAX_CACHE_ENTRIES = 256

    def __init__(self, campus_code: str = DEFAULT_CAMPUS) -> None:
        config = COLLEGE_CONFIGS[campus_code]
        self.campus_code = campus_code
        self.site_content_id = config["site_content_id"]
        self.campus_name = config["name"]
        self.headers = build_request_headers(campus_code)
        self.cache_ttl = timedelta(seconds=settings.program_mapper_cache_ttl_seconds)
        self._cache: OrderedDict[str, tuple[Any, datetime]] = OrderedDict()
        self._cache_sizes: dict[str, int] = {}
        self._cache_bytes = 0

    def _cache_key(self, namespace: str, *parts: object) -> str:
        normalized = [str(part).strip().lower() for part in parts if part is not None]
        return ":".join([self.campus_code, namespace, *normalized])

    def _get_cached(self, key: str) -> Any:
        cached = self._cache.get(key)
        if not cached:
            return _CACHE_MISS
        value, cached_at = cached
        if datetime.now() - cached_at >= self.cache_ttl:
            self._cache.pop(key, None)
            self._cache_bytes -= self._cache_sizes.pop(key, 0)
            return _CACHE_MISS
        self._cache.move_to_end(key)
        return deepcopy(value)

    def _set_cached(self, key: str, value: Any) -> Any:
        now = datetime.now()
        for existing_key, (_, cached_at) in list(self._cache.items()):
            if now - cached_at >= self.cache_ttl:
                self._cache.pop(existing_key, None)
                self._cache_bytes -= self._cache_sizes.pop(existing_key, 0)
        encoded_size = len(json.dumps(value, default=str).encode("utf-8"))
        if encoded_size > MAX_CACHE_BYTES:
            return deepcopy(value)
        self._cache_bytes -= self._cache_sizes.pop(key, 0)
        self._cache[key] = (deepcopy(value), now)
        self._cache_sizes[key] = encoded_size
        self._cache_bytes += encoded_size
        self._cache.move_to_end(key)
        while len(self._cache) > self.MAX_CACHE_ENTRIES or self._cache_bytes > MAX_CACHE_BYTES:
            evicted, _ = self._cache.popitem(last=False)
            self._cache_bytes -= self._cache_sizes.pop(evicted, 0)
        return deepcopy(value)

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        return re.sub(r"\s+", " ", query.strip().lower())

    def cache_stats(self) -> dict:
        return {
            "campus": self.campus_code,
            "entries": len(self._cache),
            "ttl_seconds": round(self.cache_ttl.total_seconds()),
            "keys": sorted(self._cache),
        }

    def _check_breaker(self, path: str) -> None:
        if _breaker.is_open():
            raise ProgramMapperUnavailable(
                f"skipping {path}: host refused this machine ({_breaker.reason})"
            )

    async def _request_json(self, method: str, path: str, *, payload: dict | None = None) -> dict:
        self._check_breaker(path)
        try:
            async with _PROVIDER_SEMAPHORE:
                async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
                    async with client.stream(
                        method,
                        f"{API_BASE}{path}",
                        headers=self.headers,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > MAX_RESPONSE_BYTES:
                                raise ProgramMapperUnavailable("Program Mapper response exceeds byte limit")
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ProgramMapperUnavailable("Program Mapper response must be an object")
        except Exception as exc:
            _breaker.record_failure(exc)
            raise
        _breaker.record_success()
        return data

    async def _get(self, path: str) -> dict:
        return await self._request_json("GET", path)

    async def _post(self, path: str, payload: dict) -> dict:
        return await self._request_json("POST", path, payload=payload)

    @observe(name="program.get_groups")
    async def get_program_groups(self) -> list[dict]:
        key = self._cache_key("groups")
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            cached_at = self._cache[key][1]
            get_client().update_current_span(
                metadata={"cache": "hit", "cache_age_seconds": round((datetime.now() - cached_at).total_seconds())},
            )
            return cached
        data = await self._get(f"/site-contents/{self.site_content_id}/home-page-content")
        groups = data.get("programGroups", [])
        if not isinstance(groups, list):
            groups = []
        groups = [group for group in groups[:MAX_PROGRAM_GROUPS] if isinstance(group, dict)]
        get_client().update_current_span(metadata={"cache": "miss"})
        return self._set_cached(key, groups)

    async def get_programs_in_group(self, group_id: str) -> list[dict]:
        key = self._cache_key("programs_in_group", group_id)
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            return cached
        data = await self._get(f"/site-contents/{self.site_content_id}/program-groups/{group_id}")
        programs = data.get("programs", [])
        if not isinstance(programs, list):
            programs = []
        return self._set_cached(key, [program for program in programs[:MAX_PROGRAMS] if isinstance(program, dict)])

    async def get_all_programs(self) -> list[dict]:
        key = self._cache_key("all_programs")
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            return cached

        groups = await self.get_program_groups()

        async def collect(group: dict) -> list[dict]:
            group_id = str(group.get("masterRecordId") or "")[:128]
            if not group_id:
                return []
            programs = await self.get_programs_in_group(group_id)
            for program in programs:
                program["group_title"] = group.get("title")
            return programs

        programs: list[dict] = []
        for group in groups:
            try:
                result = await collect(group)
            except Exception as exc:
                logger.warning("Program group fetch failed: %s", type(exc).__name__)
                continue
            programs.extend(result[: max(0, MAX_PROGRAMS - len(programs))])
            if len(programs) >= MAX_PROGRAMS:
                break
        return self._set_cached(key, programs)

    async def search_programs(self, query: str) -> list[dict]:
        query = self._normalize_search_query(query)[:200]
        if not query:
            raise ValueError("program search query must not be blank")

        try:
            programs = await self.get_all_programs()
        except Exception as exc:
            if not _is_unreachable(exc):
                raise
            snapshot = await self._programs_from_graph()
            if not snapshot:
                raise
            logger.info(
                "Program Mapper unreachable (%s); searching %d seeded %s programs instead",
                exc.__class__.__name__, len(snapshot), self.campus_code,
            )
            # Deliberately not cached: the vendor may allowlist this box at any
            # moment, and a week-long cache of snapshot results would keep
            # serving it long after the live API came back.
            return _match_programs(query, snapshot)
        return _match_programs(query, programs)

    async def get_program_details(self, program_id: str) -> dict:
        key = self._cache_key("program_details", program_id)
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            return cached
        data = await self._get(f"/site-contents/{self.site_content_id}/programs/{program_id}")
        return self._set_cached(key, data)

    async def get_program_requirements(self, program_map_id: str) -> dict:
        key = self._cache_key("program_requirements", program_map_id)
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            return cached
        data = await self._get(f"/site-contents/{self.site_content_id}/program-maps/{program_map_id}")
        return self._set_cached(key, data)

    async def get_careers(self, soc_codes: list[str]) -> list[dict]:
        if not soc_codes:
            return []
        normalized_codes = sorted({code for code in soc_codes if code})
        if not normalized_codes:
            return []
        key = self._cache_key("careers", ",".join(normalized_codes))
        cached = self._get_cached(key)
        if cached is not _CACHE_MISS:
            return cached
        data = await self._post("/standard-occupations/batch", {"standardOccupationalCodes": normalized_codes})
        return self._set_cached(key, data.get("careers", []))

    async def get_program_bundle(self, program_id: str) -> dict:
        try:
            program = await self.get_program_details(program_id)
            pathways = program.get("pathways", [])
            chosen = next((pathway for pathway in pathways if pathway.get("defaultPathway")), pathways[0] if pathways else None)
            requirements = None
            if chosen and chosen.get("programMapId"):
                requirements = await self.get_program_requirements(chosen["programMapId"])
            careers = await self.get_careers(program.get("careerStandardOccupationalCodes", []))
        except Exception as exc:
            if not _is_unreachable(exc):
                raise
            bundle = await self._bundle_from_graph(program_id)
            if bundle is None:
                raise
            logger.info(
                "Program Mapper unreachable (%s); serving %s from the seeded snapshot",
                exc.__class__.__name__, program_id,
            )
            return bundle
        return {
            "campus_code": self.campus_code,
            "campus_name": self.campus_name,
            "program": program,
            "requirements": requirements,
            "careers": careers,
        }

    # ------------------------------------------------------------------
    # Seeded-snapshot fallback
    #
    # The vendor's AWS load balancer 403s the production VPS by IP before their
    # application is reached, and no header or client change moves it — only
    # the vendor allowlisting us. The full dataset is already on the box
    # though, replayed weekly into Neo4j from an unblocked machine, so a
    # refused host degrades to last Sunday's data instead of to a vague RAG
    # summary of a web page.
    # ------------------------------------------------------------------

    async def _programs_from_graph(self) -> list[dict]:
        try:
            rows = await neo4j_service.program_snapshot_for_campus(self.campus_code)
        except Exception as exc:  # the graph is a fallback; it must not raise its own
            logger.warning("Program snapshot lookup failed for %s: %s", self.campus_code, exc)
            return []
        return [_program_from_graph(row) for row in rows]

    async def _bundle_from_graph(self, program_id: str) -> dict | None:
        try:
            row = await neo4j_service.program_snapshot(program_id)
        except Exception as exc:
            logger.warning("Program snapshot lookup failed for %s: %s", program_id, exc)
            return None
        if not row:
            return None
        campus_code = row.get("campus_code") or self.campus_code
        campus_name = COLLEGE_CONFIGS.get(campus_code, {}).get("name", self.campus_name)
        return {
            "campus_code": campus_code,
            "campus_name": campus_name,
            "program": _program_from_graph(row),
            "requirements": {"pathwayElements": _pathway_elements_from_graph(row.get("pathway_elements"))},
            "careers": [_career_from_graph(career) for career in row.get("careers") or []],
            "source": GRAPH_SOURCE,
        }


_clients: Dict[str, ProgramMapperClient] = {}


def get_program_mapper_client(campus_code: str = DEFAULT_CAMPUS) -> ProgramMapperClient:
    campus_code = campus_code.upper()
    if campus_code not in _clients:
        _clients[campus_code] = ProgramMapperClient(campus_code)
    return _clients[campus_code]


async def search_programs_all_campuses(query: str) -> list[dict]:
    async def search(campus_code: str) -> list[dict]:
        client = get_program_mapper_client(campus_code)
        try:
            programs = await client.search_programs(query)
        except Exception as exc:
            logger.warning("Program search failed for %s: %s", campus_code, exc)
            return []
        for program in programs:
            program["campus_code"] = campus_code
            program["campus_name"] = COLLEGE_CONFIGS[campus_code]["name"]
        return programs

    results = await asyncio.gather(*[search(code) for code in COLLEGE_CONFIGS], return_exceptions=True)
    flattened: list[dict] = []
    for result in results:
        if isinstance(result, list):
            flattened.extend(result)
    return flattened
