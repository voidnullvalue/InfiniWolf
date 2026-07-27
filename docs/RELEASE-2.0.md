# InfiniWolf 2.0.0

2.0 reorganises the generator around design-decision ownership and makes a broad
pass over map quality. **Generated output differs from 1.9.x for every seed.** A
reproduction command from an older release will not reproduce its campaign under
this version; the version and commit recorded in every package are what make a
campaign reproducible, and both have moved.

---

## Intentional output changes

Listed in the order they were made, each with the measurement that justified it.

| Change | Effect |
| --- | --- |
| Authored views | The line ahead from a primary landmark's approach doorway is reserved so no prop fills it. 0.88 per floor, 70 of 70 unobstructed, validated on the finished map. |
| Shared inaccessible voids | At most one per campaign: an interior pocket overlooked by two or more rooms and entered by none, sealed with a pillar screen and proved contained. Ten realized across 16 campaigns, no breaches. |
| Clustering reduced | 33.5% of props touching another, now 22.9% against 18.6% authored. |
| Every room is lit | Eight concepts had no lighting entry and silently resolved to unlit, among them `barracks` — the theme every ordinary room receives. Rooms containing any fixture went from 24% to 100%. |
| Decoration density raised to the authored band | 0.063 → 0.136 decorations per floor cell, against 0.134 in 207 hand-authored community maps. |
| Floor lamps rationed by position | Never on a cell with floor on all four sides, never abutting another, at most two per room. Open-floor placements went from 17.2% of lamps to 0.0%, against a corpus rate of 1%. |
| Hallways furnished | The composition skip tested minimum dimension, and `validate_map` pins corridors at exactly three wide, so no corridor had ever received furniture. 63% now carry props; the centre lane is reserved so none can block it. |
| No accidental one-cell alcoves, and none left empty | A one-cell dead end with nothing in it reads as a hole cut in a wall for no reason. 2.47 per floor, 34% of them empty; now 0.73 per floor and 0% empty. Corridor widening was the main source at 1.27 per floor: it widens a straight run cell by cell, so the last wing at each end had wall on three sides. Those are trimmed back, and symmetric profiles and sightline breaks are now rejected if they would create one. What remains is only the deliberate ambush recess, whose empty twin — half of the feature, by construction — is now furnished. `validate_map` refuses an empty one. |
| One motif per room | Nine compositions were independent probability gates drawing on a shared budget, so source order was priority order. The concept-specific signature went from 1.0% of rooms to 28.0%; rooms with no composition fell from 69.6% to 17.6%. |
| Item mix matched to the corpus | Flags 15.6 → 5.8 per map (authored 4.6), blue barrels 16.1 → 7.0 (6.7), white pillars 7.2 → 16.2 (18.0), Pots 0.1 → 2.9 (5.3). Distinct item types 14.0 → 17.0 (19.3). |
| Functional room adjacency planned | Affine connections — a mess beside the barracks, an ossuary beside the crypt — doubled from 8.2% to 16.0% of room edges. |
| All five boss-arena families reachable | `command-bunker` and `columned-fortress` were planned ~40% of the time between them and realized *never*: their wall displays sat at offsets that are always interior floor, and `validate_map` requires a flag on the perimeter. |
| All six native bosses reachable | Floor 9's exit was gold-locked, and only Hans and Gretel drop a gold key. The arena now gates the exit positionally when the boss drops nothing, enforced as a cut vertex. |
| Landmark hierarchy | One primary and up to three secondary landmark spaces per floor, never graph-adjacent, chosen from geometry before decoration runs. |
| Campaign aesthetic arc | Early floors orderly and intact, late floors battered. Visible within a variant: catacombs average 18.9 damaged wall tiles when they appear early against 54.9 late. |
| `generation_quality` default `thorough` | Ranking eight valid candidates per floor rather than taking the first clean one roughly halves critique flags, 7.3 → 3.3 per campaign. |
| Candidate density target recalibrated | The term aimed at 3.6–5.8 objects per room while the generator produced 12.8–15.9, so it had inverted into "always prefer sparser" and its tension rhythm was dead. |
| Four new critique flags | `shape_monotony`, `flat_area_rhythm` and two regression tripwires. Thresholds calibrated against measured distributions. |

### Known deviations from the corpus

Stated rather than hidden, with what is known about each.

- **Clustering: 22.9% of props touch another, against 18.6% authored.** Down from
  33.5%, so most of the gap is closed. What remains is diffuse rather than one
  cause: measured per item, the excess is 2.9 clustered cells per map for ceiling
  lights, 2.1 for brown plants, and under 1.0 each for everything else, while
  barrels are *below* the authored rate and offset 1.7 of it. Ledger attribution
  drove the fixes — 71% of floor-prop clustering was coming from multi-cell
  composition commits, which had been exempted from spacing wholesale rather than
  only from their own members.
- **Wall adjacency 82% against 88%**, and **light fixtures 62 per map against 74.**
- **Item 69, the spear display, is in neither static registry.** Three blocking
  palettes place it and `validate_map` has a rule for it, but it is absent from
  `STATIC_BLOCKING`, `STATIC_OPEN`, and from all 43,122 authored decorations. The
  spacing and clustering logic reads `_ALL_DECOR`, so a spear rack is invisible to
  both. Left alone deliberately: adding it to the registry shifts the metrics,
  dropping it changes what rooms contain, and choosing needs a look in the engine.
  `test_the_registry_gap_has_not_grown` fails if a second item drifts out.
- **Positional boss gating is weaker than a key gate.** It forces the player to
  enter and cross the arena, not to kill its occupant. Crossing a fifteen-tile room
  under fire is a fight in practice, but the two contracts are not identical.
- **Four of five `AestheticPhase` fields are unused.** Declared and recorded, not
  consumed. Tilting decoration's clutter palette by them produced no measurable
  change, because concept gating already decides what a room may hold.

---

## Architecture

`generator.py` went from 6,073 lines owning nine systems to an orchestrator of
about 700. Sixteen modules each own one design decision, and the dependency graph
is acyclic with no deferred or bottom-of-file imports.

```
wl6 ← model ← grid ← placement ← decorations ← semantics
                  ↖ campaign, planning, progression, pickups,
                    encounters, geometry, special_floors,
                    quality, ledger, generator_artifacts
                                    ↖ generator_validation ← generator
```

| Module | Owns |
| --- | --- |
| `wl6` | The native WL6 code vocabulary. Imports nothing. |
| `model` | Record types more than one system reads. |
| `grid` | Structure queries: what is at a cell, what can be walked to, how rooms connect. |
| `campaign` | Ten-map scheduling (attempt-invariant) and candidate ranking. |
| `planning` | The abstract building program, before any tile exists. |
| `geometry` | Tile realization, corridor routing, repair passes, space partitioning. |
| `progression` | Elevators, doors, locks, keys, secrets — whether the floor can be finished. |
| `semantics` | `RoomIdentity`, concept affinity, wall treatment, landmark hierarchy. |
| `encounters` | Room-owned combat composition and patrols. |
| `pickups` | Economy compositions and the ammo budget. |
| `decorations` | Aesthetic prop composition. |
| `special_floors` | Floor 9 and 10 authored contracts. |
| `quality` | Soft critique. Cannot reach validation. |
| `ledger` | Cell reservation provenance. |
| `generator_validation` | Hard validation. Non-negotiable, raises. |
| `generator_artifacts` | WAD/PK3 encoding, manifest, package verification. |
| `generator` | Orchestration only. |

### The rules that keep the boundaries honest

These are the cases where the line was genuinely unclear, and how it was decided.

- `model` holds a type when several subsystems consult the decision it carries, not
  merely because it is a dataclass.
- `grid` never answers *what should go here*. `_snap_offsets` draws from the RNG to
  prefer certain room alignments, so it is placement policy and lives in `geometry`.
- Space partitioning stays in `geometry` even though only `semantics` uses the
  labels, because the partition is a connectivity question and splitting it would
  fragment one flood fill.
- Geometry offers candidate threshold cells; `progression` decides whether each
  becomes a doorway, a locked gate, a secret entrance or nothing.
- `RoomIdentity` is resolved once and consumed everywhere after. No later system
  re-decides what a room represents.
- `quality` cannot import validation, so a soft score can order hard-valid
  candidates and never excuse an invalid one.

---

## Verification

Behaviour-preserving extraction was proven by byte-identity, not by inspection.
`tools/fingerprint.py` hashes the tile plane, thing plane, packed WAD bytes and a
canonical metadata projection for 32 combinations — two seeds × four configurations
× four structurally distinct floors — and every extraction step was checked
individually rather than once at the end.

That gate earned its cost repeatedly. It caught two imports left behind when code
moved between modules, where the package still imported cleanly and the pure-logic
test tier still passed because a `NameError` in a moved function body does not fire
until it runs. It caught a dropped `reserved.update` that let props into sealed
secret pockets on four seeds. It caught a lost reservation in the first ledger
wiring. None of those were visible in the diff.

### Release-candidate verification

Run against 2.0.0 on the commit that carries this file:

| check | result |
| --- | --- |
| Full test suite | 253 tests, all passing |
| Fingerprint corpus | 32 of 32 identical |
| Deterministic fuzzing | 90 maps across 3 seeds and all three intensity extremes, all validated |
| End-to-end package | validates, 35 KiB, **zero critique flags across all ten floors**, no registered assets bundled |
| Campaign generation time | 231s at the `thorough` default on four cores |
| Decoration against the corpus | density 0.136 against 0.134; distinct item types 17.0 against 19.3 |

```sh
python3 tools/unresolved_names.py       # names a module uses but never imports
python3 tools/fingerprint.py --check    # generated maps still byte-identical
python3 tools/reservation_sites.py      # who writes to the shared reservations
python3 tools/decor_stats.py            # decoration against the authored corpus
python3 tools/check.py --fast           # ~2s, no generation
python3 tools/check.py --full           # the whole suite
```

---

## Note for contributors

Where new work belongs:

- A new **prop or material** goes in `wl6` and then into the concept palettes in
  `decorations`. A palette is the real cap on what the density fill can place —
  `Pots` was in the WL6 registry but no palette, so it never appeared however often
  the mined corpus asked for it.
- A new **room composition** goes in `decorations` with an entry in
  `_MOTIF_WEIGHTS`. Weight inversely to how widely it is eligible: a narrowly-scoped
  motif cannot crowd anything out, and a broad one will dominate at any weight.
- A new **hard rule** goes in `generator_validation` and must raise. A new **soft
  observation** goes in `quality` and must return a flag.
- A new **campaign-scale choice** goes in `campaign` and must derive from a stream
  that excludes `attempt`, or a rejected floor will re-roll it. That exact bug
  skewed the floor-9 boss two to one.
- Anything that **reserves a cell** should use `ledger.reserve` with an owner and a
  reason.

Two habits worth copying. Calibrate thresholds against a measured distribution, not
intuition: all four new critique flags first shipped with thresholds outside the
range real floors occupy, so none could ever fire. And when a change is meant to
preserve behaviour, prove it with `fingerprint.py` after each individual step.
