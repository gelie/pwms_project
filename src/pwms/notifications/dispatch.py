"""Turn workflow activity into recorded, delivered alerts.

Three questions shape this module.

*Who?* :func:`stakeholders` answers it for role-driven work: the officers who run
the workflow type, anyone a ``Transition`` names as a subscriber
(``notify_roles``), whoever can act next, and the two people the record itself
names (``owner`` / ``assigned_to``). :func:`referral_audience` answers it for
referrals, where the referral is what entitles the committee to know. That
union feeds the in-app bell; **email is narrower** — :func:`email_audience`
keeps only active staff who belong to a group affected by the alert, so a
workflow or referral is not blasted to every member who can merely see it.

*What?* Messages are rendered from ``templates/emails/notification.txt`` (the
stored plain-text body) and ``templates/emails/notification.html`` (the HTML
alternative that rides with it), so the alert explains itself later.

*When?* Rows are written immediately, inside the caller's transaction, and the
mail is handed to the background queue **in that same transaction** — so the
task commits atomically with the alert row and a workflow change that rolls back
queues nothing. Dispatching therefore never talks to a mail server; the worker
(``manage.py process_tasks``) does, and retries a transient failure.

Every dispatch writes two rows per recipient — one ``in_app`` (the bell) and one
``email`` — and the email row carries the delivery outcome. The
:class:`~pwms.models.Notification` table is therefore the log of what was sent,
to whom, and whether it worked.

Dispatching never raises. A notification is a side effect of the work, so a dead
mail server must not fail the transition that triggered it — and with delivery
queued, the transition never speaks to a mail server at all. The failure is
recorded on the row (``error`` / ``attempts``) and logged; the *task* raises so
the queue reschedules it (see :func:`deliver_email`).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import Meeting, Notification, User
from ..services.permissions import VIEW, resolve

logger = logging.getLogger(__name__)

#: Body templates for every alert (see the module docstring).
EMAIL_TEMPLATE = "emails/notification.txt"
HTML_EMAIL_TEMPLATE = "emails/notification.html"

#: ``User.employee_type`` values that identify employees rather than members.
#: ``is_staff_member`` is checked as the explicit override; the ERP sync writes
#: ``employee_type`` and not that flag, so both have to count.
STAFF_EMPLOYEE_TYPES = ("staff", "graduate")


# -- recipients -------------------------------------------------------------
def _officers(instance):
    """
    Active members of the workflow type's group who hold one of its create roles.

    These are the people who run this instrument — the audience that exists for
    *every* workflow type without further configuration, unlike
    ``Transition.notify_roles`` (which the RBAC seeds leave empty and an
    administrator fills in per transition).
    """
    workflow_type = instance.workflow_type
    if workflow_type.group_id is None:
        return []

    return list(
        User.objects.filter(
            is_active=True,
            memberships__is_active=True,
            memberships__group_id=workflow_type.group_id,
            memberships__role__in=workflow_type.create_roles.all(),
        ).distinct()
    )


def _role_holders(instance, roles):
    """
    Active holders of ``roles`` within the groups that have access to ``instance``.

    Scoped to the instance's own groups on purpose: ``Role`` is shared across the
    organisation, so "everyone holding *Committee Secretary*" would be a blast at
    hundreds of unrelated people. ``Transition.notify_roles`` is documented as
    *group members* holding those roles, and a group that should hear about the
    instrument can be attached to its type as a viewer group.
    """
    roles = list(roles)
    if not roles:
        return []

    group_ids = instance.group_accesses().values_list("group_id", flat=True)
    return list(
        User.objects.filter(
            is_active=True,
            memberships__is_active=True,
            memberships__group_id__in=group_ids,
            memberships__role__in=roles,
        ).distinct()
    )


def _named(instance):
    """The people the record names: its owner and whoever owes the next action."""
    return [user for user in (instance.owner, instance.assigned_to) if user is not None]


def _visible(users, instance):
    """
    Drop anyone who may not open ``instance``.

    The RBAC chain is re-checked here rather than assumed: an alert links to the
    record and quotes its state, so telling somebody who would get a 403 leaks
    both. ``resolve`` also covers the owner / assignee short-cuts.
    """
    allowed = {}
    for user in users:
        if not user.is_active:
            continue
        if resolve(user, instance, VIEW):
            allowed[user.pk] = user
    return list(allowed.values())


def stakeholders(instance, *, roles=()):
    """
    Everyone who should hear about a change to ``instance``.

    The union of the type's officers, the holders of ``roles`` (a transition's
    declared subscribers and the roles allowed to act next) and the people named
    on the record — minus anyone who cannot view it. A :class:`Meeting` adds its
    attendance register to that union, because the people invited to a sitting are
    exactly the people who need to hear that it moved.
    """
    candidates = _officers(instance) + _role_holders(instance, roles) + _named(instance)
    if isinstance(instance, Meeting):
        candidates += instance.notification_audience()
    return _visible(candidates, instance)


def next_actor_roles(instance):
    """Roles entitled to perform a transition available from the current state."""
    roles = {}
    for transition in instance.get_available_transitions().prefetch_related(
        "allowed_roles"
    ):
        for role in transition.allowed_roles.all():
            roles[role.pk] = role
    return list(roles.values())


def referral_audience(referral):
    """
    Who hears about a referral: the referred group's members and the people the
    record names (owner and assignee).

    The named pair is the same one :func:`stakeholders` uses, so whoever owes the
    next action hears about referral activity exactly as they do about creations
    and transitions.

    Deliberately *not* filtered through :func:`_visible`: a committee may be
    referred a matter it holds no ``WorkflowGroupAccess`` row for — the referral
    itself is the grant of interest — and the message carries only the referral's
    own facts (title, committee, due date), so nothing is disclosed beyond what
    the referral already states. Inactive accounts are still skipped: their
    directory logins are closed, so an alert would only be noise.
    """
    members = referral.referred_to.members.filter(
        is_active=True, user__is_active=True
    ).select_related("user")
    audience = {membership.user.pk: membership.user for membership in members}

    workflow = referral.content_object
    if workflow is not None:
        for user in _named(workflow):
            if user.is_active:
                audience[user.pk] = user
    return list(audience.values())


def affected_groups(instance):
    """
    The groups with a stake in ``instance``: its type's owning group, every group
    the record grants access to (viewer and referral groups included), and any
    group the instrument itself names — a bill's responsible committee, a
    resolution's responsible group, an agreement's referral committees, or a
    meeting's host group.

    This is the "affected groups" an email audience is drawn from. It is broader
    than the type's own group on purpose: a committee the matter was referred to
    has a real interest, and so does a viewer group attached to the type.
    """
    if instance is None:
        return []

    groups = {}
    workflow_type = getattr(instance, "workflow_type", None)
    if workflow_type is not None and workflow_type.group_id is not None:
        groups[workflow_type.group_id] = workflow_type.group

    accesses = getattr(instance, "group_accesses", None)
    if callable(accesses):
        for access in accesses().select_related("group"):
            groups[access.group_id] = access.group

    # Group-valued fields, whichever concrete instrument declares them.
    for attribute in ("responsible_group", "responsible_committee", "host_group"):
        group = getattr(instance, attribute, None)
        if group is not None:
            groups[group.pk] = group

    referral_committees = getattr(instance, "referral_committees", None)
    if referral_committees is not None:
        for group in referral_committees.all():
            groups[group.pk] = group

    return list(groups.values())


def staff_recipients(recipients, groups):
    """
    Keep only the recipients who are active staff in one of ``groups``.

    Staff means the explicit ``is_staff_member`` flag or an ``employee_type`` of
    ``staff`` / ``graduate``. Membership must be *active* and in an affected
    group, which is what stops a record's owner or an invited guest from being
    emailed merely because they are named. Order is preserved so a caller's
    audience stays deterministic.
    """
    recipients = list(recipients)
    group_ids = {group.pk for group in groups if group is not None}
    if not recipients or not group_ids:
        return []

    staff_ids = set(
        User.objects.filter(pk__in=[user.pk for user in recipients])
        .filter(Q(is_staff_member=True) | Q(employee_type__in=STAFF_EMPLOYEE_TYPES))
        .filter(
            is_active=True,
            memberships__is_active=True,
            memberships__group_id__in=group_ids,
        )
        .values_list("pk", flat=True)
    )
    return [user for user in recipients if user.pk in staff_ids]


def email_audience(instance, recipients, *, extra_groups=()):
    """
    The subset of ``recipients`` who should receive the alert by email.

    In-app alerts go to the whole audience; mail is limited to staff of the
    affected groups (see :func:`affected_groups`). ``extra_groups`` names groups
    the target itself does not know about — a referral's ``referred_to`` group,
    for example.
    """
    groups = affected_groups(instance)
    groups.extend(group for group in extra_groups if group is not None)
    return staff_recipients(recipients, groups)


# -- composition & delivery -------------------------------------------------
def default_from_email():
    """Sender address for alerts (``settings.DEFAULT_FROM_EMAIL``)."""
    return getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@parliament.gov.za")


def site_base_url():
    """Base URL used to make alert links absolute in email."""
    return getattr(settings, "PWMS_BASE_URL", "").rstrip("/")


def _body_context(*, recipient, message, url="", instance=None, actor=None, kind=""):
    """The variables both the text and HTML alert bodies are rendered from."""
    base = site_base_url()
    return {
        "recipient": recipient,
        "message": message,
        "path": url,
        "link": f"{base}{url}" if (base and url) else url,
        "instance": instance,
        "actor": actor,
        "kind": kind,
        "kind_label": dict(Notification.KIND_CHOICES).get(kind, kind),
    }


def render_body(*, recipient, message, url="", instance=None, actor=None, kind=""):
    """Render one recipient's plain-text message body."""
    return render_to_string(
        EMAIL_TEMPLATE,
        _body_context(
            recipient=recipient,
            message=message,
            url=url,
            instance=instance,
            actor=actor,
            kind=kind,
        ),
    )


def render_html_body(*, recipient, message, url="", instance=None, actor=None, kind=""):
    """Render one recipient's HTML message body (the alternative part)."""
    return render_to_string(
        HTML_EMAIL_TEMPLATE,
        _body_context(
            recipient=recipient,
            message=message,
            url=url,
            instance=instance,
            actor=actor,
            kind=kind,
        ),
    )


# -- delivery ---------------------------------------------------------------
def max_attempts():
    """
    How many attempts an alert email gets before it is marked failed.

    Reads the same ``MAX_ATTEMPTS`` django-background-tasks uses to decide when
    it stops retrying the *task*, so the row and the queue give up together.
    """
    return getattr(settings, "MAX_ATTEMPTS", 3)


class NotificationDeliveryError(RuntimeError):
    """
    A retryable delivery failure.

    Raised by :func:`deliver_email` so the queued task fails and
    django-background-tasks reschedules it. Deliberately not raised on the last
    attempt: there is nothing left to retry, and the row carries the reason from
    then on.
    """


def _pending_email_row(notification_id):
    """The email row still to be delivered, or ``None`` when there is none."""
    return (
        Notification.objects.select_related("recipient")
        .filter(pk=notification_id, status="pending")
        .first()
    )


def deliver_email(notification_id):
    """
    Make one delivery attempt for one queued email row, and record the outcome.

    Returns the row it acted on, or ``None`` when the row is gone or no longer
    ``pending``. That guard is what makes delivery idempotent: a retried task
    cannot send the same alert twice.

    Failure policy:

    * a recipient with no address is permanent, so the row is marked ``failed``;
    * anything else leaves the row ``pending`` with the reason in ``error`` and
      **raises** :class:`NotificationDeliveryError`, which is how the queue
      learns to try again (its own ``MAX_ATTEMPTS`` and backoff do the timing);
    * once :func:`max_attempts` attempts have been made the row is marked
      ``failed`` and nothing is raised.

    Dispatching an alert never raises — ``_record`` only queues this work — so a
    dead mail server still cannot break the workflow change that produced the
    alert. The error surfaces here, in the worker, and in the log.
    """
    row = _pending_email_row(notification_id)
    if row is None:
        return None

    recipient = row.recipient
    to_email = row.to_email or (recipient.email if recipient is not None else "")
    if not to_email:
        return row.mark_failed("recipient has no email address")

    row.attempts += 1
    try:
        send_mail(
            subject=row.subject,
            message=row.body,
            from_email=row.from_email or default_from_email(),
            recipient_list=[to_email],
            fail_silently=False,
            html_message=row.html_body or None,
        )
    except Exception as error:
        logger.warning(
            "Notification %s to %s failed on attempt %s/%s: %s",
            row.pk,
            to_email,
            row.attempts,
            max_attempts(),
            error,
        )
        if row.attempts >= max_attempts():
            return row.mark_failed(error)
        # Stays pending with the reason, so the log shows why it is still trying.
        row.error = str(error)
        row.save(update_fields=["attempts", "error", "updated_at"])
        raise NotificationDeliveryError(str(error)) from error

    logger.info(
        "Notification %s emailed to %s (%s) on attempt %s",
        row.pk,
        to_email,
        row.kind,
        row.attempts,
    )
    return row.mark_sent()


def _record(
    *,
    recipient,
    kind,
    subject,
    body,
    url,
    instance,
    actor,
    context,
    html_body="",
    send_email=True,
):
    """Write the in-app row and, when allowed, the queued email row."""
    content_type = None
    object_id = None
    if instance is not None:
        content_type = ContentType.objects.get_for_model(instance)
        object_id = instance.pk

    shared = {
        "recipient": recipient,
        "kind": kind,
        "subject": subject,
        "body": body,
        "url": url,
        "actor": actor,
        "context": context,
        "content_type": content_type,
        "object_id": object_id,
    }

    rows = [
        Notification.objects.create(
            channel="in_app", status="sent", sent_at=timezone.now(), **shared
        )
    ]

    if send_email and recipient.email:
        email_row = Notification.objects.create(
            channel="email",
            html_body=html_body,
            to_email=recipient.email,
            from_email=default_from_email(),
            **shared,
        )
        rows.append(email_row)
        queue_email(email_row)

    return rows


def record_email(
    *,
    kind,
    subject,
    body,
    to_email,
    html_body="",
    from_email=None,
    actor=None,
    url="",
    target=None,
    context=None,
):
    """
    Write one ``pending`` email row to the outbound log.

    Used by explicit sharing, which sends inline rather than through the alert
    queue: the caller sends the message and then marks the row sent/failed (see
    ``Notification.mark_sent`` / ``mark_failed``). ``target`` is any model the
    message is about — a workflow record or a ``ReportShare``.
    """
    content_type = None
    object_id = None
    if target is not None:
        content_type = ContentType.objects.get_for_model(target)
        object_id = target.pk

    return Notification.objects.create(
        channel="email",
        kind=kind,
        subject=subject,
        body=body,
        html_body=html_body,
        to_email=to_email,
        from_email=from_email or default_from_email(),
        url=url,
        actor=actor,
        context=context or {},
        content_type=content_type,
        object_id=object_id,
    )


def queue_email(row):
    """
    Hand one email row to the background queue.

    Queued *inside* the caller's transaction rather than deferred with
    ``on_commit``: the task row then commits atomically with the alert row, so a
    crash immediately after COMMIT cannot lose the send. That is precisely what
    ``on_commit`` could not promise — a callback lost that way left the alert
    ``pending`` with nothing left to pick it up. A rolled-back change queues
    nothing, because the task row rolls back with the alert it belongs to.

    Imported lazily: ``pwms.tasks`` builds its background-task proxies from this
    module's :func:`deliver_email`, so importing it at module level is a cycle.
    """
    from ..tasks import queue_notification_email

    return queue_notification_email(row.pk, verbose_name=f"Alert: {row.subject}")


def alert(
    *,
    kind,
    instance,
    recipients,
    subject,
    message,
    actor=None,
    context=None,
    email_groups=(),
):
    """
    Record one alert for each recipient and queue any permitted email.

    Every recipient gets the in-app row; only staff of the affected groups are
    emailed (see :func:`email_audience`). ``email_groups`` adds groups the
    instance itself does not know about — a referral's ``referred_to`` committee.

    Returns the rows written. The actor is dropped from the audience — nobody
    needs telling about something they just did.
    """
    audience = [user for user in recipients if actor is None or user.pk != actor.pk]
    if not audience:
        logger.debug("No recipients for %s alert on %s", kind, instance)
        return []

    url = instance.get_absolute_url() if instance is not None else ""
    emailable = {
        user.pk
        for user in email_audience(instance, audience, extra_groups=email_groups)
    }

    rows = []
    for user in audience:
        body = render_body(
            recipient=user,
            message=message,
            url=url,
            instance=instance,
            actor=actor,
            kind=kind,
        )
        html_body = ""
        if user.pk in emailable:
            html_body = render_html_body(
                recipient=user,
                message=message,
                url=url,
                instance=instance,
                actor=actor,
                kind=kind,
            )
        rows.extend(
            _record(
                recipient=user,
                kind=kind,
                subject=subject,
                body=body,
                html_body=html_body,
                send_email=user.pk in emailable,
                url=url,
                instance=instance,
                actor=actor,
                context=context or {},
            )
        )
    logger.info("Recorded %s alerts for %s", len(rows), kind)
    return rows


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
        recipients=stakeholders(instance),
        subject=subject,
        message=message,
        actor=actor,
        context={"state": state.name, "priority": instance.priority},
    )


def notify_transition(instance, transition, *, actor=None, comment=""):
    """
    Tell the stakeholders that an instance moved state.

    The audience is the transition's declared subscribers plus the roles allowed
    to act from the new state — the people whose turn it now is. A meeting
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

    roles = list(transition.notify_roles.all()) + next_actor_roles(instance)
    return alert(
        kind="workflow-transition",
        instance=instance,
        recipients=stakeholders(instance, roles=roles),
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
        recipients=stakeholders(meeting),
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

    roles = list(transition.notify_roles.all()) + next_actor_roles(meeting)
    return alert(
        kind="meeting-status",
        instance=meeting,
        recipients=stakeholders(meeting, roles=roles),
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
        recipients=stakeholders(meeting),
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
        recipients=stakeholders(meeting),
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
        recipients=referral_audience(referral),
        subject=subject,
        message=message,
        actor=actor,
        email_groups=[referral.referred_to],
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
        recipients=referral_audience(referral),
        subject=subject,
        message=message,
        actor=actor,
        email_groups=[referral.referred_to],
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
        recipients=referral_audience(referral),
        subject=subject,
        message=message,
        actor=actor,
        email_groups=[referral.referred_to],
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
