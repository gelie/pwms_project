from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework.decorators import api_view
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from ..models import Bill, InternationalAgreement, InternationalResolution
from ..utils.audit_helpers import get_audit_trail_by_public_id
from .permissions import WorkflowViewPermission
from .serializers import AuditLogEntrySerializer


@extend_schema(
    description="Entry point listing the endpoints exposed by this API.",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
def api_root(request):
    """
    Entry point for the PWMS workflow audit API.

    Lists the available endpoints. Authentication matches the global defaults
    (session/basic); browse to the ``login`` link first when logged out.
    """
    # Typed <uuid:public_id> converters reject placeholder tokens during
    # reverse(), so the audit-history pattern is documented as an absolute URL
    # with an explicit {public_id} token (append after the base to keep the
    # braces readable instead of percent-encoded).
    audit_pattern = (
        request.build_absolute_uri("/").rstrip("/")
        + "/api/resolutions/{public_id}/audit/"
    )
    agreement_audit_pattern = (
        request.build_absolute_uri("/").rstrip("/")
        + "/api/agreements/{public_id}/audit/"
    )
    bill_audit_pattern = (
        request.build_absolute_uri("/").rstrip("/") + "/api/bills/{public_id}/audit/"
    )
    return Response(
        {
            "api": "PWMS workflow audit API",
            "endpoints": {
                "resolution-audit-history": {
                    "method": "GET",
                    "description": (
                        "auditlog CRUD trail for an InternationalResolution "
                        "(replace {public_id} with the instance's UUID)"
                    ),
                    "url_pattern": audit_pattern,
                },
                "agreement-audit-history": {
                    "method": "GET",
                    "description": (
                        "auditlog CRUD trail for an InternationalAgreement "
                        "(replace {public_id} with the instance's UUID)"
                    ),
                    "url_pattern": agreement_audit_pattern,
                },
                "bill-audit-history": {
                    "method": "GET",
                    "description": (
                        "auditlog CRUD trail for a Bill "
                        "(replace {public_id} with the instance's UUID)"
                    ),
                    "url_pattern": bill_audit_pattern,
                },
            },
            "documentation": {
                "openapi_schema": reverse("api-schema", request=request),
                "swagger_ui": reverse("api-docs", request=request),
                "redoc": reverse("api-redoc", request=request),
            },
            "login": reverse("rest_framework:login", request=request),
            "logout": reverse("rest_framework:logout", request=request),
        }
    )


@extend_schema(
    description=(
        "Auditlog CRUD history (create/update/delete) for a workflow instance, "
        "looked up by its public UUID."
    ),
    parameters=[
        OpenApiParameter("public_id", OpenApiTypes.UUID, OpenApiParameter.PATH)
    ],
    responses=AuditLogEntrySerializer(many=True),
)
class WorkflowAuditHistoryView(APIView):
    """
    Return the auditlog CRUD trail (create/update/delete) for a single workflow
    instance, looked up by its public UUID.

    Concrete subclasses set ``model_class``; the audit trail is read through the
    shared helper, which filters on the internal integer PK via ContentType.
    """

    permission_classes = [IsAuthenticated, WorkflowViewPermission]
    serializer_class = AuditLogEntrySerializer

    #: Concrete workflow model to audit (set on subclasses).
    model_class = None

    def get(self, request, public_id):
        logs = get_audit_trail_by_public_id(self.model_class, public_id)
        serializer = self.serializer_class(logs, many=True)
        return Response(serializer.data)


class ResolutionAuditHistoryView(WorkflowAuditHistoryView):
    """Audit trail for an :class:`InternationalResolution` by public_id."""

    model_class = InternationalResolution


class AgreementAuditHistoryView(WorkflowAuditHistoryView):
    """Audit trail for an :class:`InternationalAgreement` by public_id."""

    model_class = InternationalAgreement


class BillAuditHistoryView(WorkflowAuditHistoryView):
    """Audit trail for a :class:`Bill` by public_id."""

    model_class = Bill


# When Bill/Motion/Question are added, register them the same way:
#
# class BillAuditHistoryView(WorkflowAuditHistoryView):
#     model_class = Bill
