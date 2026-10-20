"""Small in-process TTL cache. SoundCloud signed URLs expire, so entries must too."""

from __future__ import annotations

import threading
import time


class TTLCache[T]:
    """Process-local cache. A threading lock keeps it usable from any event loop."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, T]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> T | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                del self._items[key]
                return None
            return value

    def set(self, key: str, value: T, ttl: float) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
