import ipaddress
import threading
import time
from collections import OrderedDict, deque

# Bounds the memory a flood of distinct clients can use.
MAX_KEYS = 10000

# Number of proxies in front of the app that append to X-Forwarded-For. The
# entry that many places from the right was written by our own proxy; entries
# further left can be written by the client, so they are never trusted.
# CONFIRM on the first real deployment: a hop count that is too high lets a
# client pick its own key, and one that is too low gives everyone behind the
# proxy the same key.
TRUSTED_PROXY_HOPS = 1


class RateLimiter:
    """Sliding window per key, kept in memory only and never logged or saved."""

    def __init__(self, limit, window_s, clock=time.monotonic, max_keys=MAX_KEYS):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._hits = OrderedDict()

    def allow(self, key):
        """Return (allowed, retry_after_s); retry_after_s is 0 when allowed."""
        with self._lock:
            now = self._clock()
            self._drop_expired(now)
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= self._max_keys:
                    self._hits.popitem(last=False)
                hits = self._hits[key] = deque()
            else:
                self._hits.move_to_end(key)
            if len(hits) >= self.limit:
                return False, max(1, int(hits[0] + self.window_s - now) + 1)
            hits.append(now)
            return True, 0

    def _drop_expired(self, now):
        # Oldest keys sit first; stop at the first one that still has a live hit.
        while self._hits:
            key, hits = next(iter(self._hits.items()))
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if hits:
                break
            del self._hits[key]

    def size(self):
        with self._lock:
            return len(self._hits)

    def reset(self):
        with self._lock:
            self._hits.clear()


def client_key(forwarded_for, host, hops=TRUSTED_PROXY_HOPS):
    """Pick the key for one request from the proxy header or the connection host."""
    fallback = host or "unknown"
    if not forwarded_for or not isinstance(forwarded_for, str):
        return fallback
    entries = [part.strip() for part in forwarded_for.split(",")]
    if hops < 1 or len(entries) < hops:
        return fallback
    chosen = entries[-hops]
    try:
        return str(ipaddress.ip_address(chosen))
    except ValueError:
        return fallback
