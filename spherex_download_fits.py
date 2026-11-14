#!/usr/bin/env python3
"""
SPHEREx DR1 + QR3 local downloader and frame maker.

Searches IRSA SIA2 (not TAP), downloads D4 spectral-image FITS cutouts,
keeps the original FITS files, and creates PNG frames plus an animated GIF.

Example:
  python3 spherex_download_frames.py --ra 269.44627 --dec 4.76820

Optional:
  --radius 0.06       Search radius in degrees (default 0.06)
  --cutout 0.12       FITS cutout width in degrees (default 0.12)
  --max-files 30      Maximum FITS files to download (default 30)
  --include-deep      Search DR1 and QR3 deep-field collections too
  --out ./spherex_data
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import re
import sys
import time
import urllib.parse
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.coordinates import SkyCoord
from astropy import units as u
from astroquery.ipac.irsa import Irsa
from astropy.table import Table
import matplotlib
matplotlib.use("Agg")
import imageio.v2 as imageio

SIA_URL = "https://irsa.ipac.caltech.edu/SIA?"
COLLECTIONS = ["spherex_dr1", "spherex_qr3"]
DEEP_COLLECTIONS = ["spherex_dr1_deep", "spherex_qr3_deep"]
USER_AGENT = "SPHEREx-local-downloader/1.0 (Python; FITS archive client)"
RETRY_STATUS = {429, 500, 502, 503, 504}


def request_with_retries(session, method, url, *, params=None, stream=False,
                         timeout=(30, 240), attempts=5):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.request(
                method, url, params=params, stream=stream,
                timeout=timeout, headers={"User-Agent": USER_AGENT}
            )
            if response.status_code in RETRY_STATUS:
                wait = min(30, 1.5 * (2 ** attempt))
                print(f"  HTTP {response.status_code}; retrying in {wait:.1f}s")
                response.close()
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            wait = min(30, 1.5 * (2 ** attempt))
            print(f"  Connection/service error: {exc}; retrying in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"Request failed after {attempts} attempts: {last_error}")


def colname(table, wanted):
    mapping = {str(name).lower(): name for name in table.colnames}
    return mapping.get(wanted.lower())


def is_d4_url(url):
    """Keep detector 4 products; filenames normally contain ...D4... and path /4/."""
    path = urllib.parse.urlparse(url).path
    name = Path(path).name.upper()
    return bool(re.search(r"D4(?:_|\.|$)", name)) or bool(re.search(r"/4/", path))


def query_collection(session, collection, ra, dec, radius):
    """Use the Astroquery SIA2 client, matching IRSA's official SPHEREx tutorial."""
    print(f"Searching {collection} around RA={ra:.6f}, Dec={dec:.6f}...")
    coord = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs")
    try:
        table = Irsa.query_sia(pos=(coord, radius * u.deg), collection=collection)
    except Exception as exc:
        raise RuntimeError(f"Astroquery SIA2 request failed: {exc}") from exc

    if table is None or len(table) == 0:
        print("  SIA2 returned zero observations.")
        return []

    names = {str(name).lower(): name for name in table.colnames}
    access_col = names.get("access_url")
    if access_col is None:
        raise RuntimeError(
            f"SIA2 table has no access_url column. Columns: {table.colnames}"
        )

    rows = []
    for row in table:
        value = row[access_col]
        if value is np.ma.masked:
            continue
        url = str(value).strip()
        if not url or url.lower() in {"--", "none", "null"}:
            continue
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = "https://irsa.ipac.caltech.edu" + url
        if not url.startswith("https://"):
            continue

        path = urllib.parse.urlparse(url).path
        filename = Path(path).name.upper()
        # SPHEREx Level-2 names encode detector as _2D4_ (D4).
        if re.search(r"_2D4(?:_|\\.|$)", filename) or re.search(r"/4/", path):
            rows.append({"collection": collection, "url": url})

    print(f"  SIA2 observations: {len(table)}; D4 candidates: {len(rows)}")
    return rows


def add_cutout(url, ra, dec, size):
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.extend([
        ("center", f"{ra:.8f},{dec:.8f}"),
        ("size", f"{size:.4f}"),
    ])
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path,
         urllib.parse.urlencode(query), parts.fragment)
    )


def safe_name(url):
    name = Path(urllib.parse.urlparse(url).path).name
    if not name.lower().endswith((".fits", ".fits.gz", ".fits.fz",
                                  ".fit", ".fit.gz", ".fit.fz",
                                  ".fts", ".fts.gz", ".fts.fz")):
        name += ".fits"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def download_fits(session, url, destination):
    if destination.exists() and destination.stat().st_size > 2880:
        try:
            with fits.open(destination, memmap=False) as hdul:
                hdul.verify("silentfix")
            print(f"  Already downloaded: {destination.name}")
            return True
        except Exception:
            print(f"  Existing file failed FITS validation; downloading again: {destination.name}")
            destination.unlink(missing_ok=True)

    response = request_with_retries(session, "GET", url, stream=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    try:
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        response.close()
        if temp.stat().st_size <= 2880:
            raise RuntimeError("Downloaded response is too small to be a FITS file.")
        with fits.open(temp, memmap=False) as hdul:
            hdul.verify("silentfix")
            if not any(hdu.data is not None for hdu in hdul):
                raise RuntimeError("FITS file contains no image/table data.")
        temp.replace(destination)
        print(f"  Saved {destination.name} ({destination.stat().st_size / 1024**2:.1f} MB)")
        return True
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def find_image_hdus(hdul):
    """Prefer SPHEREx IMAGE extensions; fall back to any 2-D image HDU."""
    preferred = []
    fallback = []
    for index, hdu in enumerate(hdul):
        data = getattr(hdu, "data", None)
        if data is None:
            continue
        arr = np.asarray(data)
        if arr.ndim != 2 or min(arr.shape) < 8:
            continue
        extname = str(hdu.header.get("EXTNAME", "")).strip().upper()
        item = (index, hdu)
        if extname == "IMAGE":
            preferred.append(item)
        elif np.issubdtype(arr.dtype, np.number):
            fallback.append(item)
    return preferred or fallback[:1]


def make_frame(fits_path, output_path):
    """
    Render the original FITS IMAGE array at its native sampling.

    This follows the notebook's clear inspection view:
      - use the actual IMAGE pixels (no reprojection or 96/128-pixel resize)
      - linear grayscale stretch using the 2nd and 99.5th percentiles
      - nearest-neighbour enlargement for display only
      - lossless PNG output
    """
    with fits.open(fits_path, memmap=False) as hdul:
        image_hdus = find_image_hdus(hdul)
        if not image_hdus:
            print(f"  No 2-D image extension found in {fits_path.name}; skipping frame.")
            return None

        index, hdu = image_hdus[0]
        data = np.asarray(hdu.data, dtype=np.float32)
        if data.ndim != 2:
            print(f"  IMAGE is not 2-D in {fits_path.name}; skipping frame.")
            return None

        finite = np.isfinite(data)
        if not finite.any():
            print(f"  Image is all non-finite: {fits_path.name}")
            return None

        # Match the notebook's native-image inspection stretch.
        lo, hi = np.nanpercentile(data[finite], [2.0, 99.5])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo = float(np.nanmin(data[finite]))
            hi = float(np.nanmax(data[finite]))
        if not np.isfinite(lo + hi) or hi <= lo:
            hi = lo + 1.0

        scaled = np.clip((data - lo) / (hi - lo), 0.0, 1.0)
        scaled[~finite] = 0.0

        # Enlarge with nearest-neighbour replication only. This makes small
        # native cutouts easier to inspect without blending or inventing detail.
        display_scale = 3
        scaled = np.repeat(np.repeat(scaled, display_scale, axis=0),
                           display_scale, axis=1)
        pixels = np.round(scaled * 255).astype(np.uint8)

        # Write a true, lossless grayscale PNG; no matplotlib figure cropping,
        # interpolation, labels, or lossy WebP/JPEG compression.
        from PIL import Image
        Image.fromarray(pixels, mode="L").save(output_path, format="PNG",
                                               optimize=True)

        header = hdu.header
        ra = header.get("CRVAL1")
        dec = header.get("CRVAL2")
        band = header.get("BAND") or header.get("FILTER") or header.get("WAVELNTH")
        obs = header.get("DATE-OBS") or header.get("MJD-OBS") or ""

        return {
            "fits_file": fits_path.name,
            "frame_file": output_path.name,
            "hdu": index,
            "ra": "" if ra is None else ra,
            "dec": "" if dec is None else dec,
            "band": "" if band is None else band,
            "observation": obs,
            "native_width": int(data.shape[1]),
            "native_height": int(data.shape[0]),
            "display_scale": display_scale,
            "stretch_percentiles": "2.0,99.5",
            "stretch_min": float(lo),
            "stretch_max": float(hi),
        }

def main():
    parser = argparse.ArgumentParser(description="Download SPHEREx DR1/QR3 D4 FITS and make frames.")
    parser.add_argument("--ra", type=float, default=269.44627, help="Target RA in degrees")
    parser.add_argument("--dec", type=float, default=4.76820, help="Target Dec in degrees")
    parser.add_argument("--radius", type=float, default=0.06, help="SIA2 search radius in degrees")
    parser.add_argument("--cutout", type=float, default=0.12, help="FITS cutout size in degrees")
    parser.add_argument("--max-files", type=int, default=30, help="Maximum files to download; 0 means no limit")
    parser.add_argument("--include-deep", action="store_true", help="Also search DR1/QR3 deep collections")
    parser.add_argument("--out", type=Path, default=Path("spherex_data"), help="Output folder")
    args = parser.parse_args()

    if not (0 <= args.ra < 360 and -90 <= args.dec <= 90):
        parser.error("RA must be [0,360), Dec must be [-90,90].")
    if args.radius <= 0 or args.cutout <= 0:
        parser.error("Search radius and cutout size must be positive.")

    root = args.out.expanduser().resolve()
    fits_dir = root / "fits"
    frames_dir = root / "frames"
    fits_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    collections = COLLECTIONS + (DEEP_COLLECTIONS if args.include_deep else [])
    found = []
    failures = []

    print("\nSPHEREx local downloader")
    print(f"Target: RA {args.ra:.6f}°, Dec {args.dec:.6f}°")
    print(f"Search radius: {args.radius}° · cutout: {args.cutout}° · detector: D4")
    print(f"Output: {root}\n")

    for collection in collections:
        try:
            found.extend(query_collection(session, collection, args.ra, args.dec, args.radius))
        except Exception as exc:
            failures.append(f"{collection} search: {exc}")
            print(f"  WARNING: {exc}")

    # Deduplicate identical direct access URLs.
    unique = {}
    for item in found:
        unique[item["url"]] = item
    candidates = list(unique.values())

    if not candidates:
        # If a previous run already downloaded FITS files, allow the processing
        # stage to resume even when SIA2 currently returns no candidates.
        existing_fits = sorted(p for p in fits_dir.glob("*") if p.is_file() and
                               p.name.lower().endswith((".fits", ".fits.gz", ".fits.fz",
                                                        ".fit", ".fit.gz", ".fit.fz",
                                                        ".fts", ".fts.gz", ".fts.fz")))
        if existing_fits:
            print("\nSIA2 returned no new D4 candidates; reprocessing existing local FITS files.")
            downloaded = [(path, path.name.split("_", 1)[0], "") for path in existing_fits]
        else:
            print("\nNo D4 FITS files were found through SIA2.")
            print("This can mean no matching D4 coverage, a temporary archive outage,")
            print("or that the archive's SIA2 response uses a different detector URL pattern.")
            for failure in failures:
                print(" -", failure)
            print("\nTry again later, or run with --include-deep.")
            return 2
    else:
        candidates.sort(key=lambda x: (x["collection"], x["url"]))
        if args.max_files > 0:
            candidates = candidates[:args.max_files]
        print(f"\nUnique D4 files selected: {len(candidates)}")

        downloaded = []
        for number, item in enumerate(candidates, 1):
            cutout_url = add_cutout(item["url"], args.ra, args.dec, args.cutout)
            filename = safe_name(item["url"])
            # Prefix collection to avoid same-name collisions between releases.
            destination = fits_dir / f"{item['collection']}_{filename}"
            print(f"\n[{number}/{len(candidates)}] {item['collection']}")
            try:
                if download_fits(session, cutout_url, destination):
                    downloaded.append((destination, item["collection"], cutout_url))
            except Exception as exc:
                failures.append(f"{item['url']}: {exc}")
                print(f"  DOWNLOAD FAILED: {exc}")

    frame_records = []
    gif_images = []
    for number, (fits_path, collection, url) in enumerate(downloaded, 1):
        frame_path = frames_dir / f"frame_{number:04d}.png"
        print(f"Making frame {number}/{len(downloaded)}: {fits_path.name}")
        try:
            record = make_frame(fits_path, frame_path)
            if record:
                record["collection"] = collection
                record["source_url"] = url
                frame_records.append(record)
                gif_images.append(imageio.imread(frame_path))
        except Exception as exc:
            failures.append(f"Frame {fits_path.name}: {exc}")
            print(f"  FRAME FAILED: {exc}")

    if frame_records:
        index_path = root / "frames_index.csv"
        with index_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(frame_records[0].keys()))
            writer.writeheader()
            writer.writerows(frame_records)

        gif_path = root / "spherex_blink.gif"
        imageio.mimsave(gif_path, gif_images, duration=0.65, loop=0)
        print("\nCOMPLETE")
        print(f"FITS files: {fits_dir}")
        print(f"PNG frames: {frames_dir}")
        print(f"Frame index: {index_path}")
        print(f"Animated GIF: {gif_path}")
        print(f"Frames created: {len(frame_records)}")
    else:
        print("\nFITS downloads completed, but no displayable image frames were generated.")

    if failures:
        log_path = root / "download_errors.txt"
        log_path.write_text("\n\n".join(failures), encoding="utf-8")
        print(f"\nSome operations failed; details saved to {log_path}")
    return 0 if frame_records else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)
