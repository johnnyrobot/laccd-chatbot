from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import httpx
from langfuse import get_client, observe

from app.config import settings
from app.models import ChatResponse, Citation
from app.services.crawl4ai_service import Crawl4AIService
from app.services.gemini_file_search import GeminiFileSearchService
from app.services.assist_articulation import AssistAPIError, AssistService, get_assist_service
from app.services.clarification import Clarification
from app.services.live_schedule import (
    ALL_CAMPUSES,
    CAMPUS_NAMES,
    ScheduleFilters,
    building_codes,
    find_subject_code,
    format_sections,
    get_class_listing,
    get_subjects,
    get_term_name,
    MAX_GE_SUBJECTS,
    search_all_campuses,
    search_live_schedule,
    search_ztc_sections,
)
from app.services.neo4j_service import neo4j_service
from app.services.openstax import get_openstax_suggestions, get_openstax_suggestions_multi
from app.services.privacy import redact_pii
from app.services.program_mapper import get_program_mapper_client, search_programs_all_campuses
from app.services.course_sequence_service import get_course_sequence_service, format_sequence_for_context
from app.services.enrollment_planner import (
    build_candidate_enrollment_plan,
    format_candidate_enrollment_plan,
)
from app.services.schedule_availability import ScheduleAvailabilityService
from shared import addenda, terms
from shared.campus_config import get_campus
from shared.program_matching import select_explicit_program
from shared.prompts import get_system_prompt

logger = logging.getLogger(__name__)


PROGRAM_TERMS = {"program", "degree", "certificate", "certifications", "major", "pathway", "requirements", "career", "study"}
# Schedule routing splits by how much a term means on its own.
#
# STRONG terms name the schedule directly and route by themselves. WEAK terms are
# ordinary English that merely *co-occur* with schedule questions, so they only
# count when something else corroborates them (a strong term, a course code, or a
# term reference like "Fall 2026").
#
# "take" used to route on its own, which sent "Do I still need to take the general
# education course it covers?" -- an advising question -- to the class schedule at
# every campus. Matching is word-boundary now too: as bare substrings
# "take" matched "mistake" and "taken", "time" matched "sometimes" and "part-time",
# "class" matched "classic", and "section" matched "intersection".
SCHEDULE_TERMS_STRONG = {
    "schedule", "class", "classes", "section", "sections", "instructor",
    "instructors", "offered", "available",
}
SCHEDULE_TERMS_WEAK = {"time", "take"}
# Union kept for callers that just want the vocabulary.
SCHEDULE_TERMS = SCHEDULE_TERMS_STRONG | SCHEDULE_TERMS_WEAK


def _word_in(term: str, text: str) -> bool:
    """True if term appears in text on word boundaries.

    ``"take" in "mistake"`` is True and that is how #47 happened.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None
ALL_CAMPUS_TERMS = {"district", "all campuses", "other campuses", "laccc", "laccd"}
ZTC_TERMS = {"ztc", "zero textbook", "textbook cost", "oer", "free textbook",
             "no textbook", "open educational", "low cost textbook", "low textbook",
             "afford textbook", "save on textbook", "free book"}

# Knowledge graph routing — transfer, GE area, and renumbering queries
KG_TRANSFER_TERMS = {"transferable", "uc transferable", "csu transferable", "transfer to uc", "transfer to csu", "transfer to a uc", "transfer to a csu"}
KG_GE_TERMS = {"igetc", "csu-ge", "csuge", "cal-getc", "calgetc", "ge area", "general education"}
KG_RENUMBERING_TERMS = {
    "renumber", "renamed", "same as", "changed to", "what happened",
    "equivalent to",
}
KG_PREREQ_TERMS = {"prerequisite", "prereq", "pre-req", "need to take before", "required before"}
KG_ADVISORY_TERMS = {"advisory", "advised", "recommended before", "advisories"}
KG_COREQ_TERMS = {"corequisite", "coreq", "co-requisite", "co-req", "taken together", "taken concurrently"}
KG_COURSE_INFO_TERMS = {"tell me about", "course description", "what is", "describe", "course info", "course details"}
KG_ALL_TERMS = KG_TRANSFER_TERMS | KG_GE_TERMS | KG_RENUMBERING_TERMS | KG_PREREQ_TERMS | KG_ADVISORY_TERMS | KG_COREQ_TERMS | KG_COURSE_INFO_TERMS

# Instructor search routing
INSTRUCTOR_TERMS = {"who teaches", "taught by", "professor", "instructor for", "teacher", "faculty",
                    "who is the instructor", "the instructor for"}

# Course sequence routing
SEQUENCE_TERMS = {"course sequence", "course plan", "course order", "semester plan",
                  "what order", "take first", "prerequisite chain", "course roadmap",
                  "course map", "course pathway", "amp template", "finish",
                  "complete", "map out", "in what order", "what classes should i take",
                  "what courses should i take"}

# One live-schedule API call per subject, so the cross-reference bounds how many
# it will make. The bound is fine; hiding it was not -- see
# `_handle_course_schedule_crossref`, which reports whatever it could not check.
MAX_CROSSREF_SUBJECTS = 5

ENROLLMENT_PLAN_TERMS = {
    "enrollment plan", "class plan", "schedule for me", "build a schedule",
    "plan my classes", "plan my courses",
}
_UNIT_TARGET_RE = re.compile(
    r"\b(\d{1,2}(?:\.\d)?)[\s-]*(?:semester[\s-]+)?units?\b",
    re.IGNORECASE,
)

# ASSIST.org articulation routing — explicit triggers (v1 keeps trigger surface
# narrow; widen via logged misses). Receiving-institution names below act as
# secondary triggers when paired with a transfer verb.
ASSIST_EXPLICIT_TRIGGERS = (
    "articulation", "articulat", "assist.org",
    "transfer to ", "transfers to ", "transfer agreement",
    "transferable to", "satisfies at",
)
ASSIST_TARGET_INSTITUTIONS = (
    # UCs
    "ucla", "uc berkeley", "ucb", "berkeley", "ucsd", "uc san diego",
    "uci", "uc irvine", "ucd", "uc davis", "ucsb", "uc santa barbara",
    "ucsc", "uc santa cruz", "ucr", "uc riverside", "ucm", "uc merced",
    # CSUs (LA-area + most-asked-about)
    "csun", "cal state northridge", "csula", "cal state la",
    "csulb", "cal state long beach", "long beach state",
    "sdsu", "san diego state", "sfsu", "san francisco state",
    "sjsu", "san jose state", "cpp", "cal poly pomona",
    "cal poly slo", "cal poly san luis obispo", "cal poly",
    "csu fullerton", "csuf", "fullerton",
    "csu dominguez hills", "csudh",
)
ASSIST_TRANSFER_VERBS = ("transfer", "satisfies", "satisfy", "count for", "counts for")

# "transferable to UC and CSU", "transfers to a CSU" — the SYSTEMS, no campus
# named. Word-bounded so it can't fire on 'uc' inside another word. Paired with
# a course code and the absence of any ASSIST_TARGET_INSTITUTIONS name, this is
# a knowledge-graph transferability lookup, not an ASSIST articulation.
_SYSTEM_LEVEL_TRANSFER_RE = re.compile(
    r'(?:'
    r'\b(?:transfer|transfers|transferable|transferrable)\b[^.?!]{0,30}?'
    r'\b(?:uc|ucs|csu|csus|university of california|cal state|california state)\b'
    r'|'
    r'\b(?:uc|ucs|csu|csus|university of california|cal state|california state)\b'
    r'[^.?!]{0,30}?\b(?:transfer|transfers|transferable|transferrable)\b'
    r')',
    re.IGNORECASE,
)

# Pace keyword detection — used to map free-text intent ("part time",
# "fast track") onto the pace tracks supported by CourseSequenceService.
# First matching key wins; default falls through to "full".
PACE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("part_time", ["part time", "part-time", "9 units", "9-unit", "working full time",
                   "while working", "evening only", "evenings only"]),
    ("momentum", ["fast track", "fast-track", "accelerated", "ahead of schedule",
                  "as fast as possible", "as quickly as possible",
                  "18 units", "18-unit", "momentum"]),
]


def detect_pace_from_message(text: str) -> str | None:
    """Return one of {"part_time", "momentum"} if the message asks for that
    pace, else None (caller should fall back to default 'full')."""
    if not text:
        return None
    lowered = text.lower()
    for pace, kws in PACE_KEYWORDS:
        if any(kw in lowered for kw in kws):
            return pace
    return None
_INSTRUCTOR_QUERY_RE = re.compile(
    r'(?:what|which)\s+(?:does|do|is|classes|courses)\s+.*\s+teach'
    r'|(?:does|do)\s+.*\s+teach\b',
    re.IGNORECASE,
)

# Pattern to detect GE area references like "IGETC 2A", "CSU-GE A2", "Cal-GETC 1A"
_GE_AREA_RE = re.compile(
    r'\b(?:igetc|csu[\s-]?ge|cal[\s-]?getc)\s+(?:area\s+)?([A-Z0-9]{1,3})\b',
    re.IGNORECASE,
)
# Pattern to extract course codes like "STAT C1000", "MATH 265", "ADM JUS 111"
# Case-sensitive: handles multi-word subjects (ADM JUS) without grabbing lowercase words
# Allows optional hyphen after C prefix (students type "C-1000" for "C1000")
_COURSE_CODE_RE = re.compile(
    r'\b([A-Z]{2,}(?:\s+[A-Z]{1,3})?)\s+(C?-?\d{2,4}[A-Z]{0,2})\b',
)
# Fallback for lowercase input — single-word subjects only, searched on message.upper()
_COURSE_CODE_RE_UPPER = re.compile(
    r'\b([A-Z]{2,})\s+(C?\d{2,4}[A-Z]{0,2})\b',
)


def _conversation_history(history: list[dict] | None) -> list[dict]:
    """History from the student's first message on.

    Whatever an assistant "said" before that is UI chrome — the web client sends
    its welcome banner — not conversation. Keeping it made every first turn look
    like a follow-up, so the rewriter paraphrased it: "classes in AMP" became
    "classes in the AMP program" and routed to Program Mapper.
    """
    for index, item in enumerate(history or []):
        if item.get("role") == "user":
            return history[index:]
    return []


def _find_course_code(message: str):
    """Find a course code in the message, trying case-sensitive first then uppercase fallback."""
    return _COURSE_CODE_RE.search(message) or _COURSE_CODE_RE_UPPER.search(message.upper())


def _extract_all_course_codes(text: str) -> list[tuple[str, str]]:
    """Extract ALL course codes from text as deduplicated (subject, number) tuples."""
    results = _COURSE_CODE_RE.findall(text)
    if not results:
        results = _COURSE_CODE_RE_UPPER.findall(text.upper())
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for subject, number in results:
        key = (subject.strip(), number.replace("-", ""))
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


_BOLD_SPAN_RE = re.compile(r"\*\*(.+?)\*\*")


def _extract_course_codes_from_answer(text: str) -> list[tuple[str, str]]:
    """Course codes from one of *our own* answers, ignoring room numbers.

    A room ("INST 2016", "CMS 029") has the same shape as a course code, so
    shape alone cannot separate them — cross-referencing a schedule answer used
    to report rooms back to the student as courses that were "not offered this
    term". Our schedule formatter bolds the course code and leaves the room
    bare, so when an answer contains bolded spans, trust only those. Falls back
    to the whole text for answers that do not bold (plain prose).
    """
    bold = " ".join(_BOLD_SPAN_RE.findall(text))
    if bold:
        codes = _extract_all_course_codes(bold)
        if codes:
            return codes
    return _extract_all_course_codes(text)

LANG_NAMES = {
    "es": "Spanish", "hy": "Armenian", "zh": "Chinese", "ko": "Korean",
    "tl": "Tagalog", "vi": "Vietnamese", "ru": "Russian", "ar": "Arabic",
    "ja": "Japanese", "fr": "French",
}

_NON_LATIN_RE = re.compile(r'[^\u0000-\u024F]')
# Accented characters and common non-English Latin patterns
_ACCENTED_LATIN_RE = re.compile(r'[\u00C0-\u024F]')

# Known course renumberings: (old_subject, old_number) → (new_subject, new_number, note)
COURSE_CROSS_REFS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("MATH", "227"): ("STAT", "C1000", "MATH 227 was renumbered to STAT C1000 (Intro to Statistics)."),
}

# Campus-service questions that should always route to RAG even if they contain program/schedule keywords
RAG_OVERRIDE_PHRASES = [
    "admission", "financial aid", "career center", "career services",
    "student services", "student id", "parking", "library", "tutoring",
    "counseling", "bookstore", "email", "password", "gpa",
    "transfer to a", "transfer to uc", "transfer to csu",
    "how do i apply", "how to apply", "how do i register",
    "high school student", "eops", "calworks", "dsps",
    "online classes", "offer online",
    "what services", "campus map", "location",
    "address", "phone number", "contact", "hours",
    "transcript", "ordering transcript",
]

SEQUENCE_CONTEXT_PROMPT = """

The following is a generated course sequence plan. Present it to the student in a clear,
semester-by-semester format. Use bold headers for each semester. Include course codes, titles,
units, and requirement types. Mention prerequisite chains if relevant. Add a brief note if
prerequisite data was unavailable.

{sequence_data}

Format each semester as:
**Semester N (Fall/Spring) — X units**
- COURSE CODE: Title (N units) — Requirement Type

End with any prerequisite chains and notes.
"""


@dataclass(slots=True)
class RewriteResult:
    english_query: str   # English version for routing
    detected_lang: str   # ISO 639-1 code ("en", "es", "zh", "hy", etc.)


@dataclass(slots=True)
class Tooling:
    gemini_file_search: GeminiFileSearchService
    crawler: Crawl4AIService
    availability: ScheduleAvailabilityService
    assist: AssistService


class ChatOrchestrator:
    def __init__(self) -> None:
        self.tools = Tooling(
            gemini_file_search=GeminiFileSearchService(),
            crawler=Crawl4AIService(),
            availability=ScheduleAvailabilityService(),
            assist=get_assist_service(),
        )

    def _needs_language_detection(self, message: str) -> bool:
        # Non-Latin scripts (Chinese, Armenian, Arabic, etc.)
        if _NON_LATIN_RE.search(message):
            return True
        # Accented Latin characters suggest non-English (Spanish, French, etc.)
        if _ACCENTED_LATIN_RE.search(message):
            return True
        return False

    _ANAPHORIC_RE = re.compile(
        r'\b(these|those|them|they|it|which ones|the same|that class|that course|'
        r'above|listed|mentioned|previous|from that list|out of)\b',
        re.IGNORECASE,
    )

    def _rewrite_with_context(self, message: str, history: list[dict] | None) -> RewriteResult:
        """Rewrite an ambiguous follow-up, detect language, and translate to English for routing."""
        history = _conversation_history(history)
        has_history = bool(history) and (
            len(message.split()) <= 15 or self._ANAPHORIC_RE.search(message)
        )

        if has_history:
            # A clarify-back round trip is 2 messages, so a 4-message window is
            # exactly consumed by two of them — and the original question (the
            # only place the course code appears) falls out. When that happened
            # the rewriter echoed the bot's own clarifying question back as the
            # "standalone query" and it was answered by RAG.
            recent = history[-8:]
            def _trim(m):
                limit = 800 if m['role'] == 'assistant' else 300
                return m['content'][:limit]
            context = "\n".join(f"{m['role']}: {_trim(m)}" for m in recent)
            prompt = (
                "Given this conversation history:\n"
                f"{context}\n\n"
                f"The user's latest message is: \"{message}\"\n\n"
                "Return a JSON object with exactly these fields:\n"
                '- "english": the message rewritten as a clear standalone question in English. '
                'If the user refers to courses from the conversation, include ALL specific '
                'course codes (e.g., CIS 101, MATH 261, STAT C1000) from the conversation context. '
                'Also include any GE system/area references (e.g., IGETC Area 2A, CSU-GE Area B4). '
                'If the user references a program or degree, include the full name '
                '(e.g., "Computer Science AS-T") in the rewritten question. '
                'Keep acronyms, abbreviations, and building or room names exactly as the '
                'user wrote them — never expand an acronym (keep "AMP"; do not write '
                '"Arts, Media, and Performance"). Do not add words such as "program", '
                '"degree", "major", or "pathway" that the user did not say.\n'
                '- "lang": ISO 639-1 language code of the user\'s message (e.g. "en", "es", "hy", "zh")\n'
                "If the message is already clear and in English, return it unchanged as the english field.\n"
                "Return ONLY valid JSON, no markdown fences."
            )
            try:
                raw = self.tools.gemini_file_search.generate(prompt)
                if raw:
                    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    parsed = json.loads(cleaned)
                    english = parsed.get("english", message).strip()
                    lang = parsed.get("lang", "en").strip().lower()
                    english = self._drop_invented_program_terms(english, message, history)
                    logger.info("Query rewritten (lang=%s): %r -> %r", lang, redact_pii(message), redact_pii(english))
                    return RewriteResult(english_query=english, detected_lang=lang)
            except Exception:
                logger.warning("Query rewrite failed, using original", exc_info=True)
            return RewriteResult(english_query=message, detected_lang="en")

        if self._needs_language_detection(message):
            prompt = (
                f"The following text is a user question:\n\"{message}\"\n\n"
                "Return a JSON object with exactly these fields:\n"
                '- "english": the question translated to English\n'
                '- "lang": ISO 639-1 language code of the original text (e.g. "es", "hy", "zh")\n'
                "Return ONLY valid JSON, no markdown fences."
            )
            try:
                raw = self.tools.gemini_file_search.generate(prompt)
                if raw:
                    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    parsed = json.loads(cleaned)
                    english = parsed.get("english", message).strip()
                    lang = parsed.get("lang", "en").strip().lower()
                    logger.info("Language detected: %s, translated: %r -> %r", lang, redact_pii(message), redact_pii(english))
                    return RewriteResult(english_query=english, detected_lang=lang)
            except Exception:
                logger.warning("Language detection failed, assuming English", exc_info=True)

        return RewriteResult(english_query=message, detected_lang="en")

    @staticmethod
    def _drop_invented_program_terms(english: str, message: str, history: list[dict]) -> str:
        """Remove program-route keywords the rewriter made up.

        The program branch sits above the schedule branch in `answer()`, so one
        invented word reroutes the turn: prod rewrote "are there classes in AMP
        on Saturday's" as "...in the AMP program..." and answered from RAG. A term
        survives only if the student or the conversation actually used it.
        """
        grounded = " ".join([message, *(item.get("content", "") for item in history)]).lower()
        for term in PROGRAM_TERMS:
            if term not in grounded:
                english = re.sub(rf"\s*\b{re.escape(term)}s?\b", "", english, flags=re.IGNORECASE)
        return english

    def resolve_query(self, message: str, history: list[dict] | None = None) -> RewriteResult:
        """Resolve a turn to a standalone English query + language code.

        Public because the guardrails layer must run this BEFORE its dialog rails:
        the rails classify intent from the latest message alone, so a terse reply
        to one of our own clarify-backs ("UCLA", "MATH 31A") carries no scope
        signal and gets misclassified. The result is handed back to `answer()`
        via `resolved=` so the rewrite is paid for exactly once.
        """
        return self._rewrite_with_context(message, history)

    # ------------------------------------------------------------------
    # Clarification state machine
    # ------------------------------------------------------------------

    def _fill_clarification(
        self, pending: Clarification, message: str, majors: list[dict] | None = None,
    ) -> Clarification | None:
        """Fill `pending.needs` from the RAW reply. No rewriter, no history, no LLM.

        Returns the completed Clarification, or None when the reply doesn't answer
        the question we asked — in which case we re-ask rather than guess. Guessing
        is what produced an articulation answer for a Computer Science major the
        student never named.
        """
        text = message.strip()
        if not text:
            return None

        if pending.needs == "receiving_institution":
            # Store the canonical alias, which re-resolves on later turns.
            # "both", "any", "all of them" are not campuses and never will be.
            term = self._matched_target_term(text)
            return pending.with_slot("campus", term) if term else None

        if pending.needs == "major":
            if majors is None:
                return None
            match = self._fuzzy_match_major(text, majors)
            if not match or not match.get("label"):
                return None  # e.g. the student answered with a course code
            return pending.with_slot("major", match["label"])

        return None

    # A reply this short is an attempted ANSWER, not a new question. Answers to
    # "which major?" ("Computer Science major") collide head-on with the routing
    # keyword sets — PROGRAM_TERMS contains "major" — so keyword matching alone
    # would read a perfectly good answer as a topic change and drop the context.
    _CLARIFY_ANSWER_MAX_WORDS = 5

    def is_clarification_continuation(self, pending: Clarification, message: str) -> bool:
        """Is this turn genuinely answering the clarify-back we sent?

        Public because the guardrails layer gates on it: `pending` is echoed by
        the browser and therefore forgeable, so only a real continuation may skip
        classification. A topic change carrying a forged `pending` must be
        classified like any other new turn.
        """
        return not self._should_abandon_clarification(pending, message)

    def _should_abandon_clarification(self, pending: Clarification, message: str) -> bool:
        """True when the reply is a NEW question rather than an answer to ours.

        A pending clarification must not trap the conversation: a student who
        gives up on the transfer question and asks about parking gets an answer
        about parking, not the same clarify-back a third time.

        Deliberately conservative — it takes a question-shaped message that also
        fires another intent to abandon. Mistakenly abandoning a real answer is
        the worse failure: it drops context the student already gave us.
        """
        text = message.strip()
        lowered = text.lower()

        # An answer to the question we asked is never a topic change.
        if self._fill_clarification(pending, text) is not None:
            return False

        # A major answer can't be slot-filled here (needs the live majors list),
        # so judge by shape: terse replies are answers, not new questions.
        question_shaped = "?" in text or len(text.split()) > self._CLARIFY_ANSWER_MAX_WORDS
        if not question_shaped:
            return False

        return self._matches_any_intent(text, lowered)

    def _schedule_terms_matched(self, message: str, lowered: str) -> list[str]:
        """Schedule terms present, word-boundary matched, weak ones corroborated.

        Returns the matched terms so the caller can report them; an empty list
        means "not a schedule query". Single source of truth for the schedule
        predicate — dispatch, ``_matches_any_intent`` and the telemetry all read
        it, so they cannot drift apart the way the ladder and its mirror did.
        """
        strong = sorted(t for t in SCHEDULE_TERMS_STRONG if _word_in(t, lowered))
        weak = sorted(t for t in SCHEDULE_TERMS_WEAK if _word_in(t, lowered))
        if strong:
            return strong + weak
        # A weak term on its own is just English. Needs a course code or a term
        # reference to read as a schedule question: "can I take MATH 101" yes,
        # "do I need to take the general education course" no.
        if weak and (_find_course_code(message) or self._TERM_REF_RE.search(lowered)):
            return weak
        return []

    def _matches_any_intent(self, message: str, lowered: str) -> bool:
        """Does this message trigger one of the other routes? Used to detect a
        topic change away from a pending clarification."""
        return (
            self._is_system_level_transfer_query(message, lowered)
            or self._is_enrollment_plan_query(lowered)
            or self._should_route_to_kg(message, lowered)
            or self._should_route_to_instructor(message, lowered)
            or self._is_sequence_query(lowered)
            or any(t in lowered for t in ZTC_TERMS)
            or bool(self._schedule_terms_matched(message, lowered))
            or any(t in lowered for t in PROGRAM_TERMS)
            or any(p in lowered for p in RAG_OVERRIDE_PHRASES)
        )

    # ------------------------------------------------------------------
    # Corrections ("this is not correct")
    # ------------------------------------------------------------------

    _CORRECTION_RE = re.compile(
        r"\b(?:not|isn't|isnt)\s+(?:correct|right|accurate)\b"
        r"|\bincorrect\b|\bwrong\b"
        r"|\bnot\s+what\s+i\s+(?:asked|meant|wanted|was\s+looking)"
        r"|\b(?:do|does|did)\s+not\s+belong\b"
        r"|\b(?:don't|doesn't|didn't|dont|doesnt|didnt)\s+belong\b",
        re.IGNORECASE,
    )
    # A correction is short. Anything longer is a new question that happens to
    # contain "wrong" ("what happens if I register for the wrong class?").
    _CORRECTION_MAX_WORDS = 12
    # Our own schedule answers start "Found N ... section(s) at <college> for <term>".
    _SCHEDULE_ANSWER_RE = re.compile(r"^\s*Found\b.*?\bsections?\b.*?\bat (.+?) for ", re.DOTALL)

    def _is_user_correction(self, message: str, history: list[dict] | None) -> bool:
        """Is this turn telling us the previous answer was wrong?"""
        history = _conversation_history(history)
        if not any(item.get("role") == "assistant" for item in history):
            return False  # nothing to correct
        text = message.strip().replace("’", "'")
        if len(text.split()) > self._CORRECTION_MAX_WORDS or not self._CORRECTION_RE.search(text):
            return False
        return not self._matches_any_intent(text, text.lower())

    def _handle_user_correction(self, history: list[dict], campus_code: str) -> ChatResponse:
        """Acknowledge, say where the last answer came from, and ask — never re-run it."""
        last = next(
            (item.get("content", "") for item in reversed(history) if item.get("role") == "assistant"),
            "",
        )
        lines = [
            "Sorry, that wasn't the answer you needed. Rather than run the same "
            "search again, can you tell me what was off?",
        ]
        if source := self._SCHEDULE_ANSWER_RE.search(last):
            lines += [
                "",
                "For reference, the sections I listed come straight from the LACCD "
                f"district class schedule, which lists them under {source.group(1).strip()}.",
            ]
        lines += [
            "",
            "- **Not the classes you meant?** Tell me the subject, building, day, or "
            'time you had in mind, for example "classes in AMP on Saturday".',
            "- **A detail looks wrong?** If a time, room, or instructor looks off, the "
            f"official LACCD Class Search is the final word: {self._class_search_url(campus_code)}",
        ]
        return ChatResponse(answer="\n".join(lines), route="user_correction")

    def _translate_if_needed(self, text: str, lang: str) -> str:
        """Translate a response into the user's language. No-op for English."""
        if lang == "en":
            return text
        lang_name = LANG_NAMES.get(lang, lang)
        prompt = (
            f"Translate to {lang_name}. "
            "Keep markdown formatting, course codes, names, and links unchanged. "
            "Return ONLY the translation.\n\n" + text
        )
        translated = self.tools.gemini_file_search.generate(prompt)
        return translated.strip() if translated else text

    @observe(name="chat.answer")
    async def answer(
        self,
        message: str,
        history: list[dict] | None = None,
        campus: str | None = None,
        use_live_crawl: bool = False,
        resolved: RewriteResult | None = None,
        pending: Clarification | None = None,
    ) -> ChatResponse:
        # A reply to one of our own clarify-backs is answered from the carried
        # slots — no rewriter, no history window, no LLM. That whole path is what
        # produced the greeting, the invented major, and the bot answering its own
        # clarifying question; a slot fill cannot fail those ways.
        if pending and not self._should_abandon_clarification(pending, message):
            campus_code = (campus or settings.default_campus).upper()
            # Only pay for language detection when the reply isn't plain ASCII.
            lang = (
                (await asyncio.to_thread(self._rewrite_with_context, message, history)).detected_lang
                if self._needs_language_detection(message)
                else "en"
            )
            resp = await self._handle_assist_query(
                message, campus_code, lang=lang, pending=pending,
            )
            resp.detected_lang = lang
            lf = get_client()
            lf.set_current_trace_io(input=message, output=resp.answer)
            lf.update_current_span(
                metadata={
                    "route": resp.route,
                    "route_trigger": "assist_clarification",
                    "matched_terms": [pending.needs],
                    "campus": campus_code,
                    "detected_lang": lang,
                },
            )
            return resp

        history = _conversation_history(history)
        # "this is not correct" is feedback on our last answer, not a query. The
        # rewriter turned it into a restatement of that answer's question, which
        # re-ran the same search and returned the same answer, twice. Handled
        # before the routing ladder because no route can answer it.
        if self._is_user_correction(message, history):
            campus_code = (campus or settings.default_campus).upper()
            resp = self._handle_user_correction(history, campus_code)
            lf = get_client()
            lf.set_current_trace_io(input=message, output=resp.answer)
            lf.update_current_span(
                metadata={"route": resp.route, "route_trigger": "user_correction", "campus": campus_code},
            )
            try:
                lf.score_current_trace(name="user_correction", value=1, data_type="NUMERIC")
            except Exception as exc:  # telemetry must not break the reply
                logger.warning("Could not emit the user_correction score: %s", exc)
            return resp

        # `resolved` is supplied when the caller (the guardrails layer) already
        # ran resolve_query() to classify against the standalone query.
        result = resolved or await asyncio.to_thread(
            self._rewrite_with_context,
            message,
            history,
        )
        lang = result.detected_lang
        english_query = result.english_query
        campus_code = (campus or settings.default_campus).upper()
        lowered = english_query.lower()
        # Campus-service questions bypass program/schedule routing
        route_trigger = "default_rag"
        matched_terms: list[str] = []
        if self._is_enrollment_plan_query(lowered):
            route_trigger = "enrollment_plan"
            matched_terms = [term for term in ENROLLMENT_PLAN_TERMS if term in lowered]
            resp = await self._handle_enrollment_plan_query(english_query, campus_code, lang=lang)
        elif self._is_ge_schedule_query(english_query, lowered):
            route_trigger = "ge_schedule_cross_ref"
            matched_terms = [t for t in (KG_GE_TERMS | self._SCHED_AVAIL_TERMS) if t in lowered]
            resp = await self._handle_ge_schedule_query(english_query, campus_code, lang=lang)
        elif self._is_course_schedule_crossref_query(english_query, lowered, history, original_message=message):
            route_trigger = "course_schedule_crossref"
            matched_terms = [t for t in (self._SCHED_AVAIL_TERMS | ALL_CAMPUS_TERMS) if t in lowered]
            logger.info("course_schedule_crossref triggered: query=%r", redact_pii(english_query[:120]))
            resp = await self._handle_course_schedule_crossref(english_query, history, campus_code, lang=lang)
        elif self._is_assist_query(english_query, lowered):
            route_trigger = "assist_articulation"
            matched_terms = [t for t in ASSIST_EXPLICIT_TRIGGERS if t in lowered]
            resp = await self._handle_assist_query(english_query, campus_code, lang=lang)
        elif self._should_route_to_kg(english_query, lowered):
            route_trigger = "knowledge_graph"
            matched_terms = [t for t in KG_ALL_TERMS if t in lowered]
            resp = await self._handle_kg_query(english_query, campus_code, lang=lang)
        elif self._should_route_to_instructor(english_query, lowered):
            route_trigger = "instructor_search"
            matched_terms = [t for t in INSTRUCTOR_TERMS if t in lowered]
            resp = await self._handle_instructor_query(english_query, campus_code, lang=lang)
        elif any(phrase in lowered for phrase in RAG_OVERRIDE_PHRASES):
            route_trigger = "rag_override"
            matched_terms = [p for p in RAG_OVERRIDE_PHRASES if p in lowered]
            resp = await self._handle_rag_query(english_query, campus_code=campus_code, lang=lang, use_live_crawl=use_live_crawl)
        elif any(term in lowered for term in ZTC_TERMS):
            route_trigger = "ztc"
            matched_terms = [t for t in ZTC_TERMS if t in lowered]
            resp = await self._handle_ztc_query(english_query, campus_code, lang=lang)
        elif self._is_sequence_query(lowered):
            route_trigger = "course_sequence"
            matched_terms = [t for t in SEQUENCE_TERMS if t in lowered]
            resp = await self._handle_sequence_query(english_query, campus_code, lang=lang)
        elif any(term in lowered for term in PROGRAM_TERMS):
            route_trigger = "program"
            matched_terms = [t for t in PROGRAM_TERMS if t in lowered]
            resp = await self._handle_program_query(english_query, campus_code, lang=lang)
        elif self._schedule_terms_matched(english_query, lowered):
            route_trigger = "schedule"
            matched_terms = self._schedule_terms_matched(english_query, lowered)
            resp = await self._handle_schedule_query(
                english_query, campus_code, lang=lang, original_message=message,
            )
        else:
            resp = await self._handle_rag_query(english_query, campus_code=campus_code, lang=lang, use_live_crawl=use_live_crawl)
        resp.detected_lang = lang
        lf = get_client()
        lf.set_current_trace_io(input=message, output=resp.answer)
        lf.update_current_span(
            metadata={
                "route": resp.route,
                "route_trigger": route_trigger,
                "matched_terms": matched_terms,
                "campus": campus_code,
                "detected_lang": lang,
            },
        )
        return resp

    @observe(name="chat.rag_query")
    async def _handle_rag_query(self, message: str, campus_code: str = "LAMC", lang: str = "en", use_live_crawl: bool = False) -> ChatResponse:
        campus_cfg = get_campus(campus_code)
        system_prompt = get_system_prompt(campus_code, campus_cfg.name)
        lang_instruction = ""
        if lang != "en":
            lang_name = LANG_NAMES.get(lang, lang)
            lang_instruction = f"\n\nIMPORTANT: Respond entirely in {lang_name}."
        # Enrich RAG prompt with graph context when available
        graph_context = ""
        if neo4j_service.is_configured():
            try:
                graph_context = await neo4j_service.get_entity_context(message)
                if graph_context:
                    graph_context = f"\n\n{graph_context}\nIncorporate these facts naturally into your answer when relevant."
            except Exception:
                logger.debug("Graph context enrichment failed", exc_info=True)
        if self.tools.gemini_file_search.is_configured():
            answer, citations, route = await asyncio.to_thread(
                self.tools.gemini_file_search.answer,
                message, system_prompt=system_prompt + graph_context + lang_instruction,
                campus_code=campus_code,
            )
            if answer:
                return ChatResponse(answer=answer, route=route, citations=citations)
        return ChatResponse(answer=f"I couldn't find grounded {campus_code} website content for that yet.", route="gemini_file_search_unconfigured")

    # Schedule-availability intent: "offered/available" + semester term reference
    _SCHED_AVAIL_TERMS = {"offered", "available", "being taught", "being offered",
                          "can i take", "scheduled", "open section"}
    _TERM_REF_RE = re.compile(
        r'\b(spring|fall|summer|winter)\s+\d{4}\b'
        r'|\bthis\s+(semester|term|spring|fall|summer|winter)\b'
        r'|\bnext\s+(semester|term|spring|fall|summer|winter)\b'
        r'|\b(spring|fall|summer|winter)\s+semester\b',
        re.IGNORECASE,
    )

    def _is_enrollment_plan_query(self, lowered: str) -> bool:
        """Detect a unit-target request that needs schedule + requirements."""

        # A program-specific semester sequence can also mention a unit pace and
        # prerequisites. It must stay on the program sequence route instead of
        # being reduced to a generic one-term class schedule.
        if self._is_sequence_query(lowered):
            return False
        has_target = bool(_UNIT_TARGET_RE.search(lowered))
        has_planning_language = (
            any(term in lowered for term in ENROLLMENT_PLAN_TERMS)
            or bool(
                re.search(
                    r"\b(?:build|create|make|give me)\b[^.?!]{0,80}\b(?:plan|schedule)\b",
                    lowered,
                )
            )
            or (
                any(noun in lowered for noun in ("course", "courses", "class", "classes"))
                and any(
                    term in lowered
                    for term in ("which", "what", "can i take", "should i take", "could i take")
                )
            )
        )
        has_multi_constraint = any(
            term in lowered
            for term in (
                "prerequisite", "prereq", "zero-cost", "zero cost", "ztc",
                "textbook cost", "offered", "this term", "this semester",
            )
        )
        return has_target and has_planning_language and has_multi_constraint

    def _should_route_to_kg(self, message: str, lowered: str) -> bool:
        """Determine if a query should route to the knowledge graph."""
        if not neo4j_service.is_configured():
            return False
        # Schedule-availability intent trumps KG — e.g. "which IGETC 2A courses are offered in Spring 2026?"
        if (any(t in lowered for t in self._SCHED_AVAIL_TERMS)
                and self._TERM_REF_RE.search(lowered)):
            return False
        # Course code + transfer/GE/renumbering terms
        has_course = bool(_find_course_code(message))
        has_kg_terms = any(t in lowered for t in KG_ALL_TERMS)
        if has_course and has_kg_terms:
            return True
        # GE area pattern: "IGETC 2A", "CSU-GE A2", etc.
        if _GE_AREA_RE.search(message):
            return True
        # Renumbering queries (even without a course code: "show me renumberings")
        if any(t in lowered for t in KG_RENUMBERING_TERMS):
            return True
        # GE fulfillment queries with "satisfy" or "fulfill" + GE system name
        if any(t in lowered for t in ("satisfy", "fulfill", "fulfills", "satisfies")):
            if any(t in lowered for t in ("igetc", "csu-ge", "csuge", "cal-getc", "calgetc", "ge ", "general education", "written communication", "oral communication")):
                return True
        return False

    def _is_ge_schedule_query(self, message: str, lowered: str) -> bool:
        """Detect queries that need GE area + schedule cross-reference."""
        if not neo4j_service.is_configured():
            return False
        has_ge = bool(_GE_AREA_RE.search(message)) or (
            any(t in lowered for t in ("satisfy", "fulfill", "fulfills", "satisfies"))
            and any(t in lowered for t in ("igetc", "csu-ge", "csuge", "cal-getc", "calgetc", "ge ", "general education"))
        )
        has_avail = any(t in lowered for t in self._SCHED_AVAIL_TERMS)
        has_term = bool(self._TERM_REF_RE.search(lowered))
        return has_ge and has_avail and has_term

    # Semester filter regex: "semester 1", "first semester", "term 2", etc.
    _SEMESTER_NUM_RE = re.compile(
        r'semester\s+(\d)'
        r'|(\d)(?:st|nd|rd|th)\s+semester'
        r'|term\s+(\d)'
        r'|(first|second|third|fourth)\s+(?:semester|term)',
        re.IGNORECASE,
    )
    _ORDINAL_MAP = {"first": 1, "second": 2, "third": 3, "fourth": 4}

    # Anaphoric cross-campus phrases: "What about at other campuses?", "same courses at other colleges"
    _XREF_CAMPUS_RE = re.compile(
        r'(?:what about|how about|same|these|those|them)\b.*\b(?:other|all|different)\s+campus',
        re.IGNORECASE,
    )

    def _is_course_schedule_crossref_query(
        self, message: str, lowered: str, history: list[dict] | None,
        *, original_message: str = "",
    ) -> bool:
        """Detect follow-up queries that cross-reference courses from history with the schedule."""
        if not history:
            return False
        has_avail = any(t in lowered for t in self._SCHED_AVAIL_TERMS)
        has_term = bool(self._TERM_REF_RE.search(lowered))
        has_all_campus = any(t in lowered for t in ALL_CAMPUS_TERMS)
        # Primary trigger: availability terms + (term reference OR cross-campus)
        primary = has_avail and (has_term or has_all_campus)
        # Secondary trigger: anaphoric cross-campus follow-up (e.g. "what about at other campuses?")
        # Check BOTH rewritten and original message since the rewriter may strip anaphoric references
        secondary = (
            bool(self._XREF_CAMPUS_RE.search(message))
            or bool(self._XREF_CAMPUS_RE.search(original_message))
        )
        if not (primary or secondary):
            return False
        # Previous assistant message must have >= 3 course codes
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                codes = _extract_course_codes_from_answer(msg["content"])
                return len(codes) >= 3
        return False

    def _extract_semester_filter(self, lowered: str) -> int | None:
        """Extract semester number from user query (e.g., 'semester 1' -> 1)."""
        m = self._SEMESTER_NUM_RE.search(lowered)
        if not m:
            return None
        if m.group(1):
            return int(m.group(1))
        if m.group(2):
            return int(m.group(2))
        if m.group(3):
            return int(m.group(3))
        if m.group(4):
            return self._ORDINAL_MAP.get(m.group(4).lower())
        return None

    @staticmethod
    def _filter_codes_by_semester(
        history: list[dict], semester_num: int
    ) -> list[tuple[str, str]] | None:
        """Extract course codes under a specific 'Semester N' header from history."""
        for msg in reversed(history):
            if msg.get("role") != "assistant":
                continue
            text = msg["content"]
            # Match block under "Semester N" header (various formats)
            pattern = re.compile(
                rf'(?:^|\n)\**\s*Semester\s+{semester_num}\b[^\n]*\n((?:.*\n)*?)'
                rf'(?=\n\**\s*Semester\s+\d|\Z)',
                re.IGNORECASE,
            )
            m = pattern.search(text)
            if m:
                block = m.group(1)
                codes = _extract_course_codes_from_answer(block)
                if codes:
                    return codes
        return None

    def _should_route_to_instructor(self, message: str, lowered: str) -> bool:
        """Determine if a query should route to instructor search."""
        if not neo4j_service.is_configured():
            return False
        if any(t in lowered for t in INSTRUCTOR_TERMS):
            return True
        if _INSTRUCTOR_QUERY_RE.search(message):
            return True
        return False

    @observe(name="chat.kg_query")
    async def _handle_kg_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        """Handle knowledge graph queries: transfer, GE areas, renumberings."""
        lowered = message.lower()

        # 1. Detect query type and extract parameters
        course_match = _find_course_code(message)
        ge_match = _GE_AREA_RE.search(message)

        # GE area courses query: "What courses fulfill IGETC 2A?"
        if ge_match:
            ge_code = ge_match.group(1).upper()
            system = self._detect_ge_system(lowered)
            return await self._kg_ge_courses(ge_code, system, campus_code, lang)

        # GE system-level query without area code: "What courses satisfy CSU-GE Written Communication?"
        if any(t in lowered for t in ("satisfy", "fulfill", "fulfills", "satisfies")) and not course_match:
            system = self._detect_ge_system(lowered)
            ge_code = self._detect_ge_code_from_name(lowered)
            if ge_code:
                return await self._kg_ge_courses(ge_code, system, campus_code, lang)

        # Course-specific queries
        if course_match:
            raw_subject = course_match.group(1).strip().upper()
            number = course_match.group(2).replace("-", "")  # Normalize C-1000 → C1000
            # Resolve subject code for this specific course (ENGLISH C1000 → ENGL C1000)
            subject = await neo4j_service.resolve_course_subject(raw_subject, number, campus_code)
            # Multi-fact questions must stay together.  The single-purpose
            # handlers below intentionally short-circuit, which previously
            # dropped renumbering from transfer + GE + history questions.
            requested_facets = sum(
                (
                    any(term in lowered for term in KG_RENUMBERING_TERMS),
                    any(term in lowered for term in KG_PREREQ_TERMS),
                    any(term in lowered for term in KG_ADVISORY_TERMS),
                    any(term in lowered for term in KG_COREQ_TERMS),
                    any(term in lowered for term in KG_TRANSFER_TERMS),
                    any(term in lowered for term in KG_GE_TERMS)
                    or bool(re.search(r"\bge\b", lowered)),
                )
            )
            if requested_facets >= 2:
                return await self._kg_course_info(subject, number, campus_code, lang)
            # Renumbering query (check BEFORE GE to avoid "renumber*ge*d" false match)
            if any(t in lowered for t in KG_RENUMBERING_TERMS):
                return await self._kg_renumbering(subject, number, campus_code, lang)
            # Prerequisite query
            if any(t in lowered for t in KG_PREREQ_TERMS):
                return await self._kg_prerequisites(subject, number, campus_code, lang)
            # Advisory query
            if any(t in lowered for t in KG_ADVISORY_TERMS):
                return await self._kg_advisories(subject, number, campus_code, lang)
            # Corequisite query
            if any(t in lowered for t in KG_COREQ_TERMS):
                return await self._kg_corequisites(subject, number, campus_code, lang)
            # Transfer query
            if any(t in lowered for t in KG_TRANSFER_TERMS):
                return await self._kg_transfer(subject, number, campus_code, lang)
            # GE areas query for a course
            if any(t in lowered for t in KG_GE_TERMS) or re.search(r'\bge\b', lowered):
                return await self._kg_course_ge(subject, number, campus_code, lang)
            # Default: show all course info
            return await self._kg_course_info(subject, number, campus_code, lang)

        # Renumbering list: "Show me all renumberings at LAMC"
        if any(t in lowered for t in KG_RENUMBERING_TERMS):
            return await self._kg_renumberings_list(campus_code, lang)

        # Fallback to RAG
        return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)

    def _detect_ge_system(self, lowered: str) -> str:
        if "cal-getc" in lowered or "calgetc" in lowered:
            return "Cal-GETC"
        if "csu-ge" in lowered or "csuge" in lowered or "csu ge" in lowered:
            return "CSU-GE"
        return "IGETC"

    def _detect_ge_code_from_name(self, lowered: str) -> str | None:
        """Map GE area names to codes."""
        name_map = {
            "written communication": "A2", "oral communication": "A1",
            "critical thinking": "A3", "english composition": "1B",
            "english communication": "1A",
            "math": "2A", "quantitative reasoning": "2A", "mathematics": "B4",
            "physical science": "5A", "biological science": "5B",
            "laboratory": "5C", "lab": "5C",
            "arts": "3A", "humanities": "3B",
            "social science": "4", "social and behavioral": "4",
            "ethnic studies": "7",
        }
        for name, code in name_map.items():
            if name in lowered:
                return code
        return None

    async def _kg_not_found(self, subject: str, number: str, campus_code: str, lang: str, query_type: str = "data") -> ChatResponse:
        """Generate a helpful 'not found' response with suggestions."""
        code_label = f"{subject} {number}"
        lines = [f"I couldn't find {query_type} for **{code_label}** in the verified academic records."]
        lines.append("")
        lines.append("This could mean:")
        lines.append(f"- The course code may be slightly different — LACCD courses sometimes use prefixes like **C** (e.g., CHEM **C**211 vs CHEM **211**) or abbreviated subjects (e.g., **ENGL** instead of ENGLISH)")
        lines.append("- The course may not be offered at this campus")
        lines.append("- There may be a typo in the course code")
        # Try to find similar courses
        similar = await neo4j_service.find_similar_courses(subject, number, campus_code)
        if similar:
            lines.append("")
            lines.append("**Did you mean one of these?**")
            for s in similar:
                lines.append(f"- **{s['code']}** — {s['title']}")
        return ChatResponse(answer=self._translate_if_needed("\n".join(lines), lang), route="knowledge_graph")

    async def _kg_transfer(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        transfer = await neo4j_service.get_course_transfer_status(subject, number, campus_code)
        if not transfer:
            return await self._kg_not_found(subject, number, campus_code, lang, "transfer data")
        code = transfer["code"]
        title = transfer["title"]
        uc = "Yes" if transfer["uc_transferable"] else "No"
        csu = "Yes" if transfer["csu_transferable"] else "No"
        lines = [f"**{code}** — {title}", ""]
        lines.append(f"- **UC Transferable:** {uc}")
        lines.append(f"- **CSU Transferable:** {csu}")
        # Also show GE areas
        ge = await neo4j_service.get_course_ge_areas(subject, number, campus_code)
        if ge:
            lines.append("")
            lines.append("**GE areas fulfilled:**")
            for g in ge:
                lines.append(f"- {g['ge_system']}: Area {g['ge_code']} ({g['ge_name']})")
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"transfer": transfer, "ge_areas": ge},
        )

    async def _kg_course_ge(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        ge = await neo4j_service.get_course_ge_areas(subject, number, campus_code)
        transfer = await neo4j_service.get_course_transfer_status(subject, number, campus_code)
        title = transfer["title"] if transfer else ""
        code_label = f"{subject} {number}"
        if not ge:
            return await self._kg_not_found(subject, number, campus_code, lang, "GE area data")
        lines = [f"**{code_label}** — {title}" if title else f"**{code_label}**", ""]
        lines.append("**GE areas fulfilled:**")
        for g in ge:
            lines.append(f"- {g['ge_system']}: Area {g['ge_code']} ({g['ge_name']})")
        if transfer:
            lines.append("")
            uc = "Yes" if transfer["uc_transferable"] else "No"
            csu = "Yes" if transfer["csu_transferable"] else "No"
            lines.append(f"UC Transferable: {uc} | CSU Transferable: {csu}")
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"ge_areas": ge, "transfer": transfer},
        )

    @staticmethod
    def _format_renumbering_lines(queried_code: str, refs: list[dict]) -> list[str]:
        """Render cross-references without confusing the two directions.

        ``find_course_cross_refs`` returns edges both ways. A forward row means
        the queried course became ``new_code``; a REVERSE row means some other
        course became the queried course, and so carries the queried course
        itself in ``new_code``. Rendering every row as a target listed the
        course as its own replacement, once per reverse edge.
        """
        became: list[tuple[str, str]] = []
        replaced: list[tuple[str, str]] = []
        for r in refs:
            new_code = (r.get("new_code") or "").strip()
            old_code = (r.get("old_code") or "").strip()
            title = r.get("title") or ""
            if new_code and new_code != queried_code:
                became.append((new_code, title))
            elif old_code and old_code != queried_code:
                # Reverse edge: `old_code` is the course that became this one.
                replaced.append((old_code, title))

        def _bullets(pairs: list[tuple[str, str]]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for code, title in pairs:
                if code in seen:
                    continue
                seen.add(code)
                out.append(f"- **{code}** — {title}" if title else f"- **{code}**")
            return out

        lines: list[str] = []
        forward = _bullets(became)
        if forward:
            lines += [f"**{queried_code}** has been renumbered:", ""] + forward
        backward = _bullets(replaced)
        if backward:
            if lines:
                lines.append("")
            lines += [f"These courses were renumbered to **{queried_code}**:", ""]
            lines += backward
        return lines

    async def _kg_renumbering(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        refs = await neo4j_service.find_course_cross_refs(subject, number, campus_code)
        if not refs:
            return await self._kg_not_found(subject, number, campus_code, lang, "renumbering data")
        lines = self._format_renumbering_lines(f"{subject.upper()} {number}", refs)
        if not lines:
            return await self._kg_not_found(subject, number, campus_code, lang, "renumbering data")
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"cross_refs": refs},
        )

    async def _kg_course_info(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        """Show comprehensive course info from the knowledge graph."""
        details = await neo4j_service.get_course_details(subject, number, campus_code)
        transfer = await neo4j_service.get_course_transfer_status(subject, number, campus_code)
        ge = await neo4j_service.get_course_ge_areas(subject, number, campus_code)
        refs = await neo4j_service.find_course_cross_refs(subject, number, campus_code)
        prereqs = await neo4j_service.get_prerequisites(subject, number, campus_code)
        coreqs = await neo4j_service.get_course_corequisites(subject, number, campus_code)
        advisories = await neo4j_service.get_course_advisories(subject, number, campus_code)

        if not transfer and not ge and not refs and not details:
            return await self._kg_not_found(subject, number, campus_code, lang, "course data")

        code_label = f"{subject} {number}"
        title = (details or {}).get("title") or (transfer["title"] if transfer else "")
        units_min = (details or {}).get("units_min") or (transfer or {}).get("units_min")
        units_str = f" ({units_min} units)" if units_min else ""
        lines = [f"**{code_label}** — {title}{units_str}" if title else f"**{code_label}**"]

        # Description — prefer catalog, fall back to eLumen
        description = (details or {}).get("catalog_description") or (details or {}).get("description", "")
        if description:
            lines.append("")
            lines.append(description)

        # Transfer status
        if transfer:
            uc = "Yes" if transfer["uc_transferable"] else "No"
            csu = "Yes" if transfer["csu_transferable"] else "No"
            lines.append("")
            lines.append(f"- **UC Transferable:** {uc} | **CSU Transferable:** {csu}")

        # C-ID (from catalog)
        c_id = (details or {}).get("c_id")
        if c_id:
            c_id_list = c_id if isinstance(c_id, list) else [c_id]
            if c_id_list:
                lines.append(f"- **C-ID:** {', '.join(c_id_list)}")

        # Prerequisites — catalog text is authoritative, then structured, then eLumen
        lines.append("")
        catalog_prereq = (details or {}).get("catalog_prereq_text")
        if prereqs:
            prereq_strs = [f"**{p['prereq_code']}** ({p['prereq_title']})" for p in prereqs]
            lines.append(f"- **Prerequisite:** {', '.join(prereq_strs)}")
        elif catalog_prereq is not None:
            if catalog_prereq == "":
                lines.append("- **Prerequisite:** None")
            else:
                lines.append(f"- **Prerequisite:** {catalog_prereq}")
        elif details and details.get("prereq_status") == "text_only" and details.get("prereq_text"):
            lines.append(f"- **Prerequisite:** {details['prereq_text']}")
        elif details and details.get("prereq_status") == "none":
            lines.append("- **Prerequisite:** None")

        # Corequisites — catalog supplements eLumen
        catalog_coreq = (details or {}).get("catalog_coreq_text", "")
        if coreqs:
            coreq_strs = [f"**{c['coreq_code']}** ({c['coreq_title']})" for c in coreqs]
            lines.append(f"- **Corequisite:** {', '.join(coreq_strs)}")
        elif catalog_coreq:
            lines.append(f"- **Corequisite:** {catalog_coreq}")
        elif details and details.get("corequisite_text"):
            lines.append(f"- **Corequisite:** {details['corequisite_text']}")
        elif details and (details.get("prereq_status") is not None or catalog_prereq is not None):
            lines.append("- **Corequisite:** None")

        # Advisories — catalog supplements eLumen
        catalog_advisory = (details or {}).get("catalog_advisory_text", "")
        if advisories:
            adv_strs = [f"**{a['advisory_code']}** ({a['advisory_title']})" for a in advisories]
            lines.append(f"- **Advisory:** {', '.join(adv_strs)}")
        elif catalog_advisory:
            lines.append(f"- **Advisory:** {catalog_advisory}")
        elif details and details.get("advisory_text"):
            lines.append(f"- **Advisory:** {details['advisory_text']}")
        elif details and (details.get("prereq_status") is not None or catalog_prereq is not None):
            lines.append("- **Advisory:** None")

        # GE areas
        if ge:
            lines.append("")
            lines.append("**GE areas fulfilled:**")
            for g in ge:
                lines.append(f"- {g['ge_system']}: Area {g['ge_code']} ({g['ge_name']})")

        # Renumberings
        if refs:
            lines.append("")
            lines.append("**Course renumberings:**")
            for r in refs:
                lines.append(f"- Renumbered to **{r['new_code']}** ({r['title']})")

        # Everything above comes from the base catalog. If an addendum has since
        # touched this course, say so rather than presenting stale units or
        # prerequisites as current.
        addendum_note = addenda.notice_for(campus_code, code_label)
        if addendum_note:
            lines.append("")
            lines.append(f"_{addendum_note}_")

        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={
                "transfer": transfer, "ge_areas": ge, "cross_refs": refs,
                "details": details is not None,
                "addendum_affected": addendum_note is not None,
            },
        )

    async def _kg_ge_courses(self, ge_code: str, system: str, campus_code: str, lang: str) -> ChatResponse:
        courses = await neo4j_service.get_ge_courses(ge_code, system, campus_code)
        if not courses:
            answer = f"No courses found for {system} Area {ge_code} at {campus_code}."
            return ChatResponse(answer=self._translate_if_needed(answer, lang), route="knowledge_graph")
        # Group by subject for readability
        by_subject: dict[str, list[dict]] = {}
        for c in courses:
            subj = c.get("subject_code", "Other")
            by_subject.setdefault(subj, []).append(c)
        lines = [f"**{system} Area {ge_code}** — {len(courses)} courses at {campus_code}", ""]
        if len(by_subject) <= 3:
            # Few subjects: flat list
            for c in courses:
                lines.append(f"- **{c['course_code']}** — {c['title']} ({c['units_min']} units)")
        else:
            # Many subjects: group with headers for accordion display
            for subj, subj_courses in sorted(by_subject.items()):
                lines.append(f"{subj} ({len(subj_courses)} courses):")
                for c in subj_courses:
                    lines.append(f"- **{c['course_code']}** — {c['title']} ({c['units_min']} units)")
                lines.append("")
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"ge_code": ge_code, "system": system, "courses": courses},
        )

    @observe(name="chat.enrollment_plan_query")
    async def _handle_enrollment_plan_query(
        self,
        message: str,
        campus_code: str,
        lang: str = "en",
    ) -> ChatResponse:
        """Build a candidate unit plan from offered, no-prerequisite sections."""

        unit_match = _UNIT_TARGET_RE.search(message)
        target_units = float(unit_match.group(1)) if unit_match else 12.0
        term = self._extract_term_code(message.lower())
        payload = await build_candidate_enrollment_plan(
            campus_code=campus_code,
            term=term,
            target_units=target_units,
        )
        answer = format_candidate_enrollment_plan(payload)
        return ChatResponse(
            answer=self._translate_if_needed(answer, lang),
            route="enrollment_plan",
            tool_payload=payload,
        )

    @observe(name="chat.ge_schedule_query")
    async def _handle_ge_schedule_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        """Cross-reference GE area courses with the live schedule."""
        lowered = message.lower()
        term = self._extract_term_code(lowered)
        term_name = get_term_name(term)

        # Extract GE area
        ge_match = _GE_AREA_RE.search(message)
        if ge_match:
            ge_code = ge_match.group(1).upper()
            system = self._detect_ge_system(lowered)
        else:
            system = self._detect_ge_system(lowered)
            ge_code = self._detect_ge_code_from_name(lowered)
            if not ge_code:
                return await self._handle_schedule_query(message, campus_code, lang=lang)

        async def collect_for_campus(code: str) -> tuple[str, dict[str, Any]]:
            ge_courses = await neo4j_service.get_ge_courses(ge_code, system, code)
            ge_codes = {course["course_code"] for course in ge_courses}
            subjects = sorted({course["subject_code"] for course in ge_courses})
            checked_subjects = subjects[:MAX_GE_SUBJECTS]
            unchecked_subjects = subjects[MAX_GE_SUBJECTS:]
            listing = await get_class_listing(code, term, checked_subjects)
            checked_codes = {
                course_code
                for course_code in ge_codes
                if course_code.split(" ", 1)[0] in set(checked_subjects)
            }
            offered_sections = [
                section
                for section in format_sections(listing)
                if section.get("course_code") in checked_codes
            ]
            offered_codes = {section["course_code"] for section in offered_sections}
            return code, {
                "campus_name": CAMPUS_NAMES.get(code, code),
                "ge_courses": ge_courses,
                "offered_sections": offered_sections,
                "offered_codes": sorted(offered_codes),
                "not_offered": sorted(checked_codes - offered_codes),
                "unchecked_subjects": unchecked_subjects,
            }

        wants_cross_campus = any(term_phrase in lowered for term_phrase in ALL_CAMPUS_TERMS)
        target_campuses = ALL_CAMPUSES if wants_cross_campus else [campus_code]
        collected = await asyncio.gather(
            *[collect_for_campus(code) for code in target_campuses],
            return_exceptions=True,
        )
        campuses = {
            code: payload
            for item in collected
            if isinstance(item, tuple)
            for code, payload in [item]
            if payload.get("ge_courses")
        }
        campuses_with_sections = {
            code: payload
            for code, payload in campuses.items()
            if payload.get("offered_sections")
        }

        if not campuses:
            scope = "across LACCD" if wants_cross_campus else f"at {campus_code}"
            answer = f"No courses found for {system} Area {ge_code} {scope}."
            return ChatResponse(
                answer=self._translate_if_needed(answer, lang),
                route="ge_schedule_cross_ref",
            )

        if not campuses_with_sections:
            scope = "across LACCD" if wants_cross_campus else f"at {campus_code}"
            answer = (
                f"None of the verified **{system} Area {ge_code}** courses "
                f"appear on the {term_name} schedule {scope} yet."
            )
            return ChatResponse(
                answer=self._translate_if_needed(answer, lang),
                route="ge_schedule_cross_ref",
            )

        if wants_cross_campus:
            total_sections = sum(
                len(payload["offered_sections"])
                for payload in campuses_with_sections.values()
            )
            cost_sections = sum(
                1
                for payload in campuses_with_sections.values()
                for section in payload["offered_sections"]
                if any(
                    marker in (section.get("class_type") or [])
                    for marker in ("OER", "LCT")
                )
            )
            lines = [
                f"**{system} Area {ge_code} across LACCD — {term_name}**",
                "",
                f"Found **{total_sections} offered sections** at "
                f"**{len(campuses_with_sections)} colleges**; "
                f"**{cost_sections}** are marked Zero Textbook Cost or Low Cost.",
                "",
            ]
            ordered = [campus_code] + [code for code in ALL_CAMPUSES if code != campus_code]
            for code in ordered:
                payload = campuses_with_sections.get(code)
                if not payload:
                    continue
                sections = payload["offered_sections"]
                sections.sort(
                    key=lambda section: (
                        0 if "OER" in (section.get("class_type") or []) else
                        1 if "LCT" in (section.get("class_type") or []) else 2,
                        section.get("course_code", ""),
                        section.get("class_number", ""),
                    )
                )
                campus_cost = sum(
                    1
                    for section in sections
                    if any(
                        marker in (section.get("class_type") or [])
                        for marker in ("OER", "LCT")
                    )
                )
                lines.append(
                    f"**{payload['campus_name']}** — "
                    f"{len(payload['offered_codes'])} courses / {len(sections)} sections; "
                    f"{campus_cost} ZTC or Low Cost"
                )
                for section in sections[:5]:
                    lines.append(self._fmt_section(section, strip_campus=True))
                if len(sections) > 5:
                    lines.append(f"- *...{len(sections) - 5} more offered sections*")
                lines.append("")
            lines.append(
                "*The public schedule confirms that these sections are offered, "
                "but it does not expose reliable live seat counts here. Confirm "
                "current availability before registering.*"
            )
            return ChatResponse(
                answer=self._translate_if_needed("\n".join(lines), lang),
                route="ge_schedule_cross_ref",
                tool_payload={
                    "ge_code": ge_code,
                    "system": system,
                    "scope": "district",
                    "campuses": campuses_with_sections,
                    "offered_sections": total_sections,
                    "cost_sections": cost_sections,
                },
            )

        payload = campuses_with_sections[campus_code]
        ge_courses = payload["ge_courses"]
        ge_by_code = {course["course_code"]: course for course in ge_courses}
        offered_sections = payload["offered_sections"]
        lines = [
            f"**{system} Area {ge_code}** — {len(payload['offered_codes'])} of "
            f"{len(ge_courses)} courses offered at {campus_code} for {term_name}",
            "",
        ]
        by_course: dict[str, list[dict]] = {}
        for section in offered_sections:
            by_course.setdefault(section["course_code"], []).append(section)
        for code in sorted(by_course):
            info = ge_by_code.get(code, {})
            sections = by_course[code]
            lines.append(
                f"**{code}** — {info.get('title', '')} "
                f"({len(sections)} section{'s' if len(sections) != 1 else ''}):"
            )
            for section in sections[:5]:
                lines.append(self._fmt_section(section, strip_campus=True))
            if len(sections) > 5:
                lines.append(f"- *...{len(sections) - 5} more sections*")
            lines.append("")
        if payload["not_offered"]:
            lines.append(
                f"**Not offered this term** ({len(payload['not_offered'])}): "
                f"{', '.join(payload['not_offered'])}"
            )
        lines.append(
            "\n*The public schedule confirms offered sections, not reliable live seat counts.*"
        )
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="ge_schedule_cross_ref",
            tool_payload={
                "ge_code": ge_code,
                "system": system,
                "scope": campus_code,
                "offered": len(payload["offered_codes"]),
                "total": len(ge_courses),
            },
        )

    @observe(name="chat.course_schedule_crossref")
    async def _handle_course_schedule_crossref(
        self, message: str, history: list[dict], campus_code: str, lang: str = "en"
    ) -> ChatResponse:
        """Cross-reference courses from conversation history with the live schedule."""
        lowered = message.lower()
        term = self._extract_term_code(lowered)
        term_name = get_term_name(term)
        wants_cross_campus = any(t in lowered for t in ALL_CAMPUS_TERMS)

        # Extract all course codes from most recent assistant message
        all_codes: list[tuple[str, str]] = []
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                all_codes = _extract_course_codes_from_answer(msg["content"])
                break

        if not all_codes:
            return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)

        # Optionally filter to a specific semester
        sem_num = self._extract_semester_filter(lowered)
        if sem_num:
            sem_codes = self._filter_codes_by_semester(history, sem_num)
            if sem_codes:
                all_codes = sem_codes

        # Group codes by subject. Each subject costs one live-schedule call, so
        # the number searched stays capped -- but whatever the cap drops is now
        # tracked and said out loud. Insertion order is the order the courses
        # appear in the sequence, so the earliest terms are the ones searched,
        # which is what a student asking about next semester cares about.
        by_subject: dict[str, list[str]] = {}
        for subject, number in all_codes:
            by_subject.setdefault(subject, []).append(number)
        subjects_to_search = dict(list(by_subject.items())[:MAX_CROSSREF_SUBJECTS])
        skipped_subjects: dict[str, list[str]] = {
            subject: numbers
            for subject, numbers in by_subject.items()
            if subject not in subjects_to_search
        }

        # Search schedule for each subject and filter to listed courses
        from app.services.live_schedule import CAMPUS_NAMES
        offered_sections: list[dict] = []
        offered_codes: set[str] = set()
        # Every course the student was actually shown, whether or not we manage
        # to look it up. Counting only the searched ones is what let "7 of 10"
        # describe a sequence of eighteen courses.
        listed_codes = {f"{s} {n}" for s, numbers in by_subject.items() for n in numbers}
        target_codes = {f"{s} {n}" for s, numbers in subjects_to_search.items() for n in numbers}
        target_numbers_by_subject = {s: set(nums) for s, nums in subjects_to_search.items()}

        for subject, numbers in subjects_to_search.items():
            # Resolve subject code for schedule API
            subj_code, _ = await find_subject_code(subject, campus_code, term)
            if not subj_code:
                # We cannot say anything about these courses, so record them as
                # unchecked. Falling through silently would have let them be
                # counted as absent from the schedule.
                skipped_subjects[subject] = numbers
                continue
            if wants_cross_campus:
                results = await search_all_campuses(
                    term=term, subject=subj_code, primary_campus=campus_code
                )
                for campus_data in (results.get("campuses") or {}).values():
                    for s in campus_data.get("sections", []):
                        sec_code = s.get("course_code", "")
                        # Check if section matches any of our target course numbers
                        sec_parts = sec_code.split()
                        sec_num = sec_parts[-1] if sec_parts else ""
                        if sec_code in target_codes or sec_num in target_numbers_by_subject.get(subject, set()):
                            # Tag with campus name for cross-campus display
                            s["_campus_name"] = campus_data.get("campus_name", "")
                            offered_sections.append(s)
                            offered_codes.add(sec_code)
            else:
                result = await search_live_schedule(campus_code, term=term, subject=subj_code)
                for s in result.get("sections", []):
                    sec_code = s.get("course_code", "")
                    sec_parts = sec_code.split()
                    sec_num = sec_parts[-1] if sec_parts else ""
                    if sec_code in target_codes or sec_num in target_numbers_by_subject.get(subject, set()):
                        offered_sections.append(s)
                        offered_codes.add(sec_code)

        unchecked_codes = sorted(
            f"{s} {n}" for s, numbers in skipped_subjects.items() for n in numbers
        )
        # "Not offered" must mean looked up and absent. A course we never
        # queried is a different statement, and the student acts on it
        # differently: one means pick another class, the other means ask again.
        checked_codes = target_codes - set(unchecked_codes)
        not_offered = sorted(checked_codes - offered_codes)
        total_listed = len(listed_codes)
        unchecked_note = (
            ""
            if not unchecked_codes
            else (
                f"\n\n**I couldn't check {len(unchecked_codes)} of these** "
                f"({', '.join(unchecked_codes)}) — ask me about them directly "
                "and I'll look them up."
            )
        )

        if not offered_sections:
            sem_note = f" semester {sem_num}" if sem_num else ""
            scope = "across the district" if wants_cross_campus else f"at {campus_code}"
            if unchecked_codes:
                answer = (
                    f"None of the {len(checked_codes)} courses I could check (of "
                    f"{total_listed}{sem_note} listed) appear on the {term_name} "
                    f"schedule {scope} yet."
                )
            else:
                answer = (
                    f"None of the {total_listed}{sem_note} listed courses appear on the "
                    f"{term_name} schedule {scope} yet."
                )
            if not_offered:
                answer += f"\n\n**Not offered this term:** {', '.join(not_offered)}"
            answer += unchecked_note
            return ChatResponse(
                answer=self._translate_if_needed(answer, lang),
                route="course_schedule_crossref",
            )

        # Format results
        sem_label = f" (semester {sem_num})" if sem_num else ""
        scope = "across the district" if wants_cross_campus else f"at {campus_code}"
        lines = [
            f"**{len(offered_codes)}** of **{total_listed}** listed courses{sem_label} "
            f"are offered {scope} for {term_name}",
            "",
        ]

        # Group sections by course code
        by_course: dict[str, list[dict]] = {}
        for s in offered_sections:
            by_course.setdefault(s["course_code"], []).append(s)

        for code in sorted(by_course.keys()):
            secs = by_course[code]
            title = secs[0].get("course_title", "")
            lines.append(f"**{code}** — {title} ({len(secs)} section{'s' if len(secs) != 1 else ''}):")
            for s in secs[:5]:
                if wants_cross_campus and s.get("_campus_name"):
                    campus_tag = f"[{s['_campus_name']}] "
                else:
                    campus_tag = ""
                lines.append(f"{campus_tag}{self._fmt_section(s, strip_campus=True)}")
            if len(secs) > 5:
                lines.append(f"- *...{len(secs) - 5} more sections*")
            lines.append("")

        if not_offered:
            lines.append(f"**Not offered this term** ({len(not_offered)}): {', '.join(not_offered)}")
        if unchecked_note:
            lines.append(unchecked_note.strip())

        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="course_schedule_crossref",
            tool_payload={"offered": len(offered_codes), "total": total_listed, "semester_filter": sem_num},
        )

    async def _kg_renumberings_list(self, campus_code: str, lang: str) -> ChatResponse:
        renumberings = await neo4j_service.get_all_renumberings(campus_code)
        if not renumberings:
            answer = f"No renumbering data found for {campus_code}."
            return ChatResponse(answer=self._translate_if_needed(answer, lang), route="knowledge_graph")
        # Group by old subject code (department)
        by_dept: dict[str, list[dict]] = {}
        for r in renumberings:
            dept = r["old_code"].split()[0] if " " in r["old_code"] else "Other"
            by_dept.setdefault(dept, []).append(r)
        # Sort departments by number of renumberings (most first)
        sorted_depts = sorted(by_dept.items(), key=lambda x: -len(x[1]))
        # Use header:list pattern so frontend auto-creates accordions per department
        lines = [f"**Course renumberings at {campus_code}** — {len(renumberings)} total across {len(by_dept)} departments", ""]
        for dept, dept_renums in sorted_depts:
            lines.append(f"{dept} ({len(dept_renums)} renumberings):")
            for r in dept_renums:
                lines.append(f"- {r['old_code']} → **{r['new_code']}**")
            lines.append("")
        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"renumberings": renumberings[:50], "total": len(renumberings)},
        )

    async def _kg_prerequisites(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        """Show prerequisites for a course, with renumbering fallback and related requisites."""
        prereqs = await neo4j_service.get_prerequisites(subject, number, campus_code)
        details = await neo4j_service.get_course_details(subject, number, campus_code)
        title = (details or {}).get("title") or ""
        if not title:
            transfer = await neo4j_service.get_course_transfer_status(subject, number, campus_code)
            title = transfer["title"] if transfer else ""
        code_label = f"{subject} {number}"
        cross_ref_note = ""

        # If no prereqs found, check renumbered course and use its prereqs
        if not prereqs:
            cross_refs = await neo4j_service.find_course_cross_refs(subject, number, campus_code)
            for ref in cross_refs:
                new_subj = ref.get("new_subject", "")
                new_num = ref.get("new_number", "")
                if new_subj and new_num:
                    prereqs = await neo4j_service.get_prerequisites(new_subj, new_num, campus_code)
                    if prereqs:
                        if not title:
                            title = ref.get("title", "")
                        cross_ref_note = f"\n\n*Note: {code_label} has been renumbered to {ref.get('new_code', '')}. Prerequisites shown are from the current course number.*"
                        break

        title_part = f" — {title}" if title else ""

        if not prereqs:
            # Check catalog prereq text first (authoritative source)
            catalog_prereq = (details or {}).get("catalog_prereq_text")

            if catalog_prereq is not None:
                # Catalog was parsed for this course
                lines = [f"**{code_label}**{title_part}", ""]
                if catalog_prereq == "":
                    lines.append("**Prerequisite:** None")
                else:
                    lines.append(f"**Prerequisite:** {catalog_prereq}")
                await self._append_coreqs_advisories_async(lines, details, campus_code, subject, number)
                return ChatResponse(
                    answer=self._translate_if_needed("\n".join(lines), lang),
                    route="knowledge_graph",
                )

            # Check eLumen prereq_status (fallback when no catalog data)
            prereq_status = (details or {}).get("prereq_status")
            prereq_text = (details or {}).get("prereq_text", "")

            if prereq_status == "text_only" and prereq_text:
                # Text-based prerequisites (competency/placement)
                lines = [f"**{code_label}**{title_part}", ""]
                lines.append(f"**Prerequisite:** {prereq_text}")
                await self._append_coreqs_advisories_async(lines, details, campus_code, subject, number)
                return ChatResponse(
                    answer=self._translate_if_needed("\n".join(lines), lang),
                    route="knowledge_graph",
                )

            if prereq_status == "none":
                # eLumen explicitly says no prerequisites
                lines = [f"**{code_label}**{title_part}", ""]
                lines.append("**Prerequisite:** None")
                await self._append_coreqs_advisories_async(lines, details, campus_code, subject, number)
                return ChatResponse(
                    answer=self._translate_if_needed("\n".join(lines), lang),
                    route="knowledge_graph",
                )

            # No catalog or eLumen data — fall back to RAG
            logger.info("No KG prereqs for %s (status=%s), falling back to RAG", code_label, prereq_status)
            return await self._handle_rag_query(
                f"What are the prerequisites for {code_label} at {campus_code}?",
                campus_code, lang,
            )

        # Group by req_group for "or" formatting
        groups: dict[int, list[dict]] = {}
        for p in prereqs:
            g = p.get("req_group") or 0
            groups.setdefault(g, []).append(p)

        lines = [f"**{code_label}**{title_part}", ""]
        lines.append("**Prerequisite:**")

        for group_id in sorted(groups.keys()):
            group_prereqs = groups[group_id]
            if len(group_prereqs) == 1:
                p = group_prereqs[0]
                lines.append(f"- **{p['prereq_code']}** — {p['prereq_title']}")
            else:
                options = " **or** ".join(f"**{p['prereq_code']}** ({p['prereq_title']})" for p in group_prereqs)
                lines.append(f"- {options}")

        if cross_ref_note:
            lines.append(cross_ref_note)

        # Also show coreqs/advisories
        await self._append_coreqs_advisories_async(lines, details, campus_code, subject, number)

        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
            tool_payload={"prerequisites": prereqs},
        )

    async def _append_coreqs_advisories_async(
        self, lines: list[str], details: dict | None,
        campus_code: str, subject: str, number: str,
    ) -> None:
        """Append corequisite and advisory info to response lines."""
        coreqs = await neo4j_service.get_course_corequisites(subject, number, campus_code)
        advisories = await neo4j_service.get_course_advisories(subject, number, campus_code)

        catalog_coreq = (details or {}).get("catalog_coreq_text", "")
        catalog_advisory = (details or {}).get("catalog_advisory_text", "")

        if coreqs:
            coreq_strs = [f"**{c['coreq_code']}** ({c['coreq_title']})" for c in coreqs]
            lines.append(f"**Corequisite:** {', '.join(coreq_strs)}")
        elif catalog_coreq:
            lines.append(f"**Corequisite:** {catalog_coreq}")
        elif details and details.get("corequisite_text"):
            lines.append(f"**Corequisite:** {details['corequisite_text']}")

        if advisories:
            adv_strs = [f"**{a['advisory_code']}** ({a['advisory_title']})" for a in advisories]
            lines.append(f"**Advisory:** {', '.join(adv_strs)}")
        elif catalog_advisory:
            lines.append(f"**Advisory:** {catalog_advisory}")
        elif details and details.get("advisory_text"):
            lines.append(f"**Advisory:** {details['advisory_text']}")

        # Prerequisites and corequisites are exactly what addenda most often
        # change, so every requirement answer routes through here to be flagged.
        addendum_note = addenda.notice_for(campus_code, f"{subject} {number}")
        if addendum_note:
            lines.append("")
            lines.append(f"_{addendum_note}_")

    async def _kg_advisories(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        """Show advisories for a course."""
        advisories = await neo4j_service.get_course_advisories(subject, number, campus_code)
        details = await neo4j_service.get_course_details(subject, number, campus_code)
        code_label = f"{subject} {number}"
        title = (details or {}).get("title") or ""
        title_part = f" — {title}" if title else ""

        if not advisories and not (details and details.get("advisory_text")):
            # Check if course exists at all
            if not details:
                return await self._kg_not_found(subject, number, campus_code, lang, "advisory data")
            lines = [f"**{code_label}**{title_part}", "", "**Advisory:** None"]
            return ChatResponse(
                answer=self._translate_if_needed("\n".join(lines), lang),
                route="knowledge_graph",
            )

        lines = [f"**{code_label}**{title_part}", ""]
        if advisories:
            lines.append("**Advisory:**")
            for a in advisories:
                lines.append(f"- **{a['advisory_code']}** — {a['advisory_title']}")
        elif details and details.get("advisory_text"):
            lines.append(f"**Advisory:** {details['advisory_text']}")

        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
        )

    async def _kg_corequisites(self, subject: str, number: str, campus_code: str, lang: str) -> ChatResponse:
        """Show corequisites for a course."""
        coreqs = await neo4j_service.get_course_corequisites(subject, number, campus_code)
        details = await neo4j_service.get_course_details(subject, number, campus_code)
        code_label = f"{subject} {number}"
        title = (details or {}).get("title") or ""
        title_part = f" — {title}" if title else ""

        if not coreqs and not (details and details.get("corequisite_text")):
            if not details:
                return await self._kg_not_found(subject, number, campus_code, lang, "corequisite data")
            lines = [f"**{code_label}**{title_part}", "", "**Corequisite:** None"]
            return ChatResponse(
                answer=self._translate_if_needed("\n".join(lines), lang),
                route="knowledge_graph",
            )

        lines = [f"**{code_label}**{title_part}", ""]
        if coreqs:
            lines.append("**Corequisite:**")
            for c in coreqs:
                lines.append(f"- **{c['coreq_code']}** — {c['coreq_title']}")
        elif details and details.get("corequisite_text"):
            lines.append(f"**Corequisite:** {details['corequisite_text']}")

        return ChatResponse(
            answer=self._translate_if_needed("\n".join(lines), lang),
            route="knowledge_graph",
        )

    @observe(name="chat.instructor_query")
    async def _handle_instructor_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        """Handle instructor search queries: course→instructor and instructor→courses."""
        course_match = _find_course_code(message)

        # Direction 1: Course → Instructor ("Who teaches MATH 261?")
        if course_match:
            raw_subject = course_match.group(1).strip().upper()
            number = course_match.group(2).replace("-", "")  # Normalize C-1000 → C1000
            subject = await neo4j_service.resolve_course_subject(raw_subject, number, campus_code)
            instructors = await neo4j_service.find_course_instructors(subject, number, campus_code)
            if not instructors:
                answer = (
                    f"No instructor data found for **{subject} {number}** at {campus_code}. "
                    "The schedule for this course may not be published yet, or the course "
                    "may not be offered this term."
                )
                return ChatResponse(
                    answer=self._translate_if_needed(answer, lang),
                    route="instructor_search",
                )
            course_title = instructors[0]["course_title"]
            names = list(dict.fromkeys(i["name"] for i in instructors))
            names_str = ", ".join(f"**{n}**" for n in names)
            answer = f"**{subject} {number}** ({course_title}) is taught by {names_str} at {campus_code}."
            return ChatResponse(
                answer=self._translate_if_needed(answer, lang),
                route="instructor_search",
                tool_payload={"instructors": instructors},
            )

        # Direction 2: Instructor → Courses ("What does Professor Smith teach?")
        name = self._extract_instructor_name(message)
        if name:
            results = await neo4j_service.search_instructors(name, campus_code)
            if not results:
                results = await neo4j_service.search_instructors(name)
            if not results:
                answer = f"No instructor named **{name}** found in our records. Try a different spelling or check the class schedule."
                return ChatResponse(
                    answer=self._translate_if_needed(answer, lang),
                    route="instructor_search",
                )
            lines: list[str] = []
            for r in results:
                display = r["name"]
                campus = r["campus_code"]
                courses = [f"{code} ({title})" for code, title in zip(r["courses"][:8], r["course_titles"][:8])]
                courses_str = ", ".join(courses) if courses else "no courses listed"
                lines.append(f"**{display}** at {campus}: {courses_str}")
            answer = "\n\n".join(lines)
            return ChatResponse(
                answer=self._translate_if_needed(answer, lang),
                route="instructor_search",
                tool_payload={"results": results},
            )

        # Fallback — couldn't parse the query
        return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)

    def _extract_instructor_name(self, message: str) -> str | None:
        """Extract instructor name from a query."""
        # "Professor Smith", "Prof. Garcia", "Dr. Lee"
        title_match = re.search(r'\b(?:professor|prof\.?|dr\.?)\s+([A-Za-z][A-Za-z\s\'-]+)', message, re.IGNORECASE)
        if title_match:
            return title_match.group(1).strip()
        # "what does Smith teach", "what courses does Garcia teach"
        teach_match = re.search(
            r'(?:what|which)\s+(?:does|do|is|classes|courses)\s+(.+?)\s+teach',
            message, re.IGNORECASE,
        )
        if teach_match:
            name = teach_match.group(1).strip()
            name = re.sub(r'^(?:professor|prof\.?|dr\.?)\s+', '', name, flags=re.IGNORECASE)
            return name if name else None
        # "taught by Smith"
        by_match = re.search(r'taught\s+by\s+([A-Za-z][A-Za-z\s\'-]+)', message, re.IGNORECASE)
        if by_match:
            return by_match.group(1).strip()
        return None

    def _is_sequence_query(self, lowered: str) -> bool:
        """Check if query is about course sequencing."""
        has_seq_term = any(t in lowered for t in SEQUENCE_TERMS)
        has_program_ref = (
            any(t in lowered for t in PROGRAM_TERMS)
            or bool(_find_course_code(lowered))
            or bool(re.search(r'\b(?:aa|as|adt|associate|transfer|cert|certificate)\b', lowered))
        )

        if re.search(r'\bin what order\b', lowered):
            return True

        casual_sequence_patterns = (
            r'\bwhat\s+(?:classes|courses)\s+should\s+i\s+take\b',
            r'\bhow\s+(?:do|can|should)\s+i\s+(?:finish|complete)\b',
            r'\bmap\s+out\b.*\bpath\b',
        )
        if any(re.search(pattern, lowered) for pattern in casual_sequence_patterns):
            return has_program_ref or "map out" in lowered

        if not has_seq_term:
            return False
        # Strong sequence terms don't need a program term — they're unambiguous
        strong_terms = {"course sequence", "course plan", "course order", "semester plan",
                        "prerequisite chain", "course roadmap", "course map", "course pathway",
                        "amp template", "what order", "in what order"}
        if any(t in lowered for t in strong_terms):
            return True
        # Weaker terms need a program/discipline mention to avoid false matches
        return has_program_ref

    async def _search_programs_neo4j(self, query: str, campus_code: str | None = None) -> list[dict]:
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

    # ------------------------------------------------------------------
    # ASSIST.org articulation routing
    # ------------------------------------------------------------------

    def _is_system_level_transfer_query(self, message: str, lowered: str) -> bool:
        """True for 'is COURSE transferable to UC/CSU?' — a question about the
        SYSTEMS, not a named campus.

        This is a course attribute (uc_transferable / csu_transferable) the
        knowledge graph answers outright. ASSIST cannot: it needs a specific
        receiving campus, so it clarifies back forever — the student means
        "both", which is not a campus and never will be.
        """
        if not _find_course_code(message):
            return False  # general transfer advice, not a course lookup
        if any(target in lowered for target in ASSIST_TARGET_INSTITUTIONS):
            return False  # a campus IS named — that's genuine ASSIST territory
        return bool(_SYSTEM_LEVEL_TRANSFER_RE.search(lowered))

    def _is_assist_query(self, message: str, lowered: str) -> bool:
        """An ASSIST query has either an explicit trigger word ('articulation',
        'transfer to ...') OR a UC/CSU institution name paired with a transfer
        verb.

        System-level transferability is explicitly excluded — it belongs to the
        knowledge graph (see _is_system_level_transfer_query).
        """
        if self._is_system_level_transfer_query(message, lowered):
            return False
        if any(trigger in lowered for trigger in ASSIST_EXPLICIT_TRIGGERS):
            return True
        if any(target in lowered for target in ASSIST_TARGET_INSTITUTIONS):
            if any(verb in lowered for verb in ASSIST_TRANSFER_VERBS):
                return True
        return False

    def _matched_target_term(self, text: str) -> str | None:
        """The UC/CSU alias named in `text` ("ucla", "csun"), longest match first.

        The campus SLOT must store this alias, not ASSIST's display name: the
        display name ("University of California, Los Angeles") contains none of
        the aliases, so re-resolving it on the next turn finds nothing and we ask
        for the campus we already have.
        """
        lowered = text.lower()
        for target in sorted(ASSIST_TARGET_INSTITUTIONS, key=len, reverse=True):
            if target in lowered:
                return target
        return None

    async def _extract_receiving_institution(self, message: str) -> dict | None:
        """Find the UC/CSU institution mentioned in the message (longest match
        wins to prefer 'cal poly san luis obispo' over 'cal poly')."""
        lowered = message.lower()
        candidates = sorted(ASSIST_TARGET_INSTITUTIONS, key=len, reverse=True)
        matched_term = next((t for t in candidates if t in lowered), None)
        if not matched_term:
            return None
        return await self.tools.assist.find_institution(matched_term)

    def _detect_assist_subintent(self, message: str, lowered: str) -> str:
        """Pick one of: 'list_majors', 'articulation_for_major', 'course_transfer_check'.

        Course-transfer-check requires a specific course code in the message.
        List-majors fires on bare 'what majors transfer to UCLA' style.
        Otherwise (named major or articulation request) → articulation_for_major.
        """
        if _find_course_code(message):
            return "course_transfer_check"
        list_phrases = ("what majors", "which majors", "list of majors",
                        "available majors", "majors transfer", "majors at",
                        "majors offered",
                        # "Does LAMC have a transfer program to UCLA?" asks WHETHER
                        # agreements exist. Listing them answers it; asking "which
                        # major?" first makes the student guess before we've shown
                        # them what's on offer.
                        "transfer program", "transfer agreement", "agreements with")
        if any(p in lowered for p in list_phrases):
            return "list_majors"
        return "articulation_for_major"

    def _format_majors_list(
        self, sending: dict, receiving: dict, year_id: int, majors: list[dict],
        source: str, lang: str,
    ) -> str:
        sending_name = (sending.get("names", [{}])[0].get("name")
                        or (sending.get("code") or "").strip())
        receiving_name = (receiving.get("names", [{}])[0].get("name")
                          or (receiving.get("code") or "").strip())
        if not majors:
            txt = f"ASSIST has no published major-level articulation between {sending_name} and {receiving_name} for the current academic year."
            return self._translate_if_needed(txt, lang)
        lines = [f"**Majors with articulation: {sending_name} → {receiving_name}**", ""]
        for m in majors:
            label = m.get("label", "?")
            lines.append(f"- {label}")
        if len(majors) >= 30:
            lines.append("")
            lines.append(f"_Showing all {len(majors)} agreements. Ask about a specific major to see the course-by-course articulation._")
        if source == "neo4j_fallback":
            lines.append("")
            lines.append("_(Source: cached snapshot — live ASSIST API was unreachable.)_")
        return self._translate_if_needed("\n".join(lines), lang)

    def _llm_compose_articulation(
        self, sending: dict, receiving: dict, articulation: dict,
        user_question: str, lang: str,
    ) -> str:
        """LLM-compose a student-facing answer from a structured articulation doc."""
        # Strip the bulkier nested fields to keep the prompt within token budget
        slim = {
            "name": articulation.get("name"),
            "type": articulation.get("type"),
            "publishDate": articulation.get("publishDate"),
            "articulations": articulation.get("articulations"),
        }
        sending_name = (sending.get("names", [{}])[0].get("name")
                        or (sending.get("code") or "").strip())
        receiving_name = (receiving.get("names", [{}])[0].get("name")
                          or (receiving.get("code") or "").strip())
        lang_instruction = ""
        if lang != "en":
            lang_instruction = f"\n\nIMPORTANT: Respond entirely in {LANG_NAMES.get(lang, lang)}."
        prompt = (
            "You are a transfer counselor. The student asked: "
            f"\"{user_question}\"\n\n"
            f"Below is the official ASSIST.org articulation document for "
            f"{sending_name} → {receiving_name}.\n\n"
            "Summarize the articulation clearly. List each receiving requirement "
            "alongside the sending courses that satisfy it. Use bullets, group "
            "by section, keep course codes intact, and call out any 'No Articulation' "
            "rows the student should know about. Cite the publish date if shown."
            f"{lang_instruction}\n\n"
            f"```json\n{json.dumps(slim, default=str)[:60000]}\n```"
        )
        answer = self.tools.gemini_file_search.generate(prompt)
        if not answer:
            return self._translate_if_needed(
                f"I found the {receiving_name} articulation document but couldn't summarize it. "
                "Please visit assist.org for the full details.", lang,
            )
        return answer.strip()

    def _llm_compose_course_transfer(
        self, course_code: str, sending: dict, receiving: dict,
        major_label: str, articulation: dict, user_question: str, lang: str,
    ) -> str:
        """LLM-compose answer to 'does X transfer for major Y at campus Z'."""
        lang_instruction = ""
        if lang != "en":
            lang_instruction = f"\n\nIMPORTANT: Respond entirely in {LANG_NAMES.get(lang, lang)}."
        sending_name = (sending.get("names", [{}])[0].get("name")
                        or (sending.get("code") or "").strip())
        receiving_name = (receiving.get("names", [{}])[0].get("name")
                          or (receiving.get("code") or "").strip())
        prompt = (
            f"The student asked: \"{user_question}\"\n\n"
            f"Search the ASSIST.org articulation below ({sending_name} → "
            f"{receiving_name} for major \"{major_label}\") and answer: does "
            f"{course_code} satisfy any requirement under this major? If yes, "
            f"name the specific receiving requirement(s) it satisfies and any "
            f"co-courses required. If no, say so plainly. Quote course codes "
            f"verbatim. Be concise (3-5 sentences).{lang_instruction}\n\n"
            f"```json\n{json.dumps({'articulations': articulation.get('articulations')}, default=str)[:60000]}\n```"
        )
        answer = self.tools.gemini_file_search.generate(prompt)
        if not answer:
            return self._translate_if_needed(
                f"I couldn't determine whether {course_code} transfers to "
                f"{receiving_name} for {major_label}. Please check assist.org directly.",
                lang,
            )
        return answer.strip()

    def _fuzzy_match_major(self, user_text: str, majors: list[dict]) -> dict | None:
        """Pick the major from `majors` whose label best matches the user's text.

        Strategy: token-overlap score on label words (excluding stop words and
        the trailing degree codes like ', B.S.'). Returns None if no candidate
        scores above a minimum threshold."""
        lowered = user_text.lower()
        # Strip degree-code tail (", B.S.", ", B.A.", etc.) for matching
        def normalize(s: str) -> set[str]:
            s = re.sub(r",\s*[bm]\.[as]\.?\b.*$", "", s, flags=re.IGNORECASE)
            return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2}
        user_tokens = normalize(lowered)
        stop_tokens = {"the", "and", "for", "from", "into", "with", "transfer", "transfers",
                       "articulation", "major", "majors", "ucla", "csun", "berkeley"}
        user_tokens -= stop_tokens
        if not user_tokens:
            return None
        best_score = 0.0
        best_match: dict | None = None
        for m in majors:
            label_tokens = normalize(m.get("label", ""))
            if not label_tokens:
                continue
            overlap = len(user_tokens & label_tokens)
            if overlap == 0:
                continue
            score = overlap / max(len(label_tokens), 1)
            if score > best_score:
                best_score = score
                best_match = m
        # Require at least one matching token AND > 30% of label tokens
        return best_match if best_score >= 0.3 else None

    def _known_assist_slots(self, message: str, slots: dict[str, str]) -> dict[str, str]:
        """Slots we can state as fact: what a previous turn resolved, plus anything
        this turn's message adds. Never a guess — an unresolved slot stays empty
        so we ask for it rather than inventing it."""
        known = dict(slots)
        if not known.get("course"):
            match = _find_course_code(message)
            if match:
                known["course"] = f"{match.group(1)} {match.group(2)}"
        return known

    def _format_campus_clarify(self) -> str:
        """Clarify-back when no receiving campus was named.

        Names real campuses so the student knows what a valid answer looks like.
        A bare 'which campus?' invited answers like "both" — which is not a
        campus, so the same question came back verbatim, forever. (Course-level
        "transferable to UC and CSU" no longer reaches ASSIST at all; see
        _is_system_level_transfer_query.)
        """
        return (
            "Which UC or CSU campus are you transferring to? Articulation "
            "agreements are specific to one receiving campus.\n\n"
            "For example: UCLA, UC Berkeley, UC San Diego, UC Irvine, CSUN, "
            "Cal State LA, Cal State Long Beach, or Cal Poly Pomona.\n\n"
            "If you just want to know whether a course is UC/CSU transferable "
            "in general, ask me that instead — I can answer it without a campus."
        )

    _MAJOR_CLARIFY_SAMPLE = 8

    def _format_major_clarify(
        self, message: str, majors: list[dict], slots: dict[str, str] | None = None,
    ) -> str:
        """Clarify-back for a turn we couldn't pin to a major.

        Offers REAL majors from the live agreement list. The old copy named a
        hypothetical one ("...may articulate as MATH 31A for Computer Science
        but not for English"); on the next turn the context rewriter folded that
        example into the standalone query and we answered for Computer Science —
        a major the student never picked.
        """
        lines = ["Which major are you applying to? Articulation depends on the major."]
        # Did the student answer "which major?" with a course code? With a pending
        # clarification we know it for certain: we hold the course already, so any
        # code in the reply is a second one. Without one, fall back to counting.
        answered_with_a_course = (
            bool(_find_course_code(message))
            if (slots or {}).get("course")
            else len(_extract_all_course_codes(message)) >= 2
        )
        if answered_with_a_course:
            lines.append(
                "A course code isn't a major — tell me the major you're applying to "
                "and I'll check how your course fits it."
            )
        labels = [m.get("label") for m in majors if m.get("label")]
        sample = labels[: self._MAJOR_CLARIFY_SAMPLE]
        if sample:
            lines.append("")
            lines.append("For example:")
            lines.extend(f"- {label}" for label in sample)
            remaining = len(labels) - len(sample)
            if remaining > 0:
                lines.append(f"…and {remaining} more.")
        return "\n".join(lines)

    @observe(name="chat.assist_query")
    async def _handle_assist_query(
        self, message: str, campus_code: str, lang: str = "en",
        pending: Clarification | None = None,
    ) -> ChatResponse:
        """Dispatch to one of three ASSIST sub-intents and compose the answer.

        `pending` carries the slots resolved on earlier turns (course, campus,
        major). They are used verbatim — never re-derived from the transcript.
        """
        lowered = message.lower()
        slots = dict(pending.slots) if pending else {}

        # A carried course means we're mid-clarification on a course question,
        # even though this turn's message ("UCLA") contains no course code.
        if slots.get("course"):
            sub_intent = "course_transfer_check"
        else:
            sub_intent = self._detect_assist_subintent(message, lowered)

        # Resolve sending campus from request's campus_code
        sending = await self.tools.assist.find_laccd_institution(campus_code)
        if not sending:
            return ChatResponse(
                answer=self._translate_if_needed(
                    f"This campus ({campus_code}) is not in the ASSIST institution list. "
                    "I can't look up articulation for this campus.", lang,
                ),
                route="assist_articulation",
                tool_payload={"intent": sub_intent, "error": "unknown_campus", "campus_code": campus_code},
            )

        # Resolve receiving institution: this turn's message first, then the slot
        # a previous turn already filled.
        receiving = await self._extract_receiving_institution(message)
        if not receiving and slots.get("campus"):
            receiving = await self._extract_receiving_institution(slots["campus"])
        if not receiving:
            known = self._known_assist_slots(message, slots)
            return ChatResponse(
                answer=self._translate_if_needed(self._format_campus_clarify(), lang),
                route="assist_articulation",
                tool_payload={"intent": sub_intent, "needs": "receiving_institution"},
                pending=Clarification(
                    intent="assist", needs="receiving_institution", slots=known,
                ).to_dict(),
            )

        try:
            year_id = await self.tools.assist.current_academic_year_id()
        except (httpx.TimeoutException, httpx.HTTPError, AssistAPIError) as exc:
            logger.warning("ASSIST live API unavailable: %s", exc)
            return ChatResponse(
                answer=self._translate_if_needed(
                    "ASSIST.org is temporarily unavailable. Please try again later, "
                    "or visit assist.org directly for course articulation.", lang,
                ),
                route="assist_articulation",
                tool_payload={"intent": sub_intent, "source": "unavailable"},
            )

        sending_id = sending.get("id")
        receiving_id = receiving.get("id")
        if sending_id is None or receiving_id is None:
            # Fallback institution entries lack `id`. We can't query the live
            # API without it, and there's no flat-file articulation either.
            return ChatResponse(
                answer=self._translate_if_needed(
                    "ASSIST.org is temporarily unavailable for major-level "
                    "articulation. Please try again later.", lang,
                ),
                route="assist_articulation",
                tool_payload={"intent": sub_intent, "source": "unavailable"},
            )

        try:
            majors = await self.tools.assist.list_agreements(
                sending_id, receiving_id, year_id, category="major",
            )
        except AssistAPIError:
            return ChatResponse(
                answer=self._translate_if_needed(
                    "ASSIST.org is temporarily unavailable. Please try again later, "
                    "or visit assist.org directly.", lang,
                ),
                route="assist_articulation",
                tool_payload={"intent": sub_intent, "source": "unavailable"},
            )

        source = self.tools.assist.last_source.get("list_agreements", "live")
        base_payload = {
            "intent": sub_intent,
            "source": source,
            "sending": {
                "id": sending_id,
                "code": (sending.get("code") or "").strip(),
                "name": sending.get("names", [{}])[0].get("name"),
            },
            "receiving": {
                "id": receiving_id,
                "code": (receiving.get("code") or "").strip(),
                "name": receiving.get("names", [{}])[0].get("name"),
            },
            "year_id": year_id,
        }

        if sub_intent == "list_majors":
            answer = self._format_majors_list(sending, receiving, year_id, majors, source, lang)
            return ChatResponse(
                answer=answer,
                route="assist_articulation",
                tool_payload={**base_payload, "majors": majors},
            )

        # For the other two intents we need a specific major. Prefer the slot a
        # previous turn already filled; otherwise read it from this turn.
        major_match = None
        if slots.get("major"):
            major_match = next((m for m in majors if m.get("label") == slots["major"]), None)
        if not major_match:
            major_match = self._fuzzy_match_major(message, majors)
        if not major_match:
            # Couldn't pin down the major — clarify-back with real major options,
            # carrying the course/campus we already have so the next terse reply
            # doesn't have to be reconstructed from the transcript.
            known = self._known_assist_slots(message, slots)
            # Store the alias we can re-resolve ("ucla"), never the display name.
            # This turn's campus wins, so a student can switch ("actually, CSUN").
            known["campus"] = (
                self._matched_target_term(message) or known.get("campus") or ""
            )
            return ChatResponse(
                answer=self._translate_if_needed(
                    self._format_major_clarify(message, majors, slots), lang,
                ),
                route="assist_articulation",
                tool_payload={
                    **base_payload,
                    "needs": "major",
                    "majors_available": [m.get("label") for m in majors[:20]],
                },
                pending=Clarification(
                    intent="assist", needs="major",
                    slots={k: v for k, v in known.items() if v},
                ).to_dict(),
            )

        try:
            articulation = await self.tools.assist.get_articulation(major_match["key"])
        except AssistAPIError:
            return ChatResponse(
                answer=self._translate_if_needed(
                    "ASSIST.org is temporarily unavailable for this articulation document. "
                    "Please try again later.", lang,
                ),
                route="assist_articulation",
                tool_payload={**base_payload, "source": "unavailable", "major": major_match.get("label")},
            )

        if sub_intent == "course_transfer_check":
            # The course comes from the carried slot when this turn is a terse reply
            # ("Mathematics") — the message itself has no course code in it.
            course_code = self._known_assist_slots(message, slots).get("course", "")
            # Likewise, ask the composer the ORIGINAL question, not the bare reply.
            question = (
                f"Is {course_code} transferable to "
                f"{receiving.get('names', [{}])[0].get('name')} for {major_match['label']}?"
                if slots.get("course") else message
            )
            answer = self._llm_compose_course_transfer(
                course_code, sending, receiving, major_match["label"],
                articulation, question, lang,
            )
            return ChatResponse(
                answer=answer,
                route="assist_articulation",
                tool_payload={
                    **base_payload,
                    "major": major_match.get("label"),
                    "course_code": course_code,
                    "articulation_key": major_match.get("key"),
                },
            )

        # articulation_for_major
        answer = self._llm_compose_articulation(
            sending, receiving, articulation, message, lang,
        )
        return ChatResponse(
            answer=answer,
            route="assist_articulation",
            tool_payload={
                **base_payload,
                "major": major_match.get("label"),
                "articulation_key": major_match.get("key"),
            },
        )

    @observe(name="chat.sequence_query")
    async def _handle_sequence_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        """Generate a semester-by-semester course sequence for a program."""
        search_all = any(term in message.lower() for term in ALL_CAMPUS_TERMS)

        # Detect disambiguation selection BEFORE search so we use a clean query
        # Regex accounts for query rewriter appending "degree."/"program." after parens
        title_award_match = re.search(
            r'(?:for|about)\s+(?:the\s+)?(.+?)\s*\(([^)]+)\)(?:\s+(?:degree|program|major|certificate))?\.?\s*$',
            message, re.IGNORECASE,
        )
        if title_award_match:
            query = title_award_match.group(1).strip()
            target_award = title_award_match.group(2).strip().lower()
        else:
            query = self._extract_program_phrase(message)
            target_award = None

        matches = []
        try:
            if search_all:
                matches = await search_programs_all_campuses(query)
            else:
                client = get_program_mapper_client(campus_code)
                matches = await client.search_programs(query)
        except Exception as e:
            logger.warning(f"PM API search failed, trying Neo4j: {e}")
        # Neo4j fallback for program search
        if not matches:
            matches = await self._search_programs_neo4j(query, None if search_all else campus_code)
        if not matches:
            return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
        matches = select_explicit_program(message, matches)
        # Filter to exact match on both title and award to break disambiguation loops
        if target_award and len(matches) > 1:
            target_title = query.lower()
            exact = [m for m in matches
                     if target_title in (m.get('title') or '').lower()
                     and target_award in (m.get('awardShortTitle') or m.get('award_type') or '').lower()]
            if exact:
                matches = exact[:1]
        if len(matches) > 3:
            preview = "\n".join(
                f"- {m.get('title')} ({m.get('awardShortTitle', '')})" for m in matches[:6]
            )
            answer_text = f"I found multiple programs. Which one do you want the course sequence for?\n{preview}"
            return ChatResponse(
                answer=self._translate_if_needed(answer_text, lang),
                route="course_sequence",
                tool_payload={"matches": matches[:10]},
            )
        # Generate sequence for the top match
        top = matches[0]
        target_campus = top.get("campus_code", campus_code)
        # Detect pace intent from the user's wording ("part time", "fast track").
        # Default ("full" / 15 u-per-term) when no keyword matches.
        pace = detect_pace_from_message(message)
        try:
            svc = get_course_sequence_service(target_campus)
            program_id = top.get("masterRecordId") or top.get("master_record_id")
            sequence = await svc.generate_sequence(
                program_id, target_campus, pace=pace, assist_service=self.tools.assist,
            )
            context = format_sequence_for_context(sequence)
        except Exception as e:
            logger.warning(
                "Course sequence generation failed for %s at %s: %s",
                top.get("title") or top.get("name") or top.get("masterRecordId"),
                target_campus,
                e,
                exc_info=True,
            )
            fallback = await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
            program_title = top.get("title") or "that program"
            answer_prefix = self._translate_if_needed(
                f"I found {program_title}, but I couldn't build the semester-by-semester course sequence right now. "
                "Here is the best program information I could find instead:",
                lang,
            )
            return ChatResponse(
                answer=f"{answer_prefix}\n\n{fallback.answer}",
                route="course_sequence",
                tool_payload={
                    "sequence_status": "generation_failed",
                    "program": top,
                    "fallback_route": fallback.route,
                    "error": str(e),
                },
                citations=fallback.citations,
                fallback_mode=True,
            )
        # Pass to Gemini for natural language formatting
        lang_instruction = ""
        if lang != "en":
            lang_name = LANG_NAMES.get(lang, lang)
            lang_instruction = f"\n\nIMPORTANT: Respond entirely in {lang_name}."
        campus_cfg = get_campus(campus_code)
        system_prompt = get_system_prompt(campus_code, campus_cfg.name)
        if self.tools.gemini_file_search.is_configured():
            answer, citations, route = self.tools.gemini_file_search.answer(
                message,
                system_prompt=system_prompt + SEQUENCE_CONTEXT_PROMPT.format(sequence_data=context) + lang_instruction,
                campus_code=campus_code,
            )
            if answer:
                return ChatResponse(answer=answer, route="course_sequence", tool_payload=sequence, citations=citations)
        # Fallback: return raw formatted sequence
        return ChatResponse(
            answer=self._translate_if_needed(context, lang),
            route="course_sequence",
            tool_payload=sequence,
        )

    @observe(name="chat.program_query")
    async def _handle_program_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        search_all = any(term in message.lower() for term in ALL_CAMPUS_TERMS)

        # Detect disambiguation selection BEFORE search so we use a clean query
        # Regex accounts for query rewriter appending "degree."/"program." after parens
        title_award_match = re.search(
            r'(?:for|about)\s+(?:the\s+)?(.+?)\s*\(([^)]+)\)(?:\s+(?:degree|program|major|certificate))?\.?\s*$',
            message, re.IGNORECASE,
        )
        if title_award_match:
            query = title_award_match.group(1).strip()
            target_award = title_award_match.group(2).strip().lower()
        else:
            query = self._extract_program_phrase(message)
            target_award = None

        try:
            if search_all:
                matches = await search_programs_all_campuses(query)
                # Filter for disambiguation selection
                if target_award and len(matches) > 1:
                    target_title = query.lower()
                    exact = [m for m in matches
                             if target_title in (m.get('title') or '').lower()
                             and target_award in (m.get('awardShortTitle') or m.get('award_type') or '').lower()]
                    if exact:
                        matches = exact[:1]
                if len(matches) > 6:
                    preview = "\n".join(
                        f"- {match.get('title')} — {match.get('awardShortTitle', 'award not labeled')} ({match.get('campus_name')})" for match in matches[:6]
                    )
                    answer_text = f"I found multiple program matches across the district. Please pick one:\n{preview}"
                    return ChatResponse(
                        answer=self._translate_if_needed(answer_text, lang),
                        route="program_mapper_search_all",
                        tool_payload={"matches": matches[:10]},
                    )
                if not matches:
                    logger.info("District-wide program search empty for '%s', falling back to RAG", redact_pii(query))
                    return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
                top = matches[0]
                client = get_program_mapper_client(top["campus_code"])
                bundle = await client.get_program_bundle(top["masterRecordId"])
                return ChatResponse(
                    answer=self._translate_if_needed(self._format_program_bundle(bundle), lang),
                    route="program_mapper_bundle_all",
                    tool_payload=bundle,
                )
            client = get_program_mapper_client(campus_code)
            matches = await client.search_programs(query)
        except Exception as e:
            logger.warning(f"PM API failed for program query: {e}")
            return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
        if not matches:
            # "classes ... for Fall 2026" is a schedule question whatever else it
            # says; website RAG has no section data and answers "no information".
            lowered = message.lower()
            if self._schedule_terms_matched(message, lowered) and self._TERM_REF_RE.search(lowered):
                logger.info("Program search empty for '%s' at %s, answering from the schedule", redact_pii(query), campus_code)
                return await self._handle_schedule_query(message, campus_code, lang=lang)
            logger.info("Program search empty for '%s' at %s, falling back to RAG", redact_pii(query), campus_code)
            return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
        matches = select_explicit_program(message, matches)
        # Filter for disambiguation selection
        if target_award and len(matches) > 1:
            target_title = query.lower()
            exact = [m for m in matches
                     if target_title in (m.get('title') or '').lower()
                     and target_award in (m.get('awardShortTitle') or m.get('award_type') or '').lower()]
            if exact:
                matches = exact[:1]
        if len(matches) > 1:
            preview = "\n".join(f"- {match.get('title')} ({match.get('awardShortTitle', 'award not labeled')})" for match in matches[:6])
            answer_text = f"I found multiple {client.campus_name} programs that match. Please clarify which one you want:\n{preview}"
            return ChatResponse(
                answer=self._translate_if_needed(answer_text, lang),
                route="program_mapper_search",
                tool_payload={"matches": matches[:10]},
            )
        try:
            bundle = await client.get_program_bundle(matches[0]["masterRecordId"])
        except Exception as e:
            logger.warning(f"PM API bundle fetch failed: {e}")
            return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)
        return ChatResponse(answer=self._translate_if_needed(self._format_program_bundle(bundle), lang), route="program_mapper_bundle", tool_payload=bundle)

    @observe(name="chat.schedule_query")
    async def _handle_schedule_query(
        self, message: str, campus_code: str, lang: str = "en", original_message: str | None = None,
    ) -> ChatResponse:
        lowered = message.lower()
        term = self._extract_term_code(lowered)
        course_number = self._extract_course_number(message)
        # Read constraints from the student's own words too: the rewriter has
        # expanded "AMP" to "Arts, Media, and Performance" and dropped it outright.
        texts = [message] + ([original_message] if original_message and original_message != message else [])
        filters = self._extract_schedule_filters(" ".join(texts))
        building = await self._detect_building(texts, campus_code, term)
        if building:
            filters = replace(filters, building=building)
        # A building scopes the search by itself. Mining the words around it for
        # subjects is how "Arts, Media, and Performance (AMP)" became Media Arts.
        # Only an explicit course code ("ART 201 in AMP") still narrows it. Case-
        # sensitive on purpose: the upper-cased fallback reads "Fall 2026" as a
        # course, and "AMP 302" is a room, not a course.
        explicit = _COURSE_CODE_RE.search(message)
        names_course = bool(explicit) and explicit.group(1).strip().upper() != building
        subject_guesses = (
            self._extract_subject_guesses(message) if not building or names_course else []
        )
        subject_code = None
        subject_name = None
        subject_guess = subject_guesses[0] if subject_guesses else None

        # Multi-subject search: when follow-up references multiple subjects (e.g. MATH + STAT)
        if len(subject_guesses) > 1:
            all_sections = []
            matched_subjects = []
            matched_codes: set[str] = set()
            for guess in subject_guesses:
                code, name = await find_subject_code(guess, campus_code, term)
                # Two guesses often land on one subject ("media", "arts"); fetching
                # it twice listed every section twice.
                if code and code not in matched_codes:
                    matched_codes.add(code)
                    result = await search_live_schedule(
                        campus_code, term=term, subject=code,
                        course_number=course_number,
                        keyword=None,
                        filters=filters,
                    )
                    sections = result.get("sections", [])
                    all_sections.extend(sections)
                    matched_subjects.append(name or code)
                    if len(matched_subjects) >= 3:  # cap to avoid huge responses
                        break
            if len(matched_subjects) > 1:
                # Build a combined payload and format it
                combined_payload = {
                    "campus": campus_code,
                    "sections": all_sections,
                    "total_sections": len(all_sections),
                    "campus_name": result.get("campus_name", campus_code),
                    "term_name": result.get("term_name", get_term_name(term)),
                }
                subj_label = ", ".join(matched_subjects)
                answer_text = self._format_live_schedule(combined_payload, subj_label, filters)
                return ChatResponse(
                    answer=self._translate_if_needed(answer_text, lang),
                    route="live_schedule",
                    tool_payload=combined_payload,
                )
            # If only one resolved, fall through to normal single-subject path
            if matched_subjects:
                subject_code = None  # reset — will be set in loop below
                subject_name = None

        # Try each candidate until one resolves to a real subject code
        for guess in subject_guesses:
            code, name = await find_subject_code(guess, campus_code, term)
            if code:
                subject_code = code
                subject_name = name
                subject_guess = guess
                break
        # If no subject matched, check whether the term has any data at all
        if subject_guesses and subject_code is None:
            subjects = await get_subjects(campus_code, term)
            if not subjects:
                term_name = get_term_name(term)
                answer_text = f"The {term_name} schedule hasn't been published yet. Check back closer to the start of the term for available classes."
                return ChatResponse(
                    answer=self._translate_if_needed(answer_text, lang),
                    route="live_schedule",
                )
        wants_cross_campus = any(term_phrase in lowered for term_phrase in ALL_CAMPUS_TERMS) or "where is" in lowered
        if wants_cross_campus:
            results = await search_all_campuses(term=term, subject=subject_code, course_number=course_number, keyword=subject_guess, primary_campus=campus_code, filters=filters)
            # With filters, zero is a real answer ("none on Sunday"), not a miss to retry.
            if results.get("total_sections", 0) == 0 and subject_code and course_number and not filters:
                return await self._retry_cross_ref_or_rag(message, subject_code, course_number, campus_code, term, lang=lang, cross_campus=True)
            return ChatResponse(answer=self._translate_if_needed(self._format_multi_campus_schedule(results, filters), lang), route="live_schedule_all", tool_payload=results)
        live = await search_live_schedule(campus_code, term=term, subject=subject_code, course_number=course_number, keyword=subject_guess if not subject_code else None, filters=filters)
        if live["total_sections"] == 0 and subject_code and course_number and not filters:
            return await self._retry_cross_ref_or_rag(message, subject_code, course_number, campus_code, term, lang=lang, cross_campus=False)
        answer_text = self._format_live_schedule(live, subject_name, filters)
        # Enrich with graph context for specific course lookups
        if subject_code and course_number and neo4j_service.is_configured():
            enrichment = await self._get_schedule_enrichment(subject_code, course_number, campus_code)
            if enrichment:
                answer_text += enrichment
        return ChatResponse(answer=self._translate_if_needed(answer_text, lang), route="live_schedule", tool_payload=live)

    _DAY_WORDS = (
        ("monday", "M"), ("tuesday", "T"), ("wednesday", "W"), ("thursday", "Th"),
        ("friday", "F"), ("saturday", "S"), ("sunday", "Su"),
    )
    _TIME_OF_DAY_WORDS = (
        ("morning", r"\bmornings?\b"),
        ("afternoon", r"\bafternoons?\b"),
        ("evening", r"\bevenings?\b|\bnights?\b"),
    )

    def _extract_schedule_filters(self, text: str) -> ScheduleFilters:
        """Day, time-of-day and online/in-person constraints named in the text.

        Buildings need the campus's own room list, so `_detect_building` adds them.
        """
        lowered = text.lower()
        days = {code for word, code in self._DAY_WORDS if re.search(rf"\b{word}s?\b", lowered)}
        if re.search(r"\bweekends?\b", lowered):
            days |= {"S", "Su"}
        # Two different times of day, or both modalities, is no constraint at all.
        times = [name for name, pattern in self._TIME_OF_DAY_WORDS if re.search(pattern, lowered)]
        in_person = re.search(r"\bin[\s-]person\b|\bon[\s-]campus\b", lowered) is not None
        online = re.search(r"\bonline\b", lowered) is not None
        return ScheduleFilters(
            days=frozenset(days),
            time_of_day=times[0] if len(times) == 1 else None,
            modality=("in_person" if in_person else "online") if in_person != online else None,
        )

    # How a building is named:
    #   - any case before the word "building" ("the chem building");
    #   - in capitals, 3+ characters or with a digit ("AMP", "CMS", "B3");
    #   - a 1-2 letter code in capitals right after "in"/"at" ("in FH", "at CC").
    # Lower-case words are not buildings on their own: EL, FIELD, MATH and ROOM
    # are all building codes somewhere in the district. Measured on Fall 2026,
    # without the third rule LACC's main halls (FH, DH, JH, CC) never matched.
    _NAMED_BUILDING_RE = re.compile(r"\b([A-Za-z0-9/]+)\s+(?:building|bldg)\b", re.IGNORECASE)
    _CAPS_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9/]*[A-Z0-9]\b")
    _SHORT_CODE_AFTER_IN_RE = re.compile(r"\b(?i:in|at)\s+(?:(?i:the)\s+)?([A-Z]{1,2})\b")
    _NEVER_BUILDINGS = set(CAMPUS_NAMES) | {"LACCD", "LACCC"}
    # Building codes that are usually something else. LA is a building at City
    # and East but far more often "Los Angeles"; only "the LA building" counts.
    _AMBIGUOUS_CODES = {"LA", "AM", "PM"}

    async def _detect_building(self, texts: list[str], campus_code: str, term: str) -> str | None:
        """The campus building these texts name, if any.

        Only a code that really appears in this term's room list counts. A code
        that is also a subject (ART is a subject everywhere and a building at
        Valley and Pierce) is read as the subject unless the student says
        "building" — "ART classes" means Art, "the ART building" means the place.
        """
        named: list[str] = []
        loose: list[str] = []
        for text in texts:
            named += [token.upper() for token in self._NAMED_BUILDING_RE.findall(text)]
            loose += [
                token for token in self._CAPS_TOKEN_RE.findall(text)
                if len(token) >= 3 or any(ch.isdigit() for ch in token)
            ]
            loose += self._SHORT_CODE_AFTER_IN_RE.findall(text)
        named = [c for c in dict.fromkeys(named) if c not in self._NEVER_BUILDINGS]
        loose = [
            c for c in dict.fromkeys(loose)
            if c not in self._NEVER_BUILDINGS and c not in self._AMBIGUOUS_CODES and c not in named
        ]
        if not named and not loose:
            return None
        try:
            if loose:
                subjects = await get_subjects(campus_code, term)
                subject_codes = {str(s.get("code", "")).upper() for s in subjects}
                loose = [c for c in loose if c not in subject_codes]
            if not named and not loose:
                return None
            buildings = building_codes(await get_class_listing(campus_code, term))
        except Exception:
            logger.warning("Building detection failed; searching without a building", exc_info=True)
            return None
        return next((c for c in [*named, *loose] if c in buildings), None)

    @observe(name="chat.ztc_query")
    async def _handle_ztc_query(self, message: str, campus_code: str, lang: str = "en") -> ChatResponse:
        lowered = message.lower()
        term = self._extract_term_code(lowered)
        subject_guesses = self._extract_subject_guesses(message)
        subject_code = None
        subject_name = None
        for guess in subject_guesses:
            code, name = await find_subject_code(guess, campus_code, term)
            if code:
                subject_code = code
                subject_name = name
                break
        wants_cross_campus = any(term_phrase in lowered for term_phrase in ALL_CAMPUS_TERMS)
        if wants_cross_campus:
            import asyncio
            from app.services.live_schedule import ALL_CAMPUSES, CAMPUS_NAMES

            async def fetch_ztc(code: str) -> tuple[str, dict]:
                return code, await search_ztc_sections(code, term, subject=subject_code)

            ordered = [campus_code] + [c for c in ALL_CAMPUSES if c != campus_code]
            results = await asyncio.gather(*[fetch_ztc(c) for c in ordered], return_exceptions=True)
            campuses: dict[str, Any] = {}
            total = 0
            for r in results:
                if isinstance(r, tuple):
                    c_code, payload = r
                    if payload["sections"]:
                        campuses[c_code] = {
                            "campus_name": CAMPUS_NAMES.get(c_code, c_code),
                            "sections": payload["sections"],
                            "section_count": len(payload["sections"]),
                        }
                        total += len(payload["sections"])
            multi_payload = {"term_name": get_term_name(term), "campuses": campuses, "total_sections": total}
            answer_text = self._format_ztc_multi_campus(multi_payload, subject_name)
            return ChatResponse(answer=self._translate_if_needed(answer_text, lang), route="ztc_all", tool_payload=multi_payload)

        ztc_result = await search_ztc_sections(campus_code, term, subject=subject_code)
        answer_text = self._format_ztc_results(ztc_result, subject_name)
        return ChatResponse(answer=self._translate_if_needed(answer_text, lang), route="ztc", tool_payload=ztc_result)

    def _format_ztc_results(self, payload: dict[str, Any], subject_name: str | None) -> str:
        sections = payload["sections"]
        campus_name = payload["campus_name"]
        term_name = payload["term_name"]
        campus_code = payload["campus"]
        if not sections:
            qualifier = f" {subject_name}" if subject_name else ""
            return f"I didn't find any Zero Textbook Cost{qualifier} sections at {campus_name} for {term_name}."
        ztc_count = sum(1 for s in sections if "OER" in s.get("class_type", []))
        lct_count = sum(1 for s in sections if "LCT" in s.get("class_type", []))
        subject_label = f" **{subject_name}**" if subject_name else ""
        parts = []
        if ztc_count:
            parts.append(f"**{ztc_count}** ZTC (Zero Textbook Cost)")
        if lct_count:
            parts.append(f"**{lct_count}** Low Cost")
        count_str = " and ".join(parts)
        lines = [f"Found {count_str}{subject_label} sections at {campus_name} for {term_name}.", ""]
        # Group by subject
        by_subject: dict[str, list[dict]] = {}
        for s in sections:
            subj = s["course_code"].split()[0] if " " in s["course_code"] else "Other"
            by_subject.setdefault(subj, []).append(s)
        for subj, subj_sections in sorted(by_subject.items()):
            lines.append(f"**{subj}** ({len(subj_sections)} sections):")
            for s in subj_sections:
                lines.append(self._fmt_section(s, strip_campus=True))
            lines.append("")
        # Append OpenStax free textbook suggestions
        subject_codes = list(by_subject.keys())
        openstax = get_openstax_suggestions_multi(subject_codes)
        if openstax:
            book_lines: list[str] = []
            for books in openstax.values():
                for b in books:
                    if len(book_lines) >= 4:
                        break
                    book_lines.append(f"- [{b['title']}]({b['url']})")
                if len(book_lines) >= 4:
                    break
            if book_lines:
                lines.append("Free peer-reviewed OpenStax textbooks for this subject:")
                lines.extend(book_lines)
                lines.append("")
        return "\n".join(lines)

    def _format_ztc_multi_campus(self, payload: dict[str, Any], subject_name: str | None) -> str:
        campuses = payload["campuses"]
        term_name = payload["term_name"]
        if not campuses:
            qualifier = f" {subject_name}" if subject_name else ""
            return f"I didn't find any Zero Textbook Cost{qualifier} sections across the district for {term_name}."
        total = payload["total_sections"]
        subject_label = f" {subject_name}" if subject_name else ""
        lines = [f"Found **{total}** Zero Textbook Cost / Low Cost{subject_label} sections across the district for {term_name}.", ""]
        for campus_code, campus_data in campuses.items():
            n = campus_data["section_count"]
            cname = campus_data["campus_name"]
            sec_word = "section" if n == 1 else "sections"
            lines.append(f"**{cname}** — {n} {sec_word}:")
            for s in campus_data["sections"]:
                lines.append(self._fmt_section(s, strip_campus=True))
            lines.append("")
        # Append OpenStax free textbook suggestions across all campuses
        all_subjects: set[str] = set()
        for campus_data in campuses.values():
            for s in campus_data["sections"]:
                subj = s["course_code"].split()[0] if " " in s["course_code"] else None
                if subj:
                    all_subjects.add(subj)
        openstax = get_openstax_suggestions_multi(list(all_subjects))
        if openstax:
            book_lines: list[str] = []
            for books in openstax.values():
                for b in books:
                    if len(book_lines) >= 5:
                        break
                    book_lines.append(f"- [{b['title']}]({b['url']})")
                if len(book_lines) >= 5:
                    break
            if book_lines:
                lines.append("Free peer-reviewed OpenStax textbooks for these subjects:")
                lines.extend(book_lines)
                lines.append("")
        return "\n".join(lines)

    async def _retry_cross_ref_or_rag(
        self, message: str, subject_code: str, course_number: str, campus_code: str, term: str, *, lang: str = "en", cross_campus: bool
    ) -> ChatResponse:
        """When schedule returns 0 sections, check cross-references then fall back to RAG."""
        # Try Neo4j graph first for dynamic cross-references
        refs = []
        if neo4j_service.is_configured():
            all_refs = await neo4j_service.find_course_cross_refs(subject_code, course_number, campus_code)
            # Sort: prefer shorter course codes (primary renumbering over variants like C1000E)
            refs = sorted(all_refs, key=lambda r: len(r.get("new_number", "")))
        # Fall back to hardcoded dict if Neo4j unavailable or returned nothing
        if not refs:
            static_ref = COURSE_CROSS_REFS.get((subject_code, course_number))
            if static_ref:
                refs = [{"new_subject": static_ref[0], "new_number": static_ref[1], "title": "", "old_code": f"{subject_code} {course_number}", "new_code": f"{static_ref[0]} {static_ref[1]}"}]
        # Try each cross-reference
        for ref in refs:
            new_subj = ref["new_subject"]
            new_num = ref["new_number"]
            new_code = ref["new_code"]
            rename_note = f"{subject_code} {course_number} was renumbered to {new_code}."
            new_subj_code, new_subj_name = await find_subject_code(new_subj, campus_code, term)
            if not new_subj_code:
                continue
            if cross_campus:
                results = await search_all_campuses(term=term, subject=new_subj_code, course_number=new_num, primary_campus=campus_code)
                if results.get("total_sections", 0) > 0:
                    header = f"**Note:** {rename_note}\n\n"
                    answer_text = header + self._format_multi_campus_schedule(results)
                    return ChatResponse(
                        answer=self._translate_if_needed(answer_text, lang),
                        route="live_schedule_all+cross_ref",
                        tool_payload=results,
                    )
            else:
                live = await search_live_schedule(campus_code, term=term, subject=new_subj_code, course_number=new_num)
                if live["total_sections"] > 0:
                    header = f"**Note:** {rename_note}\n\n"
                    answer_text = header + self._format_live_schedule(live, new_subj_name)
                    return ChatResponse(
                        answer=self._translate_if_needed(answer_text, lang),
                        route="live_schedule+cross_ref",
                        tool_payload=live,
                    )
        # Final fallback: try RAG for context about the course
        logger.info("Schedule empty for %s %s, falling back to RAG", subject_code, course_number)
        return await self._handle_rag_query(message, campus_code=campus_code, lang=lang)

    def _extract_program_phrase(self, message: str) -> str:
        lowered = re.sub(r"[?!.,;:\"'()]+", " ", message.lower())
        stop = {
            "show", "me", "tell", "about", "what", "is", "are", "the", "a", "an",
            "for", "at", "in", "of", "to", "do", "does", "can", "i", "you", "how",
            "requirements", "requirement", "career", "careers", "program", "programs",
            "degree", "degrees", "certificate", "certificates", "major", "pathway",
            "mission", "valley", "lamc", "lavc", "lapc", "lahc", "lattc", "lacc",
            "elac", "lasc", "wlac", "college", "colleges", "campus", "campuses", "please",
            "offered", "offer", "offers", "have", "has", "and", "or",
            "spring", "summer", "fall", "winter", "this", "that", "these",
            "those", "with", "pursue", "where", "when", "which", "why", "there",
            "available", "any", "some", "all", "laccd", "district", "2025", "2026",
            "course", "courses", "sequence", "plan", "order", "chain",
            "prerequisite", "prereq", "semester", "semesters",
            "finish", "complete", "take", "first", "class", "classes", "should",
            "map", "out", "path", "roadmap", "road", "my",
            # Award words. The Neo4j fallback feeds this phrase into a Lucene
            # fulltext index (OR semantics), so leaving "associate" in matched
            # every program with "Associate" in its TITLE — a student asking for
            # a psychology associate degree was offered Cyber Security
            # Associate. The award is matched separately, so the search phrase
            # should carry the subject only.
            # Award ABBREVIATIONS (aa, as, bs, ...) are deliberately NOT stripped:
            # the Program Mapper path matches query terms against title + award
            # tokens, so they help it pick the right award.
            "associate", "associates", "bachelor", "bachelors", "transfer",
            "arts", "science", "sciences",
            # Intent filler that survives the list above.
            "want", "wants", "wanted", "need", "needs", "get", "getting",
            "looking", "interested", "like", "would", "am", "im",
        }
        words = re.findall(r"[a-z0-9]+", lowered)
        kept = [w for w in words if w not in stop and len(w) > 1]
        return " ".join(kept) or message

    # LACCD term codes are "22" + last digit of the year + a season digit,
    # so Fall 2026 is 2268 and Spring 2027 is 2274. The rule itself lives in
    # shared/terms.py owns the rule so chat and voice cannot drift.
    _SEASON_DIGITS = terms.SEASON_DIGITS
    _SEASON_LAST_MONTH = terms.SEASON_LAST_MONTH
    _SEASON_YEAR_RE = terms.SEASON_YEAR_RE

    @classmethod
    def _term_code(cls, season: str, year: int) -> str:
        return terms.term_code(season, year)

    @classmethod
    def _current_season(cls, month: int) -> str:
        return terms.current_season(month)

    def _extract_term_code(self, message: str, today: date | None = None) -> str:
        """The term the message is asking about, defaulting to the one running now.

        Derive the term from the calendar so an unqualified request follows the
        current term and future years do not require lookup-table updates.

        ``today`` is injectable for tests.
        """
        today = today or date.today()
        lowered = message.lower()

        # An explicit season + year wins outright.
        match = self._SEASON_YEAR_RE.search(lowered)
        if match:
            return self._term_code(match.group(1), int(match.group(2)))

        # A bare season word means its next occurrence — never a term that has
        # already finished.
        for season, last_month in self._SEASON_LAST_MONTH.items():
            if season in lowered:
                year = today.year if today.month <= last_month else today.year + 1
                return self._term_code(season, year)

        return self._term_code(self._current_season(today.month), today.year)

    def _extract_course_number(self, message: str) -> str | None:
        match = re.search(r"\b(C\d{3,4}[A-Z]{0,2}|\d{2,3}[A-Z]?)\b", message)
        if not match:
            return None
        num = match.group(1)
        # Strip leading zeros for numeric course numbers (001 → 1)
        if num.isdigit():
            num = str(int(num))
        return num

    def _extract_subject_guesses(self, message: str) -> list[str]:
        """Return ranked list of candidate subject names from the message."""
        if match := re.search(r"\b([A-Z]{2,}\s?[A-Z]{0,3})\s+C?\d{2,4}[A-Z]{0,2}\b", message):
            return [match.group(1).strip()]
        stopwords = {
            "where", "what", "when", "which", "spring", "summer", "fall", "winter",
            "district", "across", "offered", "available", "mission", "college", "campus",
            "class", "classes", "section", "sections", "schedule", "instructor",
            "show", "does", "there", "this", "that", "these", "those", "have", "help",
            "teach", "tell", "about", "give", "find", "looking", "need", "want", "like",
            "know", "also", "evening", "morning", "afternoon", "online", "with", "from",
            "some", "many", "they", "than", "2025", "2026",
            "become", "take", "study", "start", "year", "high", "school", "student",
            "saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday",
            "saturdays", "sundays", "mondays", "tuesdays", "wednesdays", "thursdays", "fridays",
            "mornings", "afternoons", "evenings",
            "meet", "meets", "time", "offer", "offers", "lamc", "lavc", "lapc",
            "other", "campuses", "besides",
            "schedules", "look", "weekend", "weekends", "night", "nights", "person",
            "building", "buildings", "room",
            "free", "book", "books", "textbook", "textbooks", "cost", "zero",
            "afford", "save", "options", "open", "educational",
        }
        words = [word.lower() for word in re.findall(r"[A-Za-z]+", message) if len(word) > 3]
        candidates = [word for word in words if word not in stopwords]
        return candidates

    def _format_program_bundle(self, bundle: dict[str, Any]) -> str:
        program = bundle["program"]
        requirements = bundle.get("requirements") or {}
        careers = bundle.get("careers") or []
        award = program.get("awardShortTitle") or ""
        title_line = f"{program.get('title')} ({bundle['campus_name']})"
        if award:
            title_line = f"{program.get('title')} — {award} ({bundle['campus_name']})"
        lines = [
            title_line,
            program.get("description", "")[:550],
            "",
        ]
        elements = requirements.get("pathwayElements", [])[:12]
        if elements:
            lines.append("Requirements:")
            for element in elements:
                opp = element.get("recommendedOpportunity", {})
                lines.append(
                    f"- {element.get('name')}: {element.get('shortDescription', '')} ({opp.get('minUnits', '?')} units)"
                )
        if careers:
            lines.append("")
            lines.append("Career examples:")
            for career in careers[:4]:
                lines.append(f"- {career.get('title')}: avg ${career.get('averageSalaryK', 'n/a')}K")
        if catalog := program.get("catalogUrl"):
            lines.append("")
            lines.append(f"Catalog: {catalog}")
        return "\n".join(lines)

    @staticmethod
    def _fmt_section(section: dict[str, Any], strip_campus: bool = False) -> str:
        """Format one section into a readable bullet line."""
        code = section["course_code"]
        title = section.get("course_title", "")
        days = section.get("days", "TBA")
        time = section.get("time", "TBA")
        instructor = section.get("instructor", "TBA")
        location = section.get("location", "TBA")
        raw_section = str(section.get("section", "") or "").strip()
        class_number = str(section.get("class_number", "") or "").strip()
        if not class_number and raw_section:
            class_number = (raw_section.split() or [""])[0]
        dates = str(section.get("dates", "") or "").strip()
        # Strip campus prefix from location (e.g. "Mission-CMS 128" → "CMS 128")
        if strip_campus and "-" in location and location not in ("TBA", "Online", ""):
            location = location.split("-", 1)[1].strip()
        # Build detail fragments, skip TBA values
        parts: list[str] = []
        if class_number and class_number.upper() != "N/A":
            parts.append(f"Class #{class_number}")
        if dates:
            parts.append(f"Dates: {dates}")
        if days not in ("TBA", "") and time not in ("TBA", ""):
            parts.append(f"{days} {time}")
        elif days not in ("TBA", ""):
            parts.append(days)
        elif time not in ("TBA", ""):
            parts.append(time)
        else:
            parts.append("Online / TBA")
        if instructor not in ("TBA", ""):
            parts.append(instructor)
        if location not in ("TBA", "Online", ""):
            parts.append(location)
        detail = " — ".join(parts)
        # ZTC / Low Cost badges
        class_types = section.get("class_type", [])
        badges = []
        if "OER" in class_types:
            badges.append("[ZTC]")
        if "LCT" in class_types:
            badges.append("[Low Cost]")
        badge_str = " ".join(badges)
        label = f"**{code}** {title}" if title else f"**{code}**"
        if badge_str:
            return f"- {badge_str} {label} — {detail}"
        return f"- {label} — {detail}"

    @staticmethod
    def _class_search_url(campus: str) -> str:
        return f"https://mycollege-guest.laccd.edu/psc/classsearchguest/EMPLOYEE/HRMS/c/COMMUNITY_ACCESS.CLASS_SEARCH.GBL?Campus={campus}"

    async def _get_schedule_enrichment(self, subject_code: str, course_number: str, campus_code: str) -> str | None:
        """Get graph enrichment text to append to schedule results."""
        try:
            enrichment = await neo4j_service.get_course_enrichment(subject_code, course_number, campus_code)
            if not enrichment:
                # Try resolving subject code (STAT vs STATISTICS, etc.)
                resolved = await neo4j_service.resolve_course_subject(subject_code, course_number, campus_code)
                if resolved != subject_code:
                    enrichment = await neo4j_service.get_course_enrichment(resolved, course_number, campus_code)
            if not enrichment:
                return None
            parts: list[str] = []
            uc = enrichment.get("uc_transferable")
            csu = enrichment.get("csu_transferable")
            if uc is not None or csu is not None:
                transfer_parts = []
                if uc:
                    transfer_parts.append("UC")
                if csu:
                    transfer_parts.append("CSU")
                if transfer_parts:
                    parts.append(f"Transferable to: **{', '.join(transfer_parts)}**")
                else:
                    parts.append("Not UC/CSU transferable")
            ge_areas = enrichment.get("ge_areas", [])
            if ge_areas:
                ge_strs = [f"{g['ge_system']} {g['ge_code']}" for g in ge_areas[:4]]
                parts.append(f"GE areas: {', '.join(ge_strs)}")
            if not parts:
                return None
            return "\n\n---\n*Verified academic details:* " + " | ".join(parts)
        except Exception:
            logger.debug("Schedule enrichment failed", exc_info=True)
            return None

    def _format_live_schedule(
        self, payload: dict[str, Any], subject_name: str | None, filters: ScheduleFilters | None = None,
    ) -> str:
        sections = payload["sections"]
        campus_name = payload["campus_name"]
        campus_code = payload["campus"]
        term_name = payload["term_name"]
        # Say what was searched, so "none" reads as "none on Sunday in AMP".
        described = filters.describe() if filters else ""
        if not sections:
            if described:
                return f"I didn't find any sections{described} at {campus_name} for {term_name}."
            return f"I didn't find any matching live sections at {campus_name} for {term_name}."
        total = payload["total_sections"]
        subject_label = f" **{subject_name}**" if subject_name else ""
        count_word = "section" if total == 1 else "sections"
        lines = [f"Found **{total}**{subject_label} {count_word}{described} at {campus_name} for {term_name}.", ""]
        for section in sections:
            lines.append(self._fmt_section(section, strip_campus=True))
        return "\n".join(lines)

    def _format_multi_campus_schedule(
        self, payload: dict[str, Any], filters: ScheduleFilters | None = None,
    ) -> str:
        campuses = payload["campuses"]
        term_name = payload["term_name"]
        # search_all_campuses drops the building (codes are campus-local).
        described = filters.without_building().describe() if filters else ""
        if not campuses:
            return f"I didn't find matching live sections{described} across the district for {term_name}."
        total = payload["total_sections"]
        count_word = "section" if total == 1 else "sections"
        lines = [f"Found **{total}** {count_word}{described} across the district for {term_name}.", ""]
        for campus_code, campus_data in campuses.items():
            n = campus_data["section_count"]
            cname = campus_data["campus_name"]
            sec_word = "section" if n == 1 else "sections"
            lines.append(f"**{cname}** - {n} {sec_word}:")
            for section in campus_data["sections"]:
                lines.append(self._fmt_section(section, strip_campus=True))
            lines.append("")
        return "\n".join(lines)


_orchestrator: ChatOrchestrator | None = None


def get_orchestrator() -> ChatOrchestrator:
    """Process-wide singleton so the app and the guardrails layer share one
    ChatOrchestrator (and its service clients) instead of double-initializing."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ChatOrchestrator()
    return _orchestrator
