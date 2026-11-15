# SPHEREx coordinate viewer

A small local browser viewer for downloading SPHEREx observations by sky coordinate and browsing the resulting PNG frames.

## Files

Keep these files together in the project root:

```text
project/
├── spherex_server.py
├── spherex_local_png_viewer.html
├── spherex_download_fits.py
└── spherex_data/                  # created automatically
```

## One-time setup (macOS / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install astroquery astropy numpy requests imageio matplotlib pillow
```

## Start

```bash
source .venv/bin/activate
python spherex_server.py
```

Open <http://127.0.0.1:8000/index.html> and keep the terminal running.

## Use

1. Enter RA and Dec in decimal degrees.
2. Click **INITIATE SCAN**.
3. The local server creates the coordinate folder and runs the existing downloader automatically.
4. When the PNG frames and `frames_index.csv` are ready, the HTML viewer loads them.

For RA `269.45208` and Dec `4.69336`, the downloader is run with the equivalent of:

```bash
python spherex_download_fits.py \
  --ra 269.45208 \
  --dec 4.69336 \
  --out "./spherex_data/ra_269.45208_dec_+4.69336"
```

The output is stored under:

```text
spherex_data/
└── ra_269.45208_dec_+4.69336/
    ├── fits/
    ├── frames/
    ├── frames_index.csv
    └── spherex_blink.gif
```

Saved targets are discovered by scanning `spherex_data/`; the HTML does not contain a hardcoded coordinate or frame folder. If a target already has an index and PNG frames, selecting it loads the saved frames instead of downloading again. If only FITS files exist, initiating the scan reruns the downloader, which can reuse valid local FITS files and regenerate frames.

## Notes

- RA must be in `[0, 360)` degrees; Dec must be in `[-90, 90]` degrees.
- Folder names use five decimal places and include an explicit `+` for positive declination.
- The downloader's IRSA retrieval and FITS-to-PNG rendering logic is preserved.
- The local server listens only on `127.0.0.1`.
- To stop the server, press `Ctrl+C` in its terminal.
