"""Shared structural records.

The record types more than one generation system reads. A type belongs here when
it is a *shared decision* -- something planning writes and geometry, progression,
semantics, encounters, pickups or decoration later consult -- not merely because
it is a dataclass. Subsystem-private records stay with their subsystem.

Imports only the WL6 vocabulary, so this is a leaf alongside `wl6` and `grid`.
Nothing here may import a generation module; that is what keeps the dependency
graph acyclic and lets validation and artifact encoding read these types without
reaching back into the generator.
"""

from __future__ import annotations

from dataclasses import dataclass

from .wl6 import WALL


@dataclass(frozen=True, slots=True)
class Room:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

@dataclass(frozen=True, slots=True)
class RoomSpec:
    role: str
    tier: str
    district: int
    motif: str = "spine"

@dataclass(frozen=True, slots=True)
class RoomIdentity:
    """One semantic decision shared by wall, population and decor passes."""
    role: str
    tier: str
    motif: str
    district: int
    variant: str
    concept: str
    base_theme: str
    wall_base: int = WALL
    special: str = ""

@dataclass(frozen=True, slots=True)
class SpritePlacement:
    """Auditable proof that sprites belong to an authored composition."""
    reason: str
    template: str
    room_index: int
    cells: tuple[tuple[int, int, int], ...]

@dataclass(frozen=True, slots=True)
class VineScreen:
    """One complete, auditable vine pseudowall composition."""
    kind: str
    room_index: int
    cells: tuple[tuple[int, int], ...]
    ambush_anchor: tuple[int, int] | None = None
