"""Parsing a superseded catalog must not pass silently.

`CATALOG_CONFIGS` pins both a PDF filename AND edition-specific page ranges
("start": 93, "end": 154), so a new edition cannot simply be dropped in — the
ranges have to be re-derived. The failure mode that actually bit us is quieter
than a crash: the pinned 2025-2026 filename kept parsing happily while the
colleges published 2026-2027, and nothing anywhere said so.

LAMC, WLAC and LAHC had published 2026-2027 editions while the local source
collection still held 2025-2026 for all nine campuses.
"""

from __future__ import annotations

import pytest

from scripts.parse_catalog_pdf import catalog_edition, newer_catalog_available


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("LAMC-Catalog-2025-2026.pdf", (2025, 2026)),
        ("LAMC-Catalog-2026-2027.pdf", (2026, 2027)),
        ("Los Angeles Pierce College 2025-2026 General Catalog.pdf", (2025, 2026)),
        ("2026_2027 catalog.pdf", (2026, 2027)),
        ("LATTC 2025-26 Catalog-web.pdf", (2025, 2026)),
        ("catalog.pdf", None),
    ],
)
def test_edition_is_read_from_the_filename(filename, expected):
    assert catalog_edition(filename) == expected


def test_a_newer_edition_on_disk_is_reported(tmp_path):
    (tmp_path / "LAMC-Catalog-2025-2026.pdf").touch()
    (tmp_path / "LAMC-Catalog-2026-2027.pdf").touch()
    found = newer_catalog_available("LAMC-Catalog-2025-2026.pdf", tmp_path)
    assert found == "LAMC-Catalog-2026-2027.pdf"


def test_no_complaint_when_the_configured_edition_is_the_newest(tmp_path):
    (tmp_path / "LAMC-Catalog-2025-2026.pdf").touch()
    (tmp_path / "LAMC-Catalog-2026-2027.pdf").touch()
    assert newer_catalog_available("LAMC-Catalog-2026-2027.pdf", tmp_path) is None


def test_unrelated_campuses_are_not_compared(tmp_path):
    """A newer WLAC catalog says nothing about LAMC's edition."""
    (tmp_path / "LAMC-Catalog-2025-2026.pdf").touch()
    (tmp_path / "WLAC-Catalog-2026-2027.pdf").touch()
    assert newer_catalog_available("LAMC-Catalog-2025-2026.pdf", tmp_path) is None


def test_an_undated_filename_is_never_flagged(tmp_path):
    (tmp_path / "catalog.pdf").touch()
    (tmp_path / "LAMC-Catalog-2026-2027.pdf").touch()
    assert newer_catalog_available("catalog.pdf", tmp_path) is None
