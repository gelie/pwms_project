"""Typed referrals and notes attached to workflow instances."""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..base import BaseModel
from .events import EventType, WorkflowEvent


class WorkflowReferral(BaseModel):
    """
    A referral of a workflow instance to a parliamentary group/committee.

    Replaces the old ``referred_to_groups`` M2M, which could not carry who
    referred, when, to whom, by when, or the response. Creation is gated by
    ``State.allows_referrals`` (see ``AbstractLegislativeWorkflow.refer()``).

    Creation and lifecycle actions (``respond()`` / ``recall()`` /
    ``mark_expired()``) emit ``referral-*`` :class:`WorkflowEvent` rows, so
    every referral lands on the instance timeline automatically.

    A referral also carries the referred group's access to the record: while it is
    open the group may view and edit the record — enough to read it and contribute
    the answer — and after it closes the group keeps **view** for audit and
    reporting. That grant is materialised as a :class:`WorkflowGroupAccess` row and
    kept in step from here (see
    ``AbstractLegislativeWorkflow.sync_referral_access``).
    """

    STATUS_CHOICES = [
        ("open", _("Open")),
        ("responded", _("Responded")),
        ("recalled", _("Recalled")),
        ("expired", _("Expired")),
        ("cancelled", _("Cancelled")),
    ]

    #: Status -> event type slug emitted on the transition into that status.
    STATUS_EVENT_SLUGS = {
        "responded": "referral-responded",
        "recalled": "referral-recalled",
        "expired": "referral-expired",
    }

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    referred_to = models.ForeignKey(
        "Group",
        on_delete=models.PROTECT,
        related_name="workflow_referrals",
        help_text="Committee / group asked to consider the workflow.",
    )
    referred_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_made",
    )
    referred_at = models.DateTimeField(default=timezone.now)
    due_date = models.DateTimeField(
        null=True, blank=True, help_text="Response deadline for the referral."
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")

    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_responded",
    )
    response_document_url = models.URLField(max_length=2048, blank=True)
    response_notes = models.TextField(blank=True)

    recalled_at = models.DateTimeField(null=True, blank=True)
    recalled_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_recalled",
    )
    recall_reason = models.TextField(blank=True)

    deadline_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last deadline reminder sent (for the scheduled job).",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-referred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-referred_at"]),
            models.Index(fields=["status", "due_date"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} → {self.referred_to.name} ({self.get_status_display()})"

    # -- lifecycle actions --------------------------------------------------
    @property
    def is_open(self):
        return self.status == "open"

    @property
    def is_overdue(self):
        return bool(self.is_open and self.due_date and self.due_date < timezone.now())

    def _alert(self, kind, *, actor=None):
        """
        Record the alert for a lifecycle change.

        Imported lazily because the notifications service imports these models,
        and a module-level import would close the loop.

        Only these explicit lifecycle methods raise alerts — not ``save()`` — so
        creating a referral row (a fixture, an import, the admin) does not mail
        anyone.
        """
        from ...notifications import notify_referral_closed

        notify_referral_closed(self, kind, actor=actor)

    def respond(self, *, responded_by=None, document_url="", notes=""):
        """Mark the referral answered (emits ``referral-responded``)."""
        if not self.is_open:
            raise ValidationError("Only open referrals can be responded to.")
        self.status = "responded"
        self.responded_at = timezone.now()
        self.responded_by = responded_by
        if document_url:
            self.response_document_url = document_url
        if notes:
            self.response_notes = notes
        self.save()
        self._alert("referral-responded", actor=responded_by)
        return self

    def recall(self, *, recalled_by=None, reason=""):
        """Withdraw the referral (emits ``referral-recalled``)."""
        if not self.is_open:
            raise ValidationError("Only open referrals can be recalled.")
        self.status = "recalled"
        self.recalled_at = timezone.now()
        self.recalled_by = recalled_by
        if reason:
            self.recall_reason = reason
        self.save()
        self._alert("referral-recalled", actor=recalled_by)
        return self

    def mark_expired(self):
        """Mark an overdue open referral expired (emits ``referral-expired``)."""
        if not self.is_open:
            return self
        self.status = "expired"
        self.save()
        # No actor: the scheduled deadline job is what expired it.
        self._alert("referral-expired")
        return self

    # -- event emission -----------------------------------------------------
    def _emit_event(self, slug, *, actor=None, payload=None, notes=""):
        event_type = EventType.objects.filter(slug=slug).first()
        if event_type is None:  # registry entry removed — skip silently
            return None
        return WorkflowEvent.objects.create(
            content_type_id=self.content_type_id,
            object_id=self.object_id,
            event_type=event_type,
            actor=actor,
            origin="user" if actor else "system",
            payload=payload or {},
            notes=notes,
        )

    def save(self, *args, **kwargs):
        created = self._state.adding
        previous = None
        if not created:
            previous = (
                WorkflowReferral.objects.filter(pk=self.pk)
                .values("status", "referred_to_id")
                .first()
            )
        super().save(*args, **kwargs)

        if created:
            self._emit_event(
                "referral-created",
                actor=self.referred_by,
                payload={"referred_to": self.referred_to.name},
            )
        elif previous and previous["status"] != self.status:
            slug = self.STATUS_EVENT_SLUGS.get(self.status)
            if slug:
                self._emit_event(
                    slug,
                    actor=self.responded_by or self.recalled_by,
                    payload={"status": self.status},
                )

        # The referral is what grants the referred group its access, so keep that
        # grant in step — including for a group a reassignment moved it away from.
        self._sync_access(self.referred_to_id)
        if previous and previous["referred_to_id"] != self.referred_to_id:
            self._sync_access(previous["referred_to_id"])

    def delete(self, *args, **kwargs):
        """
        Drop this referral, then let the referred group's access fall back.

        The grant is granted for the referral's lifetime, so removing the row has
        to re-derive it: a purely referral-driven grant goes with it, while an
        organic one is only narrowed back to what it held before. (A bulk
        ``queryset.delete()`` bypasses this, as it always does.)
        """
        group_id = self.referred_to_id
        instance = self.content_object
        result = super().delete(*args, **kwargs)
        if instance is not None:
            instance.sync_referral_access(group_id)
        return result

    def _sync_access(self, group_id):
        """Ask the instance to re-derive ``group_id``'s access from its referrals."""
        instance = self.content_object
        if instance is not None:
            instance.sync_referral_access(group_id)


class WorkflowNote(BaseModel):
    """
    A dated note someone recorded against a workflow instance.

    Notes are rows rather than a column on the record, so each one keeps its own
    author and timestamp, and so any instrument can carry them without every
    workflow type growing a ``notes`` field. Where a type does have a ``notes``
    column it stays the BRS attribute it documents (BR02.3.13), edited with the
    record and shown above the log.

    There is no lifecycle here — unlike a referral, a note is written, read and
    (if recorded in error) deleted. Nothing about it is a state change, so no
    :class:`WorkflowEvent` is emitted and the timeline is unaffected.
    """

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    author = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_notes",
        help_text="Who recorded the note.",
    )
    body = models.TextField(
        help_text="What was noted: a decision, a follow-up, a conversation.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-created_at"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"Note on {label}"
