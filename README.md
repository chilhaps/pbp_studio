# PBP Studio

A standalone, local GUI for converting PlayStation 1 `.cue`/`.bin` disc dumps to PSP `EBOOT.PBP` files. The converter is implemented in this project and does not import, launch, or require PSXPackager or pop-fe.

## Run

Requires Python 3.10 or later and a current desktop browser. There are no Python package dependencies.

```sh
cd pbp_tool
python3 app.py
```

PBP Studio opens a browser tab and serves its interface on a temporary loopback-only port. Keep the terminal open while using it; press Ctrl+C to stop the app and clear its temporary uploads.

## Convert a game

1. Select or drop the game's `.cue` sheet and every `.bin` file referenced by it.
2. Select the CUE sheet if more than one was added, then enter a game title and a nine-character ID (four letters and five digits, such as `SLUS-12345`).
3. Optionally add an icon, thumbnail, and background by choosing an image, dropping it onto a frame, or copying an image and using Paste (then press Ctrl+V / Cmd+V).
4. Choose a compression level and build the PBP. The browser downloads the finished file.

Artwork is normalized to the PBP dimensions: icon 80 × 80, thumbnail 310 × 180, and background 480 × 272. For each image, choose **Crop** to fill the frame or **Fit** to keep the entire image (with transparent padding as needed). In Crop mode, drag the preview to reposition the image and use the zoom slider to adjust the crop. Original image files are not modified.

## Input support and limitations

- CUE sheets must be UTF-8 or ASCII and reference raw 2352-byte-sector BIN files. Multi-track and multi-BIN sheets, audio tracks, `INDEX 00`/`INDEX 01`, and virtual `PREGAP` are supported.
- Cooked 2048-byte ISO files, 2448-byte subchannel images, compressed BIN files, and other CUE sector modes are not supported.
- Track numbers must be sequential, from 1 through 99. A single CUE sheet produces one PBP.
- CUE filenames are resolved against the uploaded files by basename, case-insensitively. Include every referenced BIN file and avoid duplicate basenames.
- Large source files are streamed to disk in chunks; they are not read into memory as a whole. Conversion may take a while for large games.

## Privacy and security

The application binds only to `127.0.0.1`, uses a per-run request token, and does not make external network requests. Uploaded disc and image files are stored in a temporary directory on this computer and removed when the application exits. Do not expose the local port through a proxy or port-forward.

## Implementation and bundled resources

The CUE reader, PBP/SFO writer, PSAR block encoder, local HTTP server, and browser UI are self-contained in this repository. Four PS1 bootstrap resources in `assets/` (`data_psp.bin`, `pops_logo.png`, and the two STARTDAT fragments) are bundled from pop-fe under the GNU LGPL version 2.1; see [licenses/POP-FE-LGPL-2.1.txt](licenses/POP-FE-LGPL-2.1.txt). These resources are used locally to create compatible PBP files. No reference-project source code is imported or invoked at runtime.

The project code is licensed under the MIT License; see [LICENSE](LICENSE).

## Tests

```sh
python3 -m unittest discover -s tests -v
```
