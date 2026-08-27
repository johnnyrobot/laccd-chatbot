"""Parse campus catalog PDFs into structured JSON.

Supports all 9 LACCD campuses with per-campus format configurations.

Usage:
    python scripts/parse_catalog_pdf.py                # LAMC only (default)
    python scripts/parse_catalog_pdf.py --campus LACC  # Single campus
    python scripts/parse_catalog_pdf.py --all           # All campuses
    python scripts/parse_catalog_pdf.py --source-dir data/source_documents/catalogs
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import re
import sys
from pathlib import Path

import pdfplumber

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "source_documents" / "catalogs"
OUT_DIR = PROJECT_ROOT / "data" / "catalog"

# Matches "2025-2026", "2025-26" and "2026_2027" in a catalog filename.
_EDITION_RE = re.compile(r"(20\d{2})[-_](20\d{2}|\d{2})")


def catalog_edition(filename: str) -> tuple[int, int] | None:
    """The academic years a catalog filename declares, or None if it declares none.

    Campuses name these inconsistently — "LAMC-Catalog-2025-2026.pdf",
    "LATTC 2025-26 Catalog-web.pdf", "2026_2027 catalog.pdf" — and WLAC's is
    simply "catalog.pdf", which carries no edition at all.
    """
    match = _EDITION_RE.search(filename)
    if not match:
        return None
    start = int(match.group(1))
    end_raw = match.group(2)
    end = int(end_raw) if len(end_raw) == 4 else 2000 + int(end_raw)
    return start, end


def newer_catalog_available(
    configured: str,
    source_dir: Path = DEFAULT_SOURCE_DIR,
) -> str | None:
    """A newer edition of the same campus's catalog in the source directory.

    The configured filename is pinned alongside edition-specific page ranges, so
    a newer PDF cannot simply be substituted — the ranges must be re-derived.
    The point of this check is that the mismatch is reported instead of the
    parser quietly re-reading a superseded edition, which is what happened while
    LAMC, WLAC and LAHC published 2026-2027 while local sources still held 2025-2026.
    """
    current = catalog_edition(configured)
    if current is None:
        return None
    # Compare only within a campus: a newer WLAC catalog says nothing about LAMC.
    prefix = _EDITION_RE.split(configured)[0].strip(" -_")
    best_name, best_edition = None, current
    for path in sorted(source_dir.glob("*.pdf")):
        if path.name == configured:
            continue
        if not path.name.startswith(prefix):
            continue
        edition = catalog_edition(path.name)
        if edition and edition > best_edition:
            best_name, best_edition = path.name, edition
    return best_name


# A course entry header: a subject, a number, then a separator —
# "PHILOS 042:", "STAT C1000E:", "THEATER 271 -". Anchored per line so running
# headers and prose can't match.
_COURSE_HEADER_RE = re.compile(
    r"^([A-Z][A-Z&./ ]{1,13}?)\s*([A-Z]?\d{1,4}[A-Z]{0,2})\s*[:\-–—]", re.M
)

# Pages inside the course-description section carry many headers; the first page
# past it carries none. Measured across all nine 2025-2026 catalogs: the page
# after a CORRECT end scored 0 (one scored 1, a stray match on a faculty roster),
# while the page after a WRONG end scored 2, 6 and 7. Two is the separating line.
COURSE_PAGE_MIN_HEADERS = 2


def course_header_count(text: str | None) -> int:
    """How many course-entry headers a page's text contains."""
    return len(_COURSE_HEADER_RE.findall(text or ""))


def looks_like_course_page(text: str | None) -> bool:
    """Whether a page still belongs to the course-description section."""
    return course_header_count(text) >= COURSE_PAGE_MIN_HEADERS


def running_header(text: str | None) -> str:
    """The page's first non-blank line — its running header, for catalogs that
    print one."""
    for line in (text or "").split("\n"):
        line = line.strip()
        if line:
            return line
    return ""


def continues_section(text: str | None, campus_code: str) -> bool | None:
    """Whether a page still carries the campus's course-section running header.

    `looks_like_course_page` needs a subject on the entry line, which the
    `dept_grouped` campuses never print — they group under a department heading
    and start entries with a bare number ("262 Shielded Metal Arc Welding II
    (3) CSU"). Those two catalogs do print a running header on every page of the
    section, so that is the signal for them.

    Returns None when the campus declares no `section_header`, meaning "no
    opinion" — the caller falls back to the course-entry count. Deliberately not
    generalised to a "header seen inside the range" rule: measured across all
    nine 2025-2026 catalogs, LATTC repeats its college banner on every page of
    the book and WLAC's configured range already overruns into the student
    handbook, so that rule fired on pages that were not course descriptions.
    """
    pattern = CATALOG_CONFIGS.get(campus_code, {}).get("section_header")
    if pattern is None:
        return None
    return bool(pattern.match(running_header(text)))

# ---------------------------------------------------------------------------
# Campus catalog configurations
# ---------------------------------------------------------------------------

CATALOG_CONFIGS: dict[str, dict] = {
    "LAMC": {
        "pdf": "LAMC-Catalog-2026-2027.pdf",
        "start": 88, "end": 161,
        "col_boundaries": [280],
        "style": "lamc",
    },
    "LACC": {
        "pdf": "LACC-Catalog-2025-2026.pdf",
        "start": 229, "end": 372,
        "col_boundaries": [291],
        "style": "lacc",
        "section_header": re.compile(r"^(CREDIT|NONCREDIT) COURSES$"),
    },
    "LAVC": {
        "pdf": "LAVC-Catalog-2025-2026.pdf",
        "start": 162, "end": 264,
        "col_boundaries": [215, 395],
        "style": "general",
        "section_header": re.compile(r"^COURSE DESCRIPTIONS$"),
    },
    "ELAC": {
        "pdf": "ELAC-Catalog-2025-2026.pdf",
        "start": 66, "end": 390,
        "col_boundaries": [295],
        "style": "dept_grouped",
        # ELAC prints the owning department on every course page and switches to
        # prose section titles ("History and District Mission and Vision") once
        # the section ends. That shape is what catches a short `end` here, since
        # ELAC never prints a subject on the entry line.
        "section_header": re.compile(r"^.+\s+Department$"),
    },
    "LAHC": {
        "pdf": "LAHC-Catalog-2026-2027.pdf",
        "start": 184, "end": 317,
        "col_boundaries": [295],
        "style": "lahc",
    },
    "LAPC": {
        "pdf": "Los Angeles Pierce College 2025-2026 General Catalog.pdf",
        "start": 155, "end": 272,
        "col_boundaries": [295],
        "style": "dept_grouped",
        "section_header": re.compile(r"^Course Descriptions$"),
    },
    "LASC": {
        "pdf": "LASC-Catalog-2025-2026.pdf",
        "start": 173, "end": 230,
        "col_boundaries": [306],
        "style": "general",
    },
    "LATTC": {
        "pdf": "LATTC-Catalog-2025-2026.pdf",
        "start": 221, "end": 316,
        "col_boundaries": [296],
        "style": "general",
    },
    "WLAC": {
        "pdf": "WLAC-Catalog-2026-2027.pdf",
        "start": 158, "end": 265,
        "col_boundaries": [283],
        "style": "general",
        # Credit listings run 158-249 and noncredit 250-265, with the running
        # header alternating between the two on facing pages.
        "section_header": re.compile(
            r"^(CREDIT COURSE LISTINGS & DESCRIPTIONS|NONCREDIT PROGRAMS & COURSES)$"
        ),
    },
}

# Known multi-word subject codes across all LACCD campuses
MULTI_WORD_SUBJECTS = {
    "ADM JUS", "AFRO AM", "ART HISTORY", "ARTHIST",
    "A S L", "CH DEV", "CLN ART", "CO SCI",
    "DEN HY", "ENV SCI", "ESL NC", "FAM &CS",
    "HLTH OCC", "KIN ATH", "NRS-HCA", "PHYS ED",
    "PHYS SC", "POL SCI", "REAL ES", "VOC ED",
}

# Campus-specific page footer/header patterns to filter out
FOOTER_PATTERNS: dict[str, list[re.Pattern]] = {
    "LAMC": [
        re.compile(r"^\d+\s+COURSE DESCRIPTIONS"),
        re.compile(r"^COURSE DESCRIPTIONS\s+\d+"),
        re.compile(r"^Enroll now at"),
        re.compile(r"^Los Angeles Mission College Catalog"),
    ],
    "LACC": [
        re.compile(r"^\d+\s+LACC"),
        re.compile(r"^LACC\s+2025"),
        re.compile(r"^CREDIT COURSES$"),
    ],
    "LAVC": [
        re.compile(r"^Los Angeles Valley College"),
        re.compile(r"^A-Z List"),
        re.compile(r"^COURSE DESCRIPTIONS$"),
    ],
    "ELAC": [
        re.compile(r"^EAST LOS ANGELES COLLEGE"),
        re.compile(r"^\d+\s+EAST LOS"),
        # Section header repeated on 33 pages of the range; it leaked into the
        # description of whichever entry it landed inside.
        re.compile(r"^Subjects & Course Descriptions"),
    ],
    "LAHC": [
        re.compile(r"^Los Angeles Harbor College"),
        re.compile(r"^Enroll now at"),
    ],
    "LAPC": [
        re.compile(r"^GENERAL CATALOG\s+2025"),
        re.compile(r"^2025.*GENERAL CATALOG"),
        re.compile(r"^Course Descriptions$"),
    ],
    "LASC": [
        re.compile(r"^\d+\s*\|\s*P\s*a\s*g\s*e"),
    ],
    "LATTC": [
        re.compile(r"^Los Angeles Trade"),
        # The page number sits AFTER this header on odd pages and BEFORE it on
        # even ones ("Course Descriptions - Credit Courses233" vs "226 Course
        # Descriptions - Credit Courses"), so only half of them were filtered.
        re.compile(r"^\d*\s*Course Descriptions\s*[-–]"),
        # The right-hand column carries the catalog year as its own header line;
        # unfiltered it leaked into 98 descriptions. Year-agnostic so it survives
        # the next edition.
        re.compile(r"^\d{4}\s*-\s*\d{4}\s+GENERAL CATALOG"),
    ],
    "WLAC": [
        re.compile(r"^West Los Angeles College"),
        re.compile(r"^CREDIT COURSE LISTINGS"),
        # WLAC alternates this header with the credit one on facing pages, and
        # only the credit half was ever filtered — so it landed mid-description
        # in 53 entries of the 2026-2027 parse (32 of 2025-2026). It sits on its
        # own reconstructed line, so an anchored pattern removes the header
        # without taking real course text with it.
        re.compile(r"^\d*\s*NONCREDIT PROGRAMS & COURSES"),
        re.compile(r"^\d*\s*NONCREDIT COURSE LISTINGS"),
    ],
}

# Department name -> subject code mapping for dept_grouped format (LAPC/ELAC)
DEPT_NAME_TO_CODE: dict[str, str] = {
    "Accounting": "ACCTG",
    "Accounting - Computerized": None,  # skip, see other dept
    "Addiction Studies": "ADDICST",
    "Administration of Justice": "ADM JUS",
    "African American Studies": "AFRO AM",
    "American Indian Studies": "AM IND",
    "American Sign Language": "A S L",
    "Anatomy": "ANATOMY",
    "Anthropology": "ANTHRO",
    "Architecture": "ARCH",
    "Art": "ART",
    "Asian American Studies": "ASIAN AM",
    "Astronomy": "ASTRON",
    "Aviation Maintenance Technician": "AVIATEK",
    "Biology": "BIOLOGY",
    "Biotechnology": "BIOTECH",
    "Broadcasting": "BRDCSTG",
    "Business": "BUS",
    "Chemistry": "CHEM",
    "Chicano Studies": "CHIC ST",
    "Child Development": "CH DEV",
    "Cinema": "CINEMA",
    "Communication Studies": "COMM",
    "Computer Applications and Office Technologies": "CAOT",
    "Computer Information Systems": "CIS",
    "Computer Science": "CS",
    "Computer Science and Information Technology": "CS",
    "Cooperative Education": None,  # cross-dept
    "Counseling": "COUNSEL",
    "Dance": "DANCE",
    "Dental Hygiene": "DEN HY",
    "Economics": "ECON",
    "Education": "EDUC",
    "Electronics": "ELECTRN",
    "Engineering": "ENGR",
    "English": "ENGLISH",
    "English as a Second Language": "ESL",
    "Environmental Design": "ENV",
    "Environmental Science": "ENV SCI",
    "Ethnic Studies": "ETH ST",
    "Family and Consumer Studies": "FAM &CS",
    "Film": "FILM",
    "Fire Technology": "FIRETEK",
    "French": "FRENCH",
    "Geography": "GEOG",
    "Geology": "GEOLOGY",
    "Health": "HEALTH",
    "Health Education": "HEALTH",
    "Health Information Technology": "HIT",
    "Health Occupations": "HLTH OCC",
    "History": "HISTORY",
    "Hospitality": "HOSPT",
    "Humanities": "HUMAN",
    "Italian": "ITALIAN",
    "Japanese": "JAPAN",
    "Journalism": "JOURNAL",
    "Kinesiology": "KIN",
    "Labor Studies": "LABOR",
    "Learning Skills": "LRN SKL",
    "Library Science": "LIB SCI",
    "Linguistics": "LING",
    "Mathematics": "MATH",
    "Media Arts": "MEDIA",
    "Music": "MUSIC",
    "Nursing": "NURSING",
    "Nutrition": "NUTRTN",
    "Philosophy": "PHILOS",
    "Photography": "PHOTO",
    "Physical Education": "PHYS ED",
    "Physical Science": "PHYS SC",
    "Physics": "PHYSICS",
    "Physiology": "PHYSIOL",
    "Political Science": "POL SCI",
    "Psychology": "PSYCH",
    "Real Estate": "REAL ES",
    "Sign Language": "SIGN L",
    "Sociology": "SOC",
    "Spanish": "SPANISH",
    "Speech": "SPEECH",
    "Statistics": "STAT",
    "Theater": "THEATER",
    "Theatre": "THEATER",
    "Theatre Arts": "THEATER",
    "Welding": "WELD",
}


# ---------------------------------------------------------------------------
# PDF text extraction — column-aware
# ---------------------------------------------------------------------------

def _reconstruct_text(word_list: list[dict]) -> str:
    """Reconstruct text from word dicts, grouping by y-position into lines."""
    if not word_list:
        return ""
    word_list.sort(key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    current_line = [word_list[0]]
    for w in word_list[1:]:
        if abs(w["top"] - current_line[0]["top"]) < 3:
            current_line.append(w)
        else:
            lines.append(current_line)
            current_line = [w]
    lines.append(current_line)
    result = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        result.append(" ".join(w["text"] for w in line))
    return "\n".join(result)


def extract_columns(page, col_boundaries: list[float]) -> list[str]:
    """Extract text columns from a page given column x-boundaries."""
    words = page.extract_words(keep_blank_chars=True, use_text_flow=False)
    if not words:
        return []
    n_cols = len(col_boundaries) + 1
    col_words: list[list[dict]] = [[] for _ in range(n_cols)]
    for w in words:
        placed = False
        for i, boundary in enumerate(col_boundaries):
            if w["x0"] < boundary:
                col_words[i].append(w)
                placed = True
                break
        if not placed:
            col_words[-1].append(w)
    return [_reconstruct_text(cw) for cw in col_words]


def _is_footer(line: str, campus_code: str) -> bool:
    """Check if a line matches a campus-specific footer pattern."""
    stripped = line.strip()
    if not stripped:
        return False
    for pattern in FOOTER_PATTERNS.get(campus_code, []):
        if pattern.match(stripped):
            return True
    return False


def extract_text(pdf_path: Path, start_page: int, end_page: int | None,
                 col_boundaries: list[float], campus_code: str) -> str:
    """Extract course descriptions text from a PDF."""
    pdf = pdfplumber.open(str(pdf_path))
    if end_page is None:
        end_page = len(pdf.pages) - 1
    end_page = min(end_page, len(pdf.pages) - 1)

    all_parts: list[str] = []
    for i in range(start_page, end_page + 1):
        page = pdf.pages[i]
        columns = extract_columns(page, col_boundaries)
        for col_text in columns:
            if not col_text.strip():
                continue
            lines = col_text.split("\n")
            filtered = [ln for ln in lines if not _is_footer(ln, campus_code)]
            text = "\n".join(filtered).strip()
            if text:
                all_parts.append(text)
    pdf.close()
    return "\n\n".join(all_parts)


# ---------------------------------------------------------------------------
# Subject code normalization
# ---------------------------------------------------------------------------

def _normalize_subject(code: str) -> tuple[str, str]:
    """Split a course code into (subject, number). Handles multi-word subjects."""
    code = code.strip()
    for subj in sorted(MULTI_WORD_SUBJECTS, key=len, reverse=True):
        if code.upper().startswith(subj + " "):
            rest = code[len(subj):].strip()
            parts = rest.split(None, 1)
            if parts:
                return subj.upper(), parts[0].upper()
    parts = code.rsplit(None, 1)
    if len(parts) == 2:
        return parts[0].upper(), parts[1].upper()
    return code.upper(), ""


# ---------------------------------------------------------------------------
# Title case conversion
# ---------------------------------------------------------------------------

def _title_case(text: str) -> str:
    """Convert ALL CAPS to Title Case with smart handling."""
    if not text:
        return text
    # If already mixed case, return as-is
    if not text.isupper():
        return text
    title = text.title()
    for word in ("And", "Or", "Of", "In", "For", "To", "The", "With",
                 "A", "An", "At", "By", "On", "From", "Through"):
        title = title.replace(f" {word} ", f" {word.lower()} ")
    # Fix Roman numerals
    title = re.sub(r"\bIi\b", "II", title)
    title = re.sub(r"\bIii\b", "III", title)
    title = re.sub(r"\bIv\b", "IV", title)
    title = re.sub(r"\bVi\b", "VI", title)
    # Fix acronyms
    for acr in ("Css", "Html5", "Html", "Aws", "Sql", "Pl/Sql", "Ged", "Cpr",
                 "Mos", "Gis", "Cad", "Ged/Hiset", "Dna", "Irs", "Ihss",
                 "Emt", "Cpr", "Bim", "Gps", "Asl"):
        title = title.replace(acr, acr.upper())
    if title:
        title = title[0].upper() + title[1:]
    return title


# ---------------------------------------------------------------------------
# Shared field extraction
# ---------------------------------------------------------------------------

def _clean_field_value(val: str) -> str:
    """Remove description text that leaked into a metadata field value."""
    if not val:
        return val

    # Description-start phrases that should NOT be in prereq/advisory fields.
    # These are course description sentences, not prerequisite text.
    _DESC_STARTS = (
        r"This (?:course|class|is a|program|provides|covers|examines|"
        r"introduces|offers|deals|surveys|addresses|study|supervised|"
        r"is an|basic|practical|lab|beginning|intermediate|advanced|"
        r"hands-on|supervised|introductory|capstone|service)",
        r"Students (?:will|learn|are|examine|study|explore|gain|develop|"
        r"analyze|build|engage|evaluate|practice|survey|review|prepare)",
        r"The (?:student|principles|course|class|fundamentals|history|"
        r"study|medical|behavioral|growing|doctrines|origin|basic)",
        r"A (?:study|survey|detailed|basic|comprehensive|conceptual|"
        r"practical|continuation|supervised|course|examination|review)",
        r"An (?:introduction|introductory|overview|examination|analysis|"
        r"in-depth|advanced)",
        r"In this course",
        r"Topics (?:include|covered|are)",
        r"Fundamentals of",
        r"Emphasis (?:is|will)",
        r"Instruction (?:is|in|offered)",
        r"Examination of",
        r"It is a",
        r"The above course",
    )
    desc_pattern = "|".join(_DESC_STARTS)

    # Try truncating at description-start phrases that appear AFTER some content
    m = re.search(r"\s(" + desc_pattern + r")", val)
    if m and m.start() > 3:
        val = val[:m.start()].strip()

    val = val.rstrip(". ").strip()
    return val


def _extract_prereqs(text: str) -> str | None:
    """Extract prerequisite text from course body."""
    # Try with lookahead for common following fields
    m = re.search(
        r"Prerequisites?:\s*(.+?)(?=\s*\|?\s*(?:Lecture|Lab|Advisory|Advisories|"
        r"Coreq|Recommended|C-ID|Cal-GETC|\(GE |Note:|\*UC|Total Hours|"
        r"Transfer Credit))",
        text, re.IGNORECASE,
    )
    if not m:
        m = re.search(r"Prerequisites?:\s*(.+)", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        val = val.rstrip("| ").strip()
        val = _clean_field_value(val)
        if val.lower() in ("none", "none.", ""):
            return ""
        val = re.sub(r"\s+", " ", val)
        return val
    return None


def _extract_coreqs(text: str) -> str:
    """Extract corequisite text from course body."""
    m = re.search(
        r"(?:Co-?requisites?|Pre/Corequisite):\s*(.+?)(?=\s*\|?\s*(?:Lecture|Lab|"
        r"Advisory|Advisories|Prereq|Recommended|C-ID|Transfer Credit))",
        text, re.IGNORECASE,
    )
    if not m:
        m = re.search(r"(?:Co-?requisites?|Pre/Corequisite):\s*(.+)", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip().rstrip("| ").strip()
        if val.lower() in ("none", "none."):
            return ""
        val = re.sub(r"\s+", " ", val)
        val = _clean_field_value(val)
        return val
    return ""


def _extract_advisories(text: str) -> str:
    """Extract advisory/recommended text from course body."""
    m = re.search(
        r"(?:Advisor(?:y|ies)|Recommended):\s*(.+?)(?=\s*\|?\s*(?:Lecture|Lab|"
        r"Prereq|Coreq|C-ID|Cal-GETC|\(GE |Note:|Transfer Credit))",
        text, re.IGNORECASE,
    )
    if not m:
        m = re.search(r"(?:Advisor(?:y|ies)|Recommended):\s*(.+)", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip().rstrip("| ").strip()
        if val.lower() in ("none", "none."):
            return ""
        val = re.sub(r"\s+", " ", val)
        val = _clean_field_value(val)
        return val
    return ""


def _extract_hours(text: str) -> tuple[float | None, float | None]:
    """Extract lecture and lab hours from various format patterns."""
    lecture = None
    lab = None
    # Lecture patterns
    for pat in [
        r"Lecture[,:]?\s*(\d+(?:\.\d+)?)\s*hours?",
        r"LECTURE[,:]?\s*(\d+(?:\.\d+)?)\s*HOURS?",
        r"Lec\s+(\d+(?:\.\d+)?)\s*(?:Hrs?|hours?)",
        r"(\d+(?:\.\d+)?)\s+Lec(?:\b|/)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            lecture = float(m.group(1))
            if lecture == int(lecture):
                lecture = int(lecture)
            break
    # Lab patterns
    for pat in [
        r"Lab(?:oratory)?[,:]?\s*(\d+(?:\.\d+)?)\s*hours?",
        r"LABORATORY[,:]?\s*(\d+(?:\.\d+)?)\s*HOURS?",
        r"Lab\s+(\d+(?:\.\d+)?)\s*(?:Hrs?|hours?)",
        r"(\d+(?:\.\d+)?)\s+Lab(?:\b|/)",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            lab = float(m.group(1))
            if lab == int(lab):
                lab = int(lab)
            break
    return lecture, lab


def _extract_transfer(text: str) -> str:
    """Extract transfer status from text."""
    if re.search(r"\*?UC[:/]CSU", text):
        return "UC:CSU"
    if re.search(r"\bUC/CSU\b", text):
        return "UC:CSU"
    # "Transfer Credit: CSU, UC" (LACC style)
    m = re.search(r"Transfer Credit:\s*(.*?)(?:\n|$)", text, re.IGNORECASE)
    if m:
        tc = m.group(1).upper()
        if "UC" in tc and "CSU" in tc:
            return "UC:CSU"
        if "CSU" in tc:
            return "CSU"
        if "UC" in tc:
            return "UC"
    if re.search(r"\bCSU\b", text.split("\n")[0]) and not re.search(r"\bUC\b", text.split("\n")[0]):
        return "CSU"
    if re.search(r"\bUC\b", text.split("\n")[0]) and not re.search(r"\bCSU\b", text.split("\n")[0]):
        return "UC"
    return ""


def _extract_cids(text: str) -> list[str]:
    """Extract C-ID numbers from text."""
    c_ids = []
    for m in re.finditer(r"C-ID[:\s]+([A-Z]+\s+\d+[A-Z]*(?:\s*&\s*\d+[A-Z]*)?)", text):
        c_ids.append(m.group(1).strip())
    return c_ids


def _extract_description(raw: str, skip_labels: list[str] | None = None) -> str:
    """Extract description text from a course entry."""
    if skip_labels is None:
        skip_labels = []
    lines = raw.split("\n")
    desc_lines = []
    in_desc = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_desc:
                desc_lines.append("")
            continue

        # Skip metadata lines
        if re.match(r"^(Prerequisites?|Corequisites?|Co-?requisite|Advisor(?:y|ies)|"
                    r"Recommended|Pre/Corequisite)\s*:", stripped, re.IGNORECASE):
            in_desc = False
            continue
        if re.match(r"^(Lecture|Lab|LECTURE|LABORATORY)[,:]?\s*\d", stripped, re.IGNORECASE):
            in_desc = True
            continue
        if re.match(r"^Transfer Credit:", stripped, re.IGNORECASE):
            continue
        if re.match(r"^C-ID:", stripped):
            continue
        if any(re.match(pat, stripped) for pat in skip_labels):
            continue

        if not in_desc:
            # Check if this is still header/metadata
            if re.search(r"\d+\s+UNITS?\s*$", stripped):
                continue
            if re.match(r"^\(", stripped) and re.search(r"UNITS?\s*$", stripped):
                continue
            if re.match(r"^\(formerly\s+", stripped, re.IGNORECASE):
                continue
            if re.match(r"^\(UC|^\(CSU|^\*UC", stripped):
                continue
            if re.match(r"^\d+\.?\d*\s+Units?\b", stripped, re.IGNORECASE):
                continue
            if re.match(r"^NOTE:", stripped):
                desc_lines.append(stripped)
                in_desc = True
                continue
            if re.match(r"^(Same as|see )", stripped, re.IGNORECASE):
                desc_lines.append(stripped)
                in_desc = True
                continue
            # Sentence-like line
            if len(stripped) > 20 and re.match(r"^[A-Z]", stripped):
                in_desc = True
                desc_lines.append(stripped)
                continue
        else:
            desc_lines.append(stripped)

    desc = " ".join(desc_lines)
    desc = re.sub(r"(\w)- (\w)", r"\1\2", desc)
    desc = re.sub(r"\s{2,}", " ", desc)
    return desc.strip()


# =========================================================================
# LAMC FORMAT — existing parser
# =========================================================================

def _split_entries_lamc(text: str) -> list[dict]:
    """Split text into course entries for LAMC format."""
    code_pattern = re.compile(
        r"^([A-Z][A-Z &./-]*\s+(?:NC\s+)?[A-Z]*\d+[A-Z]*(?:CE)?(?:-\d+)?)\s+([A-Z])",
        re.MULTILINE,
    )
    lines = text.split("\n")
    header_indices: list[int] = []
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[A-Z][A-Z &./,-]+\s*–\s*[A-Z, &]+\s*$", line):
            continue
        if _is_footer(raw_line, "LAMC"):
            continue
        if re.match(r"^(Old Course|New Course|COMMON COURSE|MATH to|PSYCH to|COMM to|POL SCI to|CO SCI to)", line):
            continue
        if re.match(r"^[A-Z]+ \d+[A-Z]*\s+[A-Z]+ C?\d+[A-Z]*\s+[A-Z][a-z]", line):
            continue
        m = code_pattern.match(line)
        if m:
            potential_code = m.group(1).strip()
            subj, num = _normalize_subject(potential_code)
            if num and re.match(r"^\d", num.lstrip("NC").lstrip()):
                header_indices.append(i)

    entries: list[dict] = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        cleaned = []
        for el in lines[start:end]:
            if not _is_footer(el, "LAMC"):
                cleaned.append(el)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            raw = "\n".join(cleaned)
            m = code_pattern.match(cleaned[0].strip())
            if m:
                entries.append({"code": m.group(1).strip(), "raw": raw})
    return entries


def _parse_entry_lamc(entry: dict) -> dict | None:
    """Parse a single LAMC course entry."""
    code = entry["code"]
    raw = entry["raw"]
    subject, number = _normalize_subject(code)
    if not number:
        return None
    if len(subject.split()) > 3:
        return None

    # Extract transfer and C-IDs from header
    transfer = ""
    c_ids: list[str] = []
    cross_listed = ""

    first_line = raw.split("\n")[0]
    if re.search(r"\bUC:CSU\b", first_line) or re.search(r"\(UC:CSU", raw):
        transfer = "UC:CSU"
    elif re.search(r"\bCSU\b", first_line) or re.search(r"\(CSU", raw):
        transfer = "CSU"
    elif re.search(r"\bUC\b", first_line):
        transfer = "UC"
    for line in raw.split("\n")[:3]:
        if not transfer:
            if re.match(r"^\(UC:CSU", line.strip()):
                transfer = "UC:CSU"
            elif re.match(r"^\(CSU", line.strip()):
                transfer = "CSU"
            elif re.match(r"^\(UC\b", line.strip()):
                transfer = "UC"

    for m in re.finditer(r"C-ID\s+([A-Z]+\s+\d+[A-Z]*)", raw):
        c_ids.append(m.group(1))

    eq_match = re.search(r"([A-Z]+\s+\d+[A-Z]*\s*=\s*[A-Z]+\s+\d+[A-Z]*(?:\s*\+\s*[A-Z]+\s+\d+[A-Z]*)*)", raw)
    if eq_match:
        cross_listed = eq_match.group(1).strip()

    # Units
    units = None
    m = re.search(r"(\d+(?:\.\d+)?)\s+UNITS?\b", raw)
    if m:
        val = float(m.group(1))
        units = int(val) if val == int(val) else val

    # Title
    first_lines = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            break
        if re.match(r"^(Prerequisites?|Corequisites?|Advisor|Lecture|Lab|NOTE|This |Same as|Students |\(see )", line):
            break
        first_lines.append(line)
        if re.search(r"\d+(?:\.\d+)?\s+UNITS?\s*$", line):
            break

    header = " ".join(first_lines)
    if header.upper().startswith(code.upper()):
        header = header[len(code):].strip()

    parts = re.split(r"\s+[–-]\s+", header, maxsplit=1)
    title = parts[0].strip()
    title = re.sub(r"\s*\((?:UC|CSU|C-ID|RPT).*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+\d+(?:\.\d+)?\s+UNITS?\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(formerly\s+.+?\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(RPT\s*\d+\)", "", title)
    title = re.sub(r"\s+[A-Z]+ C?\d+[A-Z]*\s+[A-Z].*$", "", title)
    title = _title_case(title.strip())

    joined = " ".join(line.strip() for line in raw.split("\n") if line.strip())
    prereq_text = _extract_prereqs(joined)
    coreq_text = _extract_coreqs(joined)
    advisory_text = _extract_advisories(joined)
    lecture_hours, lab_hours = _extract_hours(joined)
    description = _extract_description(raw)

    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "number": number,
        "title": title,
        "units": units,
        "transfer": transfer,
        "c_ids": c_ids,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "advisory_text": advisory_text,
        "lecture_hours": lecture_hours,
        "lab_hours": lab_hours,
        "cross_listed": cross_listed,
        "description": description,
    }


# =========================================================================
# GENERAL FORMAT — LASC, WLAC, LATTC, LAVC
# Header: CODE NNN Title (N) UC:CSU  or  CODE NNN TITLE (N) CSU
# =========================================================================

# Regex: course code at start of line followed by title text (at least one word)
_GENERAL_HEADER_RE = re.compile(
    r"^([A-Z][A-Z &./-]*\s+(?:NC\s+)?\d+[A-Z]*(?:CE)?(?:-\d+)?)"  # code
    r"\s+([A-Z])",                                                    # title start
)

# Section / department headers to skip
_DEPT_HEADER_GENERAL_RE = re.compile(
    r"^[A-Z][A-Z &./,()-]+$"  # all-caps line without a course number
)


def _is_section_header_general(line: str) -> bool:
    """Check if a line is a department/section header (not a course entry)."""
    stripped = line.strip()
    if not stripped:
        return False
    # All-caps line that's a department name (no digits)
    if _DEPT_HEADER_GENERAL_RE.match(stripped) and not re.search(r"\d", stripped):
        return True
    # Lines like "ADMINISTRATION OF JUSTICE (ADM JUS)"
    if re.match(r"^[A-Z][A-Z\s&.,/-]+\([A-Z\s&./-]+\)\s*$", stripped):
        return True
    return False


def _split_entries_general(text: str, campus_code: str) -> list[dict]:
    """Split text into course entries for general format."""
    lines = text.split("\n")
    header_indices: list[int] = []

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if _is_footer(raw_line, campus_code):
            continue
        if _is_section_header_general(line):
            continue
        m = _GENERAL_HEADER_RE.match(line)
        if m:
            code = m.group(1).strip()
            subj, num = _normalize_subject(code)
            if num and re.match(r"\d", num.lstrip("NC").lstrip()):
                header_indices.append(i)

    entries: list[dict] = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        cleaned = [ln for ln in lines[start:end] if not _is_footer(ln, campus_code)]
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            raw = "\n".join(cleaned)
            m = _GENERAL_HEADER_RE.match(cleaned[0].strip())
            if m:
                entries.append({"code": m.group(1).strip(), "raw": raw})
    return entries


def _parse_entry_general(entry: dict) -> dict | None:
    """Parse a single course entry in general format."""
    code = entry["code"]
    raw = entry["raw"]
    subject, number = _normalize_subject(code)
    if not number or not re.match(r"\d", number.lstrip("NC").lstrip()):
        return None
    if len(subject.split()) > 3:
        return None

    # Build header from first few lines (may wrap across lines)
    header_lines = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            break
        if re.match(r"^(Prerequisites?|Corequisites?|Advisor|Recommended|"
                    r"Lecture|Lab|LECTURE|Transfer Credit|Note:|C-ID:)", stripped, re.IGNORECASE):
            break
        # Description-start lines
        if re.match(r"^(This |Students |The student|A study|An introduction|In this)", stripped):
            break
        header_lines.append(stripped)
        # Stop if we found units in parens
        if re.search(r"\(\d+(?:\.\d+)?(?:-\d+)?\)", stripped):
            break

    header_text = " ".join(header_lines)

    # Title: between code and (N)
    after_code = header_text
    if header_text.upper().startswith(code.upper()):
        after_code = header_text[len(code):].strip()

    title_match = re.match(r"(.+?)\s*\(\d+", after_code)
    title = title_match.group(1).strip() if title_match else after_code
    title = re.sub(r"\s*-\s*$", "", title).strip()
    title = re.sub(r"\s*\(formerly\s+.+?\)", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*✡\s*$", "", title)  # LAVC uses ✡ symbol
    title = re.sub(r"\s*☆\s*$", "", title)
    title = re.sub(r"\s*Ö\s*$", "", title)
    title = _title_case(title)

    # Units from (N) — search in header
    units = None
    units_match = re.search(r"\((\d+(?:\.\d+)?(?:-\d+)?)\)", header_text)
    if units_match:
        try:
            units = float(units_match.group(1).split("-")[0])
            if units == int(units):
                units = int(units)
        except ValueError:
            pass

    # Transfer — search in header
    transfer = _extract_transfer(header_text)

    joined = " ".join(ln.strip() for ln in raw.split("\n") if ln.strip())
    prereq_text = _extract_prereqs(joined)
    coreq_text = _extract_coreqs(joined)
    advisory_text = _extract_advisories(joined)
    lecture_hours, lab_hours = _extract_hours(joined)
    c_ids = _extract_cids(raw)
    description = _extract_description(raw)

    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "number": number,
        "title": title,
        "units": units,
        "transfer": transfer,
        "c_ids": c_ids,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "advisory_text": advisory_text,
        "lecture_hours": lecture_hours,
        "lab_hours": lab_hours,
        "cross_listed": "",
        "description": description,
    }


# =========================================================================
# LACC FORMAT
# Header: CODE NNN - TITLE  (all-caps, dash-separated)
# Next lines: N.00 Units (Lec N Hrs)  /  Transfer Credit: CSU
# =========================================================================

_LACC_HEADER_RE = re.compile(
    r"^([A-Z][A-Z &./-]*\s+\d+[A-Z]*(?:CE)?)\s+-\s+([A-Z])",
)


def _split_entries_lacc(text: str) -> list[dict]:
    """Split text into course entries for LACC format."""
    lines = text.split("\n")
    header_indices: list[int] = []

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if _is_footer(raw_line, "LACC"):
            continue
        # Skip section headers like "ADDICTION STUDIES (ADDICST)"
        if re.match(r"^[A-Z][A-Z\s&./-]+\([A-Z\s&./-]+\)\s*$", line):
            continue
        m = _LACC_HEADER_RE.match(line)
        if m:
            code = m.group(1).strip()
            subj, num = _normalize_subject(code)
            if num and re.match(r"\d", num.lstrip("NC").lstrip()):
                header_indices.append(i)

    entries: list[dict] = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        cleaned = [ln for ln in lines[start:end] if not _is_footer(ln, "LACC")]
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            raw = "\n".join(cleaned)
            m = _LACC_HEADER_RE.match(cleaned[0].strip())
            if m:
                entries.append({"code": m.group(1).strip(), "raw": raw})
    return entries


def _parse_entry_lacc(entry: dict) -> dict | None:
    """Parse a single LACC course entry."""
    code = entry["code"]
    raw = entry["raw"]
    subject, number = _normalize_subject(code)
    if not number:
        return None
    if len(subject.split()) > 3:
        return None

    first_line = raw.split("\n")[0].strip()

    # Title: after "CODE - " up to end of line
    title_match = re.match(
        r"[A-Z][A-Z &./-]*\s+\d+[A-Z]*(?:CE)?\s+-\s+(.+)",
        first_line,
    )
    title = title_match.group(1).strip() if title_match else ""
    title = _title_case(title)

    # Units: from "N.00 Units" line
    units = None
    units_match = re.search(r"(\d+(?:\.\d+)?)\s+Units?\b", raw, re.IGNORECASE)
    if units_match:
        val = float(units_match.group(1))
        units = int(val) if val == int(val) else val

    # Transfer: from "Transfer Credit: CSU, UC"
    transfer = ""
    tc_match = re.search(r"Transfer Credit:\s*(.+?)(?:\n|$)", raw, re.IGNORECASE)
    if tc_match:
        tc = tc_match.group(1).upper()
        if "UC" in tc and "CSU" in tc:
            transfer = "UC:CSU"
        elif "CSU" in tc:
            transfer = "CSU"
        elif "UC" in tc:
            transfer = "UC"

    joined = " ".join(ln.strip() for ln in raw.split("\n") if ln.strip())
    prereq_text = _extract_prereqs(joined)
    coreq_text = _extract_coreqs(joined)
    advisory_text = _extract_advisories(joined)
    lecture_hours, lab_hours = _extract_hours(joined)
    c_ids = _extract_cids(raw)
    description = _extract_description(raw, skip_labels=[
        r"^Transfer Credit:",
        r"^\d+\.\d+\s+Units?\b",
    ])

    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "number": number,
        "title": title,
        "units": units,
        "transfer": transfer,
        "c_ids": c_ids,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "advisory_text": advisory_text,
        "lecture_hours": lecture_hours,
        "lab_hours": lab_hours,
        "cross_listed": "",
        "description": description,
    }


# =========================================================================
# LAHC FORMAT
# Header: CODE NNN: Title  or  CODE NNN Title
# Metadata: dash-bulleted  – Pre: ...  – Total Hours: ...  – Description: ...
# =========================================================================

_LAHC_HEADER_RE = re.compile(
    r"^([A-Z][A-Z &./-]*\s+(?:NC\s+)?\d+[A-Z]*(?:CE)?(?:-\d+)?)"
    r"\s*:?\s+[A-Z]",
)

# LAHC section headers in blue banners
_LAHC_SECTION_RE = re.compile(
    r"^[A-Z][A-Z\s&./-]+\s*[–—-]\s*[A-Z][A-Za-z\s&./-]+$"
)


def _split_entries_lahc(text: str) -> list[dict]:
    """Split text into course entries for LAHC format."""
    lines = text.split("\n")
    header_indices: list[int] = []

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        if _is_footer(raw_line, "LAHC"):
            continue
        # Skip section headers (blue banners)
        if _LAHC_SECTION_RE.match(line):
            continue
        # Skip department banners like "ENV – Environmental Science"
        if re.match(r"^[A-Z]{2,}\s*[–—-]\s*[A-Z]", line) and ":" not in line:
            continue
        # Skip lines that are just "also refer to ..."
        if re.match(r"^also refer to", line, re.IGNORECASE):
            continue

        m = _LAHC_HEADER_RE.match(line)
        if m:
            code = m.group(1).strip()
            subj, num = _normalize_subject(code)
            if num and re.match(r"\d", num.lstrip("NC").lstrip()):
                # Make sure it's not a department header (no number)
                if re.search(r"\d", code):
                    header_indices.append(i)

    entries: list[dict] = []
    for idx, start in enumerate(header_indices):
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        cleaned = [ln for ln in lines[start:end] if not _is_footer(ln, "LAHC")]
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        if cleaned:
            raw = "\n".join(cleaned)
            m = _LAHC_HEADER_RE.match(cleaned[0].strip())
            if m:
                entries.append({"code": m.group(1).strip(), "raw": raw})
    return entries


def _parse_entry_lahc(entry: dict) -> dict | None:
    """Parse a single LAHC course entry."""
    code = entry["code"]
    raw = entry["raw"]
    subject, number = _normalize_subject(code)
    if not number:
        return None
    if len(subject.split()) > 3:
        return None

    first_line = raw.split("\n")[0].strip()

    # Title: after "CODE:" or "CODE " — up to end of line or "(now listed as..."
    after_code = first_line
    if first_line.upper().startswith(code.upper()):
        after_code = first_line[len(code):].strip()
    after_code = after_code.lstrip(": ").strip()
    title = re.sub(r"\s*\(now listed as.*$", "", after_code, flags=re.IGNORECASE)
    title = title.strip()

    # Extract from dash-bulleted metadata
    prereq_text = None
    advisory_text = ""
    coreq_text = ""
    units = None
    transfer = ""
    lecture_hours = None
    lab_hours = None
    c_ids: list[str] = []
    description = ""

    for line in raw.split("\n"):
        stripped = line.strip().lstrip("–—-−• ").strip()
        if not stripped:
            continue

        # Pre: ...
        m = re.match(r"Pre:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val.lower() in ("none", "none."):
                prereq_text = ""
            else:
                prereq_text = re.sub(r"\s+", " ", val)
            continue

        # Corequisite: ...
        m = re.match(r"Co-?requisite:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            coreq_text = re.sub(r"\s+", " ", m.group(1).strip())
            continue

        # Total Hours: 54 Lec; DE  or  Total Hours: 54 Lec/54 Lab; DE
        m = re.match(r"Total Hours:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            hours_text = m.group(1)
            lec_m = re.search(r"(\d+(?:\.\d+)?)\s*Lec", hours_text)
            lab_m = re.search(r"(\d+(?:\.\d+)?)\s*Lab", hours_text)
            if lec_m:
                lecture_hours = float(lec_m.group(1))
                if lecture_hours == int(lecture_hours):
                    lecture_hours = int(lecture_hours)
            if lab_m:
                lab_hours = float(lab_m.group(1))
                if lab_hours == int(lab_hours):
                    lab_hours = int(lab_hours)
            continue

        # Units: "3 units; Letter Grade"
        m = re.match(r"(\d+(?:\.\d+)?)\s+units?\b", stripped, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            units = int(val) if val == int(val) else val
            continue

        # Maj; ... CSU/UC; Cal-GETC ...  (transfer/GE info)
        if re.match(r"^Maj;|^Maj,|^LACCDGE|^CSU|^UC|^Cal-GETC|^DE$|^DE;|^Letter Grade", stripped):
            if "CSU/UC" in stripped or "CSU; UC" in stripped or "UC/CSU" in stripped:
                transfer = "UC:CSU"
            elif "CSU" in stripped and "UC" not in stripped:
                transfer = "CSU"
            elif "UC" in stripped:
                transfer = "UC"
            # C-ID
            cid_match = re.search(r"C-ID\s+([A-Z]+\s+\d+[A-Z]*)", stripped)
            if cid_match:
                c_ids.append(cid_match.group(1))
            continue

        # Description: ...
        m = re.match(r"Description:\s*(.+)", stripped, re.IGNORECASE)
        if m:
            description = re.sub(r"\s+", " ", m.group(1).strip())
            continue

    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "number": number,
        "title": title,
        "units": units,
        "transfer": transfer,
        "c_ids": c_ids,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "advisory_text": advisory_text,
        "lecture_hours": lecture_hours,
        "lab_hours": lab_hours,
        "cross_listed": "",
        "description": description,
    }


# =========================================================================
# DEPT-GROUPED FORMAT — LAPC, ELAC
# Department headers: "Subject (ABBREV)" or "Subject Department"
# Course headers: NNN Title (N) UC:CSU  (number-only, subject from dept)
# =========================================================================

# Matches department headers like "Accounting (ACCTG)" or "Addiction Studies (ADDICST)"
_DEPT_HEADER_RE = re.compile(
    r"^([A-Z][A-Za-z\s&,.\'-]+?)\s*\(([A-Z][A-Z\s&./-]+)\)\s*$"
)

# Course entry with number-only header: "001 Title (N) UC:CSU"
_DEPT_COURSE_RE = re.compile(
    r"^(\d+[A-Z]*(?:CE)?(?:-\d+)?)\s+(.+?)\s*\((\d+(?:\.\d+)?(?:-\d+)?)\)"
)

# Also match full-code entries that might appear in these catalogs
_FULL_CODE_COURSE_RE = re.compile(
    r"^([A-Z][A-Z &./-]*\s+\d+[A-Z]*(?:CE)?(?:-\d+)?)\s+(.+?)\s*\((\d+(?:\.\d+)?(?:-\d+)?)\)"
)


def _split_entries_dept_grouped(text: str, campus_code: str) -> list[dict]:
    """Split text for dept-grouped format (LAPC/ELAC)."""
    lines = text.split("\n")
    current_subject: str | None = None
    markers: list[tuple[int, str, str, str]] = []  # (line_idx, type, subject, number)

    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _is_footer(raw_line, campus_code):
            continue

        # Check for department header with abbreviation
        dept_match = _DEPT_HEADER_RE.match(stripped)
        if dept_match:
            abbrev = dept_match.group(2).strip()
            current_subject = abbrev
            markers.append((i, "dept", current_subject, ""))
            continue

        # Check for department header without abbreviation (name only)
        # e.g., "Administration of Justice Department"
        dept_name_match = re.match(r"^([A-Z][A-Za-z\s&,.\'-]+?)\s+Department\s*$", stripped)
        if dept_name_match:
            name = dept_name_match.group(1).strip()
            mapped = DEPT_NAME_TO_CODE.get(name)
            if mapped:
                current_subject = mapped
                markers.append((i, "dept", current_subject, ""))
            continue

        # Check for full-code course entry (some depts use full codes)
        full_match = _FULL_CODE_COURSE_RE.match(stripped)
        if full_match:
            full_code = full_match.group(1).strip()
            subj, num = _normalize_subject(full_code)
            if num and re.match(r"\d", num.lstrip("NC").lstrip()):
                markers.append((i, "course", subj, num))
                continue

        # Check for number-only course entry
        if current_subject:
            course_match = _DEPT_COURSE_RE.match(stripped)
            if course_match:
                number = course_match.group(1)
                markers.append((i, "course", current_subject, number))

    # Extract entries between course markers
    entries: list[dict] = []
    for idx in range(len(markers)):
        line_idx, mtype, subject, number = markers[idx]
        if mtype != "course":
            continue
        # Find end: next marker of any type
        end = len(lines)
        for j in range(idx + 1, len(markers)):
            end = markers[j][0]
            break
        raw = "\n".join(lines[line_idx:end])
        # Remove trailing blanks
        while raw.endswith("\n"):
            raw = raw.rstrip("\n")
        code = f"{subject} {number}"
        entries.append({"code": code, "raw": raw})

    return entries


def _parse_entry_dept_grouped(entry: dict) -> dict | None:
    """Parse a course entry from dept-grouped format."""
    code = entry["code"]
    raw = entry["raw"]
    subject, number = _normalize_subject(code)
    if not number:
        return None

    first_line = raw.split("\n")[0].strip()

    # Title: extract from between number/code and (units)
    # Could be "001 Title (3) UC:CSU" or "SUBJECT 001 Title (3) UC:CSU"
    after_code = first_line
    # Try to strip the full code first
    if first_line.upper().startswith(code.upper()):
        after_code = first_line[len(code):].strip()
    else:
        # Number-only header: strip just the number
        m = re.match(r"\d+[A-Z]*(?:CE)?\s+", first_line)
        if m:
            after_code = first_line[m.end():].strip()

    title_match = re.match(r"(.+?)\s*\(\d+", after_code)
    title = title_match.group(1).strip() if title_match else after_code
    title = re.sub(r"\s*\(formerly\s+.+?\)", "", title, flags=re.IGNORECASE)
    title = title.strip()
    # Don't title-case if already mixed case
    if title.isupper():
        title = _title_case(title)

    # Units
    units = None
    units_match = re.search(r"\((\d+(?:\.\d+)?(?:-\d+)?)\)", first_line)
    if units_match:
        try:
            units = float(units_match.group(1).split("-")[0])
            if units == int(units):
                units = int(units)
        except ValueError:
            pass

    # Transfer
    transfer = _extract_transfer(first_line)

    joined = " ".join(ln.strip() for ln in raw.split("\n") if ln.strip())
    prereq_text = _extract_prereqs(joined)
    coreq_text = _extract_coreqs(joined)
    advisory_text = _extract_advisories(joined)
    lecture_hours, lab_hours = _extract_hours(joined)
    c_ids = _extract_cids(raw)
    description = _extract_description(raw)

    return {
        "course_code": f"{subject} {number}",
        "subject": subject,
        "number": number,
        "title": title,
        "units": units,
        "transfer": transfer,
        "c_ids": c_ids,
        "prereq_text": prereq_text,
        "coreq_text": coreq_text,
        "advisory_text": advisory_text,
        "lecture_hours": lecture_hours,
        "lab_hours": lab_hours,
        "cross_listed": "",
        "description": description,
    }


# =========================================================================
# Orchestrator
# =========================================================================

STYLE_SPLITTERS = {
    "lamc": lambda text, _cc: _split_entries_lamc(text),
    "lacc": lambda text, _cc: _split_entries_lacc(text),
    "lahc": lambda text, _cc: _split_entries_lahc(text),
    "general": _split_entries_general,
    "dept_grouped": _split_entries_dept_grouped,
}

STYLE_PARSERS = {
    "lamc": _parse_entry_lamc,
    "lacc": _parse_entry_lacc,
    "lahc": _parse_entry_lahc,
    "general": _parse_entry_general,
    "dept_grouped": _parse_entry_dept_grouped,
}


def parse_campus(
    campus_code: str,
    source_dir: Path = DEFAULT_SOURCE_DIR,
) -> list[dict]:
    """Parse catalog PDF for a campus and return list of course dicts."""
    cfg = CATALOG_CONFIGS[campus_code]
    pdf_path = source_dir / cfg["pdf"]
    style = cfg["style"]

    print(f"\n{'='*60}")
    print(f"Parsing {campus_code} — {pdf_path.name}")
    print(f"  Style: {style}, Pages: {cfg['start']+1}-{(cfg['end'] or '?')}")
    print(f"{'='*60}")

    # A superseded edition must never be parsed quietly. The page ranges above
    # are edition-specific, so this deliberately does NOT auto-switch — it says
    # the ranges need re-deriving and leaves the decision to a human.
    superseded_by = newer_catalog_available(cfg["pdf"], source_dir)
    if superseded_by:
        print(
            f"  !! {campus_code} is parsing a SUPERSEDED catalog.\n"
            f"     configured: {cfg['pdf']}\n"
            f"     newer:      {superseded_by}\n"
            f"     Page ranges are edition-specific — re-derive 'start'/'end' for the\n"
            f"     new edition before switching CATALOG_CONFIGS."
        )

    if not pdf_path.exists():
        print(f"  ERROR: PDF not found at {pdf_path}")
        return []

    text = extract_text(
        pdf_path, cfg["start"], cfg["end"],
        cfg["col_boundaries"], campus_code,
    )
    print(f"  Extracted {len(text):,} characters")

    splitter = STYLE_SPLITTERS[style]
    entries = splitter(text, campus_code)
    print(f"  Found {len(entries)} raw course entries")

    parser = STYLE_PARSERS[style]
    courses: list[dict] = []
    skipped = 0
    for entry in entries:
        parsed = parser(entry)
        if parsed:
            courses.append(parsed)
        else:
            skipped += 1

    print(f"  Parsed {len(courses)} courses ({skipped} skipped)")

    # Stats
    with_prereqs = sum(1 for c in courses if c["prereq_text"] not in (None, ""))
    with_none = sum(1 for c in courses if c["prereq_text"] == "")
    with_null = sum(1 for c in courses if c["prereq_text"] is None)
    with_transfer = sum(1 for c in courses if c["transfer"])
    with_cids = sum(1 for c in courses if c["c_ids"])

    print(f"  Prerequisites (has text): {with_prereqs}")
    print(f"  Prerequisites (none):     {with_none}")
    print(f"  Prerequisites (missing):  {with_null}")
    print(f"  Transfer status:          {with_transfer}")
    print(f"  C-IDs:                    {with_cids}")

    return courses


def write_output(campus_code: str, courses: list[dict], source_pdf: str | None = None) -> Path:
    """Write parsed courses, plus a sidecar naming the edition they came from.

    `catalog_synced` records when a sync ran, not which PDF it read. The sidecar
    makes the exact source and edition explicit rather than inferring them from
    file modification times.

    It is a sidecar rather than a wrapper object because several scripts read
    the courses file as a bare list; changing that shape to carry metadata would
    break them.
    """
    out_path = OUT_DIR / f"{campus_code.lower()}_catalog_courses.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(courses, f, indent=2, ensure_ascii=False)

    edition = catalog_edition(source_pdf) if source_pdf else None
    meta_path = OUT_DIR / f"{campus_code.lower()}_catalog_meta.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "campus_code": campus_code,
                "source_pdf": source_pdf,
                # None, not a guess: WLAC has shipped a catalog named simply
                # "catalog.pdf", which declares no edition at all.
                "edition": f"{edition[0]}-{edition[1]}" if edition else None,
                "parsed_at": datetime.now(timezone.utc).isoformat(),
                "course_count": len(courses),
            },
            f,
            indent=2,
        )
    print(f"  Wrote {len(courses)} courses to {out_path.name} (+ {meta_path.name})")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse LACCD campus catalog PDFs")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--campus", type=str, help="Campus code (e.g., LACC, LAVC)")
    group.add_argument("--all", action="store_true", help="Parse all campuses")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"External catalog directory (default: {DEFAULT_SOURCE_DIR})",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.all:
        campuses = list(CATALOG_CONFIGS.keys())
    elif args.campus:
        campus = args.campus.upper()
        if campus not in CATALOG_CONFIGS:
            print(f"Unknown campus: {campus}. Available: {', '.join(CATALOG_CONFIGS)}")
            sys.exit(1)
        campuses = [campus]
    else:
        campuses = ["LAMC"]  # default

    total = 0
    for campus_code in campuses:
        courses = parse_campus(campus_code, args.source_dir)
        if courses:
            write_output(campus_code, courses, CATALOG_CONFIGS[campus_code]["pdf"])
            total += len(courses)

    print(f"\n{'='*60}")
    print(f"Total: {total} courses across {len(campuses)} campus(es)")


if __name__ == "__main__":
    main()
