"""Rate limits for the API.

The credential endpoint is the one anonymous, unauthenticated-write surface in
the API, so it is the one throttled today: :class:`TokenEndpointThrottle` caps a
``POST /api/auth/token/`` caller **by client IP** at
``REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["token"]`` (``TOKEN_THROTTLE_RATE``,
default ``20/min``). That blunts scripted password grinding without locking out an
office of people who share one NAT address.

Only the token endpoint opts in (it sets ``throttle_classes`` itself); the other
endpoints are authenticated and carry no throttle. The counters live in Django's
cache, which is the per-process ``LocMemCache`` until ``CACHES`` points at a
shared store — so behind several gunicorn workers each worker counts separately.
Point ``CACHES`` at Redis/Memcached if the limit must be exact across workers.
"""

from rest_framework.throttling import AnonRateThrottle


class TokenEndpointThrottle(AnonRateThrottle):
    """IP-scoped limit for the anonymous token-obtain endpoint.

    ``AnonRateThrottle`` only counts unauthenticated callers (an authenticated
    request has no cache key and is never throttled), which is exactly the
    population that hits ``POST /api/auth/token/``.
    """

    scope = "token"
