"""The audit log for Bill update webhook posts.

A Bill's NEW / EDITS / STATUS_CHANGE updates are posted to a webhook after the
change is saved. This table is the durable record of that exchange: one row for
the outbound attempt and one for the payload the testing endpoint received, with
the HTTP outcome and any error. A row survives the Bill's deletion (the
``bill`` FK is ``SET_NULL``, and the identifying fields are snapshots), so the
trail remains truthful after the record is gone.

The endpoint is a stand-in for an external API. Point
``BILL_UPDATE_ENDPOINT_URL`` elsewhere when the real system is ready; the log
shape does not change.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from .base import BaseModel
from .users import User


class BillUpdateLog(BaseModel):
    """One outbound post or inbound receipt of a Bill update."""

    EVENT_NEW = "NEW"
    EVENT_EDITS = "EDITS"
    EVENT_STATUS_CHANGE = "STATUS_CHANGE"
    #: A receiver's decision sent back to the sender (bi-directional exchange).
    EVENT_REJECTION = "REJECTION"
    EVENT_CHOICES = [
        (EVENT_NEW, _("New bill")),
        (EVENT_EDITS, _("Bill edited")),
        (EVENT_STATUS_CHANGE, _("Status changed")),
        (EVENT_REJECTION, _("Rejected")),
    ]
    #: The events that carry a Bill update and therefore wait for review.
    UPDATE_EVENTS = (EVENT_NEW, EVENT_EDITS, EVENT_STATUS_CHANGE)

    DIRECTION_OUTBOUND = "outbound"
    DIRECTION_INBOUND = "inbound"
    DIRECTION_CHOICES = [
        (DIRECTION_OUTBOUND, _("Outbound")),
        (DIRECTION_INBOUND, _("Inbound")),
    ]

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_RECEIVED = "received"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_SENT, _("Sent")),
        (STATUS_FAILED, _("Failed")),
        (STATUS_RECEIVED, _("Received")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
    ]

    #: The idempotency key shared by an outbound post and its inbound receipt.
    update_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Idempotency key shared by the outbound post and its receipt.",
    )
    bill = models.ForeignKey(
        "pwms.Bill",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="update_logs",
        help_text="The bill, when it still exists.",
    )
    # Snapshots so the log stays readable after the bill is deleted.
    bill_public_id = models.UUIDField(null=True, blank=True, db_index=True)
    bill_number = models.CharField(max_length=50, blank=True)

    event = models.CharField(max_length=20, choices=EVENT_CHOICES)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING
    )

    payload = models.JSONField(
        default=dict, help_text="The exact JSON body sent or received."
    )
    target_url = models.URLField(
        max_length=2048, blank=True, help_text="Endpoint posted to or received at."
    )
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(
        blank=True, help_text="Truncated response body, for diagnosis."
    )
    error = models.TextField(blank=True)

    actor = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bill_update_logs",
        help_text="Who made the change, when known.",
    )
    # Inbound rows only: a received update waits for a reviewer to approve it,
    # so the receiving side adopts changes deliberately rather than implicitly.
    reviewed_at = models.DateTimeField(
        null=True, blank=True, help_text="When an inbound update was approved."
    )
    reviewed_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_bill_update_logs",
        help_text="Who approved the inbound update.",
    )
    # A rejection travels back to the sender as its own message; these fields
    # also stamp the original outbound row once the callback arrives, so the
    # sender's log answers "was this update accepted?" without joining sides.
    rejection_reason = models.TextField(blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by_name = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "bill update log"
        verbose_name_plural = "bill update logs"
        indexes = [
            models.Index(fields=["bill", "-created_at"]),
            models.Index(fields=["event", "direction", "status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["update_id", "direction"],
                condition=models.Q(update_id__isnull=False),
                name="pwms_billupdatelog_update_direction_uniq",
            )
        ]

    def __str__(self):
        return (
            f"{self.get_event_display()} {self.direction} "
            f"({self.bill_number or self.bill_public_id or '—'}) — {self.status}"
        )
