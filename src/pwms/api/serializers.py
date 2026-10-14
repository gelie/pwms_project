from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from ..models import (
    Bill,
    DelegationReport,
    Group,
    GroupMembership,
    InternationalAgreement,
    InternationalResolution,
    Role,
    State,
    Transition,
    TransitionLog,
    WorkflowEvent,
    WorkflowType,
)

User = get_user_model()


class AuditLogEntrySerializer(serializers.ModelSerializer):
    """Serialize an ``auditlog.LogEntry`` for the workflow audit API."""

    actor_email = serializers.SerializerMethodField()
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    changes = serializers.JSONField(
        source="changes_dict"
    )  # before/after field diffs (dict)

    class Meta:
        model = LogEntry
        fields = [
            "id",
            "action",  # 0: Create, 1: Update, 2: Delete
            "action_display",  # human-readable label
            "actor_email",
            "timestamp",
            "changes",
            "remote_addr",  # IP captured by AuditlogMiddleware
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str | None:
        """Actor email, or ``None`` when the change was made anonymously."""
        return getattr(obj.actor, "email", None) if obj.actor_id else None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class AuthTokenRequestSerializer(serializers.Serializer):
    """Credentials exchanged for an API token at ``POST /api/auth/token/``."""

    username = serializers.CharField()
    password = serializers.CharField(
        style={"input_type": "password"}, trim_whitespace=False
    )


class AuthTokenResponseSerializer(serializers.Serializer):
    """The token issued to an authenticated caller, plus a little whoami."""

    token = serializers.CharField(read_only=True)
    user_id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True, allow_blank=True)


# ---------------------------------------------------------------------------
# Identity, structure and the workflow registry
# ---------------------------------------------------------------------------


class UserSummarySerializer(serializers.ModelSerializer):
    """A compact, read-only user view for embedding in rosters and pickers."""

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = [
            "public_id",
            "username",
            "title",
            "first_name",
            "middle_name",
            "last_name",
            "full_name",
            "display_name",
            "email",
            "employee_type",
            "positiondesc",
            "department",
            "is_mp",
            "is_staff_member",
            "is_active",
            "constituency",
            "party_affiliation",
            "phone",
        ]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    """A PWMS user. Password is write-only; identity numbers never leave the DB."""

    full_name = serializers.CharField(source="get_full_name", read_only=True)
    display_name = serializers.CharField(read_only=True)
    password = serializers.CharField(
        write_only=True,
        required=False,
        style={"input_type": "password"},
        help_text="Optional plaintext password; stored hashed, never returned.",
    )

    class Meta:
        model = User
        fields = [
            "id",
            "public_id",
            "username",
            "title",
            "first_name",
            "middle_name",
            "last_name",
            "full_name",
            "display_name",
            "email",
            "employee_type",
            "positiondesc",
            "department",
            "supervisor",
            "is_active",
            "is_mp",
            "is_staff_member",
            "is_staff",
            "is_superuser",
            "phone",
            "gender",
            "date_of_birth",
            "constituency",
            "party_affiliation",
            "date_joined_parliament",
            "termination_date",
            "bio",
            "avatar",
            "date_joined",
            "password",
        ]
        # Privilege flags and system timestamps are never writable through the
        # API: exposing them would let any manager escalate an account.
        read_only_fields = [
            "public_id",
            "full_name",
            "display_name",
            "is_staff",
            "is_superuser",
            "date_joined",
        ]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save(update_fields=["password"])
        return user


class UserDetailSerializer(UserSerializer):
    """User detail: the manager (``supervisor``) and immediate reports.

    On top of :class:`UserSerializer`, ``supervisor`` is nested (a summary, not a
    bare row id) and ``subordinates`` lists the people who report **directly** to
    this user. The list endpoint keeps the flat :class:`UserSerializer`, so paging
    through users never pays for the extra queries.
    """

    supervisor = UserSummarySerializer(read_only=True)
    subordinates = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = UserSerializer.Meta.fields + ["subordinates"]

    @extend_schema_field(UserSummarySerializer(many=True))
    def get_subordinates(self, obj):
        reports = User.objects.filter(supervisor=obj).order_by(
            "last_name", "first_name", "username"
        )
        return UserSummarySerializer(reports, many=True).data


class GroupSerializer(serializers.ModelSerializer):
    """A parliamentary or administrative group (MPTT tree node)."""

    full_path = serializers.CharField(source="get_full_path", read_only=True)

    class Meta:
        model = Group
        fields = [
            "id",
            "public_id",
            "name",
            "slug",
            "short_name",
            "group_type",
            "parent",
            "full_path",
            "description",
            "start_date",
            "end_date",
            "is_active",
            "contact_email",
            "contact_phone",
            "location",
        ]
        read_only_fields = ["public_id", "slug", "full_path"]


class RoleSerializer(serializers.ModelSerializer):
    """A role carrying non-state-dependent workflow capabilities."""

    workflow_permissions = serializers.DictField(
        source="get_workflow_permissions", read_only=True
    )

    class Meta:
        model = Role
        fields = [
            "id",
            "public_id",
            "name",
            "slug",
            "description",
            "can_transition_workflows",
            "can_create_workflows",
            "can_assign_workflows",
            "can_manage_permissions",
            "workflow_permissions",
        ]
        read_only_fields = ["public_id", "slug", "workflow_permissions"]


class MembershipSerializer(serializers.ModelSerializer):
    """Links a user to a group in a role, over a period of time."""

    user_display = serializers.CharField(source="user.display_name", read_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True)
    role_name = serializers.CharField(source="role.name", read_only=True)

    class Meta:
        model = GroupMembership
        fields = [
            "id",
            "public_id",
            "user",
            "user_display",
            "group",
            "group_name",
            "role",
            "role_name",
            "start_date",
            "end_date",
            "is_active",
            "notes",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "public_id",
            "user_display",
            "group_name",
            "role_name",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class UserMembershipSerializer(serializers.ModelSerializer):
    """One of a user's memberships, with the group and role spelled out.

    Used by ``GET /api/users/{username}/memberships/``: the user is the path, so
    it is not repeated here, and the group is identified by its ``public_id``
    (like every other resource) rather than a database key.
    """

    group_public_id = serializers.UUIDField(source="group.public_id", read_only=True)
    group_name = serializers.CharField(source="group.name", read_only=True)
    group_type = serializers.CharField(source="group.group_type", read_only=True)
    role = serializers.CharField(source="role.name", read_only=True)
    role_slug = serializers.CharField(source="role.slug", read_only=True)

    class Meta:
        model = GroupMembership
        fields = [
            "public_id",
            "group_public_id",
            "group_name",
            "group_type",
            "role",
            "role_slug",
            "start_date",
            "end_date",
            "is_active",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class GroupMemberSerializer(serializers.ModelSerializer):
    """One member of a group: the user, their role, and the appointment window."""

    user = UserSummarySerializer(read_only=True)
    role = serializers.CharField(source="role.name", read_only=True)
    role_slug = serializers.CharField(source="role.slug", read_only=True)

    class Meta:
        model = GroupMembership
        fields = [
            "public_id",
            "user",
            "role",
            "role_slug",
            "start_date",
            "end_date",
            "is_active",
            "notes",
        ]
        read_only_fields = fields


class GroupWithMembersSerializer(GroupSerializer):
    """Group details plus its member roster (used by the ``lookup`` action)."""

    members = serializers.SerializerMethodField()

    class Meta(GroupSerializer.Meta):
        fields = GroupSerializer.Meta.fields + ["members"]

    @extend_schema_field(GroupMemberSerializer(many=True))
    def get_members(self, obj):
        memberships = obj.members.select_related("user", "role").order_by(
            "user__last_name", "user__first_name", "user__username"
        )
        if not self.context.get("include_inactive", False):
            memberships = memberships.filter(is_active=True)
        return GroupMemberSerializer(memberships, many=True).data


class WorkflowTypeSerializer(serializers.ModelSerializer):
    """A reusable legislative state machine (states + transitions)."""

    is_root_type = serializers.BooleanField(read_only=True)

    class Meta:
        model = WorkflowType
        fields = [
            "id",
            "public_id",
            "name",
            "slug",
            "description",
            "enabled",
            "group",
            "create_roles",
            "viewer_groups",
            "parent_type",
            "is_root_type",
            "owner_can_edit",
            "owner_can_transition",
            "owner_can_delete",
            "owner_can_share",
            "owner_can_comment",
            "owner_can_manage",
        ]
        read_only_fields = ["public_id", "slug", "is_root_type"]


class StateSerializer(serializers.ModelSerializer):
    """One state of a workflow type's state machine."""

    public_name_display = serializers.CharField(
        source="get_public_name_display", read_only=True
    )

    class Meta:
        model = State
        fields = [
            "id",
            "public_id",
            "workflow_type",
            "name",
            "public_name",
            "public_name_display",
            "slug",
            "description",
            "is_initial",
            "is_terminal",
            "allows_referrals",
            "order",
            "color",
        ]
        read_only_fields = ["public_id", "slug", "public_name_display"]


class TransitionSerializer(serializers.ModelSerializer):
    """An allowed move between two states of a workflow type."""

    class Meta:
        model = Transition
        fields = [
            "id",
            "public_id",
            "workflow_type",
            "name",
            "slug",
            "from_state",
            "to_state",
            "allowed_roles",
            "notify_roles",
            "required_event_types",
            "requires_comment",
            "order",
        ]
        read_only_fields = ["public_id", "slug"]


# ---------------------------------------------------------------------------
# Workflow instances
# ---------------------------------------------------------------------------


class WorkflowInstanceSerializer(serializers.ModelSerializer):
    """Common shape of every concrete workflow instance.

    Abstract: subclasses set ``Meta.model`` and extend ``Meta.fields``. ``owner``
    and ``current_state`` are read-only: the creating user is pinned by the
    viewset, and an instance always starts in its type's initial state — moving
    it on is what the ``transition`` action is for, so the change is audited.
    """

    identifier = serializers.CharField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    detail_url = serializers.CharField(source="get_absolute_url", read_only=True)
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    current_state = serializers.PrimaryKeyRelatedField(read_only=True)

    def validate(self, attrs):
        # The type owns the states, so it cannot be swapped after creation: doing
        # so would either strand the current state or silently restart the
        # machine, and neither belongs in an audit trail.
        if self.instance is not None and attrs.get("workflow_type") not in (
            None,
            self.instance.workflow_type,
        ):
            raise serializers.ValidationError(
                {"workflow_type": "The workflow type cannot be changed."}
            )
        return attrs

    class Meta:
        fields = [
            "id",
            "public_id",
            "workflow_type",
            "title",
            "description",
            "current_state",
            "owner",
            "assigned_to",
            "deadline",
            "priority",
            "identifier",
            "is_overdue",
            "detail_url",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "public_id",
            "owner",
            "identifier",
            "is_overdue",
            "detail_url",
            "created_at",
            "updated_at",
        ]


class DelegationReportSerializer(WorkflowInstanceSerializer):
    # Read-only "latest BR03 update" roll-ups; declared so the schema knows their
    # types instead of falling back to string for these model properties.
    atc_reference = serializers.CharField(read_only=True)
    atc_publication_date = serializers.DateField(read_only=True, allow_null=True)
    atc_page_number = serializers.CharField(read_only=True)
    atc_document_url = serializers.CharField(read_only=True)

    class Meta(WorkflowInstanceSerializer.Meta):
        model = DelegationReport
        fields = WorkflowInstanceSerializer.Meta.fields + [
            "reference_number",
            "engagement_name",
            "engagement_start_date",
            "engagement_end_date",
            "location_city",
            "location_country",
            "notes",
            "report_document_url",
            "atc_reference",
            "atc_publication_date",
            "atc_page_number",
            "atc_document_url",
        ]
        read_only_fields = WorkflowInstanceSerializer.Meta.read_only_fields + [
            "reference_number",
            "atc_reference",
            "atc_publication_date",
            "atc_page_number",
            "atc_document_url",
        ]


class InternationalResolutionSerializer(WorkflowInstanceSerializer):
    class Meta(WorkflowInstanceSerializer.Meta):
        model = InternationalResolution
        fields = WorkflowInstanceSerializer.Meta.fields + [
            "resolution_number",
            "resolution_text",
            "adoption_date",
            "responsible_group",
            "implementation_progress",
        ]


class InternationalAgreementSerializer(WorkflowInstanceSerializer):
    class Meta(WorkflowInstanceSerializer.Meta):
        model = InternationalAgreement
        fields = WorkflowInstanceSerializer.Meta.fields + [
            "reference_number",
            "agreement_type",
            "submitting_department",
            "responsible_minister",
            "responsible_minister_name",
            "atc_tabling_date",
            "atc_reference",
            "referral_committees",
            "notes",
            "agreement_document_url",
            "explanatory_memorandum_url",
        ]
        read_only_fields = WorkflowInstanceSerializer.Meta.read_only_fields + [
            "reference_number",
        ]


class BillSerializer(WorkflowInstanceSerializer):
    public_status = serializers.CharField(read_only=True)
    current_version = serializers.CharField(read_only=True)

    class Meta(WorkflowInstanceSerializer.Meta):
        model = Bill
        fields = WorkflowInstanceSerializer.Meta.fields + [
            "bill_number",
            "short_title",
            "bill_type",
            "house_of_origin",
            "sponsor",
            "sponsor_name",
            "introduced_date",
            "responsible_committee",
            "atc_reference",
            "order_paper_reference",
            "bill_document_url",
            "notes",
            "public_status",
            "current_version",
        ]
        read_only_fields = WorkflowInstanceSerializer.Meta.read_only_fields + [
            "public_status",
            "current_version",
        ]


# ---------------------------------------------------------------------------
# Domain history and the transition action
# ---------------------------------------------------------------------------


class TransitionLogSerializer(serializers.ModelSerializer):
    """A state change recorded by ``perform_transition()``."""

    from_state_name = serializers.SerializerMethodField()
    to_state_name = serializers.SerializerMethodField()
    actor_email = serializers.SerializerMethodField()

    class Meta:
        model = TransitionLog
        fields = [
            "id",
            "action",
            "from_state",
            "from_state_name",
            "to_state",
            "to_state_name",
            "actor",
            "actor_email",
            "ip_address",
            "timestamp",
            "notes",
        ]
        read_only_fields = fields

    def get_from_state_name(self, obj) -> str | None:
        return obj.from_state.name if obj.from_state_id else None

    def get_to_state_name(self, obj) -> str | None:
        return obj.to_state.name if obj.to_state_id else None

    def get_actor_email(self, obj) -> str | None:
        return getattr(obj.actor, "email", None) if obj.actor_id else None


class WorkflowEventSerializer(serializers.ModelSerializer):
    """An append-only domain event on a workflow instance."""

    event_type_name = serializers.CharField(source="event_type.name", read_only=True)
    actor_email = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowEvent
        fields = [
            "id",
            "event_type",
            "event_type_name",
            "occurred_at",
            "actor",
            "actor_email",
            "origin",
            "payload",
            "document_url",
            "notes",
            "transition_log",
        ]
        read_only_fields = fields

    def get_actor_email(self, obj) -> str | None:
        return getattr(obj.actor, "email", None) if obj.actor_id else None


class WorkflowTransitionRequestSerializer(serializers.Serializer):
    """Body of the ``transition`` action: which transition, and the comment."""

    transition = serializers.UUIDField(
        help_text="public_id of the Transition to perform."
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Reason for the move; required when the transition demands one.",
    )
