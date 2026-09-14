from typing import ClassVar

from django import forms
from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db.models import Q
from mptt.admin import MPTTModelAdmin

from .models import (
    City,
    Country,
    DelegationParticipant,
    DelegationReport,
    DelegationReportUpdate,
    EventType,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    SharepointDrive,
    SharepointFolder,
    SharepointSite,
    SharepointSiteMember,
    SharepointToken,
    State,
    Transition,
    TransitionCondition,
    TransitionLog,
    User,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowReferral,
    WorkflowRelationship,
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


class SharepointSiteMemberInline(admin.TabularInline):
    """Show and edit a site's members directly on the site change page."""

    model = SharepointSiteMember
    extra = 0
    fields = ("user", "is_active", "date_added")
    readonly_fields = ("date_added",)
    autocomplete_fields = ("user",)
    ordering = ("user__last_name", "user__first_name")
    show_change_link = True
    verbose_name = "member"
    verbose_name_plural = "members"


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
    inlines = (SharepointSiteMemberInline,)


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


class WorkflowTypeAdminForm(forms.ModelForm):
    """
    Constrain ``create_roles`` to roles held by members of the type's group.

    When the type already has a group the choices are filtered to that group's
    roles — keeping any currently-selected role so a stale assignment is
    reported rather than silently dropped. ``clean()`` then enforces the rule
    against the group submitted in the same form, which also covers the "add
    type" flow where the group is only known once the request is bound.
    """

    class Meta:
        model = WorkflowType
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        group = self.instance.group if self.instance.pk else None
        if group is not None:
            selected = self.instance.create_roles.all()
            self.fields["create_roles"].queryset = (
                Role.objects.filter(
                    Q(groupmembership__group_id=group.pk) | Q(pk__in=selected)
                )
                .distinct()
                .order_by("name")
            )

    def clean(self):
        cleaned = super().clean()
        group = cleaned.get("group")
        roles = cleaned.get("create_roles")
        if group is not None and roles:
            allowed_ids = set(
                Role.objects.filter(groupmembership__group_id=group.pk).values_list(
                    "pk", flat=True
                )
            )
            invalid = sorted(role.name for role in roles if role.pk not in allowed_ids)
            if invalid:
                raise forms.ValidationError(
                    {
                        "create_roles": (
                            f"These roles do not belong to '{group.name}': "
                            f"{', '.join(invalid)}."
                        )
                    }
                )
        return cleaned


@admin.register(WorkflowType)
class WorkflowTypeAdmin(admin.ModelAdmin):
    """Admin for reusable workflow state machines (states + transitions inline)."""

    form = WorkflowTypeAdminForm
    list_display = ("name", "group", "enabled", "created_at")
    list_filter = ("enabled",)
    search_fields = ("name", "group__name")
    autocomplete_fields = ("group",)
    filter_horizontal = ("create_roles", "viewer_groups")
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


class TransitionConditionInline(admin.TabularInline):
    """Extra guard rules evaluated when this transition is performed."""

    model = TransitionCondition
    extra = 0
    fields = ("condition_type", "field_name", "enabled", "order")
    ordering = ("order", "id")


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
    filter_horizontal = ("allowed_roles", "notify_roles", "required_event_types")
    ordering = ("workflow_type", "order", "name")
    inlines: ClassVar[list[type[admin.TabularInline]]] = [TransitionConditionInline]


@admin.register(TransitionCondition)
class TransitionConditionAdmin(admin.ModelAdmin):
    """Admin for standalone inspection of transition guard rules."""

    list_display = ("transition", "condition_type", "field_name", "enabled", "order")
    list_filter = ("condition_type", "enabled")
    search_fields = (
        "transition__name",
        "transition__workflow_type__name",
        "field_name",
    )
    autocomplete_fields = ("transition",)
    ordering = ("transition", "order")


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
        "can_share",
        "can_comment",
        "can_manage",
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
        "can_share",
        "can_comment",
        "can_manage",
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
        "can_share",
        "can_comment",
        "can_manage",
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


class WorkflowEventInline(GenericTabularInline):
    """Read-only timeline of domain events recorded on a workflow instance."""

    model = WorkflowEvent
    ct_field = "content_type"
    fk_field = "object_id"
    extra = 0
    can_delete = False
    fields = (
        "occurred_at",
        "event_type",
        "origin",
        "actor",
        "document_url",
        "notes",
    )
    readonly_fields = fields
    verbose_name_plural = "Domain events (timeline)"

    def has_add_permission(self, request, obj=None):
        return False


class WorkflowReferralInline(GenericTabularInline):
    """Referrals of a workflow instance to committees/groups (BR02.8)."""

    model = WorkflowReferral
    ct_field = "content_type"
    fk_field = "object_id"
    extra = 0
    can_delete = False
    fields = (
        "referred_to",
        "referred_by",
        "due_date",
        "status",
        "responded_at",
        "notes",
    )
    readonly_fields = ("responded_at",)
    autocomplete_fields = ("referred_to", "referred_by")
    verbose_name_plural = "Referrals"


class WorkflowGroupAccessInline(GenericTabularInline):
    """Read-only view of the group grants materialised on a workflow instance.

    Rows come from the type's owning group (``is_primary``) and its viewer
    groups; edit or add grants through the WorkflowGroupAccess admin, not here,
    so the instance page stays a faithful view of what was materialised.
    """

    model = WorkflowGroupAccess
    ct_field = "content_type"
    fk_field = "object_id"
    extra = 0
    can_delete = False
    fields = (
        "group",
        "is_primary",
        "can_view",
        "can_edit",
        "can_delete",
        "can_share",
        "can_comment",
        "can_manage",
        "can_transition",
        "granted_by",
        "granted_at",
    )
    readonly_fields = fields
    ordering = ("-is_primary", "group__name")
    verbose_name_plural = "Group access (owner + viewer groups)"
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


class DelegationReportUpdateInline(admin.TabularInline):
    """BR03 report updates (ATC publication details) appended to a report."""

    model = DelegationReportUpdate
    extra = 0
    can_delete = False
    fields = (
        "update_date",
        "resulting_state",
        "atc_reference",
        "atc_publication_date",
        "atc_page_number",
        "atc_document_url",
        "notes",
        "recorded_by",
    )
    autocomplete_fields = ("resulting_state", "recorded_by")
    ordering = ("-update_date",)


@admin.register(InternationalResolution)
class InternationalResolutionAdmin(admin.ModelAdmin):
    """Admin for international-resolution workflow instances."""

    list_display = (
        "resolution_number",
        "title",
        "workflow_type",
        "current_state",
        "responsible_group",
        "owner",
        "priority",
        "deadline",
    )
    list_filter = ("workflow_type", "current_state", "priority")
    search_fields = ("title", "resolution_number", "resolution_text")
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "responsible_group",
        "owner",
        "assigned_to",
    )
    ordering = ("resolution_number",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
    ]


@admin.register(InternationalAgreement)
class InternationalAgreementAdmin(admin.ModelAdmin):
    """Admin for international-agreement workflow instances (BRS BR02/BR03)."""

    list_display = (
        "reference_number",
        "title",
        "agreement_type",
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "deadline",
        "is_overdue",
    )
    list_filter = ("workflow_type", "current_state", "agreement_type", "priority")
    search_fields = (
        "reference_number",
        "title",
        "submitting_department",
        "responsible_minister",
    )
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
    )
    readonly_fields = ("reference_number",)
    filter_horizontal = ("referral_committees",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
    ]

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue


class DelegationParticipantInline(admin.TabularInline):
    """Delegation members / support officials edited on the report page."""

    model = DelegationParticipant
    extra = 0
    fields = (
        "participant_type",
        "title",
        "first_name",
        "last_name",
        "delegation_role",
        "user",
        "order",
    )
    autocomplete_fields = ("user",)
    ordering = ("participant_type", "order", "last_name")


@admin.register(DelegationReport)
class DelegationReportAdmin(admin.ModelAdmin):
    """Admin for delegation reports (BRS BR02/BR03)."""

    list_display = (
        "reference_number",
        "title",
        "engagement_name",
        "current_state",
        "owner",
        "assigned_to",
        "deadline",
        "is_overdue",
    )
    list_filter = ("workflow_type", "current_state", "priority")
    search_fields = (
        "reference_number",
        "title",
        "engagement_name",
        "location_city__name",
        "location_country__name",
    )
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "location_country",
        "location_city",
    )
    readonly_fields = ("reference_number",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        DelegationParticipantInline,
        DelegationReportUpdateInline,
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
    ]

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue


@admin.register(DelegationParticipant)
class DelegationParticipantAdmin(admin.ModelAdmin):
    """Admin for delegation members and support officials (BR02.3.7/8)."""

    list_display = (
        "delegation_report",
        "participant_type",
        "full_name",
        "delegation_role",
        "user",
    )
    list_filter = ("participant_type",)
    search_fields = (
        "first_name",
        "last_name",
        "delegation_role",
        "delegation_report__reference_number",
        "delegation_report__title",
    )
    autocomplete_fields = ("delegation_report", "user")
    ordering = ("delegation_report", "participant_type", "order", "last_name")


@admin.register(WorkflowRelationship)
class WorkflowRelationshipAdmin(admin.ModelAdmin):
    """Admin for generic parent/child links between workflow instances."""

    list_display = (
        "parent_target",
        "child_target",
        "relationship_type",
        "order",
    )
    list_filter = (
        "relationship_type",
        "parent_content_type",
        "child_content_type",
    )
    search_fields = ("notes",)
    ordering = ("order", "id")

    @admin.display(description="Parent")
    def parent_target(self, obj):
        if obj.parent is not None:
            return str(obj.parent)
        return f"#{obj.parent_object_id}"

    @admin.display(description="Child")
    def child_target(self, obj):
        if obj.child is not None:
            return str(obj.child)
        return f"#{obj.child_object_id}"


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


# --- Domain events: registry + append-only log ------------------------------


@admin.register(EventType)
class EventTypeAdmin(admin.ModelAdmin):
    """Admin for the data-driven registry of workflow event types."""

    list_display = ("name", "slug", "is_system", "created_at")
    list_filter = ("is_system",)
    search_fields = ("name", "description")
    ordering = ("name",)


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(admin.ModelAdmin):
    """Read-only view of the append-only domain event log."""

    list_display = (
        "id",
        "workflow_target",
        "event_type",
        "occurred_at",
        "origin",
        "actor",
    )
    list_filter = ("event_type", "origin", "content_type")
    search_fields = ("event_type__name", "notes")
    readonly_fields = (
        "content_type",
        "object_id",
        "event_type",
        "occurred_at",
        "actor",
        "origin",
        "payload",
        "document_url",
        "notes",
        "transition_log",
    )
    ordering = ("-occurred_at",)

    @admin.display(description="Workflow instance")
    def workflow_target(self, obj):
        if obj.content_object is not None:
            return str(obj.content_object)
        return f"#{obj.object_id}"

    def has_add_permission(self, request):
        return False


# --- Referrals & BR03 report updates ----------------------------------------


@admin.register(WorkflowReferral)
class WorkflowReferralAdmin(admin.ModelAdmin):
    """Admin for typed referrals (who / to whom / due / response)."""

    list_display = (
        "id",
        "workflow_target",
        "referred_to",
        "status",
        "referred_by",
        "referred_at",
        "due_date",
    )
    list_filter = ("status", "referred_to", "content_type")
    search_fields = ("notes", "response_notes", "referred_to__name")
    autocomplete_fields = ("referred_to", "referred_by", "responded_by", "recalled_by")
    readonly_fields = ("referred_at", "deadline_notified_at")
    ordering = ("-referred_at",)

    @admin.display(description="Workflow instance")
    def workflow_target(self, obj):
        if obj.content_object is not None:
            return str(obj.content_object)
        return f"#{obj.object_id}"


@admin.register(DelegationReportUpdate)
class DelegationReportUpdateAdmin(admin.ModelAdmin):
    """Admin for the BR03 update history (append rows; no deletions)."""

    list_display = (
        "id",
        "delegation_report",
        "update_date",
        "resulting_state",
        "atc_reference",
        "atc_publication_date",
        "recorded_by",
    )
    list_filter = ("resulting_state",)
    search_fields = (
        "atc_reference",
        "notes",
        "delegation_report__reference_number",
        "delegation_report__title",
    )
    autocomplete_fields = ("delegation_report", "resulting_state", "recorded_by")
    ordering = ("-update_date",)

    def has_delete_permission(self, request, obj=None):
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


# --- Reference data: countries & cities (loaded by load_places) ----------------


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    """Admin for the country list behind the location pickers."""

    list_display = ("name", "code", "iso3", "continent")
    list_filter = ("continent",)
    search_fields = ("name", "code", "iso3")
    ordering = ("name",)


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    """Admin for the city list; reference data, curated by ``load_places``."""

    list_display = ("name", "country", "population")
    list_filter = ("country",)
    search_fields = ("name", "ascii_name")
    autocomplete_fields = ("country",)
    ordering = ("country", "name")
