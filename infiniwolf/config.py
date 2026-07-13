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


class ThemeBias(str, Enum):
    MIXED = "mixed"
    GARRISON = "garrison"
    CATACOMBS = "catacombs"
    GRAND_HALLS = "grand-halls"
    STOREHOUSE = "storehouse"
    QUARTERS = "quarters"


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

    @classmethod
    def with_seed(cls, seed: str | int | None = None, **settings: object) -> "CampaignConfig":
        return cls(seed=resolve_seed(seed), **settings)

    def floor_seed(self, floor: int, attempt: int = 0) -> int:
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        payload = f"infiniwolf:v1:{self.seed}:{floor}:{attempt}".encode("ascii")
        return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little")

    def variant_seed(self, floor: int) -> int:
        """Seed for a floor's base-variant pick, separate from floor_seed.

        Deliberately independent of attempt: validation retries reroll a
        floor's layout but must keep its variant identity. A distinct payload
        prefix keeps this stream decoupled from floor_seed, whose format is
        frozen by the determinism contract."""
        if not 1 <= floor <= 10:
            raise ValueError("floor must be between 1 and 10")
        payload = f"infiniwolf:variant:v1:{self.seed}:{floor}".encode("ascii")
        return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little")

    def lock_seed(self) -> int:
        """Campaign-wide stream for the authored lock/key schedule."""
        payload = f"infiniwolf:locks:v1:{self.seed}".encode("ascii")
        return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "little")

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
