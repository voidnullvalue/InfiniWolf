# Round 2: Human-Authored Map Feel — status + handoff plan

Working doc for the in-progress round 2 ("make maps look human-built structurally").
Written for agent handoff: each phase below is a self-contained implementation brief.
Delete this file when round 2 ships.

## Protocol (applies to every phase)

- All work in `infiniwolf/generator.py` (single-file architecture), tests in `tests/test_generator.py` / `tests/test_topology_regression.py`, docs in `DESIGN.md`.
- Every new parameter keyword-with-default so direct-call synthetic tests keep passing.
- All randomness through the per-floor `rng`; determinism = byte-identical PK3 per seed. Fixed-seed outputs may reroll (sanctioned); `test_variant_sequence_fixed_seed_regression` must NOT change.
- Never run the full local suite serially; run the targeted tests named per phase. Seed sweeps must use `_generate_with_retries` (attempt-0 failures ~0.4% are legitimate).
- Verification per layout phase: targeted unit tests, then `tests.test_topology_regression` + `tests.test_patrol`, then `tools/fuzz.py --seeds 40` (background, parallel).
- Budgets that must hold: doors ≤56 per map, statics ≤320 (enforced by headroom counter in `_place_decorations`), longest straight run ≤21.

## Status

| Phase | State |
|---|---|
| 0 baseline metrics | DONE — mean 3.32 bends/corridor, tortuosity max 9.1, room-mask symmetry 69.5%, drop rate ~0.01/floor |
| 1 corridor straightening | IMPLEMENTED (turn-penalized Dijkstra + portal ranking `(est_bends, distance, centering)` in `_carve_connection`; `_path_bends` helper; goal-state min-total fix). Bends now 1.95 mean. Unit + campaign tests green. ONE topology property test failed in the full-suite run — identity unknown (output was truncated); four parallel single-test runs are identifying it. Do not commit phase 1 until triaged. |
| 2 room snapping | IMPLEMENTED, UNTESTED — `_snap_offsets` ladder (center → edge-flush ×2 → small jitter, first 20 of 60 attempts) is in the working tree, but the implementing agent did NOT write the two tests it was asked for. NEXT STEP: add `test_snap_offsets_prefers_center_then_flush_edges` (candidate order/dedupe unit test) and `test_adjacent_rooms_usually_align_or_flush` (over ~8 `_generate_with_retries` floors, ≥50% of room pairs with a 1-3 tile gap on one axis share a centerline or flush edge), review the diff, then land with phase 1. |
| 3 mirrored interiors | READY — brief below |
| 4 set-piece rooms | READY — brief below |
| 5 arrival staging + door-flank tune | READY — brief below |
| 6 DESIGN.md + final sweep | READY — brief below |

Landed separately on main already: floor-number mapinfo fix (`floornumber = N` per map block), hallway-pots pocket fix, themed decor-pool expansion (45 BunkBed→barracks, 40/41 cages→jail, 39 SuitOfArmor→grand, 62 Flag→guardpost, 60 EmptyWell→storage; 39/62 added to `_FRAMEABLE`).

## Phase 3 brief — mirrored notches/alcoves

`_carve_notches` (~line 629): add `mirrored_chance: float = 0.6`. Rooms passing the existing gate with area ≥25 and a mirrored roll bite corner PAIRS with identical dims across one axis (~75% of mirrored rolls) or all four corners (~25%). Existing `(dim-2)//2` caps stay (center row/column remains floor → connectivity argument unchanged). Non-mirrored path stays verbatim.

`_carve_alcoves` (~line 649): add `mirrored_chance: float = 0.35`. On a mirrored roll pick an opposite-side pair (N+S or E+W), carve BOTH centered bumps with same span/depth only if both pass the existing bounds/overlap checks (append both to `established`); else fall back to the single-alcove loop.

Tests: forced `mirrored_chance=1.0` synthetic rooms → bite mask mirror-symmetric across an axis + center row/column intact; alcove pair same-sized opposite centered bumps or clean single fallback. Names: `test_mirrored_notches_produce_symmetric_bites`, `test_mirrored_alcoves_carve_matching_opposite_bumps`.

## Phase 4 brief — signature set-piece rooms (floors 1–8)

New module tables near the decor pools:
- `_SET_PIECE_BUILDERS: dict[str, Callable[[Room], list[tuple[list[tuple[int,int]], int, bool]]]]` — (cells, item, blocking) pieces, all mirror-symmetric about `room.center`, degrade per-piece, fit rooms ≥7×7 by dropping outer pieces.
- Templates: **mess-hall** (25 TableWithChairs rows ±1 off center line + 27 Chandeliers on it + one 68 Stove + 33 Sink pair on the kitchen short wall + 38 KitchenStuff open between them); **shrine-crypt** (30 pillar pairs flanking axis, 35 Vase center, 32 skeletons at mirrored corners, 61 Blood); **courtyard** (59 Well center, 31 plants at 4 inset corners, 67 Pots at wall mids); **armory-depot** (69 Spears racks one long wall, 58/24 barrels other, 62 Flag pair, 46 Basket open); **war-room** (36 BareTable center block, 62 Flag pair, 39 SuitOfArmor mirrored corners, 27 Chandelier); **barracks-dorm** (45 BunkBed rows both long walls, 33 Sink on short wall, 37 CeilingLight center line).
- `_VARIANT_SET_PIECES`: garrison→(armory-depot, war-room); catacombs→(shrine-crypt,); grand-halls→(courtyard, war-room); storehouse→(armory-depot, courtyard); quarters→(mess-hall, barracks-dorm). No stronghold/vault entries (floors 9/10 excluded).
- `_select_set_piece(rng, rooms, roles, specs, jail_rooms, exit_room, variant_name)`: eligible = area ≥48, not index 0, not exit host, not jail; prefer anchor tier else largest; rng.choice of variant's templates; None if none.
- `generate_map`: call right before `_place_decorations`, pass `set_piece=` kwarg. In `_place_decorations`: matching room applies pieces via `_try_place`/`_place_open` then skips all other room treatments. Record `GeneratedMap.set_piece: str = ""` + manifest floor key `"set_piece"`.

Tests: `test_set_piece_room_is_mirror_symmetric` (12×12 forced mess-hall → blocking cells symmetric across long axis, ≥4 things), `test_set_piece_degrades_gracefully_when_cells_occupied` (pre-filled treasure untouched, no exception), `test_floors_record_set_piece_and_boss_floors_do_not`, manifest key test.

## Phase 5 brief — arrival staging + door-flank tune

1. **Elevator lobby**: `_place_decorations` gains `exit_stand=None`. Derive shaft dir dx from which of `(sx±1, sy)` is tile 21; elevator door at `(sx-2dx, sy)`; approach `(sx-3dx, sy)`. In the room containing the approach: add approach + one further cell to `keep_clear`, ALWAYS attempt a matched `frame_pool` pair on the two cells flanking the approach (no roll, exempt from `pair_budget`), place 37 CeilingLight on the approach via `_place_open`. Skip defensively if no tile 21 adjacent. `generate_map` passes `exit_stand=exit_stand`.
2. **Start room** (index 0): if it has door entries, one matched pair at `start ± perpendicular` of the first entry's inward axis. Exempt from budget/cap.
3. **Door-flank tune** (explicit user requirement "only occasionally"): existing doorway-flank vignette roll 0.4 → `door_flank_chance: float = 0.15`; new `important_doors: frozenset[tuple[int,int]] = frozenset()` (locked-door tiles {92,93,94,95} minus elevator door, computed in `generate_map`); sort candidate entries important-first (door cell for entry `(ex,ey)` inward `(ix,iy)` is `(ex-ix, ey-iy)`); per-floor cap of 3 flank pairs (lobby/start exempt).

Tests: `test_elevator_approach_gets_a_framed_lobby` (synthetic shaft per `test_elevator_is_usable_and_authentic` pattern), `test_start_room_gets_a_composed_opening_pair`, `test_door_flanks_are_occasional_and_capped` (≤3/floor over ~12 floors), and `test_doorway_approach_cells_never_get_blocking_decor` must stay green.

## Phase 6 brief — docs + sweep

- DESIGN.md: §3 snap ladder; §4 turn-penalized routing + portal ranking + mirrored carving; decoration section (set pieces, elevator lobby, start staging, door-frame importance + cap); §9.3 add achieved numbers (bends 3.32→~1.9, symmetry, drops); §9.5 lesson entry; §10 manifest `set_piece`.
- Final: targeted suites + `tools/fuzz.py --seeds 40` + small metric re-measure (scripts exist in the session scratchpad; re-derivable from this doc: bends = direction changes per `_carve_connection` path).
- Squash-review the whole round; commit convention: imperative summary + wrapped body; end body with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Known risks / notes

- The unidentified topology failure from the phase-1 full-suite run MUST be identified and fixed (or its seed swapped with justification) before phases 1+2 commit. Four single-test triage runs were in flight at handoff; if their outputs are gone, re-run these four individually (each ~4-8 min) — the failure is one of them: `TopologyRegressionTests.test_no_unbounded_sightlines`, `TopologyRegressionTests.test_plain_doors_have_no_floor_only_walkaround`, `AreaThemeRegressionTests.test_wall_material_balance_across_seeds`, `TopologyRegressionTests.test_door_bounded_rooms_are_not_gigantic_blobs` (plus the floor-ten trio and remaining suite tests if all four pass).
- Straighter corridors lengthen sightlines: `_break_long_sightlines` (cap 21) fires more; if door budget or retry rates degrade, lower `turn_penalty` 4→3.
- Snapping raises collision rates: watch dropped-room counts (baseline ~0.01/floor).
- `heapq` ties must stay broken by the monotone counter (determinism).
