from __future__ import annotations

from time import monotonic
from typing import Hashable


class InMemoryCooldowns:
    """Small process-local cooldown registry for user and chat actions."""

    def __init__(self, *, max_entries: int = 4096) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._last_used: dict[Hashable, float] = {}

    def allow(self, key: Hashable, seconds: float, *, now: float | None = None) -> bool:
        if seconds < 0:
            raise ValueError("cooldown duration cannot be negative")
        if seconds == 0:
            return True

        current = monotonic() if now is None else now
        last_used = self._last_used.get(key)
        if last_used is not None and current - last_used < seconds:
            return False

        self._last_used[key] = current
        self._prune(current, seconds)
        return True

    def _prune(self, now: float, seconds: float) -> None:
        if len(self._last_used) <= self.max_entries:
            return

        expired_before = now - max(seconds, 60.0)
        self._last_used = {
            key: timestamp
            for key, timestamp in self._last_used.items()
            if timestamp >= expired_before
        }
        if len(self._last_used) > self.max_entries:
            self._last_used = dict(
                sorted(self._last_used.items(), key=lambda item: item[1], reverse=True)[: self.max_entries]
            )
