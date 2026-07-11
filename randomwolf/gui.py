"""Small Tkinter front end for campaign configuration."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
import threading
from tkinter import filedialog, messagebox, ttk

from .config import CampaignConfig, Intensity
from .generator import GenerationCancelled, generate_campaign, read_manifest
from .runtime import AppSettings, launch_ecwolf, load_settings, save_settings, validate_settings


CONTROL_LABELS = (
    ("guard_density", "Guard density"),
    ("enemy_toughness", "Enemy toughness"),
    ("supplies", "Health and ammunition"),
    ("treasure", "Treasure"),
    ("secrets", "Secret frequency"),
    ("locked_doors", "Locked doors"),
    ("layout_complexity", "Layout complexity"),
)
INTENSITY_NAMES = ("", "Very Low", "Low", "Normal", "High", "Very High")


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=16)
        self.grid(sticky="nsew")
        master.title("Random Wolf Campaign Generator")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.seed = tk.StringVar()
        self.status = tk.StringVar(value="Choose settings, then generate a campaign.")
        saved = load_settings()
        self.ecwolf = tk.StringVar(value=saved.ecwolf)
        self.wl6_data = tk.StringVar(value=saved.wl6_data)
        self.output = tk.StringVar(value=saved.output)
        self.values = {name: tk.IntVar(value=3) for name, _ in CONTROL_LABELS}
        self.value_labels = {name: tk.StringVar(value=INTENSITY_NAMES[3]) for name, _ in CONTROL_LABELS}
        self.cancel_event = threading.Event()
        self._build()

    def _build(self) -> None:
        ttk.Label(self, text="Campaign seed (blank = time-based)").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.seed, width=32).grid(row=0, column=1, sticky="ew", padx=(12, 0))
        for row, (name, label) in enumerate(CONTROL_LABELS, 1):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=4)
            scale = ttk.Scale(self, from_=1, to=5, variable=self.values[name], orient="horizontal",
                              command=lambda raw, key=name: self._snap_intensity(key, raw))
            scale.grid(row=row, column=1, sticky="ew", padx=(12, 0))
            ttk.Label(self, textvariable=self.value_labels[name], width=9).grid(row=row, column=2, sticky="e")
        self._path_row(8, "ECWolf executable", self.ecwolf, self._choose_ecwolf, False)
        self._path_row(9, "Registered WL6 data", self.wl6_data, self._choose_data, True)
        self._path_row(10, "Output PK3", self.output, self._choose_output, False)
        self.generate_button = ttk.Button(self, text="Generate", command=self._generate)
        self.generate_button.grid(row=11, column=0, pady=(16, 0), sticky="w")
        self.cancel_button = ttk.Button(self, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.grid(row=11, column=1, pady=(16, 0))
        self.play_button = ttk.Button(self, text="Play", command=self._play)
        self.play_button.grid(row=11, column=2, pady=(16, 0), sticky="e")
        self._sync_play_state()
        ttk.Label(self, textvariable=self.status, wraplength=520).grid(row=12, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.columnconfigure(1, weight=1)

    def _snap_intensity(self, name: str, raw: str) -> None:
        value = min(5, max(1, round(float(raw))))
        self.values[name].set(value)
        self.value_labels[name].set(INTENSITY_NAMES[value])

    def _path_row(self, row: int, label: str, variable: tk.StringVar,
                  command: object, directory: bool) -> None:
        ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(12, 6), pady=(8, 0))
        ttk.Button(self, text="Browse…", command=command).grid(row=row, column=2, pady=(8, 0))

    def _choose_ecwolf(self) -> None:
        value = filedialog.askopenfilename(parent=self, title="Choose ECWolf executable")
        if value: self.ecwolf.set(value)

    def _choose_data(self) -> None:
        value = filedialog.askdirectory(parent=self, title="Choose registered WL6 data directory")
        if value: self.wl6_data.set(value)

    def _choose_output(self) -> None:
        value = filedialog.asksaveasfilename(parent=self, title="Install generated campaign",
                                             defaultextension=".pk3", filetypes=(("ECWolf package", "*.pk3"),))
        if value: self.output.set(value)

    def _settings(self) -> AppSettings:
        return AppSettings(self.ecwolf.get().strip(), self.wl6_data.get().strip(), self.output.get().strip())

    def _sync_play_state(self) -> None:
        exists = bool(self.output.get()) and Path(self.output.get()).expanduser().is_file()
        self.play_button.configure(state="normal" if exists else "disabled")

    def _generate(self) -> None:
        try:
            settings = {name: Intensity(value.get()) for name, value in self.values.items()}
            config = CampaignConfig.with_seed(self.seed.get(), **settings)
        except ValueError as error:
            messagebox.showerror("Invalid configuration", str(error), parent=self)
            return
        self.seed.set(str(config.seed))
        settings = self._settings()
        errors = validate_settings(settings)
        if errors:
            messagebox.showerror("Setup required", "\n".join(errors), parent=self)
            return
        save_settings(settings)
        self.generate_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.play_button.configure(state="disabled")
        self.cancel_event.clear()
        self.status.set("Generating and validating ten maps…")
        output_path = Path(self.output.get()).expanduser()

        def work() -> None:
            try:
                output = generate_campaign(
                    config, output_path,
                    progress=lambda current, total: self.after(0, self._progress, current, total),
                    cancelled=self.cancel_event.is_set,
                )
            except GenerationCancelled:
                self.after(0, self._generation_cancelled)
                return
            except (OSError, RuntimeError, ValueError) as error:
                self.after(0, self._generation_failed, str(error))
                return
            self.after(0, self._generation_finished, output)

        threading.Thread(target=work, name="randomwolf-generator", daemon=True).start()

    def _progress(self, current: int, total: int) -> None:
        self.status.set(f"Generated and validated floor {current} of {total}…")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelling after the current floor…")

    def _generation_cancelled(self) -> None:
        self.generate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._sync_play_state()
        self.status.set("Generation cancelled; the previous campaign was preserved.")

    def _generation_failed(self, error: str) -> None:
        self.generate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._sync_play_state()
        self.status.set("Generation failed; the previous campaign was preserved.")
        messagebox.showerror("Generation failed", error, parent=self)

    def _generation_finished(self, output: Path) -> None:
        self.generate_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._sync_play_state()
        manifest = read_manifest(output)
        floors = manifest["floors"]
        enemies = sum(sum(floor["enemy_tiers"]) for floor in floors)
        secrets = sum(floor["secrets"] for floor in floors)
        locks = sum(floor["locked_doors"] for floor in floors)
        self.status.set(f"Ready — seed {manifest['seed']}; {enemies} enemy placements, "
                        f"{secrets} secrets, {locks} locks. Installed at {output}")

    def _play(self) -> None:
        settings = self._settings()
        try:
            save_settings(settings)
            launch_ecwolf(settings)
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not launch ECWolf", str(error), parent=self)
            return
        self.status.set("ECWolf launched with the generated campaign.")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.minsize(720, 560)
    root.mainloop()
