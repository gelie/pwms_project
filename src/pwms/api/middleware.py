"""Usage logging for the external API.

Ninja has no "after response" hook, and the token endpoints run before a client
is authenticated, so a small Django middleware records every request under the
API prefix after the response is produced. It writes one append-only
:class:`~pwms.api.models.ApiUsageLog` row, including throttled (429)
requests, and updates the client's ``last_used_at``.

Logging must never take the API down with it: failures are reported to the
``pwms.api`` logger and swallowed.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.utils import timezone

from .models import ApiClient, ApiUsageLog

logger = logging.getLogger("pwms.api")


class ExternalApiUsageLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.prefix = getattr(settings, "EXTERNAL_API_PREFIX", "/api/v1/")

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        if self._is_api(request):
            self._record(request, response, (time.monotonic() - started) * 1000)
        return response

    def _is_api(self, request) -> bool:
        return request.path_info.startswith(self.prefix)

    def _record(self, request, response, duration_ms: float) -> None:
        try:
            client = getattr(request, "api_client", None)
            if client is None:
                principal = getattr(request, "auth", None)
                client = getattr(principal, "client", None)

            match = getattr(request, "resolver_match", None)
            endpoint = getattr(match, "url_name", "") if match else ""
            status_code = getattr(response, "status_code", 0)

            ApiUsageLog.objects.create(
                client=client,
                method=request.method or "",
                path=request.path_info[:500],
                endpoint=(endpoint or "")[:200],
                status_code=status_code,
                duration_ms=max(0, int(duration_ms)),
                remote_addr=(request.META.get("REMOTE_ADDR") or "")[:45],
                user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:300],
                was_throttled=status_code == 429,
            )
            if client is not None:
                ApiClient.objects.filter(pk=client.pk).update(
                    last_used_at=timezone.now()
                )
        except Exception:
            logger.exception("Failed to record external API usage")
