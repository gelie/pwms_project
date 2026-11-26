"""Admin for the workflow engine, RBAC and audit/event tables."""

from typing import ClassVar

from django import forms
from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline
from django.db.models import Q
from django.forms.models import BaseInlineFormSet

from ..models import (
    EventType,
    NotificationRule,
    PublicStatus,
    Role,
    State,
    Transition,
    TransitionCondition,
    TransitionLog,
    WorkflowEvent,
    WorkflowGroupAccess,
    WorkflowNote,
    WorkflowReferral,
    WorkflowRelationship,
    WorkflowRolePermission,
    WorkflowStatePermission,
    WorkflowType,
)
from ..models.notifications import EVENT_TRANSITION


class StateInline(admin.TabularInline):
    """States belonging to a ``WorkflowType``, editable on the type page."""

    model = State
    extra = 0
    fields = (
        "name",
        "public_status",
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
    and then add the transition). Allowed roles and the alert rules that name a
    transition are edited from the :class:`TransitionAdmin` change page, not
    inline.
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


class NotificationRuleInline(admin.TabularInline):
    """Who is alerted about a type's records: one row per audience declaration.

    These add to the built-in audience (named people, whoever acts next, officer
    oversight). Pick the ``scope`` deliberately: ``custodian`` is the owning group
    only, ``referred_group`` the committee a referral went to, and
    ``instance_groups`` every group with access to the record.
    """

    model = NotificationRule
    extra = 0
    fields = ("event", "transition", "scope", "role", "channel", "is_active")
    autocomplete_fields = ("role",)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Only this type's transitions are valid choices on its rules.
        if db_field.name == "transition":
            type_id = request.resolver_match.kwargs.get("object_id")
            if type_id:
                kwargs["queryset"] = Transition.objects.filter(workflow_type_id=type_id)
            else:
                kwargs["queryset"] = Transition.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class TransitionRuleFormSet(BaseInlineFormSet):
    """A rule added on a transition's page is, by definition, a rule about it."""

    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        form.instance.workflow_type_id = self.instance.workflow_type_id
        form.instance.event = EVENT_TRANSITION
        return form


class TransitionNotificationRuleInline(admin.TabularInline):
    """Rules that name this transition, on the transition's own page."""

    model = NotificationRule
    formset = TransitionRuleFormSet
    extra = 0
    fields = ("scope", "role", "channel", "is_active")
    autocomplete_fields = ("role",)
    verbose_name = "alert rule"
    verbose_name_plural = "alert rules (who is told when this transition happens)"


@admin.register(WorkflowType)
class WorkflowTypeAdmin(admin.ModelAdmin):
    """Admin for reusable workflow state machines (states + transitions inline)."""

    form = WorkflowTypeAdminForm
    list_display = ("name", "group", "enabled", "oversight_channel", "created_at")
    list_filter = ("enabled", "oversight_channel")
    search_fields = ("name", "group__name")
    autocomplete_fields = ("group",)
    filter_horizontal = ("create_roles", "viewer_groups")
    ordering = ("name",)
    inlines: ClassVar[list[type[admin.TabularInline]]] = [
        StateInline,
        TransitionInline,
        NotificationRuleInline,
    ]


@admin.register(State)
class StateAdmin(admin.ModelAdmin):
    """Admin for workflow states (per workflow type)."""

    list_display = (
        "workflow_type",
        "name",
        "public_status",
        "order",
        "is_initial",
        "is_terminal",
        "color",
    )
    list_filter = ("workflow_type", "public_status", "is_initial", "is_terminal")
    search_fields = ("name", "workflow_type__name")
    autocomplete_fields = ("workflow_type", "public_status")
    ordering = ("workflow_type", "order", "name")


@admin.register(PublicStatus)
class PublicStatusAdmin(admin.ModelAdmin):
    """Admin for the shared public-status vocabulary (BRS §12)."""

    list_display = ("name", "slug", "order", "color")
    search_fields = ("name", "slug", "description")
    ordering = ("order", "name")


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
    filter_horizontal = ("allowed_roles", "required_event_types")
    ordering = ("workflow_type", "order", "name")
    inlines: ClassVar[list[type[admin.TabularInline]]] = [
        TransitionConditionInline,
        TransitionNotificationRuleInline,
    ]


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
        "via_referral",
        "granted_at",
    )
    list_filter = (
        "group",
        "can_view",
        "can_edit",
        "can_transition",
        "is_primary",
        "via_referral",
    )
    # Provenance is written by the referral lifecycle (and read back to decide what
    # may be withdrawn), so it is shown but not editable: a hand-edited flag would
    # make a referral close with the wrong grant.
    readonly_fields = ("via_referral", "referral_raised_edit")
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


# --- Notes -------------------------------------------------------------------


@admin.register(WorkflowNote)
class WorkflowNoteAdmin(admin.ModelAdmin):
    """Admin for the notes recorded against workflow instances."""

    list_display = ("id", "workflow_target", "author", "created_at")
    list_filter = ("content_type", "author")
    search_fields = ("body", "author__username")
    autocomplete_fields = ("author",)
    ordering = ("-created_at",)

    @admin.display(description="Workflow instance")
    def workflow_target(self, obj):
        if obj.content_object is not None:
            return str(obj.content_object)
        return f"#{obj.object_id}"


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
