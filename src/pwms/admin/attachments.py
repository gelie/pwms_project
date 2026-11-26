"""Admin registrations for workflow document attachments."""

from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from ..models import Attachment, AttachmentVersion


class AttachmentInline(GenericTabularInline):
    """
    SharePoint documents attached to a workflow instance.

    Read-only: attachments are created from the record's page (the picker
    uploads/links through Graph), so the admin only reviews them. Deleting a row
    detaches the document; the file itself stays in SharePoint.
    """

    model = Attachment
    extra = 0
    fields = (
        "name",
        "type",
        "size",
        "sharepoint_folder_path",
        "uploaded_by",
        "created_at",
    )
    readonly_fields = fields
    ordering = ("-created_at",)
    verbose_name = "attachment"
    verbose_name_plural = "attachments"
    classes = ("collapse",)

    def has_add_permission(self, request, obj=None):
        return False


class AttachmentVersionInline(admin.TabularInline):
    """
    SharePoint version rows mirrored onto an attachment.

    Read-only: the rows mirror upstream history, refreshed from the record's page
    (or by a re-upload), and are never hand-edited here.
    """

    model = AttachmentVersion
    extra = 0
    can_delete = False
    fields = ("version_id", "size", "modified_at", "modified_by", "is_current")
    readonly_fields = fields
    ordering = ("-modified_at",)
    verbose_name = "version"
    verbose_name_plural = "versions"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """Admin for SharePoint document attachments."""

    list_display = (
        "name",
        "type",
        "size",
        "content_type",
        "object_id",
        "uploaded_by",
        "created_at",
    )
    list_filter = ("type", "content_type")
    search_fields = ("name", "item_id", "drive_id")
    autocomplete_fields = ("uploaded_by", "sharepoint_site", "sharepoint_drive")
    readonly_fields = ("public_id", "created_at", "updated_at")
    ordering = ("-created_at",)
    inlines = (AttachmentVersionInline,)
