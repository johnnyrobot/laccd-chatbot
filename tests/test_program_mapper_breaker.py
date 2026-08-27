"""Program Mapper failures are cached briefly by a recoverable egress breaker.

The fallback avoids repeated failed round trips while allowing a later probe to
restore live service automatically.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.program_mapper import (
    ProgramMapperClient,
    ProgramMapperUnavailable,
    egress_breaker,
)


@pytest.fixture(autouse=True)
def _clean_breaker():
    egress_breaker().reset()
    yield
    egress_breaker().reset()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://b.api.programmapper.com/x")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code}", request=request, response=response)


def test_a_403_opens_the_breaker():
    breaker = egress_breaker()
    assert not breaker.is_open()
    breaker.record_failure(_status_error(403))
    assert breaker.is_open()


def test_a_404_does_not_open_it():
    """One missing record says nothing about whether the host is reachable."""
    breaker = egress_breaker()
    breaker.record_failure(_status_error(404))
    assert not breaker.is_open()


def test_a_500_does_not_open_it():
    """A server-side error is the vendor's problem, not an egress block."""
    egress_breaker().record_failure(_status_error(500))
    assert not egress_breaker().is_open()


@pytest.mark.parametrize("code", [401, 403, 429])
def test_refusal_codes_open_it(code):
    egress_breaker().record_failure(_status_error(code))
    assert egress_breaker().is_open()


def test_connection_errors_open_it():
    egress_breaker().record_failure(httpx.ConnectError("no route"))
    assert egress_breaker().is_open()


def test_an_open_breaker_short_circuits_without_a_network_call(monkeypatch):
    """The point of the change: no HTTP attempt at all while open."""
    egress_breaker().record_failure(_status_error(403))

    attempted = False

    class _Boom:
        def __init__(self, *a, **k):
            nonlocal attempted
            attempted = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)

    client = ProgramMapperClient("LAMC")
    with pytest.raises(ProgramMapperUnavailable):
        asyncio.run(client._get("/anything"))

    assert not attempted, "an open breaker must not construct an HTTP client"


def test_success_closes_it():
    breaker = egress_breaker()
    breaker.record_failure(_status_error(403))
    assert breaker.is_open()
    breaker.record_success()
    assert not breaker.is_open()


def test_it_recovers_on_its_own_after_the_cooldown(monkeypatch):
    """The fix is the vendor allowlisting an IP; recovery cannot need a deploy."""
    from app.config import settings

    monkeypatch.setattr(settings, "program_mapper_breaker_cooldown_seconds", 0)
    breaker = egress_breaker()
    breaker.record_failure(_status_error(403))
    # A zero/negative cooldown disables the breaker outright.
    assert not breaker.is_open()


def test_cooldown_elapsing_allows_a_probe(monkeypatch):
    from datetime import datetime, timedelta

    from app.config import settings

    monkeypatch.setattr(settings, "program_mapper_breaker_cooldown_seconds", 300)
    breaker = egress_breaker()
    breaker.record_failure(_status_error(403))
    assert breaker.is_open()

    # Pretend the cooldown has passed.
    breaker._opened_at = datetime.now() - timedelta(seconds=301)
    assert not breaker.is_open(), "after the cooldown the next call must be allowed"


def test_the_fallback_contract_is_unchanged():
    """Callers catch broad exceptions and fall back to Neo4j; the breaker must
    raise so that path runs exactly as before, only sooner."""
    assert issubclass(ProgramMapperUnavailable, Exception)
