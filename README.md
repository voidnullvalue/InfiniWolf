<div align="center">

<h1>InfiniWolf</h1>

<img src="assets/infiniwolf-logo.png" alt="InfiniWolf logo" width="520">

<p>
  <a href="https://github.com/voidnullvalue/InfiniWolf/actions/workflows/release.yml?query=branch%3Amain+event%3Apush"><img src="https://github.com/voidnullvalue/InfiniWolf/actions/workflows/release.yml/badge.svg?branch=main&event=push" alt="CI"></a>
  <a href="https://github.com/voidnullvalue/InfiniWolf/releases/latest"><img src="https://img.shields.io/github/v/release/voidnullvalue/InfiniWolf?display_name=tag&sort=date" alt="Latest release"></a>
  <a href="https://github.com/voidnullvalue/InfiniWolf/releases"><img src="https://img.shields.io/github/downloads/voidnullvalue/InfiniWolf/total" alt="Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/voidnullvalue/InfiniWolf" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+"></a>
</p>

</div>
InfiniWolf generates deterministic ten-map Wolfenstein 3D campaigns for ECWolf, with varied building layouts, coherent room themes, staged progression, and context-aware encounters and rewards. Independent progression grammars, circulation skeletons, scheduled hallway-first forms, district patterns, reconvergence motifs, and asymmetric room profiles prevent one repeated generator silhouette from owning the campaign. Every floor begins at one of three believable, complete, rock-bounded horizontal elevator-car arrivals, each with a real working door. District-aware stone, brick, wood, metal, marble, plaster, and damaged wall families give rooms a stronger sense of place, with purple reserved for floors 6–10 as a late-campaign escalation. Recessed exterior vistas and room-semantic prop families add visual character without exposing the map shell or scattering arbitrary clutter. Room-owned sentries, flanks, ambushes, strongpoints, moving patrols, and rare firing galleries make combat spaces feel purposeful. Each room is composed around one deliberate motif rather than several competing ones, and its decoration mix is tuned against 43,122 prop placements mined from 207 hand-authored community maps. Functionally related rooms are planned adjacent — a mess beside the barracks, an ossuary beside the crypt — because a building that had a purpose before it had rooms is what reads as designed. Every floor names one primary landmark space and two or three supporting ones, and a campaign-wide aesthetic arc leaves early floors orderly and occupied while late ones are battered and derelict. The goal is simple: make each seed fun to explore and enjoyable to replay, building toward one of six bosses in one of five geometry-rich strongholds on floor 9 and a secret reward expedition on floor 10. It uses the player's registered WL6 data at runtime and never copies Wolfenstein graphics, sounds, music, or data files into generated packages.

## What changed in 2.0

2.0 is a rewrite of how the generator is organised and a broad pass over map quality. Generated output differs from 1.9.x for every seed; the settings and reproduction commands from an older release will not reproduce an older campaign under this version.

**Architecture.** `generator.py` went from 6,073 lines owning nine systems to a coordinator of about 700 lines, alongside sixteen modules that each own one design decision — `planning`, `geometry`, `progression`, `semantics`, `encounters`, `pickups`, `decorations`, `special_floors`, `campaign`, `quality`, `ledger` and the `wl6`/`model`/`grid` leaves. The import cycle that forced validation and artifact encoding to be imported from the generator's last lines is gone; the package is an acyclic graph with no deferred or bottom-of-file relative imports. `watermark_cli.py` holds the Tkinter interface so `watermark.py` can be imported eagerly without making headless generation require a GUI toolkit. Every extraction step was proven byte-identical against a 32-combination fingerprint corpus before any behaviour was changed.

**Map quality.** One deliberate motif per room instead of nine racing probability gates, which took the concept-specific composition — bunks in a barracks, a spear rack in an armory — from 1% of rooms to 28%, and cut rooms with no composition from 70% to 18%. Functional room adjacency doubled from 8.2% to 16.0% of connections. Decoration density and per-item mix now track the authored corpus closely. Every room is lit. Hallways are furnished. All five boss-arena families and all six native bosses can appear, where previously two families and four bosses were unreachable.

**Performance.** Generation is 2.8× faster, so the new `--generation-quality thorough` default — which halves critique flags by ranking eight candidates per floor instead of taking the first clean one — still finishes sooner than 1.9.x did taking the first.

The full list of intentional output changes, the architecture, and a note on where new work belongs are in [`docs/RELEASE-2.0.md`](docs/RELEASE-2.0.md).

Curious how the generator actually works? Start with the human-readable
[`GENERATION_FLOW.md`](GENERATION_FLOW.md) flowchart, then use
[`DESIGN.md`](DESIGN.md) for the detailed floor grammar, room realization,
actor placement, and validation rules.

## Prebuilt release (Windows / macOS / Linux)

Every tagged release publishes a self-contained `.zip` per platform on the [Releases page](https://github.com/voidnullvalue/InfiniWolf/releases) — no Python install required. Each one bundles:

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

After generation, **View Maps** opens a scalable top-down viewer for all ten
maps. The floor list and optional start/exit, route, enemy, pickup, and secret
overlays make it possible to inspect a campaign without launching ECWolf.
The window title and footer show the exact InfiniWolf version and abbreviated
Git commit, making screenshots and bug reports straightforward to identify.

When the tool detects the `/data`, `/mods`, and `/games` layout used by this collection, it installs to `mods/installed/infiniwolf/infiniwolf.pk3` automatically. The campaign will then also appear in the collection's normal mod selector.

## Command line

```sh
python3 -m infiniwolf --seed castle --output infiniwolf.pk3
```

Run `python3 -m infiniwolf --version` to print the same version and commit
identifier shown by the desktop interface. Normal CLI runs print it before
generation as well.

Every intensity option accepts `1` through `5`:

```sh
python3 -m infiniwolf --seed 42 --generation-quality thorough \
  --guard-density 4 --enemy-toughness 3 \
  --supplies 3 --treasure 2 --secrets 4 --locked-doors 3 \
  --layout-complexity 5 --decoration-amount 4 --room-shape-variation 4 \
  --patrol-activity 3 --atmosphere 2 --secret-reward-quality 4 \
  --theme-bias catacombs --output infiniwolf.pk3
```

The desktop interface groups the original gameplay controls separately from
the style controls. Style settings deliberately influence bounded choices
rather than disabling map validation: decoration amount controls prop budget,
room-shape variation controls a target mix of chamfers, L/T profiles, offset bays,
mirrored notches, and symmetric profiles (40% shaped rooms at the normal setting), patrol activity controls
the target share of actors assigned to validated moving routes, atmosphere controls
how clean or grim rooms look, and secret
reward quality shifts the secret-room reward mix. Theme bias strongly favors a
floor identity without forcing every floor to repeat it; `mixed` keeps the
default rotating sequence. Adjacent floors are guaranteed to use different
base identities and different circulation skeletons. Layout complexity now
scales planned room count through 16/18/20/22/24 rooms; saturated optional
fillers try another nearby host in the same district, increasing the number of
distinct rooms without enlarging their dimensions or creating remote corridors.
Floor 10 plans up to four additional expedition destinations within the same
24-room ceiling to compensate for its larger room footprints.
About three floors per campaign may instead begin from a hallway-first form:
a central axis, plus, T, or offset boulevard. These forms use broad major
hallways, narrow connectors, balanced asymmetric room loading, and no empty
arms; ordinary graph-first floors remain the majority. Decoration also keeps
blue and green barrels in separate room-level families and treats blue urns
as singular wall-backed accents rather than loose repeated clutter.

`--generation-quality` controls how hard the generator looks before accepting a floor. `fast` takes the first candidate with no critique flags; `balanced` ranks five valid candidates; `thorough` (the default) ranks eight. On the project's four-core test machine, averaged three ten-floor campaigns: fast averaged 7.3 critique flags per campaign in 127s, balanced 5.0 in 178s, thorough 3.3 in 281s. Thorough is the default because it roughly halves the flag count and, since the corridor router became 2.8x faster, still finishes sooner than fast did in earlier releases. Candidate generation stays deterministic and ranking only ever chooses among maps that already passed validation, so a higher setting cannot make an invalid map acceptable — it only widens the pool. The setting is recorded in the manifest and in the reproduction command, because it changes which candidate wins and therefore the output.

Using the same version, commit, seed, and settings produces byte-identical output. The named `LittleEntropyMachine` seed source derives blake2b-based floor, variant, circulation, progression-grammar, lock, vine-sector, rare-gallery, and rare-motif streams without retry attempts perturbing campaign-scale choices; its payload-name strings are compatibility-sensitive. Within a floor, each of the eight subsystems — planning, geometry, progression, semantics, encounters, pickups, decorations, special_floors — draws from its own `infiniwolf:stream:v1` derivation keyed by seed, floor, attempt and subsystem name, so an extra draw in one cannot move another's choices. Those eight names are part of the seed payload and therefore compatibility-sensitive: renaming one changes every map for every existing seed. A manifest inside the PK3 records that seed source, the resolved seed and settings, arrival elevator, circulation or hallway form, exterior vista, semantic prop families, wall and room identity, encounter compositions, patrol routes, the single-floor corridor-vine schedule, rare guard galleries, special-floor family, room shapes, lighting families, key objectives, bounded secrets, pickup compositions, and validation results. Every generated PK3 also includes `infiniwolf-settings.txt`: a plain-text record of the exact version, commit, resolved seed, every control value, and a copyable reproduction command.

Generated maps also carry a gameplay-neutral provenance signature in their
sound-zone numbering. Each standalone `IWNN.wad` has two independently
checkable residues; all ten primary residues additionally total 42 modulo 43.
The signature is bound to zone layout, door geometry, and selected ordinary
map objects, so removing a metadata file is insufficient and broad edits tend
to invalidate it. Check a campaign or one extracted map from either CLI or an
optional GUI:

```sh
python3 tools/check_infiniwolf.py campaign.pk3
python3 tools/check_infiniwolf.py maps/iw05.wad
python3 tools/check_infiniwolf.py renamed.wad --floor 5 --json
python3 tools/check_infiniwolf.py --gui
```

This is evidence of generator origin, not cryptographic proof of authorship;
someone deliberately re-encoding a copied map can forge it.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Most tests generate whole floors, and a floor costs a few seconds, so the full
suite runs for roughly 50 minutes on four cores. The current `--fast` gate runs
109 tests; `tools/check.py` offers three gates sized to what you changed — pick
the smallest one that can actually catch your mistake:

```sh
python3 tools/check.py --fast    # ~2s   tables, docs, config, CLI; no generation
python3 tools/check.py --decor   # ~15m  decoration, lighting and placement invariants
python3 tools/check.py --full    # ~50m  the whole suite, sharded across cores
```

`--fast` skips map generation entirely, so it cannot see reachability, actor-facing
or placement regressions; run `--full` before committing.

Two cheaper checks matter more when moving code between modules:

```sh
python3 tools/unresolved_names.py    # names a module uses but never imports
python3 tools/fingerprint.py --check # generated maps still byte-identical
python3 tools/reservation_sites.py   # who writes to the shared cell reservations
```

A name a moved function needs but did not bring along raises `NameError` only
when that function runs, so the package imports fine and the pure-logic tier
passes. The scanner catches it in milliseconds; the fingerprint gate catches it
in minutes; the test suite may not catch it at all.

Sharding is hand-rolled rather than delegated to `pytest-xdist` so a bare checkout
needs no extra packages — but do not expect it to scale with core count. Measured
on a 4-core machine, `--full -j 4` took *longer* than running the suite serially
(55 min against 49): map generation churns 4096-element planes and repeated
flood fills, so parallel shards saturate memory bandwidth and starve each other.
The default leaves one core free, which helps a little. If you need a fast gate,
use a narrower tier rather than more shards.

Broader deterministic fuzzing and a real-engine smoke check are also included:

```sh
python3 tools/fuzz.py --seeds 1000
python3 tools/smoke_ecwolf.py --ecwolf /path/to/ecwolf --data /path/to/wl6-data
```

Decoration quality is measured rather than eyeballed. With a collection of
hand-authored community maps installed alongside this repo, `tools/decor_stats.py`
reports decoration density, wall-adjacency, clustering, and per-item frequency for
that reference corpus and for freshly generated floors side by side, and
`tools/mine_decor_patterns.py` regenerates
[`docs/decor-corpus-patterns.md`](docs/decor-corpus-patterns.md), the per-item
placement model mined from the same corpus:

```sh
python3 tools/decor_stats.py
python3 tools/mine_decor_patterns.py
```

Generated packages contain only WAD map data, MAPINFO, and reproducibility metadata. Registered WL6 assets remain in the user's data directory.

## Building a release locally

`.github/workflows/release.yml` tests each push to `main`, creates the declared
`vX.Y.Z` tag when that commit is still current, and then builds and publishes
the three platform packages. A manually pushed version tag follows the same
test/build/publish path (see `packaging/make_release.py`). To reproduce a
package by hand:

```sh
pip install pyinstaller .
pyinstaller --onefile --windowed --name InfiniWolf run.py
pyinstaller --onefile --name infiniwolf-cli infiniwolf_cli.py
python3 packaging/make_release.py --platform linux --version 2.0.1   # or windows / macos
```

The script downloads ECWolf's official prebuilt binary for the target platform from `maniacsvault.net`, checks it against a pinned SHA-256, and packages it alongside the two executables. It never touches Wolfenstein 3D game data.

## Licensing

InfiniWolf itself is MIT licensed (`LICENSE`). ECWolf is dual licensed by its authors under either the original id Software non-commercial license or GPLv2+; `packaging/make_release.py` only ever fetches and bundles the **GPL edition** (verified against ECWolf's own bundled `readme.1st`/license files, and against the fact that the Linux build is literally the Debian-archived package, which cannot legally carry the non-commercial edition). Prebuilt release packages include ECWolf's GPL license text and copyright notices under `THIRD_PARTY_LICENSES/ecwolf/`. ECWolf's source is at [github.com/ECWolfEngine/ECWolf](https://github.com/ECWolfEngine/ECWolf).

## Credits

Señor Frijole — testing and map-design feedback.
