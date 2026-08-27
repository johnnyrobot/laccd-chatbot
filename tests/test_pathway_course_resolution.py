from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAME = "sync_programs_pathway_under_test"


def _load():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    path = PROJECT_ROOT / "scripts" / "sync_programs_to_neo4j.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# Real LACC Course.code values from prod, and real LACC pathway element names
# from the recorded dump. LACC emits codes with no separator ("MUSIC200"), which
# is why _PATHWAY_COURSE_RE -- it requires \s+ -- matched nothing and the campus
# had 0 REQUIRES_COURSE edges for the life of the graph.
LACC_INDEX = {
    "MUSIC200": "MUSIC 200",
    "ENGLC1000": "ENGL C1000",
    "CINEMA001": "CINEMA 001",
    "CINEMA1": "CINEMA 1",
    "MATH262": "MATH 262",
    "CAOT084": "CAOT 084",
    "ASL001": "A S L 001",
    "CS101": "CS 101",
}


def test_resolves_a_concatenated_lacc_code():
    mod = _load()
    assert mod.resolve_pathway_course_code("MUSIC200", LACC_INDEX) == "MUSIC 200"


def test_resolves_a_c_prefixed_ccn_code_that_regex_splitting_would_get_wrong():
    """ENGLC1000 is ENGL + C1000, not ENGLC + 1000. Splitting on the first
    digit gives the wrong subject, so resolution goes through the campus's
    real course list instead of guessing."""
    mod = _load()
    assert mod.resolve_pathway_course_code("ENGLC1000", LACC_INDEX) == "ENGL C1000"


def test_resolves_a_multi_word_subject():
    mod = _load()
    assert mod.resolve_pathway_course_code("ASL001", LACC_INDEX) == "A S L 001"


def test_still_resolves_the_spaced_form_other_campuses_use():
    mod = _load()
    assert mod.resolve_pathway_course_code("MATH 262", LACC_INDEX) == "MATH 262"


def test_does_not_resolve_placeholder_elements():
    """The failure mode to avoid: matching junk and inventing edges.

    'ADDITIONAL COURSE' and GE-area headings are real pathway element names.
    """
    mod = _load()
    for junk in (
        "ADDITIONAL COURSE",
        "ADDITIONAL COURSES",
        "Cal-GETC Area 4: Social and Behavioral Sciences",
        "LACC GE Area 1A: English Composition",
        "First Semester Milestones",
        "",
    ):
        assert mod.resolve_pathway_course_code(junk, LACC_INDEX) is None, junk


def test_does_not_resolve_a_course_the_campus_does_not_have():
    mod = _load()
    assert mod.resolve_pathway_course_code("PHYSICS999", LACC_INDEX) is None
