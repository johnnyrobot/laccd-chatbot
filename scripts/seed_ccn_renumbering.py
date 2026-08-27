#!/usr/bin/env python3
"""Seed CCN renumbering RENUMBERED_TO edges from a curated CSV.

Loads `data/ccn_renumbering_pairs.csv` (or a path passed via --csv) and
creates :Course-[:RENUMBERED_TO]->:Course edges in Neo4j. Both endpoints
are MERGEd, so the loader works whether or not the campus catalog already
has the new C-form code as a node.

The walker in `neo4j_service.resolve_course_code` consumes these edges to
map prerequisites and graph lookups across CCN aliases.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/seed_ccn_renumbering.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/seed_ccn_renumbering.py --csv data/ccn_renumbering_pairs.csv --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.services.neo4j_service import ASSIST_INSTITUTION_CODES, neo4j_service  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"from_subject", "from_number", "to_subject", "to_number", "campus_code"}


def _load_rows(csv_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with csv_path.open() as fh:
        clean = (line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        reader = csv.DictReader(clean)
        if reader.fieldnames is None:
            return rows
        missing = REQUIRED_COLUMNS - {c.strip() for c in reader.fieldnames}
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")
        for raw in reader:
            row = {k: (v or "").strip() for k, v in raw.items()}
            if not all(row.get(c) for c in REQUIRED_COLUMNS):
                logger.warning("Skipping row with missing required field: %s", row)
                continue
            rows.append(row)
    return rows


def _expand_campuses(campus_code: str) -> list[str]:
    if campus_code == "*":
        return list(ASSIST_INSTITUTION_CODES.keys())
    return [campus_code]


def _build_payload(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for row in rows:
        for campus in _expand_campuses(row["campus_code"]):
            from_subject = row["from_subject"].upper()
            from_number = row["from_number"]
            to_subject = row["to_subject"].upper()
            to_number = row["to_number"]
            payload.append({
                "from_subject": from_subject,
                "from_number": from_number,
                "from_code": f"{from_subject} {from_number}",
                "to_subject": to_subject,
                "to_number": to_number,
                "to_code": f"{to_subject} {to_number}",
                "campus": campus,
                "effective_term": row.get("effective_term", ""),
                "source_url": row.get("source_url", ""),
                "notes": row.get("notes", ""),
            })
    return payload


async def seed(payload: list[dict[str, str]], dry_run: bool) -> int:
    if not payload:
        logger.info("No rows to seed")
        return 0
    if dry_run:
        for row in payload:
            logger.info(
                "[dry-run] %-6s %-12s -> %-12s (effective=%s)",
                row["campus"], row["from_code"], row["to_code"], row["effective_term"] or "?",
            )
        return len(payload)

    cypher = """
    UNWIND $rows AS r
    MERGE (a:Course {code: r.from_code, campus_code: r.campus})
      ON CREATE SET a.subject_code = r.from_subject, a.number = r.from_number,
                    a.created_via = 'ccn_renumbering_seed'
    MERGE (b:Course {code: r.to_code, campus_code: r.campus})
      ON CREATE SET b.subject_code = r.to_subject, b.number = r.to_number,
                    b.created_via = 'ccn_renumbering_seed'
    MERGE (a)-[rel:RENUMBERED_TO]->(b)
      ON CREATE SET rel.source = 'ccn_seed'
    SET rel.effective_term = CASE WHEN r.effective_term = '' THEN rel.effective_term ELSE r.effective_term END,
        rel.source_url = CASE WHEN r.source_url = '' THEN rel.source_url ELSE r.source_url END,
        rel.notes = CASE WHEN r.notes = '' THEN rel.notes ELSE r.notes END
    """
    await neo4j_service.run_write(cypher, {"rows": payload})
    logger.info("Seeded %d RENUMBERED_TO edges from CCN CSV", len(payload))
    return len(payload)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed CCN renumbering pairs into Neo4j")
    parser.add_argument(
        "--csv",
        default=str(PROJECT_ROOT / "data" / "ccn_renumbering_pairs.csv"),
        help="Path to renumbering CSV (default: data/ccn_renumbering_pairs.csv)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        sys.exit(1)

    rows = _load_rows(csv_path)
    payload = _build_payload(rows)
    logger.info("Loaded %d source rows → %d (campus, pair) edges", len(rows), len(payload))

    try:
        await seed(payload, dry_run=args.dry_run)
    finally:
        await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
