"""Forms for scheduling meetings and recording attendance."""

from django import forms
from django.contrib.auth import get_user_model
from django_flatpickr.widgets import DateTimePickerInput

from ..models import Group, Meeting, MeetingAttendance, Venue
from .base import DATETIME_INPUT_FORMATS, WorkflowInstanceFormMixin

User = get_user_model()


class MeetingForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for a parliamentary :class:`Meeting`.

    Covers the whole scheduling detail: what kind of meeting it is, when and
    where it sits, who convenes and chairs it, and the quorum. The agenda and the
    minutes are *documents* (attachments), not fields, so they are filed from the
    detail page rather than entered here.
    """

    initial_workflow_type = "Meeting"

    #: People/group pickers: long registers become HTMX search boxes.
    search_picker_fields = (
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
    )

    start_datetime = forms.DateTimeField(
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
        label="Starts",
    )
    end_datetime = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
        label="Ends",
    )

    class Meta:
        model = Meeting
        fields = [
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "priority",
            "reference_number",
            "meeting_type",
            "start_datetime",
            "end_datetime",
            "all_day",
            "attendance_mode",
            "venue",
            "off_site_venue",
            "off_site_address",
            "meeting_link",
            "host_group",
            "chaired_by",
            "secretary",
            "quorum",
            "agenda_summary",
            "notes",
        ]
        widgets = {
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "reference_number": forms.TextInput(attrs={"class": "form-control"}),
            "meeting_type": forms.Select(attrs={"class": "form-select"}),
            "all_day": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "attendance_mode": forms.Select(attrs={"class": "form-select"}),
            "venue": forms.Select(attrs={"class": "form-select"}),
            "off_site_venue": forms.TextInput(attrs={"class": "form-control"}),
            "off_site_address": forms.TextInput(attrs={"class": "form-control"}),
            "meeting_link": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
            "host_group": forms.Select(attrs={"class": "form-select"}),
            "chaired_by": forms.Select(attrs={"class": "form-select"}),
            "secretary": forms.Select(attrs={"class": "form-select"}),
            "quorum": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "agenda_summary": forms.Textarea(
                attrs={"class": "form-control", "rows": 8, "data-rich-text": ""}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        labels = {
            # The agenda is written in the rich-text editor (static/js/rich-text.js),
            # which turns this textarea into a toolbar + contenteditable surface.
            "agenda_summary": "Agenda",
            "reference_number": "Reference",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        people = User.objects.filter(is_active=True).order_by("username")
        for name in ("chaired_by", "secretary"):
            if name in self.fields:
                self.fields[name].queryset = people
        if "host_group" in self.fields:
            self.fields["host_group"].queryset = Group.objects.order_by("name")
        if "venue" in self.fields:
            # Only active rooms in active buildings are offered; leaving it blank
            # is how an off-premises venue is recorded (the off-site fields).
            self.fields["venue"].queryset = (
                Venue.objects.filter(is_active=True, building__is_active=True)
                .select_related("building")
                .order_by("building__name", "name")
            )


class MeetingImportForm(forms.Form):
    """Upload a CSV/XLSX file of meetings to import (bulk create/update).

    The format is the one the calendar exports (see
    ``docs/Meetings Import & Export.md`` and
    :mod:`pwms.services.meetings_io`): the same columns come back out, so a file
    downloaded from the calendar can be edited and re-imported. The view decides
    who may import; this form only checks the upload itself.
    """

    file = forms.FileField(
        label="File",
        help_text="A .csv or .xlsx file in the meeting import format.",
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".csv,.xlsx,.xlsm"}
        ),
    )
    dry_run = forms.BooleanField(
        required=False,
        label="Check only (do not save anything)",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def clean_file(self):
        upload = self.cleaned_data["file"]
        name = (upload.name or "").lower()
        if not name.endswith((".csv", ".xlsx", ".xlsm")):
            raise forms.ValidationError("Upload a .csv or .xlsx file.")
        return upload


class MeetingAgendaForm(forms.ModelForm):
    """Inline edit of a meeting's agenda (the rich-text body only).

    Separate from :class:`MeetingForm` so the detail page can save the agenda on
    its own, without a full meeting edit round trip. The widget is the same
    rich-text textarea (see ``static/js/rich-text.js``).
    """

    class Meta:
        model = Meeting
        fields = ["agenda_summary"]
        labels = {"agenda_summary": "Agenda"}
        widgets = {
            "agenda_summary": forms.Textarea(
                attrs={"class": "form-control", "rows": 8, "data-rich-text": ""}
            ),
        }


class MeetingAttendanceAdderForm(forms.ModelForm):
    """Add one invitee to a meeting's attendance register.

    An invitee is anyone the hosting group's generated roster would not cover: a
    PWMS user outside the hosting group (chosen through the user lookup) or a
    guest with no account (named, optionally with an email). The meeting is
    supplied by the page, not chosen, and a person may only appear on the register
    once (``clean``).
    """

    class Meta:
        model = MeetingAttendance
        fields = ["user", "capacity", "is_required", "guest_name", "guest_email"]
        labels = {
            "user": "Invitee",
            "guest_name": "Guest name",
            "guest_email": "Guest email",
        }
        help_texts = {
            "user": "Anyone with a PWMS account who is not in the hosting group.",
            "guest_name": "For an invitee without a PWMS account.",
        }
        widgets = {
            "capacity": forms.Select(attrs={"class": "form-select"}),
            "is_required": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "guest_name": forms.TextInput(attrs={"class": "form-control"}),
            "guest_email": forms.EmailInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, meeting=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.meeting = meeting
        user_field = self.fields["user"]
        user_field.queryset = User.objects.filter(is_active=True).order_by("username")
        user_field.required = False
        # Always the search lookup, never a <select> of every account.
        user_field.widget = forms.HiddenInput()

    @property
    def selected_user_label(self):
        """Name to show in the row's search box for the chosen user."""
        pk = self["user"].value()
        if not pk:
            return ""
        user = self.fields["user"].queryset.filter(pk=pk).first()
        return user.display_name if user is not None else ""

    def clean(self):
        cleaned = super().clean()
        user = cleaned.get("user")
        guest_name = (cleaned.get("guest_name") or "").strip()
        if user is None and not guest_name:
            raise forms.ValidationError("Choose a person or name a guest.")
        if user is not None and self.meeting is not None:
            duplicate = self.meeting.attendances.filter(user=user)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise forms.ValidationError("That person is already on the register.")
        return cleaned


class MeetingAttendanceRecordForm(forms.Form):
    """Record one attendee's RSVP or attendance (during or after the meeting)."""

    rsvp_status = forms.ChoiceField(
        choices=[("", "—")] + MeetingAttendance.RSVP_STATUS_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    attendance_status = forms.ChoiceField(
        choices=MeetingAttendance.ATTENDANCE_STATUS_CHOICES,
        required=False,
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )
    note = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control form-control-sm", "placeholder": "Remark"}
        ),
    )
