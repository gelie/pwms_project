import logging

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.http import (
    Http404,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import (
    Attachment,
    AttachmentVersion,
)
from ..services import attachments as attachments_service
from ..services import bill_folders
from ..services.permissions import (
    EDIT,
    VIEW,
    require,
    resolve,
)

logger = logging.getLogger(__name__)

from .common import _record_counters


def _attachment_target(request, action):
    """Resolve a request's ``content_type``/``object_id`` to an allowed record."""
    raw_type = request.GET.get("content_type") or request.POST.get("content_type")
    object_id = request.GET.get("object_id") or request.POST.get("object_id")
    app_label, separator, model_name = (raw_type or "").partition(".")
    content_type = (
        ContentType.objects.filter(app_label=app_label, model=model_name).first()
        if separator
        else None
    )
    model = content_type.model_class() if content_type is not None else None
    if model is None or not object_id:
        raise Http404("Unknown attachment target.")
    target = get_object_or_404(model, pk=object_id)
    require(request.user, target, action)
    return target


def _bill_filing_folder(target):
    """A bill's own SharePoint folder; ``None`` for any other record — or bill.

    Documents uploaded for a bill land here regardless of what the picker
    posted — the record's EDIT right governs the write, so drive membership is
    not re-checked. Linking an *existing* document keeps its real location: an
    upload creates content, a link only records where a document already lives.
    """
    if getattr(target, "_meta", None) is None or target._meta.model_name != "bill":
        return None
    return target.sharepoint_folder


def _attachment_context(request, target):
    """Template context for the attachment section/partials of one record."""
    meta = target._meta
    attachments = attachments_service.attachments_for(target)
    # Bills file into their own SharePoint folder (service-linked on save); the
    # chip and the picker's default pane read it. Other records have neither.
    bill_folder = getattr(target, "sharepoint_folder", None)
    is_bill = meta.model_name == "bill"
    return {
        "attachment_target": target,
        "attachment_content_type": f"{meta.app_label}.{meta.model_name}",
        "attachment_object_id": str(target.pk),
        "attachments": attachments,
        "attachment_events": attachments_service.attachment_activity(target),
        # Which documents an activity row names are still attached: those open
        # through the record's own endpoint, while a detached document keeps only
        # the snapshot URL its event recorded.
        "live_attachment_ids": {
            str(attachment.public_id) for attachment in attachments
        },
        "can_attach": resolve(request.user, target, EDIT),
        "attachment_bill_folder": bill_folder,
        "attachment_is_bill": is_bill,
        "sharepoint_filing_configured": bill_folders.filing_configured(),
    }


def attachment_browser(request):
    """Render the SharePoint document picker for a record (HTMX fragment)."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    context["attachment_sites"] = attachments_service.member_sites(request.user)
    # Preselect an attachment type (a meeting preselects agenda or minutes), so
    # the picker files the document under the right classification by default.
    context["attachment_default_type"] = request.GET.get("default_type", "")
    # A bill with a filed folder opens right on it: loading its contents pane on
    # display, but only when the user may use the drive the folder lives on.
    bill_folder = context["attachment_bill_folder"]
    if bill_folder is not None:
        try:
            attachments_service.require_drive_access(
                request.user, bill_folder.drive.drive_id
            )
        except attachments_service.AttachmentError:
            bill_folder = None
    context["attachment_default_folder"] = bill_folder
    return render(request, "pwms/partials/attachment_browser.html", context)


def attachment_tree_children(request):
    """One tree level: a site's drives, or a folder's subfolders."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    context["attachment_default_type"] = request.GET.get("default_type", "")
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    drive_id = request.GET.get("drive_id")
    try:
        if drive_id:
            drive = attachments_service.require_drive_access(request.user, drive_id)
            subfolders, _ = attachments_service.folder_children(
                drive.drive_id, folder_id
            )
            context["attachment_subfolders"] = subfolders
            context["attachment_drive_id"] = drive.drive_id
            context["attachment_parent_folder_id"] = folder_id
        else:
            site = attachments_service.require_site_access(
                request.user, request.GET.get("site_id")
            )
            context["attachment_drives"] = attachments_service.site_drives(site)
    except attachments_service.AttachmentError as exc:
        context["attachment_error"] = str(exc)
    return render(request, "pwms/partials/attachment_tree_children.html", context)


def attachment_folder(request):
    """The files in a selected folder, with the upload form aimed at it."""
    target = _attachment_target(request, EDIT)
    context = _attachment_context(request, target)
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    context.update(
        {
            "attachment_folder_id": folder_id,
            "attachment_folder_name": request.GET.get("folder_name") or "Root",
            "attachment_parent_folder_id": request.GET.get("parent_folder_id") or "",
            "attachment_types": Attachment.ATTACHMENT_TYPE_CHOICES,
            "attachment_default_type": request.GET.get("default_type", ""),
        }
    )
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.GET.get("drive_id")
        )
        _, files = attachments_service.folder_children(drive.drive_id, folder_id)
    except attachments_service.AttachmentError as exc:
        context["attachment_error"] = str(exc)
        return render(request, "pwms/partials/attachment_folder_contents.html", context)
    context["attachment_drive"] = drive
    context["attachment_files"] = files
    return render(request, "pwms/partials/attachment_folder_contents.html", context)


@require_POST
def attachment_link(request):
    """Attach an existing SharePoint document to the current record."""
    target = _attachment_target(request, EDIT)
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.POST.get("drive_id")
        )
        folder = attachments_service.resolve_folder(
            drive,
            request.POST.get("folder_id"),
            request.POST.get("folder_name", ""),
            request.POST.get("parent_folder_id", ""),
        )
        attachment = attachments_service.link_document(
            obj=target,
            user=request.user,
            drive=drive,
            item_id=request.POST.get("item_id", ""),
            folder=folder,
            attachment_type=request.POST.get("type", "document"),
        )
    except attachments_service.AttachmentError as exc:
        return _attachment_response(request, target, error=str(exc))
    return _attachment_response(
        request, target, success=f'"{attachment.name}" attached.'
    )


@require_POST
def attachment_upload(request):
    """Upload a file into the selected folder and attach it in one step."""
    target = _attachment_target(request, EDIT)
    upload = request.FILES.get("file")
    if upload is None:
        return _attachment_response(request, target, error="No file was selected.")
    try:
        # A bill with a filed folder is the destination, whatever the picker
        # posted: the record's EDIT right governs the write, so drive membership
        # is not re-checked. Other records keep the purely picker-driven flow.
        forced_folder = _bill_filing_folder(target)
        if forced_folder is not None:
            drive, folder = forced_folder.drive, forced_folder
        else:
            drive = attachments_service.require_drive_access(
                request.user, request.POST.get("drive_id")
            )
            folder = attachments_service.resolve_folder(
                drive,
                request.POST.get("folder_id"),
                request.POST.get("folder_name", ""),
                request.POST.get("parent_folder_id", ""),
            )
        attachment, created = attachments_service.upload_document(
            obj=target,
            user=request.user,
            drive=drive,
            filename=upload.name,
            content=upload.read(),
            folder=folder,
            attachment_type=request.POST.get("type", "document"),
        )
    except attachments_service.AttachmentError as exc:
        return _attachment_response(request, target, error=str(exc))
    if created:
        message = f'"{attachment.name}" uploaded and attached.'
    else:
        # SharePoint kept the existing item and added a version to it.
        message = f'"{attachment.name}" uploaded as a new version.'
    if forced_folder is not None:
        message += f' Filed into the bill folder "{forced_folder.name}".'
    return _attachment_response(request, target, success=message)


@require_POST
def attachment_delete(request, public_id):
    """Detach a document from the record, leaving the file in SharePoint."""
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, EDIT)
    name = attachment.name
    # Detach through the service so the removal lands on the audit trail.
    attachments_service.detach_document(attachment, actor=request.user)
    return _attachment_response(request, target, success=f'"{name}" removed.')


def attachment_versions(request, public_id):
    """Version history for one attachment, refreshed from SharePoint."""
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    context = _attachment_context(request, target)
    context["attachment"] = attachment
    try:
        context["versions"] = attachments_service.sync_versions(attachment)
    except attachments_service.AttachmentError as exc:
        # Show whatever was mirrored last rather than an empty panel.
        context["attachment_error"] = str(exc)
        context["versions"] = attachment.versions.all()
    return render(request, "pwms/partials/attachment_versions.html", context)


def attachment_version_download(request, public_id):
    """Send the browser to SharePoint's pre-authenticated URL for one version."""
    version = get_object_or_404(AttachmentVersion, public_id=public_id)
    attachment = version.attachment
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    try:
        url = attachments_service.version_download_url(attachment, version)
    except attachments_service.AttachmentError as exc:
        messages.error(request, str(exc))
        return redirect(target.get_absolute_url() or reverse("pwms:dashboard"))
    return HttpResponseRedirect(url)


def attachment_open(request, public_id):
    """Send the browser to a fresh pre-authenticated URL for the document.

    This is what the attachment's name links to. The document opens under the
    application's own authorisation — see
    :func:`pwms.services.attachments.open_url` — so the reader is never asked to
    sign in to SharePoint. VIEW on the owning record is required, exactly as for
    its detail page, so the endpoint can only hand out documents the reader could
    already open.
    """
    attachment = get_object_or_404(Attachment, public_id=public_id)
    target = attachment.content_object
    if target is None:
        raise Http404("Attachment is not linked to a record.")
    require(request.user, target, VIEW)
    try:
        url = attachments_service.open_url(attachment)
    except attachments_service.AttachmentError as exc:
        messages.error(request, str(exc))
        return redirect(target.get_absolute_url() or reverse("pwms:dashboard"))
    return HttpResponseRedirect(url)


def _attachment_response(request, target, *, success="", error=""):
    """Re-render the attachment list, announcing a change when there is one."""
    context = _attachment_context(request, target)
    context.update(_record_counters(target))
    if error:
        context["attachment_status"] = {"level": "danger", "message": error}
    elif success:
        context["attachment_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/attachment_list.html", context)
