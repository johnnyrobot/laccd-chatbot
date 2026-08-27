"""LACCD term-code encoding.

Codes are 4 digits: "2" + decade digit + year digit + season digit.
Decade digit 1 means 201x, 2 means 202x. Season digit: 2 Winter, 4 Spring,
6 Summer, 8 Fall. So 2168 is Fall 2016 and 2258 is Fall 2025.

live_schedule.get_term_name() only decodes codes starting "22" and echoes
everything else back raw, which silently mis-reads the pre-2020 half of the
schedule archive. Use these helpers for anything that walks terms.
"""
from __future__ import annotations

SEASONS: tuple[str, ...] = ("Winter", "Spring", "Summer", "Fall")

_SEASON_TO_DIGIT = {"Winter": "2", "Spring": "4", "Summer": "6", "Fall": "8"}
_DIGIT_TO_SEASON = {v: k for k, v in _SEASON_TO_DIGIT.items()}
_DECADE_TO_BASE = {"1": 2010, "2": 2020}
_BASE_TO_DECADE = {v: k for k, v in _DECADE_TO_BASE.items()}


def parse_term_code(code: str) -> tuple[str, int] | None:
    """Return (season, year) for a term code, or None if it is not one."""
    text = str(code or "").strip()
    if len(text) != 4 or not text.isdigit() or text[0] != "2":
        return None
    base = _DECADE_TO_BASE.get(text[1])
    season = _DIGIT_TO_SEASON.get(text[3])
    if base is None or season is None:
        return None
    return season, base + int(text[2])


def build_term_code(season: str, year: int) -> str:
    """Inverse of parse_term_code. Raises ValueError on an unrepresentable input."""
    digit = _SEASON_TO_DIGIT.get(season)
    decade = _BASE_TO_DECADE.get(year - (year % 10))
    if digit is None or decade is None:
        raise ValueError(f"cannot encode {season} {year} as a LACCD term code")
    return f"2{decade}{year % 10}{digit}"


def recent_term_codes(anchor: str, count: int) -> list[str]:
    """`count` term codes ending at and including `anchor`, oldest first."""
    parsed = parse_term_code(anchor)
    if parsed is None:
        raise ValueError(f"not a term code: {anchor!r}")
    season, year = parsed
    index = SEASONS.index(season)
    codes: list[str] = []
    for _ in range(count):
        codes.append(build_term_code(SEASONS[index], year))
        index -= 1
        if index < 0:
            index = len(SEASONS) - 1
            year -= 1
    return list(reversed(codes))
