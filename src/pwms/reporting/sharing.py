"""Share a report by link or email.

Two ways to hand a report to somebody, both built on a
:class:`~pwms.models.ReportShare` row:

* **link** — mint a token-addressed URL anyone signed in can open. The link
  renders the *creator's* figures read-only, so it never widens to the reader's
  access, and it can be revoked or given an expiry.
* **email** — send the same link to a list of addresses, optionally attaching
  the exported document so the recipient has the numbers even without signing
  in.

A share can also **repeat** (``schedule``): :func:`send_scheduled_shares` is the
drain the ``send_scheduled_report_shares`` command and the background task both
call, and it emails every due share one interval apart.

An immediate, user-requested send propagates its failure — a person who asked to
send a report deserves to know it did not go. A *scheduled* send does not: one
undeliverable share must not stall the rest of the batch, so the reason is
recorded on the row (``last_error``) and logged instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from ..models import ReportShare
from ..models.reports import SCHEDULE_CHOICES, next_occurrence
from .builder import ReportFilters, build_report
from .exports import export_content

#: The ``schedule`` values that mean "repeat".
SCHEDULED_VALUES = tuple(value for value, _label in SCHEDULE_CHOICES if value != "none")

logger = logging.getLogger(__name__)

TEXT_TEMPLATE = "emails/report_share.txt"
HTML_TEMPLATE = "emails/report_share.html"


def default_from_email():
    """Sender address for share mail (``settings.DEFAULT_FROM_EMAIL``)."""
    return getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@parliament.gov.za")


def site_base_url():
    """Base URL used to make share links absolute outside a request."""
    return getattr(settings, "PWMS_BASE_URL", "").rstrip("/")


def create_share(
    *,
    user,
    filters,
    title="",
    recipients="",
    message="",
    expires_days=None,
    schedule="none",
    schedule_format="",
):
    """
    Mint a share for ``filters``.

    ``expires_days`` sets an expiry; ``schedule`` makes the share repeat, with
    its first repeat one interval out (``next_send_at``) — the immediate send
    the caller makes covers "now".
    """
    now = timezone.now()
    expires_at = None
    if expires_days:
        expires_at = now + timedelta(days=int(expires_days))
    schedule = schedule or "none"
    return ReportShare.objects.create(
        title=title or "",
        filters=filters.to_dict(),
        created_by=user,
        recipients=recipients or "",
        message=message or "",
        expires_at=expires_at,
        schedule=schedule,
        schedule_format=schedule_format or "",
        next_send_at=next_occurrence(now, schedule),
    )


def absolute_share_url(share, request=None):
    """The absolute URL that opens ``share``."""
    path = reverse("pwms:report_shared", args=[share.token])
    if request is not None:
        return request.build_absolute_uri(path)
    base = site_base_url()
    return f"{base}{path}" if base else path


def resolve_share(token):
    """The share for ``token``, or ``None`` when there is no such link."""
    if not token:
        return None
    return ReportShare.objects.select_related("created_by").filter(token=token).first()


def report_for_share(share):
    """
    Rebuild the report a share points at, as its creator saw it.

    ``None`` when the creator's account is gone (the share is then unreadable
    rather than silently widened to the viewer's access).
    """
    if share is None or share.created_by is None:
        return None
    return build_report(share.created_by, ReportFilters.from_dict(share.filters))


def send_share_email(share, *, request=None, report=None, attachment_format=""):
    """
    Email the share link to its recipients, returning how many were addressed.

    ``attachment_format`` names one of :data:`~pwms.reporting.exports.EXPORT_FORMATS`
    to attach the rendered document as well. The link always works while the
    share is active; the attachment is a point-in-time copy.
    """
    recipients = share.recipient_list
    if not recipients:
        return 0

    link = absolute_share_url(share, request)
    context = {
        "share": share,
        "report": report,
        "link": link,
        "sender": share.created_by,
        "title": share.title or (report.title if report else "Report"),
        "generated_on": timezone.now(),
    }

    subject = f"Report shared with you: {context['title']}"
    body = render_to_string(TEXT_TEMPLATE, context)
    # Multi-alternatives so the HTML part rides alongside the plain-text body
    # (an EmailMessage cannot carry both).
    message = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=default_from_email(),
        to=recipients,
    )
    message.attach_alternative(render_to_string(HTML_TEMPLATE, context), "text/html")

    if attachment_format and report is not None:
        filename, content, media_type = export_content(report, attachment_format)
        message.attach(filename, content, media_type)

    message.send(fail_silently=False)
    logger.info("Report share %s emailed to %s", share.pk, ", ".join(recipients))
    return len(recipients)


# -- scheduled delivery -----------------------------------------------------
@dataclass
class ScheduledShareDelivery:
    """The outcome of one scheduled send."""

    share: ReportShare
    recipients: int
    sent: bool
    error: str = ""

    @property
    def title(self):
        return self.share.title or f"Report share {self.share.public_id}"


def due_scheduled_shares(now=None):
    """
    Active, repeating shares whose next send has fallen due, oldest first.

    Filtered in SQL so the delivery job reads only what it will act on, which
    also means a revoke or an expiry takes a share out of the queue at once.
    """
    now = now or timezone.now()
    return (
        ReportShare.objects.filter(
            schedule__in=SCHEDULED_VALUES,
            revoked_at__isnull=True,
            next_send_at__lte=now,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .select_related("created_by")
        .order_by("next_send_at", "id")
    )


def deliver_scheduled_share(share, *, now=None):
    """
    Email one due share and advance its schedule by an interval.

    Failure is recorded, never raised: one undeliverable share must not stall
    the rest of the batch. The schedule advances either way, so a broken address
    is retried at the next interval rather than on every run of the job, and
    ``last_error`` keeps the reason readable.
    """
    now = now or timezone.now()
    recipients = share.recipient_list
    error = ""
    delivered = 0

    if not recipients:
        error = "share has no recipients"
    else:
        try:
            delivered = send_share_email(
                share,
                report=report_for_share(share),
                attachment_format=share.schedule_format,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never re-raised
            error = str(exc)
            logger.warning("Scheduled share %s failed: %s", share.pk, exc)

    # One write for the outcome and the next send time together, so a crash
    # between them cannot lose the advance (or the error).
    share.record_delivery(sent=not error, error=error, when=now)
    return ScheduledShareDelivery(
        share=share, recipients=delivered, sent=not error, error=error
    )


def send_scheduled_shares(*, now=None):
    """
    Deliver every due scheduled share.

    The single entry point shared by the ``send_scheduled_report_shares`` command
    and the background task, so the two can never diverge. Returns a summary
    plus one result per share.
    """
    now = now or timezone.now()
    results = [
        deliver_scheduled_share(share, now=now) for share in due_scheduled_shares(now)
    ]
    summary = {
        "considered": len(results),
        "sent": sum(1 for result in results if result.sent),
        "failed": sum(1 for result in results if not result.sent),
        "recipients": sum(result.recipients for result in results),
    }
    if results:
        logger.info("Scheduled report shares: %s", summary)
    return {"summary": summary, "results": results}
