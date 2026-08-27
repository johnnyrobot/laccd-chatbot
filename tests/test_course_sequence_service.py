from __future__ import annotations

from app.services.course_sequence_service import CourseSequenceService


def _course(code: str, term: object = 1, units: object = 3, title: str | None = None, requirement: object | None = None) -> dict:
    return {
        "name": code,
        "shortDescription": title or code,
        "recommendedOpportunity": {
            "term": {"termNumber": term},
            "minUnits": units,
        },
        "requirement": {"requirementType": "MAJOR_REQUIRED"} if requirement is None else requirement,
    }


def _svc() -> CourseSequenceService:
    return CourseSequenceService()


def _codes_by_term(terms: list[dict]) -> list[list[str]]:
    return [[course["code"] for course in term["courses"]] for term in terms]


def test_pathway_parser_handles_malformed_elements_without_throwing():
    svc = _svc()
    elements = [
        _course("CIS 101", term=None, units="3-4", requirement=None),
        {
            "name": "CIS 102",
            "shortDescription": "Programming Logic",
            "recommendedOpportunity": None,
            "requirement": {"requirementType": "MAJOR_REQUIRED"},
        },
        {
            "name": "CIS 103",
            "shortDescription": "Applications",
            "recommendedOpportunity": {"term": None, "minUnits": ""},
            "requirement": {"requirementType": "MAJOR_REQUIRED"},
        },
        {"name": "", "shortDescription": "", "recommendedOpportunity": None},
        "not an object",
    ]

    terms, _chains, diagnostics = svc._topological_sort_with_terms(
        elements,
        {},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CIS 101", "CIS 102", "CIS 103"]]
    assert terms[0]["total_units"] == 9.0
    assert len(diagnostics["skipped_elements"]) == 2


def test_pm_term_order_does_not_collapse_ready_courses_into_one_semester():
    svc = _svc()
    elements = [
        _course("CIS 101", term=1),
        _course("CIS 201", term=2),
        _course("CIS 202", term=3),
    ]

    terms, _chains, _diagnostics = svc._topological_sort_with_terms(
        elements,
        {},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CIS 101"], ["CIS 201"], ["CIS 202"]]


def test_hard_prerequisite_chain_pushes_courses_later_even_when_pm_terms_match():
    svc = _svc()
    elements = [
        _course("CIS 202", term=1),
        _course("CIS 101", term=1),
        _course("CIS 201", term=1),
    ]

    terms, chains, _diagnostics = svc._topological_sort_with_terms(
        elements,
        {"CIS 201": [["CIS 101"]], "CIS 202": [["CIS 201"]]},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CIS 101"], ["CIS 201"], ["CIS 202"]]
    assert chains == [["CIS 101", "CIS 201", "CIS 202"]]


def test_external_prerequisites_warn_without_blocking_sequence():
    svc = _svc()
    elements = [
        _course("CIS 101", term=1, units=3),
        _course("CIS 201", term=1, units=4),
    ]

    terms, chains, diagnostics = svc._topological_sort_with_terms(
        elements,
        {"CIS 201": [["MATH 125"]]},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CIS 101", "CIS 201"]]
    cis_201 = terms[0]["courses"][1]
    assert cis_201["prerequisites"] == ["MATH 125"]
    assert cis_201["prerequisites_met"] is False
    assert diagnostics["external_prerequisites"] == {"CIS 201": ["MATH 125"]}
    assert chains == [["MATH 125", "CIS 201"]]


def test_corequisites_are_scheduled_atomically_even_when_group_exceeds_cap():
    svc = _svc()
    elements = [
        _course("CHEM 101", term=1, units=4),
        _course("CHEM 101L", term=1, units=1),
    ]

    terms, _chains, _diagnostics = svc._topological_sort_with_terms(
        elements,
        {},
        set(),
        4,
        coreq_groups=[{"CHEM 101", "CHEM 101L"}],
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CHEM 101", "CHEM 101L"]]
    assert terms[0]["total_units"] == 5.0


def test_generic_electives_are_placed_at_or_after_pm_term():
    svc = _svc()
    elements = [
        _course("CIS 101", term=1, units=3),
        {
            "name": "GE Area C Elective",
            "shortDescription": "Arts and Humanities",
            "recommendedOpportunity": {"term": {"termNumber": 2}, "minUnits": 3},
            "requirement": {"requirementType": "GENERAL_ED"},
        },
    ]

    terms, _chains, _diagnostics = svc._topological_sort_with_terms(
        elements,
        {},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["CIS 101"], ["GE Area C Elective"]]


def test_ccn_and_multiword_course_codes_are_real_courses_not_generics():
    svc = _svc()
    elements = [
        _course("STAT C1000", term=1),
        _course("ADM JUS 001", term=1),
        _course("CH DEV 001", term=1),
    ]

    terms, _chains, _diagnostics = svc._topological_sort_with_terms(
        elements,
        {},
        set(),
        15,
        include_diagnostics=True,
    )

    assert _codes_by_term(terms) == [["ADM JUS 001", "CH DEV 001", "STAT C1000"]]
    assert all(course["requirement_type"] == "Major Required" for course in terms[0]["courses"])
