"""
Sync eLumen course data from JSON files into Neo4j.

Reads scraped JSON from data/elumen_courses/ (output of scrape_elumen_courses.py)
and batch-loads into Neo4j using UNWIND for speed:

1. Enriches Course node properties (description, hours, topics, SLOs, etc.)
2. Creates PREREQUISITE_OF relationships
3. Creates COREQUISITE_OF relationships
4. Creates ADVISORY_FOR relationships

Supersedes load_prerequisites.py.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_elumen_to_neo4j.py --campus LAMC
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_elumen_to_neo4j.py --campus all
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_elumen_to_neo4j.py --campus LAMC --clear
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = PROJECT_ROOT / "data" / "elumen_courses"

ALL_CAMPUSES = ["LAMC", "LAVC", "LAPC", "LAHC", "LATTC", "LACC", "ELAC", "LASC", "WLAC"]

#: Minimum fraction of a campus's JSON entries that must match a Course node.
#: Enrichment is campus-scoped, so a campus_code mismatch silently updates nothing.
DEFAULT_MIN_YIELD = 0.5

# Split "ADM JUS 111" -> ("ADM JUS", "111"), "STAT C1000" -> ("STAT", "C1000")
_CODE_SPLIT_RE = re.compile(r'^(.+?)\s+(C?\d+\S*)$')


def split_course_code(code: str) -> tuple[str, str] | None:
    """Split a course code into (subject, number), handling multi-word subjects."""
    m = _CODE_SPLIT_RE.match(code.strip())
    if m:
        return m.group(1), m.group(2)
    return None


async def clear_elumen_data(
    neo4j_service, campuses: list[str] | None = None
) -> dict[str, int]:
    """Remove eLumen-sourced relationships and properties.

    ``campuses`` confines the delete to those campus codes. It must match the
    campuses about to be re-synced: sync_from_json writes per campus, so a global
    clear paired with a single-campus sync silently destroys the other eight
    colleges' enriched data and only restores one. Pass None only when every
    campus is being re-synced.

    Only relationships tagged ``source = "elumen"`` are deleted, so rows created
    by load_prerequisites.py (source unset) are never touched.
    """
    stats = {}
    scoped = bool(campuses)
    params = {"campuses": campuses or []}
    scope_desc = ", ".join(campuses) if scoped else "ALL campuses"
    logger.info("Clearing eLumen data for %s", scope_desc)

    for rel_type in ("PREREQUISITE_OF", "COREQUISITE_OF", "ADVISORY_FOR"):
        if scoped:
            query = (
                f"MATCH (a:Course)-[r:{rel_type}]->(b:Course) "
                'WHERE r.source = "elumen" '
                "AND a.campus_code IN $campuses AND b.campus_code IN $campuses "
                "DELETE r RETURN count(r) AS deleted"
            )
        else:
            query = (
                f"MATCH ()-[r:{rel_type}]->() "
                'WHERE r.source = "elumen" DELETE r RETURN count(r) AS deleted'
            )
        result = await neo4j_service.run_query(query, params if scoped else None)
        count = result[0]["deleted"] if result else 0
        stats[rel_type] = count
        logger.info("  Cleared %d %s relationships", count, rel_type)

    # Clear eLumen-set properties
    prop_filter = (
        "WHERE c.elumen_synced IS NOT NULL AND c.campus_code IN $campuses"
        if scoped
        else "WHERE c.elumen_synced IS NOT NULL"
    )
    await neo4j_service.run_write(
        f"""
        MATCH (c:Course) {prop_filter}
        REMOVE c.description, c.lecture_hours, c.lab_hours,
               c.topics, c.slos, c.objectives,
               c.prereq_status, c.prereq_text, c.advisory_text, c.corequisite_text,
               c.transfer_status_elumen, c.elumen_synced
        """,
        params if scoped else None,
    )
    logger.info("  Cleared eLumen properties from Course nodes (%s)", scope_desc)

    return stats


async def sync_from_json(neo4j_service, campus_code: str) -> dict[str, int]:
    """Load course data from JSON file and sync to Neo4j."""
    json_file = DATA_DIR / f"{campus_code.lower()}_courses.json"
    if not json_file.exists():
        logger.error("JSON file not found: %s", json_file)
        logger.error("Run scrape_elumen_courses.py --campus %s first", campus_code)
        return {"error": 1}

    data = json.loads(json_file.read_text())
    logger.info("Loaded %d courses from %s", len(data), json_file.name)

    stats = {"courses_enriched": 0, "prerequisites": 0, "corequisites": 0,
             "advisories": 0, "skipped": 0}

    # --- Phase 1: Batch-enrich Course node properties ---
    enrich_batch = []
    for entry in data:
        parsed = split_course_code(entry["course_code"])
        if not parsed:
            stats["skipped"] += 1
            continue
        subject, number = parsed
        enrich_batch.append({
            "subject": subject,
            "number": number,
            "description": entry.get("description", ""),
            "lecture_hours": entry.get("lecture_hours", 0.0),
            "lab_hours": entry.get("lab_hours", 0.0),
            "topics": entry.get("topics", []),
            "slos": entry.get("slos", []),
            "objectives": entry.get("objectives", []),
            "prereq_status": entry.get("prereq_status", "none"),
            "prereq_text": entry.get("prereq_text", ""),
            "advisory_text": entry.get("advisory_text", ""),
            "corequisite_text": entry.get("corequisite_text", ""),
            "transfer_status": entry.get("transfer_status", ""),
        })

    # Scoped to the campus being synced. This previously matched across ALL
    # campuses — a stopgap from when only LAMC had been scraped — which wrote
    # one college's descriptions, SLOs, objectives, hours and transfer status
    # onto every other college's same-numbered course. Now that all 9 campuses
    # are scraped, each gets its own data.
    BATCH_SIZE = 100
    for i in range(0, len(enrich_batch), BATCH_SIZE):
        batch = enrich_batch[i:i + BATCH_SIZE]
        result = await neo4j_service.run_query(
            """
            UNWIND $batch AS item
            MATCH (c:Course {subject_code: item.subject, number: item.number,
                             campus_code: $campus})
            SET c.description = item.description,
                c.lecture_hours = item.lecture_hours,
                c.lab_hours = item.lab_hours,
                c.topics = item.topics,
                c.slos = item.slos,
                c.objectives = item.objectives,
                c.prereq_status = item.prereq_status,
                c.prereq_text = item.prereq_text,
                c.advisory_text = item.advisory_text,
                c.corequisite_text = item.corequisite_text,
                c.transfer_status_elumen = item.transfer_status,
                c.elumen_synced = datetime()
            RETURN count(c) AS updated
            """,
            {"batch": batch, "campus": campus_code},
        )
        updated = result[0]["updated"] if result else 0
        stats["courses_enriched"] += updated

    # Denominator for the caller's yield check: entries that had a parseable
    # course code and so were eligible to match a Course node.
    stats["entries_considered"] = len(enrich_batch)
    logger.info("  Enriched %d Course nodes (from %d entries)", stats["courses_enriched"], len(enrich_batch))

    # --- Phase 2: Create PREREQUISITE_OF relationships ---
    prereq_rels = []
    for entry in data:
        parsed = split_course_code(entry["course_code"])
        if not parsed:
            continue
        target_subject, target_number = parsed
        for prereq in entry.get("prerequisite_courses", []):
            prereq_rels.append({
                "p_subject": prereq["subject"],
                "p_number": prereq["number"],
                "t_subject": target_subject,
                "t_number": target_number,
                "group": prereq.get("group", 0),
            })

    for i in range(0, len(prereq_rels), BATCH_SIZE):
        batch = prereq_rels[i:i + BATCH_SIZE]
        result = await neo4j_service.run_query(
            """
            UNWIND $batch AS item
            MATCH (prereq:Course {subject_code: item.p_subject, number: item.p_number,
                                  campus_code: $campus})
            MATCH (target:Course {subject_code: item.t_subject, number: item.t_number,
                                  campus_code: $campus})
            MERGE (prereq)-[r:PREREQUISITE_OF]->(target)
            ON CREATE SET r.source = "elumen"
            SET r.group = item.group
            RETURN count(r) AS upserted
            """,
            {"batch": batch, "campus": campus_code},
        )
        upserted = result[0]["upserted"] if result else 0
        stats["prerequisites"] += upserted

    logger.info(
        "  Upserted %d PREREQUISITE_OF relationships (from %d entries; MERGE, so this\n"
        "    counts pre-existing rows too — only new ones get source=elumen)",
        stats["prerequisites"], len(prereq_rels),
    )

    # --- Phase 3: Create COREQUISITE_OF relationships ---
    coreq_rels = []
    for entry in data:
        parsed = split_course_code(entry["course_code"])
        if not parsed:
            continue
        target_subject, target_number = parsed
        for coreq in entry.get("corequisite_courses", []):
            coreq_rels.append({
                "c_subject": coreq["subject"],
                "c_number": coreq["number"],
                "t_subject": target_subject,
                "t_number": target_number,
            })

    for i in range(0, len(coreq_rels), BATCH_SIZE):
        batch = coreq_rels[i:i + BATCH_SIZE]
        result = await neo4j_service.run_query(
            """
            UNWIND $batch AS item
            MATCH (coreq:Course {subject_code: item.c_subject, number: item.c_number,
                                 campus_code: $campus})
            MATCH (target:Course {subject_code: item.t_subject, number: item.t_number,
                                  campus_code: $campus})
            MERGE (coreq)-[r:COREQUISITE_OF]->(target)
            ON CREATE SET r.source = "elumen"
            RETURN count(r) AS upserted
            """,
            {"batch": batch, "campus": campus_code},
        )
        upserted = result[0]["upserted"] if result else 0
        stats["corequisites"] += upserted

    logger.info(
        "  Upserted %d COREQUISITE_OF relationships (from %d entries; MERGE, so this\n"
        "    counts pre-existing rows too — only new ones get source=elumen)",
        stats["corequisites"], len(coreq_rels),
    )

    # --- Phase 4: Create ADVISORY_FOR relationships ---
    advisory_rels = []
    for entry in data:
        parsed = split_course_code(entry["course_code"])
        if not parsed:
            continue
        target_subject, target_number = parsed
        for advisory in entry.get("advisory_courses", []):
            advisory_rels.append({
                "a_subject": advisory["subject"],
                "a_number": advisory["number"],
                "t_subject": target_subject,
                "t_number": target_number,
            })

    for i in range(0, len(advisory_rels), BATCH_SIZE):
        batch = advisory_rels[i:i + BATCH_SIZE]
        result = await neo4j_service.run_query(
            """
            UNWIND $batch AS item
            MATCH (adv:Course {subject_code: item.a_subject, number: item.a_number,
                               campus_code: $campus})
            MATCH (target:Course {subject_code: item.t_subject, number: item.t_number,
                                  campus_code: $campus})
            MERGE (adv)-[r:ADVISORY_FOR]->(target)
            ON CREATE SET r.source = "elumen"
            RETURN count(r) AS upserted
            """,
            {"batch": batch, "campus": campus_code},
        )
        upserted = result[0]["upserted"] if result else 0
        stats["advisories"] += upserted

    logger.info(
        "  Upserted %d ADVISORY_FOR relationships (from %d entries; MERGE, so this\n"
        "    counts pre-existing rows too — only new ones get source=elumen)",
        stats["advisories"], len(advisory_rels),
    )

    return stats


async def main():
    parser = argparse.ArgumentParser(description="Sync eLumen course data to Neo4j from JSON files")
    parser.add_argument("--campus", type=str, default="LAMC", help="Campus code or 'all'. Default: LAMC")
    parser.add_argument("--clear", action="store_true", help="Clear existing eLumen data before loading")
    parser.add_argument(
        "--min-yield",
        type=float,
        default=DEFAULT_MIN_YIELD,
        help=(
            "Fail (exit 1) if a campus enriches less than this fraction of the courses "
            f"in its JSON. Default {DEFAULT_MIN_YIELD}; pass 0 to disable."
        ),
    )
    args = parser.parse_args()

    from app.services.neo4j_service import neo4j_service

    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI and NEO4J_PASSWORD.")
        sys.exit(1)

    ok = await neo4j_service.verify_connectivity()
    if not ok:
        logger.error("Cannot connect to Neo4j.")
        sys.exit(1)

    campuses = ALL_CAMPUSES if args.campus.upper() == "ALL" else [args.campus.upper()]

    if args.clear:
        # Scope the clear to exactly the campuses about to be re-synced. Passing
        # nothing here would delete every campus's eLumen data and then restore
        # only the ones in `campuses`.
        await clear_elumen_data(neo4j_service, campuses)

    total_stats: dict[str, int] = {}
    failures = []
    for campus in campuses:
        if campus not in ALL_CAMPUSES:
            logger.warning("Unknown campus code: %s, skipping", campus)
            continue
        stats = await sync_from_json(neo4j_service, campus)
        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v
        logger.info("  %s done: %s", campus, stats)

        # Now that enrichment is campus-scoped, a campus_code that matches nothing
        # in the graph enriches 0 rows and still exits 0. Catch that.
        considered = stats.get("entries_considered", 0)
        if args.min_yield > 0 and considered:
            ratio = stats.get("courses_enriched", 0) / considered
            if ratio < args.min_yield:
                failures.append(
                    f"{campus}: enriched {stats.get('courses_enriched', 0)}/{considered} "
                    f"= {ratio:.0%} (below {args.min_yield:.0%})"
                )

    logger.info("Sync complete. Totals: %s", total_stats)

    await neo4j_service.close()

    if failures:
        logger.error("FAILED enrichment-yield check:")
        for f in failures:
            logger.error("  %s", f)
        logger.error(
            "A near-zero enrichment rate usually means the campus_code in the JSON does "
            "not match campus_code on the Course nodes, so the scoped MATCH found "
            "nothing. Re-run with --min-yield 0 to override."
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
