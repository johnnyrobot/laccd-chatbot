from __future__ import annotations

"""Regenerate ``tests/guardrails_eval/dataset.jsonl``.

The in-scope rows are the real advertised student questions: every
``data-question`` button in ``frontend/index.html`` (prefixes A/Q/SEQ/XREF),
rendered concretely for two campuses (LAMC + ELAC) so the per-campus Jinja
placeholders become real text. The out-of-scope rows are curated adversarials
across the PRD's off-scope taxonomy (homework, general knowledge, coding,
creative writing, unrelated).

Run with the repo importable::

    PYTHONPATH=backend:. python tests/guardrails_eval/build_dataset.py

This is a maintenance helper, not a test. Re-run it when the index.html
question set changes, then commit the refreshed ``dataset.jsonl``.
"""

import html
import json
import re
from pathlib import Path

from jinja2 import Template

from shared.campus_config import get_campus, get_sample_questions

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "frontend" / "index.html"
OUT_PATH = Path(__file__).resolve().parent / "dataset.jsonl"

# Campuses whose sample-question fill-ins we render concretely. The first is the
# canonical default; the second surfaces different program/certificate names so
# placeholder-bearing questions contribute distinct concrete exemplars.
RENDER_CAMPUSES = ("LAMC", "ELAC")

_BUTTON_RE = re.compile(r'data-question="([^"]*)"[^>]*>\s*([A-Za-z]+)\d+')


def _extract_buttons() -> list[tuple[str, str]]:
    """Return (category, raw_data_question) for every question button."""
    buttons: list[tuple[str, str]] = []
    for line in INDEX_HTML.read_text().splitlines():
        for match in _BUTTON_RE.finditer(line):
            raw = html.unescape(match.group(1))
            category = match.group(2)  # A | Q | SEQ | XREF
            buttons.append((category, raw))
    return buttons


def _in_scope_rows() -> list[dict]:
    buttons = _extract_buttons()
    seen: set[str] = set()
    rows: list[dict] = []
    for category, raw in buttons:
        has_placeholder = "{{" in raw
        for campus_code in RENDER_CAMPUSES:
            campus = get_campus(campus_code)
            q = get_sample_questions(campus_code)
            text = Template(raw).render(campus=campus, q=q).strip()
            if text in seen:
                continue
            seen.add(text)
            rows.append({"text": text, "label": "in_scope", "category": category})
            # Questions without placeholders render identically per campus; emit once.
            if not has_placeholder:
                break
    return rows


# Curated out-of-scope adversarials mirroring PRD taxonomy (§5.4).
_OUT_OF_SCOPE: dict[str, list[str]] = {
    "homework": [
        "solve this quadratic equation: x^2 + 5x + 6 = 0",
        "what's the answer to question 5 on my chemistry worksheet",
        "write a 500 word essay on the causes of World War 1",
        "do my algebra homework for me",
        "prove that the square root of 2 is irrational",
        "help me finish my lab report on photosynthesis",
        "what is the derivative of 3x^2 + 2x",
        "solve for x: 2x + 7 = 15",
        "summarize chapter 4 of my history textbook for my assignment",
        "answer these multiple choice biology questions for me",
    ],
    "general_knowledge": [
        "what is the capital of France",
        "who won the Super Bowl last year",
        "how tall is Mount Everest",
        "what year did World War 2 end",
        "explain how photosynthesis works",
        "what's the weather in Los Angeles today",
        "who is the president of the United States",
        "how many planets are in the solar system",
        "what is the speed of light",
        "tell me a fun fact about octopuses",
    ],
    "coding": [
        "write a python function to reverse a string",
        "debug this javascript code for me",
        "how do I center a div in css",
        "what's the time complexity of quicksort",
        "write a SQL query to join two tables",
        "fix the bug in my react component",
        "explain how recursion works in programming",
        "convert this for loop to a list comprehension",
    ],
    "creative_writing": [
        "write me a poem about the ocean",
        "tell me a bedtime story about a dragon",
        "write a short story about a robot who falls in love",
        "compose a haiku about autumn",
        "write song lyrics about heartbreak",
        "give me a creative name for my startup",
        "write a birthday message for my mom",
        "draft a wedding toast for my best friend",
    ],
    "unrelated": [
        "what's a good recipe for chocolate chip cookies",
        "recommend a good movie to watch tonight",
        "how do I fix a flat tire",
        "what stocks should I invest in",
        "plan a weekend trip to San Diego for me",
        "how do I lose 10 pounds fast",
        "what's the best smartphone to buy in 2026",
        "how do I set up a Netflix account",
    ],
}


def _out_of_scope_rows() -> list[dict]:
    rows: list[dict] = []
    for category, utterances in _OUT_OF_SCOPE.items():
        for text in utterances:
            rows.append({"text": text, "label": "out_of_scope", "category": category})
    return rows


def build() -> list[dict]:
    return _in_scope_rows() + _out_of_scope_rows()


def main() -> None:
    rows = build()
    with OUT_PATH.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    in_scope = sum(1 for r in rows if r["label"] == "in_scope")
    out_scope = sum(1 for r in rows if r["label"] == "out_of_scope")
    print(f"wrote {len(rows)} rows -> {OUT_PATH}")
    print(f"  in_scope:     {in_scope}")
    print(f"  out_of_scope: {out_scope}")


if __name__ == "__main__":
    main()
