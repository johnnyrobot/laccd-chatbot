from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class PaginationLimitExceeded(RuntimeError):
    pass


@dataclass
class PaginationGuard:
    max_pages: int = 100
    max_seconds: float = 600.0
    clock: Callable[[], float] = time.monotonic
    _started: float = field(init=False)
    _fingerprints: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def before_request(self, page: int) -> None:
        if page > self.max_pages:
            raise PaginationLimitExceeded(f"pagination page limit {self.max_pages} exceeded")
        if self.clock() - self._started > self.max_seconds:
            raise PaginationLimitExceeded("pagination deadline exceeded")

    def observe(self, items: list[dict]) -> None:
        payload = json.dumps(items, sort_keys=True, separators=(",", ":"), default=str)
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if fingerprint in self._fingerprints:
            raise PaginationLimitExceeded("pagination repeated page without progress")
        self._fingerprints.add(fingerprint)
