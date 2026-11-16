import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


SECTOR_SIZE = 2352


@dataclass
class CueFile:
    name: str
    tracks: list["CueTrack"] = field(default_factory=list)


@dataclass
class CueTrack:
    number: int
    mode: str
    file: CueFile
    indexes: dict[int, int] = field(default_factory=dict)
    pregap: int = 0


@dataclass
class CueSheet:
    files: list[CueFile]
    tracks: list[CueTrack]


def _time_to_frames(value: str) -> int:
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-7]\d)", value)
    if not match:
        raise ValueError(f"Invalid CUE time value: {value}")
    minutes, seconds, frames = map(int, match.groups())
    if frames >= 75:
        raise ValueError(f"Invalid CUE frame value: {value}")
    return (minutes * 60 + seconds) * 75 + frames


def parse_cue(path: Path) -> CueSheet:
    files: list[CueFile] = []
    tracks: list[CueTrack] = []
    current_file: CueFile | None = None
    current_track: CueTrack | None = None

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("The CUE sheet must be a UTF-8 or ASCII text file.") from error

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.upper().startswith("REM "):
            continue
        try:
            parts = shlex.split(line, posix=True)
        except ValueError as error:
            raise ValueError(f"Invalid quoting on CUE line {line_number}.") from error
        if not parts:
            continue

        command = parts[0].upper()
        if command == "FILE":
            if len(parts) != 3 or parts[2].upper() not in ("BINARY", "MOTOROLA"):
                raise ValueError(f"Unsupported FILE directive on CUE line {line_number}.")
            name = Path(parts[1].replace("\\", "/")).name
            if not name or name in (".", ".."):
                raise ValueError(f"Invalid image filename on CUE line {line_number}.")
            current_file = CueFile(name)
            files.append(current_file)
            current_track = None
        elif command == "TRACK":
            if current_file is None or len(parts) != 3:
                raise ValueError(f"TRACK must follow FILE on CUE line {line_number}.")
            try:
                number = int(parts[1])
            except ValueError as error:
                raise ValueError(f"Invalid track number on CUE line {line_number}.") from error
            if number != len(tracks) + 1 or number > 99:
                raise ValueError("Tracks must be numbered sequentially from 1 to 99.")
            mode = parts[2].upper()
            if mode not in ("AUDIO", "MODE1/2352", "MODE2/2352"):
                raise ValueError(
                    f"Unsupported track mode {mode}; use raw 2352-byte sectors."
                )
            current_track = CueTrack(number, mode, current_file)
            current_file.tracks.append(current_track)
            tracks.append(current_track)
        elif command == "INDEX":
            if current_track is None or len(parts) != 3:
                raise ValueError(f"INDEX must follow TRACK on CUE line {line_number}.")
            try:
                index = int(parts[1])
            except ValueError as error:
                raise ValueError(f"Invalid index number on CUE line {line_number}.") from error
            if index not in (0, 1):
                raise ValueError("Only CUE indexes 00 and 01 are supported.")
            if index in current_track.indexes:
                raise ValueError(f"Duplicate INDEX {index:02d} on CUE line {line_number}.")
            current_track.indexes[index] = _time_to_frames(parts[2])
        elif command == "PREGAP":
            if current_track is None or len(parts) != 2:
                raise ValueError(f"PREGAP must follow TRACK on CUE line {line_number}.")
            current_track.pregap = _time_to_frames(parts[1])

    if not files or not tracks:
        raise ValueError("The selected CUE sheet contains no tracks.")
    for track in tracks:
        if 1 not in track.indexes:
            raise ValueError(f"Track {track.number:02d} is missing INDEX 01.")
        if 0 in track.indexes and track.indexes[0] > track.indexes[1]:
            raise ValueError(f"Track {track.number:02d} has INDEX 00 after INDEX 01.")
        if track.pregap and 0 in track.indexes:
            raise ValueError(
                f"Track {track.number:02d} has both PREGAP and INDEX 00; "
                "remove one to avoid duplicating its pregap."
            )
    return CueSheet(files, tracks)


def resolve_source_files(sheet: CueSheet, uploaded: dict[str, Path]) -> dict[str, Path]:
    by_name: dict[str, Path] = {}
    for name, path in uploaded.items():
        key = Path(name.replace("\\", "/")).name.casefold()
        if key in by_name and by_name[key] != path:
            raise ValueError(f"More than one uploaded file is named {name}.")
        by_name[key] = path

    resolved: dict[str, Path] = {}
    for cue_file in sheet.files:
        key = cue_file.name.casefold()
        if key not in by_name:
            raise ValueError(f"The CUE sheet references a missing file: {cue_file.name}")
        source = by_name[key]
        if source.stat().st_size == 0:
            raise ValueError(f"The image file is empty: {cue_file.name}")
        if source.stat().st_size % SECTOR_SIZE:
            raise ValueError(
                f"{cue_file.name} is not a raw 2352-byte-sector BIN. "
                "Cooked ISO and 2448-byte subchannel images are not supported."
            )
        resolved[cue_file.name] = source

    for cue_file in sheet.files:
        sectors = resolved[cue_file.name].stat().st_size // SECTOR_SIZE
        for track in cue_file.tracks:
            if track.indexes[1] >= sectors:
                raise ValueError(
                    f"Track {track.number:02d} starts past the end of {cue_file.name}."
                )
            if track.pregap and track.indexes[1] > sectors:
                raise ValueError(
                    f"Track {track.number:02d} pregap exceeds {cue_file.name}."
                )
    return resolved


def image_sector_count(sheet: CueSheet, sources: dict[str, Path]) -> int:
    sectors = 0
    for cue_file in sheet.files:
        source_sectors = sources[cue_file.name].stat().st_size // SECTOR_SIZE
        pregap_sectors = sum(track.pregap for track in cue_file.tracks)
        sectors += source_sectors + pregap_sectors
    return sectors


def track_start_sectors(
    sheet: CueSheet, sources: dict[str, Path]
) -> dict[int, int]:
    starts: dict[int, int] = {}
    base_sector = 0
    for cue_file in sheet.files:
        track_gaps = sorted(
            (track.indexes.get(0, track.indexes[1]), track.pregap)
            for track in cue_file.tracks
            if track.pregap
        )
        prior_gaps = 0
        for track in cue_file.tracks:
            source_start = track.indexes[1]
            prior_gaps = sum(gap for position, gap in track_gaps if position <= source_start)
            starts[track.number] = base_sector + source_start + prior_gaps
        base_sector += (
            sources[cue_file.name].stat().st_size // SECTOR_SIZE
            + sum(gap for _, gap in track_gaps)
        )
    return starts


def iter_image_segments(sheet: CueSheet, sources: dict[str, Path]):
    for cue_file in sheet.files:
        source = sources[cue_file.name]
        events = sorted(
            (track.indexes.get(0, track.indexes[1]), track.pregap)
            for track in cue_file.tracks
            if track.pregap
        )
        cursor = 0
        with source.open("rb") as stream:
            for sector, gap in events:
                position = sector * SECTOR_SIZE
                stream.seek(cursor)
                remaining = position - cursor
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(f"Unexpected end of image file: {cue_file.name}")
                    yield chunk
                    remaining -= len(chunk)
                zero_bytes = gap * SECTOR_SIZE
                zero_chunk = bytes(min(1024 * 1024, zero_bytes))
                while zero_bytes:
                    count = min(len(zero_chunk), zero_bytes)
                    yield zero_chunk[:count]
                    zero_bytes -= count
                cursor = position
            stream.seek(cursor)
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
