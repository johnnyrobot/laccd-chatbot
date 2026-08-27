"""Deterministic clarification state for multi-turn clarify-backs.

When the orchestrator asks a clarifying question it already knows what it is
waiting for. Carrying that forward explicitly -- instead of asking an LLM to
re-derive it from a truncated transcript on the next turn -- is what makes a
terse reply ("UCLA", "Mathematics") a slot fill rather than a guess.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# `pending` is echoed back by the browser, so it is UNTRUSTED input. Everything
# it may contain is allowlisted here.
ALLOWED_INTENTS = frozenset({"assist"})
ALLOWED_NEEDS = frozenset({"receiving_institution", "major"})
# Note: `campus_code` is deliberately NOT a slot. The serving campus is resolved
# from the Host header; a client must not be able to cross-campus itself here.
ALLOWED_SLOTS = frozenset({"course", "campus", "major"})
MAX_SLOT_LEN = 200


@dataclass(frozen=True, slots=True)
class Clarification:
    """A clarify-back the bot is waiting on.

    `needs` is the slot still missing; `slots` is what we already resolved and
    must not re-derive.
    """

    intent: str
    needs: str
    slots: dict[str, str] = field(default_factory=dict)

    def with_slot(self, name: str, value: str) -> "Clarification":
        return Clarification(intent=self.intent, needs=self.needs, slots={**self.slots, name: value})

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "needs": self.needs, "slots": dict(self.slots)}


def parse_pending(raw: Any) -> Clarification | None:
    """Validate client-supplied `pending`.

    Returns None for anything malformed or not allowlisted. Deliberately lenient
    about *rejecting* rather than raising: a stale browser tab holding an old
    pending shape should degrade to a normal turn, not a 422.
    """
    if not isinstance(raw, dict):
        return None

    intent = raw.get("intent")
    needs = raw.get("needs")
    # Type-check BEFORE the membership test: `[] in frozenset(...)` raises
    # TypeError on an unhashable value, so `pending: {"intent": []}` would 500
    # the chat endpoint rather than being ignored.
    if not isinstance(intent, str) or not isinstance(needs, str):
        return None
    if intent not in ALLOWED_INTENTS or needs not in ALLOWED_NEEDS:
        return None

    slots_raw = raw.get("slots", {})
    if not isinstance(slots_raw, dict):
        return None

    slots: dict[str, str] = {}
    for key, value in slots_raw.items():
        if key not in ALLOWED_SLOTS:
            return None  # an unknown slot key means a client we don't understand
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_SLOT_LEN:
            return None
        slots[key] = value.strip()

    # We only ever ask for a slot we don't have. A client claiming to be answering
    # "which major?" while already holding a major is not a client we understand —
    # and honoring it would let them skip the clarify-back entirely.
    if needs == "major" and slots.get("major"):
        return None
    if needs == "receiving_institution" and slots.get("campus"):
        return None

    return Clarification(intent=intent, needs=needs, slots=slots)
