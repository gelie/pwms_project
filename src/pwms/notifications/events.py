"""Entry points that turn workflow activity into alerts."""

from django.utils import timezone

from ..models import Meeting
from ..models.notifications import (
    EVENT_CREATED,
    EVENT_MEETING_ATTENDANCE,
    EVENT_MEETING_DOCUMENT,
    EVENT_REFERRAL_CLOSED,
    EVENT_REFERRAL_CREATED,
    EVENT_REFERRAL_DEADLINE,
    EVENT_TRANSITION,
)
from .audience import referral_audience, stakeholders
from .delivery import alert


# -- entry points -----------------------------------------------------------
def notify_workflow_created(instance, actor=None):
    """Tell the people who run this instrument that a new record exists."""
    state = instance.current_state
    subject = f"New {instance.workflow_type.name}: {instance.title}"
    message = (
        f"A new {instance.workflow_type.name} has been created and is waiting in "
        f"the '{state.name}' state.\n\n"
        f"Title: {instance.title}\n"
        f"Reference: {instance.identifier or '-'}\n"
        f"Priority: {instance.get_priority_display()}"
    )
    return alert(
        kind="workflow-created",
        instance=instance,
        audience=stakeholders(instance, event=EVENT_CREATED),
        subject=subject,
        message=message,
        actor=actor,
        context={"state": state.name, "priority": instance.priority},
    )


def notify_transition(instance, transition, *, actor=None, comment=""):
    """
    Tell the stakeholders that an instance moved state.

    The audience is whoever acts next (the people whose turn it now is), the
    people the record names, the officers at the type's oversight channel and
    every :class:`NotificationRule` for this transition. A meeting
    transition takes the meeting-specific wording and kind (see
    :func:`notify_meeting_status`) so its alerts read as meeting news rather than
    generic workflow traffic.
    """
    if isinstance(instance, Meeting):
        return notify_meeting_status(instance, transition, actor=actor, comment=comment)

    from_state = transition.from_state
    to_state = transition.to_state
    subject = f"{instance.title}: {from_state.name} → {to_state.name}"

    message = (
        f"'{instance.title}' moved from '{from_state.name}' to '{to_state.name}' "
        f"via '{transition.name}'.\n\n"
        f"Reference: {instance.identifier or '-'}"
    )
    if comment:
        message += f"\n\nComment: {comment}"

    return alert(
        kind="workflow-transition",
        instance=instance,
        audience=stakeholders(instance, event=EVENT_TRANSITION, transition=transition),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "from_state": from_state.name,
            "to_state": to_state.name,
            "transition": transition.name,
            "comment": comment,
        },
    )


# -- meetings (the Events workflow) -----------------------------------------
def _meeting_when(meeting):
    """One-line human summary of when a meeting is scheduled."""
    if meeting.start_datetime is None:
        return "date to be confirmed"
    when = meeting.start_datetime.strftime("%a %d %b %Y %H:%M")
    if meeting.end_datetime is not None:
        when += "–" + meeting.end_datetime.strftime("%H:%M")
    return when


def _meeting_where(meeting):
    """One-line human summary of where (and how) a meeting is held.

    The on-premises room or the off-site venue, tagged when the meeting is
    virtual or hybrid so the mode reads alongside the place.
    """
    where = meeting.venue_display
    if meeting.is_virtual:
        return f"{where} (virtual)" if where else "virtual"
    if meeting.is_hybrid:
        return f"{where} (hybrid)" if where else "hybrid"
    return where or "venue to be confirmed"


def notify_meeting_created(meeting, actor=None):
    """Tell the meeting's audience that a new sitting has been scheduled."""
    subject = f"Meeting scheduled: {meeting.title}"
    message = (
        f"A new {meeting.get_meeting_type_display()} has been scheduled.\n\n"
        f"Title: {meeting.title}\n"
        f"Reference: {meeting.identifier or '-'}\n"
        f"When: {_meeting_when(meeting)}\n"
        f"Where: {_meeting_where(meeting)}"
    )
    if meeting.host_group is not None:
        message += f"\nConvener: {meeting.host_group.name}"
    if meeting.chaired_by is not None:
        message += f"\nChairperson: {meeting.chaired_by.display_name}"
    return alert(
        kind="meeting-created",
        instance=meeting,
        audience=stakeholders(meeting, event=EVENT_CREATED),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "meeting_type": meeting.meeting_type,
            "start_datetime": _iso(meeting.start_datetime),
            "state": meeting.current_state.name if meeting.current_state_id else "",
        },
    )


def notify_meeting_status(meeting, transition, *, actor=None, comment=""):
    """Tell the meeting's audience that its status moved (scheduled, postponed, …)."""
    from_state = transition.from_state
    to_state = transition.to_state
    subject = f"{meeting.title}: {from_state.name} → {to_state.name}"
    message = (
        f"'{meeting.title}' moved from '{from_state.name}' to '{to_state.name}' "
        f"via '{transition.name}'.\n\n"
        f"When: {_meeting_when(meeting)}\n"
        f"Reference: {meeting.identifier or '-'}"
    )
    if comment:
        message += f"\n\nComment: {comment}"

    return alert(
        kind="meeting-status",
        instance=meeting,
        audience=stakeholders(meeting, event=EVENT_TRANSITION, transition=transition),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "from_state": from_state.name,
            "to_state": to_state.name,
            "transition": transition.name,
            "comment": comment,
        },
    )


#: Human verbs for the attachment actions a meeting alert can carry.
_DOCUMENT_ACTIONS = {
    "attached": "uploaded",
    "detached": "removed",
    "version-added": "replaced with a new version",
}


def notify_meeting_document(meeting, action, attachment, *, actor=None):
    """Tell the meeting's audience that a document was uploaded or removed.

    Called by the attachment service after it has recorded the matching domain
    event, so the audit trail exists before anyone is told about the change. The
    subject names the document type (agenda, minutes, …) when it is one of those,
    so a filed agenda reads differently from a general paper.
    """
    verb = _DOCUMENT_ACTIONS.get(action, "changed")
    type_label = ""
    try:
        type_label = attachment.get_type_display()
    except Exception:  # noqa: BLE001 - a snapshot object must still notify
        type_label = ""
    kind_label = f"{type_label} " if type_label else "document "
    subject = f"{meeting.title}: {kind_label}{verb}"
    message = (
        f"A {kind_label}was {verb} on '{meeting.title}'.\n\n"
        f"Document: {getattr(attachment, 'name', '-')}\n"
        f"When: {_meeting_when(meeting)}"
    )
    if actor is not None:
        message += f"\nBy: {_actor_label(actor)}"

    kind = "meeting-document"
    if getattr(attachment, "type", "") in ("minutes",):
        kind = "meeting-minutes"
    return alert(
        kind=kind,
        instance=meeting,
        audience=stakeholders(meeting, event=EVENT_MEETING_DOCUMENT),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "action": action,
            "attachment_id": str(getattr(attachment, "public_id", "")),
            "name": getattr(attachment, "name", ""),
            "type": getattr(attachment, "type", ""),
        },
    )


def notify_meeting_attendance(meeting, attendance, *, actor=None, status=""):
    """Email the meeting's audience a recorded attendance summary.

    Deliberately a *summary* rather than one message per attendee: recording a
    register produces many row changes at once, so a single message reporting the
    tally is what a convener actually wants.
    """
    counts = meeting.attendance_counts()
    subject = f"{meeting.title}: attendance recorded"
    message = (
        f"Attendance has been recorded for '{meeting.title}'.\n\n"
        f"Invited: {counts['invited']}\n"
        f"Present: {counts['present']}\n"
        f"Absent: {counts['absent']}\n"
        f"Excused: {counts['excused']}\n"
        f"Not yet recorded: {counts['not_recorded']}"
    )
    if meeting.quorum is not None:
        message += (
            f"\n\nQuorum: {meeting.quorum} — "
            f"{'met' if counts['quorum_met'] else 'not met'}"
        )
    return alert(
        kind="meeting-attendance",
        instance=meeting,
        audience=stakeholders(meeting, event=EVENT_MEETING_ATTENDANCE),
        subject=subject,
        message=message,
        actor=actor,
        context={"counts": counts, "status": status},
    )


def notify_referral_created(referral, actor=None):
    """Tell the committee a matter has been referred to it."""
    workflow = referral.content_object
    if workflow is None:
        return []

    subject = f"Referred to {referral.referred_to.name}: {workflow.title}"
    message = (
        f"'{workflow.title}' has been referred to {referral.referred_to.name} for "
        f"consideration.\n\n"
        f"Referred by: {_actor_label(referral.referred_by)}\n"
        f"Due date: {_due_label(referral)}"
    )
    return alert(
        kind="referral-created",
        instance=workflow,
        audience=referral_audience(referral, event=EVENT_REFERRAL_CREATED),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "referred_to": referral.referred_to.name,
            "due_date": _iso(referral.due_date),
        },
    )


#: Referral status -> (subject label, sentence fragment) for the closing notice.
REFERRAL_CLOSED_LABELS = {
    "referral-responded": ("Answered", "answered"),
    "referral-recalled": ("Recalled", "recalled"),
    "referral-expired": ("Expired", "expired unanswered"),
}


def notify_referral_closed(referral, kind, actor=None):
    """Tell the parties that a referral was answered, recalled or expired."""
    workflow = referral.content_object
    if workflow is None:
        return []

    subject_label, phrase = REFERRAL_CLOSED_LABELS.get(kind, ("Closed", "closed"))

    subject = f"Referral {subject_label}: {workflow.title}"
    message = (
        f"The referral of '{workflow.title}' to {referral.referred_to.name} was "
        f"{phrase}.\n\n"
        f"Referred by: {_actor_label(referral.referred_by)}"
    )
    if referral.response_notes:
        message += f"\n\nResponse: {referral.response_notes}"
    if referral.recall_reason:
        message += f"\n\nReason: {referral.recall_reason}"
    if kind == "referral-expired":
        message += f"\n\nDeadline was: {_due_label(referral)}"

    return alert(
        kind=kind,
        instance=workflow,
        audience=referral_audience(referral, event=EVENT_REFERRAL_CLOSED),
        subject=subject,
        message=message,
        actor=actor,
        context={
            "referred_to": referral.referred_to.name,
            "status": referral.status,
            "response_notes": referral.response_notes,
            "recall_reason": referral.recall_reason,
        },
    )


def notify_referral_deadline(referral, *, subject, message, actor=None):
    """
    Record a deadline reminder composed by the scheduled command.

    The command owns the wording (and its 24-hour / final-hour windows); this
    entry point exists so those sends land in the same log as every other alert.
    """
    return alert(
        kind="referral-deadline",
        instance=referral.content_object,
        audience=referral_audience(referral, event=EVENT_REFERRAL_DEADLINE),
        subject=subject,
        message=message,
        actor=actor,
        context={"due_date": _iso(referral.due_date)},
    )


# -- small helpers ----------------------------------------------------------
def _actor_label(user):
    if user is None:
        return "PWMS (automatic)"
    return user.get_full_name() or user.get_username()


def _due_label(referral):
    if referral.due_date is None:
        return "no deadline set"
    return timezone.localtime(referral.due_date).strftime("%Y-%m-%d %H:%M")


def _iso(value):
    return value.isoformat() if value is not None else ""
