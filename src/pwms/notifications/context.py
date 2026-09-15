"""Template context for the alert bell in the site chrome.

Kept apart from ``dispatch`` so the navigation can import it without dragging in
the mail machinery, and apart from ``pwms.navigation`` because that module is
about menu entries.
"""

from __future__ import annotations

from ..models import Notification

#: How many alerts the bell dropdown lists.
RECENT_ALERTS = 5


def alerts(request):
    """
    Unread count and the newest in-app alerts for the signed-in user.

    Only ``in_app`` rows are considered: the email rows for the same event are
    the delivery log, and showing them here would double every alert.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"unread_alert_count": 0, "recent_alerts": []}

    inbox = Notification.objects.filter(recipient=user, channel="in_app")
    return {
        "unread_alert_count": inbox.filter(read_at__isnull=True).count(),
        "recent_alerts": list(inbox.order_by("-created_at")[:RECENT_ALERTS]),
    }
