#!/usr/bin/env python3
"""Inventory every write to the shared cell-reservation set.

Placement systems coordinate through one untyped `set[tuple[int, int]]` called
`reserved`. Whoever adds a cell records that *something* wants it, but not who, not
why, and not whether a later pass may take it back. That is the gap behind several
real bugs in this project: a flush-to-wall repair pulled one member out of a
matched pair because nothing recorded that the pair was a composition, and a
zoning test could not distinguish a fill-placed prop from a zone-placed one
because the things plane carries no provenance either.

Replacing the set with a typed ledger is a large change, and the first thing it
needs is an honest inventory: how many owners write to the set, what each is
protecting, and which writes are later released. This produces that inventory from
the AST, so it stays accurate as the code moves.

    tools/reservation_sites.py            # grouped summary
    tools/reservation_sites.py --detail   # every site with its reason comment
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"reserved", "keep_clear", "blocked_cells", "protected",
         "secret_protected", "composed_cells", "room_blocked"}
MUTATORS = {"add", "update", "discard", "remove", "difference_update",
            "intersection_update", "clear"}
RELEASING = {"discard", "remove", "difference_update", "clear"}


def shared_collections(tree: ast.AST) -> set[str]:
    """Names that arrive as parameters, i.e. are shared across module boundaries.

    A collection built and consumed inside one function has exactly one owner by
    construction and needs no ledger -- `keep_clear` never leaves the decoration
    pass. Only the sets handed between modules can suffer the ownership problem,
    and counting the locals alongside them overstates it.
    """
    shared = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if arg.arg in NAMES:
                    shared.add(arg.arg)
    return shared


def enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every line number to the innermost function that contains it."""
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                # Inner definitions win: walk order is outer-first, so a later
                # assignment for the same line is the more deeply nested one.
                owner[line] = node.name
    return owner


def reason_for(lines: list[str], index: int) -> str:
    """The nearest comment above a site, which is where the why usually lives."""
    reason = []
    cursor = index - 2
    while cursor >= 0 and lines[cursor].strip().startswith("#"):
        reason.insert(0, lines[cursor].strip().lstrip("# ").rstrip())
        cursor -= 1
    return " ".join(reason)[:110]


def collect():
    sites = []
    for path in sorted((ROOT / "infiniwolf").glob("*.py")):
        source = path.read_text()
        lines = source.splitlines()
        tree = ast.parse(source)
        owners = enclosing_functions(tree)
        shared = shared_collections(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in MUTATORS
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in NAMES):
                continue
            sites.append({
                "module": path.stem,
                "line": node.lineno,
                "collection": node.func.value.id,
                "operation": node.func.attr,
                "function": owners.get(node.lineno, "<module>"),
                "releases": node.func.attr in RELEASING,
                "reason": reason_for(lines, node.lineno),
                "shared": node.func.value.id in shared,
            })
    return sites


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--detail", action="store_true",
                    help="list every site, not just the summary")
    args = ap.parse_args(argv)

    sites = collect()
    by_collection = Counter(site["collection"] for site in sites)
    by_module = defaultdict(Counter)
    for site in sites:
        by_module[site["module"]][site["collection"]] += 1

    shared_sites = [s for s in sites if s["shared"]]
    local_sites = [s for s in sites if not s["shared"]]
    print(f"{len(sites)} reservation writes across {len(by_module)} modules\n"
          f"  {len(shared_sites)} to collections passed between modules "
          f"(these need provenance)\n"
          f"  {len(local_sites)} to function-local working sets "
          f"(single owner by construction)\n")
    print(f"{'collection':<18}{'writes':>7}{'releases':>10}  modules")
    for name, count in by_collection.most_common():
        releases = sum(1 for s in sites
                       if s["collection"] == name and s["releases"])
        mods = sorted({s["module"] for s in sites if s["collection"] == name})
        print(f"{name:<18}{count:>7}{releases:>10}  {', '.join(mods)}")

    unexplained = [s for s in shared_sites if not s["reason"]]
    print(f"\n{len(unexplained)} of {len(shared_sites)} cross-module writes carry "
          f"no explanatory comment")
    if shared_sites:
        print("Cross-module writes should go through ledger.reserve(), which makes "
              "owner and reason required rather than conventional.")

    if args.detail:
        print()
        for site in sites:
            flag = "release" if site["releases"] else "reserve"
            print(f"{site['module']}:{site['line']:<5} {flag:<8} "
                  f"{site['collection']:<16} in {site['function']}")
            if site["reason"]:
                print(f"      why: {site['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
