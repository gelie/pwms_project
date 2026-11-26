"""Delegation report and participant forms (BRS BR02/BR03)."""

from django import forms
from django.contrib.auth import get_user_model
from django.forms import BaseInlineFormSet, inlineformset_factory
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from ..models import (
    DelegationParticipant,
    DelegationReport,
    DelegationReportUpdate,
    State,
)
from .base import DATETIME_INPUT_FORMATS, WorkflowInstanceFormMixin

User = get_user_model()


class DelegationReportForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for a :class:`DelegationReport` (BRS BR02)."""

    initial_workflow_type = "Delegation Report"

    # The report's status is moved only by the transition machine (the detail
    # page's workflow menu), so the edit form renders it read-only.
    protect_current_state = True

    engagement_start_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    engagement_end_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )

    class Meta:
        model = DelegationReport
        fields = [
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "deadline",
            "priority",
            "engagement_name",
            "engagement_start_date",
            "engagement_end_date",
            "location_city",
            "location_country",
            "notes",
            "report_document_url",
        ]
        widgets = {
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            # Swapped to a search picker by the mixin when the user list is long.
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "engagement_name": forms.TextInput(attrs={"class": "form-control"}),
            "location_city": forms.Select(attrs={"class": "form-select"}),
            # Both locations are swapped to search pickers by the mixin when the
            # place tables are populated (country -> city is a cascade).
            "location_country": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "report_document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
        }


class DelegationParticipantForm(forms.ModelForm):
    """
    One delegate / support official on a delegation report (BR02.3.7/8).

    The person is chosen from the user register through the shared lookup rather
    than typed in: the row records *who* and *in what role*, and the name columns
    the model keeps are filled from the chosen account (see :meth:`save`).
    """

    class Meta:
        model = DelegationParticipant
        fields = ["user", "participant_type", "delegation_role"]
        widgets = {
            "participant_type": forms.Select(attrs={"class": "form-select"}),
            "delegation_role": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].label = "Delegate"
        self.fields["participant_type"].label = "Type"
        self.fields["user"].queryset = User.objects.filter(is_active=True).order_by(
            "username"
        )
        # Always the search lookup, never a <select> of every account.
        self.fields["user"].widget = forms.HiddenInput()
        # Not required at field level: an untouched "add another" row must not
        # complain, and a participant recorded without an account stays valid
        # until the row is actually edited (see ``clean``).
        self.fields["user"].required = False
        if "DELETE" in self.fields:
            self.fields["DELETE"].widget.attrs["class"] = "form-check-input"

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
        # A row that carries data has to name a person: the name columns are taken
        # from that account, so without one there would be nothing to record.
        if self.is_bound and self.has_changed() and not cleaned.get("user"):
            self.add_error("user", "Choose the delegate.")
        return cleaned

    def save(self, commit=True):
        participant = super().save(commit=False)
        user = self.cleaned_data.get("user")
        if user is not None:
            participant.user = user
            # Keep the name columns in step with the account the row picked; the
            # report lists people, and the form never asks for the name twice.
            participant.first_name = user.first_name or user.get_username()
            participant.last_name = user.last_name
        if commit:
            participant.save()
        return participant


class DelegationParticipantAdderForm(DelegationParticipantForm):
    """The delegation report page's one-row "add a participant" form.

    Same fields and name-filling as the inline row on the report form, plus the
    check the formset makes for the whole list: a person may only appear on the
    report once. The report is passed in rather than chosen, because the page the
    form is rendered on already is that report.
    """

    def __init__(self, *args, report=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.report = report

    def clean(self):
        # The parent insists on a person only for a row that changed; this form
        # always does, so that requirement lives in ``clean_user`` instead.
        return forms.ModelForm.clean(self)

    def clean_user(self):
        user = self.cleaned_data.get("user")
        if user is None:
            raise forms.ValidationError("Choose the delegate.")
        if (
            self.report is not None
            and self.report.participants.filter(
                user=user, removed_at__isnull=True
            ).exists()
        ):
            raise forms.ValidationError(
                f"{user.display_name} is already on this delegation."
            )
        return user


class DelegationParticipantInlineFormSet(BaseInlineFormSet):
    """Rejects the same person twice, and removes delegates without deleting them."""

    def __init__(self, *args, removed_by=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Who is taking the person off, recorded on the row that is kept.
        self.removed_by = removed_by

    def delete_existing(self, obj, commit=True):
        """
        Take the delegate off the delegation instead of deleting the row.

        The report keeps the record of who was on it and who removed them, so
        removing someone is a soft delete here too (see
        ``DelegationParticipant.remove``).
        """
        if commit:
            obj.remove(by=self.removed_by)

    def clean(self):
        super().clean()
        listed = set()
        for form in self.forms:
            cleaned = getattr(form, "cleaned_data", None)
            # A form the formset treats as empty (an untouched "add" row, or one
            # marked for removal) has no cleaned data and is not a delegate.
            if not cleaned or cleaned.get("DELETE"):
                continue
            user = cleaned.get("user")
            if user is None:
                continue
            if user.pk in listed:
                form.add_error(
                    "user",
                    f"{user.display_name} is already on this delegation.",
                )
            else:
                listed.add(user.pk)


#: Delegates are rows of the report, so they are edited inline with it. No blank
#: rows are pre-rendered: the form's one-line "add a delegate" row appends them.
DelegationParticipantFormSet = inlineformset_factory(
    DelegationReport,
    DelegationParticipant,
    form=DelegationParticipantForm,
    formset=DelegationParticipantInlineFormSet,
    extra=0,
    can_delete=True,
)


class DelegationReportUpdateForm(forms.ModelForm):
    """Record one BR03 update on a delegation report, the ATC publication included.

    Creating a row that carries any ATC detail emits the ``atc-update-published``
    event the report's close transition requires (see
    :class:`~pwms.models.DelegationReportUpdate`), so this is the screen that
    unblocks closing a report. The report itself is supplied by the view, which is
    also what scopes ``resulting_state`` to the report's own workflow type.
    """

    class Meta:
        model = DelegationReportUpdate
        fields = [
            "update_date",
            "resulting_state",
            "atc_reference",
            "atc_publication_date",
            "atc_page_number",
            "atc_document_url",
            "notes",
        ]
        widgets = {
            "update_date": DatePickerInput(attrs={"class": "form-control"}),
            "resulting_state": forms.Select(attrs={"class": "form-select"}),
            "atc_reference": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "ATC reference"}
            ),
            "atc_publication_date": DatePickerInput(attrs={"class": "form-control"}),
            "atc_page_number": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Page number"}
            ),
            "atc_document_url": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "https://…"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        }

    def __init__(self, *args, report=None, **kwargs):
        super().__init__(*args, **kwargs)
        # An update records a report's status; it never moves it. Status changes
        # go through the transition engine (the detail page's workflow menu), which
        # checks guards and permissions and logs the move. So the field is
        # display-only: it offers just the report's own state, and being disabled
        # Django ignores whatever a hand-crafted POST puts there and uses the
        # report's state instead.
        state_field = self.fields["resulting_state"]
        state_field.disabled = True
        state_field.label = "Current state"
        state_field.help_text = (
            "The report's status, recorded with this update. Change it from "
            "the workflow menu, not here."
        )
        states = State.objects.none()
        if report is not None and report.current_state_id:
            states = State.objects.filter(pk=report.current_state_id)
            self.initial["resulting_state"] = report.current_state_id
        state_field.queryset = states


class DelegateAdderForm(forms.Form):
    """The one-line "add a delegate" row: who, and in what role."""

    user = forms.ModelChoiceField(
        queryset=User.objects.all(),
        required=False,
        label="Delegate",
        widget=forms.HiddenInput(),
    )
    participant_type = forms.ChoiceField(
        choices=DelegationParticipant.PARTICIPANT_TYPE_CHOICES,
        label="Type",
        widget=forms.Select(
            attrs={"class": "form-select", "aria-label": "Participant type"}
        ),
    )
    delegation_role = forms.CharField(
        required=False,
        label="Delegation role",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Select a role...",
                "aria-label": "Delegation role",
            }
        ),
    )


class ResolutionAdderForm(forms.Form):
    """The one-line "add a resolution" row: its number, title and adoption date."""

    resolution_number = forms.CharField(
        label="Number",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Resolution number",
                "aria-label": "Resolution number",
            }
        ),
    )
    title = forms.CharField(
        label="Title",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Resolution title",
                "aria-label": "Resolution title",
            }
        ),
    )
    adoption_date = forms.DateField(
        required=False,
        label="Adopted",
        widget=DatePickerInput(
            attrs={
                "class": "form-control",
                "placeholder": "Adoption date",
                "aria-label": "Adoption date",
            }
        ),
    )
