"""Outbound alerts raised by workflow activity.

One row per (recipient, channel, event). The table is deliberately also the
*log* of what was sent: every dispatch records its subject, body, template
context and delivery outcome, so "why did this person get that mail?" is
answerable from the database rather than from mail-server logs.

Channels are split into rows rather than columns because an alert usually goes
to a person twice — once into the in-app bell (``in_app``) and once by mail
(``email``) — and each half can succeed, fail or be read independently. Only the
email row carries a delivery status worth waiting on; an in-app row is "sent"
the moment it exists.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .base import BaseModel
from .users import User


class Notification(BaseModel):
    """A single alert, addressed to one user on one channel."""

    CHANNEL_CHOICES = [
        ("in_app", _("In-app")),
        ("email", _("Email")),
    ]

    STATUS_CHOICES = [
        # Only email rows wait; in-app rows are written as "sent".
        ("pending", _("Pending")),
        ("sent", _("Sent")),
        ("failed", _("Failed")),
    ]

    KIND_CHOICES = [
        ("workflow-created", _("Workflow created")),
        ("workflow-transition", _("Workflow transitioned")),
        ("referral-created", _("Referral raised")),
        ("referral-responded", _("Referral answered")),
        ("referral-recalled", _("Referral recalled")),
        ("referral-expired", _("Referral expired")),
        ("referral-deadline", _("Referral deadline reminder")),
    ]

    recipient = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="Who is being told.",
    )
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)

    subject = models.CharField(max_length=255)
    body = models.TextField(
        blank=True, help_text="Rendered exactly as it was (or would be) sent."
    )
    #: Site-relative link to the thing the alert is about, for the bell menu.
    url = models.CharField(max_length=500, blank=True)

    # Who or what caused it (null for system-generated alerts such as deadline
    # reminders). ``SET_NULL`` keeps the notification readable after the actor's
    # account is removed, which matters because these rows are an audit trail.
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications_triggered",
        help_text="Triggering user, when there is one.",
    )

    # The target workflow instance, if any. Generic because one table serves
    # every concrete workflow subclass (see AbstractLegislativeWorkflow).
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    #: Variables the message was composed from, kept so a rendered alert can be
    #: explained (and re-rendered) without replaying the workflow.
    context = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(
        blank=True, help_text="Delivery failure, when there is one."
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The bell menu asks for one user's unread count on every page.
            models.Index(fields=["recipient", "read_at"]),
            models.Index(fields=["recipient", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_channel_display()} → {self.recipient}: {self.subject}"

    # -- state --------------------------------------------------------------
    @property
    def is_unread(self) -> bool:
        """
        True for an in-app alert the recipient has not opened yet.

        Channel-aware on purpose: ``read_at`` tracks reading the bell, so an
        email row (a delivery record) is never "unread", however long it sits
        there.
        """
        return self.channel == "in_app" and self.read_at is None

    @property
    def is_email(self) -> bool:
        return self.channel == "email"

    def mark_sent(self):
        """Record a successful delivery."""
        self.status = "sent"
        self.sent_at = timezone.now()
        self.error = ""
        self.save(update_fields=["status", "sent_at", "error", "updated_at"])
        return self

    def mark_failed(self, error):
        """Record a failed delivery; the reason is kept for the log."""
        self.status = "failed"
        self.error = str(error)
        self.save(update_fields=["status", "error", "updated_at"])
        return self

    def mark_read(self):
        """Mark an in-app alert as seen (idempotent)."""
        if self.read_at is not None:
            return self
        self.read_at = timezone.now()
        self.save(update_fields=["read_at", "updated_at"])
        return self
