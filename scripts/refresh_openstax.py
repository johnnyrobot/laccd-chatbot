#!/usr/bin/env python3
"""Fetch the live OpenStax catalog and print an updated OPENSTAX_BOOKS dict.

Usage:
    python scripts/refresh_openstax.py

Review the output and paste into backend/app/services/openstax.py if changes
look correct.  The LACCD subject-code mapping is based on title keywords and
requires manual curation — the script suggests mappings, but a human should
verify before committing.
"""

from __future__ import annotations

import json
import pprint
import re
import urllib.request

API_URL = "https://openstax.org/apps/cms/api/v2/pages/?type=books.Book&offset=0&limit=200"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 200
MAX_FIELD_CHARS = 300

# Title keyword → LACCD subject code.  Checked in order; first match wins.
TITLE_RULES: list[tuple[str, str]] = [
    ("anatomy", "ANATOMY"),
    ("physiology", "ANATOMY"),
    ("microbiology", "BIOLOGY"),
    ("biology", "BIOLOGY"),
    ("statistics", "STAT"),
    ("algebra", "MATH"),
    ("precalculus", "MATH"),
    ("calculus", "MATH"),
    ("trigonometry", "MATH"),
    ("prealgebra", "MATH"),
    ("contemporary mathematics", "MATH"),
    ("chemistry", "CHEM"),
    ("physics", "PHYSICS"),
    ("psychology", "PSYCH"),
    ("sociology", "SOC"),
    ("macroeconomics", "ECON"),
    ("microeconomics", "ECON"),
    ("economics", "ECON"),
    ("accounting", "ACCTG"),
    ("business", "BUS"),
    ("entrepreneurship", "BUS"),
    ("american government", "POLSCI"),
    ("political science", "POLSCI"),
    ("anthropology", "ANTHRO"),
    ("philosophy", "PHILOS"),
    ("astronomy", "ASTRON"),
    ("nursing", "NURSE"),
    ("pharmacology", "NURSE"),
    ("clinical nursing", "NURSE"),
    ("computer science", "CS"),
    ("python programming", "CS"),
    ("nutrition", "NUTR"),
    ("u.s. history", "HIST"),
    ("world history", "HIST"),
    ("writing guide", "ENGLISH"),
]

# Regex to extract edition number from title, e.g. "2e" → 2
_EDITION_RE = re.compile(r"(\d+)e\b")


def _base_title(title: str) -> str:
    """Strip edition suffix to group editions of the same book."""
    return _EDITION_RE.sub("", title).strip().rstrip(",").strip()


def _edition(title: str) -> int:
    m = _EDITION_RE.search(title)
    return int(m.group(1)) if m else 1


def _classify(title: str) -> str | None:
    """Map a book title to an LACCD subject code using keyword rules."""
    low = title.lower()
    for keyword, code in TITLE_RULES:
        if keyword in low:
            return code
    return None


def render_python_mapping(grouped: dict[str, list[dict[str, str]]]) -> str:
    """Render remote catalog data as correctly escaped Python literals."""

    return (
        "OPENSTAX_BOOKS: dict[str, list[dict[str, str]]] = "
        + pprint.pformat(grouped, sort_dicts=True, width=100)
    )


def main() -> None:
    print(f"Fetching catalog from {API_URL} ...")
    req = urllib.request.Request(API_URL, headers={"User-Agent": "refresh-openstax/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("OpenStax response exceeds byte limit")
        data = json.loads(body)

    items = data.get("items", [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("OpenStax item count exceeds limit")
    print(f"  {len(items)} books returned\n")

    # Collect books, skip non-English by locale and title patterns
    books: list[dict] = []
    for item in items:
        meta = item.get("meta", {})
        slug = str(meta.get("slug", ""))[:MAX_FIELD_CHARS]
        title = str(item.get("title", ""))[:MAX_FIELD_CHARS]
        locale = meta.get("locale", "en")
        # Skip non-English locales and non-English titles
        if locale != "en":
            continue
        if any(kw in title.lower() for kw in ("polski", "szkół", "español", "cálculo")):
            continue
        # Skip AP/high-school specific editions
        if "ap®" in title.lower() or "ap " in title.lower():
            continue
        books.append({"title": title, "slug": slug})

    # Deduplicate: keep only latest edition per base title
    best: dict[str, dict] = {}
    for book in books:
        base = _base_title(book["title"])
        ed = _edition(book["title"])
        if base not in best or ed > _edition(best[base]["title"]):
            best[base] = book

    # Group by LACCD subject code using title-based heuristics
    grouped: dict[str, list[dict[str, str]]] = {}
    unmapped: list[dict] = []
    for book in sorted(best.values(), key=lambda b: b["title"]):
        code = _classify(book["title"])
        if code:
            grouped.setdefault(code, []).append(
                {"title": book["title"], "slug": book["slug"]}
            )
        else:
            unmapped.append(book)

    # Print Python dict
    print(render_python_mapping(grouped))

    if unmapped:
        print(f"\n# {len(unmapped)} books not mapped to an LACCD subject code:")
        for b in unmapped:
            print(f"#   {b!r}")


if __name__ == "__main__":
    main()
