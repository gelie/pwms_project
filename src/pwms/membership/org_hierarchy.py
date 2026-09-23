"""Derive the organisational tree Oracle implies, and diff it against PWMS.

``APPS.XXPER_PEOPLE_INTERFACE`` hands the sync two separate things per person: an
organisation pair — ``CHILD_ORG_NAME`` (the cost centre) and ``PARENT_ORG_NAME``
(the organisational unit) — and an ``EMPLOYEEID`` / ``SUPERVISORID`` chain.
``sync_groups_oracle`` built its tree from the organisation pair alone, which
says nothing about how units nest, so every unit ended up directly under
``Administration``: a flat forest, with the division/section relationship lost.

The unit-to-unit relationship lives in the supervisor chain. This module derives
it — a unit's parent is the unit its people's manager reports into — and diffs it
against the tree PWMS currently stores, so the gap can be reviewed before
anything is re-parented.

The inference is a heuristic, not a fact:

* a unit with several outward reporting lines is robust — the majority wins, and
  the supervising unit's span of control breaks a tie (a division manager's unit
  outweighs a section manager's);
* a unit whose people all report internally gets no parent, which is how the top
  of a division is recognised;
* a unit whose *only* report goes to a manager in another unit takes that
  manager's unit as its parent even where the org chart disagrees — a
  *functional* reporting line, such as a liaison officer attached to a different
  section. Those single-report edges are flagged ``low_confidence`` so a human
  can overrule them.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass

# Acronyms / abbreviations that must stay uppercase in organisational unit
# names. ``OracleSyncBase.strip_group_code_prefix`` only preserves a smaller
# set, so we restore these after sanitising. ``sync_roles_oracle`` keeps its own
# copy for role names.
ACRONYMS = {
    "BO",
    "BP1",
    "BP2",
    "CAE",
    "CBS",
    "CCSC",
    "CEO",
    "CFO",
    "CIO",
    "CIS",
    "CISO",
    "CS",
    "DS",
    "ECM",
    "ERP",
    "FMO",
    "HC",
    "HR",
    "ICT",
    "IP",
    "IR",
    "IRP",
    "ISS",
    "IT",
    "KIS",
    "LOGB",
    "LR",
    "LS",
    "LSO",
    "LSS",
    "MIS",
    "MP",
    "MR",
    "MPs",
    "MSR",
    "MSS",
    "NA",
    "NCOP",
    "OM",
    "OISD",
    "OSTP",
    "PA",
    "PBO",
    "PCS",
    "PCSD",
    "PDO",
    "PISC",
    "POSA",
    "PMO",
    "PP",
    "PPS",
    "PM",
    "PMU",
    "RM",
    "RMI",
    "RS",
    "SC",
    "SCM",
    "SMG",
    "SS",
    "TAO",
    "TM",
}

#: Lower-cased lookup so mixed-case input still resolves to the canonical form
#: (e.g. "CBS", "Cbs" and "cbs" all become "CBS").
_ACRONYM_BY_LOWER = {name.lower(): name for name in ACRONYMS}


def restore_acronym_case(name: str) -> str:
    """Uppercase known acronyms that title-casing would have mangled.

    ``strip_group_code_prefix`` title-cases every word and only preserves a
    small acronym set (ICT, HR, ...). Words such as "CBS", "CAE", "FMO"
    therefore come back as "Cbs", "Cae", "Fmo". This re-uppercases any token
    whose lower-cased form matches a known acronym, ignoring surrounding
    punctuation (so "Cbs:" becomes "CBS:"), while leaving other casing
    untouched. Shared by the group sync and the hierarchy diff command.
    """
    words = (name or "").split()
    restored = []
    for word in words:
        core = word.strip("():;,./-'\"")
        replacement = _ACRONYM_BY_LOWER.get(core.lower())
        if replacement is None:
            restored.append(word)
            continue
        # Preserve punctuation that surrounded the acronym token.
        start = word.find(core)
        lead = word[:start]
        trail = word[start + len(core) :]
        restored.append(f"{lead}{replacement}{trail}")
    return " ".join(restored)


@dataclass(frozen=True)
class Person:
    """One Oracle employee row, as far as the org tree needs it."""

    employee_id: str
    supervisor_id: str
    child_org: str  # cost centre, e.g. "18301-IRP: MR: Man and Gen"
    parent_org: str  # organiational unit, e.g. "IRP: Multilateral Relations..."

    @property
    def unit(self) -> str:
        return self.parent_org


def person_from_row(row) -> Person:
    """Build a :class:`Person` from an Oracle row's first four columns."""
    values = [("" if value is None else str(value)).strip() for value in row]
    values += [""] * (4 - len(values))
    return Person(*values[:4])


@dataclass(frozen=True)
class UnitInference:
    """What the supervisor chain says about one organisational unit."""

    unit: str
    parent: str | None
    members: int
    #: candidate parent unit -> number of this unit's members reporting into it
    votes: dict[str, int]
    #: candidate parent unit -> the largest span of control behind it
    spans: dict[str, int]
    #: candidate parent unit -> employee ids of the members that voted for it
    reports: dict[str, tuple[str, ...]]

    @property
    def tied(self) -> bool:
        """True when the top two candidates score equally *after* tie-breaks."""
        if len(self.votes) < 2:
            return False
        scores = sorted(
            (-count, -self.spans.get(unit, 0)) for unit, count in self.votes.items()
        )
        return scores[0] == scores[1]

    @property
    def low_confidence(self) -> bool:
        """True when the winning edge rests on a single outward report."""
        return bool(self.votes) and max(self.votes.values()) < 2


def infer_unit_parents(people, normalize) -> dict[str, UnitInference]:
    """Derive each unit's parent from the supervisor chain.

    ``normalize`` is applied to every raw Oracle org name so the results line up
    with the group names the sync stores. People with no employee id or no unit
    are ignored; a person whose supervisor is unknown or in their own unit casts
    no vote, which is what leaves the top of a division parentless.
    """
    unit_of: dict[str, str] = {}
    for person in people:
        if person.employee_id:
            unit_of[person.employee_id] = normalize(person.parent_org)

    span = Counter(p.supervisor_id for p in people if p.supervisor_id)

    members_of: dict[str, list] = defaultdict(list)
    for person in people:
        unit = unit_of.get(person.employee_id)
        if unit:
            members_of[unit].append(person)

    inferences: dict[str, UnitInference] = {}
    for unit, members in members_of.items():
        votes: Counter = Counter()
        spans: dict[str, int] = {}
        reports: dict[str, list[str]] = defaultdict(list)
        for member in members:
            supervisor = member.supervisor_id
            if not supervisor:
                continue
            parent_unit = unit_of.get(supervisor)
            if not parent_unit or parent_unit == unit:
                continue
            votes[parent_unit] += 1
            spans[parent_unit] = max(spans.get(parent_unit, 0), span.get(supervisor, 0))
            reports[parent_unit].append(member.employee_id)

        if votes:
            rank = sorted(votes, key=lambda name: (-votes[name], -spans[name], name))
            parent = rank[0]
        else:
            parent = None

        inferences[unit] = UnitInference(
            unit=unit,
            parent=parent,
            members=len(members),
            votes=dict(votes),
            spans=spans,
            reports={name: tuple(ids) for name, ids in reports.items()},
        )
    return inferences


def unit_parent_map(inferences, min_votes: int = 1) -> dict[str, str]:
    """``{unit: parent}`` for units whose parent was inferred.

    ``min_votes`` drops weakly-evidenced edges. A single outward report is as
    likely a functional reporting line as real containment — a liaison officer
    attached to another section, or a manager whose supervisor sits in an
    unrelated unit — so callers that act on the result should ask for
    corroboration (several of a unit's people reporting into the same unit).
    Roots, and edges below ``min_votes``, are omitted.
    """
    return {
        unit: inference.parent
        for unit, inference in inferences.items()
        if inference.parent and max(inference.votes.values(), default=0) >= min_votes
    }


@dataclass(frozen=True)
class ComparisonRow:
    """One unit's stored parent next to the one Oracle implies."""

    unit: str
    stored_parent: str | None
    inferred_parent: str | None
    status: str  # "match" | "differs" | "missing"
    low_confidence: bool


def compare_with_stored(
    inferences: dict[str, UnitInference],
    stored_parent: dict[str, str | None],
    *,
    administration_name: str = "Administration",
    prefix: str | None = None,
) -> list[ComparisonRow]:
    """Compare the inferred tree with the parents PWMS currently stores.

    ``stored_parent`` maps a group name to its current parent's name (or
    ``None``); a name absent from it has no PWMS group at all. Names are matched
    case-insensitively, because a group stored before an acronym was added to the
    table ("...The Ir And P...") and the normalised name ("...The IR And P...")
    are the same unit. A parent named ``administration_name`` means "top of the
    forest", which is what the inference reports as ``None``, so those two
    compare equal.
    """
    stored_by_case = {
        name.casefold(): (name, parent) for name, parent in stored_parent.items()
    }
    rows: list[ComparisonRow] = []
    for unit, inference in sorted(inferences.items()):
        if prefix and not unit.casefold().startswith(prefix.casefold()):
            continue
        match = stored_by_case.get(unit.casefold())
        if match is None:
            rows.append(
                ComparisonRow(
                    unit, None, inference.parent, "missing", inference.low_confidence
                )
            )
            continue
        _, current = match
        if current == administration_name:
            current = None
        status = "match" if current == inference.parent else "differs"
        rows.append(
            ComparisonRow(
                unit, current, inference.parent, status, inference.low_confidence
            )
        )
    return rows
