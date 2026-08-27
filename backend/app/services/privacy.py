"""PII redaction helpers for privacy-preserving observability.

Student chat text can contain volunteered PII (a student typing "my ID is
881234567" or "email me at jane@student.laccd.edu"). Two sinks receive that
text today: Langfuse traces (``langfuse_mask``) and application logs
(``redact_pii``). This module masks emails, phone numbers, and long digit runs
that look like student IDs *before* the text leaves the process, while keeping
the ops-useful signal (route names, campus, course codes, years) intact.

The redaction is deliberately conservative: it only touches things that match a
PII shape, so ordinary text — course codes like ``MATH 261``, years like
``2024``, program names — passes through unchanged.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["redact_pii", "langfuse_mask"]

# Email addresses: local@domain.tld.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# US-style phone numbers, with or without country code and common separators:
#   (555) 123-4567 / 555-123-4567 / 555.123.4567 / +1 555 123 4567 / 5551234567
# Runs the phone pass before the generic ID pass so separated numbers (which the
# bare digit-run rule would miss) are caught as [PHONE].
_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+?\d{1,2}[\s.-]?)?"          # optional country code
    r"(?:\(\d{3}\)|\d{3})"             # area code, optionally parenthesized
    r"[\s.-]?\d{3}[\s.-]?\d{4}"        # prefix + line number
    r"(?!\d)"
)

# Long digit runs that look like a student ID (LACCD IDs are 9 digits). Anything
# with 6+ consecutive digits is masked; shorter runs (course numbers, years,
# unit counts) are left alone.
_ID_RE = re.compile(r"\d{6,}")


def redact_pii(text: str) -> str:
    """Mask emails, phone numbers, and student-ID-like digit runs in ``text``.

    Replaces matches with ``[EMAIL]`` / ``[PHONE]`` / ``[ID]`` tokens. Order
    matters: emails first (so an address's digits aren't clipped), then phone
    numbers (multi-group, separated), then bare long digit runs.

    Non-string input is coerced to ``str`` so this is safe to call on log args.
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    text = _ID_RE.sub("[ID]", text)
    return text


def _redact_obj(obj: Any) -> Any:
    """Recursively apply :func:`redact_pii` to strings inside a payload.

    Dict keys are field names (``message``, ``history``, ``question`` …), not
    user content, so they are preserved; only values are redacted. Numbers,
    bools, and ``None`` pass through unchanged.
    """
    if isinstance(obj, str):
        return redact_pii(obj)
    if isinstance(obj, dict):
        return {key: _redact_obj(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(item) for item in obj]
    return obj


def langfuse_mask(*, data: Any, **_kwargs: Any) -> Any:
    """Langfuse ``MaskFunction`` — mask PII in trace input/output/metadata.

    Called by the Langfuse SDK for every span's input, output, and metadata
    before export. Matches the ``MaskFunction`` protocol (keyword-only ``data``,
    tolerant of extra kwargs) and returns a JSON-serializable, PII-masked copy.
    """
    return _redact_obj(data)
