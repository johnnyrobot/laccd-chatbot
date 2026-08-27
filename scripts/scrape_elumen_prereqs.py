#!/usr/bin/env python3
"""Scrape ALL course prerequisites from eLumen Public Portal API.

Queries per-subject (eLumen caps at 25 results per query) and extracts
structured prerequisite data from the `requisites` field.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_prereqs.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_prereqs.py --campus LAMC
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_prereqs.py --campus all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import httpx
from scripts.pagination_guard import PaginationGuard

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://portalapi-laccd.elumenapp.com/public"
API_TIMEOUT = 60.0  # eLumen is slow
REQUEST_DELAY = 1.5  # seconds between requests to avoid overloading

CAMPUS_TENANTS = {
    "LAMC": "lamission.elumenapp.com",
    "LAVC": "lavc.elumenapp.com",
    "LAPC": "pierce.elumenapp.com",
    "LAHC": "lahc.elumenapp.com",
    "LATTC": "lattc.elumenapp.com",
    "LACC": "lacc.elumenapp.com",
    "ELAC": "elac.elumenapp.com",
    "LASC": "lasc.elumenapp.com",
    "WLAC": "wlac.elumenapp.com",
}

# Subject codes to query — comprehensive list across LACCD
# The schedule API gives per-campus subjects; these cover all campuses
ALL_SUBJECTS = [
    "ACAD PR", "ACCTG", "ADM JUS", "AFRO AM", "ALD HTH", "ANATOMY", "ANIMATN",
    "ANTHRO", "ART", "ARTHIST", "ASTRON", "BIOLOGY", "BIOTECH", "BSICSKL",
    "BUS", "CAOT", "CH DEV", "CHEM", "CHICANO", "CINEMA", "CIS", "CLN ART",
    "COMM", "COUNSEL", "CS", "DANCEST", "DRAFT", "E.S.L.", "EARTH", "ECON",
    "EDUC", "EGD TEK", "ENG GEN", "ENG SUP", "ENGL", "ENGLISH", "ENV SCI",
    "FAM &CS", "FINANCE", "FIRETEK", "FRENCH", "GEOG", "HEALTH", "HISTORY",
    "HLTHOCC", "HTHTEK", "HUMAN", "IND TEK", "INTRDGN", "ITALIAN", "JOURNAL",
    "KIN", "KIN ATH", "KIN MAJ", "LAW", "LING", "MARKET", "MATH", "MGMT",
    "MICRO", "MULTIMD", "MUSIC", "NRS-HCA", "NURSING", "NUTRTN", "OCEANO",
    "PHILOS", "PHOTO", "PHRMCTK", "PHYS SC", "PHYSICS", "PHYSIOL",
    "POL SCI", "POLS", "PROFBKG", "PSYC", "PSYCH", "REAL ES", "SOC",
    "SPANISH", "STAT", "TECTHTR", "THEATER", "TUTOR", "VOC ED",
]

CODE_RE = re.compile(r'^([A-Z]+(?:\s+[A-Z]+)*)\s*(\d+[A-Z]*(?:CE)?)$')


def normalize_code(raw_code: str) -> str:
    """Normalize 'MATH262' -> 'MATH 262', 'STATC1000' -> 'STAT C1000'."""
    raw = raw_code.strip().upper()
    # Handle C-prefix courses: STATC1000 -> STAT C1000
    m = re.match(r'^([A-Z]{2,6}?)C(\d+[A-Z]*)$', raw)
    if m:
        prefix = m.group(1)
        # Exclude subjects that end in C naturally (PSYC, MUSIC, etc.)
        if prefix not in ('MUSI', 'PSYC', 'ACAC', 'DANC'):
            return f"{prefix} C{m.group(2)}"
    # Standard: MATH262 -> MATH 262
    m = CODE_RE.match(raw)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return raw


def parse_requisites(requisites: dict) -> list[dict]:
    """Parse eLumen requisites tree into flat prerequisite entries."""
    if not requisites:
        return []

    results = []
    group_id = 0

    block_list = requisites.get("blockList", [])
    top_type = requisites.get("type", "AND")

    for block in block_list:
        if "item" in block:
            item = block["item"]
            if not item.get("isCourse", False):
                continue
            item_type = item.get("itemType", "Prerequisite")
            if item_type not in ("Prerequisite", "Corequisite"):
                continue
            code = normalize_code(item.get("code", ""))
            parts = code.split(maxsplit=1)
            if len(parts) == 2:
                results.append({
                    "subject": parts[0],
                    "number": parts[1],
                    "type": item_type.lower(),
                    "group": group_id,
                })
            group_id += 1

        elif "blockList" in block:
            inner_items = []
            for inner in block.get("blockList", []):
                if "item" not in inner:
                    continue
                item = inner["item"]
                if not item.get("isCourse", False):
                    continue
                item_type = item.get("itemType", "Prerequisite")
                if item_type not in ("Prerequisite", "Corequisite"):
                    continue
                code = normalize_code(item.get("code", ""))
                parts = code.split(maxsplit=1)
                if len(parts) == 2:
                    inner_items.append({
                        "subject": parts[0],
                        "number": parts[1],
                        "type": item_type.lower(),
                        "group": group_id,
                    })

            if inner_items:
                if top_type == "OR":
                    for item in inner_items:
                        item["group"] = group_id
                    results.extend(inner_items)
                    group_id += 1
                else:
                    for item in inner_items:
                        item["group"] = group_id
                        group_id += 1
                    results.extend(inner_items)

    return results


def _requisites_to_text(requisites: dict) -> str:
    """Convert requisites tree to human-readable text."""
    if not requisites:
        return "None"
    parts = []
    for block in requisites.get("blockList", []):
        if "item" in block:
            item = block["item"]
            code = normalize_code(item.get("code", ""))
            parts.append(f"{item.get('itemType', '')}: {code}")
        elif "blockList" in block:
            inner = []
            for b in block.get("blockList", []):
                if "item" in b:
                    code = normalize_code(b["item"].get("code", ""))
                    inner.append(code)
            if inner:
                join_word = " and " if block.get("type") == "AND" else " or "
                parts.append(join_word.join(inner))
    top_type = requisites.get("type", "AND")
    join_word = " or " if top_type == "OR" else " and "
    return join_word.join(parts) if parts else "None"


async def fetch_courses_for_subject(
    client: httpx.AsyncClient, tenant: str, subject: str
) -> list[dict]:
    """Fetch courses for a single subject from eLumen."""
    courses = []
    page = 1
    guard = PaginationGuard()

    while True:
        guard.before_request(page)
        params = {
            "status": "approved",
            "tenant": tenant,
            "query": subject,
            "pageSize": 25,
            "page": page,
        }
        try:
            resp = await client.get(f"{API_BASE}/courses", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.warning("Timeout fetching %s page %d, retrying...", subject, page)
            await asyncio.sleep(3)
            try:
                resp = await client.get(f"{API_BASE}/courses", params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error("Retry failed for %s page %d: %s", subject, page, e)
                break
        except Exception as e:
            logger.error("Failed fetching %s page %d: %s", subject, page, e)
            break

        items = data.get("_embedded", {}).get("courses", [])
        if not items:
            break
        guard.observe(items)

        for item in items:
            raw_json = item.get("fullCourseInfo", "")
            if not raw_json:
                continue
            try:
                info = json.loads(raw_json)
            except json.JSONDecodeError:
                continue

            subj = item.get("subject", "")
            number = item.get("number", "")
            code = normalize_code(f"{subj}{number}")
            title = info.get("title", "")
            requisites = info.get("requisites", {})

            courses.append({
                "code": code,
                "title": title,
                "requisites_raw": requisites,
            })

        # eLumen caps at 25 per page — if we got 25, there might be more
        if len(items) < 25:
            break
        page += 1
        await asyncio.sleep(REQUEST_DELAY)

    return courses


async def scrape_campus(campus_code: str) -> list[dict]:
    """Scrape all prerequisites for a campus by querying each subject."""
    tenant = CAMPUS_TENANTS.get(campus_code)
    if not tenant:
        logger.error("Unknown campus code: %s", campus_code)
        return []

    logger.info("Scraping %s (tenant: %s)...", campus_code, tenant)

    seen_codes: set[str] = set()
    all_courses: list[dict] = []

    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        for i, subject in enumerate(ALL_SUBJECTS):
            courses = await fetch_courses_for_subject(client, tenant, subject)
            new = 0
            for course in courses:
                if course["code"] not in seen_codes:
                    seen_codes.add(course["code"])
                    all_courses.append(course)
                    new += 1
            if new > 0:
                logger.info(
                    "[%d/%d] %s: %d courses (%d new, %d total)",
                    i + 1, len(ALL_SUBJECTS), subject, len(courses), new, len(all_courses),
                )
            await asyncio.sleep(REQUEST_DELAY)

    logger.info("Found %d unique courses for %s", len(all_courses), campus_code)

    results = []
    with_prereqs = 0

    for course in all_courses:
        prereqs = parse_requisites(course["requisites_raw"])
        entry = {
            "course_code": course["code"],
            "title": course["title"],
            "has_prerequisites": len(prereqs) > 0,
            "prerequisite_courses": prereqs,
        }
        if course["requisites_raw"]:
            entry["prerequisite_text"] = _requisites_to_text(course["requisites_raw"])
        results.append(entry)
        if prereqs:
            with_prereqs += 1

    logger.info(
        "%s: %d courses total, %d with prerequisites, %d without",
        campus_code, len(results), with_prereqs, len(results) - with_prereqs,
    )
    return results


async def main():
    parser = argparse.ArgumentParser(description="Scrape prerequisites from eLumen")
    parser.add_argument("--campus", default="LAMC", help="Campus code or 'all'")
    args = parser.parse_args()

    out_dir = PROJECT_ROOT / "data" / "prerequisites"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.campus.upper() == "ALL":
        campuses = list(CAMPUS_TENANTS.keys())
    else:
        campuses = [args.campus.upper()]

    for campus in campuses:
        start = time.time()
        results = await scrape_campus(campus)
        elapsed = time.time() - start
        out_file = out_dir / f"{campus.lower()}_prerequisites.json"
        out_file.write_text(json.dumps(results, indent=2))
        logger.info("Saved %d courses to %s (%.0fs)", len(results), out_file, elapsed)


if __name__ == "__main__":
    asyncio.run(main())
