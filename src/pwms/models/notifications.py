"""The outbound notification log.

One row per (recipient, channel, event). The table is deliberately also the
*log* of what was sent: every dispatch records its subject, body, template
context and delivery outcome, so "why did this person get that mail?" is
answerable from the database rather than from mail-server logs.

Channels are split into rows rather than columns because an alert usually goes
to a person twice — once into the in-app bell (``in_app``) and once by mail
(``email``) — and each half can succeed, fail or be read independently. Only the
email row carries a delivery status worth waiting on; an in-app row is "sent"
the moment it exists.

The same table is the log for explicit sharing (``report-share`` and
``instrument-share``): those write email rows only, with no bell row, and the
recipient may be an external address with no ``User`` account. ``to_email`` and
``from_email`` snapshot the envelope actually used, so the log stays truthful
after a profile or setting changes.

The email half is delivered by a queued background task for alerts (see
``pwms.notifications.dispatch`` and ``pwms.tasks``), which retries a transient
failure; explicit shares send inline and record one attempt. ``attempts`` and
``error`` are what that looks like from the outside: a row stays ``pending``
with the reason of its last failed attempt, and only becomes ``failed`` once the
attempts are spent.

Two more models configure *who* is told, and ``Notification.reasons`` records
*why* each person was:

* :class:`NotificationRule` — an explicit, per-workflow-type declaration ("on this
  event, tell this role in this group, by this channel"), replacing the old
  per-transition ``notify_roles``;
* :class:`NotificationPreference` — a user's own mute switches. A preference can
  only *narrow* an audience, never widen it, and never silences an alert the user
  owes an action on.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .base import BaseModel
from .users import User

#: Kinds written by explicit sharing. They are not alerts: no bell row is written,
#: they are always sent, and a user cannot mute them.
SHARE_KINDS = frozenset({"report-share", "instrument-share"})

#: Alerts a user may not mute: the committee has to act on a deadline whatever
#: its members' personal settings say.
MANDATORY_KINDS = frozenset({"referral-deadline"})

#: The rule "events" an administrator can attach a :class:`NotificationRule` to.
EVENT_CREATED = "created"
EVENT_TRANSITION = "transition"
EVENT_REFERRAL_CREATED = "referral-created"
EVENT_REFERRAL_CLOSED = "referral-closed"
EVENT_REFERRAL_DEADLINE = "referral-deadline"
EVENT_MEETING_DOCUMENT = "meeting-document"
EVENT_MEETING_ATTENDANCE = "meeting-attendance"

REFERRAL_EVENTS = frozenset(
    {EVENT_REFERRAL_CREATED, EVENT_REFERRAL_CLOSED, EVENT_REFERRAL_DEADLINE}
)

#: Which rule event each alert ``kind`` belongs to.
EVENT_FOR_KIND = {
    "workflow-created": EVENT_CREATED,
    "meeting-created": EVENT_CREATED,
    "workflow-transition": EVENT_TRANSITION,
    "meeting-status": EVENT_TRANSITION,
    "referral-created": EVENT_REFERRAL_CREATED,
    "referral-responded": EVENT_REFERRAL_CLOSED,
    "referral-recalled": EVENT_REFERRAL_CLOSED,
    "referral-expired": EVENT_REFERRAL_CLOSED,
    "referral-deadline": EVENT_REFERRAL_DEADLINE,
    "meeting-document": EVENT_MEETING_DOCUMENT,
    "meeting-minutes": EVENT_MEETING_DOCUMENT,
    "meeting-attendance": EVENT_MEETING_ATTENDANCE,
}


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
        # Meetings (the Events workflow).
        ("meeting-created", _("Meeting created")),
        ("meeting-status", _("Meeting status changed")),
        ("meeting-document", _("Meeting document changed")),
        ("meeting-attendance", _("Meeting attendance recorded")),
        ("meeting-minutes", _("Meeting minutes filed")),
        # Explicit sharing (not alerts: no in-app row is written).
        ("report-share", _("Report shared")),
        ("instrument-share", _("Record shared")),
    ]

    recipient = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Who is being told, when they have an account.",
    )
    #: The address the message was actually sent to. Snapshotted because the
    #: recipient's profile (or an externally typed address) can change later; an
    #: audit log must show the envelope as it was, not as it is now.
    to_email = models.EmailField(
        blank=True, help_text="Envelope address used for this message."
    )
    from_email = models.EmailField(
        blank=True, help_text="Sender address used for this message."
    )
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, db_index=True)

    subject = models.CharField(max_length=1000)
    body = models.TextField(
        blank=True, help_text="Rendered exactly as it was (or would be) sent."
    )
    html_body = models.TextField(
        blank=True,
        help_text=(
            "HTML body sent as the alternative part with ``body``; blank on "
            "in-app rows and on rows written before HTML alerts shipped."
        ),
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

    # The target of the message, if any: a workflow instance or a ReportShare.
    # Generic because one table serves every concrete workflow subclass.
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True, blank=True
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    content_object = GenericForeignKey("content_type", "object_id")

    #: Variables the message was composed from, kept so a rendered alert can be
    #: explained (and re-rendered) without replaying the workflow.
    context = models.JSONField(default=dict, blank=True)
    #: Why this person was told: one entry per audience rule that selected them
    #: (``next-actor``, ``assignee``, ``owner``, ``officer``, ``rule:<id>`` ...).
    #: Empty on rows written before reasons were recorded, and on shares.
    reasons = models.JSONField(
        default=list,
        blank=True,
        help_text="Why the recipient was told (audience rules that selected them).",
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    sent_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(
        default=0,
        help_text="Delivery attempts made so far (the queue retries a failure).",
    )
    error = models.TextField(
        blank=True,
        help_text=(
            "Delivery failure: the last attempt while the row is still pending, "
            "or the reason it was given up on."
        ),
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            # The bell menu asks for one user's unread count on every page.
            models.Index(fields=["recipient", "read_at"]),
            models.Index(fields=["recipient", "-created_at"]),
        ]

    def __str__(self):
        audience = self.to_email or self.recipient or "unknown recipient"
        return f"{self.get_channel_display()} → {audience}: {self.subject}"

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
        self.save(
            update_fields=["status", "sent_at", "error", "attempts", "updated_at"]
        )
        return self

    def mark_failed(self, error):
        """
        Record a delivery that is finally given up on; the reason is kept.

        A *retryable* failure is not this: the row stays ``pending`` with the
        reason in ``error`` until the attempts run out (see ``deliver_email``).
        """
        self.status = "failed"
        self.error = str(error)
        self.save(update_fields=["status", "error", "attempts", "updated_at"])
        return self

    def mark_read(self):
        """Mark an in-app alert as seen (idempotent)."""
        if self.read_at is not None:
            return self
        self.read_at = timezone.now()
        self.save(update_fields=["read_at", "updated_at"])
        return self

    #: The kinds a user can have preferences for: every alert, no share.
    @classmethod
    def alert_kinds(cls):
        """``(value, label)`` for each alert kind (shares excluded)."""
        return [choice for choice in cls.KIND_CHOICES if choice[0] not in SHARE_KINDS]


class NotificationRule(BaseModel):
    """
    One explicit audience declaration for a workflow type.

    "When ``event`` happens on a record of ``workflow_type``, tell the holders of
    ``role`` in the groups ``scope`` resolves to, over ``channel``." A type with no
    rules still has its built-in audience (named people, whoever acts next, the
    officers at the type's ``oversight_channel``); rules *add* to it.

    The ``scope`` is what keeps a role from becoming a blast: ``Role`` is shared
    across the organisation, so a rule always names *where* the role is held.

    ``custodian``
        The type's owning group — the unit that runs the instrument.
    ``referred_group``
        The committee a referral was raised to (referral events only).
    ``host_group``
        A meeting's convening group (meetings only).
    ``instance_groups``
        Every group with access to the record, viewer and referral groups
        included. The widest scope, and so an explicit opt-in.

    Email still only reaches active staff: a rule can put a member (MP) in the
    bell but never in their inbox.
    """

    EVENT_CHOICES = [
        (EVENT_CREATED, _("Record created")),
        (EVENT_TRANSITION, _("Record transitioned")),
        (EVENT_REFERRAL_CREATED, _("Referral raised")),
        (EVENT_REFERRAL_CLOSED, _("Referral answered, recalled or expired")),
        (EVENT_REFERRAL_DEADLINE, _("Referral deadline reminder")),
        (EVENT_MEETING_DOCUMENT, _("Meeting document changed")),
        (EVENT_MEETING_ATTENDANCE, _("Meeting attendance recorded")),
    ]

    SCOPE_CUSTODIAN = "custodian"
    SCOPE_REFERRED_GROUP = "referred_group"
    SCOPE_HOST_GROUP = "host_group"
    SCOPE_INSTANCE_GROUPS = "instance_groups"
    SCOPE_CHOICES = [
        (SCOPE_CUSTODIAN, _("Custodian group (the type's owning group)")),
        (SCOPE_REFERRED_GROUP, _("Referred group (referral events)")),
        (SCOPE_HOST_GROUP, _("Host group (meetings)")),
        (SCOPE_INSTANCE_GROUPS, _("Every group with access to the record")),
    ]

    CHANNEL_IN_APP = "in_app"
    CHANNEL_EMAIL = "email"
    CHANNEL_BOTH = "both"
    CHANNEL_CHOICES = [
        (CHANNEL_IN_APP, _("In-app only")),
        (CHANNEL_EMAIL, _("Email only")),
        (CHANNEL_BOTH, _("In-app and email")),
    ]

    workflow_type = models.ForeignKey(
        "WorkflowType",
        on_delete=models.CASCADE,
        related_name="notification_rules",
    )
    event = models.CharField(max_length=30, choices=EVENT_CHOICES)
    transition = models.ForeignKey(
        "Transition",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notification_rules",
        help_text=(
            "Only for 'Record transitioned': limit the rule to this transition. "
            "Leave empty to apply to every transition."
        ),
    )
    scope = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_CUSTODIAN,
        help_text="Which group(s) the role is looked up in.",
    )
    role = models.ForeignKey(
        "Role",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notification_rules",
        help_text="Holders of this role are told. Leave empty for every member.",
    )
    channel = models.CharField(
        max_length=10, choices=CHANNEL_CHOICES, default=CHANNEL_BOTH
    )
    is_active = models.BooleanField(
        default=True, help_text="Untick to pause the rule without deleting it."
    )

    class Meta:
        ordering = ["workflow_type", "event", "transition", "scope", "role"]
        indexes = [models.Index(fields=["workflow_type", "event"])]

    def __str__(self):
        who = self.role.name if self.role_id else "every member"
        event = self.get_event_display()
        if self.transition_id:
            event = f"{event} ({self.transition.name})"
        return f"{self.workflow_type.name}: {event} → {who} @ {self.scope}"

    def clean(self):
        super().clean()
        errors = {}
        if self.transition_id:
            if self.event != EVENT_TRANSITION:
                errors["transition"] = (
                    "A transition can only be named on a 'Record transitioned' rule."
                )
            elif self.transition.workflow_type_id != self.workflow_type_id:
                errors["transition"] = (
                    "This transition belongs to a different workflow type."
                )
        if self.scope == self.SCOPE_REFERRED_GROUP and self.event not in (
            REFERRAL_EVENTS
        ):
            errors["scope"] = "'Referred group' only makes sense for referral events."
        if self.scope == self.SCOPE_HOST_GROUP and self.event in REFERRAL_EVENTS:
            errors["scope"] = "'Host group' only makes sense for meeting events."
        if errors:
            raise ValidationError(errors)

    @property
    def sends_in_app(self):
        return self.channel in (self.CHANNEL_IN_APP, self.CHANNEL_BOTH)

    @property
    def sends_email(self):
        return self.channel in (self.CHANNEL_EMAIL, self.CHANNEL_BOTH)


class NotificationPreference(BaseModel):
    """
    A user's own switch for one alert kind.

    No row means "as the audience rules say": preferences are exceptions, written
    only when somebody turns something off. They can only *narrow* what a workflow
    type's rules decided — never add a recipient — and they are ignored for an
    alert the user owes an action on (assignee, next actor, referred committee)
    and for the kinds in :data:`MANDATORY_KINDS`.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    kind = models.CharField(max_length=40, choices=Notification.alert_kinds())
    in_app_enabled = models.BooleanField(default=True)
    email_enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["user", "kind"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind"], name="notification_preference_user_kind_uniq"
            )
        ]

    def __str__(self):
        return (
            f"{self.user}: {self.kind} "
            f"(bell {'on' if self.in_app_enabled else 'off'}, "
            f"email {'on' if self.email_enabled else 'off'})"
        )
