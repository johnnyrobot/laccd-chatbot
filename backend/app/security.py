from __future__ import annotations

import hashlib
import ipaddress
import secrets
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request

from app.config import settings


def parse_csv_set(value: str | Iterable[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = value
    return {part.strip() for part in parts if part and part.strip()}


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return ""
    return token.strip()


def _require_static_secret(
    *,
    configured_secret: str | None,
    provided_secret: str,
    surface: str,
) -> None:
    if not configured_secret:
        raise HTTPException(status_code=503, detail=f"{surface} API is not configured")
    if not provided_secret or not secrets.compare_digest(provided_secret, configured_secret):
        raise HTTPException(status_code=401, detail=f"Invalid or missing {surface} API key")


async def require_admin_api_key(
    authorization: str | None = Header(default=None),
    x_admin_api_key: str | None = Header(default=None),
) -> None:
    provided = (x_admin_api_key or "").strip() or _bearer_token(authorization)
    _require_static_secret(
        configured_secret=settings.admin_api_key,
        provided_secret=provided,
        surface="Admin",
    )


async def require_voice_api_key(
    authorization: str | None = Header(default=None),
    x_voice_api_key: str | None = Header(default=None),
) -> None:
    provided = (x_voice_api_key or "").strip() or _bearer_token(authorization)
    _require_static_secret(
        configured_secret=settings.voice_api_key,
        provided_secret=provided,
        surface="Voice",
    )


def is_trusted_browser_origin(request: Request) -> bool:
    """True when a request looks like a first-party browser call: its
    ``Origin`` (or ``Referer``) host matches the ``Host`` it was served from.

    Gates public-but-costly browser endpoints (minting a LiveKit voice token)
    against off-site pages driving them, without a shared secret the browser
    can't keep. Caddy preserves the original ``Host`` and this app only ever
    answers the campus chat domains, so an Origin equal to the Host is a
    legitimate same-site request; anything else — or a missing Origin — is
    rejected. Not a defense against a scripted client forging headers; that's
    what the ``check_voice_call`` daily cap is for.
    """
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    if not host:
        return False
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if not value:
            continue
        origin_host = (urlparse(value).hostname or "").strip().lower()
        if origin_host and origin_host == host:
            return True
    return False


async def require_trusted_origin(request: Request) -> None:
    if not is_trusted_browser_origin(request):
        raise HTTPException(status_code=403, detail="Origin not allowed")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_allowed_bearer_token(token: str, allowed_hashes: Iterable[str]) -> bool:
    digest = token_hash(token)
    return any(secrets.compare_digest(digest, allowed) for allowed in allowed_hashes)


def client_identity_from_request(
    request: Request,
    *,
    trusted_proxy_hosts: Iterable[str] | None = None,
) -> str:
    """Resolve a stable per-client identity for rate limiting.

    ``X-Forwarded-For`` is honored **only** when the socket peer is a
    configured trusted proxy; otherwise a client could forge it. In the prod
    topology the sole trusted hop is Caddy, which *appends* the real client IP
    as the **rightmost** ``X-Forwarded-For`` element. A spoofed value injected
    by the client therefore lands to the left of the real IP.

    So we walk the header right-to-left, skip any hop that is itself a trusted
    proxy, and take the first non-trusted hop — that is the client Caddy saw.
    The hop must parse as an IP address; if it doesn't, we fall back to the
    (trusted) peer rather than trust an attacker-supplied token.
    """
    peer = request.client.host if request.client else "unknown"
    trusted = set(trusted_proxy_hosts) if trusted_proxy_hosts is not None else set(settings.trusted_proxy_hosts)
    forwarded = request.headers.get("x-forwarded-for", "")
    if peer in trusted and forwarded:
        for hop in reversed([part.strip() for part in forwarded.split(",")]):
            if not hop or hop in trusted:
                continue
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                # Rightmost non-trusted hop is not a valid IP (spoofed junk) —
                # don't trust it; collapse to the proxy peer instead.
                return peer
            return hop
    return peer


def resolve_under(root: Path, candidate: str | Path, *, field_name: str = "path") -> Path:
    root_path = Path(root).expanduser().resolve()
    candidate_path = Path(candidate).expanduser()
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    resolved = candidate_path.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must stay inside {root_path}",
        ) from exc
    return resolved
