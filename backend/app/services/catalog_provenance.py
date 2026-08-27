"""Record which catalog edition each campus is actually serving.

`Course.catalog_synced` records when a sync ran, not which PDF it read. Artifact
modification times are not reliable provenance because parsed files and the
graph can drift independently.

The `:CatalogSync` node this reads is written by
``scripts/sync_catalog_to_neo4j.py`` from the parse's sidecar, so the recorded
edition is the one that was really parsed rather than the one
``CATALOG_CONFIGS`` currently names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.neo4j_service import neo4j_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogProvenance:
    """`recorded=False` means nothing was stamped — never assume that is fine."""

    campus_code: str
    recorded: bool
    edition: str | None = None
    source_pdf: str | None = None
    parsed_at: str | None = None
    synced_at: str | None = None
    parsed_count: int | None = None
    matched_count: int | None = None

    @classmethod
    def unknown(cls, campus_code: str) -> CatalogProvenance:
        return cls(campus_code=campus_code, recorded=False)


async def campus_catalog_provenance(campus_code: str) -> CatalogProvenance:
    """Read the provenance stamp for one campus. Never raises."""
    try:
        rows = await neo4j_service.run_query(
            """
            MATCH (s:CatalogSync {campus_code: $campus})
            RETURN s.campus_code AS campus_code,
                   s.source_pdf AS source_pdf,
                   s.edition AS edition,
                   s.parsed_at AS parsed_at,
                   s.synced_at AS synced_at,
                   s.parsed_count AS parsed_count,
                   s.matched_count AS matched_count
            """,
            {"campus": campus_code.upper()},
        )
    except Exception as exc:
        logger.warning("Could not read catalog provenance for %s: %s", campus_code, exc)
        return CatalogProvenance.unknown(campus_code)

    if not rows:
        return CatalogProvenance.unknown(campus_code)

    row = rows[0]
    return CatalogProvenance(
        campus_code=campus_code.upper(),
        recorded=True,
        edition=row.get("edition"),
        source_pdf=row.get("source_pdf"),
        parsed_at=str(row["parsed_at"]) if row.get("parsed_at") else None,
        synced_at=str(row["synced_at"]) if row.get("synced_at") else None,
        parsed_count=row.get("parsed_count"),
        matched_count=row.get("matched_count"),
    )


def catalog_health_dict(prov: CatalogProvenance) -> dict:
    """Both counts are reported, and their difference named.

    ``sync_catalog_to_neo4j`` only MATCHes existing Course nodes, so a campus
    can parse 848 courses and update 700 of them. Much of that gap is noncredit
    CE with no node, which is legitimate — but nothing used to compare the two
    numbers at all, so a real regression in it was invisible.
    """
    unmatched = (
        prov.parsed_count - prov.matched_count
        if prov.parsed_count is not None and prov.matched_count is not None
        else None
    )
    return {
        "recorded": prov.recorded,
        "edition": prov.edition,
        "source_pdf": prov.source_pdf,
        "parsed_at": prov.parsed_at,
        "synced_at": prov.synced_at,
        "parsed_count": prov.parsed_count,
        "matched_count": prov.matched_count,
        "unmatched_count": unmatched,
    }
