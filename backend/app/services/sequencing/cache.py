# backend/app/services/sequencing/cache.py
"""Offline term-availability table.

Built by scripts/build_schedule_availability.py, never at request time: a
16-term x 9-campus sweep is 144 calls and roughly 106 seconds serially.

Every file stamps its own provenance. Program nodes in Neo4j carry no
last_synced property, which makes "how old is this data?" unanswerable about
production; this table does not repeat that mistake.

rollup() never emits confidence: "absent" -- that is deliberate, not a gap.
This module only knows what the schedule API returned; it has no program
course list to check a course against, so it cannot enumerate courses that
were never offered. A course simply missing from `courses` (this table's
dict keyed by course code) IS the "absent" tier: nothing was seen, so
nothing was recorded. A downstream consumer reading a missing key as
"unknown"/"absent" is reading the same state this module represents by
omission -- do not go looking for a tier that is represented by a record's
non-existence rather than its presence.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from app.services.schedule_sections import iter_schedule_sections
from app.services.sequencing.terms import parse_term_code, recent_term_codes

logger = logging.getLogger(__name__)

API_BASE = "https://services.laccd.edu/apps/api/classschedule"
CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "schedule_availability"
CACHE_MAX_AGE_DAYS = 120
SWEEP_TERM_COUNT = 16
MIN_CONFIDENT_OBSERVATIONS = 3

# Only Fall and Spring may constrain a placement. Winter and Summer are far
# too sparse to read as evidence of absence: six terms inside the archive
# window return zero Multimedia courses at all, and most Winter/Summer terms
# return exactly one. "Winter: 0" means no evidence, never "not offered".
CONSTRAINING_SEASONS = ("Fall", "Spring")


class SweepError(RuntimeError):
    """The API returned something that is not a real listing."""


def assert_real_listing(listing: dict, campus: str, term: str) -> None:
    """Guard against the API's habit of returning 200 for anything.

    Term 9999 yields "Winter 2799"; terms 0 and -1 yield "Winter 1800"; bogus
    campus and subject codes also return 200. Without this check a sweep
    writes a table in which every course reads "never offered".
    """
    subjects = listing.get("subjects") or []
    if not subjects:
        raise SweepError(f"{campus} {term}: empty subjects array")

    # `term` is always something we generated via recent_term_codes() in
    # production, so it always parses. An unparseable term here only shows
    # up when garbage was passed in directly (as in the 9999 / 0 / -1 cases
    # this guard exists for), and in that case we have no expected
    # season/year to compare against -- fold it into the termname-mismatch
    # error rather than a separate "not a term code" branch, since either
    # way the API's termname cannot be trusted.
    termname = str(listing.get("termname") or "")
    parsed = parse_term_code(term)
    if parsed is None or parsed[0] not in termname or str(parsed[1]) not in termname:
        raise SweepError(
            f"{campus} {term}: termname {termname!r} does not match the requested term"
        )


def _iter_sections(listing: dict):
    """Yield compatibility ``(course_code, section)`` pairs."""
    for course, section, _primary in iter_schedule_sections(listing):
        subject = str(course.get("subject") or "").strip()
        catalog = str(course.get("catalogNbr") or "").strip()
        yield (f"{subject} {catalog}" if subject and catalog else ""), section


def rollup(records: list[dict]) -> dict[str, dict]:
    """Aggregate raw per-section observations into one record per course.

    `obs_count`, `confidence`, and `constrains` are keyed on *distinct terms*
    (season, year) a course was seen in -- never on the number of section
    records. A single term routinely contains 3+ sections of the same
    course (a lecture plus 2 LAB relsections, or multiple lecture
    sections): on a live LAMC Fall-2025 sweep, 104 of 420 courses (25%)
    already reach 3+ *sections* inside that one term, one of them 29. If
    `obs_count` counted sections, that single term alone would satisfy
    MIN_CONFIDENT_OBSERVATIONS and downstream would read "confidently
    offered every Fall" from one term of evidence. `seasons` below counts
    distinct terms per season for the same reason -- also not a section
    tally, so a consumer reading `seasons["Fall"]` sees "how many distinct
    Fall terms", not "how many Fall sections".

    Each record's `modality_tags` (a list -- a section commonly carries 2+
    tags at once, e.g. ["ONLINE", "OER"]) is tallied per tag so every
    resulting `modalities` key round-trips through `modality_legend`.
    """
    terms_seen: dict[str, set[tuple[str, int]]] = defaultdict(set)
    seasons_seen: dict[str, dict[str, set[int]]] = defaultdict(lambda: {
        "Winter": set(), "Spring": set(), "Summer": set(), "Fall": set(),
    })
    modalities: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for rec in records:
        code = rec["course_code"]
        term = (rec["season"], rec["year"])
        terms_seen[code].add(term)
        seasons_seen[code][rec["season"]].add(rec["year"])
        for tag in rec.get("modality_tags") or []:
            modalities[code][tag] += 1

    out: dict[str, dict] = {}
    for code, terms in terms_seen.items():
        total = len(terms)
        confidence = "high" if total >= MIN_CONFIDENT_OBSERVATIONS else "thin"
        constrains: list[str] = []
        if confidence == "high":
            constrains = [s for s in CONSTRAINING_SEASONS if seasons_seen[code][s]]
        out[code] = {
            "course_code": code,
            "seasons": {season: len(years) for season, years in seasons_seen[code].items()},
            "modalities": dict(modalities[code]),
            "obs_count": total,
            "confidence": confidence,
            "constrains": constrains,
        }
    return out


async def _fetch_listing(campus: str, term: str) -> dict:
    """Unfiltered listing. Never pass a subject filter.

    classTypeList is scoped to the subjects returned, not the term: a
    subject-filtered call for LAMC 2258 returns a 3-entry legend where the
    unfiltered call returns all 10 (confirmed live). Filtering silently
    under-decodes modality.
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(f"{API_BASE}/listing/{campus}/{term}")
        response.raise_for_status()
        return response.json()


async def build_campus(
    campus: str,
    anchor_term: str,
    fetch: Callable[[str, str], Awaitable[dict]] | None = None,
) -> dict:
    """Sweep SWEEP_TERM_COUNT terms and return the full file payload."""
    fetch = fetch or _fetch_listing
    terms = recent_term_codes(anchor_term, SWEEP_TERM_COUNT)
    records: list[dict] = []
    legend: dict[str, str] = {}

    for term in terms:
        listing = await fetch(campus, term)
        assert_real_listing(listing, campus, term)
        # classTypeList entries use {"name": <code>, "value": <description>}
        # The upstream payload uses these names, not {"code", "descr"}.
        for entry in listing.get("classTypeList") or []:
            if entry.get("name"):
                legend[str(entry["name"])] = str(entry.get("value") or "")
        season, year = parse_term_code(term)
        for code, section in _iter_sections(listing):
            if not code:
                continue
            # One record per section, NOT one per classType tag -- a section
            # commonly carries 2+ tags at once (e.g. ["ONLINE", "OER"]),
            # 42% of a live LAMC Fall-2025 sweep did. The full tag list
            # travels on the record as `modality_tags`; rollup() tallies
            # each tag separately so every histogram key it produces still
            # decodes against `modality_legend` above (a joined string like
            # "ONLINE, OER" would not: 42% of a live sweep's sections
            # produced a joined key absent from the legend, and joins are
            # order-dependent -- "A, B" and "B, A" would count as different
            # keys for the same section type).
            records.append({
                "course_code": code,
                "season": season,
                "year": year,
                "modality_tags": list(section.get("classType") or []),
            })

    if not records:
        raise SweepError(f"{campus}: zero observations across {len(terms)} terms")

    return {
        "campus": campus,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "swept_terms": len(terms),
        "term_range": [terms[0], terms[-1]],
        "observation_count": len(records),
        "modality_legend": legend,
        "courses": rollup(records),
    }


def load_campus(campus: str, now: datetime | None = None) -> dict | None:
    """Load a campus table, or None when it is missing, corrupt, or older than
    the max age.

    Returning None disables availability for that campus rather than failing
    the request: a campus with no availability beats one with confidently
    wrong availability. That contract extends to a corrupt file: a bad
    JSON parse or an unparseable `built_at` stamp is treated exactly like a
    missing file, not raised, so a damaged cache disables a campus instead
    of crashing its caller.
    """
    path = CACHE_DIR / f"{campus}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.warning("%s availability cache is corrupt JSON; disabling availability", campus)
        return None
    built_at = payload.get("built_at")
    if not built_at:
        return None
    try:
        stamped = datetime.fromisoformat(built_at)
    except ValueError:
        logger.warning(
            "%s availability cache has an unparseable built_at %r; disabling availability",
            campus, built_at,
        )
        return None
    # Both timestamps are stamped tz-aware in build_campus(), but `now` is a
    # public parameter a caller could pass naive, and a hand-written cache
    # file could too -- normalize before subtracting so that never raises.
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    if reference - stamped > timedelta(days=CACHE_MAX_AGE_DAYS):
        logger.warning(
            "%s availability cache is %s days old (max %s); disabling availability",
            campus, (reference - stamped).days, CACHE_MAX_AGE_DAYS,
        )
        return None
    return payload
