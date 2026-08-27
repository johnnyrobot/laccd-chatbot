"""District-wide regulatory facts that per-campus RAG must not answer on its own.

Some rules are set in Title 5 and are identical at all nine colleges. The Gemini
File Search corpus, however, is built per campus from campus websites we do not
control, so nine independently-maintained sites end up answering one statewide
question nine different ways.

Campus sources have returned incompatible answers about whether credit for prior
learning counts toward the 12 units of residence required for an associate
degree, while the statewide rule also has a defined effective-date change.
Both wrong answers were ungrounded (zero citations). The district-wide rule is
therefore injected from the reviewed regulatory text rather than inferred from
independently maintained campus pages.

The fix is to state the authoritative rule *in the prompt*, so retrieval cannot
decide it. A fact fires only when the question matches every one of its term
groups, which keeps false positives low: the CPL fact needs a prior-learning term
AND a residency term, so "am I a California resident for tuition purposes?" -- a
completely different sense of "residency" -- does not trigger it.

Deliberately NOT a new branch on the ``ChatOrchestrator`` routing ladder. That
ladder is ten keyword branches where intent priority is line order, and it has
already produced two prod misroutes. This is additive prompt context, injected
the same way graph context already is, so it changes no routing decision.

To add a fact: append a ``DistrictFact`` to ``DISTRICT_FACTS``. Prefer a rule that
(a) is genuinely district-wide, and (b) the campus corpora get wrong or disagree
on -- not everything in Title 5 belongs here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

__all__ = ["DistrictFact", "DISTRICT_FACTS", "district_facts_for"]


def _has_any(text: str, terms: Sequence[str]) -> bool:
    """True if any term appears in text on word boundaries.

    Word boundaries matter: a bare substring test makes "cpl" match "discipline"
    and "duplicate", which would fire the fact on unrelated questions.
    """
    for term in terms:
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            return True
    return False


@dataclass(frozen=True)
class DistrictFact:
    """One statewide rule, with the terms that summon it.

    ``term_groups`` is ALL-of-any-of: every group must match for the fact to
    fire. Requiring two independent groups is what keeps precision high.
    """

    name: str
    term_groups: tuple[tuple[str, ...], ...]
    render: Callable[[date], str]

    def matches(self, question: str) -> bool:
        lowered = question.lower()
        return all(_has_any(lowered, group) for group in self.term_groups)


# --- Credit for prior learning vs. the associate-degree residency requirement ---

# Title 5 section 55050(h) changes to section 55050(d) on the effective date.
CPL_RESIDENCY_CHANGE_DATE = date(2026, 9, 9)


def _render_cpl_residency(today: date) -> str:
    if today < CPL_RESIDENCY_CHANGE_DATE:
        rule = (
            "Under Title 5 section 55050(h), units awarded through credit for prior "
            "learning **shall NOT be counted** toward the 12 semester hours of credit "
            "in residence required for an associate degree. This is the rule in force "
            "today."
        )
        change = (
            f"This REVERSES on {CPL_RESIDENCY_CHANGE_DATE:%B %-d, %Y}. From that date, "
            "amended section 55050(d) provides that such units **MAY be counted** "
            "toward the 12 units of residence."
        )
    else:
        rule = (
            "Under Title 5 section 55050(d), units awarded through credit for prior "
            "learning **MAY be counted** toward the 12 semester hours of credit in "
            "residence required for an associate degree. This is the rule in force "
            "today."
        )
        change = (
            f"This CHANGED on {CPL_RESIDENCY_CHANGE_DATE:%B %-d, %Y}. Before that date "
            "the previous section 55050(h) provided that such units could NOT be "
            "counted, so a student whose petition was denied earlier should ask a "
            "counselor whether the current rule now applies to them."
        )
    return (
        "Credit for prior learning and the associate-degree residency requirement:\n"
        f"{rule}\n{change}\n"
        "This rule is set in state regulation and is the SAME at all nine LACCD "
        "colleges. If a campus webpage in your sources says otherwise, it is out of "
        "date -- use the rule above instead.\n"
        "In your answer you MUST state the effective date alongside the rule, and tell "
        "the student to confirm with a counselor which rule applies to their own "
        "petition. Do not predict whether their petition will be granted."
    )


# --- Advanced Placement and other external standardized examinations ---

# Title 5 s55052 and s55052.5. "May" becomes "shall", and awarding credit toward
# Cal-GETC or local GE becomes mandatory on the effective date.
EXAM_CREDIT_CHANGE_DATE = date(2026, 9, 9)


def _render_exam_credit(today: date) -> str:
    if today < EXAM_CREDIT_CHANGE_DATE:
        duty = (
            "Under Title 5 section 55052, a district governing board **may** adopt "
            "policies granting credit for Advanced Placement examinations; section "
            "55052.5 covers International Baccalaureate and College Level Examination "
            "Program examinations. LACCD colleges do award such credit today."
        )
        change = (
            f"On {EXAM_CREDIT_CHANGE_DATE:%B %-d, %Y} this becomes mandatory. Amended "
            "section 55052(a) will require each district to adopt AP credit policies, "
            "and 55052(c) will require a college to **award** credit toward Cal-GETC or "
            "local general education requirements for a satisfactory AP score, with "
            "elective credit where no course or GE area matches. Section 55052.5 is "
            "broadened to cover any external standardized examination, and replaces "
            "IGETC and CSU GE Breadth with Cal-GETC."
        )
    else:
        duty = (
            "Under Title 5 section 55052(a) a district governing board **shall** adopt "
            "AP credit policies, and section 55052(c) provides that a college **shall "
            "award credit** toward Cal-GETC or local general education requirements for "
            "satisfactory completion of an Advanced Placement examination. Where no "
            "course or general education area matches, the college **may** award "
            "elective credit. Section 55052.5 applies the same treatment to "
            "International Baccalaureate, CLEP and other external standardized "
            "examinations."
        )
        change = (
            f"This became mandatory on {EXAM_CREDIT_CHANGE_DATE:%B %-d, %Y}; before that "
            "date awarding the credit was permissive, so a student turned down earlier "
            "should ask again."
        )
    return (
        "Advanced Placement, IB, CLEP and other external exam credit:\n"
        f"{duty}\n{change}\n"
        "What is NOT standardized: the **minimum score** that counts and **which course "
        "or GE area** each exam maps to are set by discipline faculty and the curriculum "
        "committee at each college, and they differ between colleges. The student's "
        "record is annotated to show credit came from an examination.\n"
        "In your answer, say that the college awards AP/IB/CLEP credit toward general "
        "education, give the effective date if the timing matters, and send the student "
        "to Admissions and Records or a counselor for the score thresholds and course "
        "equivalencies at their college. Do NOT state a specific minimum score, and do "
        "NOT promise that a particular course will be waived — that is the college's "
        "determination."
    )


DISTRICT_FACTS: tuple[DistrictFact, ...] = (
    DistrictFact(
        name="cpl_residency",
        term_groups=(
            (
                "credit for prior learning",
                "prior learning",
                "cpl",
                "credit by exam",
                "credit by examination",
                "portfolio credit",
                "military credit",
                "military training",
            ),
            (
                "residence",
                "residency",
                "resident credit",
                "in residence",
                "12 units",
                "12 semester",
            ),
        ),
        render=_render_cpl_residency,
    ),
    DistrictFact(
        name="exam_credit",
        term_groups=(
            # The exam itself. Bare "ap" is deliberately absent — even on word
            # boundaries it is too common a token to trigger on safely.
            (
                "advanced placement",
                "ap exam",
                "ap exams",
                "ap test",
                "ap tests",
                "ap score",
                "ap scores",
                "ap credit",
                "international baccalaureate",
                "ib exam",
                "clep",
            ),
            # What the student wants to know about it.
            (
                "credit",
                "count",
                "counts",
                "satisfy",
                "satisfies",
                "waive",
                "waived",
                "skip",
                "place out",
                "general education",
                "ge",
                "cal-getc",
                "igetc",
                "requirement",
                "requirements",
                "still need",
                "transfer",
            ),
        ),
        render=_render_exam_credit,
    ),
)


def district_facts_for(question: str, today: date | None = None) -> str:
    """Authoritative district facts relevant to ``question``, or "" if none.

    The returned block is meant to be concatenated into the File Search system
    prompt, alongside graph context. Empty string means inject nothing, so the
    normal RAG path is untouched for the overwhelming majority of turns.
    """
    if not question:
        return ""
    current = today or date.today()
    blocks = [f.render(current) for f in DISTRICT_FACTS if f.matches(question)]
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    return (
        "\n\nAUTHORITATIVE DISTRICT-WIDE FACTS -- these come from state regulation "
        "and OVERRIDE any retrieved campus webpage that conflicts with them:\n\n"
        f"{body}"
    )
