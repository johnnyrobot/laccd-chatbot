from __future__ import annotations

import pytest

from voice_agent import token_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://voice.example",
        "ws://voice.example",
        "wss://user:pass@voice.example",
        "wss:///missing-host",
        "wss://voice.example?token=secret",
    ],
)
def test_livekit_url_rejects_unsafe_values(monkeypatch, url: str) -> None:
    monkeypatch.setattr(token_endpoint, "LIVEKIT_URL", url)
    monkeypatch.setenv("APP_ENV", "production")
    assert token_endpoint.validated_livekit_url() is None


def test_livekit_url_accepts_secure_origin(monkeypatch) -> None:
    monkeypatch.setattr(token_endpoint, "LIVEKIT_URL", "wss://voice.example/")
    monkeypatch.setenv("APP_ENV", "production")
    assert token_endpoint.validated_livekit_url() == "wss://voice.example"
