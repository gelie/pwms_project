"""ModelForms backing the workflow-instance CRUD views.

Only the concrete workflow subclasses are covered here
(:class:`~pwms.models.DelegationReport`,
:class:`~pwms.models.InternationalResolution`,
:class:`~pwms.models.InternationalAgreement` and :class:`~pwms.models.Bill`);
the reusable machine itself (``WorkflowType`` / ``State`` / ``Transition``) is
curated through the Django admin.

The forms implement one rule that the abstract base cannot express directly:
a new instance always starts in its workflow type's initial state, while an
existing instance may only move between states that belong to the type it was
created with.

The report form also carries two child lists: the delegates attending the
engagement (``DelegationParticipant`` rows) and the resolutions adopted there
(``InternationalResolution`` instances nested through ``WorkflowRelationship``).

One plain :class:`forms.Form` lives here too: :class:`ReportShareForm` collects
the recipient list and expiry for sharing a report (see ``pwms.reporting``).
"""

from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import validate_email
from django.forms import (
    BaseFormSet,
    BaseInlineFormSet,
    formset_factory,
    inlineformset_factory,
)
from django_flatpickr.widgets import DatePickerInput, DateTimePickerInput

from .models import (
    Bill,
    BillVersion,
    DelegationParticipant,
    DelegationReport,
    Group,
    InternationalAgreement,
    InternationalResolution,
    State,
    WorkflowNote,
    WorkflowType,
)
from .models.reports import SCHEDULE_CHOICES
from .reporting.exports import ATTACHMENT_CHOICES
from .services.permissions import EDIT, resolve

User = get_user_model()

#: The flatpickr datetime widget posts ``2026-09-11 14:30:00`` (its
#: ``dateFormat`` is ``Y-m-d H:i:S``); the ISO variants are accepted too so a
#: value submitted by a non-JS browser (or a script) is not silently rejected.
DATETIME_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


class WorkflowInstanceFormMixin:
    """
    Shared create/update behaviour for concrete workflow instances.

    Each concrete form stands for exactly one workflow type, so the type is
    never a choice: on **create** it is pinned to this form's type and rendered
    hidden, and ``current_state`` is derived from that type's initial state. On
    **update** the type is fixed by the instance and ``current_state`` becomes
    editable, limited to the states of that type.

    ``owner`` is not a choice either — it is the acting user on create, and the
    instance's existing owner on update — so it too is rendered hidden, and its
    posted value is ignored.

    Pass the acting user as the ``user`` keyword argument to ``__init__``;
    omitting it fails closed (nothing may be created).
    """

    #: Workflow type pre-selected when creating a new instance.
    initial_workflow_type = ""

    #: Option lists longer than this are unusable as a <select>, so those fields
    #: render as HTMX search pickers instead (see ``pwms/_picker_field.html``).
    search_picker_threshold = 10

    #: Fields that may become search pickers, checked in this order. ``owner`` is
    #: deliberately absent: it is never chosen by hand (see ``__init__``).
    search_picker_fields = (
        "assigned_to",
        "responsible_group",
        "responsible_committee",
        "responsible_minister",
        "sponsor",
        "location_country",
        "location_city",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.is_create = self.instance.pk is None

        users = User.objects.filter(is_active=True).order_by("username")
        for name in ("owner", "assigned_to", "sponsor"):
            if name in self.fields:
                self.fields[name].queryset = users

        # The owner is never picked: a new instance belongs to the user creating
        # it, and an existing one keeps the owner it already has. ``disabled``
        # makes Django use the initial/instance value and ignore what was posted,
        # so a crafted POST cannot hand ownership to someone else.
        if "owner" in self.fields:
            owner_field = self.fields["owner"]
            owner_field.disabled = True
            owner_field.widget = forms.HiddenInput()
            if self.is_create and user is not None:
                self.initial["owner"] = user

        if "responsible_minister" in self.fields:
            # Only serving office holders are offered: a former minister, or one
            # without a PWMS account, is recorded on the document name instead.
            self.fields["responsible_minister"].queryset = User.ministers()
        if "responsible_group" in self.fields:
            self.fields["responsible_group"].queryset = self.fields[
                "responsible_group"
            ].queryset.order_by("name")
        if "responsible_committee" in self.fields:
            self.fields["responsible_committee"].queryset = self.fields[
                "responsible_committee"
            ].queryset.order_by("name")

        # Long option lists become search pickers: the widget then posts only the
        # chosen pk, and the queryset is left to validate it. Short lists stay a
        # plain <select> so the choice is visible without any JavaScript.
        self.search_pickers = set()
        for name in self.search_picker_fields:
            field = self.fields.get(name)
            queryset = getattr(field, "queryset", None)
            if queryset is None:
                continue
            if queryset.count() > self.search_picker_threshold:
                field.widget = forms.HiddenInput()
                self.search_pickers.add(name)
            elif not field.required:
                field.empty_label = "—"

        if self.is_create:
            self.fields.pop("current_state", None)
            # The type is implicit in the form, so it is not offered as a choice.
            # It is pinned to this form's type and rendered hidden; narrowing the
            # queryset to that single type also stops a hand-crafted POST from
            # swapping in another type, which would create a mis-typed record.
            type_field = self.fields["workflow_type"]
            creatable = WorkflowType.creatable_by(user)
            if self.initial_workflow_type:
                creatable = creatable.filter(name=self.initial_workflow_type)
            type_field.queryset = creatable
            # Seed both the field and the form: a ModelForm fills ``self.initial``
            # from the (empty) instance, and that mapping wins over the field's
            # own ``initial`` when Django resolves the value to render.
            default_type = creatable.first()
            type_field.initial = default_type
            self.initial["workflow_type"] = default_type
            type_field.widget = forms.HiddenInput()
        else:
            self.fields.pop("workflow_type", None)
            self.fields["current_state"].queryset = State.objects.filter(
                workflow_type=self.instance.workflow_type
            ).order_by("order", "name")

    @staticmethod
    def _initial_state(workflow_type):
        """Initial state for ``workflow_type`` (falls back to the first state)."""
        if workflow_type is None:
            return None
        state = workflow_type.get_initial_state()
        if state is None:
            state = workflow_type.states.order_by("order", "name").first()
        return state

    @property
    def picker_labels(self):
        """
        Label of the selected option for each search picker, keyed by field.

        The pickers (see ``pwms/_lookup_field.html``) only post a pk, so the page
        needs the matching label to prefill the visible search box. It comes from
        the instance on an update form, or from the form's initial on create,
        which keeps the box and the pk that would be submitted in step.
        """
        labels = {}
        for name in self.search_picker_fields:
            field = self.fields.get(name)
            queryset = getattr(field, "queryset", None)
            if queryset is None:
                continue
            selected = getattr(self.instance, name, None)
            value = selected if selected is not None else self.initial.get(name)
            if value is None:
                continue
            option = queryset.filter(pk=getattr(value, "pk", value)).first()
            if option is not None:
                labels[name] = getattr(option, "display_name", None) or str(option)
        return labels

    def clean(self):
        cleaned = super().clean()
        if self.is_create:
            workflow_type = cleaned.get("workflow_type")
            if workflow_type is not None and self._initial_state(workflow_type) is None:
                self.add_error(
                    "workflow_type",
                    "This workflow type has no states defined yet, so an "
                    "instance cannot be created for it.",
                )
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.is_create and not instance.current_state_id:
            instance.current_state = self._initial_state(instance.workflow_type)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class DelegationReportForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for a :class:`DelegationReport` (BRS BR02)."""

    initial_workflow_type = "Delegation Report"

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


class InternationalResolutionForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for an :class:`InternationalResolution`."""

    initial_workflow_type = "International Resolution"

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


class InternationalAgreementForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for an :class:`InternationalAgreement` (BRS BR02)."""

    initial_workflow_type = "International Agreement"

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


class BillForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for a :class:`Bill` (Online Bill Tracking BRS)."""

    initial_workflow_type = "Bill"

    introduced_date = forms.DateField(
        required=False,
        widget=DatePickerInput(attrs={"class": "form-control"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=DateTimePickerInput(attrs={"class": "form-control"}),
        input_formats=DATETIME_INPUT_FORMATS,
    )

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
            "atc_reference",
            "order_paper_reference",
            "bill_document_url",
            "notes",
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
            "sponsor_name": forms.TextInput(attrs={"class": "form-control"}),
            # Swapped to a search picker by the mixin when there are many groups.
            "responsible_committee": forms.Select(attrs={"class": "form-select"}),
            "atc_reference": forms.TextInput(attrs={"class": "form-control"}),
            "order_paper_reference": forms.TextInput(attrs={"class": "form-control"}),
            "bill_document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
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


# -- "Add a …" rows -----------------------------------------------------------
# The delegates and resolutions lists on the delegation report form are built
# from a one-line row of controls: the page's JavaScript reads that row, appends
# a formset row built from it and clears it again (see static/js/formset.js).
# Nothing below is validated on the server — the fields exist so the row keeps
# the shared lookup widget and stable ids — and each is prefixed so its inputs
# cannot collide with the report's own fields.


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


class ReportShareForm(forms.Form):
    """Details for minting a report share: who to tell, how long, and how often.

    A share always produces a link; emailing it, attaching a rendered copy and
    repeating it are optional extras. Recipients are validated one by one so a
    typo in a list is named rather than swallowed.
    """

    title = forms.CharField(
        required=False,
        max_length=255,
        label="Share name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "e.g. Q3 oversight report",
            }
        ),
    )
    message = forms.CharField(
        required=False,
        label="Message",
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional note to include with the link",
            }
        ),
    )
    expires_days = forms.ChoiceField(
        required=False,
        label="Link expires",
        choices=(
            ("", "Never"),
            ("7", "In 7 days"),
            ("30", "In 30 days"),
            ("90", "In 90 days"),
            ("365", "In a year"),
        ),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    schedule = forms.ChoiceField(
        required=False,
        label="Repeat",
        choices=SCHEDULE_CHOICES,
        initial="none",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    send_email = forms.BooleanField(
        required=False,
        label="Email the link",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    recipients = forms.CharField(
        required=False,
        label="Recipients",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "name@parliament.gov.za, other@example.org",
            }
        ),
    )
    attach_format = forms.ChoiceField(
        required=False,
        label="Attach a copy",
        choices=(("", "Do not attach"),) + ATTACHMENT_CHOICES,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def clean_recipients(self):
        raw = self.cleaned_data.get("recipients", "")
        addresses = [
            address.strip()
            for address in raw.replace(";", ",").split(",")
            if address.strip()
        ]
        for address in addresses:
            validate_email(address)
        return ", ".join(addresses)

    def clean(self):
        data = super().clean()
        scheduled = data.get("schedule") not in (None, "", "none")
        # A repeating share has to be emailed *somewhere*, so a schedule implies
        # the address list even when "Email the link" was left clear.
        if (data.get("send_email") or scheduled) and not data.get("recipients"):
            self.add_error(
                "recipients",
                "Add at least one address to email the report to.",
            )
        return data


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
