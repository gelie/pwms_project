"""Bill update webhook receiver (the testing stand-in)."""

from django.db import IntegrityError
from django.utils import timezone
from ninja.errors import HttpError

from ...models import Bill, BillUpdateLog
from ..app import bill_updates_router
from ..schemas import (
    BillUpdateIn,
    BillUpdateReceiptOut,
    BillUpdateRejectionIn,
    BillUpdateRejectionReceiptOut,
    ErrorOut,
)


def _receipt(log, *, duplicate=False):
    return {
        "update_id": log.update_id,
        "event": log.event,
        "received_at": log.created_at,
        "duplicate": duplicate,
        "public_status": (log.payload.get("bill") or {}).get("public_status", ""),
    }


def _rejection_receipt(log, *, duplicate=False):
    return {
        "rejection_id": log.update_id,
        "update_id": log.payload.get("update_id"),
        "received_at": log.created_at,
        "duplicate": duplicate,
    }


@bill_updates_router.post(
    "/",
    response={200: BillUpdateReceiptOut, 404: ErrorOut},
    summary="Receive a Bill update (testing stand-in for the external API)",
)
def receive_bill_update(request, payload: BillUpdateIn):
    """
    Accept one ``NEW`` / ``EDITS`` / ``STATUS_CHANGE`` update and keep it.

    This is the testing stand-in for the partner system's endpoint. It never
    mutates the Bill: it checks that the referenced bill still exists, stores
    the exact body as an inbound ``BillUpdateLog`` row and returns a receipt.
    Idempotent on ``update_id`` — a retried outbound post returns the original
    receipt instead of writing a second row.
    """
    existing = BillUpdateLog.objects.filter(
        update_id=payload.update_id, direction=BillUpdateLog.DIRECTION_INBOUND
    ).first()
    if existing is not None:
        return 200, _receipt(existing, duplicate=True)

    body = payload.model_dump(mode="json")
    bill = None
    if payload.bill.public_id is not None:
        bill = Bill.objects.filter(public_id=payload.bill.public_id).first()

    if bill is None:
        # Rejections are evidence too: keep why the post was refused.
        try:
            BillUpdateLog.objects.create(
                update_id=payload.update_id,
                bill_public_id=payload.bill.public_id,
                bill_number=payload.bill.bill_number,
                event=payload.event,
                direction=BillUpdateLog.DIRECTION_INBOUND,
                status=BillUpdateLog.STATUS_REJECTED,
                payload=body,
                target_url=request.path[:2048],
                error="Unknown bill public_id.",
            )
        except IntegrityError:
            existing = BillUpdateLog.objects.filter(
                update_id=payload.update_id,
                direction=BillUpdateLog.DIRECTION_INBOUND,
            ).first()
            return 200, _receipt(existing, duplicate=True)
        raise HttpError(404, "Unknown bill for this update.")

    try:
        log = BillUpdateLog.objects.create(
            update_id=payload.update_id,
            bill=bill,
            bill_public_id=bill.public_id,
            bill_number=bill.bill_number,
            event=payload.event,
            direction=BillUpdateLog.DIRECTION_INBOUND,
            status=BillUpdateLog.STATUS_RECEIVED,
            payload=body,
            target_url=request.path[:2048],
        )
    except IntegrityError:
        # A concurrent retry won the race; answer with its receipt.
        existing = BillUpdateLog.objects.filter(
            update_id=payload.update_id,
            direction=BillUpdateLog.DIRECTION_INBOUND,
        ).first()
        return 200, _receipt(existing, duplicate=True)

    return 200, _receipt(log)


@bill_updates_router.post(
    "/rejections/",
    response={200: BillUpdateRejectionReceiptOut},
    summary="Receive a rejection decision from the consuming API",
)
def receive_bill_update_rejection(request, payload: BillUpdateRejectionIn):
    """
    Log one update's rejection, posted back by the consuming API.

    This is the other half of the bi-directional exchange: the receiver decides
    an update is wrong and tells the sender, which records the outcome on the
    message it originally sent. The same shared token authenticates the call,
    and ``rejection_id`` makes a retried callback idempotent. The rejection is
    logged even when the original message is no longer present.
    """
    existing = BillUpdateLog.objects.filter(
        update_id=payload.rejection_id,
        direction=BillUpdateLog.DIRECTION_INBOUND,
        event=BillUpdateLog.EVENT_REJECTION,
    ).first()
    if existing is not None:
        return 200, _rejection_receipt(existing, duplicate=True)

    body = payload.model_dump(mode="json")
    bill = None
    if payload.bill is not None and payload.bill.public_id is not None:
        bill = Bill.objects.filter(public_id=payload.bill.public_id).first()

    rejected_by_name = ""
    if payload.rejected_by is not None:
        rejected_by_name = (
            payload.rejected_by.display_name or payload.rejected_by.username
        )
    rejected_at = payload.rejected_at or timezone.now()

    try:
        log = BillUpdateLog.objects.create(
            update_id=payload.rejection_id,
            bill=bill,
            bill_public_id=payload.bill.public_id if payload.bill else None,
            bill_number=payload.bill.bill_number if payload.bill else "",
            event=BillUpdateLog.EVENT_REJECTION,
            direction=BillUpdateLog.DIRECTION_INBOUND,
            status=BillUpdateLog.STATUS_RECEIVED,
            payload=body,
            target_url=request.path[:2048],
            rejection_reason=payload.reason,
            rejected_at=rejected_at,
            rejected_by_name=rejected_by_name,
        )
    except IntegrityError:
        # A concurrent retry won the race; answer with its receipt.
        existing = BillUpdateLog.objects.filter(
            update_id=payload.rejection_id,
            direction=BillUpdateLog.DIRECTION_INBOUND,
        ).first()
        return 200, _rejection_receipt(existing, duplicate=True)

    # Stamp the sender's original message, so its log answers "was this
    # accepted?" without joining the two sides.
    original = BillUpdateLog.objects.filter(
        update_id=payload.update_id,
        direction=BillUpdateLog.DIRECTION_OUTBOUND,
    ).first()
    if original is not None:
        original.rejection_reason = payload.reason
        original.rejected_at = rejected_at
        original.rejected_by_name = rejected_by_name
        original.save(
            update_fields=[
                "rejection_reason",
                "rejected_at",
                "rejected_by_name",
                "updated_at",
            ]
        )

    return 200, _rejection_receipt(log)
