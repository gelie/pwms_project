from typing import ClassVar

from django.contrib import admin
from mptt.admin import MPTTModelAdmin

from .models import (
    Group,
    GroupMembership,
    InternationalResolution,
    Role,
    SharepointDrive,
    SharepointFolder,
    SharepointSite,
    SharepointSiteMember,
    SharepointToken,
    State,
    Transition,
    TransitionLog,
    User,
    WorkflowGroupAccess,
    WorkflowRolePermission,
    WorkflowStatePermission,
    WorkflowType,
)

admin.site.site_header = "PWMS Admin"
admin.site.site_title = "PWMS Admin Portal"
admin.site.index_title = "Welcome to the PWMS Admin Portal"

# Register your models here.


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """Admin for the custom ``pwms.User``."""

    list_display = (
        "username",
        "last_name",
        "first_name",
        "email",
        "employee_type",
        "is_mp",
        "is_active",
    )
    list_filter = ("is_active", "is_mp", "is_staff", "employee_type")
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("last_name", "first_name")
    filter_horizontal = ("groups", "user_permissions")


@admin.register(Group)
class GroupAdmin(MPTTModelAdmin):
    """Admin for the hierarchical ``pwms.Group`` (MPTT).

    Subclasses ``mptt.admin.MPTTModelAdmin`` so the change list is ordered as
    a tree (``tree_id``, ``lft``) and every row is indented by its depth,
    showing each group together with all of its descendants.
    """

    list_display = ("name", "parent", "group_type", "short_name", "is_active")
    list_filter = ("group_type", "is_active")
    search_fields = ("name", "short_name")
    # Note: deliberately no ``ordering`` here. MPTTModelAdmin falls back to
    # tree ordering (tree_id, lft) when it's empty, which is what keeps the
    # hierarchy visually intact. An explicit ordering would flatten the tree.


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    """Admin for ``pwms.Role``."""

    list_display = (
        "name",
        "can_create_workflows",
        "can_transition_workflows",
        "can_assign_workflows",
        "can_manage_permissions",
    )
    search_fields = ("name",)
    ordering = ("name",)


@admin.register(GroupMembership)
class GroupMembershipAdmin(admin.ModelAdmin):
    """Admin for ``pwms.GroupMembership`` (user ↔ group ↔ role)."""

    list_display = (
        "group",
        "user",
        "role",
        "start_date",
        "end_date",
        "is_active",
    )
    list_filter = ("group", "role", "is_active")
    search_fields = (
        "user__username",
        "user__first_name",
        "user__last_name",
        "group__name",
        "role__name",
    )
    autocomplete_fields = ("group", "user", "role")
    ordering = ("group", "-start_date")


@admin.register(SharepointSite)
class SharepointSiteAdmin(admin.ModelAdmin):
    """Admin for the mirrored ``SharepointSite`` rows."""

    list_display = (
        "name",
        "url",
        "site_id",
        "is_personal_site",
        "last_synced_at",
    )
    list_filter = ("is_personal_site",)
    search_fields = ("name", "url", "site_id")
    ordering = ("name",)


@admin.register(SharepointSiteMember)
class SharepointSiteMemberAdmin(admin.ModelAdmin):
    """Admin for ``SharepointSiteMember`` (site ↔ user)."""

    list_display = ("site", "user", "date_added", "is_active")
    list_filter = ("is_active",)
    search_fields = (
        "site__name",
        "user__username",
        "user__first_name",
        "user__last_name",
    )
    autocomplete_fields = ("site", "user")
    ordering = ("site", "-date_added")


@admin.register(SharepointDrive)
class SharepointDriveAdmin(admin.ModelAdmin):
    """Admin for the mirrored ``SharepointDrive`` rows."""

    list_display = ("site", "name", "drive_id")
    list_filter = ("site",)
    search_fields = ("name", "drive_id", "site__name")
    autocomplete_fields = ("site",)
    ordering = ("site", "name")


# --- Workflow engine: types, states, transitions ---------------------------


class StateInline(admin.TabularInline):
    """States belonging to a ``WorkflowType``, editable on the type page."""

    model = State
    extra = 0
    fields = (
        "name",
        "order",
        "is_initial",
        "is_terminal",
        "allows_referrals",
        "color",
    )
    ordering = ("order", "name")


class TransitionInline(admin.TabularInline):
    """Transitions belonging to a ``WorkflowType``, editable on the type page.

    ``from_state`` / ``to_state`` reference states of this same workflow type
    (choose existing states; to wire up a brand-new state, save the type first
    and then add the transition). Allowed/notify roles are edited from the
    :class:`TransitionAdmin` change page, not inline.
    """

    model = Transition
    extra = 0
    fields = (
        "name",
        "from_state",
        "to_state",
        "requires_comment",
        "order",
    )
    ordering = ("order", "name")
    autocomplete_fields = ("from_state", "to_state")


@admin.register(WorkflowType)
class WorkflowTypeAdmin(admin.ModelAdmin):
    """Admin for reusable workflow state machines (states + transitions inline)."""

    list_display = ("name", "enabled", "created_at")
    list_filter = ("enabled",)
    search_fields = ("name",)
    ordering = ("name",)
    inlines: ClassVar[list[type[admin.TabularInline]]] = [
        StateInline,
        TransitionInline,
    ]


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    """Admin for workflow states (per workflow type)."""

    list_display = (
        "workflow_type",
        "name",
        "order",
        "is_initial",
        "is_terminal",
        "color",
    )
    list_filter = ("workflow_type", "is_initial", "is_terminal")
    search_fields = ("name", "workflow_type__name")
    autocomplete_fields = ("workflow_type",)
    ordering = ("workflow_type", "order", "name")


@admin.register(Transition)
class TransitionAdmin(admin.ModelAdmin):
    """Admin for allowed transitions between states."""

    list_display = (
        "workflow_type",
        "name",
        "from_state",
        "to_state",
        "requires_comment",
        "order",
    )
    list_filter = ("workflow_type", "requires_comment")
    search_fields = (
        "name",
        "workflow_type__name",
        "from_state__name",
        "to_state__name",
    )
    autocomplete_fields = ("workflow_type", "from_state", "to_state")
    filter_horizontal = ("allowed_roles", "notify_roles")
    ordering = ("workflow_type", "order", "name")


# --- Workflow RBAC: instance access, role & state permissions --------------


@admin.register(WorkflowGroupAccess)
class WorkflowGroupAccessAdmin(admin.ModelAdmin):
    """Admin for group-level access granted to a workflow instance (GFK)."""

    list_display = (
        "group",
        "workflow_target",
        "can_view",
        "can_edit",
        "can_delete",
        "can_transition",
        "is_primary",
        "granted_at",
    )
    list_filter = ("group", "can_view", "can_edit", "can_transition", "is_primary")
    search_fields = ("group__name", "object_id")
    autocomplete_fields = ("group",)
    ordering = ("group", "-granted_at")

    @admin.display(description="Workflow instance")
    def workflow_target(self, obj):
        if obj.content_object is not None:
            return str(obj.content_object)
        return f"#{obj.object_id}"


@admin.register(WorkflowRolePermission)
class WorkflowRolePermissionAdmin(admin.ModelAdmin):
    """Admin for per-role overrides on a group's workflow access."""

    list_display = (
        "group_access",
        "role",
        "can_view",
        "can_edit",
        "can_delete",
        "can_transition",
    )
    list_filter = ("can_view", "can_edit", "can_transition")
    search_fields = ("group_access__group__name", "role__name")
    autocomplete_fields = ("group_access", "role", "allowed_states")
    ordering = ("group_access", "role")


@admin.register(WorkflowStatePermission)
class WorkflowStatePermissionAdmin(admin.ModelAdmin):
    """Admin for per-state permissions on a group's workflow access."""

    list_display = (
        "group_access",
        "state",
        "can_view",
        "can_edit",
        "can_delete",
        "can_transition",
    )
    list_filter = ("can_view", "can_edit", "can_transition")
    search_fields = ("group_access__group__name", "state__name")
    autocomplete_fields = ("group_access", "state")
    ordering = ("group_access", "state")


# --- Concrete workflow instance & its audit log ----------------------------


@admin.register(InternationalResolution)
class InternationalResolutionAdmin(admin.ModelAdmin):
    """Admin for international-resolution workflow instances."""

    list_display = (
        "resolution_number",
        "title",
        "workflow_type",
        "current_state",
        "owner",
        "priority",
        "deadline",
    )
    list_filter = ("workflow_type", "current_state", "priority")
    search_fields = ("title", "resolution_number")
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
    )
    filter_horizontal = ("referred_to_groups",)
    ordering = ("resolution_number",)


@admin.register(TransitionLog)
class TransitionLogAdmin(admin.ModelAdmin):
    """Read-only view of semantic workflow state-transition audit entries."""

    list_display = (
        "id",
        "workflow_target",
        "action",
        "from_state",
        "to_state",
        "actor",
        "timestamp",
    )
    list_filter = ("action", "content_type")
    readonly_fields = (
        "content_type",
        "object_id",
        "action",
        "from_state",
        "to_state",
        "actor",
        "ip_address",
        "timestamp",
        "notes",
    )
    ordering = ("-timestamp",)

    @admin.display(description="Workflow instance")
    def workflow_target(self, obj):
        if obj.content_object is not None:
            return str(obj.content_object)
        return f"#{obj.object_id}"

    def has_add_permission(self, request):
        return False


# --- SharePoint: cached token & folder hierarchy ----------------------------


@admin.register(SharepointToken)
class SharepointTokenAdmin(admin.ModelAdmin):
    """Admin for cached SharePoint Graph access tokens."""

    list_display = ("is_active", "expires_at", "created_at")
    list_filter = ("is_active",)
    ordering = ("-created_at",)
    readonly_fields = ("access_token",)


@admin.register(SharepointFolder)
class SharepointFolderAdmin(admin.ModelAdmin):
    """Admin for the mirrored SharePoint folder hierarchy."""

    list_display = ("site", "drive", "name", "parent_folder", "web_url")
    search_fields = ("name", "site__name", "drive__name")
    autocomplete_fields = ("site", "drive", "parent_folder")
    ordering = ("site", "drive", "name")
