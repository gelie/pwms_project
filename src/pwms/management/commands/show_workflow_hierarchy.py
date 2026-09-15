"""
Print the parent/child hierarchy of concrete workflow instances.

Concrete workflow types live in separate tables, so there is no single
``Workflow`` model to query: the command gathers the four registers on the
unified list and links them through :class:`WorkflowRelationship`, which every
instance exposes as ``parent_workflow`` / ``sub_workflows``.
"""

from django.core.management.base import BaseCommand, CommandError

from pwms.models import (
    Bill,
    DelegationReport,
    InternationalAgreement,
    InternationalResolution,
)

#: Concrete workflow models, in the same display order as the unified register.
WORKFLOW_MODELS = (
    DelegationReport,
    InternationalResolution,
    InternationalAgreement,
    Bill,
)


class Command(BaseCommand):
    help = "Display workflow hierarchies in a tree format"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-id",
            type=str,
            help="Show the hierarchy for one workflow, by its public UUID",
        )
        parser.add_argument(
            "--show-all",
            action="store_true",
            help="Show every hierarchy (root workflows and their descendants)",
        )
        parser.add_argument(
            "--show-orphans",
            action="store_true",
            help="Show workflows without parent or children",
        )
        parser.add_argument(
            "--workflow-type",
            type=str,
            help="Filter by workflow type name",
        )

    def handle(self, *args, **options):
        workflow_id = options["workflow_id"]
        workflow_type = options["workflow_type"]

        if workflow_id:
            self.show_specific_workflow(workflow_id)
        elif options["show_all"]:
            self.show_all_hierarchies(workflow_type)
        elif options["show_orphans"]:
            self.show_orphan_workflows(workflow_type)
        else:
            self.show_root_workflows(workflow_type)

    @staticmethod
    def _key(instance):
        """Identity of an instance, comparable across the concrete tables."""
        return (instance._instance_ct().pk, instance.pk)

    def _instances(self, workflow_type_filter=None):
        """Every concrete workflow instance, optionally filtered by type name."""
        instances = []
        for model in WORKFLOW_MODELS:
            queryset = model.objects.select_related(
                "workflow_type", "current_state", "owner"
            )
            if workflow_type_filter:
                queryset = queryset.filter(
                    workflow_type__name__icontains=workflow_type_filter
                )
            instances.extend(queryset)
        return instances

    def _find_instance(self, workflow_id):
        """Locate one instance by its public UUID, or raise ``CommandError``."""
        for model in WORKFLOW_MODELS:
            instance = model.objects.filter(public_id=workflow_id).first()
            if instance is not None:
                return instance
        raise CommandError(f"No workflow with public id {workflow_id}.")

    def show_specific_workflow(self, workflow_id):
        workflow = self._find_instance(workflow_id)

        self.stdout.write(self.style.SUCCESS(f"\nWorkflow hierarchy for: {workflow}"))
        self.stdout.write("=" * 60)

        # Root-first path from the top of the tree down to this instance.
        self.stdout.write("\nHierarchy path:")
        for depth, node in enumerate(workflow.get_workflow_hierarchy_path()):
            indent = "  " * depth
            marker = "└─ " if depth else "📁 "
            self.stdout.write(f"{indent}{marker}{self.style_workflow_status(node)}")

        parent = workflow.parent_workflow
        if parent is not None:
            self.stdout.write(f"\n📤 Parent: {parent}")
            self.stdout.write(
                f"   Relationship: {workflow.relationship_type or 'Not specified'}"
            )

            siblings = [
                child
                for child in parent.sub_workflows
                if self._key(child) != self._key(workflow)
            ]
            if siblings:
                self.stdout.write("\n👥 Siblings:")
                for sibling in siblings:
                    self.stdout.write(f"   • {sibling}")

        if workflow.has_sub_workflows:
            self.stdout.write("\n📥 Sub-workflows:")
            self.display_workflow_tree(workflow, indent_level=1)

        self.stdout.write("\n📊 Summary:")
        self.stdout.write(f"   • Hierarchy level: {workflow.hierarchy_level}")
        self.stdout.write(f"   • Is root: {workflow.is_root_workflow}")
        self.stdout.write(f"   • Has children: {workflow.has_sub_workflows}")
        self.stdout.write(
            f"   • Total descendants: {len(workflow.get_all_descendants())}"
        )

    def show_root_workflows(self, workflow_type_filter=None):
        roots = [
            instance
            for instance in self._instances(workflow_type_filter)
            if instance.is_root_workflow
        ]

        if not roots:
            self.stdout.write(self.style.WARNING("No root workflows found."))
            return

        self.stdout.write(self.style.SUCCESS("\nRoot Workflows (with hierarchies):"))
        self.stdout.write("=" * 50)

        for root in roots:
            self.stdout.write(f"\n📁 {self.style_workflow_status(root)}")
            self.display_workflow_tree(root, indent_level=1)

    def show_all_hierarchies(self, workflow_type_filter=None):
        roots = [
            instance
            for instance in self._instances(workflow_type_filter)
            if instance.is_root_workflow
        ]

        if not roots:
            self.stdout.write(self.style.WARNING("No workflows found."))
            return

        self.stdout.write(self.style.SUCCESS("\nAll Workflow Hierarchies:"))
        self.stdout.write("=" * 50)

        for root in roots:
            self.stdout.write(f"\n📁 {self.style_workflow_status(root)}")
            self.display_workflow_tree(root, indent_level=1)

            descendants = root.get_all_descendants()
            if descendants:
                self.stdout.write(f"   📊 Total descendants: {len(descendants)}")

    def show_orphan_workflows(self, workflow_type_filter=None):
        orphans = [
            instance
            for instance in self._instances(workflow_type_filter)
            if instance.is_root_workflow and not instance.has_sub_workflows
        ]

        if not orphans:
            self.stdout.write(self.style.WARNING("No orphan workflows found."))
            return

        self.stdout.write(
            self.style.SUCCESS("\nOrphan Workflows (no parent or children):")
        )
        self.stdout.write("=" * 50)

        for workflow in orphans:
            self.stdout.write(f"• {self.style_workflow_status(workflow)}")

    def display_workflow_tree(self, workflow, indent_level=0):
        """Recursively display ``workflow``'s children as a tree."""
        children = workflow.sub_workflows
        for position, child in enumerate(children):
            indent = "  " * indent_level
            marker = "└─ " if position == len(children) - 1 else "├─ "
            relationship = (
                f" ({child.relationship_type})" if child.relationship_type else ""
            )
            self.stdout.write(
                f"{indent}{marker}{child.workflow_type.name}: "
                f"{self.style_workflow_status(child)}{relationship}"
            )

            if child.has_sub_workflows:
                self.display_workflow_tree(child, indent_level + 1)

    def style_workflow_status(self, workflow):
        """Colour a workflow by its status: overdue first, then priority."""
        if workflow.is_overdue:
            return self.style.ERROR(str(workflow))
        if workflow.priority == "urgent":
            return self.style.WARNING(str(workflow))
        if workflow.priority == "high":
            return self.style.HTTP_INFO(str(workflow))
        return str(workflow)
