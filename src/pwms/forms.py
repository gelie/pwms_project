"""ModelForms backing the workflow-instance CRUD views.

Only the two concrete workflow subclasses are covered here
(:class:`~pwms.models.DelegationReport` and
:class:`~pwms.models.InternationalResolution`); the reusable machine itself
(``WorkflowType`` / ``State`` / ``Transition``) is curated through the Django
admin.

The forms implement one rule that the abstract base cannot express directly:
a new instance always starts in its workflow type's initial state, while an
existing instance may only move between states that belong to the type it was
created with.
"""

from django import forms
from django.contrib.auth import get_user_model

from .models import (
    DelegationReport,
    InternationalResolution,
    State,
    WorkflowType,
)

User = get_user_model()

#: ``datetime-local`` inputs post ``2026-09-11T14:30``; the ISO variants are
#: accepted too so a value submitted by an older browser (or a script) is not
#: silently rejected.
DATETIME_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]


class WorkflowInstanceFormMixin:
    """
    Shared create/update behaviour for concrete workflow instances.

    On **create** the form exposes ``workflow_type`` (only the enabled types the
    ``user`` may create in — creation is group-scoped RBAC, see
    :meth:`WorkflowType.can_create`) and hides ``current_state``, which is
    derived from the type's initial state. On **update** ``workflow_type`` is
    fixed and ``current_state`` becomes editable, limited to the states of that
    type.

    Pass the acting user as the ``user`` keyword argument to
    ``__init__``; omitting it yields no creatable types, which fails closed.
    """

    #: Workflow type pre-selected when creating a new instance.
    initial_workflow_type = ""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.is_create = self.instance.pk is None

        users = User.objects.filter(is_active=True).order_by("username")
        for name in ("owner", "assigned_to"):
            if name in self.fields:
                self.fields[name].queryset = users
                if not self.fields[name].required:
                    self.fields[name].empty_label = "—"
        if "responsible_group" in self.fields:
            self.fields["responsible_group"].queryset = self.fields[
                "responsible_group"
            ].queryset.order_by("name")

        if self.is_create:
            self.fields.pop("current_state", None)
            creatable = WorkflowType.creatable_by(user)
            self.fields["workflow_type"].queryset = creatable
            if self.initial_workflow_type:
                default_type = creatable.filter(name=self.initial_workflow_type).first()
                if default_type is not None:
                    self.fields["workflow_type"].initial = default_type
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
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    engagement_end_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
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
            "workflow_type": forms.Select(attrs={"class": "form-select"}),
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "owner": forms.Select(attrs={"class": "form-select"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "engagement_name": forms.TextInput(attrs={"class": "form-control"}),
            "location_city": forms.TextInput(attrs={"class": "form-control"}),
            "location_country": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "report_document_url": forms.TextInput(
                attrs={"class": "form-control", "type": "url"}
            ),
        }


class InternationalResolutionForm(WorkflowInstanceFormMixin, forms.ModelForm):
    """Create/update form for an :class:`InternationalResolution`."""

    initial_workflow_type = "International Resolution"

    adoption_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}),
    )
    deadline = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(
            attrs={"class": "form-control", "type": "datetime-local"},
            format="%Y-%m-%dT%H:%M",
        ),
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
            "workflow_type": forms.Select(attrs={"class": "form-select"}),
            "current_state": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "owner": forms.Select(attrs={"class": "form-select"}),
            "assigned_to": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "resolution_number": forms.TextInput(attrs={"class": "form-control"}),
            "resolution_text": forms.Textarea(
                attrs={"class": "form-control", "rows": 4}
            ),
            "responsible_group": forms.Select(attrs={"class": "form-select"}),
            "implementation_progress": forms.Textarea(
                attrs={"class": "form-control", "rows": 4}
            ),
        }
