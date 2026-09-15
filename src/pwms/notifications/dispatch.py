"""Turn workflow activity into recorded, delivered alerts.

Three questions shape this module.

*Who?* :func:`stakeholders` answers it for role-driven work: the officers who run
the workflow type, anyone a ``Transition`` names as a subscriber
(``notify_roles``), whoever can act next, and the two people the record itself
names (``owner`` / ``assigned_to``). :func:`referral_audience` answers it for
referrals, where the referral is what entitles the committee to know.

*What?* Messages are rendered from ``templates/emails/notification.txt`` and the
rendered text is stored on the row, so the alert explains itself later.

*When?* Rows are written immediately, inside the caller's transaction, but the
mail is sent with :func:`django.db.transaction.on_commit`, so a workflow change
that rolls back never mails anybody.

Every dispatch writes two rows per recipient — one ``in_app`` (the bell) and one
``email`` — and the email row carries the delivery outcome. The
:class:`~pwms.models.Notification` table is therefore the log of what was sent,
to whom, and whether it worked.

Nothing here raises. A notification is a side effect of the work, so a dead mail
server must not fail the transition that triggered it; the failure is recorded on
the row and logged instead.
"""

from __future__ import annotations

import logging
from functools import partial

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import Notification, User
from ..services.permissions import VIEW, resolve

logger = logging.getLogger(__name__)

#: Body template for every alert (see the module docstring).
EMAIL_TEMPLATE = "emails/notification.txt"


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
    on the record — minus anyone who cannot view it.
    """
    candidates = _officers(instance) + _role_holders(instance, roles) + _named(instance)
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


# -- composition & delivery -------------------------------------------------
def default_from_email():
    """Sender address for alerts (``settings.DEFAULT_FROM_EMAIL``)."""
    return getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@parliament.gov.za")


def site_base_url():
    """Base URL used to make alert links absolute in email."""
    return getattr(settings, "PWMS_BASE_URL", "").rstrip("/")


def render_body(*, recipient, message, url="", instance=None, actor=None, kind=""):
    """Render one recipient's message body from the email template."""
    base = site_base_url()
    return render_to_string(
        EMAIL_TEMPLATE,
        {
            "recipient": recipient,
            "message": message,
            "path": url,
            "link": f"{base}{url}" if (base and url) else url,
            "instance": instance,
            "actor": actor,
            "kind": kind,
            "kind_label": dict(Notification.KIND_CHOICES).get(kind, kind),
        },
    )


def deliver_email(notification_id):
    """
    Send one queued email row and record what happened.

    Runs through ``transaction.on_commit``, so it never executes for work that was
    rolled back. Failures are recorded on the row rather than raised: losing an
    alert must not break the request that produced it.
    """
    row = (
        Notification.objects.select_related("recipient")
        .filter(pk=notification_id)
        .first()
    )
    if row is None or row.status != "pending":
        return None

    recipient = row.recipient
    if not recipient.email:
        return row.mark_failed("recipient has no email address")

    try:
        send_mail(
            subject=row.subject,
            message=row.body,
            from_email=default_from_email(),
            recipient_list=[recipient.email],
            fail_silently=False,
        )
    except Exception as error:  # noqa: BLE001 - recorded, never re-raised
        logger.warning(
            "Notification %s to %s failed: %s", row.pk, recipient.email, error
        )
        return row.mark_failed(error)

    logger.info("Notification %s emailed to %s (%s)", row.pk, recipient.email, row.kind)
    return row.mark_sent()


def _record(*, recipient, kind, subject, body, url, instance, actor, context):
    """Write the in-app row and the queued email row for one recipient."""
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

    if recipient.email:
        email_row = Notification.objects.create(channel="email", **shared)
        rows.append(email_row)
        # Deferred: the alert only goes out if the workflow change commits.
        transaction.on_commit(partial(deliver_email, email_row.pk))

    return rows


def alert(*, kind, instance, recipients, subject, message, actor=None, context=None):
    """
    Record one alert for each recipient and queue its email.

    Returns the rows written. The actor is dropped from the audience — nobody
    needs telling about something they just did.
    """
    audience = [user for user in recipients if actor is None or user.pk != actor.pk]
    if not audience:
        logger.debug("No recipients for %s alert on %s", kind, instance)
        return []

    url = instance.get_absolute_url() if instance is not None else ""
    rows = []
    for user in audience:
        rows.extend(
            _record(
                recipient=user,
                kind=kind,
                subject=subject,
                body=render_body(
                    recipient=user,
                    message=message,
                    url=url,
                    instance=instance,
                    actor=actor,
                    kind=kind,
                ),
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
    to act from the new state — the people whose turn it now is.
    """
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
