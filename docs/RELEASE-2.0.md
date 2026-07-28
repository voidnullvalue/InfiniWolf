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

- **Clustering: 20.6% against 18.6% authored.** It was 33.5%, then 22.9%, then
  briefly 18.9% — an exact match — before the terminus-wall work took it back to
  20.6%. Ledger attribution drove the earlier fixes: 71% of floor-prop clustering
  came from multi-cell composition commits, exempted from spacing wholesale
  rather than only from their own members. The 1.7-point regression is the cost
  of biasing display props toward terminus walls, which roughly doubled the floor
  lamp count. Named as a regression rather than presented as the plateau, because
  it was demonstrably reachable.
- **Flags still ignore terminus walls: 8.5% against 28.4% authored.** The
  terminus work moved floor lamps from 5.6% to 22.9% and green plants from 6.2%
  to 18.9%, and lifted the overall rate from 8.3% to 12.0% against 16.3%
  authored — but flags did not move at all. They come from the concept
  `signatures` table rather than the density fill or the dedicated candidate
  lists, and that path still picks its cell without consulting orientation. This
  is the original complaint that started the work and it is not fixed.
- **Distinct item types 15.2 against 19.3 authored, and light fixtures 57.3
  against 74.1.** Both moved *further* from the corpus as clustering closed —
  17.0 → 15.2 and 62 → 57.3 respectively. That is the trade the spacing and
  positional rules make: refusing to place a prop beside another one removes the
  marginal instances first, and those are what carry variety. Named here because
  the next decoration pass should attack variety without reopening clustering,
  and it is the harder of the two problems.
- **Wall adjacency 83.2% against 88.1%.**
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
is acyclic with no deferred or bottom-of-file relative imports. `watermark_cli.py`
holds the Tkinter interface, so `watermark.py` can be imported eagerly without
making headless generation require a GUI toolkit.

```
wl6, room_policy, ledger  (import nothing from the package)
   ↑
model ← grid ← placement ← decorations
   ↑                          semantics ← campaign ← planning
   ↖ geometry, progression, encounters, pickups, special_floors,
     quality, generator_artifacts, watermark
                                    ↖ generator_validation ← generator
```

`semantics` does not import `decorations`. It used to, for one role/tier lookup
that resolves a room's base theme — but that lookup is consumed *before* a
`RoomIdentity` exists, by identity resolution itself, by the decoration
no-identity fallback, and by the structural-pillar decision. It could not move
up without a downstream module importing `semantics` for a pre-identity value,
so it lives in `room_policy`, which imports nothing.

| Module | Owns |
| --- | --- |
| `wl6` | The native WL6 code vocabulary. Imports nothing. |
| `room_policy` | Role/tier → base theme, decided before a `RoomIdentity` exists. Imports nothing. |
| `model` | Record types more than one system reads. |
| `grid` | Structure queries: what is at a cell, what can be walked to, how rooms connect. |
| `campaign` | Ten-map scheduling (attempt-invariant) and candidate ranking. |
| `planning` | The abstract building program, before any tile exists. |
| `geometry` | General structural realization: reusable room painting, corridor routing, repair passes, and space partitioning. |
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
| `generator` | Phase order, floor-seed derivation, and the `boss_locks_exit` integration seam. |

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
| Full test suite | Passing at release-candidate verification; no stale count retained here |
| Fingerprint corpus | 32 of 32 identical |
| Deterministic fuzzing | 90 maps across 3 seeds and all three intensity extremes, all validated |
| End-to-end package | validates, 35 KiB, **zero critique flags across all ten floors**, no registered assets bundled |
| Campaign generation time | 231s for one ten-floor `thorough` campaign on the project's four-core test machine |
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
  reason. A cell retains every claim until its final claim is released; `release`
  removes only claims owned by its caller.

Two habits worth copying. Calibrate thresholds against a measured distribution, not
intuition: all four new critique flags first shipped with thresholds outside the
range real floors occupy, so none could ever fire. And when a change is meant to
preserve behaviour, prove it with `fingerprint.py` after each individual step.
