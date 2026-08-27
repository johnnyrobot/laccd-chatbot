"""Sync parsed catalog data from JSON into Neo4j Course nodes.

Reads data/catalog/<campus>_catalog_courses.json and sets catalog_* properties
on matching Course nodes in the graph.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_catalog_to_neo4j.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_catalog_to_neo4j.py --campus LACC
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_catalog_to_neo4j.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure project root and backend on sys.path for imports
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))

from dotenv import load_dotenv

load_dotenv(project_root / ".env")

from app.services.neo4j_service import neo4j_service

CATALOG_DIR = project_root / "data" / "catalog"
BATCH_SIZE = 50

ALL_CAMPUSES = ["LAMC", "LACC", "LAVC", "ELAC", "LAHC", "LAPC", "LASC", "LATTC", "WLAC"]


async def sync_campus(campus_code: str) -> int:
    """Sync catalog data for a single campus. Returns number of courses updated."""
    json_path = CATALOG_DIR / f"{campus_code.lower()}_catalog_courses.json"
    if not json_path.exists():
        print(f"  SKIP {campus_code}: {json_path.name} not found")
        return 0

    with open(json_path) as f:
        courses = json.load(f)

    print(f"\n--- {campus_code}: {len(courses)} courses from {json_path.name} ---")

    # Prepare batch items
    batch_items: list[dict] = []
    for c in courses:
        item = {
            "subject": c["subject"],
            "number": c["number"],
            "prereq_text": c.get("prereq_text"),      # None = not in catalog, "" = "None"
            "coreq_text": c.get("coreq_text") or "",
            "advisory_text": c.get("advisory_text") or "",
            "c_ids": c.get("c_ids") or [],
            "transfer": c.get("transfer") or "",
            "lecture_hours": c.get("lecture_hours"),
            "lab_hours": c.get("lab_hours"),
            "cross_listed": c.get("cross_listed") or "",
            "description": c.get("description") or "",
        }
        batch_items.append(item)

    # Batch UNWIND update — parameterized campus_code
    cypher = """
    UNWIND $batch AS item
    MATCH (c:Course {subject_code: item.subject, number: item.number, campus_code: $campus})
    SET c.catalog_prereq_text = item.prereq_text,
        c.catalog_coreq_text = item.coreq_text,
        c.catalog_advisory_text = item.advisory_text,
        c.c_id = item.c_ids,
        c.catalog_transfer = item.transfer,
        c.catalog_lecture_hours = item.lecture_hours,
        c.catalog_lab_hours = item.lab_hours,
        c.cross_listed = item.cross_listed,
        c.catalog_description = item.description,
        c.catalog_synced = datetime()
    RETURN count(c) AS updated
    """

    total_updated = 0
    for start in range(0, len(batch_items), BATCH_SIZE):
        batch = batch_items[start : start + BATCH_SIZE]
        result = await neo4j_service.run_query(
            cypher, {"batch": batch, "campus": campus_code}
        )
        count = result[0]["updated"] if result else 0
        total_updated += count

    print(f"  Updated: {total_updated} / {len(courses)} courses matched in Neo4j")
    await record_catalog_provenance(campus_code, matched_count=total_updated)
    return total_updated


async def record_catalog_provenance(campus_code: str, matched_count: int) -> None:
    """Stamp which catalog edition this campus is now serving.

    `catalog_synced` records when the sync ran, not which PDF it read, so the
    exact source must come from the parse artifact rather than file mtimes.

    The edition comes from the parse's sidecar, never from ``CATALOG_CONFIGS``.
    Reading the config here would record what the config *claims* is configured,
    and config drifting ahead of the artifacts is exactly the failure this is
    meant to make visible. No sidecar means no edition — recorded as null rather
    than inferred, because a plausible guess is what caused the bad diagnosis.
    """
    meta_path = CATALOG_DIR / f"{campus_code.lower()}_catalog_meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (ValueError, OSError) as exc:
            print(f"  WARNING: could not read {meta_path.name}: {exc}")
    else:
        print(
            f"  WARNING: no {meta_path.name}; recording an unknown edition for "
            f"{campus_code}. Re-run parse_catalog_pdf.py to stamp it."
        )

    await neo4j_service.run_query(
        """
        MERGE (s:CatalogSync {campus_code: $campus})
        SET s.source_pdf = $source_pdf,
            s.edition = $edition,
            s.parsed_at = $parsed_at,
            s.parsed_count = $parsed_count,
            s.matched_count = $matched_count,
            s.synced_at = datetime()
        """,
        {
            "campus": campus_code,
            "source_pdf": meta.get("source_pdf"),
            "edition": meta.get("edition"),
            "parsed_at": meta.get("parsed_at"),
            "parsed_count": meta.get("course_count"),
            "matched_count": matched_count,
        },
    )


async def run(campuses: list[str]) -> None:
    """Sync catalog data for specified campuses."""
    if not neo4j_service.is_configured():
        print("ERROR: Neo4j not configured. Set NEO4J_URI and NEO4J_PASSWORD.")
        sys.exit(1)

    if not await neo4j_service.verify_connectivity():
        print("ERROR: Cannot connect to Neo4j.")
        sys.exit(1)

    grand_total = 0
    for campus_code in campuses:
        updated = await sync_campus(campus_code)
        grand_total += updated

    print(f"\n{'='*50}")
    print(f"Grand total: {grand_total} courses updated across {len(campuses)} campus(es)")

    await neo4j_service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync catalog data to Neo4j")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--campus", type=str, help="Campus code (e.g., LACC)")
    group.add_argument("--all", action="store_true", help="Sync all campuses")
    args = parser.parse_args()

    if args.all:
        campuses = ALL_CAMPUSES
    elif args.campus:
        campus = args.campus.upper()
        if campus not in ALL_CAMPUSES:
            print(f"Unknown campus: {campus}. Available: {', '.join(ALL_CAMPUSES)}")
            sys.exit(1)
        campuses = [campus]
    else:
        campuses = ["LAMC"]  # default

    asyncio.run(run(campuses))


if __name__ == "__main__":
    main()
