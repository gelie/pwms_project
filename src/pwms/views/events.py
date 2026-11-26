import logging

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..forms import (
    WorkflowEventForm,
)
from ..models import (
    SharepointDrive,
)
from ..services import attachments as attachments_service
from ..services import bill_folders
from ..services.permissions import (
    EDIT,
    resolve,
)

logger = logging.getLogger(__name__)

from .common import _record_counters, _target_identifiers, _workflow_target

#: DOM id of the event form's document-URL field, reused by the picker JS.
EVENT_DOCUMENT_FIELD_ID = "event-document_url"


def _event_form(*args, **kwargs):
    """``WorkflowEventForm`` with a page-unique id on its document field."""
    form = WorkflowEventForm(*args, **kwargs)
    form.fields["document_url"].widget.attrs["id"] = EVENT_DOCUMENT_FIELD_ID
    # The inline bill-version form also has a ``notes`` field; keep the two
    # labels from pointing at the same DOM id on a bill page.
    form.fields["notes"].widget.attrs["id"] = "event-notes"
    return form


def _workflow_type_sharepoint_location(workflow_type):
    """The configured Events document location for ``workflow_type``."""
    configured = getattr(settings, "WORKFLOW_TYPE_SHAREPOINT_LOCATIONS", {})
    return configured.get(workflow_type.name) or {}


def _event_document_base_context(request, target):
    """Context shared by the Events form and its SharePoint browser fragments."""
    meta = target._meta
    return {
        "event_document_field_id": EVENT_DOCUMENT_FIELD_ID,
        "event_content_type": f"{meta.app_label}.{meta.model_name}",
        "event_object_id": str(target.pk),
        "event_document_tree_url": reverse("pwms:workflow_event_document_tree"),
        "event_document_folder_url": reverse("pwms:workflow_event_document_folder"),
        "event_document_upload_url": reverse("pwms:workflow_event_document_upload"),
        "event_document_sites": attachments_service.member_sites(request.user),
        "event_is_bill": meta.model_name == "bill",
        "event_bill_folder": getattr(target, "sharepoint_folder", None),
        "event_filing_configured": bill_folders.filing_configured(),
    }


def _event_default_document_location(target, user):
    """``(drive, folder, label)`` the event browser opens on, or ``None``.

    A bill opens on its dedicated B-number folder, filing it first when the
    deployment is configured but the folder has not been linked yet. Another
    workflow type opens on the folder configured for it, when there is one.
    Only locations the user may browse are returned; the tree still lists the
    sites they can reach when a default is unavailable.
    """
    if target._meta.model_name == "bill":
        folder = target.sharepoint_folder
        if folder is None and bill_folders.filing_configured():
            try:
                bill_folders.file_bill_folder(target)
            except bill_folders.BillFolderError:
                logger.info(
                    "Could not file the SharePoint folder for bill %s", target.pk
                )
            folder = target.sharepoint_folder
        if folder is None:
            return None
        try:
            attachments_service.require_drive_access(user, folder.drive.drive_id)
        except attachments_service.AttachmentError:
            return None
        return folder.drive, folder, "bill folder"

    location = _workflow_type_sharepoint_location(target.workflow_type)
    drive_id = (location.get("drive_id") or "").strip()
    if not drive_id:
        return None
    drive = (
        SharepointDrive.objects.filter(drive_id=drive_id).select_related("site").first()
    )
    if drive is None:
        # A configured drive need not have been through populate_sites yet.
        try:
            drive = bill_folders.mirror_drive(drive_id)
        except Exception:  # a Graph failure must not break the detail page
            logger.exception("Could not mirror the event document drive %s", drive_id)
    if drive is None:
        return None
    try:
        attachments_service.require_drive_access(user, drive_id)
    except attachments_service.AttachmentError:
        return None
    label = location.get("name") or "workflow folder"
    folder = attachments_service.resolve_folder(
        drive, (location.get("folder_id") or "").strip(), label
    )
    return drive, folder, label


def _event_document_location(target):
    """Resolve ``(drive, folder, label)`` for an event upload destination.

    A bill files into its own B-number folder (created if missing). For another
    record this is the workflow type's configured folder, used when the browser
    did not post a drive of its own.
    """
    if target._meta.model_name == "bill":
        folder = target.sharepoint_folder
        wanted = bill_folders.folder_name_for(target.bill_number)
        if folder is None or folder.name != wanted:
            try:
                status = bill_folders.file_bill_folder(target)
            except bill_folders.BillFolderError as exc:
                raise attachments_service.AttachmentError(str(exc)) from exc
            if status == bill_folders.UNCONFIGURED:
                raise attachments_service.AttachmentError(
                    "SharePoint filing is not configured."
                )
            folder = target.sharepoint_folder
        if folder is None:
            raise attachments_service.AttachmentError(
                "The bill's SharePoint folder could not be resolved."
            )
        return folder.drive, folder, "bill folder"

    location = _workflow_type_sharepoint_location(target.workflow_type)
    drive_id = (location.get("drive_id") or "").strip()
    if not drive_id:
        raise attachments_service.AttachmentError(
            "No SharePoint location is configured for this workflow type."
        )
    drive = (
        SharepointDrive.objects.filter(drive_id=drive_id).select_related("site").first()
    )
    if drive is None:
        # A configured drive need not have been through populate_sites yet.
        drive = bill_folders.mirror_drive(drive_id)
    if drive is None:
        raise attachments_service.AttachmentError(
            "The configured SharePoint drive could not be resolved; check the "
            "drive id and the site mirror."
        )
    label = location.get("name") or "workflow folder"
    folder = attachments_service.resolve_folder(
        drive, (location.get("folder_id") or "").strip(), label
    )
    return drive, folder, label


def _events_context(request, instance):
    """
    Template context for a record's domain-event panel and its recorder.

    ``event_rows`` is the instance's event log, newest first, shown to any reader
    who may view the record. ``can_record_event`` folds the edit right into
    whether the recorder is offered, because an event is evidence *on* the record
    — writing one is an edit of the record, not a comment on it.
    """
    can_record = resolve(request.user, instance, EDIT)
    context = {
        **_target_identifiers(instance),
        "event_rows": list(instance.events()),
        "can_record_event": can_record,
    }
    if can_record:
        context["event_form"] = _event_form()
        # The document field is a SharePoint browser: the bill's dedicated
        # folder, or the sites the user belongs to, plus a local upload.
        context.update(_event_document_base_context(request, instance))
    return context


def _events_response(request, target, *, success="", error="", form=None):
    """Re-render the events panel, announcing a change when there is one."""
    context = _events_context(request, target)
    context.update(_record_counters(target))
    if form is not None:
        # Hand the rejected form back bound, so its errors and the values already
        # typed are shown again rather than silently dropped.
        context["event_form"] = form
    if error:
        context["events_status"] = {"level": "danger", "message": error}
    elif success:
        context["events_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/events_section.html", context)


@require_POST
def workflow_event_add(request):
    """Record a domain event against a record (HTMX fragment)."""
    target = _workflow_target(request, EDIT)
    form = _event_form(request.POST)
    if not form.is_valid():
        return _events_response(request, target, form=form)
    event_type = form.cleaned_data["event_type"]
    # ``record_event`` is the model's own append path: it stamps the actor and
    # origin and never updates an existing row.
    target.record_event(
        event_type,
        actor=request.user,
        notes=form.cleaned_data["notes"],
        document_url=form.cleaned_data["document_url"],
    )
    return _events_response(request, target, success=f"Recorded “{event_type.name}”.")


def workflow_event_document_browser(request):
    """HTMX: the SharePoint browser for an event's document field."""
    target = _workflow_target(request, EDIT)
    context = _event_document_base_context(request, target)
    # A bill opens on its dedicated folder; other types open on their configured
    # folder when one exists. Without either, the tree is the starting point.
    default = _event_default_document_location(target, request.user)
    if default is not None:
        drive, folder, label = default
        context["event_default_drive"] = drive
        context["event_default_folder"] = folder
        context["event_default_label"] = label
    return render(request, "pwms/partials/event_document_browser.html", context)


def workflow_event_document_tree(request):
    """HTMX: one level of the event browser's site/drive/folder tree."""
    target = _workflow_target(request, EDIT)
    context = _event_document_base_context(request, target)
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    drive_id = request.GET.get("drive_id")
    try:
        if drive_id:
            drive = attachments_service.require_drive_access(request.user, drive_id)
            subfolders, _ = attachments_service.folder_children(
                drive.drive_id, folder_id
            )
            context["event_document_subfolders"] = subfolders
            context["event_document_drive_id"] = drive.drive_id
            context["event_document_parent_folder_id"] = folder_id
        else:
            site = attachments_service.require_site_access(
                request.user, request.GET.get("site_id")
            )
            context["event_document_drives"] = attachments_service.site_drives(site)
    except attachments_service.AttachmentError as exc:
        context["event_document_error"] = str(exc)
    return render(request, "pwms/partials/event_document_tree_children.html", context)


def workflow_event_document_folder(request):
    """HTMX: the documents in one folder, with Use and Upload actions."""
    target = _workflow_target(request, EDIT)
    context = _event_document_base_context(request, target)
    folder_id = request.GET.get("folder_id") or attachments_service.ROOT
    context.update(
        {
            "event_document_folder_id": folder_id,
            "event_document_folder_name": request.GET.get("folder_name") or "Root",
            "event_document_parent_folder_id": request.GET.get("parent_folder_id")
            or "",
        }
    )
    try:
        drive = attachments_service.require_drive_access(
            request.user, request.GET.get("drive_id")
        )
        _, files = attachments_service.folder_children(drive.drive_id, folder_id)
    except attachments_service.AttachmentError as exc:
        context["event_document_error"] = str(exc)
        return render(
            request,
            "pwms/partials/event_document_folder_contents.html",
            context,
        )
    context["event_document_drive"] = drive
    context["event_document_files"] = files
    return render(request, "pwms/partials/event_document_folder_contents.html", context)


@require_POST
def workflow_event_document_upload(request):
    """Upload an event document and put its SharePoint link in the field.

    A bill always files into its dedicated folder, whatever folder the browser
    was showing. Every other record uploads into the folder the browser
    selected, falling back to its configured workflow-type folder when no drive
    was posted.
    """
    target = _workflow_target(request, EDIT)
    upload = request.FILES.get("file")
    current_url = request.POST.get("document_url", "")
    context = _event_document_base_context(request, target)
    context["event_document_browser_open"] = True
    status = None
    drive = folder = None
    folder_label = "folder"
    posted_folder_id = request.POST.get("folder_id") or attachments_service.ROOT
    posted_folder_name = request.POST.get("folder_name") or "Root"
    posted_parent_id = request.POST.get("parent_folder_id") or ""

    if upload is None:
        status = {"level": "danger", "message": "No file was selected."}
    else:
        try:
            if target._meta.model_name == "bill":
                drive, folder, folder_label = _event_document_location(target)
            elif (request.POST.get("drive_id") or "").strip():
                drive = attachments_service.require_drive_access(
                    request.user, request.POST.get("drive_id")
                )
                folder = attachments_service.resolve_folder(
                    drive, posted_folder_id, posted_folder_name, posted_parent_id
                )
                folder_label = folder.name if folder else drive.name
            else:
                drive, folder, folder_label = _event_document_location(target)
        except attachments_service.AttachmentError as exc:
            status = {"level": "danger", "message": str(exc)}
        else:
            try:
                stored = attachments_service.upload_to_folder(
                    drive=drive,
                    folder=folder,
                    filename=upload.name,
                    content=upload.read(),
                )
            except attachments_service.AttachmentError as exc:
                status = {"level": "danger", "message": str(exc)}
            else:
                current_url = stored["web_url"] or current_url
                status = {
                    "level": "success",
                    "message": f'"{stored["name"]}" uploaded to the {folder_label}.',
                }

    if drive is not None:
        context["event_document_drive"] = drive
        context["event_document_folder_id"] = (
            folder.folder_id if folder else attachments_service.ROOT
        )
        context["event_document_folder_name"] = folder.name if folder else drive.name
        context["event_document_parent_folder_id"] = (
            folder.parent_folder.folder_id
            if folder is not None and folder.parent_folder is not None
            else ""
        )
        try:
            _, files = attachments_service.folder_children(
                drive.drive_id, context["event_document_folder_id"]
            )
        except attachments_service.AttachmentError as exc:
            # Keep a successful upload's own message; add the listing failure
            # only when there is not already an alert explaining the outcome.
            if status is None or status["level"] == "success":
                context["event_document_error"] = str(exc)
        else:
            context["event_document_files"] = files
            context["event_document_files_loaded"] = True
    else:
        context["event_document_folder_id"] = posted_folder_id
        context["event_document_folder_name"] = posted_folder_name
        context["event_document_parent_folder_id"] = posted_parent_id

    form = _event_form(initial={"document_url": current_url})
    context["field"] = form["document_url"]
    context["event_document_status"] = status
    return render(request, "pwms/partials/event_document_field.html", context)
