"""Who an alert goes to, and why (the ``Audience`` model)."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Q

from ..models import Meeting, User
from ..models.notifications import (
    EVENT_CREATED,
    EVENT_REFERRAL_CLOSED,
    EVENT_REFERRAL_CREATED,
    EVENT_TRANSITION,
    NotificationRule,
)
from ..services.permissions import TRANSITION, VIEW, resolve

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
