"""How far a workflow instance has travelled through its state machine.

The Progress tab answers two questions: *where are we now?* and *how much is
left?* It is built from two different measures, deliberately:

* the **sequence** — the type's states in display order (``State.order``), each
  marked done / current / upcoming from the instance's ``TransitionLog``. This is
  the qualitative part, and it is exact: a state is done because the instance has
  been in it.
* the **percentage** — measured along the *route to completion*, not along the
  state list. Counting positions would misreport a branched machine: a bill at
  *NCOP Consideration* is two steps from *Signed into Law*, but it sits only
  halfway down the bill's twelve states because Mediation and the
  constitutional-review branch are in that list too.

The percentage is therefore ``1 - (steps left / steps from the start)``, where
both are shortest paths over the transition graph. Two subtleties matter:

* **which terminal to measure against** — the *furthest* one, not the nearest. A
  bill can be withdrawn straight from *Introduced*, so the nearest terminal is
  one step away and would score a half-finished bill as complete. The furthest
  terminal is the destination of the main line, which is what "progress" means
  to a reader.
* **an instance that never reaches it** — one sitting in *Withdrawn* is closed,
  so it is 100% by the same rule that closes it. Only a genuinely unreachable
  target downgrades to a position-in-the-list estimate.

All of that lives in :class:`WorkflowMachine`, which is the states-and-graph half
of the work and depends on nothing but the *type*. It is built once and shared,
so a page that lists many instances (the dashboard) costs two queries per
workflow type rather than two per row — the percentage of an instance needs only
its ``current_state``, never its logs.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from django.utils import timezone

from .history import actor_label


@dataclass(frozen=True)
class WorkflowMachine:
    """One workflow type's states and transition graph, ready to be asked."""

    states: tuple
    names: dict
    adjacency: dict
    terminal_ids: frozenset
    initial_ids: tuple
    target_id: Any = None
    span: int | None = None

    @classmethod
    def build(cls, workflow_type):
        """Read one type's states and transitions (two queries)."""
        states = tuple(workflow_type.states.all())
        if not states:
            return cls(
                states=(),
                names={},
                adjacency={},
                terminal_ids=frozenset(),
                initial_ids=(),
            )
        adjacency = _adjacency(workflow_type)
        target_id, span = _target_terminal(
            adjacency,
            tuple(state.pk for state in states if state.is_initial) or (states[0].pk,),
            [(state.pk, state.order) for state in states if state.is_terminal],
        )
        return cls(
            states=states,
            names={state.pk: state.name for state in states},
            adjacency=adjacency,
            terminal_ids=frozenset(state.pk for state in states if state.is_terminal),
            initial_ids=tuple(state.pk for state in states if state.is_initial),
            target_id=target_id,
            span=span,
        )

    @property
    def has_machine(self):
        """False for a type with no states configured — nothing to measure."""
        return bool(self.states)

    @property
    def target_name(self):
        """Name of the state the percentage measures against."""
        return self.names.get(self.target_id, "")

    def is_terminal(self, state_id):
        return state_id in self.terminal_ids

    def steps_left(self, state_id):
        """Fewest transitions from ``state_id`` to the completion state."""
        if self.target_id is None:
            return None
        return _shortest_path(self.adjacency, state_id, self.target_id)

    def percent(self, state_id):
        """How far along the route to completion, as a whole percent."""
        if self.is_terminal(state_id):
            return 100
        steps_left = self.steps_left(state_id)
        if steps_left is not None and self.span:
            travelled = max(0, self.span - steps_left)
            return max(0, min(100, round(100 * travelled / self.span)))
        return self._position_percent(state_id)

    def _position_percent(self, state_id):
        """Fallback for a machine with no route to its completion state."""
        index = next(
            (i for i, state in enumerate(self.states) if state.pk == state_id), 0
        )
        if len(self.states) < 2:
            return 0
        return round(100 * index / (len(self.states) - 1))


def machine_for(workflow_type):
    """
    The machine view of one workflow type.

    Callers that walk many instances should cache it per type (the dashboard
    does): it is the only part of the maths that costs queries.
    """
    return WorkflowMachine.build(workflow_type)


@dataclass(frozen=True)
class ProgressStep:
    """One state in the type's sequence, and how the instance relates to it."""

    state: Any
    status: str  #: ``done`` | ``current`` | ``upcoming``
    entered_at: Any = None
    left_at: Any = None

    @property
    def is_current(self):
        return self.status == "current"

    @property
    def is_done(self):
        return self.status == "done"

    @property
    def is_upcoming(self):
        return self.status == "upcoming"


@dataclass(frozen=True)
class ProgressMove:
    """One recorded transition: a step of the journey that got us here."""

    from_state: str
    to_state: str
    timestamp: Any
    actor: str
    notes: str


@dataclass(frozen=True)
class WorkflowProgress:
    """The whole picture rendered by the Progress tab."""

    steps: tuple = ()
    current: ProgressStep | None = None
    moves: tuple = ()
    total: int = 0
    completed: int = 0
    percent: int = 0
    steps_left: int | None = None
    target: str = ""
    is_closed: bool = False
    started_at: Any = None
    last_moved_at: Any = None
    days_in_state: int | None = None
    has_machine: bool = False


def workflow_progress(instance, *, machine=None):
    """
    Describe ``instance``'s position in its workflow type's machine.

    ``machine`` lets a caller that already holds this type's
    :class:`WorkflowMachine` (a page rendering many instances) skip rebuilding
    it.
    """
    machine = machine or machine_for(instance.workflow_type)
    if not machine.has_machine:
        return WorkflowProgress()

    logs = list(
        instance.audit_logs()
        .select_related("from_state", "to_state", "actor")
        .order_by("timestamp", "id")
    )
    moves = tuple(_move(log) for log in logs)

    entered, left = _state_timestamps(instance, logs)
    # A state the record has left was, by definition, visited — without this the
    # state it started in would read as "upcoming" once it moved on.
    visited = set(entered) | set(left)
    steps = tuple(
        ProgressStep(
            state=state,
            status=(
                "current"
                if state.pk == instance.current_state_id
                else "done"
                if state.pk in visited
                else "upcoming"
            ),
            entered_at=entered.get(state.pk),
            left_at=left.get(state.pk),
        )
        for state in machine.states
    )

    state_id = instance.current_state_id
    started_at = moves[0].timestamp if moves else instance.created_at
    last_moved_at = moves[-1].timestamp if moves else None

    return WorkflowProgress(
        steps=steps,
        current=next((step for step in steps if step.is_current), None),
        moves=moves,
        total=len(machine.states),
        completed=sum(1 for step in steps if step.is_done),
        percent=machine.percent(state_id),
        steps_left=machine.steps_left(state_id),
        target=machine.target_name,
        is_closed=machine.is_terminal(state_id),
        started_at=started_at,
        last_moved_at=last_moved_at,
        days_in_state=(timezone.now() - (last_moved_at or started_at)).days,
        has_machine=True,
    )


def _state_timestamps(instance, logs):
    """
    When the instance entered and left each state.

    A state's first arrival wins (a record that visits a state twice entered it
    the first time). The current state counts as entered when the record was
    created, so an instance that has never transitioned still shows its start.
    """
    entered = {}
    left = {}
    for log in logs:
        if log.to_state_id and log.to_state_id not in entered:
            entered[log.to_state_id] = log.timestamp
        if log.from_state_id:
            left[log.from_state_id] = log.timestamp
    if instance.current_state_id and instance.current_state_id not in entered:
        entered[instance.current_state_id] = instance.created_at
    return entered, left


def _move(log):
    return ProgressMove(
        from_state=log.from_state.name if log.from_state else "",
        to_state=log.to_state.name if log.to_state else "",
        timestamp=log.timestamp,
        actor=actor_label(log.actor),
        notes=log.notes,
    )


def _adjacency(workflow_type):
    """``{from_state_id: {to_state_id, ...}}`` for the type's transitions."""
    adjacency = {}
    for from_id, to_id in workflow_type.transitions.values_list(
        "from_state_id", "to_state_id"
    ):
        adjacency.setdefault(from_id, set()).add(to_id)
    return adjacency


def _target_terminal(adjacency, initial_ids, terminals):
    """
    The terminal the progress bar measures against, and the steps to reach it.

    The *furthest* terminal from the initial state wins — the end of the main
    line. Measuring against the nearest one would be wrong for a machine with an
    early exit (a bill withdrawn straight from *Introduced* is one step from a
    terminal), so a half-finished record would look complete. Ties fall to the
    later ``State.order``, which keeps the choice stable and inspectable.
    """
    target_id, span = None, None
    for state_id, _order in sorted(terminals, key=lambda pair: pair[1]):
        distance = min(
            (
                found
                for found in (
                    _shortest_path(adjacency, initial, state_id)
                    for initial in initial_ids
                )
                if found is not None
            ),
            default=None,
        )
        if distance is None:
            continue
        if span is None or distance >= span:
            target_id, span = state_id, distance
    return target_id, span


def _shortest_path(adjacency, start_id, goal_id):
    """Fewest transitions from ``start_id`` to ``goal_id``, or ``None``."""
    if start_id is None or goal_id is None:
        return None
    if start_id == goal_id:
        return 0
    seen = {start_id}
    queue = deque([(start_id, 0)])
    while queue:
        state_id, distance = queue.popleft()
        for neighbour in adjacency.get(state_id, ()):
            if neighbour == goal_id:
                return distance + 1
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append((neighbour, distance + 1))
    return None
