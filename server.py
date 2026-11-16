import json
import re
import secrets
import shutil
import tempfile
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cue_sheet import parse_cue, resolve_source_files
from pbp_writer import (
    MAX_DISCS,
    RECOMMENDED_COMPRESSION_LEVEL,
    write_uploaded_multidisc_pbp,
)


ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
MAX_UPLOAD_SIZE = 8 * 1024**3
MAX_IMAGE_SIZE = 20 * 1024**2
UPLOAD_CHUNK = 1024 * 1024
ARTWORK_LABELS = {
    "icon": "Game cover",
    "thumbnail": "Logo",
    "background": "Background",
}


@dataclass
class Upload:
    path: Path
    name: str
    size: int


@dataclass
class UploadedDisc:
    cue_path: Path
    sources: dict[str, Path]
    name: str
    track_count: int


def _safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip()
    if not name or name in (".", "..") or "\0" in name:
        raise ValueError("Invalid uploaded filename.")
    return name


def _safe_output_name(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", title).strip(" .-")
    return (name or "PSX-Game") + ".pbp"


def _verify_png(data: bytes, kind: str) -> None:
    dimensions = {
        "icon": (80, 80),
        "thumbnail": (310, 180),
        "background": (480, 272),
    }
    if len(data) > MAX_IMAGE_SIZE or len(data) < 24:
        raise ValueError(f"{ARTWORK_LABELS[kind]} PNG must be between 24 bytes and 20 MB.")
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"{ARTWORK_LABELS[kind]} must be a PNG image.")
    actual = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
    if actual != dimensions[kind]:
        width, height = dimensions[kind]
        raise ValueError(
            f"{ARTWORK_LABELS[kind]} must be resized to {width} x {height} pixels."
        )


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _resolve_uploaded_disc(source_ids, uploads: dict[str, Upload], uploads_lock) -> UploadedDisc:
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError("Select one CUE sheet and its BIN file(s) for each disc.")
    if any(not isinstance(asset_id, str) for asset_id in source_ids):
        raise ValueError("Invalid disc file selection.")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("A disc file was selected more than once.")

    with uploads_lock:
        selected = [uploads.get(asset_id) for asset_id in source_ids]
    if any(upload is None for upload in selected):
        raise ValueError("A disc file upload expired. Select the files again.")

    named_uploads = [(upload, _safe_filename(upload.name)) for upload in selected]
    names = [name.casefold() for _, name in named_uploads]
    if len(set(names)) != len(names):
        raise ValueError("A disc contains files with duplicate names.")
    cue_uploads = [
        upload for upload, name in named_uploads if Path(name).suffix.casefold() == ".cue"
    ]
    bin_uploads = [
        upload for upload, name in named_uploads if Path(name).suffix.casefold() == ".bin"
    ]
    if len(cue_uploads) != 1 or not bin_uploads or len(named_uploads) != len(cue_uploads) + len(bin_uploads):
        raise ValueError("Choose exactly one .cue sheet and all of its .bin file(s).")

    cue_upload = cue_uploads[0]
    sheet = parse_cue(cue_upload.path)
    uploaded_sources = {upload.name: upload.path for upload in bin_uploads}
    sources = resolve_source_files(sheet, uploaded_sources)
    used_names = {name.casefold() for name in sources}
    selected_names = {upload.name.casefold() for upload in bin_uploads}
    if used_names != selected_names:
        raise ValueError("The selection includes a BIN file not referenced by the CUE sheet.")

    return UploadedDisc(cue_upload.path, sources, cue_upload.name, len(sheet.tracks))


def _create_handler(token: str, uploads: dict[str, Upload], uploads_lock, staging: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PBPStudio/1.0"

        def _is_allowed_host(self) -> bool:
            host = self.headers.get("Host", "")
            return host in (
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            )

        def _is_allowed_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return origin in (
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            )

        def _authorize(self) -> bool:
            if not self._is_allowed_host() or not self._is_allowed_origin():
                self._send_json(403, {"error": "Requests must come from this local app."})
                return False
            if not secrets.compare_digest(self.headers.get("X-App-Token", ""), token):
                self._send_json(403, {"error": "This app session has expired; restart PBP Studio."})
                return False
            return True

        def _send_json(self, status: int, value):
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_ui_file(self, name: str, content_type: str):
            path = UI_DIR / name
            data = path.read_bytes()
            if name == "index.html":
                data = data.replace(b"__APP_TOKEN__", token.encode("ascii"))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._is_allowed_host():
                self._send_json(403, {"error": "PBP Studio only serves local requests."})
                return
            if self.path == "/":
                self._send_ui_file("index.html", "text/html; charset=utf-8")
            elif self.path == "/app.css":
                self._send_ui_file("app.css", "text/css; charset=utf-8")
            elif self.path == "/app.js":
                self._send_ui_file("app.js", "text/javascript; charset=utf-8")
            elif self.path == "/favicon.ico":
                self.send_error(404)
            else:
                self._send_json(404, {"error": "Not found."})

        def do_PUT(self):
            if not self._authorize():
                return
            if self.path != "/api/uploads":
                self._send_json(404, {"error": "Not found."})
                return
            try:
                name = _safe_filename(
                    urllib.parse.unquote(self.headers.get("X-File-Name", ""))
                )
                length_text = self.headers.get("Content-Length")
                if not length_text or not length_text.isdigit():
                    raise ValueError("The upload size could not be determined.")
                length = int(length_text)
                if length <= 0 or length > MAX_UPLOAD_SIZE:
                    raise ValueError("Each uploaded file must be smaller than 8 GB.")
                asset_id = uuid.uuid4().hex
                path = staging / f"{asset_id}.upload"
                remaining = length
                with path.open("wb") as target:
                    while remaining:
                        chunk = self.rfile.read(min(UPLOAD_CHUNK, remaining))
                        if not chunk:
                            raise ValueError(f"Upload ended early: {name}")
                        target.write(chunk)
                        remaining -= len(chunk)
                upload = Upload(path, name, length)
                with uploads_lock:
                    uploads[asset_id] = upload
                self._send_json(201, {"id": asset_id, "name": name, "size": length})
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
            except OSError as error:
                self._send_json(500, {"error": f"Could not store upload: {error}"})

        def do_POST(self):
            if not self._authorize():
                return
            if self.path not in ("/api/discs", "/api/convert"):
                self._send_json(404, {"error": "Not found."})
                return
            try:
                request = self._read_json_request()
                if self.path == "/api/discs":
                    self._validate_disc(request)
                else:
                    self._convert(request)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                self._send_json(400, {"error": str(error) or "Invalid request."})
            except Exception as error:
                traceback.print_exc()
                self._send_json(500, {"error": f"Request failed: {error}"})

        def _read_json_request(self):
            length_text = self.headers.get("Content-Length")
            if not length_text or not length_text.isdigit():
                raise ValueError("Missing request length.")
            length = int(length_text)
            if length > 1024 * 1024:
                raise ValueError("Request is too large.")
            return json.loads(self.rfile.read(length))

        def _validate_disc(self, request):
            if not isinstance(request, dict):
                raise ValueError("Invalid disc upload.")
            disc = _resolve_uploaded_disc(
                request.get("sourceIds"), uploads, uploads_lock
            )
            self._send_json(
                200,
                {
                    "name": disc.name,
                    "tracks": disc.track_count,
                    "binCount": len(disc.sources),
                    "sourceIds": request["sourceIds"],
                },
            )

        def _convert(self, request):
            if not isinstance(request, dict):
                raise ValueError("Invalid conversion request.")
            title = request.get("title")
            game_id = request.get("gameId")
            disc_selections = request.get("discs")
            image_ids = request.get("images", {})
            if not isinstance(title, str) or not isinstance(game_id, str):
                raise ValueError("Enter a game title and game ID.")
            if (
                not isinstance(disc_selections, list)
                or not 1 <= len(disc_selections) <= MAX_DISCS
            ):
                raise ValueError("Add between one and five disc uploads.")
            if not isinstance(image_ids, dict):
                raise ValueError("Invalid image resource selection.")
            if any(not isinstance(asset_id, str) for asset_id in image_ids.values()):
                raise ValueError("Invalid artwork selection.")
            if any(kind not in ("icon", "thumbnail", "background") for kind in image_ids):
                raise ValueError("Unknown artwork slot.")

            source_groups = []
            all_source_ids = []
            for selection in disc_selections:
                if not isinstance(selection, dict):
                    raise ValueError("Invalid disc upload.")
                source_ids = selection.get("sourceIds")
                if not isinstance(source_ids, list) or not source_ids:
                    raise ValueError("A disc upload has no source files.")
                if any(not isinstance(asset_id, str) for asset_id in source_ids):
                    raise ValueError("Invalid disc file selection.")
                if len(set(source_ids)) != len(source_ids):
                    raise ValueError("A disc file was selected more than once.")
                source_groups.append(source_ids)
                all_source_ids.extend(source_ids)
            if len(set(all_source_ids)) != len(all_source_ids):
                raise ValueError("A source file cannot be used by more than one disc.")

            selected_discs = [
                _resolve_uploaded_disc(source_ids, uploads, uploads_lock)
                for source_ids in source_groups
            ]
            with uploads_lock:
                selected_images = {
                    kind: uploads.get(asset_id) if asset_id else None
                    for kind, asset_id in image_ids.items()
                }
            if any(upload is None for upload in selected_images.values()):
                raise ValueError("An image upload expired. Select the images again.")

            for kind in ("icon", "thumbnail", "background"):
                image_upload = selected_images.get(kind)
                if image_upload:
                    if image_upload.size > MAX_IMAGE_SIZE:
                        raise ValueError(
                            f"{ARTWORK_LABELS[kind]} PNG must be smaller than 20 MB."
                        )

            with tempfile.TemporaryDirectory(prefix="pbp-build-") as directory:
                work = Path(directory)
                images = {}
                for kind in ("icon", "thumbnail", "background"):
                    image_upload = selected_images.get(kind)
                    if image_upload:
                        data = image_upload.path.read_bytes()
                        _verify_png(data, kind)
                        images[kind] = data

                output_path = work / "output.pbp"
                write_uploaded_multidisc_pbp(
                    [(disc.cue_path, disc.sources) for disc in selected_discs],
                    output_path,
                    title,
                    game_id,
                    images,
                    RECOMMENDED_COMPRESSION_LEVEL,
                )
                self._send_pbp(output_path, _safe_output_name(title))

        def _send_pbp(self, path: Path, filename: str):
            size = path.stat().st_size
            encoded_name = urllib.parse.quote(filename)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"game.pbp\"; filename*=UTF-8''{encoded_name}",
            )
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=UPLOAD_CHUNK)

        def log_message(self, format_string, *args):
            print(f"[PBP Studio] {self.address_string()} - {format_string % args}")

    return Handler


def main():
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="pbp-studio-") as upload_dir:
        staging = Path(upload_dir)
        uploads: dict[str, Upload] = {}
        uploads_lock = threading.Lock()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _create_handler(token, uploads, uploads_lock, staging),
        )
        url = f"http://127.0.0.1:{server.server_port}/"
        print(f"PBP Studio is running at {url}")
        print("Source files and images stay on this computer. Press Ctrl+C to stop.")
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping PBP Studio.")
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
