"""Admin registrations for the mirrored SharePoint tables."""

from django.contrib import admin

from ..models import (
    SharepointDrive,
    SharepointFolder,
    SharepointSite,
    SharepointSiteMember,
    SharepointToken,
)


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
        "enabled",
        "is_personal_site",
        "last_synced_at",
    )
    # ``enabled`` is curated straight from the changelist, so a SharePoint admin
    # can hide libraries from the attachment picker without opening each site.
    # ``site_id`` is dropped from the list (long, and not meant for scanning) but
    # stays searchable below and still appears on the change form.
    list_editable = ("enabled",)
    list_filter = ("enabled", "is_personal_site")
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
