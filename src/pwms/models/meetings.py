"""Meetings: the parliamentary Events workflow.

A *meeting* is a concrete workflow instrument (it subclasses
:class:`~pwms.models.workflows.AbstractLegislativeWorkflow`), so it inherits the
whole platform's machinery for free:

* a reusable state machine (:class:`WorkflowType` → :class:`State` /
  :class:`Transition`) drives the meeting lifecycle
  (*Scheduled → In Progress → Completed*, with *Postponed* and *Cancelled*
  branches), and moving a meeting on writes a ``TransitionLog`` and alerts its
  stakeholders;
* per-instance RBAC through :class:`WorkflowGroupAccess`;
* append-only domain events (:class:`WorkflowEvent`) and a create/update/delete
  ``auditlog`` trail;
* SharePoint documents through :class:`~pwms.models.attachments.Attachment` — the
  agenda, the minutes and any supporting papers are attachments with a type.

Two things are modelled here beyond the shared schema:

* :class:`Meeting` — the scheduling detail a parliamentary meeting carries
  (type, when, where, who chairs it, the committee/House that convenes it), the
  different **meeting types** the institution runs (House Plenary, committee,
  administration, …), and the agenda/minutes helpers layered on attachments;
* :class:`MeetingAttendance` — the invitee/attendance register. Each row is one
  person's place at one meeting, carrying both their RSVP and the attendance
  recorded during or after the sitting, with who recorded it and from where, so
  the register is auditable rather than a snapshot.

Attendance is deliberately a child row rather than an M2M: it has its own
lifecycle (invited → responded → attended) and must keep its own audit trail
(see ``PwmsConfig.ready()``), exactly as ``DelegationParticipant`` does.
"""

from __future__ import annotations

from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .base import BaseModel
from .users import User
from .workflows import AbstractLegislativeWorkflow

#: ``EventType`` slugs the meeting workflow appends to a meeting's event log.
EVENT_ATTENDANCE_RECORDED = "meeting-attendance-recorded"
EVENT_MINUTES_FILED = "meeting-minutes-filed"
EVENT_MINUTES_APPROVED = "meeting-minutes-approved"

#: Attachment types that make up a meeting's agenda / minutes, for the helpers.
AGENDA_ATTACHMENT_TYPES = ("agenda", "order_paper", "notice")
MINUTES_ATTACHMENT_TYPES = ("minutes",)

#: Colour per meeting type, for the calendar. Drawn from the parliamentary theme
#: palette (the same greens and golds the workflow states use), so a month of
#: meetings reads at a glance. A type without an entry falls back to the default.
MEETING_TYPE_COLORS = {
    "plenary": "#275937",
    "joint_sitting": "#1f4d2e",
    "committee": "#5b8f22",
    "portfolio_committee": "#3f7d1f",
    "select_committee": "#6f9c3a",
    "ad_hoc_committee": "#8aa84f",
    "administration": "#b3995d",
    "management": "#c2a86f",
    "task_team": "#8c6d2f",
    "workshop": "#3c7a8c",
    "public_participation": "#a05a2c",
    "other": "#6c757d",
}
DEFAULT_MEETING_COLOR = "#5b8f22"


class MeetingQuerySet(models.QuerySet):
    """Query helpers shared by the calendar, the views and the API."""

    def in_period(self, start, end):
        """Meetings that begin within ``[start, end]`` (date bounds, inclusive).

        Filtered on the ``date`` part rather than the datetime so a plain
        :class:`datetime.date` can be passed without Django coercing it to a
        naive midnight datetime (which would warn under ``USE_TZ``).
        """
        return self.filter(
            start_datetime__date__gte=start, start_datetime__date__lte=end
        )

    def for_member(self, user):
        """Meetings ``user`` is a party to, *before* the RBAC filter.

        Membership here is deliberately operational rather than the access-control
        chain: the meetings a person is on the attendance register for, the ones
        they chair or minute, the ones they own or are assigned, and the ones
        convened by a group they actively belong to. Callers still pass the result
        through ``visible_instances`` so this can only ever *narrow* what the user
        already may see.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        group_ids = user.memberships.filter(is_active=True).values_list(
            "group_id", flat=True
        )
        return self.filter(
            models.Q(attendances__user=user)
            | models.Q(host_group_id__in=group_ids)
            | models.Q(chaired_by=user)
            | models.Q(secretary=user)
            | models.Q(owner=user)
            | models.Q(assigned_to=user)
        ).distinct()


class Meeting(AbstractLegislativeWorkflow):
    """One scheduled sitting — a House Plenary, a committee or an admin meeting."""

    MEETING_TYPE_PLENARY = "plenary"
    MEETING_TYPE_CHOICES = [
        ("plenary", _("House Plenary")),
        ("joint_sitting", _("Joint Sitting")),
        ("committee", _("Committee Meeting")),
        ("portfolio_committee", _("Portfolio Committee")),
        ("select_committee", _("Select Committee")),
        ("ad_hoc_committee", _("Ad Hoc Committee")),
        ("administration", _("Administration Meeting")),
        ("management", _("Management Meeting")),
        ("task_team", _("Task Team")),
        ("workshop", _("Workshop")),
        ("public_participation", _("Public Participation")),
        ("other", _("Other")),
    ]

    #: Meeting types that convene the whole House rather than a committee.
    HOUSE_MEETING_TYPES = ("plenary", "joint_sitting")

    MINUTES_STATUS_CHOICES = [
        ("not_recorded", _("Not recorded")),
        ("draft", _("Draft")),
        ("filed", _("Filed")),
        ("approved", _("Approved")),
    ]

    #: How the meeting is attended. "On the premises" is a room in a building;
    #: "hybrid" mixes a physical room with remote participants; "virtual" has no
    #: physical room at all.
    ATTENDANCE_MODE_ON_PREMISE = "on_premise"
    ATTENDANCE_MODE_HYBRID = "hybrid"
    ATTENDANCE_MODE_VIRTUAL = "virtual"
    ATTENDANCE_MODE_CHOICES = [
        ("on_premise", _("On the premises")),
        ("hybrid", _("Hybrid")),
        ("virtual", _("Virtual")),
    ]

    reference_number = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Human-facing meeting reference, e.g. NA/PLEN/2026-03-04.",
    )
    meeting_type = models.CharField(
        max_length=30,
        choices=MEETING_TYPE_CHOICES,
        default="committee",
        db_index=True,
        help_text="What kind of meeting this is (Plenary, committee, administration, …).",
    )
    start_datetime = models.DateTimeField(
        db_index=True, help_text="When the meeting is scheduled to start."
    )
    end_datetime = models.DateTimeField(
        null=True, blank=True, help_text="When the meeting is expected to end."
    )
    all_day = models.BooleanField(
        default=False, help_text="The meeting runs the whole day (no times shown)."
    )
    venue = models.ForeignKey(
        "Venue",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings",
        help_text=(
            "On-premises room the meeting is held in (a room in a building). "
            "Leave blank and use the off-site fields for an external venue."
        ),
    )
    off_site_venue = models.CharField(
        max_length=255,
        blank=True,
        help_text="Name of an off-premises venue (used when no on-premises room is set).",
    )
    off_site_address = models.CharField(
        max_length=255,
        blank=True,
        help_text="Address of the off-premises venue.",
    )
    attendance_mode = models.CharField(
        max_length=20,
        choices=ATTENDANCE_MODE_CHOICES,
        default=ATTENDANCE_MODE_ON_PREMISE,
        help_text="How the meeting is attended: on the premises, hybrid, or virtual.",
    )
    meeting_link = models.URLField(
        max_length=2048,
        blank=True,
        help_text="Join link for a hybrid or virtual meeting.",
    )

    #: The House / committee / unit that convenes the meeting. Distinct from the
    #: workflow type's owning group (the unit that administers the register).
    host_group = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hosted_meetings",
        help_text="House / committee / unit that convenes this meeting.",
    )
    chaired_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="chaired_meetings",
        help_text="Who chairs the meeting.",
    )
    secretary = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="secretary_meetings",
        help_text="Committee / meeting secretary taking the minutes.",
    )
    quorum = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Number of members required for the meeting to be quorate.",
    )

    agenda_summary = models.TextField(
        blank=True,
        help_text=(
            "The meeting agenda (limited rich text, written in the editor). "
            "Formal agenda documents are attachments."
        ),
    )
    notes = models.TextField(blank=True)
    minutes_status = models.CharField(
        max_length=20,
        choices=MINUTES_STATUS_CHOICES,
        default="not_recorded",
        help_text="Where the minutes of this meeting have got to.",
    )
    minutes_filed_at = models.DateTimeField(
        null=True, blank=True, help_text="When the minutes were filed."
    )
    register_generated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the attendance register was last generated from the host group. "
            "The register is the host group's members plus any invitees added by hand."
        ),
    )

    #: URL name of the detail view (read by ``get_absolute_url``).
    detail_url_name = "meeting_detail"

    objects = MeetingQuerySet.as_manager()

    class Meta:
        ordering = ["-start_datetime", "title"]
        indexes = [
            models.Index(fields=["start_datetime", "end_datetime"]),
            models.Index(fields=["meeting_type", "start_datetime"]),
        ]

    def __str__(self):
        return self.title or f"Meeting #{self.pk}"

    # -- scheduling ----------------------------------------------------------
    @property
    def end(self):
        """The meeting's end time, falling back to the start when none is set."""
        return self.end_datetime or self.start_datetime

    @property
    def duration_minutes(self):
        """Scheduled length in minutes (``None`` when there is no end time)."""
        if self.start_datetime is None or self.end_datetime is None:
            return None
        return int((self.end_datetime - self.start_datetime).total_seconds() // 60)

    @property
    def is_house_meeting(self):
        return self.meeting_type in self.HOUSE_MEETING_TYPES

    @property
    def is_virtual(self):
        """True when the meeting has no physical room."""
        return self.attendance_mode == self.ATTENDANCE_MODE_VIRTUAL

    @property
    def is_hybrid(self):
        """True when the meeting mixes a physical room with remote participants."""
        return self.attendance_mode == self.ATTENDANCE_MODE_HYBRID

    @property
    def is_on_premise(self):
        """True when the meeting is held on the parliamentary premises."""
        return self.attendance_mode == self.ATTENDANCE_MODE_ON_PREMISE

    @property
    def venue_display(self):
        """Where the meeting is held: the on-premises room, or the off-site venue.

        An on-premises room is the venue's fully-qualified label (building and
        room); an off-premises meeting falls back to the free-text name and
        address it carries. Empty when neither is set.
        """
        if self.venue_id:
            return str(self.venue)
        parts = [part for part in (self.off_site_venue, self.off_site_address) if part]
        return ", ".join(parts)

    @property
    def color(self):
        """The theme colour this meeting's type is coded with on the calendar."""
        return MEETING_TYPE_COLORS.get(self.meeting_type, DEFAULT_MEETING_COLOR)

    @property
    def is_upcoming(self):
        """True while the meeting has not started yet."""
        return self.start_datetime is not None and self.start_datetime > timezone.now()

    @property
    def is_running(self):
        """True while the meeting is underway (between start and end)."""
        if self.start_datetime is None:
            return False
        now = timezone.now()
        return self.start_datetime <= now <= self.end

    @property
    def is_finished(self):
        """True once the meeting's end has passed."""
        return self.start_datetime is not None and self.end < timezone.now()

    @property
    def calendar_state(self):
        """A coarse time-based label for the calendar, independent of the machine."""
        if self.current_state_id and self.current_state.is_terminal:
            return "cancelled" if self.current_state.slug == "cancelled" else "closed"
        if self.is_running:
            return "running"
        if self.is_finished:
            return "past"
        return "scheduled"

    # -- attendance ----------------------------------------------------------
    def regenerate_attendance(self, actor=None):
        """Rebuild the host-group part of the register, keeping the invitees.

        The register is the union of two sets: the hosting group's active members
        (the *generated* rows) and the invitees added by hand (rows with
        ``source='invitee'`` — anyone outside the hosting group, or a guest with
        no PWMS account). Regenerating re-syncs only the generated rows — a member
        who has joined the group is added, one who has left is removed — and
        leaves every invitee (and every RSVP / recorded attendance on a row that
        survives) untouched.

        Generated rows are derived data, so they are written with ``auditlog``
        switched off (a 400-member House Plenary should not leave a CREATE entry
        per member); ``register_generated_at`` records that generation happened.
        Returns a small summary of what changed.
        """
        from auditlog.context import disable_auditlog
        from django.db import transaction

        generated = {
            row.user_id: row
            for row in self.attendances.filter(
                source=MeetingAttendance.SOURCE_HOST_GROUP, user__isnull=False
            )
        }
        member_ids = set()
        added = 0

        with disable_auditlog(), transaction.atomic():
            if self.host_group_id is not None:
                members = list(self.host_group.get_active_members())
                member_ids = {member.pk for member in members}
                on_register = set(
                    self.attendances.filter(user__isnull=False).values_list(
                        "user_id", flat=True
                    )
                )
                for member in members:
                    if member.pk in on_register:
                        continue
                    MeetingAttendance.objects.create(
                        meeting=self,
                        user=member,
                        source=MeetingAttendance.SOURCE_HOST_GROUP,
                        capacity="member",
                    )
                    added += 1

            removed = 0
            for user_id, row in generated.items():
                if user_id not in member_ids:
                    row.delete()
                    removed += 1

        self.register_generated_at = timezone.now()
        self.save(update_fields=["register_generated_at", "updated_at"])
        return {
            "added": added,
            "removed": removed,
            "members": len(member_ids),
            "total": self.attendances.count(),
        }

    def attendances_active(self):
        """Attendance rows for this meeting: host group first, then invitees."""
        return self.attendances.select_related("user", "recorded_by").order_by(
            "source",
            "capacity",
            "user__last_name",
            "user__first_name",
            "guest_name",
        )

    def attendance_counts(self):
        """Tally of attendance statuses plus the invited/quorum headline figures."""
        counts = {
            "invited": 0,
            "present": 0,
            "absent": 0,
            "excused": 0,
            "late": 0,
            "not_recorded": 0,
            "accepted": 0,
            "declined": 0,
            "required": 0,
            "host_group": 0,
            "invitee": 0,
        }
        for row in self.attendances.all():
            counts["invited"] += 1
            if row.is_required:
                counts["required"] += 1
            if row.source in ("host_group", "invitee"):
                counts[row.source] += 1
            if row.attendance_status in ("present", "late", "left_early", "virtual"):
                counts["present"] += 1
            elif row.attendance_status == "not_recorded":
                counts["not_recorded"] += 1
            elif row.attendance_status in counts:
                counts[row.attendance_status] += 1
            if row.rsvp_status in ("accepted", "declined"):
                counts[row.rsvp_status] += 1
        counts["quorum_met"] = bool(
            self.quorum is not None and counts["present"] >= self.quorum
        )
        return counts

    def attendee_users(self):
        """The users on the attendance register (for notifications)."""
        user_ids = self.attendances.filter(user__isnull=False).values_list(
            "user_id", flat=True
        )
        return list(User.objects.filter(pk__in=user_ids, is_active=True))

    def record_attendance_event(
        self, attendance, *, actor=None, status="", ip_address=None
    ):
        """Append an ``meeting-attendance-recorded`` event to this meeting's log.

        Appended, never edited: a correction is a new event (see
        :class:`~pwms.models.workflows.WorkflowEvent`). The referenced
        ``EventType`` is looked up by slug and skipped — with a log line — when the
        registry row is missing, so a data change cannot break recording.
        """
        return self._record_slug_event(
            EVENT_ATTENDANCE_RECORDED,
            actor=actor,
            payload={
                "attendance_id": str(attendance.public_id),
                "person": attendance.display_name,
                "status": status or attendance.attendance_status,
                "capacity": attendance.capacity,
                "ip_address": ip_address or "",
            },
        )

    # -- agenda / minutes (attachments) --------------------------------------
    @property
    def agenda_attachments(self):
        """Agenda, order-paper and notice documents attached to the meeting."""
        return self.attachments.filter(type__in=AGENDA_ATTACHMENT_TYPES)

    @property
    def agenda_html(self):
        """The agenda body as sanitised, display-ready HTML.

        The stored value is attacker-controlled markup from the editor, so it is
        never rendered raw: :func:`pwms.utils.richtext.render_rich_text` puts it
        through the allowlist sanitiser and marks the result safe.
        """
        from ..utils.richtext import render_rich_text

        return render_rich_text(self.agenda_summary)

    @property
    def minutes_attachments(self):
        """Minutes documents attached to the meeting, newest first."""
        return self.attachments.filter(type__in=MINUTES_ATTACHMENT_TYPES)

    @property
    def has_minutes(self):
        return self.minutes_attachments.exists()

    def record_minutes_filed(self, *, actor=None, attachment=None):
        """Mark the minutes filed and append the matching domain event."""
        self.minutes_status = "filed"
        self.minutes_filed_at = timezone.now()
        self.save(update_fields=["minutes_status", "minutes_filed_at", "updated_at"])
        return self._record_slug_event(
            EVENT_MINUTES_FILED,
            actor=actor,
            payload={
                "attachment_id": str(getattr(attachment, "public_id", "")),
                "name": getattr(attachment, "name", ""),
            },
            document_url=getattr(attachment, "sharepoint_web_url", "") or "",
        )

    def record_minutes_approved(self, *, actor=None, comment=""):
        """Mark the minutes approved and append the matching domain event."""
        self.minutes_status = "approved"
        self.save(update_fields=["minutes_status", "updated_at"])
        return self._record_slug_event(
            EVENT_MINUTES_APPROVED,
            actor=actor,
            payload={"comment": comment},
        )

    # -- notification hooks --------------------------------------------------
    def notification_audience(self):
        """Everyone who should hear about this meeting changing.

        The attendees plus the people named on the record (chair, secretary,
        owner, assignee). De-duplicated across the sets.
        """
        people = {}
        for user in self.attendee_users():
            people[user.pk] = user
        for user in (self.chaired_by, self.secretary, self.owner, self.assigned_to):
            if user is not None and user.is_active:
                people[user.pk] = user
        return list(people.values())

    def notify_document_change(self, action, attachment, *, actor=None):
        """Announce a document upload/removal on the meeting (see the service hook).

        Called by :mod:`pwms.services.attachments` after it has recorded the
        domain event, so the audited fact exists before anyone is told about it.
        Every meeting inherits this; the service only calls it when the target is a
        meeting, so other instrument types are unaffected.
        """
        from ..notifications import notify_meeting_document

        return notify_meeting_document(self, action, attachment, actor=actor)

    # -- internals -----------------------------------------------------------
    def _record_slug_event(self, slug, *, actor=None, payload=None, document_url=""):
        """Append an event by ``EventType`` slug, tolerating a missing registry row."""
        from .workflows import EventType

        event_type = EventType.objects.filter(slug=slug).first()
        if event_type is None:
            return None
        return self.record_event(
            event_type,
            actor=actor,
            payload=payload or {},
            document_url=document_url,
        )


class MeetingAttendance(BaseModel):
    """One person's place at one meeting, and the attendance recorded for them.

    The row covers the whole lifecycle: it is created when the person is invited
    (``rsvp_status=pending``), updated when they respond, and updated again when
    their attendance is recorded during or after the sitting. ``recorded_by`` /
    ``recorded_at`` / ``recorded_ip`` capture who said so and when, and the row is
    registered with ``auditlog`` so every change keeps a field-level trail.
    """

    CAPACITY_CHOICES = [
        ("chairperson", _("Chairperson")),
        ("deputy_chairperson", _("Deputy Chairperson")),
        ("member", _("Member")),
        ("secretary", _("Secretary")),
        ("presenter", _("Presenter")),
        ("official", _("Official")),
        ("guest", _("Guest")),
        ("observer", _("Observer")),
    ]

    RSVP_STATUS_CHOICES = [
        ("pending", _("No response")),
        ("accepted", _("Accepted")),
        ("tentative", _("Tentative")),
        ("declined", _("Declined")),
    ]

    ATTENDANCE_STATUS_CHOICES = [
        ("not_recorded", _("Not recorded")),
        ("present", _("Present")),
        ("virtual", _("Present (virtual)")),
        ("late", _("Late")),
        ("left_early", _("Left early")),
        ("excused", _("Excused")),
        ("absent", _("Absent")),
    ]

    #: Attendance statuses that count as "the member was there" for quorum.
    PRESENT_STATUSES = ("present", "virtual", "late", "left_early")

    #: Where a register row came from: generated from the hosting group, or added
    #: by hand (an invitee outside the hosting group, or a guest with no account).
    SOURCE_HOST_GROUP = "host_group"
    SOURCE_INVITEE = "invitee"
    SOURCE_CHOICES = [
        ("host_group", _("Host group")),
        ("invitee", _("Invitee")),
    ]

    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name="attendances",
        help_text="The meeting this person is invited to.",
    )
    #: The PWMS user, when the attendee has an account. A guest without an
    #: account is named on ``guest_name`` instead, so the register can still be
    #: complete for a public-participation hearing.
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="meeting_attendances",
        help_text="Attendee, when they have a PWMS account.",
    )
    guest_name = models.CharField(
        max_length=255, blank=True, help_text="Name of an attendee without an account."
    )
    guest_email = models.EmailField(blank=True)

    source = models.CharField(
        max_length=20,
        choices=SOURCE_CHOICES,
        default=SOURCE_INVITEE,
        help_text=(
            "How the person came to be on the register: generated from the hosting "
            "group, or invited by hand. The two are kept apart so the register can "
            "be regenerated without disturbing the invitees."
        ),
    )

    capacity = models.CharField(
        max_length=30,
        choices=CAPACITY_CHOICES,
        default="member",
        help_text="The role this person takes at the meeting.",
    )
    is_required = models.BooleanField(
        default=False,
        help_text="Counts towards quorum and is expected to attend.",
    )
    rsvp_status = models.CharField(
        max_length=20, choices=RSVP_STATUS_CHOICES, default="pending"
    )
    attendance_status = models.CharField(
        max_length=20, choices=ATTENDANCE_STATUS_CHOICES, default="not_recorded"
    )

    invited_at = models.DateTimeField(default=timezone.now)
    responded_at = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(
        null=True, blank=True, help_text="When attendance was last recorded."
    )
    recorded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attendance_records",
        help_text="Who recorded the attendance (during or after the meeting).",
    )
    recorded_ip = models.GenericIPAddressField(
        null=True, blank=True, help_text="Client address the record was made from."
    )
    note = models.TextField(
        blank=True, help_text="Any remark recorded against this attendee."
    )

    class Meta:
        ordering = ["capacity", "guest_name", "user__last_name"]
        indexes = [
            models.Index(fields=["meeting", "attendance_status"]),
            models.Index(fields=["user", "attendance_status"]),
            models.Index(fields=["meeting", "source"]),
        ]
        constraints = [
            # One place per account per meeting; guests (user is null) are exempt.
            models.UniqueConstraint(
                fields=["meeting", "user"],
                name="meetings_attendance_meeting_user_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.display_name} @ {self.meeting}"

    # -- identity ------------------------------------------------------------
    @property
    def display_name(self):
        """The attendee's label: the user's display name, else the guest name."""
        if self.user_id:
            return self.user.display_name
        return self.guest_name or "Unnamed attendee"

    @property
    def email(self):
        """Where to reach the attendee, from the account or the guest address."""
        if self.user_id and self.user.email:
            return self.user.email
        return self.guest_email

    @property
    def is_present(self):
        return self.attendance_status in self.PRESENT_STATUSES

    # -- lifecycle -----------------------------------------------------------
    def respond(self, rsvp_status, *, note=None):
        """Record an RSVP response (idempotent per status)."""
        self.rsvp_status = rsvp_status
        self.responded_at = timezone.now()
        fields = ["rsvp_status", "responded_at", "updated_at"]
        if note is not None:
            self.note = note
            fields.append("note")
        self.save(update_fields=fields)
        return self

    def record(self, status, *, actor=None, ip_address=None, note=None):
        """Record attendance during or after the meeting, and audit it.

        Sets who recorded it and from where, then appends a domain event to the
        meeting's log so the change is visible on the timeline as well as in the
        attendance register.
        """
        self.attendance_status = status
        self.recorded_at = timezone.now()
        self.recorded_by = actor
        if ip_address:
            self.recorded_ip = ip_address
        fields = [
            "attendance_status",
            "recorded_at",
            "recorded_by",
            "recorded_ip",
            "updated_at",
        ]
        if note is not None:
            self.note = note
            fields.append("note")
        self.save(update_fields=fields)
        self.meeting.record_attendance_event(
            self, actor=actor, status=status, ip_address=ip_address
        )
        return self

    def get_absolute_url(self):
        return reverse(
            "pwms:meeting_detail", kwargs={"public_id": self.meeting.public_id}
        )
