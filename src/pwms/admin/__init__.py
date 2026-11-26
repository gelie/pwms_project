"""Django admin registrations.

This was a single ``pwms/admin.py``; Django's admin autodiscovery imports this
package, whose submodules register every model. The names are re-exported so
tests and existing imports (``from pwms.admin import NotificationAdmin``) keep
working.
"""

from django.contrib import admin

from .attachments import AttachmentAdmin, AttachmentInline, AttachmentVersionInline
from .geography import BuildingAdmin, CityAdmin, CountryAdmin, VenueAdmin
from .instruments import (
    BillAdmin,
    BillUpdateLogAdmin,
    BillVersionAdmin,
    BillVersionInline,
    DelegationParticipantAdmin,
    DelegationParticipantInline,
    DelegationReportAdmin,
    DelegationReportUpdateAdmin,
    DelegationReportUpdateInline,
    InternationalAgreementAdmin,
    InternationalResolutionAdmin,
)
from .meetings import MeetingAdmin, MeetingAttendanceAdmin, MeetingAttendanceInline
from .notifications import (
    NotificationAdmin,
    NotificationPreferenceAdmin,
    NotificationRuleAdmin,
    ReportShareAdmin,
)
from .sharepoint import (
    SharepointDriveAdmin,
    SharepointFolderAdmin,
    SharepointSiteAdmin,
    SharepointSiteMemberAdmin,
    SharepointSiteMemberInline,
    SharepointTokenAdmin,
)
from .users import (
    DutyDelegationAdmin,
    GroupAdmin,
    GroupMembershipAdmin,
    RoleAdmin,
    UserAdmin,
)
from .workflows import (
    EventTypeAdmin,
    NotificationRuleInline,
    PublicStatusAdmin,
    StateAdmin,
    StateInline,
    TransitionAdmin,
    TransitionConditionAdmin,
    TransitionConditionInline,
    TransitionInline,
    TransitionLogAdmin,
    TransitionNotificationRuleInline,
    TransitionRuleFormSet,
    WorkflowEventAdmin,
    WorkflowEventInline,
    WorkflowGroupAccessAdmin,
    WorkflowGroupAccessInline,
    WorkflowNoteAdmin,
    WorkflowReferralAdmin,
    WorkflowReferralInline,
    WorkflowRelationshipAdmin,
    WorkflowRolePermissionAdmin,
    WorkflowStatePermissionAdmin,
    WorkflowTypeAdmin,
    WorkflowTypeAdminForm,
)

admin.site.site_header = "PWMS Admin"
admin.site.site_title = "PWMS Admin Portal"
admin.site.index_title = "Welcome to the PWMS Admin Portal"

__all__ = [
    "AttachmentAdmin",
    "AttachmentInline",
    "AttachmentVersionInline",
    "BillAdmin",
    "BillUpdateLogAdmin",
    "BillVersionAdmin",
    "BillVersionInline",
    "BuildingAdmin",
    "CityAdmin",
    "CountryAdmin",
    "DelegationParticipantAdmin",
    "DelegationParticipantInline",
    "DelegationReportAdmin",
    "DelegationReportUpdateAdmin",
    "DelegationReportUpdateInline",
    "DutyDelegationAdmin",
    "EventTypeAdmin",
    "GroupAdmin",
    "GroupMembershipAdmin",
    "InternationalAgreementAdmin",
    "InternationalResolutionAdmin",
    "MeetingAdmin",
    "MeetingAttendanceAdmin",
    "MeetingAttendanceInline",
    "NotificationAdmin",
    "NotificationPreferenceAdmin",
    "NotificationRuleAdmin",
    "NotificationRuleInline",
    "PublicStatusAdmin",
    "ReportShareAdmin",
    "RoleAdmin",
    "SharepointDriveAdmin",
    "SharepointFolderAdmin",
    "SharepointSiteAdmin",
    "SharepointSiteMemberAdmin",
    "SharepointSiteMemberInline",
    "SharepointTokenAdmin",
    "StateAdmin",
    "StateInline",
    "TransitionAdmin",
    "TransitionConditionAdmin",
    "TransitionConditionInline",
    "TransitionInline",
    "TransitionLogAdmin",
    "TransitionNotificationRuleInline",
    "TransitionRuleFormSet",
    "UserAdmin",
    "VenueAdmin",
    "WorkflowEventAdmin",
    "WorkflowEventInline",
    "WorkflowGroupAccessAdmin",
    "WorkflowGroupAccessInline",
    "WorkflowNoteAdmin",
    "WorkflowReferralAdmin",
    "WorkflowReferralInline",
    "WorkflowRelationshipAdmin",
    "WorkflowRolePermissionAdmin",
    "WorkflowStatePermissionAdmin",
    "WorkflowTypeAdmin",
    "WorkflowTypeAdminForm",
]
