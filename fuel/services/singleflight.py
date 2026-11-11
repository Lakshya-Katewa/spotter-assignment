"""Make sure concurrent requests for the same uncached value trigger one upstream call.

Two layers:
  * a threading.Lock for the key: threads in this process queue up behind the first;
  * a short cache lock (`cache.add`, atomic in every Django backend): with a shared
    cache (Redis), workers in other processes wait for the leader as well.

After taking the locks the caller re-checks the cache, so whoever waited gets the
leader's result instead of calling upstream again. If the leader dies, its cache
lock expires (`lock_seconds`) and a waiter takes over.
"""

import threading
import time
import uuid

from django.core.cache import cache

_MISSING = object()
# Fixed pool of locks picked by key hash: bounded memory, rare harmless sharing.
_local_locks = [threading.Lock() for _ in range(256)]


class Busy(TimeoutError):
    """Could not get the value or the lock within `wait_seconds`."""


def _local_lock(key):
    return _local_locks[hash(key) % len(_local_locks)]


def get_or_compute(key, compute, ttl, *, lock_seconds=30, wait_seconds=30, poll=0.05):
    """Return (value, from_cache). `compute()` runs at most once at a time per key.

    `ttl` is seconds, or a function of the computed value (e.g. shorter for failures).
    """
    value = cache.get(key, _MISSING)
    if value is not _MISSING:
        return value, True

    deadline = time.monotonic() + wait_seconds
    local = _local_lock(key)
    if not local.acquire(timeout=wait_seconds):
        raise Busy(key)
    try:
        lock_key, token = f"lock:{key}", uuid.uuid4().hex
        while True:
            value = cache.get(key, _MISSING)  # filled while we waited?
            if value is not _MISSING:
                return value, True
            if cache.add(lock_key, token, timeout=lock_seconds):
                break
            if time.monotonic() >= deadline:
                raise Busy(key)
            time.sleep(poll)  # another process is computing it
        try:
            value = compute()
            cache.set(key, value, ttl(value) if callable(ttl) else ttl)
            return value, False
        finally:
            if cache.get(lock_key) == token:
                cache.delete(lock_key)
    finally:
        local.release()
