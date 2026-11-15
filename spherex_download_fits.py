#!/usr/bin/env python3
"""
SPHEREx DR1 + QR3 downloader and frame maker.

Searches IRSA SIA2, then fetches detector D4 cutouts around the target:
  - preferred: read only the needed pixels straight from NASA's public
    AWS S3 bucket (nasa-irsa-spherex), several frames in parallel
  - fallback: IRSA's cutout service

Frames are made as soon as each file arrives, and frames_index.csv is
kept up to date (in time order) so the viewer can start playing early.
Live progress is written to progress.json for the web page.

Example:
  python3 spherex_download_fits.py --ra 269.44627 --dec 4.76820

Optional:
  --radius 0.06       Search radius in degrees (default 0.06)
  --cutout 0.12       Cutout width in degrees (default 0.12)
  --max-files 30      Maximum frames, spread evenly over time (default 30)
  --workers 6         Parallel downloads (default 6)
  --include-deep      Search DR1 and QR3 deep-field collections too
  --out ./spherex_data
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import urllib.parse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import requests
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.ipac.irsa import Irsa
import matplotlib
matplotlib.use("Agg")
import imageio.v2 as imageio

try:
    import s3fs  # noqa: F401  (lets astropy open s3:// paths)
    HAVE_S3 = True
except ImportError:
    HAVE_S3 = False

warnings.simplefilter("ignore", category=FITSFixedWarning)

COLLECTIONS = ["spherex_dr1", "spherex_qr3"]  # DR1 first: preferred when both have an exposure
DEEP_COLLECTIONS = ["spherex_dr1_deep", "spherex_qr3_deep"]
USER_AGENT = "SPHEREx-local-downloader/1.2 (Python; FITS archive client)"
RETRY_STATUS = {429, 500, 502, 503, 504}
PIXEL_SCALE = 6.15  # SPHEREx arcsec per pixel
FITS_ENDINGS = (".fits", ".fits.gz", ".fits.fz", ".fit", ".fit.gz", ".fit.fz",
                ".fts", ".fts.gz", ".fts.fz")


class OffImage(Exception):
    """The target falls outside (or at the very edge of) this exposure."""


def replace_with_retry(temp, final, attempts=10):
    """Rename temp -> final; retry briefly if the server is reading the file (Windows)."""
    for _ in range(attempts):
        try:
            temp.replace(final)
            return True
        except PermissionError:
            time.sleep(0.1)
    return False


class Progress:
    """Live scan status for the web page, saved as progress.json."""

    def __init__(self, root):
        self.path = root / "progress.json"
        self.data = {
            "stage": "searching",   # searching, downloading, processing, done, no_data, error
            "total": 0,             # frames expected
            "done": 0,              # frames ready to view
            "downloaded": 0,
            "sources": {"s3": 0, "irsa": 0, "cached": 0},
            "skipped": 0,
            "failed": 0,
            "message": "",
        }
        self.write()

    def update(self, **changes):
        self.data.update(changes)
        self.write()

    def write(self):
        self.data["updated"] = time.time()
        temp = self.path.with_suffix(".json.part")
        try:
            temp.write_text(json.dumps(self.data), encoding="utf-8")
            replace_with_retry(temp, self.path)
        except OSError:
            pass  # a missed update is fine; the next one will catch up


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


def cell(row, col):
    """Read a table cell, returning None if the column is missing or masked."""
    if col is None:
        return None
    value = row[col]
    return None if np.ma.is_masked(value) else value


def query_collection(collection, ra, dec, radius):
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
        raise RuntimeError(f"SIA2 table has no access_url column. Columns: {table.colnames}")
    cloud_col = names.get("cloud_access")
    obs_col = names.get("obs_id")
    time_col = names.get("t_min")

    rows = []
    for row in table:
        value = cell(row, access_col)
        if value is None:
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
        if not (re.search(r"D4(?:_|\.|$)", filename) or re.search(r"/4/", path)):
            continue

        # Path in NASA's S3 bucket, if IRSA provides one for this file
        s3_path = None
        raw_cloud = cell(row, cloud_col)
        if raw_cloud is not None and str(raw_cloud).strip():
            try:
                aws = json.loads(str(raw_cloud))["aws"]
                s3_path = f"{aws['bucket_name']}/{aws['key']}"
            except (ValueError, KeyError, TypeError):
                s3_path = None

        # IRSA didn't give a cloud path: derive it from the IRSA address,
        # since the S3 bucket mirrors IRSA's folder layout
        if s3_path is None:
            marker = "/ibe/data/spherex/"
            if marker in path:
                s3_path = "nasa-irsa-spherex/" + path.split(marker, 1)[1]

        obs_id = cell(row, obs_col)
        t_min = cell(row, time_col)
        rows.append({
            "collection": collection,
            "url": url,
            "s3": s3_path,
            "obs_id": "" if obs_id is None else str(obs_id),
            "mjd": None if t_min is None else float(t_min),
        })

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
    if not name.lower().endswith(FITS_ENDINGS):
        name += ".fits"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def valid_fits(path):
    """True if the file exists and opens as FITS with some data in it."""
    if not path.exists() or path.stat().st_size <= 2880:
        return False
    try:
        with fits.open(path, memmap=False) as hdul:
            hdul.verify("silentfix")
            return any(hdu.data is not None for hdu in hdul)
    except Exception:
        return False


def download_fits(session, url, destination):
    """Fallback: download an IRSA cutout over HTTPS."""
    response = request_with_retries(session, "GET", url, stream=True)
    temp = destination.with_suffix(destination.suffix + ".part")
    try:
        with temp.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        response.close()
        if not valid_fits(temp):
            raise RuntimeError("Downloaded response is not a usable FITS file.")
        temp.replace(destination)
        return True
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def shift_header(header, x0, y0):
    """Adjust a header so its WCS still matches after cropping at (x0, y0)."""
    for key in ("BSCALE", "BZERO", "BLANK"):
        header.pop(key, None)  # data is already scaled to real values
    for key in list(header.keys()):
        if re.fullmatch(r"CRPIX1[A-Z]?", key):
            header[key] = header[key] - x0
        elif re.fullmatch(r"CRPIX2[A-Z]?", key):
            header[key] = header[key] - y0
    return header


def s3_cutout(s3_path, ra, dec, size_deg, destination):
    """Read only the pixels around (ra, dec) from NASA's public S3 bucket."""
    half = int(math.ceil(size_deg * 3600 / PIXEL_SCALE / 2)) + 2
    with fits.open(f"s3://{s3_path}", use_fsspec=True,
                   fsspec_kwargs={"anon": True}) as hdul:
        primary = hdul[0].header
        image = hdul["IMAGE"]
        header = image.header.copy()
        ny, nx = image.shape

        x, y = WCS(header).world_to_pixel(SkyCoord(ra * u.deg, dec * u.deg))
        if not (np.isfinite(x) and np.isfinite(y)):
            raise OffImage("target is not on this exposure")
        cx, cy = int(round(float(x))), int(round(float(y)))
        x0, x1 = max(cx - half, 0), min(cx + half + 1, nx)
        y0, y1 = max(cy - half, 0), min(cy + half + 1, ny)
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise OffImage("target is at the edge of this exposure")

        # .section downloads only these rows/columns, not the whole file
        data = np.array(image.section[y0:y1, x0:x1], dtype=np.float32)

        # observation time often lives in the primary header; copy it over
        for key in ("DATE-OBS", "MJD-OBS", "DATE-AVG", "MJD-AVG", "DATE-END"):
            if key not in header and key in primary:
                header[key] = primary[key]

        hdus = [fits.PrimaryHDU(), fits.ImageHDU(data=data, header=shift_header(header, x0, y0), name="IMAGE")]
        try:
            flags_hdu = hdul["FLAGS"]
            flags = np.array(flags_hdu.section[y0:y1, x0:x1])
            hdus.append(fits.ImageHDU(data=flags, header=shift_header(flags_hdu.header.copy(), x0, y0), name="FLAGS"))
        except KeyError:
            pass

    temp = destination.with_suffix(destination.suffix + ".part")
    try:
        fits.HDUList(hdus).writeto(temp, overwrite=True)
        temp.replace(destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def fetch_one(item, args, fits_dir):
    """Get one cutout: S3 first, IRSA cutout service as a fallback."""
    destination = fits_dir / f"{item['collection']}_{safe_name(item['url'])}"
    if valid_fits(destination):
        return destination, "cached"

    if item.get("s3") and HAVE_S3:
        try:
            s3_cutout(item["s3"], args.ra, args.dec, args.cutout, destination)
            return destination, f"s3://{item['s3']}"
        except OffImage:
            raise
        except Exception as exc:
            print(f"  S3 read failed for {destination.name} ({exc}); using IRSA cutout instead")

    cutout_url = add_cutout(item["url"], args.ra, args.dec, args.cutout)
    with requests.Session() as session:
        download_fits(session, cutout_url, destination)
    return destination, cutout_url


def source_kind(source):
    if source == "cached":
        return "cached"
    return "s3" if source.startswith("s3://") else "irsa"


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
    Render the FITS IMAGE array at its native sampling:
      - actual IMAGE pixels (no reprojection or resizing)
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

        lo, hi = np.nanpercentile(data[finite], [2.0, 99.5])
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            lo = float(np.nanmin(data[finite]))
            hi = float(np.nanmax(data[finite]))
        if not np.isfinite(lo + hi) or hi <= lo:
            hi = lo + 1.0

        scaled = np.clip((data - lo) / (hi - lo), 0.0, 1.0)
        scaled[~finite] = 0.0

        display_scale = 3
        scaled = np.repeat(np.repeat(scaled, display_scale, axis=0),
                           display_scale, axis=1)
        pixels = np.round(scaled * 255).astype(np.uint8)

        from PIL import Image
        Image.fromarray(pixels, mode="L").save(output_path, format="PNG", optimize=True)

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


def time_order(records):
    """Sort frame records by observation time (MJD, then date text)."""
    return sorted(records, key=lambda r: (r["mjd"] == "", r["mjd"] if r["mjd"] != "" else 0,
                                          str(r["observation"])))


def write_index(index_path, records):
    """Write frames_index.csv in time order, swapping it in all at once."""
    ordered = time_order(records)
    temp = index_path.with_suffix(".csv.part")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ordered[0].keys()))
        writer.writeheader()
        writer.writerows(ordered)
    replace_with_retry(temp, index_path)
    return ordered


def run(args, root, fits_dir, frames_dir, progress):
    collections = COLLECTIONS + (DEEP_COLLECTIONS if args.include_deep else [])
    found = []
    failures = []
    started = time.time()
    index_path = root / "frames_index.csv"

    # Start clean so the page never shows frames from an earlier run
    for old in frames_dir.glob("frame_*.png"):
        old.unlink(missing_ok=True)
    for name in ("frames_index.csv", "spherex_blink.gif"):
        (root / name).unlink(missing_ok=True)

    print("\nSPHEREx downloader")
    print(f"Target: RA {args.ra:.6f}°, Dec {args.dec:.6f}°")
    print(f"Search radius: {args.radius}° · cutout: {args.cutout}° · detector: D4")
    print(f"Source: {'AWS S3 (IRSA fallback)' if HAVE_S3 else 'IRSA cutouts (install s3fs for AWS S3)'}")
    print(f"Output: {root}\n")

    progress.update(stage="searching")
    for collection in collections:
        try:
            found.extend(query_collection(collection, args.ra, args.dec, args.radius))
        except Exception as exc:
            failures.append(f"{collection} search: {exc}")
            print(f"  WARNING: {exc}")

    # The same exposure can appear in DR1 and QR3; keep the first one found (DR1).
    unique = {}
    for item in found:
        unique.setdefault(item["obs_id"] or item["url"], item)
    candidates = list(unique.values())

    records = []

    def add_frame(fits_path, collection, source, mjd):
        """Make a PNG right away and refresh the index so the page can use it."""
        frame_path = frames_dir / f"frame_{len(records) + 1:04d}.png"
        try:
            record = make_frame(fits_path, frame_path)
        except Exception as exc:
            failures.append(f"Frame {fits_path.name}: {exc}")
            print(f"  FRAME FAILED: {exc}")
            return
        if record:
            record["collection"] = collection
            record["source_url"] = source
            record["mjd"] = "" if mjd is None else mjd
            records.append(record)
            write_index(index_path, records)

    if not candidates:
        existing_fits = sorted(p for p in fits_dir.glob("*")
                               if p.is_file() and p.name.lower().endswith(FITS_ENDINGS))
        if not existing_fits:
            message = "No SPHEREx D4 exposures were found here (or IRSA is temporarily unavailable)."
            print("\n" + message)
            for failure in failures:
                print(" -", failure)
            progress.update(stage="no_data", message=message)
            return 2

        print("\nSIA2 returned no new D4 candidates; reprocessing existing local FITS files.")
        progress.update(stage="downloading", total=len(existing_fits))
        for path in existing_fits:
            add_frame(path, "_".join(path.name.split("_")[:2]), "cached", None)
            progress.data["sources"]["cached"] += 1
            progress.update(downloaded=progress.data["downloaded"] + 1, done=len(records))
    else:
        # Time order, and spread evenly over time if there are more than max-files
        candidates.sort(key=lambda x: (x["mjd"] is None, x["mjd"] or 0))
        if args.max_files > 0 and len(candidates) > args.max_files:
            idx = np.linspace(0, len(candidates) - 1, args.max_files).round().astype(int)
            candidates = [candidates[i] for i in sorted(set(idx))]
        print(f"\nUnique D4 exposures selected: {len(candidates)} · {args.workers} parallel workers")
        progress.update(stage="downloading", total=len(candidates))

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(fetch_one, item, args, fits_dir): item for item in candidates}
            for done_count, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                try:
                    path, source = future.result()
                except OffImage as exc:
                    progress.data["skipped"] += 1
                    print(f"  [{done_count}/{len(candidates)}] skipped: {exc}")
                except Exception as exc:
                    progress.data["failed"] += 1
                    failures.append(f"{item['url']}: {exc}")
                    print(f"  [{done_count}/{len(candidates)}] FAILED: {exc}")
                else:
                    kind = source_kind(source)
                    progress.data["sources"][kind] += 1
                    progress.data["downloaded"] += 1
                    print(f"  [{done_count}/{len(candidates)}] {path.name} ({kind.upper()})")
                    add_frame(path, item["collection"], source, item["mjd"])

                # Expected total shrinks if some exposures turn out unusable
                usable = len(candidates) - progress.data["skipped"] - progress.data["failed"]
                progress.update(done=len(records), total=max(usable, len(records)))

        print(f"\nDownloads finished in {time.time() - started:.1f}s")

    if not records:
        message = "Files were found, but no displayable frames could be made."
        print("\n" + message)
        progress.update(stage="error", message=message)
        if failures:
            (root / "download_errors.txt").write_text("\n\n".join(failures), encoding="utf-8")
        return 1

    # Final pass: index in time order, plus the animated GIF
    progress.update(stage="processing", done=len(records), total=len(records))
    ordered = write_index(index_path, records)
    try:
        gif_images = [imageio.imread(frames_dir / r["frame_file"]) for r in ordered]
        imageio.mimsave(root / "spherex_blink.gif", gif_images, duration=0.65, loop=0)
    except Exception as exc:
        print(f"  GIF skipped: {exc}")  # frames near image edges can differ in size

    print("\nCOMPLETE")
    print(f"Frames created: {len(records)} in {time.time() - started:.1f}s total")
    print(f"Frame index: {index_path}")

    if failures:
        log_path = root / "download_errors.txt"
        log_path.write_text("\n\n".join(failures), encoding="utf-8")
        print(f"\nSome operations failed; details saved to {log_path}")

    progress.update(stage="done", done=len(records), total=len(records), message="")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Download SPHEREx DR1/QR3 D4 cutouts and make frames.")
    parser.add_argument("--ra", type=float, default=269.44627, help="Target RA in degrees")
    parser.add_argument("--dec", type=float, default=4.76820, help="Target Dec in degrees")
    parser.add_argument("--radius", type=float, default=0.06, help="SIA2 search radius in degrees")
    parser.add_argument("--cutout", type=float, default=0.12, help="Cutout size in degrees")
    parser.add_argument("--max-files", type=int, default=30, help="Maximum frames (spread over time); 0 means no limit")
    parser.add_argument("--workers", type=int, default=6, help="Parallel downloads")
    parser.add_argument("--include-deep", action="store_true", help="Also search DR1/QR3 deep collections")
    parser.add_argument("--out", type=Path, default=Path("spherex_data"), help="Output folder")
    args = parser.parse_args()

    if not (0 <= args.ra < 360 and -90 <= args.dec <= 90):
        parser.error("RA must be [0,360), Dec must be [-90,90].")
    if args.radius <= 0 or args.cutout <= 0:
        parser.error("Search radius and cutout size must be positive.")
    args.workers = max(1, args.workers)

    root = args.out.expanduser().resolve()
    fits_dir = root / "fits"
    frames_dir = root / "frames"
    fits_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    progress = Progress(root)
    try:
        return run(args, root, fits_dir, frames_dir, progress)
    except Exception as exc:
        progress.update(stage="error", message=f"Scan failed: {exc}")
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)