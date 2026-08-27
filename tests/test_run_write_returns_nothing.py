"""`run_write` returns None — never read a count back from it.

This trap fooled us in both directions in one day:

  * `load_credit_limitations` did `result = await run_write(... RETURN count(c))`
    and then `if result:`, which never fired. It logged "Attached
    credit_limitations to 0 Course nodes (out of 1441 candidates)" while the SET
    worked perfectly — a real success reporting as a silent failure.
  * `load_prerequisites` did the same on its --clear path, so it always said
    "Cleared 0 existing PREREQUISITE_OF relationships" however many it deleted.

The mirror image (a real failure reporting success) is what hid the Program
Mapper 403 and the renumbering MERGE-with-no-DELETE. Whichever direction, the
signal was uncorrelated with the data.

`RETURN` in a write query is not wrong in itself — the value is simply
discarded. Count with `run_query` instead.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.services.neo4j_service import Neo4jService


REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIRS = ("scripts", "backend", "shared", "voice_agent")

# `x = await ...run_write(` — binding the result is the mistake this guards.
_ASSIGNED_RUN_WRITE = re.compile(r"=\s*await\s+[\w.]*run_write\s*\(")


def test_run_write_declares_no_return_value():
    """If this ever returns rows, the guard below can be relaxed."""
    assert inspect.signature(Neo4jService.run_write).return_annotation in (None, "None")


def test_no_caller_reads_a_value_back_from_run_write():
    offenders: list[str] = []
    for directory in SEARCH_DIRS:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if _ASSIGNED_RUN_WRITE.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "run_write() returns None, so these read a value that is always None.\n"
        "Count with run_query() instead:\n  " + "\n  ".join(offenders)
    )
