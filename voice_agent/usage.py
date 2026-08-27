"""Cost accounting for realtime voice sessions.

The Gemini Live session runs inside LiveKit's google plugin, which never passes
through Langfuse -- so voice spend was invisible. This module turns the token
counts LiveKit already emits into a USD figure we can log per session.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.agents.metrics import UsageSummary

logger = logging.getLogger("voice_agent.usage")

_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class VoiceCostRates:
    """USD per 1M tokens, split by modality."""

    input_audio: float
    input_text: float
    output_audio: float
    output_text: float
    cached_input_audio: float = 0.0
    cached_input_text: float = 0.0


@dataclass(frozen=True)
class VoiceSessionCost:
    """USD breakdown for a single voice session."""

    input_audio_usd: float
    input_text_usd: float
    output_audio_usd: float
    output_text_usd: float
    total_usd: float


def _priced(total_tokens: int, cached_tokens: int, rate: float, cached_rate: float) -> float:
    """Price an input modality where `cached_tokens` is a subset of `total_tokens`.

    LiveKit populates the cached counters from `cached_tokens_details`, which
    breaks down the cached portion of the input totals. Treating them as an
    additional bucket would bill the same tokens twice.
    """
    uncached = max(total_tokens - cached_tokens, 0)
    return (uncached * rate + cached_tokens * cached_rate) / _PER_MILLION


def estimate_session_cost(
    *,
    input_audio_tokens: int = 0,
    input_text_tokens: int = 0,
    output_audio_tokens: int = 0,
    output_text_tokens: int = 0,
    cached_input_audio_tokens: int = 0,
    cached_input_text_tokens: int = 0,
    rates: VoiceCostRates,
) -> VoiceSessionCost:
    """Price a session's token counts using per-modality rates."""
    input_audio_usd = _priced(
        input_audio_tokens, cached_input_audio_tokens,
        rates.input_audio, rates.cached_input_audio,
    )
    input_text_usd = _priced(
        input_text_tokens, cached_input_text_tokens,
        rates.input_text, rates.cached_input_text,
    )
    output_audio_usd = output_audio_tokens / _PER_MILLION * rates.output_audio
    output_text_usd = output_text_tokens / _PER_MILLION * rates.output_text
    return VoiceSessionCost(
        input_audio_usd=input_audio_usd,
        input_text_usd=input_text_usd,
        output_audio_usd=output_audio_usd,
        output_text_usd=output_text_usd,
        total_usd=(
            input_audio_usd + input_text_usd + output_audio_usd + output_text_usd
        ),
    )


def cost_from_usage_summary(
    summary: UsageSummary,
    *,
    rates: VoiceCostRates,
) -> VoiceSessionCost:
    """Price a LiveKit UsageSummary collected over a voice session."""
    return estimate_session_cost(
        input_audio_tokens=summary.llm_input_audio_tokens,
        input_text_tokens=summary.llm_input_text_tokens,
        output_audio_tokens=summary.llm_output_audio_tokens,
        output_text_tokens=summary.llm_output_text_tokens,
        cached_input_audio_tokens=summary.llm_input_cached_audio_tokens,
        cached_input_text_tokens=summary.llm_input_cached_text_tokens,
        rates=rates,
    )


# Published Gemini Live native-audio pricing, USD per 1M tokens. These are
# defaults, not gospel: the Live API is preview-tier and its rates move, so
# every one is overridable via env without a redeploy of this module.
_DEFAULT_RATES = {
    "VOICE_PRICE_INPUT_AUDIO_PER_MTOK": 3.0,
    "VOICE_PRICE_INPUT_TEXT_PER_MTOK": 0.5,
    "VOICE_PRICE_OUTPUT_AUDIO_PER_MTOK": 12.0,
    "VOICE_PRICE_OUTPUT_TEXT_PER_MTOK": 2.0,
    "VOICE_PRICE_CACHED_INPUT_AUDIO_PER_MTOK": 0.75,
    "VOICE_PRICE_CACHED_INPUT_TEXT_PER_MTOK": 0.125,
}


def _rate(env: Mapping[str, str], name: str) -> float:
    """Read one rate, falling back to the default on absence or a bad value.

    A mistyped price must never raise: this runs inside a live call.
    """
    default = _DEFAULT_RATES[name]
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring unparseable %s=%r; using %s", name, raw, default)
        return default


def rates_from_env(env: Mapping[str, str] | None = None) -> VoiceCostRates:
    """Build cost rates from environment variables."""
    env = os.environ if env is None else env
    return VoiceCostRates(
        input_audio=_rate(env, "VOICE_PRICE_INPUT_AUDIO_PER_MTOK"),
        input_text=_rate(env, "VOICE_PRICE_INPUT_TEXT_PER_MTOK"),
        output_audio=_rate(env, "VOICE_PRICE_OUTPUT_AUDIO_PER_MTOK"),
        output_text=_rate(env, "VOICE_PRICE_OUTPUT_TEXT_PER_MTOK"),
        cached_input_audio=_rate(env, "VOICE_PRICE_CACHED_INPUT_AUDIO_PER_MTOK"),
        cached_input_text=_rate(env, "VOICE_PRICE_CACHED_INPUT_TEXT_PER_MTOK"),
    )
