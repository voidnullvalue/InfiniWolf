"""Typed provenance for cell reservations.

Placement systems coordinate through one shared set of cells. Adding a cell says
that *something* wants it, but not who, not why, and not whether a later pass may
take it back. tools/reservation_sites.py counts 51 writes across six modules, 49 of
them with no stated reason, and that gap has produced real bugs: a flush-to-wall
repair pulled one member out of a matched pair because nothing recorded the pair was
a composition, and a secret pocket's footprint went unreserved for four seeds
because a refactor consumed the line that reserved it.

`Ledger` is deliberately a `set` subclass. Every existing `.add()`, `.update()`,
`in`, `|` and `-` keeps working untouched, so adopting it changes no behaviour and
cannot perturb generated output -- which the fingerprint gate then proves rather
than assumes. Attribution is added incrementally: a pass that calls `reserve()`
records its owner and reason, a pass that still calls `.add()` is recorded as
unattributed, and `report()` shows how far the migration has got.

The eventual goal is that `explain(cell)` answers "why is this cell off limits"
for any cell, so a placement conflict produces a specific diagnostic instead of a
silent skip. Getting there does not require converting every caller at once.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

Cell = tuple[int, int]


@dataclass(frozen=True, slots=True)
class Claim:
    """Why one cell is reserved."""
    owner: str
    reason: str
    hard: bool = True
    room_index: int = -1

    def __str__(self) -> str:
        where = f" room {self.room_index}" if self.room_index >= 0 else ""
        return f"{self.owner}:{self.reason}{where}{'' if self.hard else ' (soft)'}"


class Ledger(set):
    """A cell reservation set that can say who reserved what, and why.

    Soft claims are advisory: a later phase may knowingly release one, and
    `release()` refuses to drop a hard claim so a decoration pass cannot quietly
    overwrite a progression reservation. Both are recorded either way -- the
    ledger's first job is to describe what already happens, not to start
    forbidding things.
    """

    __slots__ = ("_claims",)

    def __init__(self, cells=(), *, owner: str = "unattributed",
                 reason: str = "pre-existing") -> None:
        super().__init__(cells)
        self._claims: dict[Cell, Claim] = {
            cell: Claim(owner, reason) for cell in self}

    # -- attributed writes -------------------------------------------------

    def reserve(self, cells, owner: str, reason: str, *, hard: bool = True,
                room_index: int = -1) -> None:
        """Claim cells with provenance. Re-claiming keeps the first owner.

        First-writer-wins matches the existing semantics of a set: whoever got
        there first is why the cell is unavailable, and a second claim adding
        nothing should not rewrite history.
        """
        claim = Claim(owner, reason, hard, room_index)
        for cell in cells:
            super().add(cell)
            self._claims.setdefault(cell, claim)

    def release(self, cells, owner: str, reason: str) -> list[Cell]:
        """Drop soft or own claims; refuse others. Returns what was released."""
        released = []
        for cell in list(cells):
            claim = self._claims.get(cell)
            if claim is not None and claim.hard and claim.owner != owner:
                continue
            self.discard(cell)
            self._claims.pop(cell, None)
            released.append(cell)
        return released

    # -- set compatibility -------------------------------------------------

    def add(self, cell) -> None:
        super().add(cell)
        self._claims.setdefault(cell, Claim("unattributed", "add"))

    def update(self, *others) -> None:
        for other in others:
            for cell in other:
                self.add(cell)

    def discard(self, cell) -> None:
        super().discard(cell)
        self._claims.pop(cell, None)

    def remove(self, cell) -> None:
        super().remove(cell)
        self._claims.pop(cell, None)

    def difference_update(self, *others) -> None:
        for other in others:
            for cell in list(other):
                self.discard(cell)

    def clear(self) -> None:
        super().clear()
        self._claims.clear()

    # -- diagnostics -------------------------------------------------------

    def explain(self, cell) -> str:
        """Why this cell is unavailable, in one line."""
        if cell not in self:
            return f"{cell} is not reserved"
        claim = self._claims.get(cell)
        return f"{cell} reserved by {claim}" if claim else f"{cell} reserved (no claim)"

    def report(self) -> Counter:
        """Reserved-cell counts per owner, for migration progress."""
        return Counter(claim.owner for claim in self._claims.values())


def reserve(target, cells, owner: str, reason: str, *, hard: bool = True,
            room_index: int = -1) -> None:
    """Attribute a reservation if `target` is a Ledger, else just add the cells.

    Subsystems receive the reservation set as a parameter, and unit tests
    legitimately hand them a plain `set` -- a decoration test should not have to
    construct a ledger to check where a lamp goes. This keeps attribution optional
    at the call site so a module can be migrated without breaking its tests, and
    without any `isinstance` checks scattered through placement code.
    """
    if isinstance(target, Ledger):
        target.reserve(cells, owner, reason, hard=hard, room_index=room_index)
    else:
        target.update(cells)
