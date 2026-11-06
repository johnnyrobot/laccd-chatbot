"""Read-only summary of the academic graph for the /knowledge-graph page.

The page is presented live, so it must never fail because the database did.
Counts and example courses are read from the graph, kept for a while, and
served stale when a refresh fails. With nothing cached the page renders its
structure without numbers.

Every count is grouped by campus: a district total can hide a campus at zero.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.services.neo4j_service import neo4j_service

logger = logging.getLogger(__name__)

CACHE_SECONDS = 900
RETRY_SECONDS = 30
BUILD_TIMEOUT_SECONDS = 15

CAMPUS_CODES = ("ELAC", "LACC", "LAHC", "LAMC", "LAPC", "LASC", "LATTC", "LAVC", "WLAC")

MAX_CHAIN = 3
MAX_UNLOCKS = 3
MAX_GE = 4
MAX_PROGRAMS = 3
MAX_CAREERS = 3

_NODE_COUNTS = """
MATCH (n)
WHERE n.campus_code IS NOT NULL AND (n:Course OR n:Program OR n:Instructor OR n:Subject)
RETURN n.campus_code AS campus, labels(n)[0] AS label, count(*) AS total
"""

_EDGE_COUNTS = """
MATCH (a)-[r]->()
WITH CASE WHEN a:Campus THEN a.code ELSE a.campus_code END AS campus, type(r) AS kind
WHERE campus IS NOT NULL
RETURN campus, kind, count(*) AS total
"""

_SHARED_COUNTS = """
CALL () {
    MATCH (p:Program)-[:LEADS_TO_CAREER]->(c:Career)
    RETURN p.campus_code AS campus, 'careers' AS kind, count(DISTINCT c) AS total
    UNION ALL
    MATCH (c:Course)-[:FULFILLS_GE]->(g:GEArea)
    RETURN c.campus_code AS campus, 'ge_areas' AS kind, count(DISTINCT g) AS total
    UNION ALL
    MATCH (s:Subject)-[:HAS_OPENSTAX_BOOK]->(b:OpenStaxBook)
    RETURN s.campus_code AS campus, 'books' AS kind, count(DISTINCT b) AS total
}
RETURN campus, kind, total
"""

_DISTRICT_SHARED = """
RETURN COUNT { (:Career) } AS careers,
       COUNT { (:GEArea) } AS ge_areas,
       COUNT { (:OpenStaxBook) } AS books
"""

_CANDIDATE_FIELDS = """
WITH cur, collect(DISTINCT old) AS olds
WITH cur, size(olds) AS renumbered, [cur] + olds AS family
RETURN cur.code AS code,
       renumbered,
       coalesce(cur.uc_transferable, false) AS uc,
       COUNT { (:Course)-[:PREREQUISITE_OF]->(cur) } AS before,
       COUNT { (cur)-[:PREREQUISITE_OF]->(:Course) } AS after,
       COUNT { (cur)-[:FULFILLS_GE]->(:GEArea) } AS ge,
       COUNT { MATCH (p:Program)-[:REQUIRES_COURSE]->(c:Course)
               WHERE c IN family RETURN DISTINCT p } AS programs
"""

_CURRENT_COURSE = """
WHERE cur.end_term = '999999' AND cur.title IS NOT NULL
  AND NOT (cur)-[:RENUMBERED_TO]->()
"""

# Every campus has dozens of these, so this is the cheap query that normally
# runs. Scanning all active courses happens only for a campus with none.
_RENUMBERED_CANDIDATES = (
    "MATCH (old:Course {campus_code: $campus})-[:RENUMBERED_TO*1..3]->(cur:Course)"
    + _CURRENT_COURSE
    + _CANDIDATE_FIELDS
)

_ALL_CANDIDATES = (
    "MATCH (cur:Course {campus_code: $campus})"
    + _CURRENT_COURSE
    + "OPTIONAL MATCH (old:Course)-[:RENUMBERED_TO*1..3]->(cur)"
    + _CANDIDATE_FIELDS
)

_WALK_DETAIL = """
MATCH (cur:Course {campus_code: $campus, code: $code})
OPTIONAL MATCH (old:Course)-[:RENUMBERED_TO*1..3]->(cur)
WITH cur, collect(DISTINCT old) AS olds
WITH cur, olds, [cur] + olds AS family
CALL (cur) {
    OPTIONAL MATCH (g:GEArea)<-[:FULFILLS_GE]-(cur)
    RETURN collect(DISTINCT {system: g.system, code: g.code, name: g.name}) AS ge
}
CALL (cur) {
    OPTIONAL MATCH (cur)-[:PREREQUISITE_OF]->(n:Course)
    RETURN collect(DISTINCT {code: n.code, title: n.title}) AS unlocks
}
CALL (cur) {
    OPTIONAL MATCH path = (:Course)-[:PREREQUISITE_OF*1..3]->(cur)
    WITH path WHERE path IS NOT NULL
    RETURN collect([n IN nodes(path) | {code: n.code, title: n.title}])[..60] AS chains
}
CALL (family) {
    OPTIONAL MATCH (p:Program)-[:REQUIRES_COURSE]->(c:Course)
    WHERE c IN family
    RETURN collect(DISTINCT {id: p.master_record_id, title: p.title}) AS programs
}
CALL (family) {
    OPTIONAL MATCH (p:Program)-[:REQUIRES_COURSE]->(c:Course), (p)-[:LEADS_TO_CAREER]->(k:Career)
    WHERE c IN family
    WITH k.title AS title, count(DISTINCT p) AS uses WHERE title IS NOT NULL
    ORDER BY uses DESC, title
    RETURN collect(title) AS careers
}
RETURN cur.code AS code,
       trim(cur.title) AS title,
       coalesce(cur.uc_transferable, false) AS uc,
       coalesce(cur.csu_transferable, false) AS csu,
       [o IN olds | o.code] AS olds,
       COUNT { (:Course)-[:PREREQUISITE_OF]->(cur) } AS before,
       ge, unlocks, chains, programs, careers
"""


def candidate_rank(row: dict[str, Any]) -> tuple:
    """Sort key that puts the course with the most to show first.

    Bigger is better except the final code, which only makes the choice
    repeatable when everything else ties.
    """
    breadth = sum(
        (
            5 if row["renumbered"] else 0,
            4 if row["before"] else 0,
            3 if row["ge"] else 0,
            3 if row["programs"] else 0,
            2 if row["uc"] else 0,
            1 if row["after"] else 0,
        )
    )
    return (
        -breadth,
        -min(row["before"], 2),
        -min(row["programs"], 5),
        -min(row["after"], 3),
        row["code"],
    )


def _code_order(code: str) -> tuple[int, str]:
    """Shortest first, so MATH 261 leads MATH 261S."""
    return (len(code), code)


def best_chain(chains: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    """Longest loop-free prerequisite path, without the course itself.

    Ordered first-taken to last-taken. Support-section codes lose ties because
    their codes are longer.
    """
    usable = [c for c in chains if len({step["code"] for step in c}) == len(c)]
    if not usable:
        return []
    chosen = min(
        usable,
        key=lambda c: (-len(c), sum(len(s["code"]) for s in c), [s["code"] for s in c]),
    )
    return chosen[:-1][-MAX_CHAIN:]


def build_walk(row: dict[str, Any]) -> dict[str, Any]:
    """Shape one course's neighborhood for the page, capping every list."""
    olds = sorted({code for code in row["olds"] if code}, key=_code_order)
    ge_order = {"Cal-GETC": 0, "IGETC": 1, "CSU-GE": 2}
    ge = sorted(
        (g for g in row["ge"] if g.get("code")),
        key=lambda g: (ge_order.get(g["system"], 9), g["code"]),
    )
    unlocks = sorted(
        (u for u in row["unlocks"] if u.get("code")),
        key=lambda u: _code_order(u["code"]),
    )
    programs = sorted(
        (p for p in row["programs"] if p.get("title")),
        key=lambda p: p["title"],
    )
    chain = best_chain(row["chains"])
    return {
        "code": row["code"],
        "title": row["title"],
        "uc": row["uc"],
        "csu": row["csu"],
        "old": olds[0] if olds else None,
        "olds_total": len(olds),
        "chain": chain,
        "unlocks": unlocks[:MAX_UNLOCKS],
        "unlocks_total": len(unlocks),
        "ge": ge[:MAX_GE],
        "ge_total": len(ge),
        # Different awards can share a title; the total still counts each one.
        "programs": list(dict.fromkeys(p["title"] for p in programs))[:MAX_PROGRAMS],
        "programs_total": len(programs),
        "careers": row["careers"][:MAX_CAREERS],
        "careers_total": len(row["careers"]),
        "links": len(olds) + row["before"] + len(unlocks) + len(ge) + len(programs),
    }


async def _load_walk(campus: str) -> dict[str, Any] | None:
    params = {"campus": campus}
    candidates = await neo4j_service.run_query(_RENUMBERED_CANDIDATES, params)
    if not candidates:
        candidates = await neo4j_service.run_query(_ALL_CANDIDATES, params)
    if not candidates:
        return None
    chosen = min(candidates, key=candidate_rank)
    rows = await neo4j_service.run_query(
        _WALK_DETAIL, {"campus": campus, "code": chosen["code"]}
    )
    return build_walk(rows[0]) if rows else None


async def _build_snapshot() -> dict[str, Any]:
    nodes = await neo4j_service.run_query(_NODE_COUNTS)
    edges = await neo4j_service.run_query(_EDGE_COUNTS)
    shared = await neo4j_service.run_query(_SHARED_COUNTS)
    district_shared = await neo4j_service.run_query(_DISTRICT_SHARED)

    campuses: dict[str, dict[str, Any]] = {
        code: {
            "Course": 0, "Program": 0, "Instructor": 0, "Subject": 0,
            "careers": 0, "ge_areas": 0, "books": 0,
            "edges": {}, "connections": 0,
        }
        for code in CAMPUS_CODES
    }
    for row in nodes:
        if row["campus"] in campuses:
            campuses[row["campus"]][row["label"]] = row["total"]
    for row in shared:
        if row["campus"] in campuses:
            campuses[row["campus"]][row["kind"]] = row["total"]
    for row in edges:
        if row["campus"] in campuses:
            campuses[row["campus"]]["edges"][row["kind"]] = row["total"]
            campuses[row["campus"]]["connections"] += row["total"]

    walks = {code: await _load_walk(code) for code in CAMPUS_CODES}

    return {
        "campuses": campuses,
        "walks": walks,
        "district": {
            "courses": sum(c["Course"] for c in campuses.values()),
            "programs": sum(c["Program"] for c in campuses.values()),
            "connections": sum(c["connections"] for c in campuses.values()),
            **(district_shared[0] if district_shared else {}),
        },
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class _SnapshotCache:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.fresh_until = 0.0
        self.lock = asyncio.Lock()

    async def get(self) -> dict[str, Any] | None:
        if time.monotonic() < self.fresh_until:
            return self.value
        if not neo4j_service.is_configured():
            return self.value
        async with self.lock:
            if time.monotonic() < self.fresh_until:
                return self.value
            try:
                self.value = await asyncio.wait_for(
                    _build_snapshot(), BUILD_TIMEOUT_SECONDS
                )
                self.fresh_until = time.monotonic() + CACHE_SECONDS
            except Exception:
                logger.warning("Knowledge graph summary refresh failed", exc_info=True)
                self.fresh_until = time.monotonic() + RETRY_SECONDS
            return self.value


_cache = _SnapshotCache()


async def get_snapshot() -> dict[str, Any] | None:
    """Latest summary, possibly stale; None when the graph has never answered."""
    return await _cache.get()
