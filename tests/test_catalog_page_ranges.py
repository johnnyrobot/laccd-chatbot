"""Catalog page ranges must stay inside each PDF and cover the complete course
description section. Entry counts and declared running headers provide the
available continuation signals; edition-specific start pages remain pinned.
"""

from __future__ import annotations

import pdfplumber
import pytest

from scripts.parse_catalog_pdf import (
    CATALOG_CONFIGS,
    DEFAULT_SOURCE_DIR,
    continues_section,
    course_header_count,
    looks_like_course_page,
    running_header,
)


CAMPUSES = sorted(CATALOG_CONFIGS)


def _page_text(pdf_path, index: int) -> str | None:
    with pdfplumber.open(pdf_path) as doc:
        if index < 0 or index >= len(doc.pages):
            return None
        return doc.pages[index].extract_text()


def _page_count(pdf_path) -> int:
    with pdfplumber.open(pdf_path) as doc:
        return len(doc.pages)


def test_a_page_of_course_entries_is_recognised():
    assert looks_like_course_page(
        "PHILOS 042: Philosophy of Religion\nPHYS SC 001: Physical Science\n"
    )


def test_prose_is_not_mistaken_for_course_entries():
    assert not looks_like_course_page(
        "Admissions Policies & Information\n"
        "Students living outside the district may apply online.\n"
    )
    assert course_header_count(None) == 0


@pytest.mark.parametrize("campus", CAMPUSES)
def test_end_page_is_inside_the_pdf(campus):
    """LAPC pinned end=350 against a 348-page PDF."""
    cfg = CATALOG_CONFIGS[campus]
    pdf = DEFAULT_SOURCE_DIR / cfg["pdf"]
    if not pdf.exists():
        pytest.skip(f"{cfg['pdf']} not present")
    pages = _page_count(pdf)
    assert cfg["start"] < cfg["end"] < pages, (
        f"{campus}: end={cfg['end']} outside a {pages}-page PDF"
    )


@pytest.mark.parametrize("campus", CAMPUSES)
def test_course_descriptions_do_not_continue_past_the_end_page(campus):
    """A short range must fail rather than silently drop trailing courses."""
    cfg = CATALOG_CONFIGS[campus]
    pdf = DEFAULT_SOURCE_DIR / cfg["pdf"]
    if not pdf.exists():
        pytest.skip(f"{cfg['pdf']} not present")
    after = _page_text(pdf, cfg["end"] + 1)
    if after is None:
        return  # end is the last page; the range check above owns that case
    assert not looks_like_course_page(after), (
        f"{campus}: page {cfg['end'] + 1} still holds "
        f"{course_header_count(after)} course entries, so end={cfg['end']} "
        f"truncates the section and those courses are silently dropped"
    )


def test_a_declared_section_header_is_recognised():
    assert continues_section("Theater Arts Department\n293 Rehearsals\n", "ELAC")
    assert continues_section("Course Descriptions\n262 Welding II (3)\n", "LAPC")


def test_prose_after_the_section_is_not_mistaken_for_it():
    assert not continues_section("History and District Mission and Vision\n", "ELAC")
    assert not continues_section("Scholastic Policies\n", "LAPC")


def test_campuses_without_a_declared_header_have_no_opinion():
    """`None` means "fall back to the entry count", not "this page is fine"."""
    assert continues_section("ACCTG 001: Bookkeeping\n", "LAMC") is None
    assert running_header("\n\n  COURSE DESCRIPTIONS  \nACCTG 001\n") == "COURSE DESCRIPTIONS"


@pytest.mark.parametrize("campus", CAMPUSES)
def test_the_section_header_does_not_continue_past_the_end_page(campus):
    """The dept_grouped half of the guard — this is what caught ELAC at 369."""
    cfg = CATALOG_CONFIGS[campus]
    if "section_header" not in cfg:
        pytest.skip(f"{campus} declares no section_header")
    pdf = DEFAULT_SOURCE_DIR / cfg["pdf"]
    if not pdf.exists():
        pytest.skip(f"{cfg['pdf']} not present")
    after = _page_text(pdf, cfg["end"] + 1)
    if after is None:
        return
    assert not continues_section(after, campus), (
        f"{campus}: page {cfg['end'] + 1} still carries the course-section "
        f"running header {running_header(after)!r}, so end={cfg['end']} "
        f"truncates the section"
    )


@pytest.mark.parametrize("campus", CAMPUSES)
def test_the_start_page_is_where_the_section_begins(campus):
    """The `start` side has no automated guard. This pins the corrected values so
    they cannot regress, and records why the obvious check does not work.

    Mirroring the end check — "the page before `start` must not be course
    descriptions" — was tried and removed. It fired on LACC and LAMC, and both
    were false positives: the page before those sections is a cross-reference
    table ("COURSE FAMILIES": `Ceramics - A01 ART 709, 710, 711`), whose lines
    satisfy `_COURSE_HEADER_RE` as readily as a real entry does. It cannot be
    fixed by raising the threshold, because the interior it would be compared
    against scores 0 on six of the nine campuses (see the module docstring).

    So these are hand-verified against the PDFs and asserted as constants, keyed
    by PDF filename rather than campus: `start` is edition-specific, so adopting
    a new catalog must re-verify it rather than inherit the old edition's number.
    """
    expected_start = {
        "ELAC-Catalog-2025-2026.pdf": 66,
        "LACC-Catalog-2025-2026.pdf": 229,
        "LAHC-Catalog-2025-2026.pdf": 125,
        "LAHC-Catalog-2026-2027.pdf": 184,
        "LAMC-Catalog-2025-2026.pdf": 88,
        "LAMC-Catalog-2026-2027.pdf": 88,
        "Los Angeles Pierce College 2025-2026 General Catalog.pdf": 155,
        "LASC-Catalog-2025-2026.pdf": 173,
        "LATTC-Catalog-2025-2026.pdf": 221,
        "LAVC-Catalog-2025-2026.pdf": 162,
        "WLAC-Catalog-2025-2026.pdf": 169,
        "WLAC-Catalog-2026-2027.pdf": 158,
    }
    cfg = CATALOG_CONFIGS[campus]
    pdf_name = cfg["pdf"]
    assert pdf_name in expected_start, (
        f"{campus} points at {pdf_name!r}, which has no hand-verified start. "
        f"Open the PDF, find the first course-description page, and add it here."
    )
    assert cfg["start"] == expected_start[pdf_name], (
        f"{campus}: start changed from the hand-verified value "
        f"{expected_start[pdf_name]} for {pdf_name}; re-check the first course page"
    )
