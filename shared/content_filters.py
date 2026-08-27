"""Single source of truth for campus RAG store purge/keep/priority patterns.

Both the purge script and crawl script import from here so filtering
logic stays consistent across ingestion and cleanup.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pattern tiers — applied against display_name (filename)
# ---------------------------------------------------------------------------

PURGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("staff_directory_404", re.compile(r"^about_directory_")),
    ("old_events", re.compile(r"^campus_life_events_")),
    ("calendar_pages", re.compile(r"^campus_life_calendar")),
    ("student_gov", re.compile(r"^campus_life_asu")),
    ("news_press", re.compile(r"^news_")),
    ("inter_campus_links", re.compile(r"^colleges_")),
    ("sitemaps", re.compile(r"^sitemap")),
    ("cms_templates", re.compile(r"^pattern_library")),
    ("old_uploads", re.compile(r"^sites_.*_files_20(1[0-9]|2[0-3])_")),
    ("drupal_filter_views", re.compile(r"^academics_aos_field_area")),
    ("drupal_pagination_views", re.compile(r"^academics_aos_page_\d")),
    ("old_calendars", re.compile(
        r"^academics_academic_calendars_"
        r"(fall|spring|summer|winter)_20(1[0-9]|2[0-4])"
    )),
    ("drupal_filter_params", re.compile(r"f_5B\d+_5D")),
    ("pagination", re.compile(r"_page_\d+\.txt$")),
    ("covid_content", re.compile(r"^news_special_covid")),
]

KEEP_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^admissions"),
    re.compile(r"^financial"),
    re.compile(r"^cost_tuition"),
    re.compile(r"^student_services"),
    re.compile(r"^academic_services"),
    re.compile(r"^academics_aos_(?!field_area|page_\d)"),
    re.compile(r"^offices"),
    re.compile(r"^about_accessibility"),
    re.compile(r"^about_accreditation"),
    re.compile(r"^about_contact"),
    re.compile(r"^about_map"),
    re.compile(r"^about_parking"),
    re.compile(r"^campus_life\.txt$"),
    re.compile(r"^campus_life_clubs"),
]

PRIORITY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^admissions"),
    re.compile(r"^financial"),
    re.compile(r"^cost_tuition"),
    re.compile(r"^student_services_counseling"),
    re.compile(r"^academics_aos_(?!field_area|page_\d)"),
    re.compile(r"^academic_services_learning_center"),
    re.compile(r"^about_contact"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _matches_keep(display_name: str) -> bool:
    return any(p.search(display_name) for p in KEEP_PATTERNS)


def _matches_priority(display_name: str) -> bool:
    return any(p.search(display_name) for p in PRIORITY_PATTERNS)


def should_purge(display_name: str) -> bool:
    """Return True if *display_name* matches a PURGE pattern and no KEEP pattern."""
    if _matches_keep(display_name):
        return False
    return any(pat.search(display_name) for _, pat in PURGE_PATTERNS)


def get_purge_reason(display_name: str) -> str | None:
    """Return the label of the first matching PURGE pattern, or None."""
    if _matches_keep(display_name):
        return None
    for label, pat in PURGE_PATTERNS:
        if pat.search(display_name):
            return label
    return None


def classify(display_name: str) -> str:
    """Classify a document as ``"priority"``, ``"keep"``, or ``"purge"``."""
    if _matches_priority(display_name):
        return "priority"
    if should_purge(display_name):
        return "purge"
    return "keep"
