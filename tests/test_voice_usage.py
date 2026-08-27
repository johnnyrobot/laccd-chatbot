from __future__ import annotations

from livekit.agents.metrics import UsageSummary

from voice_agent.usage import (
    VoiceCostRates,
    cost_from_usage_summary,
    estimate_session_cost,
    rates_from_env,
)


def _rates() -> VoiceCostRates:
    """Rates in USD per 1M tokens, chosen so each modality is distinguishable."""
    return VoiceCostRates(
        input_audio=3.0,
        input_text=1.0,
        output_audio=12.0,
        output_text=6.0,
        cached_input_audio=0.75,
        cached_input_text=0.25,
    )


def test_audio_input_tokens_are_priced_at_the_input_audio_rate() -> None:
    cost = estimate_session_cost(
        input_audio_tokens=1_000_000,
        rates=_rates(),
    )

    assert cost.input_audio_usd == 3.0
    assert cost.total_usd == 3.0


def test_each_modality_is_priced_separately_and_summed() -> None:
    cost = estimate_session_cost(
        input_audio_tokens=1_000_000,
        input_text_tokens=1_000_000,
        output_audio_tokens=1_000_000,
        output_text_tokens=1_000_000,
        rates=_rates(),
    )

    assert cost.input_audio_usd == 3.0
    assert cost.input_text_usd == 1.0
    assert cost.output_audio_usd == 12.0
    assert cost.output_text_usd == 6.0
    assert cost.total_usd == 22.0


def test_cached_input_tokens_are_a_subset_billed_at_the_cached_rate() -> None:
    """LiveKit reports cached tokens as a breakdown of the input totals, not as
    an extra bucket. Billing them on top would inflate every session."""
    cost = estimate_session_cost(
        input_audio_tokens=1_000_000,
        cached_input_audio_tokens=400_000,
        rates=_rates(),
    )

    # 600k uncached @ $3/M + 400k cached @ $0.75/M
    assert cost.input_audio_usd == 2.1
    assert cost.total_usd == 2.1


def test_fully_cached_input_costs_only_the_cached_rate() -> None:
    cost = estimate_session_cost(
        input_text_tokens=1_000_000,
        cached_input_text_tokens=1_000_000,
        rates=_rates(),
    )

    assert cost.input_text_usd == 0.25


def test_cost_is_derived_from_a_real_livekit_usage_summary() -> None:
    """Guards against LiveKit renaming its counters: this uses the real
    dataclass, so a field rename breaks the test rather than silently
    reporting $0.00 forever."""
    summary = UsageSummary(
        llm_input_audio_tokens=1_000_000,
        llm_input_cached_audio_tokens=400_000,
        llm_input_text_tokens=1_000_000,
        llm_output_audio_tokens=1_000_000,
        llm_output_text_tokens=1_000_000,
    )

    cost = cost_from_usage_summary(summary, rates=_rates())

    assert cost.input_audio_usd == 2.1
    assert cost.input_text_usd == 1.0
    assert cost.output_audio_usd == 12.0
    assert cost.output_text_usd == 6.0
    assert cost.total_usd == 21.1


def test_rates_fall_back_to_published_gemini_live_pricing() -> None:
    rates = rates_from_env(env={})

    assert rates.input_audio == 3.0
    assert rates.output_audio == 12.0


def test_rates_can_be_overridden_per_modality() -> None:
    """Live API pricing is preview-tier and moves; operators must be able to
    correct it without a code change."""
    rates = rates_from_env(env={"VOICE_PRICE_INPUT_AUDIO_PER_MTOK": "2.50"})

    assert rates.input_audio == 2.50
    assert rates.output_audio == 12.0


def test_unparseable_rate_falls_back_instead_of_crashing_the_session() -> None:
    """A typo in a price env var must never take voice down."""
    rates = rates_from_env(env={"VOICE_PRICE_INPUT_AUDIO_PER_MTOK": "not-a-number"})

    assert rates.input_audio == 3.0
