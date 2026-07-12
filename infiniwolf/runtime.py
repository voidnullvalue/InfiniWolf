"""Cross-platform settings, path discovery, and ECWolf launching."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

from .paths import validate_ecwolf, validate_wl6_data


@dataclass(slots=True)
class AppSettings:
    ecwolf: str = ""
    wl6_data: str = ""
    output: str = ""


def settings_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "infiniwolf"


def discover_settings() -> AppSettings:
    """Return safe guesses; discovery never opens or copies registered data."""
    executable_names = ("ecwolf.exe",) if sys.platform == "win32" else ("ecwolf",)
    executable_roots = [
        Path("/media/mount/ecwolf/games"), Path.home() / "ecwolf",
        Path("C:/Program Files/ECWolf"), Path("/Applications/ECWolf.app/Contents/MacOS"),
    ]
    data_roots = [
        Path("/media/mount/ecwolf/data"), Path.home() / "ecwolf/data",
        Path("C:/Program Files/ECWolf"), Path.home() / "Library/Application Support/ecwolf",
    ]
    ecwolf = next((root / name for root in executable_roots for name in executable_names
                   if (root / name).is_file()), None)
    data = next((root for root in data_roots if not validate_wl6_data(root)), None)
    if data is not None and (data.parent / "mods/installed").is_dir():
        output = data.parent / "mods/installed/infiniwolf/infiniwolf.pk3"
    else:
        output = Path.home() / "InfiniWolf" / "infiniwolf.pk3"
    return AppSettings(str(ecwolf) if ecwolf is not None else "",
                       str(data) if data is not None else "", str(output))


def load_settings(path: Path | None = None) -> AppSettings:
    path = path or settings_dir() / "settings.json"
    defaults = discover_settings()
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    return AppSettings(
        ecwolf=str(values.get("ecwolf", defaults.ecwolf)),
        wl6_data=str(values.get("wl6_data", defaults.wl6_data)),
        output=str(values.get("output", defaults.output)),
    )


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    path = path or settings_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def validate_settings(settings: AppSettings, require_output: bool = True) -> list[str]:
    errors = validate_ecwolf(Path(settings.ecwolf))
    errors.extend(validate_wl6_data(Path(settings.wl6_data)))
    if require_output:
        output = Path(settings.output).expanduser()
        if not output.name.lower().endswith(".pk3"):
            errors.append("Output must be a .pk3 file")
        elif output.exists() and not output.is_file():
            errors.append("Output path is not a file")
    return errors


def launch_command(settings: AppSettings) -> list[str]:
    return [settings.ecwolf, "--data", "wl6", "--file", str(Path(settings.output).expanduser().resolve())]


def launch_ecwolf(settings: AppSettings) -> subprocess.Popen[bytes]:
    errors = validate_settings(settings)
    if errors:
        raise ValueError("\n".join(errors))
    if not Path(settings.output).is_file():
        raise ValueError("Generate the campaign before launching it")
    return subprocess.Popen(launch_command(settings), cwd=settings.wl6_data)
