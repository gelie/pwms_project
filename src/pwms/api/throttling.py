"""Per-key rate limiting for the external API.

Ninja's built-in throttles key on the caller's IP address, which is the wrong
axis for a partner integration: several clients can share a NAT address and one
client can fan out across many addresses. :class:`ClientRateThrottle` keys on
the authenticated ``ApiClient`` instead and reads the limit from the client row,
so an administrator can raise or lower a single integrator's ceiling without a
deploy.

Counters are fixed one-minute buckets incremented atomically with
``cache.add``/``cache.incr``. With ``REDIS_URL`` configured that makes the limit
exact across gunicorn workers; without it the fallback LocMem cache makes the
limit per process (fine for development and single-worker deployments).

The token endpoints are unauthenticated, so they use an IP-keyed throttle
(:class:`TokenEndpointThrottle`) to blunt credential guessing. Both throttles
fail **open** if the cache itself is unavailable: an outage must not take the
whole API down, and the failure is logged loudly instead.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache
from ninja.throttling import AnonRateThrottle, BaseThrottle

logger = logging.getLogger("pwms.api")

TOKEN_THROTTLE_SCOPE = "api_token"


def _hit(cache_backend, key: str, window_seconds: int) -> int | None:
    """Atomically add one to the bucket and return the new count.

    Returns ``None`` when the cache is unreachable, which callers treat as
    "allow the request". ``add`` is used so two workers starting a fresh bucket
    cannot both initialise it to 1; ``incr`` is atomic on Redis.
    """
    try:
        if cache_backend.add(key, 1, timeout=window_seconds + 1):
            return 1
        try:
            return cache_backend.incr(key)
        except ValueError:
            # The key expired between ``add`` and ``incr``. Start a new bucket;
            # if another worker won that race, increment its value instead.
            if cache_backend.add(key, 1, timeout=window_seconds + 1):
                return 1
            return cache_backend.incr(key)
    except Exception:
        logger.exception("Rate-limit cache operation failed; allowing request")
        return None


class ClientRateThrottle(BaseThrottle):
    """Fixed-window requests-per-minute limit keyed by ``ApiClient``.

    Ninja attaches one throttle instance to every operation, so this class is
    intentionally stateless between calls. ``wait`` returns one full window
    rather than the exact remainder; a client that keeps reconnecting gets
    another 429 once the bucket rolls over.
    """

    cache_format = "api:throttle:%(ident)s:%(bucket)s"
    window_seconds = 60

    def allow_request(self, request) -> bool:
        principal = getattr(request, "auth", None)
        client = getattr(principal, "client", None)
        if client is None:
            # Unauthenticated requests are rejected by auth before throttling
            # (or handled by the token endpoint's own IP throttle).
            return True

        limit = int(client.rate_limit_per_minute or 0)
        if limit <= 0:
            return True

        bucket = int(time.time() // self.window_seconds)
        key = self.cache_format % {"ident": client.client_id, "bucket": bucket}
        count = _hit(cache, key, self.window_seconds)
        return True if count is None else count <= limit

    def wait(self) -> float:
        return float(self.window_seconds)


class TokenEndpointThrottle(AnonRateThrottle):
    """IP-scoped throttle for ``POST /auth/token/`` and its refresh sibling."""

    scope = TOKEN_THROTTLE_SCOPE

    def get_rate(self) -> str:
        return getattr(settings, "EXTERNAL_API_TOKEN_THROTTLE_RATE", "20/min")

    def allow_request(self, request) -> bool:
        if self.rate is None or self.duration is None:
            return True
        ident = self.get_ident(request)
        if ident is None:
            return True

        bucket = int(time.time() // self.duration)
        key = f"api:token-throttle:{ident}:{bucket}"
        count = _hit(self.cache, key, self.duration)
        return True if count is None else count <= self.num_requests

    def wait(self) -> float | None:
        return float(self.duration) if self.duration else None
