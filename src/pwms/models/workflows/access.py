"""Per-instance RBAC rows: group access and role/state overrides."""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from ..base import BaseModel


class WorkflowGroupAccess(BaseModel):
    """
    RBAC link between a workflow *instance* and the parliamentary ``Group``
    (House / Committee) that holds access on it.

    The instance is referenced through a generic foreign key so a single table
    serves every concrete workflow subclass. ``can_*`` here are the group-level
    defaults; :class:`WorkflowRolePermission` refines them per role and
    :class:`WorkflowStatePermission` per state.

    A row may exist because the instance was **referred** to the group (see
    :meth:`AbstractLegislativeWorkflow.sync_referral_access`): while the referral
    is open the group may view and **edit** the record, and once it closes the grant
    narrows to view so the group can still see what it was asked about.
    ``via_referral`` and ``referral_raised_edit`` record that provenance, and only a
    capability a referral itself raised is ever taken back. A referral does not
    confer ``transition`` — see
    :meth:`~AbstractLegislativeWorkflow.sync_referral_access`.
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
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    is_primary = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        "User", on_delete=models.SET_NULL, null=True, blank=True
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    # Provenance for a referral-driven grant: the row was created by a referral
    # rather than by the type's materialisation or an administrator, and these are
    # the capabilities that referral actually raised (and so may take back — a
    # right the group already held is never marked here).
    via_referral = models.BooleanField(
        default=False,
        help_text=(
            "Created by a referral, rather than by materialisation or an administrator."
        ),
    )
    referral_raised_edit = models.BooleanField(
        default=False,
        help_text="A referral raised can_edit; withdraw it once the referrals close.",
    )

    #: Capabilities that make a grant more than "may read this record".
    BEYOND_VIEW_FIELDS = (
        "can_edit",
        "can_delete",
        "can_share",
        "can_comment",
        "can_manage",
        "can_transition",
    )

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

    @property
    def grants_only_view(self):
        """True when nothing on this row lets the group do more than read."""
        return not any(getattr(self, field) for field in self.BEYOND_VIEW_FIELDS)


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
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
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
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
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
