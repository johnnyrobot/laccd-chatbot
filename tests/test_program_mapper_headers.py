from __future__ import annotations

import asyncio
import json

import httpx

from app.services.program_mapper import ProgramMapperClient


def run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        yield json.dumps({"programGroups": []}).encode()


def test_program_mapper_get_sends_a_browser_user_agent(monkeypatch):
    """b.api.programmapper.com 403s every request that lacks a browser UA.

    Observed in prod as a 100% failure rate on ``program.get_groups``; curl
    reproduces it (403 bare, 200 with a Mozilla UA).
    """
    client = ProgramMapperClient("LAMC")
    captured: dict = {}

    def fake_stream(self, method, url, headers=None, **kwargs):
        captured["method"] = method
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    run(client._get(f"/site-contents/{client.site_content_id}/home-page-content"))

    assert "Mozilla/" in captured["headers"].get("User-Agent", "")


def test_program_mapper_post_sends_a_browser_user_agent(monkeypatch):
    client = ProgramMapperClient("LAMC")
    captured: dict = {}

    def fake_stream(self, method, url, headers=None, **kwargs):
        captured["method"] = method
        captured["headers"] = headers
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    run(client._post("/anything", {"ids": []}))

    assert "Mozilla/" in captured["headers"].get("User-Agent", "")
    assert captured["method"] == "POST"
