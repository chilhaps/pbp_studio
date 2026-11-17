import ctypes
import hashlib
import json
import platform
import re
import struct
import sys
import zlib
from functools import lru_cache
from pathlib import Path

from cue_sheet import (
    SECTOR_SIZE,
    CueSheet,
    image_sector_count,
    iter_image_segments,
    parse_cue,
    resolve_sibling_source_files,
    resolve_source_files,
    track_start_sectors,
)


BLOCK_SIZE = 0x9300
PSAR_IMAGE_OFFSET = 0x100000
INDEX_OFFSET = 0x4000
MAX_INDEXES = 0x7E00
MAX_DISCS = 5
POP_FE_COMPRESSION_LEVEL = 1
POP_FE_DATAPSP_SIZE = 30632
ASSET_DIR = Path(__file__).resolve().parent / "assets"


@lru_cache(maxsize=1)
def _load_pop_fe_compressor():
    if zlib.ZLIB_RUNTIME_VERSION == "1.2.13":
        return None, None
    # zlib-ng emits valid DEFLATE, but not the byte stream used by pop-fe's zlib 1.2.13.
    path = _pop_fe_compressor_path()
    if path is None:
        raise RuntimeError(
            "Pop-fe-compatible PSAR compression requires zlib 1.2.13 or a bundled "
            "x86-64 Linux, macOS, or Windows runtime."
        )

    try:
        library = ctypes.CDLL(str(path))
    except OSError as error:
        raise RuntimeError(
            f"The bundled pop-fe-compatible zlib library could not be loaded: {path.name}."
        ) from error

    version = library.zlibVersion
    version.restype = ctypes.c_char_p
    if version() != b"1.2.13":
        raise RuntimeError("The bundled pop-fe-compatible zlib library has an invalid version.")

    compress2 = library.compress2
    compress2.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    )
    compress2.restype = ctypes.c_int
    return library, compress2


def _pop_fe_compressor_path(
    system: str | None = None,
    machine: str | None = None,
) -> Path | None:
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    if machine.casefold() not in ("x86_64", "amd64"):
        return None
    library_names = {
        "linux": "libz_1_2_13.so",
        "darwin": "libz_1_2_13.dylib",
        "win32": "libz_1_2_13.dll",
    }
    library_name = library_names.get(system)
    return ASSET_DIR / library_name if library_name else None


DEFAULT_PSP_CONFIG = bytes.fromhex(
    "700007060000060600000000ffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffff00000000ffffffffffffffffffffffffffff0000"
    "ffffffffffffffff0000000000000000ffffffffffffffffffffffffffffffff"
    "0000000000000000ffffffff00000000ffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffff00000000ffffffffffffffffffffffff"
    "ffff0000ffffffffffffffff0000000000000000ffffffffffffffffffffffff"
    "ffffffff0000000000000000ffffffff"
)
# Keep the tested Silent Hill profile used by the working reference PBP.
PSP_CONFIG_OVERRIDES = {
    "SLUS00707": bytes.fromhex(
        "700007060000060602000000ffffffffffffffffffffffffffffffffffffffff"
        "fffffffffffffffffffffffffffffffffefefeffffffffffffffffffffffffff"
        "fffffffffeffffffffffffff00000000ffffffffffffffffffffffffffff0000"
        "ffffffffffffffff0000000000000000ffffffffffffffffffffffffffffffff"
        "0000000000000000ffffffff02000000ffffffffffffffffffffffffffffffff"
        "fffffffffffffffffffffffffffffffffffffffffefefeffffffffffffffffff"
        "fffffffffffffffffeffffffffffffff00000000ffffffffffffffffffffffff"
        "ffff0000ffffffffffffffff0000000000000000ffffffffffffffffffffffff"
        "ffffffff0000000000000000ffffffff"
    )
}


def _read_asset(name: str) -> bytes:
    path = ASSET_DIR / name
    try:
        return path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"Required PBP resource is missing: {name}") from error


def _load_psp_config_map() -> dict[str, str]:
    path = ASSET_DIR / "pspconfig_map.json"
    try:
        profiles = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("The bundled PSP profile map is missing or invalid.") from error
    if not isinstance(profiles, dict) or any(
        not isinstance(game_id, str)
        or not re.fullmatch(r"[A-Z]{4}\d{5}", game_id)
        or not isinstance(profile, str)
        or Path(profile).is_absolute()
        or ".." in Path(profile).parts
        for game_id, profile in profiles.items()
    ):
        raise RuntimeError("The bundled PSP profile map contains invalid entries.")
    return profiles


PSP_CONFIG_MAP = _load_psp_config_map()


def _psp_config_for_id(disc_id: str) -> bytes:
    override = PSP_CONFIG_OVERRIDES.get(disc_id)
    if override is not None:
        return override
    relative_path = PSP_CONFIG_MAP.get(disc_id)
    if relative_path is None:
        return DEFAULT_PSP_CONFIG
    path = ASSET_DIR / "pspconfigs" / relative_path
    try:
        config = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"Bundled PSP profile is missing: {relative_path}") from error
    if len(config) > 0x3E0:
        raise RuntimeError(f"Bundled PSP profile is too large: {relative_path}")
    return config


def _round_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _zlib_compress_pop_fe(block: bytes) -> bytes:
    _, compress2 = _load_pop_fe_compressor()
    if compress2 is None:
        return zlib.compress(block, POP_FE_COMPRESSION_LEVEL)

    block_size = len(block)
    capacity = (
        block_size
        + (block_size >> 12)
        + (block_size >> 14)
        + (block_size >> 25)
        + 13
    )
    source = ctypes.create_string_buffer(block, block_size)
    destination = ctypes.create_string_buffer(capacity)
    destination_length = ctypes.c_ulong(capacity)
    result = compress2(
        destination,
        ctypes.byref(destination_length),
        source,
        block_size,
        POP_FE_COMPRESSION_LEVEL,
    )
    if result != 0:
        raise RuntimeError(f"zlib 1.2.13 failed to compress a PSAR block (error {result}).")
    return destination.raw[:destination_length.value]


def _encode_psar_block(block: bytes) -> bytes:
    compressed = _zlib_compress_pop_fe(block)[2:-4]
    return block if len(compressed) >= BLOCK_SIZE else compressed


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
    toc[20:30] = bytes((0x01, 0, 0xA2, 0, 0, 0, 0, *_msf(sector_count + 1)))

    for track in sheet.tracks:
        offset = 30 + (track.number - 1) * 10
        track_type = 0x01 if track.mode == "AUDIO" else 0x41
        toc[offset:offset + 3] = bytes((track_type, 0, _bcd(track.number)))
        toc[offset + 3:offset + 6] = _msf(
            151 if track.number == 1 else starts[track.number]
        )
        toc[offset + 6] = 0
        toc[offset + 7:offset + 10] = _msf(
            150 if track.number == 1 else starts[track.number] + 150
        )
    return bytes(toc)


def _build_sfo(title: str, disc_id: str) -> bytes:
    values = [
        ("BOOTABLE", 0x0404, struct.pack("<I", 1), 4),
        ("CATEGORY", 0x0204, "ME", 4),
        ("DISC_ID", 0x0204, disc_id, 16),
        ("DISC_VERSION", 0x0204, "1.00", 8),
        ("LICENSE", 0x0204, "Copyright(C) Sony Computer Entertainment America Inc.", 512),
        ("PARENTAL_LEVEL", 0x0404, struct.pack("<I", 3), 4),
        ("PSP_SYSTEM_VER", 0x0204, "3.01", 8),
        ("REGION", 0x0404, struct.pack("<I", 0x8000), 4),
        ("TITLE", 0x0204, title, 128),
    ]

    key_table = bytearray()
    data_table = bytearray()
    entries = []
    for key, fmt, value, max_length in values:
        key_offset = len(key_table)
        data_offset = len(data_table)
        key_table.extend(key.encode("utf-8") + b"\0")
        if fmt == 0x0204:
            encoded = value.encode("utf-8") + b"\0"
            if len(encoded) > max_length:
                raise ValueError(f"{key} exceeds its PARAM.SFO size limit.")
            data_table.extend(encoded)
            data_table.extend(bytes(max_length - len(encoded)))
            data_length = len(encoded)
        else:
            data_table.extend(value)
            data_length = len(value)
        entries.append((key_offset, fmt, data_length, max_length, data_offset))

    key_table.extend(bytes((-len(key_table)) % 4))
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


def _write_psar(output, sheet: CueSheet, sources, title: str, disc_id: str):
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
    config = _psp_config_for_id(disc_id)
    id_table[0x20:0x20 + len(config)] = config
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
        payload = _encode_psar_block(block)

        entry = bytearray(32)
        struct.pack_into("<IH", entry, 0, data_offset, len(payload))
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


def _write_startdat(output) -> None:
    start_header = bytearray(_read_asset("startdat_header.bin"))
    logo = _read_asset("pops_logo.png")
    struct.pack_into("<I", start_header, 20, len(logo))
    output.write(start_header)
    output.write(logo)
    output.write(_read_asset("startdat_footer.bin"))


def _write_multidisc_psar(output, discs, title: str, disc_id: str) -> None:
    psar_start = output.tell()
    title_header = bytearray(_read_asset("pstitle_header.bin"))
    if len(title_header) != 0x400:
        raise RuntimeError("The multi-disc PBP title header has an invalid size.")
    output.write(title_header)

    disc_offsets = []
    for sheet, sources in discs:
        disc_start = _round_up(output.tell(), 0x8000)
        disc_offset = disc_start - psar_start
        if disc_offset > 0xFFFFFFFF:
            raise ValueError("The multi-disc PBP exceeds its 32-bit disc offset limit.")
        output.write(bytes(disc_start - output.tell()))
        disc_offsets.append(disc_offset)
        _write_psar(output, sheet, sources, title, disc_id)

    startdat_offset = _round_up(output.tell(), 0x10)
    if startdat_offset - psar_start > 0xFFFFFFFF:
        raise ValueError("The multi-disc PBP exceeds its 32-bit trailer offset limit.")
    output.write(bytes(startdat_offset - output.tell()))
    title_header[:16] = b"PSTITLEIMG000000"
    struct.pack_into("<I", title_header, 0x10, startdat_offset - psar_start)
    for index, disc_offset in enumerate(disc_offsets):
        struct.pack_into("<I", title_header, 0x200 + index * 4, disc_offset)
    struct.pack_into(
        "<I", title_header, 0x284, startdat_offset - psar_start + 0x2D31
    )
    title_header[0x264:0x26F] = ("_" + disc_id[:4] + "_" + disc_id[4:]).encode("ascii")
    title_header[0x30C:0x30C + len(title.encode("utf-8"))] = title.encode("utf-8")
    output.seek(psar_start)
    output.write(title_header)
    output.seek(startdat_offset)
    _write_startdat(output)


def write_pbp(
    cue_path: Path,
    uploaded_sources: dict[str, Path],
    output_path: Path,
    title: str,
    game_id: str,
    images: dict[str, bytes],
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Enter a game title.")
    if len(title.encode("utf-8")) > 127:
        raise ValueError("Game title must be 127 UTF-8 bytes or fewer.")
    disc_id = _validate_game_id(game_id)

    sheet = parse_cue(cue_path)
    sources = resolve_source_files(sheet, uploaded_sources)
    sfo = _build_sfo(title, disc_id)

    resources = [
        sfo,
        images.get("icon", b""),
        b"",
        images.get("thumbnail", b""),
        images.get("background", b""),
        images.get("snd0", b""),
        _read_asset("data_psp.bin"),
    ]
    offsets = [0x28]
    for resource in resources:
        offsets.append(offsets[-1] + len(resource))
    psar_offset = _round_up(offsets[6] + POP_FE_DATAPSP_SIZE, 0x10000)
    header = struct.pack("<10I", 0x50425000, 0x00010000, *offsets[:7], psar_offset)

    with output_path.open("wb+") as output:
        output.write(header)
        for resource in resources:
            output.write(resource)
        output.write(bytes(psar_offset - output.tell()))
        _write_multidisc_psar(
            output, [(sheet, sources)], title, disc_id
        )


def write_multidisc_pbp(
    cue_paths: list[Path],
    output_path: Path,
    title: str,
    game_id: str,
    images: dict[str, bytes],
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Enter a game title.")
    if len(title.encode("utf-8")) > 127:
        raise ValueError("Game title must be 127 UTF-8 bytes or fewer.")
    disc_id = _validate_game_id(game_id)
    if not 1 <= len(cue_paths) <= MAX_DISCS:
        raise ValueError("Select between one and five CUE sheets.")

    discs = []
    seen_paths = set()
    for cue_path in cue_paths:
        resolved_path = cue_path.resolve()
        if resolved_path in seen_paths:
            raise ValueError("The same CUE sheet was selected more than once.")
        seen_paths.add(resolved_path)
        if resolved_path.suffix.casefold() != ".cue" or not resolved_path.is_file():
            raise ValueError(f"Selected disc is not a readable CUE sheet: {cue_path.name}")
        sheet = parse_cue(resolved_path)
        sources = resolve_sibling_source_files(resolved_path, sheet)
        discs.append((sheet, sources))

    _write_prepared_pbp(discs, output_path, title, disc_id, images)


def write_uploaded_multidisc_pbp(
    disc_sources: list[tuple[Path, dict[str, Path]]],
    output_path: Path,
    title: str,
    game_id: str,
    images: dict[str, bytes],
) -> None:
    title = title.strip()
    if not title:
        raise ValueError("Enter a game title.")
    if len(title.encode("utf-8")) > 127:
        raise ValueError("Game title must be 127 UTF-8 bytes or fewer.")
    disc_id = _validate_game_id(game_id)
    if not 1 <= len(disc_sources) <= MAX_DISCS:
        raise ValueError("Select between one and five CUE/BIN disc sets.")

    discs = []
    for cue_path, uploaded_sources in disc_sources:
        if not cue_path.is_file():
            raise ValueError(f"Selected disc CUE upload is missing: {cue_path.name}")
        sheet = parse_cue(cue_path)
        sources = resolve_source_files(sheet, uploaded_sources)
        discs.append((sheet, sources))

    _write_prepared_pbp(discs, output_path, title, disc_id, images)


def _write_prepared_pbp(
    discs: list[tuple[CueSheet, dict[str, Path]]],
    output_path: Path,
    title: str,
    disc_id: str,
    images: dict[str, bytes],
) -> None:
    sfo = _build_sfo(title, disc_id)
    resources = [
        sfo,
        images.get("icon", b""),
        b"",
        images.get("thumbnail", b""),
        images.get("background", b""),
        images.get("snd0", b""),
        _read_asset("data_psp.bin"),
    ]
    offsets = [0x28]
    for resource in resources:
        offsets.append(offsets[-1] + len(resource))
    psar_offset = _round_up(offsets[6] + POP_FE_DATAPSP_SIZE, 0x10000)
    header = struct.pack("<10I", 0x50425000, 0x00010000, *offsets[:7], psar_offset)

    with output_path.open("wb+") as output:
        output.write(header)
        for resource in resources:
            output.write(resource)
        output.write(bytes(psar_offset - output.tell()))
        _write_multidisc_psar(output, discs, title, disc_id)
