"""A user's own mute switches, and how they narrow an audience.

The audience rules (``pwms.notifications.dispatch``) decide who *may* be told; a
:class:`~pwms.models.NotificationPreference` lets one person turn a kind of alert
off for themselves, per channel. Two properties keep that safe:

* a preference can only **narrow**: it never adds a recipient or a channel the
  rules did not grant, so the workflow type's configuration stays the source of
  truth for who is told;
* it never silences an alert the person **owes an action on** (assignee, next
  actor, referred committee — ``Recipient.must_act``) nor a kind in
  ``MANDATORY_KINDS`` (a referral deadline). Muting "workflow moved" must not hide
  "it is your turn".

There is no row until someone turns something off, and a row whose switches are
both back on is deleted, so the table only ever holds real exceptions.
"""

from __future__ import annotations

from ..models.notifications import MANDATORY_KINDS, Notification, NotificationPreference

IN_APP_PREFIX = "in_app__"
EMAIL_PREFIX = "email__"


def apply_preferences(recipients, kind):
    """
    Switch off the channels each recipient has muted for ``kind``.

    Works on ``Recipient`` objects (anything with ``user``, ``in_app``, ``email``
    and ``must_act``) and returns those still reachable on some channel.
    """
    recipients = list(recipients)
    if not recipients or kind in MANDATORY_KINDS:
        return recipients

    preferences = {
        row.user_id: row
        for row in NotificationPreference.objects.filter(
            kind=kind, user_id__in=[recipient.user.pk for recipient in recipients]
        )
    }
    for recipient in recipients:
        preference = preferences.get(recipient.user.pk)
        if preference is None or recipient.must_act:
            continue
        if not preference.in_app_enabled:
            recipient.in_app = False
        if not preference.email_enabled:
            recipient.email = False
    return [recipient for recipient in recipients if recipient.delivers]


def preference_rows(user):
    """
    One dict per alert kind for the settings page.

    ``in_app`` / ``email`` are the user's current switches (on unless they turned
    it off); ``locked`` kinds cannot be muted.
    """
    saved = {row.kind: row for row in NotificationPreference.objects.filter(user=user)}
    rows = []
    for kind, label in Notification.alert_kinds():
        row = saved.get(kind)
        rows.append(
            {
                "kind": kind,
                "label": label,
                "in_app": row.in_app_enabled if row else True,
                "email": row.email_enabled if row else True,
                "locked": kind in MANDATORY_KINDS,
            }
        )
    return rows


def save_preferences(user, submitted):
    """
    Store the switches ``submitted`` by the settings form for ``user``.

    ``submitted`` is the set of checked boxes, named ``in_app__<kind>`` and
    ``email__<kind>``: a missing box means *off*. Locked kinds are ignored, and a
    kind left fully on has no row.
    """
    submitted = set(submitted)
    for kind, _label in Notification.alert_kinds():
        if kind in MANDATORY_KINDS:
            continue
        in_app = f"{IN_APP_PREFIX}{kind}" in submitted
        email = f"{EMAIL_PREFIX}{kind}" in submitted
        if in_app and email:
            NotificationPreference.objects.filter(user=user, kind=kind).delete()
        else:
            NotificationPreference.objects.update_or_create(
                user=user,
                kind=kind,
                defaults={"in_app_enabled": in_app, "email_enabled": email},
            )
