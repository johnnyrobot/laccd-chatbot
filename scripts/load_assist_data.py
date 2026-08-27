#!/usr/bin/env python3
"""Parse ASSIST flat files and load Course nodes, GE relationships, and renumberings into Neo4j.

Loads data for all 9 LACCD campuses from the tilde-delimited ASSIST database files.
Auto-detects course renumberings by matching EndingTerm/BeginningTerm with same title+units.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/load_assist_data.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/load_assist_data.py --campus LAMC
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.services.neo4j_service import (  # noqa: E402
    ASSIST_INSTITUTION_CODES,
    INSTITUTION_TO_CAMPUS,
    neo4j_service,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ASSIST institution codes we care about (all 9 LACCD campuses)
LACCD_INSTITUTION_CODES = set(ASSIST_INSTITUTION_CODES.values())

BATCH_SIZE = 500


@dataclass
class AssistCourse:
    institution_code: str
    subject_code: str
    number: str
    title: str
    units_min: float
    units_max: float
    unit_type: str
    begin_term: str
    end_term: str
    uc_transferable: bool = False
    csu_transferable: bool = False
    ge_areas: list[tuple[str, str]] = field(default_factory=list)  # (system, code)

    @property
    def campus_code(self) -> str:
        return INSTITUTION_TO_CAMPUS.get(self.institution_code, "UNKNOWN")

    @property
    def course_code(self) -> str:
        return f"{self.subject_code} {self.number}"

    @property
    def is_current(self) -> bool:
        return self.end_term == "999999" or int(self.end_term) > 202400

    @property
    def norm_key(self) -> str:
        """Normalized key for renumbering detection: title + units at institution."""
        return f"{self.institution_code}|{self.title.lower().strip()}|{self.units_min}|{self.units_max}"

    @property
    def fuzzy_key(self) -> str:
        """Fuzzy title key: core subject words PLUS the sequence position.

        The position is part of the key on purpose. Without it "Calculus I",
        "Calculus II" and "Calculus III" all normalise to "calculus", land in one
        group, and get cross-producted into each other as renumberings — which
        is how MATH 265 came to "renumber" to MATH 261, 262 and 263 at once.
        """
        return (
            f"{self.institution_code}|{_normalize_title(self.title)}"
            f"|seq{_title_sequence(self.title)}|{self.units_min}|{self.units_max}"
        )


def find_latest_file(pattern: str) -> Path | None:
    """Find the latest ASSIST file matching a glob pattern in the assist_db directory."""
    assist_dir = settings.assist_db_dir
    if not assist_dir.exists():
        return None
    files = sorted(assist_dir.glob(pattern), reverse=True)
    return files[0] if files else None


def parse_csu_file(path: Path) -> dict[str, AssistCourse]:
    """Parse CSU transferable courses file.
    Format: InstitutionCode~CoursePrefix~CourseNumber~BeginTerm~EndTerm~Title~UnitsMin~UnitsMax~UnitType
    """
    courses: dict[str, AssistCourse] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("~")
            if len(parts) < 9:
                continue
            inst = parts[0]
            if inst not in LACCD_INSTITUTION_CODES:
                continue
            key = f"{inst}|{parts[1]}|{parts[2]}|{parts[3]}"
            courses[key] = AssistCourse(
                institution_code=inst,
                subject_code=parts[1],
                number=parts[2],
                title=parts[5],
                units_min=float(parts[6]),
                units_max=float(parts[7]),
                unit_type=parts[8],
                begin_term=parts[3],
                end_term=parts[4],
                csu_transferable=True,
            )
    return courses


def parse_uc_file(path: Path) -> dict[str, AssistCourse]:
    """Parse the UC transferable courses file (ASSIST V3 spec, File #1).

    Format: InstitutionCode~CoursePrefix~CourseNumber~BeginTerm~EndTerm~Title~
            UnitsMin~UnitsMax~UnitType~UCCreditLimitFlag~UCTransferAreas

    **Membership in this file is what makes a course UC transferable** — per the
    spec, File #1 "includes data for all community college courses that are UC
    transferable." Position 10 is the *UC-Credit-Limit-Flag*, which only marks
    courses whose transfer credit is capped ("If the UC-Credit-Limit-Flag data
    element is a 'Y', the course has a textual comment or footnote that limits
    the amount of credit that will be granted"). It is NOT a UC-eligibility flag.

    Reading position 10 as eligibility marked ~2/3 of UC-transferable LACCD
    courses as non-transferable, including bedrock IGETC Area 1A courses like
    LASC ENGLISH 101. The credit-limit text itself is loaded separately from the
    Addendum file by ``load_credit_limitations``.
    """
    courses: dict[str, AssistCourse] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("~")
            if len(parts) < 10:
                continue
            inst = parts[0]
            if inst not in LACCD_INSTITUTION_CODES:
                continue
            key = f"{inst}|{parts[1]}|{parts[2]}|{parts[3]}"
            courses[key] = AssistCourse(
                institution_code=inst,
                subject_code=parts[1],
                number=parts[2],
                title=parts[5],
                units_min=float(parts[6]),
                units_max=float(parts[7]),
                unit_type=parts[8],
                begin_term=parts[3],
                end_term=parts[4],
                uc_transferable=True,
            )
    return courses


def parse_institutions_file(path: Path) -> list[dict]:
    """Parse the Institutions File (insts.txt).

    Format per ASSIST V3 spec p.7:
      Institution-Code~Beginning-Term~Ending-Term~Institution-Abbreviation
        ~Institution-Name~Unit-Type(Q/S/T)~System(C/S/U/I)

    Keeps the most current record per institution (latest begin_term among
    rows whose end_term is 999999 or future).
    """
    by_code: dict[str, dict] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("~")
            if len(parts) < 7:
                continue
            ets_code, begin_term, end_term, abbrev, name, unit_type, system = parts[:7]
            # Keep only currently-active records
            if end_term != "999999" and end_term != "207010":
                try:
                    if int(end_term) < 202400:
                        continue
                except ValueError:
                    continue
            existing = by_code.get(ets_code)
            if existing is None or begin_term > existing["begin_term"]:
                by_code[ets_code] = {
                    "ets_code": ets_code,
                    "short_code": abbrev.strip(),
                    "name": name.strip(),
                    "unit_type": unit_type,
                    "system": system,  # C=CCC, S=CSU, U=UC, I=Independent
                    "begin_term": begin_term,
                    "end_term": end_term,
                }
    return list(by_code.values())


def parse_credit_limitations_file(path: Path) -> dict[str, list[dict]]:
    """Parse uc_all_creditlimitations.txt (Addendum to File #1).

    Format per ASSIST V3 spec p.11-13:
      Institution-Code~Course-Prefix~Course-Number~Course-Title~Units-Min
        ~Units-Max~Unit-Type~UC-Credit-Limit-Footnote~UC-Credit-Limit-Footnote-Indicator
        ~Footnote-BeginTerm~Footnote-EndTerm~UC-Credit-Limit-UC-Note
        ~UC-Note-BeginTerm~UC-Note-EndTerm

    Returns: ``{course_key: [{"footnote", "indicator", "uc_note", "begin_term", "end_term"}, ...]}``
    where ``course_key`` is ``f"{inst}|{prefix}|{number}"``. Filters to LACCD
    institutions only (the Course nodes we have in graph).
    """
    by_course: dict[str, list[dict]] = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split("~")  # do NOT strip — preserve trailing empties
            if len(parts) < 11:
                continue
            inst = parts[0]
            if inst not in LACCD_INSTITUTION_CODES:
                continue
            prefix = parts[1]
            number = parts[2]
            footnote = parts[7].strip() if len(parts) > 7 else ""
            indicator = parts[8].strip() if len(parts) > 8 else ""
            begin = parts[9].strip() if len(parts) > 9 else ""
            end = parts[10].strip() if len(parts) > 10 else ""
            uc_note = parts[11].strip() if len(parts) > 11 else ""
            # Skip historical (ended before 2024)
            if end and end != "999999":
                try:
                    if int(end) < 202400:
                        continue
                except ValueError:
                    pass
            if not footnote and not uc_note:
                continue
            course_key = f"{inst}|{prefix}|{number}"
            by_course[course_key].append({
                "footnote": footnote,
                "indicator": indicator,
                "uc_note": uc_note,
                "begin_term": begin,
                "end_term": end,
            })
    return by_course


def parse_ge_file(path: Path, system: str) -> list[tuple[str, str, str, str, str, str]]:
    """Parse a GE area file (IGETC, CSU-GE, or Cal-GETC).
    Format: InstitutionCode~CoursePrefix~CourseNumber~GECode~BeginTerm~EndTerm~Title~UnitsMin~UnitsMax~UnitType
    Returns: list of (institution_code, subject, number, ge_code, begin_term, end_term)
    """
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("~")
            if len(parts) < 10:
                continue
            inst = parts[0]
            if inst not in LACCD_INSTITUTION_CODES:
                continue
            records.append((inst, parts[1], parts[2], parts[3], parts[4], parts[5]))
    return records


def merge_courses(csu: dict[str, AssistCourse], uc: dict[str, AssistCourse]) -> dict[str, AssistCourse]:
    """Merge CSU and UC course data into a unified dict keyed by inst|subject|number|begin_term."""
    merged = dict(csu)
    for key, uc_course in uc.items():
        if key in merged:
            merged[key].uc_transferable = uc_course.uc_transferable
        else:
            merged[key] = uc_course
    return merged


_TITLE_STRIP_PREFIXES = [
    "introduction to ", "intro to ", "elementary ", "principles of ",
    "fundamentals of ", "survey of ", "concepts of ", "topics in ",
    "advanced ", "beginning ", "intermediate ",
]
_TITLE_STRIP_SUFFIXES = [
    " with corequisite", " with support", " with lab",
    " with analytic geometry", " with analytical geometry",
]
_TITLE_STRIP_WORDS = {"i", "ii", "iii", "iv", "a", "b", "c", "1", "2", "3"}

# A trailing sequence marker is stripped from the title STEM so that
# "Calculus with Analytical Geometry I" and "Calculus I" can be compared — but
# the position itself is semantic and must not be thrown away. Calculus II is
# not a renumbering of Calculus I. Roman, arabic and letter forms map to the
# same ordinal so "Spanish 1" and "Spanish I" are recognised as one position.
_SEQUENCE_ORDINALS = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4",
    "1": "1", "2": "2", "3": "3", "4": "4",
    "a": "1", "b": "2", "c": "3",
}


def _title_sequence(title: str) -> str:
    """The sequence position a title ends on, or "" when it has none.

    Only the RIGHTMOST marker is the sequence position; the loop keeps stripping
    so the stem matches ``_normalize_title``, but the first one popped is the
    one that counts.
    """
    words = title.lower().strip().split()
    ordinal = ""
    while words and words[-1] in _TITLE_STRIP_WORDS | set(_SEQUENCE_ORDINALS):
        if not ordinal:
            ordinal = _SEQUENCE_ORDINALS.get(words[-1], "")
        words.pop()
    return ordinal


def _normalize_title(title: str) -> str:
    """Normalize course title for fuzzy matching.

    "Introduction to Statistics" → "statistics"
    "Calculus with Analytical Geometry I" → "calculus"
    "Elementary Statistics" → "statistics"
    """
    t = title.lower().strip()
    # Strip trailing roman numerals / sequence letters FIRST
    words = t.split()
    while words and words[-1] in _TITLE_STRIP_WORDS:
        words.pop()
    t = " ".join(words)
    # Then strip common prefixes
    for prefix in _TITLE_STRIP_PREFIXES:
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    # Then strip common suffixes
    for suffix in _TITLE_STRIP_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    return t.strip()


# A level prefix is as significant as a sequence marker: "Introduction to Comic
# Book Design" and "Advanced Comic Book Design" both normalise to "comic book
# design" once _TITLE_STRIP_PREFIXES has run, which made them look like one
# course. Prefixes that mean the same level share a bucket, so the pairing this
# stripping exists for ("Introduction to Statistics" -> "Elementary Statistics")
# still works.
_TITLE_LEVELS = {
    "introduction to ": "intro", "intro to ": "intro", "elementary ": "intro",
    "beginning ": "intro", "principles of ": "intro", "fundamentals of ": "intro",
    "survey of ": "intro", "concepts of ": "intro",
    "intermediate ": "intermediate",
    "advanced ": "advanced",
}


def _title_level(title: str) -> str:
    """The level a title's prefix declares, or "" when it declares none."""
    t = title.lower().strip()
    for prefix, level in _TITLE_LEVELS.items():
        if t.startswith(prefix):
            return level
    return ""


def _number_sequence(number: str) -> str:
    """The trailing sequence letter of a course number, or "".

    "40A" -> "A", "C1000" -> "", "227" -> "", "261S" -> "S".
    Only a letter following a digit counts, so a bare "C" prefix is not mistaken
    for a sequence.
    """
    match = re.search(r"\d([A-Za-z])$", number.strip())
    return match.group(1).upper() if match else ""


def _normalize_number(number: str) -> str:
    """Strip leading zeros and C-prefix for comparison: C1000→1000, 032→32."""
    n = number.lstrip("0") or "0"
    return n


def _number_stem(number: str) -> str:
    """The course number without its trailing sequence letter: "101H" -> "101"."""
    n = number.strip()
    if _number_sequence(n):
        n = n[:-1]
    return _normalize_number(n)


def _course_family(course: AssistCourse) -> tuple[str, str]:
    """Subject plus number stem — the unit a renumbering actually moves.

    MATH 227 and MATH 227S are one course offered two ways, as are STAT C1000 and its
    embedded-support twin C1000E. Counting *codes* makes the CCN transition look like
    two courses becoming two courses; counting families makes it the one-to-many split
    it is, which is what keeps `_reject_cross_products` off it.
    """
    return (course.subject_code, _number_stem(course.number))


def _course_identity(course: AssistCourse) -> tuple[str, str, str]:
    """The catalog entry a record is a version of, independent of begin term."""
    return (course.institution_code, course.subject_code, _normalize_number(course.number))


def _begin_terms_by_course(
    courses: dict[str, AssistCourse],
) -> dict[tuple[str, str, str], set[int]]:
    """Every term each course code appears under, across all its ASSIST records."""
    terms: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    for course in courses.values():
        terms[_course_identity(course)].add(int(course.begin_term))
    return terms


# Absent and "intro" share a rank so the pairing this stripping exists for
# ("Introduction to Statistics" -> "Statistics") still works, while a title that
# declares a HIGHER level than its partner never matches it. The audited data had
# edges of the form X -> "Advanced X", where only one side declared a level and
# the old both-sides-declared guard therefore stayed silent.
_LEVEL_RANK = {"": 0, "intro": 0, "intermediate": 1, "advanced": 2}


def _is_real_renumbering(old: AssistCourse, new: AssistCourse) -> bool:
    """Filter out false positives from renumbering detection.

    Real renumbering examples:
      MATH 227 → STAT C1000 (different subject AND different number)
      MATH 265 → MATH 261 (same subject, different number)
      BIOL 101 → BIOL C101 (same subject, C-prefix added)

    False positives to reject:
      PSYCH 32 → PSYCH 032 (zero padding, same effective number)
      MATH 485 → REAL ES 485 (cross-listed co-op courses, same number different dept)
    """
    old_num_norm = _normalize_number(old.number)
    new_num_norm = _normalize_number(new.number)

    # A sequence position carried in the NUMBER is as significant as one in the
    # title: PERSDEV 40A/40B/40C share a title, so title matching groups them and
    # cross-products them into each other. 40A may become COUNSEL 40A; it is
    # never COUNSEL 40B.
    old_seq = _number_sequence(old.number)
    new_seq = _number_sequence(new.number)
    if old_seq and new_seq and old_seq != new_seq:
        return False

    # A trailing letter on ONE side of the same stem marks a concurrent variant,
    # not a successor: ENGLISH 101 / 101H is Honors, MATH 227 / 227S is the AB 705
    # support section. Both run alongside the original with an identical title, so
    # title matching pairs them and the guard above stays silent (it needs a letter
    # on both sides). The audited data contained this false Honors pattern.
    if bool(old_seq) != bool(new_seq) and _number_stem(old.number) == _number_stem(new.number):
        return False

    # Levels must match by rank. Absent ranks with "intro", so "Introduction to
    # Statistics" -> "Statistics" survives while "Programming in JAVA" ->
    # "Advanced Programming in JAVA" does not.
    old_level = _title_level(old.title)
    new_level = _title_level(new.title)
    if _LEVEL_RANK[old_level] != _LEVEL_RANK[new_level]:
        return False

    if old.subject_code == new.subject_code:
        # Same department: numbers must actually differ (not just zero-padding)
        return old_num_norm != new_num_norm

    # Different departments: reject if numbers are the same (cross-listing, not renumbering)
    # e.g., MATH 485 → REAL ES 485 is cross-listing of "Cooperative Education"
    if old_num_norm == new_num_norm:
        return False

    return True


def _is_lifecycle_transition(
    old: AssistCourse,
    new: AssistCourse,
    begin_terms: dict[tuple[str, str, str], set[int]],
) -> bool:
    """Did the old course actually retire in this term, and the new one appear?

    The pair always comes from one transition, so `old.end_term` *is* the term.

    A record ending where another begins is only a renumbering if the *course code*
    changed hands. ASSIST issues a fresh record per catalog revision, so a course that
    has run for decades "begins" many times: LAPC THEATER 279 has been offered since
    200110 and begins again at 200410, which made it look like the successor of MUSIC
    99V. ELAC PHYS ED 663 is the mirror image — at 199610 it passed the title
    "Activities" to PHYS ED 627 and kept running under a new one, so neither course
    retired and neither renumbered.

    Requiring the old code to have no record starting at or after the term, and the
    new code none starting before it, drops lifecycle-overlap false positives.
    It leaves MATH 227 -> STAT C1000 intact, because
    STAT C1000 first appears in the transition term itself.
    """
    term = int(old.end_term)
    if any(t >= term for t in begin_terms.get(_course_identity(old), ())):
        return False
    return not any(t < term for t in begin_terms.get(_course_identity(new), ()))


def _reject_cross_products(
    pairs: list[tuple[AssistCourse, AssistCourse]],
) -> list[tuple[AssistCourse, AssistCourse]]:
    """Drop a transition where several course families retire into several others.

    Everything in a group shares a title and a unit count, so when two or more
    families end in one term and two or more begin in it, nothing in the data says
    which became which and detection emits every combination. WLAC's six
    "Keyboarding/Document Processing II" records are the clearest case: OFF ADM
    2A/B2/C2 -> CAOT 2A/B2/C2 is a subject rename that kept the numbers, so the only
    correct pairing is the one the cross-listing guard rejects and *every* surviving
    edge is wrong.

    Families, not codes — see `_course_family`. MATH 227S retires in the same term as
    MATH 227 and STAT C1000E begins beside STAT C1000, so on a code count the CCN
    transition is 2x2 and this rule would delete the most important edge in the graph
    on 8 of the 9 campuses. On a family count it is 1x1 and never reaches the test.
    """
    sources = {_course_family(old) for old, _ in pairs}
    targets = {_course_family(new) for _, new in pairs}
    return [] if len(sources) >= 2 and len(targets) >= 2 else pairs


def _find_sequential_pairs(
    group: list[AssistCourse],
    begin_terms: dict[tuple[str, str, str], set[int]],
) -> list[tuple[AssistCourse, AssistCourse]]:
    """Find sequential EndTerm→BeginTerm pairs within a group of courses."""
    by_end: dict[str, list[AssistCourse]] = defaultdict(list)
    by_begin: dict[str, list[AssistCourse]] = defaultdict(list)
    for c in group:
        if c.end_term != "999999":
            by_end[c.end_term].append(c)
        by_begin[c.begin_term].append(c)

    pairs: list[tuple[AssistCourse, AssistCourse]] = []
    for term, enders in by_end.items():
        beginners = by_begin.get(term, [])
        # Judged per transition term: a cross-product is a property of one term's
        # worth of movement, not of the whole group's history.
        in_term = [
            (old, new)
            for old in enders
            for new in beginners
            if old.course_code != new.course_code
            and _is_real_renumbering(old, new)
            and _is_lifecycle_transition(old, new, begin_terms)
        ]
        pairs.extend(_reject_cross_products(in_term))
    return pairs


def detect_renumberings(courses: dict[str, AssistCourse]) -> list[tuple[AssistCourse, AssistCourse]]:
    """Detect course renumberings using two strategies:

    1. **Exact title match**: Group by (institution, title, units). Catches renumberings
       where the title didn't change (most common).
    2. **Fuzzy title match**: Group by (institution, normalized_title, units). Catches
       renumberings where the title also changed, e.g.:
       - "Statistics" → "Introduction to Statistics" (both normalize to "statistics")
       - "Calculus with Analytical Geometry I" → "Calculus I" (both normalize to "calculus")

    Both strategies filter out zero-padding and cross-listing false positives.

    Order is load-bearing: the exact-title pass runs first and `add_pairs` dedupes, so
    a pair the stricter grouping already proved survives whatever the fuzzy pass makes
    of the looser group it falls into. LATTC NUR SCI 101 -> REGNRSG 121 is matched on
    the exact title "Introduction to Nursing"; the fuzzy pass then collapses that
    title and "Fundamentals of Nursing" both to "nursing" and its 2x2 is rejected
    whole by `_reject_cross_products`, without disturbing the answer already found.
    """
    begin_terms = _begin_terms_by_course(courses)
    seen_pairs: set[tuple[str, str, str, str, str]] = set()
    renumberings: list[tuple[AssistCourse, AssistCourse]] = []

    def add_pairs(pairs: list[tuple[AssistCourse, AssistCourse]]) -> None:
        for old, new in pairs:
            # Key on the NORMALIZED number so zero-padded variants of one target
            # collapse: ENV SCI 17 -> GEOG 14 and -> GEOG 014 is one renumbering,
            # not two, and counting it twice made the course look ambiguous.
            key = (
                old.subject_code, _normalize_number(old.number),
                new.subject_code, _normalize_number(new.number),
                old.campus_code,
            )
            if key not in seen_pairs:
                seen_pairs.add(key)
                renumberings.append((old, new))

    # Strategy 1: group by exact title + units
    by_title: dict[str, list[AssistCourse]] = defaultdict(list)
    for course in courses.values():
        by_title[course.norm_key].append(course)
    for group in by_title.values():
        if len(group) >= 2:
            add_pairs(_find_sequential_pairs(group, begin_terms))

    # Strategy 2: group by fuzzy (normalized) title + units
    by_fuzzy: dict[str, list[AssistCourse]] = defaultdict(list)
    for course in courses.values():
        by_fuzzy[course.fuzzy_key].append(course)
    for group in by_fuzzy.values():
        if len(group) >= 2:
            add_pairs(_find_sequential_pairs(group, begin_terms))

    return _drop_bidirectional(renumberings)


def _drop_bidirectional(
    pairs: list[tuple[AssistCourse, AssistCourse]],
) -> list[tuple[AssistCourse, AssistCourse]]:
    """Drop both halves of any pair detected in both directions.

    A renumbering is directional: the old course retires and the new one succeeds
    it. When detection emits A -> B *and* B -> A there is no fact about which is
    the successor, so neither edge can be trusted. This is the shape a cross-listing
    takes -- WLAC HISTORY 44 and CHICANO 8 are one class under two subjects, both
    still current. Bidirectional candidates are therefore rejected.

    `_is_lifecycle_transition` now subsumes this and removes 0 pairs over
    data/assist_db/. It cannot help but subsume it: A -> B needs B to begin in the
    term A ends, and B -> A needs A to begin in the term B ends, so each direction
    supplies the other's disqualifying record. Kept as the cheap backstop for the day
    a caller reaches `_drop_bidirectional` with pairs built some other way.
    """

    def key(old: AssistCourse, new: AssistCourse) -> tuple[str, ...]:
        return (
            old.campus_code,
            old.subject_code, _normalize_number(old.number),
            new.subject_code, _normalize_number(new.number),
        )

    directed = {key(old, new) for old, new in pairs}
    return [(old, new) for old, new in pairs if key(new, old) not in directed]


def deduplicate_courses(courses: dict[str, AssistCourse]) -> dict[str, AssistCourse]:
    """Keep only the most current version of each course per campus.

    For courses with the same code at the same campus, keep the one with the latest begin_term.
    """
    best: dict[str, AssistCourse] = {}
    for course in courses.values():
        dedup_key = f"{course.institution_code}|{course.subject_code}|{course.number}"
        existing = best.get(dedup_key)
        if existing is None or course.begin_term > existing.begin_term:
            # Preserve transfer flags from earlier entries
            if existing:
                if existing.uc_transferable:
                    course.uc_transferable = True
                if existing.csu_transferable:
                    course.csu_transferable = True
            best[dedup_key] = course
        else:
            # Keep transfer flags from later entries
            if course.uc_transferable:
                existing.uc_transferable = True
            if course.csu_transferable:
                existing.csu_transferable = True
    return best


async def load_courses(courses: dict[str, AssistCourse]) -> int:
    """Load Course and Subject nodes into Neo4j in batches."""
    # Deduplicate: keep most current version per campus
    deduped = deduplicate_courses(courses)
    course_list = list(deduped.values())

    # Batch upsert subjects first
    subjects_seen: set[str] = set()
    subject_batch: list[dict] = []
    for c in course_list:
        key = f"{c.subject_code}|{c.campus_code}"
        if key not in subjects_seen:
            subjects_seen.add(key)
            subject_batch.append({"code": c.subject_code, "campus_code": c.campus_code})

    for i in range(0, len(subject_batch), BATCH_SIZE):
        batch = subject_batch[i : i + BATCH_SIZE]
        await neo4j_service.run_write(
            """
            UNWIND $subjects AS s
            MERGE (subj:Subject {code: s.code, campus_code: s.campus_code})
            """,
            {"subjects": batch},
        )
    logger.info("Loaded %d Subject nodes", len(subjects_seen))

    # Batch upsert courses
    total = 0
    for i in range(0, len(course_list), BATCH_SIZE):
        batch = course_list[i : i + BATCH_SIZE]
        params = [
            {
                "code": c.course_code,
                "subject_code": c.subject_code,
                "number": c.number,
                "title": c.title,
                "units_min": c.units_min,
                "units_max": c.units_max,
                "unit_type": c.unit_type,
                "campus_code": c.campus_code,
                "begin_term": c.begin_term,
                "end_term": c.end_term,
                "uc_transferable": c.uc_transferable,
                "csu_transferable": c.csu_transferable,
                "is_current": c.is_current,
            }
            for c in batch
        ]
        await neo4j_service.run_write(
            """
            UNWIND $courses AS c
            MERGE (course:Course {code: c.code, campus_code: c.campus_code})
            SET course.subject_code = c.subject_code,
                course.number = c.number,
                course.title = c.title,
                course.units_min = c.units_min,
                course.units_max = c.units_max,
                course.unit_type = c.unit_type,
                course.begin_term = c.begin_term,
                course.end_term = c.end_term,
                course.uc_transferable = c.uc_transferable,
                course.csu_transferable = c.csu_transferable,
                course.is_current = c.is_current
            """,
            {"courses": params},
        )
        total += len(batch)

    # Create Course→Campus relationships
    await neo4j_service.run_write(
        """
        MATCH (c:Course), (campus:Campus)
        WHERE c.campus_code = campus.code AND NOT (c)-[:AT_CAMPUS]->(campus)
        CREATE (c)-[:AT_CAMPUS]->(campus)
        """
    )

    # Create Subject→Course relationships
    await neo4j_service.run_write(
        """
        MATCH (s:Subject), (c:Course)
        WHERE s.code = c.subject_code AND s.campus_code = c.campus_code AND NOT (s)-[:HAS_COURSE]->(c)
        CREATE (s)-[:HAS_COURSE]->(c)
        """
    )

    # Create Campus→Subject relationships
    await neo4j_service.run_write(
        """
        MATCH (campus:Campus), (s:Subject)
        WHERE campus.code = s.campus_code AND NOT (campus)-[:OFFERS_SUBJECT]->(s)
        CREATE (campus)-[:OFFERS_SUBJECT]->(s)
        """
    )

    logger.info("Loaded %d Course nodes (deduplicated from %d entries)", total, len(courses))
    return total


async def load_ge_relationships(
    ge_records: list[tuple[str, str, str, str, str, str]],
    system: str,
) -> int:
    """Create FULFILLS_GE relationships between courses and GE areas."""
    # Filter to current entries only (end_term 999999 or recent)
    current_records = [r for r in ge_records if r[5] == "999999" or int(r[5]) > 202400]

    batch: list[dict] = []
    for inst, subject, number, ge_code, begin_term, end_term in current_records:
        campus_code = INSTITUTION_TO_CAMPUS.get(inst)
        if not campus_code:
            continue
        batch.append({
            "course_code": f"{subject} {number}",
            "campus_code": campus_code,
            "ge_code": ge_code,
            "system": system,
        })

    total = 0
    for i in range(0, len(batch), BATCH_SIZE):
        chunk = batch[i : i + BATCH_SIZE]
        await neo4j_service.run_write(
            """
            UNWIND $rels AS r
            MATCH (c:Course {code: r.course_code, campus_code: r.campus_code})
            MATCH (ge:GEArea {code: r.ge_code, system: r.system})
            MERGE (c)-[:FULFILLS_GE]->(ge)
            """,
            {"rels": chunk},
        )
        total += len(chunk)

    logger.info("Created %d FULFILLS_GE relationships for %s", total, system)
    return total


async def load_institutions(institutions: list[dict]) -> int:
    """Upsert Institution nodes for ASSIST fallback institution lookup.

    Used by AssistService when the live API is unreachable. The
    ``ets_code`` is the unique key (6-digit ETS code, e.g. '004404' for
    LAMC). The ``short_code`` is the ASSIST website's display code.
    """
    # Constraint: ets_code is the natural unique key
    try:
        await neo4j_service.run_write(
            "CREATE CONSTRAINT institution_ets_code IF NOT EXISTS FOR (i:Institution) REQUIRE i.ets_code IS UNIQUE"
        )
    except Exception:
        logger.debug("Institution constraint already exists")

    total = 0
    for i in range(0, len(institutions), BATCH_SIZE):
        batch = institutions[i : i + BATCH_SIZE]
        await neo4j_service.run_write(
            """
            UNWIND $insts AS inst
            MERGE (i:Institution {ets_code: inst.ets_code})
            SET i.short_code = inst.short_code,
                i.name = inst.name,
                i.unit_type = inst.unit_type,
                i.system = inst.system,
                i.begin_term = inst.begin_term,
                i.end_term = inst.end_term
            """,
            {"insts": batch},
        )
        total += len(batch)
    logger.info("Loaded %d Institution nodes", total)
    return total


async def load_credit_limitations(by_course: dict[str, list[dict]]) -> int:
    """Attach UC credit limitation footnotes to existing Course nodes.

    Stored as the ``credit_limitations`` array property on each Course
    (one course can have multiple footnotes). The course_key format is
    ``f"{ets_code}|{prefix}|{number}"`` matching the keys we build in the
    parser.
    """
    batch: list[dict] = []
    for course_key, limits in by_course.items():
        ets_code, prefix, number = course_key.split("|", 2)
        campus_code = INSTITUTION_TO_CAMPUS.get(ets_code)
        if not campus_code:
            continue
        # Compose human-readable lines: "<symbol> <footnote>" or "(UC Note) <text>"
        lines: list[str] = []
        for lim in limits:
            if lim["footnote"]:
                prefix_sym = f"{lim['indicator']} " if lim["indicator"] else ""
                lines.append(f"{prefix_sym}{lim['footnote']}")
            if lim["uc_note"]:
                lines.append(f"(UC Note) {lim['uc_note']}")
        if not lines:
            continue
        batch.append({
            "course_code": f"{prefix} {number}",
            "campus_code": campus_code,
            "limitations": lines,
        })

    total = 0
    for i in range(0, len(batch), BATCH_SIZE):
        chunk = batch[i : i + BATCH_SIZE]
        # run_write returns None, so `if result:` was never true and this always
        # reported 0 attached no matter how many courses actually matched. The
        # SET itself worked; only the count was wrong — which made a working
        # step look like a silent failure. Count with run_query instead.
        await neo4j_service.run_write(
            """
            UNWIND $rows AS r
            MATCH (c:Course {code: r.course_code, campus_code: r.campus_code})
            SET c.credit_limitations = r.limitations
            """,
            {"rows": chunk},
        )
        rows = await neo4j_service.run_query(
            """
            UNWIND $rows AS r
            MATCH (c:Course {code: r.course_code, campus_code: r.campus_code})
            WHERE c.credit_limitations IS NOT NULL
            RETURN count(c) AS matched
            """,
            {"rows": chunk},
        )
        total += rows[0]["matched"] if rows else 0
    logger.info(
        "Attached credit_limitations to %d Course nodes (out of %d candidates)",
        total,
        len(batch),
    )
    return total


async def clear_inferred_renumberings(campus_codes: set[str]) -> int:
    """Drop previously-INFERRED RENUMBERED_TO edges for these campuses.

    ``load_renumberings`` only MERGEs, so without this a re-run leaves every
    previously-inferred edge in place and the detection fixes have no effect on
    the graph — the script logs "Created N relationships", exits 0, and the bad
    edges survive. That is how 2,728 edges accumulated, 1,731 of them wrong.

    Curated edges from ``data/ccn_renumbering_pairs.csv`` are preserved: those
    carry ``source_url``/``notes``, inferred ones carry only ``transition_term``.
    """
    if not campus_codes:
        return 0
    campuses = sorted(campus_codes)
    match_inferred = (
        "MATCH (old:Course)-[rel:RENUMBERED_TO]->(:Course) "
        "WHERE old.campus_code IN $campuses AND rel.source_url IS NULL"
    )
    # run_write returns nothing, so count before deleting.
    rows = await neo4j_service.run_query(
        f"{match_inferred} RETURN count(rel) AS n", {"campuses": campuses}
    )
    doomed = rows[0]["n"] if rows else 0
    await neo4j_service.run_write(
        f"{match_inferred} DELETE rel", {"campuses": campuses}
    )
    logger.info(
        "Cleared %d inferred RENUMBERED_TO edges across %d campuses "
        "(curated CSV edges with source_url kept)",
        doomed, len(campuses),
    )
    return doomed


async def load_renumberings(renumberings: list[tuple[AssistCourse, AssistCourse]]) -> int:
    """Create RENUMBERED_TO relationships."""
    batch: list[dict] = []
    for old, new in renumberings:
        batch.append({
            "old_code": old.course_code,
            "new_code": new.course_code,
            "campus_code": old.campus_code,
            "transition_term": old.end_term,
        })

    if not batch:
        logger.info("No renumberings detected")
        return 0

    await neo4j_service.run_write(
        """
        UNWIND $rels AS r
        MATCH (old:Course {code: r.old_code, campus_code: r.campus_code})
        MATCH (new:Course {code: r.new_code, campus_code: r.campus_code})
        MERGE (old)-[rel:RENUMBERED_TO]->(new)
        SET rel.transition_term = r.transition_term
        """,
        {"rels": batch},
    )
    logger.info("Created %d RENUMBERED_TO relationships", len(batch))
    for old, new in renumberings:
        logger.info("  %s → %s (%s, term %s)", old.course_code, new.course_code, old.campus_code, old.end_term)
    return len(batch)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Load ASSIST data into Neo4j")
    parser.add_argument("--campus", help="Load only a specific campus (e.g., LAMC)")
    args = parser.parse_args()

    if not neo4j_service.is_configured():
        logger.error("Neo4j not configured. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD.")
        sys.exit(1)

    connected = await neo4j_service.verify_connectivity()
    if not connected:
        logger.error("Cannot connect to Neo4j")
        sys.exit(1)
    logger.info("Connected to Neo4j")

    await neo4j_service.ensure_schema()

    # Find latest ASSIST files
    csu_file = find_latest_file("*_csu_all.txt")
    uc_file = find_latest_file("*_uc_all.txt")
    calgetc_file = find_latest_file("*_calgetc_all.txt")
    csuge_file = find_latest_file("*_csuge_all.txt")
    igetc_file = find_latest_file("*_igetc_all.txt")
    csuai_file = find_latest_file("*_csuai_all.txt")
    insts_file = find_latest_file("*_insts.txt")
    creditlim_file = find_latest_file("*_uc_all_creditlimitations.txt")

    if not csu_file or not uc_file:
        logger.error("CSU or UC ASSIST file not found in %s", settings.assist_db_dir)
        sys.exit(1)

    logger.info("Using CSU file: %s", csu_file.name)
    logger.info("Using UC file: %s", uc_file.name)

    # Parse and merge course data
    csu_courses = parse_csu_file(csu_file)
    logger.info("Parsed %d CSU course entries (LACCD)", len(csu_courses))

    uc_courses = parse_uc_file(uc_file)
    logger.info("Parsed %d UC course entries (LACCD)", len(uc_courses))

    merged = merge_courses(csu_courses, uc_courses)
    logger.info("Merged to %d course entries", len(merged))

    # Filter to single campus if requested
    if args.campus:
        campus_upper = args.campus.upper()
        inst_code = ASSIST_INSTITUTION_CODES.get(campus_upper)
        if not inst_code:
            logger.error("Unknown campus: %s", args.campus)
            sys.exit(1)
        merged = {k: v for k, v in merged.items() if v.institution_code == inst_code}
        logger.info("Filtered to %d entries for %s", len(merged), campus_upper)

    # Detect renumberings before dedup (needs full history)
    renumberings = detect_renumberings(merged)
    logger.info("Detected %d renumberings", len(renumberings))

    # Load courses
    course_count = await load_courses(merged)

    # Load renumberings. Clear the previously-inferred edges first — MERGE alone
    # would leave every stale edge in place and silently no-op the detection fix.
    await clear_inferred_renumberings({c.campus_code for c in merged.values()})
    renumber_count = await load_renumberings(renumberings)

    # Load GE relationships
    ge_total = 0
    if calgetc_file:
        logger.info("Using Cal-GETC file: %s", calgetc_file.name)
        calgetc_records = parse_ge_file(calgetc_file, "Cal-GETC")
        ge_total += await load_ge_relationships(calgetc_records, "Cal-GETC")

    if csuge_file:
        logger.info("Using CSU-GE file: %s", csuge_file.name)
        csuge_records = parse_ge_file(csuge_file, "CSU-GE")
        ge_total += await load_ge_relationships(csuge_records, "CSU-GE")

    if igetc_file:
        logger.info("Using IGETC file: %s", igetc_file.name)
        igetc_records = parse_ge_file(igetc_file, "IGETC")
        ge_total += await load_ge_relationships(igetc_records, "IGETC")

    if csuai_file:
        logger.info("Using CSU American Institutions file: %s", csuai_file.name)
        csuai_records = parse_ge_file(csuai_file, "CSU-AI")
        ge_total += await load_ge_relationships(csuai_records, "CSU-AI")

    # Institutions (used by AssistService Neo4j fallback)
    inst_count = 0
    if insts_file:
        logger.info("Using Institutions file: %s", insts_file.name)
        institutions = parse_institutions_file(insts_file)
        inst_count = await load_institutions(institutions)

    # UC credit limitations (attached as Course property)
    creditlim_count = 0
    if creditlim_file:
        logger.info("Using UC Credit Limitations file: %s", creditlim_file.name)
        creditlim_data = parse_credit_limitations_file(creditlim_file)
        creditlim_count = await load_credit_limitations(creditlim_data)

    logger.info(
        "ASSIST load complete: %d courses, %d renumberings, %d GE relationships, "
        "%d institutions, %d credit-limitation tags",
        course_count,
        renumber_count,
        ge_total,
        inst_count,
        creditlim_count,
    )
    await neo4j_service.close()


if __name__ == "__main__":
    asyncio.run(main())
