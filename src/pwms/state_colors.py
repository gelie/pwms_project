"""The one colour scheme for workflow states, shared by every workflow type.

A state's colour says where it sits in the life of a record, not which workflow
it belongs to, so a Bill, an Agreement and a Meeting all use the same four:

* **initial**: slate. Received, nothing done yet. Neutral on purpose, so it does
  not compete with the priority badge (blue / yellow / orange / red) beside it.
* **in progress**: brand green. Every state that is neither initial nor terminal.
* **terminal**: deep pine. Finished successfully. Same family as in progress but
  much darker, so it reads as "done".
* **unsuccessful**: maroon. A record that ended without completing: Withdrawn,
  Lapsed, Cancelled. Darker than the Urgent priority red so the two differ.

Badges print white text on these, and each colour has at least 4.8:1 contrast.
The colour is derived from the state's flags and name (:func:`state_color`), and
stored on ``State.color`` by migration ``0059`` and by the import commands, so
nothing has to pick a colour by hand.
"""

from __future__ import annotations

import re

INITIAL = "#5b6b7f"
IN_PROGRESS = "#3c7f52"
TERMINAL = "#14532d"
UNSUCCESSFUL = "#8c2f2f"

#: Names that mark a record as having ended unsuccessfully. Matched as whole
#: words, case-insensitively, so "Lapsed" and "Bill Lapsed" both count.
_UNSUCCESSFUL_NAME = re.compile(r"\b(withdrawn|lapsed|cancell?ed)\b", re.IGNORECASE)


def is_unsuccessful_name(name: str) -> bool:
    """Whether ``name`` says the record ended without completing."""
    return bool(_UNSUCCESSFUL_NAME.search(name or ""))


def state_color(*, is_initial: bool, is_terminal: bool, name: str = "") -> str:
    """The hex colour for a state with these flags and this name.

    A withdrawn / lapsed / cancelled state is red whatever its flags say; after
    that the initial flag wins over the terminal one, and anything else is in
    progress.
    """
    if is_unsuccessful_name(name):
        return UNSUCCESSFUL
    if is_initial:
        return INITIAL
    if is_terminal:
        return TERMINAL
    return IN_PROGRESS
