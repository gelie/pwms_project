"""Reusable state-machine definitions: states, transitions and guards."""

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField

from ... import state_colors
from ..base import BaseModel
from ..permissions import Role
from .types import PublicStatus, WorkflowType


class State(BaseModel):
    """
    Workflow states: Draft, Submitted, Under Review, Approved, Rejected, etc.

    ``public_status`` is the simplified, citizen-facing status a state maps to
    (:class:`PublicStatus`). Internal machines keep their detailed procedural
    states while the public view collapses them onto that vocabulary — the
    Online Bill Tracking BRS, for example, tracks each committee stage
    separately but publishes all of them as *Under Parliamentary
    Consideration*.
    """

    workflow_type = models.ForeignKey(
        WorkflowType, on_delete=models.CASCADE, related_name="states"
    )
    name = models.CharField(max_length=100)
    public_status = models.ForeignKey(
        PublicStatus,
        on_delete=models.PROTECT,
        related_name="states",
        help_text=("Simplified citizen-facing status this state publishes."),
    )
    slug = AutoSlugField(
        populate_from="name",
        unique=False,
        db_index=True,
        editable=False,
        help_text="Stable internal identifier for this state (unique per workflow type).",
    )
    description = models.TextField(blank=True)

    # State properties
    is_initial = models.BooleanField(
        default=False,
        help_text="Is this the initial state?",  # ignore
    )  # type: ignore
    is_terminal = models.BooleanField(default=False, help_text="Is this a final state?")  # type: ignore
    allows_referrals = models.BooleanField(
        default=True,
        help_text="Can workflows in this state be referred to other groups?",
    )  # type: ignore

    # Ordering for display
    order = models.IntegerField(default=0)  # type: ignore

    # Visual properties
    color = models.CharField(
        max_length=7,
        default=state_colors.IN_PROGRESS,
        help_text=(
            "Hex color code. Use the standard colour for the state's role "
            "(see the default_color property) so every workflow looks alike."
        ),
    )

    @property
    def default_color(self):
        """The standard colour for this state's role (see ``pwms.state_colors``)."""
        return state_colors.state_color(
            is_initial=self.is_initial,
            is_terminal=self.is_terminal,
            name=self.name,
        )

    class Meta:
        ordering = ["workflow_type", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_type", "name"],
                name="workflows_state_type_name_uniq",
            ),
            models.UniqueConstraint(
                fields=["workflow_type", "slug"],
                name="workflows_state_type_slug_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.workflow_type.name} - {self.name}"


class Transition(BaseModel):
    """
    Defines allowed transitions between workflow states.
    """

    workflow_type = models.ForeignKey(
        WorkflowType, on_delete=models.CASCADE, related_name="transitions"
    )
    name = models.CharField(max_length=100)
    slug = AutoSlugField(
        populate_from="name",
        unique=False,
        db_index=True,
        editable=False,
        help_text="Stable internal identifier for this transition (unique per workflow type).",
    )
    from_state = models.ForeignKey(
        State, on_delete=models.CASCADE, related_name="transitions_from"
    )
    to_state = models.ForeignKey(
        State, on_delete=models.CASCADE, related_name="transitions_to"
    )

    # Permissions (roles are the same Role records used for group membership)
    allowed_roles = models.ManyToManyField(Role, related_name="allowed_transitions")

    # Who is alerted about this transition is declared with ``NotificationRule``
    # rows (event "Record transitioned", optionally limited to this transition).

    # Transition guards: at least one event of each listed type must have been
    # recorded on the instance before this transition can be performed, e.g.
    # "Closed – House approved" requires an ``atc_update_published`` event.
    required_event_types = models.ManyToManyField(
        "EventType",
        related_name="required_by_transitions",
        blank=True,
        help_text=(
            "Transitions are blocked until at least one event of each selected "
            "type has been recorded on the workflow instance."
        ),
    )

    # Transition properties
    requires_comment = models.BooleanField(default=True)  # type: ignore
    order = models.IntegerField(default=0)  # type: ignore

    class Meta:
        ordering = ["workflow_type", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_type", "from_state", "to_state"],
                name="workflows_transition_type_from_to_uniq",
            ),
            models.UniqueConstraint(
                fields=["workflow_type", "slug"],
                name="workflows_transition_type_slug_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.name}: {self.from_state.name} → {self.to_state.name}"


class TransitionCondition(BaseModel):
    """
    Extra business rule guarding a transition, beyond the state machine and
    the event guards (``Transition.required_event_types``).

    ``condition_type`` selects a handler from
    :data:`TRANSITION_CONDITION_HANDLERS`; ``field_name`` parameterises handlers
    that need it (``field_set``). Conditions are evaluated by
    ``AbstractLegislativeWorkflow.unmet_transition_conditions()`` and therefore
    block ``perform_transition()`` until satisfied.
    """

    CONDITION_CHOICES = [
        ("no_open_referrals", _("No open referrals")),
        ("all_children_closed", _("All child workflows are terminal")),
        ("field_set", _("Required field is set")),
    ]

    transition = models.ForeignKey(
        Transition,
        on_delete=models.CASCADE,
        related_name="conditions",
        help_text="Transition this rule guards.",
    )
    condition_type = models.CharField(max_length=50, choices=CONDITION_CHOICES)
    field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Field checked by parameterised conditions, e.g. field_set.",
    )
    enabled = models.BooleanField(
        default=True, help_text="Uncheck to temporarily disable the rule."
    )
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["transition", "condition_type", "field_name"],
                name="workflows_transitioncondition_uniq",
            ),
        ]

    def __str__(self):
        detail = f" ({self.field_name})" if self.field_name else ""
        return f"{self.transition.name}: {self.get_condition_type_display()}{detail}"

    def evaluate(self, instance):
        """Return a message when this rule is unmet, else ``None``."""
        handler = TRANSITION_CONDITION_HANDLERS.get(self.condition_type)
        if handler is None:
            return f"Unknown transition condition '{self.condition_type}'."
        return handler(instance, self)


#: Registry of condition handlers, keyed by ``TransitionCondition.condition_type``.
#: A handler takes ``(instance, condition)`` and returns an unmet message or ``None``.
TRANSITION_CONDITION_HANDLERS = {}


def transition_condition(condition_type):
    """Register a handler for a ``TransitionCondition.condition_type``."""

    def decorator(handler):
        TRANSITION_CONDITION_HANDLERS[condition_type] = handler
        return handler

    return decorator


@transition_condition("no_open_referrals")
def _no_open_referrals(instance, condition):
    count = instance.referrals().filter(status="open").count()
    if count:
        return f"Resolve {count} open referral(s) before this transition."
    return None


@transition_condition("all_children_closed")
def _all_children_closed(instance, condition):
    open_children = [
        child for child in instance.sub_workflows if not child.current_state.is_terminal
    ]
    if open_children:
        return f"{len(open_children)} child workflow(s) are not in a terminal state."
    return None


@transition_condition("field_set")
def _field_set(instance, condition):
    if not condition.field_name:
        return "Condition misconfigured: no field name set."
    if not getattr(instance, condition.field_name, None):
        return f"Requires '{condition.field_name}' to be set."
    return None
