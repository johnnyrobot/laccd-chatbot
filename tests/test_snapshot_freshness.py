"""A stale Program Mapper snapshot must announce itself.

Production cannot reach the vendor — their WAF refuses the box by IP — so
program answers are served from a snapshot replayed weekly into Neo4j from an
unblocked Mac. That fallback answers *successfully*, which is exactly what makes
it dangerous: if the Mac dies or the launchd job stops, the graph freezes at
whatever Sunday it last ran and every answer still looks fine.

Nothing recorded when a refresh last succeeded, so staleness was undetectable.
These tests pin the three properties that make it detectable:

* **A run that wrote nothing must not certify itself as fresh.** Stamping the
  heartbeat unconditionally would be the same silent-success bug one layer up.
* **Never-stamped is not the same as fresh.** It reports distinctly and emits
  no score, so Langfuse's NO_DATA handling is what speaks. Emitting a 0 here
  would read as "perfectly fresh" and defeat the alert entirely — Langfuse's
  default no-data mode treats missing data as 0.
* **Staleness is loud locally too**, on a timer rather than on request traffic,
  so a quiet weekend cannot look like a dead pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import neo4j_service as neo4j_module
from app.services import snapshot_monitor
from app.services.snapshot_freshness import (
    SCORE_NAME,
    program_snapshot_freshness,
    snapshot_health_dict,
)


MONITOR_LOGGER = "app.services.snapshot_monitor"


def _our_warnings(caplog) -> list:
    """Only this module's warnings.

    The langfuse SDK logs its own warnings during tests (it detects multiple
    clients in-process and skips tracing), and those say nothing about whether
    the monitor did its job.
    """
    return [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and record.name == MONITOR_LOGGER
    ]


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _stub_heartbeat(monkeypatch, row):
    async def fake_run_query(cypher: str, params: dict | None = None) -> list[dict]:
        if "SyncHeartbeat" not in cypher:
            return []
        return [row] if row else []

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", fake_run_query)


class _FakeLangfuse:
    def __init__(self):
        self.scores: list[dict] = []

    def score_current_trace(self, **kwargs):
        self.scores.append(kwargs)

    def update_current_span(self, **kwargs):
        pass


@pytest.fixture
def fake_langfuse(monkeypatch):
    client = _FakeLangfuse()
    monkeypatch.setattr(snapshot_monitor, "get_client", lambda: client)
    return client


# ---------------------------------------------------------------- freshness


def test_a_recent_refresh_is_fresh(monkeypatch):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(2), "programs": 1719})
    freshness = asyncio.run(program_snapshot_freshness())

    assert freshness.recorded is True
    assert freshness.stale is False
    assert 1.9 < freshness.age_days < 2.1
    assert freshness.programs == 1719


def test_a_refresh_older_than_the_threshold_is_stale(monkeypatch):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(12), "programs": 1719})
    freshness = asyncio.run(program_snapshot_freshness())

    assert freshness.stale is True
    assert freshness.age_days > 11


def test_the_threshold_allows_one_late_weekly_run(monkeypatch):
    """launchd runs the job at next wake if the Mac was asleep, so a job a few
    days late is normal and must not cry wolf."""
    assert settings.program_mapper_snapshot_max_age_days >= 8
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(8), "programs": 1719})
    assert asyncio.run(program_snapshot_freshness()).stale is False


def test_a_never_stamped_snapshot_is_not_reported_as_fresh(monkeypatch):
    """Reporting healthy because nobody ever wrote a heartbeat would recreate
    the exact silent-success bug this feature exists to catch."""
    _stub_heartbeat(monkeypatch, None)
    freshness = asyncio.run(program_snapshot_freshness())

    assert freshness.recorded is False
    assert freshness.stale is None, "unknown must not collapse to False"
    assert freshness.age_days is None


def test_an_unreadable_timestamp_is_treated_as_unknown_not_fresh(monkeypatch):
    _stub_heartbeat(monkeypatch, {"completed_at": "not-a-date", "programs": 5})
    freshness = asyncio.run(program_snapshot_freshness())

    assert freshness.recorded is False
    assert freshness.stale is None


# ---------------------------------------------------------------- health view


def test_health_reports_the_snapshot_age(monkeypatch):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(3), "programs": 1719})
    health = snapshot_health_dict(asyncio.run(program_snapshot_freshness()))

    assert health["recorded"] is True
    assert health["stale"] is False
    assert health["age_days"] == pytest.approx(3, abs=0.2)
    assert health["max_age_days"] == settings.program_mapper_snapshot_max_age_days
    assert health["last_refreshed"]


def test_health_says_never_recorded_rather_than_fresh(monkeypatch):
    _stub_heartbeat(monkeypatch, None)
    health = snapshot_health_dict(asyncio.run(program_snapshot_freshness()))

    assert health["recorded"] is False
    assert health["stale"] is None
    assert health["last_refreshed"] is None


# ---------------------------------------------------------------- monitor


def test_the_monitor_emits_the_age_as_a_numeric_score(monkeypatch, fake_langfuse):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(4), "programs": 1719})
    asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert len(fake_langfuse.scores) == 1
    score = fake_langfuse.scores[0]
    assert score["name"] == SCORE_NAME
    assert score["data_type"] == "NUMERIC"
    assert score["value"] == pytest.approx(4, abs=0.2)


def test_the_monitor_emits_nothing_when_never_stamped(monkeypatch, fake_langfuse):
    """Langfuse's default no-data mode treats missing data as 0, which would read
    as 'perfectly fresh'. Emitting no score is what lets NO_DATA fire instead."""
    _stub_heartbeat(monkeypatch, None)
    asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert fake_langfuse.scores == []


def test_the_monitor_warns_when_stale(monkeypatch, fake_langfuse, caplog):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(15), "programs": 1719})
    with caplog.at_level(logging.WARNING):
        asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert _our_warnings(caplog), "a stale snapshot must warn"
    assert fake_langfuse.scores[0]["value"] > 14


def test_the_monitor_warns_when_never_stamped(monkeypatch, fake_langfuse, caplog):
    _stub_heartbeat(monkeypatch, None)
    with caplog.at_level(logging.WARNING):
        asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert _our_warnings(caplog), "a never-stamped snapshot must warn"


def test_the_monitor_is_quiet_when_fresh(monkeypatch, fake_langfuse, caplog):
    _stub_heartbeat(monkeypatch, {"completed_at": _iso(1), "programs": 1719})
    with caplog.at_level(logging.WARNING):
        asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert not _our_warnings(caplog)


def test_a_broken_graph_read_never_takes_down_the_app(monkeypatch, fake_langfuse):
    """This is a monitor. It must not be able to break the thing it watches."""

    async def boom(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    monkeypatch.setattr(neo4j_module.neo4j_service, "run_query", boom)
    freshness = asyncio.run(snapshot_monitor.report_snapshot_freshness())

    assert freshness.recorded is False
    assert fake_langfuse.scores == []


# ---------------------------------------------------------------- write side


def test_the_sync_stamps_a_heartbeat_only_when_it_wrote_programs():
    """A run that wrote nothing must not certify the snapshot as fresh."""
    import scripts.sync_programs_to_neo4j as sync

    writes: list[tuple[str, dict]] = []

    async def fake_run_write(cypher: str, params: dict | None = None) -> None:
        writes.append((cypher, params or {}))

    original = neo4j_module.neo4j_service.run_write
    neo4j_module.neo4j_service.run_write = fake_run_write
    try:
        asyncio.run(sync.record_sync_heartbeat({"programs": 0, "careers": 0}, campuses=9))
        assert writes == [], "an empty run must not stamp a heartbeat"

        asyncio.run(sync.record_sync_heartbeat({"programs": 1719, "careers": 506}, campuses=9))
        assert len(writes) == 1
        cypher, params = writes[0]
        assert "SyncHeartbeat" in cypher
        assert params["programs"] == 1719
        assert params["completed_at"]
    finally:
        neo4j_module.neo4j_service.run_write = original
