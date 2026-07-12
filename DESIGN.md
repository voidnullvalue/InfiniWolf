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

- **Spine**: five to eight rooms, always `start → beat* → climax → relief → exit`. `spine_count` scales with `--layout-complexity` and floor number.
- **Room tiers**: `standard` (6–9 square), `hall` (long-side 9–13), `anchor` (10–13 square, exactly one per floor — the climax room), `closet` (4–5 square).
- **Districts**: two or three (a third is possible once the spine reaches seven rooms); each spine index is assigned to a district and downstream wall-theming reads this.
- **Motifs**: `ring` is always present; up to two of `hub`, `wings`, `gallery` are added based on the complexity budget. Each motif is realised as extra rooms and edges:
  - `ring` picks a `(left, right)` pair biased toward the spine centre and inserts a 1–2 room detour that rejoins at `right`; the rejoin edge is recorded in `loop_edges`.
  - `hub` promotes one middle beat to an anchor tier and hangs two branches + 1–2 closets off it, and downgrades the climax to `standard` so there's only ever one anchor.
  - `wings` adds two same-sized branches (sizes shared via `size_groups`).
  - `gallery` chains 2–3 closets off a middle beat.
- **Filler**: rooms are added until `target = min(20, 14 + 2 * complexity)`. Filler prefers previous filler tips 55% of the time so the perimeter breathes; otherwise it hangs off the lowest-degree middle beat.

The plan carries the graph (`edges`, `loop_edges`), the tier and role of every room, motif membership, and a `critical` set (spine + motif-added rooms — filler is not critical for lock-and-key placement).

## 3. Room realisation (`_place_planned_rooms`)

Sizes are drawn from `_room_size` per tier; grouped rooms (e.g. `wings`) share the same drawn size. The start room lands in a randomised quadrant with a `heading` offset from an edge, and every subsequent room is placed adjacent to its parent by `adjacent(...)`. `legal(...)` enforces the 3-tile map margin and the pad-2 overlap rule; if a room can't fit adjacent to its parent, the plan drops it (and its subtree). Dropped rooms and edges are cleaned up before the planner returns a `PlacedPlan`.

## 4. Carving and connections

- **Room interiors**: floor codes taken from the district's sound zone (see §6). Interior variety comes from `_carve_notches`, `_carve_alcoves`, and `_add_pillars`, each guarded by the room tier so anchor/hall rooms get more structure than closets.
- **Corridors**: `_carve_connection` runs an L-shaped path between paired rooms, biased to enter through the room's short face. `_widen_corridors` opens the tightest single-tile chokes to two tiles based on traffic (paths that lie on more than one connection get widened).
- **Doors**: `_door_candidate` finds a single-tile choke on the wall between two rooms and `_door_axis` sets the vertical/horizontal door variant. `_place_doors` skips choke points that would trap a locked-door key behind itself and downgrades officer/SS spawns within three tiles of a door (see `near_door` in `_place_population`) so a door breach cannot reveal a point-blank elite.
- **Secrets**: `_place_secret` and `_carve_secret_pocket` push a wall into a small alcove reachable only from the room side. Pockets never break outer-wall integrity; there's a run-time check in `validate_map`.

## 5. Doors, keys, and the exit

`_key_spot` places the gold key in a room whose distance from `start` (with the locked door closed) is largest, which forces the player to detour through the locked wing rather than getting the key on the way. `_reachable` re-checks with the locked door open to ensure the exit is still reachable. `_place_elevator` places the exit switch in the exit room, and, when the floor has a secret exit, secret exits are guarded by a two-tile "elevator pocket" so the switch face never bleeds through walls (fix landed in `e20a530`/`89b04f6`).

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
ECWolf uses the floor tile code as the sound-propagation zone identifier. `_assign_sound_zones` flood-fills contiguous rooms with a shared zone code so a single alerted guard raises everyone in that acoustic pocket, and neighbouring zones stay silent. `_split_oversized_zones` breaks any single zone that grew beyond the safe cap so alerting one guard cannot cascade the whole floor.

### 6.4 Wall theming (`_apply_wall_theme`)
District determines which `WALL_THEMES` entry is used; `DECOR_WALLS` are inserted only as accents (portraits, banners, insignia), never as room material, following original-episode conventions. `SECRET_HINTS` covers the fallback banner/portrait tiles used on pushwalls when a floor's theme lacks a decor accent of its own.

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

### 7.4 Tier structure
ECWolf treats each `+36` on an actor code as the next cumulative skill tier: tier-1 actors join the base population on medium, tier-2 joins both on hard. `_place_population` places the base tier first, then two rounds of skill-only actors with `extra = round(base_budget * (0.20 + progression * 0.12))`. Skill actors need their own free cells in the `things` plane.

### 7.5 Rewards
After enemies are placed, every second room places an ammo/food/first-aid or treasure pickup in an unused candidate cell. `_ensure_early_heal` guarantees a first-aid in the low-depth zone so a rough opening doesn't spiral.

## 8. Actor facing (rayscore)

Enemy facing is the frame the player sees when they open a door. A misfaced guard reads as broken, so this pass is deliberately conservative.

### 8.1 Room entry cells (`room_entries`)
For every room, we collect *every* floor or door tile immediately outside its boundary — all four sides scanned. Storing the full list (not just the closest to `start`) is what lets a two-door room orient different actors toward different doors.

### 8.2 Rayscore (`_clear_ahead`, `_entry_pull`, `_pick_facing`)
For every candidate facing (N/E/S/W), we compute two numbers from tile geometry:

- **`_clear_ahead(x, y, idx)`**: number of open floor tiles ahead in that direction, capped at 5. This is what makes patrol facing safe — patrol actors physically walk forward in their initial facing, and less than 3 clear tiles produces the "running against a wall" bug.
- **`_entry_pull(x, y, idx, entries)`**: for each entry cell `e`, take the parallel component `para = dir · (e − pos)` and the perpendicular magnitude `perp = |dy·vx − dx·vy|`; only entries with `para > 0` count, and the score is `para − 2·perp`. The perpendicular penalty is why a south-facing guard near the south wall correctly beats an east-facing guard on the south wall even when an east entry is Manhattan-closer.

`_pick_facing(x, y, room, want_patrol)` combines them:

- **Patrol**: require `clear_ahead >= 3`. Among qualifying directions, maximise `entry_pull`, break ties on `clear_ahead`. If no direction qualifies, the actor is downgraded to stationary — better a facing-a-wall standing guard than a walking-into-a-wall patrol.
- **Stationary**: require `clear_ahead >= 1` when possible (avoid the ugly nose-in-wall pose). Maximise `entry_pull`, break ties on `clear_ahead`.

Dogs are patrol-only in WL6 (no stationary sprite). If the geometry forces a dog to stationary, the spawn is skipped rather than emitting a broken code.

### 8.3 Patrol probability
```
guard: 0.35   officer: 0.20   ss: 0.10   dog: 1.00
```
Guards were briefly boosted to 0.55 in `4aa1042` as a workaround for the facing bug; that bump has been reverted now that the rayscore keeps patrols oriented and walkable.

## 9. Determinism and validation

Every RNG call goes through a per-floor `random.Random` seeded from the campaign seed and floor number. Given the same version, seed, and settings the generator produces byte-identical PK3s (the manifest inside the PK3 records both the resolved seed and the floor-by-floor derived seeds).

`validate_map`, `validate_objects`, and `validate_door_axes` run after every floor and reject: unreachable exits, unreachable keys, floor tiles outside the sound-zone range, disconnected floor components, doors with mismatched axis tiles, and actor codes outside `ENEMY_CODES`. `tools/fuzz.py` and `tools/smoke_ecwolf.py` exercise the generator across thousands of seeds and against a real ECWolf install respectively.
