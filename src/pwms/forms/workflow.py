"""Forms for referrals, notes, events and workflow actions."""

from django import forms
from django.contrib.auth import get_user_model
from django_flatpickr.widgets import DateTimePickerInput

from ..models import EventType, Group, WorkflowEvent, WorkflowNote
from .base import DATETIME_INPUT_FORMATS

User = get_user_model()


class ReferralForm(forms.Form):
    """Refer a workflow instance to a committee or other group.

    Mirrors the instance forms' search-picker treatment: a group register longer
    than ``search_picker_threshold`` renders the field as an HTMX search box
    (``pwms/_picker_field.html``) rather than a <select> of every group, while a
    short register stays an ordinary <select>.
    """

    search_picker_threshold = 10

    referred_to = forms.ModelChoiceField(
        queryset=Group.objects.order_by("name"),
        label="Refer to",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    due_date = forms.DateTimeField(
        required=False,
        label="Response due",
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "What should the committee consider?",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Long option lists become search pickers, exactly as the instance forms
        # do: the widget then posts only the chosen pk and the queryset is left
        # to validate it.
        self.search_pickers = set()
        field = self.fields["referred_to"]
        if field.queryset.count() > self.search_picker_threshold:
            field.widget = forms.HiddenInput()
            self.search_pickers.add("referred_to")

    @property
    def picker_labels(self):
        """Label of the selected group, for the picker's visible search box.

        The picker posts only a pk, so the box is prefilled from here; a form
        that has not been submitted has nothing selected and maps to nothing.
        """
        labels = {}
        value = self.initial.get("referred_to")
        if value is None and self.is_bound:
            value = self.data.get(self.add_prefix("referred_to"))
        if value in (None, ""):
            return labels
        try:
            group = self.fields["referred_to"].queryset.filter(pk=value).first()
        except TypeError, ValueError:
            return labels
        if group is not None:
            labels["referred_to"] = group.name
        return labels


class WorkflowNoteForm(forms.ModelForm):
    """One note to record against a workflow instance.

    The note's author and the record it belongs to are supplied by the view, so
    the form carries only the text.
    """

    class Meta:
        model = WorkflowNote
        fields = ["body"]
        labels = {"body": "New note"}
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": "Decision, follow-up or conversation to record",
                }
            )
        }


class WorkflowEventForm(forms.ModelForm):
    """One domain event to record against a workflow instance.

    The event's actor and the record it belongs to are supplied by the view, so
    the form carries only what the person chooses: which event, any note, and an
    optional document reference. Events are append-only (see
    :class:`~pwms.models.WorkflowEvent`), so there is no edit form beside it.
    """

    class Meta:
        model = WorkflowEvent
        fields = ["event_type", "notes", "document_url"]
        labels = {"event_type": "Event", "notes": "Notes"}
        widgets = {
            "event_type": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "What happened, and any detail worth keeping",
                }
            ),
            "document_url": forms.URLInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "https://… (optional)",
                    "hidden": "hidden",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The registry is global, so the picker offers every event type; ordering
        # by name keeps it stable as administrators add more.
        self.fields["event_type"].queryset = EventType.objects.order_by("name")


class WorkflowDeadlineForm(forms.Form):
    """The date posted from the workflow register's "Set deadline" button.

    A date rather than a date-time: the register shows dates only. The view turns
    it into the end of that day, so a deadline set for today is not already overdue.
    """

    deadline = forms.DateField(
        label="Deadline",
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(
            attrs={"type": "date", "class": "form-control form-control-sm"}
        ),
    )


class WorkflowAssignForm(forms.Form):
    """The person picked in the detail page's "Assign" modal.

    Rendered as the search picker (the hidden pk plus a text box), because the
    list of people is far too long for a <select>. Only active accounts qualify,
    the same set the record's own form offers.
    """

    assigned_to = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Assign to",
        error_messages={"required": "Choose a person from the list."},
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = User.objects.filter(is_active=True)
