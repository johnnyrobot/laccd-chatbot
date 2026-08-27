from __future__ import annotations

import asyncio

from voice_agent import agent, tools


def test_realtime_model_uses_low_latency_turn_settings():
    model = agent._build_realtime_model("test-key")
    options = model._opts

    assert options.thinking_config.thinking_budget == 0
    assert options.thinking_config.include_thoughts is False
    activity = options.realtime_input_config.automatic_activity_detection
    assert activity.prefix_padding_ms == 300
    assert activity.silence_duration_ms == 500


def test_voice_rag_disables_thinking(monkeypatch):
    calls = []

    class _FileSearch:
        def is_configured(self):
            return True

        def answer(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "A concise answer.", [], "gemini_file_search"

    monkeypatch.setattr(tools, "get_file_search", lambda: _FileSearch())
    monkeypatch.setattr(tools, "get_system_prompt", lambda *args: "system")

    answer = asyncio.run(tools.rag_answer("Question?", "LAMC"))

    assert answer == "A concise answer."
    assert calls[0][1]["thinking_budget"] == 0
