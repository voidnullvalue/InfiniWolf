# Roadmap: Human-Quality InfiniWolf Maps

> **Status as of 2.1.0.** This is a live roadmap, not a wishlist and not a record of
> completed work. Three of its twelve recommendations have shipped, three are partial,
> and six are untouched — including the two the document itself ranks highest.
>
> | # | Recommendation | Status |
> |---|----------------|--------|
> | 1 | Whole-map `QualityReport` | **Done** (2.1.0) |
> | 2 | Corpus analysis beyond decoration | **Done** (2.1.0) |
> | 3 | Concept-first planner | **Partial** — `SetPiecePlan` ships; contracts omitted |
> | 4 | Vignettes before geometry | **Partial** — request side only; geometry ignores it |
> | 5 | Spatial scale and hierarchy | **Partial** — anchor reserved early; no sector packing |
> | 6 | Staged beam search | Outstanding |
> | 7 | Multi-room encounter grammars | Outstanding |
> | 8 | Simulated player experience | Outstanding |
> | 9 | Pacing as explicit beats | Outstanding |
> | 10 | Landmark usefulness scoring | Outstanding |
> | 11 | Secrets as authored deductions | Outstanding |
> | 12 | Room-sequence composition | Outstanding |
>
> **One correction the document could not have known.** §2 and §5 reason from a corpus
> that is 227 fan maps, and conclude generated floors carry ~30% less usable space.
> Decoding id's own 60 maps from `GAMEMAPS.WL6` (`tools/gamemaps.py`, added in 2.1.0)
> showed the generator was already id-sized — 1120 walkable cells against id's 1098.
> `totengraeber` and `wolfoverdrive` alone are 44% of the fan corpus and both build
> large. The real defect was **distribution**, not scale: id spends ~1100 cells on 15
> rooms with a 514-cell largest; the generator spent them on 21 smaller, squarer ones.
> So §5's "do not solve this by uniformly enlarging every room" was right, and its
> premise that the floor is short of space was not. Measure against
> `structure_stats.py --id-corpus` before acting on any area figure quoted below.
>
> **Where the honest gaps are.** `QualityReport.encounter_quality` and
> `.pacing_quality` are wired but literally `UNMEASURED = 0.0` — they are #7 and #9,
> stubbed deliberately rather than faked. And §4's inversion is half-built: planning
> can express a vignette's adjacency request, but `_place_planned_rooms` never reads
> one, so nothing yet honours it.

## Assessment

InfiniWolf is no longer limited by basic procedural-generation competence. The current system already has:

- Campaign-level theme, circulation, progression, and lock scheduling.
- Independent deterministic RNG streams.
- Semantic room identities shared by architecture, encounters, pickups, and decoration.
- Contextual encounter templates rather than actor scatter.
- Corpus-calibrated decoration and lighting.
- Hard validation plus candidate selection.

The main remaining gap is **authorial intent at the whole-map level**. InfiniWolf is good at producing valid, coherent rooms. Human mappers design memorable situations, spatial revelations, tactical sequences, and recognizable places.

This assessment focuses on the current `main` implementation, its design documentation, generation policy, and existing corpus measurements. It is a code and generation-policy assessment rather than a blind ECWolf playtest of a large seed sample.

## The biggest current limitation: candidate selection

The `thorough` setting evaluates only eight candidates per floor. A candidate is primarily preferred because it has fewer critique flags; among equally clean candidates, selection rewards contrast with earlier maps, concept diversity, density targets, and other variation measures.

That is not equivalent to choosing the most human-designed map.

The current critique checks useful defects such as:

- No loop.
- Corridor-heavy structure.
- Repetitive encounters.
- Flat room-size rhythm.
- Unrewarded dead ends.
- Shape and concept monotony.

Several checks are explicitly regression tripwires that almost never fire. A map with no flags is therefore merely free of known measurable defects; it is not necessarily compelling.

Meanwhile, `_candidate_score` strongly rewards difference from previous maps, actor and object density targets, concept variety, and layout-signature distance. It has almost no direct measurement of combat quality, navigational clarity, dramatic staging, spatial composition, or player experience.

**This should be the first major system changed.**

# Highest-impact recommendations

## 1. Introduce a whole-map `QualityReport` — **DONE (2.1.0)**

> Shipped in `infiniwolf/quality.py`. Selection is lexicographic and campaign contrast is
> demoted to the final tiebreaker, as recommended. Six flags that provably never fire were
> split out as regression-only tripwires. `encounter_quality` and `pacing_quality` remain
> `UNMEASURED` stubs — they are items 7 and 9 below.

Keep hard validation exactly separate, but replace the mostly binary soft critique with a structured report:

```python
@dataclass(frozen=True)
class QualityReport:
    severe_defects: tuple[str, ...]
    spatial_composition: float
    route_quality: float
    navigational_legibility: float
    encounter_quality: float
    pacing_quality: float
    secret_quality: float
    landmark_quality: float
    corpus_similarity: float
    campaign_contrast: float
```

Selection should become lexicographic:

1. Hard validity.
2. No severe experiential defects.
3. Human-map structural similarity.
4. Gameplay and pacing quality.
5. Aesthetic composition.
6. Campaign contrast as the final tiebreaker.

At present, contrast receives too much authority. A novel mediocre map should not outrank a less unusual excellent map.

### Spatial metrics worth adding

Measure:

- Walkable floor area and occupied bounding-box ratio.
- Room-area distribution and spatial hierarchy.
- Number and usefulness of alternate routes.
- Loop detour ratio: whether a loop creates a meaningful choice rather than two nearly equivalent hallways.
- Door and junction degree distributions.
- Critical-route turn rhythm.
- Branch depth and branch payoff.
- District transition strength.
- Ratio of connective space to destination space.
- Large-space placement along the route.
- Repeated room dimensions and repeated doorway arrangements.
- Map silhouette and mass distribution.
- Amount of unused enclosed rock inside the occupied bounding box.

These should be calibrated against the authored corpus, not assigned arbitrary ideals.

## 2. Expand corpus analysis beyond decoration — **DONE (2.1.0)**

> `tools/structure_stats.py`, `tools/corpus_io.py` and `tools/gamemaps.py`. The mode this
> section assumed already worked (`inspect_map --compare`) had in fact been globbing loose
> `*.wad` while the corpus ships inside `.pk3`, so it was measuring zero maps. See the
> corpus correction in the status header before trusting any area figure here.

The decoration system is already one of InfiniWolf's strongest components. Internal measurements report nearly identical decoration density:

- Authored: `0.134` decorations per floor cell.
- Generated: `0.135` decorations per floor cell.
- Clustering and flag placement are also at or beyond authored targets.

More decoration work is therefore unlikely to produce human parity.

Mine the 207-map corpus for:

- Room and connected-area size distributions.
- Map occupied area.
- Door placement and offset distributions.
- Junction types.
- Route lengths and bends.
- Graph motifs.
- Loops and shortcuts.
- Landmark placement relative to entrances.
- Enemy visibility on room entry.
- Enemy distance and angular spread.
- Patrol-route lengths.
- Sound-zone sizes and adjacency.
- Pickup timing relative to damage and ammunition demand.
- Secret placement, hints, and detour cost.
- Sequences such as narrow → large → narrow, calm → combat → reward, and reveal → objective → shortcut.

The existing corpus work demonstrates that empirical analysis is effective. Apply the same discipline to layout and gameplay that was applied to props.

## 3. Make the planner concept-first rather than graph-first — **PARTIAL (2.1.0)**

> `SetPiecePlan` exists and `planning.py` reserves one primary 3–5 room program plus one or
> two secondaries before any filler, so a floor now reads as
> `processing-desk → guardroom → cell-block`. The `visibility_contracts`,
> `encounter_contract`, `reward_contract` and `landmark_contract` fields proposed below were
> deliberately **omitted** rather than stubbed: nothing downstream can consume them until
> geometry integration lands.

The current planner creates a progression spine, corridor nodes, generic motifs, and filler branches. Filler rooms are attached until the room target is reached, usually as a closet, standard room, or hall.

This is competent graph generation, but human maps usually begin with ideas such as:

- A checkpoint controlling access to an administrative wing.
- A barracks connected to a mess hall and armory.
- A prison-processing area overlooking cell blocks.
- A ceremonial hall revealing a later destination.
- A storage route wrapping around an inaccessible machinery bay.
- A crypt whose reliquary is hinted from an earlier ossuary.
- A defensive position that can be approached by a dangerous direct route or a longer flank.

These are **space programs**, not themes applied to generic rooms afterward.

Add something like:

```python
@dataclass(frozen=True)
class SetPiecePlan:
    family: str
    room_roles: tuple[str, ...]
    required_edges: tuple[tuple[int, int], ...]
    entry_role: str
    exit_role: str
    required_geometry: tuple[str, ...]
    visibility_contracts: tuple[VisibilityContract, ...]
    encounter_contract: EncounterContract
    reward_contract: RewardContract
    landmark_contract: LandmarkContract
```

Each normal floor should receive:

- One primary three-to-six-room set piece.
- One or two secondary one-to-three-room compositions.
- Ordinary connective and utility space only after those are reserved.

This would make the floor feel like a designed location rather than a high-quality sequence of independently sensible rooms.

## 4. Move vignettes before geometry — **PARTIAL (2.1.0)**

> Planning can now express a vignette's adjacency request, and `_FAMILIES` gained
> request-side entries. The inversion is **not** complete: `_place_planned_rooms` never reads
> a request, so geometry still does not exist *because of* a scenario. Separately, a vignette
> that cannot be realized now degrades to no vignette instead of failing the whole map —
> that alone cut rejected generation attempts from 46% to 31%.

The current vignette layer is promising but too limited. It contains six two-room families, produces at most one vignette, and only attempts one on roughly one-third of ordinary floors. It selects from room identities and adjacencies that already happen to exist.

That means it coordinates decorations, encounters, and pickups after the important architectural decisions have already been made.

Promote vignettes into the planning phase. A vignette should be able to request:

- Specific adjacency.
- An overlook.
- A long or interrupted sightline.
- A rear entrance.
- An asymmetric room pair.
- A shared wall or shared inaccessible void.
- A patrol route through both spaces.
- A locked objective and later return path.
- A visible but initially unreachable reward.
- A shortcut that opens after the encounter.

The geometry should exist **because of the scenario**, not merely accommodate a scenario afterward.

## 5. Increase spatial scale and hierarchy — **PARTIAL (2.1.0)**

> The anchor's footprint is now reserved *before* the spine grows, which lifted the largest
> room from 222 to 418 cells and biggest-room-share p90 from 0.19 to 0.33. Room proportions
> were the bigger win: both corpora report a median aspect of 1.40 and the generator sat at
> 1.22 because `standard` and `anchor` drew both axes from one range. Still absent:
> **sector-first packing** and the **compaction pass**. Rooms currently *plan* to ~1638
> walkable cells but only ~1150 realize, because the greedy spine-growth placer cannot pack
> them — that gap, not the size grammar, is what caps the large-space tail.

The repository's own measurements show generated maps averaging about 1,086 walkable floor cells versus about 1,554 for the authored maps. That is roughly 30 percent less usable space. The remaining decoration variety and fixture-count gaps are explicitly attributed to this structural difference.

Do not solve this by uniformly enlarging every room. Instead:

- Add two or three major spatial masses per floor.
- Use larger anchor and secondary-anchor spaces.
- Build suites around small shared corridors.
- Allow rooms to wrap inaccessible voids.
- Use broad circulation spaces with narrow local approaches.
- Introduce deliberate negative space.
- Pack districts as sectors before placing individual rooms.
- Compact the layout after initial placement to reclaim unusable rock.
- Permit limited shared-wall arrangements where theming and doorway ownership remain safe.

The current conservative room shell and axis-aligned rectangle model simplify correctness, but they also reduce usable map area and suppress spatial relationships.

A sector-first layout algorithm would likely outperform simply increasing the 24-room cap.

## 6. Use staged search rather than fully generating only eight candidates — **OUTSTANDING**

> Not started. Still eight fully-realized candidates per floor. Note the interaction with
> item 5: ~31% of generation attempts are still rejected outright, and a beam search that
> scored cheap abstract plans first would avoid paying for geometry on plans that cannot work.

Generating 32 complete decorated maps per floor would be expensive. Instead, use a beam-search pipeline.

### Stage A: abstract planning

Generate perhaps 48–64 inexpensive `FloorPlan` candidates.

Score:

- Graph structure.
- Set-piece realization.
- Progression length.
- Branch utility.
- Planned area hierarchy.
- District organization.
- Predicted packing pressure.

Keep the best 12.

### Stage B: geometry

Realize those 12.

Score:

- Occupied area.
- Route quality.
- Sightlines.
- Door geometry.
- Landmark opportunities.
- Actual room hierarchy.
- Spatial composition.

Keep the best four.

### Stage C: full realization

Only then run progression, semantics, encounters, pickups, and decoration.

Evaluate the final four with gameplay and visual metrics, then choose one.

This widens the design search substantially without multiplying the most expensive passes by eight.

Determinism can be preserved by assigning candidate and mutation streams explicit seed names.

# Gameplay-specific improvements

## 7. Add multi-room encounter grammars — **OUTSTANDING**

> Not started. `QualityReport.encounter_quality` is an `UNMEASURED` stub awaiting this.

The encounter system is currently room-owned. It has good local behavior—entry-aware placement, visible sentries, staggered flanks, strongpoints, blind ambushes, objective guards, patrols, and rare galleries—but most fights remain compositions inside one room.

Template selection ultimately draws from a relatively small set of room-local patterns.

Add encounter sequences spanning rooms and sound zones:

- **Checkpoint response:** a visible sentry alerts a lateral patrol.
- **Elastic defense:** front guards lead the player toward a deeper strongpoint.
- **Crossfire loop:** the direct entrance exposes the player; an optional loop provides a flank.
- **Layered breach:** the first room is lightly guarded, while the second contains the actual resistance.
- **Overlook pressure:** shootable enemies occupy an inaccessible or alternate-access position.
- **Patrol intersection:** a patrol crosses a room shortly after the expected player arrival.
- **Objective defense:** key visibility, guard positioning, and retreat path form one composition.
- **False calm:** readable supply or empty space precedes a controlled ambush.
- **Counterflow:** enemies occupy the return route through sound-zone and path arrangement rather than dynamic spawning.

Wolfenstein's sound propagation should become an authoring instrument. Design intentional alert chains rather than merely preventing zones from becoming too large.

## 8. Simulate approximate player experience — **OUTSTANDING**

> Not started.

Current pacing mainly depends on BFS depth and a fixed density multiplier. Visibility is used locally for placement.

Add deterministic lightweight simulation for three player profiles:

- Direct-route player.
- Cautious corner-checking player.
- Explorer who enters optional branches.

For each, estimate:

- Enemies visible immediately on entry.
- Maximum simultaneous attackers.
- Angular spread of threats.
- Distance to the nearest usable retreat.
- Time spent in long exposed lanes.
- Available lateral movement.
- Choke congestion.
- Likely sound-zone activation.
- Expected ammunition expenditure before resupply.
- Expected health pressure.
- Backtracking distance.
- Time between major combat beats.

This need not be a perfect Wolf3D bot. Even a tile-based exposure and resource model would distinguish dramatically different maps that currently receive similar scores.

## 9. Design pacing as a sequence of beats — **OUTSTANDING**

> Not started. `QualityReport.pacing_quality` is an `UNMEASURED` stub awaiting this.

The current depth curve provides a sensible overall ramp, peak, and recovery. Human pacing is not merely a smooth function of distance.

Plan explicit beats:

```text
orientation
→ modest resistance
→ exploration choice
→ memorable encounter
→ recovery
→ objective pressure
→ shortcut/recontextualization
→ climax
→ decompression
```

Not every floor needs this exact pattern. The point is to schedule contrasting beats deliberately.

Track sequences such as:

- Combat intensity.
- Room scale.
- Route complexity.
- Visibility.
- Resource abundance.
- Decoration density.
- Architectural formality.
- Sound-zone activation risk.

Then reject sequences with repeated medium-intensity rooms, even when every individual room is acceptable.

# Navigation, secrets, and visual composition

## 10. Score landmark usefulness, not landmark existence — **OUTSTANDING**

> Not started. `landmark_hierarchy_broken` remains a count-of-primaries tripwire, which is
> exactly the "existence, not usefulness" check this section criticises.

The current system identifies a primary landmark and reserves authored sightlines. That is a good foundation. The quality check mainly verifies that exactly one primary landmark exists.

A useful landmark should:

- Be seen from multiple meaningful positions.
- Help identify a district.
- Reappear after a loop or shortcut.
- Orient the player at a major choice point.
- Have a distinct silhouette, lighting treatment, or material language.
- Remain recognizable from the reverse direction.

Build a visibility graph from door thresholds and choice points to landmarks. Reward maps where the primary landmark provides repeated spatial orientation rather than a single attractive view.

Secondary landmarks should identify district transitions and branch destinations.

## 11. Turn secrets into authored deductions — **OUTSTANDING**

> Not started.

Current secret pockets have bespoke shapes, containment, rewards, and material-aware hints. Structurally they are strong.

The next improvement is epistemic: **why would a player suspect the secret?**

Add secret grammars:

- A repeated wall composition with one deliberate anomaly.
- A room visible from two sides that appears spatially incomplete.
- A landmark whose symmetry implies a hidden counterpart.
- A glimpse of inaccessible treasure.
- A patrol or guard facing an otherwise unimportant wall.
- A sound-zone or room-shape clue.
- A map or sign composition pointing toward a district.
- An optional shortcut that is valuable even after its reward is collected.

Score:

- Hint salience.
- Number of misleading false positives.
- Detour cost.
- Reward proportionality.
- Discovery timing.
- Whether the secret teaches a visual language used elsewhere in the campaign.

Guarantee at least one fairly deducible secret on secret-rich settings rather than treating every secret as equally obscure.

## 12. Use composition rules at the room-sequence level — **OUTSTANDING**

> Not started.

The decoration system already composes individual rooms intelligently. Extend composition across adjacent rooms:

- Repetition followed by deliberate interruption.
- Lighting-family transitions at district boundaries.
- Increasing monumentality toward a landmark.
- Damage gradients rather than independent damaged-room decisions.
- Props that imply movement of goods between storage, workshop, and checkpoint.
- Bodies, blood, or disorder that imply an event across multiple spaces.
- Repeated architectural elements that culminate in a major composition.
- Sightlines linking an earlier room to a later destination.

This creates environmental storytelling without requiring text or new assets.

# What not to prioritize

These changes are unlikely to deliver major gains by themselves:

- More random room shapes.
- More decoration density.
- More isolated rare motifs.
- More enemy types per room.
- More binary critique flags.
- More floor variants that only alter weights.
- More one-off special features scheduled once per campaign.

Those increase variety, but InfiniWolf already has substantial variety. Human-made quality comes from **relationships between elements**.

# Recommended implementation order

## Stage 1: measure real quality

1. Add full structural, navigation, exposure, and route metrics for authored and generated maps.
2. Split `quality.py` into live quality diagnostics and regression-only tripwires.
3. Add candidate-selection traces showing why each candidate won.
4. Generate top-down contact sheets with route, landmark, encounter, sound-zone, and sightline overlays.

## Stage 2: change how maps are conceived

1. Add `SetPiecePlan`.
2. Promote vignettes into planning.
3. Require one primary and one or two secondary set pieces per ordinary floor.
4. Replace generic filler with concept-compatible district completion.
5. Increase occupied area through sector-first packing and compaction.

## Stage 3: change how maps are selected

1. Generate dozens of abstract plans cheaply.
2. Beam-search through planning, geometry, and final realization.
3. Rank human-likeness and experience before campaign contrast.
4. Introduce local deterministic mutations: move a room, reroute an edge, resize an anchor, alter a doorway, or replace an encounter.

## Stage 4: close the human-evaluation loop

Create a blind pairwise-rating tool showing either maps or short play sessions. Ask testers:

- Which map feels more deliberately designed?
- Which is easier to navigate without being trivial?
- Which has better combat?
- Which has more memorable spaces?
- Which would you replay?

Collect the seed, current metrics, and preference. Fit a small interpretable preference model—logistic regression or Bradley–Terry is sufficient—to determine which metrics actually predict human judgment.

That feedback loop is the realistic path from statistically similar to human maps to players being unable to reliably identify which maps were generated.

# The single best next project — **HALF DONE**

> The set-piece half landed in 2.1.0; the staged-search half has not started, and the
> pipeline below is still the target rather than the implementation.
>
> **What is now true.** `campaign identity → primary and secondary set-piece programs`
> works: every ordinary floor reserves a 3–5 room primary program and one or two
> secondaries before any generic filler is allocated. `full quality report → campaign
> contrast tiebreaker` also works, in that order.
>
> **What is still missing, and the order has changed.** `many abstract floor plans →
> structural corpus scoring → realize best plans` does not exist — generation is still
> eight fully-realized candidates. Two things learned since make this *more* worth doing,
> not less:
>
> 1. **Roughly 31% of generation attempts are still rejected outright**, each discarding
>    planning, placement, routing and population. The largest single cause is the exit
>    elevator's 5×5 rock requirement, which cannot be evaluated until the whole floor
>    exists. A staged search that scored cheap abstract plans first would stop paying
>    geometry costs for plans that were never going to validate.
> 2. **Backtracking may matter more than beam width.** Most late failures have a local
>    repair — pick a different exit host, nudge one room — but today they restart the
>    entire floor. Checkpointing state before each risky stage and retrying just that
>    stage would recover most of that waste, and is a smaller change than a full
>    beam-search pipeline.
>
> The one prerequisite the document does not mention: the greedy spine-growth placer in
> `_place_planned_rooms` cannot pack the rooms it is already given — planned area is
> ~1638 walkable cells against ~1150 realized. Sector-first packing (item 5) should land
> before, or alongside, staged search; otherwise better plans will still fail to realize.

Build **concept-first set pieces plus staged candidate search**.

The revised pipeline should be:

```text
campaign identity
→ primary and secondary set-piece programs
→ many abstract floor plans
→ structural corpus scoring
→ realize best plans
→ visibility/navigation/gameplay scoring
→ populate and decorate finalists
→ full quality report
→ campaign contrast tiebreaker
```

InfiniWolf's existing semantic, encounter, decoration, validation, and deterministic-stream systems are sufficiently mature to support this. The missing layer is an architect/director that decides what the player should experience before the individual systems begin filling the map.
