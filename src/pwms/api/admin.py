"""Admin registration for external API clients and their usage logs.

This is where a PWMS administrator registers a partner application, chooses its
scopes and rate limit, and — once, on creation or rotation — copies the client
secret into the partner's onboarding channel. The secret is hashed in the
database and can never be read back from here.
"""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from .models import ApiClient, ApiRefreshToken, ApiUsageLog
from .scopes import Scope


class ApiClientAdminForm(forms.ModelForm):
    scopes = forms.MultipleChoiceField(
        choices=Scope.choices,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="Each endpoint checks the scope it needs; grant only what "
        "the integration uses.",
    )

    class Meta:
        model = ApiClient
        fields = "__all__"

    def clean_scopes(self):
        return list(self.cleaned_data.get("scopes") or [])


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    form = ApiClientAdminForm
    list_display = (
        "name",
        "contact_email",
        "scope_summary",
        "rate_limit_per_minute",
        "secret_status",
        "is_active",
        "last_used_at",
    )
    list_filter = ("is_active",)
    search_fields = ("name", "client_id", "contact_email", "contact_name")
    readonly_fields = (
        "client_id",
        "secret_rotated_at",
        "last_used_at",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            None,
            {"fields": ("name", "description", "contact_name", "contact_email")},
        ),
        (
            "Credentials",
            {
                "fields": ("client_id", "secret_rotated_at", "last_used_at"),
                "description": (
                    "The client id is public. The secret is shown once when the "
                    "client is created or its secret is rotated; only a hash is "
                    "stored. Use the “Rotate client secret” action to issue a "
                    "new one for an existing integration."
                ),
            },
        ),
        (
            "Access",
            {"fields": ("scopes", "rate_limit_per_minute", "is_active")},
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    actions = ("rotate_secret",)

    @admin.display(description="Scopes")
    def scope_summary(self, obj):
        return ", ".join(obj.enabled_scopes) or "—"

    @admin.display(description="Secret")
    def secret_status(self, obj):
        return "configured" if obj.has_secret else "not set"

    def save_model(self, request, obj, form, change):
        raw_secret = None
        if not change:
            raw_secret = obj.set_client_secret()
        super().save_model(request, obj, form, change)
        if raw_secret:
            self.message_user(
                request,
                f"Client secret for “{obj.name}” (shown once): {raw_secret}",
                level=messages.WARNING,
            )

    @admin.action(description="Rotate client secret (shown once)")
    def rotate_secret(self, request, queryset):
        for client in queryset:
            raw_secret = client.rotate_secret()
            self.message_user(
                request,
                f"New client secret for “{client.name}” (shown once): {raw_secret}",
                level=messages.WARNING,
            )


@admin.register(ApiRefreshToken)
class ApiRefreshTokenAdmin(admin.ModelAdmin):
    list_display = ("client", "expires_at", "revoked_at", "created_at")
    list_filter = ("client",)
    search_fields = ("client__name", "client__contact_email")
    date_hierarchy = "created_at"
    readonly_fields = (
        "client",
        "token_hash",
        "expires_at",
        "revoked_at",
        "created_at",
        "updated_at",
        "public_id",
    )
    actions = ("revoke_tokens",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.action(description="Revoke selected refresh tokens")
    def revoke_tokens(self, request, queryset):
        updated = queryset.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        self.message_user(request, f"Revoked {updated} refresh token(s).")


@admin.register(ApiUsageLog)
class ApiUsageLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "client",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "was_throttled",
        "remote_addr",
    )
    list_filter = ("was_throttled", "method", "status_code")
    search_fields = ("path", "endpoint", "client__name", "remote_addr")
    date_hierarchy = "created_at"
    readonly_fields = (
        "client",
        "method",
        "path",
        "endpoint",
        "status_code",
        "duration_ms",
        "remote_addr",
        "user_agent",
        "was_throttled",
        "created_at",
        "public_id",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
