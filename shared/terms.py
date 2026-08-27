"""LACCD term codes, resolved from the calendar rather than a lookup table.

A term code is ``22`` + the last digit of the year + a season digit
(2=Winter, 4=Spring, 6=Summer, 8=Fall), so Fall 2026 is ``2268`` and
Spring 2027 is ``2274``.

The term must be derived from the calendar rather than a finite lookup table or
hard-coded default. The shared rule keeps chat and voice from drifting and
supports future years without a code update.
"""

from __future__ import annotations

import re
from datetime import date

SEASON_DIGITS = {"winter": "2", "spring": "4", "summer": "6", "fall": "8"}

# Last calendar month in which each season's term is still in progress. Used
# both to name the term running today and to decide whether a bare season word
# means this year or next.
SEASON_LAST_MONTH = {"winter": 1, "spring": 5, "summer": 7, "fall": 12}

SEASON_YEAR_RE = re.compile(r"\b(winter|spring|summer|fall)\s+(20[2-9]\d)\b")


def term_code(season: str, year: int) -> str:
    """The LACCD code for a season and year, e.g. ``("spring", 2027) -> "2274"``."""
    return f"22{year % 10}{SEASON_DIGITS[season]}"


def current_season(month: int) -> str:
    """The season whose term is running in a given calendar month."""
    if month == 1:
        return "winter"
    if month <= 5:
        return "spring"
    if month <= 7:
        return "summer"
    return "fall"


def current_term_code(today: date | None = None) -> str:
    """The term running today. Never stale, because nothing is hardcoded."""
    today = today or date.today()
    return term_code(current_season(today.month), today.year)


def resolve_term_code(text: str | None, today: date | None = None) -> str:
    """The term some text is asking about, defaulting to the one running now.

    ``today`` is injectable so tests keep their meaning as real time passes.
    """
    today = today or date.today()
    if not text:
        return current_term_code(today)

    lowered = text.lower()

    # An explicit season + year wins outright.
    match = SEASON_YEAR_RE.search(lowered)
    if match:
        return term_code(match.group(1), int(match.group(2)))

    # A bare season word means its next occurrence — never a term that has
    # already finished.
    for season, last_month in SEASON_LAST_MONTH.items():
        if season in lowered:
            year = today.year if today.month <= last_month else today.year + 1
            return term_code(season, year)

    return current_term_code(today)
