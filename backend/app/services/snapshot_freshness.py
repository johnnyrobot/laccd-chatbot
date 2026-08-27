"""How old the Program Mapper snapshot is, and whether that is a problem.

Production cannot reach the vendor at all — their AWS WAF refuses the box by IP
— so program answers come from a snapshot replayed weekly into Neo4j from an
unblocked machine. The fallback answers *successfully*, which is what makes a
dead refresh dangerous: the graph would freeze at whatever Sunday it last ran
and every answer would still look right.

The distinction this module exists to preserve is **stale vs unknown**. A
snapshot nobody has ever stamped is not a fresh one, and collapsing that to
``False`` would rebuild the silent success it is meant to catch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import settings
from app.services.neo4j_service import neo4j_service

logger = logging.getLogger(__name__)

# The singleton node the seed stamps on a successful run.
HEARTBEAT_SOURCE = "program_mapper"

# The Langfuse score the monitor emits. The alert is configured against this
# name, so changing it silently disconnects the alert — rename in both places.
SCORE_NAME = "program_mapper_snapshot_age_days"


@dataclass(frozen=True)
class SnapshotFreshness:
    """``stale`` is deliberately tri-state: True, False, or None for unknown."""

    recorded: bool
    completed_at: datetime | None
    age_days: float | None
    stale: bool | None
    max_age_days: int
    programs: int | None = None

    @classmethod
    def unknown(cls) -> SnapshotFreshness:
        return cls(
            recorded=False,
            completed_at=None,
            age_days=None,
            stale=None,
            max_age_days=settings.program_mapper_snapshot_max_age_days,
        )


def _parse_timestamp(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.warning("Snapshot heartbeat has an unreadable timestamp (%r)", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def program_snapshot_freshness() -> SnapshotFreshness:
    """Read the heartbeat and turn it into an age. Never raises."""
    try:
        row = await neo4j_service.program_snapshot_heartbeat()
    except Exception as exc:
        logger.warning("Could not read the snapshot heartbeat: %s", exc)
        return SnapshotFreshness.unknown()

    if not row:
        return SnapshotFreshness.unknown()

    completed_at = _parse_timestamp(row.get("completed_at"))
    if completed_at is None:
        return SnapshotFreshness.unknown()

    max_age = settings.program_mapper_snapshot_max_age_days
    age_days = (datetime.now(timezone.utc) - completed_at).total_seconds() / 86400
    return SnapshotFreshness(
        recorded=True,
        completed_at=completed_at,
        age_days=age_days,
        stale=age_days > max_age,
        max_age_days=max_age,
        programs=row.get("programs"),
    )


def snapshot_health_dict(freshness: SnapshotFreshness) -> dict:
    return {
        "recorded": freshness.recorded,
        "last_refreshed": freshness.completed_at.isoformat() if freshness.completed_at else None,
        "age_days": round(freshness.age_days, 2) if freshness.age_days is not None else None,
        "stale": freshness.stale,
        "max_age_days": freshness.max_age_days,
        "programs": freshness.programs,
    }
