# InfiniWolf

[![CI](https://github.com/voidnullvalue/InfiniWolf/actions/workflows/release.yml/badge.svg)](https://github.com/voidnullvalue/InfiniWolf/actions/workflows/release.yml)
[![Latest release](https://img.shields.io/github/v/release/voidnullvalue/InfiniWolf?display_name=tag&sort=date)](https://github.com/voidnullvalue/InfiniWolf/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/voidnullvalue/InfiniWolf/total)](https://github.com/voidnullvalue/InfiniWolf/releases)
[![License](https://img.shields.io/github/license/voidnullvalue/InfiniWolf)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)

InfiniWolf generates deterministic ten-map Wolfenstein 3D campaigns for ECWolf. It uses the player's registered WL6 data at runtime and never copies Wolfenstein graphics, sounds, music, or data files into generated packages.

Curious how the generator actually works — floor grammar, room realisation, actor placement, and the rayscore facing pass? See [`DESIGN.md`](DESIGN.md).

## Prebuilt release (Windows / macOS / Linux)

Every tagged release publishes a self-contained `.zip` per platform on the [Releases page](../../releases) — no Python install required. Each one bundles:

- `InfiniWolf` — the desktop generator (double-click to run)
- `infiniwolf-cli` — the same generator as a command-line tool
- ECWolf itself (the **GPL edition**; see [Licensing](#licensing) below), so there's nothing else to install

To use it: download the archive for your platform, unpack it, and drop its contents next to (or into) your own registered Wolfenstein 3D install — you still need to supply your own legally owned WL6 data; nothing here includes or downloads it for you. Run `InfiniWolf`, choose settings, **Generate**, then **Play**.

Prefer to run from source, or want to build these packages yourself? See below.

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

When the tool detects the `/data`, `/mods`, and `/games` layout used by this collection, it installs to `mods/installed/infiniwolf/infiniwolf.pk3` automatically. The campaign will then also appear in the collection's normal mod selector.

## Command line

```sh
python3 -m infiniwolf --seed castle --output infiniwolf.pk3
```

Every intensity option accepts `1` through `5`:

```sh
python3 -m infiniwolf --seed 42 --guard-density 4 --enemy-toughness 3 \
  --supplies 3 --treasure 2 --secrets 4 --locked-doors 3 \
  --layout-complexity 5 --output infiniwolf.pk3
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

## Building a release locally

`.github/workflows/release.yml` builds and publishes the prebuilt packages automatically whenever a `vX.Y.Z` tag is pushed (see `packaging/make_release.py`). To reproduce a package by hand:

```sh
pip install pyinstaller .
pyinstaller --onefile --windowed --name InfiniWolf run.py
pyinstaller --onefile --name infiniwolf-cli infiniwolf_cli.py
python3 packaging/make_release.py --platform linux --version 0.1.0   # or windows / macos
```

The script downloads ECWolf's official prebuilt binary for the target platform from `maniacsvault.net`, checks it against a pinned SHA-256, and packages it alongside the two executables. It never touches Wolfenstein 3D game data.

## Licensing

InfiniWolf itself is MIT licensed (`LICENSE`). ECWolf is dual licensed by its authors under either the original id Software non-commercial license or GPLv2+; `packaging/make_release.py` only ever fetches and bundles the **GPL edition** (verified against ECWolf's own bundled `readme.1st`/license files, and against the fact that the Linux build is literally the Debian-archived package, which cannot legally carry the non-commercial edition). Prebuilt release packages include ECWolf's GPL license text and copyright notices under `THIRD_PARTY_LICENSES/ecwolf/`. ECWolf's source is at [github.com/ECWolfEngine/ECWolf](https://github.com/ECWolfEngine/ECWolf).
