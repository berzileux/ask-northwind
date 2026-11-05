import threading
import time

# Long enough for a per-minute provider quota to refill, short enough that a
# visitor is not locked out for long.
COOLDOWN_S = 60.0


class QuotaCooldown:
    """Remembers that the provider quota is used up, so no call is made meanwhile.

    The state lives in this process only: two instances of the service do not
    share it, so each one finds out about an exhausted quota on its own.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0

    def start(self, seconds):
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)

    def remaining(self):
        with self._lock:
            return max(0.0, self._until - self._clock())

    def active(self):
        return self.remaining() > 0.0

    def reset(self):
        with self._lock:
            self._until = 0.0


# One shared instance per process.
COOLDOWN = QuotaCooldown()
