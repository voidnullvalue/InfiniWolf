"""Progression and solvency: can the floor be finished, and is optional optional.

Seeded with the single route contract that both the generator and the validator
read. Stage 5 of the decomposition moves arrival and exit placement, gates, key
objectives, key-state simulation and the secret contracts in here; until then
this deliberately holds one function rather than becoming a grab bag.

Kept a leaf on purpose. Progression consumes finished geometry, so it must never
import the generator, or the bottom-of-file import this module exists to remove
would simply reappear one layer down.
"""

from __future__ import annotations

import math


def _minimum_critical_route_rooms(roles: list[str] | tuple[str, ...]) -> int:
    """Require most of the progression spine, independent of side-room count.

    Optional density must not make a valid exit mathematically impossible.
    Roles used exclusively by optional graph nodes are excluded; a reassigned
    optional exit still adds itself to the requirement and its realized route.
    """
    optional_roles = {"ring", "branch", "closet"}
    spine_rooms = sum(role not in optional_roles for role in roles)
    return max(6, math.ceil(spine_rooms * 0.90))
