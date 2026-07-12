# Wolfenstein 3D Level Quality and Balance for Automated Generation

## Executive summary

A "good" classic **Wolfenstein 3D** level is not primarily a maze, nor primarily a sandbox. It is a **legible sequence of door-delimited combat cells**, short orthogonal connectors, optional reward branches, and conservative key gating that fits the original engine's hard limits: a **64×64 map**, **37 floor areas**, **64 sliding doors**, **150 actors**, **400 statics**, and **64 wall tiles**. The engine's geometry is fundamentally constrained to same-height, orthogonal wall blocks on a square grid, so balance comes less from complex architecture and more from **where doors, corners, offsets, sightlines, and enemy types are arranged**. citeturn41view0turn41view1turn41view2turn43view0turn43view1

For automated generation, the most important design rule is that **routine combat should happen mostly inside reliable ballistic ranges**. In the original game, player gunfire is strong at close range, degrades sharply with distance, and becomes impossible beyond about **21 tiles**; the manual also advises the player to fight at close range, enter rooms from an angle, and avoid rushing straight through doors. That means good generated levels should usually hold important firefights in about **3–12 tiles** of unobstructed line of sight, use **offset doors and corners** to create peekable attacks, and reserve **15–21-tile lines** for rare spectacle corridors or optional set-pieces rather than mandatory attrition fights. citeturn19view0turn22view3

Fairness in Wolf3D also has unusually clear original guidance. The manual explicitly says **necessary items are not hidden**, keys are on the same level as the locked doors they open, and hidden passages are a source of extra ammo and health rather than core progression. That is extremely useful for a generator: **do not put mandatory progression behind secrets**, keep keys on readable side branches, and treat secrets as optional surplus or shortcuts. Community documentation on original maps shows that when this rule is violated, such as the famously hostile **Episode 2, Floor 2** mandatory-secret case, the level is remembered as unfair rather than elegant. citeturn22view0turn22view3turn38search3

For a final logic pass, the best implementation strategy is to treat generation as a sequence of **hard validation rules**, then a smaller set of **threat-budget and resource-solvency repairs**. Validate connectivity, non-secret main progression, engine limits, and line-of-sight thresholds first. Then solve local balance with targeted edits: downgrade or move an SS, widen a corridor from 1 tile to 2, add an offset corner, insert a visible clip before a mutant introduction, or move a key from a secret to a visible side room. The final pass should be able to explain every fix in terms of **engine mechanics, original map practice, or manual guidance**. citeturn28view3turn29view4turn18view0turn22view3

## Assumptions and engine realities

This report assumes a **classic DOS-like Wolf3D rule set** with the original tile grid, original sprite AI logic, original door behavior, original damage formulas, and original episode-style progression. If your pipeline targets **ECWolf, LZWolf, Jaguar/SNES-derived ports, or custom source mods**, treat several details here as variant-sensitive: pushwall behavior, some item semantics, ammo semantics on ports, and certain bug quirks differ outside vanilla. Those variant choices were **not specified** in the request, so where behavior differs by port, this report calls that out rather than guessing. citeturn24search3turn24search10turn43view1

At the data-structure level, original Wolf3D maps are **64×64 tile grids** with a wall/door layer and an object layer for player start, enemies, items, and bonuses. The world is strictly orthogonal: walls are same-height blocks, intersections are rectangular, and the engine does not support stairs, height changes, or room-over-room geometry. These are not cosmetic details; they are the reason main-path readability, area partitioning, and line-of-sight control matter so much more than in later id engines. citeturn41view0turn43view0turn43view1

The original source code also exposes the limits that matter most to a procedural pipeline. The engine caps maps at **64×64**, uses **37 area IDs** for connectivity logic, allows at most **64 sliding doors**, **150 actors**, and **400 statics** such as lamps, treasure, and bonus objects. The same header also fixes the starting ammo constant at **8**, and the new-game initialization sets the player to **100 health**, **pistol**, **8 ammo**, and **3 lives**. A generator that does not track those budgets can easily output maps that are valid "on paper" but unstable or unplayable in an authentic implementation. citeturn41view0turn41view1turn41view2turn41view3turn41view4turn42view0turn42view1turn42view2

Doors matter mechanically, not just visually. In the original source, a door opening **connects adjacent areas**, a door remains open for **300 tics** with the code comment "Close the door after three seconds," and when the door closes it disconnects those areas again. This means your level logic should think in terms of **combat and sound partitions**: too many tiny door-separated regions can exhaust area IDs or produce brittle AI activation patterns, while too few doors flatten pace and remove Wolf3D's signature "room-by-room breach" rhythm. citeturn28view0turn28view2turn28view3turn5view0

Pushwalls also matter mechanically. The original pushwall code increments the pushed wall forward and stops once "the block has been pushed two tiles," unless blocked earlier. In vanilla-compatible play and community documentation, secret-wall overtravel and secret blocking can still create awkward or even broken outcomes in some versions or framerate conditions, so a generator should **backstop every rewarded pushwall with a known stopping condition** rather than assuming secrets are harmless. citeturn29view4turn24search3turn24search10

## Geometry, flow, readability, and secrets

Representative original maps show the dominant Wolf3D grammar clearly: **orthogonal rooms joined by short halls, doors at room thresholds, a readable main route, optional side pockets, and secrets tucked just off the critical path**. Episode 1 Floor 1 is practically a tutorial in door-separated escalation and optional secret reward, while Episode 2 Floor 2 shows how the same grammar becomes harsher when key routing, early pressure, and poor visible health are combined. citeturn36image0turn36image1turn37image0turn38search3

iturn36image1turn37image0turn36image0turn37image1

The most robust room-and-corridor defaults for generation are these:

| Element | Recommended baseline | Hard floor | Soft floor | Why |
|---|---:|---:|---:|---|
| Main room size | 5×5 to 11×11 walkable | 4×4 | 13×13 | Enough room for enemy separation, dodging, landmarks |
| Small side room / closet | 2×2 to 4×4 | 2×2 | 5×5 | Good for pickups, single ambush, secrets |
| Default corridor width | 2 tiles | 1 tile | 3 tiles | 2 tiles is the safest "Wolf-like" default |
| One-tile corridor usage | ≤ 25% of traversed path | 0% on start route | 35% | Reserve for tension, not most of navigation |
| Straight corridor length | 4–12 tiles common | 3 | 18 | Longer sightlines need explicit intent |
| Doors between major cells | 1 per room transition | 0 | 2 chained max | Supports pacing without exhausting door/area budget |
| Keys on a level | 0–2 typical | 0 | 3 with caution | Gold/silver only; overuse hurts readability |

These recommendations are synthesis, but they follow directly from the engine's orthogonal tile world, the original maps, and the manual's emphasis on angled entry, peeking, and close-range fighting. citeturn43view0turn43view1turn22view3turn36image0turn37image0

**Corridor width** is one of the highest-value generator choices. The engine allows 1-tile corridors, but because combat is tile-based, doors open into a single lane, enemies path by tile selection, and officers/dogs rush fast, overusing 1-tile halls makes the map feel sticky, door-campy, and over-deterministic. Use **2 tiles as the default width**, allow **1-tile halls** only for short connectors, secret runs, or deliberate pressure bursts, and use **3-tile halls** only for hub junctions or late-game spectacle spaces. The manual's "Don't Rush Into the Room!" and "Get at an Angle" advice is effectively a warning against perpendicular, single-lane breach traps becoming the entire map. citeturn16view1turn16view2turn22view3

**Sightline heuristics** should be explicit in code. Because player bullets degrade hard with distance and become impossible past roughly **21 tiles**, the logic pass should classify sightlines like this:

| Sightline length | Use |
|---|---|
| 1–3 tiles | melee or point-blank breach only; dangerous if hitscan enemies are centered |
| 4–8 tiles | ideal routine engagement length |
| 9–12 tiles | good midrange pressure band |
| 13–18 tiles | set-piece or high-readability lane; needs cover or door segmentation |
| 19–21 tiles | rare spectacle / optional lane only |
| 22+ tiles | avoid for mandatory combat; player cannot hit |

This thresholding follows directly from the original damage and hit formulas plus the manual's close-range guidance. In practice, it means the final pass should measure straight unobstructed tile runs from **door thresholds**, **spawn room exits**, and **critical-path nodes**, then add bends, pillars, or alcoves where those values exceed target bands. citeturn19view0turn22view3

Flow should also be segmented, not amorphous. A strong Wolf3D floor usually reads as **onboarding → first branch → first key or weapon relief → escalation loop → climax → short exit run**. Branching is best when it is **shallow but meaningful**: let the player choose between reward, progress, or information, but avoid branching so evenly and symmetrically that rooms blur together. This is one reason E1F1 still reads well: it branches enough to feel exploratory, but not enough to become a maze. citeturn9view0turn36image0

```mermaid
flowchart LR
    A[Start room] --> B[Light breach]
    B --> C{Choice point}
    C -->|Main path| D[Key route]
    C -->|Optional reward| E[Secret or ammo side room]
    D --> F[Locked gate]
    E --> F
    F --> G[Escalation loop]
    G --> H[Climax room]
    H --> I[Short relief corridor]
    I --> J[Exit elevator]
```

That structure matches both original manuals and original maps: keys and elevators are on the normal route, while secrets supply optional advantage, score, or shortcuts. citeturn22view0turn22view3turn9view0turn9view1

A practical **backtracking rule** for generation is: allow **10–35% critical-path overlap** after the first key, but if backtracking exceeds that, either create a loop or ensure that the return trip is meaningfully recontextualized by new enemy activation, a second lock, or a visible reward. Long pure retraces are especially bad in Wolf3D because the engine offers little vertical or systemic variation to keep revisits fresh. Maps like **Episode 6 Floor 8**, which have very long pars, show that long duration can work, but only when structure, theme, and escalation remain legible. citeturn38search12turn36image1turn37image0

Aesthetic readability matters more than many procedural systems assume. Because walls are same-height and intersections are rectangular, the player orients through **texture zones, room silhouette, door offsets, statics, and reward placement**. A room that is a tiny square with doors on all sides can be memorable, but it is also intrinsically disorienting because landmarks disappear; Liz Ryerson's level analysis of an original map is excellent evidence that "strange" rooms can create atmosphere yet also destabilize the player's spatial memory. For a generator, that means anomaly rooms should be **rare accents**, not default junctions on the main route. citeturn40view0

Secrets should follow the manual's implied contract: they are **optional value**, not mandatory infrastructure. Good secret budgets are usually **2–6 per standard floor**, with at least one practical reward secret and at least one flavor/score secret. Recommended secret reward balance is **15–35% of total optional resources** on the map, while the non-secret route remains fully solvable. Use subtle but detectable telegraphing: off-pattern eagles, suspicious dead-end panels, treasure "teasers," or slightly anomalous wall runs. Place a **hard backstop exactly two tiles** behind important pushwall rewards so original pushwall motion cannot seal or overshoot the reward. citeturn22view0turn22view3turn29view4turn24search3turn35search16

## Enemies, items, and progression

The table below uses original enemy behavior as documented in the manual, source-backed community references, and the source release. The numerical stats are factual; the **threat weights** and **placement rules** are recommended synthesis for procedural generation. citeturn18view0turn19view1turn22view2

| Enemy | Canon stats and behavior | Suggested threat weight | Recommended placement heuristic |
|---|---|---:|---|
| Guard | 25 HP; pistol; drops used clip worth 4 ammo; common baseline enemy. citeturn18view0turn34view1 | 1.0 | Use in groups of 2–5. Good behind first doors, side rooms, and visible patrol lanes. Safe default filler. |
| Dog | 1 HP; melee only; fastest enemy; patrol only, no standing spawn. citeturn18view0 | 0.5 | Use 1–3 at a time, especially mixed with humans. Strong as door rushers or corridor pressure. Avoid using large dog-only swarms. |
| Officer | 50 HP; pistol; faster reaction and lateral movement than guards. citeturn18view0 | 1.75 | Use 1–2 early, 2–4 late with cover. Dangerous in narrow halls; never stack many in immediate spawn LOS. |
| SS | 100 HP; machine gun bursts; very high burst threat; drops machine gun or used clip. citeturn18view0 | 3.0 | Use 1–2 in routine play, 3 only in larger rooms or offset cover. Highest regular priority target. |
| Mutant | 45/55/55/65 HP by difficulty; rapid double shot; silent alert; poor surprise fairness if under-supplied. citeturn18view0 | 2.25 | Introduce with visible ammo nearby and cover. Use in corners and offset approach lanes, not as surprise frontload in pistol-starved starts. |
| Boss | Usually ambushes, always face player, do not behave like regular fodder. citeturn18view0 | 8–15 | Opt-in only. Boss floors should simplify layout and raise arena clarity rather than add maze complexity. |

Two original-mechanics details should drive your AI-aware placement rules. First, non-boss regular enemies can be **standing or patrolling**, except dogs, which are patrol-only. Second, some enemies can be flagged **ambush**, in which case they ignore weapon noise and only engage on direct sight. Bosses are effectively always treated as ambushes for activation purposes. Those flags are a gift to procedural generation: use ordinary patrols for readable pressure, and use ambush sparingly to create **earned** surprises rather than constant unfairness. citeturn18view0turn41view0turn31view3

The original source also shows why geometry and AI cannot be separated. Enemies choose tile directions toward the player, open doors during movement, and resolve pursuit through tile-based chase selection. Closed doors reconnect/disconnect areas, which changes how broadly enemies can become active. In other words, a "combat encounter" in Wolf3D is partly a room composition problem and partly an **area-partition and door-threshold problem**. citeturn16view1turn16view2turn28view0turn28view2

For automated placement, a simple threat-budget model works well:

- **Guard** = 1.0
- **Dog** = 0.5
- **Officer** = 1.75
- **Mutant** = 2.25
- **SS** = 3.0
- **Boss** = 8.0–15.0 depending on arena and support enemies

Then target zone budgets like these:

| Episode band | Typical major-zone budget | Density target |
|---|---:|---:|
| Early Episode 1 | 2–4 threat | 0.03–0.05 enemies / walkable tile |
| Late Episode 1 to early Episode 2 | 4–6 threat | 0.05–0.07 |
| Late Episode 2 to Episode 3 | 5–8 threat | 0.06–0.08 |
| Episodes 4–5 | 6–10 threat | 0.07–0.10 |
| Episode 6 | 7–12 threat | 0.08–0.12 |

Those values are synthesis, but they are consistent with the original episode introductions, the roster rollout in the manual, and late-map examples such as **Episode 6 Floor 1** with **137 enemies** and **Episode 6 Floor 9** being remembered as a map in which many enemies hear you at once. Treat those late originals as upper-bound stress tests, not as your default output. citeturn22view2turn38search8turn38search0

The table below covers items, pickups, and recommended placement logic. As above, the item effects are factual; the right-hand placement rules are guidance for generation. citeturn22view1turn34view0turn34view1turn34view2turn34view3turn35search1turn35search3turn35search4turn35search2

| Item | Canon effect | Baseline generation rule | Secret generation rule |
|---|---|---|---|
| Dog food | +4 health. citeturn35search1 | Use as light correction after dogs or chip damage. | Fine in dog-themed closets or low-tier secrets. |
| Food | +10 health. citeturn22view1turn35search3 | Default visible heal. Place every 1–2 light fights. | Good in exploration side rooms. |
| First aid | +25 health. citeturn22view1turn34view0 | Place before or after a defined spike, or in visible side room near key path. | High-value secret reward. |
| Blood/gibs | +1 health only at low health. citeturn35search4 | Do not rely on for balance. Flavor only. | Fine as incidental flavor. |
| Ammo clip | +8 ammo from map; +4 when dropped by gunners. citeturn22view1turn34view1 | Baseline resource unit. Place before new enemy-type spikes and after ammo-negative groups. | Excellent practical secret reward. |
| Machine gun | Weapon upgrade; practical DPS increase. citeturn22view1turn22view0 | Put one early if floor can start from pistol. | Strong secret reward on early floors. |
| Chaingun | Best standard weapon; strong room-clear tool. citeturn22view0turn22view1 | Use sparingly; mid/late floor payoff or hard secret. | Premium secret reward. |
| Key | Gold or silver; unlimited uses for matching locks. citeturn34view2turn22view0 | Must be on visible, normal route or obvious side branch. Never secret-gated. | Usually avoid hiding. |
| Treasure | 100/500/1000/5000 score depending on type. citeturn22view1turn35search2 | Use to guide optional exploration and theme rooms. | Core secret filler. |
| One-up | Full health, +25 ammo, extra life; secret-only in original games. citeturn22view1turn34view3 | At most one per standard floor, and usually not visible on main path. | Best capstone secret reward. |

For **ammo budgeting**, treat enemies as "expected bullet sinks" rather than raw HP only. Using the original damage formulas, guards are cheap to kill at close range, officers and mutants are moderate, and SS become expensive if engaged from mid-long range. A practical conservative model for routine 3–8-tile fights is:

- Guard: **1.5 bullets**
- Dog: **0.25 bullets** if you expect knife play, otherwise **1.0**
- Officer: **3 bullets**
- Mutant: **3 bullets**
- SS: **6 bullets**

Then compute:

```text
expected_ammo_need =
    1.5*guards +
    dog_cost*dogs +
    3.0*officers +
    3.0*mutants +
    6.0*ss
```

and require:

```text
accessible_ammo =
    start_ammo +
    8*(placed_map_clips) +
    4*(conservative_gunner_drops) +
    25*(full_heals if counted as ammo source)
```

with a target of **accessible_ammo ≥ 1.2 × expected_ammo_need** for a normal floor and **≥ 1.35 × expected_ammo_need** for a standalone, pistol-start floor. The underlying reasons are original: map clips give 8, dropped clips give 4, the player starts a new game with a pistol and 8 rounds, and long-range fire gets progressively inefficient. citeturn34view1turn42view0turn19view0

For **health budgeting**, Wolf3D benefits from local rather than global rules. Use these:

- Put the **first visible heal** within **20 traversed tiles** of a pistol-start spawn.
- After any combat zone with **threat ≥ 6**, ensure there is either **+10 to +25 visible healing** within **12 tiles** or a nearby safe side room.
- Do not let the first encounter with a new high-tier threat be both **ammo-negative** and **heal-negative** unless the level explicitly aims to mimic infamous original punishment maps. citeturn22view1turn34view0turn38search3

Across episodes, follow original roster progression rather than randomizing all enemies from the start. The manual gives the cleanest episode-level cadence: Episode 1 focuses on guards, SS, and dogs; Episode 2 adds mutants; Episode 3 adds officers; Episodes 4–6 remix the same tools into harsher Nocturnal Missions with denser, meaner compositions. That rollout is valuable because it teaches both pace and counterplay. citeturn22view2

## Prioritized checklist

The checklist below is ordered for a **final logic pass**. The items at the top are hard failures or near-hard failures under authentic Wolf3D assumptions; the lower items are quality or polish constraints derived from the same engine and level-design sources. citeturn41view0turn41view1turn41view2turn19view0turn22view3

1. **Main progression is fully solvable without secrets.** Keys, locks, and the exit must be reachable through non-secret paths. This is the single highest-priority fairness rule.
2. **Map stays under engine hard limits.** Enforce: map 64×64, areas ≤ 37, doors ≤ 64, actors ≤ 150, statics ≤ 400, wall tiles ≤ 64. Prefer soft caps below those.
3. **Start condition is validated against the intended loadout profile.** At minimum test a fresh-episode pistol start. If the level is for carryover play, test at least one weak carryover profile too.
4. **Critical-path sightlines are within target bands.** Routine mandatory fights should occur mostly at 3–12 tiles, rarely above 18, never depend on >21-tile engagements.
5. **No unavoidable instant-death door reveals.** If a door breach opens into officers, mutants, or SS, the first high-tier shooter must be offset laterally or set back.
6. **Keys are placed on readable branches.** No hidden key, no same-color self-locking, no giant retrace to discover the only usable branch.
7. **Ammo solvency passes conservative estimates.** Use expected bullet sinks, visible clips, and conservative drops.
8. **Healing is paced locally, not just totaled globally.** Every spike must have a nearby recovery opportunity or safe reset pocket.
9. **One-tile corridors are rationed.** Default to 2 tiles; use 1-tile halls deliberately and briefly.
10. **Room silhouettes and textures create landmarks.** Every major branch or key node needs a visual identity.
11. **Secrets are optional surplus, not hidden chores.** Aim for 2–6, with practical and score rewards mixed.
12. **Pushwall secrets are backstopped.** Two-tile pushwall logic must not jam or seal rewards.
13. **Backtracking is purposeful.** If overlap exceeds roughly one-third of the critical path, add a loop, a reveled shortcut, or fresh context.
14. **Enemy introductions respect episode learning.** Do not frontload mutants or clustered SS on a pistol-starved new-player floor unless you intentionally want a tribute to punitive originals.
15. **Soft performance headroom remains.** Ideal final values: areas ≤ 32, doors ≤ 56, actors ≤ 120, statics ≤ 320.

## Evaluation metrics

For code, treat "good and balanced" as a measurable property set, not a vague aesthetic label. The thresholds below are intentionally conservative; they are suitable for a **repair pass** that should reject or auto-fix marginal outputs instead of shipping them. The thresholds are derived from the engine limits, ballistic formulas, original-map routing patterns, and the original manual's progression contract. citeturn41view0turn41view1turn41view2turn19view0turn22view0turn22view3

| Metric | How to compute | Target | Hard fail | Typical auto-fix |
|---|---|---|---|---|
| Non-secret solvability | BFS over non-secret doors/floors from start to exit with keys collected normally | True | False | Move key/exit/lock off secret path |
| Area count | Distinct floor areas used by connectivity logic | ≤ 32 | > 37 | Merge adjacent floorcodes; remove excess door partitions |
| Door count | Sliding doors placed | ≤ 56 | > 64 | Replace some doors with open thresholds |
| Actor count | Enemies + actors | ≤ 120 | > 150 | Remove or downgrade filler groups |
| Static count | Items, decor, lamps, etc. | ≤ 320 | > 400 | Cull decor and low-value treasure |
| Critical-path sightline p90 | 90th percentile straight LOS from critical-path nodes | 12–16 | > 21 | Add bend, pillar, alcove, or door offset |
| Max mandatory sightline | Longest mandatory LOS | ≤ 18 | > 21 | Segment with cover or geometry |
| One-tile path share | Fraction of critical path in 1-tile corridors | ≤ 0.25 | > 0.40 | Widen selected corridors |
| Branching factor | Mean available forward choices on critical path | 1.2–1.8 | < 1.0 or > 2.5 | Add reward branch or prune maze fanout |
| Backtrack ratio | Reused path length / critical path length | 0.10–0.35 | > 0.50 | Carve loop or add shortcut |
| Ammo margin | accessible_ammo / expected_ammo_need | 1.2–1.45 | < 1.0 | Add clip, downgrade SS, move weapon pickup earlier |
| Healing spacing | Max traversed tiles between visible heal opportunities on main route | ≤ 20 early, ≤ 28 late | > 35 | Add food/first aid visible pickup |
| Door ambush unfairness | Count of door reveals with >1 high-tier shooter in same immediate cone at ≤4 tiles | 0 early, ≤1 late | > 2 | Move, offset, or downgrade shooters |
| Secret reward share | Optional resource value / total resource value | 0.15–0.35 | > 0.50 or < 0.05 | Shift pickups between main route and secrets |
| Landmark coverage | % of major branch nodes with unique texture/shape/decor signature | ≥ 0.70 | < 0.50 | Swap texture zone, add decor anchor, reshape room |

For implementation, define a **combat zone** as a room or hall segment bounded by doors, branch points, or major bends; define a **critical-path node** as any door threshold, key pickup, lock, or branch on the shortest non-secret solution path. Those abstractions line up well with Wolf3D's room-by-room combat logic and make LOS, threat, and resource measurements stable across many generated layouts. citeturn28view2turn16view1turn16view2

## Procedural algorithms

The best architecture for an automated Wolf3D pipeline is a **generate broadly, then repair locally** workflow. Do not try to solve all balance during initial carving. Initial generation should only establish a legal topology and an intended pacing skeleton; the final logic pass should enforce solvability, budget, pacing, and fairness. That approach matches Wolf3D especially well because so many high-impact problems are **localized**: one SS too close to a door, one too-long corridor, one hidden key, one ammo-negative mutant introduction, one secret that blocks another. citeturn29view4turn38search3turn22view3

```mermaid
flowchart TD
    A[Seed + episode profile + desired floor length] --> B[Carve topology]
    B --> C[Place locks and keys]
    C --> D[Assign pacing beats]
    D --> E[Place enemies by threat budget]
    E --> F[Place visible resources]
    F --> G[Place optional secrets]
    G --> H[Run hard validation]
    H -->|fail| I[Localized repairs]
    I --> H
    H -->|pass| J[Run balance metrics]
    J -->|fail| K[Threat/resource/LOS repairs]
    K --> J
    J -->|pass| L[Export tile/object planes]
```

That pipeline is a direct fit for the classic engine's structure: tile planes, door-delimited pacing, explicit resource pickups, and AI behavior that is highly sensitive to local geometry. citeturn43view1turn28view0turn16view1

A strong baseline room/corridor generator can be written as follows:

```python
def generate_layout(seed, profile):
    rng = RNG(seed)
    grid = solid_walls(64, 64)

    # soft budgets leave headroom for repair
    budgets = {
        "areas_max": 32,
        "doors_max": 56,
        "actors_max": 120,
        "statics_max": 320,
    }

    beats = choose_floor_beats(profile.length)  # 4..8 major beats
    start_room = carve_room(grid, size=rand_rect(5, 7, 5, 7))
    exit_room  = reserve_far_room(grid, size=rand_rect(5, 7, 5, 7))

    rooms = [start_room]
    current = start_room

    for beat in beats[1:]:
        next_room = carve_room_nearby(
            grid,
            anchor=current,
            size=rand_rect(5, 11, 5, 11),
            orthogonal_only=True,
            avoid_overlap=True,
        )

        corridor = connect_rooms(
            grid,
            current,
            next_room,
            width=2 if rng.rand() < 0.75 else 1,
            max_straight=12,
            with_bend=(rng.rand() < 0.6),
        )

        if is_major_transition(current, next_room):
            place_door_at_threshold(grid, corridor)

        maybe_add_side_branch(grid, current, reward_bias=True)
        maybe_add_loop(grid, rooms, max_overlap_ratio=0.35)

        rooms.append(next_room)
        current = next_room

    connect_to_exit(grid, current, exit_room, width=2)
    place_elevator(exit_room)

    return grid, rooms, budgets
```

The post-carve geometry pass should then enforce a few non-negotiable structural edits:

```python
def repair_geometry(grid):
    widen_critical_one_tile_corridors(grid, max_share=0.25)
    split_overlong_los_segments(grid, hard_max=21, preferred_max=16)
    ensure_every_key_has_visible_access(grid)
    ensure_non_secret_solution_path(grid)
    backstop_pushwall_rewards(grid, stop_distance=2)
    reduce_area_fragmentation(grid, soft_max_areas=32)
    return grid
```

Enemy placement should be zone-based and episode-aware, rather than uniformly random:

```python
ENEMY_WEIGHT = {
    "guard": 1.0,
    "dog": 0.5,
    "officer": 1.75,
    "mutant": 2.25,
    "ss": 3.0,
}

def place_enemies(level, episode_profile):
    zones = partition_into_combat_zones(level)  # doors, bends, branches
    curve = threat_curve_for_episode(episode_profile)

    for zone in zones:
        target_threat = curve(zone.progress_0_to_1)
        roster = allowed_roster(episode_profile, zone.progress_0_to_1)

        while zone.threat < target_threat:
            enemy = weighted_pick(roster)
            tile = choose_enemy_tile(
                zone,
                require_path_to_player_space=True,
                avoid_spawn_los_unfairness=True,
                avoid_blocking_key_or_exit=True,
            )

            if acceptable_grouping(zone, enemy, tile):
                place_enemy(zone, enemy, tile)

    return level
```

The fairness filter for local enemy placement should be explicit:

```python
def acceptable_grouping(zone, enemy, tile):
    # never put many high-tier shooters in a fresh breach cone
    if enemy in {"ss", "officer", "mutant"}:
        if immediate_door_reveal_range(tile) <= 4:
            if count_high_tier_same_cone(zone, tile) >= 1:
                return False

    # dogs are okay as close rushers, but not in giant swarms
    if enemy == "dog" and count_in_zone(zone, "dog") >= 3 and zone.is_narrow:
        return False

    # mutants need nearby ammo or prior weapon maturity
    if enemy == "mutant" and zone.is_first_mutant_intro:
        if not visible_ammo_within(zone, tiles=10):
            return False

    return True
```

Item placement should solve a conservative resource equation, then distribute rewards with visibility rules:

```python
AMMO_COST = {
    "guard": 1.5,
    "dog": 0.25,      # change to 1.0 if knife play is not assumed
    "officer": 3.0,
    "mutant": 3.0,
    "ss": 6.0,
}

def tune_resources(level, start_profile):
    need = sum(AMMO_COST[e.type] for e in level.enemies)
    have = (
        start_profile.ammo
        + 8 * count_map_clips(level)
        + 4 * conservative_drop_count(level)
        + 25 * count_full_heals_if_counted(level)
    )

    ratio = have / max(need, 1)

    while ratio < start_profile.target_ammo_ratio:
        if can_add_visible_clip_before_spike(level):
            add_visible_clip_before_spike(level)
        elif can_move_weapon_pickup_earlier(level):
            move_weapon_pickup_earlier(level)
        else:
            downgrade_local_enemy_group(level)  # usually SS -> officer or remove one filler
        have = recompute_accessible_ammo(level, start_profile)
        ratio = have / max(need, 1)

    enforce_visible_heal_spacing(level)
    enforce_keys_on_normal_route(level)
    return level
```

Finally, difficulty tuning across a level and across episodes should be rule-based rather than purely statistical:

```python
def apply_progression(level, episode, floor_index):
    # within-floor beat curve
    place_light_opening(level, max_threat=opening_budget(episode))
    place_peak_at(level, progress_range=(0.60, 0.80))
    ensure_exit_run_is_short(level)

    # across-episode roster
    roster = {
        1: {"guard", "dog", "ss"},
        2: {"guard", "dog", "ss", "mutant"},
        3: {"guard", "dog", "ss", "officer"},
        4: {"guard", "dog", "ss", "officer", "mutant"},
        5: {"guard", "dog", "ss", "officer", "mutant"},
        6: {"guard", "dog", "ss", "officer", "mutant"},
    }[episode]

    # bias toward simpler, fairer new-type introductions
    if episode == 2 and floor_index <= 2:
        isolate_first_mutant_groups(level)
    if episode == 3 and floor_index <= 2:
        isolate_first_officer_groups(level)

    return level
```

Two implementation recommendations are especially important in practice. First, test every generated floor under **multiple loadout profiles**, not just a fresh-episode start: a weak carryover state, an average mid-episode state, and a strong carryover state. Second, make repair operations **monotonic and local**: moving one enemy, adding one clip, widening one corridor, or converting one secret key room into a visible side room is usually better than regenerating the whole level. That preserves variety while still converging on fairness. The original game's deterministic tile logic is well-suited to this kind of repair system. citeturn42view0turn16view1turn28view2

## Pitfalls and annotated bibliography

**Common pitfalls and fixes**

| Pitfall | Why it happens in generators | Why it is bad in Wolf3D | Fast fix |
|---|---|---|---|
| Mandatory secret | Secret placement runs before progression validation | Violates original manual contract; hostiles like E2F2 are remembered for it | Move key/exit path to visible route |
| Overlong 1-tile corridors | BSP-like carving or maze bias | Creates door camping, rush traps, low dodge freedom | Widen critical ones to 2 tiles |
| Too many tiny door cells | Naive "every room gets a door" logic | Burns area/door budgets and over-fragments AI behavior | Merge cells, replace some doors with open thresholds |
| Instant SS/officer breach ambush | Enemies placed without threshold LOS checks | Feels unavoidable, especially at fresh pistol start | Offset shooter, add vestibule, downgrade group |
| Early mutant ammo starvation | Roster progression not linked to resources | Mutants are punishing if introduced before adequate ammo | Add visible clips or move weapon pickup earlier |
| Symmetric texture mush | Generator optimizes topology only | Player loses orientation in same-height orthogonal engine | Assign texture regions and landmark rooms |
| Secret-wall reward jam | Pushwalls placed without backstop | Two-tile pushwalls and bugs can block access | Put solid stop exactly two tiles behind reward |
| Deep backtracking with no loop | Key/lock system layered after carving | Revisits are dull without new context | Carve shortcut or add recontextualized return |
| Keys hidden behind same-color lock logic errors | Constraint solver places lock before visibility check | Creates literal unwinnables | Forward-simulate key collection and lock opening |
| "Tribute difficulty" everywhere | Tuning copies late originals indiscriminately | E6 extremes are memorable because they are exceptions | Reserve high-density extremes for explicit hard profile |

The strongest original examples are useful here. **Episode 2 Floor 2** is a cautionary tale about hostile early pressure and a mandatory secret; **Episode 5 Floor 7** is remembered for enemies coming through multiple doors into the same room; late Episode 6 maps show that very long or very dense floors can work, but only as deliberate extremes rather than baseline procedural output. citeturn38search3turn38search4turn38search8turn38search12

**Annotated bibliography to prioritize**

**id Software Wolf3D source release on GitHub.** This is the first source to consult for hard constraints and exact behaviors: map size, area count, actor/static/door caps, start loadout, door timing, pushwall motion, connectivity, and the tile-based chase code. For an automated pipeline, this should be treated as the canonical source of truth for vanilla assumptions. citeturn23search4turn41view0turn41view1turn41view2turn42view0

**Original Wolfenstein 3-D manual.** This is the best source for the original design contract presented to players: necessary items are not hidden, keys are on the same level, hidden passages provide extra value, and players are encouraged to fight at angles and close range. Those statements are directly actionable as generator rules. citeturn22view0turn22view1turn22view2turn22view3

**Original map references and screenshots from VGMaps and Wolfenstein Wiki floor pages.** These are the fastest way to inspect authentic room grammars, branch depth, par times, secrets, enemy counts by difficulty, and episode pacing. They are especially useful for building test corpora and archetype templates. citeturn8search0turn9view0turn9view1turn9view2turn36image1turn37image0

**TASVideos Wolfenstein 3D mechanics page.** This is the most concise source for player-weapon fire rates and damage formulas. If your final logic pass computes sightline thresholds, expected bullet sinks, or worst-case ammo margins, this source is invaluable. citeturn19view0

**Wolfenstein Wiki and The Wolf Front enemy/item references.** These are secondary/community sources, but they are useful because they summarize source-backed enemy HP, drop behavior, item values, and AI notes in a form that is much easier to operationalize than raw C files. Use them as convenience layers, not as replacements for the id source. citeturn18view0turn34view0turn34view1turn34view2turn34view3

**DieHard Wolfers Bunker and related community editor indexes.** Useful for the practical modding/tool chain: MapEdit, FloEdit, WDC, ChaosEdit, and other utilities. The Bunker also contains good historical context on random-map generators, with the important caveat that those generators still needed human personalization—exactly the lesson behind this report's "generation plus final logic pass" approach. citeturn40view2

**ChaosEdit and editor-history resources.** Useful if your pipeline should interoperate with existing community formats or if you want to compare generated layouts in a 3D-aware editor workflow. Good for validation and human review, even if not your runtime target. citeturn8search2turn8search8

**Influential analysis: Liz Ryerson's Wolf3D level reading.** Not a mechanics source, but valuable for one thing procedural systems often miss: the relationship between layout weirdness, orientation, surrealism, and memory. Read this when tuning visual readability and deciding how often to allow deliberately disorienting anomaly rooms. citeturn40view0

## Addendum (2026-07-12): room-shape patterns mined from real map corpora

The sections above already cover pacing, threat, and resource balance in depth, and the current generator (`randomwolf/generator.py`) implements most of it. What is still missing is **room silhouette variety** — the thing that most immediately reads as "hand-designed" versus "boxes connected by hallways." This addendum is the result of two independent lines of evidence gathered specifically to close that gap.

### Evidence 1: structural analysis of 254 real map files, 6277 rooms

This machine (and this repo's sibling `ecwolf/mods/installed/` directory) already had a genuine corpus of real, playable Wolf3D-family campaigns sitting locally: the id-numbered **classics** conversions (`classics_ahitler`, `classics_c2d`, `classics_second` — Spear of Destiny and its official mission packs, in native WL6 tile numbering) plus several well-regarded independent total conversions (`totengraeber`, `rtotenhaus_enh`, `wolfoverdrive`, `pthollenteufel`). All of them turn out to share ECWolf's default tile-numbering convention (walls low, doors 90–101, floor/zone codes 108–143) — the same convention this generator already writes — so a small script (parsing the same `WDC3.1`/`PWAD` container this generator's own `_wad_bytes` produces) could measure every map's actual room geometry directly, with no guessing about a mod-specific format.

Method: for every map, flood-fill door-bounded floor regions (the same "zone" definition `_assign_sound_zones` already uses) and, for every region of at least 12 floor tiles, measure its footprint against its own bounding box.

Results across 6277 rooms in 254 maps:

| Metric | Real maps | This generator (pre-addendum) |
|---|---:|---:|
| Rooms that are a perfect rectangle | 18.1% | ~100% |
| Rooms with fill ratio < 0.75 (visibly irregular silhouette) | 52.8% | ~0% |
| Rooms containing ≥1 isolated interior pillar/column | 13.8% (median 2, i.e. usually a mirrored pair) | 0% (no wall-plane pillars; only movable static props) |
| Rooms ≥25 tiles with strong bilateral symmetry (≥85% of cells mirror across one axis) | 44–46% | trivially 100% (every room is a plain rectangle) |
| Room bounding-box aspect ratio, median | 1.40 : 1 | ~1.1 : 1 (rooms are always 6–9 × 6–9) |
| Major (door-bounded) rooms per map, mean | 24.7 | ≤17 (hard cap) |
| Doors per map, mean | 27.2 | lower (no explicit floor target) |

This is a strikingly consistent signature across eight independently authored campaigns spanning 20+ years of Wolf3D mapping — official id content and fan work agree: **roughly half of all rooms are not plain rectangles**, a nontrivial minority carry a real architectural obstruction, and symmetry is common but never total. The pattern is the opposite of a coincidence; it is what a person laying out rooms by eye naturally produces, and it is exactly what a rectangle-only room carver can never produce no matter how it varies size or connectivity.

### Evidence 2: community level-design guides

Two long-standing Wolf3D mapping references (B.J. Rowan's map-design tips, first published 1994, and "From Column to Column: The Wolfenstein 3D Level Design Bible") independently converge on the same qualitative advice:

- **Five basic room constructs**: the square, the rectangle, the corner (conceals what's ahead), the T-junction (a real choice), and the intersection (used sparingly — genuinely disorienting). Most good levels combine one or two of these per area rather than repeating one shape everywhere.
- **The mirroring rule**: "if you add a column at the top of the room, add one at the bottom." Symmetry is a deliberate choice applied per-feature, not an emergent property of a rectangle.
- **"A room that is roughly symmetrical with one intentional exception feels considered and interesting"** — i.e. near-symmetry, not total symmetry, reads as authored. This matches Evidence 1's ~45% (not ~100%) symmetry rate almost exactly.
- **Room size cap**: avoid rooms larger than roughly a quarter of the playable map area; oversized rooms make the renderer's distance aliasing visible and "look wrong."
- **Alcoves and niches**: shallow bumps off a wall are the natural home for a portrait, a key, or a decoration, and they break up an otherwise flat wall run.
- **"Make every room unique"**: distinct silhouette (not just distinct wall texture) is part of how a player keeps their bearings without a map screen.
- Strict symmetry is reserved for formal/ceremonial spaces (a castle hall, an officer's room); organic spaces (caves, sewers) lean irregular. The generator doesn't model "biome," but this maps naturally onto *frequency*: not every room should get the same treatment.

### Rules adopted from this addendum

1. Roughly half of generated rooms should get **silhouette-breaking treatment**: either a corner notch (cut a small rectangular bite out of one or two corners) or a wall-flush alcove bump (extend a shallow niche outward), sized so the room's own center and a full central cross always stay open — this can never threaten connectivity by construction.
2. Sufficiently large rooms (≥7×7) occasionally (~40%) get an **interior pillar** — a single wall-plane tile, strictly interior and isolated (floor on all four sides, so it can never disconnect anything), placed either as a mirrored pair (the common case, matching "add one at the top, add one at the bottom") or, less often, a single off-center column (the "one intentional exception" pattern).
3. Room proportions should vary: keep the current squarish 6–9×6–9 default for most rooms, but let a minority roll an elongated "hall" shape (major axis 9–13, minor axis 5–7) to move the aspect-ratio distribution toward the real median of ~1.4:1 instead of ~1.1:1.
4. None of this is copied from any specific map — no coordinates, dimensions, or layouts from any analyzed file were transcribed. Only the aggregate statistical pattern and the community guidance principles were carried into the generator's rules.

### Rules adopted in the second pass (same evidence base)

After the silhouette work landed, measurement of generated output against the corpus exposed four remaining gaps, closed as follows:

5. **Structural density.** Generated maps averaged 10.7 door-bounded rooms and 15.9 doors versus the corpus means of 24.7 and 27.2. The room count target was raised (cap 17 → 24) and a small-room tier added (~30% of rooms roll a compact 4–5×4–5 side cell — real maps are full of closets and side pockets, and without them high counts don't fit the grid). Hard budget guards were added at the same time: more than 56 doors or more than 36 sound-zone components now fails the floor into the existing retry loop instead of silently degrading (the zone overflow previously wrapped ids modulo 36, which could give two distant rooms the same zone so one gunshot woke both).
6. **Within-floor pacing.** Enemy placement was uniform by depth (distance-from-start quantiles ~0.20/0.28/0.50/0.72/0.89 — i.e. flat). Hand-made floors open light and climax late. Room enemy budgets are now weighted by normalized BFS depth from the spawn: ~0.4× in the opening fifth, ramping to ~1.5× across the 0.6–0.85 depth band, easing off near the exit, with the exit room itself always light; officer/SS picks are additionally suppressed in the opening and boosted in the climax band, and a recovery pickup is guaranteed just past the climax. Measured after: quantiles 0.28/0.43/0.61/0.74/0.84 with 44% of enemies in the climax band and only 5% in the opening fifth.
7. **Ambient patrols.** Real maps use patrolling actors as readable pressure and life; the generator placed only standing ones. Per ECWolf's base translator (`xlat/wolf3d.txt`), every family's patrol variant is its standing thing-code +4 at every skill tier, and a patroller that walks into a wall simply halts (benign worst case). ~25% of guard and dog placements now use the patrol variant, faced toward an open floor neighbor; officers/SS stay standing (fast walkers wandering into door lanes read as chaos, not design).
8. **Sightline cap.** Straight unobstructed floor runs longer than 16 tiles are now broken near their midpoint with a single isolated pillar where legal (main report: routine fights at 3–12 tiles, >18 needs intent, >21 never). Each candidate pillar is committed only after a full reachability re-check confirms nothing but the pillar cell itself left the reachable set — a plain "floor on all four flanks" test is *not* sufficient, because at a narrow crossing whose diagonals are solid the center tile is an articulation point and walling it would split the map. Runs that admit no legal pillar are left alone rather than forced.
