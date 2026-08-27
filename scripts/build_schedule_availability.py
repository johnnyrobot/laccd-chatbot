# scripts/build_schedule_availability.py
"""Build the term-availability table for every campus.

Usage:
    PYTHONPATH=backend:. python3 scripts/build_schedule_availability.py --anchor 2258 [--campus LAMC]

Fails loudly on a campus with zero observations. Warns when a campus's
observation count drops more than 30% against its previous recorded count --
an absolute floor is not defensible across campuses of very different sizes,
but a regression against a campus's own history is.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.services.live_schedule import ALL_CAMPUSES  # noqa: E402
from app.services.sequencing import cache  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REGRESSION_THRESHOLD = 0.30


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campus", action="append", default=None)
    parser.add_argument("--anchor", required=True, help="anchor term code, e.g. 2258")
    args = parser.parse_args()

    campuses = args.campus or ALL_CAMPUSES
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    for campus in campuses:
        previous = None
        path = cache.CACHE_DIR / f"{campus}.json"
        if path.exists():
            previous = json.loads(path.read_text()).get("observation_count")

        try:
            payload = await cache.build_campus(campus, args.anchor)
        except cache.SweepError as exc:
            logger.error("%s FAILED: %s", campus, exc)
            failures.append(campus)
            continue

        count = payload["observation_count"]
        if previous and count < previous * (1 - REGRESSION_THRESHOLD):
            logger.warning(
                "%s observations dropped %.0f%% (%s -> %s) -- verify before trusting",
                campus, (1 - count / previous) * 100, previous, count,
            )

        path.write_text(json.dumps(payload, indent=1))
        logger.info(
            "%s: %s courses, %s observations, terms %s",
            campus, len(payload["courses"]), count, payload["term_range"],
        )

    if failures:
        logger.error("FAILED campuses: %s", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
