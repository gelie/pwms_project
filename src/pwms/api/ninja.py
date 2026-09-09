"""
django-ninja spike: prototype of the workflow-audit endpoints alongside DRF.

This mirrors ``pwms.api.views`` (audit history for a workflow instance) using
FastAPI-style typing + Pydantic. It is mounted at ``/ninja/`` purely to compare
the two approaches before deciding which API stack to standardise on. Remove
this module (and the ``/ninja/`` URL) if you keep DRF.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.http import Http404
from ninja import NinjaAPI
from ninja.errors import HttpError
from pydantic import BaseModel, ConfigDict

from ..models import InternationalResolution
from ..utils.audit_helpers import get_audit_trail_by_public_id


def session_auth(request):
    """Authenticate via the Django session (like DRF's SessionAuthentication)."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user
    return None


api = NinjaAPI(
    title="PWMS workflow audit API (ninja spike)",
    description=(
        "FastAPI-style prototype of the audit-history endpoint. "
        "Compare with the DRF version at /api/resolutions/{public_id}/audit/."
    ),
    version="1.0.0",
    auth=session_auth,
    urls_namespace="pwms_ninja",
)


class AuditEntrySchema(BaseModel):
    """Pydantic view of an ``auditlog.LogEntry`` (mirrors AuditLogEntrySerializer)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    action: int
    action_display: str
    actor_email: str | None = None
    timestamp: datetime
    changes: dict[str, Any]
    remote_addr: str | None = None


def _entry_to_schema(entry) -> AuditEntrySchema:
    return AuditEntrySchema(
        id=entry.pk,
        action=entry.action,
        action_display=entry.get_action_display(),
        actor_email=entry.actor.email if entry.actor_id else None,
        timestamp=entry.timestamp,
        changes=entry.changes_dict,
        remote_addr=entry.remote_addr,
    )


@api.get("/", summary="API root")
def ninja_root(request):
    """Index of the endpoints exposed by the ninja spike."""
    return {
        "api": "PWMS workflow audit API (ninja spike)",
        "endpoints": {
            "resolution-audit-history": {
                "method": "GET",
                "url_pattern": "/ninja/resolutions/{public_id}/audit/",
                "description": (
                    "auditlog CRUD trail for an InternationalResolution "
                    "(replace {public_id} with the instance's UUID)"
                ),
            }
        },
    }


@api.get(
    "/resolutions/{public_id}/audit/",
    response=list[AuditEntrySchema],
    summary="Audit history for an InternationalResolution",
)
def resolution_audit_history(request, public_id: UUID):
    """Return the auditlog CRUD trail for a resolution by its public UUID."""
    try:
        logs = get_audit_trail_by_public_id(InternationalResolution, public_id)
    except Http404:
        raise HttpError(404, "Resolution not found")
    return [_entry_to_schema(entry) for entry in logs]
