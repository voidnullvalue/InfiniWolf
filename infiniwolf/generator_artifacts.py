"""ECWolf artifact encoding and package verification."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import zipfile

from . import __version__

from .build_info import COMMIT as BUILD_COMMIT
from .config import CampaignConfig
from .wl6 import GRID
from .watermark import (_parse_wad, floor_target, plane_residue,
                        plane_residue_secondary, secondary_target)

# MAPINFO presentation tables. These live here rather than in the generator
# because nothing in generation reads them -- they are consumed only by the
# encoders below, and keeping them here is what lets this module stop importing
# `generator` and closes half of the import cycle that previously forced
# generator.py to import validation and artifacts from its own last lines.
CEILINGS = ("#383838", "#202840", "#402828", "#303820", "#382840")
MUSIC = ("GETTHEM", "SEARCHN", "POW", "SUSPENSE", "WARMARCH", "NAZI_OMI")

# In-game display flavor for mapinfo level names.
_VARIANT_TITLES = {
    "garrison": "The Garrison",
    "catacombs": "The Catacombs",
    "grand-halls": "Grand Halls",
    "storehouse": "The Storehouse",
    "quarters": "Officers' Quarters",
    "stronghold": "The Stronghold",
    "vault": "Treasure Vault",
}

def _wad_bytes(name: str, tiles: list[int], things: list[int]) -> bytes:
    planes = (tiles, things, [0] * (GRID * GRID))
    map_name = name.encode("ascii")[:15].ljust(16, b"\0")
    payload = b"WDC3.1" + struct.pack("<IHH", 1, 3, 16) + map_name + struct.pack("<HH", GRID, GRID)
    payload += b"".join(struct.pack("<4096H", *plane) for plane in planes)
    marker = name.encode("ascii")[:8].ljust(8, b"\0")
    directory = struct.pack("<II8s", 12, 0, marker) + struct.pack("<II8s", 12, len(payload), b"PLANES\0\0")
    return b"PWAD" + struct.pack("<II", 2, 12 + len(payload)) + payload + directory


def _mapinfo(secret_from: int, variants: tuple[str, ...] = ()) -> str:
    lines = [
        'gameinfo { drawreadthis = false }',
        'clearepisodes',
        'episode "IW01" { name = "InfiniWolf" key = "I" }',
    ]
    for number in range(1, 10):
        # ECWolf only recognizes "EndSequence:<id>" or "EndTitle" as a real
        # end-of-game next-map value (see wl_game.cpp); anything else,
        # including the Doom/ZDoom-only "EndGameC" cast-call keyword this
        # used to say, is treated as a literal (nonexistent) map name and
        # crashes on exit once LevelInfo::Find fails to resolve it.
        nxt = f'IW{number + 1:02d}' if number < 9 else 'EndTitle'
        secret = ' secretnext = "IW10"' if number == secret_from else ''
        ceiling = CEILINGS[(number - 1) % len(CEILINGS)]
        music = MUSIC[(number - 1) % len(MUSIC)]
        par = 90 + number * 30
        title = (_VARIANT_TITLES.get(variants[number - 1], "")
                 if number <= len(variants) else "")
        name = f"Floor {number}: {title}" if title else f"Random Floor {number}"
        # ECWolf's MAPINFO FloorNumber defaults to "1" for any map that does
        # not set it (g_mapinfo.cpp), so without this every floor reads
        # "Floor 1" on the status bar and the score tally.
        lines.append(f'map "IW{number:02d}" "{name}" {{ next = "{nxt}"{secret} '
                     f'levelnum = {number} floornumber = {number} par = {par} '
                     f'defaultceiling = "{ceiling}" music = "{music}" }}')
    lines.append(f'map "IW10" "Secret Floor" {{ next = "IW{secret_from + 1:02d}" levelnum = 10 '
                 f'floornumber = 10 par = 360 defaultceiling = "{CEILINGS[4]}" music = "{MUSIC[5]}" }}')
    return "\n".join(lines) + "\n"


def _display_name(number: int, variant: str = "") -> str:
    if number == 10:
        return "Secret Floor"
    title = _VARIANT_TITLES.get(variant, "")
    return f"Floor {number}: {title}" if title else f"Random Floor {number}"


def _reproducibility_text(config: CampaignConfig, secret_from: int) -> str:
    """Human-readable, copyable record of every campaign input."""
    settings = json.loads(config.to_json())
    commit = BUILD_COMMIT or "unknown"
    lines = [
        "InfiniWolf campaign reproducibility record",
        "==========================================",
        "",
        f"version = {__version__}",
        f"commit = {commit}",
        "seed_source = LittleEntropyMachine",
        f"seed = {config.seed}",
        f"secret_floor_source = {secret_from}",
        "",
        "Resolved settings",
        "-----------------",
    ]
    for name, value in settings.items():
        if name != "seed":
            lines.append(f"{name} = {value}")
    arguments = [f"--seed {config.seed}"]
    for name, value in settings.items():
        if name == "seed":
            continue
        if name == "say_aardwolf":
            if value:
                arguments.append("--say-aardwolf")
            continue
        arguments.append(f"--{name.replace('_', '-')} {value}")
    lines.extend((
        "",
        "Reproduction command",
        "--------------------",
        "python3 -m infiniwolf " + " ".join(arguments)
        + " --output infiniwolf.pk3",
        "",
        "The same InfiniWolf version and commit are required for byte-identical output.",
        "",
    ))
    return "\n".join(lines)




def read_manifest(package_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(package_path) as package:
        return json.loads(package.read("infiniwolf-manifest.json"))


def validate_package(package_path: Path) -> dict[str, object]:
    """Reopen and parse a completed temporary package before installation."""
    with zipfile.ZipFile(package_path) as package:
        corrupt = package.testzip()
        if corrupt:
            raise ValueError(f"corrupt package entry: {corrupt}")
        names = set(package.namelist())
        expected_maps = {f"maps/iw{number:02d}.wad" for number in range(1, 11)}
        if not expected_maps.issubset(names):
            raise ValueError("package is missing one or more campaign maps")
        if not {"mapinfo.txt", "infiniwolf-manifest.json",
                "infiniwolf-settings.txt"}.issubset(names):
            raise ValueError("package is missing required reproducibility metadata")
        forbidden = (".wl6", ".png", ".wav", ".ogg", ".voc")
        if any(name.lower().endswith(forbidden) for name in names):
            raise ValueError("package contains an asset file instead of map metadata")
        manifest = json.loads(package.read("infiniwolf-manifest.json"))
        if len(manifest.get("floors", ())) != 10:
            raise ValueError("manifest does not describe ten floors")
        settings_text = package.read("infiniwolf-settings.txt").decode("utf-8")
        settings_lines = set(settings_text.splitlines())
        required_settings = {
            f"version = {manifest.get('version')}",
            f"commit = {manifest.get('commit')}",
            f"seed = {manifest.get('seed')}",
            f"secret_floor_source = {manifest.get('secret_from')}",
            "seed_source = LittleEntropyMachine",
        }
        required_settings.update(
            f"{name} = {value}"
            for name, value in manifest.get("settings", {}).items()
            if name != "seed")
        if not required_settings <= settings_lines:
            raise ValueError("reproducibility text disagrees with the manifest")
        for name in expected_maps:
            wad = package.read(name)
            if len(wad) < 46 or wad[:4] != b"PWAD" or wad[12:18] != b"WDC3.1":
                raise ValueError(f"{name} has an invalid ECWolf WAD header")
            width, height = struct.unpack_from("<HH", wad, 42)
            if (width, height) != (GRID, GRID):
                raise ValueError(f"{name} is not a {GRID}x{GRID} map")
        # Provenance is part of the installed artifact contract, not merely
        # descriptive manifest data. Recompute it from the map planes before
        # the temporary package is allowed to replace the previous campaign.
        primary = []
        for number in range(1, 11):
            record = _parse_wad(package.read(f"maps/iw{number:02d}.wad"))
            first = plane_residue(record.tiles, record.things, number)
            second = plane_residue_secondary(record.tiles, record.things, number)
            if first != floor_target(number) or second != secondary_target(number):
                raise ValueError(f"IW{number:02d} provenance watermark is invalid")
            primary.append(first)
        if sum(primary) % 43 != 42:
            raise ValueError("campaign provenance residue is not 42")
        return manifest


def _manifest(config, levels, secret_from, vine_floor, vine_budget,
              realized_vine_runs, gallery_floor, realized_gallery_floors,
              rare_motif_floor, realized_rare, vista_parity, lock_schedule):
    """Build the manifest recorded inside the package.

    Lives with encoding rather than orchestration: it is a serialization of
    decisions already made, and every value it reads is final by the time it
    runs. Keeping it here means adding a manifest field cannot accidentally
    change generation, and generate_campaign stays a control-flow function
    rather than a 140-line dict literal.
    """
    manifest = {
        "generator": "infiniwolf", "version": __version__,
        "commit": BUILD_COMMIT or "unknown", "seed": config.seed,
        "seed_source": "LittleEntropyMachine",
        "watermark": {"scheme": "zone-item-geometry-v2",
                      "primary_modulus": 43, "secondary_modulus": 17,
                      "per_map": True, "campaign_residue": 42},
        "settings": json.loads(config.to_json()), "secret_from": secret_from,
        "vine_schedule": {"floor": vine_floor, "requested_runs": vine_budget,
                          "realized_runs": realized_vine_runs},
        "guard_gallery_schedule": {"floor": gallery_floor,
                                   "realized": bool(realized_gallery_floors)},
        "rare_motif_schedule": {"floor": rare_motif_floor,
                                "realized_floor": (realized_rare[0]
                                                   if realized_rare else 0)},
        "sky_vista_schedule": {
            "eligible_parity": vista_parity,
            "realized_floors": [level.number for level in levels
                                if level.sky_vistas]},
        "lock_schedule": [plan.colors for plan in lock_schedule],
        "floors": [{"number": level.number,
                    "name": _display_name(level.number, level.variant),
                    "seed": level.seed,
                    "secrets": len(level.secret_rewards),
                    "locked_doors": level.locked_doors,
                    "key_order": level.key_order,
                    "critical_route_rooms": len(level.critical_route),
                    "exit_depth_ratio": round(level.exit_depth_ratio, 4),
                    "exit_stand": level.exit_stand,
                    "boss": level.boss,
                    "special_family": level.special_family,
                    "secret_source": level.secret_source,
                    "boss_arena_room": level.boss_arena_room,
                    "preboss_room": level.preboss_room,
                    "premium_room": level.premium_room,
                    "expedition_rooms": level.expedition_rooms,
                    "arrival": ({"kind": level.arrival.kind,
                                  "portal": level.arrival.portal,
                                  "player": level.arrival.player,
                                  "facing": level.arrival.facing,
                                  "car_cells": level.arrival.car_cells,
                                  "item": level.arrival.item}
                                 if level.arrival else None),
                    "guard_recesses": [
                        {"room": recess.room_index, "cells": recess.cells,
                         "actor_cell": recess.actor_cell}
                        for recess in level.guard_recesses],
                    "guard_galleries": [
                        {"room": gallery.room_index, "screen": gallery.screen,
                         "actors": gallery.actor_cells,
                         "rear_cells": gallery.rear_cells,
                         "treatment": gallery.treatment}
                        for gallery in level.guard_galleries],
                    "encounters": [
                        {"template": encounter.template,
                         "room": encounter.room_index,
                         "actors": [item for _, _, item in encounter.cells],
                         "hidden_cells": encounter.hidden_cells,
                         "family": encounter.family,
                         "patrol_kind": encounter.patrol_kind,
                         "patrol_path": encounter.patrol_path}
                        for encounter in level.encounters],
                    "patrol_target": level.patrol_target,
                    "enemy_tiers": level.enemy_tiers,
                    "variant": level.variant,
                    "circulation_skeleton": level.circulation_skeleton,
                    "progression_grammar": level.progression_grammar,
                    "district_circulation": level.district_circulation,
                    "layout_signature": level.layout_signature,
                    "primary_hall_geometry": level.primary_hall_geometry,
                    "barrel_families": level.barrel_families,
                    "sky_vistas": level.sky_vistas,
                    "sky_vista_recesses": level.sky_vista_recesses,
                    "sky_vista_supports": level.sky_vista_supports,
                    "door_axis_parity": [
                        {"room": index, "width": room.w,
                         "height": room.h,
                         "odd_width": bool(room.w % 2),
                         "odd_height": bool(room.h % 2)}
                        for index, room in enumerate(level.rooms)],
                    "motif_realizations": level.motif_realizations,
                    "shape_target": level.shape_target,
                    "rare_motif": ({"kind": level.rare_motif.kind,
                                    "room": level.rare_motif.room_index,
                                    "realization": level.rare_motif.realization,
                                    "endpoints": level.rare_motif.endpoints}
                                   if level.rare_motif else None),
                    "boss_arena": ({"family": level.boss_arena.family,
                                    "profile": level.boss_arena.profile,
                                    "geometry": level.boss_arena.geometry,
                                    "decorations": level.boss_arena.decorations}
                                   if level.boss_arena else None),
                    "landmarks": [
                        {"room": plan.room_index, "rank": plan.rank,
                         "purpose": plan.purpose, "score": plan.score,
                         "approach_room": plan.approach_room}
                        for plan in level.landmarks],
                    # The one composition each room realized, "" for the rooms
                    # deliberately left plain. This is the audit trail for motif
                    # selection: without it there is no way to tell from a package
                    # whether a floor's rooms were composed or merely filled.
                    "shared_void": ({"family": level.shared_void.family,
                                     "interior": len(level.shared_void.interior),
                                     "screens": len(level.shared_void.screens),
                                     "viewing_rooms": list(
                                         level.shared_void.viewing_rooms)}
                                    if level.shared_void else None),
                    "authored_sightlines": [
                        {"purpose": line.purpose, "length": line.length,
                         "origin_room": line.origin_room,
                         "target_room": line.target_room,
                         "cells": [list(c) for c in line.cells]}
                        for line in level.authored_sightlines],
                    "room_motifs": level.room_motifs,
                    "room_concepts": level.room_concepts,
                    "room_shapes": level.room_shapes,
                    "lighting_families": level.lighting_families,
                    "vine_screens": [
                        {"kind": screen.kind, "room": screen.room_index,
                         "cells": screen.cells,
                         "ambush_anchor": screen.ambush_anchor}
                        for screen in level.vine_screens],
                    "motifs": level.motifs,
                    "secret_variants": level.secret_variants,
                    "secret_details": [
                        {"shape": detail.shape,
                         "reward_count": detail.reward_count,
                         "host_room": detail.host_room,
                         "depth_ratio": round(detail.depth_ratio, 4),
                         "pushwall": detail.pushwall,
                         "secret_exit": detail.secret_exit,
                         "hint_treatment": detail.hint_treatment,
                         "return_floor": detail.return_floor,
                         "push_direction": detail.push_direction}
                        for detail in level.secret_details],
                    "key_objectives": [
                        {"color": objective.color, "cell": objective.cell,
                         "host_room": objective.host_room,
                         "stage": objective.stage, "detour": objective.detour,
                         "treatment": objective.treatment}
                        for objective in level.key_objectives],
                    "pickup_compositions": [
                        {"reason": placement.reason,
                         "template": placement.template,
                         "room": placement.room_index,
                         "items": [item for _, _, item in placement.cells]}
                        for placement in level.pickup_placements],
                    "critique": level.critique,
                    "validation": {
                        "passed": True,
                        "checks": ["bounds", "connectivity", "door_axes", "elevator",
                                   "exit_depth", "critical_route",
                                   "dual_key_progression", "key_room_separation",
                                   "pushwall_clearance", "rewarded_secrets",
                                   "secret_hints", "secret_route", "boss",
                                   "circulation_hierarchy", "arrival_elevator",
                                   "hallway_first_scaffold", "sky_vista_depth",
                                   "room_barrel_family", "wall_backed_blue_urn",
                                   "encounter_provenance", "patrol_routes",
                                   "wall_backed_flags", "pickup_provenance"],
                    }} for level in levels],
    }
    return manifest
