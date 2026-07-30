"""Browser-safe adapters for campaign generation and provenance checking.

The functions in this module deliberately accept and return JSON-compatible
values so Pyodide callers do not need to understand InfiniWolf's Python types.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from pathlib import Path
from typing import Any
import zipfile

from .build_info import COMMIT as BUILD_COMMIT, build_label
from .config import CampaignConfig, GenerationQuality, Intensity, ThemeBias
from .generator import generate_campaign
from .watermark import verify_path

_INTENSITY_FIELDS = (
    "guard_density",
    "enemy_toughness",
    "supplies",
    "treasure",
    "secrets",
    "locked_doors",
    "layout_complexity",
    "decoration_amount",
    "room_shape_variation",
    "patrol_activity",
    "atmosphere",
    "secret_reward_quality",
)
_ALLOWED_FIELDS = frozenset({
    "seed",
    "theme_bias",
    "generation_quality",
    "say_aardwolf",
    *_INTENSITY_FIELDS,
})


def _settings_object(settings: str | Mapping[str, object]) -> dict[str, object]:
    if isinstance(settings, str):
        parsed = json.loads(settings)
    else:
        parsed = dict(settings)
    if not isinstance(parsed, dict):
        raise ValueError("settings must be a JSON object")
    unknown = sorted(set(parsed) - _ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"unknown setting: {', '.join(unknown)}")
    return parsed


def campaign_config(settings: str | Mapping[str, object]) -> CampaignConfig:
    """Convert browser settings into the same configuration used by the CLI."""
    values = _settings_object(settings)
    kwargs: dict[str, Any] = {
        name: Intensity(int(values.get(name, Intensity.NORMAL)))
        for name in _INTENSITY_FIELDS
    }
    kwargs["theme_bias"] = ThemeBias(
        str(values.get("theme_bias", ThemeBias.MIXED.value)))
    kwargs["generation_quality"] = GenerationQuality(
        str(values.get("generation_quality", GenerationQuality.THOROUGH.value)))
    kwargs["say_aardwolf"] = bool(values.get("say_aardwolf", False))
    return CampaignConfig.with_seed(values.get("seed"), **kwargs)


def generate_for_web(
    settings: str | Mapping[str, object],
    output: str | Path,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """Generate a campaign and return JSON metadata for the browser shell."""
    config = campaign_config(settings)
    output_path = generate_campaign(config, Path(output), progress=progress)
    return json.dumps({
        "output": str(output_path),
        "seed": config.seed,
        "build": build_label(),
        "commit": BUILD_COMMIT or "unknown",
    })


def check_for_web(path: str | Path, floor: int | None = None) -> str:
    """Run the existing PK3/WAD provenance checker and return its JSON result."""
    check_path = Path(path)
    effective_floor = None if zipfile.is_zipfile(check_path) else floor
    return verify_path(check_path, effective_floor).to_json()
