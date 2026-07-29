#!/usr/bin/env python3
"""Decode original Wolfenstein 3D maps for reference-corpus measurements."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import struct


NEAR_TAG = 0xA7
FAR_TAG = 0xA8
MAP_COUNT = 100
MAP_HEADER = struct.Struct("<iiiHHHHH16s")


@dataclass(frozen=True)
class MapRecord:
    """The two gameplay planes needed for corpus measurements."""

    index: int
    name: str
    width: int
    height: int
    tiles: list[int]
    things: list[int]


def read_maphead(path: str | Path) -> tuple[int, list[int]]:
    """Read the fixed index separately so shipped data never enters the package."""
    data = Path(path).read_bytes()
    expected = 2 + MAP_COUNT * 4
    if len(data) < expected:
        raise ValueError(f"{path}: MAPHEAD is {len(data)} bytes, expected at least {expected}")
    rlew_magic = struct.unpack_from("<H", data)[0]
    offsets = list(struct.unpack_from(f"<{MAP_COUNT}i", data, 2))
    return rlew_magic, offsets


def _take_byte(data: bytes, position: int) -> tuple[int, int]:
    if position >= len(data):
        raise ValueError("truncated Carmack stream")
    return data[position], position + 1


def _carmack_expand(data: bytes, expected_length: int) -> bytes:
    if expected_length % 2:
        raise ValueError("Carmack output length is not word-aligned")

    output = bytearray()
    position = 0
    while len(output) < expected_length:
        count, position = _take_byte(data, position)
        tag, position = _take_byte(data, position)

        if tag not in (NEAR_TAG, FAR_TAG):
            if len(output) + 2 > expected_length:
                raise ValueError("Carmack stream exceeds its declared length")
            output.extend((count, tag))
            continue

        if count == 0:
            value, position = _take_byte(data, position)
            if len(output) + 2 > expected_length:
                raise ValueError("Carmack stream exceeds its declared length")
            output.extend((value, tag))
            continue

        if len(output) + 2 * count > expected_length:
            raise ValueError("Carmack pointer exceeds its declared length")

        if tag == NEAR_TAG:
            offset, position = _take_byte(data, position)
            source = len(output) - 2 * offset
        else:
            if position + 2 > len(data):
                raise ValueError("truncated Carmack far pointer")
            offset = struct.unpack_from("<H", data, position)[0]
            position += 2
            source = 2 * offset

        for _ in range(count):
            if source < 0 or source + 2 > len(output):
                raise ValueError("Carmack pointer refers outside expanded data")
            output.extend(output[source:source + 2])
            source += 2

    return bytes(output)


def _rlew_expand(data: bytes, expected_length: int, rlew_magic: int) -> list[int]:
    if expected_length % 2:
        raise ValueError("RLEW output length is not word-aligned")

    expected_words = expected_length // 2
    output: list[int] = []
    position = 0
    while len(output) < expected_words:
        if position + 2 > len(data):
            raise ValueError("truncated RLEW stream")
        value = struct.unpack_from("<H", data, position)[0]
        position += 2
        if value != rlew_magic:
            output.append(value)
            continue

        if position + 4 > len(data):
            raise ValueError("truncated RLEW run")
        count, value = struct.unpack_from("<HH", data, position)
        position += 4
        if len(output) + count > expected_words:
            raise ValueError("RLEW run exceeds its declared length")
        output.extend([value] * count)

    return output


def _decode_plane(
    data: bytes,
    offset: int,
    compressed_length: int,
    expected_length: int,
    rlew_magic: int,
) -> list[int]:
    if offset < 0 or compressed_length < 2 or offset + compressed_length > len(data):
        raise ValueError("plane lies outside GAMEMAPS")

    compressed = data[offset:offset + compressed_length]
    carmack_length = struct.unpack_from("<H", compressed)[0]
    rlew_data = _carmack_expand(compressed[2:], carmack_length)
    if len(rlew_data) < 2:
        raise ValueError("RLEW stream has no output length")

    rlew_length = struct.unpack_from("<H", rlew_data)[0]
    if rlew_length != expected_length:
        raise ValueError("plane dimensions disagree with its decompressed length")
    return _rlew_expand(rlew_data[2:], rlew_length, rlew_magic)


def read_gamemaps(
    gamemaps_path: str | Path,
    maphead_path: str | Path,
) -> list[MapRecord]:
    """Decode usable slots while isolating corrupt entries from the corpus."""
    rlew_magic, offsets = read_maphead(maphead_path)
    data = Path(gamemaps_path).read_bytes()
    records: list[MapRecord] = []

    for index, offset in enumerate(offsets):
        if offset <= 0 or offset + MAP_HEADER.size > len(data):
            continue
        try:
            (
                plane0_offset,
                plane1_offset,
                _plane2_offset,
                plane0_length,
                plane1_length,
                _plane2_length,
                width,
                height,
                raw_name,
            ) = MAP_HEADER.unpack_from(data, offset)
            if width <= 0 or height <= 0:
                raise ValueError("map has invalid dimensions")
            expected_length = width * height * 2
            tiles = _decode_plane(
                data, plane0_offset, plane0_length, expected_length, rlew_magic)
            things = _decode_plane(
                data, plane1_offset, plane1_length, expected_length, rlew_magic)
            name = raw_name.split(b"\0", 1)[0].decode("ascii")
        except (UnicodeDecodeError, ValueError, struct.error):
            continue

        records.append(MapRecord(index, name, width, height, tiles, things))

    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decode original Wolfenstein 3D maps for corpus measurements.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--variant", default="WL6", choices=("WL6", "WL1"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    maps = read_gamemaps(
        args.data_dir / f"GAMEMAPS.{args.variant}",
        args.data_dir / f"MAPHEAD.{args.variant}",
    )
    for record in maps:
        floor_cells = sum(tile >= 106 for tile in record.tiles)
        door_count = sum(90 <= tile <= 101 for tile in record.tiles)
        if args.json:
            print(json.dumps({
                "index": record.index,
                "name": record.name,
                "width": record.width,
                "height": record.height,
                "floor_cells": floor_cells,
                "doors": door_count,
            }))
        else:
            print(
                f"{record.index:2d} {record.name:<16} "
                f"{record.width}x{record.height} "
                f"floor={floor_cells} doors={door_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
