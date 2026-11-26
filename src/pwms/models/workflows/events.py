"""Append-only workflow events and the state-transition audit log."""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField

from ..base import BaseModel
from ..users import User


class TransitionLog(models.Model):
    """
    Generic domain audit log for workflow progression.

    ``content_object`` is the concrete workflow instance
    (``InternationalResolution`` today, ``Bill``/``Motion``/``Question`` later),
    so one log table serves every subclass. Entries are written by
    ``AbstractLegislativeWorkflow.perform_transition()``.
    """

    id = models.BigAutoField(primary_key=True)

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    action = models.CharField(max_length=100)  # e.g., 'STATE_TRANSITION', 'REFERRAL'
    from_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )
    to_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-timestamp"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} - {self.action} @ {self.timestamp}"


class EventType(BaseModel):
    """
    Data-driven registry of business-meaningful workflow events.

    A table rather than code choices so administrators can add event types per
    instrument without a migration, and so transition guards
    (``Transition.required_event_types``) can reference the same registry.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(
        default=False,
        help_text="Seeded standard type; treated as read-only in the admin.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkflowEvent(models.Model):
    """
    Append-only domain event log for any concrete workflow instance.

    Complements :class:`TransitionLog`: ``TransitionLog`` records *state
    changes*, while ``WorkflowEvent`` records other lifecycle facts
    ("delegation report document attached", "ATC update published", "referral
    answered"). Rows are immutable — corrections are compensating events —
    which keeps the timeline trustworthy for audit (BR08).

    The instance is referenced through a generic foreign key so one table
    serves every concrete subclass, and ``transition_log`` optionally links an
    event back to the state change that produced it.
    """

    ORIGIN_CHOICES = [
        ("user", _("User")),
        ("system", _("System")),
        ("integration", _("Integration")),
    ]

    id = models.BigAutoField(primary_key=True)

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    event_type = models.ForeignKey(
        EventType,
        on_delete=models.PROTECT,
        related_name="events",
        help_text="What happened.",
    )
    occurred_at = models.DateTimeField(
        default=timezone.now, help_text="When the event happened."
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Who caused the event (null for system-generated events).",
    )
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default="user")
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific structured detail.",
    )
    document_url = models.URLField(
        max_length=2048,
        blank=True,
        # Sized for what actually lands here: the attachment service records an
        # event with ``Attachment.sharepoint_web_url``, which Graph's ``webUrl``
        # pushes past 200 characters routinely. Nothing validates these before the
        # insert, so a tighter column is a 500 rather than a form error.
        help_text="Optional SharePoint / document reference.",
    )
    notes = models.TextField(blank=True)
    transition_log = models.ForeignKey(
        "TransitionLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        help_text="State change that produced this event, when applicable.",
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-occurred_at"]),
            models.Index(fields=["event_type", "-occurred_at"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} - {self.event_type.name} @ {self.occurred_at}"

    # -- payload accessors ---------------------------------------------------
    # The payload is JSON, so it need not carry any particular key: seed_demo_data
    # records document events with no payload at all. Reading a key straight out of
    # it in a template raises ``VariableDoesNotExist`` when that read is a *filter
    # argument* -- ``event.document_url|default:event.payload.web_url`` -- because
    # Django guards the filter's own variable but resolves its arguments through
    # ``Variable.resolve``, which does not catch. These keep the read in Python,
    # where a missing key is an empty string.
    @property
    def payload_name(self):
        """Document name an attachment event records, "" when it has none."""
        return (self.payload or {}).get("name", "")

    @property
    def payload_attachment_id(self):
        """``public_id`` of the document an event names, "" when it has none."""
        return (self.payload or {}).get("attachment_id", "")

    @property
    def payload_web_url(self):
        """SharePoint URL of the document an event names, "" when it has none."""
        return (self.payload or {}).get("web_url", "")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "WorkflowEvent rows are append-only; record a new event instead "
                "of editing this one."
            )
        return super().save(*args, **kwargs)
