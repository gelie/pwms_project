import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from ..forms import (
    BillDocumentUrlForm,
    BillForm,
    BillVersionForm,
)
from ..models import (
    Bill,
    BillVersion,
)
from ..notifications import (
    notify_workflow_created,
)
from ..services import attachments as attachments_service
from ..services import bill_folders
from ..services.bill_updates import (
    EVENT_EDITS,
    EVENT_NEW,
    queue_bill_update,
)
from ..services.permissions import (
    DELETE,
    EDIT,
    VIEW,
    require,
    resolve,
    visible_instances,
)

logger = logging.getLogger(__name__)

from .attachments import _attachment_context
from .common import _can_create_form_type, _deny_uncreatable_workflow_type
from .workflow_views import _workflow_detail_context


def bills(request):
    """List bills the user may view, with a free-text filter (BRS §7.4)."""
    query = request.GET.get("q", "").strip()
    matching = Bill.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "responsible_committee",
    ).order_by("-created_at")
    if query:
        matching = matching.filter(
            Q(bill_number__icontains=query)
            | Q(title__icontains=query)
            | Q(short_title__icontains=query)
            | Q(sponsor_name__icontains=query)
            | Q(sponsor__first_name__icontains=query)
            | Q(sponsor__last_name__icontains=query)
        )
    # View access gates the listing itself, not just the row actions.
    viewable = visible_instances(request.user, matching)
    editable_pks = {bill.pk for bill in viewable if resolve(request.user, bill, EDIT)}
    deletable_pks = {
        bill.pk for bill in viewable if resolve(request.user, bill, DELETE)
    }
    return render(
        request,
        "pwms/bill-list.html",
        {
            "bills": viewable,
            "query": query,
            "editable_pks": editable_pks,
            "deletable_pks": deletable_pks,
        },
    )


def bill_detail(request, public_id):
    """Show one bill: profile, public status, transitions and audit trail."""
    bill = get_object_or_404(
        Bill.objects.select_related(
            "workflow_type",
            "current_state",
            "owner",
            "assigned_to",
            "responsible_committee",
        ),
        public_id=public_id,
    )
    require(request.user, bill, VIEW)
    versions = bill.versions.select_related("recorded_by")
    context = {
        "bill": bill,
        "versions": versions,
        # The Related tab's Version history card: the tab badge counts the rows.
        "related_count": versions.count(),
        "transitions": bill.get_available_transitions(),
    }
    context.update(_workflow_detail_context(request, bill))
    context.update(_attachment_context(request, bill))
    if context["perms"].get("edit"):
        # The Related tab's inline "Add Version" form. Recording a version is
        # gated on edit by bill_version_create, so only build it for an editor.
        context["version_form"] = BillVersionForm(initial={"is_current": True})
    return render(request, "pwms/bill-detail.html", context)


def _file_bill_folder(request, bill, verbose=False):
    """File the bill's SharePoint folder (B-number named) after a save; never blocks.

    The bill row is already saved when this runs, so SharePoint being down must
    not lose the submission: Graph failures downgrade to a warning the user can
    retry later by editing (or re-saving) the bill, or from the detail page's
    folder chip.

    ``verbose`` (the manual retry path) reports an already-filed folder too;
    a plain save stays quiet about the idempotent no-op.
    """
    try:
        status = bill_folders.file_bill_folder(bill)
    except bill_folders.BillFolderError as exc:
        messages.warning(
            request,
            f"Saved, but its SharePoint folder could not be created: {exc}",
        )
        return
    folder = bill.sharepoint_folder
    if status == bill_folders.CREATED:
        name = folder.name if folder else bill_folders.folder_name_for(bill.bill_number)
        messages.success(request, f"Filed its SharePoint folder '{name}'.")
    elif verbose:
        if status == bill_folders.ALREADY_THERE:
            messages.info(
                request,
                f"Already filed: the SharePoint folder '{bill.bill_number}' exists.",
            )
        else:
            messages.info(
                request,
                "SharePoint filing is not configured (BILL_DRIVE / BILL_FOLDER).",
            )


def bill_folder_preview(request):
    """HTMX: the SharePoint folder a typed B-number will file into."""
    return render(
        request,
        "pwms/partials/bill_folder_preview.html",
        {
            "configured": bill_folders.filing_configured(),
            "bill_number": (request.GET.get("bill_number") or "").strip(),
            "folder_name": bill_folders.folder_name_for(
                request.GET.get("bill_number") or ""
            ),
        },
    )


def bill_create(request):
    """Create a bill; the initial state is derived from its type."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = BillForm(request.POST, user=request.user)
        if form.is_valid():
            bill = form.save()
            _file_bill_folder(request, bill)
            messages.success(request, f'Bill "{bill.bill_number}" created.')
            notify_workflow_created(bill, actor=request.user)
            queue_bill_update(bill, EVENT_NEW, actor=request.user)
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        if not _can_create_form_type(request.user, BillForm):
            messages.error(request, "You do not have a role that may create bills.")
            return redirect("pwms:bills")
        form = BillForm(user=request.user, initial={"owner": request.user})
    return render(
        request,
        "pwms/bill-form.html",
        {"form": form, "is_create": True},
    )


def bill_update(request, public_id):
    """Edit a bill."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillForm(request.POST, instance=bill)
        if form.is_valid():
            bill = form.save()
            _file_bill_folder(request, bill)
            messages.success(request, f'Bill "{bill.bill_number}" updated.')
            queue_bill_update(bill, EVENT_EDITS, actor=request.user)
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        form = BillForm(instance=bill)
    return render(
        request,
        "pwms/bill-form.html",
        {"form": form, "bill": bill, "is_create": False},
    )


def bill_delete(request, public_id):
    """Confirm (GET) then delete (POST) a bill."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, DELETE)
    if request.method == "POST":
        label = bill.bill_number or bill.title
        bill.delete()
        messages.success(request, f'Bill "{label}" deleted.')
        return redirect("pwms:bills")
    return render(
        request,
        "pwms/bill-confirm-delete.html",
        {"bill": bill},
    )


@require_POST
def bill_file_folder(request, public_id):
    """Re-file a bill's SharePoint folder (the detail page chip's retry)."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    _file_bill_folder(request, bill, verbose=True)
    return redirect("pwms:bill_detail", public_id=bill.public_id)


def bill_version_create(request, public_id):
    """Record a preserved version on a bill from the web UI (BRS §15A).

    Gated on ``edit`` rights on the bill rather than a separate capability:
    recording a version is an edit of the bill's record, and who recorded it is
    captured automatically for contributor accountability.
    """
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillVersionForm(request.POST)
        if form.is_valid():
            version = form.save(commit=False)
            version.bill = bill
            version.recorded_by = request.user
            version.save()
            messages.success(request, f'Version "{version.version_label}" recorded.')
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        # A newly recorded version is normally the one before Parliament now.
        form = BillVersionForm(initial={"is_current": True})
    return render(
        request,
        "pwms/bill-version-form.html",
        {"form": form, "bill": bill, "is_create": True},
    )


def _bill_version_document_folder(bill):
    """The bill's own SharePoint folder, or raise when it is not filed yet."""
    folder = bill.sharepoint_folder
    if folder is None:
        raise attachments_service.AttachmentError(
            "The bill's SharePoint folder has not been filed yet. "
            "File it from the bill page first."
        )
    return folder


def _folder_document_files(folder):
    """Files directly inside one folder (no site browsing)."""
    _, files = attachments_service.folder_children(
        folder.drive.drive_id, folder.folder_id
    )
    return files


def _bill_version_document_files(bill):
    """Files in the bill's own folder; no site browsing is involved."""
    return _folder_document_files(_bill_version_document_folder(bill))


def bill_version_document_options(request, public_id):
    """HTMX: files in the bill's folder for the version document dropdown."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    files = []
    error = ""
    try:
        files = _bill_version_document_files(bill)
    except attachments_service.AttachmentError as exc:
        error = str(exc)
    return render(
        request,
        "pwms/partials/sharepoint_document_options.html",
        {
            "document_files": files,
            "document_options_error": error,
            "field_id": request.GET.get("field_id") or "id_document_url",
            "folder_label": "bill folder",
        },
    )


@require_POST
def bill_version_document_upload(request, public_id):
    """Upload a version document into the bill's folder (HTMX field response)."""
    bill = get_object_or_404(Bill, public_id=public_id)
    require(request.user, bill, EDIT)
    upload = request.FILES.get("file")
    # The upload button includes the enclosing version form, so this is the URL
    # currently in the field (including unsaved typing), not a stale snapshot.
    current_url = request.POST.get("document_url", "")
    status = None
    if upload is None:
        status = {"level": "danger", "message": "No file was selected."}
    else:
        try:
            folder = _bill_version_document_folder(bill)
            stored = attachments_service.upload_to_folder(
                drive=folder.drive,
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
                "message": f'"{stored["name"]}" uploaded to the bill folder.',
            }

    files = []
    error = ""
    try:
        files = _bill_version_document_files(bill)
    except attachments_service.AttachmentError as exc:
        # The status alert already explains a failed upload; only add the listing
        # error when it is the sole thing worth saying (a successful upload still
        # keeps its own message).
        if status is None or status["level"] == "success":
            error = str(exc)

    form = BillVersionForm(initial={"document_url": current_url})
    return render(
        request,
        "pwms/partials/sharepoint_document_field.html",
        {
            "field": form["document_url"],
            "options_url": reverse(
                "pwms:bill_version_document_options", args=[bill.public_id]
            ),
            "upload_url": reverse(
                "pwms:bill_version_document_upload", args=[bill.public_id]
            ),
            "folder_label": "bill folder",
            "document_files": files,
            "document_options_error": error,
            "document_status": status,
            "wrapper_class": request.POST.get("wrapper_class") or "col-12 col-md-6",
        },
    )


def _bill_form_document_number(request, bill):
    """The B-number the bill form's document picker should use, and a guard."""
    number = (
        request.POST.get("bill_number") or request.GET.get("bill_number") or ""
    ).strip()
    if not number:
        return "", "Enter the B-number before choosing a document."
    if bill is not None:
        if number != bill.bill_number:
            return (
                number,
                (
                    "Save the new B-number before choosing a document; the bill "
                    "folder follows the number stored on the bill."
                ),
            )
        return number, ""
    if Bill.objects.filter(bill_number=number).exists():
        return number, "A bill with this B-number already exists."
    return number, ""


def _bill_document_scope(request, public_id):
    """Resolve the bill (editing), its folder, and any guard message."""
    if public_id:
        bill = get_object_or_404(Bill, public_id=public_id)
        require(request.user, bill, EDIT)
    else:
        if not _can_create_form_type(request.user, BillForm):
            raise PermissionDenied(_("You do not have a role that may create bills."))
        bill = None

    number, error = _bill_form_document_number(request, bill)
    if error:
        return bill, None, error

    try:
        if bill is None:
            # A new bill has no row and no linked folder yet; create/reuse the
            # folder the save will later link, based on the typed B-number.
            folder = bill_folders.ensure_bill_folder_row(number)
        else:
            folder = bill.sharepoint_folder
            wanted = bill_folders.folder_name_for(bill.bill_number)
            if folder is None or folder.name != wanted:
                status = bill_folders.file_bill_folder(bill)
                if status == bill_folders.UNCONFIGURED:
                    return bill, None, "SharePoint filing is not configured."
                folder = bill.sharepoint_folder
            if folder is None:
                return bill, None, "The bill's SharePoint folder could not be resolved."
    except bill_folders.BillFolderError as exc:
        return bill, None, str(exc)
    return bill, folder, ""


def bill_document_options(request, public_id=None):
    """HTMX: files in the bill's folder for the bill document dropdown."""
    _, folder, error = _bill_document_scope(request, public_id)
    files = []
    if folder is not None and not error:
        try:
            files = _folder_document_files(folder)
        except attachments_service.AttachmentError as exc:
            error = str(exc)
    return render(
        request,
        "pwms/partials/sharepoint_document_options.html",
        {
            "document_files": files,
            "document_options_error": error,
            "field_id": request.GET.get("field_id") or "id_bill_document_url",
            "folder_label": "bill folder",
        },
    )


@require_POST
def bill_document_upload(request, public_id=None):
    """Upload a bill document into the bill's folder (HTMX field response)."""
    _, folder, error = _bill_document_scope(request, public_id)
    upload = request.FILES.get("file")
    # The upload button includes the enclosing bill form, so this is the URL
    # currently in the field, not a stale snapshot.
    current_url = request.POST.get("bill_document_url", "")
    status = None
    if error:
        status = {"level": "danger", "message": error}
        error = ""
    elif upload is None:
        status = {"level": "danger", "message": "No file was selected."}
    else:
        try:
            stored = attachments_service.upload_to_folder(
                drive=folder.drive,
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
                "message": f'"{stored["name"]}" uploaded to the bill folder.',
            }

    files = []
    if folder is not None:
        try:
            files = _folder_document_files(folder)
        except attachments_service.AttachmentError as exc:
            # Keep a successful upload's own message; add the listing failure
            # only when there is not already an alert explaining the outcome.
            if status is None or status["level"] == "success":
                error = str(exc)

    if public_id:
        options_url = reverse("pwms:bill_document_options_edit", args=[public_id])
        upload_url = reverse("pwms:bill_document_upload_edit", args=[public_id])
    else:
        options_url = reverse("pwms:bill_document_options")
        upload_url = reverse("pwms:bill_document_upload")

    form = BillDocumentUrlForm(initial={"bill_document_url": current_url})
    return render(
        request,
        "pwms/partials/sharepoint_document_field.html",
        {
            "field": form["bill_document_url"],
            "options_url": options_url,
            "upload_url": upload_url,
            "options_include": "#id_bill_number",
            "folder_label": "bill folder",
            "document_files": files,
            "document_options_error": error,
            "document_status": status,
            "wrapper_class": request.POST.get("wrapper_class") or "col-12 col-md-6",
        },
    )


def bill_version_update(request, public_id, version_public_id):
    """Edit a recorded bill version (BRS §15A).

    Corrections are made in place rather than by deleting and re-recording: the
    row keeps its identity and ``recorded_by`` (who recorded it originally),
    while ``auditlog`` captures the edit as an UPDATE. ``is_current`` still
    normalises on save, so flagging this version demotes any other current one.
    """
    bill = get_object_or_404(Bill, public_id=public_id)
    version = get_object_or_404(BillVersion, public_id=version_public_id, bill=bill)
    require(request.user, bill, EDIT)
    if request.method == "POST":
        form = BillVersionForm(request.POST, instance=version)
        if form.is_valid():
            version = form.save()
            messages.success(request, f'Version "{version.version_label}" updated.')
            return redirect("pwms:bill_detail", public_id=bill.public_id)
    else:
        form = BillVersionForm(instance=version)
    return render(
        request,
        "pwms/bill-version-form.html",
        {"form": form, "bill": bill, "version": version, "is_create": False},
    )
