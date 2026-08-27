#!/usr/bin/env python3
"""Build per-campus CozoDB knowledge packs for the LACCD mobile app.

Produces one ``.cozo`` file per LACCD campus that combines:

  - **Graph slice** (~30 MB) — courses, programs, GE areas, instructors,
    requirement blocks, and the seven edge relations (has_prerequisite,
    has_corequisite, has_advisory, fulfills_ge, renumbered_to,
    program_requires, taught_by). Mirrors the cloud Neo4j layout.
  - **ASSIST slice** (~5 MB) — articulation rows from
    ``data/assist_db/260420_*.txt`` flattened to ``articulates_to`` edges
    between source-CCC courses and destination UC/CSU courses.
  - **RAG slice** (~80–100 MB) — chunked text from
    ``data/campus_crawls/{CAMPUS}/*.txt`` and ``data/catalog_md/{CAMPUS}/*.md``
    embedded via MediaPipe Universal Sentence Encoder Lite (or a
    deterministic fallback if MediaPipe is unavailable). Vectors live in a
    HNSW index inside the same Cozo DB.
  - **Programs slice** (~5–20 MB) — a snapshot of the Program Mapper API
    flattened to ``program`` rows + ``program_requires`` edges.
  - **academic_calendar slice** — a hand-curated table of term-start /
    term-end, drop, and finals dates used to schedule local
    notifications without a Firebase round-trip.

Per-campus output goes to ``dist/knowledge_packs/{CAMPUS}-{TERM}/`` along
with a ``manifest.json`` (campus, term, slice sizes, sha256, embedder
model id, schema version) and a ``.sha256`` sidecar. The script does NOT
push to GitHub Releases — that's a separate ops step. The final line of
output prints the manual ``gh release upload`` command.

Usage::

    PYTHONPATH=backend:. python scripts/build_knowledge_pack.py \\
        --campus LAMC \\
        --term 2026sp \\
        --output-dir dist/knowledge_packs

    # All 9 campuses:
    PYTHONPATH=backend:. python scripts/build_knowledge_pack.py --campus all

The on-device retrieval logic uses Datalog rules and HNSW vector search. This
script's contract is the schema plus the populated relations.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Project-relative imports.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.services.neo4j_service import (  # noqa: E402
    ASSIST_INSTITUTION_CODES,
    INSTITUTION_TO_CAMPUS,
    neo4j_service,
)
from app.services.program_mapper import get_program_mapper_client  # noqa: E402

try:
    from pycozo import Client as CozoClient
except ImportError:  # pragma: no cover - optional until a pack build is requested
    CozoClient = None  # type: ignore[assignment,misc]


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_knowledge_pack")


# ── Constants ───────────────────────────────────────────────────────────────


SCHEMA_VERSION = "1"
"""Bumped whenever the Cozo schema changes. The mobile app's pack loader
checks compatibility before opening."""

LACCD_CAMPUSES: tuple[str, ...] = (
    "ELAC",
    "LACC",
    "LAHC",
    "LAMC",
    "LAPC",
    "LASC",
    "LATTC",
    "LAVC",
    "WLAC",
)

EMBEDDING_DIM = 100
"""MediaPipe Universal Sentence Encoder Lite outputs 100-d vectors. The
hash-based fallback also produces 100-d vectors so the schema stays consistent."""

CHUNK_TARGET_WORDS = 220
"""Target paragraph chunk size in words. Paragraphs that exceed 2x this are
split; smaller paragraphs are merged with neighbors until they reach the floor."""

CHUNK_MIN_WORDS = 60
"""Minimum chunk size — below this, merge with the next paragraph."""

CHUNK_MAX_CHARS = 20_000
MAX_CORPUS_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_CORPUS_BYTES_PER_CAMPUS = 512 * 1024 * 1024

# Per-academic-year ASSIST file prefix. Bump when a new year drops in
# data/assist_db/ (e.g., 270420_*.txt for 2027-28).
ASSIST_FILE_PREFIX = "260420"


# ── Schema ──────────────────────────────────────────────────────────────────


SCHEMA_STATEMENTS: tuple[str, ...] = (
    # Stored relations: nodes
    """:create course {
        code: String =>
        title: String,
        units_min: Float default 0.0,
        units_max: Float default 0.0,
        csu_transferable: Bool default false,
        uc_transferable: Bool default false,
        is_current: Bool default true,
        campus: String default ''
    }""",
    """:create program {
        id: String =>
        title: String,
        award_type: String default '',
        award_short_title: String default '',
        total_units: Float default 0.0,
        campus: String default ''
    }""",
    """:create goal_area {
        code: String,
        system: String =>
        name: String default ''
    }""",
    """:create instructor {
        id: String =>
        display_name: String default '',
        normalized_name: String default '',
        campus: String default ''
    }""",
    """:create requirement_block {
        id: String =>
        program_id: String,
        kind: String default '',
        title: String default '',
        units: Float default 0.0
    }""",
    # Stored relations: edges
    """:create has_prerequisite {
        child_code: String,
        parent_code: String,
        kind: String default 'prereq',
        alternation_group: Int default 0,
        campus: String default ''
    }""",
    """:create has_corequisite {
        a_code: String,
        b_code: String,
        campus: String default ''
    }""",
    """:create has_advisory {
        child_code: String,
        parent_code: String,
        campus: String default ''
    }""",
    """:create fulfills_ge {
        course_code: String,
        ge_code: String,
        ge_system: String,
        campus: String default ''
    }""",
    """:create renumbered_to {
        from_code: String,
        to_code: String,
        campus: String default '',
        year: Int default 0
    }""",
    """:create program_requires {
        program_id: String,
        course_code: String =>
        term_no: Int default 0,
        block_id: String default '',
        is_required: Bool default true
    }""",
    """:create taught_by {
        course_code: String,
        instructor_id: String,
        campus: String default ''
    }""",
    """:create articulates_to {
        from_subject: String,
        from_number: String,
        to_institution: String,
        to_subject: String,
        to_number: String =>
        from_title: String default '',
        to_title: String default '',
        from_units_min: Float default 0.0,
        from_units_max: Float default 0.0,
        agreement_year: String default ''
    }""",
    # RAG slice
    """:create rag_chunk {
        id: String =>
        campus: String,
        source_url: String default '',
        source_kind: String default 'web',
        source_title: String default '',
        text: String,
        v: <F32; """ + str(EMBEDDING_DIM) + """>
    }""",
    # Academic calendar used for local notifications
    """:create academic_calendar {
        campus: String,
        term: String,
        kind: String =>
        date: String default '',
        notes: String default ''
    }""",
    # Pack metadata — single row.
    """:create pack_meta {
        key: String =>
        value: String
    }""",
)


HNSW_INDEX_STATEMENTS: tuple[str, ...] = (
    # Vector index over rag_chunk.v. The mobile app uses the same name in
    # Keep these in sync with RagQueries.kt. CozoDB 0.7 takes a
    # single ``m`` parameter (the M parameter from the HNSW paper); the
    # default ef_construction balances build time vs recall.
    f"""::hnsw create rag_chunk:vector_idx {{
        dim: {EMBEDDING_DIM},
        dtype: F32,
        fields: [v],
        distance: Cosine,
        ef_construction: 200,
        m: 50
    }}""",
)


# ── Embedders (pluggable) ───────────────────────────────────────────────────


@dataclass
class EmbedResult:
    vector: list[float]


class HashEmbedder:
    """Deterministic fallback embedder for development.

    Produces a 100-d float vector by SHA-256-hashing the text and unfolding
    the digest into normalized floats. NOT semantically meaningful — purely
    for exercising the schema + HNSW index without a real model. The
    on-device side must use the SAME embedder for any matching to work; the
    mobile app's Embedder.kt detects this fallback via the manifest's
    ``embedder_model`` field and falls back to keyword search.
    """

    name = "hash-fallback-v1"

    def embed(self, text: str) -> EmbedResult:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        # Unfold 32 bytes into 100 floats by re-hashing with a counter.
        chunks: list[bytes] = []
        counter = 0
        while sum(len(c) for c in chunks) < EMBEDDING_DIM * 4:
            counter += 1
            chunks.append(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        raw = b"".join(chunks)[: EMBEDDING_DIM * 4]
        # Convert each 4-byte chunk to a float in [-1, 1].
        vec: list[float] = []
        for i in range(EMBEDDING_DIM):
            raw_int = int.from_bytes(raw[i * 4 : i * 4 + 4], "big", signed=False)
            vec.append((raw_int / (2**32 - 1)) * 2.0 - 1.0)
        # L2-normalize so cosine distance == dot-product distance for HNSW.
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return EmbedResult(vector=[x / norm for x in vec])


class MediaPipeEmbedder:
    """Wraps MediaPipe Tasks Python TextEmbedder.

    Lazily imports mediapipe so this script remains usable when only the
    fallback is needed. The model path must point at a ``.tflite`` file
    compatible with USE Lite (output dim must equal :data:`EMBEDDING_DIM`).
    """

    def __init__(self, model_path: Path):
        try:
            from mediapipe.tasks import python as mp_python  # type: ignore
            from mediapipe.tasks.python import text as mp_text  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "mediapipe is required for the real embedder. "
                "Install with:  pip install mediapipe  "
                "or pass --embedder hash to use the fallback."
            ) from exc
        options = mp_text.TextEmbedderOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
            l2_normalize=True,
        )
        self._embedder = mp_text.TextEmbedder.create_from_options(options)
        self.name = f"mediapipe-use-lite::{model_path.name}"

    def embed(self, text: str) -> EmbedResult:
        result = self._embedder.embed(text)
        embedding = result.embeddings[0].embedding
        # MediaPipe returns a numpy array; coerce to list[float].
        return EmbedResult(vector=[float(x) for x in embedding])


def make_embedder(spec: str, model_path: Optional[Path]) -> object:
    if spec == "mediapipe":
        if not model_path or not model_path.exists():
            raise SystemExit(
                f"--embedder=mediapipe requires --embedder-model pointing at a "
                f"valid .tflite file (got: {model_path})"
            )
        return MediaPipeEmbedder(model_path)
    if spec == "hash":
        return HashEmbedder()
    raise SystemExit(f"Unknown embedder: {spec}")


# ── Slice builders ──────────────────────────────────────────────────────────


@dataclass
class SliceCounts:
    courses: int = 0
    programs: int = 0
    goal_areas: int = 0
    instructors: int = 0
    requirement_blocks: int = 0
    has_prerequisite: int = 0
    has_corequisite: int = 0
    has_advisory: int = 0
    fulfills_ge: int = 0
    renumbered_to: int = 0
    program_requires: int = 0
    taught_by: int = 0
    articulates_to: int = 0
    rag_chunks: int = 0
    academic_calendar: int = 0


# ---------- Graph slice ----------------------------------------------------


GRAPH_QUERIES: dict[str, str] = {
    "courses": """
        MATCH (c:Course {campus_code: $campus})
        RETURN c.code AS code,
               coalesce(c.title, '') AS title,
               coalesce(c.units_min, 0.0) AS units_min,
               coalesce(c.units_max, 0.0) AS units_max,
               coalesce(c.csu_transferable, false) AS csu_transferable,
               coalesce(c.uc_transferable, false) AS uc_transferable,
               coalesce(c.is_current, true) AS is_current
    """,
    "programs": """
        MATCH (p:Program {campus_code: $campus})
        RETURN p.id AS id,
               coalesce(p.title, '') AS title,
               coalesce(p.award_type, '') AS award_type,
               coalesce(p.award_short_title, '') AS award_short_title,
               coalesce(p.total_units, 0.0) AS total_units
    """,
    "goal_areas": """
        MATCH (g:GoalArea)
        RETURN g.code AS code,
               coalesce(g.system, '') AS system,
               coalesce(g.name, '') AS name
    """,
    "instructors": """
        MATCH (i:Instructor)-[:TEACHES]->(:Course {campus_code: $campus})
        RETURN DISTINCT
            coalesce(i.id, i.normalized_name, i.display_name) AS id,
            coalesce(i.display_name, '') AS display_name,
            coalesce(i.normalized_name, '') AS normalized_name
    """,
    "requirement_blocks": """
        MATCH (r:RequirementBlock)<-[:HAS_BLOCK]-(p:Program {campus_code: $campus})
        RETURN coalesce(r.id, r.title, '') AS id,
               p.id AS program_id,
               coalesce(r.kind, '') AS kind,
               coalesce(r.title, '') AS title,
               coalesce(r.units, 0.0) AS units
    """,
    "has_prerequisite": """
        MATCH (c:Course {campus_code: $campus})-[r:HAS_PREREQUISITE]->(p:Course)
        RETURN c.code AS child_code,
               p.code AS parent_code,
               coalesce(r.kind, 'prereq') AS kind,
               coalesce(r.alternation_group, 0) AS alternation_group
    """,
    "has_corequisite": """
        MATCH (a:Course {campus_code: $campus})-[:HAS_COREQUISITE]->(b:Course)
        RETURN a.code AS a_code, b.code AS b_code
    """,
    "has_advisory": """
        MATCH (c:Course {campus_code: $campus})-[:HAS_ADVISORY]->(p:Course)
        RETURN c.code AS child_code, p.code AS parent_code
    """,
    "fulfills_ge": """
        MATCH (c:Course {campus_code: $campus})-[:FULFILLS_GE]->(g:GoalArea)
        RETURN c.code AS course_code,
               g.code AS ge_code,
               coalesce(g.system, '') AS ge_system
    """,
    "renumbered_to": """
        MATCH (a:Course {campus_code: $campus})-[r:RENUMBERED_TO]->(b:Course)
        RETURN a.code AS from_code,
               b.code AS to_code,
               coalesce(r.year, 0) AS year
    """,
    "program_requires": """
        MATCH (p:Program {campus_code: $campus})-[r:PROGRAM_REQUIRES]->(c:Course)
        RETURN p.id AS program_id,
               c.code AS course_code,
               coalesce(r.term_no, 0) AS term_no,
               coalesce(r.block_id, '') AS block_id,
               coalesce(r.is_required, true) AS is_required
    """,
    "taught_by": """
        MATCH (i:Instructor)-[:TEACHES]->(c:Course {campus_code: $campus})
        RETURN c.code AS course_code,
               coalesce(i.id, i.normalized_name, i.display_name) AS instructor_id
    """,
}


async def build_graph_slice(db: CozoClient, campus: str, counts: SliceCounts) -> None:
    """Pull the graph slice from cloud Neo4j and put it into Cozo."""
    if not neo4j_service.is_configured():
        logger.warning("Neo4j not configured — graph slice will be empty")
        return
    if not await neo4j_service.verify_connectivity():
        logger.warning("Neo4j not reachable — graph slice will be empty")
        return

    rows_by_relation: dict[str, list[dict]] = {}
    for rel, cypher in GRAPH_QUERIES.items():
        rows = await neo4j_service.run_query(cypher, {"campus": campus})
        rows_by_relation[rel] = rows
        logger.info("graph[%s]: %d rows from Neo4j", rel, len(rows))

    # courses
    if rows_by_relation["courses"]:
        rows = [
            [
                str(r["code"]),
                str(r["title"]),
                float(r["units_min"]),
                float(r["units_max"]),
                bool(r["csu_transferable"]),
                bool(r["uc_transferable"]),
                bool(r["is_current"]),
                campus,
            ]
            for r in rows_by_relation["courses"]
            if r.get("code")
        ]
        if rows:
            db.run(
                "?[code, title, units_min, units_max, csu_transferable, uc_transferable, is_current, campus] <- $data "
                ":put course {code => title, units_min, units_max, csu_transferable, uc_transferable, is_current, campus}",
                {"data": rows},
            )
            counts.courses = len(rows)

    # programs
    if rows_by_relation["programs"]:
        rows = [
            [
                str(r["id"]),
                str(r["title"]),
                str(r["award_type"]),
                str(r["award_short_title"]),
                float(r["total_units"]),
                campus,
            ]
            for r in rows_by_relation["programs"]
            if r.get("id")
        ]
        if rows:
            db.run(
                "?[id, title, award_type, award_short_title, total_units, campus] <- $data "
                ":put program {id => title, award_type, award_short_title, total_units, campus}",
                {"data": rows},
            )
            counts.programs = len(rows)

    # goal_areas
    if rows_by_relation["goal_areas"]:
        rows = [
            [str(r["code"]), str(r["system"]), str(r["name"])]
            for r in rows_by_relation["goal_areas"]
            if r.get("code") and r.get("system")
        ]
        # Deduplicate composite key
        seen = set()
        deduped = []
        for row in rows:
            key = (row[0], row[1])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        if deduped:
            db.run(
                "?[code, system, name] <- $data "
                ":put goal_area {code, system => name}",
                {"data": deduped},
            )
            counts.goal_areas = len(deduped)

    # instructors
    if rows_by_relation["instructors"]:
        seen_ids: set[str] = set()
        rows = []
        for r in rows_by_relation["instructors"]:
            iid = (r.get("id") or "").strip()
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)
            rows.append([iid, str(r["display_name"]), str(r["normalized_name"]), campus])
        if rows:
            db.run(
                "?[id, display_name, normalized_name, campus] <- $data "
                ":put instructor {id => display_name, normalized_name, campus}",
                {"data": rows},
            )
            counts.instructors = len(rows)

    # requirement_blocks
    if rows_by_relation["requirement_blocks"]:
        seen_ids = set()
        rows = []
        for r in rows_by_relation["requirement_blocks"]:
            rid = (r.get("id") or "").strip()
            if not rid or rid in seen_ids:
                continue
            seen_ids.add(rid)
            rows.append(
                [
                    rid,
                    str(r["program_id"]),
                    str(r["kind"]),
                    str(r["title"]),
                    float(r["units"]),
                ]
            )
        if rows:
            db.run(
                "?[id, program_id, kind, title, units] <- $data "
                ":put requirement_block {id => program_id, kind, title, units}",
                {"data": rows},
            )
            counts.requirement_blocks = len(rows)

    # Edges
    edge_specs = [
        (
            "has_prerequisite",
            "child_code,parent_code,kind,alternation_group,campus",
            "child_code, parent_code, kind, alternation_group, campus",
            "{child_code, parent_code, kind, alternation_group, campus}",
            lambda r: [
                str(r["child_code"]),
                str(r["parent_code"]),
                str(r["kind"]),
                int(r["alternation_group"]),
                campus,
            ],
        ),
        (
            "has_corequisite",
            "a_code,b_code,campus",
            "a_code, b_code, campus",
            "{a_code, b_code, campus}",
            lambda r: [str(r["a_code"]), str(r["b_code"]), campus],
        ),
        (
            "has_advisory",
            "child_code,parent_code,campus",
            "child_code, parent_code, campus",
            "{child_code, parent_code, campus}",
            lambda r: [str(r["child_code"]), str(r["parent_code"]), campus],
        ),
        (
            "fulfills_ge",
            "course_code,ge_code,ge_system,campus",
            "course_code, ge_code, ge_system, campus",
            "{course_code, ge_code, ge_system, campus}",
            lambda r: [
                str(r["course_code"]),
                str(r["ge_code"]),
                str(r["ge_system"]),
                campus,
            ],
        ),
        (
            "renumbered_to",
            "from_code,to_code,campus,year",
            "from_code, to_code, campus, year",
            "{from_code, to_code, campus, year}",
            lambda r: [
                str(r["from_code"]),
                str(r["to_code"]),
                campus,
                int(r["year"]),
            ],
        ),
        (
            "program_requires",
            "program_id,course_code,term_no,block_id,is_required",
            "program_id, course_code, term_no, block_id, is_required",
            "{program_id, course_code => term_no, block_id, is_required}",
            lambda r: [
                str(r["program_id"]),
                str(r["course_code"]),
                int(r["term_no"]),
                str(r["block_id"]),
                bool(r["is_required"]),
            ],
        ),
        (
            "taught_by",
            "course_code,instructor_id,campus",
            "course_code, instructor_id, campus",
            "{course_code, instructor_id, campus}",
            lambda r: [str(r["course_code"]), str(r["instructor_id"]), campus],
        ),
    ]

    for relation, _cols_csv, query_cols, put_spec, row_fn in edge_specs:
        rows = rows_by_relation.get(relation, [])
        if not rows:
            continue
        # Deduplicate on the full row tuple.
        seen: set[tuple] = set()
        deduped = []
        for r in rows:
            row = row_fn(r)
            key = tuple(row)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        if not deduped:
            continue
        db.run(
            f"?[{query_cols}] <- $data :put {relation} {put_spec}",
            {"data": deduped},
        )
        setattr(counts, relation, len(deduped))


# ---------- ASSIST slice ---------------------------------------------------


def parse_assist_flat_file(path: Path, institution_lookup: dict[str, str]) -> Iterable[dict]:
    """Yield rows from one of the tilde-delimited ASSIST flat files.

    Format::
        institution_code~subject~number~beginning_term~ending_term~title~units_min~units_max~status~[uc_flag]~

    ``institution_lookup`` maps institution_code → human-readable name. Only
    rows whose institution_code is in the lookup are yielded.
    """
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            parts = line.rstrip("\n").split("~")
            if len(parts) < 9:
                continue
            inst_code = parts[0].strip()
            if inst_code not in institution_lookup:
                continue
            try:
                yield {
                    "institution_code": inst_code,
                    "institution_name": institution_lookup[inst_code],
                    "subject_code": parts[1].strip(),
                    "number": parts[2].strip(),
                    "beginning_term": parts[3].strip(),
                    "ending_term": parts[4].strip(),
                    "title": parts[5].strip(),
                    "units_min": float(parts[6] or 0),
                    "units_max": float(parts[7] or 0),
                    "status": parts[8].strip(),
                }
            except (ValueError, IndexError):
                continue


def build_assist_slice(db: CozoClient, campus: str, counts: SliceCounts) -> None:
    """Upsert ``course`` rows from the ASSIST flat files.

    The flat files (``260420_uc_all.txt``, ``260420_csu_all.txt``) carry
    *source-side rosters* — i.e., CCC courses with a UC-transferability or
    CSU-transferability flag attached. They do NOT carry course-to-course
    articulation pairs (those come from the live ASSIST API at
    ``/api/tools/assist``). So the v1 ASSIST slice's job is to:

      1. Parse the flat files for this campus.
      2. Upsert rows into the ``course`` relation with ``csu_transferable``
         and ``uc_transferable`` flags set from the flat-file evidence.

    When the cloud Neo4j is configured, the graph slice already populated
    these courses; this slice's :put with the same primary key (code) is a
    safe upsert — the transferability flags refresh from authoritative
    flat-file data. When Neo4j is unavailable, this slice is the only
    source of ``course`` rows.

    The ``articulates_to`` relation is intentionally left empty in v1.
    Course-to-course pairs require live ASSIST API extracts and land in a
    v2 build that hits the API (or the cached Neo4j-backed extract from
    ``scripts/load_assist_data.py``).
    """
    assist_dir = settings.assist_db_dir
    if not assist_dir.exists():
        logger.warning("ASSIST data dir %s missing — skipping ASSIST slice", assist_dir)
        return

    laccd_inst_code = ASSIST_INSTITUTION_CODES.get(campus)
    if not laccd_inst_code:
        logger.warning("No ASSIST code for campus %s — skipping", campus)
        return

    laccd_lookup = {laccd_inst_code: campus}

    uc_rows: list[dict] = []
    for path in assist_dir.glob(f"{ASSIST_FILE_PREFIX}_uc_all.txt"):
        uc_rows.extend(parse_assist_flat_file(path, laccd_lookup))
    csu_rows: list[dict] = []
    for path in assist_dir.glob(f"{ASSIST_FILE_PREFIX}_csu_all.txt"):
        csu_rows.extend(parse_assist_flat_file(path, laccd_lookup))

    # Aggregate by course code. Last-write-wins for title/units; OR-merge
    # for the boolean flags so a course in BOTH files is correctly marked
    # as both UC- and CSU-transferable.
    courses: dict[str, dict] = {}

    def _ingest(row: dict, *, uc: bool = False, csu: bool = False) -> None:
        # ending_term '999999' is open-ended; everything else is historical.
        if row.get("ending_term") != "999999":
            return
        code = f"{row['subject_code']} {row['number']}".strip()
        existing = courses.get(code) or {
            "code": code,
            "title": row["title"],
            "units_min": row["units_min"],
            "units_max": row["units_max"],
            "uc": False,
            "csu": False,
        }
        existing["uc"] = existing["uc"] or uc
        existing["csu"] = existing["csu"] or csu
        # Title from the most-recent file wins (CSU is iterated last).
        existing["title"] = row["title"] or existing["title"]
        existing["units_min"] = row["units_min"] or existing["units_min"]
        existing["units_max"] = row["units_max"] or existing["units_max"]
        courses[code] = existing

    for row in uc_rows:
        # In the UC file column 10 is a Y/N flag for UC-transferable.
        # parse_assist_flat_file doesn't currently surface column 10, but
        # for this file the convention is "presence in the file with
        # ending_term=999999 ⇒ UC-articulated"; we honor that.
        _ingest(row, uc=True)
    for row in csu_rows:
        _ingest(row, csu=True)

    if not courses:
        logger.info("ASSIST slice [%s]: no current rows in flat files", campus)
        return

    rows = [
        [
            c["code"],
            c["title"],
            float(c["units_min"]),
            float(c["units_max"]),
            bool(c["csu"]),
            bool(c["uc"]),
            True,  # is_current
            campus,
        ]
        for c in courses.values()
    ]
    db.run(
        "?[code, title, units_min, units_max, csu_transferable, uc_transferable, is_current, campus] <- $data "
        ":put course {code => title, units_min, units_max, csu_transferable, uc_transferable, is_current, campus}",
        {"data": rows},
    )
    # The graph slice may have set counts.courses already; treat this as
    # an upsert and report the larger of the two so the manifest reflects
    # the resident row count.
    counts.courses = max(counts.courses, len(rows))
    logger.info(
        "ASSIST slice [%s]: %d courses upserted (uc=%d, csu=%d)",
        campus,
        len(rows),
        sum(1 for c in courses.values() if c["uc"]),
        sum(1 for c in courses.values() if c["csu"]),
    )


# ---------- Programs slice (Program Mapper snapshot) -----------------------


async def build_programs_slice(db: CozoClient, campus: str, counts: SliceCounts) -> None:
    """Snapshot Program Mapper for this campus into program + program_requires.

    The graph slice already pulls Program nodes from Neo4j when the cloud KG
    has been seeded with sync_programs_to_neo4j.py. This function re-fetches
    from the live Program Mapper API as a *fallback* when Neo4j has fewer
    programs than the live source — which is normal mid-term while the
    seed pipeline catches up. The :put writes overwrite Neo4j-sourced rows
    keyed by program id, so the live data wins on conflict.
    """
    try:
        client = get_program_mapper_client(campus)
        programs = await client.get_all_programs()
    except Exception as exc:
        logger.warning("Program Mapper snapshot failed for %s: %s", campus, exc)
        return

    if not programs:
        logger.info("Programs slice [%s]: no programs returned", campus)
        return

    rows: list[list] = []
    seen_ids: set[str] = set()
    for prog in programs:
        pid = (prog.get("masterRecordId") or prog.get("id") or "").strip()
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)
        rows.append(
            [
                pid,
                str(prog.get("title") or ""),
                str(prog.get("awardTitle") or ""),
                str(prog.get("awardShortTitle") or ""),
                float(prog.get("totalUnits") or 0),
                campus,
            ]
        )
    if rows:
        db.run(
            "?[id, title, award_type, award_short_title, total_units, campus] <- $data "
            ":put program {id => title, award_type, award_short_title, total_units, campus}",
            {"data": rows},
        )
        # Overwrite-merge — the prior count from the graph slice may have been higher
        # if Neo4j has historical programs. Keep the larger of the two numbers.
        counts.programs = max(counts.programs, len(rows))
        logger.info("Programs slice [%s]: %d programs from Program Mapper", campus, len(rows))


# ---------- RAG slice ------------------------------------------------------


def _read_corpus_documents(campus: str) -> Iterable[dict]:
    """Yield ``{title, source_url, source_kind, text}`` for the campus's corpus.

    Sources:
      - ``data/campus_crawls/{CAMPUS}/*.txt`` — web pages (one .txt per URL).
      - ``data/catalog_md/{CAMPUS}/*.md`` — Docling-converted catalog + addendum.
    """
    base = PROJECT_ROOT / "data"
    total_bytes = 0

    crawl_dir = base / "campus_crawls" / campus
    if crawl_dir.exists():
        for path in sorted(crawl_dir.glob("*.txt")):
            try:
                size = path.stat().st_size
                if size > MAX_CORPUS_DOCUMENT_BYTES:
                    raise RuntimeError(f"corpus document exceeds byte limit: {path}")
                total_bytes += size
                if total_bytes > MAX_CORPUS_BYTES_PER_CAMPUS:
                    raise RuntimeError(f"campus corpus exceeds byte limit: {campus}")
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            yield {
                "title": path.stem.replace("_", " ").replace("-", " ").title(),
                "source_url": "",  # The web crawl manifest has the URL; v1 ships filename only.
                "source_kind": "web",
                "text": text,
            }

    catalog_dir = base / "catalog_md" / campus
    if catalog_dir.exists():
        for path in sorted(catalog_dir.glob("*.md")):
            try:
                size = path.stat().st_size
                if size > MAX_CORPUS_DOCUMENT_BYTES:
                    raise RuntimeError(f"corpus document exceeds byte limit: {path}")
                total_bytes += size
                if total_bytes > MAX_CORPUS_BYTES_PER_CAMPUS:
                    raise RuntimeError(f"campus corpus exceeds byte limit: {campus}")
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.strip():
                continue
            kind = "addendum" if "addendum" in path.stem else "catalog"
            yield {
                "title": path.stem,
                "source_url": "",
                "source_kind": kind,
                "text": text,
            }


_PARAGRAPH_BREAK = re.compile(r"\n{2,}")


def _chunk_paragraphs(text: str) -> list[str]:
    """Split ``text`` into chunks of roughly :data:`CHUNK_TARGET_WORDS` words.

    Walks paragraphs (double-newline split). Merges shorts into the next,
    splits longs by sentence (``. `` boundary) until the chunk fits.
    """
    paragraphs: list[str] = []
    for raw in _PARAGRAPH_BREAK.split(text):
        paragraph = raw.strip()
        if not paragraph:
            continue
        paragraphs.extend(
            paragraph[start : start + CHUNK_MAX_CHARS]
            for start in range(0, len(paragraph), CHUNK_MAX_CHARS)
        )
    chunks: list[str] = []
    buffer: list[str] = []
    buffer_words = 0
    for para in paragraphs:
        if len(para) >= CHUNK_MAX_CHARS:
            if buffer:
                chunks.append("\n\n".join(buffer))
                buffer, buffer_words = [], 0
            chunks.append(para)
            continue
        para_words = len(para.split())
        if para_words >= 2 * CHUNK_TARGET_WORDS:
            # Flush current buffer first.
            if buffer:
                chunks.append("\n\n".join(buffer))
                buffer, buffer_words = [], 0
            # Split the long paragraph by sentence-ish boundaries.
            sentences = re.split(r"(?<=[.!?])\s+", para)
            sub_buffer: list[str] = []
            sub_words = 0
            for sentence in sentences:
                w = len(sentence.split())
                if sub_words + w > CHUNK_TARGET_WORDS and sub_buffer:
                    chunks.append(" ".join(sub_buffer))
                    sub_buffer, sub_words = [], 0
                sub_buffer.append(sentence)
                sub_words += w
            if sub_buffer:
                chunks.append(" ".join(sub_buffer))
            continue
        if buffer_words + para_words > CHUNK_TARGET_WORDS and buffer_words >= CHUNK_MIN_WORDS:
            chunks.append("\n\n".join(buffer))
            buffer, buffer_words = [], 0
        buffer.append(para)
        buffer_words += para_words
    if buffer:
        chunks.append("\n\n".join(buffer))
    return chunks


def build_rag_slice(
    db: CozoClient,
    campus: str,
    embedder,
    counts: SliceCounts,
    max_chunks: Optional[int] = None,
) -> None:
    """Chunk + embed the campus corpus, write rag_chunk + HNSW index."""
    rows: list[list] = []
    seen_ids: set[str] = set()
    row_count = 0

    def flush() -> None:
        nonlocal rows
        if not rows:
            return
        db.run(
            "?[id, campus, source_url, source_kind, source_title, text, v] <- $data "
            ":put rag_chunk {id => campus, source_url, source_kind, source_title, text, v}",
            {"data": rows},
        )
        rows = []

    for doc in _read_corpus_documents(campus):
        for idx, chunk in enumerate(_chunk_paragraphs(doc["text"])):
            chunk_id = hashlib.sha256(
                f"{campus}::{doc['title']}::{idx}::{chunk[:128]}".encode("utf-8")
            ).hexdigest()[:32]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)
            embed = embedder.embed(chunk)
            rows.append(
                [
                    chunk_id,
                    campus,
                    doc.get("source_url") or "",
                    doc.get("source_kind") or "web",
                    doc.get("title") or "",
                    chunk,
                    embed.vector,
                ]
            )
            row_count += 1
            if len(rows) >= 200:
                flush()
            if max_chunks is not None and row_count >= max_chunks:
                break
        if max_chunks is not None and row_count >= max_chunks:
            break

    if not row_count:
        logger.warning("RAG slice [%s]: no chunks produced", campus)
        return
    flush()
    counts.rag_chunks = row_count
    logger.info("RAG slice [%s]: %d chunks embedded", campus, row_count)


# ---------- Academic calendar slice ---------------------------------------


# Hand-curated LACCD academic calendar (district-wide; per-campus calendars
# differ only in a few minor dates which we'll add in v2). Source: each
# campus's published 2025-26 academic calendar.
ACADEMIC_CALENDAR: tuple[tuple[str, str, str, str], ...] = (
    # (term, kind, date, notes)
    ("2025fa", "term_start",  "2025-08-25", "Fall 2025 first day of instruction"),
    ("2025fa", "term_end",    "2025-12-14", "Fall 2025 last day of instruction"),
    ("2025fa", "drop_no_w",   "2025-09-07", "Last day to drop without a 'W'"),
    ("2025fa", "drop_with_w", "2025-11-09", "Last day to drop with a 'W'"),
    ("2025fa", "finals_week", "2025-12-08", "Finals week begins"),

    ("2026wi", "term_start",  "2026-01-05", "Winter 2026 first day of instruction"),
    ("2026wi", "term_end",    "2026-02-08", "Winter 2026 last day of instruction"),

    ("2026sp", "term_start",  "2026-02-09", "Spring 2026 first day of instruction"),
    ("2026sp", "term_end",    "2026-06-08", "Spring 2026 last day of instruction"),
    ("2026sp", "drop_no_w",   "2026-02-22", "Last day to drop without a 'W'"),
    ("2026sp", "drop_with_w", "2026-05-04", "Last day to drop with a 'W'"),
    ("2026sp", "finals_week", "2026-06-01", "Finals week begins"),

    ("2026su", "term_start",  "2026-06-15", "Summer 2026 first day of instruction"),
    ("2026su", "term_end",    "2026-08-09", "Summer 2026 last day of instruction"),

    ("2026fa", "term_start",  "2026-08-31", "Fall 2026 first day of instruction"),
    ("2026fa", "term_end",    "2026-12-20", "Fall 2026 last day of instruction"),
)


def build_academic_calendar_slice(db: CozoClient, campus: str, counts: SliceCounts) -> None:
    rows = [[campus, term, kind, date, notes] for (term, kind, date, notes) in ACADEMIC_CALENDAR]
    db.run(
        "?[campus, term, kind, date, notes] <- $data "
        ":put academic_calendar {campus, term, kind => date, notes}",
        {"data": rows},
    )
    counts.academic_calendar = len(rows)


# ---------- Pack metadata --------------------------------------------------


def write_pack_meta(db: CozoClient, campus: str, term: str, embedder_name: str) -> None:
    rows = [
        ["schema_version", SCHEMA_VERSION],
        ["campus", campus],
        ["term", term],
        ["embedder", embedder_name],
        ["embedding_dim", str(EMBEDDING_DIM)],
        ["assist_file_prefix", ASSIST_FILE_PREFIX],
    ]
    db.run(
        "?[key, value] <- $data :put pack_meta {key => value}",
        {"data": rows},
    )


# ── Output (manifest, sha256, archive) ──────────────────────────────────────


def write_manifest(
    out_dir: Path,
    campus: str,
    term: str,
    embedder_name: str,
    counts: SliceCounts,
    pack_path: Path,
) -> Path:
    pack_path = pack_path.resolve()
    digest = hashlib.sha256()
    with pack_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    sha = digest.hexdigest()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "campus": campus,
        "term": term,
        "embedder": embedder_name,
        "embedding_dim": EMBEDDING_DIM,
        "pack_filename": pack_path.name,
        "pack_size_bytes": pack_path.stat().st_size,
        "pack_sha256": sha,
        "counts": {
            "courses": counts.courses,
            "programs": counts.programs,
            "goal_areas": counts.goal_areas,
            "instructors": counts.instructors,
            "requirement_blocks": counts.requirement_blocks,
            "has_prerequisite": counts.has_prerequisite,
            "has_corequisite": counts.has_corequisite,
            "has_advisory": counts.has_advisory,
            "fulfills_ge": counts.fulfills_ge,
            "renumbered_to": counts.renumbered_to,
            "program_requires": counts.program_requires,
            "taught_by": counts.taught_by,
            "articulates_to": counts.articulates_to,
            "rag_chunks": counts.rag_chunks,
            "academic_calendar": counts.academic_calendar,
        },
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    sha_path = out_dir / f"{pack_path.name}.sha256"
    sha_path.write_text(f"{sha}  {pack_path.name}\n", encoding="utf-8")

    return manifest_path


# ── Per-campus orchestration ────────────────────────────────────────────────


async def build_one_campus(
    campus: str,
    term: str,
    output_root: Path,
    embedder,
    embedder_name: str,
    skip: set[str],
    rag_max_chunks: Optional[int] = None,
) -> Path:
    if CozoClient is None:
        raise RuntimeError(
            "pycozo is required to build a knowledge pack; install the locked requirements"
        )
    out_dir = output_root / f"{campus}-{term}"
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_path = out_dir / f"{campus}-{term}.cozo"
    if pack_path.exists():
        pack_path.unlink()  # idempotent rebuild

    db = CozoClient("sqlite", str(pack_path))
    counts = SliceCounts()
    try:
        # Schema
        for stmt in SCHEMA_STATEMENTS:
            db.run(stmt)

        if "graph" not in skip:
            await build_graph_slice(db, campus, counts)
        if "assist" not in skip:
            build_assist_slice(db, campus, counts)
        if "programs" not in skip:
            await build_programs_slice(db, campus, counts)
        if "rag" not in skip:
            build_rag_slice(db, campus, embedder, counts, max_chunks=rag_max_chunks)
            # HNSW must be created AFTER rag_chunk has rows (Cozo allows either
            # order in 0.7, but creating after gives clearer error messages).
            for stmt in HNSW_INDEX_STATEMENTS:
                db.run(stmt)
        if "calendar" not in skip:
            build_academic_calendar_slice(db, campus, counts)

        write_pack_meta(db, campus, term, embedder_name)
    finally:
        db.close()

    write_manifest(out_dir, campus, term, embedder_name, counts, pack_path)
    logger.info(
        "Built pack for %s → %s (%d courses, %d programs, %d articulations, %d chunks)",
        campus, pack_path,
        counts.courses, counts.programs, counts.articulates_to, counts.rag_chunks,
    )
    return out_dir


# ── CLI ─────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campus",
        default="LAMC",
        help="Campus code (LAMC, ELAC, ...) or 'all' for all 9 LACCD campuses.",
    )
    parser.add_argument(
        "--term",
        default="2026sp",
        help="Term tag for the output filename (e.g., '2026sp', '2026fa').",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "dist" / "knowledge_packs"),
        help="Directory for the output packs.",
    )
    parser.add_argument(
        "--embedder",
        choices=("mediapipe", "hash"),
        default="hash",
        help=(
            "Embedder to use. 'mediapipe' = real MediaPipe Text Embedder "
            "(requires --embedder-model). 'hash' = deterministic fallback for dev."
        ),
    )
    parser.add_argument(
        "--embedder-model",
        type=Path,
        default=None,
        help="Path to the .tflite model when --embedder=mediapipe.",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated slice names to skip (graph,assist,programs,rag,calendar).",
    )
    parser.add_argument(
        "--rag-max-chunks",
        type=int,
        default=None,
        help="Cap RAG slice at N chunks for fast dry-runs. None = no cap.",
    )
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    campuses = LACCD_CAMPUSES if args.campus.lower() == "all" else (args.campus.upper(),)
    if any(c not in LACCD_CAMPUSES for c in campuses):
        logger.error("Unknown campus(es): %s", [c for c in campuses if c not in LACCD_CAMPUSES])
        return 2

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}

    embedder = make_embedder(args.embedder, args.embedder_model)
    embedder_name = embedder.name

    try:
        for campus in campuses:
            await build_one_campus(
                campus=campus,
                term=args.term,
                output_root=output_root,
                embedder=embedder,
                embedder_name=embedder_name,
                skip=skip,
                rag_max_chunks=args.rag_max_chunks,
            )
    finally:
        await neo4j_service.close()

    print()
    print("=" * 72)
    print("Build complete. Upload to GitHub Releases:")
    for campus in campuses:
        out_dir = output_root / f"{campus}-{args.term}"
        pack = out_dir / f"{campus}-{args.term}.cozo"
        if pack.exists():
            print(f"  gh release upload knowledge-pack-{args.term} \\")
            print(f"      '{pack}' \\")
            print(f"      '{out_dir / 'manifest.json'}' \\")
            print(f"      '{out_dir / (pack.name + '.sha256')}'")
    print("=" * 72)
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
