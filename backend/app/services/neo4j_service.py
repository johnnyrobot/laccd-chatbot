"""Neo4j graph database service — structured entity/relationship layer.

Provides async access to Neo4j for courses, instructors, programs, GE areas,
cross-references, and prerequisites.  Falls back gracefully when Neo4j is
unavailable (``is_configured()`` returns False).
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver
except ImportError:  # pragma: no cover
    AsyncGraphDatabase = None  # type: ignore[assignment,misc]
    AsyncDriver = None  # type: ignore[assignment,misc]


# LACCD campus codes → ASSIST institution codes
ASSIST_INSTITUTION_CODES: dict[str, str] = {
    "LACC": "004391",
    "LAHC": "004395",
    "LAPC": "004398",
    "LATTC": "004400",
    "LAMC": "004404",
    "LASC": "004409",
    "ELAC": "004296",
    "WLAC": "004964",
    "LAVC": "005546",
}

# Reverse: ASSIST institution code → campus code
INSTITUTION_TO_CAMPUS: dict[str, str] = {v: k for k, v in ASSIST_INSTITUTION_CODES.items()}


class Neo4jService:
    """Async Neo4j driver wrapper with graceful degradation."""

    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None

    def is_configured(self) -> bool:
        return bool(
            AsyncGraphDatabase is not None
            and settings.neo4j_uri
            and settings.neo4j_password
        )

    async def driver(self) -> AsyncDriver | None:
        if self._driver is not None:
            return self._driver
        if not self.is_configured():
            return None
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user or "neo4j", settings.neo4j_password),
        )
        return self._driver

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def verify_connectivity(self) -> bool:
        drv = await self.driver()
        if not drv:
            return False
        try:
            await drv.verify_connectivity()
            return True
        except Exception:
            logger.warning("Neo4j connectivity check failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Generic query helper
    # ------------------------------------------------------------------

    async def run_query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        drv = await self.driver()
        if not drv:
            return []
        async with drv.session() as session:
            result = await session.run(cypher, params or {})
            return [record.data() async for record in result]

    async def run_write(self, cypher: str, params: dict[str, Any] | None = None) -> None:
        drv = await self.driver()
        if not drv:
            return
        async with drv.session() as session:
            await session.run(cypher, params or {})

    # ------------------------------------------------------------------
    # Schema creation (constraints + indexes)
    # ------------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create constraints and indexes if they don't exist."""
        constraints = [
            "CREATE CONSTRAINT campus_code IF NOT EXISTS FOR (c:Campus) REQUIRE c.code IS UNIQUE",
            "CREATE CONSTRAINT term_code IF NOT EXISTS FOR (t:Term) REQUIRE t.code IS UNIQUE",
            "CREATE CONSTRAINT subject_key IF NOT EXISTS FOR (s:Subject) REQUIRE (s.code, s.campus_code) IS UNIQUE",
            "CREATE CONSTRAINT course_key IF NOT EXISTS FOR (c:Course) REQUIRE (c.code, c.campus_code) IS UNIQUE",
            "CREATE CONSTRAINT ge_area_key IF NOT EXISTS FOR (g:GEArea) REQUIRE (g.code, g.system) IS UNIQUE",
            "CREATE CONSTRAINT openstax_slug IF NOT EXISTS FOR (b:OpenStaxBook) REQUIRE b.slug IS UNIQUE",
            "CREATE CONSTRAINT program_id IF NOT EXISTS FOR (p:Program) REQUIRE p.master_record_id IS UNIQUE",
            "CREATE CONSTRAINT program_group_id IF NOT EXISTS FOR (g:ProgramGroup) REQUIRE g.id IS UNIQUE",
            "CREATE CONSTRAINT instructor_key IF NOT EXISTS FOR (i:Instructor) REQUIRE (i.normalized_name, i.campus_code) IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX course_subject_idx IF NOT EXISTS FOR (c:Course) ON (c.subject_code)",
            "CREATE INDEX course_campus_idx IF NOT EXISTS FOR (c:Course) ON (c.campus_code)",
            "CREATE INDEX instructor_name_idx IF NOT EXISTS FOR (i:Instructor) ON (i.normalized_name)",
        ]
        fulltext_indexes = [
            "CREATE FULLTEXT INDEX ft_instructor_name IF NOT EXISTS FOR (i:Instructor) ON EACH [i.display_name, i.normalized_name]",
            "CREATE FULLTEXT INDEX ft_course_title IF NOT EXISTS FOR (c:Course) ON EACH [c.title]",
            "CREATE FULLTEXT INDEX ft_program_title IF NOT EXISTS FOR (p:Program) ON EACH [p.title]",
        ]
        for stmt in constraints + indexes + fulltext_indexes:
            try:
                await self.run_write(stmt)
            except Exception:
                logger.debug("Schema statement skipped (may already exist): %s", stmt[:80])

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    async def resolve_course_subject(self, name_or_code: str, number: str, campus_code: str = "LAMC") -> str:
        """Resolve the actual subject code for a specific course in the graph.

        Handles ASSIST subject code changes: 'ENGLISH' C1000 → 'ENGL' C1000,
        'BIOLOGY' C100 → 'BIOL' C100, etc. Falls back to input if no match.
        """
        upper = name_or_code.upper()
        # Direct match first
        rows = await self.run_query(
            "MATCH (c:Course {subject_code: $subj, number: $num, campus_code: $campus}) RETURN c.subject_code AS code LIMIT 1",
            {"subj": upper, "num": number, "campus": campus_code},
        )
        if rows:
            return rows[0]["code"]
        # Try prefix/suffix matching against courses with this number
        rows = await self.run_query(
            """
            MATCH (c:Course {number: $num, campus_code: $campus})
            WHERE $name STARTS WITH c.subject_code OR c.subject_code STARTS WITH $name
            RETURN c.subject_code AS code
            ORDER BY c.is_current DESC, size(c.subject_code) DESC
            LIMIT 1
            """,
            {"name": upper, "num": number, "campus": campus_code},
        )
        if rows:
            return rows[0]["code"]
        return upper

    async def resolve_course_code(
        self,
        subject: str,
        number: str,
        campus_code: str = "LAMC",
    ) -> tuple[str, str, str | None]:
        """Walk renumbering aliases to find the canonical (subject, number) at a campus.

        Strategy, in order:
          1. Literal — return as-is if a Course node already exists.
          2. C-form alternation — try toggling the leading "C" on the number
             (CHEM 211 ↔ CHEM C211). Pure string heuristic; needs no seeded
             RENUMBERED_TO data.
          3. RENUMBERED_TO chain walk (max 3 hops, both directions).
             Catches cross-subject renumberings like MATH 227 → STAT C1000.

        Returns ``(subject, number, resolved_via)`` where ``resolved_via`` is
        ``None`` when the literal hit, or one of ``"c_form_alternation"`` or
        ``"renumbered_to"``. The original input is returned unchanged when no
        strategy resolved.
        """
        if not self.is_configured():
            return subject.upper(), number, None
        upper_subject = subject.upper()

        literal = await self.run_query(
            """
            MATCH (c:Course {subject_code: $subject, number: $number, campus_code: $campus})
            RETURN c.subject_code AS subject, c.number AS number LIMIT 1
            """,
            {"subject": upper_subject, "number": number, "campus": campus_code},
        )
        if literal:
            return literal[0]["subject"], literal[0]["number"], None

        if number.upper().startswith("C"):
            alt_number = number[1:]
        else:
            alt_number = f"C{number}"
        alt = await self.run_query(
            """
            MATCH (c:Course {subject_code: $subject, number: $alt, campus_code: $campus})
            RETURN c.subject_code AS subject, c.number AS number LIMIT 1
            """,
            {"subject": upper_subject, "alt": alt_number, "campus": campus_code},
        )
        if alt:
            return alt[0]["subject"], alt[0]["number"], "c_form_alternation"

        chain = await self.run_query(
            """
            MATCH (start:Course {subject_code: $subject, number: $number, campus_code: $campus})
            OPTIONAL MATCH p = (start)-[:RENUMBERED_TO*1..3]->(target:Course)
            WHERE NOT (target)-[:RENUMBERED_TO]->()
            WITH coalesce(target, start) AS terminal, length(p) AS depth
            ORDER BY depth DESC
            RETURN terminal.subject_code AS subject, terminal.number AS number LIMIT 1
            """,
            {"subject": upper_subject, "number": number, "campus": campus_code},
        )
        if chain and chain[0].get("subject"):
            row = chain[0]
            if row["subject"] != upper_subject or row["number"] != number:
                return row["subject"], row["number"], "renumbered_to"

        backward = await self.run_query(
            """
            MATCH (q:Course {subject_code: $subject, number: $number, campus_code: $campus})
            OPTIONAL MATCH p = (origin:Course)-[:RENUMBERED_TO*1..3]->(q)
            WITH origin, length(p) AS depth
            WHERE origin IS NOT NULL
            ORDER BY depth ASC
            RETURN origin.subject_code AS subject, origin.number AS number LIMIT 1
            """,
            {"subject": upper_subject, "number": number, "campus": campus_code},
        )
        if backward and backward[0].get("subject"):
            return backward[0]["subject"], backward[0]["number"], "renumbered_to"

        return upper_subject, number, None

    async def find_course_cross_refs(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Look up RENUMBERED_TO relationships for a course (either direction)."""
        cypher = """
        MATCH (a:Course {subject_code: $subject, number: $number, campus_code: $campus})
        OPTIONAL MATCH (a)-[:RENUMBERED_TO]->(b:Course)
        OPTIONAL MATCH (origin:Course)-[:RENUMBERED_TO]->(a)
        WITH collect(DISTINCT {
                new_code: b.code, new_subject: b.subject_code, new_number: b.number,
                title: b.title, old_code: a.code
             }) AS forward,
             collect(DISTINCT {
                new_code: a.code, new_subject: a.subject_code, new_number: a.number,
                title: origin.title, old_code: origin.code
             }) AS reverse
        UNWIND (forward + reverse) AS row
        WITH row WHERE row.new_code IS NOT NULL AND row.old_code IS NOT NULL
        RETURN row.new_code AS new_code, row.new_subject AS new_subject,
               row.new_number AS new_number, row.title AS title, row.old_code AS old_code
        """
        rows = await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})
        if rows:
            return rows
        sub, num, via = await self.resolve_course_code(subject, number, campus_code)
        if via is None:
            return []
        return await self.run_query(
            """
            MATCH (a:Course {subject_code: $subject, number: $number, campus_code: $campus})
            OPTIONAL MATCH (a)-[:RENUMBERED_TO]->(b:Course)
            OPTIONAL MATCH (origin:Course)-[:RENUMBERED_TO]->(a)
            WITH collect(DISTINCT {new_code: b.code, new_subject: b.subject_code, new_number: b.number, title: b.title, old_code: a.code}) AS forward,
                 collect(DISTINCT {new_code: a.code, new_subject: a.subject_code, new_number: a.number, title: origin.title, old_code: origin.code}) AS reverse
            UNWIND (forward + reverse) AS row
            WITH row WHERE row.new_code IS NOT NULL AND row.old_code IS NOT NULL
            RETURN row.new_code AS new_code, row.new_subject AS new_subject,
                   row.new_number AS new_number, row.title AS title, row.old_code AS old_code
            """,
            {"subject": sub, "number": num, "campus": campus_code},
        )

    async def get_course_ge_areas(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get GE areas a course fulfills (walks renumbering chain on miss)."""
        cypher = """
        MATCH (c:Course {subject_code: $subject, number: $number, campus_code: $campus})
              -[:FULFILLS_GE]->(ge:GEArea)
        RETURN ge.code AS ge_code, ge.name AS ge_name, ge.system AS ge_system
        """
        rows = await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})
        if rows:
            return rows
        sub, num, via = await self.resolve_course_code(subject, number, campus_code)
        if via is None:
            return []
        return await self.run_query(cypher, {"subject": sub, "number": num, "campus": campus_code})

    async def get_course_transfer_status(self, subject: str, number: str, campus_code: str = "LAMC") -> dict[str, Any] | None:
        """Check if a course is UC/CSU transferable (walks renumbering chain on miss)."""
        cypher = """
        MATCH (c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN c.uc_transferable AS uc_transferable, c.csu_transferable AS csu_transferable,
               c.code AS code, c.title AS title
        """
        results = await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})
        if results:
            return results[0]
        sub, num, via = await self.resolve_course_code(subject, number, campus_code)
        if via is None:
            return None
        results = await self.run_query(cypher, {"subject": sub, "number": num, "campus": campus_code})
        return results[0] if results else None

    async def find_instructor_courses(self, name: str, campus_code: str | None = None) -> list[dict[str, Any]]:
        """Find courses taught by an instructor (fulltext name search)."""
        campus_filter = "AND i.campus_code = $campus" if campus_code else ""
        cypher = f"""
        CALL db.index.fulltext.queryNodes('ft_instructor_name', $name)
        YIELD node AS i, score
        WHERE score > 0 {campus_filter}
        WITH i, score ORDER BY score DESC LIMIT 5
        MATCH (i)-[:TEACHES]->(c:Course)-[:AT_CAMPUS]->(camp:Campus)
        RETURN DISTINCT c.code AS course_code, c.title AS title,
               camp.code AS campus_code, camp.name AS campus_name,
               i.display_name AS instructor_name, score
        ORDER BY score DESC, c.code
        """
        params: dict[str, Any] = {"name": name}
        if campus_code:
            params["campus"] = campus_code
        return await self.run_query(cypher, params)

    async def search_instructors(self, query: str, campus_code: str | None = None) -> list[dict[str, Any]]:
        """Fulltext search for instructors by name. Returns top matches with courses.

        Tries exact fulltext first, then falls back to Lucene fuzzy matching
        (edit-distance ~1 per word) to handle speech-to-text typos like
        "Stage" → "Dage" or "Curtiss" → "Curtis".
        """
        rows = await self._instructor_fulltext(query, campus_code)
        if not rows:
            # Retry with Lucene fuzzy operators on each word
            fuzzy_query = " ".join(f"{w}~1" for w in query.split() if w)
            rows = await self._instructor_fulltext(fuzzy_query, campus_code)
        if not rows:
            return []
        best = rows[0]["score"]
        return [r for r in rows if r["score"] >= best * 0.5]

    async def _instructor_fulltext(self, query: str, campus_code: str | None = None) -> list[dict[str, Any]]:
        """Run a fulltext query against the instructor name index."""
        campus_filter = "AND i.campus_code = $campus" if campus_code else ""
        cypher = f"""
        CALL db.index.fulltext.queryNodes('ft_instructor_name', $query)
        YIELD node AS i, score
        WHERE score > 0 {campus_filter}
        WITH i, score ORDER BY score DESC LIMIT 10
        OPTIONAL MATCH (i)-[:TEACHES]->(c:Course)
        RETURN i.display_name AS name, i.normalized_name AS normalized_name,
               i.campus_code AS campus_code, score,
               collect(DISTINCT c.code) AS courses,
               collect(DISTINCT c.title) AS course_titles
        """
        params: dict[str, Any] = {"query": query}
        if campus_code:
            params["campus"] = campus_code
        return await self.run_query(cypher, params)

    async def find_course_instructors(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Find instructors who teach a specific course."""
        cypher = """
        MATCH (i:Instructor)-[:TEACHES]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN DISTINCT i.display_name AS name, i.normalized_name AS normalized_name,
               c.code AS course_code, c.title AS course_title
        ORDER BY i.display_name
        """
        return await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})

    async def search_courses_by_title(self, query: str, campus_code: str | None = None) -> list[dict[str, Any]]:
        """Fulltext search for courses by title."""
        campus_filter = "AND node.campus_code = $campus" if campus_code else ""
        cypher = f"""
        CALL db.index.fulltext.queryNodes('ft_course_title', $query)
        YIELD node, score
        WHERE node.is_current = true {campus_filter}
        RETURN node.code AS code, node.title AS title,
               node.subject_code AS subject_code, node.number AS number,
               node.campus_code AS campus_code, score
        ORDER BY score DESC
        LIMIT 10
        """
        params: dict[str, Any] = {"query": query}
        if campus_code:
            params["campus"] = campus_code
        return await self.run_query(cypher, params)

    async def get_prerequisites(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get prerequisites for a course (walks renumbering chain on miss)."""
        cypher = """
        MATCH (prereq:Course)-[r:PREREQUISITE_OF]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN prereq.code AS prereq_code, prereq.title AS prereq_title,
               prereq.subject_code AS prereq_subject, prereq.number AS prereq_number,
               r.type AS req_type, r.group AS req_group
        ORDER BY r.group, prereq.code
        """
        rows = await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})
        if rows:
            return rows
        sub, num, via = await self.resolve_course_code(subject, number, campus_code)
        if via is None:
            return []
        return await self.run_query(cypher, {"subject": sub, "number": num, "campus": campus_code})

    async def get_course_corequisites(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get corequisites for a course."""
        cypher = """
        MATCH (coreq:Course)-[r:COREQUISITE_OF]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN coreq.code AS coreq_code, coreq.title AS coreq_title,
               coreq.subject_code AS coreq_subject, coreq.number AS coreq_number
        ORDER BY coreq.code
        """
        return await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})

    async def get_course_advisories(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get advisories for a course."""
        cypher = """
        MATCH (adv:Course)-[r:ADVISORY_FOR]->(c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN adv.code AS advisory_code, adv.title AS advisory_title,
               adv.subject_code AS advisory_subject, adv.number AS advisory_number
        ORDER BY adv.code
        """
        return await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})

    async def get_course_details(self, subject: str, number: str, campus_code: str = "LAMC") -> dict[str, Any] | None:
        """Get enriched course details (description, hours, topics, SLOs, prereq status)."""
        cypher = """
        MATCH (c:Course {subject_code: $subject, number: $number, campus_code: $campus})
        RETURN c.code AS code, c.title AS title, c.description AS description,
               c.lecture_hours AS lecture_hours, c.lab_hours AS lab_hours,
               c.topics AS topics, c.slos AS slos, c.objectives AS objectives,
               c.prereq_status AS prereq_status, c.prereq_text AS prereq_text,
               c.advisory_text AS advisory_text, c.corequisite_text AS corequisite_text,
               c.units_min AS units_min, c.units_max AS units_max,
               c.uc_transferable AS uc_transferable, c.csu_transferable AS csu_transferable,
               c.transfer_status_elumen AS transfer_status_elumen,
               c.catalog_prereq_text AS catalog_prereq_text,
               c.catalog_coreq_text AS catalog_coreq_text,
               c.catalog_advisory_text AS catalog_advisory_text,
               c.c_id AS c_id,
               c.catalog_transfer AS catalog_transfer,
               c.catalog_lecture_hours AS catalog_lecture_hours,
               c.catalog_lab_hours AS catalog_lab_hours,
               c.cross_listed AS cross_listed,
               c.catalog_description AS catalog_description
        LIMIT 1
        """
        results = await self.run_query(cypher, {"subject": subject.upper(), "number": number, "campus": campus_code})
        return results[0] if results else None

    async def find_programs_requiring_course(self, course_code: str) -> list[dict[str, Any]]:
        """Find programs that require a specific course."""
        cypher = """
        MATCH (p:Program)-[:REQUIRES_COURSE]->(c:Course)
        WHERE c.code = $code
        RETURN p.title AS program_title, p.award_type AS award_type,
               p.campus_code AS campus_code, p.master_record_id AS program_id
        """
        return await self.run_query(cypher, {"code": course_code})

    async def program_snapshot_for_campus(self, campus_code: str) -> list[dict[str, Any]]:
        """Every Program node for a campus, for answering without the vendor API.

        The vendor's WAF refuses the production VPS by IP, so on that box these
        rows are the only Program Mapper data available. They are a weekly
        snapshot replayed from disk by ``scripts/sync_programs_to_neo4j.py``,
        not a live read — callers must label them as such.
        """
        cypher = """
        MATCH (p:Program {campus_code: $campus})
        OPTIONAL MATCH (p)-[:IN_GROUP]->(g:ProgramGroup)
        WITH p, collect(g.title)[0] AS group_title
        RETURN p.master_record_id AS master_record_id,
               p.title AS title,
               p.award_type AS award_type,
               p.description AS description,
               p.catalog_url AS catalog_url,
               p.program_map_id AS program_map_id,
               p.campus_code AS campus_code,
               group_title AS group_title
        ORDER BY p.title
        """
        return await self.run_query(cypher, {"campus": campus_code.upper()})

    async def program_snapshot(self, master_record_id: str) -> dict[str, Any] | None:
        """One program with its requirements and careers, or None if unseeded.

        ``pathway_elements`` is the vendor's ``pathwayElements`` array stored
        verbatim as JSON at seed time, so the caller gets back the exact live
        shape rather than a lossy re-derivation.
        """
        cypher = """
        MATCH (p:Program {master_record_id: $id})
        OPTIONAL MATCH (p)-[:LEADS_TO_CAREER]->(c:Career)
        WITH p, collect(c) AS careers
        RETURN p.master_record_id AS master_record_id,
               p.title AS title,
               p.award_type AS award_type,
               p.description AS description,
               p.catalog_url AS catalog_url,
               p.program_map_id AS program_map_id,
               p.campus_code AS campus_code,
               p.pathway_elements AS pathway_elements,
               [career IN careers WHERE career IS NOT NULL |
                   {title: career.title,
                    avg_salary: career.avg_salary,
                    soc_code: career.soc_code}] AS careers
        """
        results = await self.run_query(cypher, {"id": master_record_id})
        return results[0] if results else None

    async def program_snapshot_heartbeat(self) -> dict[str, Any] | None:
        """When the Program Mapper snapshot was last replayed, or None.

        ``completed_at`` is stored as an ISO-8601 string rather than a Cypher
        ``datetime`` so that reading it back needs no driver type conversion —
        one less place for a freshness check to go quietly wrong.
        """
        cypher = """
        MATCH (s:SyncHeartbeat {source: 'program_mapper'})
        RETURN s.completed_at AS completed_at,
               s.programs AS programs,
               s.careers AS careers,
               s.campuses AS campuses
        """
        results = await self.run_query(cypher)
        return results[0] if results else None

    async def get_ge_courses(self, ge_code: str, system: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get all courses that satisfy a GE area at a campus."""
        cypher = """
        MATCH (c:Course)-[:FULFILLS_GE]->(ge:GEArea {code: $ge_code, system: $system})
        WHERE c.campus_code = $campus AND c.is_current = true
        RETURN c.code AS course_code, c.title AS title, c.units_min AS units_min,
               c.subject_code AS subject_code, c.number AS number
        ORDER BY c.subject_code, c.number
        """
        return await self.run_query(cypher, {"ge_code": ge_code, "system": system, "campus": campus_code})

    async def get_all_renumberings(self, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Get all course renumberings at a campus."""
        cypher = """
        MATCH (a:Course {campus_code: $campus})-[:RENUMBERED_TO]->(b:Course {campus_code: $campus})
        RETURN a.code AS old_code, a.title AS old_title,
               b.code AS new_code, b.title AS new_title,
               a.end_term AS transition_term
        ORDER BY a.end_term DESC
        """
        return await self.run_query(cypher, {"campus": campus_code})

    async def get_course_metadata_bulk(
        self, codes: list[str], campus_code: str = "LAMC",
    ) -> dict[str, dict[str, Any]]:
        """Bulk metadata lookup for a list of course codes at one campus.

        Returns a dict keyed by the *input* code (so callers can match against
        whatever PM gave them). Each value contains:
            - code            current canonical code (== current.code)
            - title
            - units_min, units_max
            - uc_transferable, csu_transferable
            - is_current
            - ge_areas        list of {code, system, name}
            - prerequisites   verified prerequisite course codes
            - catalog_prereq_text
            - corequisites    verified corequisite course codes
            - catalog_coreq_text
            - renumbered_from input code, if RENUMBERED_TO was followed; else None

        Codes not found in Neo4j are simply omitted from the result.
        """
        if not codes:
            return {}
        cypher = """
        UNWIND $codes AS input_code
        MATCH (orig:Course {code: input_code, campus_code: $campus})

        // Walk the renumber chain to its terminal (no outgoing RENUMBERED_TO).
        OPTIONAL MATCH path = (orig)-[:RENUMBERED_TO*1..5]->(t:Course)
        WHERE NOT (t)-[:RENUMBERED_TO]->()
        WITH input_code, orig, t, length(path) AS depth
        ORDER BY depth DESC
        WITH input_code, orig,
             head(collect(coalesce(t, orig))) AS current

        // Pull GE areas off the current node so renumbered courses pick up
        // the new node's GE links (which is what counselors expect today).
        OPTIONAL MATCH (current)-[:FULFILLS_GE]->(ge:GEArea)
        WITH input_code, orig, current,
             [g IN collect(DISTINCT {code: ge.code, system: ge.system, name: ge.name})
                 WHERE g.code IS NOT NULL] AS ge_areas
        OPTIONAL MATCH (prereq:Course)-[:PREREQUISITE_OF]->(current)
        WITH input_code, orig, current, ge_areas,
             [code IN collect(DISTINCT prereq.code) WHERE code IS NOT NULL] AS prerequisites
        OPTIONAL MATCH (coreq:Course)-[:COREQUISITE_OF]->(current)
        WITH input_code, orig, current, ge_areas, prerequisites,
             [code IN collect(DISTINCT coreq.code) WHERE code IS NOT NULL] AS corequisites
        RETURN input_code,
               current.code           AS code,
               current.title          AS title,
               current.units_min      AS units_min,
               current.units_max      AS units_max,
               current.uc_transferable AS uc_transferable,
               current.csu_transferable AS csu_transferable,
               current.is_current     AS is_current,
               ge_areas,
               prerequisites,
               current.catalog_prereq_text AS catalog_prereq_text,
               corequisites,
               current.catalog_coreq_text AS catalog_coreq_text,
               CASE WHEN current.code = orig.code THEN NULL ELSE orig.code END AS renumbered_from
        """
        rows = await self.run_query(
            cypher, {"codes": list(codes), "campus": campus_code},
        )
        result: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            input_code = row.get("input_code")
            if not input_code:
                continue
            result[input_code] = {
                "code": row.get("code"),
                "title": row.get("title"),
                "units_min": row.get("units_min"),
                "units_max": row.get("units_max"),
                "uc_transferable": row.get("uc_transferable"),
                "csu_transferable": row.get("csu_transferable"),
                "is_current": row.get("is_current"),
                "ge_areas": row.get("ge_areas") or [],
                "prerequisites": row.get("prerequisites") or [],
                "catalog_prereq_text": row.get("catalog_prereq_text"),
                "corequisites": row.get("corequisites") or [],
                "catalog_coreq_text": row.get("catalog_coreq_text"),
                "renumbered_from": row.get("renumbered_from"),
            }
        return result

    async def find_programs_by_career(self, career_query: str) -> list[dict[str, Any]]:
        """Find programs that lead to careers matching a query."""
        cypher = """
        MATCH (p:Program)-[:LEADS_TO_CAREER]->(c:Career)
        WHERE toLower(c.title) CONTAINS toLower($query)
        RETURN p.title AS program_title, p.award_type AS award_type,
               p.campus_code AS campus_code, p.master_record_id AS program_id,
               c.title AS career_title, c.avg_salary AS avg_salary
        ORDER BY c.avg_salary DESC
        """
        return await self.run_query(cypher, {"query": career_query})

    async def get_course_enrichment(self, subject: str, number: str, campus_code: str = "LAMC") -> dict[str, Any] | None:
        """Get enrichment data (transfer, GE, renumberings) for a course — for appending to schedule results."""
        transfer = await self.get_course_transfer_status(subject, number, campus_code)
        if not transfer:
            return None
        ge = await self.get_course_ge_areas(subject, number, campus_code)
        return {
            "uc_transferable": transfer.get("uc_transferable"),
            "csu_transferable": transfer.get("csu_transferable"),
            "ge_areas": ge,
        }

    async def find_similar_courses(self, subject: str, number: str, campus_code: str = "LAMC") -> list[dict[str, Any]]:
        """Find courses with similar subject and/or number for 'did you mean?' suggestions.

        Tries multiple strategies: exact subject with similar number, similar subject
        with exact number, stripped C-prefix, etc.
        """
        upper = subject.upper()
        # Strip C-prefix for alternate matching (C211 → 211)
        num_bare = number.lstrip("C") if number.startswith("C") else number
        num_with_c = f"C{num_bare}" if not number.startswith("C") else number
        cypher = """
        MATCH (c:Course {campus_code: $campus})
        WHERE c.is_current = true AND (
            (c.subject_code = $subject AND (
                c.number = $num_bare OR c.number = $num_with_c
                OR c.number STARTS WITH $num_prefix
            ))
            OR ($name STARTS WITH c.subject_code AND (
                c.number = $number OR c.number = $num_bare OR c.number = $num_with_c
            ))
            OR (c.subject_code STARTS WITH $name AND (
                c.number = $number OR c.number = $num_bare OR c.number = $num_with_c
            ))
        )
        RETURN DISTINCT c.code AS code, c.title AS title,
               c.subject_code AS subject_code, c.number AS number
        ORDER BY c.code
        LIMIT 5
        """
        num_prefix = num_bare[:2] if len(num_bare) >= 2 else num_bare
        results = await self.run_query(cypher, {
            "subject": upper,
            "number": number,
            "num_bare": num_bare,
            "num_with_c": num_with_c,
            "num_prefix": num_prefix,
            "name": upper,
            "campus": campus_code,
        })
        if results:
            return results
        # Fulltext fallback: search by title keywords
        title_query = f"{subject} {number}"
        return await self.search_courses_by_title(title_query, campus_code)

    async def get_entity_context(self, query: str, campus_code: str = "LAMC") -> str:
        """Extract entity context from the graph for RAG enrichment.

        Scans the query for course codes, then returns a concise context string
        with structured data (transfer status, GE areas, renumberings, programs)
        to prepend to the Gemini prompt.
        """
        import re
        course_re = re.compile(r'\b([A-Z]{2,}(?:\s+[A-Z]{0,3})?)\s+(C?\d{2,4}[A-Z]{0,2})\b')
        matches = course_re.findall(query)
        if not matches:
            return ""

        context_parts: list[str] = []
        seen_courses: set[str] = set()

        for raw_subject, number in matches[:3]:  # Limit to 3 courses
            subject = raw_subject.strip()
            code = f"{subject} {number}"
            if code in seen_courses:
                continue
            seen_courses.add(code)

            # Resolve subject code
            resolved = await self.resolve_course_subject(subject, number, campus_code)

            # Transfer status
            transfer = await self.get_course_transfer_status(resolved, number, campus_code)
            if not transfer:
                continue

            parts = [f"{transfer['code']} ({transfer['title']})"]

            # Transfer
            t_flags = []
            if transfer.get("uc_transferable"):
                t_flags.append("UC")
            if transfer.get("csu_transferable"):
                t_flags.append("CSU")
            if t_flags:
                parts.append(f"transferable to {', '.join(t_flags)}")
            else:
                parts.append("not UC/CSU transferable")

            # GE areas
            ge = await self.get_course_ge_areas(resolved, number, campus_code)
            if ge:
                ge_strs = [f"{g['ge_system']} Area {g['ge_code']} ({g['ge_name']})" for g in ge[:3]]
                parts.append(f"fulfills {'; '.join(ge_strs)}")

            # Renumberings
            refs = await self.find_course_cross_refs(resolved, number, campus_code)
            if refs:
                new_codes = [r["new_code"] for r in refs[:2]]
                parts.append(f"renumbered to {', '.join(new_codes)}")

            # Programs requiring this course
            programs = await self.find_programs_requiring_course(transfer["code"])
            if programs:
                prog_names = [f"{p['program_title']} ({p['campus_code']})" for p in programs[:3]]
                parts.append(f"required by: {'; '.join(prog_names)}")

            context_parts.append(". ".join(parts) + ".")

        if not context_parts:
            return ""
        return "Verified academic data:\n" + "\n".join(f"- {p}" for p in context_parts)

    async def get_node_counts(self) -> dict[str, int]:
        """Return counts of each node label for health dashboard."""
        cypher = """
        CALL db.labels() YIELD label
        CALL (label) {
            MATCH (n)
            WHERE label IN labels(n)
            RETURN count(n) AS cnt
        }
        RETURN label, cnt
        ORDER BY label
        """
        try:
            rows = await self.run_query(cypher)
            return {r["label"]: r["cnt"] for r in rows}
        except Exception:
            # Fallback for older Neo4j versions
            return {}

    async def get_relationship_counts(self) -> dict[str, int]:
        """Return counts of each relationship type."""
        cypher = """
        CALL db.relationshipTypes() YIELD relationshipType AS type
        CALL (type) {
            MATCH ()-[r]->()
            WHERE type(r) = type
            RETURN count(r) AS cnt
        }
        RETURN type, cnt
        ORDER BY type
        """
        try:
            rows = await self.run_query(cypher)
            return {r["type"]: r["cnt"] for r in rows}
        except Exception:
            return {}


# Module-level singleton
neo4j_service = Neo4jService()
