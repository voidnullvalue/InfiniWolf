# InfiniWolf design notes

This document describes how the generator turns a seed into a Wolfenstein 3D floor: the floor-plan grammar, room placement, corridor and door carving, the map analyses that inform later passes, and how enemies are placed and oriented. Everything here reflects the code in `infiniwolf/generator.py` and is meant to be read alongside it — function names are given so you can jump straight to source.

## 1. Grid model

Every level is a `GRID = 64` square. Two 4096-entry integer planes back it:

- `tiles` — walls, floor-zone codes, doors, elevator, secret pushwalls.
- `things` — actor and prop codes (WL6 old-num values, per ECWolf's base translator).

Helpers `_at`, `_set`, and `_is_floor` are the only accessors used elsewhere; `_is_floor` accepts the whole floor-zone range plus the dedicated secret-exit zone `SECRET_EXIT_ZONE = 107`.

A `Room` is an axis-aligned rectangle in tile space (`x, y, w, h`, `center` is `(x + w//2, y + h//2)`). No rooms overlap, and `_overlaps` enforces a two-tile padding so there is always at least one wall between any two rooms.

## 2. Floor-plan grammar (`_plan_floor`)

A floor plan is chosen first, geometry second. The plan is a small directed graph:

- **Spine**: approximately 55% of the planned rooms (up to eleven; nine for the enlarged floor 10), always `start → beat* → climax → relief → exit`. This keeps optional wings and motifs while ensuring the mandatory route forms most of the authored progression.
- **Room tiers**: `standard` (6–9 square), `hall` (long-side 9–13), `anchor` (10–13 square, exactly one per floor — the climax room), `closet` (4–5 square).
- **Districts**: two or three (a third is possible once the spine reaches seven rooms); each spine index is assigned to a district and downstream wall-theming reads this.
- **Motifs**: `ring` is always present; up to two of `hub`, `wings`, `gallery` are added based on the complexity budget. Each motif is realised as extra rooms and edges:
  - `ring` picks a `(left, right)` pair biased toward the spine centre and inserts a 1–2 room detour that rejoins at `right`; the rejoin edge is recorded in `loop_edges`.
  - `hub` promotes one middle beat to an anchor tier and hangs two branches + 1–2 closets off it, and downgrades the climax to `standard` so there's only ever one anchor.
  - `wings` adds two same-sized branches (sizes shared via `size_groups`).
  - `gallery` chains 2–3 closets off a middle beat.
- **Filler**: rooms are added until `target = min(20, 14 + 2 * complexity)`. Filler prefers previous filler tips 55% of the time so the perimeter breathes; otherwise it hangs off the lowest-degree middle beat.

The plan carries the graph (`edges`, `loop_edges`), the tier and role of every room, motif membership, and a `critical` spine set. Optional motifs that cannot remain local may be dropped; they never use the global-scatter fallback and therefore cannot create a remote side corridor deeper than the elevator. Ring detours are sized so their reconvergence never shortcuts the spine segment they parallel.

### 2.1 Floor variants (`FloorVariant`, `_variant_sequence`)

Each floor is generated under a named **base variant** — one frozen bundle of the parameters that used to be module constants: the carve chances (`_carve_notches`/`_carve_alcoves`/structural columns), corridor widening, the plan grammar's hall/closet/motif rolls, the allowed `WALL_THEMES` bases, the cellblock probability, and the decoration density and theme leanings. Five variants rotate on floors 1–8 (`garrison`, `catacombs`, `grand-halls`, `storehouse`, `quarters`); floors 9 and 10 are the forced `stronghold`/`vault` identities whose parameters stay at the defaults because those floors keep their purpose-built inline treatments (§3). A non-mixed theme bias gives the selected variant triple weight while retaining contrast floors and the no-immediate-repeat rule.

Selection is a pure function of the campaign seed: `CampaignConfig.variant_seed(floor)` hashes a dedicated `infiniwolf:variant:v1` payload (independent of `floor_seed`, whose format is frozen, and of the retry `attempt`, so validation retries keep a floor's identity), and `_variant_sequence` draws each floor's pick excluding the previous floor's, so consecutive floors always differ and floor N's variant is derivable without generating floors 1..N-1. A default-valued `FloorVariant` reproduces the pre-variant generator knob-for-knob. The chosen name is recorded on `GeneratedMap.variant`, in the manifest, and as in-game flavor in each floor's mapinfo display name ("Floor 3: Grand Halls").

## 3. Room realisation (`_place_planned_rooms`)

Sizes are drawn from `_room_size` per tier; grouped rooms (e.g. `wings`) share the same drawn size. The start room lands in a randomised quadrant with a `heading` offset from an edge, and every subsequent room is placed adjacent to its parent by `adjacent(...)`. `legal(...)` enforces the 3-tile map margin and the pad-2 overlap rule; if a room can't fit adjacent to its parent, the plan drops it (and its subtree). Dropped rooms and edges are cleaned up before the planner returns a `PlacedPlan`.

Floor 9 widens its single anchor to 14–17 tiles on each side. Since every other tier is smaller, that anchor is always the selected boss arena; its existing `grand` decor treatment adds the intended columned set-piece dressing. The structural BFS parent of that arena receives a small pre-boss cache: first-aid and ammo, with independent chances for a rare weapon and an extra life. It is placed before population, so `_guarantee_supplies` naturally counts the cache while calculating its remaining floor-wide deficit.

Floor 10 keeps the same plan grammar but makes anchors, halls, and standard rooms two tiles larger in each dimension; closets remain small as contrast nooks. Some non-critical filler may therefore be dropped when geometry is tight. Its treasure pickup cadence is two room slots faster, with a second treasure on each successful cadence, and its secret budget is one higher, making the finale visibly more reward-heavy without changing secret reward weighting.

## 4. Carving and connections

- **Room interiors**: floor codes taken from the district's sound zone (see §6). Interior variety comes from `_carve_notches`, `_carve_alcoves`, and `_add_pillars`, each guarded by the room tier so anchor/hall rooms get more structure than closets.
- **Corridors**: `_carve_connection` runs an L-shaped path between paired rooms, biased to enter through the room's short face. `_widen_corridors` opens the tightest single-tile chokes to two tiles based on traffic (paths that lie on more than one connection get widened).
- **Doors**: `_door_candidate` finds a single-tile choke on the wall between two rooms and `_door_axis` sets the vertical/horizontal door variant. `_place_doors` skips choke points that would trap a locked-door key behind itself and downgrades officer/SS spawns within three tiles of a door (see `near_door` in `_place_population`) so a door breach cannot reveal a point-blank elite.
- **Secrets**: `_place_secret` and `_carve_secret_pocket` push a wall into a bespoke square, vault, reliquary, gallery, or nested pocket reachable only through its pushwall. The complete footprint and its one-tile shell must be unused wall, so a secret cannot leak into a normally accessible room. Every successful pocket has exactly three deliberately spaced rewards; reward quality changes the useful/premium mix and permits a rare extra life without making it routine. Pockets never break outer-wall integrity; there is a run-time check in `validate_map`.

## 5. Doors, keys, and the exit

`_lock_schedule` uses a campaign-specific seed stream to choose a quota across floors 1–8 rather than rolling each floor independently. The `locked_doors` intensity shifts the quota between unlocked, single-key, and dual-key floors; weighted selection favors later floors, preserves at least two unlocked floors, and forbids three gated floors in succession. Single-key floors balance gold and silver across the campaign. Dual-key floors randomize their order and build a genuine staged route (`key A → lock A → key B → lock B → elevator`). If the geometry cannot make every requested lock individually mandatory, the plan downgrades to one valid gate or no gate instead of locking an optional room. Floor 9 always retains its gold boss-elevator gate and may add a seeded silver pre-boss stage; floor 10 is always keyless.

The elevator is selected from viable post-anchor rooms only after room connections exist. Its shortest room path must contain at least 55% of realized rooms, cross a district boundary, and reach at least 75% of the deepest ordinary room distance; the final tile route must also contain multiple bends. Candidate selection prefers the deepest qualifying room rather than blindly trusting the nominal `exit` role. `_place_elevator` then installs the native east/west switch geometry. Secret exits retain their two-tile elevator pocket so the switch face never bleeds through walls.

Progression validation simulates every key state with secret pushwalls already open. The first key must be reachable while the exit is blocked; on dual floors the second key must become reachable only after opening the first color while the exit remains blocked; both colors must open the exit. It also opens every other color while withholding one in turn, proving that each lock is independently necessary. Keys may not share the door-bounded room containing their own lock. Hans and Gretel remain valid gold-key providers through their native boss drops; other bosses receive a physical gold key in the correct progression stage.

## 6. Map analysis passes

These run after carving and are the inputs to actor placement.

### 6.1 Floor distances (`_floor_distances`)
A 4-neighbour BFS from a start tile over `_is_floor` and `DOORS`. Used for:
- Ranking rooms by distance from `start` (the pacing curve, §7.2).
- Picking the key/exit rooms.
- Selecting each room's entry cells (§8.1).

### 6.2 Sightline breaking (`_break_long_sightlines`)
Scans horizontal and vertical open runs; anything longer than 21 tiles gets a decoration or a jog to prevent the player from being shot from off-screen. Doors and room centres are excluded so we never block a doorway.

### 6.3 Sound zones (`_assign_sound_zones`)
ECWolf uses the floor tile code as the sound-propagation zone identifier. `_assign_sound_zones` flood-fills contiguous rooms with a shared zone code so a single alerted guard raises everyone in that acoustic pocket, and neighbouring zones stay silent. `_split_oversized_zones` breaks any single zone that grew beyond the safe cap so alerting one guard cannot cascade the whole floor. A final plain-door cleanup removes a doorway when its two sides already share a floor-only route through a small notch or alcove; locked and elevator doors are deliberately excluded.

### 6.4 Wall theming (`_apply_wall_theme`)
Districts select distinct `WALL_THEMES` materials for separate areas of a floor. The floor's variant (§2.1) may restrict the roster to a themed pool (e.g. `catacombs` draws only blue-stone/grey/brick); every pool keeps at least three distinct bases so up to three districts can still receive distinct materials, and a pool too small for the district count is ignored rather than crashing the sample; `DECOR_WALLS` are inserted only as accents (portraits, banners, insignia, and the skeleton cage), never as room material, following original-episode conventions. Plain blue panels and the blue insignia panels are separate themes; mottled BSTONEB masonry can replace one district's material on floor 10. The blue-stone cellblock landmark uses plain bars nine times out of ten and the skeleton variant once. Pushwall hints use their area's decor accents, or decor from a `WALL_THEMES` sibling with the same base material; a material with no such sibling stays plain rather than borrowing a cross-family landmark.

Eligible blue-stone rooms independently have a 35% chance to become a cellblock (`JAIL_CANDIDATE_PROBABILITY`, now the default of a per-variant knob — `catacombs` raises it to 60%, `storehouse` disables cellblocks entirely). A cellblock needs a clean five-tile wall run and paints every two of three wall tiles as barred cells: plain bars are nine times as likely as the skeleton variant, while the remaining blue-stone tile reads as a mortar pillar. Loose remains can sit beside plain bars, and jail decoration pools stay deliberately sparse: barrels, blood, and bone variants only. The selected room indices are shared by the wall and decoration passes so the visual treatment and prop bias cannot disagree.

Themes follow finalized doors rather than forcing new doors: `_assign_area_themes` starts from the door-bounded floor components, then union-finds components touching the same bare wall tile. This folds any thin, undoored shared wall into one material group before painting, so distinct materials can meet only at a real door. A bounded pre-theming pass may convert a few of the bridge seams that would otherwise form one oversized merge group into spaced real doors; it never relaxes that union rule. Room-less corridor groups inherit a district through the door graph.

Landmark accents are hung symmetrically on the room's longest clean (contiguous, same-base) wall run: short runs get one centered tile, longer runs a mirrored pair, and the longest a center-plus-pair triplet. The caged-skeleton cell wall stays a singular set piece. `_apply_wall_theme` returns each room's landmark cells so decoration can respond to them.

Decoration placement follows theming and is anchored to room features (`_room_anchors`) rather than scattered. A `RoomIdentity` joins the plan-grammar role and tier to the floor variant, district, wall material, special-room status, and a derived human-readable concept such as armory, crypt, mess-kitchen, storage, or war-room. The identity planner balances compatible concept palettes inside each base theme and penalizes assigning the same concept to connected rooms. This prevents a broad variant override (for example, grand halls turning military rooms into lived-in rooms) from collapsing the whole floor into one repeated treatment. Later passes consume that identity instead of independently guessing a theme, so architecture, wall treatment, signatures, clutter, and population tell the same story.

Each eligible concept first attempts a recognizable signature: matched bunks in barracks, spear racks in armories, wall-backed appliances in kitchens, cages in jails, armor in war rooms, barrels in storage, and restrained equivalents for the other concepts. Kitchens are a floor-level set piece capped at one, not the default treatment for every lounge. A stove establishes the room; the sink is independently optional, and any sink, stove, kitchen supplies, and pots are spaced around wall-backed positions rather than emitted as one repeated clump. Pots and pans remain kitchen-only rather than serving as universal filler. The broader registered WL6 static range supplies the concept vocabulary; object 63 (`CallApogee`) is excluded and validation rejects it defensively. Atmosphere filters clean versus grim props without changing topology.

Blocking arrangements are composed as atomic groups and commit only if every member fits and the full-map reachability check still succeeds. Wall-landmark framing has a 15% attempt rate: it frames only the center landmark on one wall, or matching center landmarks on an opposing pair, always with the same prop. Doorway frames have the same low attempt rate and a hard cap of three per floor. Doorway approaches remain clear, and decoration observes the §9.1 statics headroom. Structural one-tile columns are not generic noise: only grand-hall or catacomb concepts may request them, at their variant's roughly 10–15% rate, and the placement remains symmetric.

Open props are anchored to their composed set: kitchen supplies occupy separated wall work zones, storage spill sits by crates or barrels, ceiling fixtures follow room axes, and gore/remains are reserved for concepts and atmosphere levels where they make sense. Dog food is placed only after dog packs are known, near their room and a wall, with no more than three bowls per floor. Sealed secret pockets are never decorated by the ordinary room pass.

## 7. Population placement (`_place_population`)

This is the largest single pass; it owns enemy count, family choice, item scattering, and initial facing.

### 7.1 Enemy budget
```
per_room = max(1, round(guard_density * 0.7 + progression * 2))
progression = (number - 1) / 8       # 0.0 on E1M1, 1.0 on E1M9
```
`guard_density` is the `1..5` UI dial. The floor's overall enemy count therefore scales with both the dial and the campaign progression.

### 7.2 Pacing curve (`pacing`)
Rooms are ranked by BFS distance from `start`. `depth ∈ [0, 1]` is that distance divided by the deepest room's distance. `pacing(depth)` returns a multiplier used to shape enemy density across the floor:

| depth range | multiplier | intent |
|-------------|-----------|--------|
| < 0.20 | 0.40 | Warm-up rooms near spawn; sparse. |
| 0.20 – 0.60 | 0.40 → 1.50 (linear) | Ramp. |
| 0.60 – 0.85 | 1.50 (flat) | Peak resistance in the middle-late floor. |
| 0.85 – 0.90 | 1.50 → 0.80 (linear drop) | Recovery just before exit. |
| > 0.90 | 0.80 | Exit room and immediate neighbours. |

`exit_room` additionally gets its budget multiplied by `0.4` regardless of depth. The dip at 0.85–0.90 is why `_place_population` also guarantees an ammo or first-aid drop in the deepest non-exit room that has neither within 12 tiles.

### 7.3 Family selection (`pick_family`)
Base weights come from `ENEMY_FAMILIES = ((guard, 10, 1.5), (dog, 6, 0.5), (officer, 3, 3.0), (ss, 1, 6.0))` — name, base frequency weight, expected bullets to down. Officers and SS are scaled by depth:

```
elite_scale = 0.45     if depth < 0.20
              1.35     if 0.60 <= depth <= 0.85
              1.00     otherwise
weights = base * (1 + progression) * elite_scale   for officer/ss
```

So elites are rare near spawn, most common at the pacing peak, and their overall frequency grows across the campaign. `enemy_toughness` (1..5) gates which families are unlocked at all: at toughness 1 only guards spawn; at 4+ SS is unlocked. `near_door(x, y, radius=3)` downgrades any officer or SS drawn within three tiles of a door back to a guard, so a door-breach line-up never point-blank-fires an elite at the player.

FakeHitler is a rare one-off actor only on floor 9, never a boss; the four indestructible Pac-Man ghosts are likewise a single ~10% novelty spawn, restricted to secret floor 10. Neither participates in the normal enemy-family or ammo-budget model.

### 7.4 Tier structure
ECWolf treats each `+36` on an actor code as the next cumulative skill tier: tier-1 actors join the base population on medium, tier-2 joins both on hard. `_place_population` places the base tier first, then two rounds of skill-only actors with `extra = round(base_budget * (0.20 + progression * 0.12))`. Skill actors need their own free cells in the `things` plane.

### 7.5 Rewards
After enemies are placed, every second room places an ammo/food/first-aid or treasure pickup in an unused candidate cell. Floor 10 shortens the treasure cadence by two room slots, adds a second treasure to each successful treasure cadence, and requests one extra secret, while `_ensure_early_heal` guarantees a first-aid in the low-depth zone so a rough opening doesn't spiral.

## 8. Actor facing

Enemy facing is the frame the player sees when they open a door. A misfaced guard reads as broken, so this pass is deliberately conservative.

### 8.1 Room entry cells (`room_entries`)
For every room, we collect every floor or door tile immediately outside its boundary (all four sides scanned), then **filter to approach-side entries only**: cells whose BFS distance from `start` is strictly less than the room's own depth. These are the doors the player walked through to reach this room. Cells on the far side (leading deeper into the level) are discarded so actors don't face the exit instead of the entrance. The full list is used as a fallback for the start room or any room with no closer-than-self adjacent cells.

Note on secret doors: pushwall faces are `WALL` tiles in the tiles plane and are never collected. Secret pocket floors are carved at `px+2` onward — two tiles outside the room boundary — and are also never reached by the one-tile scan.

### 8.2 Stationary facing (`_entry_pull`, `_pick_stationary_facing`)
All actors spawn stationary. `_pick_stationary_facing` scores each of the four cardinal directions with `_entry_pull`:

For each entry cell `e`, the pull score for a direction `(dx, dy)` is `para − 2·perp` where `para = dx·(ex−x) + dy·(ey−y)` (only directions where `para > 0` count) and `perp = |dy·(ex−x) − dx·(ey−y)|`. The perpendicular penalty is what makes a south-facing guard near the south wall correctly beat an east-facing guard on the south wall even when the east entry is Manhattan-closer. The direction with the highest score is chosen; if it points at a wall tile the algorithm widens the pool to all four directions rather than forcing a wall-adjacent face.

### 8.3 Patrol actors and turn-marker routing

Most actors remain stationary and use the conservative entry-pull facing above. A small subset of actors in rooms at least 7×7 instead patrol a clockwise rectangular loop two tiles in from that room's walls. The loop is reserved before other population and decoration placement, so it cannot be obstructed by another actor or a static. It has exactly one patrol actor and four `PatrolPoint` things: each corner marker carries the direction of its outgoing leg. The actor's patrol spawn code is selected from the matching `PATROL_*` tuple using the first leg's N/E/S/W index.

This routing is required by ECWolf, not cosmetic. A patrol-coded map thing gets `FL_PATHING` and its spawn angle as `dir` (`gamemap.cpp:630-636`; `gamemap_planes.cpp:331`). In pathing mode `A_Chase` calls `SelectPathDir` after `dir` becomes `nodir` (`wl_act2.cpp:481-535`), but `SelectPathDir` only calls `TryWalk` in that one current direction (`wl_state.cpp:167`); it neither scans nor turns. A failed step therefore leaves the actor permanently walking in place. Native `PatrolPoint::Touch` overwrites the pathing actor's angle and direction when it reaches the marker tile (`g_shared/a_patrolpoint.cpp:44-49`), which turns the actor before its next `TryWalk` can meet the wall.

The old-format marker codes are deliberately cardinal only: 90 east, 92 north, 94 west, and 96 south (`xlat/wolf3d.txt:751`). Markers are always floor tiles, never doors, and are checked for collisions before placement. `validate_patrols` simulates the same constrained engine loop for 512 steps: move only in the current direction across floor or door tiles, then apply a marker on arrival, and reject any dead end or occupied route tile. This is the regression guard that catches the original walking-in-place failure without needing to observe engine ticks.

## 9. Where the numbers came from

Almost every threshold in the sections above is either an engine limit, a manual/community-guide rule, or a value measured off a real-map corpus. This section names the source for each so the parameters are auditable rather than magic. The full internal report (kept as a personal working document, not tracked) is `deep-research-report.md`; what follows is a distilled provenance record of the parts that shipped.

### 9.1 Engine limits (hard bounds)

Read directly from the original Wolf3D source (`wolfhack`/`wolf3d.txt` and `WL_DEF.H`):

| Limit | Value | Enforced by |
|-------|------:|-------------|
| Map dimensions | 64 × 64 | `GRID = 64`; every plane accessor is bounds-checked in `_at`/`_set`. |
| Distinct floor "areas" (sound zones) | 37 | `_assign_sound_zones` + `_split_oversized_zones`; a floor with more than 36 door-bounded components fails the per-floor retry loop instead of wrapping mod 36 (a real bug that predated the split). |
| Sliding doors | 64 | Soft cap 56 (headroom for repair); `_place_doors` will not exceed it. |
| Actors | 150 | Soft cap 120 in the population budget. |
| Statics | 400 | Soft cap 320 across decor + treasure + pickups, enforced by a live headroom counter in `_place_decorations`. |
| Starting player state | 100 HP, pistol, 8 rounds, 3 lives | Used as the pistol-start assumption for the ammo-solvency check. |
| Door open duration | 300 tics ("close after three seconds") | Used to size the reasoning about combat-cell partitioning, not written into a runtime constant. |
| Pushwall travel | 2 tiles | Every secret pocket is carved so the pushwall's 2-tile move can never seal a reward or overrun a wall. |

### 9.2 Ballistic and fairness rules (from the original manual)

Player gunfire is strong at close range, degrades fast, and becomes unreliable past **21 tiles**. This drives:

- The **`max_run = 21`** sightline cap in `_break_long_sightlines`.
- The **routine-fight range 3–12 tiles** target; room sizing (6–9 baseline, halls 9–13 major axis) is chosen so most rooms fall inside this band.
- The **near-door officer/SS downgrade** (`near_door(x, y, radius=3)` in `_place_population`) — the manual explicitly warns against rushing straight through doors, and a point-blank elite at the door is the worst version of that trap.
- The **"necessary items are not hidden" rule** (from the manual). Enforced by `_reachable(..., locked_open=False)` and by placing keys in `_key_spot` off critical-path rooms only, never behind pushwalls. Secrets are always optional surplus.

### 9.3 Real-map corpus (254 maps, 6277 rooms)

`tools/inspect_map.py` is the reproducible analysis tool. `--compare DIR` walks every `.wad` under a directory, parses each map's WDC3.1 PWAD container (the same container `_wad_bytes` emits), floods door-bounded rooms with the same "zone" definition `_assign_sound_zones` uses, and prints a summary table you can diff against generated output.

The corpus itself was **254 real, playable Wolf3D-family maps** already present on this machine (`ecwolf/mods/installed/…`), spanning:

- The id-numbered `classics_*` conversions — Spear of Destiny and its two official mission packs — in native WL6 tile numbering.
- Well-regarded independent total conversions: `totengraeber`, `rtotenhaus_enh`, `wolfoverdrive`, `pthollenteufel`.

Each was chosen because it uses ECWolf's default tile numbering (walls low, doors 90–101, floor/zone codes 108–143) — the same convention this generator writes — so measurements are directly comparable without format translation. Every measured metric agreed across independently authored campaigns spanning 20+ years of Wolf3D mapping, which is why the numbers below were treated as a signature rather than an accident. **No coordinates, dimensions, or layouts from any analyzed file were transcribed into the generator** — only aggregate statistics and community-guide principles.

To reproduce the comparison against a fresh generator build:

```sh
python3 tools/inspect_map.py --compare /path/to/ecwolf/mods/installed
python3 tools/inspect_map.py --seed castle --floor 1 --complexity 3
```

The numbers that shipped into the generator, and the corpus figures that produced them:

| Signal from the corpus | Corpus value | Generator response |
|------------------------|-------------:|--------------------|
| Rooms that are a plain rectangle | 18.1% | `_carve_notches`, `_carve_alcoves` add corner bites / wall-flush bumps to roughly half of rooms. |
| Rooms with an isolated interior pillar/column | 13.8% (median 2 — a mirrored pair) | `_add_pillars` adds a single interior wall-plane tile (or a mirrored pair) to ~40% of rooms ≥7×7, guarded so it cannot become an articulation point. |
| Median room aspect ratio | 1.40 : 1 | `_room_size` reserves a `hall` tier (major 9–13, minor 5–7) so a minority of rooms move the aspect distribution off 1.1:1. |
| Bilateral symmetry rate on rooms ≥25 tiles | 44–46% (not ~100%) | Notches, alcoves, and pillars are applied per-feature, not mirrored globally — the "one intentional exception" pattern. |
| Rooms per map (mean) | 24.7 | Floor-plan target `min(20, 14 + 2·complexity)` + closet tier; measured after: 22.8. |
| Doors per map (mean) | 27.2 | Door placement measured after the change: 27.7 (near-exact). |
| Door-graph cycles per map (mean) | 1.1–1.54 | `ring` motif in `_plan_floor` guarantees at least one; measured after: 1.87. |

### 9.4 Community level-design guides

B.J. Rowan's 1994 Wolf3D map-design tips and "From Column to Column: The Wolfenstein 3D Level Design Bible" converge on the same qualitative advice, which shows up in:

- **Five room constructs** (square / rectangle / corner / T-junction / intersection): the plan grammar's `standard`/`hall`/`closet`/`anchor`/hub-junction tiers cover the same vocabulary.
- **Mirroring rule** ("if you add a column at the top, add one at the bottom"): `_add_pillars` prefers mirrored pairs; `wings` motif places a mirrored branch pair with shared `size_groups`.
- **Room size cap**: no single room exceeds roughly a quarter of the playable area; enforced by `anchor` tier maxing at 13×13.
- **"Make every room unique"**: districts + `WALL_THEMES` + motif membership vary each room's silhouette *and* material.

### 9.5 Iteration lessons that shaped the code

Two specific measurements led to permanent regression tests:

- **Bare-seam bug** — a corridor router fallback could fuse two rooms into one long open sightline, invisible to every solvability validator. Found by measuring the longest unobstructed straight run across seeds (values of 22–36 tiles with no door on that row/column). Fix: the buffered search now exhausts every portal pair, and the last-resort fallback refuses to cross a cell owned by an unrelated room. `tests/test_topology_regression.py` locks in a longest-run assertion and a "no door-bounded component is half the map" check over a fixed seed list that includes the exact repro.
- **Facing regressions (three rounds)** — described in full in §8. Pass 1 (single primary entry + jittered dot product): RNG jitter flipped marginal choices; single entry wrong for multi-door rooms. Pass 2 (55% patrol chance): patrol actors got random initial facing — worse than pass 1. Pass 3 (rayscore with `_clear_ahead >= 3`): improved stationary actors but patrol actors still walked in place because ECWolf's `T_Path` requires things-plane turn-point markers that were never placed, and `_clear_ahead` stopping at door tiles forced actors to patrol away from the player even when entry_pull was correct. The resolution is now marker-routed room-local loops, with the stationary entry-pull as the common case. The engine-faithful `validate_patrols` oracle is the permanent regression test for this failure mode.

**Standing rule from these episodes:** always verify a structural change by measuring the property it could have broken, not just by re-running existing validators. Validity and quality are different axes, and bugs live in the gap between them.

## 10. Determinism and validation

Every RNG call goes through a per-floor `random.Random` seeded from the campaign seed and floor number. Variant selection (§2.1) uses its own `variant_seed` stream so it never perturbs — and is never perturbed by — the per-floor stream. Given the same version, seed, and settings the generator produces byte-identical PK3s (the manifest inside the PK3 records the resolved seed, the floor-by-floor derived seeds, and each floor's variant).

`validate_map`, `validate_objects`, and `validate_door_axes` run after every floor and reject: shallow or visually direct elevator routes, discontinuous critical routes, unreachable exits, out-of-order or redundant key gates, secret lock bypasses, floor tiles outside the sound-zone range, disconnected floor components, doors with mismatched axis tiles, and actor codes outside `ENEMY_CODES`. `tools/fuzz.py` and `tools/smoke_ecwolf.py` exercise the generator across thousands of seeds and against a real ECWolf install respectively.
