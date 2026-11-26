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

# This module is kept as the public facade: the implementation is split into
# ``audience`` (who and why), ``delivery`` (the log and the mail) and ``events``
# (the per-event entry points). Existing imports keep working unchanged.
from .audience import (
    ACTION_REASONS,
    REASON_ASSIGNEE,
    REASON_NEXT_ACTOR,
    REASON_OFFICER,
    REASON_OWNER,
    REASON_PARTICIPANT,
    REASON_REFERRED_GROUP,
    REASON_REFERRER,
    REASON_RULE_PREFIX,
    STAFF_EMPLOYEE_TYPES,
    Audience,
    Recipient,
    custodian_groups,
    event_groups,
    host_groups,
    instance_groups,
    next_actor_roles,
    referral_audience,
    scope_groups,
    staff_recipients,
    stakeholders,
)
from .delivery import (
    EMAIL_TEMPLATE,
    HTML_EMAIL_TEMPLATE,
    NotificationDeliveryError,
    alert,
    default_from_email,
    deliver_email,
    max_attempts,
    queue_email,
    record_email,
    render_body,
    render_html_body,
    site_base_url,
)
from .events import (
    REFERRAL_CLOSED_LABELS,
    notify_meeting_attendance,
    notify_meeting_created,
    notify_meeting_document,
    notify_meeting_status,
    notify_referral_closed,
    notify_referral_created,
    notify_referral_deadline,
    notify_transition,
    notify_workflow_created,
)

__all__ = [
    "ACTION_REASONS",
    "EMAIL_TEMPLATE",
    "HTML_EMAIL_TEMPLATE",
    "REASON_ASSIGNEE",
    "REASON_NEXT_ACTOR",
    "REASON_OFFICER",
    "REASON_OWNER",
    "REASON_PARTICIPANT",
    "REASON_REFERRED_GROUP",
    "REASON_REFERRER",
    "REASON_RULE_PREFIX",
    "REFERRAL_CLOSED_LABELS",
    "STAFF_EMPLOYEE_TYPES",
    "Audience",
    "NotificationDeliveryError",
    "Recipient",
    "alert",
    "custodian_groups",
    "default_from_email",
    "deliver_email",
    "event_groups",
    "host_groups",
    "instance_groups",
    "max_attempts",
    "next_actor_roles",
    "notify_meeting_attendance",
    "notify_meeting_created",
    "notify_meeting_document",
    "notify_meeting_status",
    "notify_referral_closed",
    "notify_referral_created",
    "notify_referral_deadline",
    "notify_transition",
    "notify_workflow_created",
    "queue_email",
    "record_email",
    "referral_audience",
    "render_body",
    "render_html_body",
    "scope_groups",
    "site_base_url",
    "staff_recipients",
    "stakeholders",
]
