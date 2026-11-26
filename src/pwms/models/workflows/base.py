"""The abstract workflow base shared by every concrete instrument."""

from datetime import timedelta

from django.contrib.contenttypes.fields import GenericRelation
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone

from ..base import BaseModel
from .access import WorkflowGroupAccess
from .events import TransitionLog, WorkflowEvent
from .machine import Transition
from .referrals import WorkflowNote, WorkflowReferral
from .relationships import WorkflowRelationship

# Sentinel used to cache "no parent link" without re-querying the database.
_UNSET = object()

#: BR12 identifier inserted on reports whose due date has expired while open.
OVERDUE_IDENTIFIER = "Due date expired – pending follow up"

#: A deadline this many days away (or fewer) is flagged as due soon.
DUE_SOON_DAYS = 10


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
        help_text="Current workflow state",
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

    # Reverse side of ``Attachment``'s generic link, so an instance reaches its
    # documents as ``instance.attachments``. Not a column: nothing to migrate.
    attachments = GenericRelation(
        "Attachment",
        related_query_name="%(class)s_attachments",
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

    # -- overdue handling (BR12) --------------------------------------------
    @property
    def is_overdue(self):
        """
        True when the due date has expired while the workflow is still open.

        BR12 models an expired deadline as this flag rather than as a state, so
        it applies to every concrete workflow. ``closed`` means the instance has
        reached a terminal state, whatever that state is called in its type.
        """
        if not self.deadline or not self.current_state_id:
            return False
        return self.deadline < timezone.now() and not self.current_state.is_terminal

    @property
    def overdue_identifier(self):
        """BR12 flag text; empty string when the workflow is not overdue."""
        return OVERDUE_IDENTIFIER if self.is_overdue else ""

    @property
    def deadline_status(self):
        """
        How urgent the deadline is: ``"overdue"``, ``"due_soon"`` or ``""``.

        ``"overdue"`` is :attr:`is_overdue`. ``"due_soon"`` means an open
        workflow whose deadline falls within :data:`DUE_SOON_DAYS` days. A
        workflow with no deadline, one that is further out, and one in a
        terminal state (whose deadline no longer matters) all give ``""``.
        """
        if not self.deadline or not self.current_state_id:
            return ""
        if self.current_state.is_terminal:
            return ""
        now = timezone.now()
        if self.deadline < now:
            return "overdue"
        if self.deadline <= now + timedelta(days=DUE_SOON_DAYS):
            return "due_soon"
        return ""

    def materialize_group_access(self):
        """
        Create this instance's :class:`WorkflowGroupAccess` rows from its
        workflow type, and return the rows that were created.

        Two kinds are materialised:

        * the type's owning ``group``, flagged ``is_primary`` — the unit that
          governs the type, so it may view and work on its records;
        * each of the type's ``viewer_groups`` — read-only shared stakeholders
          (an interest in every instance but no active role in producing it).

        The owning group's capabilities are the type's ``owner_can_*`` policy (view
        is always granted): by default edit and transition, so the unit that owns a
        type can work on its records and move them on out of the box, without being
        able to delete, share or administer them; viewer groups start read-only.
        Configure the type to change that for every instance at once, or raise or
        narrow a capability per instance, or override it for a role or a state
        through ``WorkflowRolePermission`` / ``WorkflowStatePermission``. Runs on
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
                defaults=workflow_type.owner_access_defaults(),
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
        log = TransitionLog.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            action="STATE_TRANSITION",
            from_state=from_state,
            to_state=transition.to_state,
            actor=actor,
            ip_address=ip_address,
            notes=comment,
        )

        # Alert the stakeholders once the new state and its audit row exist, so
        # the message can quote the state it moved into. Imported lazily: the
        # notifications service reads these models.
        from ...notifications import notify_transition

        notify_transition(self, transition, actor=actor, comment=comment)
        return log

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
        referral = WorkflowReferral.objects.create(
            content_type=self._instance_ct(),
            object_id=self.pk,
            referred_to=group,
            referred_by=referred_by,
            due_date=due_date,
            notes=notes,
        )

        # Imported lazily: the notifications service reads these models.
        from ...notifications import notify_referral_created

        notify_referral_created(referral, actor=referred_by)
        return referral

    def sync_referral_access(self, group_id):
        """
        Align ``group_id``'s access row with its referrals on this instance.

        A referral is what gives the referred group a foothold on the record:
        while a referral to it is **open** the group may view and edit the record
        — enough to read it and contribute the answer — and once every referral has
        closed the group keeps **view**, so it can still see what it was asked
        about, which is what audit and reporting need. Runs whenever a referral is
        created, changes status, is reassigned or is deleted (see
        :class:`WorkflowReferral`).

        A referral deliberately confers no ``transition``: moving a record on is the
        owning group's job, and ``transition`` is all-or-nothing — the Status menu
        offers every transition out of the current state, not just the stage the
        committee is considering, so lending it to a referred group would let that
        group close or withdraw a record it was only asked to advise on (see
        Functional Design §4).

        Only what a referral itself raised is taken back: a capability the group
        already held (as the type's owner or a viewer group, or from an
        administrator) is never narrowed, and a row that is more than a referral
        grant is never removed. A row that exists *only* because of referrals goes
        when the last referral row does — the referral registers are the history.
        """
        referrals = self.referrals().filter(referred_to_id=group_id)
        has_referral = referrals.exists()
        has_open_referral = referrals.filter(status="open").exists()

        access = self.group_accesses().filter(group_id=group_id).first()
        if access is None:
            if not has_referral:
                return None
            return WorkflowGroupAccess.objects.create(
                content_type=self._instance_ct(),
                object_id=self.pk,
                group_id=group_id,
                can_view=True,
                can_edit=has_open_referral,
                referral_raised_edit=has_open_referral,
                via_referral=True,
            )

        changed = []
        if has_open_referral:
            # The group reads and works on the record for as long as the referral is
            # open — editing it, and contributing the answer — but no transition:
            # see the note above.
            if not access.can_edit:
                access.can_edit = True
                access.referral_raised_edit = True
                changed += ["can_edit", "referral_raised_edit"]
        elif access.referral_raised_edit:
            # The referral stage is over: hand back what it raised, and keep view so
            # the group can still see the record it considered.
            access.can_edit = False
            access.referral_raised_edit = False
            changed += ["can_edit", "referral_raised_edit"]

        if changed:
            access.save(update_fields=changed)

        # The grant is the referrals': with the last of them gone, a row that
        # exists only because of them has nothing left to justify it.
        if not has_referral and access.via_referral and access.grants_only_view:
            access.delete()
            return None
        return access

    # -- notes (typed rows, see WorkflowNote) -------------------------------
    def note_log(self):
        """WorkflowNote rows recorded against this instance, newest first."""
        return WorkflowNote.objects.filter(
            content_type=self._instance_ct(), object_id=self.pk
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

    def can(self, user, action, *, ignore_referral_grants=False):
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

        ``ignore_referral_grants`` answers the same question as if no referral had
        conferred anything: a row a referral created is passed over, and a
        capability a referral raised on an existing row is not counted. A referred
        group is given edit so its members can contribute to the record, which is
        not authority to administer it — role/state rows, the type's policy and
        anything else the group genuinely holds still count.
        """
        action = action.lower()
        field = self.PERMISSION_ACTIONS.get(action)
        if field is None:
            raise ValueError(f"Unknown action: {action!r}")
        raised_by_referral = f"referral_raised_{action}"

        memberships = list(
            user.memberships.filter(is_active=True).select_related("group", "role")
        )
        # A member may delegate their duties to another member for a period; while
        # that delegation is current the delegate carries the delegator's roles
        # here too (see ``User.delegated_memberships``).
        memberships += list(user.delegated_memberships())
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
            if ignore_referral_grants and access.via_referral:
                # The row exists only because of a referral, so it grants nothing
                # when that grant is being left out of the answer.
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
            if getattr(access, field) and not (
                ignore_referral_grants and getattr(access, raised_by_referral, False)
            ):
                return True

        return False

    # -- presentation / linking ---------------------------------------------
    #: URL name of this type's detail view; set on each concrete subclass so
    #: ``get_absolute_url`` needs no model -> route mapping of its own.
    detail_url_name = ""

    @property
    def identifier(self):
        """
        The human-facing reference this workflow carries, if any.

        Concrete types number their instances differently, so this reaches for
        whichever field the instance actually has rather than assuming one.
        """
        for field in ("reference_number", "resolution_number", "bill_number"):
            value = getattr(self, field, "")
            if value:
                return value
        return ""

    def get_absolute_url(self):
        """Site-relative URL of this instance's detail page, "" when unset."""
        if not self.detail_url_name or self.public_id is None:
            return ""
        return reverse(
            f"pwms:{self.detail_url_name}", kwargs={"public_id": self.public_id}
        )

    def __str__(self):
        return self.title or f"{self._meta.verbose_name} #{self.pk}"

    class Meta:
        abstract = True
