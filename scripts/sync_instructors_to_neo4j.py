#!/usr/bin/env python3
"""Sync instructor-course mappings from the LACCD Schedule API into Neo4j.

Pulls schedule data for current terms across all 9 campuses, then creates
Instructor nodes and TEACHES relationships to Course nodes in the graph.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_instructors_to_neo4j.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/sync_instructors_to_neo4j.py --campus LAMC --term 2264
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.services.live_schedule import (  # noqa: E402
    ALL_CAMPUSES,
    CAMPUS_NAMES,
    get_class_listing,
)
from app.services.neo4j_service import neo4j_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 200

# Current terms to sync
DEFAULT_TERMS = ["2264", "2266"]  # Spring 2026, Summer 2026


def normalize_instructor_name(name: str) -> str:
    """Normalize instructor name for consistent matching.

    'Smith, J' → 'smith j'
    'De La Cruz, Maria' → 'de la cruz maria'
    """
    name = name.strip()
    if not name or name == "TBA" or name == "Staff":
        return ""
    # Remove extra whitespace and lowercase
    return re.sub(r"\s+", " ", name.lower().strip())


def extract_instructor_course_pairs(listing: dict, campus_code: str) -> list[dict]:
    """Extract unique instructor → course mappings from a schedule listing."""
    pairs: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for subject in listing.get("subjects", []):
        for course in subject.get("courses", []):
            course_code = f"{course.get('subject', '')} {course.get('catalogNbr', '')}"
            course_title = course.get("descr", "")
            for section in course.get("sections", []):
                for meeting in section.get("meetings", []):
                    raw_name = meeting.get("instr", "TBA")
                    if not raw_name or raw_name in ("TBA", "Staff", ""):
                        continue
                    normalized = normalize_instructor_name(raw_name)
                    if not normalized:
                        continue
                    key = (normalized, course_code)
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs.append({
                        "instructor_name": raw_name.strip(),
                        "normalized_name": normalized,
                        "course_code": course_code,
                        "course_title": course_title,
                        "campus_code": campus_code,
                        "subject_code": course.get("subject", ""),
                        "number": course.get("catalogNbr", ""),
                    })
    return pairs


async def sync_campus_term(campus_code: str, term: str) -> int:
    """Sync instructor data for one campus and one term."""
    try:
        listing = await get_class_listing(campus_code, term)
    except Exception as exc:
        logger.warning("Failed to fetch schedule for %s/%s: %s", campus_code, term, exc)
        return 0

    pairs = extract_instructor_course_pairs(listing, campus_code)
    if not pairs:
        logger.info("No instructor data for %s term %s", campus_code, term)
        return 0

    # Batch upsert instructors and TEACHES relationships
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i : i + BATCH_SIZE]
        await neo4j_service.run_write(
            """
            UNWIND $pairs AS p
            MERGE (i:Instructor {normalized_name: p.normalized_name, campus_code: p.campus_code})
            ON CREATE SET i.display_name = p.instructor_name
            WITH i, p
            MATCH (c:Course {code: p.course_code, campus_code: p.campus_code})
            MERGE (i)-[:TEACHES]->(c)
            """,
            {"pairs": batch},
        )

    # Also create TEACHES_AT relationships to campus
    await neo4j_service.run_write(
        """
        MATCH (i:Instructor {campus_code: $campus}), (camp:Campus {code: $campus})
        WHERE NOT (i)-[:TEACHES_AT]->(camp)
        CREATE (i)-[:TEACHES_AT]->(camp)
        """,
        {"campus": campus_code},
    )

    logger.info("Synced %d instructor-course pairs for %s term %s", len(pairs), campus_code, term)
    return len(pairs)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync instructors from LACCD Schedule API to Neo4j")
    parser.add_argument("--campus", help="Sync only a specific campus (e.g., LAMC)")
    parser.add_argument("--term", help="Sync only a specific term code (e.g., 2264)")
    args = parser.parse_args()

    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
        sys.exit(1)

    connected = await neo4j_service.verify_connectivity()
    if not connected:
        logger.error("Cannot connect to Neo4j")
        sys.exit(1)
    logger.info("Connected to Neo4j")

    campuses = [args.campus.upper()] if args.campus else ALL_CAMPUSES
    terms = [args.term] if args.term else DEFAULT_TERMS

    total = 0
    for term in terms:
        for campus in campuses:
            count = await sync_campus_term(campus, term)
            total += count

    logger.info("Instructor sync complete: %d total pairs across %d campus(es), %d term(s)",
                total, len(campuses), len(terms))
    await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
