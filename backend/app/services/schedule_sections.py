from __future__ import annotations

from collections.abc import Iterator
import logging
import re


logger = logging.getLogger(__name__)

_CLASS_COMPONENT_RE = re.compile(r"^([^\s()]+)\s+\(([A-Za-z]+)\)$")


def normalize_class_number(raw: object) -> tuple[str, str]:
    value = str(raw or "").strip()
    match = _CLASS_COMPONENT_RE.fullmatch(value)
    return (match.group(1), match.group(2).upper()) if match else (value, "")


def iter_schedule_sections(listing: dict) -> Iterator[tuple[dict, dict, str]]:
    for subject in listing.get("subjects") or []:
        for course in subject.get("courses") or []:
            seen: set[str] = set()

            def walk(section: dict, primary: str) -> Iterator[tuple[dict, dict, str]]:
                number, _component = normalize_class_number(section.get("classNbr"))
                if not number:
                    logger.warning("schedule component has blank classNbr")
                    for child in section.get("relsections") or []:
                        yield from walk(child, primary)
                    return
                if number in seen:
                    logger.warning("duplicate schedule class number %s", number)
                    for child in section.get("relsections") or []:
                        yield from walk(child, primary)
                    return
                seen.add(number)
                yield course, section, primary
                for child in section.get("relsections") or []:
                    yield from walk(child, primary)

            for primary_section in course.get("sections") or []:
                primary, _component = normalize_class_number(primary_section.get("classNbr"))
                if not primary:
                    logger.warning(
                        "primary schedule component has blank classNbr; skipping subtree"
                    )
                    continue
                yield from walk(primary_section, primary)
