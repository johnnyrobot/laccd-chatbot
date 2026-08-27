from __future__ import annotations

import asyncio

from app.services.chat import RewriteResult
from scripts.eval_guardrails import _StubOrchestrator


def test_eval_stub_tracks_the_orchestrator_answer_contract() -> None:
    result = asyncio.run(
        _StubOrchestrator().answer(
            "UCLA",
            history=[{"role": "user", "content": "Does this transfer?"}],
            campus="LAMC",
            use_live_crawl=False,
            resolved=RewriteResult(
                english_query="Does STAT C1000 transfer to UCLA?",
                detected_lang="en",
            ),
            pending=None,
        )
    )

    assert result.route == "stub_delegated"
