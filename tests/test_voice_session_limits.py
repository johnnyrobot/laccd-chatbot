from __future__ import annotations

import asyncio

from voice_agent.safety import enforce_session_limit


def test_session_watchdog_shuts_down_without_draining() -> None:
    class FakeSession:
        drain = None

        def shutdown(self, *, drain: bool = True) -> None:
            self.drain = drain

    session = FakeSession()

    asyncio.run(enforce_session_limit(session, seconds=0, room_id="test-room-id"))

    assert session.drain is False
