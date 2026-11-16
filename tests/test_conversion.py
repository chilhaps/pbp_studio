import hashlib
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from cue_sheet import (
    SECTOR_SIZE,
    image_sector_count,
    iter_image_segments,
    parse_cue,
    resolve_sibling_source_files,
    resolve_source_files,
    track_start_sectors,
)
from pbp_writer import (
    BLOCK_SIZE,
    write_multidisc_pbp,
    write_pbp,
    write_uploaded_multidisc_pbp,
)
from server import Upload, _resolve_uploaded_disc


class CueSheetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def create_cue(self, content: str) -> Path:
        path = self.root / "game.cue"
        path.write_text(content, encoding="utf-8")
        return path

    def create_bin(self, name: str, sectors: int, fill: bytes) -> Path:
        path = self.root / name
        path.write_bytes(fill * (sectors * SECTOR_SIZE // len(fill)))
        return path

    def test_multi_file_tracks_and_virtual_pregap(self):
        cue = self.create_cue(
            'FILE "first.bin" BINARY\n'
            "  TRACK 01 MODE1/2352\n"
            "    INDEX 01 00:00:00\n"
            'FILE "second.bin" BINARY\n'
            "  TRACK 02 AUDIO\n"
            "    PREGAP 00:00:02\n"
            "    INDEX 01 00:00:01\n"
        )
        first = self.create_bin("first.bin", 4, b"A")
        second = self.create_bin("second.bin", 3, b"B")

        sheet = parse_cue(cue)
        sources = resolve_source_files(sheet, {"first.bin": first, "second.bin": second})

        self.assertEqual(image_sector_count(sheet, sources), 9)
        self.assertEqual(track_start_sectors(sheet, sources), {1: 0, 2: 7})
        image = b"".join(iter_image_segments(sheet, sources))
        self.assertEqual(len(image), 9 * SECTOR_SIZE)
        self.assertEqual(image[:4 * SECTOR_SIZE], b"A" * (4 * SECTOR_SIZE))
        self.assertEqual(image[4 * SECTOR_SIZE:5 * SECTOR_SIZE], b"B" * SECTOR_SIZE)
        self.assertEqual(image[5 * SECTOR_SIZE:7 * SECTOR_SIZE], bytes(2 * SECTOR_SIZE))
        self.assertEqual(image[7 * SECTOR_SIZE:], b"B" * (2 * SECTOR_SIZE))

    def test_rejects_non_raw_sector_size_and_bad_index(self):
        cue = self.create_cue(
            'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2048\nINDEX 01 00:00:00\n'
        )
        self.assertRaisesRegex(ValueError, "Unsupported track mode", parse_cue, cue)

        cue.write_text(
            'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
            encoding="utf-8",
        )
        image = self.root / "disc.bin"
        image.write_bytes(bytes(SECTOR_SIZE + 1))
        sheet = parse_cue(cue)
        self.assertRaisesRegex(
            ValueError, "raw 2352-byte-sector BIN", resolve_source_files, sheet, {"disc.bin": image}
        )

    def test_resolves_bin_sibling_case_insensitively(self):
        cue = self.create_cue(
            'FILE "DISC.BIN" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n'
        )
        image = self.create_bin("disc.bin", 2, b"A")

        sheet = parse_cue(cue)

        self.assertEqual(resolve_sibling_source_files(cue, sheet), {"DISC.BIN": image})


class UploadedDiscTests(unittest.TestCase):
    def test_validates_one_uploaded_cue_and_all_referenced_bins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "cue.upload"
            image = root / "image.upload"
            cue.write_text(
                'FILE "DISC.BIN" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            image.write_bytes(bytes(20 * SECTOR_SIZE))
            uploads = {
                "cue-id": Upload(cue, "disc.cue", cue.stat().st_size),
                "bin-id": Upload(image, "disc.bin", image.stat().st_size),
            }

            disc = _resolve_uploaded_disc(["cue-id", "bin-id"], uploads, threading.Lock())

            self.assertEqual(disc.name, "disc.cue")
            self.assertEqual(disc.track_count, 1)
            self.assertEqual(list(disc.sources), ["DISC.BIN"])

    def test_rejects_extra_bin_or_multiple_cue_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "cue.upload"
            image = root / "image.upload"
            extra_image = root / "extra.upload"
            second_cue = root / "second-cue.upload"
            cue.write_text(
                'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            second_cue.write_text(cue.read_text(encoding="utf-8"), encoding="utf-8")
            image.write_bytes(bytes(20 * SECTOR_SIZE))
            extra_image.write_bytes(bytes(20 * SECTOR_SIZE))
            uploads = {
                "cue-id": Upload(cue, "disc.cue", cue.stat().st_size),
                "bin-id": Upload(image, "disc.bin", image.stat().st_size),
                "extra-bin-id": Upload(extra_image, "extra.bin", extra_image.stat().st_size),
                "second-cue-id": Upload(second_cue, "disc2.cue", second_cue.stat().st_size),
            }

            with self.assertRaisesRegex(ValueError, "not referenced"):
                _resolve_uploaded_disc(
                    ["cue-id", "bin-id", "extra-bin-id"], uploads, threading.Lock()
                )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                _resolve_uploaded_disc(
                    ["cue-id", "bin-id", "second-cue-id"], uploads, threading.Lock()
                )


class PbpWriterTests(unittest.TestCase):
    def _create_disc(self, root: Path, folder: str, fill: int) -> Path:
        disc_folder = root / folder
        disc_folder.mkdir()
        cue = disc_folder / f"{folder}.cue"
        image_path = disc_folder / f"{folder}.bin"
        cue.write_text(
            f'FILE "{image_path.name}" BINARY\n'
            "TRACK 01 MODE1/2352\n"
            "INDEX 01 00:00:00\n",
            encoding="utf-8",
        )
        image_path.write_bytes(bytes((fill,)) * (20 * SECTOR_SIZE))
        return cue

    def test_writes_sections_psar_indexes_and_startdat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "game.cue"
            image_path = root / "disc.bin"
            output_path = root / "game.pbp"
            cue.write_text(
                'FILE "disc.bin" BINARY\n'
                "TRACK 01 MODE1/2352\n"
                "INDEX 01 00:00:00\n",
                encoding="utf-8",
            )
            raw = bytearray((index * 37 + 11) % 256 for index in range(20 * SECTOR_SIZE))
            struct.pack_into("<I", raw, 16 * SECTOR_SIZE + 104, 20)
            image_path.write_bytes(raw)
            artwork = {
                "icon": b"icon-resource",
                "thumbnail": b"thumbnail-resource",
                "background": b"background-resource",
            }

            write_pbp(
                cue,
                {"disc.bin": image_path},
                output_path,
                "Test Game",
                "SLUS-12345",
                artwork,
            )

            output = output_path.read_bytes()
            header = struct.unpack_from("<10I", output)
            self.assertEqual(header[0], 0x50425000)
            self.assertEqual(header[1], 0x10000)
            self.assertEqual(list(header[2:]), sorted(header[2:]))
            sfo_offset, icon_offset, _, thumbnail_offset, background_offset, _, _, psar_offset = header[2:]
            self.assertEqual(output[icon_offset:thumbnail_offset], artwork["icon"])
            self.assertEqual(output[thumbnail_offset:background_offset], artwork["thumbnail"])
            self.assertIn(artwork["background"], output[background_offset:psar_offset])
            self.assertEqual(output[psar_offset:psar_offset + 12], b"PSISOIMG0000")
            toc_offset = psar_offset + 0x800
            self.assertEqual(output[toc_offset + 27:toc_offset + 30], bytes((0, 0, 0x20)))
            self.assertEqual(output[toc_offset + 33:toc_offset + 36], bytes((0, 0, 0)))
            self.assertEqual(output[toc_offset + 37:toc_offset + 40], bytes((0, 2, 0)))

            psar_size = struct.unpack_from("<I", output, psar_offset + 12)[0]
            self.assertGreater(psar_size, 0)
            block_count = (len(raw) + BLOCK_SIZE - 1) // BLOCK_SIZE
            self.assertEqual(block_count, 2)
            decoded = bytearray()
            for block_number in range(block_count):
                index = psar_offset + 0x4000 + block_number * 32
                data_offset, data_length = struct.unpack_from("<IH", output, index)
                block_hash = output[index + 8:index + 24]
                payload = output[psar_offset + 0x100000 + data_offset:
                                 psar_offset + 0x100000 + data_offset + data_length]
                block = payload if data_length == BLOCK_SIZE else zlib.decompress(payload, -15)
                self.assertEqual(len(block), BLOCK_SIZE)
                self.assertEqual(block_hash, hashlib.sha1(block).digest()[:16])
                decoded.extend(block)
            self.assertEqual(decoded[:len(raw)], raw)
            self.assertEqual(decoded[len(raw):], bytes(len(decoded) - len(raw)))
            self.assertEqual(struct.unpack_from("<I", decoded, BLOCK_SIZE + 104)[0], 20)

            startdat = output.find(b"STARTDAT", psar_offset + 0x100000)
            self.assertNotEqual(startdat, -1)
            self.assertEqual(struct.unpack_from("<I", output, startdat + 16)[0], 0x50)
            self.assertGreater(struct.unpack_from("<I", output, startdat + 20)[0], 0)
            self.assertGreater(sfo_offset, 0)

    def test_rejects_invalid_game_id_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "game.cue"
            image_path = root / "disc.bin"
            output_path = root / "game.pbp"
            cue.write_text(
                'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            image_path.write_bytes(bytes(20 * SECTOR_SIZE))
            with self.assertRaisesRegex(ValueError, "Game ID"):
                write_pbp(cue, {"disc.bin": image_path}, output_path, "Test", "invalid", {})
            self.assertFalse(output_path.exists())

    def test_writes_multi_disc_offsets_psars_and_single_startdat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue_paths = [
                self._create_disc(root, "first", 0x41),
                self._create_disc(root, "second", 0x42),
            ]
            output_path = root / "multi.pbp"

            write_multidisc_pbp(
                cue_paths, output_path, "Test Game", "SLUS-12345", {}
            )

            output = output_path.read_bytes()
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            self.assertEqual(output[psar_offset:psar_offset + 16], b"PSTITLEIMG000000")
            startdat_offset = struct.unpack_from("<I", output, psar_offset + 0x10)[0]
            disc_offsets = struct.unpack_from("<5I", output, psar_offset + 0x200)
            self.assertEqual(disc_offsets[0] % 0x8000, 0)
            self.assertEqual(disc_offsets[1] % 0x8000, 0)
            self.assertGreater(disc_offsets[1], disc_offsets[0])
            self.assertEqual(disc_offsets[2:], (0, 0, 0))
            self.assertEqual(
                output[psar_offset + 0x264:psar_offset + 0x26F],
                b"_SLUS_12345",
            )
            self.assertEqual(
                output[psar_offset + 0x30C:psar_offset + 0x315],
                b"Test Game",
            )
            self.assertEqual(output.count(b"STARTDAT"), 1)
            self.assertEqual(output[psar_offset + startdat_offset:][:8], b"STARTDAT")

            for offset, fill in zip(disc_offsets[:2], (0x41, 0x42)):
                disc_psar = psar_offset + offset
                self.assertEqual(output[disc_psar:disc_psar + 12], b"PSISOIMG0000")
                data_offset, data_length = struct.unpack_from(
                    "<IH", output, disc_psar + 0x4000
                )
                payload_start = disc_psar + 0x100000 + data_offset
                payload = output[payload_start:payload_start + data_length]
                block = payload if data_length == BLOCK_SIZE else zlib.decompress(payload, -15)
                self.assertEqual(block[:SECTOR_SIZE], bytes((fill,)) * SECTOR_SIZE)

    def test_one_selected_disc_keeps_single_disc_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = self._create_disc(root, "only", 0x41)
            output_path = root / "single.pbp"

            write_multidisc_pbp([cue], output_path, "Single Disc", "SLUS-12345", {})

            output = output_path.read_bytes()
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            self.assertEqual(output[psar_offset:psar_offset + 12], b"PSISOIMG0000")
            self.assertNotIn(b"PSTITLEIMG000000", output[psar_offset:psar_offset + 0x4000])
            self.assertEqual(output.count(b"STARTDAT"), 1)

    def test_uploaded_disc_writer_accepts_staged_file_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "cue-uuid.upload"
            image = root / "bin-uuid.upload"
            output_path = root / "uploaded.pbp"
            cue.write_text(
                'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            image.write_bytes(bytes(20 * SECTOR_SIZE))

            write_uploaded_multidisc_pbp(
                [(cue, {"disc.bin": image})],
                output_path,
                "Uploaded Disc",
                "SLUS-12345",
                {},
            )

            output = output_path.read_bytes()
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            self.assertEqual(output[psar_offset:psar_offset + 12], b"PSISOIMG0000")

    def test_rejects_more_than_five_disc_selections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_path = root / "too-many.pbp"
            cue_paths = [root / f"disc-{index}.cue" for index in range(6)]

            with self.assertRaisesRegex(ValueError, "one and five"):
                write_multidisc_pbp(
                    cue_paths, output_path, "Test Game", "SLUS-12345", {}
                )
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
