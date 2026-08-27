"""Content-free logging and bounded-session helpers for the voice runtime."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets

logger = logging.getLogger("voice_agent")
_ROOM_LOG_KEY = secrets.token_bytes(32)


def room_log_id(room_name: str) -> str:
    """Return a process-local correlation id without exposing room contents."""

    return hmac.new(_ROOM_LOG_KEY, room_name.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


async def enforce_session_limit(session, *, seconds: int, room_id: str) -> None:
    """Close a realtime session after its configured wall-clock budget."""

    await asyncio.sleep(max(0, seconds))
    logger.info("Voice session limit reached: room_id=%s seconds=%d", room_id, seconds)
    session.shutdown(drain=False)


def transcript_log_metadata(event) -> dict[str, object]:
    """Return transcript metadata without retaining or logging transcript content."""

    transcript = getattr(event, "transcript", "") or ""
    return {
        "final": bool(getattr(event, "is_final", False)),
        "characters": len(transcript),
    }
