"""International resolution forms, including child resolutions."""

from django import forms
from django.forms import BaseFormSet, formset_factory
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from ..models import DelegationReport, InternationalResolution
from ..services.permissions import EDIT, resolve
from .base import DATETIME_INPUT_FORMATS, WorkflowInstanceFormMixin


class InternationalResolutionForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for an :class:`InternationalResolution`."""

    initial_workflow_type = "International Resolution"

    # The resolution's status is moved only by the transition machine (the detail
    # page's workflow menu), so the edit form renders it read-only and ignores any
    # posted value.
    protect_current_state = True

    #: The report a resolution came out of is an optional parent link stored in
    #: ``WorkflowRelationship`` rather than a column, so it is a plain form field
    #: and is written back through ``set_parent_workflow`` when the form saves.
    parent_report = forms.ModelChoiceField(
        queryset=DelegationReport.objects.none(),
        required=False,
        label="Delegation report",
        help_text="The delegation report this resolution came out of, if any.",
    )

    #: Give ``parent_report`` the mixin's search-picker treatment as well, so a
    #: long register still renders as a search box rather than a huge <select>.
    search_picker_fields = WorkflowInstanceFormMixin.search_picker_fields + (
        "parent_report",
    )

    adoption_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )

    class Meta:
        model = InternationalResolution
        fields = [
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "deadline",
            "priority",
            "resolution_number",
            "resolution_text",
            "adoption_date",
            "responsible_group",
            "implementation_progress",
        ]
        widgets = {
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            # Swapped to a search picker by the mixin when the user list is long.
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "resolution_number": forms.TextInput(attrs={"class": "form-control"}),
            "resolution_text": forms.Textarea(
                attrs={"class": "form-control", "rows": 4}
            ),
            # Swapped to a search picker by the mixin when there are many groups.
            "responsible_group": forms.Select(attrs={"class": "form-select"}),
            "implementation_progress": forms.Textarea(
                attrs={"class": "form-control", "rows": 4}
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, user=user, **kwargs)
        # Offer only reports the user may edit: nesting a resolution under a report
        # changes that report's hierarchy, so it is an edit of both records.
        allowed = DelegationReport.objects.filter(
            pk__in=[
                report.pk
                for report in DelegationReport.objects.all()
                if resolve(user, report, EDIT)
            ]
        )
        if not self.is_create:
            current_parent = self.instance.parent_workflow
            if isinstance(current_parent, DelegationReport):
                # Whatever the resolution already sits under stays selectable even
                # if the user may no longer edit it, so saving cannot detach it by
                # accident.
                self.initial["parent_report"] = current_parent
                allowed = allowed | DelegationReport.objects.filter(
                    pk=current_parent.pk
                )
        self.fields["parent_report"].queryset = allowed.order_by("-created_at")

    def save(self, commit=True):
        resolution = super().save(commit=commit)
        if commit:
            self._sync_parent_report(resolution)
        return resolution

    def _sync_parent_report(self, resolution):
        """Attach the resolution under the chosen report, or detach it."""
        parent = self.cleaned_data.get("parent_report")
        current = resolution.parent_workflow
        if parent is None:
            if current is not None:
                resolution.clear_parent_workflow()
        elif current is None or current.pk != parent.pk:
            resolution.set_parent_workflow(parent)


class ChildResolutionForm(forms.ModelForm):
    """
    A resolution adopted at the engagement, captured from the report (BR02.3.9).

    Only what the report page knows is asked for; the resolution's workflow type,
    initial state and owner are supplied by the view, which also nests the new
    instance under the report through ``WorkflowRelationship``.
    """

    class Meta:
        model = InternationalResolution
        fields = ["resolution_number", "title", "adoption_date"]
        widgets = {
            "resolution_number": forms.TextInput(attrs={"class": "form-control"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "adoption_date": DatePickerInput(attrs={"class": "form-control"}),
        }


class ChildResolutionBaseFormSet(BaseFormSet):
    """Rejects two rows claiming the same resolution number.

    ``resolution_number`` is unique, but two *new* rows carrying the same number
    both pass the model's own check (neither is in the database yet) and the
    second insert would fail at the constraint, so the clash is caught here.
    """

    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            cleaned = getattr(form, "cleaned_data", None)
            if not cleaned or cleaned.get("DELETE"):
                continue
            number = (cleaned.get("resolution_number") or "").strip()
            if not number:
                continue
            if number in seen:
                form.add_error(
                    "resolution_number",
                    f"Resolution number {number} is listed twice.",
                )
            else:
                seen.add(number)


#: Resolutions are instances of their own, linked to the report on save rather
#: than owned by it, so this is a plain formset — not an inline one. As with the
#: delegates, rows are appended from the one-line "add a resolution" row, and
#: ``can_delete`` lets that row's remove button drop one again.
ChildResolutionFormSet = formset_factory(
    ChildResolutionForm,
    formset=ChildResolutionBaseFormSet,
    extra=0,
    can_delete=True,
)
