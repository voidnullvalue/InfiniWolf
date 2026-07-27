"""Configuration and deterministic seed handling."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, IntEnum
import hashlib
import json
from pathlib import Path
import time


class Intensity(IntEnum):
    VERY_LOW = 1
    LOW = 2
    NORMAL = 3
    HIGH = 4
    VERY_HIGH = 5


class GenerationQuality(str, Enum):
    """How hard to look for a good floor before accepting one.

    Candidate generation is deterministic, so this only widens the pool the
    selector chooses from -- it never makes an invalid map acceptable. Ranking
    happens strictly among candidates that already passed validate_map.

    FAST reproduces the historical behaviour: take the first candidate with no
    critique flags, falling back to the best of three. BALANCED and THOROUGH keep
    generating to a larger pool and then rank.

    Measured over three seeds, ten floors each: fast averaged 7.3 critique flags
    per campaign in 127s, balanced 5.0 in 178s, thorough 3.3 in 281s. Pool sizes
    are set so the three tiers actually differ -- an earlier arrangement gave
    balanced the same pool as fast's fallback, and it produced byte-for-byte the
    same flag counts for six percent more time.

    THOROUGH is the default. It halves the flag count, and because the corridor
    router got 2.8x faster it now costs less wall clock than fast did before that
    change, so the better maps are not paid for with a regression in generation
    time against any previously shipped version.
    """
    FAST = "fast"
    BALANCED = "balanced"
    THOROUGH = "thorough"

    @property
    def pool_size(self) -> int:
        return {"fast": 3, "balanced": 5, "thorough": 8}[self.value]


class ThemeBias(str, Enum):
    MIXED = "mixed"
    GARRISON = "garrison"
    CATACOMBS = "catacombs"
    GRAND_HALLS = "grand-halls"
    STOREHOUSE = "storehouse"
    QUARTERS = "quarters"


@dataclass(frozen=True, slots=True)
class LittleEntropyMachine:
    """Named deterministic source for every independent generator stream.

    Payload formats are intentionally frozen: giving the seed source a real
    identity must not perturb established campaign seeds or retry behavior.
    """
    seed: int

    @staticmethod
    def _digest(payload: str) -> int:
        return int.from_bytes(
            hashlib.blake2b(payload.encode("ascii"), digest_size=8).digest(),
            "little")

    def floor(self, floor: int, attempt: int = 0) -> int:
        return self._digest(f"infiniwolf:v1:{self.seed}:{floor}:{attempt}")

    def variant(self, floor: int) -> int:
        return self._digest(f"infiniwolf:variant:v1:{self.seed}:{floor}")

    def locks(self) -> int:
        return self._digest(f"infiniwolf:locks:v1:{self.seed}")

    def circulation(self, floor: int) -> int:
        return self._digest(f"infiniwolf:circulation:v1:{self.seed}:{floor}")

    def vines(self) -> int:
        return self._digest(f"infiniwolf:vines:v1:{self.seed}")

    def guard_gallery(self) -> int:
        return self._digest(f"infiniwolf:guard-gallery:v1:{self.seed}")

    def rare_motif(self) -> int:
        return self._digest(f"infiniwolf:rare-motif:v1:{self.seed}")

    def aardwolf(self, floor: int) -> int:
        return self._digest(f"infiniwolf:aardwolf:v1:{self.seed}:{floor}")


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    seed: int
    guard_density: Intensity = Intensity.NORMAL
    enemy_toughness: Intensity = Intensity.NORMAL
    supplies: Intensity = Intensity.NORMAL
    treasure: Intensity = Intensity.NORMAL
    secrets: Intensity = Intensity.NORMAL
    locked_doors: Intensity = Intensity.NORMAL
    layout_complexity: Intensity = Intensity.NORMAL
    decoration_amount: Intensity = Intensity.NORMAL
    room_shape_variation: Intensity = Intensity.NORMAL
    patrol_activity: Intensity = Intensity.NORMAL
    atmosphere: Intensity = Intensity.NORMAL
    secret_reward_quality: Intensity = Intensity.NORMAL
    theme_bias: ThemeBias = ThemeBias.MIXED
    generation_quality: GenerationQuality = GenerationQuality.THOROUGH
    say_aardwolf: bool = False

    @classmethod
    def with_seed(cls, seed: str | int | None = None, **settings: object) -> "CampaignConfig":
        return cls(seed=resolve_seed(seed), **settings)

    def floor_seed(self, floor: int, attempt: int = 0) -> int:
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        return LittleEntropyMachine(self.seed).floor(floor, attempt)

    def variant_seed(self, floor: int) -> int:
        """Seed for a floor's base-variant pick, separate from floor_seed.

        Deliberately independent of attempt: validation retries reroll a
        floor's layout but must keep its variant identity. A distinct payload
        prefix keeps this stream decoupled from floor_seed, whose format is
        frozen by the determinism contract."""
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        return LittleEntropyMachine(self.seed).variant(floor)

    def lock_seed(self) -> int:
        """Campaign-wide stream for the authored lock/key schedule."""
        return LittleEntropyMachine(self.seed).locks()

    def circulation_seed(self, floor: int) -> int:
        """Independent stream for a floor's building-circulation skeleton."""
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        return LittleEntropyMachine(self.seed).circulation(floor)

    def vine_seed(self) -> int:
        """Campaign-wide stream for the single overgrown hallway sector."""
        return LittleEntropyMachine(self.seed).vines()

    def guard_gallery_seed(self) -> int:
        """Campaign-wide stream for the rare inaccessible combat gallery."""
        return LittleEntropyMachine(self.seed).guard_gallery()

    def rare_motif_seed(self) -> int:
        """Campaign-wide stream for exceptionally rare plan compositions."""
        return LittleEntropyMachine(self.seed).rare_motif()

    def aardwolf_seed(self, floor: int) -> int:
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        return LittleEntropyMachine(self.seed).aardwolf(floor)

    def to_json(self) -> str:
        values = asdict(self)
        values.update({key: int(value) for key, value in values.items() if isinstance(value, IntEnum)})
        values.update({key: value.value for key, value in values.items() if isinstance(value, Enum)})
        return json.dumps(values, indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    ecwolf: Path
    wl6_data: Path
    output: Path


def resolve_seed(value: str | int | None) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return time.time_ns() & ((1 << 63) - 1)
    if isinstance(value, int):
        seed = value
    else:
        text = value.strip()
        try:
            seed = int(text, 0)
        except ValueError:
            seed = int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "little")
    if seed < 0:
        raise ValueError("seed must not be negative")
    return seed & ((1 << 64) - 1)
