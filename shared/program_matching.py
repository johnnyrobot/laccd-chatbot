"""Pure helpers for selecting an explicitly named academic program.

Program Mapper search intentionally returns fuzzy alternatives.  Once a student
names both a program title and award, however, keeping the fuzzy list creates a
clarification loop.  This module is shared by text chat and the voice agent so
both surfaces make the same deterministic selection.
"""

from __future__ import annotations

import re
from typing import Any


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(value: str | None) -> str:
    return " ".join(_NON_ALNUM_RE.sub(" ", value or "").split())


def _award_aliases(award: str) -> set[str]:
    normalized = _normalize(award)
    aliases = {normalized}
    if "associate of science" in normalized:
        aliases.update({"associate science", "as degree", "a s degree"})
    if "associate of arts" in normalized:
        aliases.update({"associate arts", "aa degree", "a a degree"})
    if "associate in science for transfer" in normalized:
        aliases.update({"as t", "ast", "associate science transfer"})
    if "associate in arts for transfer" in normalized:
        aliases.update({"aa t", "aat", "associate arts transfer"})
    if "certificate of achievement" in normalized:
        aliases.update({"certificate achievement", "certificate"})
    if "certificate of competency" in normalized:
        aliases.update({"certificate competency", "certificate"})
    return {alias for alias in aliases if alias}


def select_explicit_program(
    user_text: str,
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one match when the text explicitly names its title and award.

    The match may appear anywhere in a longer sentence and may be wrapped in
    parentheses.  If the text is not explicit enough, the original fuzzy result
    list is returned unchanged so callers can still ask a clarification.
    """

    if len(matches) <= 1:
        return matches

    normalized_text = _normalize(user_text)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for match in matches:
        title = _normalize(match.get("title") or match.get("name"))
        award = _normalize(
            match.get("awardShortTitle")
            or match.get("award_type")
            or match.get("awardType")
        )
        if not title or title not in normalized_text:
            continue
        if award and not any(alias in normalized_text for alias in _award_aliases(award)):
            continue
        candidates.append((len(title) + len(award), match))

    if not candidates:
        return matches

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score = candidates[0][0]
    best = [match for score, match in candidates if score == best_score]
    return best[:1] if len(best) == 1 else matches
