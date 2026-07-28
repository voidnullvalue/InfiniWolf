#!/usr/bin/env python3
"""Report names a module uses but never defines, imports, or gets from builtins.

An extraction bug of this shape does not fail at import time -- a NameError in a
moved function body only fires when that function is called -- so neither
`import infiniwolf.x` nor any test that avoids the path will catch it.

Written after moving code between modules produced exactly this twice: campaign.py
called itertools.combinations and dataclasses.replace, both of which had been
imported by generator.py and neither of which came along. The package imported
fine and the pure-logic test tier passed; only generating a map failed. Run this
before the fingerprint gate -- it costs milliseconds, the gate costs minutes.

    tools/unresolved_names.py
"""
import ast, builtins, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

failed = False
for path in sorted((ROOT / "infiniwolf").glob("*.py")):
    tree = ast.parse(path.read_text())
    bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.comprehension,)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name): bound.add(t.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    missing = sorted(used - bound)
    if missing:
        failed = True
        print(f"{path.relative_to(ROOT)}: {', '.join(missing)}")
if not failed:
    print("no unresolved names in any infiniwolf module")
sys.exit(1 if failed else 0)
