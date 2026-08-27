from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable, Protocol

from app.config import DEFAULT_DATA_DIR
from app.services.elumen_service import CAMPUS_TENANTS, ElumenService, get_elumen_service
from app.services.gemini_file_search import GeminiFileSearchService
from app.services.neo4j_service import neo4j_service
from app.services.catalog_provenance import campus_catalog_provenance, catalog_health_dict
from app.services.snapshot_freshness import program_snapshot_freshness, snapshot_health_dict
from app.services.program_mapper import (
    COLLEGE_CONFIGS,
    ProgramMapperClient,
    egress_breaker,
    get_program_mapper_client,
)

logger = logging.getLogger(__name__)

ALL_DATA_CAMPUSES = tuple(sorted(set(CAMPUS_TENANTS) | set(COLLEGE_CONFIGS)))


class Neo4jHealthClient(Protocol):
    def is_configured(self) -> bool: ...

    async def run_query(self, cypher: str, params: dict | None = None) -> list[dict]: ...


async def build_data_health_report(
    campuses: Iterable[str] | None = None,
    *,
    elumen: ElumenService | None = None,
    program_client_factory: Callable[[str], ProgramMapperClient] = get_program_mapper_client,
    neo4j: Neo4jHealthClient = neo4j_service,
    gemini: GeminiFileSearchService | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict:
    """Return per-campus coverage diagnostics without requiring live secrets in tests."""
    campus_codes = [campus.upper() for campus in (campuses or ALL_DATA_CAMPUSES)]
    elumen_service = elumen or get_elumen_service()
    gemini_service = gemini or GeminiFileSearchService()
    neo4j_configured = neo4j.is_configured()
    # One snapshot serves all nine campuses, so read its age once rather than
    # nine times.
    snapshot = snapshot_health_dict(await program_snapshot_freshness())

    report = {
        "campuses": {},
        "summary": {
            "campus_count": len(campus_codes),
            "neo4j_configured": neo4j_configured,
        },
    }

    for campus in campus_codes:
        report["campuses"][campus] = {
            "elumen": _elumen_status(elumen_service, campus, data_dir),
            "program_mapper": _program_mapper_status(program_client_factory, campus, snapshot),
            "neo4j": await _neo4j_status(neo4j, campus, neo4j_configured),
            # Which catalog edition this campus is actually serving. Absent
            # until the campus has been re-parsed with provenance; reported as
            # recorded=False rather than guessed.
            "catalog": catalog_health_dict(await campus_catalog_provenance(campus)),
            "gemini_file_search": _gemini_status(gemini_service, campus),
        }

    return report


def _elumen_status(service: ElumenService, campus: str, data_dir: Path) -> dict:
    snapshot_path = data_dir / "elumen_courses" / f"{campus.lower()}_courses.json"
    stats = service.cache_stats(campus) if hasattr(service, "cache_stats") else {}
    return {
        "tenant_configured": campus in CAMPUS_TENANTS,
        "tenant": CAMPUS_TENANTS.get(campus),
        "snapshot_available": snapshot_path.exists(),
        "snapshot_path": str(snapshot_path),
        "course_cache_entries": stats.get("course_entries", 0),
        "prereq_cache_entries": stats.get("prereq_entries", 0),
        "negative_course_cache_entries": stats.get("negative_course_entries", 0),
        "search_cache_entries": stats.get("search_entries", 0),
    }


def _program_mapper_status(
    program_client_factory: Callable[[str], ProgramMapperClient],
    campus: str,
    snapshot: dict | None = None,
) -> dict:
    breaker = egress_breaker()
    status = {
        "configured": campus in COLLEGE_CONFIGS,
        "campus_name": COLLEGE_CONFIGS.get(campus, {}).get("name"),
        "site_content_id": COLLEGE_CONFIGS.get(campus, {}).get("site_content_id"),
        "cache_entries": 0,
        # Production cannot reach the vendor at all — their WAF refuses this
        # box by IP — and program answers now come from the seeded snapshot
        # instead. That degradation is invisible in the answers themselves, so
        # it has to be visible here.
        "live_api_reachable": not breaker.is_open(),
        "answering_from": "neo4j_snapshot" if breaker.is_open() else "live_api",
        # How old that snapshot is. `stale: None` means no refresh has ever
        # been recorded — deliberately not the same as `False`.
        "snapshot": snapshot if snapshot is not None else {},
    }
    if breaker.is_open():
        status["unreachable_reason"] = breaker.reason
    if not status["configured"]:
        return status
    try:
        client = program_client_factory(campus)
        if hasattr(client, "cache_stats"):
            status["cache_entries"] = client.cache_stats().get("entries", 0)
    except Exception as exc:
        logger.debug("Program Mapper diagnostic failed for %s: %s", campus, exc, exc_info=True)
        status["error"] = str(exc)
    return status


async def _neo4j_status(
    neo4j: Neo4jHealthClient,
    campus: str,
    configured: bool,
) -> dict:
    status = {
        "configured": configured,
        "counts": {},
    }
    if not configured:
        return status
    try:
        rows = await neo4j.run_query(
            """
            MATCH (c:Course {campus_code: $campus})
            WITH count(c) AS courses
            OPTIONAL MATCH (p:Program {campus_code: $campus})
            WITH courses, count(p) AS programs
            OPTIONAL MATCH (i:Instructor {campus_code: $campus})
            RETURN courses, programs, count(i) AS instructors
            """,
            {"campus": campus},
        )
        if rows:
            status["counts"] = {
                "courses": rows[0].get("courses", 0),
                "programs": rows[0].get("programs", 0),
                "instructors": rows[0].get("instructors", 0),
            }
    except Exception as exc:
        logger.debug("Neo4j diagnostic failed for %s: %s", campus, exc, exc_info=True)
        status["error"] = str(exc)
    return status


def _gemini_status(service: GeminiFileSearchService, campus: str) -> dict:
    configured = service.is_configured()
    status = {
        "configured": configured,
        "store_present": False,
        "store_name": None,
    }
    if not configured:
        return status
    try:
        store_name = service.get_store_for_campus(campus)
        status["store_present"] = bool(store_name)
        status["store_name"] = store_name
    except Exception as exc:
        logger.debug("Gemini File Search diagnostic failed for %s: %s", campus, exc, exc_info=True)
        status["error"] = str(exc)
    return status
