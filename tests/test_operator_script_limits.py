from __future__ import annotations

import ast
import asyncio

import pytest

from scripts.pagination_guard import PaginationGuard, PaginationLimitExceeded
from scripts.refresh_openstax import render_python_mapping
from scripts import build_knowledge_pack, crawl_campuses_c4a


def test_pagination_guard_rejects_repeated_full_page() -> None:
    guard = PaginationGuard(max_pages=10, max_seconds=60)
    items = [{"id": index} for index in range(25)]

    guard.before_request(1)
    guard.observe(items)
    guard.before_request(2)
    with pytest.raises(PaginationLimitExceeded, match="repeated page"):
        guard.observe(items)


def test_pagination_guard_rejects_request_past_page_limit() -> None:
    guard = PaginationGuard(max_pages=2, max_seconds=60)

    guard.before_request(1)
    guard.before_request(2)
    with pytest.raises(PaginationLimitExceeded, match="page limit"):
        guard.before_request(3)


def test_openstax_renderer_quotes_remote_strings_as_data() -> None:
    grouped = {
        "MATH": [
            {
                "title": 'Algebra\n"}; __import__("os").system("false") #',
                "slug": "path\\with\\slashes",
            }
        ]
    }

    rendered = render_python_mapping(grouped)
    module = ast.parse(rendered)
    assignment = module.body[0]

    assert isinstance(assignment, ast.AnnAssign)
    assert ast.literal_eval(assignment.value) == grouped


def test_nested_sitemap_is_validated_before_fetch(monkeypatch) -> None:
    requested: list[str] = []
    index_xml = b"""<?xml version='1.0'?>
    <sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <sitemap><loc>https://evil.example/private.xml</loc></sitemap>
    </sitemapindex>"""

    class Response:
        def __init__(self, url: str, status_code: int, content: bytes = b""):
            self.url = url
            self.status_code = status_code
            self.content = content

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url: str):
            requested.append(url)
            if url.endswith("/sitemap.xml"):
                return Response(url, 200, index_xml)
            return Response(url, 404)

    monkeypatch.setattr(crawl_campuses_c4a.httpx, "AsyncClient", lambda **_kwargs: Client())

    result = asyncio.run(
        crawl_campuses_c4a.fetch_sitemap_urls(
            "https://campus.example",
            {"campus.example"},
        )
    )

    assert result == []
    assert "https://evil.example/private.xml" not in requested


def test_giant_paragraphs_are_split_by_a_hard_character_cap() -> None:
    chunks = build_knowledge_pack._chunk_paragraphs("x" * 50_000)

    assert chunks
    assert max(map(len, chunks)) <= build_knowledge_pack.CHUNK_MAX_CHARS
