"""Admin registrations for alerts, alert rules and report shares."""

from django.contrib import admin

from ..models import (
    Notification,
    NotificationPreference,
    NotificationRule,
    ReportShare,
)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Read-only view of every in-app alert and outbound email.

    These rows *are* the delivery log — who was told, on which channel, and
    whether the mail went out — so the useful operations are reading and
    filtering. Every field is read-only and rows cannot be deleted: editing or
    erasing one would falsify the record it exists to keep.
    """

    list_display = (
        "created_at",
        "recipient",
        "to_email",
        "kind",
        "channel",
        "status",
        "why",
        "subject",
    )
    list_filter = ("channel", "kind", "status")
    search_fields = (
        "subject",
        "body",
        "to_email",
        "from_email",
        "recipient__username",
        "recipient__email",
    )
    raw_id_fields = ("recipient", "actor")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    @admin.display(description="Why")
    def why(self, obj):
        return ", ".join(obj.reasons)

    def get_readonly_fields(self, request, obj=None):
        """Alerts are evidence, not data entry: nothing here is editable."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        # Raised by workflow activity, never typed in.
        return False

    def has_delete_permission(self, request, obj=None):
        # The log is evidence. Deleting a row would erase who was told what.
        return False


@admin.register(NotificationRule)
class NotificationRuleAdmin(admin.ModelAdmin):
    """Every alert audience rule, across workflow types, in one filterable list."""

    list_display = (
        "workflow_type",
        "event",
        "transition",
        "scope",
        "role",
        "channel",
        "is_active",
    )
    list_filter = ("workflow_type", "event", "scope", "channel", "is_active")
    search_fields = ("workflow_type__name", "role__name", "transition__name")
    autocomplete_fields = ("workflow_type", "transition", "role")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    """Users' own mute switches; people normally set these from the Alerts page."""

    list_display = ("user", "kind", "in_app_enabled", "email_enabled")
    list_filter = ("kind", "in_app_enabled", "email_enabled")
    search_fields = ("user__username", "user__email")
    raw_id_fields = ("user",)


@admin.register(ReportShare)
class ReportShareAdmin(admin.ModelAdmin):
    """Read-only view of shared report links, plus a revoke action.

    A share is a standing grant of access to one report's figures, so the row
    records what was handed out and to whom. Nothing is editable or deletable —
    falsifying an expiry or a recipient list would misrepresent that record —
    but a link can be cut off, which is the one write an administrator actually
    needs.
    """

    list_display = (
        "created_at",
        "title",
        "created_by",
        "active",
        "access_count",
        "expires_at",
    )
    list_filter = ("created_at", "expires_at", "revoked_at")
    search_fields = ("title", "recipients", "created_by__username")
    raw_id_fields = ("created_by",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    actions = ("revoke_shares",)

    @admin.display(boolean=True, description="Active")
    def active(self, obj):
        return obj.is_active

    def get_readonly_fields(self, request, obj=None):
        """A share is a record of what was handed out: nothing is editable."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        # Minted from the reports page, never typed in.
        return False

    def has_delete_permission(self, request, obj=None):
        # A share records what was handed out; revoke it, do not erase it.
        return False

    @admin.action(description="Revoke selected report links")
    def revoke_shares(self, request, queryset):
        revoked = 0
        for share in queryset.filter(revoked_at__isnull=True):
            share.revoke()
            revoked += 1
        self.message_user(request, f"Revoked {revoked} report link(s).")
