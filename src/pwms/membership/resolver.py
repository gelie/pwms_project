"""Resolve Oracle organisation names onto the groups ``seed_structure`` creates.

Oracle's ``XXPER_PEOPLE_INTERFACE`` gives each person a cost centre
(``CHILD_ORG_NAME``) and an organisational unit (``PARENT_ORG_NAME``) named from
the ERP chart — e.g. ``HC: TM: Talent Management: Man And Gen``. The seeded
structure uses curated labels — ``Secretary to Parliament / Support Services
Branch / Human Resource Management Division / HR Shared Services``. This module
maps the first onto the second.

Resolution is deliberately layered, most explicit first:

1. the alias file maps an Oracle name — matched against the raw ``child`` /
   ``parent`` value from the export, ignoring case and redundant whitespace, not
   against a normalised spelling — to a seeded path;
2. the same child name under the same parent name, matched exactly;
3. the only seeded group with that name;
4. the only seeded group with the parent's name;
5. otherwise the pair is unresolved.

Nothing here guesses. A name that appears more than once in the tree does not
resolve by name, because the seeded tree legitimately repeats unit names
("House Plenaries & Procedure" sits under both House divisions). Ambiguity is
reported, not papered over.

The alias file ships as ``group_aliases.csv`` next to this module and can be
overridden by the caller. The audit command writes a CSV in the same shape, so
the workflow is: run the audit, fill ``seeded_path`` for the unresolved rows,
re-run.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from django.utils import timezone

from pwms.models import Group
from pwms.models.groups import ParliamentTerm

#: Root of the permanent, term-less administration tree from ``seed_structure``.
ADMIN_ROOT_NAME = "Parliament Administration"

#: House group name for the code Oracle puts at the end of ``POSITIONDESC``.
HOUSE_BY_CODE = {
    "NA": "National Assembly",
    "NCOP": "National Council of Provinces",
}

#: Separator used in seeded paths (``get_full_path`` also uses ``>``, which the
#: resolver accepts when splitting).
PATH_SEPARATOR = "/"

#: Alias CSV columns. The audit command emits the same three names, plus
#: informational columns the loader ignores.
ALIAS_CHILD_FIELD = "oracle_child_org"
ALIAS_PARENT_FIELD = "oracle_parent_org"
ALIAS_PATH_FIELD = "seeded_path"

#: Confidence shown in reports for each method.
_CONFIDENCE = {
    "alias-child": "high",
    "alias-parent": "high",
    "path": "high",
    "unique-leaf": "medium",
    "unique-parent": "low",
    "unresolved": "none",
}


class ResolverError(RuntimeError):
    """Raised when the seeded tree or a requested alias file is unavailable."""


@dataclass(frozen=True)
class Alias:
    """One row of the alias table: an Oracle name and the seeded path it means."""

    child: str = ""
    parent: str = ""
    path: str = ""


@dataclass(frozen=True)
class Resolution:
    """What the resolver made of one Oracle organisation pair."""

    group: Group | None
    method: str
    #: The normalised Oracle name that produced the hit (or the child/parent
    #: that was tried when nothing resolved).
    oracle_name: str = ""
    #: Human-readable seeded path, e.g. ``Parliament Administration/...``.
    path: str = ""

    @property
    def resolved(self) -> bool:
        return self.group is not None

    @property
    def confidence(self) -> str:
        return _CONFIDENCE.get(self.method, "none")


class GroupResolver:
    """Resolve Oracle organisation names to groups in the seeded structure.

    ``normalize`` turns a raw Oracle name into its canonical spelling for the
    name-based fallbacks; the Oracle sync commands pass
    ``strip_group_code_prefix`` plus acronym restoration. Alias rows are matched
    on the raw name instead, so unrelated units that normalise identically
    cannot share one.
    """

    def __init__(
        self,
        *,
        normalize: Callable[[str], str],
        aliases: Iterable[Alias] | None = None,
        admin_root_name: str = ADMIN_ROOT_NAME,
        term: ParliamentTerm | None = None,
    ):
        self._normalize = normalize
        self._admin_root_name = admin_root_name
        self._term = term

        #: Oracle name -> seeded path, only where the alias file is unambiguous.
        #: Keyed by the raw name from the ERP (case/whitespace-insensitive): a
        #: normalised key would let unrelated units share one, e.g. every name
        #: ending in ": Man & Gen" collapsing to the same leaf. Conflicts are
        #: kept in ``alias_conflicts`` for reporting.
        self.alias_child: dict[str, str] = {}
        self.alias_parent: dict[str, str] = {}
        self.alias_conflicts: list[str] = []
        self._load_aliases(aliases or ())

        self.admin_root: Group | None = None
        self._by_name: dict[str, list[Group]] = defaultdict(list)
        self._by_parent_name: dict[tuple[str, str], Group] = {}
        self._path_index: dict[tuple[str, ...], Group] = {}
        self._paths: dict[int, str] = {}
        self._house_cache: dict[str, Group | None] = {}

    # ------------------------------------------------------------------ #
    #  Construction                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_database(
        cls,
        *,
        normalize: Callable[[str], str],
        aliases: Iterable[Alias] | None = None,
        alias_file: str | Path | None = None,
        admin_root_name: str = ADMIN_ROOT_NAME,
        term: ParliamentTerm | None = None,
    ) -> GroupResolver:
        """Build a resolver from the seeded tree currently in the database.

        With no ``aliases``, the packaged ``group_aliases.csv`` is used when it
        exists. An explicitly supplied ``alias_file`` must exist.
        """
        if aliases is None:
            source = Path(alias_file) if alias_file else cls.default_alias_path()
            if source is not None:
                if not Path(source).exists():
                    raise ResolverError(f"Alias file not found: {source}")
                aliases = cls.aliases_from_csv(source)

        resolver = cls(
            normalize=normalize,
            aliases=aliases or (),
            admin_root_name=admin_root_name,
            term=term,
        )
        resolver.refresh()
        return resolver

    @staticmethod
    def default_alias_path() -> Path | None:
        """The packaged alias table, or ``None`` when it is absent."""
        path = Path(__file__).with_name("group_aliases.csv")
        return path if path.exists() else None

    @staticmethod
    def aliases_from_csv(path: str | Path) -> list[Alias]:
        """Read alias rows; the audit command's CSV is accepted unchanged."""
        aliases: list[Alias] = []
        with Path(path).open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                child = (row.get(ALIAS_CHILD_FIELD) or "").strip()
                parent = (row.get(ALIAS_PARENT_FIELD) or "").strip()
                target = (row.get(ALIAS_PATH_FIELD) or "").strip()
                if target and (child or parent):
                    aliases.append(Alias(child=child, parent=parent, path=target))
        return aliases

    def _load_aliases(self, aliases: Iterable[Alias]) -> None:
        child_targets: dict[str, set[str]] = defaultdict(set)
        parent_targets: dict[str, set[str]] = defaultdict(set)
        for alias in aliases:
            if not alias.path:
                continue
            if alias.child:
                key = self._raw_key(alias.child)
                if key:
                    child_targets[key].add(alias.path)
            if alias.parent:
                key = self._raw_key(alias.parent)
                if key:
                    parent_targets[key].add(alias.path)

        conflicts: list[str] = []
        for key, targets in child_targets.items():
            if len(targets) == 1:
                self.alias_child[key] = next(iter(targets))
            elif key:
                conflicts.append(f"child:{key}")
        for key, targets in parent_targets.items():
            if len(targets) == 1:
                self.alias_parent[key] = next(iter(targets))
            elif key:
                conflicts.append(f"parent:{key}")
        self.alias_conflicts = sorted(conflicts)

    # ------------------------------------------------------------------ #
    #  Indexes                                                           #
    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        """(Re)build the name and path indexes from the seeded admin tree."""
        root = Group.objects.filter(
            name=self._admin_root_name, parent__isnull=True
        ).first()
        if root is None:
            raise ResolverError(
                f"No '{self._admin_root_name}' group found. Run "
                "'python manage.py seed_structure' before resolving users."
            )

        self.admin_root = root
        self._by_name = defaultdict(list)
        self._by_parent_name = {}
        self._path_index = {}
        self._paths = {}
        self._house_cache = {}

        nodes = list(root.get_descendants(include_self=True).select_related("parent"))
        by_id = {node.pk: node for node in nodes}
        for node in nodes:
            chain: list[Group] = []
            seen: set[int] = set()
            current: Group | None = node
            while current is not None and current.pk not in seen:
                seen.add(current.pk)
                chain.append(current)
                current = by_id.get(current.parent_id)
            chain.reverse()

            key = tuple(part.name.casefold().strip() for part in chain)
            self._path_index[key] = node
            self._paths[node.pk] = PATH_SEPARATOR.join(part.name for part in chain)

            name_key = node.name.casefold().strip()
            self._by_name[name_key].append(node)
            parent_key = node.parent.name.casefold().strip() if node.parent_id else ""
            self._by_parent_name[(parent_key, name_key)] = node

    # ------------------------------------------------------------------ #
    #  Org resolution                                                    #
    # ------------------------------------------------------------------ #

    def resolve_org(
        self, child_org: str | None = None, parent_org: str | None = None
    ) -> Resolution:
        """Resolve one Oracle ``(child, parent)`` organisation pair."""
        child = self._key(child_org)
        parent = self._key(parent_org)

        # Aliases are matched on the raw name, before normalisation collapses
        # different units ("...: Man & Gen") onto the same key.
        raw_child = self._raw_key(child_org)
        raw_parent = self._raw_key(parent_org)

        target = self.alias_child.get(raw_child) if raw_child else None
        if target:
            group = self._group_for_path(target)
            if group is not None:
                return self._resolution(group, "alias-child", child)

        target = self.alias_parent.get(raw_parent) if raw_parent else None
        if target:
            group = self._group_for_path(target)
            if group is not None:
                return self._resolution(group, "alias-parent", parent)

        if parent and child:
            group = self._by_parent_name.get((parent, child))
            if group is not None:
                return self._resolution(group, "path", child)

        if child:
            group = self._unique(self._by_name.get(child))
            if group is not None:
                return self._resolution(group, "unique-leaf", child)

        if parent:
            group = self._unique(self._by_name.get(parent))
            if group is not None:
                return self._resolution(group, "unique-parent", parent)

        return Resolution(None, "unresolved", child or parent or "")

    def resolve_house(self, house_code: str) -> Group | None:
        """The seeded House for ``"NA"`` / ``"NCOP"``, scoped to the term.

        With no term configured the lookup is not scoped, which matches the
        legacy Oracle tree; callers doing real work should pass the active term.
        """
        code = (house_code or "").strip().upper()
        if code in self._house_cache:
            return self._house_cache[code]

        name = HOUSE_BY_CODE.get(code)
        group = None
        if name:
            queryset = Group.objects.filter(name=name, group_type=Group.Type.HOUSE)
            if self._term is not None:
                queryset = queryset.filter(term=self._term)
            group = queryset.first()

        self._house_cache[code] = group
        return group

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    @property
    def term(self) -> ParliamentTerm | None:
        return self._term

    @property
    def indexed_group_count(self) -> int:
        """How many groups (the administration subtree) the resolver indexed."""
        return len(self._paths)

    @staticmethod
    def active_term(today=None) -> ParliamentTerm | None:
        """The Parliament term in force on ``today`` (latest start wins)."""
        today = today or timezone.localdate()
        return (
            ParliamentTerm.objects.filter(start_date__lte=today)
            .order_by("-number", "-start_date")
            .first()
        )

    def normalize_name(self, raw: str | None) -> str:
        """Canonical spelling for a raw Oracle name (for reports)."""
        return self._normalize(raw or "")

    def path_of(self, group: Group | None) -> str:
        if group is None:
            return ""
        return self._paths.get(group.pk, "")

    def _key(self, raw: str | None) -> str:
        return self._normalize(raw or "").casefold().strip()

    @staticmethod
    def _raw_key(raw: str | None) -> str:
        """Alias key: the raw name, case- and redundant-whitespace-insensitive."""
        return " ".join((raw or "").split()).casefold()

    @staticmethod
    def _unique(candidates: list[Group] | None) -> Group | None:
        if candidates is not None and len(candidates) == 1:
            return candidates[0]
        return None

    def _resolution(self, group: Group, method: str, oracle_name: str) -> Resolution:
        return Resolution(
            group=group,
            method=method,
            oracle_name=oracle_name,
            path=self.path_of(group),
        )

    def _group_for_path(self, path: str) -> Group | None:
        """Resolve an alias target: a full path, or a unique seeded name."""
        text = (path or "").strip()
        if not text:
            return None

        separator = PATH_SEPARATOR if PATH_SEPARATOR in text else ">"
        parts = [
            part.strip().casefold() for part in text.split(separator) if part.strip()
        ]
        if not parts:
            return None

        group = self._path_index.get(tuple(parts))
        if group is not None:
            return group

        root_key = self.admin_root.name.casefold().strip() if self.admin_root else ""
        if root_key and parts[0] != root_key:
            group = self._path_index.get((root_key, *parts))
            if group is not None:
                return group

        if len(parts) == 1:
            return self._unique(self._by_name.get(parts[0]))
        return None
