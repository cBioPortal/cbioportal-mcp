"""Small, thread-safe TTL caches for successful metadata responses.

Schema listings and generated study guides only change when the database is
refreshed (the daily clone job), so an hour of staleness is acceptable.
Set CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS=0 to disable caching.
"""

import logging
import math
import os
import time
from collections import OrderedDict
from copy import deepcopy
from threading import Lock

logger = logging.getLogger(__name__)

DEFAULT_METADATA_CACHE_TTL_SECONDS = 3600.0


def _ttl_from_env() -> float:
    raw = os.getenv("CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS")
    if raw is None:
        return DEFAULT_METADATA_CACHE_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError:
        ttl = math.nan
    # Reject nan (entries would never expire: `age >= nan` is always False),
    # inf, and negatives; 0 is the documented way to disable the cache.
    if math.isfinite(ttl) and ttl >= 0:
        return ttl
    logger.warning(
        "Invalid CBIOPORTAL_MCP_METADATA_CACHE_TTL_SECONDS=%r; using %s",
        raw,
        DEFAULT_METADATA_CACHE_TTL_SECONDS,
    )
    return DEFAULT_METADATA_CACHE_TTL_SECONDS


METADATA_CACHE_TTL_SECONDS = _ttl_from_env()


def _now() -> float:
    """Monotonic clock; a seam so tests can advance time without patching `time`."""
    return time.monotonic()


class MetadataCache:
    """Bounded LRU with TTL; values are copied in and out so callers can't mutate them.

    Callers only put() successful responses, so errors are never cached.
    TTL <= 0 disables caching.
    """

    def __init__(self, maxsize=256):
        self._entries = OrderedDict()
        self._lock = Lock()
        self._maxsize = maxsize

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            fetched_at, value = entry
            if _now() - fetched_at >= METADATA_CACHE_TTL_SECONDS:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
        # Stored values are private copies that are never mutated, so the
        # (potentially large) copy doesn't need to hold up other threads.
        return deepcopy(value)

    def put(self, key, value):
        if METADATA_CACHE_TTL_SECONDS <= 0:
            return
        value = deepcopy(value)
        with self._lock:
            self._entries[key] = (_now(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self):
        with self._lock:
            self._entries.clear()
