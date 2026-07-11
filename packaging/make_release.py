#!/usr/bin/env python3
"""Assemble a per-platform release .zip: PyInstaller output for randomwolf
plus a pinned, checksum-verified copy of ECWolf's official GPL-edition build.

Never bundles Wolfenstein 3D game data. The generated .zip is meant to be
dropped next to (or unpacked into) the user's own registered WL6 install --
ECWolf reads that data at runtime exactly as it always has.

ECWolf's source is dual licensed (id Software's non-commercial license, or
GPLv2+); only the GPL edition may be redistributed here. Every URL below was
manually verified (see the packaging notes in this repo's PR/commit history)
to be the GPL build: the Windows archive's own bundled `readme.1st` says
"this build uses the GPL", and the Linux .deb is the literal Debian archive
package, which cannot legally carry the non-commercial edition (Debian's
DFSG forbids field-of-endeavor restrictions like the id/MAME license's
"may not be sold, nor used in a commercial product" clause). Bumping
ECWOLF_VERSION requires re-verifying and re-pinning these hashes by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ECWOLF_VERSION = "1.4.2"
ECWOLF_BASE_URL = "https://maniacsvault.net/ecwolf/files/ecwolf/1.x/"
# (archive filename, sha256) -- pinned against the files actually downloaded
# and inspected while wiring up this pipeline.
ECWOLF_ARTIFACTS = {
    "windows": (f"ecwolf-{ECWOLF_VERSION}_x64.zip",
                "61bd6bf62d2e44975e46f86ca57fdabd8a3937da04750a842a970d7a8b56a58a"),
    "linux": (f"ecwolf-{ECWOLF_VERSION}_amd64.deb",
              "210a00a92567eb9d1c669acd1999d78d0667c455c566b2e8ec08d568f42c08a4"),
    "macos": (f"ecwolf-{ECWOLF_VERSION}.dmg",
              "9261a1105c48669ff27bde975a82c95605e8d36503505e3e3bee39de76baca95"),
}
LICENSE_NAME_HINTS = ("licens", "copyright", "readme.1st", "gpl", "eula")


def _download_verified(url: str, sha256: str, dest: Path) -> Path:
    print(f"Downloading {url}")
    urllib.request.urlretrieve(url, dest)  # noqa: S310 -- fixed https URL, hash-checked below
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != sha256:
        raise SystemExit(
            f"refusing to use {dest.name}: sha256 {digest} does not match the pinned "
            f"{sha256}. The upstream file changed -- re-verify its license before repinning."
        )
    return dest


def _collect_licenses(root: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for path in root.rglob("*"):
        if path.is_file() and any(hint in path.name.lower() for hint in LICENSE_NAME_HINTS):
            shutil.copy2(path, out / path.name)


def _stage_windows(archive: Path, work: Path, engine_dir: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work)
    for name in ("ecwolf.exe", "ecwolf-console.exe", "ecwolf.pk3"):
        shutil.copy2(work / name, engine_dir / name)
    licenses_zip = work / "licenses.zip"
    if licenses_zip.is_file():
        licenses_dir = work / "licenses"
        with zipfile.ZipFile(licenses_zip) as zf:
            zf.extractall(licenses_dir)
        _collect_licenses(licenses_dir, engine_dir / "THIRD_PARTY_LICENSES" / "ecwolf")


def _stage_linux(archive: Path, work: Path, engine_dir: Path) -> None:
    subprocess.run(["ar", "x", str(archive.resolve()), "data.tar.xz"], cwd=work, check=True)
    with tarfile.open(work / "data.tar.xz") as tar:
        tar.extractall(work)  # noqa: S202 -- trusted, checksum-verified upstream .deb
    binary = work / "usr/games/ecwolf"
    shutil.copy2(binary, engine_dir / "ecwolf")
    (engine_dir / "ecwolf").chmod(0o755)
    shutil.copy2(work / "usr/share/ecwolf/ecwolf.pk3", engine_dir / "ecwolf.pk3")
    _collect_licenses(work / "usr/share/doc/ecwolf", engine_dir / "THIRD_PARTY_LICENSES" / "ecwolf")


def _stage_macos(archive: Path, work: Path, engine_dir: Path) -> None:
    if sys.platform != "darwin":
        raise SystemExit("macOS packaging requires hdiutil and must run on a macOS runner")
    mount_point = work / "mnt"
    mount_point.mkdir()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount_point), str(archive)],
                   check=True)
    try:
        app = next(mount_point.glob("*.app"))
        shutil.copytree(app, engine_dir / app.name)
        _collect_licenses(mount_point, engine_dir / "THIRD_PARTY_LICENSES" / "ecwolf")
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)


STAGERS = {"windows": _stage_windows, "linux": _stage_linux, "macos": _stage_macos}


def _dist_binary(dist: Path, name: str, platform: str) -> Path:
    if platform == "windows":
        return dist / f"{name}.exe"
    if platform == "macos" and (dist / f"{name}.app").is_dir():
        return dist / f"{name}.app"
    return dist / name


README_TEMPLATE = """\
Random Wolf {version} ({platform})
====================================

1. Unpack this entire folder next to your own legally owned, registered
   copy of Wolfenstein 3D (the WL6 data files), or copy its contents into
   that folder. Random Wolf never includes any Wolfenstein 3D game data --
   you must supply your own.
2. Run {gui_binary} and choose your generation settings.
3. Click Generate, then Play. It launches the bundled ECWolf ({ecwolf_version},
   GPL edition) pointed at your WL6 data and the freshly generated campaign.

{cli_binary} is the same generator as a scriptable command-line tool; run it
with --help for options.

Licensing: Random Wolf is MIT licensed (see LICENSE). The bundled ECWolf
engine is GPLv2+ licensed; see THIRD_PARTY_LICENSES/ecwolf for its license
text and copyright notices, and https://github.com/ECWolfEngine/ECWolf for
its source.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows", "linux", "macos"))
    parser.add_argument("--version", required=True, help="randomwolf release version, e.g. 0.2.0")
    parser.add_argument("--dist", type=Path, default=Path("dist"), help="PyInstaller output directory")
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--out", type=Path, default=Path("release"))
    args = parser.parse_args()

    package_name = f"RandomWolf-{args.version}-{args.platform}"
    staging = args.out / package_name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    gui_src = _dist_binary(args.dist, "RandomWolf", args.platform)
    cli_src = _dist_binary(args.dist, "randomwolf-cli", args.platform)
    for src in (gui_src, cli_src):
        if not src.exists():
            raise SystemExit(f"expected PyInstaller output missing: {src}")
        dest = staging / src.name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dest)

    artifact_name, sha256 = ECWOLF_ARTIFACTS[args.platform]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        archive = _download_verified(ECWOLF_BASE_URL + artifact_name, sha256, work / artifact_name)
        STAGERS[args.platform](archive, work, staging)

    shutil.copy2(args.repo_root / "LICENSE", staging / "LICENSE")
    (staging / "README.txt").write_text(
        README_TEMPLATE.format(
            version=args.version, platform=args.platform, ecwolf_version=ECWOLF_VERSION,
            gui_binary=gui_src.name, cli_binary=cli_src.name,
        ),
        encoding="utf-8",
    )

    args.out.mkdir(parents=True, exist_ok=True)
    archive_path = args.out / f"{package_name}.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging.rglob("*")):
            zf.write(path, arcname=Path(package_name) / path.relative_to(staging))
    print(f"Wrote {archive_path}")


if __name__ == "__main__":
    main()
