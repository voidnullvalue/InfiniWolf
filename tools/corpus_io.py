#!/usr/bin/env python3
"""One loader for the hand-authored Wolf3D map corpus.

Every authored map on this machine lives inside a .pk3 (a plain zip), so a tool
that walks *.wad on disk finds nothing -- which is exactly why the --compare
mode documented in DESIGN.md silently measured zero maps for so long. Three
tools needed this walk and two of them had grown private copies; this is the
shared one.
"""
from __future__ import annotations

from dataclasses import dataclass
import glob
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_map import parse_wad

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = str(ROOT.parent / "installed" / "*" / "*.pk3")


@dataclass(frozen=True, slots=True)
class CorpusMap:
    label: str
    tiles: list[int]
    things: list[int]
    width: int
    height: int


def iter_corpus_maps(pattern: str = DEFAULT_CORPUS, *, include_own: bool = False):
    """Yield every readable authored map matching a .pk3 glob.

    Skips anything under an 'infiniwolf' path unless asked otherwise: measuring
    our own output as if it were authored is how a generator convinces itself
    it has already succeeded.
    """
    for path in sorted(glob.glob(pattern)):
        if not include_own and "infiniwolf" in path:
            continue
        mod = Path(path).parent.name
        try:
            package = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            continue
        with package:
            for name in package.namelist():
                if not name.lower().endswith(".wad"):
                    continue
                try:
                    parsed = parse_wad(package.read(name))
                except (OSError, zipfile.BadZipFile):
                    continue
                if parsed is None:
                    continue
                tiles, things, width, height = parsed
                yield CorpusMap(f"{mod}/{name}", tiles, things, width, height)


def corpus_inventory(pattern: str = DEFAULT_CORPUS) -> dict[str, dict[str, int]]:
    """Per-pack counts of .wad members vs. readable maps, by map size.

    Reported rather than swallowed: a pack whose maps are all 128x128 used to
    vanish from every survey without a word.
    """
    inventory: dict[str, dict[str, int]] = {}
    for path in sorted(glob.glob(pattern)):
        if "infiniwolf" in path:
            continue
        mod = Path(path).parent.name
        entry = inventory.setdefault(mod, {"wads": 0, "readable": 0})
        try:
            package = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile):
            continue
        with package:
            for name in package.namelist():
                if not name.lower().endswith(".wad"):
                    continue
                entry["wads"] += 1
                parsed = parse_wad(package.read(name))
                if parsed is None:
                    continue
                entry["readable"] += 1
                size = f"{parsed[2]}x{parsed[3]}"
                entry[size] = entry.get(size, 0) + 1
    return inventory


if __name__ == "__main__":
    pattern = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CORPUS
    total = 0
    for mod, entry in sorted(corpus_inventory(pattern).items()):
        sizes = " ".join(f"{k}={v}" for k, v in sorted(entry.items())
                         if k not in {"wads", "readable"})
        print(f"{mod:20s} wads={entry['wads']:3d} readable={entry['readable']:3d}  {sizes}")
        total += entry["readable"]
    print(f"{'TOTAL':20s} readable={total}")
