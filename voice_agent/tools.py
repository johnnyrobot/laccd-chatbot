"""Service wrappers for the LAMC voice agent function tools.

Each function wraps existing backend services from backend/app/services/
and returns plain-text results suitable for voice output.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import replace

from app.services.crawl4ai_service import Crawl4AIService
from app.services.gemini_file_search import GeminiFileSearchService
from app.services.chat import ChatOrchestrator
from app.services.neo4j_service import neo4j_service
from langfuse import observe

from app.services.live_schedule import (
    ALL_CAMPUSES,
    CAMPUS_NAMES,
    ScheduleFilters,
    building_codes,
    filters_from_text,
    find_subject_code,
    get_class_listing,
    get_term_name,
    search_all_campuses,
    search_live_schedule,
    search_ztc_sections,
)
from app.services.course_sequence_service import (
    get_course_sequence_service,
    format_sequence_for_context,
)
from app.services.openstax import get_openstax_suggestions_multi
from app.services.program_mapper import (
    COLLEGE_CONFIGS,
    get_program_mapper_client,
    search_programs_all_campuses,
)
from shared.campus_config import CAMPUSES, get_campus
from shared.program_matching import select_explicit_program
from shared.prompts import SYSTEM_PROMPT, get_system_prompt
from shared.terms import resolve_term_code

logger = logging.getLogger(__name__)

# ── Thread-safe service singletons ────────────────────

_file_search: GeminiFileSearchService | None = None
_crawler: Crawl4AIService | None = None
_chat_orchestrator: ChatOrchestrator | None = None
_init_lock = threading.Lock()


def get_file_search() -> GeminiFileSearchService:
    global _file_search
    if _file_search is None:
        with _init_lock:
            if _file_search is None:
                _file_search = GeminiFileSearchService()
    return _file_search


def get_crawler() -> Crawl4AIService:
    global _crawler
    if _crawler is None:
        with _init_lock:
            if _crawler is None:
                _crawler = Crawl4AIService()
    return _crawler


def get_chat_orchestrator() -> ChatOrchestrator:
    global _chat_orchestrator
    if _chat_orchestrator is None:
        with _init_lock:
            if _chat_orchestrator is None:
                _chat_orchestrator = ChatOrchestrator()
    return _chat_orchestrator


# ── Term resolution ───────────────────────────────────

DEFAULT_CAMPUS = "LAMC"

def resolve_term(term: str | None) -> str:
    """Convert a natural-language term like 'Summer 2026' to a 4-digit code.

    A finite lookup table and hard-coded default become stale as new terms are
    published. Chat and voice therefore call the same calendar-derived rule.
    """
    return resolve_term_code(term)


# ── Voice formatters ──────────────────────────────────


def _schedule_coverage_voice(sections: list[dict], course_limit: int = 12) -> str:
    """Summarize all schedule courses before the spoken detail cap."""
    courses = list(dict.fromkeys(s.get("course_code", "") for s in sections if s.get("course_code")))
    shown = courses[:course_limit]
    coverage = f"{len(courses)} courses and {len(sections)} components"
    if shown:
        coverage += f": {', '.join(shown)}"
    remaining = len(courses) - len(shown)
    if remaining:
        coverage += f". Plus {remaining} additional course{'s' if remaining != 1 else ''}."
    return coverage


def _fmt_sections_voice(sections: list[dict], limit: int = 8, show_location: bool = False) -> str:
    """Format schedule sections as readable plain text for voice."""
    if not sections:
        return "No sections found."
    lines = []
    for s in sections[:limit]:
        parts = [s["course_code"]]
        if s.get("course_title"):
            parts.append(s["course_title"])
        days = s.get("days", "TBA")
        time_ = s.get("time", "TBA")
        if days not in ("TBA", "") and time_ not in ("TBA", ""):
            parts.append(f"{days} {time_}")
        elif days not in ("TBA", ""):
            parts.append(days)
        else:
            parts.append("Online or TBA")
        location = s.get("location", "TBA")
        if show_location and location not in ("TBA", "") and "online" not in location.lower():
            parts.append(f"in {location}")
        instructor = s.get("instructor", "TBA")
        if instructor not in ("TBA", ""):
            parts.append(f"with {instructor}")
        lines.append(", ".join(parts))
    text = _schedule_coverage_voice(sections) + "\n\n" + "\n".join(lines)
    remaining = len(sections) - min(len(sections), limit)
    if remaining > 0:
        text += f"\n\nPlus {remaining} additional component{'s' if remaining != 1 else ''}. Students can view the full list on the LACCD class search website."
    return text


def _fmt_program_voice(bundle: dict) -> str:
    """Format program bundle as readable plain text for voice."""
    program = bundle["program"]
    requirements = bundle.get("requirements") or {}
    careers = bundle.get("careers") or []

    lines = [f"{program.get('title')} at {bundle['campus_name']}."]

    desc = program.get("description", "")
    if desc:
        if len(desc) > 300:
            desc = desc[:300].rsplit(" ", 1)[0] + "..."
        lines.append(desc)

    elements = requirements.get("pathwayElements", [])[:6]
    if elements:
        lines.append("\nKey requirements:")
        for el in elements:
            name = el.get("name", "")
            units = el.get("recommendedOpportunity", {}).get("minUnits", "")
            if name:
                unit_str = f" ({units} units)" if units else ""
                lines.append(f"  {name}{unit_str}")

    if careers:
        lines.append("\nCareer examples:")
        for c in careers[:3]:
            title = c.get("title", "")
            salary = c.get("averageSalaryK", "")
            if title:
                salary_str = f", average salary ${salary}K" if salary else ""
                lines.append(f"  {title}{salary_str}")

    return "\n".join(lines)


# ── Tool implementations ──────────────────────────────


@observe(name="voice.rag_answer")
async def rag_answer(question: str, campus_code: str = "LAMC") -> str:
    """Query Gemini File Search RAG for campus information."""
    started = time.perf_counter()
    fs = get_file_search()
    if not fs.is_configured():
        return "The campus knowledge base is not currently available."
    campus = get_campus(campus_code)
    prompt = get_system_prompt(campus_code, campus.name)
    answer, _citations, route = await asyncio.to_thread(
        fs.answer,
        question,
        prompt,
        campus_code,
        thinking_budget=0,
    )
    logger.info(
        "Voice RAG completed: campus=%s route=%s latency_ms=%d answer_chars=%d",
        campus.code,
        route,
        round((time.perf_counter() - started) * 1000),
        len(answer or ""),
    )
    return answer or "I couldn't find specific information about that in the campus knowledge base."


def _voice_plain_text(text: str, max_chars: int = 3600) -> str:
    """Remove visual markdown while retaining facts for the realtime model."""

    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#`]+", "", text)
    text = re.sub(r"(?m)^\s*-\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars].rsplit("\n", 1)[0].rstrip()
    return clipped + "\nI found additional details; ask me to continue if you want the rest."


def _voice_district_ge_summary(payload: dict) -> str:
    """Compress a district GE/schedule result without dropping any college."""

    campuses = payload.get("campuses") or {}
    lines = [
        f"{payload.get('system', 'GE')} Area {payload.get('ge_code', '')} has "
        f"{payload.get('offered_sections', 0)} offered sections across "
        f"{len(campuses)} colleges. {payload.get('cost_sections', 0)} sections "
        "are marked Zero Textbook Cost or Low Cost.",
        "",
    ]
    for campus in campuses.values():
        sections = campus.get("offered_sections") or []
        course_count = len(
            {section.get("course_code") for section in sections if section.get("course_code")}
        )
        cost_sections = [
            section
            for section in sections
            if {"OER", "LCT"}.intersection(section.get("class_type") or [])
        ]
        cost_codes = sorted(
            {section.get("course_code") for section in cost_sections if section.get("course_code")}
        )
        cost_detail = (
            f" Cost-marked examples include {', '.join(cost_codes[:3])}."
            if cost_codes
            else " No sections are currently marked Zero Textbook Cost or Low Cost."
        )
        lines.append(
            f"{campus.get('campus_name', 'College')}: {course_count} courses in "
            f"{len(sections)} sections; {len(cost_sections)} cost-marked sections."
            f"{cost_detail}"
        )
    lines.extend(
        [
            "",
            "These are published offerings, not reliable live seat counts. "
            "Ask me for one college if you want specific section times.",
        ]
    )
    return "\n".join(lines)


@observe(name="voice.academic_answer")
async def academic_answer(question: str, campus_code: str = "LAMC") -> str:
    """Route a multi-fact academic question through the text-chat orchestrator."""

    started = time.perf_counter()
    response = await get_chat_orchestrator().answer(
        question,
        campus=campus_code,
    )
    logger.info(
        "Voice academic router completed: campus=%s route=%s latency_ms=%d answer_chars=%d",
        campus_code,
        response.route,
        round((time.perf_counter() - started) * 1000),
        len(response.answer or ""),
    )
    if (
        response.route == "ge_schedule_cross_ref"
        and isinstance(response.tool_payload, dict)
        and response.tool_payload.get("scope") == "district"
    ):
        return _voice_district_ge_summary(response.tool_payload)
    return _voice_plain_text(response.answer)


# Callers and the realtime model shorten days ("Sat", "Tues/Thurs"). The shared
# parser reads only full names, which is right for chat sentences ("I sat in").
_DAY_ABBREVIATIONS = {
    "mon": "monday", "tu": "tuesday", "tue": "tuesday", "tues": "tuesday",
    "wed": "wednesday", "th": "thursday", "thu": "thursday", "thur": "thursday",
    "thurs": "thursday", "fri": "friday", "sat": "saturday", "sun": "sunday",
}
_FILTER_HELP = (
    "Days must be spelled out (Saturday, Tuesday Thursday, weekend), time of day "
    "must be morning, afternoon, or evening, and format must be online or in person."
)


def _voice_filters(
    days: str = "", time_of_day: str = "", modality: str = "",
) -> tuple[ScheduleFilters, list[str]]:
    """Schedule filters from tool arguments, plus any argument that wasn't understood.

    An unreadable value is reported, never dropped: searching without it would
    answer a different question than the caller asked.
    """
    problems = []
    day_words = " ".join(_DAY_ABBREVIATIONS.get(w, w) for w in re.findall(r"[a-z]+", days.lower()))
    parsed_days = filters_from_text(day_words).days
    if days.strip() and not parsed_days:
        problems.append(f"days '{days}'")
    time_band = filters_from_text(time_of_day).time_of_day
    if time_of_day.strip() and not time_band:
        problems.append(f"time of day '{time_of_day}'")
    mode = filters_from_text(modality.replace("_", " ")).modality
    if modality.strip() and not mode:
        problems.append(f"class format '{modality}'")
    return ScheduleFilters(days=parsed_days, time_of_day=time_band, modality=mode), problems


def _match_building(spoken: str, codes: set[str]) -> str | None:
    """The schedule's building code for what the caller said, if exactly one fits.

    Speech arrives as "A M P", "A.M.P." or "the AMP building"; codes can carry
    punctuation the caller won't say ("PE/W"). Compare letters and digits only.
    """
    wanted = re.sub(r"\b(?:the|building|bldg)\b", " ", spoken, flags=re.IGNORECASE).strip().upper()
    if wanted in codes:
        return wanted
    squashed = re.sub(r"[^A-Z0-9]", "", wanted)
    if not squashed:
        return None
    hits = [code for code in codes if re.sub(r"[^A-Z0-9]", "", code) == squashed]
    return hits[0] if len(hits) == 1 else None


async def _resolve_voice_building(
    building: str, campus_code: str, term_code: str,
) -> tuple[str | None, str | None]:
    """(building code, None), or (None, a reply naming the codes in use)."""
    codes = building_codes(await get_class_listing(campus_code, term_code))
    code = _match_building(building, codes)
    if code:
        return code, None
    campus_name = CAMPUS_NAMES.get(campus_code, campus_code)
    return None, (
        f"I couldn't find a building coded '{building}' in the {campus_name} "
        f"{get_term_name(term_code)} schedule. The schedule names buildings only by "
        f"the code in the room number. Codes in use this term: {', '.join(sorted(codes))}."
    )


@observe(name="voice.schedule_search")
async def schedule_search(
    subject: str = "",
    course_number: str = "",
    term: str | None = None,
    campus: str = DEFAULT_CAMPUS,
    days: str = "",
    building: str = "",
    time_of_day: str = "",
    modality: str = "",
) -> str:
    """Search the live LACCD class schedule for a single campus.

    The filters apply to one meeting together, as in web chat: "Saturday in
    AMP" is a Saturday meeting in AMP. A building or other filter can stand in
    for the subject ("what's in AMP on Saturday?").
    """
    term_code = resolve_term(term)
    campus_code = (campus or DEFAULT_CAMPUS).upper()

    filters, problems = _voice_filters(days, time_of_day, modality)
    if problems:
        return f"I couldn't read the {' or the '.join(problems)}. {_FILTER_HELP}"
    if not subject.strip() and not building.strip() and not filters:
        return (
            "I need a subject, or a day, time of day, building, or online or in person, "
            "to search the schedule."
        )
    if building.strip():
        code, problem = await _resolve_voice_building(building, campus_code, term_code)
        if problem:
            return problem
        filters = replace(filters, building=code)

    subject_code = subject_name = None
    if subject.strip():
        subject_code, subject_name = await find_subject_code(subject, campus_code, term_code)
        if not subject_code:
            return f"I couldn't find a subject matching '{subject}' in the {get_term_name(term_code)} schedule."

    result = await search_live_schedule(
        campus_code, term=term_code, subject=subject_code,
        course_number=course_number or None,
        filters=filters or None,
    )
    sections = result.get("sections", [])
    total = result.get("total_sections", len(sections))
    campus_name = result.get("campus_name", campus_code)
    term_name = result.get("term_name", get_term_name(term_code))
    described = filters.describe()

    if not sections:
        # With a filter, zero is the answer ("nothing on Sunday"), not a miss.
        what = " ".join(filter(None, [subject_code, course_number])) or "classes"
        return f"No sections found for {what}{described} at {campus_name} for {term_name}."

    header = (
        f"Found {total} section{'s' if total != 1 else ''} for "
        f"{subject_name or subject_code or 'classes'}{described} at {campus_name} for {term_name}:\n\n"
    )
    if result.get("truncated"):
        header += f"The first {len(sections)} are summarized here; the LACCD class search website has the rest.\n\n"
    return header + _fmt_sections_voice(sections, show_location=bool(filters))


@observe(name="voice.schedule_search_all")
async def schedule_search_all(
    subject: str,
    course_number: str = "",
    term: str | None = None,
    days: str = "",
    time_of_day: str = "",
    modality: str = "",
) -> str:
    """Search classes across all 9 LACCD campuses.

    No building filter: building codes are campus-local.
    """
    term_code = resolve_term(term)
    filters, problems = _voice_filters(days, time_of_day, modality)
    if problems:
        return f"I couldn't read the {' or the '.join(problems)}. {_FILTER_HELP}"
    described = filters.describe()

    # Resolve subject code against a few campuses
    subject_code = None
    for try_campus in ["LAMC", "LAVC", "LACC", "LAPC", "ELAC"]:
        code, _ = await find_subject_code(subject, try_campus, term_code)
        if code:
            subject_code = code
            break
    if not subject_code:
        return f"I couldn't find a subject matching '{subject}' in the schedule."

    results = await search_all_campuses(
        term=term_code, subject=subject_code,
        course_number=course_number or None,
        filters=filters or None,
    )
    total = results.get("total_sections", 0)
    term_name = results.get("term_name", get_term_name(term_code))

    if total == 0:
        what = " ".join(filter(None, [subject_code, course_number]))
        return f"No sections found for {what}{described} across the district for {term_name}."

    lines = [f"Found {total} section{'s' if total != 1 else ''}{described} across the district for {term_name}:\n"]
    for campus_code, campus_data in results.get("campuses", {}).items():
        cname = campus_data["campus_name"]
        n = campus_data["section_count"]
        lines.append(f"\n{cname} ({n} section{'s' if n != 1 else ''}):")
        for s in campus_data["sections"][:3]:
            parts = [s["course_code"]]
            days = s.get("days", "TBA")
            time_ = s.get("time", "TBA")
            if days not in ("TBA", "") and time_ not in ("TBA", ""):
                parts.append(f"{days} {time_}")
            else:
                parts.append("Online or TBA")
            inst = s.get("instructor", "TBA")
            if inst not in ("TBA", ""):
                parts.append(f"with {inst}")
            lines.append(f"  {', '.join(parts)}")
        remaining = n - min(n, 3)
        if remaining > 0:
            lines.append(f"  ...and {remaining} more.")

    return "\n".join(lines)


def _filter_disambiguation(query: str, matches: list[dict]) -> list[dict]:
    """If query contains 'Title (Award Type)', filter matches to the exact one."""
    m = re.search(r'^(.+?)\s*\(([^)]+)\)\s*$', query.strip())
    if not m or len(matches) <= 1:
        return matches
    target_title = m.group(1).strip().lower()
    target_award = m.group(2).strip().lower()
    exact = [
        p for p in matches
        if target_title in (p.get('title') or '').lower()
        and target_award in (p.get('awardShortTitle') or p.get('award_type') or '').lower()
    ]
    return exact[:1] if exact else matches


async def _search_programs_neo4j(query: str, campus_code: str | None = None) -> list[dict]:
    """Search programs in Neo4j (fallback when PM API is unavailable)."""
    if not neo4j_service.is_configured():
        return []
    campus_filter = "AND p.campus_code = $campus" if campus_code else ""
    rows = await neo4j_service.run_query(
        f"""
        CALL db.index.fulltext.queryNodes('ft_program_title', $query)
        YIELD node AS p, score
        WHERE p.pathway_elements IS NOT NULL {campus_filter}
        RETURN p.master_record_id AS masterRecordId, p.title AS title,
               p.award_type AS awardShortTitle, p.campus_code AS campus_code,
               score
        ORDER BY score DESC
        LIMIT 10
        """,
        {"query": query, "campus": campus_code},
    )
    return rows if rows else []


@observe(name="voice.program_search")
async def program_search(
    query: str,
    campus: str = DEFAULT_CAMPUS,
) -> str:
    """Search for academic programs at a specific campus."""
    campus_code = (campus or DEFAULT_CAMPUS).upper()
    if campus_code not in COLLEGE_CONFIGS:
        valid = ", ".join(sorted(COLLEGE_CONFIGS))
        return f"I don't recognize the campus code '{campus_code}'. Valid LACCD campuses are: {valid}."
    client = get_program_mapper_client(campus_code)

    matches = []
    try:
        matches = await client.search_programs(query)
    except Exception as e:
        logger.warning("PM API failed for voice program search, trying Neo4j: %s", e)
    # Neo4j fallback
    if not matches:
        matches = await _search_programs_neo4j(query, campus_code)
    if not matches:
        return (
            f"I couldn't find any programs matching '{query}' at {client.campus_name}. "
            "You could try asking about it differently, or I can search the campus knowledge base instead."
        )

    matches = select_explicit_program(query, matches)
    # Break disambiguation loops when query contains "Title (Award)"
    matches = _filter_disambiguation(query, matches)

    if len(matches) == 1:
        try:
            bundle = await client.get_program_bundle(matches[0]["masterRecordId"])
        except Exception as e:
            logger.warning("PM API bundle fetch failed: %s", e)
            return f"I found {matches[0].get('title', query)} but had trouble loading the full details. Please check the college website."
        return _fmt_program_voice(bundle)

    lines = [f"I found {len(matches)} programs matching '{query}' at {client.campus_name}:"]
    for m in matches[:6]:
        award = m.get("awardShortTitle", "")
        title = m.get("title", "")
        award_str = f" ({award})" if award else ""
        lines.append(f"  {title}{award_str}")
    if len(matches) > 6:
        lines.append(f"  ...and {len(matches) - 6} more.")
    lines.append("\nWhich one would you like to know more about?")
    return "\n".join(lines)


@observe(name="voice.program_search_all")
async def program_search_all(query: str) -> str:
    """Search programs across all LACCD campuses."""
    matches = []
    try:
        matches = await search_programs_all_campuses(query)
    except Exception as e:
        logger.warning("PM API failed for voice district-wide program search, trying Neo4j: %s", e)
    # Neo4j fallback (no campus filter = all campuses)
    if not matches:
        matches = await _search_programs_neo4j(query, None)
    if not matches:
        return f"I couldn't find any programs matching '{query}' across the district."

    matches = select_explicit_program(query, matches)
    # Break disambiguation loops when query contains "Title (Award)"
    matches = _filter_disambiguation(query, matches)

    if len(matches) == 1:
        m = matches[0]
        client = get_program_mapper_client(m["campus_code"])
        try:
            bundle = await client.get_program_bundle(m["masterRecordId"])
        except Exception as e:
            logger.warning("PM API bundle fetch failed: %s", e)
            return f"I found {m.get('title', query)} at {m.get('campus_name', '')} but had trouble loading the full details."
        return _fmt_program_voice(bundle)

    lines = [f"I found {len(matches)} programs matching '{query}' across the district:"]
    for m in matches[:8]:
        campus_name = m.get("campus_name", "")
        title = m.get("title", "")
        award = m.get("awardShortTitle", "")
        award_str = f" ({award})" if award else ""
        lines.append(f"  {title}{award_str} at {campus_name}")
    if len(matches) > 8:
        lines.append(f"  ...and {len(matches) - 8} more.")
    lines.append("\nWhich one would you like to know more about?")
    return "\n".join(lines)


def _fmt_sequence_voice(data: dict) -> str:
    """Format course sequence data as readable plain text for voice."""
    name = data.get("program_name", "this program")
    award = data.get("award_type", "")
    total_units = data.get("total_units", 0)
    terms = data.get("terms_to_complete", 0)

    lines = [f"Here is the course sequence for {name}."]
    if award:
        lines[0] = f"Here is the course sequence for the {award} in {name}."
    lines.append(f"It's designed for {terms} semesters, totaling {total_units} units.")

    for term in data.get("sequence", []):
        label = term.get("term_label", "")
        term_units = term.get("total_units", 0)
        courses = term.get("courses", [])
        lines.append(f"\n{label}, {term_units} units:")
        for c in courses[:6]:
            code = c.get("code", "")
            title = c.get("title", "")
            units = c.get("units", "")
            lines.append(f"  {code}, {title}, {units} units")
        if len(courses) > 6:
            lines.append(f"  ...plus {len(courses) - 6} more courses.")

    chains = data.get("prerequisite_chains", [])
    if chains:
        lines.append("\nImportant prerequisite chains:")
        for chain in chains[:3]:
            lines.append(f"  {' then '.join(chain)}")

    return "\n".join(lines)


@observe(name="voice.course_sequence")
async def course_sequence(
    query: str,
    campus: str = DEFAULT_CAMPUS,
    pace: str | None = None,
) -> str:
    """Generate a semester-by-semester course sequence for a program.

    Args:
        query: Program name (e.g., "Computer Programmer").
        campus: Campus code (LAMC by default).
        pace: Optional pace track. One of "full" (15u/term, default),
            "momentum" (18u/term, fast track), or "part_time" (9u/term).
    """
    campus_code = (campus or DEFAULT_CAMPUS).upper()

    # Search for the program (PM API with Neo4j fallback)
    matches = []
    if campus_code in COLLEGE_CONFIGS:
        client = get_program_mapper_client(campus_code)
        try:
            matches = await client.search_programs(query)
        except Exception as e:
            logger.warning("PM API failed for voice sequence search, trying Neo4j: %s", e)
    if not matches:
        matches = await _search_programs_neo4j(query, campus_code)
    if not matches:
        return (
            f"I couldn't find any programs matching '{query}' at "
            f"{COLLEGE_CONFIGS.get(campus_code, {}).get('name', campus_code)}. "
            "Could you try a different name?"
        )

    matches = select_explicit_program(query, matches)
    # Break disambiguation loops
    matches = _filter_disambiguation(query, matches)

    if len(matches) > 3:
        lines = [f"I found {len(matches)} programs matching '{query}'. Which one do you want the course sequence for?"]
        for m in matches[:6]:
            title = m.get("title", "")
            award = m.get("awardShortTitle", m.get("award_type", ""))
            award_str = f" ({award})" if award else ""
            lines.append(f"  {title}{award_str}")
        return "\n".join(lines)

    # Generate sequence for top match
    top = matches[0]
    target_campus = top.get("campus_code", campus_code)
    try:
        svc = get_course_sequence_service(target_campus)
        sequence = await svc.generate_sequence(top["masterRecordId"], target_campus, pace=pace)
    except Exception as e:
        logger.warning("Course sequence generation failed: %s", e)
        title = top.get("title", query)
        return f"I found {title} but had trouble generating the course sequence. Please check the college website or consult a counselor."

    return _fmt_sequence_voice(sequence)


def _is_laccd_url(value: str | None) -> bool:
    """Check if a URL belongs to any LACCD campus website."""
    if not value:
        return False
    from urllib.parse import urlparse
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    laccd_domains = {c.website.lower() for c in CAMPUSES.values()}
    # Also accept without www. prefix
    laccd_domains |= {d.removeprefix("www.") for d in laccd_domains}
    return any(host == d or host.endswith("." + d) for d in laccd_domains)


@observe(name="voice.crawl_website")
async def crawl_website(url: str) -> str:
    """Crawl an LACCD campus website page for current information."""
    from app.services.corpus import extract_text_from_html

    if not _is_laccd_url(url):
        return "I can only look up pages from LACCD campus websites."

    crawler = get_crawler()
    if not crawler.is_available():
        return "Live website lookup is not available right now. Let me check the campus knowledge base instead."

    try:
        result = await crawler.crawl_live_page(url)
        text = extract_text_from_html(result.html, fallback_url=result.url)
        if len(text.strip()) < 100:
            return "That page didn't have much useful content. Let me try the campus knowledge base instead."
        if len(text) > 2000:
            text = text[:2000] + "..."
        return f"Here's what I found on that page:\n\n{text}"
    except Exception as exc:
        logger.warning("Live crawl failed for %s: %s", url, exc)
        return "I wasn't able to load that page. Let me check the campus knowledge base instead."


# ── Course info / prerequisites (Neo4j KG) ───────────


def _parse_course_code(course_code: str) -> tuple[str, str]:
    """Parse 'BIOLOGY 006' or 'BIO 006' into (subject, number)."""
    # Handle multi-word subjects
    for subj in sorted(
        ["ADM JUS", "AFRO AM", "ART HISTORY", "CH DEV", "CO SCI", "ESL NC",
         "FAM &CS", "KIN ATH", "NRS-HCA", "PHYS ED", "PHYS SC", "POL SCI",
         "REAL ES", "A S L", "DEN HY", "ENV SCI", "HLTH OCC"],
        key=len, reverse=True,
    ):
        if course_code.upper().startswith(subj + " "):
            rest = course_code[len(subj):].strip()
            parts = rest.split(None, 1)
            if parts:
                return subj, parts[0].upper()
    parts = course_code.strip().rsplit(None, 1)
    if len(parts) == 2:
        return parts[0].upper(), parts[1].upper()
    return course_code.upper(), ""


@observe(name="voice.course_info")
async def course_info(
    course_code: str,
    campus_code: str = "LAMC",
) -> str:
    """Look up prerequisites, corequisites, advisories, and details for a course."""
    subject, number = _parse_course_code(course_code)
    if not number:
        return f"I couldn't parse the course code '{course_code}'. Please say it like 'MATH 101' or 'BIOLOGY 006'."

    campus_code = campus_code.upper()

    if not neo4j_service.is_configured():
        return "The course database is not currently available."
    if not await neo4j_service.verify_connectivity():
        return "I'm having trouble connecting to the course database right now."

    # Get course details
    details = await neo4j_service.get_course_details(subject, number, campus_code)
    if not details:
        return f"I couldn't find {subject} {number} in the {campus_code} catalog. Please check the course code."

    title = details.get("title") or ""
    units = details.get("units") or ""
    transfer = details.get("catalog_transfer") or details.get("transfer") or ""

    lines = [f"{subject} {number}, {title}."]
    if units:
        lines[0] += f" {units} units."
    if transfer:
        lines[0] += f" Transferable to {transfer.replace(':', ' and ')}."

    # Prerequisites — graph first, then catalog text
    prereqs = await neo4j_service.get_prerequisites(subject, number, campus_code)
    if prereqs:
        prereq_names = []
        for p in prereqs:
            name = f"{p['prereq_code']}"
            if p.get("prereq_title"):
                name += f", {p['prereq_title']}"
            prereq_names.append(name)
        lines.append("Prerequisites: " + "; or ".join(prereq_names) + ".")
    else:
        catalog_prereq = details.get("catalog_prereq_text")
        if catalog_prereq:
            lines.append(f"Prerequisites: {catalog_prereq}.")
        elif catalog_prereq == "":
            lines.append("Prerequisites: None.")

    # Corequisites — graph first, then catalog text
    coreqs = await neo4j_service.get_course_corequisites(subject, number, campus_code)
    if coreqs:
        coreq_names = [f"{c['coreq_code']}" for c in coreqs]
        lines.append("Corequisites: " + ", ".join(coreq_names) + ".")
    else:
        catalog_coreq = details.get("catalog_coreq_text")
        if catalog_coreq:
            lines.append(f"Corequisites: {catalog_coreq}.")

    # Advisories — graph first, then catalog text
    advisories = await neo4j_service.get_course_advisories(subject, number, campus_code)
    if advisories:
        adv_names = [f"{a['advisory_code']}" for a in advisories]
        lines.append("Advisories: " + ", ".join(adv_names) + ".")
    else:
        catalog_adv = details.get("catalog_advisory_text")
        if catalog_adv:
            lines.append(f"Advisory: {catalog_adv}.")

    # Description (brief for voice)
    desc = details.get("catalog_description") or details.get("description") or ""
    if desc:
        # Truncate for voice
        if len(desc) > 250:
            desc = desc[:250].rsplit(" ", 1)[0] + "..."
        lines.append(desc)

    return "\n".join(lines)


# ── Transfer, GE, and renumbering tools ──────────────


@observe(name="voice.transfer_and_ge")
async def transfer_and_ge(
    course_code: str,
    campus_code: str = "LAMC",
) -> str:
    """Look up transfer status, GE areas, and renumbering history for a course."""
    subject, number = _parse_course_code(course_code)
    if not number:
        return f"I couldn't parse the course code '{course_code}'. Please say it like 'MATH 261' or 'ENGL C1000'."

    campus_code = campus_code.upper()

    if not neo4j_service.is_configured():
        return "The course database is not currently available."
    if not await neo4j_service.verify_connectivity():
        return "I'm having trouble connecting to the course database right now."

    # Resolve subject in case of abbreviation mismatch
    resolved = await neo4j_service.resolve_course_subject(subject, number, campus_code)
    if resolved:
        subject = resolved

    # Fetch transfer, GE, and cross-refs in parallel
    transfer_task = neo4j_service.get_course_transfer_status(subject, number, campus_code)
    ge_task = neo4j_service.get_course_ge_areas(subject, number, campus_code)
    xref_task = neo4j_service.find_course_cross_refs(subject, number, campus_code)
    transfer, ge_areas, cross_refs = await asyncio.gather(transfer_task, ge_task, xref_task)

    if not transfer and not ge_areas and not cross_refs:
        return f"I couldn't find {subject} {number} in the {campus_code} course database. Please check the course code."

    lines: list[str] = []

    # Transfer status
    if transfer:
        title = transfer.get("title") or ""
        header = f"{subject} {number}"
        if title:
            header += f", {title}"
        uc = transfer.get("uc_transferable")
        csu = transfer.get("csu_transferable")
        transfers_to = []
        if uc:
            transfers_to.append("UC")
        if csu:
            transfers_to.append("CSU")
        if transfers_to:
            lines.append(f"{header} is transferable to {' and '.join(transfers_to)}.")
        else:
            lines.append(f"{header} is not listed as UC or CSU transferable.")

    # GE areas
    if ge_areas:
        by_system: dict[str, list[str]] = {}
        for ge in ge_areas:
            sys_name = ge.get("ge_system", "")
            label = ge.get("ge_code", "")
            ge_name = ge.get("ge_name", "")
            if ge_name:
                label += f", {ge_name}"
            by_system.setdefault(sys_name, []).append(label)
        for sys_name, areas in by_system.items():
            lines.append(f"For {sys_name}, it fulfills: {', '.join(areas)}.")

    # Renumbering cross-references
    if cross_refs:
        for xref in cross_refs:
            new_code = xref.get("new_code", "")
            lines.append(f"This course was renumbered to {new_code}.")
    else:
        # Check reverse direction: maybe THIS course is the new one
        # (find_course_cross_refs only looks forward; check if old courses point here)
        pass

    return "\n".join(lines) if lines else f"I found {subject} {number} but no transfer, GE, or renumbering data is available."


@observe(name="voice.ge_area_courses")
async def ge_area_courses(
    ge_area: str,
    system: str = "IGETC",
    campus_code: str = "LAMC",
) -> str:
    """Look up courses that satisfy a GE area."""
    campus_code = campus_code.upper()

    if not neo4j_service.is_configured():
        return "The course database is not currently available."
    if not await neo4j_service.verify_connectivity():
        return "I'm having trouble connecting to the course database right now."

    # Normalize: strip "Area " prefix, uppercase
    ge_code = ge_area.strip().upper()
    for prefix in ["AREA ", "AREA"]:
        if ge_code.startswith(prefix):
            ge_code = ge_code[len(prefix):].strip()

    system = system.strip().upper()
    # Normalize common system names
    system_map = {
        "IGETC": "IGETC",
        "CSU-GE": "CSU-GE",
        "CSU GE": "CSU-GE",
        "CSUGE": "CSU-GE",
        "CAL-GETC": "Cal-GETC",
        "CALGETC": "Cal-GETC",
        "CAL GETC": "Cal-GETC",
    }
    system = system_map.get(system, system)

    courses = await neo4j_service.get_ge_courses(ge_code, system, campus_code)

    if not courses:
        return f"I didn't find any courses for {system} Area {ge_code} at {campus_code}. Please check the area code and system."

    count = len(courses)
    lines = [f"Found {count} course{'s' if count != 1 else ''} for {system} Area {ge_code} at {campus_code}:"]

    # Show first 10 for voice
    for c in courses[:10]:
        code = c.get("course_code", "")
        title = c.get("title", "")
        units = c.get("units_min", "")
        entry = code
        if title:
            entry += f", {title}"
        if units:
            entry += f", {units} units"
        lines.append(f"  {entry}")

    if count > 10:
        lines.append(f"Plus {count - 10} more courses. Would you like me to continue listing them?")

    return "\n".join(lines)


@observe(name="voice.instructor_search")
async def instructor_search(
    course_code: str = "",
    instructor_name: str = "",
    campus_code: str = "LAMC",
) -> str:
    """Bidirectional instructor/course lookup."""
    campus_code = campus_code.upper()

    if not neo4j_service.is_configured():
        return "The course database is not currently available."
    if not await neo4j_service.verify_connectivity():
        return "I'm having trouble connecting to the course database right now."

    # Mode 1: course → instructors
    if course_code:
        subject, number = _parse_course_code(course_code)
        if not number:
            return f"I couldn't parse the course code '{course_code}'. Please say it like 'MATH 261'."
        results = await neo4j_service.find_course_instructors(subject, number, campus_code)
        if not results:
            return f"I don't have instructor information for {subject} {number} at {campus_code}."
        names = list({r["name"] for r in results})
        title = results[0].get("course_title", "")
        course_label = f"{subject} {number}"
        if title:
            course_label += f", {title}"
        if len(names) == 1:
            return f"{course_label} is taught by {names[0]} at {campus_code}."
        return f"{course_label} at {campus_code} is taught by: {', '.join(names)}."

    # Mode 2: instructor → courses
    if instructor_name:
        results = await neo4j_service.search_instructors(instructor_name, campus_code)
        # Retry without campus filter if no results (matching text chatbot pattern)
        if not results:
            results = await neo4j_service.search_instructors(instructor_name, None)
        if not results:
            return (
                f"I couldn't find an instructor matching '{instructor_name}'. "
                f"I may have misheard the name. Could you spell the last name "
                f"for me, or try saying it again more slowly?"
            )
        # Take top match
        top = results[0]
        name = top.get("name", instructor_name)
        courses = top.get("courses", [])
        titles = top.get("course_titles", [])
        inst_campus = top.get("campus_code", campus_code)
        if not courses:
            return f"I found {name} at {inst_campus} but don't have their course assignments listed."
        course_list = []
        for i, code in enumerate(courses[:8]):
            title = titles[i] if i < len(titles) else ""
            entry = code
            if title:
                entry += f", {title}"
            course_list.append(entry)
        result_text = f"{name} at {inst_campus} teaches: {'; '.join(course_list)}."
        if len(courses) > 8:
            result_text += f" Plus {len(courses) - 8} more courses."
        return result_text

    return "Please provide either a course code or an instructor name to search."


# ── ZTC (Zero Textbook Cost) tools ───────────────────


def _fmt_ztc_voice(sections: list[dict], limit: int = 8) -> str:
    """Format ZTC sections as plain text for voice (no markdown)."""
    if not sections:
        return ""
    lines = []
    for s in sections[:limit]:
        parts = [s["course_code"]]
        if s.get("course_title"):
            parts.append(s["course_title"])
        badge = "ZTC" if "OER" in s.get("class_type", []) else "Low Cost"
        parts.append(badge)
        days = s.get("days", "TBA")
        time_ = s.get("time", "TBA")
        if days not in ("TBA", "") and time_ not in ("TBA", ""):
            parts.append(f"{days} {time_}")
        else:
            parts.append("Online or TBA")
        instructor = s.get("instructor", "TBA")
        if instructor not in ("TBA", ""):
            parts.append(f"with {instructor}")
        lines.append(", ".join(parts))
    remaining = len(sections) - min(len(sections), limit)
    if remaining > 0:
        lines.append(f"\nPlus {remaining} more sections. Students can view the full list on the LACCD class search website.")
    return "\n".join(lines)


def _fmt_openstax_voice(subject_codes: list[str]) -> str:
    """Format OpenStax suggestions as plain text for voice."""
    openstax = get_openstax_suggestions_multi(subject_codes)
    if not openstax:
        return ""
    books: list[str] = []
    for book_list in openstax.values():
        for b in book_list:
            if len(books) >= 3:
                break
            books.append(b["title"])
        if len(books) >= 3:
            break
    if not books:
        return ""
    return "\n\nFree OpenStax textbooks available: " + ", ".join(books) + ". Students can find these at openstax.org."


@observe(name="voice.ztc_search")
async def ztc_search(
    subject: str = "",
    term: str | None = None,
    campus: str = DEFAULT_CAMPUS,
) -> str:
    """Search for Zero Textbook Cost classes at a single campus."""
    term_code = resolve_term(term)
    campus_code = (campus or DEFAULT_CAMPUS).upper()

    subject_code = None
    subject_name = None
    if subject:
        subject_code, subject_name = await find_subject_code(subject, campus_code, term_code)
        if not subject_code:
            return f"I couldn't find a subject matching '{subject}' in the {get_term_name(term_code)} schedule."

    result = await search_ztc_sections(campus_code, term_code, subject=subject_code)
    sections = result.get("sections", [])
    campus_name = result.get("campus_name", campus_code)
    term_name = result.get("term_name", get_term_name(term_code))

    if not sections:
        qualifier = f" {subject_name}" if subject_name else ""
        return f"I didn't find any Zero Textbook Cost{qualifier} sections at {campus_name} for {term_name}."

    ztc_count = sum(1 for s in sections if "OER" in s.get("class_type", []))
    lct_count = sum(1 for s in sections if "LCT" in s.get("class_type", []))
    parts = []
    if ztc_count:
        parts.append(f"{ztc_count} ZTC")
    if lct_count:
        parts.append(f"{lct_count} Low Cost")
    count_str = " and ".join(parts)
    qualifier = f" {subject_name}" if subject_name else ""

    header = f"Found {count_str}{qualifier} sections at {campus_name} for {term_name}:\n\n"
    body = _fmt_ztc_voice(sections)

    # Collect subject codes for OpenStax suggestions
    subj_codes = list({s["course_code"].split()[0] for s in sections if " " in s["course_code"]})
    openstax_text = _fmt_openstax_voice(subj_codes)

    return header + body + openstax_text


@observe(name="voice.ztc_search_all")
async def ztc_search_all(
    subject: str = "",
    term: str | None = None,
) -> str:
    """Search for Zero Textbook Cost classes across all LACCD campuses."""
    import asyncio as _asyncio

    term_code = resolve_term(term)

    subject_code = None
    subject_name = None
    if subject:
        for try_campus in ["LAMC", "LAVC", "LACC", "LAPC", "ELAC"]:
            code, name = await find_subject_code(subject, try_campus, term_code)
            if code:
                subject_code = code
                subject_name = name
                break
        if not subject_code:
            return f"I couldn't find a subject matching '{subject}' in the schedule."

    async def fetch_ztc(campus: str) -> tuple[str, dict]:
        return campus, await search_ztc_sections(campus, term_code, subject=subject_code)

    results = await _asyncio.gather(*[fetch_ztc(c) for c in ALL_CAMPUSES], return_exceptions=True)

    term_name = get_term_name(term_code)
    lines: list[str] = []
    total = 0
    all_subjects: set[str] = set()

    for r in results:
        if isinstance(r, tuple):
            c_code, payload = r
            secs = payload.get("sections", [])
            if secs:
                c_name = CAMPUS_NAMES.get(c_code, c_code)
                n = len(secs)
                total += n
                lines.append(f"\n{c_name} ({n} section{'s' if n != 1 else ''}):")
                for s in secs[:3]:
                    parts = [s["course_code"]]
                    badge = "ZTC" if "OER" in s.get("class_type", []) else "Low Cost"
                    parts.append(badge)
                    days = s.get("days", "TBA")
                    time_ = s.get("time", "TBA")
                    if days not in ("TBA", "") and time_ not in ("TBA", ""):
                        parts.append(f"{days} {time_}")
                    else:
                        parts.append("Online or TBA")
                    lines.append(f"  {', '.join(parts)}")
                remaining = n - min(n, 3)
                if remaining > 0:
                    lines.append(f"  ...and {remaining} more.")
                for s in secs:
                    subj = s["course_code"].split()[0] if " " in s["course_code"] else None
                    if subj:
                        all_subjects.add(subj)

    if total == 0:
        qualifier = f" {subject_name}" if subject_name else ""
        return f"I didn't find any Zero Textbook Cost{qualifier} sections across the district for {term_name}."

    qualifier = f" {subject_name}" if subject_name else ""
    header = f"Found {total} Zero Textbook Cost / Low Cost{qualifier} section{'s' if total != 1 else ''} across the district for {term_name}:"

    openstax_text = _fmt_openstax_voice(list(all_subjects))

    return header + "\n".join(lines) + openstax_text
