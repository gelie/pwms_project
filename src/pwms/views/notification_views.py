import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..models import (
    Notification,
)
from ..notifications import (
    preference_rows,
    save_preferences,
)
from ..notifications.context import alerts as alert_context

logger = logging.getLogger(__name__)


ALERT_PAGE_SIZE = 25


def notifications(request):
    """Every in-app alert for the signed-in user, newest first."""
    inbox = Notification.objects.filter(
        recipient=request.user, channel="in_app"
    ).select_related("actor")
    return render(
        request,
        "pwms/notifications.html",
        {
            "alerts": inbox[:ALERT_PAGE_SIZE],
            "alert_total": inbox.count(),
            "unread_count": inbox.filter(read_at__isnull=True).count(),
        },
    )


def notification_open(request, public_id):
    """Open an alert's subject: mark it read, then follow its link."""
    alert = get_object_or_404(Notification, public_id=public_id, recipient=request.user)
    alert.mark_read()
    # Alerts without a target (e.g. an account-level notice) just mark themselves
    # read and leave the reader on the list.
    return redirect(alert.url or "pwms:notifications")


def notification_preferences(request):
    """
    The signed-in user's own mute switches, per alert kind and channel.

    A switch can only turn an alert *off* for this person: who is told is still
    decided by the workflow type's audience rules, and alerts the user owes an
    action on (their turn, a referral to their committee) are always delivered.
    """
    if request.method == "POST":
        save_preferences(request.user, request.POST.keys())
        messages.success(request, "Alert preferences saved.")
        return redirect("pwms:notification_preferences")
    return render(
        request,
        "pwms/notification-preferences.html",
        {"rows": preference_rows(request.user)},
    )


@require_POST
def notifications_read_all(request):
    """Mark every unread alert read. POST-only: reading is a state change."""
    updated = request.user.notifications.filter(
        channel="in_app", read_at__isnull=True
    ).update(read_at=timezone.now())
    if request.headers.get("HX-Request") == "true":
        # The bell's clear button swaps only the bell, so the menu and badge
        # update in place rather than sending the reader to the alerts page.
        return render(
            request,
            "pwms/partials/alerts_dropdown.html",
            alert_context(request),
        )
    if updated:
        messages.success(request, f"Marked {updated} alert(s) as read.")
    return redirect("pwms:notifications")
