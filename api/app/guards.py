"""Bearer auth and Redis-backed rate limiting (backend spec §7.1, §9).

Redis is best-effort: if it is unreachable, limiting is skipped (and logged) and the
request proceeds. A single-user backend never fails closed on a cache outage.
"""
import hashlib
import hmac
import logging
import time

from fastapi import Request

from .errors import ApiError
from .logs import log

_WINDOW_SECONDS = 60
_last_outage_log = 0.0


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def _note_redis_outage() -> None:
    global _last_outage_log
    now = time.monotonic()
    if now - _last_outage_log > 30:  # don't flood the log while Redis is down
        _last_outage_log = now
        log("rate_limiting_disabled", logging.WARNING, reason="redis unavailable")


async def _bump(redis, key: str) -> tuple[int, int] | None:
    """Fixed-window counter. Returns (count, seconds_to_reset) or None if Redis is down."""
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, _WINDOW_SECONDS, nx=True)
        pipe.ttl(key)
        count, _, ttl = await pipe.execute()
        return count, max(int(ttl), 1)
    except Exception:
        _note_redis_outage()
        return None


def guard(bucket: str, limit_attr: str, count: bool = True):
    """FastAPI dependency: authenticate, then rate-limit by (token hash, bucket).

    `count=False` keeps a route (the stats poll) out of the home-page API-call counter."""

    async def dependency(request: Request) -> None:
        settings = request.app.state.settings
        redis = request.app.state.redis
        presented = _presented_token(request)
        ok = presented is not None and hmac.compare_digest(
            token_digest(presented), request.app.state.token_digest
        )
        if not ok:
            # Brute-force brake: failed attempts share one global counter. A valid token
            # never reaches this branch, so the real client cannot be locked out by it.
            bumped = await _bump(redis, "rl:authfail")
            if bumped and bumped[0] > settings.auth_fail_limit:
                raise ApiError(429, "rate_limited", "too many failed attempts", {"Retry-After": str(bumped[1])})
            raise ApiError(401, "unauthorized", "unauthorized", {"WWW-Authenticate": "Bearer"})

        digest = hashlib.sha256(presented.encode()).hexdigest()[:16]
        bumped = await _bump(redis, f"rl:{digest}:{bucket}")
        if bumped and bumped[0] > getattr(settings, limit_attr):
            raise ApiError(429, "rate_limited", "rate limit exceeded", {"Retry-After": str(bumped[1])})
        if count:
            request.app.state.usage.bump("api")

    return dependency
