# Handoff: encounter contracts uncap patrol planning

One failing test. The cause is located and the exact lines are named below; what
remains is choosing a bound and verifying it.

```
FAIL tests/test_generator.py::GeneratorTests::test_room_encounters_do_not_huddle_their_actors
AssertionError: 0.6224489795918368 not less than or equal to 0.6
```

Everything else passes. `main` is green at the released `v2.1.0`; this is
uncommitted work on top of it.

## The bug

`infiniwolf/encounters.py` around **lines 690–747**, inside `_place_population`.

Patrol planning has a floor-wide budget, `patrol_target`, derived from
`patrol_chance` and the estimated actor count. The loop that assigns patrol
routes stops once that target is reached — **except** for rooms carrying a
`"patrolled"` encounter contract, which are exempted from the check in two
places:

```python
contracted_patrol_rooms = {index for index, intent in contract_intents.items()
                           if intent == "patrolled"}          # line 690

for ridx in patrol_rooms:
    if (ridx not in contracted_patrol_rooms                    # line ~725
            and sum(len(r) for r in planned_patrols.values()) >= patrol_target):
        break
    ...
        if (ridx not in contracted_patrol_rooms                # line ~745
                and sum(...) >= patrol_target):
            break
```

The exemption is unbounded. Every contracted room gets a patrol no matter how
far past `patrol_target` the floor already is. On the failing case that yields
**15 patrol rooms on one floor**, which is more than the programs own — a floor
allocates one primary set piece of 3–5 rooms plus one or two secondaries, so
roughly 5–8 rooms should carry any contracted intent at all.

Patrol routes reserve cells and place turn-point markers, so over-planning them
both **displaces actors into fewer legal cells** (108 → 98) and **clusters what
remains** (adjacency 0.546 → 0.622).

## Evidence

Measured against a worktree at the released commit `124dc98`:

| tree | actors | adjacency |
|---|---|---|
| released `124dc98` | 108 | 0.546 ✓ |
| current | 98 | 0.622 ✗ |
| current, `_encounter_contracts_by_room` stubbed to `return {}` | 108 | **0.546** ✓ |

The third row isolates it conclusively: disabling the contract consumer restores
the released numbers exactly.

Reproduce the metric directly:

```python
from infiniwolf.config import CampaignConfig, Intensity
from infiniwolf import generator as G
from infiniwolf.wl6 import GRID, BOSSES
for a in range(50):
    try: L = G.generate_map(CampaignConfig(seed=600, guard_density=Intensity.HIGH), 8, a); break
    except ValueError: continue
act = {(i % GRID, i // GRID) for i, t in enumerate(L.things)
       if t in G.ENEMY_CODES and t not in BOSSES}
touch = sum(any((x+dx, y+dy) in act for dx in (-1,0,1) for dy in (-1,0,1) if (dx,dy) != (0,0))
            for x, y in act)
print(len(act), touch / len(act))
import collections; print(collections.Counter(e.template for e in L.encounters).most_common(4))
```

Current output: `98 0.6224…` and `[('patrol', 15), ('staggered-flank', 9), ('strongpoint', 3), ('visible-sentry', 2)]`.
Target: patrol count in single figures, adjacency ≤ 0.60, actors back near 108.

## Suggested fix

Bound the exemption rather than remove it. A contracted room should be able to
*jump the queue* for a patrol, not *ignore the budget*. Options, cheapest first:

1. Cap the overshoot — allow contracted rooms to exceed `patrol_target` by a
   small fixed margin, then apply the same `break`.
2. Cap the contracted set — take at most N `"patrolled"` rooms (N ≈ 3), chosen
   deterministically, e.g. the deepest or those on the critical route.
3. Fold contracted rooms into `patrol_rooms` **ordering** instead of exempting
   them: sort them first, keep the budget check unconditional. This honours the
   contract wherever the budget allows and drops it when it does not, which
   matches the advisory rule every other contract follows.

Option 3 is the most consistent with the design — the contract layer's stated
invariant is that an unhonoured contract degrades silently rather than
overriding a budget.

## What did NOT work

Three attempts were made and reverted. Do not repeat them:

- **Gating by per-room actor budget** (`budget > 3` → skip forcing). Never fires;
  the per-room budget is ≤ 3 even at HIGH density.
- **Capping contract application at 8 rooms** at the template-selection site
  (~line 890). No effect — by then the patrols are already planned. This is the
  key insight: the damage happens in *planning* (line 690–747), not in the
  later template pick.
- **Gating the multi-room `sequence_templates` override**. Unrelated; changed
  nothing.

## Verify the fix

```
python3 tools/check.py --fast
python3 -m pytest tests/test_generator.py::GeneratorTests::test_room_encounters_do_not_huddle_their_actors -q
python3 -m pytest tests/test_patrol.py tests/test_quality.py -q
```

Then confirm the contract is still doing its job — it was measured at **334/335
(99.7%)** honoured before this bound, and should stay high:

```python
from infiniwolf.model import SET_PIECE_CONTRACTS
# for each room, read its setpiece:<family>:<role> tag from level.motif_rooms,
# look up the encounter intent, and check: "light" => room has no encounter,
# everything else => room has one.
```

`patrolled` is the intent that will legitimately drop; the other five should
remain at or near 100%. A honour rate that stays at 99.7% means the bound is not
biting and the huddle will still be there.

Finally, re-record fingerprints (`python3 tools/fingerprint.py --record`) — any
change here shifts generated output — and run the full suite, which is
~55 minutes and is the project's release gate:

```
python3 -m unittest discover -s tests
```

## Context

This is the last open item from `docs/codexsuggestions.md`, whose status header
is current. Ten of twelve recommendations are implemented, two are partial, and
Stage 4 (human evaluation) was removed by project decision. The encounter
contract is one of four consumers built for recommendation 3; the other three
(rewards 89.8%, landmarks 32.6%, visibility 16.2%) are unaffected by this bug.

One fix already landed in this same session and is committed: contract reward
ammo was excluded from the floor's supply accounting, so the route-ammo pass
placed its full quota on top and floor 5 came out at 0.50 supply against a 0.45
ceiling. `infiniwolf/pickups.py` now counts it.
