#!/usr/bin/env python3
"""Sync ProgramMapper data into Neo4j for all 9 LACCD campuses.

Creates Program, ProgramGroup, and Career nodes with relationships.
Best-effort REQUIRES_COURSE linking from pathway element names to Course nodes.
Stores full pathwayElements JSON for course sequence generation.

The vendor may block a deployment host by network before the application sees
the request. Firecrawl can proxy GET requests but cannot issue the required
careers POST, so the supported fallback is record on an allowed machine and
replay from disk on the deployment host.

So the supported path is record on an unblocked machine, replay on the VPS:

    # 1. on a machine that is not blocked (e.g. a laptop) -- no Firecrawl needed
    python scripts/sync_programs_to_neo4j.py --dump-dir data/program_mapper --fetch-only

    # 2. copy the dump over (data/ is gitignored)
    rsync -az --exclude='.env*' data/program_mapper/ operator-host:/path/to/app/data/program_mapper/

    # 3. on the VPS -- seeds from disk, makes no network calls at all
    python scripts/sync_programs_to_neo4j.py --replay-dir data/program_mapper

This also matches how the rest of the graph is built: from disk fixtures rather
than crawled at runtime.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_programs_to_neo4j.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_programs_to_neo4j.py --campus LAMC
"""

from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

import httpx  # noqa: E402
from app.services.neo4j_service import neo4j_service  # noqa: E402
from app.services.program_mapper import COLLEGE_CONFIGS, build_request_headers  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Firecrawl config
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
FIRECRAWL_API_URL = "https://api.firecrawl.dev/v1/scrape"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_GROUPS = 64
MAX_PROGRAMS = 2_000
MAX_PATHWAYS_PER_PROGRAM = 32
MAX_ELEMENTS_PER_PATHWAY = 500
MAX_SOC_CODES = 64
OPERATOR_DEADLINE_SECONDS = 600
MAX_DUMP_BYTES = 256 * 1024 * 1024


async def _read_limited(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Program Mapper response exceeds byte limit")
    return bytes(body)

# Program Mapper API
PM_API_BASE = "https://b.api.programmapper.com"

# Pattern to extract course codes from pathway element names
_PATHWAY_COURSE_RE = re.compile(r"^([A-Z][A-Z &/]+\s+C?\d{2,4}[A-Z]{0,2})(?:\s*:|$)")


def normalize_course_code(code: str) -> str:
    """Collapse a course code to a comparison key: no spaces, upper case.

    ``MATH 262``, ``MATH262`` and ``math 262`` all key to ``MATH262``; the
    multi-word subject ``A S L 001`` keys to ``ASL001``.
    """
    return "".join(code.split()).upper()


def resolve_pathway_course_code(name: str, code_index: dict[str, str]) -> str | None:
    """Map a pathway element name to a real Course.code for that campus.

    Resolution goes through the campus's actual course list rather than parsing
    the name, because the name alone is ambiguous: LACC emits ``ENGLC1000``,
    which is ``ENGL C1000`` (a CCN C-prefixed number) and not ``ENGLC 1000``.
    Splitting on the first digit gets the subject wrong. Looking the normalized
    string up in the campus index cannot.

    Returning None for anything not in the index is the point, not a limitation:
    pathway elements also carry placeholders like ``ADDITIONAL COURSE`` and GE
    headings like ``Cal-GETC Area 4: …``, and inventing edges for those would be
    worse than the honest zero this replaces.
    """
    if not name:
        return None
    return code_index.get(normalize_course_code(name))


def extract_course_code_from_element(name: str) -> str | None:
    """Extract course code from a pathway element name."""
    match = _PATHWAY_COURSE_RE.match(name.strip())
    if match:
        return match.group(1).strip()
    return None


async def fetch_via_firecrawl(url: str, headers: dict | None = None) -> dict | None:
    """Fetch a URL via Firecrawl's scrape API, parse JSON from the response.

    Firecrawl proxies the request from their servers, bypassing IP blocks.
    Returns parsed JSON dict or None on failure.
    """
    if not FIRECRAWL_API_KEY:
        return None

    payload = {
        "url": url,
        "formats": ["markdown"],
    }
    if headers:
        payload["headers"] = headers

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                FIRECRAWL_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                resp.raise_for_status()
                data = json.loads(await _read_limited(resp))

        # Firecrawl returns {success: true, data: {markdown: "...", ...}}
        markdown = data.get("data", {}).get("markdown", "")
        if not markdown:
            return None

        # PM API returns JSON — Firecrawl wraps it in markdown code blocks
        # Strip ```json ... ``` wrapper
        cleaned = markdown.strip()
        if cleaned.startswith("```"):
            # Remove first line (```json) and last line (```)
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        return json.loads(cleaned)

    except json.JSONDecodeError as e:
        logger.warning("Failed to parse JSON from Firecrawl response for %s: %s", url, e)
        return None
    except Exception as e:
        logger.warning("Firecrawl fetch failed for %s: %s", url, e)
        return None


async def fetch_via_httpx(url: str, headers: dict) -> dict | None:
    """Direct fetch via httpx (works when PM API isn't blocking our IP)."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                resp.raise_for_status()
                return json.loads(await _read_limited(resp))
    except Exception as e:
        logger.debug("Direct httpx fetch failed for %s: %s", url, e)
        return None


# Record/replay. If a deployment host cannot reach Program Mapper, an allowed
# machine records responses to disk and the deployment replays the copied dump. This
# also matches how the rest of the graph is built: from disk fixtures, not crawled
# at runtime.
class _MissingRecording:
    """Sentinel: replay was asked for a request that was never recorded."""


_MISSING_RECORDING = _MissingRecording()

DUMP_DIR: Path | None = None
REPLAY_DIR: Path | None = None


def _recording_path(base: Path, campus_code: str, kind: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return base / campus_code / f"{kind}_{digest}.json"


def _read_recording(campus_code: str, kind: str, key: str) -> Any | _MissingRecording:
    path = _recording_path(REPLAY_DIR, campus_code, kind, key)
    if not path.exists():
        logger.warning(
            "%s: no recording for %s %s (looked in %s) — re-run the dump on an "
            "unblocked machine; this would otherwise seed empty data",
            campus_code, kind, key, path,
        )
        return _MISSING_RECORDING
    if path.stat().st_size > MAX_RESPONSE_BYTES:
        raise ValueError(f"recording exceeds byte limit: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_recording(campus_code: str, kind: str, key: str, payload: Any) -> None:
    path = _recording_path(DUMP_DIR, campus_code, kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("recording exceeds per-file byte limit")
    existing_bytes = sum(
        candidate.stat().st_size
        for candidate in DUMP_DIR.rglob("*.json")
        if candidate != path
    )
    if existing_bytes + len(encoded) > MAX_DUMP_BYTES:
        raise ValueError("recording dump exceeds aggregate byte limit")
    path.write_bytes(encoded)


async def pm_fetch(path: str, campus_code: str) -> dict | None:
    """Fetch from PM API — replay from disk, else Firecrawl/direct httpx."""
    if REPLAY_DIR is not None:
        recorded = _read_recording(campus_code, "get", path)
        return None if recorded is _MISSING_RECORDING else recorded

    url = f"{PM_API_BASE}{path}"
    headers = build_request_headers(campus_code)

    # Try direct first (faster, no credits)
    result = await fetch_via_httpx(url, headers)
    if result is None:
        # Fall back to Firecrawl proxy
        logger.debug("Using Firecrawl proxy for %s", url)
        result = await fetch_via_firecrawl(url, headers)

    if result is not None and DUMP_DIR is not None:
        _write_recording(campus_code, "get", path, result)
    return result


async def fetch_careers(campus_code: str, soc_codes: list[str]) -> list[dict]:
    """Look up Career records for a batch of SOC codes.

    POST-only, so the Firecrawl proxy in ``pm_fetch`` cannot cover it — this is
    the one Program Mapper call with no fallback. It is therefore also the one
    that must be loud: a silent failure here empties
    ``(:Program)-[:LEADS_TO_CAREER]->(:Career)`` for the whole district while the
    sync still exits 0, which is exactly what happened (0 career edges in prod).
    """
    if not soc_codes:
        return []

    soc_codes = [str(code)[:32] for code in soc_codes[:MAX_SOC_CODES]]
    key = ",".join(sorted(soc_codes))
    if REPLAY_DIR is not None:
        recorded = _read_recording(campus_code, "careers", key)
        return [] if recorded is _MISSING_RECORDING else recorded

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"{PM_API_BASE}/standard-occupations/batch",
                headers=build_request_headers(campus_code),
                json={"standardOccupationalCodes": soc_codes},
            ) as resp:
                status_code = resp.status_code
                body = await _read_limited(resp)
    except Exception as exc:
        logger.warning(
            "%s: careers request failed for %d SOC codes: %s",
            campus_code, len(soc_codes), exc,
        )
        return []

    if status_code != 200:
        logger.warning(
            "%s: careers request rejected with HTTP %s for %d SOC codes — "
            "LEADS_TO_CAREER will be empty for this campus",
            campus_code, status_code, len(soc_codes),
        )
        return []

    careers = json.loads(body).get("careers", [])[:MAX_PROGRAMS]
    if DUMP_DIR is not None:
        _write_recording(campus_code, "careers", key, careers)
    return careers


async def sync_campus(campus_code: str) -> dict[str, int]:
    """Sync all programs for one campus. Returns counts."""
    config = COLLEGE_CONFIGS[campus_code]
    site_content_id = config["site_content_id"]
    counts = {"groups": 0, "programs": 0, "careers": 0, "course_links": 0, "pathway_elements": 0}

    # One lookup table per campus, not one query per pathway element.
    code_rows = await neo4j_service.run_query(
        "MATCH (c:Course {campus_code: $campus}) RETURN c.code AS code",
        {"campus": campus_code},
    ) or []
    code_index = {normalize_course_code(r["code"]): r["code"] for r in code_rows if r.get("code")}
    logger.info("%s: %d course codes indexed for pathway linking", campus_code, len(code_index))

    # Fetch program groups
    groups_data = await pm_fetch(f"/site-contents/{site_content_id}/home-page-content", campus_code)
    if not groups_data:
        logger.warning("Failed to fetch program groups for %s", campus_code)
        return counts

    groups = groups_data.get("programGroups", [])[:MAX_GROUPS]

    # Upsert ProgramGroups
    for group in groups:
        await neo4j_service.run_write(
            """
            MERGE (g:ProgramGroup {id: $id})
            SET g.title = $title
            """,
            {"id": group["masterRecordId"], "title": group.get("title", "")},
        )
    counts["groups"] = len(groups)

    # Collect all programs across all groups
    all_programs: list[dict] = []
    for group in groups:
        group_data = await pm_fetch(
            f"/site-contents/{site_content_id}/program-groups/{group['masterRecordId']}",
            campus_code,
        )
        if not group_data:
            logger.warning("Failed to fetch programs for group %s at %s", group.get("title"), campus_code)
            continue
        programs = group_data.get("programs", [])[:MAX_PROGRAMS]
        for p in programs:
            p["group_id"] = group["masterRecordId"]
            p["group_title"] = group.get("title", "")
        all_programs.extend(programs[: max(0, MAX_PROGRAMS - len(all_programs))])
        if len(all_programs) >= MAX_PROGRAMS:
            break

    # Deduplicate by masterRecordId
    seen: set[str] = set()
    unique_programs: list[dict] = []
    for p in all_programs:
        pid = p.get("masterRecordId", "")
        if pid and pid not in seen:
            seen.add(pid)
            unique_programs.append(p)

    logger.info("  %s: %d groups, %d unique programs to sync", campus_code, len(groups), len(unique_programs))

    # Upsert Programs and link to campus + group
    for i, p in enumerate(unique_programs):
        pid = p["masterRecordId"]

        # Get full details for award type and SOC codes
        details = await pm_fetch(f"/site-contents/{site_content_id}/programs/{pid}", campus_code)
        if not details:
            details = p

        award = details.get("awardShortTitle") or p.get("awardShortTitle") or ""
        soc_codes = details.get("careerStandardOccupationalCodes", [])
        description = (details.get("description") or "")[:500]
        catalog_url = details.get("catalogUrl") or ""

        await neo4j_service.run_write(
            """
            MERGE (prog:Program {master_record_id: $id})
            SET prog.title = $title,
                prog.award_type = $award,
                prog.campus_code = $campus,
                prog.description = $description,
                prog.catalog_url = $catalog_url
            WITH prog
            MATCH (campus:Campus {code: $campus})
            MERGE (prog)-[:AT_CAMPUS]->(campus)
            WITH prog
            MATCH (g:ProgramGroup {id: $group_id})
            MERGE (prog)-[:IN_GROUP]->(g)
            """,
            {
                "id": pid,
                "title": p.get("title", ""),
                "award": award,
                "campus": campus_code,
                "description": description,
                "catalog_url": catalog_url,
                "group_id": p.get("group_id", ""),
            },
        )

        # Upsert careers from SOC codes
        if soc_codes:
            careers = await fetch_careers(campus_code, soc_codes)

            for career in careers:
                soc = career.get("standardOccupationalCode") or career.get("code") or ""
                if not soc:
                    continue
                await neo4j_service.run_write(
                    """
                    MERGE (c:Career {soc_code: $soc})
                    SET c.title = $title, c.avg_salary = $salary
                    WITH c
                    MATCH (prog:Program {master_record_id: $pid})
                    MERGE (prog)-[:LEADS_TO_CAREER]->(c)
                    """,
                    {
                        "soc": soc,
                        "title": career.get("title", ""),
                        "salary": career.get("averageSalaryK"),
                        "pid": pid,
                    },
                )
                counts["careers"] += 1

        # Fetch and store pathway elements (for course sequence generation)
        try:
            pathways = details.get("pathways", [])[:MAX_PATHWAYS_PER_PROGRAM]
            chosen = next((pw for pw in pathways if pw.get("defaultPathway")), pathways[0] if pathways else None)
            if chosen and chosen.get("programMapId"):
                reqs = await pm_fetch(
                    f"/site-contents/{site_content_id}/program-maps/{chosen['programMapId']}",
                    campus_code,
                )
                if reqs:
                    elements = reqs.get("pathwayElements", [])[:MAX_ELEMENTS_PER_PATHWAY]

                    # Store full pathwayElements JSON on the Program node
                    if elements:
                        await neo4j_service.run_write(
                            """
                            MATCH (prog:Program {master_record_id: $pid})
                            SET prog.pathway_elements = $elements_json,
                                prog.program_map_id = $map_id
                            """,
                            {
                                "pid": pid,
                                "elements_json": json.dumps(elements),
                                "map_id": chosen["programMapId"],
                            },
                        )
                        counts["pathway_elements"] += len(elements)

                    # Link pathway elements to Course nodes (best-effort)
                    for element in elements:
                        raw_name = element.get("name", "")
                        course_code = resolve_pathway_course_code(raw_name, code_index)
                        if course_code is None:
                            # Fall back to the legacy regex for anything the index
                            # misses, so behaviour can only widen, never narrow.
                            extracted = extract_course_code_from_element(raw_name)
                            if extracted:
                                course_code = code_index.get(normalize_course_code(extracted))
                        if course_code:
                            await neo4j_service.run_write(
                                """
                                MATCH (prog:Program {master_record_id: $pid})
                                MATCH (c:Course {code: $code, campus_code: $campus})
                                MERGE (prog)-[:REQUIRES_COURSE]->(c)
                                """,
                                {"pid": pid, "code": course_code, "campus": campus_code},
                            )
                            counts["course_links"] += 1
        except Exception as exc:
            logger.debug("Pathway element sync failed for %s: %s", pid, exc)

        if (i + 1) % 10 == 0:
            logger.info("  %s: %d/%d programs synced", campus_code, i + 1, len(unique_programs))

    counts["programs"] = len(unique_programs)
    logger.info(
        "Synced %s: %d groups, %d programs, %d careers, %d course links, %d pathway elements",
        campus_code, counts["groups"], counts["programs"], counts["careers"],
        counts["course_links"], counts["pathway_elements"],
    )
    return counts


async def record_sync_heartbeat(totals: dict[str, int], campuses: int) -> None:
    """Stamp when this snapshot was written, so prod can tell if it went stale.

    Production serves program answers from this snapshot and cannot reach the
    vendor at all, so a refresh that quietly stops running would freeze the data
    while every answer still looked correct. The heartbeat is what makes that
    visible (``snapshot_freshness.py`` reads it).

    **Only a run that actually wrote programs stamps it.** Certifying an empty
    run as fresh would be the same silent success the heartbeat exists to catch
    — and this repo has shipped that bug before, in scripts that exit 0 having
    written nothing.
    """
    if not totals.get("programs"):
        logger.warning(
            "Not recording a sync heartbeat: this run wrote 0 programs, so the "
            "snapshot on disk is not proven fresh",
        )
        return

    await neo4j_service.run_write(
        """
        MERGE (s:SyncHeartbeat {source: 'program_mapper'})
        SET s.completed_at = $completed_at,
            s.programs = $programs,
            s.careers = $careers,
            s.campuses = $campuses,
            // Cleared here on purpose: a heartbeat backfilled by hand must stop
            // claiming to be one the moment a real run replaces it.
            s.backfilled = false
        """,
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "programs": totals.get("programs", 0),
            "careers": totals.get("careers", 0),
            "campuses": campuses,
        },
    )
    logger.info(
        "Recorded sync heartbeat: %d programs across %d campus(es)",
        totals.get("programs", 0), campuses,
    )


async def fetch_campus(campus_code: str) -> int:
    """Walk every Program Mapper endpoint sync_campus needs, recording as we go.

    Deliberately mirrors sync_campus's traversal rather than inventing its own, so
    the dump is complete by construction: anything the seed will ask for on replay
    is fetched here. Writes nothing to Neo4j.
    """
    config = COLLEGE_CONFIGS[campus_code]
    site_content_id = config["site_content_id"]

    groups_data = await pm_fetch(f"/site-contents/{site_content_id}/home-page-content", campus_code)
    if not groups_data:
        logger.error("%s: could not fetch program groups — dump will be incomplete", campus_code)
        return 0
    groups = groups_data.get("programGroups", [])[:MAX_GROUPS]

    all_programs: list[dict] = []
    for group in groups:
        group_data = await pm_fetch(
            f"/site-contents/{site_content_id}/program-groups/{group['masterRecordId']}",
            campus_code,
        )
        if group_data:
            all_programs.extend(
                group_data.get("programs", [])[: max(0, MAX_PROGRAMS - len(all_programs))]
            )
            if len(all_programs) >= MAX_PROGRAMS:
                break

    seen: set[str] = set()
    fetched = 0
    careers_seen = 0
    maps_seen = 0
    for p in all_programs:
        pid = p.get("masterRecordId", "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        details = await pm_fetch(f"/site-contents/{site_content_id}/programs/{pid}", campus_code)
        if not details:
            continue
        fetched += 1
        soc_codes = details.get("careerStandardOccupationalCodes", [])
        if soc_codes:
            careers = await fetch_careers(campus_code, soc_codes)
            careers_seen += len(careers)

        # Record EVERY pathway's program map, not just the default one that
        # sync_campus happens to pick today. Recording only the chosen map makes
        # the dump depend on selection logic staying identical; recording all of
        # them makes replay correct regardless. Missing these is what seeded 0
        # pathway elements and 0 course links on the first replay.
        for pathway in (details.get("pathways", []) or [])[:MAX_PATHWAYS_PER_PROGRAM]:
            map_id = pathway.get("programMapId")
            if not map_id:
                continue
            if await pm_fetch(f"/site-contents/{site_content_id}/program-maps/{map_id}", campus_code):
                maps_seen += 1

    logger.info(
        "%s: recorded %d groups, %d programs, %d program maps, %d career records",
        campus_code, len(groups), fetched, maps_seen, careers_seen,
    )
    return fetched


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync ProgramMapper data to Neo4j")
    parser.add_argument("--campus", help="Sync only a specific campus (e.g., LAMC)")
    parser.add_argument(
        "--dump-dir",
        help="Record every Program Mapper response to this directory (run from an "
             "unblocked machine; the production VPS is WAF-blocked by IP).",
    )
    parser.add_argument(
        "--replay-dir",
        help="Seed from a recorded dump instead of calling the API. No network.",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="With --dump-dir: record responses and skip Neo4j entirely.",
    )
    args = parser.parse_args()

    global DUMP_DIR, REPLAY_DIR
    if args.dump_dir:
        DUMP_DIR = Path(args.dump_dir)
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Recording Program Mapper responses to %s", DUMP_DIR)
    if args.replay_dir:
        REPLAY_DIR = Path(args.replay_dir)
        if not REPLAY_DIR.is_dir():
            logger.error("--replay-dir %s does not exist", REPLAY_DIR)
            sys.exit(1)
        logger.info("Replaying Program Mapper responses from %s (no network)", REPLAY_DIR)

    if REPLAY_DIR is None and not FIRECRAWL_API_KEY:
        logger.warning("FIRECRAWL_API_KEY not set — will try direct PM API calls only")

    if args.fetch_only:
        if not DUMP_DIR:
            logger.error("--fetch-only requires --dump-dir")
            sys.exit(1)
        campuses = [args.campus.upper()] if args.campus else list(COLLEGE_CONFIGS.keys())
        for campus in campuses:
            await asyncio.wait_for(fetch_campus(campus), timeout=OPERATOR_DEADLINE_SECONDS)
        logger.info("Fetch-only complete. Copy %s to the VPS and seed with --replay-dir.", DUMP_DIR)
        return

    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
        sys.exit(1)

    connected = await neo4j_service.verify_connectivity()
    if not connected:
        logger.error("Cannot connect to Neo4j")
        sys.exit(1)
    logger.info("Connected to Neo4j")

    campuses = [args.campus.upper()] if args.campus else list(COLLEGE_CONFIGS.keys())
    totals = {"groups": 0, "programs": 0, "careers": 0, "course_links": 0, "pathway_elements": 0}

    for campus in campuses:
        counts = await asyncio.wait_for(sync_campus(campus), timeout=OPERATOR_DEADLINE_SECONDS)
        for k in totals:
            totals[k] += counts[k]

    logger.info(
        "Program sync complete: %d groups, %d programs, %d careers, %d course links, "
        "%d pathway elements across %d campus(es)",
        totals["groups"], totals["programs"], totals["careers"], totals["course_links"],
        totals["pathway_elements"], len(campuses),
    )
    await record_sync_heartbeat(totals, len(campuses))
    await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
