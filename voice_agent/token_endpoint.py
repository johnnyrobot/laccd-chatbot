"""LiveKit token generation for the voice agent.

Provides create_voice_token() which generates a room token and dispatches
the voice agent. Called from the FastAPI backend at /api/voice/token.

Campus is embedded in the room name prefix (e.g. "lamc-voice-abc123")
so the agent server can determine which campus to serve.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from urllib.parse import urlparse
from uuid import uuid4

from shared.campus_config import get_campus

logger = logging.getLogger(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# Must match AGENT_NAME in voice_agent/agent.py
VOICE_AGENT_NAME = "campus-assistant"
VOICE_TOKEN_TTL = timedelta(
    seconds=max(60, min(int(os.getenv("VOICE_TOKEN_TTL_SECONDS", "600")), 1800))
)


def validated_livekit_url() -> str | None:
    """Return a safe signaling URL, or None when configuration is invalid."""

    raw = LIVEKIT_URL.strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not parsed.hostname
    ):
        return None
    if parsed.scheme == "wss":
        return raw.rstrip("/")
    is_development = os.getenv("APP_ENV", "development").lower() != "production"
    if parsed.scheme == "ws" and is_development and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return raw.rstrip("/")
    return None


def is_configured() -> bool:
    return bool(validated_livekit_url() and LIVEKIT_API_KEY and LIVEKIT_API_SECRET)


async def create_voice_token(campus_code: str = "LAMC") -> dict | None:
    """Create a LiveKit room token and dispatch the campus voice agent.

    The campus code is embedded in the room name prefix so the agent
    server can determine which campus to serve.

    Args:
        campus_code: LACCD campus code (e.g. "LAMC", "ELAC"). Defaults to LAMC.

    Returns dict with {token, url, room_name} or None if unconfigured.
    """
    if not is_configured():
        return None
    livekit_url = validated_livekit_url()
    assert livekit_url is not None

    campus = get_campus(campus_code)

    from livekit.api import AccessToken, VideoGrants

    # Room name encodes campus: "lamc-voice-abc123", "elac-voice-def456"
    room_name = f"{campus.code.lower()}-voice-{uuid4().hex[:12]}"
    identity = f"student-{uuid4().hex[:8]}"

    # Generate participant access token
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_ttl(VOICE_TOKEN_TTL)
        .with_identity(identity)
        .with_name("Student")
        .with_grants(
            VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )

    # Create room and dispatch agent
    try:
        from livekit.api import CreateAgentDispatchRequest, CreateRoomRequest, LiveKitAPI

        api = LiveKitAPI(
            url=livekit_url,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        try:
            await api.room.create_room(
                CreateRoomRequest(name=room_name, empty_timeout=300)
            )
            await api.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(
                    room=room_name,
                    agent_name=VOICE_AGENT_NAME,
                )
            )
        finally:
            await api.aclose()
    except Exception:
        logger.warning(
            "Agent dispatch failed for %s in room %s (agent server may not be running). "
            "Room token still valid -- agent will join when available.",
            VOICE_AGENT_NAME,
            room_name,
            exc_info=True,
        )

    return {
        "token": token.to_jwt(),
        "url": livekit_url,
        "room_name": room_name,
    }
