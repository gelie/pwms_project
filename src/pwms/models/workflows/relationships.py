"""Generic parent/child links between concrete workflow instances."""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from ..base import BaseModel


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
        # Imported lazily: ``base`` imports this module at load time.
        from .base import AbstractLegislativeWorkflow

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
