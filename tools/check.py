#!/usr/bin/env python3
"""Tiered test runner: pick a gate proportional to what you changed.

The suite is slow for a real reason -- most tests generate whole floors, and a
floor costs a few seconds -- so running all 169 tests after every edit wastes
most of an hour. These tiers exist so the inner loop stays in seconds and the
expensive gate runs when it actually earns its cost.

  --fast    (~2s)   Pure logic: docs, config, CLI, tables, the geometry
                    classifier. No map generation at all. Run constantly.
  --decor   (~15m)  Everything touching decoration, lighting and placement
                    invariants. Run after any change under decorations.py.
  --full    (~50m)  The whole suite, sharded across cores. Run before a commit.

Sharding is done here rather than with pytest-xdist so this works on a bare
checkout with no extra dependencies. Tests are independent (each builds its own
config and RNG), so splitting them across processes is safe.

Sharding buys much less than the core count suggests, and past a point it costs.
Measured on 4 cores: serial 49 min, `-j 4` 55 min -- slower. Generating a floor
churns 4096-element tile and thing planes plus repeated reachability floods, so
concurrent shards contend for memory bandwidth rather than scaling with cores.
The default therefore leaves one core free. Do not reach for a bigger -j to make
the gate fast; reach for a narrower tier.

  tools/check.py --fast
  tools/check.py --decor
  tools/check.py --full -j 4
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Tests that never generate a map, selected by module.
FAST_MODULES = ("tests/test_documentation.py", "tests/test_config.py",
                "tests/test_runtime.py", "tests/test_planning.py",
                "tests/test_grid.py", "tests/test_selection.py",
                "tests/test_quality.py")
# Pure-logic tests inside the generation modules, selected by name.
FAST_NAMES = ("cell_geometry or fill_never or apogee or decor_theme "
              "or no_concept_resolves or lighting_family_items")
# Anything that guards decoration, lighting or placement behaviour.
DECOR_NAMES = (
    "decor or lamp or light or lit or rhythm or fixture or vase or flag "
    "or armor or spear or barrel or statics or reachability or traversal "
    "or doorway or notch or vine or zoned or alcove or corridor or pillar "
    "or geometry or density or motif or sky_vista"
)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, **kwargs)


def collect(target: str, keyword: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", target, "--collect-only", "-q",
           "-p", "no:cacheprovider"]
    if keyword:
        cmd += ["-k", keyword]
    proc = run(cmd, capture_output=True)
    ids = [line.strip() for line in proc.stdout.splitlines()
           if "::" in line and not line.startswith(" ")]
    if not ids:
        sys.stderr.write(proc.stdout + proc.stderr)
    return ids


def shard(ids: list[str], jobs: int) -> list[list[str]]:
    # Round-robin rather than contiguous blocks: the slow generation tests are
    # clustered together in the file, so contiguous shards finish very unevenly.
    buckets: list[list[str]] = [[] for _ in range(jobs)]
    for index, test_id in enumerate(ids):
        buckets[index % jobs].append(test_id)
    return [b for b in buckets if b]


def run_sharded(ids: list[str], jobs: int) -> int:
    buckets = shard(ids, jobs)
    # Shard logs go to a temp dir, never the repo: a crashed run used to leave
    # .check-shard*.log sitting in the working tree waiting to be committed.
    scratch = Path(tempfile.mkdtemp(prefix="infiniwolf-check-"))
    procs = []
    for index, bucket in enumerate(buckets):
        log = scratch / f"shard{index}.log"
        handle = log.open("w")
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--no-header", *bucket]
        procs.append((subprocess.Popen(cmd, cwd=ROOT, stdout=handle,
                                       stderr=subprocess.STDOUT), handle, log,
                      len(bucket)))
    failed = 0
    for proc, handle, log, count in procs:
        code = proc.wait()
        handle.close()
        text = log.read_text()
        tail = [line for line in text.splitlines() if line.strip()]
        summary = tail[-1] if tail else "(no output)"
        status = "ok  " if code == 0 else "FAIL"
        print(f"  [{status}] {count:3d} tests  {summary}")
        if code != 0:
            failed += 1
            # Surface the actual assertion, not just the count.
            for line in text.splitlines():
                if line.startswith(("FAILED", "ERROR", "E   ")):
                    print(f"      {line}")
    shutil.rmtree(scratch, ignore_errors=True)
    return failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    tier = ap.add_mutually_exclusive_group(required=True)
    tier.add_argument("--fast", action="store_true", help="pure logic, no generation")
    tier.add_argument("--decor", action="store_true", help="decoration and lighting guards")
    tier.add_argument("--full", action="store_true", help="the whole suite, sharded")
    ap.add_argument("-j", "--jobs", type=int,
                    default=max(1, (os.cpu_count() or 2) - 1),
                    help="parallel shards (default: %(default)s, one core left free)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    if args.fast:
        print("fast tier: pure logic, no map generation")
        code = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    *FAST_MODULES]).returncode
        code |= run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     "tests/test_generator.py", "-k", FAST_NAMES]).returncode
        failed = code
    elif args.decor:
        print("decor tier: decoration, lighting and placement invariants")
        failed = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                      "tests/test_generator.py", "-k", DECOR_NAMES]).returncode
    else:
        ids = collect("tests/", None)
        if not ids:
            print("could not collect tests")
            return 1
        print(f"full tier: {len(ids)} tests across {args.jobs} shards")
        failed = run_sharded(ids, args.jobs)

    elapsed = time.monotonic() - started
    print(f"\n{'FAILED' if failed else 'PASSED'} in {elapsed:.0f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
