"""CLI/GUI launcher for InfiniWolf map-plane provenance checks."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, ttk
import zipfile

from .watermark import verify_path

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
