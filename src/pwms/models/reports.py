"""Saved report shares.

A report is normally an ad-hoc view: the reader picks filters and looks at the
result. Sharing needs that result to stay still while it is *not* being looked
at, so a share pins the filter set to a row and mints an unguessable token for
it. Anyone signed in who holds the link can open that exact report read-only —
without being given the creator's account, and without the report widening to
whatever the *reader* may see.

The row is also the audit trail for the link: when it was minted, who sent it,
to which addresses, how often it was opened, and whether it has since been
revoked or has expired.

A share may additionally **repeat**. With a schedule set, a background job
emails the same report (optionally with the rendered document attached) to the
recipient list on a cadence — a standing "send the oversight report every
Monday" arrangement. ``next_send_at`` is when the next send falls due, and
``last_sent_at`` / ``send_count`` / ``last_error`` record how the last one went.
"""

import calendar
import secrets
from datetime import timedelta

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .base import BaseModel
from .users import User

#: Formats a report can be exported to. Canonical here because ``ReportShare``
#: stores the one it attaches; ``pwms.reporting.exports`` re-exports this as
#: ``ATTACHMENT_CHOICES`` so the two can never drift apart.
ATTACHMENT_FORMAT_CHOICES = [
    ("xlsx", _("Excel workbook (.xlsx)")),
    ("pdf", _("PDF document (.pdf)")),
    ("html", _("Web page (.html)")),
    ("csv", _("Comma-separated values (.csv)")),
]

#: How often a scheduled share repeats.
SCHEDULE_CHOICES = [
    ("none", _("Do not repeat")),
    ("daily", _("Daily")),
    ("weekly", _("Weekly")),
    ("monthly", _("Monthly")),
]


def generate_token():
    """An unguessable, URL-safe share token (~43 characters of entropy)."""
    return secrets.token_urlsafe(32)


def next_occurrence(after, schedule):
    """
    When a share on ``schedule`` next falls due, one interval after ``after``.

    ``None`` for an unscheduled share. ``monthly`` steps the calendar month
    rather than adding 30 days, so a link that lands on the 1st keeps landing on
    the 1st; a day that overshoots a short month clamps to that month's last day
    (31 Jan -> 28 Feb).
    """
    if schedule == "daily":
        return after + timedelta(days=1)
    if schedule == "weekly":
        return after + timedelta(days=7)
    if schedule == "monthly":
        return _add_one_month(after)
    return None


def _add_one_month(moment):
    """``moment`` one calendar month on, clamped to the target month's length."""
    # ``month - 1 + 1`` is just ``month``: the zero-based index of the *next*
    # month, which lets the year roll over with plain integer arithmetic.
    offset = moment.month
    year = moment.year + offset // 12
    month = offset % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


class ReportShare(BaseModel):
    """A read-only, token-addressed view of one report's filter set."""

    token = models.CharField(
        max_length=64,
        unique=True,
        default=generate_token,
        editable=False,
        db_index=True,
        help_text="Unguessable URL token the share is opened with.",
    )
    title = models.CharField(
        max_length=255,
        blank=True,
        help_text="Human label for the share (defaults to the report title).",
    )
    filters = models.JSONField(
        default=dict,
        blank=True,
        help_text="Report filter values the share was created with.",
    )
    # SET_NULL rather than CASCADE: a share is part of the report's audit trail
    # and must survive the departure of whoever minted it (as Notification.actor
    # does). A share with no creator simply stops resolving to any data.
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="report_shares",
        help_text="Who created the share.",
    )
    recipients = models.TextField(
        blank=True,
        help_text="Comma-separated email addresses the link was sent to.",
    )
    message = models.TextField(blank=True, help_text="Note sent alongside the link.")

    # -- scheduled delivery --------------------------------------------------
    schedule = models.CharField(
        max_length=10,
        choices=SCHEDULE_CHOICES,
        default="none",
        help_text="How often to re-email this report; 'none' sends it once.",
    )
    schedule_format = models.CharField(
        max_length=10,
        choices=ATTACHMENT_FORMAT_CHOICES,
        blank=True,
        help_text="Document to attach to each scheduled send (blank = link only).",
    )
    next_send_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the next scheduled send falls due.",
    )
    last_sent_at = models.DateTimeField(
        null=True, blank=True, help_text="When a scheduled send last ran."
    )
    send_count = models.PositiveIntegerField(
        default=0, help_text="How many scheduled sends have run."
    )
    last_error = models.TextField(
        blank=True, help_text="Why the last scheduled send failed, if it did."
    )

    expires_at = models.DateTimeField(
        null=True, blank=True, help_text="When the link stops working (null = never)."
    )
    revoked_at = models.DateTimeField(
        null=True, blank=True, help_text="When the link was withdrawn."
    )

    access_count = models.PositiveIntegerField(default=0)
    last_accessed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # ``token`` already carries a unique index (it is the lookup key).
            models.Index(fields=["created_by", "-created_at"]),
            # The scheduled-delivery job's "due" query.
            models.Index(fields=["schedule", "next_send_at"]),
        ]

    def __str__(self):
        label = self.title or f"Report share {self.public_id}"
        return f"{label} ({'active' if self.is_active else 'inactive'})"

    # -- state --------------------------------------------------------------
    @property
    def is_active(self):
        """True while the link is neither revoked nor past its expiry."""
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    def revoke(self):
        """Withdraw the link. Idempotent."""
        if self.revoked_at is not None:
            return self
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])
        return self

    def record_access(self):
        """Note that the link was opened (best-effort access auditing)."""
        self.access_count += 1
        self.last_accessed_at = timezone.now()
        self.save(update_fields=["access_count", "last_accessed_at", "updated_at"])
        return self

    @property
    def recipient_list(self):
        """``recipients`` split into stripped, non-empty addresses."""
        return [
            address.strip()
            for address in (self.recipients or "").replace(";", ",").split(",")
            if address.strip()
        ]

    # -- scheduling ---------------------------------------------------------
    @property
    def is_scheduled(self):
        """True when the share repeats."""
        return (self.schedule or "none") not in ("", "none")

    def is_due(self, now=None):
        """True when an active, repeating share is ready to be sent."""
        now = now or timezone.now()
        return (
            self.is_scheduled
            and self.is_active
            and self.next_send_at is not None
            and self.next_send_at <= now
        )

    def advance_schedule(self, after=None):
        """
        Move ``next_send_at`` on by one interval, measured from ``after``.

        Returns the new value (``None`` when the share does not repeat) without
        saving, so the caller can write it in the same ``update_fields`` as the
        delivery outcome.
        """
        self.next_send_at = next_occurrence(after or timezone.now(), self.schedule)
        return self.next_send_at

    def record_delivery(self, *, sent, error="", when=None):
        """Record one scheduled send and advance the schedule by an interval."""
        when = when or timezone.now()
        self.last_sent_at = when
        self.send_count = (self.send_count or 0) + 1
        self.last_error = error if not sent else ""
        self.advance_schedule(when)
        self.save(
            update_fields=[
                "last_sent_at",
                "send_count",
                "last_error",
                "next_send_at",
                "updated_at",
            ]
        )
        return self
