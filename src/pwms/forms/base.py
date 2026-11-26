"""Shared form mixins and constants for workflow instance forms."""

from django import forms
from django.contrib.auth import get_user_model

from ..models import State, WorkflowType

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
    **update** the type is fixed by the instance, and ``current_state`` renders
    either editable (limited to the states of that type) or — for forms opting
    into :attr:`protect_current_state` — disabled display-only, because state
    changes must go through the transition machine instead.

    ``owner`` is not a choice either — it is the acting user on create, and the
    instance's existing owner on update — so it too is rendered hidden, and its
    posted value is ignored.

    Pass the acting user as the ``user`` keyword argument to ``__init__``;
    omitting it fails closed (nothing may be created).
    """

    #: Workflow type pre-selected when creating a new instance.
    initial_workflow_type = ""

    #: Opt-in protection of ``current_state`` on the update form: the state is
    #: a transition-machine concern (the detail page's status menu posts to
    #: ``workflow_transition``, which validates guards/permissions and logs the
    #: move), never a profile edit. The disabled field also ignores posted
    #: values, so a hand-crafted POST cannot move the state through this form.
    protect_current_state = False

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

        # Concrete forms narrow an option list here, before the picker decision
        # below measures it: a field limited to a handful of rows should stay a
        # <select> even when the underlying table is large.
        self.narrow_querysets()

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
            state_field = self.fields["current_state"]
            if self.protect_current_state:
                # Display-only: the one rendered option is this instance's own
                # state, and Django ignores POSTed data for disabled fields.
                state_field.disabled = True
                state_field.queryset = State.objects.filter(
                    pk=self.instance.current_state_id
                )
                state_field.help_text = (
                    "The status moves through the state machine on this "
                    "instrument's page, not by editing it."
                )
            else:
                state_field.queryset = State.objects.filter(
                    workflow_type=self.instance.workflow_type
                ).order_by("order", "name")

    def narrow_querysets(self):
        """Hook for concrete forms to limit a field's options (default: no-op)."""

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


class SearchPickerMixin:
    """Render selected foreign keys as HTMX search pickers.

    A long option list is unusable as a ``<select>``, so these forms post the
    chosen pk from a lookup view instead (see ``pwms/_lookup_field.html``).
    ``picker_fields`` names the fields that use it; their querysets still
    validate whatever pk is submitted.
    """

    picker_fields = ()

    def enable_pickers(self):
        self.search_pickers = set()
        for name in self.picker_fields:
            field = self.fields.get(name)
            if field is None or getattr(field, "queryset", None) is None:
                continue
            field.widget = forms.HiddenInput()
            self.search_pickers.add(name)

    @property
    def picker_labels(self):
        """Label of the selected option for each picker, keyed by field name."""
        labels = {}
        for name in self.picker_fields:
            field = self.fields.get(name)
            if field is None:
                continue
            selected = getattr(self.instance, name, None)
            value = selected if selected is not None else self.initial.get(name)
            if value is None:
                continue
            option = field.queryset.filter(pk=getattr(value, "pk", value)).first()
            if option is not None:
                labels[name] = getattr(option, "display_name", None) or str(option)
        return labels
