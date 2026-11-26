"""Admin registrations for users, groups, roles and delegations."""

from django.contrib import admin
from mptt.admin import MPTTModelAdmin

from ..models import DutyDelegation, Group, GroupMembership, Role, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    """Admin for the custom ``pwms.User``."""

    list_display = (
        "username",
        "last_name",
        "first_name",
        "email",
        "employee_type",
        "identity_source",
        "is_mp",
        "is_active",
    )
    list_filter = (
        "is_active",
        "is_mp",
        "is_staff",
        "employee_type",
        "identity_source",
    )
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

    def has_delete_permission(self, request, obj=None):
        """Memberships are soft-deleted only; use Edit to end one instead."""
        return False


@admin.register(DutyDelegation)
class DutyDelegationAdmin(admin.ModelAdmin):
    """Admin for ``pwms.DutyDelegation`` (a member's duties lent to another)."""

    list_display = (
        "group",
        "delegator",
        "delegate",
        "start_date",
        "end_date",
        "is_active",
    )
    list_filter = ("group", "is_active")
    search_fields = (
        "group__name",
        "delegator__username",
        "delegator__last_name",
        "delegate__username",
        "delegate__last_name",
    )
    autocomplete_fields = ("group", "delegator", "delegate", "created_by")
    ordering = ("group", "-start_date")

    def has_delete_permission(self, request, obj=None):
        """Delegations are soft-deleted only; use Edit to end one instead."""
        return False
