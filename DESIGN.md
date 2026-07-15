# InfiniWolf design notes

This document describes how the generator turns a seed into a Wolfenstein 3D floor: the floor-plan grammar, room placement, corridor and door carving, the map analyses that inform later passes, and how enemies and pickups are placed. These systems work together to make seeds varied, readable, and fun to explore rather than merely valid. Start with the end-to-end diagram in [`GENERATION_FLOW.md`](GENERATION_FLOW.md), then use this document for the rationale and implementation details. Everything here reflects `infiniwolf/generator.py`; function names are included so readers can jump straight to source.

## 1. Grid model

Every level is a `GRID = 64` square. Two 4096-entry integer planes back it:

- `tiles` — walls, floor-zone codes, doors, elevator, secret pushwalls.
- `things` — actor and prop codes (WL6 old-num values, per ECWolf's base translator).

Helpers `_at`, `_set`, and `_is_floor` are the only accessors used elsewhere; `_is_floor` accepts the whole floor-zone range plus the dedicated secret-exit zone `SECRET_EXIT_ZONE = 107`.

A `Room` is an axis-aligned rectangle in tile space (`x, y, w, h`, `center` is `(x + w//2, y + h//2)`). No rooms overlap, and `_overlaps` enforces two complete rock tiles between room rectangles. That conservative shell leaves room for elevator and secret-pocket containment.

## 2. Floor-plan grammar (`_plan_floor`)

A floor plan is chosen first, geometry second. The plan is a small directed graph:

- **Spine**: approximately 55% of the planned rooms (up to eleven; nine for the enlarged floor 10), normally `start → beat* → climax → relief → exit`. Floor 9 specializes that ending as `staging → boss-arena → victory → exit`; floor 10 uses `arrival → premium-vault → recovery → exit`. This keeps optional wings and motifs while ensuring the mandatory route forms most of the planned progression.
- **Room tiers**: baseline floors use `standard` (6–9 square), `hall` (major side 9–13), `corridor` (major side 8–13 and minor side 3–4), `anchor` (10–13 square, exactly one per floor — normally the climax room), and `closet` (4–5 square). Floor 10 adds two tiles to the sampled standard, hall, corridor, and anchor dimensions; closets retain their compact size, while floor 9 owns its separate 14–17 tile boss arena. Two or three mandatory spine beats become true `corridor` nodes instead of merely room-shaped connectors.
- **Districts**: two or three (a third is possible once the spine reaches seven rooms); each spine index is assigned to a district and downstream wall-theming reads this.
- **Progression grammars**: `axial-journey`, `hub-relay`, `offset-ladder`, `clustered-chain`, `nested-circuit`, and `bounded-perimeter` independently change circulation-beat rhythm, placement turns, spacing, and the kind of reconvergence used. Adjacent campaign floors cannot repeat a grammar.
- **Motifs**: one purposeful reconvergence is selected from an asymmetric detour, short room loop, courtyard circuit, service loop, ladder rung, nested circuit, or bounded perimeter; up to two of `hub`, `wings`, `gallery`, `courtyard`, and `service` may be added based on the complexity budget. Each reconvergence rejoins after a short local span and is recorded in `loop_edges`; the corridor router rejects routes whose length implies an accidental map-edge wrap.
  - `hub` promotes one middle beat to an anchor tier and hangs two branches + 1–2 closets off it, and downgrades the climax to `standard` so there's only ever one anchor.
  - `wings` adds two same-sized branches (sizes shared via `size_groups`).
  - `gallery` chains 2–3 closets off a middle beat.
- **Circulation composition**: the floor composes its progression grammar with one of six skeletons (`bent-spine`, `parallel-cross`, `central-wings`, `forked`, `perimeter-loop`, or `staggered-grid`). Each district independently chooses one of six modes (`double-loaded`, `single-loaded`, `suite`, `service-bays`, `formal-axis`, or `tunnel-cluster`). Corridor-tier nodes must have graph degree two or greater, so a long hallway cannot terminate without a destination. The base variant weights compatible choices but never owns a fixed layout, so a theme cannot become a recognizable topology stamp.
- **Filler**: rooms are added until `target = min(24, 14 + 2 * complexity)`, giving the five settings distinct 16/18/20/22/24-room plans. Floor 10 adds up to four expedition destinations within the same 24-room cap to offset the higher placement pressure of its larger rooms. Suite and tunnel-cluster districts may extend short room chains. Other modes prefer attaching rooms to circulation nodes, producing corridor-mediated offices, bays, and wings instead of a continuous carpet of adjacent rooms. If a filler host is saturated, placement tries compatible realized rooms in the same district and chooses the legal attachment with the least bounding-box growth. This recovers useful rooms from internal voids without enlarging room dimensions or scattering a long corridor across the map.

The plan carries the graph (`edges`, `loop_edges`), the tier and role of every room, motif membership, circulation skeleton and district modes, and a `critical` spine set. Optional motifs that cannot remain local may be dropped; they never use the global-scatter fallback and therefore cannot create a remote side corridor deeper than the elevator. Ring detours are sized so their reconvergence never shortcuts the spine segment they parallel.

### 2.1 Floor variants (`FloorVariant`, `_variant_sequence`)

Each floor is generated under a named **base variant** — one frozen bundle of the parameters that used to be module constants: mirrored-notch and structural-column chances, corridor widening, plan-grammar hall/closet/motif rolls, allowed wall-material bases, cellblock probability, decoration density, and concept leanings. Five variants rotate on floors 1–8 (`garrison`, `catacombs`, `grand-halls`, `storehouse`, `quarters`); floors 9 and 10 are the forced `stronghold`/`vault` identities. A non-mixed theme bias gives the selected variant triple weight while retaining contrast floors and the no-immediate-repeat rule.

Selection is a pure function of the campaign seed. `LittleEntropyMachine` is the named source for the independent floor, variant, circulation, progression-grammar, campaign-lock, vine-sector, guard-gallery, and rare-motif streams. `_variant_sequence`, `_circulation_sequence`, and `_progression_sequence` exclude immediate repeats, campaign assembly validates those invariants again, and retries affect only the floor-attempt stream. Retries therefore keep both the floor identity and its high-level building organization. The chosen variant, special family, grammar, skeleton, district modes, motif realizations, and compact layout signature are recorded in `GeneratedMap` and the reproducibility manifest.

## 3. Room realisation (`_place_planned_rooms`)

Sizes are drawn from `_room_size` per tier; grouped rooms (e.g. `wings`) share the same drawn size. The start room lands in a randomised quadrant with a `heading` offset from an edge, and every subsequent room is first placed adjacent to its planned parent by `adjacent(...)`. Circulation nodes align their major axis with travel and skeleton-specific turns shape the route; corridor-loaded district modes prefer cross-axis room attachments. This gives each seed a legible floor-plan rhythm without reducing themes to fixed templates. `legal(...)` enforces the 3-tile map margin and pad-2 overlap rule. A saturated filler tries compatible realized hosts in the same district and chooses the legal placement with the least bounding-box growth; other optional rooms that cannot remain local are dropped. Surviving descendants are attached to their nearest realized ancestor, and dropped rooms and edges are cleaned up before the planner returns a `PlacedPlan`.

Every realized start room receives an `ArrivalDetail`. `_place_arrival_elevator` chooses among three rationalized inactive arrivals: an empty car behind the player, a car containing one theme-aware supply, or a player start inside the car. Every arrival retains a real, working elevator door; old-format maps cannot encode a door slab permanently parked open, so it begins closed and opens normally. A bare single-panel façade is forbidden because its visible face can read as an exposed rail rather than a doorway. The car walls use native inert tile 85, which renders the exact same `ELEV1` faces as functional tile 21 without carrying its exit trigger; opening the functional door therefore does not make the elevator capable of changing floors. Cars reuse the real elevator's two-tile interior, recessed side rails, back wall, and complete five-by-five rock envelope so elevator textures cannot leak into another space or through the doorway sightline. Floor 10 uses the same weighted choice rather than forcing an inside-car signature. Outside starts face into the floor with the elevator behind them; inside starts face out through the doorway. A staged car's single ammo, health, food, or treasure item follows the floor variant rather than a raw random roll. The planner's N/E/S/W convention is explicitly translated to ECWolf's old-format East/North/West/South player-start order. Validation proves the exact archetype geometry, player orientation, working door, clear threshold, contextual item, inert panels, and bounded shell.

Floor 9 is a dedicated boss-stronghold grammar. Its single 14–17 tile anchor is the mandatory boss arena, preceded by a visible staging room and followed by a calm victory room before the elevator. A seeded family (`throne-stronghold`, `command-bunker`, `laboratory-gauntlet`, `columned-fortress`, or `central-duel`) owns a distinct profile—stepped apse, offset bunker, paired laboratories, cruciform colonnade, or chamfered duel ring—plus family-specific cover, decoration, and supply placement. This changes motifs, concepts, and arena character independently of the circulation skeleton and district modes. Hans or Gretel supplies the verified native gold-key drop; an optional silver stage can still precede the fight. Floor-9 secrets are reachable before the arena and become seven-item boss-preparation caches with four ammo clips, first-aid, a weapon, and a premium reward.

Floor 10 is a dedicated secret reward expedition, entered from the campaign's secret elevator source and scaled from that source floor rather than treated as a conventional late combat floor. Its independently selected family (`central-vault`, `museum-circuit`, `nested-reliquary`, `abandoned-armory`, or `treasure-palace`) combines with the normal skeleton, districts, motifs, and room-shape choices, preserving substantial cross-seed variation. The arrival is calm, the mandatory route contains a premium vault, and two to four distinct side expeditions use geometry-aware reward compositions. Optional rooms may contain the rare ghost novelty, but the critical route remains readable and keyless.

## 4. Carving and connections

- **Room interiors**: floor codes taken from the district's sound zone (see §6). Normal room-shape variation targets 40% of realized rooms and scales from 0% to 55% across the control. `_carve_notches` supplies mirrored cuts, while `_carve_symmetric_profiles` now includes single chamfers, L shapes, shallow T shapes, offset side bays, stepped crosses, and paired side/end bays. No one family may dominate the shaped set, utility/circulation/arrival rooms remain legible, and validation caps all non-rectangular rooms at 60%. `_add_pillars` provides rarer symmetric structural landmarks where the room tier permits them.
- **Rare late motif**: 3% of campaigns nominate at most one floor from 6–9 for a bounded three-tile-wide hooked-cross (swastika) room profile. It is an optional branch only: never the critical route, boss room, key host, or progression gate. The low rate keeps a historically charged symbol from becoming a routine visual signature while still making it discoverable across campaign seeds.
- **Corridors**: explicit `corridor` rooms establish the building-scale circulation hierarchy. `_carve_connection` then routes safe portal paths between graph neighbours, biased toward clean, centered entrances; it searches the best bounded portal set before using protected seam fallbacks. `_widen_corridors` opens eligible single-tile chokes based on traffic.
- **Doors**: `_door_candidate` finds a single-tile choke on the wall between two rooms and `_door_axis` sets the vertical/horizontal door variant. `_place_doors` skips choke points that would trap a locked-door key behind itself and downgrades officer/SS spawns within three tiles of a door (see `near_door` in `_place_population`) so a door breach cannot reveal a point-blank elite.
- **Secrets**: `_place_secret` and `_carve_secret_pocket` push a wall into a bespoke square, vault, reliquary, gallery, or nested pocket reachable only through its pushwall. The complete footprint and its one-tile shell must be unused wall, so a secret cannot leak into a normally accessible room. Ordinary pockets have three deliberately spaced rewards; floor 9 uses the richer pre-boss cache described above. The campaign's special elevator is planned separately from the ordinary secret budget: it requires a deep optional host, uses a vault/reliquary/gallery/nested approach, then opens into a real two-tile elevator car with a door, side rails, back switch, and a rock envelope. Its matching symmetric wall landmarks provide a subtle in-family hint. Required solvency remains on the normal route, so secrets stay advantageous rather than mandatory.

## 5. Doors, keys, and the exit

`_lock_schedule` uses a campaign-specific seed stream to choose a quota across floors 1–8 rather than rolling each floor independently. The `locked_doors` intensity shifts the quota between unlocked, single-key, and dual-key floors; weighted selection favors later floors, preserves at least two unlocked floors, and forbids three gated floors in succession. Single-key floors balance gold and silver across the campaign. Dual-key floors randomize their order and build a genuine staged route (`key A → lock A → key B → lock B → elevator`). If the geometry cannot make every requested lock individually mandatory, the plan downgrades to one valid gate or no gate instead of locking an optional room. Floor 9 always retains its gold boss-elevator gate and may add a seeded silver pre-boss stage; floor 10 is always keyless.

The ordinary elevator is selected from viable post-anchor rooms only after room connections exist. Its shortest room path must contain at least 90% of the authored progression spine, independent of how many optional rooms are realized; it must also cross a district boundary and reach at least 75% of the deepest ordinary room distance, and the final tile route must contain multiple bends. Candidate selection prefers the deepest qualifying room rather than blindly trusting the nominal `exit` role. `_place_elevator` then installs the native east/west switch geometry. The secret elevator uses the same bounded-car principle and records its pushwall, host depth, shape, destination, hint treatment, and return floor as explicit metadata.

Progression validation simulates every key state with secret pushwalls already open. The first key must be reachable while the exit is blocked; on dual floors the second key must become reachable only after opening the first color while the exit remains blocked; both colors must open the exit. It also opens every other color while withholding one in turn, proving that each lock is independently necessary. A physical key is scored as an off-route objective: its route detour must be meaningful, it avoids room centers and direct doorway sightlines, and dual keys use distinct host rooms. If the geometry cannot provide that objective, the lock is downgraded instead of making a trivial or pointless gate. Floor 9 selects only Hans or Gretel, whose native gold-key drop is verified as part of the progression contract.

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
ECWolf uses the floor tile code as the sound-propagation zone identifier. `_assign_sound_zones` flood-fills contiguous rooms with a shared zone code so a single alerted guard raises everyone in that acoustic pocket, and neighbouring zones stay silent. `_split_oversized_zones` breaks any single zone that grew beyond the safe cap so alerting one guard cannot cascade the whole floor. A final plain-door cleanup removes a doorway when its two sides already share a floor-only route; locked and elevator doors are deliberately excluded.

### 6.4 Wall theming (`_apply_wall_theme`)
Districts select from explicit `WallMaterialFamily` records rather than a flat texture lottery. The roster covers clean and damp grey stone, blue stone and panels, wood, metal, brick, purple, chipped stone, grey brick, marble, brown stone, and plaster. Each family distinguishes base fill, coherent plain variants, atmosphere-gated damage, and sparse landmarks. Floor variants route those materials to compatible building identities: damp/chipped/brown construction favors catacomb or stronghold districts, grey brick favors institutional/service wings, marble favors prestigious halls and the reward floor, and plaster favors later administrative or residential space. Purple is restricted to floors 6–10 so its richer, ominous finish contributes to campaign escalation. No generated floor uses more material groups than its districts can support, and different families may meet only behind a door or protected architectural turn.

Room identity governs landmark eligibility and damage. Maps, vents, signs, banners, portraits, cell panels, and insignia appear only in concepts that give them a reason to exist; a room selects one matching landmark language and places it on a centered wall, mirrored pair, or matched opposing composition. Purple/chipped/grey-brick damage requires both a compatible room and sufficient atmosphere. The clean-grey Achtung sign is excluded from damp/mossy stone because its surrounding masonry does not match. Pushwalls prefer an in-family landmark; families without one use a restrained same-family material anomaly, while plaster pushwalls remain plain instead of borrowing the fake elevator-door surface.

Tiles 13, 16, 33, and 85 remain outside general materials. A fake door is an extremely rare, singly visible, rock-backed service feature and never owns progression. Stained glass is a matched formal composition rather than an isolated panel. Sky is used only on an outside-most wall: every sky tile is completely occluded by its own blocking white pillar, appears as a symmetric pair, and has uninterrupted solid backing to the map boundary. Tile 85 is reserved for inactive elevator arrivals. Validation proves each of these geometric contracts.

Eligible blue-stone rooms independently have a 35% chance to become a cellblock (`JAIL_CANDIDATE_PROBABILITY`, now the default of a per-variant knob — `catacombs` raises it to 60%, `storehouse` disables cellblocks entirely). A cellblock needs a clean five-tile wall run and paints every two of three wall tiles as barred cells: plain bars are nine times as likely as the skeleton variant, while the remaining blue-stone tile reads as a mortar pillar. Loose remains can sit beside plain bars, and jail decoration pools stay deliberately sparse: barrels, blood, and bone variants only. The selected room indices are shared by the wall and decoration passes so the visual treatment and prop bias cannot disagree.

Themes follow finalized doors rather than forcing new doors: `_assign_area_themes` starts from the door-bounded floor components, then union-finds components touching the same bare wall tile. This folds any thin, undoored shared wall into one material group before painting, so distinct materials can meet only at a real door. A bounded pre-theming pass may convert a few of the bridge seams that would otherwise form one oversized merge group into spaced real doors; it never relaxes that union rule. Room-less corridor groups inherit a district through the door graph.

Landmark accents are hung symmetrically on the room's longest clean wall run: short runs get one centered tile, longer runs a mirrored pair, and the longest a center-plus-pair triplet. The caged-skeleton cell wall stays a singular set piece. `_apply_wall_theme` returns each room's landmark cells so decoration can respond to them.

Decoration placement follows theming and is anchored to room features (`_room_anchors`) rather than scattered. A `RoomIdentity` joins the plan-grammar role and tier to the floor variant, district, wall material, special-room status, and a derived human-readable concept such as armory, crypt, mess-kitchen, storage, or war-room. The identity planner balances compatible concept palettes inside each base theme and penalizes assigning the same concept to connected rooms. This prevents a broad variant override (for example, grand halls turning military rooms into lived-in rooms) from collapsing the whole floor into one repeated treatment. Later passes consume that identity instead of independently guessing a theme, so architecture, wall treatment, signatures, clutter, and population tell the same story.

`_room_traversal_frame` also resolves the dominant route through each room from its door entries. Opposing doors win in multi-door rooms; a single door projects toward the room center; doorless rooms fall back to their major axis. Matched lamps, displays, and other paired signatures prefer equal offsets on opposite sides of that route at the same travel depth. Corridors and formal rooms use this strongly, while irregular utility rooms retain more asymmetry. Movement therefore bisects the composition instead of passing two matching objects stranded on one side.

Each eligible concept first attempts a recognizable signature: matched bunks in barracks, wall-backed spear displays in armories and training rooms, wall-backed appliances in kitchens, cages in jails, wall-mounted armor displays and flags in formal military rooms, barrels in storage, and restrained equivalents for the other concepts. Spears, flags, and suits of armor require a solid, non-door wall directly behind them. Flags are singular focal heraldry or matching formal pairs, never freestanding filler. Kitchens are a floor-level set piece capped at one, not the default treatment for every lounge. A stove establishes the room; the sink is independently optional, and any sink, stove, kitchen supplies, and pots are spaced around wall-backed positions rather than emitted as one repeated clump. Pots and pans remain kitchen-only rather than serving as universal filler. The broader registered WL6 static range supplies the concept vocabulary; object 63 (`CallApogee`) is excluded and validation rejects it defensively. Atmosphere filters clean versus grim props without changing topology.

Blocking arrangements are composed as atomic groups and commit only if every member fits and the full-map reachability check still succeeds. Wall-landmark framing has a 15% attempt rate: it frames only the center landmark on one wall, or matching center landmarks on an opposing pair, always with the same prop. Doorway frames have the same low attempt rate and a hard cap of three per floor. Doorway approaches remain clear, and decoration observes the §9.1 statics headroom. Structural one-tile columns are not generic noise: only grand-hall or catacomb concepts may request them, at their variant's roughly 10–15% rate, and the placement remains symmetric.

Each room chooses one compatible lighting family—chandeliers, green ceiling lamps, or floor lamps—before fixtures are composed, so those visual systems never mix within a room and nearby repetition is softly penalized. Open props are anchored to their composed set: kitchen supplies occupy separated wall work zones, storage spill sits by crates or barrels, ceiling fixtures follow room axes, and gore/remains are reserved for concepts and atmosphere levels where they make sense. Vines are not loose clutter: compatible rooms may receive one complete wall-to-wall divider perpendicular to travel. Separately, `LittleEntropyMachine` nominates only one floor per campaign for corridor overgrowth; that floor requests one longitudinal run normally or two roughly 28% of the time. A hallway run fills its complete safe central length, stays within one path/district story, and prefers an existing actor beyond its end or around a bend. It may fail cleanly when no safe corridor exists, but can never collapse to an isolated vine. Dog food is placed only after dog packs are known, near their room and a wall, with no more than three bowls per floor. Sealed secret pockets are never decorated by the ordinary room pass.

## 7. Population placement (`_place_population`)

This pass owns enemy economy and realizes room-owned `EncounterPlacement` records after room identities, doors, progression objects, and wall themes are known. Actors are never assigned by an unexplained floor-wide scatter: each belongs to a visible sentry, staggered flank, blind-corner ambush, strongpoint, objective guard, boss-support group, novelty, or patrol composition. One primary family is selected for the room so its squad reads coherently, while the existing threat budget still controls count and difficulty tiers.

Candidate slots are ranked from the room's actual approach entries and dominant traversal axis. Visible sentries favor a readable five-tile reveal; flanks occupy lateral space; strongpoints and objective guards sit deeper while preserving a route around them. Blind ambushes require real occlusion from the approach or a dedicated guard recess. Ambushes are capped at roughly 18% of combat rooms, never occupy consecutive critical-route beats, remain outside the start safety radius, and retain the near-door elite downgrade.

`_carve_guard_recesses` is a separate, rare hallway grammar rather than a return of the removed general alcove pass. It may create one mirrored pair of one-cell pockets in a suitable hall or corridor. One pocket owns a guard facing the travel lane, the reflected pocket remains clear, and solid shoulders hide the actor until the player reaches the composition. Recesses are forbidden near doors, starts, exits, calm rooms, and progression transitions.

A separate campaign stream may nominate at most one floor for a `GuardGallery`. An optional rectangular formal room is divided by one complete matched line of blocking white pillars. Collision-aware reachability must prove the rear chamber inaccessible while two symmetric stationary guards remain visible and shootable through the screen. The complete rear chamber is reserved before ordinary population, pickup, and decoration passes; consequently it can contain only the gallery's owned guard pair and never a key, health, ammunition, treasure, or decorative clutter. Floors 9–10 and all progression rooms are ineligible.

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

`exit_room` additionally gets its budget multiplied by `0.4` regardless of depth. Recovery and ammo are no longer emitted inside this pass; the pickup planner consumes the finalized encounter economy and route position afterward (§7.5).

### 7.3 Family selection (`pick_family`)
Base weights come from `ENEMY_FAMILIES = ((guard, 10, 1.5), (dog, 6, 0.5), (officer, 3, 3.0), (ss, 1, 6.0))` — name, base frequency weight, expected bullets to down. Officers and SS are scaled by depth:

```
elite_scale = 0.45     if depth < 0.20
              1.35     if 0.60 <= depth <= 0.85
              1.00     otherwise
weights = base * (1 + progression) * elite_scale   for officer/ss
```

So elites are rare near spawn, most common at the pacing peak, and their overall frequency grows across the campaign. `enemy_toughness` (1..5) gates which families are unlocked at all: at toughness 1 only guards spawn; at 4+ SS is unlocked. `near_door(x, y, radius=3)` downgrades any officer or SS drawn within three tiles of a door back to a guard, so a door-breach line-up never point-blank-fires an elite at the player.

FakeHitler is a rare one-off actor only on floor 9, never a boss; the four indestructible Pac-Man ghosts are likewise a single ~10% novelty spawn, restricted to optional rooms on secret floor 10. Neither participates in the normal enemy-family or ammo-budget model. Floor 10 derives its progression pressure from the recorded secret-source floor, while its arrival remains calm; floor 9 similarly reserves its post-boss victory room as a quiet release.

### 7.4 Tier structure
ECWolf treats each `+36` on an actor code as the next cumulative skill tier: tier-1 actors join the base population on medium, tier-2 joins both on hard. `_place_population` places the base tier first, then two rounds of skill-only actors with `extra = round(base_budget * (0.20 + progression * 0.12))`. Skill actors need their own free cells in the `things` plane.

### 7.5 Rewards

`_place_population` owns enemies, not general rewards. After encounters and room identities are finalized, `_place_authored_pickups` converts gameplay needs into explicit intents: early recovery, mandatory-route ammo, post-combat health, exploration treasure, kennel support, and pre-boss stock-up. Necessary supplies remain on the critical route; treasure prefers optional branches, dead ends, relief rooms, and display-oriented concepts. Secret rewards never count toward mandatory solvency.

`_PlacementGrammar` realizes each intent through a named, geometry-aware composition such as a wall cache, entry staging line, recovery station, treasure display, corner cache, or center dais. Callers provide items and compatible rooms, never raw coordinates. Randomness may select among valid templates and orientations, but the whole composition must fit, remain clear of door approaches and reservations, and satisfy wall-backing where required. Each committed group writes a `SpritePlacement` provenance record; `validate_map` proves every ordinary in-room pickup belongs to exactly one such record. This makes rewards read as deliberate level-design decisions while preserving seeded variety.

Floor 9 adds the staging cache and pre-arena secret caches without counting either as a substitute for normal-route solvency. Floor 10 requires a premium route reward and two to four distinct expedition-room compositions; the owning rooms and concepts are recorded so validation can reject a nominal reward floor that repeats one vignette everywhere.

## 8. Actor facing

Enemy facing is the frame the player sees when they open a door. A misfaced guard reads as broken, so this pass is deliberately conservative.

### 8.1 Room entry cells (`room_entries`)
For every room, we collect every floor or door tile immediately outside its boundary (all four sides scanned), then **filter to approach-side entries only**: cells whose BFS distance from `start` is strictly less than the room's own depth. These are the doors the player walked through to reach this room. Cells on the far side (leading deeper into the level) are discarded so actors don't face the exit instead of the entrance. The full list is used as a fallback for the start room or any room with no closer-than-self adjacent cells.

Note on secret doors: pushwall faces are `WALL` tiles in the tiles plane and are never collected. Secret pocket floors are carved at `px+2` onward — two tiles outside the room boundary — and are also never reached by the one-tile scan.

### 8.2 Stationary facing (`_entry_pull`, `_pick_stationary_facing`)
Actors not assigned to patrol routes use `_pick_stationary_facing`, which scores each of the four cardinal directions with `_entry_pull`:

For each entry cell `e`, the pull score for a direction `(dx, dy)` is `para − 2·perp` where `para = dx·(ex−x) + dy·(ey−y)` (only directions where `para > 0` count) and `perp = |dy·(ex−x) − dx·(ey−y)|`. The perpendicular penalty is what makes a south-facing guard near the south wall correctly beat an east-facing guard on the south wall even when the east entry is Manhattan-closer. The direction with the highest score is chosen; if it points at a wall tile the algorithm widens the pool to all four directions rather than forcing a wall-adjacent face.

### 8.3 Patrol actors and turn-marker routing

Patrol activity is a target moving-actor share rather than a per-room attempt. The planner ranks eligible rooms globally and reserves routes until it approaches the selected target: 4%, 9%, 16%, 23%, or 30%. Route families include inset room loops, compact loops, straight hall shuttles, and doorway shuttles through compatible unlocked doors. This produces visible movement at normal settings without turning every encounter into circulating traffic. Routes are reserved before stationary actors, pickups, and decoration, so later passes cannot invalidate them.

This routing is required by ECWolf, not cosmetic. A patrol-coded map thing gets `FL_PATHING` and its spawn angle as `dir` (`gamemap.cpp:630-636`; `gamemap_planes.cpp:331`). In pathing mode `A_Chase` calls `SelectPathDir` after `dir` becomes `nodir` (`wl_act2.cpp:481-535`), but `SelectPathDir` only calls `TryWalk` in that one current direction (`wl_state.cpp:167`); it neither scans nor turns. A failed step therefore leaves the actor permanently walking in place. Native `PatrolPoint::Touch` overwrites the pathing actor's angle and direction when it reaches the marker tile (`g_shared/a_patrolpoint.cpp:44-49`), which turns the actor before its next `TryWalk` can meet the wall.

The old-format marker codes are deliberately cardinal only: 90 east, 92 north, 94 west, and 96 south (`xlat/wolf3d.txt:751`). Markers are always floor tiles, never doors, and are checked for collisions before placement. `validate_patrols` proves that every pathing actor owns exactly one declared route, every marker is owned by exactly one route and points to its next cell, and no pathing actor or marker exists outside that provenance. It then simulates the same constrained engine loop for 512 steps: move only in the current direction across floor or door tiles, apply a marker on arrival, stay within the declared path, and reject any dead end or occupied route tile. This is the regression guard that catches the original walking-in-place failure without needing to observe engine ticks.

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
- The **"necessary items are not hidden" rule** (from the manual). Enforced by `_reachable(..., locked_open=False)` and by placing keys in reachable off-route rooms, never behind pushwalls. A key may be tucked away from the direct door line, but its measured detour and progression state remain explicit and solvable. Secrets are always optional surplus.

### 9.3 Real-map corpus (254 maps, 6277 rooms)

`tools/inspect_map.py` is the reproducible analysis tool. `--compare DIR` walks every `.wad` under a directory, parses each map's WDC3.1 PWAD container (the same container `_wad_bytes` emits), floods door-bounded rooms with the same "zone" definition `_assign_sound_zones` uses, and prints a summary table you can diff against generated output.

The corpus itself was **254 real, playable Wolf3D-family maps** already present on this machine (`ecwolf/mods/installed/…`), spanning:

- The id-numbered `classics_*` conversions — Spear of Destiny and its two official mission packs — in native WL6 tile numbering.
- Well-regarded independent total conversions: `totengraeber`, `rtotenhaus_enh`, `wolfoverdrive`, `pthollenteufel`.

Each was chosen because it uses ECWolf's default tile numbering (walls low, doors 90–101, floor/zone codes 108–143) — the same convention this generator writes — so measurements are directly comparable without format translation. Every measured metric agreed across independently created campaigns spanning 20+ years of Wolf3D mapping, which is why the numbers below were treated as a signature rather than an accident. **No coordinates, dimensions, or layouts from any analyzed file were transcribed into the generator** — only aggregate statistics and community-guide principles.

To reproduce the comparison against a fresh generator build:

```sh
python3 tools/inspect_map.py --compare /path/to/ecwolf/mods/installed
python3 tools/inspect_map.py --seed castle --floor 1 --complexity 3
```

The numbers that shipped into the generator, and the corpus figures that produced them:

| Signal from the corpus | Corpus value | Generator response |
|------------------------|-------------:|--------------------|
| Rooms that are a plain rectangle | 18.1% | Normal generation now targets 40% shaped rooms rather than forcing a rectangular majority. Seven bounded shape families broaden silhouettes while a 60% ceiling protects navigation and combat space. |
| Rooms with an isolated interior pillar/column | 13.8% (median 2 — a mirrored pair) | `_add_pillars` uses a low, variant-controlled rate and prefers symmetric structural pairs, guarded so they cannot become articulation points. |
| Median room aspect ratio | 1.40 : 1 | `hall` destinations and true `corridor` nodes broaden the aspect distribution while ordinary rooms retain useful combat space. |
| Bilateral symmetry rate on rooms ≥25 tiles | 44–46% (not ~100%) | Individual notch and pillar compositions are mirrored, but the complete room graph is not globally mirrored. This keeps spaces legible without making every seed predictable. |
| Rooms per map (mean) | 24.7 | The compact Wolf3D grid targets `min(24, 14 + 2·complexity)` rooms; floor 10 adds up to four planned expedition rooms within the same 24-room cap. Local same-district filler recovery raises Normal realization from roughly 16.3 to 18.1 rooms while retaining the elevator-safe rock shell. |
| Doors per map (mean) | 27.2 | Doors emerge from finalized graph connections and choke analysis, with a 56-door safety budget. |
| Door-graph cycles per map (mean) | 1.1–1.54 | Each progression grammar chooses a local reconvergence family; routing bounds prevent it from growing into an accidental perimeter ring or shortcutting mandatory progression. |

### 9.4 Community level-design guides

B.J. Rowan's 1994 Wolf3D map-design tips and "From Column to Column: The Wolfenstein 3D Level Design Bible" converge on the same qualitative advice, which shows up in:

- **Five room constructs** (square / rectangle / corner / T-junction / intersection): the plan grammar's `standard`/`hall`/`closet`/`anchor`/hub-junction tiers cover the same vocabulary.
- **Mirroring rule** ("if you add a column at the top, add one at the bottom"): `_add_pillars` prefers mirrored pairs; `wings` motif places a mirrored branch pair with shared `size_groups`.
- **Room size cap**: no single room exceeds roughly a quarter of the playable area; enforced by `anchor` tier maxing at 13×13.
- **"Make every room unique"**: districts + explicit wall-material families + motif membership vary each room's silhouette *and* material.

### 9.5 Iteration lessons that shaped the code

Two specific measurements led to permanent regression tests:

- **Bare-seam bug** — a corridor-router fallback could fuse two rooms into one long open sightline, invisible to every solvability validator. Found by measuring the longest unobstructed straight run across seeds (values of 22–36 tiles with no door on that row/column). Fix: the router ranks portal pairs and searches the best 64, then uses protected seam fallbacks that refuse to cross cells owned by unrelated rooms. `tests/test_topology_regression.py` locks in a longest-run assertion and a "no door-bounded component is half the map" check over a fixed seed list that includes the exact repro.
- **Facing regressions (three rounds)** — described in full in §8. Pass 1 (single primary entry + jittered dot product): RNG jitter flipped marginal choices; single entry wrong for multi-door rooms. Pass 2 (55% patrol chance): patrol actors got random initial facing — worse than pass 1. Pass 3 (rayscore with `_clear_ahead >= 3`): improved stationary actors but patrol actors still walked in place because ECWolf's `T_Path` requires things-plane turn-point markers that were never placed, and `_clear_ahead` stopping at door tiles forced actors to patrol away from the player even when entry_pull was correct. The resolution is now marker-routed loops and shuttles, with the stationary entry-pull as the common case. The engine-faithful `validate_patrols` oracle is the permanent regression test for this failure mode.

**Standing rule from these episodes:** always verify a structural change by measuring the property it could have broken, not just by re-running existing validators. Validity and quality are different axes, and bugs live in the gap between them.

## 10. Determinism and validation

`LittleEntropyMachine` derives every independent seed stream, after which floor-local choices use an isolated `random.Random`. Variant, circulation, progression grammar, lock schedule, vine-sector, guard-gallery, rare-motif, and floor-attempt streams cannot perturb one another. Given the same version, commit, seed, and settings the generator produces byte-identical PK3s. The manifest names `LittleEntropyMachine` and records the version, commit, resolved seed, floor seeds, arrival, encounter templates and families, patrol routes and target, guard recesses and galleries, wall treatments, circulation skeleton, progression grammar, district modes, motif realizations, layout signature, room shapes and concepts, lighting families, vine schedule/screens, key objectives, secret details, special-floor anchors, and pickup compositions. `infiniwolf-settings.txt` repeats every campaign input in a concise human-readable form and includes a copyable command; package validation cross-checks it against the manifest before installation.

After gameplay generation is final, `apply_campaign_watermark` permutes only sound-zone labels: it never changes which cells share an acoustic zone. Two different weighted residues bind those labels to door coordinates and selected ordinary thing codes on every map. Thus a standalone floor remains checkable with about a 1-in-731 accidental two-residue match before structural evidence; the ten primary targets also total 42 modulo 43. `tools/check_infiniwolf.py` works as a CLI or optional Tk GUI and deliberately ignores the manifest when deciding the result. This is robust provenance evidence, not a secret or a cryptographic signature: a determined party with the public encoder can forge it.

`validate_map`, `validate_objects`, and `validate_door_axes` run after every floor and reject: invalid arrival cabs or player facing, actors without encounter provenance, consecutive critical-route ambushes, stationary floors that miss a meaningful patrol setting, broken patrol routes, malformed guard recesses or galleries, inaccessible-room pickups, shallow or visually direct elevator routes, discontinuous critical routes, unreachable exits, out-of-order, trivial, or redundant key gates, secret lock bypasses, unbounded or normally exposed secret elevators, incompatible wall families, malformed fake-door/glass/sky compositions, invalid or dominated room profiles, incomplete vine runs, unbacked spear/armor/flag displays, mixed room lighting families, invalid circulation hierarchy, dead-end corridor nodes, corridor mediation outside its quality band, excessive perimeter-wrap routes, pickups without exact provenance, floor tiles outside the sound-zone range, disconnected floor components, doors with mismatched axis tiles, and actor codes outside `ENEMY_CODES`. Campaign validation additionally proves non-repeating adjacent floor types, circulation skeletons, and progression grammars plus the single-floor vine, gallery, and rare-motif budgets. Special-floor validation proves the floor-9 themed arena/staging/victory contract and the floor-10 source/premium/expedition contract. These checks protect the qualities players actually notice—fair progress, readable layouts, and worthwhile exploration—not just file validity. `tools/fuzz.py` and `tools/smoke_ecwolf.py` provide broader seed and engine coverage.
