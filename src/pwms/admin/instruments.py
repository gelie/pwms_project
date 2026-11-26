"""Admin registrations for the concrete legislative instruments."""

from typing import ClassVar

from django.contrib import admin

from ..forms import DelegationParticipantInlineFormSet
from ..models import (
    Bill,
    BillUpdateLog,
    BillVersion,
    DelegationParticipant,
    DelegationReport,
    DelegationReportUpdate,
    InternationalAgreement,
    InternationalResolution,
)
from .attachments import AttachmentInline
from .workflows import (
    WorkflowEventInline,
    WorkflowGroupAccessInline,
    WorkflowReferralInline,
)


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
        AttachmentInline,
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
        "responsible_minister_name",
        "responsible_minister__first_name",
        "responsible_minister__last_name",
    )
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "responsible_minister",
    )
    readonly_fields = ("reference_number",)
    filter_horizontal = ("referral_committees",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
        AttachmentInline,
    ]

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue


class BillVersionInline(admin.TabularInline):
    """Preserved bill versions, edited on the bill page (BRS §15A)."""

    model = BillVersion
    extra = 0
    fields = (
        "version_label",
        "version_type",
        "version_date",
        "is_current",
        "document_url",
        "notes",
        "recorded_by",
    )
    autocomplete_fields = ("recorded_by",)
    ordering = ("-version_date",)
    verbose_name_plural = "Versions (preserved history)"


@admin.register(Bill)
class BillAdmin(admin.ModelAdmin):
    """Admin for bill workflow instances (Online Bill Tracking BRS)."""

    list_display = (
        "bill_number",
        "title",
        "bill_type",
        "house_of_origin",
        "current_state",
        "public_status_column",
        "responsible_committee",
        "owner",
        "deadline",
        "is_overdue",
    )
    list_filter = (
        "workflow_type",
        "current_state",
        "bill_type",
        "house_of_origin",
        "priority",
    )
    search_fields = (
        "bill_number",
        "title",
        "short_title",
        "sponsor_name",
        "sponsor__first_name",
        "sponsor__last_name",
    )
    autocomplete_fields = (
        "workflow_type",
        "current_state",
        "responsible_committee",
        "owner",
        "assigned_to",
        "sponsor",
    )
    ordering = ("bill_number",)
    inlines: ClassVar[list[type[admin.InlineModelAdmin]]] = [
        BillVersionInline,
        WorkflowEventInline,
        WorkflowReferralInline,
        WorkflowGroupAccessInline,
        AttachmentInline,
    ]

    @admin.display(description="Public status")
    def public_status_column(self, obj):
        """Simplified citizen-facing status the internal state maps to."""
        return obj.public_status

    @admin.display(boolean=True, description="Overdue")
    def is_overdue(self, obj):
        return obj.is_overdue


@admin.register(BillVersion)
class BillVersionAdmin(admin.ModelAdmin):
    """Admin for the preserved bill version history (BRS §15A)."""

    list_display = (
        "bill",
        "version_label",
        "version_type",
        "version_date",
        "is_current",
        "recorded_by",
    )
    list_filter = ("version_type", "is_current")
    search_fields = ("bill__bill_number", "bill__title", "version_label")
    autocomplete_fields = ("bill", "recorded_by")
    ordering = ("-version_date", "-id")


class DelegationParticipantInline(admin.TabularInline):
    """Delegation members / support officials edited on the report page."""

    model = DelegationParticipant
    extra = 0
    # Shares the report form's rule that one person appears at most once, so the
    # admin reports the clash instead of letting the unique index fail on save.
    formset = DelegationParticipantInlineFormSet
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
        AttachmentInline,
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
        "removed_at",
        "removed_by",
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


@admin.register(BillUpdateLog)
class BillUpdateLogAdmin(admin.ModelAdmin):
    """Read-only view of every Bill update posted, and every receipt.

    These rows are the audit trail for the webhook exchange — what was sent,
    when, and what HTTP status came back — so nothing is editable and rows
    cannot be deleted. The Bill FK is ``SET_NULL``, so a row outlives the bill
    and the snapshot fields keep it readable.
    """

    list_display = (
        "created_at",
        "event",
        "direction",
        "status",
        "bill_number",
        "http_status",
        "attempts",
        "reviewed_by",
    )
    list_filter = ("direction", "event", "status")
    search_fields = ("bill_number", "bill_public_id", "update_id", "target_url")
    raw_id_fields = ("bill", "actor", "reviewed_by")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def get_readonly_fields(self, request, obj=None):
        """The log is evidence, not data entry: nothing here is editable."""
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        # Written by the webhook exchange, never typed in.
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting a row would erase what was sent and whether it arrived.
        return False
