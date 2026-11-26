"""Render alert messages, write the notification log and queue email."""

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import Notification
from .preferences import apply_preferences

logger = logging.getLogger(__name__)

#: Body templates for every alert (see the dispatch module docstring).
EMAIL_TEMPLATE = "emails/notification.txt"
HTML_EMAIL_TEMPLATE = "emails/notification.html"


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
