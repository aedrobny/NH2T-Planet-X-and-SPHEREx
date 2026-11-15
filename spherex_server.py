#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import sys
import os
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "spherex_data"
DOWNLOADER = ROOT / "spherex_download_fits.py"
HOST, PORT = "0.0.0.0", int(os.environ.get("PORT", 8000))
LOCK = threading.Lock()
JOBS: set[str] = set()
KEY_RE = re.compile(r"ra_[0-9]+(?:\.[0-9]+)?_dec_[+-][0-9]+(?:\.[0-9]+)?")

PRELOADED_FILE = ROOT / "preloaded_targets.json"


def load_preloaded_targets() -> dict:
    try:
        data = json.loads(PRELOADED_FILE.read_text(encoding="utf-8"))
        return {
            target["key"]: target["name"]
            for target in data.get("targets", [])
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[viewer] Could not load preloaded targets: {exc}")
        return {}


def coord_key(ra: float, dec: float) -> str:
    return f"ra_{ra:.5f}_dec_{dec:+.5f}"


def read_progress(folder: Path) -> dict | None:
    """Live progress written by the downloader, if any."""
    try:
        return json.loads((folder / "progress.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def target_info(folder: Path) -> dict:
    fits_dir, frames_dir = folder / "fits", folder / "frames"
    fits_count = sum(1 for p in fits_dir.iterdir() if p.is_file() and ".fit" in p.name.lower()) if fits_dir.exists() else 0
    frames = sorted(p.name for p in frames_dir.glob("*.png")) if frames_dir.exists() else []
    has_index = (folder / "frames_index.csv").is_file()
    progress = read_progress(folder)

    if folder.name in JOBS:
        status = "running"
    elif frames and has_index:
        status = "ready"
    elif progress and progress.get("stage") in {"error", "no_data"}:
        status = "error"
    elif fits_count:
        status = "needs_processing"
    else:
        status = "not_started"

    match = re.fullmatch(r"ra_([0-9]+(?:\.[0-9]+)?)_dec_([+-][0-9]+(?:\.[0-9]+)?)", folder.name)
    
    preloaded_names = load_preloaded_targets()
    display_name = preloaded_names.get(folder.name, folder.name)

    return {
        "key": folder.name,
        "display_name": display_name,
        "ra": float(match.group(1)) if match else None,
        "dec": float(match.group(2)) if match else None,
        "status": status,
        "fits_count": fits_count,
        "frame_count": len(frames),
        "frames": frames,
        "progress": progress,
        "index_url": f"/spherex_data/{folder.name}/frames_index.csv" if has_index else None,
    }


def run_downloader(key: str, ra: float, dec: float) -> None:
    folder = DATA / key
    try:
        command = [
            sys.executable, str(DOWNLOADER),
            "--ra", str(ra), "--dec", str(dec),
            "--out", str(folder),
        ]
        # Output stays in the server terminal; progress.json feeds the web page.
        subprocess.run(command, cwd=ROOT, check=False)
    finally:
        with LOCK:
            JOBS.discard(key)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[viewer] " + fmt % args)

    def send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/targets":
            DATA.mkdir(exist_ok=True)
            targets = [target_info(p) for p in sorted(DATA.iterdir()) if p.is_dir() and KEY_RE.fullmatch(p.name)]
            return self.send_json({"targets": targets})
        if path.startswith("/api/target/"):
            key = path.rsplit("/", 1)[-1]
            if not KEY_RE.fullmatch(key):
                return self.send_json({"error": "Invalid coordinate folder name."}, 400)
            folder = DATA / key
            if not folder.is_dir():
                return self.send_json({"error": "Target not found."}, 404)
            return self.send_json(target_info(folder))
        return super().do_GET()

    def do_POST(self):
        if unquote(urlparse(self.path).path) != "/api/scan":
            return self.send_json({"error": "Unknown endpoint."}, 404)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size) or b"{}")
            ra, dec = float(payload["ra"]), float(payload["dec"])
            if not (0 <= ra < 360 and -90 <= dec <= 90):
                raise ValueError("RA must be from 0 to less than 360; Dec must be from -90 to +90.")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)

        DATA.mkdir(exist_ok=True)
        key = coord_key(ra, dec)
        folder = DATA / key
        (folder / "fits").mkdir(parents=True, exist_ok=True)
        (folder / "frames").mkdir(parents=True, exist_ok=True)
        with LOCK:
            if key in JOBS:
                return self.send_json({"key": key, "status": "running"}, 202)
            JOBS.add(key)
            threading.Thread(target=run_downloader, args=(key, ra, dec), daemon=True).start()
        return self.send_json({"key": key, "status": "running", "folder": f"spherex_data/{key}"}, 202)


if __name__ == "__main__":
    if not DOWNLOADER.is_file():
        raise SystemExit(f"Downloader not found: {DOWNLOADER}")
    DATA.mkdir(exist_ok=True)
    print(f"Open http://127.0.0.1:{PORT}/index.html")
    print("Leave this terminal open while using the viewer. Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()