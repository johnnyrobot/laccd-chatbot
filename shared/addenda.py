"""Catalog addenda: flag affected courses, do not parse their contents.

A campus publishes an addendum when curriculum changes after the catalog goes
to print — units change, prerequisites change, courses are added or removed.
Our catalog data comes from the base PDF only, so for any course an addendum
touches, what we hold may be out of date.

**This module deliberately does not parse what changed.** Addenda are change
sets written as prose, not course lists: LAPC's Addendum B says "KIN ATH 503,
504, 506, 508, 511, 512, 513, 514, and 516: Units increased from 3 to 4", and
ELAC marks deletions with strikethrough that pdfplumber does not expose as
formatting. Extracting values from that reliably is a much larger job, and a
half-working version would replace a knowably-stale answer with a confidently
wrong one. So this records only *that* a course is affected, and says so.

What it does use is the cheap, reliable half: which course codes each addendum
mentions at all. `scripts/index_addenda.py` extracts those into
`data/addenda_index.json`. The public repository carries only the small set of
course codes needed at runtime; source PDFs and generated extraction output are
excluded.

Campuses absent from ADDENDA have no live addendum: LAMC, WLAC and LAHC now run
2026-2027 catalogs, which their 2025-2026 addenda predate, and LASC and LATTC
have published none for this cycle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "addenda_index.json"


@dataclass(frozen=True)
class Addendum:
    """One published addendum to a campus's current catalog."""

    filename: str
    published: str  # YYYY-MM, as the campus posted it
    effective: str  # the term its changes take effect
    url: str


# Verified against each campus's own catalog page; the three local
# files matched the published bytes exactly.
ADDENDA: dict[str, tuple[Addendum, ...]] = {
    "ELAC": (
        Addendum(
            "ELAC-2025-26-Catalog-Supplement-v3.pdf", "2026-02", "Spring 2026",
            "https://www.elac.edu/sites/elac.edu/files/2026-02/2025-26-Catalog-Supplement-v3.pdf",
        ),
    ),
    "LACC": (
        Addendum(
            "LACC-2025-26-Spring-Addendum.pdf", "2026-02", "Spring 2026",
            "https://www.lacitycollege.edu/sites/lacc.edu/files/2026-02/LACC_Catalog_2025-2026_Spring_Addendum_022026.pdf",
        ),
    ),
    "LAPC": (
        Addendum(
            "LAPC-2025-26-Addendum-A.pdf", "2026-02", "Spring 2026",
            "https://www.piercecollege.edu/sites/lapc.edu/files/2026-02/LAPC%202025-2026%20General%20Catalog%20Addendum%20A.pdf",
        ),
        Addendum(
            "LAPC-2025-26-Addendum-B.pdf", "2026-05", "Summer 2026",
            "https://www.piercecollege.edu/sites/lapc.edu/files/2026-05/LAPC%202025-2026%20General%20Catalog%20Addendum%20B.pdf",
        ),
    ),
    "LAVC": (
        Addendum(
            "LAVC-2025-26-Addendum.pdf", "2025-12", "Spring 2026",
            "https://www.lavc.edu/sites/lavc.edu/files/2025-12/2025-2026%20LAVC%20Catalog%20Addendum.pdf",
        ),
    ),
}


def has_addendum(campus_code: str) -> bool:
    """Whether this campus has an addendum to its current catalog."""
    return bool(ADDENDA.get((campus_code or "").upper()))


@lru_cache(maxsize=1)
def _index() -> dict[str, set[str]]:
    """Course codes each campus's addenda mention, keyed by campus."""
    if not _INDEX_PATH.exists():
        return {}
    try:
        raw = json.loads(_INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        campus: {str(code).upper() for code in entry.get("codes", [])}
        for campus, entry in raw.items()
    }


def affected_courses(campus_code: str) -> set[str]:
    """The course codes this campus's addenda mention."""
    return _index().get((campus_code or "").upper(), set())


def is_affected(campus_code: str, course_code: str | None) -> bool:
    if not course_code:
        return False
    return course_code.strip().upper() in affected_courses(campus_code)


def notice_for(campus_code: str, course_code: str | None = None) -> str | None:
    """A one-line caveat for a course an addendum touches, else None.

    Passing no course code asks the broader question "does this campus have an
    addendum at all", which suits program- and plan-shaped answers where no
    single course is in view.
    """
    campus = (campus_code or "").upper()
    entries = ADDENDA.get(campus)
    if not entries:
        return None
    if course_code is not None and not is_affected(campus, course_code):
        return None

    newest = max(entries, key=lambda a: a.published)
    subject = (
        f"{course_code.strip().upper()} appears in"
        if course_code
        else "This campus has published"
    )
    # Says what is actually known -- the course is named in the addendum -- and
    # not that it changed. The index cannot tell the difference, and claiming
    # otherwise would be the confident-wrong-answer failure this avoids.
    return (
        f"Note: {subject} a catalog addendum published {newest.published} "
        f"(effective {newest.effective}). The details above come from the base "
        f"catalog and may not reflect it — check the college's addendum or a "
        f"counselor before relying on units or prerequisites."
    )
