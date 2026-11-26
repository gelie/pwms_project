"""International agreement form (BRS BR02)."""

from django import forms
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from ..models import InternationalAgreement
from .base import DATETIME_INPUT_FORMATS, WorkflowInstanceFormMixin


class InternationalAgreementForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for an :class:`InternationalAgreement` (BRS BR02)."""

    initial_workflow_type = "International Agreement"

    # The agreement's status is moved only by the transition machine (the detail
    # page's workflow menu), so the edit form renders it read-only.
    protect_current_state = True

    atc_tabling_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )

    class Meta:
        model = InternationalAgreement
        fields = [
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "deadline",
            "priority",
            "agreement_type",
            "submitting_department",
            "responsible_minister",
            "responsible_minister_name",
            "atc_tabling_date",
            "atc_reference",
            "referral_committees",
            "notes",
            "agreement_document_url",
            "explanatory_memorandum_url",
        ]
        widgets = {
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            # Swapped to a search picker by the mixin when the user list is long.
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "agreement_type": forms.Select(attrs={"class": "form-select"}),
            "submitting_department": forms.TextInput(attrs={"class": "form-control"}),
            # Swapped to a search picker by the mixin when there are many
            # serving ministers; the name below covers everyone else.
            "responsible_minister": forms.Select(attrs={"class": "form-select"}),
            "responsible_minister_name": forms.TextInput(
                attrs={"class": "form-control"}
            ),
            "referral_committees": forms.SelectMultiple(
                attrs={"class": "form-select", "size": 6}
            ),
            "atc_reference": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "agreement_document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
            "explanatory_memorandum_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
        }
