"""Simple in-memory daily rate limiter for Gemini API usage.

Tracks request counts per service (text chat, voice, fallback) and resets at midnight UTC.
Not persistent across container restarts — this is intentional to keep it simple.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from time import monotonic

from app.config import settings

logger = logging.getLogger(__name__)

# Default daily limits. These are the *defaults*; the actual values are now
# env-configurable knobs (TEXT_CHAT_DAILY_LIMIT / VOICE_CALL_DAILY_LIMIT /
# FALLBACK_DAILY_LIMIT, wired via config.py into the singleton below). The
# conservative default (~$1.85/day of Gemini spend) is the tight pilot budget;
# raise the env vars to scale up without a code change.
TEXT_CHAT_DAILY_LIMIT = 2000
VOICE_CALL_DAILY_LIMIT = 20
FALLBACK_DAILY_LIMIT = 500  # OpenRouter free tier fallback

# Hard ceiling on distinct client keys tracked for per-client chat limiting, so
# a flood of unique client identities can't grow the per-client maps without
# bound (memory-exhaustion DoS). Well above any plausible real daily client
# count for a pilot; when exceeded we FIFO-evict the oldest-seen client.
CHAT_CLIENT_MAX_KEYS = 50_000


class DailyRateLimiter:
    def __init__(
        self,
        *,
        text_daily_limit: int = TEXT_CHAT_DAILY_LIMIT,
        voice_daily_limit: int = VOICE_CALL_DAILY_LIMIT,
        fallback_daily_limit: int = FALLBACK_DAILY_LIMIT,
        chat_per_client_daily_limit: int | None = None,
        chat_per_client_minute_limit: int | None = None,
        voice_per_client_daily_limit: int | None = None,
        chat_client_max_keys: int = CHAT_CLIENT_MAX_KEYS,
    ) -> None:
        self._lock = threading.Lock()
        self._text_daily_limit = text_daily_limit
        self._voice_daily_limit = voice_daily_limit
        self._fallback_daily_limit = fallback_daily_limit
        self._chat_per_client_daily_limit = chat_per_client_daily_limit
        self._chat_per_client_minute_limit = chat_per_client_minute_limit
        self._voice_per_client_daily_limit = voice_per_client_daily_limit
        self._chat_client_max_keys = max(1, chat_client_max_keys)
        self._text_count = 0
        self._voice_count = 0
        self._fallback_count = 0
        self._text_client_counts: dict[str, int] = {}
        self._text_client_windows: dict[str, deque[float]] = {}
        self._voice_client_counts: dict[str, int] = {}
        self._current_date: str = self._today()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_if_new_day(self) -> None:
        today = self._today()
        if today != self._current_date:
            logger.info(
                "Daily rate limiter reset: text=%d/%d, voice=%d/%d, fallback=%d/%d (was %s, now %s)",
                self._text_count, self._text_daily_limit,
                self._voice_count, self._voice_daily_limit,
                self._fallback_count, self._fallback_daily_limit,
                self._current_date, today,
            )
            self._text_count = 0
            self._voice_count = 0
            self._fallback_count = 0
            self._text_client_counts.clear()
            self._text_client_windows.clear()
            self._voice_client_counts.clear()
            self._current_date = today

    def _evict_text_clients_if_needed(self) -> None:
        """Bound the per-client chat maps to ``_chat_client_max_keys`` distinct
        keys, FIFO-evicting the oldest-inserted client. Python dicts preserve
        insertion order and re-assigning an existing key doesn't move it, so the
        first key is the least-recently-first-seen. Prevents unbounded growth
        from a flood of distinct client identities (memory-exhaustion DoS)."""
        while len(self._text_client_counts) > self._chat_client_max_keys:
            oldest = next(iter(self._text_client_counts))
            del self._text_client_counts[oldest]
            self._text_client_windows.pop(oldest, None)
        while len(self._text_client_windows) > self._chat_client_max_keys:
            oldest = next(iter(self._text_client_windows))
            del self._text_client_windows[oldest]

    def _check_client_text_limits(self, client_id: str) -> bool:
        if self._chat_per_client_daily_limit is not None:
            current = self._text_client_counts.get(client_id, 0)
            if current >= self._chat_per_client_daily_limit:
                return False
        if self._chat_per_client_minute_limit is not None:
            now = monotonic()
            bucket = self._text_client_windows.setdefault(client_id, deque())
            while bucket and now - bucket[0] > 60.0:
                bucket.popleft()
            if len(bucket) >= self._chat_per_client_minute_limit:
                return False
            bucket.append(now)
        if self._chat_per_client_daily_limit is not None:
            self._text_client_counts[client_id] = self._text_client_counts.get(client_id, 0) + 1
        self._evict_text_clients_if_needed()
        return True

    def check_text_chat(self, client_id: str | None = None) -> bool:
        """Returns True if a Gemini text chat request is allowed."""
        with self._lock:
            self._reset_if_new_day()
            if self._text_count >= self._text_daily_limit:
                return False
            if client_id and not self._check_client_text_limits(client_id):
                return False
            self._text_count += 1
            return True

    def refund_text_chat(self, client_id: str | None = None) -> None:
        """Return a text-chat slot reserved by ``check_text_chat`` when the
        request turned out not to spend Gemini budget — e.g. a guardrail
        refusal, which never reaches a generation call. Keeps off-topic
        refusals from exhausting the daily budget (PRD §5.6)."""
        with self._lock:
            self._reset_if_new_day()
            if self._text_count > 0:
                self._text_count -= 1
            if client_id and self._chat_per_client_daily_limit is not None:
                current = self._text_client_counts.get(client_id, 0)
                if current <= 1:
                    self._text_client_counts.pop(client_id, None)
                else:
                    self._text_client_counts[client_id] = current - 1
            if client_id and self._chat_per_client_minute_limit is not None:
                bucket = self._text_client_windows.get(client_id)
                if bucket:
                    bucket.pop()
                    if not bucket:
                        self._text_client_windows.pop(client_id, None)

    def check_voice_call(self, client_id: str | None = None) -> bool:
        """Returns True if a voice call is allowed.

        Enforces both the global daily cap and, when ``client_id`` is supplied
        and a per-client voice limit is configured, a per-client daily cap so a
        single actor can't drain every campus's LiveKit token budget. The
        per-client map is naturally bounded by the (small) global daily cap:
        entries are only written on a globally-allowed call."""
        with self._lock:
            self._reset_if_new_day()
            if self._voice_count >= self._voice_daily_limit:
                return False
            if (
                client_id
                and self._voice_per_client_daily_limit is not None
                and self._voice_client_counts.get(client_id, 0) >= self._voice_per_client_daily_limit
            ):
                return False
            self._voice_count += 1
            if client_id and self._voice_per_client_daily_limit is not None:
                self._voice_client_counts[client_id] = self._voice_client_counts.get(client_id, 0) + 1
            return True

    def check_fallback(self) -> bool:
        """Returns True if an OpenRouter fallback request is allowed."""
        with self._lock:
            self._reset_if_new_day()
            if self._fallback_count >= self._fallback_daily_limit:
                return False
            self._fallback_count += 1
            return True

    def get_status(self) -> dict:
        """Return current usage counts and limits."""
        with self._lock:
            self._reset_if_new_day()
            return {
                "text_chat": {
                    "used": self._text_count,
                    "limit": self._text_daily_limit,
                    "remaining": max(0, self._text_daily_limit - self._text_count),
                },
                "voice_call": {
                    "used": self._voice_count,
                    "limit": self._voice_daily_limit,
                    "remaining": max(0, self._voice_daily_limit - self._voice_count),
                },
                "fallback": {
                    "used": self._fallback_count,
                    "limit": self._fallback_daily_limit,
                    "remaining": max(0, self._fallback_daily_limit - self._fallback_count),
                },
                "date": self._current_date,
            }


# Module-level singleton. Global daily caps come from env (config.py) so they
# can be retuned to the budget without a code change; they default to the
# constants above.
rate_limiter = DailyRateLimiter(
    text_daily_limit=settings.text_chat_daily_limit,
    voice_daily_limit=settings.voice_call_daily_limit,
    fallback_daily_limit=settings.fallback_daily_limit,
    chat_per_client_daily_limit=settings.chat_rate_limit_per_day_per_client,
    chat_per_client_minute_limit=settings.chat_rate_limit_per_minute,
    voice_per_client_daily_limit=settings.voice_rate_limit_per_day_per_client,
)
