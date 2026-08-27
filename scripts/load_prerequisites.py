#!/usr/bin/env python3
"""Load prerequisite data from JSON into Neo4j as PREREQUISITE_OF relationships.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/load_prerequisites.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/load_prerequisites.py --file data/prerequisites/lamc_prerequisites.json
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

from app.services.neo4j_service import neo4j_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_FILE = PROJECT_ROOT / "data" / "prerequisites" / "lamc_prerequisites.json"
CAMPUS_CODE = "LAMC"

# Split "ADM JUS 111" -> ("ADM JUS", "111"), "STAT C1000" -> ("STAT", "C1000")
_CODE_SPLIT_RE = re.compile(r'^(.+?)\s+(C?\d+\S*)$')


def split_course_code(code: str) -> tuple[str, str] | None:
    """Split a course code into (subject, number), handling multi-word subjects."""
    m = _CODE_SPLIT_RE.match(code.strip())
    if m:
        return m.group(1), m.group(2)
    return None


async def load_prerequisites(file_path: Path, clear: bool = False) -> None:
    data = json.loads(file_path.read_text())
    logger.info("Loaded %d courses from JSON", len(data))

    if clear:
        # run_write returns None, so reading a count back from it always yielded
        # 0 and this reported "Cleared 0" however many it actually deleted.
        # Count first, then delete.
        match_typed = "MATCH ()-[r:PREREQUISITE_OF]->() WHERE r.type IS NOT NULL"
        rows = await neo4j_service.run_query(f"{match_typed} RETURN count(r) AS n")
        count = rows[0]["n"] if rows else 0
        await neo4j_service.run_write(f"{match_typed} DELETE r")
        logger.info("Cleared %d existing PREREQUISITE_OF relationships", count)

    created = 0
    skipped = 0

    # First pass: mark all courses with their prereq status on the Course node
    # Use run_query to check existence, then run_write to set the property
    marked = 0
    for entry in data:
        course_code = entry["course_code"]
        parsed = split_course_code(course_code)
        if not parsed:
            continue
        subj, num = parsed
        status = "has_prereqs" if entry.get("has_prerequisites") else "none"
        # Match across ALL campuses — prereq status is per-course, not per-campus
        await neo4j_service.run_write(
            "MATCH (c:Course {subject_code: $subj, number: $num}) "
            "SET c.prereq_status = $status",
            {"subj": subj, "num": num, "status": status},
        )
        marked += 1
    logger.info("Marked prereq_status on %d course entries (across all campuses)", marked)

    # Second pass: create PREREQUISITE_OF relationships
    for entry in data:
        if entry.get("has_prerequisites") is False:
            continue
        course_code = entry["course_code"]
        parsed = split_course_code(course_code)
        if not parsed:
            logger.warning("Skipping malformed course code: %s", course_code)
            skipped += 1
            continue

        target_subject, target_number = parsed

        # Verify target course exists
        target = await neo4j_service.run_query(
            "MATCH (c:Course {subject_code: $subj, number: $num, campus_code: $campus}) RETURN c.code AS code LIMIT 1",
            {"subj": target_subject, "num": target_number, "campus": CAMPUS_CODE},
        )
        if not target:
            logger.debug("Target course not in graph: %s", course_code)
            skipped += 1
            continue

        for prereq in entry.get("prerequisite_courses", []):
            prereq_subj = prereq["subject"]
            prereq_num = prereq["number"]
            req_type = prereq.get("type", "required")
            group = prereq.get("group", 0)

            # Verify prereq course exists
            exists = await neo4j_service.run_query(
                "MATCH (c:Course {subject_code: $subj, number: $num, campus_code: $campus}) RETURN c.code LIMIT 1",
                {"subj": prereq_subj, "num": prereq_num, "campus": CAMPUS_CODE},
            )
            if not exists:
                # Try resolving the subject code
                resolved = await neo4j_service.resolve_course_subject(prereq_subj, prereq_num, CAMPUS_CODE)
                exists = await neo4j_service.run_query(
                    "MATCH (c:Course {subject_code: $subj, number: $num, campus_code: $campus}) RETURN c.code LIMIT 1",
                    {"subj": resolved, "num": prereq_num, "campus": CAMPUS_CODE},
                )
                if exists:
                    prereq_subj = resolved
                else:
                    logger.debug("Prereq course not in graph: %s %s", prereq_subj, prereq_num)
                    skipped += 1
                    continue

            await neo4j_service.run_write(
                """
                MATCH (prereq:Course {subject_code: $p_subj, number: $p_num, campus_code: $campus})
                MATCH (target:Course {subject_code: $t_subj, number: $t_num, campus_code: $campus})
                MERGE (prereq)-[r:PREREQUISITE_OF]->(target)
                SET r.type = $type, r.group = $group
                """,
                {
                    "p_subj": prereq_subj, "p_num": prereq_num,
                    "t_subj": target_subject, "t_num": target_number,
                    "campus": CAMPUS_CODE,
                    "type": req_type, "group": group,
                },
            )
            created += 1

    logger.info("Prerequisites loaded: %d relationships created, %d skipped", created, skipped)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load prerequisites into Neo4j")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="Path to prerequisites JSON")
    parser.add_argument("--clear", action="store_true", help="Clear existing prerequisites before loading")
    args = parser.parse_args()

    if not args.file.exists():
        logger.error("File not found: %s", args.file)
        sys.exit(1)

    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
        sys.exit(1)

    connected = await neo4j_service.verify_connectivity()
    if not connected:
        logger.error("Cannot connect to Neo4j")
        sys.exit(1)

    await load_prerequisites(args.file, clear=args.clear)
    await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
