from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CacheEntry:
    value: Any
    expires_at: Optional[float]


class CacheManager:
    """Simple in-memory cache manager with optional expiration."""

    def __init__(self) -> None:
        self._store: Dict[str, CacheEntry] = {}

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        self._store[key] = CacheEntry(value=value, expires_at=expires_at)

    def get(self, key: str) -> Any:
        entry = self._store.get(key)
        if not entry:
            return None
        if entry.expires_at and entry.expires_at < time.time():
            self._store.pop(key, None)
            return None
        return entry.value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def cleanup(self) -> None:
        now = time.time()
        expired_keys = [key for key, entry in self._store.items() if entry.expires_at and entry.expires_at < now]
        for key in expired_keys:
            self._store.pop(key, None)









