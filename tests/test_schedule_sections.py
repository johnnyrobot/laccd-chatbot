from __future__ import annotations

import logging

from app.services.schedule_sections import (
    iter_schedule_sections,
    normalize_class_number,
)


def _listing(*courses: dict) -> dict:
    return {"subjects": [{"courses": list(courses)}]}


def test_iter_schedule_sections_yields_primary_before_nested_descendants() -> None:
    primary = {
        "classNbr": "10001 (LEC)",
        "relsections": [
            {
                "classNbr": "10002 (LAB)",
                "relsections": [{"classNbr": "10003 (LAB)"}],
            }
        ],
    }
    course = {"courseCode": "MATH 001", "sections": [primary]}

    rows = list(iter_schedule_sections(_listing(course)))

    assert [
        (normalize_class_number(section["classNbr"])[0], primary_number)
        for _course, section, primary_number in rows
    ] == [
        ("10001", "10001"),
        ("10002", "10001"),
        ("10003", "10001"),
    ]


def test_iter_schedule_sections_keeps_the_first_duplicate_record_per_course(
    caplog,
) -> None:
    first = {"classNbr": "20001 (LEC)", "marker": "first"}
    duplicate = {"classNbr": "20001 (LAB)", "marker": "duplicate"}
    course = {"courseCode": "MATH 002", "sections": [first, duplicate]}

    with caplog.at_level(logging.WARNING):
        rows = list(iter_schedule_sections(_listing(course)))

    assert [(section["classNbr"], section["marker"]) for _, section, _ in rows] == [
        ("20001 (LEC)", "first"),
    ]
    assert "duplicate schedule class number 20001" in caplog.text


def test_iter_schedule_sections_allows_same_class_number_in_different_courses() -> None:
    first_course = {
        "courseCode": "MATH 003",
        "sections": [{"classNbr": "30001 (LEC)"}],
    }
    second_course = {
        "courseCode": "MATH 004",
        "sections": [{"classNbr": "30001 (LEC)"}],
    }

    rows = list(iter_schedule_sections(_listing(first_course, second_course)))

    assert [(course["courseCode"], primary) for course, _, primary in rows] == [
        ("MATH 003", "30001"),
        ("MATH 004", "30001"),
    ]


def test_normalize_class_number_preserves_malformed_values() -> None:
    assert normalize_class_number(" 10001 (lab) ") == ("10001", "LAB")
    assert normalize_class_number("10001 (LAB 01)") == ("10001 (LAB 01)", "")
    assert normalize_class_number(10001) == ("10001", "")
    assert normalize_class_number(None) == ("", "")


def test_iter_schedule_sections_skips_blank_related_record_but_visits_children(
    caplog,
) -> None:
    course = {
        "courseCode": "MATH 005",
        "sections": [
            {
                "classNbr": "50001 (LEC)",
                "relsections": [
                    {
                        "classNbr": "  ",
                        "relsections": [{"classNbr": "50003 (LAB)"}],
                    }
                ],
            }
        ],
    }

    with caplog.at_level(logging.WARNING):
        rows = list(iter_schedule_sections(_listing(course)))

    assert [
        (normalize_class_number(section["classNbr"])[0], primary)
        for _, section, primary in rows
    ] == [("50001", "50001"), ("50003", "50001")]
    assert "schedule component has blank classNbr" in caplog.text


def test_iter_schedule_sections_skips_blank_primary_subtree(caplog) -> None:
    course = {
        "courseCode": "MATH 006",
        "sections": [
            {
                "classNbr": "",
                "relsections": [{"classNbr": "60002 (LAB)"}],
            }
        ],
    }

    with caplog.at_level(logging.WARNING):
        rows = list(iter_schedule_sections(_listing(course)))

    assert rows == []
    assert "primary schedule component has blank classNbr; skipping subtree" in caplog.text


def test_iter_schedule_sections_visits_unique_descendant_of_duplicate(caplog) -> None:
    course = {
        "courseCode": "MATH 007",
        "sections": [
            {
                "classNbr": "70001 (LEC)",
                "relsections": [
                    {
                        "classNbr": "70001 (LAB)",
                        "relsections": [{"classNbr": "70003 (LAB)"}],
                    }
                ],
            }
        ],
    }

    with caplog.at_level(logging.WARNING):
        rows = list(iter_schedule_sections(_listing(course)))

    assert [
        (normalize_class_number(section["classNbr"])[0], primary)
        for _, section, primary in rows
    ] == [("70001", "70001"), ("70003", "70001")]
    assert "duplicate schedule class number 70001" in caplog.text


def test_iter_schedule_sections_tolerates_null_collections() -> None:
    assert list(iter_schedule_sections({"subjects": None})) == []
    assert list(iter_schedule_sections({"subjects": [{"courses": None}]})) == []
    assert list(
        iter_schedule_sections({"subjects": [{"courses": [{"sections": None}]}]})
    ) == []

    course = {
        "courseCode": "MATH 008",
        "sections": [{"classNbr": "80001 (LEC)", "relsections": None}],
    }
    rows = list(iter_schedule_sections(_listing(course)))
    assert [(section["classNbr"], primary) for _, section, primary in rows] == [
        ("80001 (LEC)", "80001")
    ]
