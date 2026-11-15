import time
from threading import Lock
from typing import Any, Optional

class TTLCache:
    """
    Lightweight, thread-safe in-memory cache with Time-To-Live (TTL).
    Zero external dependencies, minimal memory footprint (<1MB).

    Bounded: expired entries are swept on write, and the store is capped
    at ``max_items`` (oldest first). Previously there was no cap and
    expired entries were only removed when individually read, so the
    assistant cache — keyed by arbitrary user questions — could grow
    without limit on a 512MB Render instance.
    """
    def __init__(self, default_ttl_seconds: int = 300, max_items: int = 200):
        self.default_ttl = default_ttl_seconds
        self.max_items = max_items
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.time() < expires_at:
                    return value
                # Expired
                del self._store[key]
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            now = time.time()
            # Sweep expired entries first, then drop oldest if still over cap.
            expired = [k for k, (_, exp) in self._store.items() if exp <= now]
            for k in expired:
                del self._store[k]
            while len(self._store) >= self.max_items:
                self._store.pop(next(iter(self._store)))
            lifespan = ttl if ttl is not None else self.default_ttl
            self._store[key] = (value, now + lifespan)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            active_keys = [k for k, (_, exp) in self._store.items() if exp > now]
            return {
                "active_items": len(active_keys),
                "total_stored": len(self._store)
            }

# Reusable cache singletons (DRY principle)
dashboard_cache = TTLCache(default_ttl_seconds=60)    # 60s cache for fast dashboard reloads
assistant_cache = TTLCache(default_ttl_seconds=900)   # 15m cache for repeated AI queries

def invalidate_inbox_caches():
    """Clear cached data when new emails are synced."""
    dashboard_cache.clear()
    assistant_cache.clear()
