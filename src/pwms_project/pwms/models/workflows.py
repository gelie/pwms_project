from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django_extensions.db.fields import AutoSlugField

from .base import BaseModel
from .permissions import Role
from .users import User


class WorkflowType(BaseModel):
    """
    Registry of reusable legislative state machines.

    A ``WorkflowType`` owns the :class:`State`\\ s and :class:`Transition`\\ s that
    describe how workflow *instances* (subclasses of
    :class:`AbstractLegislativeWorkflow`, e.g. ``InternationalResolution``) move
    through the legislature. Concrete instances reference a type and store only
    their ``current_state``.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to prevent creating new workflow instances of this type.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_initial_state(self):
        return self.states.filter(is_initial=True).first()


class AbstractLegislativeWorkflow(BaseModel):
    """
    Common schema shared by every concrete legislative workflow
    (``InternationalResolution`` today; ``Bill`` / ``Motion`` / ``Question`` later).

    The reusable machine (states + transitions) lives on ``workflow_type``; the
    instance only records which type it follows and its ``current_state``. RBAC
    is attached per instance through :class:`WorkflowGroupAccess` using a generic
    foreign key, so one set of access tables serves every concrete subclass.
    """

    workflow_type = models.ForeignKey(
        "WorkflowType",
        on_delete=models.PROTECT,
        related_name="%(class)s_instances",
        help_text="Reusable state machine (states/transitions) this instance follows.",
    )
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)

    current_state = models.ForeignKey(
        "State",
        on_delete=models.PROTECT,
        related_name="%(class)s_current",
        help_text="Current workflow state. Must belong to ``workflow_type``.",
    )

    # Clean reverse relations per concrete subclass (e.g. bill_owned, parliamentaryquestion_owned)
    owner = models.ForeignKey(
        "User",
        on_delete=models.PROTECT,
        related_name="%(class)s_owned",
    )
    assigned_to = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_assigned",
    )

    # Optional referrals relationship via Generic Relation or ManyToMany
    referred_to_groups = models.ManyToManyField(
        "Group",
        blank=True,
        related_name="%(class)s_referred",
        help_text="Groups this workflow is dynamically referred to",
    )

    deadline = models.DateTimeField(null=True, blank=True)
    priority = models.CharField(
        max_length=20,
        choices=[
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ],
        default="medium",
    )

    # -- lifecycle / RBAC helpers -------------------------------------------
    def clean(self):
        super().clean()
        if (
            self.workflow_type_id
            and self.current_state_id
            and self.current_state.workflow_type_id != self.workflow_type_id
        ):
            raise ValidationError(
                {
                    "current_state": (
                        f"State '{self.current_state.name}' does not belong to "
                        f"workflow type '{self.workflow_type.name}'."
                    )
                }
            )

    def _instance_ct(self):
        """ContentType of the concrete subclass row (never the abstract base)."""
        return ContentType.objects.get_for_model(self)

    def get_available_transitions(self):
        """Transitions that may be taken from the instance's current state."""
        return Transition.objects.filter(
            workflow_type=self.workflow_type, from_state=self.current_state
        ).order_by("order", "name")

    def perform_transition(
        self, transition, *, actor=None, comment="", ip_address=None
    ):
        """
        Validate and apply ``transition`` to this instance: advance
        ``current_state`` and append a ``TransitionLog`` audit entry.
        """
        if transition.workflow_type_id != self.workflow_type_id:
            raise ValidationError(
                "Transition does not belong to this instance's workflow type."
            )
        if transition.from_state_id != self.current_state_id:
            raise ValidationError(
                f"Transition '{transition.name}' is not valid from current "
                f"state '{self.current_state.name}'."
            )
        if transition.requires_comment and not comment:
            raise ValidationError("A comment is required for this transition.")

        from_state = self.current_state
        self.current_state = transition.to_state
        self.save(update_fields=["current_state", "updated_at"])
        return TransitionLog.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            action="STATE_TRANSITION",
            from_state=from_state,
            to_state=transition.to_state,
            actor=actor,
            ip_address=ip_address,
            notes=comment,
        )

    def group_accesses(self):
        """WorkflowGroupAccess rows granting groups access to this instance."""
        return WorkflowGroupAccess.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
        )

    def audit_logs(self):
        """TransitionLog entries for this instance, newest first."""
        return TransitionLog.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
        ).order_by("-timestamp")

    PERMISSION_ACTIONS = {
        "view": "can_view",
        "edit": "can_edit",
        "delete": "can_delete",
        "transition": "can_transition",
    }

    def can(self, user, action):
        """
        Resolve effective permission for ``user`` across every group holding
        access on this instance.

        ``action`` is one of ``view`` / ``edit`` / ``delete`` / ``transition``.
        For each group the user actively belongs to, the most specific grant wins:

        1. :class:`WorkflowRolePermission` for the user's role in that group
           (authoritative for the role; if it lists ``allowed_states`` the
           override applies only in those states);
        2. :class:`WorkflowStatePermission` for ``(group_access, current_state)``;
        3. base flags stored on :class:`WorkflowGroupAccess`.
        """
        field = self.PERMISSION_ACTIONS.get(action.lower())
        if field is None:
            raise ValueError(f"Unknown action: {action!r}")

        memberships = user.memberships.filter(is_active=True).select_related(
            "group", "role"
        )
        accesses = list(
            self.group_accesses().prefetch_related(
                "role_permissions", "state_permissions"
            )
        )
        access_by_group = {access.group_id: access for access in accesses}

        for membership in memberships:
            access = access_by_group.get(membership.group_id)
            if access is None:
                continue

            # 1) Role override (authoritative when it applies in this state)
            role_perm = next(
                (
                    rp
                    for rp in access.role_permissions.all()
                    if rp.role_id == membership.role_id
                ),
                None,
            )
            if role_perm is not None:
                applies_here = not role_perm.allowed_states.exists() or (
                    role_perm.allowed_states.filter(pk=self.current_state_id).exists()
                )
                if applies_here:
                    if getattr(role_perm, field):
                        return True
                    continue  # explicitly denied for this role in this state

            # 2) State permission for (group_access, current_state)
            state_perm = next(
                (
                    sp
                    for sp in access.state_permissions.all()
                    if sp.state_id == self.current_state_id
                ),
                None,
            )
            if state_perm is not None and getattr(state_perm, field):
                return True

            # 3) Group-level default
            if getattr(access, field):
                return True

        return False

    class Meta:
        abstract = True


class WorkflowGroupAccess(BaseModel):
    """
    RBAC link between a workflow *instance* and the parliamentary ``Group``
    (House / Committee) that holds access on it.

    The instance is referenced through a generic foreign key so a single table
    serves every concrete workflow subclass. ``can_*`` here are the group-level
    defaults; :class:`WorkflowRolePermission` refines them per role and
    :class:`WorkflowStatePermission` per state.
    """

    group = models.ForeignKey(
        "Group",
        on_delete=models.CASCADE,
        related_name="workflow_accesses",
        help_text="House / Committee (or sub-unit) granted access to the workflow.",
    )

    # Generic Foreign Key pointing to the concrete workflow's BigAutoField PK
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    # Group-level default permissions (used when no role/state rows exist)
    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    is_primary = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        "User", on_delete=models.SET_NULL, null=True, blank=True
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        obj = self.content_object
        label = (
            getattr(obj, "title", str(obj)) if obj is not None else f"#{self.object_id}"
        )
        return f"{self.group.name} -> {label}"


class WorkflowRolePermission(BaseModel):
    """
    Instance-level override for a *specific role* within a group's access.

    Roles are the same ``Role`` records a user holds through
    ``GroupMembership``. If no row exists for a role, the group-level defaults
    (``WorkflowGroupAccess.can_*``) and state permissions apply. If a row
    exists, it is authoritative for that role: it overrides both the group
    default and the state permission. The optional ``allowed_states`` M2M scopes
    the override to specific states only.
    """

    group_access = models.ForeignKey(
        WorkflowGroupAccess,
        on_delete=models.CASCADE,
        related_name="role_permissions",
    )
    role = models.ForeignKey(
        "Role",
        on_delete=models.CASCADE,
        related_name="workflow_permissions",
    )

    # Override permissions for this specific role
    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    # State-specific permissions (optional, overrides state permissions)
    allowed_states = models.ManyToManyField(
        "State",
        blank=True,
        related_name="role_workflow_permissions",
        help_text="If specified, these permissions only apply in these states",
    )

    class Meta:
        ordering = ["role__name"]
        indexes = [
            models.Index(fields=["group_access", "role"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group_access", "role"],
                name="workflows_workflowrolepermission_access_role_uniq",
            ),
        ]

    def __str__(self):
        perms = []
        if self.can_view:
            perms.append("view")
        if self.can_edit:
            perms.append("edit")
        if self.can_delete:
            perms.append("delete")
        if self.can_transition:
            perms.append("transition")
        perm_str = ", ".join(perms) if perms else "none"
        return f"{self.group_access.group.name} - {self.role.name}: [{perm_str}]"


class WorkflowStatePermission(BaseModel):
    """
    State-level permissions for a group's access to a workflow ("what roles can
    view/edit in Drafting vs Gazetted"). Rows are keyed by
    ``(group_access, state)``; the four ``can_*`` flags describe what the granted
    group may do while the workflow is in that state.
    """

    group_access = models.ForeignKey(
        WorkflowGroupAccess,
        on_delete=models.CASCADE,
        related_name="state_permissions",
    )
    state = models.ForeignKey(
        "State",
        on_delete=models.CASCADE,
        related_name="group_workflow_permissions",
    )

    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    class Meta:
        ordering = ["state__name"]
        indexes = [
            models.Index(fields=["group_access", "state"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group_access", "state"],
                name="workflows_workflowstatepermission_access_state_uniq",
            ),
        ]

    def __str__(self):
        perms = []
        if self.can_view:
            perms.append("view")
        if self.can_edit:
            perms.append("edit")
        if self.can_delete:
            perms.append("delete")
        if self.can_transition:
            perms.append("transition")
        perm_str = ", ".join(perms) if perms else "none"
        return f"{self.group_access.group.name} - {self.state.name}: [{perm_str}]"


class State(BaseModel):
    """
    Workflow states: Draft, Submitted, Under Review, Approved, Rejected, etc.
    """

    workflow_type = models.ForeignKey(
        WorkflowType, on_delete=models.CASCADE, related_name="states"
    )
    name = models.CharField(max_length=100)
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
        max_length=7, default="#5b8f22", help_text="Hex color code"
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

    # Email alert roles - members with these roles will be notified on this transition
    notify_roles = models.ManyToManyField(
        Role,
        related_name="notified_transitions",
        blank=True,
        help_text="Group members with these roles will receive an email alert when this transition occurs",
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


class TransitionLog(models.Model):
    """
    Generic domain audit log for workflow progression.

    ``content_object`` is the concrete workflow instance
    (``InternationalResolution`` today, ``Bill``/``Motion``/``Question`` later),
    so one log table serves every subclass. Entries are written by
    ``AbstractLegislativeWorkflow.perform_transition()``.
    """

    id = models.BigAutoField(primary_key=True)

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    action = models.CharField(max_length=100)  # e.g., 'STATE_TRANSITION', 'REFERRAL'
    from_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )
    to_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-timestamp"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} - {self.action} @ {self.timestamp}"


class InternationalResolution(AbstractLegislativeWorkflow):
    """
    Represents an international resolution workflow.
    Inherits from AbstractLegislativeWorkflow and adds specific fields if needed.
    """

    # Additional fields specific to international resolutions can be added here
    resolution_number = models.CharField(max_length=50, unique=True)
    adoption_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "International Resolution"
        verbose_name_plural = "International Resolutions"

    def __str__(self):
        return f"Resolution {self.resolution_number} - {self.title}"
