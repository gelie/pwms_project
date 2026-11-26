import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from ..forms import (
    MeetingAgendaForm,
    MeetingAttendanceAdderForm,
    MeetingAttendanceRecordForm,
    MeetingForm,
)
from ..models import (
    Meeting,
    MeetingAttendance,
)
from ..notifications import (
    notify_meeting_attendance,
    notify_meeting_created,
)
from ..services.permissions import (
    DELETE,
    EDIT,
    VIEW,
    permissions_for,
    require,
    resolve,
)

logger = logging.getLogger(__name__)

from .attachments import _attachment_context
from .calendar import _meeting_create_initial
from .common import (
    _can_create_form_type,
    _deny_uncreatable_workflow_type,
    _record_counters,
    _target_identifiers,
)
from .workflow_views import _workflow_detail_context


def _meeting_detail_model():
    return Meeting.objects.select_related(
        "workflow_type",
        "workflow_type__group",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
        "venue",
        "venue__building",
    )


def _attendance_record_form(attendance):
    """Per-row form, prefixed by the row's pk so many rows can share a page."""
    return MeetingAttendanceRecordForm(
        prefix=f"a{attendance.pk}",
        initial={
            "rsvp_status": ""
            if attendance.rsvp_status == "pending"
            else attendance.rsvp_status,
            "attendance_status": attendance.attendance_status,
            "note": attendance.note,
        },
    )


def _meeting_attendance_context(request, meeting):
    """The attendance register plus the controls the reader may use on it."""
    can_edit = resolve(request.user, meeting, EDIT)
    register = meeting.attendances_active()
    rows = [
        {"attendance": attendance, "form": _attendance_record_form(attendance)}
        for attendance in register
    ]
    context = {
        **_target_identifiers(meeting),
        "meeting": meeting,
        "attendance_rows": rows,
        "attendance_counts": meeting.attendance_counts(),
        "can_edit_attendance": can_edit,
        # The register starts collapsed (the counts stay visible); a response to
        # any panel action re-renders it open so a save does not hide the table.
        "attendance_expanded": False,
    }
    if can_edit:
        context["attendance_form"] = MeetingAttendanceAdderForm(meeting=meeting)
    return context


def _meeting_minutes_context(request, meeting):
    """The minutes panel: current status, filed minutes and the actions."""
    can_edit = resolve(request.user, meeting, EDIT)
    return {
        "meeting": meeting,
        "minutes_attachments": meeting.minutes_attachments.select_related(
            "uploaded_by"
        ),
        "agenda_attachments": meeting.agenda_attachments.select_related("uploaded_by"),
        "minutes_status_choices": Meeting.MINUTES_STATUS_CHOICES,
        "can_edit_minutes": can_edit,
    }


def meeting_detail(request, public_id):
    """Show one parliamentary meeting: schedule, attendance, documents, minutes."""
    meeting = get_object_or_404(_meeting_detail_model(), public_id=public_id)
    require(request.user, meeting, VIEW)
    context = {
        "meeting": meeting,
        "transitions": meeting.get_available_transitions(),
    }
    context.update(_meeting_attendance_context(request, meeting))
    context.update(_meeting_minutes_context(request, meeting))
    context.update(_workflow_detail_context(request, meeting))
    context.update(_attachment_context(request, meeting))
    return render(request, "pwms/meeting-detail.html", context)


def meeting_create(request):
    """Create a meeting; its register is generated and its audience alerted."""
    if request.method == "POST":
        _deny_uncreatable_workflow_type(request)
        form = MeetingForm(request.POST, user=request.user)
        if form.is_valid():
            meeting = form.save()
            # The register starts as the hosting group's members; invitees are
            # added by hand afterwards (and survive a regenerate).
            if meeting.host_group_id is not None:
                meeting.regenerate_attendance(actor=request.user)
            messages.success(request, f'Meeting "{meeting.title}" created.')
            notify_meeting_created(meeting, actor=request.user)
            return redirect("pwms:meeting_detail", public_id=meeting.public_id)
    else:
        if not _can_create_form_type(request.user, MeetingForm):
            messages.error(request, "You do not have a role that may create meetings.")
            return redirect("pwms:meetings")
        initial = {"owner": request.user}
        # The calendar links in with a date (and optional time) to prefill the
        # schedule, so "new meeting on this day" opens with the slot already set.
        initial.update(_meeting_create_initial(request.GET))
        form = MeetingForm(user=request.user, initial=initial)
    return render(
        request,
        "pwms/meeting-form.html",
        {"form": form, "meeting": None, "is_create": True},
    )


def meeting_update(request, public_id):
    """Edit a meeting's scheduling detail."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    if request.method == "POST":
        previous_host_group_id = meeting.host_group_id
        form = MeetingForm(request.POST, instance=meeting, user=request.user)
        if form.is_valid():
            meeting = form.save()
            # Re-generate only when the hosting group changed: an unrelated edit
            # must not re-add a generated row the secretary deliberately removed.
            if meeting.host_group_id != previous_host_group_id:
                meeting.regenerate_attendance(actor=request.user)
            messages.success(request, f'Meeting "{meeting.title}" updated.')
            return redirect("pwms:meeting_detail", public_id=meeting.public_id)
    else:
        form = MeetingForm(instance=meeting, user=request.user)
    return render(
        request,
        "pwms/meeting-form.html",
        {"form": form, "meeting": meeting, "is_create": False},
    )


def meeting_delete(request, public_id):
    """Confirm (GET) then delete (POST) a meeting."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, DELETE)
    if request.method == "POST":
        title = meeting.title
        meeting.delete()
        messages.success(request, f'Meeting "{title}" deleted.')
        return redirect("pwms:meetings")
    return render(request, "pwms/meeting-confirm-delete.html", {"meeting": meeting})


def _meeting_attendance_response(request, meeting, *, success="", error=""):
    """Re-render the attendance register, announcing a change when there is one."""
    context = _meeting_attendance_context(request, meeting)
    context.update(_record_counters(meeting))
    # An action came from inside the register, so hand it back open.
    context["attendance_expanded"] = True
    if error:
        context["attendance_status"] = {"level": "danger", "message": error}
    elif success:
        context["attendance_status"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/meetings/attendance.html", context)


@require_POST
def meeting_attendance_add(request, public_id):
    """Add a person (account or guest) to a meeting's register (HTMX fragment)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    form = MeetingAttendanceAdderForm(request.POST, meeting=meeting)
    if not form.is_valid():
        errors = " ".join(
            str(message)
            for messages_list in form.errors.values()
            for message in messages_list
        )
        return _meeting_attendance_response(request, meeting, error=errors)
    attendance = form.save(commit=False)
    attendance.meeting = meeting
    attendance.save()
    return _meeting_attendance_response(
        request, meeting, success=f"{attendance.display_name} added to the register."
    )


def _attendance_for(request, meeting, attendance_id):
    return get_object_or_404(
        MeetingAttendance, public_id=attendance_id, meeting=meeting
    )


@require_POST
def meeting_attendance_record(request, public_id, attendance_id):
    """Record one attendee's RSVP and/or attendance during or after the meeting."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    attendance = _attendance_for(request, meeting, attendance_id)
    form = MeetingAttendanceRecordForm(request.POST, prefix=f"a{attendance.pk}")
    if not form.is_valid():
        return _meeting_attendance_response(
            request, meeting, error="The attendance update was not understood."
        )
    rsvp_status = form.cleaned_data.get("rsvp_status")
    status = form.cleaned_data.get("attendance_status")
    note = form.cleaned_data.get("note", "")
    if rsvp_status:
        attendance.respond(rsvp_status, note=note or None)
    if status:
        attendance.record(
            status,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
            note=note or None,
        )
    return _meeting_attendance_response(
        request, meeting, success=f"Attendance updated for {attendance.display_name}."
    )


@require_POST
def meeting_attendance_remove(request, public_id, attendance_id):
    """Take a person off a meeting's register (HTMX fragment)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    attendance = _attendance_for(request, meeting, attendance_id)
    name = attendance.display_name
    attendance.delete()
    return _meeting_attendance_response(
        request, meeting, success=f"{name} removed from the register."
    )


@require_POST
def meeting_attendance_notify(request, public_id):
    """Email the meeting's audience a recorded attendance summary."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    notify_meeting_attendance(meeting, None, actor=request.user, status="manual")
    return _meeting_attendance_response(
        request, meeting, success="Attendance summary sent to the meeting's audience."
    )


@require_POST
def meeting_attendance_regenerate(request, public_id):
    """Rebuild the register from the hosting group, keeping the invitees (HTMX)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    if meeting.host_group_id is None:
        return _meeting_attendance_response(
            request,
            meeting,
            error=(
                "No hosting group is set, so there is nothing to generate. Set one "
                "on the meeting, or add invitees by hand."
            ),
        )
    summary = meeting.regenerate_attendance(actor=request.user)
    message = (
        f"Register regenerated from {meeting.host_group.name}: "
        f"{summary['added']} added, {summary['removed']} removed — "
        f"{summary['total']} on the register."
    )
    return _meeting_attendance_response(request, meeting, success=message)


def _meeting_minutes_response(request, meeting, *, success="", error=""):
    """Re-render the minutes panel, announcing a change when there is one."""
    context = _meeting_minutes_context(request, meeting)
    context.update(_record_counters(meeting))
    if error:
        context["minutes_status_message"] = {"level": "danger", "message": error}
    elif success:
        context["minutes_status_message"] = {"level": "success", "message": success}
    return render(request, "pwms/partials/meetings/minutes.html", context)


@require_POST
def meeting_minutes_status(request, public_id):
    """Advance the minutes status (draft / filed / approved) and record it."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    status = request.POST.get("status", "")
    if status == "draft":
        meeting.minutes_status = "draft"
        meeting.save(update_fields=["minutes_status", "updated_at"])
    elif status == "filed":
        meeting.record_minutes_filed(actor=request.user)
    elif status == "approved":
        meeting.record_minutes_approved(
            actor=request.user, comment=request.POST.get("comment", "")
        )
    else:
        return _meeting_minutes_response(
            request, meeting, error="Unknown minutes status."
        )
    return _meeting_minutes_response(
        request, meeting, success="Minutes status updated."
    )


def _meeting_agenda_context(request, meeting):
    """Context the agenda panel and its inline editor both render from.

    Carries what the panel needs standalone (it is returned as an HTMX fragment
    as well as included in the page): the meeting, the reader's capabilities, and
    the attachment-target identifiers the "Upload agenda" button posts to.
    """
    meta = meeting._meta
    return {
        "meeting": meeting,
        "perms": permissions_for(request.user, meeting),
        "attachment_content_type": f"{meta.app_label}.{meta.model_name}",
        "attachment_object_id": str(meeting.pk),
    }


def meeting_agenda(request, public_id):
    """The read-only agenda panel (restores it when the inline editor is cancelled)."""
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, VIEW)
    return render(
        request,
        "pwms/partials/meetings/agenda.html",
        _meeting_agenda_context(request, meeting),
    )


def meeting_agenda_edit(request, public_id):
    """Edit the agenda inline: GET shows the editor, POST saves it (HTMX fragment).

    Editing is an ``edit`` action on the meeting, exactly as the full meeting form
    is. The saved value is the rich-text editor's HTML; it is sanitised when it is
    rendered (see ``Meeting.agenda_html``), never here.
    """
    meeting = get_object_or_404(Meeting, public_id=public_id)
    require(request.user, meeting, EDIT)
    context = _meeting_agenda_context(request, meeting)

    if request.method == "POST":
        form = MeetingAgendaForm(request.POST, instance=meeting)
        if form.is_valid():
            meeting = form.save(commit=False)
            meeting.save(update_fields=["agenda_summary", "updated_at"])
            context["meeting"] = meeting
            context["agenda_status"] = {
                "level": "success",
                "message": "Agenda updated.",
            }
            return render(request, "pwms/partials/meetings/agenda.html", context)
    else:
        form = MeetingAgendaForm(instance=meeting)

    context["form"] = form
    return render(request, "pwms/partials/meetings/agenda_editor.html", context)
