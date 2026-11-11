"""Client-side rate limits for the free upstream services (max 1 request/second each).

Both public servers we use state this limit in their usage policies:
  * Nominatim: https://operations.osmfoundation.org/policies/nominatim/
  * OSRM demo: https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server

The spacing comes from timestamps, not from key expiry: some cache backends round
expiry to whole seconds, so a "1-second" key can disappear early (Memcached). For
each call we:
  1. take a short mutex (`cache.add`; its expiry only matters if a holder crashes),
  2. read the time of the last call and wait until MIN_INTERVAL has passed,
  3. record the new call time and release the mutex.
The caller makes its HTTP request right after `acquire` returns. MIN_INTERVAL is
1.05 s, so the gap between recording a time and opening the connection can't
bring two call starts closer than 1 second.

The timestamps live in the cache, so with a shared cache (Redis) the limit holds
across all worker processes on one host (wall-clock time); with the default
in-process cache it holds per process. The whole wait is bounded by `deadline`.
"""

import threading
import time
import uuid

from django.core.cache import cache

MIN_INTERVAL = 1.05  # seconds between call starts: the 1/s limit plus a margin
_MUTEX_SECONDS = 5  # crash recovery only; far longer than a single hold
_POLL = 0.02

_locks = {"nominatim": threading.Lock(), "osrm": threading.Lock()}


class RateLimited(TimeoutError):
    pass


def _keys(service):
    return f"ratelimit:{service}:mutex", f"ratelimit:{service}:last"


def last_call_key(service):
    return _keys(service)[1]


def acquire(service, deadline):
    """Wait for permission to start one request to `service`, before `deadline` (time.monotonic())."""
    lock = _locks[service]  # threads in this process queue here instead of polling the cache
    if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise RateLimited(service)
    try:
        mutex_key, last_key = _keys(service)
        token = uuid.uuid4().hex
        while not cache.add(mutex_key, token, timeout=_MUTEX_SECONDS):  # another process is in step 2
            if time.monotonic() + _POLL >= deadline:
                raise RateLimited(service)
            time.sleep(_POLL)
        try:
            wait = cache.get(last_key, 0.0) + MIN_INTERVAL - time.time()
            if wait > 0:
                if time.monotonic() + wait >= deadline:
                    raise RateLimited(service)
                time.sleep(wait)
            cache.set(last_key, time.time(), timeout=60)
        finally:
            if cache.get(mutex_key) == token:
                cache.delete(mutex_key)
    finally:
        lock.release()
