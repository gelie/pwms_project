"""Workflow models: registry, state machine, RBAC, events and instruments.

This module used to be a single 3 000-line ``models/workflows.py``. It is now a
package split by responsibility; every public name is re-exported here so
``from pwms.models.workflows import Bill`` and
``from pwms.models import Bill`` keep working unchanged.
"""

from .access import (
    WorkflowGroupAccess,
    WorkflowRolePermission,
    WorkflowStatePermission,
)
from .base import (
    DUE_SOON_DAYS,
    OVERDUE_IDENTIFIER,
    AbstractLegislativeWorkflow,
)
from .events import EventType, TransitionLog, WorkflowEvent
from .instruments import (
    Bill,
    BillVersion,
    DelegationParticipant,
    DelegationReport,
    DelegationReportUpdate,
    InternationalAgreement,
    InternationalResolution,
)
from .machine import (
    TRANSITION_CONDITION_HANDLERS,
    State,
    Transition,
    TransitionCondition,
    transition_condition,
)
from .referrals import WorkflowNote, WorkflowReferral
from .relationships import WorkflowRelationship
from .types import PublicStatus, WorkflowType

__all__ = [
    "DUE_SOON_DAYS",
    "OVERDUE_IDENTIFIER",
    "TRANSITION_CONDITION_HANDLERS",
    "AbstractLegislativeWorkflow",
    "Bill",
    "BillVersion",
    "DelegationParticipant",
    "DelegationReport",
    "DelegationReportUpdate",
    "EventType",
    "InternationalAgreement",
    "InternationalResolution",
    "PublicStatus",
    "State",
    "Transition",
    "TransitionCondition",
    "TransitionLog",
    "WorkflowEvent",
    "WorkflowGroupAccess",
    "WorkflowNote",
    "WorkflowReferral",
    "WorkflowRelationship",
    "WorkflowRolePermission",
    "WorkflowStatePermission",
    "WorkflowType",
    "transition_condition",
]
