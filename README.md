# PBP Studio

A standalone, local GUI for converting PlayStation 1 `.cue`/`.bin` disc dumps to PSP `EBOOT.PBP` files. The converter is implemented in this project and does not import, launch, or require PSXPackager or pop-fe.

## Run

Requires Python 3.10 or later and a current desktop browser. There are no Python package dependencies.

```sh
cd pbp_studio
python3 app.py
```

PBP Studio opens a browser tab and serves its interface on a temporary loopback-only port. Keep the terminal open while using it; press Ctrl+C to stop the app and clear its temporary uploads.

## Convert a game

1. Use **Add Disc** to select one `.cue` sheet and all `.bin` files it references in your browser's file picker. Each disc is uploaded and validated before you add the next one.
2. Add discs in sequence, then use the up/down controls to set disc order. Enter a game title and a nine-character ID (four letters and five digits, such as `SLUS-12345`).
3. Optionally add a game cover, logo, and background by choosing an image, dropping it onto a frame, or copying an image and clicking that frame's Paste button. Ctrl+V / Cmd+V also pastes into the active frame. You can also add an existing `.AT3`/`.SND0` file for the optional XMB audio section.
4. Build the PBP. The browser downloads the finished file directly from the local app.

Artwork is normalized to the PBP dimensions: game cover 80 × 80, logo 310 × 180, and background 480 × 272. The 384 × 216 live preview follows pop-fe-psp's composition: the background fills the frame, the logo overlays near the center, and the game cover appears as a 54 × 54 icon near the left. Choose whether to compose the logo into the exported background (PIC0 remains blank; reposition and resize it in the preview) or store it in PIC0 (default position and scale; not included in the background). For each image, choose **Crop** to fill the image frame or **Fit** to keep the entire image centered horizontally and vertically, with transparent padding as needed. In Crop mode, drag the small image preview to reposition the crop and use the zoom slider to adjust it. Original image files are not modified.

## Input support and limitations

- CUE sheets must be UTF-8 or ASCII and reference raw 2352-byte-sector BIN files. Multi-track and multi-BIN sheets, audio tracks, `INDEX 00`/`INDEX 01`, and virtual `PREGAP` are supported.
- Cooked 2048-byte ISO files, 2448-byte subchannel images, compressed BIN files, and other CUE sector modes are not supported.
- Track numbers must be sequential, from 1 through 99. A PBP can contain one through five ordered discs, each added as its own slot.
- Each disc selection must contain exactly one CUE sheet and every BIN file it references. CUE filenames are matched case-insensitively; unreferenced BIN files are rejected.
- Large source files are streamed to disk in chunks; they are not read into memory as a whole. During conversion, the PBP is written alongside uploaded files and each disc's temporary uploads are removed after that disc has been added to the output, reducing peak disk use. The finished PBP is downloaded directly from the local app rather than buffered into browser memory. If a later disc fails, the UI removes already-processed discs from the list so they can be re-added. Conversion may take a while for large games.

## Privacy and security

The application binds only to `127.0.0.1`, uses a per-run request token, and does not make external network requests. Disc files, images, and optional SND0 audio are uploaded to the local app and stored in a temporary directory on this computer; disc uploads are removed as they are converted, and remaining temporary files are removed when the application exits. Do not expose the local port through a proxy or port-forward.

## Implementation and bundled resources

The CUE reader, PBP/SFO writer, PSAR block encoder, local HTTP server, and browser UI are self-contained in this repository. PSAR blocks follow popstation's compressed path: each 0x9300-byte block is zlib-compressed at level 1, stored as raw DEFLATE (without the zlib header/trailer), and stored uncompressed if compression is not smaller. The compressor is pinned to classic zlib 1.2.13 for byte-stable output. When Python's zlib runtime differs, x86-64 Linux, macOS, and Windows use the matching bundled shared library; other systems require Python's zlib runtime to be 1.2.13. The macOS and Windows binaries are from conda-forge's x86-64 `libzlib` 1.2.13 build (build `hfd90126_4` and `hcfcfb64_4`, respectively). The Windows binary uses the Microsoft Visual C++ runtime, which is included with standard CPython Windows distributions. All bundled zlib libraries are covered by [licenses/ZLIB-1.2.13.txt](licenses/ZLIB-1.2.13.txt). PBP resources and PSP game profiles are bundled from pop-fe under the GNU LGPL version 2.1; see [licenses/POP-FE-LGPL-2.1.txt](licenses/POP-FE-LGPL-2.1.txt). The profile map covers the available per-game configurations in pop-fe's game database. These resources are used locally to create compatible PBP files. No reference-project source code is imported or invoked at runtime.

The project code is licensed under the MIT License; see [LICENSE](LICENSE).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## AI Usage Disclosure

GitHub Copilot was extensively used throughout the creation of this application.
