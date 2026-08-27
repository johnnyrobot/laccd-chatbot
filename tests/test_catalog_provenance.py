"""Catalog provenance comes from the parsed artifact, never configuration guesses
or file modification times. Unknown editions remain explicitly unknown.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.services import neo4j_service as neo4j_module
from app.services.catalog_provenance import (
    catalog_health_dict,
    campus_catalog_provenance,
)


def _row(**over):
    base = {
        "campus_code": "LAMC",
        "source_pdf": "LAMC-Catalog-2026-2027.pdf",
        "edition": "2026-2027",
        "parsed_at": "2026-08-18T09:00:00+00:00",
        "synced_at": "2026-08-18T09:05:00+00:00",
        "parsed_count": 848,
        "matched_count": 700,
    }
    base.update(over)
    return base


def _stub(monkeypatch, row):
    async def fake_run_query(cypher, params=None):
        if "CatalogSync" not in cypher:
            return []
        return [row] if row else []

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", fake_run_query)


# ------------------------------------------------------------------ reading


def test_provenance_reports_the_edition_actually_parsed(monkeypatch):
    _stub(monkeypatch, _row())
    prov = asyncio.run(campus_catalog_provenance("LAMC"))

    assert prov.recorded is True
    assert prov.edition == "2026-2027"
    assert prov.source_pdf == "LAMC-Catalog-2026-2027.pdf"
    assert prov.parsed_count == 848
    assert prov.matched_count == 700


def test_an_unrecorded_campus_is_unknown_not_fine(monkeypatch):
    """The whole point: absence of provenance must be visible as absence."""
    _stub(monkeypatch, None)
    prov = asyncio.run(campus_catalog_provenance("LAMC"))

    assert prov.recorded is False
    assert prov.edition is None
    assert prov.parsed_count is None


def test_a_broken_graph_read_does_not_raise(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", boom)
    assert asyncio.run(campus_catalog_provenance("LAMC")).recorded is False


def test_health_surfaces_the_edition_and_both_counts(monkeypatch):
    _stub(monkeypatch, _row())
    health = catalog_health_dict(asyncio.run(campus_catalog_provenance("LAMC")))

    assert health["edition"] == "2026-2027"
    assert health["source_pdf"] == "LAMC-Catalog-2026-2027.pdf"
    assert health["parsed_count"] == 848
    assert health["matched_count"] == 700
    # 848 parsed vs 700 matched is the gap nothing used to compare.
    assert health["unmatched_count"] == 148


def test_health_says_unknown_rather_than_inventing_an_edition(monkeypatch):
    _stub(monkeypatch, None)
    health = catalog_health_dict(asyncio.run(campus_catalog_provenance("LAMC")))

    assert health["recorded"] is False
    assert health["edition"] is None


# ------------------------------------------------------------------ writing


def test_the_parse_writes_a_sidecar_naming_its_source(tmp_path, monkeypatch):
    """The artifact records what it came from, so the sync never has to guess."""
    import scripts.parse_catalog_pdf as parse

    monkeypatch.setattr(parse, "OUT_DIR", tmp_path)
    parse.write_output("LAMC", [{"course_code": "MATH 261"}], "LAMC-Catalog-2026-2027.pdf")

    meta = json.loads((tmp_path / "lamc_catalog_meta.json").read_text())
    assert meta["source_pdf"] == "LAMC-Catalog-2026-2027.pdf"
    assert meta["edition"] == "2026-2027"
    assert meta["course_count"] == 1
    assert meta["parsed_at"]
    # The courses file keeps its bare-list shape; several scripts read it.
    assert isinstance(json.loads((tmp_path / "lamc_catalog_courses.json").read_text()), list)


def test_a_pdf_with_no_edition_in_its_name_is_recorded_as_unknown(tmp_path, monkeypatch):
    """WLAC has shipped a catalog simply named "catalog.pdf"."""
    import scripts.parse_catalog_pdf as parse

    monkeypatch.setattr(parse, "OUT_DIR", tmp_path)
    parse.write_output("WLAC", [{"course_code": "ACCTG 001"}], "catalog.pdf")

    meta = json.loads((tmp_path / "wlac_catalog_meta.json").read_text())
    assert meta["source_pdf"] == "catalog.pdf"
    assert meta["edition"] is None, "an unparseable name must not be given a plausible edition"


def test_the_sync_records_provenance_from_the_sidecar_not_the_config(tmp_path, monkeypatch):
    """If the artifact and CATALOG_CONFIGS disagree, the artifact is the truth —
    that disagreement is exactly what produced the wrong answer in #59."""
    import scripts.sync_catalog_to_neo4j as sync

    (tmp_path / "lamc_catalog_courses.json").write_text(json.dumps([]))
    (tmp_path / "lamc_catalog_meta.json").write_text(json.dumps({
        "campus_code": "LAMC",
        "source_pdf": "LAMC-Catalog-2025-2026.pdf",   # deliberately the OLD edition
        "edition": "2025-2026",
        "parsed_at": "2026-03-16T00:28:00+00:00",
        "course_count": 696,
    }))
    monkeypatch.setattr(sync, "CATALOG_DIR", tmp_path)

    writes: list[tuple[str, dict]] = []

    async def fake_run_query(cypher, params=None):
        writes.append((cypher, params or {}))
        return [{"updated": 0}]

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", fake_run_query)

    asyncio.run(sync.record_catalog_provenance("LAMC", matched_count=0))

    cypher, params = writes[-1]
    assert "CatalogSync" in cypher
    assert params["edition"] == "2025-2026", "must reflect the artifact, not the config"
    assert params["source_pdf"] == "LAMC-Catalog-2025-2026.pdf"
    assert params["parsed_count"] == 696
    assert params["matched_count"] == 0


def test_the_sync_records_unknown_when_no_sidecar_exists(tmp_path, monkeypatch):
    """Older artifacts predate the sidecar; they must not be given an edition."""
    import scripts.sync_catalog_to_neo4j as sync

    (tmp_path / "lamc_catalog_courses.json").write_text(json.dumps([]))
    monkeypatch.setattr(sync, "CATALOG_DIR", tmp_path)

    writes: list[tuple[str, dict]] = []

    async def fake_run_query(cypher, params=None):
        writes.append((cypher, params or {}))
        return [{"updated": 0}]

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", fake_run_query)
    asyncio.run(sync.record_catalog_provenance("LAMC", matched_count=5))

    _, params = writes[-1]
    assert params["edition"] is None
    assert params["source_pdf"] is None
    assert params["matched_count"] == 5
