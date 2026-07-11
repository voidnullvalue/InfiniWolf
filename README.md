# Random Wolf

Random Wolf generates deterministic ten-map Wolfenstein 3D campaigns for ECWolf. It uses the player's registered WL6 data at runtime and never copies Wolfenstein graphics, sounds, music, or data files into generated packages.

## Requirements

- Python 3.11 or newer
- ECWolf
- Registered Wolfenstein 3D WL6 data
- Tkinter for the desktop interface

## Desktop interface

```sh
python3 run.py
```

The first launch attempts to find ECWolf and WL6 data automatically. Confirm the paths, choose generation settings and an optional seed, then select **Generate**. Once validation succeeds, select **Play**.

When the tool detects the `/data`, `/mods`, and `/games` layout used by this collection, it installs to `mods/installed/randomwolf/randomwolf.pk3` automatically. The campaign will then also appear in the collection's normal mod selector.

## Command line

```sh
python3 -m randomwolf --seed castle --output randomwolf.pk3
```

Every intensity option accepts `1` through `5`:

```sh
python3 -m randomwolf --seed 42 --guard-density 4 --enemy-toughness 3 \
  --supplies 3 --treasure 2 --secrets 4 --locked-doors 3 \
  --layout-complexity 5 --output randomwolf.pk3
```

Using the same version, seed, and settings produces byte-identical output. A manifest inside the PK3 records the resolved seed, settings, floor seeds, enemy tiers, locks, secrets, and boss floor.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Broader deterministic fuzzing and a real-engine smoke check are also included:

```sh
python3 tools/fuzz.py --seeds 1000
python3 tools/smoke_ecwolf.py --ecwolf /path/to/ecwolf --data /path/to/wl6-data
```

Generated packages contain only WAD map data, MAPINFO, and the reproducibility manifest. Registered WL6 assets remain in the user's data directory.
