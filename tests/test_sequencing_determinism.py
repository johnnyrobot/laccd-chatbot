"""The same student asking twice must get the same plan.

Two independent sources of nondeterminism have been found here, and they need
two different shapes of test:

1. **An LLM ordering pass.** Five identical calls for the LAMC Animation
   certificate returned ``terms_to_complete`` = 6, 6, 7, 6, 5 with different
   term membership each time. Repeated calls inside one process catch this.

2. **Unordered set iteration.** Python randomizes string hashing per process,
   so iterating a ``set`` of course codes yields a different order in every
   worker. Repeated calls inside one process can *never* catch this — they
   share one seed. Only running the generator in subprocesses under different
   ``PYTHONHASHSEED`` values does.

(2) is what a student actually hits: uvicorn workers are separate processes,
so the same question routed to a different worker was answered from a
differently-ordered set.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.course_sequence_service import CourseSequenceService

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sequences"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACES = ("full", "momentum", "part_time")


def _run(fixture: dict, pace: str | None = None) -> dict:
    """Generate a sequence with only the network seams stubbed.

    Deliberately does *not* stub any ordering pass: an ordering layer that
    cannot survive being called twice is the bug under test.
    """
    svc = CourseSequenceService(fixture["campus_code"])
    program_info = deepcopy(fixture.get("program_info") or {})
    elements = deepcopy(fixture.get("elements") or [])
    graph = deepcopy(fixture.get("graph") or {
        "prerequisites": {}, "advisories": {}, "corequisites": {}, "course_details": {},
    })

    async def fake_get_program_requirements(program_id, campus_code):
        return program_info, elements

    async def fake_build_full_dependency_graph(course_codes, campus_code):
        return graph

    svc._get_program_requirements = fake_get_program_requirements  # type: ignore[assignment]
    svc._build_full_dependency_graph = fake_build_full_dependency_graph  # type: ignore[assignment]

    return asyncio.run(svc.generate_sequence(
        fixture["program_id"], fixture["campus_code"], pace=pace,
    ))


def _canonical(payload: dict) -> str:
    """Serialize a plan for comparison.

    ``sort_keys`` stays False on purpose. Dict insertion order is student-
    visible — ``prerequisite_chains`` is printed in order into the model
    context, and ``external_prerequisites`` is returned to the frontend as-is —
    so a plan whose key order shifts between calls is not the same plan.
    """
    return json.dumps(payload, sort_keys=False, indent=1, default=str)


# ── 1. Repeated calls in one process ────────────────────────────────────────

@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json")), ids=lambda p: p.stem)
@pytest.mark.parametrize("pace", PACES)
def test_five_identical_runs_are_identical(path, pace):
    fixture = json.loads(path.read_text())
    runs = [_run(fixture, pace) for _ in range(5)]
    first = _canonical(runs[0])
    for index, run in enumerate(runs[1:], start=2):
        assert _canonical(run) == first, (
            f"run {index} differs from run 1 for {path.stem} at pace {pace}"
        )


@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json")), ids=lambda p: p.stem)
@pytest.mark.parametrize("pace", PACES)
def test_term_count_is_stable(path, pace):
    fixture = json.loads(path.read_text())
    counts = {_run(fixture, pace)["terms_to_complete"] for _ in range(5)}
    assert len(counts) == 1, (
        f"{path.stem} at pace {pace}: terms_to_complete varied across runs: {sorted(counts)}"
    )


# ── 2. Repeated calls across processes (hash-seed independence) ─────────────

def _run_under_seed(seed: int) -> str:
    """Run every fixture at every pace in a subprocess with a fixed hash seed."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT / "backend"), str(PROJECT_ROOT)]
    )
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--emit-plans"],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=300,
    )
    assert proc.returncode == 0, (
        f"plan subprocess failed under PYTHONHASHSEED={seed}:\n{proc.stderr[-4000:]}"
    )
    return proc.stdout


def test_plans_do_not_depend_on_the_hash_seed():
    """A different worker process must not produce a different plan.

    Every ordering decision has to come from the data, never from the
    iteration order of a set. This is the only test in the suite that can
    observe that, because it is the only one that changes the seed.
    """
    baseline_seed, *other_seeds = [1, 2, 3, 4]
    baseline = _run_under_seed(baseline_seed)
    for seed in other_seeds:
        current = _run_under_seed(seed)
        if current == baseline:
            continue
        # Report the first differing plan rather than a wall of JSON.
        base_plans = json.loads(baseline)
        cur_plans = json.loads(current)
        offenders = [
            key for key in base_plans
            if json.dumps(base_plans[key]) != json.dumps(cur_plans.get(key))
        ]
        pytest.fail(
            f"plans changed between PYTHONHASHSEED={baseline_seed} and {seed}; "
            f"{len(offenders)} of {len(base_plans)} differ, first: {offenders[0]}\n"
            f"  seed {baseline_seed}: {json.dumps(base_plans[offenders[0]])[:900]}\n"
            f"  seed {seed}: {json.dumps(cur_plans.get(offenders[0]))[:900]}"
        )


def _emit_plans() -> None:
    """Subprocess entry point: print every fixture's plan as one JSON object."""
    out = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        fixture = json.loads(path.read_text())
        for pace in PACES:
            out[f"{path.stem}|{pace}"] = _run(fixture, pace)
    sys.stdout.write(json.dumps(out, sort_keys=False, indent=1, default=str))


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    if "--emit-plans" in sys.argv:
        _emit_plans()


def test_the_analyzer_no_longer_exposes_an_llm_hook():
    """Regression guard: the layer is gone, not merely bypassed.

    A bypassed-but-present hook is what let seven test harnesses stub it and
    report green while production ran the live version.
    """
    svc = CourseSequenceService("LAMC")
    assert not hasattr(svc.analyzer, "analyze_with_gemini")
    assert not hasattr(svc.analyzer, "merge_gemini_edges")
