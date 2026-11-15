#!/usr/bin/env python3
"""
SPHEREx DR1 + QR3 downloader, frame maker and moving-object finder.

1. Searches IRSA SIA2 for detector D4 exposures covering the target.
2. Fetches cutouts: straight from NASA's public AWS S3 bucket when possible
   (only the needed pixels), IRSA's cutout service as a fallback.
3. Aligns every frame onto one north-up grid centered on the target and
   masks flagged pixels (cosmic rays, hot/bad pixels), making PNG frames as
   files arrive so the viewer can start early.
4. Finds moving objects: median "still sky", difference images, spots above
   the noise, then links spots that line up in 3+ frames at steady speed.
   Results go to candidates.json; difference frames go to diffs/.

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
from astropy.time import Time
from astropy.wcs import WCS, FITSFixedWarning
from astroquery.ipac.irsa import Irsa
from PIL import Image
from reproject import reproject_interp
from scipy import ndimage
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
USER_AGENT = "SPHEREx-local-downloader/1.3 (Python; FITS archive client)"
RETRY_STATUS = {429, 500, 502, 503, 504}
PIXEL_SCALE = 6.15  # SPHEREx arcsec per pixel
FITS_ENDINGS = (".fits", ".fits.gz", ".fits.fz", ".fit", ".fit.gz", ".fit.fz",
                ".fts", ".fts.gz", ".fts.fz")

# ---- display ----
DISPLAY_SCALE = 3           # each pixel drawn as a 3x3 block in the PNGs

# ---- pixel masking ----
# FLAGS bits treated as artifacts. Names are matched against the bit
# definitions in the FLAGS header (MP_<NAME> = bit). Anything marking a
# real source is deliberately NOT in this list.
BAD_FLAG_NAMES = {
    "TRANSIENT", "OVERFLOW", "SUR_ERROR", "NONFUNC", "MISSING_DATA",
    "HOT", "COLD", "NONLINEAR", "PERSIST", "OUTLIER", "PHANMISS", "DICHROIC",
}

# ---- moving-object search ----
PASS_GAP_DAYS = 30          # frames further apart than this belong to different survey passes
DETECT_SIGMA = 5.0          # detection threshold in difference images (x noise)
MIN_BLOB_PIXELS = 2         # single-pixel spikes = cosmic rays / hot pixels
MAX_DETECTIONS_PER_FRAME = 40
LINK_TOLERANCE_PX = 1.5     # allowed distance from the straight-line prediction
MIN_TRACK_POINTS = 3
MIN_TRACK_SHIFT_PX = 1.0    # ignore "tracks" that don't really move (static residuals)
SLOW_MAX_PX_PER_DAY = 3.0   # within-pass tracks slower than this = "slow mover"
MAX_PX_PER_DAY = 200.0
PAIR_MIN_SHIFT_PX = 0.7     # pass-to-pass shifts for proper-motion candidates
PAIR_MAX_SHIFT_PX = 4.0


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
            "total": 0,
            "done": 0,
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
            pass


# ---------------------------------------------------------------- search

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
        if not (re.search(r"D4(?:_|\.|$)", filename) or re.search(r"/4/", path)):
            continue

        s3_path = None
        raw_cloud = cell(row, cloud_col)
        if raw_cloud is not None and str(raw_cloud).strip():
            try:
                aws = json.loads(str(raw_cloud))["aws"]
                s3_path = f"{aws['bucket_name']}/{aws['key']}"
            except (ValueError, KeyError, TypeError):
                s3_path = None
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


# ---------------------------------------------------------------- downloads

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
        header.pop(key, None)
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

        data = np.array(image.section[y0:y1, x0:x1], dtype=np.float32)
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


# ---------------------------------------------------------------- frames

def target_grid(ra, dec, cutout_deg):
    """A north-up, east-left grid centered on the target at SPHEREx pixel scale."""
    n = max(16, int(round(cutout_deg * 3600 / PIXEL_SCALE)))
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(n + 1) / 2, (n + 1) / 2]
    w.wcs.cdelt = [-PIXEL_SCALE / 3600, PIXEL_SCALE / 3600]
    return w, n


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


_FLAG_NOTE_PRINTED = False


def bad_flag_mask(flags_hdu):
    """Boolean mask of artifact pixels, using the bit names in the FLAGS header."""
    global _FLAG_NOTE_PRINTED
    bits = {}
    for key, value in flags_hdu.header.items():
        if key.startswith("MP_"):
            try:
                bits[key[3:]] = int(value)
            except (TypeError, ValueError):
                pass
    if not _FLAG_NOTE_PRINTED:
        _FLAG_NOTE_PRINTED = True
        if bits:
            print("  FLAGS bit definitions: " + ", ".join(f"{k}={v}" for k, v in sorted(bits.items(), key=lambda kv: kv[1])))
        else:
            print("  FLAGS header has no MP_ bit definitions; flag masking is off (spike filter still applies).")
    bad = 0
    used = []
    for name, bit in bits.items():
        if name in BAD_FLAG_NAMES:
            bad |= (1 << bit)
            used.append(name)
    if not bad:
        return None, "none"
    flags = np.asarray(flags_hdu.data).astype(np.int64)
    return (flags & bad) != 0, ",".join(sorted(used))


def load_aligned(fits_path, grid_wcs, n):
    """Read IMAGE (+FLAGS), mask artifact pixels, reproject onto the shared grid."""
    with fits.open(fits_path, memmap=False) as hdul:
        image_hdus = find_image_hdus(hdul)
        if not image_hdus:
            return None
        index, hdu = image_hdus[0]
        data = np.array(hdu.data, dtype=np.float32)
        header = hdu.header.copy()

        masked, flag_note = 0, "none"
        try:
            flags_hdu = hdul["FLAGS"]
        except KeyError:
            flags_hdu = None
        if flags_hdu is not None and flags_hdu.data is not None and flags_hdu.data.shape == data.shape:
            bad, flag_note = bad_flag_mask(flags_hdu)
            if bad is not None:
                masked = int(bad.sum())
                data[bad] = np.nan

        aligned, footprint = reproject_interp((data, WCS(header)), grid_wcs, shape_out=(n, n))
        aligned[footprint == 0] = np.nan
    return aligned.astype(np.float32), header, index, masked, flag_note


def save_png(array, path, lo, hi, fill):
    """Grayscale PNG, north up (FITS row 0 is the bottom), pixels as blocks."""
    img = np.array(array, dtype=np.float32)
    img[~np.isfinite(img)] = fill
    if not hi > lo:
        hi = lo + 1.0
    scaled = np.clip((img - lo) / (hi - lo), 0.0, 1.0)
    scaled = np.flipud(scaled)
    scaled = np.repeat(np.repeat(scaled, DISPLAY_SCALE, axis=0), DISPLAY_SCALE, axis=1)
    Image.fromarray(np.round(scaled * 255).astype(np.uint8), mode="L").save(path, format="PNG", optimize=True)


def make_frame(fits_path, output_path, grid_wcs, n):
    """Aligned, masked frame PNG. Returns (record, aligned array) or (None, None)."""
    loaded = load_aligned(fits_path, grid_wcs, n)
    if loaded is None:
        print(f"  No 2-D image extension found in {fits_path.name}; skipping frame.")
        return None, None
    aligned, header, index, masked, flag_note = loaded

    finite = np.isfinite(aligned)
    if finite.mean() < 0.3:
        print(f"  {fits_path.name} covers too little of the target area; skipping frame.")
        return None, None

    background = float(np.nanmedian(aligned))
    lo, hi = np.nanpercentile(aligned[finite], [2.0, 99.5])
    save_png(aligned, output_path, lo, hi, fill=background)

    ra = header.get("CRVAL1")
    dec = header.get("CRVAL2")
    band = header.get("BAND") or header.get("FILTER") or header.get("WAVELNTH")
    obs = header.get("DATE-OBS") or header.get("MJD-OBS") or ""
    record = {
        "fits_file": fits_path.name,
        "frame_file": output_path.name,
        "hdu": index,
        "ra": "" if ra is None else ra,
        "dec": "" if dec is None else dec,
        "band": "" if band is None else band,
        "observation": obs,
        "native_width": n,
        "native_height": n,
        "display_scale": DISPLAY_SCALE,
        "stretch_percentiles": "2.0,99.5",
        "stretch_min": float(lo),
        "stretch_max": float(hi),
        "masked_pixels": masked,
        "masked_flags": flag_note,
    }
    return record, aligned


# ---------------------------------------------------------------- moving objects

def record_mjd(record):
    value = record.get("mjd")
    if value not in ("", None):
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    obs = str(record.get("observation") or "").strip()
    try:
        return float(obs)  # MJD-OBS stored as a number
    except ValueError:
        pass
    try:
        return float(Time(obs.replace(" ", "T")).mjd)
    except Exception:
        return float("nan")


def robust_sigma(values):
    v = values[np.isfinite(values)]
    if v.size < 10:
        return float("nan")
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def find_blobs(image, threshold, min_pixels=MIN_BLOB_PIXELS):
    """Connected spots above threshold; single-pixel spikes are dropped."""
    mask = np.isfinite(image) & (image > threshold)
    labels, count = ndimage.label(mask)
    blobs = []
    for label in range(1, count + 1):
        ys, xs = np.nonzero(labels == label)
        if len(xs) < min_pixels:
            continue
        weights = image[ys, xs]
        total = float(weights.sum())
        blobs.append({
            "x": float((xs * weights).sum() / total),
            "y": float((ys * weights).sum() / total),
            "peak": float(weights.max()),
            "pixels": int(len(xs)),
        })
    return blobs


def point_info(grid_wcs, n, x, y):
    sky = grid_wcs.pixel_to_world(x, y)
    return {
        "x": round(x, 2), "y": round(y, 2),
        # fractions of the PNG width/height from the top-left, for drawing overlays
        "fx": round((x + 0.5) / n, 4), "fy": round(1 - (y + 0.5) / n, 4),
        "ra": round(float(sky.ra.deg), 6), "dec": round(float(sky.dec.deg), 6),
    }


def position_angle(vx, vy):
    """Direction of motion in degrees east of north (east is -x on this grid)."""
    return round(math.degrees(math.atan2(-vx, vy)) % 360, 1)


def find_movers(records, aligned_by_file, grid_wcs, n, diffs_dir, ra, dec):
    """Median still sky -> difference frames -> spots -> straight-line tracks."""
    ordered = time_order(records)
    frames = [r for r in ordered if r["frame_file"] in aligned_by_file]
    for r in records:
        r.setdefault("diff_file", "")
    if len(frames) < 3:
        return {"note": "Fewer than 3 frames; motion search skipped.", "tracks": [], "pairs": []}

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        cube = np.array([aligned_by_file[r["frame_file"]] - np.nanmedian(aligned_by_file[r["frame_file"]])
                         for r in frames], dtype=np.float32)
        still = np.nanmedian(cube, axis=0)
    diffs = cube - still
    mjds = [record_mjd(r) for r in frames]

    # survey passes: groups of frames separated by long gaps
    pass_ids, current, last = [], 0, None
    for t in mjds:
        if last is not None and np.isfinite(t) and t - last > PASS_GAP_DAYS:
            current += 1
        pass_ids.append(current)
        if np.isfinite(t):
            last = t
    by_pass = {}
    for i, p in enumerate(pass_ids):
        by_pass.setdefault(p, []).append(i)

    # difference frames + detections
    detections, sigmas = [], []
    for i, (record, diff) in enumerate(zip(frames, diffs), 1):
        sigma = robust_sigma(diff)
        sigmas.append(sigma)
        diff_name = f"diff_{i:04d}.png"
        lim = 6 * sigma if np.isfinite(sigma) and sigma > 0 else 1.0
        save_png(diff, diffs_dir / diff_name, -lim, lim, fill=0.0)  # gray = no change
        record["diff_file"] = diff_name
        if not (np.isfinite(sigma) and sigma > 0):
            detections.append([])
            continue
        blobs = find_blobs(diff, DETECT_SIGMA * sigma)
        for b in blobs:
            b["snr"] = b["peak"] / sigma
        blobs.sort(key=lambda b: -b["snr"])
        detections.append(blobs[:MAX_DETECTIONS_PER_FRAME])

    # link detections within each pass along straight lines at steady speed
    proposals = []
    for p, fidx in by_pass.items():
        fidx = [f for f in fidx if np.isfinite(mjds[f])]
        if len(fidx) < MIN_TRACK_POINTS:
            continue
        for ai, fa in enumerate(fidx):
            for fb in fidx[ai + 1:]:
                dt = mjds[fb] - mjds[fa]
                if dt <= 0:
                    continue
                for ia, da in enumerate(detections[fa]):
                    for ib, db in enumerate(detections[fb]):
                        dx, dy = db["x"] - da["x"], db["y"] - da["y"]
                        if math.hypot(dx, dy) < MIN_TRACK_SHIFT_PX:
                            continue
                        vx, vy = dx / dt, dy / dt
                        if math.hypot(vx, vy) > MAX_PX_PER_DAY:
                            continue
                        points = [(fa, ia), (fb, ib)]
                        for fk in fidx:
                            if fk in (fa, fb):
                                continue
                            px = da["x"] + vx * (mjds[fk] - mjds[fa])
                            py = da["y"] + vy * (mjds[fk] - mjds[fa])
                            best, best_d = None, LINK_TOLERANCE_PX
                            for ik, dk in enumerate(detections[fk]):
                                d = math.hypot(dk["x"] - px, dk["y"] - py)
                                if d <= best_d:
                                    best, best_d = ik, d
                            if best is not None:
                                points.append((fk, best))
                        if len(points) >= MIN_TRACK_POINTS:
                            proposals.append((p, tuple(sorted(set(points)))))

    tracks, used = [], set()
    scored = []
    for p, points in set(proposals):
        t = np.array([mjds[f] for f, _ in points])
        xs = np.array([detections[f][i]["x"] for f, i in points])
        ys = np.array([detections[f][i]["y"] for f, i in points])
        if np.ptp(t) <= 0:
            continue
        cx, bx = np.polyfit(t, xs, 1)
        cy, by = np.polyfit(t, ys, 1)
        rms = float(np.sqrt(np.mean((xs - (cx * t + bx)) ** 2 + (ys - (cy * t + by)) ** 2)))
        if rms > LINK_TOLERANCE_PX:
            continue
        scored.append((-len(points), rms, p, points, cx, cy))
    scored.sort(key=lambda s: (s[0], s[1]))
    for _, rms, p, points, vx, vy in scored:
        if any(pt in used for pt in points):
            continue
        used.update(points)
        speed = math.hypot(vx, vy)
        tracks.append({
            "kind": "slow mover" if speed <= SLOW_MAX_PX_PER_DAY else "fast mover",
            "pass": p,
            "speed_px_per_day": round(speed, 3),
            "speed_arcsec_per_day": round(speed * PIXEL_SCALE, 2),
            "direction_deg": position_angle(vx, vy),
            "rms_px": round(rms, 2),
            "points": [
                {
                    "frame_file": frames[f]["frame_file"],
                    "diff_file": frames[f]["diff_file"],
                    "mjd": round(mjds[f], 5),
                    "snr": round(detections[f][i]["snr"], 1),
                    **point_info(grid_wcs, n, detections[f][i]["x"], detections[f][i]["y"]),
                }
                for f, i in sorted(points, key=lambda pt: mjds[pt[0]])
            ],
        })
    tracks.sort(key=lambda t: (t["kind"] != "slow mover", t["speed_px_per_day"]))

    # compare first and last pass for small shifts (nearby stars, brown dwarfs)
    pairs = []
    passes = sorted(by_pass)
    if len(passes) >= 2:
        first, last_p = by_pass[passes[0]], by_pass[passes[-1]]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            s1 = np.nanmedian(cube[first], axis=0)
            s2 = np.nanmedian(cube[last_p], axis=0)
            t1 = np.nanmean([mjds[i] for i in first])
            t2 = np.nanmean([mjds[i] for i in last_p])
        change = s2 - s1
        sigma = robust_sigma(change)
        if np.isfinite(sigma) and sigma > 0 and np.isfinite(t2 - t1) and t2 > t1:
            appeared = find_blobs(change, DETECT_SIGMA * sigma)
            vanished = find_blobs(-change, DETECT_SIGMA * sigma)
            years = (t2 - t1) / 365.25
            for a in appeared:
                best, best_d = None, PAIR_MAX_SHIFT_PX
                for v in vanished:
                    d = math.hypot(a["x"] - v["x"], a["y"] - v["y"])
                    if PAIR_MIN_SHIFT_PX <= d <= best_d:
                        best, best_d = v, d
                if best is None:
                    continue
                vanished.remove(best)
                pairs.append({
                    "kind": "proper-motion candidate",
                    "shift_px": round(best_d, 2),
                    "shift_arcsec": round(best_d * PIXEL_SCALE, 1),
                    "arcsec_per_year": round(best_d * PIXEL_SCALE / years, 1),
                    "direction_deg": position_angle(a["x"] - best["x"], a["y"] - best["y"]),
                    "snr": round(min(a["peak"], best["peak"]) / sigma, 1),
                    "from": {"mjd": round(t1, 3), **point_info(grid_wcs, n, best["x"], best["y"])},
                    "to": {"mjd": round(t2, 3), **point_info(grid_wcs, n, a["x"], a["y"])},
                })
            pairs.sort(key=lambda q: -q["snr"])

    return {
        "target": {"ra": ra, "dec": dec},
        "grid": {"size_px": n, "pixel_scale_arcsec": PIXEL_SCALE, "display_scale": DISPLAY_SCALE,
                 "orientation": "north up, east left"},
        "passes": [
            {"pass": p, "frames": len(idx),
             "start_mjd": round(min(mjds[i] for i in idx), 3) if all(np.isfinite(mjds[i]) for i in idx) else None,
             "end_mjd": round(max(mjds[i] for i in idx), 3) if all(np.isfinite(mjds[i]) for i in idx) else None}
            for p, idx in sorted(by_pass.items())
        ],
        "settings": {
            "detect_sigma": DETECT_SIGMA, "min_blob_pixels": MIN_BLOB_PIXELS,
            "link_tolerance_px": LINK_TOLERANCE_PX, "min_track_points": MIN_TRACK_POINTS,
            "slow_max_px_per_day": SLOW_MAX_PX_PER_DAY, "pass_gap_days": PASS_GAP_DAYS,
        },
        "tracks": tracks,
        "pairs": pairs,
    }


# ---------------------------------------------------------------- index + run

def time_order(records):
    return sorted(records, key=lambda r: (r["mjd"] == "", r["mjd"] if r["mjd"] != "" else 0,
                                          str(r["observation"])))


def write_index(index_path, records):
    """Write frames_index.csv in time order, swapping it in all at once."""
    ordered = time_order(records)
    fieldnames = []
    for r in ordered:
        for key in r:
            if key not in fieldnames:
                fieldnames.append(key)
    temp = index_path.with_suffix(".csv.part")
    with temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(ordered)
    replace_with_retry(temp, index_path)
    return ordered


def write_json(path, payload):
    temp = path.with_suffix(".json.part")
    temp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    replace_with_retry(temp, path)


def run(args, root, fits_dir, frames_dir, diffs_dir, progress):
    collections = COLLECTIONS + (DEEP_COLLECTIONS if args.include_deep else [])
    found = []
    failures = []
    started = time.time()
    index_path = root / "frames_index.csv"
    grid_wcs, n = target_grid(args.ra, args.dec, args.cutout)

    # Start clean so the page never shows results from an earlier run
    for folder in (frames_dir, diffs_dir):
        for old in folder.glob("*.png"):
            old.unlink(missing_ok=True)
    for name in ("frames_index.csv", "spherex_blink.gif", "candidates.json"):
        (root / name).unlink(missing_ok=True)

    print("\nSPHEREx downloader")
    print(f"Target: RA {args.ra:.6f}°, Dec {args.dec:.6f}°")
    print(f"Search radius: {args.radius}° · cutout: {args.cutout}° ({n}x{n} px grid) · detector: D4")
    print(f"Source: {'AWS S3 (IRSA fallback)' if HAVE_S3 else 'IRSA cutouts (install s3fs for AWS S3)'}")
    print(f"Output: {root}\n")

    progress.update(stage="searching")
    for collection in collections:
        try:
            found.extend(query_collection(collection, args.ra, args.dec, args.radius))
        except Exception as exc:
            failures.append(f"{collection} search: {exc}")
            print(f"  WARNING: {exc}")

    unique = {}
    for item in found:
        unique.setdefault(item["obs_id"] or item["url"], item)
    candidates = list(unique.values())

    records = []
    aligned_by_file = {}

    def add_frame(fits_path, collection, source, mjd):
        """Aligned, masked PNG right away, plus a refreshed index for the page."""
        frame_path = frames_dir / f"frame_{len(records) + 1:04d}.png"
        try:
            record, aligned = make_frame(fits_path, frame_path, grid_wcs, n)
        except Exception as exc:
            failures.append(f"Frame {fits_path.name}: {exc}")
            print(f"  FRAME FAILED: {exc}")
            return
        if record:
            record["collection"] = collection
            record["source_url"] = source
            record["mjd"] = "" if mjd is None else mjd
            records.append(record)
            aligned_by_file[record["frame_file"]] = aligned
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

    # Moving-object search
    progress.update(stage="processing", done=len(records), total=len(records),
                    message="Finding moving objects…")
    print("\nSearching for moving objects…")
    try:
        result = find_movers(records, aligned_by_file, grid_wcs, n, diffs_dir, args.ra, args.dec)
        write_json(root / "candidates.json", result)
        slow = sum(1 for t in result["tracks"] if t["kind"] == "slow mover")
        fast = len(result["tracks"]) - slow
        print(f"  Passes: {len(result.get('passes', []))} · slow movers: {slow} · fast movers: {fast} · "
              f"proper-motion candidates: {len(result['pairs'])}")
    except Exception as exc:
        failures.append(f"Motion search: {exc}")
        print(f"  Motion search failed: {exc}")

    ordered = write_index(index_path, records)
    try:
        gif_images = [imageio.imread(frames_dir / r["frame_file"]) for r in ordered]
        imageio.mimsave(root / "spherex_blink.gif", gif_images, duration=0.65, loop=0)
    except Exception as exc:
        print(f"  GIF skipped: {exc}")

    print("\nCOMPLETE")
    print(f"Frames created: {len(records)} in {time.time() - started:.1f}s total")

    if failures:
        log_path = root / "download_errors.txt"
        log_path.write_text("\n\n".join(failures), encoding="utf-8")
        print(f"\nSome operations failed; details saved to {log_path}")

    progress.update(stage="done", done=len(records), total=len(records), message="")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Download SPHEREx DR1/QR3 D4 cutouts, make aligned frames, find movers.")
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
    diffs_dir = root / "diffs"
    for folder in (fits_dir, frames_dir, diffs_dir):
        folder.mkdir(parents=True, exist_ok=True)

    progress = Progress(root)
    try:
        return run(args, root, fits_dir, frames_dir, diffs_dir, progress)
    except Exception as exc:
        progress.update(stage="error", message=f"Scan failed: {exc}")
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
        raise SystemExit(130)