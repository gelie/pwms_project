import base64
import os

import graphviz
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch

from workflows.models import State, Transition, WorkflowType


class Command(BaseCommand):
    help = "Generate visual diagrams of WorkflowTypes, including transitions and states"

    def add_arguments(self, parser):
        parser.add_argument(
            "--workflow-type",
            type=str,
            help="Generate diagram for specific workflow type (name). If not provided, generates diagrams for all workflow types.",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default=str(
                getattr(settings, "WORKFLOW_DIAGRAM_OUTPUT_DIR", "workflow_diagrams")
            ),
            help="Output directory for generated diagrams",
        )
        parser.add_argument(
            "--format",
            type=str,
            choices=["dot", "png", "svg", "pdf"],
            default="svg",
            help="Output format for diagrams (default: svg)",
        )
        parser.add_argument(
            "--show-roles",
            action="store_true",
            help="Show role permissions on transitions",
        )
        parser.add_argument(
            "--cluster-states",
            action="store_true",
            help="Group states by type (initial, terminal, regular)",
        )
        parser.add_argument(
            "--include-descriptions",
            action="store_true",
            help="Include state and transition descriptions in the diagram",
        )

    def handle(self, *args, **options):
        workflow_type_name = options["workflow_type"]
        output_dir = options["output_dir"]
        output_format = options["format"]
        show_roles = options["show_roles"]
        cluster_states = options["cluster_states"]
        include_descriptions = options["include_descriptions"]

        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Get workflow types to process
        if workflow_type_name:
            workflow_types = WorkflowType.objects.filter(
                name=workflow_type_name, enabled=True
            )
            if not workflow_types.exists():
                raise CommandError(
                    f'WorkflowType "{workflow_type_name}" does not exist or is not enabled.'
                )
        else:
            workflow_types = WorkflowType.objects.filter(enabled=True)

        if not workflow_types:
            self.stdout.write(self.style.WARNING("No workflow types found."))
            return

        # Generate diagrams
        generated_count = 0
        skipped_count = 0

        for workflow_type in workflow_types:
            self.stdout.write(f"Processing: {workflow_type.name}")

            # Prefetch related data for efficiency
            workflow_type = WorkflowType.objects.prefetch_related(
                Prefetch("states", queryset=State.objects.order_by("order", "name")),
                Prefetch(
                    "transitions",
                    queryset=Transition.objects.order_by(
                        "order", "name"
                    ).prefetch_related("allowed_roles"),
                ),
            ).get(id=workflow_type.id)

            # Skip if no states (check prefetched data)
            states_list = list(workflow_type.states.all())
            if not states_list:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Skipped: No states configured for '{workflow_type.name}'"
                    )
                )
                skipped_count += 1
                continue

            # Create and render diagram
            try:
                dot = self.create_diagram_graph(
                    workflow_type,
                    show_roles=show_roles,
                    cluster_states=cluster_states,
                    include_descriptions=include_descriptions,
                )

                # Render diagram to file
                filename = f"{workflow_type.name.lower().replace(' ', '_')}_workflow"
                filepath = os.path.join(output_dir, filename)

                # Render the diagram
                rendered_path = dot.render(filepath, format=output_format, cleanup=True)
                self.stdout.write(
                    self.style.SUCCESS(f"  Diagram saved to: {rendered_path}")
                )
                generated_count += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Failed to render diagram: {e}"))
                skipped_count += 1

        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {generated_count} diagram(s) in {output_dir}/"
            )
        )
        if skipped_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {skipped_count} workflow type(s) (no states configured)"
                )
            )
        self.stdout.write("=" * 60)

    def create_diagram_graph(
        self,
        workflow_type: WorkflowType,
        show_roles: bool = False,
        cluster_states: bool = False,
        include_descriptions: bool = False,
    ) -> "graphviz.Digraph":
        """Create a professional-grade graphviz Digraph for a workflow type"""
        states = list(workflow_type.states.all())
        transitions = list(workflow_type.transitions.all())

        # Create the main graph with professional settings
        dot = graphviz.Digraph(
            workflow_type.name.replace(" ", "_"),
            comment=f"Workflow: {workflow_type.name}",
            format="png",  # Will be overridden by command line
        )

        # === ENTERPRISE-GRADE GLOBAL STYLING ===
        dot.attr(
            rankdir="TB",
            # splines="ortho",          # Clean orthogonal lines
            ranksep="1",
            nodesep="0.7",
            bgcolor="white",
            pad="0.6",
            compound="true",  # Better cluster support
        )

        # Global node styling
        dot.attr(
            "node",
            shape="box",
            style="filled,rounded",
            fontname="Helvetica",
            fontsize="12",
            fontweight="bold",
            margin="0.18,0.11",
            penwidth="2",
        )

        # Global edge styling
        dot.attr(
            "edge",
            fontname="Helvetica",
            fontsize="12",
            fontweight="bold",
            arrowsize="1.2",
            penwidth="1.5",
            color="#2C3E50",
            labeldistance="3",
            labelangle="0",
            splines="true",
        )

        # === HEADER WITH PARLIAMENT LOGO ===
        logo_path = (
            "/home/gelie/Projects/pwms/parliament-logo.png"  # Adjust if needed
        )

        # with open(logo_path, "rb") as f:
        #     b64 = base64.b64encode(f.read()).decode("ascii")
        # img_uri = f"data:image/png;base64,{b64}"

        # single HTML node for header (image left, text right)
        label = f'''<
            <TABLE BORDER="0" CELLBORDER="0" CELLSPACING="3">
                <TR>
                <TD><IMG SRC="{logo_path}" SCALE="TRUE" /></TD>
                <TD></TD><TD></TD><TD></TD>
                <TD><FONT POINT-SIZE="18"><B>{workflow_type.name}</B></FONT></TD>
                </TR>
            </TABLE>
        >'''

        dot.node("header", label=label, shape="none")
        # dot.edge('header', initial_states[0].name)

        # dot.attr(label=f'<<B>{workflow_type.name}</B><BR/><IMG SRC="{img_uri}"/>', labelloc='t', fontsize='18', fontname='Helvetica', margin='0.5')

        # Create a header subgraph
        # with dot.subgraph(name="cluster_header") as header:
        #     header.attr(rank="min", style="invis", label="", rankdir="LR")
        #     # header.attr("graph", nodesep="0.1")

        #     # Logo node
        #     header.node(
        #         "logo",
        #         label="",
        #         image=logo_path,
        #         shape="none",
        #         style="filled",
        #         fillcolor="#ffffff",
        #         width="2.2",
        #         height="1.1",
        #         fixedsize="true",
        #         # style="invis",          # Hide border
        #         # penwidth="0",
        #         margin="0",
        #         imagescale="false"
        #     )

        #     # Title next to logo
        #     title = f"{workflow_type.name}"
        #     if include_descriptions and workflow_type.description:
        #         title += f"\\n{workflow_type.description}"

        #     header.node(
        #         "workflow_title",
        #         label=f'<<B><FONT POINT-SIZE="18" FACE="Arial">{title}</FONT></B>>',
        #         shape="plain",
        #         style="filled",
        #         fillcolor="transparent",
        #         fontcolor="#1C2526"
        #     )

        # Categorize states
        initial_states = [s for s in states if s.is_initial]
        terminal_states = [s for s in states if s.is_terminal]
        regular_states = [s for s in states if not s.is_initial and not s.is_terminal]

        # Add states with clusters
        if cluster_states:
            if initial_states:
                with dot.subgraph(name="cluster_initial") as c:
                    c.attr(
                        label="",
                        style="filled",
                        color="#E8F5E9",
                        fontname="Arial",
                        fontsize="12",
                        fontcolor="#2E7D32",
                    )
                    for state in initial_states:
                        self._add_state_node(c, state, include_descriptions)

            if regular_states:
                with dot.subgraph(name="cluster_regular") as c:
                    c.attr(
                        label="",
                        style="filled",
                        color="#E3F2FD",
                        fontname="Arial",
                        fontsize="12",
                        fontcolor="#1565C0",
                    )
                    for state in regular_states:
                        self._add_state_node(c, state, include_descriptions)

            if terminal_states:
                with dot.subgraph(name="cluster_terminal") as c:
                    c.attr(
                        label="",
                        style="filled",
                        color="#FFEBEE",
                        fontname="Arial",
                        fontsize="12",
                        fontcolor="#C62828",
                    )
                    for state in terminal_states:
                        self._add_state_node(c, state, include_descriptions)
        else:
            for state in states:
                self._add_state_node(dot, state, include_descriptions)

        # Add invisible edge from header to first initial state for layout
        dot.edge("header", initial_states[0].name, style="invis")

        # Add transitions
        for transition in transitions:
            self._add_transition_edge(dot, transition, show_roles, include_descriptions)

        return dot

    def _add_state_node(self, dot, state: State, include_descriptions: bool = False):
        """Add a professionally styled state node"""
        label = state.name

        if include_descriptions and state.description:
            desc = state.description[:70]
            if len(state.description) > 70:
                desc += "..."
            label = f"{label}\\n\\n{desc}"

        # Professional color scheme
        if state.color and state.color != "#6B7280":
            fillcolor = state.color
            fontcolor = "white"
        elif state.is_initial:
            fillcolor = "#5B8F22"  # Professional green
            fontcolor = "white"
        elif state.is_terminal:
            fillcolor = "#CC4B37"  # Professional red
            fontcolor = "white"
        else:
            fillcolor = "#1565C0"  # Professional blue
            fontcolor = "white"

        # Shape hierarchy
        if state.is_initial:
            shape = "ellipse"
            style = "filled"
            fillcolor = "#5B8F22"
        elif state.is_terminal:
            shape = "doubleoctagon"
            style = "filled"
            fillcolor = "#CC4B37"
        else:
            shape = "box"
            style = "filled,rounded"
            fillcolor = "#1565C0"

        dot.node(
            state.name,
            label=label,
            fillcolor=fillcolor,
            fontcolor=fontcolor,
            shape=shape,
            style=style,
            penwidth="2.8",
            width="2.0",
            height="0.9",
        )

    def _add_transition_edge(
        self,
        dot,
        transition: Transition,
        show_roles: bool = False,
        include_descriptions: bool = False,
    ):
        """Add a professionally styled transition"""
        label = transition.name

        if include_descriptions and transition.requires_comment:
            label += "\\n(requires comment)"

        if show_roles and transition.allowed_roles.exists():
            roles = ", ".join([role.name for role in transition.allowed_roles.all()])
            label += f"\\n[{roles}]"

        edge_attrs = {
            "label": label,
            "color": "#455A64",
            "fontcolor": "#455A64",
        }

        if transition.requires_comment:
            edge_attrs.update(
                {"style": "dashed", "color": "#275937", "fontcolor": "#275937"} # #F57C00 - original
            )

        dot.edge(transition.from_state.name, transition.to_state.name, **edge_attrs)

    def generate_summary_report(self, output_dir: str):
        """Generate a summary report of all workflow types"""
        filepath = os.path.join(output_dir, "workflow_summary.txt")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("Workflow Types Summary\n")
            f.write("=====================\n\n")

            for workflow_type in WorkflowType.objects.all().prefetch_related(
                "states", "transitions"
            ):
                f.write(f"Workflow Type: {workflow_type.name}\n")
                if workflow_type.description:
                    f.write(f"Description: {workflow_type.description}\n")

                states = workflow_type.states.all()
                transitions = workflow_type.transitions.all()

                f.write(f"States: {len(states)}\n")
                for state in states:
                    state_type = []
                    if state.is_initial:
                        state_type.append("initial")
                    if state.is_terminal:
                        state_type.append("terminal")
                    state_type_str = f" ({', '.join(state_type)})" if state_type else ""
                    f.write(f"  - {state.name}{state_type_str}\n")

                f.write(f"Transitions: {len(transitions)}\n")
                for transition in transitions:
                    f.write(
                        f"  - {transition.name}: {transition.from_state.name} → {transition.to_state.name}\n"
                    )

                f.write("\n" + "-" * 50 + "\n\n")

        self.stdout.write(self.style.SUCCESS(f"Summary report saved to: {filepath}"))
