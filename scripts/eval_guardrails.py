#!/usr/bin/env python3
"""Evaluate the guardrails scope classifier against a labeled dataset.

Loads ``tests/guardrails_eval/dataset.jsonl`` and runs every utterance through
``guarded_answer(...)`` with the ChatOrchestrator STUBBED — no Gemini/Neo4j/
network calls happen. For each row we record whether the turn was REFUSED
(``route == "guardrail_refusal"``) or DELEGATED (anything else), then print:

* in-scope recall      = in-scope turns NOT refused / all in-scope
* out-of-scope refusal = out-of-scope turns refused / all out-of-scope
* a confusion breakdown by category

This measures the classifier the threshold + exemplars control, in isolation
from the downstream orchestrator. It never enables the ``GUARDRAILS_ENABLED``
flag; it calls ``guarded_answer`` directly.

Usage::

    PYTHONPATH=backend:. python scripts/eval_guardrails.py
    PYTHONPATH=backend:. python scripts/eval_guardrails.py --threshold 0.35
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT, PROJECT_ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

DATASET = PROJECT_ROOT / "tests" / "guardrails_eval" / "dataset.jsonl"

REFUSAL_ROUTE = "guardrail_refusal"


def _load_dataset(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class _StubOrchestrator:
    """Stand-in for ChatOrchestrator: any delegated turn returns a trivial
    ChatResponse without touching the network. ``.tools.gemini_file_search``
    is stubbed too so the (unused) GeminiRailsLLM generation path is inert."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.tools = SimpleNamespace(
            gemini_file_search=SimpleNamespace(generate=lambda *_a, **_k: "stub")
        )

    async def answer(
        self,
        message,
        *,
        history=None,
        campus=None,
        use_live_crawl=False,
        resolved=None,
        pending=None,
    ):
        from app.models import ChatResponse

        return ChatResponse(answer="stub-delegated", route="stub_delegated")


async def _classify(guarded_answer, text: str, campus: str) -> str:
    """Return 'refused', 'delegated', or 'error:<msg>' for one utterance."""
    try:
        resp = await guarded_answer(text, campus=campus)
    except Exception as exc:  # pragma: no cover - surfaced in the report
        return f"error:{type(exc).__name__}"
    return "refused" if resp.route == REFUSAL_ROUTE else "delegated"


def _override_threshold(threshold: float | None):
    """Patch the embeddings-only similarity threshold in the loaded rails
    config (so a sweep doesn't require editing config.yml)."""
    if threshold is None:
        return None
    from app.services.guardrails import rails as rails_mod

    engine = rails_mod.get_rails()
    engine.config.rails.dialog.user_messages.embeddings_only_similarity_threshold = threshold
    return threshold


async def run(dataset: list[dict], campus: str, threshold: float | None) -> dict:
    from app.services.guardrails import rails as rails_mod

    # Force the classifier to be measured in isolation: no fail-open masking.
    with patch("app.services.chat.get_orchestrator", return_value=_StubOrchestrator()), \
         patch.object(rails_mod.settings, "guardrails_fail_open", False):
        _override_threshold(threshold)
        results: list[dict] = []
        for row in dataset:
            outcome = await _classify(rails_mod.guarded_answer, row["text"], campus)
            results.append({**row, "outcome": outcome})
    return _score(results)


def _score(results: list[dict]) -> dict:
    in_scope = [r for r in results if r["label"] == "in_scope"]
    out_scope = [r for r in results if r["label"] == "out_of_scope"]

    in_not_refused = sum(1 for r in in_scope if r["outcome"] == "delegated")
    out_refused = sum(1 for r in out_scope if r["outcome"] == "refused")

    recall = in_not_refused / len(in_scope) if in_scope else 0.0
    refusal_rate = out_refused / len(out_scope) if out_scope else 0.0

    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "refused": 0, "delegated": 0, "error": 0})
    for r in results:
        key = f'{r["label"]}/{r["category"]}'
        bucket = by_cat[key]
        bucket["total"] += 1
        if r["outcome"].startswith("error"):
            bucket["error"] += 1
        else:
            bucket[r["outcome"]] += 1

    return {
        "n": len(results),
        "in_scope_total": len(in_scope),
        "out_scope_total": len(out_scope),
        "in_scope_recall": recall,
        "out_scope_refusal": refusal_rate,
        "by_cat": dict(by_cat),
        "results": results,
    }


def _print_report(report: dict, threshold: float | None, campus: str) -> None:
    print("=" * 66)
    print("Guardrails scope-classifier eval")
    print(f"  campus (context): {campus}")
    print(f"  threshold:        {threshold if threshold is not None else '(config default)'}")
    print(f"  rows:             {report['n']}")
    print("=" * 66)
    print(f"in-scope recall       (NOT refused): {report['in_scope_recall']:.3f}"
          f"   [{report['in_scope_total']} rows]   target >= 0.95")
    print(f"out-of-scope refusal  (refused):     {report['out_scope_refusal']:.3f}"
          f"   [{report['out_scope_total']} rows]   target >= 0.90")
    print("-" * 66)
    print(f"{'category':<26}{'total':>7}{'refused':>9}{'delegated':>11}{'error':>7}")
    for key in sorted(report["by_cat"]):
        b = report["by_cat"][key]
        print(f"{key:<26}{b['total']:>7}{b['refused']:>9}{b['delegated']:>11}{b['error']:>7}")
    print("-" * 66)

    # Show the misclassifications so exemplar tuning is targeted.
    in_refused = [r for r in report["results"] if r["label"] == "in_scope" and r["outcome"] != "delegated"]
    out_delegated = [r for r in report["results"] if r["label"] == "out_of_scope" and r["outcome"] != "refused"]
    if in_refused:
        print(f"IN-SCOPE wrongly refused ({len(in_refused)}):")
        for r in in_refused:
            print(f"  [{r['category']}] ({r['outcome']}) {r['text']}")
    if out_delegated:
        print(f"OUT-OF-SCOPE wrongly delegated ({len(out_delegated)}):")
        for r in out_delegated:
            print(f"  [{r['category']}] ({r['outcome']}) {r['text']}")
    print("=" * 66)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=None,
                        help="Override embeddings_only_similarity_threshold for this run.")
    parser.add_argument("--campus", default="LAMC", help="Campus context code (default LAMC).")
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--json", action="store_true", help="Emit the metrics as JSON only.")
    args = parser.parse_args()

    dataset = _load_dataset(args.dataset)
    report = asyncio.run(run(dataset, args.campus, args.threshold))

    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    else:
        _print_report(report, args.threshold, args.campus)

    ok = report["in_scope_recall"] >= 0.95 and report["out_scope_refusal"] >= 0.90
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
