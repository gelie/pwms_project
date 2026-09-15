import os
from html import escape as escape_html

import graphviz
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch
from django.utils import timezone

from pwms.models import State, Transition, WorkflowType

# --- Visual language -------------------------------------------------------
# A single palette feeds both the Graphviz and Mermaid renderers so the two
# outputs stay visually consistent. Colours are deliberately muted for print.
FONT = "DejaVu Sans"

PALETTE = {
    "initial": {"fill": "#2F7D32", "border": "#1B5E20", "text": "#FFFFFF"},
    "regular": {"fill": "#1F4E9C", "border": "#163A73", "text": "#FFFFFF"},
    "terminal": {"fill": "#B23A30", "border": "#7F241D", "text": "#FFFFFF"},
    "custom": {"fill": "#334155", "border": "#1E293B", "text": "#FFFFFF"},
    "edge": "#475569",
    "comment": "#B45309",
    "muted": "#64748B",
    "heading": "#0F172A",
    "surface": "#F8FAFC",
    "rule": "#E2E8F0",
}

#: Colours treated as "not set" on State.color, i.e. fall back to the palette.
DEFAULT_STATE_COLORS = {"#6b7280", ""}


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
            default=str(getattr(settings, "WORKFLOW_DIAGRAM_OUTPUT_DIR", "diagrams")),
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
        parser.add_argument(
            "--no-legend",
            action="store_true",
            help="Omit the colour/shape legend from generated diagrams",
        )
        parser.add_argument(
            "--mermaid-dir",
            type=str,
            default=getattr(settings, "WORKFLOW_DIAGRAM_DOCS_DIR", None),
            help=(
                "Directory for Mermaid Markdown exports (defaults to "
                "WORKFLOW_DIAGRAM_DOCS_DIR when set, otherwise --output-dir)"
            ),
        )
        parser.add_argument(
            "--no-mermaid",
            action="store_true",
            help="Skip writing Mermaid Markdown alongside the rendered diagram",
        )

    def handle(self, *args, **options):
        workflow_type_name = options["workflow_type"]
        output_dir = options["output_dir"]
        output_format = options["format"]
        show_roles = options["show_roles"]
        cluster_states = options["cluster_states"]
        include_descriptions = options["include_descriptions"]
        show_legend = not options["no_legend"]
        export_mermaid = not options["no_mermaid"]
        mermaid_dir = options["mermaid_dir"] or output_dir

        # Create output directories if they don't exist
        os.makedirs(output_dir, exist_ok=True)
        if export_mermaid:
            os.makedirs(mermaid_dir, exist_ok=True)

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
                filename = f"{workflow_type.name.lower().replace(' ', '_')}_workflow"

                dot = self.create_diagram_graph(
                    workflow_type,
                    show_roles=show_roles,
                    cluster_states=cluster_states,
                    include_descriptions=include_descriptions,
                    show_legend=show_legend,
                )

                # Render the diagram. Raster output is rendered at a higher dpi
                # so PNGs stay crisp; vector formats keep their natural size.
                if output_format == "png":
                    dot.graph_attr["dpi"] = "200"

                rendered_path = dot.render(
                    os.path.join(output_dir, filename),
                    format=output_format,
                    cleanup=True,
                )
                self.stdout.write(
                    self.style.SUCCESS(f"  Diagram saved to: {rendered_path}")
                )

                if export_mermaid:
                    mermaid_path = self.write_mermaid(
                        workflow_type,
                        os.path.join(mermaid_dir, f"{filename}.md"),
                        show_roles=show_roles,
                        include_descriptions=include_descriptions,
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f"  Mermaid saved to: {mermaid_path}")
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
        if export_mermaid and generated_count:
            self.stdout.write(
                self.style.SUCCESS(f"Mermaid Markdown written to: {mermaid_dir}/")
            )
        if skipped_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {skipped_count} workflow type(s) "
                    "(no states configured or render failed)"
                )
            )
        self.stdout.write("=" * 60)

    def create_diagram_graph(
        self,
        workflow_type: WorkflowType,
        show_roles: bool = False,
        cluster_states: bool = False,
        include_descriptions: bool = False,
        show_legend: bool = True,
    ) -> graphviz.Digraph:
        """Create a print-ready graphviz Digraph for a workflow type."""
        states = list(workflow_type.states.all())
        transitions = list(workflow_type.transitions.all())

        initial_states = [s for s in states if s.is_initial]
        terminal_states = [s for s in states if s.is_terminal]
        regular_states = [s for s in states if not s.is_initial and not s.is_terminal]

        # Create the main graph with professional settings
        dot = graphviz.Digraph(
            workflow_type.slug or workflow_type.name.replace(" ", "_"),
            comment=f"Workflow: {workflow_type.name}",
            format="png",  # overridden by the --format option at render time
        )

        # === GLOBAL PAGE + TYPOGRAPHY ===
        dot.attr(
            rankdir="TB",
            ranksep="0.75",
            nodesep="0.45",
            bgcolor="white",
            pad="0.5",
            compound="true",  # Better cluster support
            splines="spline",
        )

        # Global node styling: thin borders in the node's own colour read far
        # cleaner than the old 2.8pt black outlines.
        dot.attr(
            "node",
            shape="box",
            style="filled,rounded",
            fontname=FONT,
            fontsize="11",
            margin="0.20,0.12",
            penwidth="1.2",
        )

        # Global edge styling
        dot.attr(
            "edge",
            fontname=FONT,
            fontsize="10",
            arrowsize="0.8",
            arrowhead="vee",
            penwidth="1.3",
            color=PALETTE["edge"],
            fontcolor=PALETTE["edge"],
            labeldistance="1.6",
        )

        # === HEADER (logo + title + optional description) ===
        self._add_header(dot, workflow_type)

        # Add states (clustered only where a group actually contains >1 state)
        if cluster_states:
            self._add_state_clusters(
                dot,
                initial_states,
                regular_states,
                terminal_states,
                include_descriptions,
            )
        else:
            for state in states:
                self._add_state_node(dot, state, include_descriptions)

        # Anchor the header above the entry point so it stays on top.
        anchor = initial_states[0] if initial_states else states[0]
        dot.edge("header", anchor.name, style="invis")

        # Add transitions
        for transition in transitions:
            self._add_transition_edge(dot, transition, show_roles, include_descriptions)

        # Add legend + provenance caption
        if show_legend:
            self._add_legend(dot, initial_states, regular_states, terminal_states)
        self._add_footer(dot, workflow_type, states, transitions)

        return dot

    # -- header / clusters / legend / footer -------------------------------
    def _add_header(self, dot, workflow_type: WorkflowType):
        """Add a chrome-free logo + title banner with a hairline rule."""
        logo_path = os.path.join(
            str(settings.BASE_DIR),
            "src",
            "pwms",
            "static",
            "images",
            "parliament-logo.png",
        )

        title = escape_html(workflow_type.name)
        description = escape_html(" ".join(workflow_type.description.split()))
        if len(description) > 110:
            description = description[:107] + "..."

        description_row = ""
        if description:
            description_row = (
                '<TR><TD ALIGN="LEFT">'
                f'<FONT POINT-SIZE="9" COLOR="{PALETTE["muted"]}">{description}'
                "</FONT></TD></TR>"
            )

        # NOTE: whitespace inside a <TD> is emitted as non-breaking spaces by
        # Graphviz, so every cell is written on one line with no indentation.
        label = (
            "<\n"
            '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2">\n'
            "<TR>"
            '<TD ROWSPAN="3" ALIGN="CENTER" VALIGN="MIDDLE" WIDTH="52">'
            f'<IMG SRC="{logo_path}" SCALE="TRUE"/></TD>'
            f'<TD ALIGN="LEFT"><FONT POINT-SIZE="17" COLOR="{PALETTE["heading"]}">'
            f"<B>{title}</B></FONT></TD>"
            "</TR>\n"
            '<TR><TD ALIGN="LEFT">'
            f'<FONT POINT-SIZE="9" COLOR="{PALETTE["muted"]}">Workflow state machine</FONT>'
            "</TD></TR>\n"
            f"{description_row}\n"
            f'<TR><TD COLSPAN="2" HEIGHT="2" BGCOLOR="{PALETTE["rule"]}"></TD></TR>\n'
            "</TABLE>\n"
            ">"
        )

        # shape=none alone is not enough: without an explicit transparent fill
        # the node inherits the global "filled,rounded" style and paints a grey
        # pill behind the banner.
        dot.node(
            "header",
            label=label,
            shape="none",
            style="filled",
            fillcolor="transparent",
            color="transparent",
            penwidth="0",
            margin="0",
        )

    def _add_state_clusters(
        self,
        dot,
        initial_states,
        regular_states,
        terminal_states,
        include_descriptions: bool,
    ):
        """Group states into zones, but only when a zone holds more than one.

        Single-state zones used to render as a pastel box hugging one node,
        which read as a stray highlight rather than a grouping, so those states
        are emitted without a cluster.
        """
        groups = (
            ("cluster_initial", "Entry", initial_states, PALETTE["initial"]),
            ("cluster_regular", "In progress", regular_states, PALETTE["regular"]),
            ("cluster_terminal", "Closed", terminal_states, PALETTE["terminal"]),
        )
        for name, label, group_states, accent in groups:
            if len(group_states) < 2:
                for state in group_states:
                    self._add_state_node(dot, state, include_descriptions)
                continue

            with dot.subgraph(name=name) as cluster:
                cluster.attr(
                    label=label,
                    labeljust="l",
                    style="rounded,filled",
                    fillcolor=PALETTE["surface"],
                    color=PALETTE["rule"],
                    fontname=FONT,
                    fontsize="10",
                    fontcolor=accent["border"],
                    penwidth="1",
                    margin="10",
                )
                for state in group_states:
                    self._add_state_node(cluster, state, include_descriptions)

    def _add_legend(self, dot, initial_states, regular_states, terminal_states):
        """Add a legend that mirrors the workflow's own role colours.

        Colours come from the states actually in this workflow rather than the
        palette defaults, because ``State.color`` is curated per workflow (for
        example the amber-to-dark-green progression used by Delegation Report).
        The legend therefore always matches the nodes it explains.
        """

        def representative(states, role):
            for state in states:
                if state.color and state.color.lower() not in DEFAULT_STATE_COLORS:
                    return state.color
            return PALETTE[role]["fill"]

        def swatch(color: str) -> str:
            return f'<TD WIDTH="14" HEIGHT="11" BGCOLOR="{color}"></TD>'

        def item(text: str) -> str:
            return (
                '<TD ALIGN="LEFT">'
                f'<FONT POINT-SIZE="9" COLOR="{PALETTE["muted"]}">{text}</FONT></TD>'
            )

        entries = (
            (representative(initial_states, "initial"), "Initial"),
            (representative(regular_states, "regular"), "In progress"),
            (representative(terminal_states, "terminal"), "Terminal"),
        )

        # Cells are kept on single lines because Graphviz renders the leading
        # indentation inside a <TD> as literal non-breaking spaces.
        label = (
            "<\n"
            f'<TABLE BORDER="1" COLOR="{PALETTE["rule"]}" '
            f'BGCOLOR="{PALETTE["surface"]}" CELLBORDER="0" CELLSPACING="8" '
            'CELLPADDING="4">\n'
            '<TR><TD COLSPAN="7" ALIGN="LEFT">'
            f'<FONT POINT-SIZE="9" COLOR="{PALETTE["muted"]}"><B>Legend</B></FONT>'
            "</TD></TR>\n"
            "<TR>"
            f"{swatch(entries[0][0])}{item(entries[0][1])}"
            f"{swatch(entries[1][0])}{item(entries[1][1])}"
            f"{swatch(entries[2][0])}{item(entries[2][1])}"
            f'<TD ALIGN="LEFT"><FONT POINT-SIZE="9" COLOR="{PALETTE["comment"]}">'
            "- - - requires comment</FONT></TD>"
            "</TR>\n"
            "</TABLE>\n"
            ">"
        )

        # A plain rank=sink subgraph (rather than a cluster) is what reliably
        # pins the legend below the flow; rank=sink on a cluster is not honoured.
        with dot.subgraph() as sink:
            sink.attr(rank="sink")
            sink.node(
                "legend",
                label=label,
                shape="plaintext",
                style="filled",
                fillcolor="transparent",
                color="transparent",
                penwidth="0",
                margin="0",
            )

    def _add_footer(self, dot, workflow_type, states, transitions):
        """Add a subtle provenance caption along the bottom edge."""
        dot.attr(
            label=(
                f"{workflow_type.name}  ·  {len(states)} states  ·  "
                f"{len(transitions)} transitions  ·  generated "
                f"{timezone.localdate().isoformat()}"
            ),
            labelloc="b",
            labeljust="c",
            fontname=FONT,
            fontsize="9",
            fontcolor=PALETTE["muted"],
        )

    def _add_state_node(self, dot, state: State, include_descriptions: bool = False):
        """Add a state node using a consistent shape and colour language.

        Shape encodes the state's role (ellipse = entry, rounded box = active,
        double-bordered box = terminal) so the diagram survives greyscale and
        colour-blind printing, not just the palette.
        """
        if state.is_initial:
            role = "initial"
        elif state.is_terminal:
            role = "terminal"
        else:
            role = "regular"

        if state.color and state.color.lower() not in DEFAULT_STATE_COLORS:
            fill = state.color
            border = self._darken(state.color)
            text = self._contrast_text(fill)
        else:
            fill = PALETTE[role]["fill"]
            border = PALETTE[role]["border"]
            text = PALETTE[role]["text"]

        label = state.name
        if include_descriptions and state.description:
            desc = " ".join(state.description.split())
            if len(desc) > 80:
                desc = desc[:77] + "..."
            label = f"{label}\\n\\n{desc}"

        if role == "initial":
            shape, style, peripheries = "ellipse", "filled", "1"
        elif role == "terminal":
            shape, style, peripheries = "box", "filled,rounded", "2"
        else:
            shape, style, peripheries = "box", "filled,rounded", "1"

        dot.node(
            state.name,
            label=label,
            shape=shape,
            style=style,
            fillcolor=fill,
            color=border,
            fontcolor=text,
            fontname=FONT,
            fontsize="11",
            penwidth="1.2",
            peripheries=peripheries,
            margin="0.24,0.14",
        )

    @staticmethod
    def _darken(hex_color: str, factor: float = 0.75) -> str:
        """Return a darker border tone derived from a custom state colour."""
        value = (hex_color or "").lstrip("#")
        if len(value) == 3:
            value = "".join(channel * 2 for channel in value)
        if len(value) != 6:
            return PALETTE["custom"]["border"]
        try:
            channels = [int(value[i : i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            return PALETTE["custom"]["border"]
        return "#" + "".join(
            f"{max(0, min(255, round(channel * factor))):02X}" for channel in channels
        )

    @staticmethod
    def _contrast_text(hex_color: str) -> str:
        """Pick near-black or white text for legibility against a fill colour."""
        value = (hex_color or "").lstrip("#")
        if len(value) == 3:
            value = "".join(channel * 2 for channel in value)
        if len(value) != 6:
            return PALETTE["custom"]["text"]
        try:
            red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return PALETTE["custom"]["text"]
        # Rec. 601 luma is close enough to perceived brightness for this use.
        luma = 0.299 * red + 0.587 * green + 0.114 * blue
        return PALETTE["heading"] if luma > 150 else "#FFFFFF"

    def _add_transition_edge(
        self,
        dot,
        transition: Transition,
        show_roles: bool = False,
        include_descriptions: bool = False,
    ):
        """Add a transition edge; commenting guards render as amber dashes."""
        label = transition.name

        if include_descriptions and transition.requires_comment:
            label += "\\n(comment required)"

        if show_roles and transition.allowed_roles.exists():
            roles = ", ".join(role.name for role in transition.allowed_roles.all())
            label += f"\\n[{roles}]"

        edge_attrs = {
            "label": label,
            "color": PALETTE["edge"],
            "fontcolor": PALETTE["edge"],
            "fontname": FONT,
            "fontsize": "10",
        }

        if transition.requires_comment:
            edge_attrs.update(
                {
                    "style": "dashed",
                    "color": PALETTE["comment"],
                    "fontcolor": PALETTE["comment"],
                }
            )

        dot.edge(transition.from_state.name, transition.to_state.name, **edge_attrs)

    # -- Mermaid export ----------------------------------------------------
    @staticmethod
    def _mermaid_escape(text: str) -> str:
        """Escape text for a Mermaid node or edge label."""
        if not text:
            return ""
        escaped = (
            text.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return escaped.replace("\\n", "<br/>").replace("\n", "<br/>")

    def _build_mermaid(
        self,
        workflow_type: WorkflowType,
        show_roles: bool = False,
        include_descriptions: bool = False,
    ) -> str:
        """Render a workflow as a Mermaid ``flowchart`` definition.

        The class names mirror :data:`PALETTE` so the Markdown and image
        exports stay visually consistent.
        """
        states = list(workflow_type.states.all())
        transitions = list(workflow_type.transitions.all())

        state_ids = {state.pk: f"s{index}" for index, state in enumerate(states)}

        lines = [
            "%% Generated by `manage.py generate_diagrams`; do not edit by hand.",
            f"%% Workflow: {workflow_type.name}",
            "flowchart TD",
        ]
        for role in ("initial", "regular", "terminal"):
            style = PALETTE[role]
            lines.append(
                f"    classDef {role} fill:{style['fill']},"
                f"stroke:{style['border']},color:{style['text']},stroke-width:1px"
            )

        for state in states:
            if state.is_initial:
                shape, css_class = '(["{}"])', "initial"
            elif state.is_terminal:
                shape, css_class = '[["{}"]]', "terminal"
            else:
                shape, css_class = '["{}"]', "regular"

            label = self._mermaid_escape(state.name)
            if include_descriptions and state.description:
                desc = " ".join(state.description.split())
                if len(desc) > 80:
                    desc = desc[:77] + "..."
                label += f"<br/>{self._mermaid_escape(desc)}"

            lines.append(
                f"    {state_ids[state.pk]}{shape.format(label)}:::{css_class}"
            )

        for transition in transitions:
            source = state_ids.get(transition.from_state_id)
            target = state_ids.get(transition.to_state_id)
            if source is None or target is None:
                continue

            label = self._mermaid_escape(transition.name)
            if include_descriptions and transition.requires_comment:
                label += "<br/>(comment required)"
            if show_roles and transition.allowed_roles.exists():
                roles = ", ".join(role.name for role in transition.allowed_roles.all())
                label += f"<br/>[{self._mermaid_escape(roles)}]"

            arrow = "-.->" if transition.requires_comment else "-->"
            lines.append(f'    {source} {arrow}|"{label}"| {target}')

        return "\n".join(lines) + "\n"

    def write_mermaid(
        self,
        workflow_type: WorkflowType,
        filepath: str,
        show_roles: bool = False,
        include_descriptions: bool = False,
    ) -> str:
        """Write a fenced Mermaid Markdown file and return its path."""
        diagram = self._build_mermaid(
            workflow_type,
            show_roles=show_roles,
            include_descriptions=include_descriptions,
        )
        with open(filepath, "w", encoding="utf-8") as handle:
            handle.write(f"# {workflow_type.name} workflow\n\n")
            if workflow_type.description:
                handle.write(f"{workflow_type.description.strip()}\n\n")
            handle.write("```mermaid\n")
            handle.write(diagram)
            handle.write("```\n")
        return filepath

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
                f.writelines(
                    f"  - {transition.name}: {transition.from_state.name} → {transition.to_state.name}\n"
                    for transition in transitions
                )

                f.write("\n" + "-" * 50 + "\n\n")

        self.stdout.write(self.style.SUCCESS(f"Summary report saved to: {filepath}"))
