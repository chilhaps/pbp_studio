import hashlib
import http.server
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
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
    ASSET_DIR,
    BLOCK_SIZE,
    DEFAULT_PSP_CONFIG,
    PSP_CONFIG_MAP,
    PSP_CONFIG_OVERRIDES,
    _build_sfo,
    _encode_psar_block,
    _pop_fe_compressor_path,
    _psp_config_for_id,
    write_multidisc_pbp,
    write_pbp,
    write_uploaded_multidisc_pbp,
)
from server import (
    Download,
    Upload,
    _create_handler,
    _resolve_uploaded_disc,
    _resolve_uploaded_snd0,
)


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

    def test_resolves_optional_snd0_upload_and_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "music.upload"
            path.write_bytes(b"sample soundtrack")
            oversized_path = Path(temporary) / "large.upload"
            oversized_path.write_bytes(b"")
            uploads = {
                "snd0-id": Upload(path, "music.AT3", path.stat().st_size),
                "other-id": Upload(path, "cover.png", path.stat().st_size),
                "large-id": Upload(oversized_path, "large.snd0", 64 * 1024**2 + 1),
            }

            upload = _resolve_uploaded_snd0("snd0-id", uploads, threading.Lock())

            self.assertEqual(upload.name, "music.AT3")
            self.assertIsNone(_resolve_uploaded_snd0(None, uploads, threading.Lock()))
            with self.assertRaisesRegex(ValueError, "AT3 or .SND0"):
                _resolve_uploaded_snd0("other-id", uploads, threading.Lock())
            with self.assertRaisesRegex(ValueError, "64 MB"):
                _resolve_uploaded_snd0("large-id", uploads, threading.Lock())
            with self.assertRaisesRegex(ValueError, "expired"):
                _resolve_uploaded_snd0("missing-id", uploads, threading.Lock())


class DownloadHandlerTests(unittest.TestCase):
    def test_native_download_is_streamed_once_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_path = root / "game.pbp"
            output_path.write_bytes(b"test-pbp-content")
            download_id = "a" * 32
            downloads = {
                download_id: Download(output_path, "Test Game.pbp"),
            }
            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                _create_handler(
                    "token",
                    {},
                    threading.Lock(),
                    root,
                    downloads,
                    threading.Lock(),
                ),
            )
            server_thread = threading.Thread(target=server.serve_forever)
            server_thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/download/{download_id}"
            try:
                with urllib.request.urlopen(url) as response:
                    self.assertEqual(response.read(), b"test-pbp-content")
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                    self.assertEqual(response.headers["Content-Length"], "16")
                deadline = time.monotonic() + 1
                while output_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(output_path.exists())
                self.assertNotIn(download_id, downloads)

                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(url)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join()


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

    def test_sfo_tables_are_aligned_and_values_use_fixed_slots(self):
        sfo = _build_sfo("Test Game", "SLUS12345")
        magic, version, key_start, data_start, count = struct.unpack_from("<4sIIII", sfo)

        self.assertEqual(magic, b"\0PSF")
        self.assertEqual(version, 0x101)
        self.assertEqual(data_start % 4, 0)

        values = {}
        keys = []
        previous_data_end = 0
        for index in range(count):
            key_offset, data_type, length, max_length, data_offset = struct.unpack_from(
                "<HHIII", sfo, 0x14 + index * 0x10
            )
            key_start_offset = key_start + key_offset
            key_end = sfo.index(b"\0", key_start_offset)
            key = sfo[key_start_offset:key_end].decode("ascii")
            keys.append(key)
            value_start = data_start + data_offset
            value = sfo[value_start:value_start + max_length]

            self.assertEqual(data_offset, previous_data_end)
            self.assertGreaterEqual(max_length, length)
            self.assertEqual(len(value), max_length)
            if data_type == 0x0204:
                self.assertEqual(value[length - 1], 0)
                values[key] = value[:length - 1].decode("utf-8")
            else:
                self.assertEqual(data_type, 0x0404)
                values[key] = struct.unpack_from("<I", value)[0]
            previous_data_end = data_offset + max_length

        self.assertEqual(values["CATEGORY"], "ME")
        self.assertEqual(values["DISC_ID"], "SLUS12345")
        self.assertEqual(values["TITLE"], "Test Game")
        self.assertEqual(values["BOOTABLE"], 1)
        self.assertEqual(values["PARENTAL_LEVEL"], 3)
        self.assertEqual(values["REGION"], 0x8000)
        self.assertEqual(
            keys,
            [
                "BOOTABLE",
                "CATEGORY",
                "DISC_ID",
                "DISC_VERSION",
                "LICENSE",
                "PARENTAL_LEVEL",
                "PSP_SYSTEM_VER",
                "REGION",
                "TITLE",
            ],
        )

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
                "snd0": b"snd0-resource",
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
            self.assertEqual(psar_offset, (header[8] + 30632 + 0xFFFF) & 0xFFFF0000)
            self.assertEqual(output[icon_offset:thumbnail_offset], artwork["icon"])
            self.assertEqual(output[thumbnail_offset:background_offset], artwork["thumbnail"])
            self.assertEqual(output[header[7]:header[8]], artwork["snd0"])
            self.assertIn(artwork["background"], output[background_offset:psar_offset])
            self.assertEqual(output[psar_offset:psar_offset + 16], b"PSTITLEIMG000000")
            image_psar_offset = psar_offset + struct.unpack_from("<I", output, psar_offset + 0x200)[0]
            self.assertEqual(output[image_psar_offset:image_psar_offset + 12], b"PSISOIMG0000")
            self.assertEqual(
                output[image_psar_offset + 0x420:image_psar_offset + 0x420 + len(DEFAULT_PSP_CONFIG)],
                DEFAULT_PSP_CONFIG,
            )
            toc_offset = image_psar_offset + 0x800
            self.assertEqual(output[toc_offset + 10], 0x01)
            self.assertEqual(output[toc_offset + 27:toc_offset + 30], bytes((0, 0, 0x21)))
            self.assertEqual(output[toc_offset + 33:toc_offset + 36], bytes((0, 2, 1)))
            self.assertEqual(output[toc_offset + 37:toc_offset + 40], bytes((0, 2, 0)))

            psar_size = struct.unpack_from("<I", output, image_psar_offset + 12)[0]
            self.assertGreater(psar_size, 0)
            block_count = (len(raw) + BLOCK_SIZE - 1) // BLOCK_SIZE
            self.assertEqual(block_count, 2)
            decoded = bytearray()
            for block_number in range(block_count):
                index = image_psar_offset + 0x4000 + block_number * 32
                data_offset, data_length = struct.unpack_from("<IH", output, index)
                block_hash = output[index + 8:index + 24]
                payload = output[image_psar_offset + 0x100000 + data_offset:
                                 image_psar_offset + 0x100000 + data_offset + data_length]
                block = (
                    zlib.decompress(payload, -15)
                    if data_length < BLOCK_SIZE
                    else payload
                )
                self.assertEqual(
                    payload, _encode_psar_block(block),
                )
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

    def test_compresses_blocks_as_raw_deflate_and_falls_back_to_raw(self):
        compressible = b"A" * BLOCK_SIZE
        compressed = _encode_psar_block(compressible)
        self.assertEqual(
            hashlib.sha256(compressed).hexdigest(),
            "e955cfd7c9d85033ee37a6ab08949b9123ee383cbb62ddabf5b4904bf4da64c1",
        )
        self.assertEqual(zlib.decompress(compressed, -15), compressible)

        incompressible = hashlib.shake_256(b"raw PSAR fallback").digest(BLOCK_SIZE)
        self.assertEqual(_encode_psar_block(incompressible), incompressible)

    def test_selects_bundled_compressor_for_each_x64_platform(self):
        for system, filename in (
            ("linux", "libz_1_2_13.so"),
            ("darwin", "libz_1_2_13.dylib"),
            ("win32", "libz_1_2_13.dll"),
        ):
            with self.subTest(system=system):
                path = _pop_fe_compressor_path(system, "x86_64")
                self.assertEqual(path, ASSET_DIR / filename)
                self.assertTrue(path.is_file())

        self.assertIsNone(_pop_fe_compressor_path("linux", "aarch64"))
        self.assertIsNone(_pop_fe_compressor_path("freebsd", "x86_64"))
        self.assertEqual(
            _pop_fe_compressor_path("win32", "AMD64"),
            ASSET_DIR / "libz_1_2_13.dll",
        )

    def test_raw_fallback_keeps_the_pop_fe_index_flag_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = root / "game.cue"
            image_path = root / "disc.bin"
            output_path = root / "game.pbp"
            cue.write_text(
                'FILE "disc.bin" BINARY\nTRACK 01 MODE1/2352\nINDEX 01 00:00:00\n',
                encoding="utf-8",
            )
            raw = hashlib.shake_256(b"raw PSAR fallback").digest(20 * SECTOR_SIZE)
            image_path.write_bytes(raw)

            write_pbp(cue, {"disc.bin": image_path}, output_path, "Test", "SLUS-12345", {})

            output = output_path.read_bytes()
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            image_offset = psar_offset + struct.unpack_from(
                "<I", output, psar_offset + 0x200
            )[0]
            index_offset = image_offset + 0x4000
            data_offset, data_length = struct.unpack_from("<IH", output, index_offset)
            self.assertEqual(data_length, BLOCK_SIZE)
            self.assertEqual(output[index_offset + 6], 0)
            payload_offset = image_offset + 0x100000 + data_offset
            self.assertEqual(
                output[payload_offset:payload_offset + data_length], raw[:BLOCK_SIZE]
            )

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
            header = struct.unpack_from("<10I", output)
            self.assertEqual(header[5], header[6])
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
                block = zlib.decompress(payload, -15) if data_length < BLOCK_SIZE else payload
                self.assertEqual(block[:SECTOR_SIZE], bytes((fill,)) * SECTOR_SIZE)

    def test_one_selected_disc_uses_psp_title_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cue = self._create_disc(root, "only", 0x41)
            output_path = root / "single.pbp"

            write_multidisc_pbp([cue], output_path, "Single Disc", "SLUS-00707", {})

            output = output_path.read_bytes()
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            self.assertEqual(output[psar_offset:psar_offset + 16], b"PSTITLEIMG000000")
            image_psar_offset = psar_offset + struct.unpack_from("<I", output, psar_offset + 0x200)[0]
            self.assertEqual(image_psar_offset % 0x8000, 0)
            self.assertEqual(output[image_psar_offset:image_psar_offset + 12], b"PSISOIMG0000")
            config = output[image_psar_offset + 0x420:image_psar_offset + 0x420 + 272]
            self.assertEqual(config, PSP_CONFIG_OVERRIDES["SLUS00707"])
            self.assertEqual(
                hashlib.sha256(config).hexdigest(),
                "0e82e318e6b15fda18e7b1920fb75a7953a520ccbbb7658d6fbbe1f8674a2d49",
            )
            self.assertEqual(output.count(b"STARTDAT"), 1)

    def test_uses_bundled_pop_fe_profile_for_known_game_id(self):
        game_id = "SCES00001"
        profile_path = ASSET_DIR / "pspconfigs" / PSP_CONFIG_MAP[game_id]

        self.assertEqual(_psp_config_for_id(game_id), profile_path.read_bytes())
        self.assertNotEqual(_psp_config_for_id(game_id), DEFAULT_PSP_CONFIG)

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

            completed_disc_indexes = []
            write_uploaded_multidisc_pbp(
                [(cue, {"disc.bin": image})],
                output_path,
                "Uploaded Disc",
                "SLUS-12345",
                {
                    "thumbnail": b"logo-resource",
                    "background": b"background-resource",
                },
                on_disc_complete=lambda index: completed_disc_indexes.append(index),
            )

            self.assertEqual(completed_disc_indexes, [0])
            output = output_path.read_bytes()
            header = struct.unpack_from("<10I", output)
            pic0_offset, pic1_offset = header[5:7]
            self.assertEqual(output[pic0_offset:pic1_offset], b"logo-resource")
            self.assertEqual(
                output[pic1_offset:pic1_offset + len(b"background-resource")],
                b"background-resource",
            )
            psar_offset = struct.unpack_from("<I", output, 36)[0]
            self.assertEqual(output[psar_offset:psar_offset + 16], b"PSTITLEIMG000000")
            image_psar_offset = psar_offset + struct.unpack_from("<I", output, psar_offset + 0x200)[0]
            self.assertEqual(output[image_psar_offset:image_psar_offset + 12], b"PSISOIMG0000")

            composed_output_path = root / "composed.pbp"
            write_uploaded_multidisc_pbp(
                [(cue, {"disc.bin": image})],
                composed_output_path,
                "Composed Logo",
                "SLUS-12345",
                {"background": b"composed-background"},
            )
            composed_output = composed_output_path.read_bytes()
            composed_header = struct.unpack_from("<10I", composed_output)
            composed_pic0_offset, composed_pic1_offset = composed_header[5:7]
            self.assertEqual(composed_pic0_offset, composed_pic1_offset)
            self.assertEqual(
                composed_output[composed_pic1_offset:composed_header[7]],
                b"composed-background",
            )

    def test_uploaded_writer_can_release_each_completed_disc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discs = []
            for index in range(5):
                cue = self._create_disc(root, f"disc-{index + 1}", index + 1)
                source = cue.with_suffix(".bin")
                discs.append((cue, {source.name: source}))

            completed_disc_indexes = []

            def release_disc(index):
                completed_disc_indexes.append(index)
                cue, sources = discs[index]
                cue.unlink()
                for source in sources.values():
                    source.unlink()

            write_uploaded_multidisc_pbp(
                discs,
                root / "five-disc.pbp",
                "Five Disc Game",
                "SLUS-12345",
                {},
                on_disc_complete=release_disc,
            )

            self.assertEqual(completed_disc_indexes, [0, 1, 2, 3, 4])
            self.assertTrue(all(not cue.exists() for cue, _ in discs))
            self.assertTrue(
                all(not source.exists() for _, sources in discs for source in sources.values())
            )

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
