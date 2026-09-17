"""SharePoint-backed attachments: browse, link, upload, version and detach.

The web layer is synchronous while the Graph client in
:mod:`pwms.utils.sharepoint` is async, so :func:`run_graph` bridges the two.
Every Graph call authenticates with the DB-cached application token
(:func:`pwms.utils.sharepoint.get_application_token`), so there is no
interactive login and no per-request token exchange.

Three things live here beyond the Graph plumbing:

* **access** — a user may only browse/attach within an enabled SharePoint site
  they are an active :class:`~pwms.models.SharepointSiteMember` of (superusers may
  browse any enabled site);
* **versioning** — :func:`sync_versions` mirrors SharePoint's own version history
  into :class:`~pwms.models.AttachmentVersion` rows, and
  :func:`version_download_url` resolves a retrievable link for one version;
* **the audit trail** — every attach, detach and new version is appended to the
  target record's domain event log (``WorkflowEvent``) by :func:`_record`, so who
  filed which document, and when it was removed, is explicit rather than inferred
  from ``Attachment.created_at``.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import (
    Attachment,
    AttachmentVersion,
    EventType,
    SharepointDrive,
    SharepointFolder,
    SharepointSite,
    SharepointSiteMember,
)
from ..utils import sharepoint as graph

logger = logging.getLogger(__name__)

#: Attachment types an upload/link request is allowed to store.
_ALLOWED_ATTACHMENT_TYPES = {choice[0] for choice in Attachment.ATTACHMENT_TYPE_CHOICES}

#: Graph folder id meaning "the root of the drive".
ROOT = "root"

#: ``EventType`` slugs appended to the target record's domain event log.
EVENT_DOCUMENT_ATTACHED = "document-attached"
EVENT_DOCUMENT_DETACHED = "document-detached"
EVENT_DOCUMENT_VERSION_ADDED = "document-version-added"

#: The slugs above, for filtering a record's timeline down to document facts.
ATTACHMENT_EVENT_SLUGS = (
    EVENT_DOCUMENT_ATTACHED,
    EVENT_DOCUMENT_DETACHED,
    EVENT_DOCUMENT_VERSION_ADDED,
)

#: Sort key standing in for a version SharePoint reports without a timestamp.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AttachmentError(Exception):
    """A SharePoint browse/link/upload request could not be completed."""


def run_graph(coro):
    """Run an async Graph coroutine from synchronous code.

    ``asyncio.run`` covers the ordinary sync-view case. When a loop is already
    running in this thread (an async view, or a test harness) the coroutine is
    driven to completion on a private thread instead, because ``asyncio.run``
    refuses to start a second loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _token():
    """The cached application token, refreshed on demand by the Graph client."""
    return graph.get_application_token()


def _run(awaitable_factory):
    """Run a Graph coroutine, turning any failure into :class:`AttachmentError`.

    The Graph client raises bare :class:`Exception`; views only need to catch
    one type and can render the message to the user. The original traceback is
    still logged for debugging.
    """
    try:
        return run_graph(awaitable_factory())
    except AttachmentError:
        raise
    except Exception as exc:
        logger.exception("SharePoint Graph call failed")
        raise AttachmentError(f"SharePoint request failed: {exc}") from exc


# --- audit trail --------------------------------------------------------------


def _attachment_payload(attachment, **extra):
    """The event payload describing one attachment.

    It is a *snapshot*: a detach event must still name the document and its
    SharePoint coordinates after the ``Attachment`` row itself is gone.
    """
    payload = {
        "attachment_id": str(attachment.public_id),
        "name": attachment.name,
        "item_id": attachment.item_id,
        "drive_id": attachment.drive_id,
        "folder_path": attachment.sharepoint_folder_path,
        "type": attachment.type,
        "size": attachment.size,
        "web_url": attachment.sharepoint_web_url or "",
    }
    payload.update(extra)
    return payload


def _record(target, slug, *, actor, payload, document_url=""):
    """Append a document event to ``target``'s audit trail, best effort.

    Only workflow instances carry a domain event log (``record_event``), so other
    generic targets are skipped. A missing reference ``EventType`` is logged
    rather than raised: registry data having been removed must not stop a user
    filing a document.
    """
    if target is None or not hasattr(target, "record_event"):
        return None
    event_type = EventType.objects.filter(slug=slug).first()
    if event_type is None:
        logger.warning("EventType %r is missing; attachment audit event skipped", slug)
        return None
    return target.record_event(
        event_type, actor=actor, payload=payload, document_url=document_url
    )


def attachment_activity(target, limit=20):
    """Recent attach/detach/new-version events for ``target``, newest first."""
    if target is None or not hasattr(target, "events"):
        return []
    return list(
        target.events().filter(event_type__slug__in=ATTACHMENT_EVENT_SLUGS)[:limit]
    )


# --- browsing -----------------------------------------------------------------


def member_sites(user):
    """SharePoint sites ``user`` may browse, in name order.

    Membership is the entitlement (``SharepointSiteMember`` rows written by
    ``populate_sites``); personal OneDrive sites are never listed, and a site an
    administrator has disabled is hidden from everyone. Superusers see every
    *enabled* mirrored site, which also makes the picker usable before the
    member sync has populated rows.
    """
    sites = SharepointSite.objects.filter(
        enabled=True, is_personal_site=False
    ).order_by("name")
    if user.is_superuser:
        return sites
    member_site_pks = SharepointSiteMember.objects.filter(
        user=user, is_active=True
    ).values_list("site_id", flat=True)
    return sites.filter(pk__in=member_site_pks)


def require_site_access(user, site_id):
    """Return the mirrored site ``site_id``, or raise if ``user`` may not browse it."""
    site = SharepointSite.objects.filter(site_id=site_id).first()
    if site is None:
        raise AttachmentError("Unknown SharePoint site.")
    # ``enabled`` is administrative curation, so it binds superusers too: a
    # disabled site must not be reachable through the picker by anyone.
    if not site.enabled:
        raise AttachmentError("This SharePoint site is disabled.")
    if user.is_superuser:
        return site
    if not SharepointSiteMember.objects.filter(
        user=user, site=site, is_active=True
    ).exists():
        raise AttachmentError("You are not a member of this SharePoint site.")
    return site


def site_drives(site):
    """The mirrored document drives of ``site``, in name order."""
    return site.sharepointdrive_set.order_by("name")


def require_drive_access(user, drive_id):
    """Return the mirrored drive ``drive_id``, or raise if ``user`` may not browse it."""
    drive = (
        SharepointDrive.objects.filter(drive_id=drive_id).select_related("site").first()
    )
    if drive is None:
        raise AttachmentError("Unknown SharePoint drive.")
    require_site_access(user, drive.site.site_id)
    return drive


def folder_children(drive_id, folder_id):
    """Normalised ``(folders, files)`` directly under ``folder_id``.

    ``folder_id`` may be ``"root"`` (or empty) for the drive's root folder.
    """
    response = _run(
        lambda: graph.get_folder_items(_token(), drive_id, folder_id or ROOT)
    )
    folders, files = [], []
    for item in response.get("value") or []:
        # ``item["folder"]`` may be an empty dict (an empty folder), so test for
        # the key rather than truthiness.
        if "folder" in item:
            folders.append(_folder_node(item))
        elif "file" in item:
            files.append(_file_node(item))
    folders.sort(key=lambda node: node["name"].casefold())
    files.sort(key=lambda node: node["name"].casefold())
    return folders, files


def _folder_node(item):
    return {
        "id": item["id"],
        "name": item.get("name") or "Untitled folder",
        "child_count": (item.get("folder") or {}).get("childCount", 0),
        "web_url": item.get("webUrl") or "",
    }


def _file_node(item):
    return {
        "id": item["id"],
        "name": item.get("name") or "Untitled",
        "size": item.get("size"),
        "mimetype": (item.get("file") or {}).get("mimeType") or "",
        "web_url": item.get("webUrl") or "",
        "download_url": item.get("@microsoft.graph.downloadUrl") or "",
        "modified_at": item.get("lastModifiedDateTime"),
    }


# --- attaching ----------------------------------------------------------------


def resolve_folder(drive, folder_id, folder_name="", parent_folder_id=""):
    """Mirror a selected Graph folder locally, so an attachment can reference it.

    Returns ``None`` for the drive root. The mirrored row is only ever an
    upsert — the folder itself is created in SharePoint only by an upload into
    a *non-existent* folder, which this app does not offer.
    """
    if not folder_id or folder_id == ROOT:
        return None
    parent = None
    if parent_folder_id and parent_folder_id != ROOT:
        parent = SharepointFolder.objects.filter(
            site=drive.site, drive=drive, folder_id=parent_folder_id
        ).first()
    folder, _ = SharepointFolder.objects.get_or_create(
        site=drive.site,
        drive=drive,
        folder_id=folder_id,
        defaults={"name": folder_name or folder_id, "parent_folder": parent},
    )
    return folder


def link_document(
    *, obj, user, drive, item_id, folder=None, attachment_type="document"
):
    """Attach an existing SharePoint document (``item_id``) to ``obj``."""
    metadata = _run(lambda: graph.get_item(_token(), drive.drive_id, item_id))
    return _store(
        obj=obj,
        user=user,
        drive=drive,
        folder=folder,
        metadata=metadata,
        attachment_type=attachment_type,
    )


def upload_document(
    *,
    obj,
    user,
    drive,
    filename,
    content,
    folder=None,
    attachment_type="document",
):
    """Upload ``content`` as ``filename`` into ``folder`` and attach it to ``obj``.

    Returns ``(attachment, created)``. SharePoint keys a file by folder +
    filename, so re-uploading a revision comes back as the *same* item: when that
    item is already attached, the existing row is refreshed in place and the
    upload is recorded as a new version, so ``created`` is ``False``.
    """
    metadata = _run(
        lambda: graph.upload_file(
            _token(),
            drive.drive_id,
            folder.folder_id if folder else ROOT,
            filename,
            content,
        )
    )
    if not metadata:
        raise AttachmentError("SharePoint returned no metadata for the uploaded file.")

    existing = Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(obj),
        object_id=str(obj.pk),
        item_id=metadata["id"],
    ).first()
    if existing is not None:
        # A superseding revision: keep the original classification, refresh the
        # metadata the caller would otherwise see stale.
        return (
            _refresh(
                existing, metadata=metadata, drive=drive, folder=folder, actor=user
            ),
            False,
        )
    return (
        _store(
            obj=obj,
            user=user,
            drive=drive,
            folder=folder,
            metadata=metadata,
            attachment_type=attachment_type,
        ),
        True,
    )


def _store(*, obj, user, drive, folder, metadata, attachment_type):
    """Create the local ``Attachment`` row for a SharePoint item's metadata."""
    if attachment_type not in _ALLOWED_ATTACHMENT_TYPES:
        attachment_type = "document"
    try:
        with transaction.atomic():
            attachment = Attachment.objects.create(
                content_type=ContentType.objects.get_for_model(obj),
                object_id=str(obj.pk),
                name=metadata.get("name") or "",
                drive_id=drive.drive_id,
                item_id=metadata["id"],
                mimetype=(metadata.get("file") or {}).get("mimeType") or "",
                size=metadata.get("size"),
                download_url=metadata.get("@microsoft.graph.downloadUrl") or "",
                sharepoint_web_url=metadata.get("webUrl") or "",
                sharepoint_site=drive.site,
                sharepoint_drive=drive,
                sharepoint_folder=folder,
                sharepoint_folder_path=folder.get_full_path() if folder else "",
                type=attachment_type,
                uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            )
    except IntegrityError as exc:
        raise AttachmentError(
            "That document is already attached to this record."
        ) from exc
    _record(
        obj,
        EVENT_DOCUMENT_ATTACHED,
        actor=user,
        payload=_attachment_payload(attachment),
        document_url=attachment.sharepoint_web_url,
    )
    return attachment


def _refresh(attachment, *, metadata, drive, folder, actor):
    """Re-point an already-attached file at the revision just uploaded."""
    attachment.name = metadata.get("name") or attachment.name
    attachment.mimetype = (metadata.get("file") or {}).get("mimeType") or ""
    attachment.size = metadata.get("size")
    attachment.download_url = metadata.get("@microsoft.graph.downloadUrl") or ""
    attachment.sharepoint_web_url = metadata.get("webUrl") or ""
    attachment.sharepoint_site = drive.site
    attachment.sharepoint_drive = drive
    attachment.sharepoint_folder = folder
    attachment.sharepoint_folder_path = folder.get_full_path() if folder else ""
    attachment.save()
    _record(
        attachment.content_object,
        EVENT_DOCUMENT_VERSION_ADDED,
        actor=actor,
        payload=_attachment_payload(attachment),
        document_url=attachment.sharepoint_web_url,
    )
    return attachment


def detach_document(attachment, *, actor):
    """Remove an attachment, recording who detached it.

    The payload is captured before the row is deleted, so the trail still names
    the document afterwards. The file itself is never deleted from SharePoint.
    """
    target = attachment.content_object
    payload = _attachment_payload(attachment)
    document_url = attachment.sharepoint_web_url or attachment.download_url or ""
    attachment.delete()
    _record(
        target,
        EVENT_DOCUMENT_DETACHED,
        actor=actor,
        payload=payload,
        document_url=document_url,
    )


def attachments_for(obj):
    """Attachments linked to ``obj`` (newest first), with their references warmed."""
    return obj.attachments.select_related("uploaded_by", "sharepoint_site")


# --- version history ----------------------------------------------------------


def sync_versions(attachment):
    """Refresh the local mirror of ``attachment``'s SharePoint version history.

    Returns the attachment's versions, newest first. Unknown remote versions are
    inserted and known ones refreshed; a version SharePoint no longer reports is
    left in place rather than deleted (history is evidence, and a mirror only
    ever adds).
    """
    response = _run(
        lambda: graph.get_item_versions(
            _token(), attachment.drive_id, attachment.item_id
        )
    )
    versions = []
    for raw in response.get("value") or []:
        version_id = str(raw.get("id") or "").strip()
        if not version_id:
            continue
        version, _ = AttachmentVersion.objects.update_or_create(
            attachment=attachment,
            version_id=version_id,
            defaults={
                "size": raw.get("size"),
                "modified_at": _as_aware(
                    parse_datetime(raw.get("lastModifiedDateTime") or "")
                ),
                "modified_by": _version_author(raw),
            },
        )
        versions.append(version)
    _mark_current(attachment, versions)
    return attachment.versions.all()


def _as_aware(value):
    """Coerce a parsed timestamp into the project timezone, tolerating ``None``."""
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value)
    return value


def _version_author(raw):
    """The display name SharePoint reports for a version's last modifier."""
    user = (raw.get("lastModifiedBy") or {}).get("user") or {}
    return user.get("displayName") or user.get("email") or ""


def _mark_current(attachment, versions):
    """Flag the newest of ``versions`` as current and clear the flag on the rest."""
    if not versions:
        return None
    newest = max(versions, key=lambda version: version.modified_at or _EPOCH)
    AttachmentVersion.objects.filter(attachment=attachment).exclude(
        pk=newest.pk
    ).update(is_current=False)
    if not newest.is_current:
        AttachmentVersion.objects.filter(pk=newest.pk).update(is_current=True)
    return newest


def version_download_url(attachment, version):
    """The pre-authenticated SharePoint URL holding one version's content."""
    url = _run(
        lambda: graph.get_version_download_url(
            _token(), attachment.drive_id, attachment.item_id, version.version_id
        )
    )
    if not url:
        raise AttachmentError(
            "SharePoint did not return a download link for that version."
        )
    return url


def open_url(attachment):
    """A fresh pre-authenticated URL that opens the attachment's document.

    Graph's ``@microsoft.graph.downloadUrl`` carries its own short-lived
    authorisation, so the browser fetches the document **without the reader
    signing in to SharePoint** — the application token resolved the link. It is
    looked up per request rather than read off the row because SharePoint issues
    these for about an hour, so the ``download_url`` copied onto the
    :class:`~pwms.models.Attachment` is usually stale by the time anyone clicks.
    """
    metadata = _run(
        lambda: graph.get_item(_token(), attachment.drive_id, attachment.item_id)
    )
    url = metadata.get("@microsoft.graph.downloadUrl") or ""
    if not url:
        raise AttachmentError(
            "SharePoint did not return an openable link for that document."
        )
    return url
