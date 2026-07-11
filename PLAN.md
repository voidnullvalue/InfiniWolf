# Wolf3D Random Campaign Generator

## Summary

Build a cross-platform Python desktop generator that creates a complete classic-style Wolfenstein 3D episode from a reproducible seed. Each campaign contains eight normal floors, a boss floor, and a tenth secret floor. Generated PK3s contain maps and metadata only; ECWolf loads all copyrighted graphics, sounds, music, actors, and gameplay definitions from the user’s registered WL6 installation.

## Implementation Changes

- Refactor the useful WAD-writing and room-graph portions of `wolfenslop-src/build.py` into a new, clean generator core without Wolfslop textures, actors, or story content.
- Add a Tkinter GUI with an optional seed, five-step generation controls, Generate and Play actions, progress/error reporting, and a generated-campaign summary.
- Support Windows, macOS, and Linux path handling and ECWolf launching. Validate registered WL6 data but never copy it into generated packages.
- Install generation atomically into a stable `randomwolf` campaign slot, retaining the previous valid output until its replacement passes validation.

## Generation and Gameplay Rules

- Derive every floor deterministically from the displayed campaign seed and settings. Deterministic sub-seeds are used for layout retries.
- Generate 64x64 maps with connected rooms, corridors, loops, distinct sound zones, sensible dimensions, and reserved space around starts, doors, keys, elevators, and important pickups.
- Scale enemy population through the episode. Enemy toughness adjusts the mix of standard WL6 guards, dogs, officers, and SS.
- Place health, ammunition, treasure, and weapons according to the selected settings and expected combat load.
- Use graph-aware locked doors: keys precede locks, alternate paths cannot bypass required progression, and critical objects cannot overwrite one another.
- Build exits as recessed elevator vestibules with a valid entrance, clear standing space, elevator walls, and an accessible switch on the back wall.
- Put the boss on floor 9 with suitable supplies and route it to episode completion.
- Generate floor 10 as a secret floor, reached through a concealed dedicated elevator on an eligible early or middle floor and returning to the following normal floor.
- Require every pushwall secret to be movable, reachable, non-trapping, and rewarded with treasure or a weapon upgrade. Secrets may include supplies but are never empty.

## Interfaces and Output

- Define a serializable configuration containing the seed, seven five-step settings, and resolved ECWolf/data/output paths.
- Keep generation independent of the GUI.
- Emit ECWolf WAD map planes and MAPINFO for the main route, secret route, boss completion, names, par times, ceilings, and WL6 music.
- Include a manifest containing the seed, settings, generator version, floor sub-seeds, and validation results.

## Validation and Test Plan

- Validate progression-aware connectivity, key/lock ordering, secret routing, door axes, sound zones, pushwall clearance, elevators, object collisions, spawn clearance, and bounds.
- Confirm every secret contains at least one qualifying valuable pickup.
- Test byte-identical output for identical inputs and differing campaigns for changed seeds.
- Exercise thousands of seeds across slider extremes and mixed settings.
- Parse generated packages and assert that no WL6 asset data is included.
- Provide optional ECWolf smoke tests when a local executable and registered WL6 data are available.

## Assumptions

- Version one uses Python 3, Tkinter, and the standard library.
- Maps 1-8 are normal floors, map 9 is the boss floor, and map 10 is the secret floor.
- Sliders describe intensity rather than exact counts; playability constraints take precedence.
- The initial engine target is ECWolf with registered WL6 data.

