"""Role/tier-to-base-theme policy shared before room identity exists.

This module is neutral because its base-theme decision is consumed before a
``RoomIdentity`` exists.  It therefore belongs neither to ``semantics``, which
resolves identities, nor to ``decorations``, which consumes them.
"""


def base_theme(role: str, tier: str) -> str:
    if tier == "closet":
        return "storage"
    if tier in ("hall", "corridor") or role == "circulation":
        return "corridor"
    if tier == "anchor" or role in ("climax",):
        return "grand"
    if role == "start":
        return "guardpost"
    if role == "relief":
        return "lounge"
    return "barracks"   # beat, branch, ring, hub, filler
