from __future__ import annotations

import asyncio
import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, Optional, Tuple

import httpx
from langfuse import get_client, observe

from app.services.schedule_sections import iter_schedule_sections, normalize_class_number


API_BASE = "https://services.laccd.edu/apps/api/classschedule"
API_TIMEOUT = 30.0
CAMPUS_NAMES = {
    "LAMC": "Los Angeles Mission College",
    "LAVC": "Los Angeles Valley College",
    "LAHC": "Los Angeles Harbor College",
    "LAPC": "Los Angeles Pierce College",
    "LATTC": "Los Angeles Trade-Tech College",
    "LACC": "Los Angeles City College",
    "ELAC": "East Los Angeles College",
    "LASC": "Los Angeles Southwest College",
    "WLAC": "West Los Angeles College",
}
ALL_CAMPUSES = list(CAMPUS_NAMES.keys())
MAX_CACHE_ENTRIES = 256
MAX_UPSTREAM_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PUBLIC_SECTIONS = 100
MAX_GE_SUBJECTS = 12
MAX_SCHEDULE_CONCURRENCY = 6
_subjects_cache: OrderedDict[str, Tuple[list[dict], datetime]] = OrderedDict()
_listing_cache: OrderedDict[Tuple[str, str, str], Tuple[dict, datetime]] = OrderedDict()
_schedule_semaphore = asyncio.Semaphore(MAX_SCHEDULE_CONCURRENCY)
CACHE_TTL = timedelta(hours=1)


def _validate_request(campus: str, term: str) -> tuple[str, str]:
    campus = campus.upper().strip()
    term = term.strip()
    if campus not in CAMPUS_NAMES:
        raise ValueError(f"unknown campus: {campus}")
    if len(term) != 4 or not term.isdigit():
        raise ValueError("term must be a four-digit LACCD term code")
    return campus, term


def _cache_get(cache: OrderedDict, key):
    cached = cache.get(key)
    if not cached:
        return None
    value, cached_at = cached
    if datetime.now() - cached_at >= CACHE_TTL:
        cache.pop(key, None)
        return None
    cache.move_to_end(key)
    return value, cached_at


def _cache_set(cache: OrderedDict, key, value) -> None:
    now = datetime.now()
    for existing_key, (_, cached_at) in list(cache.items()):
        if now - cached_at >= CACHE_TTL:
            cache.pop(existing_key, None)
    cache[key] = (value, now)
    cache.move_to_end(key)
    while len(cache) > MAX_CACHE_ENTRIES:
        cache.popitem(last=False)


async def _get_json_limited(client: httpx.AsyncClient, url: str, *, params=None):
    async with _schedule_semaphore:
        async with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > MAX_UPSTREAM_RESPONSE_BYTES:
                raise ValueError("schedule response exceeds the byte limit")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_UPSTREAM_RESPONSE_BYTES:
                    raise ValueError("schedule response exceeds the byte limit")
    return json.loads(body)


def get_term_name(term_code: str) -> str:
    if len(term_code) == 4 and term_code.startswith("22"):
        year = 2020 + int(term_code[2])
        season = {"2": "Winter", "4": "Spring", "6": "Summer", "8": "Fall"}.get(term_code[3], "Unknown")
        return f"{season} {year}"
    return term_code


@observe(name="schedule.get_subjects")
async def get_subjects(campus: str, term: str) -> list[dict]:
    campus, term = _validate_request(campus, term)
    key = f"{campus}:{term}"
    cached = _cache_get(_subjects_cache, key)
    if cached:
        subjects, cached_at = cached
        get_client().update_current_span(
            metadata={"cache": "hit", "cache_age_seconds": round((datetime.now() - cached_at).total_seconds())},
        )
        return subjects
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        data = await _get_json_limited(client, f"{API_BASE}/subjects/{campus}/{term}")
        _cache_set(_subjects_cache, key, data)
        get_client().update_current_span(metadata={"cache": "miss"})
        return data


def fuzzy_match_subject(query: str, subjects: list[dict], threshold: float = 0.6) -> Optional[str]:
    query_lower = query.lower().strip()
    # Exact code or name first, then the singular ("arts" means Art). Without
    # the singular pass the substring bonus below scores "Media Arts" above Art.
    singular = query_lower[:-1] if query_lower.endswith("s") and len(query_lower) > 3 else None
    for wanted in filter(None, (query_lower, singular)):
        for subject in subjects:
            if wanted in (subject.get("code", "").lower(), subject.get("name", "").lower()):
                return subject.get("code", "").upper()
    best_code = None
    best_score = 0.0
    for subject in subjects:
        code = subject.get("code", "").upper()
        name = subject.get("name", "").lower()
        if query_lower == code.lower() or query_lower == name:
            return code
        ratio = SequenceMatcher(None, query_lower, name).ratio()
        if query_lower in name:
            ratio += 0.35
        if ratio > best_score and ratio >= threshold:
            best_score = ratio
            best_code = code
    return best_code


async def find_subject_code(query: str, campus: str, term: str) -> tuple[str | None, str | None]:
    subjects = await get_subjects(campus, term)
    code = fuzzy_match_subject(query, subjects)
    if not code:
        return None, None
    match = next((item for item in subjects if item.get("code") == code), None)
    return code, (match or {}).get("name")


@observe(name="schedule.get_class_listing")
async def get_class_listing(campus: str, term: str, subjects: list[str] | None = None) -> dict:
    campus, term = _validate_request(campus, term)
    if subjects and len(subjects) > MAX_GE_SUBJECTS:
        raise ValueError(f"at most {MAX_GE_SUBJECTS} subjects may be requested")
    subjects = [subject.strip().upper()[:20] for subject in subjects or [] if subject.strip()]
    # Canonicalize the subjects list so different orderings hit the same cache key.
    subjects_key = ",".join(sorted(subjects)) if subjects else ""
    key = (campus, term, subjects_key)
    cached = _cache_get(_listing_cache, key)
    if cached:
        listing, cached_at = cached
        get_client().update_current_span(
            metadata={"cache": "hit", "cache_age_seconds": round((datetime.now() - cached_at).total_seconds())},
        )
        return listing
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        data = await _get_json_limited(
            client,
            f"{API_BASE}/listing/{campus}/{term}",
            params={"subjectlist": subjects_key},
        )
        _cache_set(_listing_cache, key, data)
        get_client().update_current_span(metadata={"cache": "miss"})
        return data


# Rooms are "<campus>-<building> <room>": "Mission-AMP 302", "Harbor - Student
# Union", "SOUTHWEST-LOCKE". Trade-Tech's prefix carries its own dash
# ("Trade-Tech B3-207", "Trade-Tech -TALLNC-DOM", but "Trade-Online").
_ROOM_PREFIX_RE = re.compile(r"^(?:trade-tech\s*-?\s*|[a-z]+\s*-\s*)", re.IGNORECASE)
_BUILDING_RE = re.compile(r"[A-Za-z0-9/.]+")
# Places a student cannot walk into. Online is a modality, not a building.
_NOT_BUILDINGS = {"", "ONLINE", "OFF", "OFFSITE", "TBA", "SCHEDULE"}


def room_building(room: str | None) -> str:
    """The building code in a schedule room string, upper-cased; "" if none."""
    rest, count = _ROOM_PREFIX_RE.subn("", (room or "").strip(), count=1)
    if not count:
        return ""
    match = _BUILDING_RE.match(rest)
    return match.group(0).upper() if match else ""


def building_codes(listing: dict) -> set[str]:
    """Every building a section in this listing meets in."""
    codes = {
        room_building(meeting.get("room"))
        for _course, section, _primary in iter_schedule_sections(listing)
        for meeting in section.get("meetings") or []
    }
    return codes - _NOT_BUILDINGS


DAY_NAMES = {"M": "Monday", "T": "Tuesday", "W": "Wednesday", "Th": "Thursday",
             "F": "Friday", "S": "Saturday", "Su": "Sunday"}
# Start-time bands. Stated in the answer so a student can see the rule applied.
TIME_OF_DAY_RULES = {
    "morning": "starting before noon",
    "afternoon": "starting between noon and 4:59 PM",
    "evening": "starting at 5 PM or later",
}
_START_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AP]M)", re.IGNORECASE)


def _start_minutes(times: str | None) -> int | None:
    match = _START_RE.match(times or "")
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)) % 12, int(match.group(2)), match.group(3).upper()
    return (hour + (12 if meridiem == "PM" else 0)) * 60 + minute


def _time_of_day(times: str | None) -> str | None:
    start = _start_minutes(times)
    if start is None:
        return None
    if start < 12 * 60:
        return "morning"
    return "afternoon" if start < 17 * 60 else "evening"


@dataclass(frozen=True, slots=True)
class ScheduleFilters:
    """Constraints a single meeting must satisfy together.

    "Saturday in AMP" means one meeting that is both — not a Saturday meeting in
    one room and an AMP meeting on another day.
    """

    days: frozenset[str] = frozenset()  # API day tokens: M T W Th F S Su
    building: str | None = None
    time_of_day: str | None = None  # a TIME_OF_DAY_RULES key
    modality: str | None = None  # "online" | "in_person"

    def __bool__(self) -> bool:
        return bool(self.days or self.building or self.time_of_day or self.modality)

    def without_building(self) -> "ScheduleFilters":
        return ScheduleFilters(days=self.days, time_of_day=self.time_of_day, modality=self.modality)

    def matches(self, meeting: dict) -> bool:
        building = room_building(meeting.get("room"))
        if self.days and not self.days & set((meeting.get("days") or "").split()):
            return False
        if self.building and building != self.building:
            return False
        if self.time_of_day and _time_of_day(meeting.get("times")) != self.time_of_day:
            return False
        if self.modality == "online" and building != "ONLINE":
            return False
        if self.modality == "in_person" and building in _NOT_BUILDINGS:
            return False
        return True

    def describe(self) -> str:
        """" meeting in AMP on Saturday", for answer headers. "" when empty."""
        parts = []
        if self.modality == "online":
            parts.append("online")
        elif self.modality == "in_person":
            parts.append("in person")
        if self.building:
            parts.append(f"in {self.building}")
        if self.days:
            ordered = [DAY_NAMES[d] for d in DAY_NAMES if d in self.days]
            parts.append("on " + " or ".join(ordered))
        if self.time_of_day:
            parts.append(f"in the {self.time_of_day} ({TIME_OF_DAY_RULES[self.time_of_day]})")
        return (" meeting " + " ".join(parts)) if parts else ""


_DAY_WORDS = (
    ("monday", "M"), ("tuesday", "T"), ("wednesday", "W"), ("thursday", "Th"),
    ("friday", "F"), ("saturday", "S"), ("sunday", "Su"),
)
_TIME_OF_DAY_WORDS = (
    ("morning", r"\bmornings?\b"),
    ("afternoon", r"\bafternoons?\b"),
    ("evening", r"\bevenings?\b|\bnights?\b"),
)


def filters_from_text(text: str) -> ScheduleFilters:
    """Day, time-of-day and online/in-person constraints named in the text.

    Shared by web chat (the student's sentence) and voice (the tool arguments).
    Buildings need the campus's own room list, so callers add them.
    """
    lowered = text.lower()
    days = {code for word, code in _DAY_WORDS if re.search(rf"\b{word}s?\b", lowered)}
    if re.search(r"\bweekends?\b", lowered):
        days |= {"S", "Su"}
    # Two different times of day, or both modalities, is no constraint at all.
    times = [name for name, pattern in _TIME_OF_DAY_WORDS if re.search(pattern, lowered)]
    in_person = re.search(r"\bin[\s-]person\b|\bon[\s-]campus\b", lowered) is not None
    online = re.search(r"\bonline\b", lowered) is not None
    return ScheduleFilters(
        days=frozenset(days),
        time_of_day=times[0] if len(times) == 1 else None,
        modality=("in_person" if in_person else "online") if in_person != online else None,
    )


def format_sections(
    listing: dict,
    course_number: str | None = None,
    keyword: str | None = None,
    filters: ScheduleFilters | None = None,
) -> list[dict]:
    rows = []
    for course, section, primary in iter_schedule_sections(listing):
        catalog = course.get("catalogNbr", "")
        if course_number:
            # Normalize both sides: strip leading zeros for comparison (001 vs 1)
            cn = course_number.lstrip("0") or "0"
            cat = catalog.lstrip("0") or "0"
            if cn != cat and not cat.startswith(cn):
                continue
        if keyword and keyword.lower() not in course.get("descr", "").lower():
            continue

        class_number, component = normalize_class_number(section.get("classNbr"))
        meetings = section.get("meetings") or [{}]
        if filters:
            # Any meeting may satisfy the filters; show the one that did.
            meeting = next((m for m in meetings if filters.matches(m)), None)
            if meeting is None:
                continue
        else:
            meeting = meetings[0]
        rows.append(
            {
                "course_code": f"{course.get('subject', '')} {catalog}",
                "course_title": course.get("descr", ""),
                "class_number": class_number,
                "section": section.get("classNbr", ""),
                "days": meeting.get("days", "TBA"),
                "time": meeting.get("times", "TBA"),
                "location": meeting.get("room", "TBA"),
                "instructor": meeting.get("instr", "TBA"),
                "dates": section.get("dates", ""),
                "class_type": section.get("classType", []),
                "component": component,
                "related_to": "" if class_number == primary else primary,
            }
        )
    return rows


async def search_live_schedule(
    campus: str,
    term: str,
    subject: str | None = None,
    course_number: str | None = None,
    keyword: str | None = None,
    filters: ScheduleFilters | None = None,
) -> dict:
    listing = await get_class_listing(campus, term, [subject] if subject else None)
    all_sections = format_sections(listing, course_number=course_number, keyword=keyword, filters=filters)
    sections = all_sections[:MAX_PUBLIC_SECTIONS]
    return {
        "campus": campus,
        "campus_name": CAMPUS_NAMES.get(campus, listing.get("campusname", campus)),
        "term": term,
        "term_name": listing.get("termname", get_term_name(term)),
        "subject": subject,
        "course_number": course_number,
        "keyword": keyword,
        "sections": sections,
        "total_sections": len(all_sections),
        "returned_sections": len(sections),
        "truncated": len(sections) < len(all_sections),
    }


async def search_ztc_sections(campus: str, term: str, subject: str | None = None) -> dict:
    """Fetch schedule listing and filter to ZTC (OER) and Low Cost (LCT) sections only."""
    listing = await get_class_listing(campus, term, [subject] if subject else None)
    all_sections = format_sections(listing)
    all_ztc_sections = [s for s in all_sections if "OER" in s.get("class_type", []) or "LCT" in s.get("class_type", [])]
    ztc_sections = all_ztc_sections[:MAX_PUBLIC_SECTIONS]
    return {
        "campus": campus,
        "campus_name": CAMPUS_NAMES.get(campus, listing.get("campusname", campus)),
        "term": term,
        "term_name": listing.get("termname", get_term_name(term)),
        "subject": subject,
        "sections": ztc_sections,
        "total_sections": len(all_ztc_sections),
        "returned_sections": len(ztc_sections),
        "truncated": len(ztc_sections) < len(all_ztc_sections),
    }


async def search_all_campuses(
    term: str,
    subject: str | None = None,
    course_number: str | None = None,
    keyword: str | None = None,
    primary_campus: str = "LAMC",
    filters: ScheduleFilters | None = None,
) -> dict:
    # Building codes are campus-local, so only the other filters travel.
    filters = filters.without_building() if filters else None

    async def fetch(campus_code: str) -> tuple[str, dict]:
        return campus_code, await search_live_schedule(
            campus_code, term=term, subject=subject, course_number=course_number, keyword=keyword,
            filters=filters,
        )

    results = await asyncio.gather(*[fetch(code) for code in ALL_CAMPUSES], return_exceptions=True)
    campuses = {}
    total_sections = 0
    ordered = [primary_campus] + [code for code in ALL_CAMPUSES if code != primary_campus]
    indexed = {
        campus_code: payload
        for campus_code, payload in (result for result in results if isinstance(result, tuple))
    }
    for campus_code in ordered:
        payload = indexed.get(campus_code)
        if payload and payload.get("sections"):
            campuses[campus_code] = {
                "campus_name": payload["campus_name"],
                "sections": payload["sections"],
                "section_count": payload["total_sections"],
                "returned_sections": payload.get("returned_sections", len(payload["sections"])),
                "truncated": payload.get("truncated", False),
            }
            total_sections += payload["total_sections"]
    return {
        "term": term,
        "term_name": get_term_name(term),
        "subject": subject,
        "course_number": course_number,
        "keyword": keyword,
        "primary_campus": primary_campus,
        "campuses": campuses,
        "total_sections": total_sections,
    }
