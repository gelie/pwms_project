"""Admin registrations for meetings and their attendance register."""

from typing import ClassVar

from django.contrib import admin

from ..models import Meeting, MeetingAttendance
from .attachments import AttachmentInline
from .workflows import (
    WorkflowEventInline,
    WorkflowGroupAccessInline,
    WorkflowReferralInline,
)


class MeetingAttendanceInline(admin.TabularInline):
    """The attendance register, edited on the meeting page."""

    model = MeetingAttendance
    extra = 0
    fields = (
        "user",
        "guest_name",
        "capacity",
        "is_required",
        "rsvp_status",
        "attendance_status",
        "recorded_by",
        "recorded_at",
        "note",
    )
    autocomplete_fields = ("user", "recorded_by")
    ordering = ("capacity", "guest_name")


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    """Admin for parliamentary meeting instances (the Events workflow)."""

    list_display = (
        "title",
        "reference_number",
        "meeting_type",
        "start_datetime",
        "current_state",
        "host_group",
        "where",
        "attendance_mode",
        "owner",
        "minutes_status",
    )
    list_filter = (
        "workflow_type",
        "current_state",
        "meeting_type",
        "minutes_status",
        "attendance_mode",
        "all_day",
    )
    search_fields = (
        "title",
        "reference_number",
        "venue__name",
        "venue__building__name",
        "off_site_venue",
        "off_site_address",
    )
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
        "venue",
    )
    date_hierarchy = "start_datetime"
    ordering = ("-start_datetime",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        MeetingAttendanceInline,
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
        AttachmentInline,
    ]

    @admin.display(description="Where")
    def where(self, obj):
        """The on-premises room, or the off-site venue, or a dash."""
        return obj.venue_display or "—"


@admin.register(MeetingAttendance)
class MeetingAttendanceAdmin(admin.ModelAdmin):
    """Admin for meeting attendance rows (the auditable register)."""

    list_display = (
        "meeting",
        "display_name",
        "capacity",
        "is_required",
        "rsvp_status",
        "attendance_status",
        "recorded_by",
        "recorded_at",
    )
    list_filter = ("attendance_status", "rsvp_status", "capacity", "is_required")
    search_fields = (
        "guest_name",
        "user__username",
        "user__first_name",
        "user__last_name",
        "meeting__title",
    )
    autocomplete_fields = ("meeting", "user", "recorded_by")
    ordering = ("-meeting__start_datetime", "capacity")
