"""Privacy-redaction tests.

Covers:
  * ``redact_pii`` masks emails / phone numbers / student-ID-like digit runs and
    leaves ordinary text (course codes, years, program names) untouched.
  * ``langfuse_mask`` recurses into dict/list trace payloads and applies the same
    redaction — the callable that is wired into the Langfuse client so raw
    student text never leaves the process.
  * The Langfuse ``mask`` hook is honored at the span level: a span emitted after
    a masked client is initialized has its input/output PII redacted.

No real network — Langfuse export is stubbed by asserting on the span object the
SDK builds, not on a live trace.
"""

from __future__ import annotations

import pytest

from app.services.privacy import langfuse_mask, redact_pii


# ── redact_pii ───────────────────────────────────────────────────────────────


def test_redacts_email():
    assert redact_pii("email me at jane.doe@student.laccd.edu please") == (
        "email me at [EMAIL] please"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "call (555) 123-4567",
        "call 555-123-4567",
        "call 555.123.4567",
        "call +1 555 123 4567",
        "call 5551234567",
    ],
)
def test_redacts_phone_numbers(raw):
    out = redact_pii(raw)
    assert "[PHONE]" in out
    assert "4567" not in out


def test_redacts_student_id_digit_run():
    assert redact_pii("my student id is 881234567") == "my student id is [ID]"


def test_redacts_multiple_pii_in_one_message():
    out = redact_pii("I'm 881234567, reach me at a@b.edu or 555-123-4567")
    assert "[ID]" in out
    assert "[EMAIL]" in out
    assert "[PHONE]" in out
    assert "881234567" not in out
    assert "a@b.edu" not in out


# ── conservative: ordinary academic text is NOT over-redacted ────────────────


@pytest.mark.parametrize(
    "clean",
    [
        "What are the prereqs for MATH 261?",
        "Is CHEM 101 offered in fall 2024?",
        "Tell me about the Computer Science AS-T degree",
        "I need 12 units to transfer",
        "Room 205, building 7",
    ],
)
def test_does_not_over_redact_ordinary_text(clean):
    assert redact_pii(clean) == clean


def test_short_digit_runs_survive():
    # Years, course numbers, unit counts (< 6 digits) must pass through.
    assert redact_pii("MATH 261 in 2024 is 3 units") == "MATH 261 in 2024 is 3 units"


def test_non_string_input_is_coerced_not_crashed():
    assert redact_pii(12345) == "12345"  # 5 digits, under the ID threshold
    assert redact_pii(881234567) == "[ID]"  # 9-digit LACCD-style id


# ── langfuse_mask recurses into structured payloads ──────────────────────────


def test_mask_redacts_plain_string_payload():
    assert langfuse_mask(data="ping jane@x.edu") == "ping [EMAIL]"


def test_mask_recurses_into_observe_style_dict():
    # This is the shape @observe captures: function args including history.
    payload = {
        "message": "my id is 881234567",
        "history": [
            {"role": "user", "content": "email jane@x.edu"},
            {"role": "assistant", "content": "call 555-123-4567"},
        ],
        "campus": "LAMC",
    }
    masked = langfuse_mask(data=payload)
    assert masked["message"] == "my id is [ID]"
    assert masked["history"][0]["content"] == "email [EMAIL]"
    assert "[PHONE]" in masked["history"][1]["content"]
    # Structural / non-PII fields are preserved verbatim.
    assert masked["campus"] == "LAMC"
    assert masked["history"][0]["role"] == "user"


def test_mask_preserves_non_string_scalars():
    payload = {"latency_ms": 1234, "ok": True, "score": None, "note": "id 881234567"}
    masked = langfuse_mask(data=payload)
    assert masked["latency_ms"] == 1234
    assert masked["ok"] is True
    assert masked["score"] is None
    assert masked["note"] == "id [ID]"


def test_mask_accepts_extra_kwargs():
    # The SDK may pass additional keyword args; the callable must tolerate them.
    assert langfuse_mask(data="id 881234567", field="input") == "id [ID]"


# ── the mask is actually honored by the Langfuse span layer ──────────────────


def test_langfuse_span_applies_mask_to_io():
    """A span built by a masked client redacts input/output before export.

    Exercises the real SDK code path (``LangfuseSpan._mask_attribute``) rather
    than a live trace, proving the wired-in ``mask`` callable takes effect.
    """
    from langfuse import Langfuse

    client = Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        host="https://cloud.langfuse.example",  # never contacted; export is dropped
        mask=langfuse_mask,
        tracing_enabled=True,
        # Masking runs when attributes are set (pre-export); dropping the span
        # keeps this test fully offline with no background OTLP flush.
        should_export_span=lambda span: False,
    )
    assert client._mask is langfuse_mask

    span = client.start_observation(
        name="chat.answer",
        as_type="span",
        input="my student id is 881234567",
        output="reach me at jane@x.edu",
    )
    try:
        attrs = span._otel_span.attributes
        blob = " ".join(str(v) for v in attrs.values())
        assert "881234567" not in blob
        assert "jane@x.edu" not in blob
        assert "[ID]" in blob
        assert "[EMAIL]" in blob
    finally:
        span.end()
