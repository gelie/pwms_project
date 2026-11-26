"""Bill and bill-version forms (Online Bill Tracking BRS)."""

from django import forms
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from ..models import Bill, BillVersion
from ..services.bill_committees import bill_committee_choices
from .base import DATETIME_INPUT_FORMATS, WorkflowInstanceFormMixin


class BillDocumentUrlForm(forms.Form):
    """The bill document URL field alone.

    The document picker re-renders just this field after an upload; building a
    full :class:`BillForm` for that would query the picker option lists for
    fields the response never renders.
    """

    bill_document_url = forms.URLField(
        required=False,
        label="Bill document url",
        help_text="SharePoint link to the bill document",
        widget=forms.TextInput(attrs={"class": "form-control", "type": "url"}),
    )


class BillForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for a :class:`Bill` (Online Bill Tracking BRS)."""

    initial_workflow_type = "Bill"

    # The B-number status is moved only by the transition machine (the detail
    # page's Status menu), so the edit form renders it read-only.
    protect_current_state = True

    introduced_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )

    def narrow_querysets(self):
        # A bill is committed to a committee of one of the two Houses, so the
        # joint committees and every non-committee group are not offered.
        self.fields["responsible_committee"].queryset = bill_committee_choices()
        self.fields["sponsor_name"].help_text = "Ministry or originating authority."

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None:
            return cleaned
        number = cleaned.get("bill_number")
        # The B-number names the bill's SharePoint folder. Re-pointing the link
        # while renumbering would leave the document pointing into the old
        # folder, so make the user clear it, save, and pick again in the new one.
        if (
            number
            and number != self.instance.bill_number
            and cleaned.get("bill_document_url")
        ):
            self.add_error(
                "bill_number",
                "The B-number names the bill's SharePoint folder. Clear the "
                "Bill document URL before changing it, then save and choose "
                "the document from the new folder.",
            )
        return cleaned

    class Meta:
        model = Bill
        fields = [
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "deadline",
            "priority",
            "bill_number",
            "short_title",
            "bill_type",
            "house_of_origin",
            "sponsor",
            "sponsor_name",
            "introduced_date",
            "responsible_committee",
            # "atc_reference",
            # "order_paper_reference",
            "bill_document_url",
            # "notes",
        ]
        widgets = {
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            # Swapped to a search picker by the mixin when the user list is long.
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "bill_number": forms.TextInput(attrs={"class": "form-control"}),
            "short_title": forms.TextInput(attrs={"class": "form-control"}),
            "bill_type": forms.Select(attrs={"class": "form-select"}),
            "house_of_origin": forms.Select(attrs={"class": "form-select"}),
            # Swapped to a search picker by the mixin when the user list is
            # long; the name below covers authorities with no account.
            "sponsor": forms.Select(attrs={"class": "form-select"}),
            # Chosen from the ministry lookup (see bill-form.html), which stores
            # the ministry's name in this text field.
            "sponsor_name": forms.HiddenInput(),
            # Swapped to a search picker by the mixin when there are many groups.
            "responsible_committee": forms.Select(attrs={"class": "form-select"}),
            # "atc_reference": forms.TextInput(attrs={"class": "form-control"}),
            # "order_paper_reference": forms.TextInput(attrs={"class": "form-control"}),
            "bill_document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
            # "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }


class BillVersionForm(forms.ModelForm):
    """Record a preserved bill version from the web UI (BRS §15A).

    The owning bill and the recording user are set by the view, not chosen
    here: a version always belongs to the bill it is recorded on, and
    ``recorded_by`` is the signed-in user (contributor accountability).
    """

    class Meta:
        model = BillVersion
        fields = [
            "version_label",
            "version_type",
            "version_date",
            "is_current",
            "document_url",
            "notes",
        ]
        widgets = {
            "version_label": forms.TextInput(attrs={"class": "form-control"}),
            "version_type": forms.Select(attrs={"class": "form-select"}),
            "version_date": DatePickerInput(attrs={"class": "form-control"}),
            "is_current": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }
