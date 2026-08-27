"""Index which course codes each catalog addendum mentions.

Deliberately extracts *only* the codes, never what changed about them — see
`shared/addenda.py` for why. The output feeds a caveat on affected courses, so
the cost asymmetry is lopsided: a false positive adds one "confirm with a
counselor" line, a false negative leaves today's behaviour unchanged.

Prose is the main source of noise ("Effective Summer 2026", "Title 5"), so a
candidate is kept only if its subject actually exists in that campus's parsed
catalog. That check is what makes a regex over prose trustworthy enough.

Usage:
    PYTHONPATH=backend:. python scripts/index_addenda.py          # external source default
    PYTHONPATH=backend:. python scripts/index_addenda.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pdfplumber

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from shared.addenda import ADDENDA  # noqa: E402

DEFAULT_SOURCE_DIR = REPO / "data" / "source_documents" / "catalogs" / "addendums"
CATALOG_DIR = REPO / "data" / "catalog"
OUT_PATH = REPO / "data" / "addenda_index.json"

# A subject (1-3 uppercase tokens) followed by a course number. Broad on
# purpose; the subject whitelist below does the real filtering.
_CODE_RE = re.compile(
    r"\b([A-Z][A-Z&/.]{1,9}(?:\s+[A-Z][A-Z&/.]{0,9}){0,2})\s+(C?\d{1,4}[A-Z]{0,3})\b"
)


def known_subjects(campus_code: str) -> set[str]:
    """Subjects that exist in this campus's parsed catalog."""
    path = CATALOG_DIR / f"{campus_code.lower()}_catalog_courses.json"
    if not path.exists():
        return set()
    courses = json.loads(path.read_text())
    subjects = set()
    for course in courses:
        code = (course.get("course_code") or course.get("code") or "").strip()
        if not code:
            continue
        subjects.add(code.rsplit(" ", 1)[0].upper())
    return subjects


def _codes_on_line(line: str, subjects: set[str]) -> set[str]:
    found: set[str] = set()
    for subject_raw, number in _CODE_RE.findall(line):
        subject = " ".join(subject_raw.split()).upper()
        if subject in subjects:
            found.add(f"{subject} {number.upper()}")
            continue
        # "KIN ATH 503" also appears as bare "ATH 503" style fragments; try the
        # trailing tokens too, so multi-word subjects are not missed.
        tokens = subject.split()
        for start in range(1, len(tokens)):
            candidate = " ".join(tokens[start:])
            if candidate in subjects:
                found.add(f"{candidate} {number.upper()}")
                break
    return found


def _is_requirement_row(line: str) -> bool:
    """A dot-leader row inside a program's requirement table.

    ``MATH 261* Calculus I .......... 5``. The course is named because a degree
    requires it, not because anything about the course changed. Measured across
    the four live addenda, these are 26% of all mentions and 66% of LACC's, so
    treating them as course changes would put a "your units may have changed"
    caveat on Calculus I district-wide.
    """
    return "...." in line or ". . ." in line


def codes_in_pdf(path: Path, subjects: set[str]) -> tuple[set[str], set[str]]:
    """(courses named in prose, courses named only in requirement tables)."""
    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").split("\n"))

    prose: set[str] = set()
    table: set[str] = set()
    for line in lines:
        codes = _codes_on_line(line, subjects)
        if _is_requirement_row(line):
            table |= codes
        else:
            prose |= codes
    return prose, table - prose


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"External addendum directory (default: {DEFAULT_SOURCE_DIR})",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    index: dict[str, dict] = {}
    for campus, entries in sorted(ADDENDA.items()):
        subjects = known_subjects(campus)
        if not subjects:
            print(f"  !! {campus}: no parsed catalog found — run parse_catalog_pdf.py first")
            continue
        all_prose: set[str] = set()
        all_table: set[str] = set()
        per_file = {}
        for entry in entries:
            path = args.source_dir / entry.filename
            if not path.exists():
                print(f"  !! {campus}: missing {entry.filename}")
                continue
            prose, table = codes_in_pdf(path, subjects)
            per_file[entry.filename] = len(prose)
            all_prose |= prose
            all_table |= table
        all_table -= all_prose
        index[campus] = {
            # What the course-level caveat uses.
            "codes": sorted(all_prose),
            # Recorded for transparency, deliberately NOT used to flag courses.
            "program_table_only": sorted(all_table),
            "files": per_file,
        }
        detail = ", ".join(f"{name}={n}" for name, n in per_file.items())
        print(
            f"  {campus}: {len(all_prose)} courses named in prose  ({detail})"
            f"  [+{len(all_table)} only in requirement tables, not flagged]"
        )

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    total = sum(len(v["codes"]) for v in index.values())
    print(f"\nWrote {OUT_PATH.relative_to(REPO)} — {total} course codes across {len(index)} campuses")


if __name__ == "__main__":
    main()
