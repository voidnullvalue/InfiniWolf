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

    Claims accumulate in insertion order. A reservation remains in the set
    until its final claim is released, so one subsystem cannot accidentally free
    a cell another subsystem still needs.
    """

    __slots__ = ("_claims",)

    def __init__(self, cells=(), *, owner: str = "unattributed",
                 reason: str = "pre-existing") -> None:
        super().__init__(cells)
        self._claims: dict[Cell, list[Claim]] = {
            cell: [Claim(owner, reason)] for cell in self}

    # -- attributed writes -------------------------------------------------

    def reserve(self, cells, owner: str, reason: str, *, hard: bool = True,
                room_index: int = -1) -> None:
        """Claim cells with provenance, preserving every distinct claim."""
        claim = Claim(owner, reason, hard, room_index)
        for cell in cells:
            super().add(cell)
            claims = self._claims.setdefault(cell, [])
            if not any((existing.owner, existing.reason)
                       == (owner, reason) for existing in claims):
                claims.append(claim)

    def release(self, cells, owner: str, reason: str) -> list[Cell]:
        """Drop this owner's claims. Returns cells from which claims were removed.

        Claims belonging to other owners, including soft claims, remain intact.
        A hard claim is therefore never removable by another owner.
        """
        released = []
        for cell in list(cells):
            claims = self._claims.get(cell)
            if not claims:
                continue
            remaining = [claim for claim in claims if claim.owner != owner]
            if len(remaining) == len(claims):
                continue
            if remaining:
                self._claims[cell] = remaining
            else:
                super().discard(cell)
                del self._claims[cell]
            released.append(cell)
        return released

    # -- set compatibility -------------------------------------------------

    def add(self, cell) -> None:
        super().add(cell)
        claims = self._claims.setdefault(cell, [])
        if not any((claim.owner, claim.reason) == ("unattributed", "add")
                   for claim in claims):
            claims.append(Claim("unattributed", "add"))

    def update(self, *others) -> None:
        for other in others:
            for cell in other:
                self.add(cell)

    def discard(self, cell) -> None:
        self.release([cell], "unattributed", "discard")

    def remove(self, cell) -> None:
        if cell not in self:
            raise KeyError(cell)
        self.discard(cell)

    def difference_update(self, *others) -> None:
        for other in others:
            for cell in list(other):
                self.discard(cell)

    def clear(self) -> None:
        self.release(list(self), "unattributed", "clear")

    # -- diagnostics -------------------------------------------------------

    def explain(self, cell) -> list[Claim]:
        """Claims for a cell, in reservation order, or an empty list."""
        return list(self._claims.get(cell, ()))

    def report(self) -> dict[str, dict[str, int]]:
        """Claim and uniquely reserved-cell counts per owner."""
        claims = Counter()
        cells = Counter()
        for cell_claims in self._claims.values():
            owners = set()
            for claim in cell_claims:
                claims[claim.owner] += 1
                owners.add(claim.owner)
            for owner in owners:
                cells[owner] += 1
        return {owner: {"claims": claims[owner], "cells": cells[owner]}
                for owner in claims.keys() | cells.keys()}


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
