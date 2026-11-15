#!/usr/bin/env python3
"""
Rebuild aligned frames, difference frames and candidates.json for saved
targets, using only the FITS files already stored in spherex_data.
No downloads, and no FITS files are changed or deleted.

Usage:
  python reprocess_targets.py                      # every saved target
  python reprocess_targets.py ra_269.44627_dec_+4.76820   # just one
  python reprocess_targets.py --cutout 0.12        # cutout size used for those scans
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import spherex_download_fits as sdf  # noqa: E402

DATA = ROOT / "spherex_data"
KEY_RE = re.compile(r"ra_([0-9]+(?:\.[0-9]+)?)_dec_([+-][0-9]+(?:\.[0-9]+)?)")


def old_index(folder: Path) -> dict:
    """Existing frames_index.csv rows, keyed by FITS file name."""
    path = folder / "frames_index.csv"
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {row.get("fits_file", ""): row for row in csv.DictReader(handle)}


def reprocess(folder: Path, cutout: float) -> bool:
    match = KEY_RE.fullmatch(folder.name)
    ra, dec = float(match.group(1)), float(match.group(2))
    fits_files = sorted(
        p for p in (folder / "fits").glob("*")
        if p.is_file() and p.name.lower().endswith(sdf.FITS_ENDINGS)
    )
    print(f"\n{folder.name}: {len(fits_files)} FITS files")
    if not fits_files:
        print("  No FITS files; skipped.")
        return False

    previous = old_index(folder)
    frames_dir = folder / "frames"
    diffs_dir = folder / "diffs"

    # Keep the original PNGs the first time, just in case
    backup = folder / "frames_original"
    if frames_dir.exists() and not backup.exists():
        shutil.copytree(frames_dir, backup)
        print(f"  Original frames backed up to {backup.name}/")

    for directory in (frames_dir, diffs_dir):
        directory.mkdir(exist_ok=True)
        for old_png in directory.glob("*.png"):
            old_png.unlink()

    grid_wcs, n = sdf.target_grid(ra, dec, cutout)
    records, aligned = [], {}
    for path in fits_files:
        prev = previous.get(path.name, {})
        frame_path = frames_dir / f"frame_{len(records) + 1:04d}.png"
        try:
            record, array = sdf.make_frame(path, frame_path, grid_wcs, n)
        except Exception as exc:
            print(f"  {path.name}: {exc}")
            continue
        if not record:
            continue
        record["collection"] = prev.get("collection") or "_".join(path.name.split("_")[:2])
        record["source_url"] = prev.get("source_url") or "local"
        record["mjd"] = prev.get("mjd", "")
        records.append(record)
        aligned[record["frame_file"]] = array

    if not records:
        print("  No usable frames could be made; skipped.")
        return False

    result = sdf.find_movers(records, aligned, grid_wcs, n, diffs_dir, ra, dec)
    sdf.write_json(folder / "candidates.json", result)
    sdf.write_index(folder / "frames_index.csv", records)

    progress = sdf.Progress(folder)
    progress.update(stage="done", total=len(records), done=len(records),
                    sources={"s3": 0, "irsa": 0, "cached": len(records)}, message="")

    slow = sum(1 for t in result["tracks"] if t["kind"] == "slow mover")
    print(f"  Frames: {len(records)} · passes: {len(result.get('passes', []))} · "
          f"slow movers: {slow} · fast movers: {len(result['tracks']) - slow} · "
          f"proper-motion candidates: {len(result['pairs'])}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Rebuild frames, diffs and candidates from local FITS files.")
    parser.add_argument("targets", nargs="*", help="Target folder names (default: all saved targets)")
    parser.add_argument("--cutout", type=float, default=0.12, help="Cutout size in degrees used for these scans")
    args = parser.parse_args()

    folders = [DATA / t for t in args.targets] if args.targets else sorted(
        p for p in DATA.iterdir() if p.is_dir() and KEY_RE.fullmatch(p.name)
    )
    done = 0
    for folder in folders:
        if not folder.is_dir() or not KEY_RE.fullmatch(folder.name):
            print(f"\n{folder.name}: not a saved target folder; skipped.")
            continue
        try:
            done += reprocess(folder, args.cutout)
        except Exception as exc:
            print(f"  FAILED: {exc}")
    print(f"\nFinished: {done} of {len(folders)} targets rebuilt.")


if __name__ == "__main__":
    main()