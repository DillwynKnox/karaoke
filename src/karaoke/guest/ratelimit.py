"""Token buckets for the public guest page.

Every limit is checked against the guest cookie and against the client IP, so neither
clearing the cookie nor sharing one gets somebody past it.
"""

import threading
import time

# Idle buckets are dropped once in a while so a long evening does not grow memory.
CLEANUP_INTERVAL = 300


class RateLimiter:
    def __init__(self):
        self._buckets = {}  # (name, key) -> [tokens, last_refill]
        self._lock = threading.Lock()
        self._last_cleanup = time.time()

    def allow(self, name, keys, limit, window):
        """True if all of these keys have a token left for `limit` requests per `window`
        seconds. Tokens are only spent when every key can pay."""
        if limit <= 0:
            return False
        now = time.time()
        with self._lock:
            self._cleanup(now, window)
            buckets = [self._refill(name, key, limit, window, now) for key in keys]
            if any(bucket[0] < 1 for bucket in buckets):
                return False
            for bucket in buckets:
                bucket[0] -= 1
            return True

    def _refill(self, name, key, limit, window, now):
        bucket = self._buckets.get((name, key))
        if bucket is None:
            bucket = self._buckets[(name, key)] = [float(limit), now]
            return bucket
        tokens, last = bucket
        bucket[0] = min(float(limit), tokens + (now - last) * limit / window)
        bucket[1] = now
        return bucket

    def _cleanup(self, now, window):
        """Forgets buckets that have been full and untouched for a while. Call with the lock."""
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        stale = [k for k, (_, last) in self._buckets.items() if now - last > max(CLEANUP_INTERVAL, window * 2)]
        for key in stale:
            del self._buckets[key]
