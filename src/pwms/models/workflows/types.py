"""Workflow-type registry and citizen-facing public statuses."""

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.fields import AutoSlugField

from ..base import BaseModel
from ..permissions import Role


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

    # Materialised capability policy: what this type's owning group may do on every
    # instance of the type. ``materialize_group_access`` writes these onto the
    # primary ``WorkflowGroupAccess`` row at creation, and
    # ``sync_type_group_access --update-existing`` re-applies them to rows that
    # predate a change. View is always granted and viewer groups stay read-only;
    # these are the owning group's defaults, not ceilings — an instance, role or
    # state row can still narrow or raise an individual capability.
    owner_can_edit = models.BooleanField(
        default=True,
        verbose_name=_("Owning group may edit"),
        help_text="New instances let this group change fields.",
    )
    owner_can_transition = models.BooleanField(
        default=True,
        verbose_name=_("Owning group may transition"),
        help_text="New instances let this group move the record on.",
    )
    owner_can_delete = models.BooleanField(
        default=False,
        verbose_name=_("Owning group may delete"),
        help_text="New instances let this group delete the record.",
    )
    owner_can_share = models.BooleanField(
        default=False,
        verbose_name=_("Owning group may share"),
        help_text="New instances let this group share the record.",
    )
    owner_can_comment = models.BooleanField(
        default=False,
        verbose_name=_("Owning group may comment"),
        help_text="New instances let this group comment on the record.",
    )
    owner_can_manage = models.BooleanField(
        default=False,
        verbose_name=_("Owning group may manage access"),
        help_text="New instances let this group manage the record's access.",
    )

    # Alert audience. Who hears about a record is declared here (and, for anything
    # more specific, in ``NotificationRule`` rows) rather than derived from RBAC
    # alone. These three are the built-in audiences every type has; see
    # ``pwms.notifications.dispatch`` for how they combine and
    # ``NotificationRule`` for the per-event additions.
    OVERSIGHT_OFF = "off"
    OVERSIGHT_IN_APP = "in_app"
    OVERSIGHT_EMAIL = "email"
    OVERSIGHT_BOTH = "both"
    OVERSIGHT_CHOICES = [
        (OVERSIGHT_OFF, _("Nothing")),
        (OVERSIGHT_IN_APP, _("Bell only")),
        (OVERSIGHT_EMAIL, _("Email only")),
        (OVERSIGHT_BOTH, _("Bell and email")),
    ]
    oversight_channel = models.CharField(
        max_length=10,
        choices=OVERSIGHT_CHOICES,
        default=OVERSIGHT_EMAIL,
        verbose_name=_("Officer oversight"),
        help_text=(
            "How the officers (holders of the create roles in the owning group) are "
            "told about creations and transitions. They can already see everything "
            "in the lists, so the default is email only: no bell count for every "
            "record of the type."
        ),
    )
    notify_next_actors = models.BooleanField(
        default=True,
        verbose_name=_("Tell whoever acts next"),
        help_text=(
            "After a transition, alert the holders of the roles allowed to act from "
            "the new state (in the groups that may transition the record)."
        ),
    )
    notify_named_people = models.BooleanField(
        default=True,
        verbose_name=_("Tell the owner and assignee"),
        help_text="Alert the people the record itself names.",
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

    @property
    def oversight_in_app(self):
        return self.oversight_channel in (self.OVERSIGHT_IN_APP, self.OVERSIGHT_BOTH)

    @property
    def oversight_email(self):
        return self.oversight_channel in (self.OVERSIGHT_EMAIL, self.OVERSIGHT_BOTH)

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

    #: Capability a type can grant its owning group, mapped to the field holding its
    #: default. ``view`` is not configurable: a group that cannot see a record cannot
    #: work on it, and viewer groups are read-only by design.
    OWNER_CAPABILITY_FIELDS = (
        ("edit", "owner_can_edit"),
        ("transition", "owner_can_transition"),
        ("delete", "owner_can_delete"),
        ("share", "owner_can_share"),
        ("comment", "owner_can_comment"),
        ("manage", "owner_can_manage"),
    )

    def owner_access_defaults(self):
        """
        ``WorkflowGroupAccess`` defaults this type materialises for its owner.

        Returns a dict suitable for ``get_or_create(defaults=...)``: the primary
        flag, view, and one ``can_*`` entry per configurable capability. Read by
        ``materialize_group_access`` for new instances and by
        ``sync_type_group_access --update-existing`` to reconcile existing ones.
        """
        defaults = {"is_primary": True, "can_view": True}
        for action, field in self.OWNER_CAPABILITY_FIELDS:
            defaults[f"can_{action}"] = getattr(self, field)
        return defaults

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


class PublicStatus(BaseModel):
    """
    The simplified, citizen-facing status a state publishes as (BRS §12).

    One row per status ("Introduced", "Under Parliamentary Consideration", …),
    shared by every workflow type, so the public vocabulary and how it is
    presented (label, description, order, colour) are data an administrator can
    curate rather than a code enum. A :class:`State` maps to exactly one through
    ``public_status``: several internal committee stages can publish under one
    status while staying distinct internally.
    """

    name = models.CharField(max_length=100, unique=True)
    slug = AutoSlugField(populate_from="name", unique=True, editable=False)
    description = models.TextField(
        blank=True,
        help_text="Citizen-facing explanation of what this status means.",
    )
    order = models.IntegerField(
        default=0, help_text="Display order on public-facing views."
    )
    color = models.CharField(
        max_length=7, default="#5b8f22", help_text="Hex color code"
    )

    class Meta:
        ordering = ["order", "name"]
        verbose_name = "public status"
        verbose_name_plural = "public statuses"

    def __str__(self):
        return self.name
