"""Per-bill SharePoint folders under the deployment's Bill Folder.

Every bill gets one folder named after its B-number (``Bill.bill_number``,
including the version suffix where one applies, BRS §13.1) inside the single
"Bill Folder" that holds the bill file plan on SharePoint, and the folder is
mirrored locally as a :class:`SharepointFolder` row written to
``Bill.sharepoint_folder`` — the row the attachment picker and uploads treat as
the bill's filing target.

Where that folder lives is a deployment decision, so both Graph ids come from
the environment (:mod:`pwms.settings`):

* ``BILL_DRIVE`` — the Graph drive id holding the Bill Folder;
* ``BILL_FOLDER`` — the Graph *item* id of the Bill Folder itself, which
  becomes the ``parent-item-id`` of the creation call.

Creation goes through Graph's
``POST /drives/{drive-id}/items/{parent-item-id}/children``
(:func:`pwms.utils.sharepoint.create_child_folder`) and is deliberately
idempotent: saving the same bill twice — creation, a later bill-number edit, a
retry after a network blip — meets Graph's 409 ``nameAlreadyExists`` again and
folds that in as *already there* rather than creating a ``"(1)"`` duplicate or
failing the save. The one name collision worth an error is a *file*: nothing
folder-shaped then exists to file documents into.

The mirror is best-effort, the Graph side is not: when SharePoint answers,
:func:`file_bill_folder` has the filing; resolving the local
``SharepointSite``/``SharepointDrive``/``SharepointFolder`` rows may still bail
(unknown drive, a Graph hiccup on the site lookup) and only costs the link,
which the next save self-heals. SharePoint being unreachable must never cost
the user their typed bill: the file path reports failure by raising
:class:`BillFolderError` and the caller decides how loudly to complain;
unconfigured deployments are logged once per call and skipped.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

from ..models import SharepointDrive, SharepointFolder, SharepointSite
from ..utils import sharepoint as graph
from .attachments import resolve_folder, run_graph

logger = logging.getLogger(__name__)

#: Statuses the ensure/file functions report to their callers.
CREATED = "created"
ALREADY_THERE = "already-there"
UNCONFIGURED = "unconfigured"

#: Characters SharePoint refuses in item names. A B-number may legitimately
#: carry some of them (its ``/`` version separator is real, BRS §13.1), so
#: they are folded to ``-`` instead of answered by Graph with a 400.
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"/\\|?*]')

#: SharePoint caps a single item name at 255 characters.
_MAX_NAME_LENGTH = 255


class BillFolderError(Exception):
    """The bill's SharePoint folder could not be filed."""


def _run_graph(awaitable):
    """Run a Graph call from sync code, typed as :class:`BillFolderError`.

    The Graph client raises plain exceptions (its own module keeps the same
    raise-on-failure shape), and callers of the mirroring must only ever catch
    one type, so every failure funnels through here.
    """
    try:
        return run_graph(awaitable)
    except BillFolderError:
        raise
    except Exception as exc:
        raise BillFolderError(f"SharePoint request failed: {exc}") from exc


def filing_configured() -> bool:
    """Whether the deployment told us where the Bill Folder lives."""
    return bool(
        (settings.BILL_DRIVE or "").strip() and (settings.BILL_FOLDER or "").strip()
    )


def folder_name_for(bill_number: str) -> str:
    """The SharePoint-safe folder name for a B-number.

    ``"B.74-2/2025 (AS) "`` files as ``"B.74-2-2025 (AS)"``: none of the
    ``" * : < > ? / \\ |`` characters SharePoint refuses, no trailing dot or
    space for the Windows-side names to fail over the UNC boundary, and never
    longer than :data:`_MAX_NAME_LENGTH`.
    """
    name = _ILLEGAL_NAME_CHARS.sub("-", bill_number.strip())
    return name.rstrip(" .")[:_MAX_NAME_LENGTH]


def ensure_bill_folder(bill_number: str) -> str:
    """Create the bill's SharePoint folder if it does not exist yet.

    Status-only wrapper around :func:`_ensure`; see it for the semantics.
    """
    return _ensure(bill_number)[0]


def file_bill_folder(bill) -> str:
    """Ensure the folder on SharePoint and link the bill to its mirror row.

    The Graph filing is authoritative; the mirroring into
    :class:`~pwms.models.SharepointFolder` and the ``Bill.sharepoint_folder``
    write are best-effort, retried by whatever next saves the bill. Returns a
    status constant; raises :class:`BillFolderError` when SharePoint refused
    the request.
    """
    status, item = _ensure(bill.bill_number)
    if status == UNCONFIGURED:
        return UNCONFIGURED

    folder = _mirror_folder(item)
    if folder is not None and bill.sharepoint_folder_id != folder.pk:
        if (
            bill.sharepoint_folder_id is not None
            and bill.sharepoint_folder.folder_id != item["id"]
        ):
            logger.warning(
                "Bill %s was refiled under folder %s but its old mirror row %s "
                "still stands; the link corrects on the next save.",
                bill.pk,
                item.get("id"),
                bill.sharepoint_folder_id,
            )
        bill.sharepoint_folder = folder
        bill.save(update_fields=["sharepoint_folder"])
    return status


def _ensure(bill_number: str) -> tuple[str, dict | None]:
    """Ensure the folder on SharePoint; return ``(status, graph_item)``.

    One Graph call in the common case; a name that is already taken resolves
    against the folder already there, so re-saving a bill is a no-op on
    SharePoint.
    """
    drive_id = (settings.BILL_DRIVE or "").strip()
    parent_item_id = (settings.BILL_FOLDER or "").strip()
    if not drive_id or not parent_item_id:
        logger.warning(
            "BILL_DRIVE/BILL_FOLDER are not configured; skipped creating the "
            "SharePoint folder for bill %s.",
            bill_number,
        )
        return UNCONFIGURED, None

    name = folder_name_for(bill_number)
    if not name:
        raise BillFolderError(
            f"Bill number {bill_number!r} names no SharePoint folder."
        )

    item, created = _run_graph(
        graph.create_child_folder(
            graph.get_application_token(), drive_id, parent_item_id, name
        )
    )

    if created:
        logger.info(
            "Created SharePoint folder %r (%s) inside the bill folder (%s) of "
            "drive %s.",
            item.get("name"),
            item.get("id"),
            parent_item_id,
            drive_id,
        )
        return CREATED, item
    logger.info(
        "SharePoint folder %r already exists (%s) inside the bill folder; reusing it.",
        item.get("name"),
        item.get("id"),
    )
    return ALREADY_THERE, item


def _mirror_folder(item: dict) -> SharepointFolder | None:
    """Mirror the Graph folder ``item`` locally and return the row.

    The bill's drive must exist as a :class:`SharepointDrive` row for the
    attachment machinery to use the folder; when the deployment never synced
    it, one is resolved from Graph (drive → parent site → local rows) — a
    failure there drops the link only, never the filing.
    """
    drive_id = (settings.BILL_DRIVE or "").strip()
    parent_item_id = (settings.BILL_FOLDER or "").strip()
    drive_row = SharepointDrive.objects.filter(drive_id=drive_id).first()
    if drive_row is None:
        drive_row = _mirror_drive(drive_id)
    if drive_row is None:
        logger.warning(
            "Bill drive %s could not be mirrored locally; the folder is filed "
            "on SharePoint but no SharepointFolder row links to it.",
            drive_id,
        )
        return None
    return resolve_folder(
        drive_row,
        folder_id=item["id"],
        folder_name=item.get("name") or item["id"],
        parent_folder_id=parent_item_id,
    )


def _mirror_drive(drive_id: str) -> SharepointDrive | None:
    """Resolve ``drive_id`` from Graph into local site/drive mirror rows."""
    token_data = graph.get_application_token()
    try:
        drive = _run_graph(graph.get_drive(token_data, drive_id))
    except BillFolderError as exc:
        logger.warning("Could not look up the bill drive: %s", exc)
        return None
    site_id = (drive.get("parentReference") or {}).get("siteId", "")
    if not site_id:
        logger.warning("Drive %s reports no parent site; cannot mirror it.", drive_id)
        return None
    try:
        site = _run_graph(graph.get_site(token_data, site_id))
    except BillFolderError as exc:
        logger.warning("Could not look up the bill drive's site: %s", exc)
        return None

    site_row, _ = SharepointSite.objects.get_or_create(
        site_id=site.get("id", site_id),
        defaults={
            "name": site.get("name") or site_id,
            "url": site.get("webUrl") or "",
        },
    )
    drive_row, _ = SharepointDrive.objects.get_or_create(
        site=site_row,
        drive_id=drive_id,
        defaults={"name": drive.get("name") or drive_id},
    )
    return drive_row
