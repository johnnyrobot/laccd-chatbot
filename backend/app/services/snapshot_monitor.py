"""Report snapshot freshness on a timer, so silence is never mistaken for health.

Two things make the timer load-bearing rather than incidental:

* **It must not depend on the refresh job.** If the Mac that runs the weekly
  refresh dies, a notifier living in that job dies with it. The check has to run
  somewhere always-on, which is here.
* **It must not depend on user traffic.** Reporting only when someone asks a
  program question would make a quiet weekend indistinguishable from a dead
  pipeline.

The Langfuse alert is configured against ``SCORE_NAME`` with no-data handling set
to *Notify after sustained NO_DATA*. That is why an unknown snapshot emits **no
score at all** rather than a large one: Langfuse's default no-data mode treats
missing data as ``0``, which for an age metric reads as perfectly fresh. Silence
has to be the signal, so silence is what we send.
"""

from __future__ import annotations

import asyncio
import logging

from langfuse import get_client, observe

from app.config import settings
from app.services.snapshot_freshness import (
    SCORE_NAME,
    SnapshotFreshness,
    program_snapshot_freshness,
)

logger = logging.getLogger(__name__)


@observe(name="ops.snapshot_freshness")
async def report_snapshot_freshness() -> SnapshotFreshness:
    """One pass: log the state and emit the age. Never raises."""
    freshness = await program_snapshot_freshness()

    if not freshness.recorded:
        logger.warning(
            "Program Mapper snapshot has no recorded refresh — cannot tell how old "
            "the program data is. Expected the weekly refresh to stamp one; check "
            "the pm-refresh launchd job.",
        )
        return freshness

    if freshness.stale:
        logger.warning(
            "Program Mapper snapshot is %.1f days old (limit %d) — program answers "
            "are being served from stale data. The weekly refresh has not landed; "
            "check the pm-refresh launchd job on the recording machine.",
            freshness.age_days, freshness.max_age_days,
        )
    else:
        logger.info(
            "Program Mapper snapshot is %.1f days old (limit %d)",
            freshness.age_days, freshness.max_age_days,
        )

    try:
        get_client().score_current_trace(
            name=SCORE_NAME,
            value=round(freshness.age_days, 3),
            data_type="NUMERIC",
            comment=f"{freshness.programs} programs; limit {freshness.max_age_days}d",
        )
    except Exception as exc:  # a monitor must not break what it watches
        logger.warning("Could not emit the snapshot-age score: %s", exc)

    return freshness


async def run_periodic() -> None:
    """Background loop. Cancelled on shutdown; survives its own failures."""
    interval = settings.snapshot_monitor_interval_seconds
    if interval <= 0:
        logger.info("Snapshot freshness monitor disabled (interval %s)", interval)
        return
    logger.info("Snapshot freshness monitor running every %ds", interval)
    while True:
        try:
            await report_snapshot_freshness()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Snapshot freshness check failed: %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
