import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from cue_sheet import (
    SECTOR_SIZE,
    image_sector_count,
    iter_image_segments,
    parse_cue,
    resolve_source_files,
    track_start_sectors,
)
from pbp_writer import BLOCK_SIZE, write_pbp


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


class PbpWriterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
