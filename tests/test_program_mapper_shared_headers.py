from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MODULE_NAME = "sync_programs_under_test"


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


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        yield json.dumps(self._payload).encode()


def test_seed_script_careers_request_sends_a_browser_user_agent(monkeypatch):
    """The seed path has its own client and must set its own User-Agent."""
    mod = _load_sync_module()
    captured: dict = {}

    def fake_stream(self, method, url, headers=None, **kwargs):
        captured["headers"] = headers
        return _Response(200, {"careers": []})

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    run(mod.fetch_careers("LAMC", ["15-1252"]))

    assert "Mozilla/" in captured["headers"].get("User-Agent", "")


def test_seed_script_logs_a_warning_when_the_careers_request_is_rejected(monkeypatch, caplog):
    """A 403 used to fall through to [] with no log line at any level."""
    mod = _load_sync_module()

    def fake_stream(self, method, url, headers=None, **kwargs):
        return _Response(403)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    with caplog.at_level(logging.WARNING):
        careers = run(mod.fetch_careers("LAMC", ["15-1252"]))

    assert careers == []
    assert any("403" in record.getMessage() for record in caplog.records)
