"""Tests for neo4j_service.resolve_course_code() — the renumbering walker.

The walker tries three strategies in order:
  1. Literal (subject, number) lookup.
  2. C-form alternation (CHEM 211 ↔ CHEM C211).
  3. RENUMBERED_TO chain walk (forward and backward, max 3 hops).

These tests use a stub Neo4jService that pattern-matches the cypher fragment
to decide which result set to return, so we exercise the walker's branch
selection without booting a real database.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from app.services.neo4j_service import Neo4jService


class _StubNeo4j(Neo4jService):
    """Configurable stub: each test sets a `responder(cypher, params) -> rows`."""

    def __init__(self, responder: Callable[[str, dict[str, Any]], list[dict[str, Any]]]):
        super().__init__()
        self._responder = responder

    def is_configured(self) -> bool:  # type: ignore[override]
        return True

    async def run_query(  # type: ignore[override]
        self, cypher: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._responder(cypher, params or {})


def _run(coro):
    return asyncio.run(coro)


def test_literal_hit_returns_no_resolution_marker():
    def responder(cypher: str, params: dict) -> list[dict]:
        if "MATCH (c:Course {subject_code: $subject, number: $number" in cypher:
            return [{"subject": "MATH", "number": "261"}]
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("MATH", "261", "WLAC"))
    assert (sub, num) == ("MATH", "261")
    assert via is None


def test_c_form_alternation_resolves_chem_c211_to_chem_211():
    """Input 'CHEM C211' has no node, but 'CHEM 211' does — alternation flips the C."""
    def responder(cypher: str, params: dict) -> list[dict]:
        if "MATCH (c:Course {subject_code: $subject, number: $number" in cypher:
            return []  # literal miss
        if "MATCH (c:Course {subject_code: $subject, number: $alt" in cypher:
            if params.get("alt") == "211":
                return [{"subject": "CHEM", "number": "211"}]
            return []
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("CHEM", "C211", "WLAC"))
    assert (sub, num) == ("CHEM", "211")
    assert via == "c_form_alternation"


def test_c_form_alternation_resolves_chem_211_to_chem_c211():
    """Input 'CHEM 211' has no node, but 'CHEM C211' does — alternation prefixes the C."""
    def responder(cypher: str, params: dict) -> list[dict]:
        if "MATCH (c:Course {subject_code: $subject, number: $number" in cypher:
            return []
        if "MATCH (c:Course {subject_code: $subject, number: $alt" in cypher:
            if params.get("alt") == "C211":
                return [{"subject": "CHEM", "number": "C211"}]
            return []
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("CHEM", "211", "WLAC"))
    assert (sub, num) == ("CHEM", "C211")
    assert via == "c_form_alternation"


def test_renumbered_to_forward_walk_resolves_math_227_to_stat_c1000():
    """When literal + alternation miss, walker follows RENUMBERED_TO forward."""
    def responder(cypher: str, params: dict) -> list[dict]:
        if "MATCH (c:Course {subject_code: $subject, number: $number" in cypher:
            return []
        if "MATCH (c:Course {subject_code: $subject, number: $alt" in cypher:
            return []
        if "RENUMBERED_TO*1..3]->(target:Course)" in cypher:
            return [{"subject": "STAT", "number": "C1000"}]
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("MATH", "227", "WLAC"))
    assert (sub, num) == ("STAT", "C1000")
    assert via == "renumbered_to"


def test_renumbered_to_backward_walk_resolves_canonical_to_origin():
    """When forward walk misses, walker tries the reverse direction.

    Useful for queries that name the *new* CCN form when only the *legacy*
    LACCD code carries data.
    """
    def responder(cypher: str, params: dict) -> list[dict]:
        if "MATCH (c:Course {subject_code: $subject, number: $number" in cypher:
            return []
        if "MATCH (c:Course {subject_code: $subject, number: $alt" in cypher:
            return []
        if "RENUMBERED_TO*1..3]->(target:Course)" in cypher:
            return []
        if "(origin:Course)-[:RENUMBERED_TO*1..3]->(q)" in cypher:
            return [{"subject": "MATH", "number": "227"}]
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("STAT", "C1000", "WLAC"))
    assert (sub, num) == ("MATH", "227")
    assert via == "renumbered_to"


def test_no_resolution_returns_input_with_via_none():
    """All strategies miss → return the original code unchanged."""
    def responder(cypher: str, params: dict) -> list[dict]:
        return []

    svc = _StubNeo4j(responder)
    sub, num, via = _run(svc.resolve_course_code("PHYSICS", "6", "WLAC"))
    assert (sub, num) == ("PHYSICS", "6")
    assert via is None


def test_unconfigured_service_short_circuits():
    """Walker returns input unchanged when neo4j isn't configured (tests/CI)."""
    class Unconfigured(_StubNeo4j):
        def is_configured(self) -> bool:  # type: ignore[override]
            return False

    svc = Unconfigured(lambda *_: pytest.fail("should not query"))
    sub, num, via = _run(svc.resolve_course_code("CHEM", "C211", "WLAC"))
    assert (sub, num, via) == ("CHEM", "C211", None)
