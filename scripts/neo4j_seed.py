#!/usr/bin/env python3
"""Seed Neo4j with static data: campuses, terms, and OpenStax books.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/neo4j_seed.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root and backend are on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.services.neo4j_service import neo4j_service  # noqa: E402
from app.services.openstax import OPENSTAX_BASE_URL, OPENSTAX_BOOKS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------
# Static data
# ---------------------------------------------------------------

CAMPUSES = [
    {"code": "LAMC", "name": "Los Angeles Mission College", "address": "13356 Eldridge Ave, Sylmar, CA 91342", "phone": "(818) 364-7600", "assist_code": "004404"},
    {"code": "LAVC", "name": "Los Angeles Valley College", "address": "5800 Fulton Ave, Valley Glen, CA 91401", "phone": "(818) 947-2600", "assist_code": "005546"},
    {"code": "LAPC", "name": "Los Angeles Pierce College", "address": "6201 Winnetka Ave, Woodland Hills, CA 91371", "phone": "(818) 710-4123", "assist_code": "004398"},
    {"code": "LAHC", "name": "Los Angeles Harbor College", "address": "1111 Figueroa Pl, Wilmington, CA 90744", "phone": "(310) 233-4000", "assist_code": "004395"},
    {"code": "LATTC", "name": "Los Angeles Trade-Technical College", "address": "400 W Washington Blvd, Los Angeles, CA 90015", "phone": "(213) 763-7000", "assist_code": "004400"},
    {"code": "LACC", "name": "Los Angeles City College", "address": "855 N Vermont Ave, Los Angeles, CA 90029", "phone": "(323) 953-4000", "assist_code": "004391"},
    {"code": "ELAC", "name": "East Los Angeles College", "address": "1301 Avenida Cesar Chavez, Monterey Park, CA 91754", "phone": "(323) 265-8650", "assist_code": "004296"},
    {"code": "LASC", "name": "Los Angeles Southwest College", "address": "1600 W Imperial Hwy, Los Angeles, CA 90047", "phone": "(323) 241-5225", "assist_code": "004409"},
    {"code": "WLAC", "name": "West Los Angeles College", "address": "9000 Overland Ave, Culver City, CA 90230", "phone": "(310) 287-4200", "assist_code": "004964"},
]

TERMS = [
    {"code": "2258", "name": "Fall 2025", "season": "Fall", "year": 2025},
    {"code": "2262", "name": "Winter 2026", "season": "Winter", "year": 2026},
    {"code": "2264", "name": "Spring 2026", "season": "Spring", "year": 2026},
    {"code": "2266", "name": "Summer 2026", "season": "Summer", "year": 2026},
    {"code": "2268", "name": "Fall 2026", "season": "Fall", "year": 2026},
    {"code": "2272", "name": "Winter 2027", "season": "Winter", "year": 2027},
    {"code": "2274", "name": "Spring 2027", "season": "Spring", "year": 2027},
]

# GE systems and their area names
GE_SYSTEMS: dict[str, dict[str, str]] = {
    "IGETC": {
        "1A": "English Communication", "1B": "Critical Thinking/English Composition",
        "1C": "Oral Communication",
        "2A": "Mathematical Concepts & Quantitative Reasoning",
        "3A": "Arts", "3B": "Humanities",
        "4": "Social and Behavioral Sciences",
        "5A": "Physical Science", "5B": "Biological Science", "5C": "Laboratory Activity",
        "6A": "Language Other Than English",
        "7": "Ethnic Studies",
    },
    "CSU-GE": {
        "A1": "Oral Communication", "A2": "Written Communication", "A3": "Critical Thinking",
        "B1": "Physical Science", "B2": "Life Science", "B3": "Laboratory Activity", "B4": "Mathematics/Quantitative Reasoning",
        "C1": "Arts", "C2": "Humanities",
        "D": "Social Sciences", "D0": "Sociology/Criminology",
        "D1": "Anthropology/Archaeology", "D2": "Economics", "D3": "Ethnic Studies",
        "D4": "Gender Studies", "D5": "Geography", "D6": "History",
        "D7": "Interdisciplinary Social/Behavioral Science", "D8": "Political Science/Government",
        "D9": "Psychology",
        "E": "Lifelong Learning and Self-Development",
        "F": "Ethnic Studies",
    },
    "Cal-GETC": {
        "1A": "English Communication", "1B": "Critical Thinking/English Composition",
        "1C": "Oral Communication",
        "2A": "Mathematical Concepts & Quantitative Reasoning",
        "3A": "Arts", "3B": "Humanities",
        "4": "Social and Behavioral Sciences",
        "5A": "Physical Science", "5B": "Biological Science", "5C": "Laboratory Activity",
        "6A": "Language Other Than English",
        "7": "Ethnic Studies",
        "8A": "Ethnic Studies (CSU)",
    },
}


async def seed_campuses() -> int:
    cypher = """
    UNWIND $campuses AS c
    MERGE (campus:Campus {code: c.code})
    SET campus.name = c.name, campus.address = c.address, campus.phone = c.phone,
        campus.assist_code = c.assist_code
    """
    await neo4j_service.run_write(cypher, {"campuses": CAMPUSES})
    logger.info("Seeded %d Campus nodes", len(CAMPUSES))
    return len(CAMPUSES)


async def seed_terms() -> int:
    cypher = """
    UNWIND $terms AS t
    MERGE (term:Term {code: t.code})
    SET term.name = t.name, term.season = t.season, term.year = t.year
    """
    await neo4j_service.run_write(cypher, {"terms": TERMS})
    logger.info("Seeded %d Term nodes", len(TERMS))
    return len(TERMS)


async def seed_ge_areas() -> int:
    areas = []
    for system, codes in GE_SYSTEMS.items():
        for code, name in codes.items():
            areas.append({"code": code, "name": name, "system": system})
    cypher = """
    UNWIND $areas AS a
    MERGE (ge:GEArea {code: a.code, system: a.system})
    SET ge.name = a.name
    """
    await neo4j_service.run_write(cypher, {"areas": areas})
    logger.info("Seeded %d GEArea nodes", len(areas))
    return len(areas)


async def seed_openstax() -> int:
    books = []
    for subject_code, book_list in OPENSTAX_BOOKS.items():
        for book in book_list:
            books.append({
                "slug": book["slug"],
                "title": book["title"],
                "url": f"{OPENSTAX_BASE_URL}/{book['slug']}",
                "subject_code": subject_code,
            })
    # Create OpenStaxBook nodes
    cypher_books = """
    UNWIND $books AS b
    MERGE (book:OpenStaxBook {slug: b.slug})
    SET book.title = b.title, book.url = b.url
    """
    await neo4j_service.run_write(cypher_books, {"books": books})

    # Create Subject→OpenStaxBook relationships
    # First create Subject nodes for subjects with books
    subject_codes = list(OPENSTAX_BOOKS.keys())
    for code in subject_codes:
        for campus in CAMPUSES:
            await neo4j_service.run_write(
                "MERGE (s:Subject {code: $code, campus_code: $campus})",
                {"code": code, "campus": campus["code"]},
            )

    # Link subjects to books
    cypher_link = """
    UNWIND $books AS b
    MATCH (s:Subject {code: b.subject_code})
    MATCH (book:OpenStaxBook {slug: b.slug})
    MERGE (s)-[:HAS_OPENSTAX_BOOK]->(book)
    """
    await neo4j_service.run_write(cypher_link, {"books": books})
    logger.info("Seeded %d OpenStaxBook nodes with Subject links", len(books))
    return len(books)


async def main() -> None:
    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
        sys.exit(1)

    connected = await neo4j_service.verify_connectivity()
    if not connected:
        logger.error("Cannot connect to Neo4j at %s", settings.neo4j_uri)
        sys.exit(1)
    logger.info("Connected to Neo4j at %s", settings.neo4j_uri)

    await neo4j_service.ensure_schema()
    logger.info("Schema constraints and indexes ensured")

    campuses = await seed_campuses()
    terms = await seed_terms()
    ge_areas = await seed_ge_areas()
    openstax = await seed_openstax()

    logger.info(
        "Seed complete: %d campuses, %d terms, %d GE areas, %d OpenStax books",
        campuses, terms, ge_areas, openstax,
    )
    await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
