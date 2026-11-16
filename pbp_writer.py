import hashlib
import re
import struct
import zlib
from pathlib import Path

from cue_sheet import (
    SECTOR_SIZE,
    CueSheet,
    image_sector_count,
    iter_image_segments,
    parse_cue,
    resolve_source_files,
    track_start_sectors,
)


BLOCK_SIZE = 0x9300
PSAR_IMAGE_OFFSET = 0x100000
INDEX_OFFSET = 0x4000
MAX_INDEXES = 0x7E00
ASSET_DIR = Path(__file__).resolve().parent / "assets"


def _read_asset(name: str) -> bytes:
    path = ASSET_DIR / name
    try:
        return path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"Required PBP resource is missing: {name}") from error


def _round_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _bcd(value: int) -> int:
    if not 0 <= value <= 99:
        raise ValueError("A CUE track address exceeds the PBP TOC time range.")
    return (value // 10) * 16 + value % 10


def _msf(sectors: int) -> bytes:
    minutes, remainder = divmod(sectors, 75 * 60)
    seconds, frames = divmod(remainder, 75)
    return bytes((_bcd(minutes), _bcd(seconds), _bcd(frames)))


def _build_toc(sheet: CueSheet, starts: dict[int, int], sector_count: int) -> bytes:
    toc = bytearray(1024)
    first, last = sheet.tracks[0], sheet.tracks[-1]
    toc[0:10] = bytes((0x41 if first.mode != "AUDIO" else 0x01, 0, 0xA0, 0, 0, 0, 0, 1, 0x20, 0))
    toc[10:20] = bytes((0x01, 0, 0xA1, 0, 0, 0, 0, _bcd(last.number), 0, 0))
    toc[20:30] = bytes((0x01, 0, 0xA2, 0, 0, 0, 0, *_msf(sector_count)))

    for track in sheet.tracks:
        offset = 30 + (track.number - 1) * 10
        track_type = 0x01 if track.mode == "AUDIO" else 0x41
        toc[offset:offset + 3] = bytes((track_type, 0, _bcd(track.number)))
        toc[offset + 3:offset + 6] = _msf(starts[track.number])
        toc[offset + 6] = 0
        toc[offset + 7:offset + 10] = _msf(starts[track.number] + 150)
    return bytes(toc)


def _build_sfo(title: str, disc_id: str) -> bytes:
    text_entries = [
        ("CATEGORY", "ME", 4),
        ("DISC_ID", disc_id, 16),
        ("DISC_VERSION", "1.00", 8),
        ("LICENSE", "Copyright(C) Sony Computer Entertainment America Inc.", 512),
        ("PSP_SYSTEM_VER", "3.01", 8),
        ("TITLE", title, 128),
    ]
    integer_entries = [
        ("BOOTABLE", 1),
        ("PARENTAL_LEVEL", 3),
        ("REGION", 0x8000),
    ]

    key_table = bytearray()
    data_table = bytearray()
    entries = []
    for key, value, max_length in text_entries:
        encoded = value.encode("utf-8") + b"\0"
        if len(encoded) > max_length:
            raise ValueError(f"{key} exceeds its PARAM.SFO size limit.")
        key_offset = len(key_table)
        key_table.extend(key.encode("ascii") + b"\0")
        data_offset = len(data_table)
        data_table.extend(encoded)
        entries.append((key_offset, 0x0204, len(encoded), max_length, data_offset))

    for key, value in integer_entries:
        key_offset = len(key_table)
        key_table.extend(key.encode("ascii") + b"\0")
        data_offset = len(data_table)
        data_table.extend(struct.pack("<I", value))
        entries.append((key_offset, 0x0404, 4, 4, data_offset))

    entries.sort(key=lambda entry: entry[0])
    index_start = 0x14
    key_start = index_start + len(entries) * 0x10
    data_start = key_start + len(key_table)
    header = struct.pack("<4sIIII", b"\0PSF", 0x00000101, key_start, data_start, len(entries))
    index = bytearray()
    for key_offset, fmt, size, max_size, data_offset in entries:
        index.extend(struct.pack("<HHIII", key_offset, fmt, size, max_size, data_offset))
    return header + index + key_table + data_table


def _validate_game_id(value: str) -> str:
    normalized = value.strip().upper().replace("-", "")
    if not re.fullmatch(r"[A-Z]{4}\d{5}", normalized):
        raise ValueError("Game ID must contain four letters and five digits (for example, SLUS-12345).")
    return normalized


def _iter_blocks(segments, actual_size: int):
    buffer = bytearray()
    written = 0
    for segment in segments:
        view = memoryview(segment)
        position = 0
        while position < len(view):
            count = min(BLOCK_SIZE - len(buffer), len(view) - position)
            buffer.extend(view[position:position + count])
            position += count
            written += count
            if len(buffer) == BLOCK_SIZE:
                yield bytes(buffer)
                buffer.clear()
    if written != actual_size:
        raise ValueError("The image byte count does not match the CUE sheet.")
    if buffer:
        buffer.extend(bytes(BLOCK_SIZE - len(buffer)))
        yield bytes(buffer)


def _write_psar(output, sheet: CueSheet, sources, title: str, disc_id: str, level: int):
    sector_count = image_sector_count(sheet, sources)
    padded_size = _round_up(sector_count * SECTOR_SIZE, BLOCK_SIZE)
    block_count = padded_size // BLOCK_SIZE
    if not 2 <= block_count < MAX_INDEXES:
        raise ValueError(
            f"The disc image needs {block_count} PBP blocks; supported range is 2 to {MAX_INDEXES}."
        )
    starts = track_start_sectors(sheet, sources)
    toc = _build_toc(sheet, starts, sector_count)
    psar_start = output.tell()

    header = bytearray(0x400)
    header[:12] = b"PSISOIMG0000"
    struct.pack_into("<I", header, 12, padded_size + PSAR_IMAGE_OFFSET)
    output.write(header)

    id_table = bytearray(0x400)
    id_table[:11] = ("_" + disc_id[:4] + "_" + disc_id[4:]).encode("ascii")
    output.write(id_table)

    toc_table = bytearray(0x400)
    toc_table[:len(toc)] = toc
    struct.pack_into("<I", toc_table, 0x3FC, PSAR_IMAGE_OFFSET)
    output.write(toc_table)

    output.write(bytes(1568))
    info = bytearray(480)
    info[8:10] = b"\xff\x07"
    title_bytes = title.encode("utf-8")
    if len(title_bytes) > len(info) - 12:
        raise ValueError("Title is too long for the PBP disc metadata.")
    info[12:12 + len(title_bytes)] = title_bytes
    output.write(info)
    output.write(bytes(INDEX_OFFSET - output.tell() + psar_start))

    index_table_offset = output.tell()
    output.write(bytes(block_count * 32))
    output.write(bytes(psar_start + PSAR_IMAGE_OFFSET - output.tell()))

    indexes = bytearray()
    data_offset = 0
    for block in _iter_blocks(
        iter_image_segments(sheet, sources), sector_count * SECTOR_SIZE
    ):
        compressed = zlib.compressobj(level, zlib.DEFLATED, -15)
        packed = compressed.compress(block) + compressed.flush()
        if len(packed) >= BLOCK_SIZE:
            payload = block
            block_length = BLOCK_SIZE
            uncompressed = True
        else:
            payload = packed
            block_length = len(packed)
            uncompressed = False

        entry = bytearray(32)
        struct.pack_into("<IH", entry, 0, data_offset, block_length)
        if uncompressed:
            entry[6] = 1
        entry[8:24] = hashlib.sha1(block).digest()[:16]
        indexes.extend(entry)
        output.write(payload)
        data_offset += len(payload)

    end_of_iso = output.tell()
    psar_size = _round_up(end_of_iso, 16) - psar_start
    output.seek(index_table_offset)
    output.write(indexes)
    output.seek(psar_start + 12)
    output.write(struct.pack("<I", psar_size))
    output.seek(psar_start + 0x1220)
    output.write(struct.pack("<I", psar_size + 0x2D31))
    output.seek(end_of_iso)
    output.write(bytes(psar_start + psar_size - output.tell()))

    start_header = bytearray(_read_asset("startdat_header.bin"))
    logo = _read_asset("pops_logo.png")
    struct.pack_into("<I", start_header, 20, len(logo))
    output.write(start_header)
    output.write(logo)
    output.write(_read_asset("startdat_footer.bin"))


def write_pbp(
    cue_path: Path,
    uploaded_sources: dict[str, Path],
    output_path: Path,
    title: str,
    game_id: str,
    images: dict[str, bytes],
    compression_level: int = 5,
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Enter a game title.")
    if len(title.encode("utf-8")) > 127:
        raise ValueError("Game title must be 127 UTF-8 bytes or fewer.")
    disc_id = _validate_game_id(game_id)
    if compression_level not in range(1, 10):
        raise ValueError("Compression level must be between 1 and 9.")

    sheet = parse_cue(cue_path)
    sources = resolve_source_files(sheet, uploaded_sources)
    sfo = _build_sfo(title, disc_id)

    resources = [
        sfo,
        images.get("icon", b""),
        b"",
        images.get("thumbnail", b""),
        images.get("background", b""),
        b"",
        _read_asset("data_psp.bin"),
    ]
    offsets = [0x28]
    for resource in resources:
        offsets.append(offsets[-1] + len(resource))
    psar_offset = _round_up(offsets[-1], 0x10000)
    header = struct.pack("<10I", 0x50425000, 0x00010000, *offsets[:7], psar_offset)

    with output_path.open("wb+") as output:
        output.write(header)
        for resource in resources:
            output.write(resource)
        output.write(bytes(psar_offset - output.tell()))
        _write_psar(output, sheet, sources, title, disc_id, compression_level)
