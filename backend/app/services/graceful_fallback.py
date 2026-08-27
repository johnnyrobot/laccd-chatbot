"""Graceful clarifying-question response for cases where the primary
Gemini path fails (transient API errors, missing config, etc.) and the
secondary OpenRouter path is unavailable or also fails.

Goal: never return a bare "internal server error" — always offer the
student a way forward by asking a clarifying question or surfacing the
chatbot's capabilities. This keeps the conversation moving when the
upstream model has a hiccup.
"""
from __future__ import annotations

import re

from app.models import ChatResponse
from shared.campus_config import get_campus

_COURSE_CODE_RE = re.compile(r"\b([A-Z]{2,}(?:\s+[A-Z]{0,3})?)\s+(C?\d{2,4}[A-Z]{0,2})\b")
_PROGRAM_TERMS = ("degree", "certificate", "major", "associate", "program", "pathway")
_TRANSFER_TERMS = ("transfer", "uc", "csu", "articulation")
_SCHEDULE_TERMS = ("schedule", "section", "class", "offered", "available", "instructor")


def _detect_intent(message: str) -> str:
    """Best-effort intent classifier — used only to pick which clarifying
    question to surface. Falls back to 'generic' when nothing matches.

    Schedule and program intents are checked before course-code so terms
    like "Spring 2026" or "Computer Science degree" don't get false-
    positive course-code matches from the broad SUBJ NUMBER regex.
    """
    lowered = message.lower()
    if any(t in lowered for t in _SCHEDULE_TERMS):
        return "schedule"
    if any(t in lowered for t in _PROGRAM_TERMS):
        return "program"
    if _COURSE_CODE_RE.search(message.upper()):
        return "course_code"
    if any(t in lowered for t in _TRANSFER_TERMS):
        return "transfer"
    return "generic"


def _clarifying_lines(intent: str, campus_name: str) -> list[str]:
    """Build the clarifying message body. One short opener + 2–4 specific
    next-step questions tailored to the apparent intent."""
    if intent == "course_code":
        return [
            f"I want to make sure I look up the right thing for you at {campus_name}.",
            "Are you asking about:",
            "- **Transfer status** (UC / CSU)?",
            "- **GE areas** the course satisfies?",
            "- **Prerequisites** or what to take before it?",
            "- **Schedule** — when sections are offered?",
            "Reply with one of those, and I'll pull it up.",
        ]
    if intent == "transfer":
        return [
            "I can check transfer/articulation a few different ways. Which would help most?",
            "- A specific course's UC/CSU transferability (e.g., \"Is MATH 261 UC transferable?\")",
            "- An ASSIST.org articulation agreement to a specific UC/CSU campus",
            "- Whether a degree is an Associate for Transfer (AA-T / AS-T)",
        ]
    if intent == "program":
        return [
            f"For programs at {campus_name}, I can help with:",
            "- The full **course sequence** semester by semester",
            "- A **list of programs** matching a keyword (e.g., \"Computer Science programs at WLAC\")",
            "- **Career pathways** and salary info tied to a program",
            "Could you tell me which one, or include the program's full name (and whether you want the AA, AS, AA-T, AS-T, or certificate)?",
        ]
    if intent == "schedule":
        return [
            f"To pull live sections for {campus_name}, I need a couple of details:",
            "- A **subject** (e.g., MATH, BIOLOGY, ENGLISH)",
            "- Optionally a **course number** (e.g., MATH 261)",
            "- Which **term** — Spring 2026, Fall 2026, etc.",
            "What's the subject you're trying to find?",
        ]
    return [
        f"Tell me a bit more about what you're looking for at {campus_name} — I can help with:",
        "- **Course info** — transfer status, GE, prereqs (e.g., \"Is CHEM 211 UC transferable?\")",
        "- **Live class schedule** for any term (e.g., \"What MATH classes are offered Spring 2026?\")",
        "- **Program sequencing** — full semester-by-semester plans",
        "- **ASSIST articulation** to UC / CSU",
        "- **Instructors** — who teaches a course",
    ]


def graceful_clarifying_response(message: str, campus_code: str) -> ChatResponse:
    """Return a ChatResponse that asks the student a clarifying question.

    Used when both the Gemini path and the OpenRouter path are unavailable
    (transient API failures, daily quota exhausted, etc.) so the user
    never sees a raw error. The route is ``"clarifying_fallback"`` so the
    frontend can style it differently if desired.
    """
    try:
        campus_name = get_campus(campus_code).name
    except Exception:
        campus_name = "your campus"
    intent = _detect_intent(message or "")
    body = "\n".join(_clarifying_lines(intent, campus_name))
    return ChatResponse(
        answer=body,
        route="clarifying_fallback",
        fallback_mode=True,
    )
