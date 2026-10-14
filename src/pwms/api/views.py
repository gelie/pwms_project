from uuid import UUID

from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import Http404
from django.utils.dateparse import parse_date
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
)
from rest_framework import filters, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from ..models import (
    Bill,
    DelegationReport,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Meeting,
    MeetingAttendance,
    Role,
    State,
    Transition,
    WorkflowType,
)
from ..services.permissions import visible_instances
from ..utils.audit_helpers import (
    get_audit_trail_by_public_id,
    get_audit_trail_for_instance,
)
from .authentication import issue_token
from .permissions import ResourcePermission, WorkflowViewPermission
from .serializers import (
    AuditLogEntrySerializer,
    AuthTokenRequestSerializer,
    AuthTokenResponseSerializer,
    BillSerializer,
    DelegationReportSerializer,
    GroupSerializer,
    GroupWithMembersSerializer,
    InternationalAgreementSerializer,
    InternationalResolutionSerializer,
    MeetingAttendanceAddSerializer,
    MeetingAttendanceRecordSerializer,
    MeetingAttendanceSerializer,
    MeetingSerializer,
    MembershipSerializer,
    RoleSerializer,
    StateSerializer,
    TransitionLogSerializer,
    TransitionSerializer,
    UserDetailSerializer,
    UserMembershipSerializer,
    UserSerializer,
    WorkflowEventSerializer,
    WorkflowTransitionRequestSerializer,
    WorkflowTypeSerializer,
)
from .throttles import TokenEndpointThrottle

User = get_user_model()


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
    resources = {
        label: reverse(f"pwms_api:{basename}-list", request=request)
        for label, basename in (
            ("users", "user"),
            ("groups", "group"),
            ("roles", "role"),
            ("memberships", "membership"),
            ("workflow-types", "workflow-type"),
            ("states", "state"),
            ("transitions", "transition"),
            ("delegation-reports", "delegation-report"),
            ("resolutions", "resolution"),
            ("agreements", "agreement"),
            ("bills", "bill"),
            ("meetings", "meeting"),
        )
    }
    return Response(
        {
            "api": "PWMS workflow API",
            "authentication": {
                "obtain_token": reverse("pwms_api:obtain-token", request=request),
                "refresh_token": reverse("pwms_api:refresh-token", request=request),
                "revoke_token": reverse("pwms_api:revoke-token", request=request),
            },
            "resources": resources,
            "lookups": {
                "group-by-name": reverse("pwms_api:group-lookup", request=request),
            },
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


# ===========================================================================
# Authentication — token issue / rotate / revoke
# ===========================================================================


def _token_payload(user, token):
    """The shared body of the token responses (the key plus a little whoami)."""
    return {
        "token": token.key,
        "user_id": user.pk,
        "username": user.get_username(),
        "email": user.email or "",
    }


@extend_schema(
    summary="Obtain an API token",
    description=(
        "Exchange a username and password for a token. Send it on later requests "
        "as ``Authorization: Token <token>``. Credentials are checked by the same "
        "backends as the web login (Active Directory, then the local database)."
    ),
    request=AuthTokenRequestSerializer,
    responses={
        200: AuthTokenResponseSerializer,
        401: OpenApiTypes.OBJECT,
        429: OpenApiTypes.OBJECT,
    },
    auth=[],
)
class ObtainTokenView(APIView):
    """Issue (or re-issue) the calling user's API token."""

    authentication_classes = []
    permission_classes = [AllowAny]
    # Anonymous + IP-scoped, so credential stuffing is rate-limited (see
    # pwms.api.throttles for the rate and its per-worker caveat).
    throttle_classes = [TokenEndpointThrottle]

    def post(self, request):
        serializer = AuthTokenRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None:
            return Response(
                {"detail": "Invalid username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not user.is_active:
            return Response(
                {"detail": "This account is inactive."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        token = issue_token(user)
        return Response(_token_payload(user, token))


@extend_schema(
    summary="Rotate the caller's API token",
    description="Delete the current token and return a fresh one.",
    request=None,
    responses={200: AuthTokenResponseSerializer},
)
class RefreshTokenView(APIView):
    """Replace the caller's token with a new one (the old key stops working)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        token = Token.objects.create(user=request.user)
        return Response(_token_payload(request.user, token))


@extend_schema(
    summary="Revoke the caller's API token",
    description="Delete the caller's token so the key can no longer be used.",
    request=None,
    responses={204: None},
)
class RevokeTokenView(APIView):
    """Delete the caller's token. Returns ``204`` whether or not one existed."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Identity, structure and the workflow registry
# ===========================================================================


class ResourceViewSet(viewsets.ModelViewSet):
    """
    Base viewset for the identity / structure / registry resources.

    Lookups are by ``public_id`` so sequential database keys never appear in a
    URL. Read is open to any signed-in user; create/update/delete are gated by
    :class:`~pwms.api.permissions.ResourcePermission` (global manage capability
    or superuser). Concrete workflow subclasses override both behaviours.
    """

    permission_classes = [ResourcePermission]
    lookup_field = "public_id"
    lookup_url_kwarg = "public_id"
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]


class UserViewSet(ResourceViewSet):
    """PWMS users, plus ``/api/users/me/`` for the caller's own record."""

    #: Users are addressed by ``username`` (``/api/users/gelie/``) rather than
    #: the public UUID: a caller almost always holds the ERP username, and a
    #: direct lookup saves the search-then-fetch round trip the UUID forces.
    #: ``[^/]+`` (not DRF's ``[^/.]+``) so a dotted ``firstname.lastname``
    #: username stays addressable. ``me`` is a list action and is matched first.
    lookup_field = "username"
    lookup_url_kwarg = "username"
    lookup_value_regex = "[^/]+"

    queryset = User.objects.select_related("department", "supervisor").all()
    serializer_class = UserSerializer
    search_fields = ["username", "first_name", "last_name", "email", "positiondesc"]
    ordering_fields = ["last_name", "first_name", "username", "date_joined"]

    def get_object(self):
        """Look a user up by ``username`` case-insensitively.

        The ERP sync stores usernames lower-cased, but callers type them as they
        know them (mixed case from AD, an email, a name), so the match ignores
        case. An exact match still wins if a case variant exists as its own row.
        """
        queryset = self.filter_queryset(self.get_queryset())
        username = self.kwargs.get(self.lookup_url_kwarg or self.lookup_field)
        obj = (
            queryset.filter(username=username).first()
            or queryset.filter(username__iexact=username).first()
        )
        if obj is None:
            raise Http404
        self.check_object_permissions(self.request, obj)
        return obj

    def get_serializer_class(self):
        # Detail and /me carry the reporting line (nested supervisor + immediate
        # subordinates); the list stays flat so a page of users stays cheap.
        if self.action in {"retrieve", "me"}:
            return UserDetailSerializer
        return UserSerializer

    @extend_schema(summary="The authenticated user")
    @action(detail=False, methods=["get"])
    def me(self, request):
        """Return the record of the user making the request."""
        return Response(self.get_serializer(request.user).data)

    @extend_schema(
        summary="A user's group memberships",
        description=(
            "Every group membership held by the user (looked up by username), "
            "newest first. Filter by whether it is current (`active`), whether it "
            "was in force on a date (`as_of`), its start/end (`start_after`, "
            "`start_before`, `end_after`, `end_before`) and by what it grants "
            "(`group`, `role`, `group_type`)."
        ),
        parameters=[
            OpenApiParameter(
                "active",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Only current (true) or ended (false) memberships.",
            ),
            OpenApiParameter(
                "as_of",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Memberships in force on this date (YYYY-MM-DD).",
            ),
            OpenApiParameter(
                "start_after",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Memberships that started on or after this date.",
            ),
            OpenApiParameter(
                "start_before",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Memberships that started on or before this date.",
            ),
            OpenApiParameter(
                "end_after",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Memberships that ended on or after this date.",
            ),
            OpenApiParameter(
                "end_before",
                OpenApiTypes.DATE,
                OpenApiParameter.QUERY,
                description="Memberships that ended on or before this date.",
            ),
            OpenApiParameter(
                "group",
                OpenApiTypes.UUID,
                OpenApiParameter.QUERY,
                description="Only memberships of this group (its public_id).",
            ),
            OpenApiParameter(
                "role",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Only memberships in this role (its slug).",
            ),
            OpenApiParameter(
                "group_type",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description="Only memberships whose group is of this type.",
            ),
            OpenApiParameter(
                "ordering",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description=(
                    "start_date (default, descending), end_date, is_active, "
                    "group__name, role__name."
                ),
            ),
        ],
        responses=UserMembershipSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def memberships(self, request, username=None):
        user = self.get_object()
        queryset = GroupMembership.objects.filter(user=user).select_related(
            "group", "role"
        )
        queryset = self._filter_memberships(queryset, request.query_params)
        queryset = queryset.order_by(*self._membership_ordering(request.query_params))

        page = self.paginate_queryset(queryset)
        serializer = UserMembershipSerializer(
            page if page is not None else queryset, many=True
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @staticmethod
    def _membership_date(params, name):
        """Parse an ISO-date query parameter, or raise a 400."""
        raw = params.get(name)
        if not raw:
            return None
        value = parse_date(raw)
        if value is None:
            raise ValidationError({name: "Expected an ISO date (YYYY-MM-DD)."})
        return value

    @classmethod
    def _membership_ordering(cls, params):
        """Resolve the ``ordering`` query parameter to a safe ``order_by`` tuple."""
        allowed = {
            "start_date",
            "end_date",
            "is_active",
            "created_at",
            "group__name",
            "role__name",
        }
        raw = params.get("ordering")
        if not raw:
            return ("-start_date", "-id")
        fields = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lstrip("-") not in allowed:
                raise ValidationError(
                    {"ordering": f"Order by one of: {', '.join(sorted(allowed))}."}
                )
            fields.append(token)
        return tuple(fields) or ("-start_date", "-id")

    def _filter_memberships(self, queryset, params):
        """Apply the supported membership filters from the query string."""
        raw_active = (params.get("active") or "").strip().lower()
        if raw_active:
            if raw_active in {"1", "true", "yes", "on"}:
                queryset = queryset.filter(is_active=True)
            elif raw_active in {"0", "false", "no", "off"}:
                queryset = queryset.filter(is_active=False)
            else:
                raise ValidationError({"active": "Use true or false."})

        as_of = self._membership_date(params, "as_of")
        if as_of is not None:
            queryset = queryset.filter(start_date__lte=as_of).filter(
                Q(end_date__isnull=True) | Q(end_date__gte=as_of)
            )

        for name, lookup in (
            ("start_after", "start_date__gte"),
            ("start_before", "start_date__lte"),
            ("end_after", "end_date__gte"),
            ("end_before", "end_date__lte"),
        ):
            value = self._membership_date(params, name)
            if value is not None:
                queryset = queryset.filter(**{lookup: value})

        group = (params.get("group") or "").strip()
        if group:
            queryset = queryset.filter(group__public_id=_parse_uuid("group", group))
        role = (params.get("role") or "").strip()
        if role:
            queryset = queryset.filter(role__slug=role)
        group_type = (params.get("group_type") or "").strip()
        if group_type:
            queryset = queryset.filter(group__group_type=group_type)

        return queryset


def _query_bool(value) -> bool:
    """Interpret a query-string flag (``?flag=true``) leniently."""
    return str(value).lower() in {"1", "true", "yes", "on"}


def _parse_uuid(name, value):
    """Validate a UUID query parameter, or raise a 400 field error."""
    try:
        return UUID(value)
    except (TypeError, ValueError):
        raise ValidationError({name: "Expected a UUID."})


class GroupViewSet(ResourceViewSet):
    """Parliamentary and administrative groups (the MPTT hierarchy)."""

    queryset = Group.objects.select_related("parent").all()
    serializer_class = GroupSerializer
    search_fields = ["name", "short_name", "description"]
    ordering_fields = ["name", "group_type", "created_at"]

    @extend_schema(
        summary="Group details and members by (partial) name",
        description=(
            "Find groups whose **name or short name** contains `name` "
            "(case-insensitive) and return each match with its member roster. "
            "`exact=true` requires the whole name to match (still "
            "case-insensitive); `include_inactive=true` also lists memberships "
            "that have ended. Members are ordered by surname, given name."
        ),
        parameters=[
            OpenApiParameter(
                "name",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=True,
                description="Full or partial group name (or short name).",
            ),
            OpenApiParameter(
                "exact",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Match the whole name only, not a substring.",
            ),
            OpenApiParameter(
                "include_inactive",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                description="Include memberships that have ended.",
            ),
        ],
        responses=GroupWithMembersSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def lookup(self, request):
        name = (request.query_params.get("name") or "").strip()
        if not name:
            raise ValidationError({"name": "Provide a group name or part of one."})

        if _query_bool(request.query_params.get("exact")):
            match = Q(name__iexact=name) | Q(short_name__iexact=name)
        else:
            match = Q(name__icontains=name) | Q(short_name__icontains=name)

        groups = self.filter_queryset(self.get_queryset()).filter(match)
        serializer = GroupWithMembersSerializer(
            groups,
            many=True,
            context={
                "request": request,
                "include_inactive": _query_bool(
                    request.query_params.get("include_inactive")
                ),
            },
        )
        return Response(serializer.data)


class RoleViewSet(ResourceViewSet):
    """Roles and their workflow capability flags."""

    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    search_fields = ["name", "description"]
    ordering_fields = ["name"]


class MembershipViewSet(ResourceViewSet):
    """Group memberships: user <> group <> role, over time."""

    queryset = GroupMembership.objects.select_related("user", "group", "role").all()
    serializer_class = MembershipSerializer
    search_fields = ["user__username", "user__last_name", "group__name", "role__name"]
    ordering_fields = ["start_date", "end_date", "created_at"]

    def perform_create(self, serializer):
        # Record who made the change on the row itself, mirroring the web form.
        serializer.save(updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)


class WorkflowTypeViewSet(ResourceViewSet):
    """The workflow-type registry — reusable legislative state machines."""

    queryset = WorkflowType.objects.select_related("group", "parent_type").all()
    serializer_class = WorkflowTypeSerializer
    search_fields = ["name", "description"]
    ordering_fields = ["name", "created_at"]


class StateViewSet(ResourceViewSet):
    """The states making up each workflow type's machine."""

    queryset = State.objects.select_related("workflow_type").all()
    serializer_class = StateSerializer
    search_fields = ["name", "slug", "description"]
    ordering_fields = ["workflow_type", "order", "name"]


class TransitionViewSet(ResourceViewSet):
    """The transitions allowed between a workflow type's states."""

    queryset = Transition.objects.select_related(
        "workflow_type", "from_state", "to_state"
    ).all()
    serializer_class = TransitionSerializer
    search_fields = ["name", "slug"]
    ordering_fields = ["workflow_type", "order", "name"]


# ===========================================================================
# Workflow instances
# ===========================================================================


class WorkflowInstanceViewSet(ResourceViewSet):
    """
    Base viewset for a concrete workflow model.

    Adds the actions that make an instance useful over the API — the transitions
    available from its current state, performing one, the domain event log and
    the auditlog CRUD history — and scopes list results to what the caller may
    view, so the index can never surface a row the detail endpoint would reject.
    """

    permission_classes = [ResourcePermission]
    #: Creation is gated by ``WorkflowType.can_create`` in ``perform_create``,
    #: not by the manage capability the other resources require.
    create_uses_workflow_rules = True

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        visible = visible_instances(request.user, queryset)
        page = self.paginate_queryset(visible)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(visible, many=True)
        return Response(serializer.data)

    def perform_create(self, serializer):
        workflow_type = serializer.validated_data.get("workflow_type")
        user = self.request.user
        if workflow_type is None or not workflow_type.can_create(user):
            raise PermissionDenied(
                "You do not have permission to create this workflow."
            )
        initial = workflow_type.get_initial_state()
        if initial is None:
            raise ValidationError(
                {
                    "workflow_type": (
                        "This workflow type has no initial state; it cannot hold "
                        "instances yet."
                    )
                }
            )
        serializer.save(owner=user, current_state=initial)

    @extend_schema(summary="States reachable from the current state")
    @action(detail=True, methods=["get"])
    def transitions(self, request, public_id=None):
        instance = self.get_object()
        serializer = TransitionSerializer(
            instance.get_available_transitions(), many=True
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Perform a state transition",
        request=WorkflowTransitionRequestSerializer,
        responses={201: TransitionLogSerializer},
    )
    @action(detail=True, methods=["post"])
    def transition(self, request, public_id=None):
        instance = self.get_object()
        payload = WorkflowTransitionRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            transition = instance.workflow_type.transitions.get(
                public_id=payload.validated_data["transition"]
            )
        except Transition.DoesNotExist:
            raise ValidationError(
                {"transition": "No such transition for this workflow type."}
            )
        try:
            log = instance.perform_transition(
                transition,
                actor=request.user,
                comment=payload.validated_data.get("comment", ""),
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except DjangoValidationError as error:
            # perform_transition raises Django's ValidationError for a missing
            # comment, an unmet event guard or a wrong from-state; surface those
            # as a 400 with the same messages rather than a 500.
            raise ValidationError(error.messages)
        return Response(
            TransitionLogSerializer(log).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(summary="Domain event log")
    @action(detail=True, methods=["get"])
    def events(self, request, public_id=None):
        instance = self.get_object()
        serializer = WorkflowEventSerializer(instance.events(), many=True)
        return Response(serializer.data)

    @extend_schema(summary="Auditlog CRUD history")
    @action(detail=True, methods=["get"])
    def audit(self, request, public_id=None):
        instance = self.get_object()
        serializer = AuditLogEntrySerializer(
            get_audit_trail_for_instance(instance), many=True
        )
        return Response(serializer.data)


class DelegationReportViewSet(WorkflowInstanceViewSet):
    """Delegation reports (BR02/BR03)."""

    queryset = DelegationReport.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).all()
    serializer_class = DelegationReportSerializer
    search_fields = ["title", "engagement_name", "reference_number"]
    ordering_fields = ["created_at", "updated_at", "deadline", "priority"]


class InternationalResolutionViewSet(WorkflowInstanceViewSet):
    """International resolutions adopted at an engagement."""

    queryset = InternationalResolution.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).all()
    serializer_class = InternationalResolutionSerializer
    search_fields = ["title", "resolution_number", "resolution_text"]
    ordering_fields = ["created_at", "updated_at", "deadline", "priority"]


class InternationalAgreementViewSet(WorkflowInstanceViewSet):
    """International agreements tabled in Parliament."""

    queryset = InternationalAgreement.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).all()
    serializer_class = InternationalAgreementSerializer
    search_fields = ["title", "reference_number", "submitting_department"]
    ordering_fields = ["created_at", "updated_at", "deadline", "priority"]


class BillViewSet(WorkflowInstanceViewSet):
    """Bills tracked through their legislative lifecycle."""

    queryset = Bill.objects.select_related(
        "workflow_type", "current_state", "owner", "assigned_to"
    ).all()
    serializer_class = BillSerializer
    search_fields = ["title", "short_title", "bill_number"]
    ordering_fields = ["created_at", "updated_at", "deadline", "priority"]


# ===========================================================================
# Meetings (the Events workflow)
# ===========================================================================


class MeetingViewSet(WorkflowInstanceViewSet):
    """Parliamentary meetings, with their attendance register and minutes.

    Beyond the shared workflow-instance behaviour (list scoped to what the caller
    may view, ``transitions`` / ``transition`` / ``events`` / ``audit``), this
    exposes the meeting detail the calendar and the register need:

    * ``GET /api/meetings/`` — filter by ``meeting_type``, ``mine=1`` (the caller),
      ``member=<username>``, ``start`` / ``end`` (dates) and free-text ``search``;
    * ``GET /api/meetings/mine/`` — the caller's meetings *by membership*
      (attendance register, convener group, chair/secretary, owner/assignee);
    * ``GET /api/meetings/calendar/`` — a date-window feed for a calendar UI;
    * ``GET /api/meetings/{public_id}/`` — the detail, attendance included;
    * ``GET|POST /api/meetings/{public_id}/attendance/`` — read the register, or
      add a person; ``.../attendance/record/`` records RSVP/attendance;
    * ``POST /api/meetings/{public_id}/minutes/`` — advance the minutes status.
    """

    queryset = Meeting.objects.select_related(
        "workflow_type",
        "current_state",
        "owner",
        "assigned_to",
        "host_group",
        "chaired_by",
        "secretary",
        "venue",
        "venue__building",
    ).all()
    serializer_class = MeetingSerializer
    search_fields = [
        "title",
        "reference_number",
        "venue__name",
        "venue__building__name",
        "off_site_venue",
        "off_site_address",
    ]
    ordering_fields = [
        "start_datetime",
        "created_at",
        "updated_at",
        "deadline",
        "priority",
    ]

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        meeting_type = params.get("meeting_type")
        if meeting_type:
            queryset = queryset.filter(meeting_type=meeting_type)
        start = params.get("start")
        if start:
            parsed = parse_date(start)
            if parsed is not None:
                queryset = queryset.filter(start_datetime__date__gte=parsed)
        end = params.get("end")
        if end:
            parsed = parse_date(end)
            if parsed is not None:
                queryset = queryset.filter(start_datetime__date__lte=parsed)
        member = params.get("member")
        if member:
            user = User.objects.filter(username__iexact=member).first()
            if user is not None:
                queryset = queryset.for_member(user)
        if params.get("mine") in ("1", "true", "yes"):
            queryset = queryset.for_member(self.request.user)
        return queryset

    def perform_create(self, serializer):
        super().perform_create(serializer)
        # The API mirrors the web create: the register is generated from the
        # hosting group and the meeting's audience is alerted.
        from ..notifications import notify_meeting_created

        meeting = serializer.instance
        if meeting.host_group_id is not None:
            meeting.regenerate_attendance(actor=self.request.user)
        notify_meeting_created(meeting, actor=self.request.user)

    def perform_update(self, serializer):
        previous_host_group_id = serializer.instance.host_group_id
        meeting = serializer.save()
        # Only a change of hosting group re-generates, so an unrelated edit does
        # not re-add a generated row that was deliberately removed.
        if meeting.host_group_id != previous_host_group_id:
            meeting.regenerate_attendance(actor=self.request.user)

    @extend_schema(
        summary="My meetings by membership",
        description=(
            "Meetings the caller is a party to by membership: on the attendance "
            "register, convener of the group, chair/secretary, owner or assignee. "
            "Still scoped to what the caller may view."
        ),
    )
    @action(detail=False, methods=["get"])
    def mine(self, request):
        queryset = self.filter_queryset(
            Meeting.objects.for_member(request.user).select_related(
                "workflow_type", "current_state", "owner", "host_group"
            )
        )
        visible = visible_instances(request.user, queryset)
        page = self.paginate_queryset(visible)
        if page is not None:
            return self.get_paginated_response(
                self.get_serializer(page, many=True).data
            )
        return Response(self.get_serializer(visible, many=True).data)

    @extend_schema(
        summary="Calendar feed",
        parameters=[
            OpenApiParameter("start", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("end", OpenApiTypes.DATE, OpenApiParameter.QUERY),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["get"])
    def calendar(self, request):
        """A date-window feed of meetings, permission-scoped, for a calendar UI."""
        queryset = self.get_queryset()
        visible = visible_instances(request.user, queryset)
        payload = [
            {
                "public_id": str(meeting.public_id),
                "title": meeting.title,
                "meeting_type": meeting.meeting_type,
                "start_datetime": meeting.start_datetime.isoformat()
                if meeting.start_datetime
                else None,
                "end_datetime": meeting.end_datetime.isoformat()
                if meeting.end_datetime
                else None,
                "all_day": meeting.all_day,
                "state": meeting.current_state.name if meeting.current_state_id else "",
                "calendar_state": meeting.calendar_state,
                "url": meeting.get_absolute_url(),
            }
            for meeting in visible
        ]
        return Response({"count": len(payload), "results": payload})

    @extend_schema(summary="Attendance register")
    @action(detail=True, methods=["get"])
    def attendance(self, request, public_id=None):
        meeting = self.get_object()
        serializer = MeetingAttendanceSerializer(
            meeting.attendances_active(), many=True
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Add an attendee",
        request=MeetingAttendanceAddSerializer,
        responses={201: MeetingAttendanceSerializer},
    )
    @action(detail=True, methods=["post"], url_path="attendance/add")
    def attendance_add(self, request, public_id=None):
        meeting = self.get_object()
        payload = MeetingAttendanceAddSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = None
        user_id = payload.validated_data.get("user")
        if user_id:
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if user is None:
                raise ValidationError({"user": "No such active user."})
            if meeting.attendances.filter(user=user).exists():
                raise ValidationError(
                    {"user": "That person is already on the register."}
                )
        attendance = MeetingAttendance.objects.create(
            meeting=meeting,
            user=user,
            guest_name=payload.validated_data.get("guest_name", ""),
            guest_email=payload.validated_data.get("guest_email", ""),
            capacity=payload.validated_data["capacity"],
            is_required=payload.validated_data["is_required"],
        )
        return Response(
            MeetingAttendanceSerializer(attendance).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Record RSVP / attendance",
        request=MeetingAttendanceRecordSerializer,
        responses={200: MeetingAttendanceSerializer},
    )
    @action(detail=True, methods=["post"], url_path="attendance/record")
    def attendance_record(self, request, public_id=None):
        meeting = self.get_object()
        payload = MeetingAttendanceRecordSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        attendance = meeting.attendances.filter(
            public_id=payload.validated_data["attendance"]
        ).first()
        if attendance is None:
            raise ValidationError({"attendance": "No such attendee on this meeting."})
        note = payload.validated_data.get("note", "")
        rsvp_status = payload.validated_data.get("rsvp_status")
        status_value = payload.validated_data.get("attendance_status")
        if rsvp_status:
            attendance.respond(rsvp_status, note=note or None)
        if status_value:
            attendance.record(
                status_value,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
                note=note or None,
            )
        return Response(MeetingAttendanceSerializer(attendance).data)

    @extend_schema(
        summary="Regenerate the attendance register",
        description=(
            "Rebuild the register from the hosting group's active members, "
            "keeping the invitees added by hand."
        ),
        request=None,
        responses={200: MeetingAttendanceSerializer(many=True)},
    )
    @action(detail=True, methods=["post"], url_path="attendance/regenerate")
    def attendance_regenerate(self, request, public_id=None):
        meeting = self.get_object()
        if meeting.host_group_id is None:
            raise ValidationError(
                {"host_group": "Set a hosting group before regenerating."}
            )
        meeting.regenerate_attendance(actor=request.user)
        serializer = MeetingAttendanceSerializer(
            meeting.attendances_active(), many=True
        )
        return Response(serializer.data)

    @extend_schema(
        summary="Advance the minutes status",
        request=OpenApiTypes.OBJECT,
        responses={200: MeetingSerializer},
    )
    @action(detail=True, methods=["post"])
    def minutes(self, request, public_id=None):
        meeting = self.get_object()
        value = (request.data.get("status") or "").strip()
        if value == "draft":
            meeting.minutes_status = "draft"
            meeting.save(update_fields=["minutes_status", "updated_at"])
        elif value == "filed":
            meeting.record_minutes_filed(actor=request.user)
        elif value == "approved":
            meeting.record_minutes_approved(
                actor=request.user, comment=request.data.get("comment", "")
            )
        else:
            raise ValidationError({"status": "Unknown minutes status."})
        return Response(self.get_serializer(meeting).data)
