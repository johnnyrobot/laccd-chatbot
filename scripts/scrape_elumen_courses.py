#!/usr/bin/env python3
"""Scrape full course data from eLumen Public Portal API.

Queries per-subject (eLumen caps at 25 results per query) and extracts:
- Catalog descriptions, lecture/lab hours, transfer status
- Structured prerequisites, corequisites, advisories
- Non-course (text-based) requisites
- Course topics, SLOs, objectives

Supersedes scrape_elumen_prereqs.py (which only captured prerequisites).

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_courses.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_courses.py --campus LAMC
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/scrape_elumen_courses.py --campus all
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


def _extract_description(info: dict) -> str:
    """Extract catalog description from customFields or courseDescription."""
    # Try courseDescription first (top-level)
    desc = (info.get("courseDescription") or "").strip()
    if desc:
        return desc

    # Try customFields for districtScheduleDescription
    custom_fields = info.get("customFields")
    if isinstance(custom_fields, list):
        for field in custom_fields:
            if isinstance(field, dict):
                name = (field.get("name") or "").lower()
                if "description" in name:
                    return (field.get("value") or "").strip()
    elif isinstance(custom_fields, dict):
        for key in ("districtScheduleDescription", "description", "courseDescription"):
            val = custom_fields.get(key)
            if val:
                return str(val).strip()

    return ""


#: eLumen writes requisite kinds as display strings — "Prerequisite",
#: "Co-Requisite", "Advisory", "Anti-Requisite". Punctuation and case vary, so
#: normalize before comparing. Anti-requisites are deliberately NOT mapped:
#: they mean "cannot be taken with", and the previous code defaulted every
#: unrecognized value to "prerequisite", which silently turned both
#: corequisites and anti-requisites into prerequisites.
_ITEM_TYPE_ALIASES = {
    "prerequisite": "prerequisite",
    "prereq": "prerequisite",
    "corequisite": "corequisite",
    "coreq": "corequisite",
    "advisory": "advisory",
    "recommendedpreparation": "advisory",
}

#: Types we recognize and deliberately do not model. Kept separate from unknown
#: values so that a genuinely new eLumen type is reported instead of blending in.
_ITEM_TYPES_IGNORED = {"antirequisite", "antireq"}

#: Unknown keys already reported, so one new type doesn't emit thousands of lines.
_warned_item_types: set[str] = set()


def normalize_item_type(raw: object) -> str | None:
    """Map an eLumen itemType to prerequisite/corequisite/advisory, else None.

    Returns None for anti-requisites (recognized, intentionally not modeled) and
    for anything unrecognized. An unrecognized value is logged once: eLumen
    renaming a type is exactly how corequisites went missing for months, and a
    silent drop would repeat that.
    """
    key = re.sub(r"[^a-z]", "", str(raw or "").lower())
    mapped = _ITEM_TYPE_ALIASES.get(key)
    if mapped is None and key not in _ITEM_TYPES_IGNORED and key not in _warned_item_types:
        _warned_item_types.add(key)
        logger.warning(
            "Unknown eLumen itemType %r (normalized %r) — requisites of this kind are "
            "being skipped. Add it to _ITEM_TYPE_ALIASES or _ITEM_TYPES_IGNORED.",
            raw,
            key,
        )
    return mapped


#: Minimum fraction of scraped courses that must carry each near-universal field.
DEFAULT_MIN_YIELD = 0.5

#: Fields eLumen populates for virtually every approved course. When a key is
#: renamed upstream, the scraper writes empty values and exits 0 — that is how
#: SLOs, objectives, transfer status and hours stayed blank for months. Requisite
#: lists are deliberately excluded: they are legitimately sparse (corequisites sit
#: near 4%), so a ratio floor cannot distinguish sparse from broken there.
COVERAGE_FIELDS = ("description", "transfer_status", "slos", "objectives")


def check_field_coverage(
    campus: str, rows: list[dict], min_yield: float
) -> list[str]:
    """Return a message per near-universal field that is suspiciously empty."""
    if not rows or min_yield <= 0:
        return []
    problems = []
    for field in COVERAGE_FIELDS:
        populated = sum(1 for r in rows if r.get(field))
        ratio = populated / len(rows)
        logger.info(
            "  coverage %-16s %4d/%4d = %3.0f%%", field, populated, len(rows), ratio * 100
        )
        if ratio < min_yield:
            problems.append(
                f"{campus}.{field}: {populated}/{len(rows)} = {ratio:.0%} "
                f"(below {min_yield:.0%})"
            )
    return problems


def _extract_hours(info: dict) -> tuple[float, float]:
    """Extract lecture and lab hours from creditsAndHours."""
    lecture_hours = 0.0
    lab_hours = 0.0
    credits_hours = info.get("creditsAndHours", [])
    if not isinstance(credits_hours, list):
        return lecture_hours, lab_hours

    # eLumen puts hours directly on each credits-and-hours profile as
    # "lectureHours" / "labHours". (An older shape keyed them off an
    # "activityType" string with min/max hours; that is kept as a fallback.)
    for entry in credits_hours:
        if not isinstance(entry, dict):
            continue

        for key, current in (("lectureHours", lecture_hours), ("labHours", lab_hours)):
            raw = entry.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (ValueError, TypeError):
                continue
            if key == "lectureHours":
                lecture_hours = max(lecture_hours, value)
            else:
                lab_hours = max(lab_hours, value)

        # Legacy shape.
        activity = str(entry.get("activityType") or "").lower()
        if not activity:
            continue
        hours = entry.get("minimumHours") or entry.get("maximumHours") or 0
        try:
            hours = float(hours)
        except (ValueError, TypeError):
            continue
        if "lecture" in activity:
            lecture_hours = max(lecture_hours, hours)
        elif "lab" in activity:
            lab_hours = max(lab_hours, hours)

    return lecture_hours, lab_hours


def parse_requisites(requisites: dict) -> dict:
    """Parse eLumen requisites tree into structured prerequisite, corequisite, and advisory entries.

    Returns:
        {
            "prerequisite_courses": [...],
            "corequisite_courses": [...],
            "advisory_courses": [...],
            "prereq_text": str,      # Non-course prerequisite text
            "corequisite_text": str,  # Non-course corequisite text
            "advisory_text": str,     # Non-course advisory text
        }
    """
    result = {
        "prerequisite_courses": [],
        "corequisite_courses": [],
        "advisory_courses": [],
        "prereq_text": "",
        "corequisite_text": "",
        "advisory_text": "",
    }
    if not requisites:
        return result

    group_id = 0
    text_items: dict[str, list[str]] = {
        "prerequisite": [],
        "corequisite": [],
        "advisory": [],
    }

    block_list = requisites.get("blockList", [])
    top_type = requisites.get("type", "AND")

    for block in block_list:
        if "item" in block:
            item = block["item"]
            item_type = normalize_item_type(item.get("itemType") or "Prerequisite")
            if item_type is None:
                # Anti-requisite or an unrecognized kind — skip rather than
                # mislabel it as a prerequisite.
                continue

            if not item.get("isCourse", False):
                # Non-course item — capture as text
                text = item.get("title") or item.get("code") or item.get("name") or ""
                if text:
                    text_items[item_type].append(text.strip())
                continue

            code = normalize_code(item.get("code", ""))
            parts = code.split(maxsplit=1)
            if len(parts) == 2:
                entry = {
                    "subject": parts[0],
                    "number": parts[1],
                    "type": item_type,
                    "group": group_id,
                }
                result[f"{item_type}_courses"].append(entry)
            group_id += 1

        elif "blockList" in block:
            inner_courses: dict[str, list[dict]] = {
                "prerequisite": [],
                "corequisite": [],
                "advisory": [],
            }
            for inner in block.get("blockList", []):
                if "item" not in inner:
                    continue
                item = inner["item"]
                item_type = normalize_item_type(item.get("itemType") or "Prerequisite")
                if item_type is None:
                    continue

                if not item.get("isCourse", False):
                    text = item.get("title") or item.get("code") or item.get("name") or ""
                    if text:
                        text_items[item_type].append(text.strip())
                    continue

                code = normalize_code(item.get("code", ""))
                parts = code.split(maxsplit=1)
                if len(parts) == 2:
                    inner_courses[item_type].append({
                        "subject": parts[0],
                        "number": parts[1],
                        "type": item_type,
                        "group": group_id,
                    })

            for itype, items in inner_courses.items():
                if items:
                    if top_type == "OR":
                        for item in items:
                            item["group"] = group_id
                        result[f"{itype}_courses"].extend(items)
                        group_id += 1
                    else:
                        for item in items:
                            item["group"] = group_id
                            group_id += 1
                        result[f"{itype}_courses"].extend(items)

    # Also check customFields for text-based requisite descriptions
    custom_fields = requisites.get("customFields")
    if isinstance(custom_fields, list):
        for field in custom_fields:
            if isinstance(field, dict):
                name = (field.get("name") or "").lower()
                value = (field.get("value") or "").strip()
                if value:
                    if "prereq" in name:
                        text_items["prerequisite"].append(value)
                    elif "coreq" in name:
                        text_items["corequisite"].append(value)
                    elif "advis" in name:
                        text_items["advisory"].append(value)
    elif isinstance(custom_fields, dict):
        for key, value in custom_fields.items():
            if not value:
                continue
            key_lower = key.lower()
            val_str = str(value).strip()
            if "prereq" in key_lower and val_str:
                text_items["prerequisite"].append(val_str)
            elif "coreq" in key_lower and val_str:
                text_items["corequisite"].append(val_str)
            elif "advis" in key_lower and val_str:
                text_items["advisory"].append(val_str)

    result["prereq_text"] = "; ".join(text_items["prerequisite"])
    result["corequisite_text"] = "; ".join(text_items["corequisite"])
    result["advisory_text"] = "; ".join(text_items["advisory"])

    return result


def _extract_coe_prereq_text(custom_fields) -> str:
    """Extract prerequisite text from top-level customFields (COE fields).

    eLumen stores competency/placement prerequisites in COE (Conditions of
    Enrollment) custom fields on the course, NOT in the requisites block.
    Example: "Intermediate algebra, the equivalent or higher."
    """
    if not isinstance(custom_fields, list):
        return ""

    parts = []
    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        field_id = field.get("customFieldID", "")
        values = field.get("customFieldValue", [])
        if not values or not isinstance(values, list):
            continue

        # COENoncourseOther has the actual text description
        if field_id == "COENoncourseOther":
            for v in values:
                if v and isinstance(v, str) and v.strip():
                    parts.append(v.strip())
        # COESpecified has the condition type (e.g., "Eligibility met to enroll in transfer-level math")
        elif field_id == "COESpecified" and not parts:
            for v in values:
                if v and isinstance(v, str) and v.strip() and "other specified" not in v.lower():
                    parts.append(v.strip())

    return "; ".join(parts)


def _determine_prereq_status(
    prerequisite_courses: list,
    prereq_text: str,
) -> str:
    """Determine prereq_status from structured data and text.

    Returns:
        "has_prereqs" — structured course prereqs found
        "text_only"  — no structured prereqs, but text-based prereqs exist
        "none"       — genuinely no prerequisites
    """
    if prerequisite_courses:
        return "has_prereqs"
    if prereq_text:
        return "text_only"
    return "none"


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

            subj = item.get("subject", "").strip().upper()
            number = item.get("number", "").strip().upper()
            # Use subject and number directly from eLumen (avoids ambiguous concat+resplit)
            code = f"{subj} {number}" if subj and number else normalize_code(f"{subj}{number}")
            title = info.get("title", "") or info.get("courseTitle", "")

            courses.append({
                "code": code,
                "title": title.strip() if title else "",
                "info": info,
            })

        # eLumen caps at 25 per page — if we got 25, there might be more
        if len(items) < 25:
            break
        page += 1
        await asyncio.sleep(REQUEST_DELAY)

    return courses


async def scrape_campus(campus_code: str) -> list[dict]:
    """Scrape all course data for a campus by querying each subject."""
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
    stats = {"has_prereqs": 0, "text_only": 0, "none": 0, "with_desc": 0,
             "with_coreqs": 0, "with_advisories": 0}

    for course in all_courses:
        info = course["info"]
        description = _extract_description(info)
        lecture_hours, lab_hours = _extract_hours(info)

        # Transfer status lives under courseTransferStatus (attributeTypeCode
        # "CB05") as a dict; the human-readable value is valueName, e.g.
        # "Transferable to both UC and CSU".
        transfer_raw = info.get("courseTransferStatus")
        if isinstance(transfer_raw, dict):
            transfer_status = transfer_raw.get("valueName") or ""
        else:
            transfer_status = transfer_raw or ""

        # Topics, SLOs and objectives are all lists of dicts keyed by "name".
        # ("text" is accepted as a fallback in case the schema shifts back.)
        def _names(items: object) -> list[str]:
            out: list[str] = []
            if not isinstance(items, list):
                return out
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = item.get("name") or item.get("text")
                if value:
                    out.append(str(value).strip())
            return out

        topics = _names(info.get("courseTopics"))
        slos = _names(info.get("outcomes"))
        objectives = _names(info.get("objectives"))

        # Extract units from creditsAndHours
        units_min = 0.0
        units_max = 0.0
        credits_hours = info.get("creditsAndHours", [])
        if isinstance(credits_hours, list):
            for entry in credits_hours:
                if isinstance(entry, dict):
                    min_c = entry.get("minimumCredit")
                    max_c = entry.get("maximumCredit")
                    if min_c is not None:
                        try:
                            units_min = max(units_min, float(min_c))
                        except (ValueError, TypeError):
                            pass
                    if max_c is not None:
                        try:
                            units_max = max(units_max, float(max_c))
                        except (ValueError, TypeError):
                            pass
        if units_max == 0.0 and units_min > 0.0:
            units_max = units_min

        # Parse requisites (prereqs, coreqs, advisories + text)
        requisites = info.get("requisites", {})
        parsed = parse_requisites(requisites)

        # Note: top-level customFields have COE (Conditions of Enrollment) text,
        # but these mix actual prerequisites with placement conditions. The catalog
        # is authoritative — e.g., STAT C1000 catalog says "Prerequisite: None" but
        # COE says "Placement as determined by...". We do NOT promote COE text to
        # prereq_text because it would produce incorrect "Prerequisite:" displays.
        # Courses with prereq_status="none" fall through to RAG which has the catalog.

        prereq_status = _determine_prereq_status(
            parsed["prerequisite_courses"],
            parsed["prereq_text"],
        )

        entry = {
            "course_code": course["code"],
            "title": course["title"],
            "description": description,
            "units_min": units_min,
            "units_max": units_max,
            "lecture_hours": lecture_hours,
            "lab_hours": lab_hours,
            "transfer_status": transfer_status,
            "topics": topics,
            "slos": slos,
            "objectives": objectives,
            "prereq_status": prereq_status,
            "prereq_text": parsed["prereq_text"],
            "prerequisite_courses": parsed["prerequisite_courses"],
            "corequisite_courses": parsed["corequisite_courses"],
            "advisory_courses": parsed["advisory_courses"],
            "corequisite_text": parsed["corequisite_text"],
            "advisory_text": parsed["advisory_text"],
        }
        results.append(entry)

        # Stats
        stats[prereq_status] += 1
        if description:
            stats["with_desc"] += 1
        if parsed["corequisite_courses"] or parsed["corequisite_text"]:
            stats["with_coreqs"] += 1
        if parsed["advisory_courses"] or parsed["advisory_text"]:
            stats["with_advisories"] += 1

    logger.info(
        "%s: %d courses — %d with prereqs, %d text-only prereqs, %d no prereqs | "
        "%d with descriptions, %d with coreqs, %d with advisories",
        campus_code, len(results),
        stats["has_prereqs"], stats["text_only"], stats["none"],
        stats["with_desc"], stats["with_coreqs"], stats["with_advisories"],
    )
    return results


async def main():
    parser = argparse.ArgumentParser(description="Scrape full course data from eLumen")
    parser.add_argument("--campus", default="LAMC", help="Campus code or 'all'")
    parser.add_argument(
        "--min-yield",
        type=float,
        default=DEFAULT_MIN_YIELD,
        help=(
            "Fail (exit 1) if any near-universal field is populated on less than this "
            f"fraction of scraped courses. Default {DEFAULT_MIN_YIELD}; pass 0 to disable."
        ),
    )
    args = parser.parse_args()

    out_dir = PROJECT_ROOT / "data" / "elumen_courses"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.campus.upper() == "ALL":
        campuses = list(CAMPUS_TENANTS.keys())
    else:
        campuses = [args.campus.upper()]

    failures = []
    for campus in campuses:
        start = time.time()
        results = await scrape_campus(campus)
        elapsed = time.time() - start
        out_file = out_dir / f"{campus.lower()}_courses.json"
        out_file.write_text(json.dumps(results, indent=2))
        logger.info("Saved %d courses to %s (%.0fs)", len(results), out_file, elapsed)
        failures.extend(check_field_coverage(campus, results, args.min_yield))

    if failures:
        logger.error("FAILED field-coverage check:")
        for f in failures:
            logger.error("  %s", f)
        logger.error(
            "Near-empty output for a field almost always means eLumen renamed the key "
            "rather than that the data is absent — compare against fullCourseInfo in a "
            "live API response. Re-run with --min-yield 0 to override."
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
