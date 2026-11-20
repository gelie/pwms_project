"""Post Bill updates to a webhook and keep the audit trail.

A Bill's ``NEW``, ``EDITS`` and ``STATUS_CHANGE`` updates are posted after the
change is saved, never inside the request/response cycle: the view writes a
:class:`~pwms.models.BillUpdateLog` row and queues a background task, and the
worker does the HTTP call (see :func:`deliver_update`). The log survives the
Bill — the FK is ``SET_NULL`` and the identifying fields are snapshots — so the
trail still answers "what did we send, when, and did it arrive" after the
record is gone.

The target is a testing receiver in this project by default (``/api/v1/
bill-updates/``). Point ``BILL_UPDATE_ENDPOINT_URL`` at the partner system when
it exists; the log shape does not change.
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx
from django.conf import settings
from django.utils import timezone

from ..models import BillUpdateLog

logger = logging.getLogger(__name__)

#: Event names, re-exported so callers do not import the model for a constant.
EVENT_NEW = BillUpdateLog.EVENT_NEW
EVENT_EDITS = BillUpdateLog.EVENT_EDITS
EVENT_STATUS_CHANGE = BillUpdateLog.EVENT_STATUS_CHANGE
EVENT_REJECTION = BillUpdateLog.EVENT_REJECTION

#: ``BillUpdateLog.response_body`` is for diagnosis, not storage.
RESPONSE_BODY_LIMIT = 2000


class BillUpdateDeliveryError(Exception):
    """A post failed in a way the queue should retry."""


def _iso(value):
    """Serialize a date/datetime for the JSON body, ``None`` when unset."""
    if value is None:
        return None
    return value.isoformat()


def _user_ref(user):
    """The published identity of a user, or ``None``."""
    if user is None:
        return None
    return {
        "public_id": str(user.public_id),
        "username": user.username,
        "display_name": user.display_name,
    }


def endpoint_url():
    """The URL a bill update is posted to, or ``""`` when none is configured.

    ``BILL_UPDATE_ENDPOINT_URL`` wins. Left blank, the testing receiver in this
    project is used, built from ``PWMS_BASE_URL`` and ``EXTERNAL_API_PREFIX``.
    """
    configured = (getattr(settings, "BILL_UPDATE_ENDPOINT_URL", "") or "").strip()
    if configured:
        return configured

    base = (getattr(settings, "PWMS_BASE_URL", "") or "").rstrip("/")
    prefix = (getattr(settings, "EXTERNAL_API_PREFIX", "/api/v1/") or "").strip("/")
    if not base:
        return ""
    return f"{base}/{prefix}/bill-updates/"


def rejection_endpoint_url():
    """Where a rejection is posted back, or ``""`` when none is configured.

    The receiving side talks to the sender's API in the same way the sender
    talks to it: one configurable URL, one shared token, one queued task.
    """
    configured = (
        getattr(settings, "BILL_UPDATE_REJECTION_ENDPOINT_URL", "") or ""
    ).strip()
    if configured:
        return configured

    base = (getattr(settings, "PWMS_BASE_URL", "") or "").rstrip("/")
    prefix = (getattr(settings, "EXTERNAL_API_PREFIX", "/api/v1/") or "").strip("/")
    if not base:
        return ""
    return f"{base}/{prefix}/bill-updates/rejections/"


def bill_snapshot(bill):
    """The bill as the webhook carries it (and as the receiver displays it).

    Snapshot fields are copied, not referenced: a post says what the bill looked
    like at the moment of the change, and an approved post stays the accepted
    version even after the source moves on.
    """
    base = (getattr(settings, "PWMS_BASE_URL", "") or "").rstrip("/")
    path = bill.get_absolute_url()
    committee = bill.responsible_committee
    return {
        "public_id": str(bill.public_id),
        "bill_number": bill.bill_number,
        "title": bill.title,
        "short_title": bill.short_title,
        "description": bill.description,
        "bill_type": bill.bill_type,
        "house_of_origin": bill.house_of_origin,
        "sponsor": _user_ref(bill.sponsor),
        "sponsor_name": bill.sponsor_name,
        "introduced_date": _iso(bill.introduced_date),
        "responsible_committee": (
            {"public_id": str(committee.public_id), "name": committee.name}
            if committee is not None
            else None
        ),
        "current_state": bill.current_state.name if bill.current_state_id else "",
        "public_status": bill.public_status,
        "owner": _user_ref(bill.owner),
        "assigned_to": _user_ref(bill.assigned_to),
        "deadline": _iso(bill.deadline),
        "priority": bill.priority,
        "is_overdue": bill.is_overdue,
        "identifier": bill.identifier,
        "url": f"{base}{path}" if (base and path) else path,
    }


def build_payload(
    bill,
    event,
    *,
    update_id=None,
    actor=None,
    transition=None,
    comment="",
    ip_address=None,
):
    """The JSON body for one Bill update.

    Snapshot fields are copied, not referenced: the receiver is told what the
    bill looked like at the moment of the change.
    """
    payload = {
        "update_id": str(update_id or uuid.uuid4()),
        "event": event,
        "occurred_at": timezone.now().isoformat(),
        "bill": bill_snapshot(bill),
        "actor": _user_ref(actor),
    }

    if event == EVENT_STATUS_CHANGE:
        # Status changes carry the move itself and its audit context.
        payload["status_change"] = {
            "from_state": transition.from_state.name if transition else "",
            "to_state": transition.to_state.name if transition else "",
            "transition": transition.name if transition else "",
            "comment": comment,
            "ip_address": ip_address or "",
        }

    return payload


def queue_bill_update(
    bill,
    event,
    *,
    actor=None,
    transition=None,
    comment="",
    ip_address=None,
):
    """Record one outbound update and queue its delivery.

    Returns the ``pending`` :class:`~pwms.models.BillUpdateLog` row, or ``None``
    when the webhook is disabled or has no target URL. Called from the view that
    saved the change, so the log and the task roll back with a rolled-back edit.
    """
    if not getattr(settings, "BILL_UPDATE_WEBHOOK_ENABLED", False):
        return None
    target = endpoint_url()
    if not target:
        logger.warning("Bill update webhook has no endpoint URL; not queueing.")
        return None

    update_id = uuid.uuid4()
    log = BillUpdateLog.objects.create(
        update_id=update_id,
        bill=bill,
        bill_public_id=bill.public_id,
        bill_number=bill.bill_number,
        event=event,
        direction=BillUpdateLog.DIRECTION_OUTBOUND,
        status=BillUpdateLog.STATUS_PENDING,
        payload=build_payload(
            bill,
            event,
            update_id=update_id,
            actor=actor,
            transition=transition,
            comment=comment,
            ip_address=ip_address,
        ),
        target_url=target,
        actor=actor,
    )

    # Imported lazily: ``pwms.tasks`` builds its background-task proxy from this
    # module's ``deliver_update``, so a module-level import would be a cycle.
    from ..tasks import queue_bill_update_post

    queue_bill_update_post(
        log.pk, verbose_name=f"Bill update: {event} {bill.bill_number}"
    )
    return log


def build_rejection_payload(log, reason, *, rejection_id, actor=None, rejected_at=None):
    """The JSON body a receiver posts back when it rejects an update.

    The bill snapshot travels with the decision so the sender can log it even if
    it no longer holds the original message. ``update_id`` identifies the
    rejected message; ``rejection_id`` identifies this rejection message and is
    its idempotency key.
    """
    decided_at = rejected_at or timezone.now()
    return {
        "rejection_id": str(rejection_id),
        "update_id": str(log.update_id),
        "event": log.event,
        "occurred_at": decided_at.isoformat(),
        "bill": log.payload.get("bill") or {},
        "reason": reason,
        "rejected_at": decided_at.isoformat(),
        "rejected_by": _user_ref(actor),
    }


def queue_bill_update_rejection(log, reason, actor=None):
    """Record a rejection and queue its post back to the sender's API.

    ``log`` is the inbound update being rejected. Returns the outbound
    ``REJECTION`` row, or ``None`` when the exchange is disabled or has no
    target. The rejection is its own message (its own ``update_id``), so it does
    not collide with the update it refers to.
    """
    if not getattr(settings, "BILL_UPDATE_WEBHOOK_ENABLED", False):
        return None
    target = rejection_endpoint_url()
    if not target:
        logger.warning("No Bill update rejection endpoint URL; not queueing.")
        return None

    rejection_id = uuid.uuid4()
    rejection = BillUpdateLog.objects.create(
        update_id=rejection_id,
        bill=log.bill,
        bill_public_id=log.bill_public_id,
        bill_number=log.bill_number,
        event=EVENT_REJECTION,
        direction=BillUpdateLog.DIRECTION_OUTBOUND,
        status=BillUpdateLog.STATUS_PENDING,
        payload=build_rejection_payload(
            log, reason, rejection_id=rejection_id, actor=actor
        ),
        target_url=target,
        actor=actor,
        rejection_reason=reason,
        rejected_at=timezone.now(),
        rejected_by_name=getattr(actor, "display_name", "") if actor else "",
    )

    from ..tasks import queue_bill_update_post

    queue_bill_update_post(
        rejection.pk,
        verbose_name=f"Bill update rejection: {log.bill_number}",
    )
    return rejection


def _max_attempts():
    return getattr(settings, "MAX_ATTEMPTS", 3)


def _record_failure(log, reason):
    """Keep the reason, and raise until the attempts are spent."""
    log.error = str(reason)
    if log.attempts >= _max_attempts():
        log.status = BillUpdateLog.STATUS_FAILED
        log.completed_at = timezone.now()
        log.save(
            update_fields=["error", "status", "completed_at", "attempts", "updated_at"]
        )
        logger.warning("Bill update %s failed permanently: %s", log.pk, reason)
        return log

    # Stays pending with the reason, so the log shows why it is still trying.
    log.save(
        update_fields=[
            "error",
            "attempts",
            "http_status",
            "response_body",
            "updated_at",
        ]
    )
    raise BillUpdateDeliveryError(str(reason)) from None


def deliver_update(log_id):
    """Post one queued Bill update and record the outcome.

    Returns the row it acted on, or ``None`` when the row is gone or already
    ``sent`` (a retried task must not post twice). A failed post stays
    ``pending`` with the error and raises, which is how the queue learns to try
    again; once ``MAX_ATTEMPTS`` is reached the row is marked ``failed`` and the
    last run returns quietly.
    """
    log = BillUpdateLog.objects.filter(
        pk=log_id, direction=BillUpdateLog.DIRECTION_OUTBOUND
    ).first()
    if log is None or log.status == BillUpdateLog.STATUS_SENT:
        return None

    log.attempts += 1
    headers = {"Content-Type": "application/json"}
    token = getattr(settings, "BILL_UPDATE_WEBHOOK_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = getattr(settings, "BILL_UPDATE_WEBHOOK_TIMEOUT", 10)

    try:
        response = httpx.post(
            log.target_url,
            content=json.dumps(log.payload, default=str),
            headers=headers,
            timeout=timeout,
        )
    except httpx.HTTPError as error:
        return _record_failure(log, f"{type(error).__name__}: {error}")

    log.http_status = response.status_code
    log.response_body = (response.text or "")[:RESPONSE_BODY_LIMIT]
    if 200 <= response.status_code < 300:
        log.status = BillUpdateLog.STATUS_SENT
        log.error = ""
        log.completed_at = timezone.now()
        log.save(
            update_fields=[
                "attempts",
                "http_status",
                "response_body",
                "status",
                "error",
                "completed_at",
                "updated_at",
            ]
        )
        logger.info(
            "Bill update %s posted to %s (%s)",
            log.pk,
            log.target_url,
            response.status_code,
        )
        return log

    return _record_failure(
        log,
        f"HTTP {response.status_code}: {(response.text or '')[:200]}",
    )
