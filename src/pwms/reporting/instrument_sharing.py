"""Email a workflow record to one or more people.

The detail page's Actions -> Share option has two halves. The *link* half needs no
server work: it is the record's own detail URL, which only opens for someone who
is signed in and may view the record. This module is the *email* half.

An email carries that link, and may also attach the formal document (PDF or HTML,
see :mod:`pwms.reporting.instruments`). The attachment is what makes sharing with
an **external** recipient useful — they have no account, so the link alone would
be a login page. Because that hands record content outside the system, the view
that calls this requires the ``share`` capability and the send is logged.
"""

from __future__ import annotations

import logging

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .instruments import instrument_content
from .sharing import default_from_email

logger = logging.getLogger(__name__)

TEXT_TEMPLATE = "emails/workflow_share.txt"
HTML_TEMPLATE = "emails/workflow_share.html"


def send_instrument_share_email(
    instance,
    *,
    sender,
    recipients,
    link,
    message="",
    attachment_format="",
):
    """
    Email ``link`` (and optionally a rendered copy of ``instance``) to ``recipients``.

    Each recipient gets their own message, so addresses are not disclosed to one
    another — a share to an external party must not reveal who else was told.
    Returns the number of messages sent. A failure propagates: a person who asked
    to share a record deserves to know it did not go.
    """
    recipients = list(recipients)
    if not recipients:
        return 0

    title = instance.title or instance.identifier
    context = {
        "instance": instance,
        "title": title,
        "identifier": instance.identifier,
        "type_name": instance._meta.verbose_name.title(),
        "state": instance.current_state,
        "link": link,
        "sender": sender,
        "message": message,
        "has_attachment": bool(attachment_format),
        "generated_on": timezone.now(),
    }
    subject = f"{instance.identifier}: {title}" if instance.identifier else title
    subject = f"Shared with you: {subject}"
    text_body = render_to_string(TEXT_TEMPLATE, context)
    html_body = render_to_string(HTML_TEMPLATE, context)

    attachment = None
    if attachment_format:
        # Rendered once, on the sender's behalf, and attached to every message.
        attachment = instrument_content(instance, sender, attachment_format)

    reply_to = [sender.email] if getattr(sender, "email", "") else None

    sent = 0
    for address in recipients:
        email = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=default_from_email(),
            to=[address],
            reply_to=reply_to,
        )
        email.attach_alternative(html_body, "text/html")
        if attachment is not None:
            filename, content, media_type = attachment
            email.attach(filename, content, media_type)
        email.send(fail_silently=False)
        sent += 1

    logger.info(
        "%s shared %s %s by email with %s (attachment=%s)",
        sender,
        instance._meta.label,
        instance.public_id,
        ", ".join(recipients),
        attachment_format or "none",
    )
    return sent
