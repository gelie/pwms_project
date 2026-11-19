"""Turn workflow activity into recorded, delivered alerts.

Three questions shape this module.

*Who?* An :class:`Audience` answers it, built by :func:`stakeholders` (records:
creations, transitions, meeting news) or :func:`referral_audience` (referrals).
Every person in it carries the **reasons** they were chosen, and each reason
carries its own channels, so the answer to "why did I get this?" is on the row:

=================  ====================================  ==============  ============
Reason             Who                                   Bell            Email
=================  ====================================  ==============  ============
``next-actor``     holders of the roles allowed to act   yes             yes
                   next *and* able to transition
``assignee``       ``assigned_to``                       yes             yes
``referred-group`` members of the referred committee     yes             yes
``owner``          ``owner``                             yes             yes
``referrer``       who raised the referral (closings)    yes             yes
``participant``    a meeting's register, chair, ...      yes             yes
``officer``        create-role holders of the owning     per type        per type
                   group (the *oversight* tier)          (default off)   (default on)
``rule:<id>``      a :class:`NotificationRule`           per rule        per rule
=================  ====================================  ==============  ============

*Scope* keeps a role from becoming a blast. Each event resolves its audience in
the groups that event concerns — the **custodian** (the type's owning group) for
records, the **referred group** for referrals, plus the **host group** for
meetings — never in "every group that can see the record" unless a rule opts into
``instance_groups``. Email is further limited to active *staff* who are members
of those groups, so a member (MP) is told in the bell and never mailed.

Two things then narrow the result: the actor is dropped (nobody needs telling
what they just did), and a user's :class:`~pwms.models.NotificationPreference`
mutes a kind — except for people who owe an action on it (see
:mod:`pwms.notifications.preferences`). Anyone who may not open the record is
dropped too, with one deliberate exception: a committee a matter was referred to
is told whether or not it holds an access row yet.

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
from dataclasses import dataclass, field

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.db.models import Q
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import Meeting, Notification, User
from ..models.notifications import (
    EVENT_CREATED,
    EVENT_MEETING_ATTENDANCE,
    EVENT_MEETING_DOCUMENT,
    EVENT_REFERRAL_CLOSED,
    EVENT_REFERRAL_CREATED,
    EVENT_REFERRAL_DEADLINE,
    EVENT_TRANSITION,
    NotificationRule,
)
from ..services.permissions import TRANSITION, VIEW, resolve
from .preferences import apply_preferences

logger = logging.getLogger(__name__)

#: Why a person is in an audience (see the module docstring).
REASON_NEXT_ACTOR = "next-actor"
REASON_ASSIGNEE = "assignee"
REASON_REFERRED_GROUP = "referred-group"
REASON_OWNER = "owner"
REASON_REFERRER = "referrer"
REASON_PARTICIPANT = "participant"
REASON_OFFICER = "officer"
REASON_RULE_PREFIX = "rule:"

#: Reasons that mean the person owes an action. A personal preference never
#: silences these: muting "workflow moved" must not hide "it is your turn".
ACTION_REASONS = frozenset({REASON_NEXT_ACTOR, REASON_ASSIGNEE, REASON_REFERRED_GROUP})

#: Body templates for every alert (see the module docstring).
EMAIL_TEMPLATE = "emails/notification.txt"
HTML_EMAIL_TEMPLATE = "emails/notification.html"

#: ``User.employee_type`` values that identify employees rather than members.
#: ``is_staff_member`` is checked as the explicit override; the ERP sync writes
#: ``employee_type`` and not that flag, so both have to count.
STAFF_EMPLOYEE_TYPES = ("staff", "graduate")


# -- the audience -----------------------------------------------------------
@dataclass
class Recipient:
    """One person to tell, why, and over which channels."""

    user: User
    reasons: list = field(default_factory=list)
    in_app: bool = False
    email: bool = False

    @property
    def must_act(self):
        """True when the person owes an action on this alert (never muted)."""
        return any(reason in ACTION_REASONS for reason in self.reasons)

    @property
    def delivers(self):
        return self.in_app or self.email


class Audience:
    """
    The people an alert goes to, each with the reasons and channels that put them
    there.

    Built up with :meth:`add`, one call per audience rule. A person selected by
    several rules appears once, with every reason and the union of the channels.
    ``groups`` on an ``add`` are the groups the email-eligibility check looks in:
    a recipient is only mailed when they are active staff *in one of them*.
    """

    def __init__(self, instance, *, groups=()):
        self.instance = instance
        self.groups = {}
        self._entries = {}
        for group in groups:
            self.add_group(group)

    def add_group(self, group):
        if group is not None:
            self.groups[group.pk] = group

    def add(
        self,
        users,
        reason,
        *,
        in_app=True,
        email=True,
        groups=None,
        check_view=True,
    ):
        """
        Add ``users`` for ``reason``.

        ``check_view`` drops anyone who may not open the record; the referral
        paths turn it off (see :func:`referral_audience`). ``groups`` defaults to
        the audience's own groups.
        """
        users = [user for user in users if user is not None]
        if not users or not (in_app or email):
            return
        if check_view and self.instance is not None:
            users = _visible(users, self.instance)
        else:
            users = [user for user in users if user.is_active]
        if not users:
            return

        emailable = set()
        if email:
            eligible_in = self.groups.values() if groups is None else groups
            emailable = {user.pk for user in staff_recipients(users, eligible_in)}

        for user in users:
            use_email = user.pk in emailable
            if not (in_app or use_email):
                continue
            entry = self._entries.setdefault(user.pk, Recipient(user))
            if reason not in entry.reasons:
                entry.reasons.append(reason)
            entry.in_app = entry.in_app or in_app
            entry.email = entry.email or use_email

    @property
    def recipients(self):
        return [entry for entry in self._entries.values() if entry.delivers]

    @property
    def users(self):
        return [entry.user for entry in self.recipients]

    def __iter__(self):
        return iter(self.recipients)

    def __len__(self):
        return len(self.recipients)

    def reasons_for(self, user):
        entry = self._entries.get(user.pk)
        return list(entry.reasons) if entry else []


# -- scopes: which groups an event concerns ---------------------------------
def custodian_groups(instance):
    """
    The type's owning group — the unit that runs the instrument.

    A type with no group has no custodian, so the record's own access groups
    stand in: an audience must not silently vanish because the type was seeded
    before its group existed.
    """
    workflow_type = getattr(instance, "workflow_type", None)
    if workflow_type is not None and workflow_type.group_id is not None:
        return [workflow_type.group]
    return instance_groups(instance)


def instance_groups(instance):
    """Every group with an access row on ``instance`` (viewer groups included)."""
    accesses = getattr(instance, "group_accesses", None)
    if not callable(accesses):
        return []
    return [access.group for access in accesses().select_related("group")]


def host_groups(instance):
    """A meeting's convening group, when it has one."""
    group = getattr(instance, "host_group", None)
    return [group] if group is not None else []


def scope_groups(scope, instance, *, referral=None):
    """Resolve a :class:`NotificationRule` scope to concrete groups."""
    if scope == NotificationRule.SCOPE_CUSTODIAN:
        return custodian_groups(instance)
    if scope == NotificationRule.SCOPE_INSTANCE_GROUPS:
        return instance_groups(instance)
    if scope == NotificationRule.SCOPE_HOST_GROUP:
        return host_groups(instance)
    if scope == NotificationRule.SCOPE_REFERRED_GROUP:
        return [referral.referred_to] if referral is not None else []
    return []


def event_groups(instance, *, referral=None):
    """
    The groups an event is *about*: where its default audience may be mailed.

    The custodian, plus the host group of a meeting or the committee of a
    referral. Deliberately narrower than "every group with access": a committee
    that was once given view access does not hear about every later move.
    """
    groups = {group.pk: group for group in custodian_groups(instance)}
    for group in host_groups(instance):
        groups[group.pk] = group
    if referral is not None:
        groups[referral.referred_to_id] = referral.referred_to
    return list(groups.values())


# -- audience building blocks -----------------------------------------------
def _officers(instance):
    """
    Active members of the workflow type's group who hold one of its create roles.

    These are the people who run this instrument — the *oversight* tier: they can
    already see every record in the lists, so by default they get an email and no
    bell count (``WorkflowType.oversight_channel``).
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


def _role_holders(groups, roles):
    """
    Active holders of ``roles`` within ``groups``.

    Always scoped to named groups: ``Role`` is shared across the organisation, so
    "everyone holding *Committee Secretary*" would be a blast at hundreds of
    unrelated people. A ``None`` role means every active member of the groups.
    """
    group_ids = [group.pk for group in groups]
    if not group_ids:
        return []

    membership = {
        "memberships__is_active": True,
        "memberships__group_id__in": group_ids,
    }
    roles = list(roles)
    if roles:
        membership["memberships__role__in"] = roles
    return list(User.objects.filter(is_active=True, **membership).distinct())


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


def next_actor_roles(instance):
    """Roles entitled to perform a transition available from the current state."""
    roles = {}
    for transition in instance.get_available_transitions().prefetch_related(
        "allowed_roles"
    ):
        for role in transition.allowed_roles.all():
            roles[role.pk] = role
    return list(roles.values())


def _add_next_actors(audience, instance):
    """
    Add whoever owes the next move: holders of an allowed role who can transition.

    Holding the role is not enough — a committee that was referred the record
    holds roles in its own group but is deliberately given no ``transition`` (see
    ``sync_referral_access``), so it is not "next" and is not told it is.
    """
    roles = next_actor_roles(instance)
    if not roles:
        return
    groups = instance_groups(instance)
    holders = [
        user
        for user in _role_holders(groups, roles)
        if resolve(user, instance, TRANSITION)
    ]
    audience.add(holders, REASON_NEXT_ACTOR, groups=groups)


def _applicable_rules(instance, event, transition):
    """The active rules of the instance's type for ``event`` (and transition)."""
    rules = NotificationRule.objects.filter(
        workflow_type_id=instance.workflow_type_id, event=event, is_active=True
    ).select_related("role")
    if event == EVENT_TRANSITION and transition is not None:
        return rules.filter(Q(transition__isnull=True) | Q(transition=transition))
    return rules.filter(transition__isnull=True)


def _add_rules(audience, instance, event, *, transition=None, referral=None):
    """Add everyone the type's explicit :class:`NotificationRule`\\ s name."""
    for rule in _applicable_rules(instance, event, transition):
        groups = scope_groups(rule.scope, instance, referral=referral)
        holders = _role_holders(groups, [rule.role] if rule.role_id else [])
        audience.add(
            holders,
            f"{REASON_RULE_PREFIX}{rule.pk}",
            in_app=rule.sends_in_app,
            email=rule.sends_email,
            groups=groups,
            # A referred committee is entitled to know without an access row.
            check_view=rule.scope != NotificationRule.SCOPE_REFERRED_GROUP,
        )


def stakeholders(instance, *, event=EVENT_CREATED, transition=None):
    """
    The audience for a change to a record: who hears about ``event`` on ``instance``.

    Built from the type's declared parts — the people the record names, whoever
    acts next (transitions), the officers at the type's oversight channel, a
    meeting's own register — plus every :class:`NotificationRule` that matches the
    event. Everyone is checked against RBAC: nobody is told about a record they
    cannot open.
    """
    workflow_type = instance.workflow_type
    audience = Audience(instance, groups=event_groups(instance))

    if workflow_type.notify_named_people:
        audience.add([instance.owner], REASON_OWNER)
        audience.add([instance.assigned_to], REASON_ASSIGNEE)

    if event == EVENT_TRANSITION and workflow_type.notify_next_actors:
        _add_next_actors(audience, instance)

    if isinstance(instance, Meeting):
        audience.add(instance.notification_audience(), REASON_PARTICIPANT)

    audience.add(
        _officers(instance),
        REASON_OFFICER,
        in_app=workflow_type.oversight_in_app,
        email=workflow_type.oversight_email,
        groups=custodian_groups(instance),
    )

    _add_rules(audience, instance, event, transition=transition)
    return audience


def referral_audience(referral, *, event=EVENT_REFERRAL_CREATED):
    """
    Who hears about a referral: the referred committee, the people the record
    names, and — once it closes — whoever raised it.

    The committee's members are the ones who owe an action, so that reason is never
    muted. The custodian's officers are *not* in it: a referral is the
    committee's business, not a blast to everyone who runs the instrument.

    Deliberately *not* filtered through :func:`_visible`: a committee may be
    referred a matter it holds no ``WorkflowGroupAccess`` row for — the referral
    itself is the grant of interest — and the message carries only the referral's
    own facts (title, committee, due date), so nothing is disclosed beyond what
    the referral already states. Inactive accounts are still skipped: their
    directory logins are closed, so an alert would only be noise.
    """
    workflow = referral.content_object
    audience = Audience(workflow, groups=event_groups(workflow, referral=referral))
    if workflow is None:
        return audience

    members = [
        membership.user
        for membership in referral.referred_to.members.filter(
            is_active=True, user__is_active=True
        ).select_related("user")
    ]
    audience.add(
        members,
        REASON_REFERRED_GROUP,
        groups=[referral.referred_to],
        check_view=False,
    )

    if workflow.workflow_type.notify_named_people:
        audience.add([workflow.owner], REASON_OWNER, check_view=False)
        audience.add([workflow.assigned_to], REASON_ASSIGNEE, check_view=False)
    if event == EVENT_REFERRAL_CLOSED:
        audience.add([referral.referred_by], REASON_REFERRER, check_view=False)

    _add_rules(audience, workflow, event, referral=referral)
    return audience


def staff_recipients(recipients, groups):
    """
    Keep only the recipients who are active staff in one of ``groups``.

    Staff means the explicit ``is_staff_member`` flag or an ``employee_type`` of
    ``staff`` / ``graduate``. Membership must be *active* and in one of the groups,
    which is what stops a member (MP), a record's owner or an invited guest from
    being emailed merely because they are named. Order is preserved so a caller's
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
    reasons=(),
    html_body="",
    send_in_app=True,
    send_email=True,
):
    """Write the in-app row and/or the queued email row the recipient is due."""
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
        "reasons": list(reasons),
    }

    rows = []
    if send_in_app:
        rows.append(
            Notification.objects.create(
                channel="in_app", status="sent", sent_at=timezone.now(), **shared
            )
        )

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
    audience,
    subject,
    message,
    actor=None,
    context=None,
):
    """
    Record the alert for everyone in ``audience`` on the channels they are due.

    The :class:`Audience` already says who, why and over which channels (the
    bell, email, or both); this drops the actor — nobody needs telling about
    something they just did — applies each person's own mute switches (see
    :mod:`pwms.notifications.preferences`), writes one row per channel and queues
    the mail. Each row keeps the ``reasons`` that put that person in the audience.

    Returns the rows written.
    """
    recipients = [
        recipient
        for recipient in audience
        if actor is None or recipient.user.pk != actor.pk
    ]
    recipients = apply_preferences(recipients, kind)
    if not recipients:
        logger.debug("No recipients for %s alert on %s", kind, instance)
        return []

    url = instance.get_absolute_url() if instance is not None else ""

    rows = []
    for recipient in recipients:
        user = recipient.user
        body = render_body(
            recipient=user,
            message=message,
            url=url,
            instance=instance,
            actor=actor,
            kind=kind,
        )
        html_body = ""
        if recipient.email:
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
                send_in_app=recipient.in_app,
                send_email=recipient.email,
                reasons=recipient.reasons,
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
