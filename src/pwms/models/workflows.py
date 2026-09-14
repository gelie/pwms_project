from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField

# Sentinel used to cache "no parent link" without re-querying the database.
_UNSET = object()

#: BR12 identifier inserted on reports whose due date has expired while open.
OVERDUE_IDENTIFIER = "Due date expired – pending follow up"

from .base import BaseModel
from .permissions import Role
from .users import User


class WorkflowType(BaseModel):
    """
    Registry of reusable legislative state machines.

    A ``WorkflowType`` owns the :class:`State`\\ s and :class:`Transition`\\ s that
    describe how workflow *instances* (subclasses of
    :class:`AbstractLegislativeWorkflow`, e.g. ``InternationalResolution``) move
    through the legislature. Concrete instances reference a type and store only
    their ``current_state``.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    description = models.TextField(blank=True)
    enabled = models.BooleanField(
        default=True,
        help_text="Uncheck to prevent creating new workflow instances of this type.",
    )

    # RBAC scope. A workflow type belongs to one Group, and creation is limited
    # to the roles listed in ``create_roles`` — which must themselves come from
    # that group (enforced by ``WorkflowTypeAdminForm``). ``null=True`` keeps the
    # column tolerant of rows seeded before their group exists; ``blank=False``
    # makes the field required in the admin.
    group = models.ForeignKey(
        "Group",
        on_delete=models.PROTECT,
        null=True,
        blank=False,
        related_name="workflow_types",
        help_text="Group whose members and roles govern this workflow type.",
    )
    create_roles = models.ManyToManyField(
        Role,
        blank=True,
        related_name="creatable_workflow_types",
        help_text=(
            "Roles from this type's group that may create workflow instances. "
            "Leave empty to let only superusers create instances."
        ),
    )

    viewer_groups = models.ManyToManyField(
        "Group",
        blank=True,
        related_name="viewer_workflow_types",
        help_text=(
            "Read-only stakeholder groups (an interest in the workflow but no "
            "active role). Every new instance of this type is shared with these "
            "groups at view level when it is created."
        ),
    )

    # Type-level hierarchy: lets the registry declare container relationships,
    # e.g. "International Resolution" nests under "Delegation Report".
    parent_type = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_types",
        help_text=(
            "Parent workflow type this type nests under (a Delegation Report "
            "report type is the parent of the International Resolution type)."
        ),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_initial_state(self):
        return self.states.filter(is_initial=True).first()

    # -- group-scoped RBAC --------------------------------------------------
    def group_roles(self):
        """Roles held by members of this type's group (``create_roles`` choices)."""
        if self.group_id is None:
            return Role.objects.none()
        return (
            Role.objects.filter(groupmembership__group_id=self.group_id)
            .distinct()
            .order_by("name")
        )

    def can_create(self, user):
        """
        True when ``user`` may create instances of this workflow type.

        Creation is granted by holding one of the type's ``create_roles`` in its
        ``group``. Superusers bypass the check; types with no group are closed to
        everyone but superusers.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if user.is_superuser:
            return True
        if self.group_id is None:
            return False
        return user.memberships.filter(
            is_active=True,
            group_id=self.group_id,
            role_id__in=self.create_roles.values_list("pk", flat=True),
        ).exists()

    @classmethod
    def creatable_by(cls, user):
        """Enabled workflow types ``user`` may create instances of."""
        if user is None or not getattr(user, "is_authenticated", False):
            return cls.objects.none()
        if user.is_superuser:
            return cls.objects.filter(enabled=True)
        allowed_ids = [
            workflow_type.pk
            for workflow_type in cls.objects.filter(enabled=True).prefetch_related(
                "create_roles"
            )
            if workflow_type.can_create(user)
        ]
        return cls.objects.filter(enabled=True, pk__in=allowed_ids)

    # -- type hierarchy helpers --------------------------------------------
    @property
    def is_root_type(self):
        return self.parent_type_id is None

    def get_ancestor_types(self):
        """Parent types ordered root-first (cycle-safe)."""
        chain = []
        seen = set()
        node = self.parent_type
        while node is not None and node.pk not in seen:
            seen.add(node.pk)
            chain.append(node)
            node = node.parent_type
        return list(reversed(chain))

    def get_descendant_types(self):
        """Every nested child type, parents before their own children."""
        collected = []
        seen = {self.pk}

        def walk(node):
            for child in node.child_types.all().order_by("name"):
                if child.pk in seen:
                    continue
                seen.add(child.pk)
                collected.append(child)
                walk(child)

        walk(self)
        return collected

    def allowed_child_types(self):
        """Directly declared child types (empty means "unrestricted")."""
        return self.child_types.all().order_by("name")


class AbstractLegislativeWorkflow(BaseModel):
    """
    Common schema shared by every concrete legislative workflow
    (``InternationalResolution`` today; ``Bill`` / ``Motion`` / ``Question`` later).

    The reusable machine (states + transitions) lives on ``workflow_type``; the
    instance only records which type it follows and its ``current_state``. RBAC
    is attached per instance through :class:`WorkflowGroupAccess` using a generic
    foreign key, so one set of access tables serves every concrete subclass.
    """

    workflow_type = models.ForeignKey(
        "WorkflowType",
        on_delete=models.PROTECT,
        related_name="%(class)s_instances",
        help_text="Reusable state machine (states/transitions) this instance follows.",
    )
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)

    current_state = models.ForeignKey(
        "State",
        on_delete=models.PROTECT,
        related_name="%(class)s_current",
        help_text="Current workflow state. Must belong to ``workflow_type``.",
    )

    # Clean reverse relations per concrete subclass (e.g. bill_owned, parliamentaryquestion_owned)
    owner = models.ForeignKey(
        "User",
        on_delete=models.PROTECT,
        related_name="%(class)s_owned",
    )
    assigned_to = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="%(class)s_assigned",
    )

    # Referrals are typed rows (WorkflowReferral) rather than an M2M, so they
    # can carry referred_by / due_date / status / response; see ``refer()``.

    deadline = models.DateTimeField(null=True, blank=True)
    priority = models.CharField(
        max_length=20,
        choices=[
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ],
        default="medium",
    )

    # -- lifecycle / RBAC helpers -------------------------------------------
    def save(self, *args, **kwargs):
        """
        Persist the instance, then materialise its type's group access.

        ``super().save()`` runs first so the row has a primary key, which the
        generic access rows need. Only the initial insert triggers
        materialisation; later saves are updates and are left alone, so
        per-instance access an administrator has since edited is never
        overwritten.
        """
        adding = self._state.adding
        super().save(*args, **kwargs)
        if adding:
            self.materialize_group_access()

    def materialize_group_access(self):
        """
        Create this instance's :class:`WorkflowGroupAccess` rows from its
        workflow type, and return the rows that were created.

        Two kinds are materialised:

        * the type's owning ``group``, flagged ``is_primary`` — the unit that
          governs the type;
        * each of the type's ``viewer_groups`` — read-only shared stakeholders
          (an interest in every instance but no active role in producing it).

        Both start read-only (``can_view`` only); raise individual capabilities
        per instance or through ``WorkflowRolePermission`` as needed. Runs on
        creation (see :meth:`save`) and is reused by the ``sync_type_group_access``
        backfill command. Idempotent: an existing row for a group is left as
        configured, so an administrator's edits are never overwritten.
        """
        if self.pk is None or self.workflow_type_id is None:
            return []
        content_type = self._instance_ct()
        workflow_type = self.workflow_type
        owner_group_id = workflow_type.group_id
        created = []

        if owner_group_id is not None:
            access, was_created = WorkflowGroupAccess.objects.get_or_create(
                content_type=content_type,
                object_id=self.pk,
                group_id=owner_group_id,
                defaults={"is_primary": True, "can_view": True},
            )
            if was_created:
                created.append(access)

        for group in workflow_type.viewer_groups.all():
            if group.pk == owner_group_id:
                # The owning group keeps its primary grant; don't downgrade it.
                continue
            access, was_created = WorkflowGroupAccess.objects.get_or_create(
                content_type=content_type,
                object_id=self.pk,
                group=group,
                defaults={"can_view": True},
            )
            if was_created:
                created.append(access)

        return created

    def clean(self):
        super().clean()
        if (
            self.workflow_type_id
            and self.current_state_id
            and self.current_state.workflow_type_id != self.workflow_type_id
        ):
            raise ValidationError(
                {
                    "current_state": (
                        f"State '{self.current_state.name}' does not belong to "
                        f"workflow type '{self.workflow_type.name}'."
                    )
                }
            )

    def _instance_ct(self):
        """ContentType of the concrete subclass row (never the abstract base)."""
        return ContentType.objects.get_for_model(self)

    def get_available_transitions(self):
        """Transitions that may be taken from the instance's current state."""
        return Transition.objects.filter(
            workflow_type=self.workflow_type, from_state=self.current_state
        ).order_by("order", "name")

    def perform_transition(
        self, transition, *, actor=None, comment="", ip_address=None
    ):
        """
        Validate and apply ``transition`` to this instance: advance
        ``current_state`` and append a ``TransitionLog`` audit entry.

        Validation covers the state machine (the transition must belong to the
        instance's type and start from the current state), the declared event
        guards (``Transition.required_event_types``) and a required comment.
        All unmet event guards are reported at once.
        """
        if transition.workflow_type_id != self.workflow_type_id:
            raise ValidationError(
                "Transition does not belong to this instance's workflow type."
            )
        if transition.from_state_id != self.current_state_id:
            raise ValidationError(
                f"Transition '{transition.name}' is not valid from current "
                f"state '{self.current_state.name}'."
            )
        unmet = self.unmet_transition_conditions(transition)
        if unmet:
            raise ValidationError({"transition": unmet})
        if transition.requires_comment and not comment:
            raise ValidationError("A comment is required for this transition.")

        from_state = self.current_state
        self.current_state = transition.to_state
        self.save(update_fields=["current_state", "updated_at"])
        return TransitionLog.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            action="STATE_TRANSITION",
            from_state=from_state,
            to_state=transition.to_state,
            actor=actor,
            ip_address=ip_address,
            notes=comment,
        )

    def group_accesses(self):
        """WorkflowGroupAccess rows granting groups access to this instance."""
        return WorkflowGroupAccess.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
        )

    def audit_logs(self):
        """TransitionLog entries for this instance, newest first."""
        return TransitionLog.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
        ).order_by("-timestamp")

    @property
    def assigned_to_email(self):
        """Email address of the assigned official (BR02.3.11)."""
        if not self.assigned_to_id:
            return ""
        return self.assigned_to.email or ""

    # -- domain events (append-only) ----------------------------------------
    # ``WorkflowEvent`` records business-meaningful facts ("ATC update
    # published", "report document attached"). Events are evidence, not state:
    # they are never updated, and transitions can require them as guards.
    def record_event(
        self,
        event_type,
        *,
        actor=None,
        payload=None,
        origin="user",
        document_url="",
        notes="",
        transition_log=None,
    ):
        """
        Append a :class:`WorkflowEvent` to this instance and return it.

        ``event_type`` is an :class:`EventType` row (resolve a slug with
        ``EventType.objects.get(slug=...)``). ``payload`` is stored as JSON for
        event-specific detail; corrections are new compensating events, never
        edits.
        """
        return WorkflowEvent.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            event_type=event_type,
            actor=actor,
            origin=origin,
            payload=payload or {},
            document_url=document_url,
            notes=notes,
            transition_log=transition_log,
        )

    def events(self):
        """WorkflowEvent rows for this instance, newest first."""
        return (
            WorkflowEvent.objects.filter(
                content_type=self._instance_ct(), object_id=self.pk
            )
            .select_related("event_type", "actor")
            .order_by("-occurred_at", "-id")
        )

    def unmet_transition_conditions(self, transition):
        """
        Reasons ``transition`` cannot be performed yet, as a list of messages.

        Evaluates both guard layers:

        1. declared event guards (``Transition.required_event_types``) — each
           required type needs at least one recorded event on this instance;
        2. :class:`TransitionCondition` rules (enabled ones) through the
           :data:`TRANSITION_CONDITION_HANDLERS` registry.
        """
        unmet = []
        required = transition.required_event_types.all()
        if required:
            recorded_ids = set(self.events().values_list("event_type_id", flat=True))
            unmet.extend(
                f"Requires a '{event_type.name}' event on this workflow."
                for event_type in required
                if event_type.pk not in recorded_ids
            )

        for condition in transition.conditions.filter(enabled=True):
            message = condition.evaluate(self)
            if message:
                unmet.append(message)
        return unmet

    # -- referrals (typed rows, see WorkflowReferral) ------------------------
    def referrals(self):
        """WorkflowReferral rows for this instance, newest first."""
        return WorkflowReferral.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
        )

    def open_referrals(self):
        """Referrals on this instance that are still awaiting a response."""
        return self.referrals().filter(status="open")

    def refer(self, group, *, referred_by=None, due_date=None, notes=""):
        """
        Refer this instance to ``group`` and return the new
        :class:`WorkflowReferral`.

        Gated by ``State.allows_referrals``; the referral's creation is recorded
        as a ``referral-created`` :class:`WorkflowEvent` by the model itself.
        """
        if self.pk is None:
            raise ValidationError("Save the workflow before referring it.")
        if self.current_state_id and not self.current_state.allows_referrals:
            raise ValidationError(
                f"Referrals are not allowed in state '{self.current_state.name}'."
            )
        return WorkflowReferral.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            referred_to=group,
            referred_by=referred_by,
            due_date=due_date,
            notes=notes,
        )

    # -- parent / child hierarchy ------------------------------------------
    # Concrete workflow instances are different tables, so the parent link is
    # stored in :class:`WorkflowRelationship` (a generic link table) rather
    # than a self-referential FK, which is impossible on an abstract model.
    # These helpers deliberately mirror the legacy ``parent_workflow`` /
    # ``sub_workflows`` vocabulary so tree code reads naturally.

    def _parent_link(self, *, refresh=False):
        """Relationship row where this instance is the child (cached)."""
        cached = self.__dict__.get("_parent_link_cache", _UNSET)
        if cached is _UNSET or refresh:
            cached = (
                WorkflowRelationship.objects.filter(
                    child_content_type=self._instance_ct(),
                    child_object_id=self.pk,
                )
                .select_related("parent_content_type")
                .first()
            )
            self.__dict__["_parent_link_cache"] = cached
        return cached

    def _child_links(self):
        """Relationship rows where this instance is the parent, in order."""
        return WorkflowRelationship.objects.filter(
            parent_content_type=self._instance_ct(),
            parent_object_id=self.pk,
        ).order_by("order", "id")

    @property
    def parent_relationship(self):
        """The :class:`WorkflowRelationship` linking to the parent, or ``None``."""
        return self._parent_link()

    @property
    def parent_workflow(self):
        """The parent workflow instance, or ``None`` for a root."""
        link = self._parent_link()
        return link.parent if link is not None else None

    @property
    def relationship_type(self):
        """How this instance is attached to its parent (``''`` when root)."""
        link = self._parent_link()
        return link.relationship_type if link is not None else ""

    @property
    def is_root_workflow(self):
        return self._parent_link() is None

    @property
    def sub_workflows(self):
        """Child instances of any concrete workflow type, in display order."""
        return [link.child for link in self._child_links()]

    @property
    def has_sub_workflows(self):
        return self._child_links().exists()

    @property
    def hierarchy_level(self):
        """Depth from the root (root = 0, its direct children = 1, ...)."""
        level = 0
        seen = {(self._instance_ct().pk, self.pk)}
        node = self.parent_workflow
        while node is not None:
            key = (node._instance_ct().pk, node.pk)
            if key in seen:
                break
            seen.add(key)
            level += 1
            node = node.parent_workflow
        return level

    def get_ancestors(self):
        """Ancestors ordered root-first (cycle-safe)."""
        chain = []
        seen = {(self._instance_ct().pk, self.pk)}
        node = self.parent_workflow
        while node is not None:
            key = (node._instance_ct().pk, node.pk)
            if key in seen:
                break
            seen.add(key)
            chain.append(node)
            node = node.parent_workflow
        return list(reversed(chain))

    def get_workflow_hierarchy_path(self):
        """``[root, ..., self]`` — the full path from the root to this row."""
        return [*self.get_ancestors(), self]

    def get_all_descendants(self):
        """All nested children, parents before their own children (cycle-safe)."""
        collected = []
        seen = {(self._instance_ct().pk, self.pk)}

        def walk(node):
            for link in node._child_links():
                child = link.child
                key = (child._instance_ct().pk, child.pk)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(child)
                walk(child)

        walk(self)
        return collected

    def set_parent_workflow(self, parent, *, relationship_type="contains", order=0):
        """
        Attach this instance under ``parent`` (replacing any existing parent).

        Pass ``parent=None`` to detach and make this instance a root. The link
        is validated *before* the old one is removed so an invalid request
        never destroys existing hierarchy.
        """
        if parent is None:
            existing = self._parent_link()
            if existing is not None:
                existing.delete()
            self.__dict__.pop("_parent_link_cache", None)
            return None

        link = WorkflowRelationship(
            parent_content_type=parent._instance_ct(),
            parent_object_id=parent.pk,
            child_content_type=self._instance_ct(),
            child_object_id=self.pk,
            relationship_type=relationship_type,
            order=order,
        )
        link.clean()  # validate (self-reference / type / cycle) before mutating
        existing = self._parent_link()
        if existing is not None:
            existing.delete()
        link.save()
        self.__dict__.pop("_parent_link_cache", None)
        return link

    def clear_parent_workflow(self):
        """Detach this instance from its parent."""
        return self.set_parent_workflow(None)

    def add_sub_workflow(self, child, *, relationship_type="contains", order=0):
        """Attach ``child`` under this instance (re-parenting it if needed)."""
        link = WorkflowRelationship.objects.filter(
            child_content_type=child._instance_ct(),
            child_object_id=child.pk,
        ).first()
        if link is None:
            link = WorkflowRelationship(
                parent_content_type=self._instance_ct(),
                parent_object_id=self.pk,
                child_content_type=child._instance_ct(),
                child_object_id=child.pk,
            )
        link.relationship_type = relationship_type
        link.order = order
        link.save()
        return link

    def remove_sub_workflow(self, child):
        """Detach ``child`` if it is currently a child of this instance."""
        deleted, _ = WorkflowRelationship.objects.filter(
            parent_content_type=self._instance_ct(),
            parent_object_id=self.pk,
            child_content_type=child._instance_ct(),
            child_object_id=child.pk,
        ).delete()
        return bool(deleted)

    PERMISSION_ACTIONS = {
        "view": "can_view",
        "edit": "can_edit",
        "delete": "can_delete",
        "share": "can_share",
        "comment": "can_comment",
        "manage": "can_manage",
        "transition": "can_transition",
    }

    def can(self, user, action):
        """
        Resolve effective permission for ``user`` across every group holding
        access on this instance.

        ``action`` is one of ``view`` / ``edit`` / ``delete`` / ``share`` /
        ``comment`` / ``manage`` / ``transition``. For each group the user
        actively belongs to, the most specific grant wins:

        1. :class:`WorkflowRolePermission` for the user's role in that group
           (authoritative for the role; if it lists ``allowed_states`` the
           override applies only in those states);
        2. :class:`WorkflowStatePermission` for ``(group_access, current_state)``;
        3. base flags stored on :class:`WorkflowGroupAccess`.
        """
        field = self.PERMISSION_ACTIONS.get(action.lower())
        if field is None:
            raise ValueError(f"Unknown action: {action!r}")

        memberships = user.memberships.filter(is_active=True).select_related(
            "group", "role"
        )
        accesses = list(
            self.group_accesses().prefetch_related(
                "role_permissions", "state_permissions"
            )
        )
        access_by_group = {access.group_id: access for access in accesses}

        for membership in memberships:
            access = access_by_group.get(membership.group_id)
            if access is None:
                continue

            # 1) Role override (authoritative when it applies in this state)
            role_perm = next(
                (
                    rp
                    for rp in access.role_permissions.all()
                    if rp.role_id == membership.role_id
                ),
                None,
            )
            if role_perm is not None:
                applies_here = not role_perm.allowed_states.exists() or (
                    role_perm.allowed_states.filter(pk=self.current_state_id).exists()
                )
                if applies_here:
                    if getattr(role_perm, field):
                        return True
                    continue  # explicitly denied for this role in this state

            # 2) State permission for (group_access, current_state)
            state_perm = next(
                (
                    sp
                    for sp in access.state_permissions.all()
                    if sp.state_id == self.current_state_id
                ),
                None,
            )
            if state_perm is not None and getattr(state_perm, field):
                return True

            # 3) Group-level default
            if getattr(access, field):
                return True

        return False

    def __str__(self):
        return self.title or f"{self._meta.verbose_name} #{self.pk}"

    class Meta:
        abstract = True


class WorkflowGroupAccess(BaseModel):
    """
    RBAC link between a workflow *instance* and the parliamentary ``Group``
    (House / Committee) that holds access on it.

    The instance is referenced through a generic foreign key so a single table
    serves every concrete workflow subclass. ``can_*`` here are the group-level
    defaults; :class:`WorkflowRolePermission` refines them per role and
    :class:`WorkflowStatePermission` per state.
    """

    group = models.ForeignKey(
        "Group",
        on_delete=models.CASCADE,
        related_name="workflow_accesses",
        help_text="House / Committee (or sub-unit) granted access to the workflow.",
    )

    # Generic Foreign Key pointing to the concrete workflow's BigAutoField PK
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    # Group-level default permissions (used when no role/state rows exist)
    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    is_primary = models.BooleanField(default=False)
    granted_by = models.ForeignKey(
        "User", on_delete=models.SET_NULL, null=True, blank=True
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self):
        obj = self.content_object
        label = (
            getattr(obj, "title", str(obj)) if obj is not None else f"#{self.object_id}"
        )
        return f"{self.group.name} -> {label}"


class WorkflowRolePermission(BaseModel):
    """
    Instance-level override for a *specific role* within a group's access.

    Roles are the same ``Role`` records a user holds through
    ``GroupMembership``. If no row exists for a role, the group-level defaults
    (``WorkflowGroupAccess.can_*``) and state permissions apply. If a row
    exists, it is authoritative for that role: it overrides both the group
    default and the state permission. The optional ``allowed_states`` M2M scopes
    the override to specific states only.
    """

    group_access = models.ForeignKey(
        WorkflowGroupAccess,
        on_delete=models.CASCADE,
        related_name="role_permissions",
    )
    role = models.ForeignKey(
        "Role",
        on_delete=models.CASCADE,
        related_name="workflow_permissions",
    )

    # Override permissions for this specific role
    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    # State-specific permissions (optional, overrides state permissions)
    allowed_states = models.ManyToManyField(
        "State",
        blank=True,
        related_name="role_workflow_permissions",
        help_text="If specified, these permissions only apply in these states",
    )

    class Meta:
        ordering = ["role__name"]
        indexes = [
            models.Index(fields=["group_access", "role"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group_access", "role"],
                name="workflows_workflowrolepermission_access_role_uniq",
            ),
        ]

    def __str__(self):
        perms = []
        if self.can_view:
            perms.append("view")
        if self.can_edit:
            perms.append("edit")
        if self.can_delete:
            perms.append("delete")
        if self.can_transition:
            perms.append("transition")
        perm_str = ", ".join(perms) if perms else "none"
        return f"{self.group_access.group.name} - {self.role.name}: [{perm_str}]"


class WorkflowStatePermission(BaseModel):
    """
    State-level permissions for a group's access to a workflow ("what roles can
    view/edit in Drafting vs Gazetted"). Rows are keyed by
    ``(group_access, state)``; the four ``can_*`` flags describe what the granted
    group may do while the workflow is in that state.
    """

    group_access = models.ForeignKey(
        WorkflowGroupAccess,
        on_delete=models.CASCADE,
        related_name="state_permissions",
    )
    state = models.ForeignKey(
        "State",
        on_delete=models.CASCADE,
        related_name="group_workflow_permissions",
    )

    can_view = models.BooleanField(default=True)
    can_edit = models.BooleanField(default=False)
    can_delete = models.BooleanField(default=False)
    can_share = models.BooleanField(default=False)
    can_comment = models.BooleanField(default=False)
    can_manage = models.BooleanField(default=False)
    can_transition = models.BooleanField(default=False)

    class Meta:
        ordering = ["state__name"]
        indexes = [
            models.Index(fields=["group_access", "state"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group_access", "state"],
                name="workflows_workflowstatepermission_access_state_uniq",
            ),
        ]

    def __str__(self):
        perms = []
        if self.can_view:
            perms.append("view")
        if self.can_edit:
            perms.append("edit")
        if self.can_delete:
            perms.append("delete")
        if self.can_transition:
            perms.append("transition")
        perm_str = ", ".join(perms) if perms else "none"
        return f"{self.group_access.group.name} - {self.state.name}: [{perm_str}]"


class State(BaseModel):
    """
    Workflow states: Draft, Submitted, Under Review, Approved, Rejected, etc.

    ``public_name`` is the simplified, citizen-facing status a state maps to.
    Internal machines keep their detailed procedural states while the public
    view collapses them onto this vocabulary — the Online Bill Tracking BRS, for
    example, tracks each committee stage separately but publishes all of them
    as *Under Parliamentary Consideration*.
    """

    PUBLIC_NAME_CHOICES = [
        # Generic buckets, shared by every workflow type.
        ("new", _("New")),
        ("in_progress", _("In Progress")),
        ("referred", _("Referred")),
        ("withdrawn", _("Withdrawn")),
        ("on_hold", _("On Hold")),
        ("cancelled", _("Cancelled")),
        ("implemented", _("Implemented")),
        ("closed", _("Closed")),
        # High-level public statuses proposed for bill tracking (BRS §12).
        ("introduced", _("Introduced")),
        ("under_consideration", _("Under Parliamentary Consideration")),
        ("ncop", _("National Council of Provinces")),
        ("mediation", _("Mediation / Reconsideration")),
        ("awaiting_assent", _("Awaiting Presidential Assent")),
        ("signed_into_law", _("Signed into Law")),
        ("constitutional_review", _("Referred Back / Constitutional Review")),
    ]

    workflow_type = models.ForeignKey(
        WorkflowType, on_delete=models.CASCADE, related_name="states"
    )
    name = models.CharField(max_length=100)
    public_name = models.CharField(max_length=50, choices=PUBLIC_NAME_CHOICES)
    slug = AutoSlugField(
        populate_from="name",
        unique=False,
        db_index=True,
        editable=False,
        help_text="Stable internal identifier for this state (unique per workflow type).",
    )
    description = models.TextField(blank=True)

    # State properties
    is_initial = models.BooleanField(
        default=False,
        help_text="Is this the initial state?",  # ignore
    )  # type: ignore
    is_terminal = models.BooleanField(default=False, help_text="Is this a final state?")  # type: ignore
    allows_referrals = models.BooleanField(
        default=True,
        help_text="Can workflows in this state be referred to other groups?",
    )  # type: ignore

    # Ordering for display
    order = models.IntegerField(default=0)  # type: ignore

    # Visual properties
    color = models.CharField(
        max_length=7, default="#5b8f22", help_text="Hex color code"
    )

    class Meta:
        ordering = ["workflow_type", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_type", "name"],
                name="workflows_state_type_name_uniq",
            ),
            models.UniqueConstraint(
                fields=["workflow_type", "slug"],
                name="workflows_state_type_slug_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.workflow_type.name} - {self.name}"


class Transition(BaseModel):
    """
    Defines allowed transitions between workflow states.
    """

    workflow_type = models.ForeignKey(
        WorkflowType, on_delete=models.CASCADE, related_name="transitions"
    )
    name = models.CharField(max_length=100)
    slug = AutoSlugField(
        populate_from="name",
        unique=False,
        db_index=True,
        editable=False,
        help_text="Stable internal identifier for this transition (unique per workflow type).",
    )
    from_state = models.ForeignKey(
        State, on_delete=models.CASCADE, related_name="transitions_from"
    )
    to_state = models.ForeignKey(
        State, on_delete=models.CASCADE, related_name="transitions_to"
    )

    # Permissions (roles are the same Role records used for group membership)
    allowed_roles = models.ManyToManyField(Role, related_name="allowed_transitions")

    # Email alert roles - members with these roles will be notified on this transition
    notify_roles = models.ManyToManyField(
        Role,
        related_name="notified_transitions",
        blank=True,
        help_text="Group members with these roles will receive an email alert when this transition occurs",
    )

    # Transition guards: at least one event of each listed type must have been
    # recorded on the instance before this transition can be performed, e.g.
    # "Closed – House approved" requires an ``atc_update_published`` event.
    required_event_types = models.ManyToManyField(
        "EventType",
        related_name="required_by_transitions",
        blank=True,
        help_text=(
            "Transitions are blocked until at least one event of each selected "
            "type has been recorded on the workflow instance."
        ),
    )

    # Transition properties
    requires_comment = models.BooleanField(default=True)  # type: ignore
    order = models.IntegerField(default=0)  # type: ignore

    class Meta:
        ordering = ["workflow_type", "order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workflow_type", "from_state", "to_state"],
                name="workflows_transition_type_from_to_uniq",
            ),
            models.UniqueConstraint(
                fields=["workflow_type", "slug"],
                name="workflows_transition_type_slug_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.name}: {self.from_state.name} → {self.to_state.name}"


class TransitionCondition(BaseModel):
    """
    Extra business rule guarding a transition, beyond the state machine and
    the event guards (``Transition.required_event_types``).

    ``condition_type`` selects a handler from
    :data:`TRANSITION_CONDITION_HANDLERS`; ``field_name`` parameterises handlers
    that need it (``field_set``). Conditions are evaluated by
    ``AbstractLegislativeWorkflow.unmet_transition_conditions()`` and therefore
    block ``perform_transition()`` until satisfied.
    """

    CONDITION_CHOICES = [
        ("no_open_referrals", _("No open referrals")),
        ("all_children_closed", _("All child workflows are terminal")),
        ("field_set", _("Required field is set")),
    ]

    transition = models.ForeignKey(
        Transition,
        on_delete=models.CASCADE,
        related_name="conditions",
        help_text="Transition this rule guards.",
    )
    condition_type = models.CharField(max_length=50, choices=CONDITION_CHOICES)
    field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Field checked by parameterised conditions, e.g. field_set.",
    )
    enabled = models.BooleanField(
        default=True, help_text="Uncheck to temporarily disable the rule."
    )
    order = models.IntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["transition", "condition_type", "field_name"],
                name="workflows_transitioncondition_uniq",
            ),
        ]

    def __str__(self):
        detail = f" ({self.field_name})" if self.field_name else ""
        return f"{self.transition.name}: {self.get_condition_type_display()}{detail}"

    def evaluate(self, instance):
        """Return a message when this rule is unmet, else ``None``."""
        handler = TRANSITION_CONDITION_HANDLERS.get(self.condition_type)
        if handler is None:
            return f"Unknown transition condition '{self.condition_type}'."
        return handler(instance, self)


#: Registry of condition handlers, keyed by ``TransitionCondition.condition_type``.
#: A handler takes ``(instance, condition)`` and returns an unmet message or ``None``.
TRANSITION_CONDITION_HANDLERS = {}


def transition_condition(condition_type):
    """Register a handler for a ``TransitionCondition.condition_type``."""

    def decorator(handler):
        TRANSITION_CONDITION_HANDLERS[condition_type] = handler
        return handler

    return decorator


@transition_condition("no_open_referrals")
def _no_open_referrals(instance, condition):
    count = instance.referrals().filter(status="open").count()
    if count:
        return f"Resolve {count} open referral(s) before this transition."
    return None


@transition_condition("all_children_closed")
def _all_children_closed(instance, condition):
    open_children = [
        child for child in instance.sub_workflows if not child.current_state.is_terminal
    ]
    if open_children:
        return f"{len(open_children)} child workflow(s) are not in a terminal state."
    return None


@transition_condition("field_set")
def _field_set(instance, condition):
    if not condition.field_name:
        return "Condition misconfigured: no field name set."
    if not getattr(instance, condition.field_name, None):
        return f"Requires '{condition.field_name}' to be set."
    return None


class TransitionLog(models.Model):
    """
    Generic domain audit log for workflow progression.

    ``content_object`` is the concrete workflow instance
    (``InternationalResolution`` today, ``Bill``/``Motion``/``Question`` later),
    so one log table serves every subclass. Entries are written by
    ``AbstractLegislativeWorkflow.perform_transition()``.
    """

    id = models.BigAutoField(primary_key=True)

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    action = models.CharField(max_length=100)  # e.g., 'STATE_TRANSITION', 'REFERRAL'
    from_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )
    to_state = models.ForeignKey(
        "State", related_name="+", on_delete=models.SET_NULL, null=True
    )

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-timestamp"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} - {self.action} @ {self.timestamp}"


class EventType(BaseModel):
    """
    Data-driven registry of business-meaningful workflow events.

    A table rather than code choices so administrators can add event types per
    instrument without a migration, and so transition guards
    (``Transition.required_event_types``) can reference the same registry.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(
        default=False,
        help_text="Seeded standard type; treated as read-only in the admin.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkflowEvent(models.Model):
    """
    Append-only domain event log for any concrete workflow instance.

    Complements :class:`TransitionLog`: ``TransitionLog`` records *state
    changes*, while ``WorkflowEvent`` records other lifecycle facts
    ("delegation report document attached", "ATC update published", "referral
    answered"). Rows are immutable — corrections are compensating events —
    which keeps the timeline trustworthy for audit (BR08).

    The instance is referenced through a generic foreign key so one table
    serves every concrete subclass, and ``transition_log`` optionally links an
    event back to the state change that produced it.
    """

    ORIGIN_CHOICES = [
        ("user", _("User")),
        ("system", _("System")),
        ("integration", _("Integration")),
    ]

    id = models.BigAutoField(primary_key=True)

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    event_type = models.ForeignKey(
        EventType,
        on_delete=models.PROTECT,
        related_name="events",
        help_text="What happened.",
    )
    occurred_at = models.DateTimeField(
        default=timezone.now, help_text="When the event happened."
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Who caused the event (null for system-generated events).",
    )
    origin = models.CharField(max_length=20, choices=ORIGIN_CHOICES, default="user")
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific structured detail.",
    )
    document_url = models.URLField(
        blank=True, help_text="Optional SharePoint / document reference."
    )
    notes = models.TextField(blank=True)
    transition_log = models.ForeignKey(
        "TransitionLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
        help_text="State change that produced this event, when applicable.",
    )

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-occurred_at"]),
            models.Index(fields=["event_type", "-occurred_at"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} - {self.event_type.name} @ {self.occurred_at}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "WorkflowEvent rows are append-only; record a new event instead "
                "of editing this one."
            )
        return super().save(*args, **kwargs)


class WorkflowReferral(BaseModel):
    """
    A referral of a workflow instance to a parliamentary group/committee.

    Replaces the old ``referred_to_groups`` M2M, which could not carry who
    referred, when, to whom, by when, or the response. Creation is gated by
    ``State.allows_referrals`` (see ``AbstractLegislativeWorkflow.refer()``).

    Creation and lifecycle actions (``respond()`` / ``recall()`` /
    ``mark_expired()``) emit ``referral-*`` :class:`WorkflowEvent` rows, so
    every referral lands on the instance timeline automatically.
    """

    STATUS_CHOICES = [
        ("open", _("Open")),
        ("responded", _("Responded")),
        ("recalled", _("Recalled")),
        ("expired", _("Expired")),
        ("cancelled", _("Cancelled")),
    ]

    #: Status -> event type slug emitted on the transition into that status.
    STATUS_EVENT_SLUGS = {
        "responded": "referral-responded",
        "recalled": "referral-recalled",
        "expired": "referral-expired",
    }

    # Generic relation to the concrete workflow instance
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")

    referred_to = models.ForeignKey(
        "Group",
        on_delete=models.PROTECT,
        related_name="workflow_referrals",
        help_text="Committee / group asked to consider the workflow.",
    )
    referred_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_made",
    )
    referred_at = models.DateTimeField(default=timezone.now)
    due_date = models.DateTimeField(
        null=True, blank=True, help_text="Response deadline for the referral."
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")

    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_responded",
    )
    response_document_url = models.URLField(blank=True)
    response_notes = models.TextField(blank=True)

    recalled_at = models.DateTimeField(null=True, blank=True)
    recalled_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflow_referrals_recalled",
    )
    recall_reason = models.TextField(blank=True)

    deadline_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last deadline reminder sent (for the scheduled job).",
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-referred_at", "-id"]
        indexes = [
            models.Index(fields=["content_type", "object_id", "-referred_at"]),
            models.Index(fields=["status", "due_date"]),
        ]

    def __str__(self):
        target = self.content_object
        label = (
            getattr(target, "title", str(target))
            if target is not None
            else f"#{self.object_id}"
        )
        return f"{label} → {self.referred_to.name} ({self.get_status_display()})"

    # -- lifecycle actions --------------------------------------------------
    @property
    def is_open(self):
        return self.status == "open"

    @property
    def is_overdue(self):
        return bool(self.is_open and self.due_date and self.due_date < timezone.now())

    def respond(self, *, responded_by=None, document_url="", notes=""):
        """Mark the referral answered (emits ``referral-responded``)."""
        if not self.is_open:
            raise ValidationError("Only open referrals can be responded to.")
        self.status = "responded"
        self.responded_at = timezone.now()
        self.responded_by = responded_by
        if document_url:
            self.response_document_url = document_url
        if notes:
            self.response_notes = notes
        self.save()
        return self

    def recall(self, *, recalled_by=None, reason=""):
        """Withdraw the referral (emits ``referral-recalled``)."""
        if not self.is_open:
            raise ValidationError("Only open referrals can be recalled.")
        self.status = "recalled"
        self.recalled_at = timezone.now()
        self.recalled_by = recalled_by
        if reason:
            self.recall_reason = reason
        self.save()
        return self

    def mark_expired(self):
        """Mark an overdue open referral expired (emits ``referral-expired``)."""
        if not self.is_open:
            return self
        self.status = "expired"
        self.save()
        return self

    # -- event emission -----------------------------------------------------
    def _emit_event(self, slug, *, actor=None, payload=None, notes=""):
        event_type = EventType.objects.filter(slug=slug).first()
        if event_type is None:  # registry entry removed — skip silently
            return None
        return WorkflowEvent.objects.create(
            content_type_id=self.content_type_id,
            object_id=self.object_id,
            event_type=event_type,
            actor=actor,
            origin="user" if actor else "system",
            payload=payload or {},
            notes=notes,
        )

    def save(self, *args, **kwargs):
        created = self._state.adding
        previous_status = None
        if not created:
            previous_status = (
                WorkflowReferral.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
        super().save(*args, **kwargs)

        if created:
            self._emit_event(
                "referral-created",
                actor=self.referred_by,
                payload={"referred_to": self.referred_to.name},
            )
        elif previous_status != self.status:
            slug = self.STATUS_EVENT_SLUGS.get(self.status)
            if slug:
                self._emit_event(
                    slug,
                    actor=self.responded_by or self.recalled_by,
                    payload={"status": self.status},
                )


class WorkflowRelationship(BaseModel):
    """
    Generic parent/child link between two concrete workflow *instances*.

    Foreign keys to the abstract :class:`AbstractLegislativeWorkflow` are not
    possible (they raise ``fields.E300``), so both endpoints are referenced
    through a generic foreign key — the same pattern as
    :class:`WorkflowGroupAccess` and :class:`TransitionLog`. One table therefore
    links every concrete subclass (``DelegationReport``,
    ``InternationalResolution``, future ``Bill``/``Motion``/``Question``).

    Each child may have at most one parent (enforced by a unique constraint), so
    the links form a tree: a ``DelegationReport`` *contains* many
    ``InternationalResolution`` rows, and may itself sit under a higher-level
    report. Traverse the tree through the helpers on
    :class:`AbstractLegislativeWorkflow` (``parent_workflow``,
    ``sub_workflows``, ``get_all_descendants()``, ...).
    """

    RELATIONSHIP_CHOICES = [
        ("contains", _("Contains")),
        ("follows_up", _("Follows Up")),
        ("supersedes", _("Supersedes")),
        ("relates_to", _("Relates To")),
    ]

    parent_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="workflow_parent_links",
        help_text="Content type of the parent workflow instance.",
    )
    parent_object_id = models.PositiveBigIntegerField()
    parent = GenericForeignKey("parent_content_type", "parent_object_id")

    child_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name="workflow_child_links",
        help_text="Content type of the child workflow instance.",
    )
    child_object_id = models.PositiveBigIntegerField()
    child = GenericForeignKey("child_content_type", "child_object_id")

    relationship_type = models.CharField(
        max_length=30,
        choices=RELATIONSHIP_CHOICES,
        default="contains",
        help_text="Nature of the link; a report normally *contains* its resolutions.",
    )
    order = models.IntegerField(default=0, help_text="Display order among siblings.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["parent_content_type", "parent_object_id"]),
            models.Index(fields=["child_content_type", "child_object_id"]),
        ]
        constraints = [
            # A child has a single parent, which keeps the graph a tree.
            models.UniqueConstraint(
                fields=["child_content_type", "child_object_id"],
                name="workflows_relationship_child_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.parent} -> {self.child} ({self.get_relationship_type_display()})"

    def clean(self):
        super().clean()
        parent, child = self.parent, self.child
        if parent is None or child is None:
            raise ValidationError(
                "A workflow relationship requires both a parent and a child."
            )
        if not isinstance(parent, AbstractLegislativeWorkflow) or not isinstance(
            child, AbstractLegislativeWorkflow
        ):
            raise ValidationError(
                "Both endpoints of a workflow relationship must be workflow instances."
            )
        if parent._instance_ct() == child._instance_ct() and parent.pk == child.pk:
            raise ValidationError("A workflow cannot be its own parent.")
        if self._creates_cycle(parent, child):
            raise ValidationError(
                "This relationship would create a cycle in the workflow hierarchy."
            )
        self._validate_types(parent, child)

    @staticmethod
    def _validate_types(parent, child):
        """Honour an optional type-level declaration of allowed child types."""
        allowed = parent.workflow_type.allowed_child_types()
        if allowed.exists() and not allowed.filter(pk=child.workflow_type_id).exists():
            raise ValidationError(
                f"Workflow type '{parent.workflow_type.name}' does not allow "
                f"'{child.workflow_type.name}' as a child type."
            )

    @staticmethod
    def _creates_cycle(parent, child):
        """True when ``parent`` is a descendant of ``child`` (would loop)."""
        child_key = (child._instance_ct().pk, child.pk)
        seen = set()
        node = parent
        while node is not None:
            key = (node._instance_ct().pk, node.pk)
            if key == child_key or key in seen:
                return True
            seen.add(key)
            link = node._parent_link()
            node = link.parent if link is not None else None
        return False

    def save(self, *args, **kwargs):
        # Endpoint / self-reference / type / cycle checks are domain validation
        # rather than field validation, so enforce them on every write.
        self.clean()
        return super().save(*args, **kwargs)


class DelegationReport(AbstractLegislativeWorkflow):
    """
    A delegation's report on an international engagement/forum and the
    resolutions concluded there (BRS BR02/BR03).

    System behaviour:

    * a unique ``reference_number`` is generated on creation (BR02.6);
    * newly created reports start in the seeded *Awaiting PGIR approval*
      state, the BRS's automatic initial status (BR02.7);
    * each report contains many :class:`InternationalResolution` rows through
      :class:`WorkflowRelationship` (see ``add_sub_workflow()``);
    * delegation members and support officials are captured as
      :class:`DelegationParticipant` rows (BR02.3.7/8);
    * the implementation due date (BR02.3.12) is the inherited ``deadline``.

    Documents (BR02.3.14, BR03.5.5, BR11) are held in SharePoint; this model
    keeps the report document link, while BR03 updates (ATC reference, date,
    page, document) are stored per update in :class:`DelegationReportUpdate` and
    exposed as read-only ``atc_*`` properties holding the latest values.
    """

    reference_number = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="System-generated unique reference number (BR02.6).",
    )
    engagement_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Name of the international engagement / forum (BR02.3.3).",
    )
    engagement_start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Engagement start date; may not be a future date (BR02.3.4).",
    )
    engagement_end_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "Engagement end date; may not be before the start date or in the "
            "future (BR02.3.5)."
        ),
    )
    location_city = models.ForeignKey(
        "City",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_reports",
        help_text="Engagement location – city (BR02.3.6).",
    )
    location_country = models.ForeignKey(
        "Country",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_reports",
        help_text="Engagement location – country (BR02.3.6).",
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Additional notes / follow-up action by Presiding Officer(s) or "
            "Parliamentarian(s) (BR02.3.13)."
        ),
    )

    # BR02.3.14 / BR03.5.5 / BR11: documents live in SharePoint.
    report_document_url = models.URLField(
        blank=True,
        help_text="SharePoint link to the delegation report document (BR02.3.14).",
    )

    class Meta:
        verbose_name = "Delegation Report"
        verbose_name_plural = "Delegation Reports"

    def __str__(self):
        label = self.reference_number or "unsaved"
        return f"{label} – {self.title}" if self.title else str(label)

    # -- BRS validation -----------------------------------------------------
    def clean(self):
        super().clean()
        today = timezone.localdate()
        errors = {}
        if self.engagement_start_date and self.engagement_start_date > today:
            errors["engagement_start_date"] = (
                "The engagement start date may not be a future date (BR02.3.4)."
            )
        if self.engagement_end_date:
            if self.engagement_end_date > today:
                errors["engagement_end_date"] = (
                    "The engagement end date may not be a future date (BR02.3.5)."
                )
            elif (
                self.engagement_start_date
                and self.engagement_end_date < self.engagement_start_date
            ):
                errors["engagement_end_date"] = (
                    "The engagement end date may not be before the start date "
                    "(BR02.3.5)."
                )
        if errors:
            raise ValidationError(errors)

    # -- system-generated reference number (BR02.6) -------------------------
    def save(self, *args, **kwargs):
        if self._state.adding and not self.reference_number:
            self.reference_number = self._next_reference_number()
        super().save(*args, **kwargs)

    @classmethod
    def _next_reference_number(cls):
        """
        ``DR-<year>-<sequence>``, sequential per calendar year.

        The unique constraint backstops the small race window between the
        lookup and the insert.
        """
        year = timezone.now().year
        prefix = f"DR-{year}-"
        last = (
            cls.objects.filter(reference_number__startswith=prefix)
            .order_by("-reference_number")
            .values_list("reference_number", flat=True)
            .first()
        )
        try:
            sequence = int(last.rsplit("-", 1)[1]) + 1
        except AttributeError, IndexError, ValueError:
            sequence = 1
        return f"{prefix}{sequence:04d}"

    # -- overdue handling (BR12) -------------------------------------------
    @property
    def is_overdue(self):
        """True when the due date has expired and the report is not closed."""
        if not self.deadline or not self.current_state_id:
            return False
        return self.deadline < timezone.now() and not self.current_state.is_terminal

    @property
    def overdue_identifier(self):
        """BR12 flag text; empty string when the report is not overdue."""
        return OVERDUE_IDENTIFIER if self.is_overdue else ""

    # -- BR03 update history ------------------------------------------------
    def record_update(
        self,
        *,
        update_date=None,
        resulting_state=None,
        atc_reference="",
        atc_publication_date=None,
        atc_page_number="",
        atc_document_url="",
        notes="",
        recorded_by=None,
    ):
        """
        Append a :class:`DelegationReportUpdate` (BR03) and return it.

        ATC publication details make the update emit an
        ``atc-update-published`` event, unlocking the seeded close guard.
        """
        return DelegationReportUpdate.objects.create(
            delegation_report=self,
            update_date=update_date or timezone.localdate(),
            resulting_state=resulting_state,
            atc_reference=atc_reference,
            atc_publication_date=atc_publication_date,
            atc_page_number=atc_page_number,
            atc_document_url=atc_document_url,
            notes=notes,
            recorded_by=recorded_by,
        )

    @property
    def latest_update(self):
        """Most recent BR03 update, or ``None``."""
        return self.updates.order_by("-update_date", "-id").first()

    # BR03 ATC detail is the read-only "latest" view of the update history;
    # the full history lives in DelegationReportUpdate rows.
    @property
    def atc_reference(self):
        update = self.latest_update
        return update.atc_reference if update else ""

    @property
    def atc_publication_date(self):
        update = self.latest_update
        return update.atc_publication_date if update else None

    @property
    def atc_page_number(self):
        update = self.latest_update
        return update.atc_page_number if update else ""

    @property
    def atc_document_url(self):
        update = self.latest_update
        return update.atc_document_url if update else ""


class DelegationParticipant(BaseModel):
    """
    A parliamentary delegation member or support official (BR02.3.7/8).

    Captures name, surname, title and delegation role per person, with an
    optional link to a :class:`User` when the person has a system account.
    """

    MEMBER = "member"
    OFFICIAL = "official"
    PARTICIPANT_TYPE_CHOICES = [
        (MEMBER, _("Parliamentary Delegation Member")),
        (OFFICIAL, _("Support Official")),
    ]

    delegation_report = models.ForeignKey(
        DelegationReport,
        on_delete=models.CASCADE,
        related_name="participants",
        help_text="Delegation report this person is attached to.",
    )
    participant_type = models.CharField(
        max_length=20,
        choices=PARTICIPANT_TYPE_CHOICES,
        default=MEMBER,
        help_text="Parliamentary delegation member or support official.",
    )
    title = models.CharField(
        max_length=50,
        blank=True,
        help_text="Honorific / job title, e.g. Hon., Dr, Prof.",
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    delegation_role = models.CharField(
        max_length=100,
        blank=True,
        help_text="Role in the delegation, e.g. Leader of the Delegation, MP, Secretary.",
    )
    user = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_participations",
        help_text="Optional link to the person's system account.",
    )
    order = models.IntegerField(
        default=0, help_text="Display order within the delegation."
    )

    class Meta:
        ordering = ["participant_type", "order", "last_name", "first_name"]
        indexes = [
            models.Index(fields=["delegation_report", "participant_type"]),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return f"{name} ({self.get_participant_type_display()})"

    @property
    def full_name(self):
        return " ".join(
            part for part in (self.title, self.first_name, self.last_name) if part
        )


class DelegationReportUpdate(BaseModel):
    """
    A recorded update on a delegation report (BRS BR03).

    The BRS "delegation report update type" is the report status the update
    moved the report to (``resulting_state``); the ATC publication details are
    the supporting evidence (BR03.5.3/5).

    Creating a row that carries ATC publication details emits an
    ``atc-update-published`` :class:`WorkflowEvent` — the evidence the seeded
    *Close – House approved* transition guard requires. Rows form the report's
    BR03 update history (the previous single-set ``atc_*`` fields are exposed
    as read-only "latest" properties on :class:`DelegationReport`).
    """

    delegation_report = models.ForeignKey(
        DelegationReport,
        on_delete=models.CASCADE,
        related_name="updates",
        help_text="Report this update belongs to.",
    )
    update_date = models.DateField(
        default=timezone.localdate, help_text="Date of the update (BR03.5.2)."
    )
    resulting_state = models.ForeignKey(
        "State",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="delegation_report_updates",
        help_text="Report status (update type) after this update (BR03.5.1).",
    )
    atc_reference = models.CharField(
        max_length=255, blank=True, help_text="ATC reference (BR03.5.3)."
    )
    atc_publication_date = models.DateField(
        null=True, blank=True, help_text="ATC publication date (BR03.5.3)."
    )
    atc_page_number = models.CharField(
        max_length=50, blank=True, help_text="ATC page number (BR03.5.3)."
    )
    atc_document_url = models.URLField(
        blank=True,
        help_text="SharePoint link to the ATC / update document (BR03.5.5).",
    )
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="delegation_report_updates",
    )

    class Meta:
        ordering = ["-update_date", "-id"]
        indexes = [
            models.Index(fields=["delegation_report", "-update_date"]),
        ]

    def __str__(self):
        return f"{self.delegation_report} — update {self.update_date}"

    @property
    def has_atc_details(self):
        """True when the update carries ATC publication evidence (BR03.5.3)."""
        return bool(
            self.atc_reference
            or self.atc_publication_date
            or self.atc_page_number
            or self.atc_document_url
        )

    def save(self, *args, **kwargs):
        created = self._state.adding
        super().save(*args, **kwargs)
        if not created or not self.has_atc_details:
            return
        event_type = EventType.objects.filter(slug="atc-update-published").first()
        if event_type is None:  # registry entry removed — skip silently
            return
        self.delegation_report.record_event(
            event_type,
            actor=self.recorded_by,
            payload={
                "atc_reference": self.atc_reference,
                "atc_page_number": self.atc_page_number,
                "update_date": str(self.update_date),
            },
            document_url=self.atc_document_url,
            notes=self.notes,
        )


class InternationalResolution(AbstractLegislativeWorkflow):
    """
    A resolution adopted at an international engagement (BRS BR02.3.9).

    Resolutions are captured from the recommendations of a delegation report and
    attached to it through :class:`WorkflowRelationship`. Their lifecycle is
    seeded as *Captured → Assigned → In Progress → Implemented → Closed* and
    implementation progress is recorded in ``implementation_progress``.
    """

    resolution_number = models.CharField(max_length=50, unique=True)
    resolution_text = models.TextField(
        blank=True,
        help_text=(
            "Resolution as concluded, captured from the delegation report "
            "recommendations (BR02.3.9)."
        ),
    )
    adoption_date = models.DateField(null=True, blank=True)
    responsible_group = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolutions_responsible",
        help_text="Committee / group responsible for implementation.",
    )
    implementation_progress = models.TextField(
        blank=True,
        help_text="Latest narrative on Parliament's implementation of the resolution.",
    )

    class Meta:
        verbose_name = "International Resolution"
        verbose_name_plural = "International Resolutions"

    def __str__(self):
        return f"Resolution {self.resolution_number} - {self.title}"


class InternationalAgreement(AbstractLegislativeWorkflow):
    """
    A government international agreement tabled in Parliament and tracked
    through referral, committee consideration and House adoption (BRS
    *International Agreements Tracking and Monitoring*, BR02/BR03).

    System behaviour:

    * a unique ``reference_number`` is generated on creation (BR02);
    * newly created agreements start in the seeded *Agreement Tabled –
      referred to Committee* state, the BRS's automatic initial status (BR02);
    * the agreement type is Section 231(2) or Section 231(3) (BR02);
    * referral committees are captured as a many-to-many link to
      :class:`Group` (BR02);
    * the implementation due date (BR02) is the inherited ``deadline``, and the
      BR12 overdue identifier is exposed by ``is_overdue`` /
      ``overdue_identifier``.

    Documents (BR02, BR04, BR11) are held in SharePoint; this model keeps the
    agreement and explanatory-memorandum links.
    """

    SECTION_231_2 = "section-231-2"
    SECTION_231_3 = "section-231-3"
    AGREEMENT_TYPE_CHOICES = [
        (SECTION_231_2, _("Section 231(2) agreement")),
        (SECTION_231_3, _("Section 231(3) agreement")),
    ]

    reference_number = models.CharField(
        max_length=30,
        unique=True,
        null=True,
        blank=True,
        editable=False,
        help_text="System-generated unique reference number (BR02).",
    )
    agreement_type = models.CharField(
        max_length=20,
        choices=AGREEMENT_TYPE_CHOICES,
        blank=True,
        help_text="Constitutional basis of the agreement (BR02).",
    )
    submitting_department = models.CharField(
        max_length=255,
        blank=True,
        help_text="Government department that submitted the agreement (BR02).",
    )
    responsible_minister = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            "Responsible Member of the Executive (Minister) submitting the "
            "agreement (BR02)."
        ),
    )
    atc_tabling_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the agreement was tabled in the ATC (BR02).",
    )
    atc_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text=(
            "Reference details of the ATC and any other relevant documents (BR02)."
        ),
    )
    referral_committees = models.ManyToManyField(
        "Group",
        blank=True,
        related_name="international_agreements_referred",
        help_text="Committee(s) to which the agreement is referred (BR02).",
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Additional notes / follow-up action by Presiding Officer(s) or "
            "Parliamentarian(s) (BR02)."
        ),
    )
    agreement_document_url = models.URLField(
        blank=True,
        help_text="SharePoint link to the uploaded agreement document (BR02).",
    )
    explanatory_memorandum_url = models.URLField(
        blank=True,
        help_text="SharePoint link to the explanatory memorandum (BR02).",
    )

    class Meta:
        verbose_name = "International Agreement"
        verbose_name_plural = "International Agreements"

    def __str__(self):
        label = self.reference_number or "unsaved"
        return f"{label} – {self.title}" if self.title else str(label)

    # -- system-generated reference number (BR02) ---------------------------
    def save(self, *args, **kwargs):
        if self._state.adding and not self.reference_number:
            self.reference_number = self._next_reference_number()
        super().save(*args, **kwargs)

    @classmethod
    def _next_reference_number(cls):
        """
        ``IA-<year>-<sequence>``, sequential per calendar year.

        The unique constraint backstops the small race window between the
        lookup and the insert.
        """
        year = timezone.now().year
        prefix = f"IA-{year}-"
        last = (
            cls.objects.filter(reference_number__startswith=prefix)
            .order_by("-reference_number")
            .values_list("reference_number", flat=True)
            .first()
        )
        try:
            sequence = int(last.rsplit("-", 1)[1]) + 1
        except AttributeError, IndexError, ValueError:
            sequence = 1
        return f"{prefix}{sequence:04d}"

    # -- overdue handling (BR12) -------------------------------------------
    @property
    def is_overdue(self):
        """True when the due date has expired and the agreement is not closed."""
        if not self.deadline or not self.current_state_id:
            return False
        return self.deadline < timezone.now() and not self.current_state.is_terminal

    @property
    def overdue_identifier(self):
        """BR12 flag text; empty string when the agreement is not overdue."""
        return OVERDUE_IDENTIFIER if self.is_overdue else ""


class Bill(AbstractLegislativeWorkflow):
    """
    A Parliamentary bill tracked through its legislative lifecycle (BRS
    *Online Bill Tracking*).

    The BRS separates detailed internal procedural tracking from a simplified
    public view: every internal state maps to one of the seven high-level
    public statuses through ``State.public_name`` (BRS §12), so the same record
    can be published at ``public_status`` without exposing internal
    granularity.

    System behaviour:

    * ``bill_number`` is Parliament's B-number (unique), including the version
      suffix where one applies (BRS §13.1);
    * the bill profile carries the long and short title, bill type (s74–s77),
      House of introduction, sponsor or originating authority, responsible
      committee, latest version and associated documents (BRS §7A);
    * the ATC and order-paper references are recorded per bill (BRS §7B);
    * ``notes`` captures sub-events under a main stage, so exceptional or
      explanatory steps need not complicate the public lifecycle (BRS §15A).

    Documents live in SharePoint; this model keeps the bill document link.
    """

    SECTION_74 = "section-74"
    SECTION_75 = "section-75"
    SECTION_76 = "section-76"
    SECTION_77 = "section-77"
    BILL_TYPE_CHOICES = [
        (SECTION_74, _("Section 74 bill (constitutional amendment)")),
        (SECTION_75, _("Section 75 bill (ordinary)")),
        (SECTION_76, _("Section 76 bill (affecting provinces)")),
        (SECTION_77, _("Section 77 bill (money)")),
    ]

    NA = "na"
    NCOP = "ncop"
    HOUSE_CHOICES = [
        (NA, _("National Assembly")),
        (NCOP, _("National Council of Provinces")),
    ]

    bill_number = models.CharField(
        max_length=50,
        unique=True,
        help_text=("Parliamentary B-number, including any version suffix (BRS §13.1)."),
    )
    short_title = models.CharField(
        max_length=255,
        blank=True,
        help_text="Short title of the bill (BRS §7A).",
    )
    bill_type = models.CharField(
        max_length=20,
        choices=BILL_TYPE_CHOICES,
        blank=True,
        help_text=(
            "Constitutional classification: Section 74, 75, 76 or 77 (BRS §13.1)."
        ),
    )
    house_of_origin = models.CharField(
        max_length=10,
        choices=HOUSE_CHOICES,
        blank=True,
        help_text="House of introduction (BRS §7A).",
    )
    sponsor = models.CharField(
        max_length=255,
        blank=True,
        help_text="Sponsor or originating authority (BRS §7A).",
    )
    introduced_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date the bill was introduced (BRS §13.1).",
    )
    responsible_committee = models.ForeignKey(
        "Group",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bills_responsible",
        help_text="Committee responsible for the bill (BRS §13.2).",
    )
    atc_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="ATC reference for the bill's movements (BRS §7B).",
    )
    order_paper_reference = models.CharField(
        max_length=255,
        blank=True,
        help_text="Order paper reference, where available (BRS §7B).",
    )
    bill_document_url = models.URLField(
        blank=True,
        help_text="SharePoint link to the bill document (BRS §7B).",
    )
    notes = models.TextField(
        blank=True,
        help_text=(
            "Sub-events or explanatory notes under the current stage (BRS §15A)."
        ),
    )

    class Meta:
        verbose_name = "Bill"
        verbose_name_plural = "Bills"

    def __str__(self):
        return (
            f"{self.bill_number} – {self.title}"
            if self.title
            else str(self.bill_number)
        )

    @property
    def public_status(self):
        """
        The simplified public status for the current state (BRS §12).

        Empty string when the bill has no state yet.
        """
        if self.current_state_id is None:
            return ""
        return self.current_state.get_public_name_display()

    @property
    def current_version(self):
        """
        Label of the bill's current version (BRS §15A).

        Read-only: the :class:`BillVersion` history is the record, so the label
        is derived rather than typed. Prefers the row flagged ``is_current`` and
        falls back to the most recent version; empty string when the bill has no
        versions recorded yet.
        """
        if self.pk is None:
            return ""
        current = self.versions.filter(is_current=True).first()
        if current is not None:
            return current.version_label
        latest = self.versions.first()  # Meta.ordering: newest first
        return latest.version_label if latest is not None else ""

    # -- overdue handling (shared indicator) --------------------------------
    @property
    def is_overdue(self):
        """True when the due date has expired and the bill is not closed."""
        if not self.deadline or not self.current_state_id:
            return False
        return self.deadline < timezone.now() and not self.current_state.is_terminal

    @property
    def overdue_identifier(self):
        """Flag text; empty string when the bill is not overdue."""
        return OVERDUE_IDENTIFIER if self.is_overdue else ""


class BillVersion(BaseModel):
    """
    A preserved version of a bill (BRS *Online Bill Tracking* §15A).

    Version integrity: rows are append-only history — historical versions may
    not be overwritten — and ``version_type`` distinguishes the introduced
    bill, amendment schedules and the resulting amended bill from one another,
    so an amendment and its schedule stay recorded together. The B-number
    suffix carried by ``version_label`` records where the number changed.

    Exactly one row per bill may be flagged ``is_current`` (a partial unique
    constraint, normalised in :meth:`save`), which is the version
    ``Bill.current_version`` reports; the rest are the previous versions.
    """

    INTRODUCED = "introduced"
    AMENDED = "amended"
    AMENDMENT_SCHEDULE = "amendment_schedule"
    VERSION_TYPE_CHOICES = [
        (INTRODUCED, _("Introduced version")),
        (AMENDED, _("Amended bill")),
        (AMENDMENT_SCHEDULE, _("Amendment schedule")),
    ]

    bill = models.ForeignKey(
        Bill,
        on_delete=models.CASCADE,
        related_name="versions",
        help_text="Bill this version belongs to.",
    )
    version_label = models.CharField(
        max_length=100,
        help_text=(
            "Version label, including the B-number suffix where it changed "
            "(e.g. 'B 12—2026 (1st amendment)')."
        ),
    )
    version_type = models.CharField(
        max_length=20,
        choices=VERSION_TYPE_CHOICES,
        default=INTRODUCED,
        help_text=("Introduced bill, amended bill or amendment schedule (BRS §15A)."),
    )
    version_date = models.DateField(
        default=timezone.localdate,
        help_text="Date this version was tabled / published.",
    )
    document_url = models.URLField(
        blank=True,
        help_text="SharePoint link to this version's document (BRS §7B).",
    )
    notes = models.TextField(
        blank=True,
        help_text="What changed in this version.",
    )
    recorded_by = models.ForeignKey(
        "User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bill_versions",
        help_text="Who recorded this version (BRS §7A contributor accountability).",
    )
    is_current = models.BooleanField(
        default=False,
        help_text="Mark the version currently before Parliament.",
    )

    class Meta:
        ordering = ["-version_date", "-id"]
        indexes = [
            models.Index(fields=["bill", "-version_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["bill"],
                condition=models.Q(is_current=True),
                name="workflows_billversion_one_current_per_bill_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.bill} — {self.version_label}"

    def save(self, *args, **kwargs):
        if not self.is_current or not self.bill_id:
            return super().save(*args, **kwargs)
        with transaction.atomic():
            # Only one version per bill may be current, so demote the others
            # before writing this row and the partial unique constraint holds.
            BillVersion.objects.filter(bill_id=self.bill_id, is_current=True).exclude(
                pk=self.pk
            ).update(is_current=False)
            return super().save(*args, **kwargs)
