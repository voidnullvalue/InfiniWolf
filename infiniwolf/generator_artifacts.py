"""ECWolf artifact encoding and package verification."""

from __future__ import annotations

import json
from pathlib import Path
import struct
import zipfile

from .generator import (
    BUILD_COMMIT, CEILINGS, CampaignConfig, GRID, MUSIC, _VARIANT_TITLES,
    __version__,
)

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
        from .watermark import (floor_target, plane_residue,
                                plane_residue_secondary, secondary_target,
                                _parse_wad)
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
