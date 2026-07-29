"""Deterministic cross-system room-story planning."""
from __future__ import annotations

import hashlib
from .model import Room, RoomIdentity, VignettePlan

# Request-side families are consumed by planning before geometry exists. Their
# local edge indices become concrete FloorPlan edges in SetPiecePlan records.
VIGNETTE_REQUEST_FAMILIES = (
    ("guardpost-supply", ("checkpoint", "supply-cache"), ((0, 1),),
     "checkpoint", "supply-cache"),
    ("barracks-mess", ("barracks", "mess-hall"), ((0, 1),),
     "barracks", "mess-hall"),
    ("prison-processing", ("processing-desk", "holding-cell"), ((0, 1),),
     "processing-desk", "holding-cell"),
    ("officer-suite", ("officers-office", "briefing-room"), ((0, 1),),
     "officers-office", "briefing-room"),
    ("crypt-ossuary", ("crypt", "ossuary"), ((0, 1),),
     "crypt", "ossuary"),
    ("workshop-service", ("workshop", "parts-store"), ((0, 1),),
     "workshop", "parts-store"),
)

_FAMILIES = (
    ("guardpost-supply", {"guardpost", "checkpoint"}, {"supply-cache", "storage", "armory"}, "strongpoint", "supply-cache", "corner-stash"),
    ("barracks-mess", {"barracks", "ready-room"}, {"mess-kitchen", "dining-hall", "lounge"}, "visible-sentry", "recovery", "banquet-row"),
    ("prison-processing", {"checkpoint", "interrogation-room", "guardpost"}, {"holding-cell", "jail"}, "objective-guard", "medical", "doorway-frame"),
    ("officer-suite", {"officers-quarters", "war-room"}, {"lounge", "gallery", "trophy-hall"}, "strongpoint", "treasure", "landmark-frame"),
    ("crypt-ossuary", {"crypt", "burial-chamber"}, {"ossuary", "crypt", "burial-chamber"}, "blind-corner-ambush", "treasure", "signature"),
    ("workshop-service", {"workshop"}, {"storage", "supply-cache"}, "visible-sentry", "supply-cache", "corner-stash"),
)

def plan_vignettes(seed: str, number: int, rooms: list[Room], identities: list[RoomIdentity], edges: list[tuple[int, int]], critical_route: tuple[int, ...], *, special_family: str = "standard") -> tuple[VignettePlan, ...]:
    """Plan zero or one vignette; campaign intent never rerolls by attempt."""
    if number >= 9 or special_family != "standard":
        return ()
    token = int.from_bytes(hashlib.blake2b(f"{seed}:vignette:{number}".encode(), digest_size=2).digest(), "big")
    if token % 3:
        return ()
    blocked = {0, *critical_route[-1:]}
    candidates = []
    for family, first, second, encounter, pickup, decor in _FAMILIES:
        for a, b in sorted(tuple(sorted(edge)) for edge in edges):
            if a in blocked or b in blocked:
                continue
            ca, cb = identities[a].concept, identities[b].concept
            if ca in first and cb in second:
                candidates.append((family, a, b, (ca, cb), encounter, pickup, decor))
            elif cb in first and ca in second:
                candidates.append((family, b, a, (cb, ca), encounter, pickup, decor))
    if not candidates:
        return ()
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    family, owner, partner, concepts, encounter, pickup, decor = candidates[token % len(candidates)]
    return (VignettePlan(family, (owner, partner), concepts, encounter, pickup, decor, (rooms[owner].center, rooms[partner].center), partner),)
