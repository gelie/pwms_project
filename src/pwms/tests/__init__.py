"""The main PWMS test suite, split by domain.

Formerly a single 5 400-line ``pwms/tests.py``; this package keeps the same
``pwms.tests`` label and re-exports every test class so the suite runs
unchanged.
"""

from .access import (
    AuditWorkflowAccessCommandTests,
    CreatableWorkflowMenuTests,
    SyncTypeGroupAccessCommandTests,
    ViewerGroupAccessTests,
)
from .base import make_state
from .commands import (
    BuildUatWorkbookCommandTests,
    CheckReferralDeadlinesCommandTests,
    GenerateGroupTreeCommandTests,
    ShowWorkflowHierarchyCommandTests,
)
from .dashboard import DashboardViewTests
from .directory import (
    DirectoryPaginationTests,
    GroupListFilterTests,
    PeopleDirectoryTests,
    PersonSearchTests,
    UserProfileTests,
)
from .documents import DocumentNameTests
from .instrument_models import (
    BillTests,
    BillVersionTests,
    DelegationReportTests,
    ImportBillVersionsCommandTests,
    InternationalAgreementTests,
)
from .membership import (
    CompareOrgHierarchyCommandTests,
    OrgHierarchyInferenceTests,
    _irp_people,
)
from .migrations import (
    OwnerGroupEditGrantMigrationTests,
    OwnerGroupTransitionGrantMigrationTests,
)
from .pages import (
    HomePageTests,
    InformationalPageTests,
)
from .permissions import (
    PermissionResolverTests,
    WorkflowPermissionInheritanceTests,
)
from .places import PlaceDataTests
from .search import (
    GlobalSearchHeaderTests,
    GlobalSearchTests,
)
from .views_crud import WorkflowCrudViewTests
from .views_forms import WorkflowFormViewTests
from .views_lists import WorkflowListViewTests
from .views_permissions import WorkflowPermissionViewTests
from .workflow_core import (
    SeededWorkflowDefinitionTests,
    WorkflowAuditingTests,
    WorkflowHierarchyTests,
)
from .workflow_events import (
    DelegationReportUpdateTests,
    ReferralTests,
    TransitionConditionTests,
    WorkflowEventTests,
)

__all__ = [
    "AuditWorkflowAccessCommandTests",
    "BillTests",
    "BillVersionTests",
    "BuildUatWorkbookCommandTests",
    "CheckReferralDeadlinesCommandTests",
    "CompareOrgHierarchyCommandTests",
    "CreatableWorkflowMenuTests",
    "DashboardViewTests",
    "DelegationReportTests",
    "DelegationReportUpdateTests",
    "DirectoryPaginationTests",
    "DocumentNameTests",
    "GenerateGroupTreeCommandTests",
    "GlobalSearchHeaderTests",
    "GlobalSearchTests",
    "GroupListFilterTests",
    "HomePageTests",
    "ImportBillVersionsCommandTests",
    "InformationalPageTests",
    "InternationalAgreementTests",
    "OrgHierarchyInferenceTests",
    "OwnerGroupEditGrantMigrationTests",
    "OwnerGroupTransitionGrantMigrationTests",
    "PeopleDirectoryTests",
    "PermissionResolverTests",
    "PersonSearchTests",
    "PlaceDataTests",
    "ReferralTests",
    "SeededWorkflowDefinitionTests",
    "ShowWorkflowHierarchyCommandTests",
    "SyncTypeGroupAccessCommandTests",
    "TransitionConditionTests",
    "UserProfileTests",
    "ViewerGroupAccessTests",
    "WorkflowAuditingTests",
    "WorkflowCrudViewTests",
    "WorkflowEventTests",
    "WorkflowFormViewTests",
    "WorkflowHierarchyTests",
    "WorkflowListViewTests",
    "WorkflowPermissionInheritanceTests",
    "WorkflowPermissionViewTests",
    "_irp_people",
    "make_state",
]
