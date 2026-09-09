from django.core.management.base import BaseCommand, CommandError

from workflows.models import Workflow


class Command(BaseCommand):
    help = "Display workflow hierarchies in a tree format"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-id",
            type=str,
            help="Show hierarchy for a specific workflow UUID",
        )
        parser.add_argument(
            "--show-all",
            action="store_true",
            help="Show all workflow hierarchies (root workflows and their descendants)",
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
        show_all = options["show_all"]
        show_orphans = options["show_orphans"]
        workflow_type = options["workflow_type"]

        if workflow_id:
            self.show_specific_workflow(workflow_id)
        elif show_all:
            self.show_all_hierarchies(workflow_type)
        elif show_orphans:
            self.show_orphan_workflows(workflow_type)
        else:
            self.show_root_workflows(workflow_type)

    def show_specific_workflow(self, workflow_id):
        try:
            workflow = Workflow.objects.get(id=workflow_id)
        except Workflow.DoesNotExist:
            raise CommandError(f"Workflow with ID {workflow_id} does not exist.")

        self.stdout.write(self.style.SUCCESS(f"\nWorkflow Hierarchy for: {workflow}"))
        self.stdout.write("=" * 60)

        # Show the full path from root to this workflow
        hierarchy_path = workflow.get_workflow_hierarchy_path()
        self.stdout.write("\nHierarchy Path:")
        for i, wf in enumerate(hierarchy_path):
            indent = "  " * i
            marker = "└─ " if i > 0 else "📁 "
            self.stdout.write(f"{indent}{marker}{wf}")

        # Show parent information
        if workflow.parent_workflow:
            self.stdout.write(f"\n📤 Parent: {workflow.parent_workflow}")
            self.stdout.write(
                f"   Relationship: {workflow.relationship_type or 'Not specified'}"
            )

        # Show siblings
        if workflow.parent_workflow:
            siblings = workflow.parent_workflow.sub_workflows.exclude(id=workflow.id)
            if siblings.exists():
                self.stdout.write("\n👥 Siblings:")
                for sibling in siblings:
                    self.stdout.write(f"   • {sibling}")

        # Show children
        if workflow.has_sub_workflows:
            self.stdout.write("\n📥 Sub-workflows:")
            self.display_workflow_tree(workflow, indent_level=1)

        # Show summary
        self.stdout.write(f"\n📊 Summary:")
        self.stdout.write(f"   • Hierarchy Level: {workflow.hierarchy_level}")
        self.stdout.write(f"   • Is Root: {workflow.is_root_workflow}")
        self.stdout.write(f"   • Has Children: {workflow.has_sub_workflows}")
        self.stdout.write(
            f"   • Total Descendants: {len(workflow.get_all_descendants())}"
        )

    def show_root_workflows(self, workflow_type_filter=None):
        queryset = Workflow.objects.filter(parent_workflow__isnull=True)

        if workflow_type_filter:
            queryset = queryset.filter(
                workflow_type__name__icontains=workflow_type_filter
            )

        root_workflows = queryset.select_related(
            "workflow_type", "current_state", "owner"
        )

        if not root_workflows.exists():
            self.stdout.write(self.style.WARNING("No root workflows found."))
            return

        self.stdout.write(self.style.SUCCESS("\nRoot Workflows (with hierarchies):"))
        self.stdout.write("=" * 50)

        for root in root_workflows:
            self.stdout.write(f"\n📁 {root}")
            self.display_workflow_tree(root, indent_level=1)

    def show_all_hierarchies(self, workflow_type_filter=None):
        self.stdout.write(self.style.SUCCESS("\nAll Workflow Hierarchies:"))
        self.stdout.write("=" * 50)

        root_workflows = Workflow.objects.filter(parent_workflow__isnull=True)

        if workflow_type_filter:
            root_workflows = root_workflows.filter(
                workflow_type__name__icontains=workflow_type_filter
            )

        root_workflows = root_workflows.select_related("workflow_type", "current_state")

        if not root_workflows.exists():
            self.stdout.write(self.style.WARNING("No workflows found."))
            return

        for root in root_workflows:
            self.stdout.write(f"\n📁 {root}")
            self.display_workflow_tree(root, indent_level=1)

            # Show stats
            descendants = root.get_all_descendants()
            if descendants:
                self.stdout.write(f"   📊 Total descendants: {len(descendants)}")

    def show_orphan_workflows(self, workflow_type_filter=None):
        orphan_workflows = Workflow.objects.filter(
            parent_workflow__isnull=True, sub_workflows__isnull=True
        )

        if workflow_type_filter:
            orphan_workflows = orphan_workflows.filter(
                workflow_type__name__icontains=workflow_type_filter
            )

        orphan_workflows = orphan_workflows.distinct().select_related("workflow_type")

        if not orphan_workflows.exists():
            self.stdout.write(self.style.WARNING("No orphan workflows found."))
            return

        self.stdout.write(
            self.style.SUCCESS("\nOrphan Workflows (no parent or children):")
        )
        self.stdout.write("=" * 50)

        for workflow in orphan_workflows:
            self.stdout.write(f"• {workflow}")

    def display_workflow_tree(self, workflow, indent_level=0):
        """Recursively display workflow tree structure"""
        sub_workflows = workflow.sub_workflows.all().select_related(
            "workflow_type", "current_state"
        )

        for i, sub_workflow in enumerate(sub_workflows):
            indent = "  " * indent_level
            is_last = i == len(sub_workflows) - 1
            marker = "└─ " if is_last else "├─ "

            # Show relationship type if available
            relationship = (
                f" ({sub_workflow.relationship_type})"
                if sub_workflow.relationship_type
                else ""
            )

            self.stdout.write(
                f"{indent}{marker}{sub_workflow.workflow_type.name}: {sub_workflow.title}{relationship}"
            )

            # Recursively show sub-workflows
            if sub_workflow.has_sub_workflows:
                next_indent = indent_level + 1
                self.display_workflow_tree(sub_workflow, next_indent)

    def style_workflow_status(self, workflow):
        """Add color coding based on workflow status"""
        if workflow.is_overdue():
            return self.style.ERROR(str(workflow))
        elif workflow.priority == "urgent":
            return self.style.WARNING(str(workflow))
        elif workflow.priority == "high":
            return self.style.HTTP_INFO(str(workflow))
        else:
            return str(workflow)
