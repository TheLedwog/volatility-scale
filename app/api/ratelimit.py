"""A tiny in-memory, per-IP rate limiter (fixed window).

The backend runs as a single instance (see PRODUCT.md), so an in-process counter
is enough and needs no external store like Redis. Its main jobs are to blunt
brute-force against the admin Basic auth and to stop abuse of the unauthenticated
surface - the API key is still the real gate on /api/v1.

Two buckets, both env-overridable as "MAX/WINDOW_SECONDS":
* admin  (RATELIMIT_ADMIN, default 30/60)  - the browser/admin UI + auth attempts,
  where per-IP is meaningful (a human, or a brute-force script).
* api    (RATELIMIT_API,   default 240/60) - deliberately generous: legitimate
  traffic arrives from the frontend's SERVER (a few shared Vercel IPs), so a tight
  per-IP cap would throttle real users. This is only an abuse backstop.
"""
from __future__ import annotations

import os
import threading
import time


def _parse_limit(env_name: str, default_max: int, default_window: int) -> tuple[int, int]:
    raw = os.environ.get(env_name)
    if raw:
        try:
            n, w = raw.split("/", 1)
            return int(n), int(w)
        except (ValueError, TypeError):
            pass
    return default_max, default_window


ADMIN_LIMIT = _parse_limit("RATELIMIT_ADMIN", 30, 60)
API_LIMIT = _parse_limit("RATELIMIT_API", 240, 60)

_hits: dict[tuple[str, str], tuple[float, int]] = {}
_lock = threading.Lock()
_last_prune = 0.0


def client_ip(request) -> str:
    """Real client IP behind the Railway/CDN proxy (X-Forwarded-For, first hop)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(now: float) -> None:
    """Drop stale IP entries occasionally so the dict can't grow unbounded."""
    global _last_prune
    if now - _last_prune < 300:
        return
    _last_prune = now
    for key in [k for k, (start, _n) in _hits.items() if now - start > 3600]:
        _hits.pop(key, None)


def check(bucket: str, ip: str, max_n: int, window: int) -> tuple[bool, int]:
    """Count one request for (bucket, ip). Returns (allowed, retry_after_seconds)."""
    now = time.time()
    key = (bucket, ip)
    with _lock:
        _prune(now)
        start, count = _hits.get(key, (now, 0))
        if now - start >= window:      # window elapsed -> start a fresh one
            start, count = now, 0
        count += 1
        _hits[key] = (start, count)
        if count > max_n:
            return False, int(window - (now - start)) + 1
        return True, 0
