from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAME = "sync_programs_record_replay_under_test"


def _load_sync_module():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    path = PROJECT_ROOT / "scripts" / "sync_programs_to_neo4j.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def run(coro):
    return asyncio.run(coro)


def _no_network(monkeypatch):
    """Any outbound HTTP during replay is a bug — make it explode loudly."""

    def boom(*args, **kwargs):
        raise AssertionError("replay mode must not touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "stream", boom)


def test_recording_a_fetch_writes_it_to_the_dump_dir(tmp_path, monkeypatch):
    mod = _load_sync_module()
    monkeypatch.setattr(mod, "REPLAY_DIR", None)
    monkeypatch.setattr(mod, "DUMP_DIR", tmp_path)

    async def fake_fetch(url, headers):
        return {"programGroups": [{"masterRecordId": "g1"}]}

    monkeypatch.setattr(mod, "fetch_via_httpx", fake_fetch)

    result = run(mod.pm_fetch("/site-contents/abc/home-page-content", "LAMC"))

    assert result == {"programGroups": [{"masterRecordId": "g1"}]}
    written = list(tmp_path.rglob("*.json"))
    assert written, "expected the response to be recorded to the dump dir"
    assert json.loads(written[0].read_text())["programGroups"][0]["masterRecordId"] == "g1"


def test_replaying_a_fetch_reads_from_disk_without_network(tmp_path, monkeypatch):
    mod = _load_sync_module()
    path = "/site-contents/abc/home-page-content"

    # record first
    monkeypatch.setattr(mod, "REPLAY_DIR", None)
    monkeypatch.setattr(mod, "DUMP_DIR", tmp_path)

    async def fake_fetch(url, headers):
        return {"programGroups": [{"masterRecordId": "g1"}]}

    monkeypatch.setattr(mod, "fetch_via_httpx", fake_fetch)
    run(mod.pm_fetch(path, "LAMC"))

    # now replay with the network mined
    monkeypatch.setattr(mod, "DUMP_DIR", None)
    monkeypatch.setattr(mod, "REPLAY_DIR", tmp_path)
    _no_network(monkeypatch)

    replayed = run(mod.pm_fetch(path, "LAMC"))

    assert replayed == {"programGroups": [{"masterRecordId": "g1"}]}


def test_careers_are_recorded_and_replayed_without_network(tmp_path, monkeypatch):
    """The careers POST is the call that has no proxy fallback — it must replay."""
    mod = _load_sync_module()
    socs = ["15-1252", "29-1141"]
    payload = {"careers": [{"standardOccupationalCode": "15-1252", "title": "Software Developers"}]}

    monkeypatch.setattr(mod, "REPLAY_DIR", None)
    monkeypatch.setattr(mod, "DUMP_DIR", tmp_path)

    class _Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_bytes(self):
            yield json.dumps(payload).encode()

    def fake_stream(self, method, url, headers=None, **kwargs):
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    recorded = run(mod.fetch_careers("LAMC", socs))
    assert recorded == payload["careers"]

    monkeypatch.setattr(mod, "DUMP_DIR", None)
    monkeypatch.setattr(mod, "REPLAY_DIR", tmp_path)
    _no_network(monkeypatch)

    replayed = run(mod.fetch_careers("LAMC", socs))

    assert replayed == payload["careers"]


def test_replay_of_a_missing_recording_warns_and_returns_empty(tmp_path, monkeypatch, caplog):
    """A gap in the dump must be visible, not silently an empty graph."""
    import logging

    mod = _load_sync_module()
    monkeypatch.setattr(mod, "DUMP_DIR", None)
    monkeypatch.setattr(mod, "REPLAY_DIR", tmp_path)
    _no_network(monkeypatch)

    with caplog.at_level(logging.WARNING):
        result = run(mod.pm_fetch("/site-contents/abc/never-recorded", "LAMC"))

    assert result is None
    assert any("no recording" in r.getMessage().lower() for r in caplog.records)


def test_fetch_campus_records_program_maps_for_replay(tmp_path, monkeypatch):
    """fetch_campus must mirror EVERY endpoint sync_campus asks for.

    It previously skipped /program-maps/, so a replay seeded 0 pathway
    elements and 0 course links while logging success.
    """
    mod = _load_sync_module()
    monkeypatch.setattr(mod, "REPLAY_DIR", None)
    monkeypatch.setattr(mod, "DUMP_DIR", tmp_path)
    monkeypatch.setattr(
        mod, "COLLEGE_CONFIGS",
        {"LAMC": {"site_content_id": "site1", "origin": "https://example.test", "name": "LAMC"}},
    )

    async def fake_fetch(url, headers):
        if url.endswith("/home-page-content"):
            return {"programGroups": [{"masterRecordId": "g1", "title": "G"}]}
        if "/program-groups/" in url:
            return {"programs": [{"masterRecordId": "p1", "title": "P"}]}
        if "/programs/" in url:
            return {"pathways": [{"defaultPathway": True, "programMapId": "map1"}]}
        if "/program-maps/" in url:
            return {"pathwayElements": [{"name": "MATH 261"}]}
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(mod, "fetch_via_httpx", fake_fetch)

    run(mod.fetch_campus("LAMC"))

    recorded = [p.name for p in (tmp_path / "LAMC").glob("*.json")]
    replayed_map = run(_replay_program_map(mod, tmp_path))
    assert replayed_map == {"pathwayElements": [{"name": "MATH 261"}]}, recorded


async def _replay_program_map(mod, dump_dir):
    mod.DUMP_DIR = None
    mod.REPLAY_DIR = dump_dir
    return await mod.pm_fetch("/site-contents/site1/program-maps/map1", "LAMC")
