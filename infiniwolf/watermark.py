"""Metadata-independent InfiniWolf map watermarking and verification.

Every map carries two independent gameplay-neutral residues.  A complete
campaign additionally makes the ten primary residues sum to 42 modulo 43.
The encoder permutes sound-zone numbers without changing zone membership,
and binds those labels to door geometry and innocuous things-plane entries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
import random
import re
import struct
import sys
import tkinter as tk
from tkinter import filedialog, ttk
import zipfile

GRID = 64
FLOOR, ZONE_MAX = 108, 143
DOORS = frozenset({90, 91, 92, 93, 94, 95, 100, 101})
ELEVATOR_TILE, DUMMY_ELEVATOR_TILE = 21, 85
PUSHWALL = 98
MODULUS = 43
SECONDARY_MODULUS = 17
ITEM_WEIGHTS = {23: 2, 24: 3, 25: 5, 26: 7, 27: 11, 30: 13,
                31: 17, 35: 19, 37: 23, 39: 29, 58: 31, 62: 37}


def floor_target(number: int) -> int:
    """Return the primary per-floor target; all ten targets total 42 mod 43."""
    if not 1 <= number <= 10:
        raise ValueError("floor number must be in the range 1..10")
    first = [((floor * 11 + 7) % MODULUS) for floor in range(1, 10)]
    return first[number - 1] if number < 10 else (42 - sum(first)) % MODULUS


def secondary_target(number: int) -> int:
    if not 1 <= number <= 10:
        raise ValueError("floor number must be in the range 1..10")
    return (number * 5 + 3) % SECONDARY_MODULUS


def _fixed_value(tiles: list[int], things: list[int], number: int) -> int:
    value = 0
    for index, tile in enumerate(tiles):
        x, y = index % GRID, index // GRID
        if tile in DOORS:
            value += x * 5 + y * 7 + tile + number * 3
        thing = things[index]
        if thing in ITEM_WEIGHTS:
            value += ITEM_WEIGHTS[thing] * (1 + (x + 2 * y + number) % 5)
    return value


def plane_residue(tiles: list[int], things: list[int], number: int) -> int:
    value = _fixed_value(tiles, things, number)
    for index, tile in enumerate(tiles):
        if FLOOR <= tile <= ZONE_MAX:
            x, y = index % GRID, index // GRID
            value += tile * (1 + (3 * x + 5 * y + number) % 17)
    return value % MODULUS


def plane_residue_secondary(tiles: list[int], things: list[int], number: int) -> int:
    value = _fixed_value(tiles, things, number) * 3 + number * 7
    for index, tile in enumerate(tiles):
        if FLOOR <= tile <= ZONE_MAX:
            x, y = index % GRID, index // GRID
            value += tile * (1 + (7 * x + 2 * y + number) % 11)
    return value % SECONDARY_MODULUS


def apply_campaign_watermark(levels: list[object], campaign_seed: int) -> None:
    """Encode a complete, independently checkable signature in every map."""
    for level in levels:
        number = int(getattr(level, "number"))
        tiles = getattr(level, "tiles")
        things = getattr(level, "things")
        zones = sorted({tile for tile in tiles if FLOOR <= tile <= ZONE_MAX})
        if not zones:
            raise ValueError(f"floor {number} has no sound zones for watermarking")

        primary_coeff = {zone: 0 for zone in zones}
        secondary_coeff = {zone: 0 for zone in zones}
        for index, tile in enumerate(tiles):
            if tile not in primary_coeff:
                continue
            x, y = index % GRID, index // GRID
            primary_coeff[tile] += 1 + (3 * x + 5 * y + number) % 17
            secondary_coeff[tile] += 1 + (7 * x + 2 * y + number) % 11

        fixed = _fixed_value(tiles, things, number)
        rng = random.Random(campaign_seed ^ getattr(level, "seed") ^ 0x57415445524D4152)
        mapping: dict[int, int] | None = None
        for _ in range(60000):
            labels = rng.sample(range(FLOOR, ZONE_MAX + 1), len(zones))
            candidate = dict(zip(zones, labels))
            primary = fixed + sum(candidate[z] * primary_coeff[z] for z in zones)
            secondary = fixed * 3 + number * 7
            secondary += sum(candidate[z] * secondary_coeff[z] for z in zones)
            if (primary % MODULUS == floor_target(number)
                    and secondary % SECONDARY_MODULUS == secondary_target(number)):
                mapping = candidate
                break
        if mapping is None:
            raise ValueError(f"floor {number} could not encode provenance watermark")
        for index, tile in enumerate(tiles):
            tiles[index] = mapping.get(tile, tile)


@dataclass(frozen=True, slots=True)
class _MapRecord:
    name: str
    tiles: list[int]
    things: list[int]


def _parse_wad(data: bytes) -> _MapRecord:
    if len(data) < 32 or data[:4] != b"PWAD":
        raise ValueError("not a WDC PWAD")
    size = struct.unpack_from("<I", data, 8)[0]
    body = data[12:12 + size]
    if body[:6] != b"WDC3.1":
        raise ValueError("unsupported WAD planes")
    _, planes, name_length = struct.unpack_from("<IHH", body, 6)
    name_start = 14
    name = body[name_start:name_start + name_length].split(b"\0", 1)[0].decode("ascii", "replace")
    offset = name_start + name_length
    width, height = struct.unpack_from("<HH", body, offset)
    offset += 4
    if width != GRID or height != GRID or planes < 2:
        raise ValueError("unsupported map dimensions")
    parsed: list[list[int]] = []
    for _ in range(2):
        if offset + 8192 > len(body):
            raise ValueError("truncated WAD plane")
        parsed.append(list(struct.unpack_from("<4096H", body, offset)))
        offset += 8192
    return _MapRecord(name, parsed[0], parsed[1])


def read_campaign(path: Path) -> list[_MapRecord]:
    with zipfile.ZipFile(path) as package:
        return [_parse_wad(package.read(f"maps/iw{number:02d}.wad"))
                for number in range(1, 11)]


def _structural_match(record: _MapRecord) -> bool:
    evidence = (ELEVATOR_TILE in record.tiles,
                DUMMY_ELEVATOR_TILE in record.tiles,
                any(tile in DOORS for tile in record.tiles),
                PUSHWALL in record.things)
    return sum(evidence) >= 3


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verdict: str
    maps_checked: int
    floor_numbers: tuple[int, ...]
    watermark_floors: int
    secondary_floors: int
    structural_floors: int
    global_42: bool
    residues: tuple[int, ...]
    expected: tuple[int, ...]
    secondary_residues: tuple[int, ...]
    secondary_expected: tuple[int, ...]
    manifest_present: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _verification(records: list[_MapRecord], numbers: list[int], manifest: bool) -> VerificationResult:
    residues = tuple(plane_residue(record.tiles, record.things, number)
                     for record, number in zip(records, numbers))
    expected = tuple(floor_target(number) for number in numbers)
    secondary = tuple(plane_residue_secondary(record.tiles, record.things, number)
                      for record, number in zip(records, numbers))
    secondary_expected = tuple(secondary_target(number) for number in numbers)
    primary_hits = sum(a == b for a, b in zip(residues, expected))
    secondary_hits = sum(a == b for a, b in zip(secondary, secondary_expected))
    structural_hits = sum(_structural_match(record) for record in records)
    count = len(records)
    global_42 = count == 10 and sum(residues) % MODULUS == 42

    if count == 1:
        if primary_hits and secondary_hits and structural_hits:
            verdict = "verified"
        elif primary_hits and secondary_hits:
            verdict = "likely"
        elif not primary_hits and not secondary_hits and not structural_hits:
            verdict = "not-infiniwolf"
        else:
            verdict = "inconclusive"
    elif primary_hits >= 7 and secondary_hits >= 7 and structural_hits >= 7:
        verdict = "verified"
    elif min(primary_hits, secondary_hits) >= 4 or structural_hits >= 8:
        verdict = "likely"
    elif max(primary_hits, secondary_hits) <= 1 and structural_hits <= 3:
        verdict = "not-infiniwolf"
    else:
        verdict = "inconclusive"
    return VerificationResult(verdict, count, tuple(numbers), primary_hits,
                              secondary_hits, structural_hits, global_42,
                              residues, expected, secondary, secondary_expected,
                              manifest)


def verify_campaign(path: Path) -> VerificationResult:
    records = read_campaign(path)
    with zipfile.ZipFile(path) as package:
        manifest = "infiniwolf-manifest.json" in package.namelist()
    return _verification(records, list(range(1, 11)), manifest)


def _infer_floor(record: _MapRecord, path: Path, requested: int | None) -> int:
    if requested is not None:
        if not 1 <= requested <= 10:
            raise ValueError("--floor must be in the range 1..10")
        return requested
    match = re.fullmatch(r"IW(0[1-9]|10)", record.name.upper())
    if match is None:
        match = re.search(r"IW(0[1-9]|10)", path.stem.upper())
    if match is None:
        raise ValueError("cannot infer floor number; pass --floor N")
    return int(match.group(1))


def verify_single_map(path: Path, floor: int | None = None) -> VerificationResult:
    record = _parse_wad(path.read_bytes())
    number = _infer_floor(record, path, floor)
    return _verification([record], [number], False)


def verify_path(path: Path, floor: int | None = None) -> VerificationResult:
    if zipfile.is_zipfile(path):
        if floor is not None:
            raise ValueError("--floor is only used with a standalone WAD")
        return verify_campaign(path)
    return verify_single_map(path, floor)


def _gui() -> int:
    root = tk.Tk()
    root.title("InfiniWolf Provenance Checker")
    root.geometry("660x430")
    path_var = tk.StringVar()
    floor_var = tk.StringVar()
    verdict_var = tk.StringVar(value="Choose a campaign PK3 or standalone WAD.")
    detail = tk.Text(root, width=80, height=16, state="disabled")

    def choose() -> None:
        value = filedialog.askopenfilename(
            parent=root, title="Choose map or campaign",
            filetypes=(("InfiniWolf maps", "*.pk3 *.wad"), ("All files", "*")))
        if value:
            path_var.set(value)

    def inspect() -> None:
        try:
            floor = int(floor_var.get()) if floor_var.get().strip() else None
            result = verify_path(Path(path_var.get()).expanduser(), floor)
            body = result.to_json()
            verdict_var.set(f"Verdict: {result.verdict}")
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            body = f"Could not inspect map/package:\n{error}"
            verdict_var.set("Verdict: error")
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", body)
        detail.configure(state="disabled")

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Entry(frame, textvariable=path_var).grid(row=0, column=0, sticky="ew")
    ttk.Button(frame, text="Browse…", command=choose).grid(row=0, column=1, padx=(8, 0))
    controls = ttk.Frame(frame)
    controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=10)
    ttk.Button(controls, text="Check", command=inspect).pack(side="left")
    ttk.Label(controls, text="Standalone floor (optional):").pack(side="left", padx=(14, 4))
    ttk.Entry(controls, textvariable=floor_var, width=4).pack(side="left")
    ttk.Label(controls, textvariable=verdict_var).pack(side="right")
    detail.grid(row=2, column=0, columnspan=2, sticky="nsew")
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check InfiniWolf map-plane provenance")
    parser.add_argument("package", nargs="?", type=Path,
                        help="campaign PK3 or standalone WAD")
    parser.add_argument("--floor", type=int,
                        help="floor number for a standalone WAD without an IWNN name")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args(argv)
    if args.gui or args.package is None:
        return _gui()
    try:
        result = verify_path(args.package, args.floor)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.as_json:
        print(result.to_json())
    else:
        count = result.maps_checked
        print(f"{result.verdict}: {result.watermark_floors}/{count} primary, "
              f"{result.secondary_floors}/{count} secondary, "
              f"{result.structural_floors}/{count} structural, "
              f"global42={result.global_42}")
    return 0 if result.verdict == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
