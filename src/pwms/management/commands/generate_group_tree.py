"""
Render the ``Group`` hierarchy (django-mptt) as a diagram.

``Group`` is an :class:`mptt.models.MPTTModel`, so every row already carries the
whole tree in its ``tree_id`` / ``lft`` / ``rght`` / ``level`` columns. This
command reads those columns instead of walking ``parent`` foreign keys, which
keeps the number of queries flat no matter how deep the tree runs.

By default it draws **every** root group and its descendants; pass ``--group``
to root the drawing at one group and render only that group's subtree. Output is
Graphviz — ``svg`` (default), ``png``, ``pdf`` or the raw ``dot`` — and, unless
suppressed, a matching Mermaid Markdown file.

Usage:
    python manage.py generate_group_tree
    python manage.py generate_group_tree --group "Administration"
    python manage.py generate_group_tree --group parliament --format png
    python manage.py generate_group_tree --group-type committee --active-only
    python manage.py generate_group_tree --group "IRPD" --max-depth 2 --show-members
    python manage.py generate_group_tree --group "IRPD" --text
"""

import os
import re
from html import escape as escape_html

import graphviz
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q
from django.utils import timezone

from pwms.models import Group, GroupMembership

# --- Visual language -------------------------------------------------------
# Group types are folded into a handful of colour *families* so a wall of
# detail survives at a glance: the eye reads "these are committees, those are
# ministries" before it reads any individual label. Colours are deliberately
# muted and dark enough for white text, so they print and photocopy cleanly.
FONT = "DejaVu Sans"

PALETTE = {
    # "edge": "#94A3B8",
    "edge": "#275937",
    "rule": "#E2E8F0",
    "muted": "#64748B",
    # "muted": "#F47533",
    # "heading": "#0F172A",
    "heading": "#9E7C0C",
    "surface": "#F8FAFC",
}

#: Group type -> colour family. Types absent here fall back to ``"other"``.
#: Keep in step with ``Group.GROUP_TYPE_CHOICES``.
TYPE_FAMILIES = {
    "legislature": "chamber",
    "parliament": "chamber",
    "house": "chamber",
    "committee": "committee",
    "portfolio_committee": "committee",
    "select_committee": "committee",
    "special_committee": "committee",
    "public_accounts_committee": "committee",
    "internal_committee": "committee",
    "ad_hoc_committee": "committee",
    "joint_committee": "committee",
    "executive": "executive",
    "presidency": "executive",
    "ministry": "executive",
    "premier": "executive",
    "department": "executive",
    "administration": "administration",
    "office": "administration",
    "division": "administration",
    "section": "administration",
    "business_unit": "administration",
    "party": "political",
    "member": "political",
    "province": "local",
    "municipality": "local",
    "delegation": "delegation",
}

#: Fill / border / text per family, plus the human label used in the legend.
FAMILY_STYLE = {
    "chamber": {
        "fill": "#1F4E9C",
        "border": "#163A73",
        "text": "#FFFFFF",
        "label": "Legislature / Parliament",
    },
    "committee": {
        "fill": "#B45309",
        "border": "#7C3A02",
        "text": "#FFFFFF",
        "label": "Committee",
    },
    "executive": {
        "fill": "#2F7D32",
        "border": "#1B5E20",
        "text": "#FFFFFF",
        "label": "Executive / Ministry",
    },
    "administration": {
        # "fill": "#475569",
        # "border": "#334155",
        "fill": "#275937",
        "border": "#2D663F",
        "text": "#FFFFFF",
        "label": "Administration",
    },
    "political": {
        "fill": "#6D28D9",
        "border": "#4C1D95",
        "text": "#FFFFFF",
        "label": "Party / Member",
    },
    "local": {
        "fill": "#0F766E",
        "border": "#115E59",
        "text": "#FFFFFF",
        "label": "Province / Municipality",
    },
    "delegation": {
        "fill": "#B23A30",
        "border": "#7F241D",
        "text": "#FFFFFF",
        "label": "Delegation",
    },
    "other": {
        "fill": "#334155",
        "border": "#1E293B",
        "text": "#FFFFFF",
        "label": "Other",
    },
}

DEFAULT_FAMILY = "other"

#: The full set of valid group types, used to reject typos in ``--group-type``.
VALID_GROUP_TYPES = {choice for choice, _ in Group.GROUP_TYPE_CHOICES}


class Command(BaseCommand):
    help = (
        "Render the Group hierarchy (django-mptt) as a Graphviz and/or Mermaid "
        "diagram, optionally rooted at a single group"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--group",
            type=str,
            default=None,
            help=(
                "Name or slug of the group to root the tree at. Only that group "
                "and its descendants are drawn. Omit to draw every root group."
            ),
        )
        parser.add_argument(
            "--format",
            type=str,
            choices=["dot", "png", "svg", "pdf"],
            default="svg",
            help="Output format for the rendered tree (default: svg)",
        )
        parser.add_argument(
            "--output-dir",
            type=str,
            default=str(getattr(settings, "GROUP_TREE_OUTPUT_DIR", "diagrams")),
            help="Directory for rendered output (default: GROUP_TREE_OUTPUT_DIR)",
        )
        parser.add_argument(
            "--group-type",
            dest="group_types",
            action="append",
            default=None,
            help=(
                "Only include groups of this type; repeat for several (e.g. "
                "--group-type committee --group-type division)."
            ),
        )
        parser.add_argument(
            "--active-only",
            action="store_true",
            help="Exclude groups whose is_active flag is False.",
        )
        parser.add_argument(
            "--max-depth",
            type=int,
            default=None,
            help=(
                "Limit how many levels below the root are drawn (0 = roots "
                "only). Applies relative to each drawn root."
            ),
        )
        parser.add_argument(
            "--rankdir",
            type=str,
            choices=["TB", "LR", "BT", "RL"],
            default="TB",
            help="Graphviz rank direction: top-bottom, left-right, etc.",
        )
        parser.add_argument(
            "--show-members",
            action="store_true",
            help="Annotate each node with its active member count.",
        )
        parser.add_argument(
            "--filename",
            type=str,
            default=None,
            help="Override the output filename stem (without extension).",
        )
        parser.add_argument(
            "--text",
            action="store_true",
            help="Also print an indented text tree to standard output.",
        )
        parser.add_argument(
            "--mermaid-dir",
            type=str,
            default=None,
            help=(
                "Directory for the Mermaid Markdown export (defaults to --output-dir)."
            ),
        )
        parser.add_argument(
            "--no-mermaid",
            action="store_true",
            help="Skip writing the Mermaid Markdown alongside the rendered tree.",
        )

    def handle(self, *args, **options):
        group_ref = options["group"]
        group_types = options["group_types"]
        active_only = options["active_only"]
        max_depth = options["max_depth"]
        output_dir = options["output_dir"]
        output_format = options["format"]
        rankdir = options["rankdir"]
        show_members = options["show_members"]
        export_mermaid = not options["no_mermaid"]
        mermaid_dir = options["mermaid_dir"] or output_dir

        if max_depth is not None and max_depth < 0:
            raise CommandError("--max-depth must be zero or greater.")
        if group_types:
            unknown = sorted(set(group_types) - VALID_GROUP_TYPES)
            if unknown:
                raise CommandError(
                    f"Unknown group type(s): {', '.join(unknown)}. "
                    f"Valid types: {', '.join(sorted(VALID_GROUP_TYPES))}"
                )

        forest = self._build_forest(group_ref, group_types, active_only, max_depth)

        if not forest:
            self.stdout.write(
                self.style.WARNING("No groups match the given scope — nothing to draw.")
            )
            return

        # Member counts in one query, kept out of the per-node loop.
        member_counts = self._member_counts() if show_members else {}

        os.makedirs(output_dir, exist_ok=True)
        if export_mermaid:
            os.makedirs(mermaid_dir, exist_ok=True)

        stem = options["filename"] or self._default_stem(group_ref)
        title = self._title(group_ref, forest)

        dot = self._build_graph(
            stem,
            title,
            forest,
            rankdir=rankdir,
            member_counts=member_counts,
        )
        if output_format == "png":
            dot.graph_attr["dpi"] = "200"

        rendered_path = dot.render(
            os.path.join(output_dir, stem), format=output_format, cleanup=True
        )
        self.stdout.write(self.style.SUCCESS(f"🌳 Tree saved to: {rendered_path}"))

        if export_mermaid:
            mermaid_path = self.write_mermaid(
                os.path.join(mermaid_dir, f"{stem}.md"),
                title,
                forest,
                rankdir=rankdir,
                member_counts=member_counts,
            )
            self.stdout.write(
                self.style.SUCCESS(f"📝 Mermaid saved to: {mermaid_path}")
            )

        if options["text"]:
            self.stdout.write("")
            self._print_text_tree(forest, member_counts)

        self.stdout.write(
            f"   {len(forest)} root(s)  ·  {self._count_nodes(forest)} group(s)  "
            f"·  {timezone.localdate().isoformat()}"
        )

    # -- scope --------------------------------------------------------------
    def _resolve_group(self, group_ref: str) -> Group:
        """Find one group by (case-insensitive) name or slug, or raise."""
        matches = list(
            Group.objects.filter(Q(name__iexact=group_ref) | Q(slug__iexact=group_ref))
        )
        if not matches:
            raise CommandError(
                f"Group '{group_ref}' not found. Check the name or slug."
            )
        if len(matches) > 1:
            options = ", ".join(f"{g.name} (id={g.id})" for g in matches)
            raise CommandError(
                f"Group reference '{group_ref}' is ambiguous: {options}. "
                f"Use a unique name or slug."
            )
        return matches[0]

    def _build_forest(
        self,
        group_ref: str | None,
        group_types: list[str] | None,
        active_only: bool,
        max_depth: int | None,
    ) -> list[Group]:
        """Return the roots of the (possibly pruned) trees to draw.

        Each returned group is linked in memory to its kept children via a
        ``_children`` list (and to its kept parent via ``_tree_parent``) so the
        renderers can walk the pruned tree without further queries. When the
        scope's own root is filtered out, its surviving descendants are promoted
        to roots so nothing is silently dropped.
        """
        fields = [
            "id",
            "name",
            "short_name",
            "group_type",
            "parent",
            "is_active",
            "level",
            "tree_id",
            "lft",
        ]

        if group_ref is not None:
            scope_root = self._resolve_group(group_ref)
            # mptt gives the whole subtree in tree order with a single query.
            nodes = list(scope_root.get_descendants(include_self=True).only(*fields))
        else:
            # The mptt manager orders by (tree_id, lft), i.e. depth-first tree
            # order, so the forest comes back already laid out.
            nodes = list(Group.objects.all().only(*fields))

        by_id = {node.id: node for node in nodes}

        def wanted(node: Group) -> bool:
            if group_types and node.group_type not in group_types:
                return False
            return not (active_only and not node.is_active)

        included = {node.id: node for node in nodes if wanted(node)}

        # Re-point every kept node at its nearest kept ancestor. Filtering out a
        # middle node must not orphan its children.
        for node in included.values():
            node._children = []
            node._tree_parent = None

        roots: list[Group] = []
        for node in nodes:  # tree order, so roots and siblings stay ordered
            if node.id not in included:
                continue
            parent = by_id.get(node.parent_id)
            while parent is not None and parent.id not in included:
                parent = by_id.get(parent.parent_id)
            node._tree_parent = parent
            if parent is None:
                roots.append(node)
            else:
                parent._children.append(node)

        if max_depth is not None:
            self._prune_depth(roots, max_depth)

        return roots

    def _prune_depth(self, roots: list[Group], max_depth: int):
        """Drop nodes deeper than ``max_depth`` levels below each root."""

        def prune(node: Group, depth: int):
            if depth >= max_depth:
                node._children = []
                return
            for child in node._children:
                prune(child, depth + 1)

        for root in roots:
            prune(root, 0)

    def _member_counts(self) -> dict[int, int]:
        """Distinct active members per group id, in a single aggregate query."""
        rows = (
            GroupMembership.objects.filter(is_active=True)
            .values("group_id")
            .annotate(total=Count("user_id", distinct=True))
        )
        return {row["group_id"]: row["total"] for row in rows}

    # -- graph --------------------------------------------------------------
    def _build_graph(
        self,
        name: str,
        title: str,
        forest: list[Group],
        rankdir: str,
        member_counts: dict[int, int],
    ) -> graphviz.Digraph:
        dot = graphviz.Digraph(
            name or "group_tree",
            comment="Group hierarchy",
            format="png",  # overridden by the --format option at render time
        )
        dot.attr(
            rankdir=rankdir,
            ranksep="0.6",
            nodesep="0.35",
            bgcolor="white",
            pad="0.5",
            splines="spline",
        )
        dot.attr(
            "node",
            shape="box",
            style="filled,rounded",
            fontname=FONT,
            fontsize="13",
            margin="0.18,0.10",
            penwidth="1.2",
        )
        dot.attr(
            "edge",
            color=PALETTE["edge"],
            penwidth="1.1",
            arrowhead="none",
        )

        self._add_header(dot, title, forest)

        for root in forest:
            self._add_node(dot, root, member_counts)
            for node in self._walk(root):
                self._add_node(dot, node, member_counts)
                dot.edge(f"g{node._tree_parent.id}", f"g{node.id}")

        # Anchor the header above the first root so it stays on top.
        dot.edge("header", f"g{forest[0].id}", style="invis")

        families = {self._family(node.group_type) for node in self._all_nodes(forest)}
        self._add_legend(dot, families)
        self._add_footer(dot, forest)

        return dot

    def _add_node(self, dot, node: Group, member_counts: dict[int, int]):
        style = self._style(node.group_type)
        dot.node(
            f"g{node.id}",
            label=self._node_label(node, member_counts),
            fillcolor=style["fill"],
            color=style["border"],
            fontcolor=style["text"],
            tooltip=self._tooltip(node),
        )

    def _node_label(self, node: Group, member_counts: dict[int, int]) -> str:
        """An HTML-like label: the name, with the type (and count) beneath.

        HTML whitespace inside a ``<TD>`` is emitted as non-breaking spaces by
        Graphviz, so every cell is written on one line with no indentation.
        """
        name = escape_html(node.name)
        subtype = escape_html(node.get_group_type_display())
        if node.short_name:
            subtype = f"{escape_html(node.short_name)}  ·  {subtype}"
        if member_counts:
            subtype = f"{subtype}  ·  {member_counts.get(node.id, 0)} members"

        return (
            "<"
            '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="1">'
            f'<TR><TD ALIGN="LEFT"><B>{name}</B></TD></TR>'
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="11">{subtype}</FONT></TD></TR>'
            "</TABLE>"
            ">"
        )

    def _tooltip(self, node: Group) -> str:
        parts = [node.name, node.get_group_type_display()]
        if node.short_name:
            parts.insert(1, node.short_name)
        return escape_html("  ·  ".join(parts))

    def _add_header(self, dot, title: str, forest: list[Group]):
        """A chrome-free title banner with a hairline rule beneath it."""
        scope = "complete hierarchy" if len(forest) != 1 else "single subtree"
        label = (
            "<"
            '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="2">'
            f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="17" COLOR="{PALETTE["heading"]}">'
            f"<B>{escape_html(title)}</B></FONT></TD></TR>"
            '<TR><TD ALIGN="LEFT">'
            f'<FONT POINT-SIZE="11" COLOR="{PALETTE["muted"]}">Group hierarchy'
            f"  ·  {scope}  ·  {self._count_nodes(forest)} group(s)</FONT></TD></TR>"
            f'<TR><TD HEIGHT="2" BGCOLOR="{PALETTE["rule"]}"></TD></TR>'
            "</TABLE>"
            ">"
        )
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

    def _add_legend(self, dot, families: set[str]):
        """A legend naming each colour family actually present in the graph."""
        ordered = [key for key in FAMILY_STYLE if key in families]

        def swatch(color: str) -> str:
            return f'<TD WIDTH="14" HEIGHT="11" BGCOLOR="{color}"></TD>'

        def item(text: str) -> str:
            return (
                '<TD ALIGN="LEFT">'
                f'<FONT POINT-SIZE="11" COLOR="{PALETTE["muted"]}">{text}</FONT></TD>'
            )

        cells = "".join(
            swatch(FAMILY_STYLE[key]["fill"]) + item(FAMILY_STYLE[key]["label"])
            for key in ordered
        )
        label = (
            "<"
            f'<TABLE BORDER="1" COLOR="{PALETTE["rule"]}" '
            f'BGCOLOR="{PALETTE["surface"]}" CELLBORDER="0" CELLSPACING="8" '
            'CELLPADDING="4">'
            '<TR><TD ALIGN="LEFT">'
            f'<FONT POINT-SIZE="11" COLOR="{PALETTE["muted"]}"><B>Legend</B></FONT>'
            "</TD></TR>"
            f"<TR>{cells}</TR>"
            "</TABLE>"
            ">"
        )

        # rank=sink on a plain subgraph (not a cluster) reliably pins the legend
        # below the tree.
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

    def _add_footer(self, dot, forest: list[Group]):
        dot.attr(
            label=(
                "manage.py generate_group_tree  ·  "
                f"{self._count_nodes(forest)} group(s)  ·  "
                f"generated {timezone.localdate().isoformat()}"
            ),
            labelloc="b",
            labeljust="c",
            fontname=FONT,
            fontsize="11",
            fontcolor=PALETTE["muted"],
        )

    # -- mermaid ------------------------------------------------------------
    @staticmethod
    def _mermaid_escape(text: str) -> str:
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
        title: str,
        forest: list[Group],
        rankdir: str,
        member_counts: dict[int, int],
    ) -> str:
        direction = {"TB": "TD", "LR": "LR", "BT": "BT", "RL": "RL"}[rankdir]
        families = sorted(
            {self._family(node.group_type) for node in self._all_nodes(forest)},
            key=list(FAMILY_STYLE).index,
        )

        lines = [
            "%% Generated by `manage.py generate_group_tree`; do not edit by hand.",
            f"%% {title}",
            f"flowchart {direction}",
        ]
        for key in families:
            style = FAMILY_STYLE[key]
            lines.append(
                f"    classDef {key} fill:{style['fill']},"
                f"stroke:{style['border']},color:{style['text']},stroke-width:1px"
            )

        for node in self._all_nodes(forest):
            label = self._mermaid_escape(node.name)
            subtype = self._mermaid_escape(node.get_group_type_display())
            label += f"<br/><small>{subtype}</small>"
            if member_counts:
                label += f"<br/><small>{member_counts.get(node.id, 0)} members</small>"
            lines.append(f'    g{node.id}["{label}"]:::{self._family(node.group_type)}')

        for node in self._all_nodes(forest):
            if node._tree_parent is not None:
                lines.append(f"    g{node._tree_parent.id} --> g{node.id}")

        return "\n".join(lines) + "\n"

    def write_mermaid(
        self,
        filepath: str,
        title: str,
        forest: list[Group],
        rankdir: str,
        member_counts: dict[int, int],
    ) -> str:
        diagram = self._build_mermaid(title, forest, rankdir, member_counts)
        with open(filepath, "w", encoding="utf-8") as handle:
            handle.write(f"# {title}\n\n")
            handle.write("```mermaid\n")
            handle.write(diagram)
            handle.write("```\n")
        return filepath

    # -- text ---------------------------------------------------------------
    def _print_text_tree(self, forest: list[Group], member_counts: dict[int, int]):
        for root in forest:
            self.stdout.write(
                self.style.SUCCESS(root.name + self._node_suffix(root, member_counts))
            )
            self._print_text_children(root, member_counts, prefix="")

    def _print_text_children(
        self, node: Group, member_counts: dict[int, int], prefix: str
    ):
        children = node._children
        for index, child in enumerate(children):
            is_last = index == len(children) - 1
            connector = "└─ " if is_last else "├─ "
            self.stdout.write(
                f"{prefix}{connector}{child.name}{self._node_suffix(child, member_counts)}"
            )
            self._print_text_children(
                child,
                member_counts,
                prefix + ("   " if is_last else "│  "),
            )

    def _node_suffix(self, node: Group, member_counts: dict[int, int]) -> str:
        suffix = f" ({node.get_group_type_display()})"
        if member_counts:
            suffix += f" [{member_counts.get(node.id, 0)} members]"
        return suffix

    # -- helpers ------------------------------------------------------------
    def _walk(self, root: Group):
        """Depth-first iteration over the pruned subtree, root excluded."""
        for child in root._children:
            yield child
            yield from self._walk(child)

    def _all_nodes(self, forest: list[Group]):
        for root in forest:
            yield root
            yield from self._walk(root)

    def _count_nodes(self, forest: list[Group]) -> int:
        return sum(1 for _ in self._all_nodes(forest))

    def _family(self, group_type: str) -> str:
        return TYPE_FAMILIES.get(group_type, DEFAULT_FAMILY)

    def _style(self, group_type: str) -> dict:
        return FAMILY_STYLE[self._family(group_type)]

    def _default_stem(self, group_ref: str | None) -> str:
        if group_ref is None:
            return "all_groups"
        stem = re.sub(r"[^a-z0-9]+", "_", group_ref.lower()).strip("_")
        return stem or "group_tree"

    def _title(self, group_ref: str | None, forest: list[Group]) -> str:
        if group_ref is None:
            return "All groups"
        if len(forest) == 1:
            return f"{forest[0].name} (and descendants)"
        return f"{group_ref} (and descendants)"
