<!-- Before submitting: search this file for "[" and fill in or delete every bracket. -->

<img width="1050" height="140" alt="Space Finder 3000 banner" src="https://github.com/user-attachments/assets/a2c3ee94-6611-47b2-bd99-d1018567e016" />

<h1 align="center">Space Finder 3000</h1>

<p align="center">
  <b>Watch the sky <i>move</i>.</b><br>
  Pick any spot in the sky and flip through real NASA SPHEREx images of it over time.
</p>

<p align="center">
  <a href="https://space-finder-3000.onrender.com/"><b>▶ Live website</b></a> ·
  <a href="[video link]">30-second video</a> ·
  <a href="https://docs.google.com/presentation/d/19FWNbYBCe5dy4fP6lRDCuZEPNJt4_YItUPUjKZMTPqE/edit?usp=sharing">Slides</a>
</p>

<p align="center">
  <img alt="NASA Space Apps Challenge 2026" src="https://img.shields.io/badge/NASA%20Space%20Apps-2026-0b3d91">
  <img alt="Challenge: Planet X and SPHEREx" src="https://img.shields.io/badge/Challenge-Planet%20X%20and%20SPHEREx-6f42c1">
  <img alt="Data: NASA SPHEREx via IRSA" src="https://img.shields.io/badge/Data-NASA%20SPHEREx%20via%20IRSA-1f6feb">
  <img alt="Status: prototype" src="https://img.shields.io/badge/Status-prototype-orange">
</p>

> **A billion objects. One telescope. The whole sky, every six months.**
> Somewhere in those images, stars, comets and maybe new worlds are moving. Space Finder 3000 helps anyone go and look.

**1B+** objects imaged · **102** infrared bands · **6 months** per full-sky map · **100%** real observations

<p align="center">
  <img src="demo.gif" alt="[Describe the GIF: enter coordinates, start the scan, then play the frames and watch a star shift]" width="85%">
</p>
<!-- Record a 5-10 second GIF: enter coordinates, scan, press play. Save it as demo.gif next to this file. -->

**Jump to:** [Why this exists](#why-this-exists) · [What you can do](#what-you-can-do) · [Try it](#try-it-in-3-steps) · [How it works](#how-it-works) · [What's different](#whats-different) · [Limits](#honest-limits) · [What's next](#whats-next) · [Data and Credits](#data-and-credits) · [AI usage](#ai-usage) · [Team](#team) · [Run it Locally](#run-it-locally) · [Good First Targets](#good-first-targets)· [Notes](#notes) · [Acknowledgements and License](#acknowledgements-and-license)
notes
acknowledgement and license

---

## Why this exists

SPHEREx is mapping the entire sky in 102 bands of near-infrared light and repeating the map every six months. Anything that moves or changes between maps (comets, asteroids, nearby stars, brown dwarfs, and possibly new planets) gives itself away by shifting position.

But there is far too much sky for one person to scan, and the data is hard to reach if you aren't a researcher. We built Space Finder 3000 so a curious student can pick a spot and start looking in a minute.

## What you can do

| You want to… | Space Finder 3000 gives you |
|---|---|
| Look at a specific spot in the sky | Enter its **right ascension and declination** in degrees |
| See how that spot changes | The SPHEREx observations of it, **in time order**, to step through or play |
| Move around quickly | A frame counter and timeline, plus keyboard shortcuts |
| Make faint things easier to see | An image stretch that changes visibility, never the underlying measurements |
| Read the image like a sky map | **North is up and east is to the left** |
| Know what you're looking at | Plain notes in the app: every frame is a real observation, and the sequence is separate observations, not video |
| Popular Locations | A list of saved coordinate targets are provided in the Target Guidance |
| Know where you are in the sky | A survey grid / area guide that allows you to see your degree values |

**Keyboard shortcuts:** arrow keys step through frames · Shift + arrows jump five frames · Space plays or pauses

## Try it in 3 steps

1. **Aim.** Enter a right ascension and declination in degrees.
2. **Scan.** The tool downloads and processes the SPHEREx observations of that spot.
3. **Explore.** Step through the frames or press play, and watch for anything that shifts.

**Good first targets**

| Target | What it is | RA, Dec (degrees) | Why try it |
|---|---|---|---|
| Barnard's Star | A nearby red dwarf | 269.4521, +4.6933 | Holds the record for the largest known proper motion, about 10 arcseconds a year |
| Epsilon Indi B | A nearby brown dwarf | 331.0438, −56.7828 | A "hidden neighbor" that moves noticeably |
| R Draconis | A variable star | 248.1675, +66.7550 | It doesn't move, but its brightness changes |

<!-- Verify these positions work in the live tool before submitting. They are J2000 positions, and fast movers have drifted since, so a star may sit off-center. -->

## How it works

<img width="728" height="282" alt="Screenshot 2026-11-15 at 2 53 49 PM" src="https://github.com/user-attachments/assets/5a3d27d1-4716-456d-985d-e64ae233a7b4" />

**Images:** NASA SPHEREx data from the NASA/IPAC Infrared Science Archive (IRSA) and AWS Open Data.
**Stack:** Python 3 (server: `spherex_server.py`) with astroquery, astropy, numpy, requests, imageio, matplotlib and pillow. Front end: HTML, CSS and JavaScript, with Aladin for sky context. Data: NASA SPHEREx via IRSA and AWS Open Data. Hosting: Render.

## What's different

- **Honest about the data.** The app tells you each frame is a real observation, that the sequence is separate observations and not video, and that the image stretch never changes the measurements.
- **Built for ease.** A simple coordinate input and plain labels instead of a wall of complex controls.
- **Mission-control feel.** A frame counter, timeline and status readouts that make exploring the sky feel like an expedition.

## Honest limits

- The sequence is separate observations taken at different times, not continuous video, so expect gaps between frames.
- Very slow movers can shift by less than one pixel between images, so they can look like they're standing still.
- SPHEREx sees the sky through a variable filter, so a star's brightness differs from image to image. A brightness change only means something when comparing the same band.
- The IRSA API occasionally bugs out/crashes and does not display data.

## What's next

The prototype gets real SPHEREx frames in front of anyone. Next we want to make the sky easier to find and easier to understand, naming stars and large bodies and improving user quality.

- **Search by name.** Look up stars, galaxies and other objects by name, and show star and constellation names in the view, so nobody needs to know coordinates.
- **More data from other missions.** Options to view Gaia and WISE data alongside SPHEREx.
- **Motion highlighting.** Mark anything that shifts between observations so movers stand out.
- **Shareable links.** Put the coordinates in the URL so you can send an exact view to someone.
- **Resizable windows.** Drag, shrink and enlarge panels to build your own layout.

## Data and credits

Images: NASA SPHEREx via the NASA/IPAC Infrared Science Archive (IRSA) and AWS Open Data. SPHEREx is a joint project of the Jet Propulsion Laboratory and the California Institute of Technology, funded by NASA.

## AI usage

- **In the product:** none yet. The current prototype shows real SPHEREx data with no AI-generated content.
- **In development:** Used AI to proofread and assist with debugging code.
- When AI descriptions are added (in the future), we'll label them as AI-generated, because they are a starting point and not a verified scientific result.

## Team

| Name | Role |
|---|---|
| Abigail Diaz | Lead UI/UX/Graphic Designer, Asst. Software/Hardware Developer |
| Addison Drobny | Lead Software/Hardware Developer |
| Drake Cheever | Lead Subject Matter Specialist, Lead Storyteller/Communicator |
| EJ Mabanto | Lead Data Scientist/AI Specialist, Asst. Software/Hardware Developer |

---

## Run it locally

### Requirements
- Python 3
- macOS or Linux
- Internet connection to retrieve SPHEREx observations

### 1. Clone the repository

```bash
git clone https://github.com/aedrobny/NH2T-Planet-X-and-SPHEREx.git
cd NH2T-Planet-X-and-SPHEREx
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install the required Python packages
```bash
python -m pip install --upgrade pip
python -m pip install astroquery astropy numpy requests imageio matplotlib pillow
pip install -r requirements.txt
```

### 4. Start the local server
```bash
python spherex_server.py
```
Keep the terminal running while using the application.

### 5. Open the website

Visit: http://127.0.0.1:8000/index.html

### 6. Start exploring
1. Enter right ascension (RA) and declination (Dec) in decimal degrees. Some presets are also present to quickly load up some galaxies/stars images.

## Good first targets**

| Target | What it is | RA, Dec (degrees) | 
|---|---|---|
| Messier 51 (M51) | Whirlpool Galaxy | 202.4696 +47.1952 |
| Epsilon Indi B | A nearby brown dwarf | 331.0438, −56.7828 |
| R Draconis | A variable star | 248.1675, +66.7550 |

3. Click INITIATE SCAN.

4. The server retrieves available SPHEREx observations and processes them into PNG frames.
The output is stored under:

```bash
spherex_data/
└── ra_269.45208_dec_+4.69336/
    ├── fits/
    ├── frames/
    ├── frames_index.csv
    └── spherex_blink.gif
```

> Saved targets are discovered by scanning spherex_data/; the HTML does not contain a hardcoded coordinate or frame folder. If a target already has an index and PNG frames, selecting it loads the saved frames instead of downloading again. If only FITS files exist, initiating the scan reruns the downloader, which can reuse valid local FITS files and regenerate frames.


6. Explore the observations using the playback controls.
7. To stop the server, press `Ctrl+C` in its terminal.

## Notes

- RA must be in `[0, 360)` degrees; Dec must be in `[-90, 90]` degrees.
- Folder names use five decimal places and include an explicit `+` for positive declination.
- The local server listens only on `127.0.0.1`.

## Acknowledgements and license

Thank you to NASA, the SPHEREx team, IPAC/IRSA, and the NASA Space Apps Challenge organizers and mentors.

License: NASA
