# SPHEREx Time Machine

A public web tool for NASA Space Apps 2026: pick any spot on the sky and watch how it changes
between NASA SPHEREx all-sky surveys (one every six months, 102 near-infrared colors).

## Run it

```bash
pip install -r requirements.txt
python -m uvicorn server.app:app --port 8000
```

Open http://localhost:8000.

## Deploy to Vercel

The repository includes `api/index.py` and `vercel.json` for a Vercel Python deployment.
Set the following environment variables in the Vercel project instead of committing a local
`.env` file:

- `GEMINI_API_KEY` — Google AI Studio key (secret)
- `GEMINI_MODEL` — `gemini-3.6-flash`

Vercel uses its writable `/tmp` directory for runtime caches. That cache is ephemeral, so a cold
serverless instance can take longer to load its first SPHEREx frame. The browser never receives
the Gemini key. Store `GEMINI_API_KEY` as a Vercel **Secret** environment variable; never commit
the key or a local `.env` file to GitHub.

### Enable AI Change Analysis

The optional **Analyze with AI** panel sends the two selected, aligned frames plus their
difference image and deterministic signal measurements to a Gemini Flash model. The API key
stays on the server and is never sent to the browser.

PowerShell:

```powershell
$env:GEMINI_API_KEY="your Google AI Studio key"
$env:GEMINI_MODEL="gemini-3.6-flash"  # optional; use a model available to your account
python -m uvicorn server.app:app --port 8000
```

The result is an AI-assisted interpretation, not a confirmed scientific discovery. The model
is explicitly instructed to consider wavelength mismatch, noise, saturation, PSF mismatch,
bad pixels, and alignment artifacts. Identical analyses are cached under `cache/ai/` to avoid
spending the free API quota twice.

## What it does

1. **Sky map** (Aladin Lite + 2MASS / WISE / DSS backgrounds served by IRSA). Click a spot, use a preset, or
   search by name (`M42`) or coordinates (`83.82 -5.39`).
2. The backend finds **every SPHEREx exposure** covering that spot (IRSA TAP, collection `spherex_qr2`).
3. Each exposure is **resampled onto one common sky grid**, so all frames line up pixel for pixel.
4. Viewer modes: **Blink**, **Swipe**, **Difference** (B − A, steady stars cancel), **Movie** (one frame per
   survey), **Side by side**.
5. **Same-color matching.** SPHEREx uses a linear-variable filter: each exposure sees a given spot at a
   different wavelength. We compute that wavelength from the file's `WCS-WAVE` table and auto-pick a B frame
   at the same wavelength as A, so the comparison is fair.
6. **Known asteroids and comets** at the exposure time are overlaid from the JPL Small-Body Identification API.
   An object that moves and is *not* marked is a candidate for something new.

## How it works (for the team)

| Part | File | Notes |
| --- | --- | --- |
| API + static server | `server/app.py` | FastAPI, everything cached under `cache/` |
| UI | `web/index.html`, `web/style.css`, `web/app.js` | no build step; Aladin Lite via jsDelivr ES module |

Endpoints:

- `GET /api/frames?ra&dec`: list of exposures `{id, det, mjd, date, epoch, key}`, grouped into surveys (epochs)
- `GET /api/wavelengths?ra&dec&det`: wavelength (µm) each exposure saw at the target
- `GET /api/image?key&ra&dec&fov`: aligned, background-subtracted, asinh-stretched PNG
- `GET /api/diff?a&b&ra&dec&fov`: PSF-smoothed, flux-matched difference image (orange = brighter in B)
- `GET /api/solar-system?ra&dec&mjd&fov`: known small bodies in the field (JPL, slow: about 1 min, then cached)
- `GET /api/resolve?name`: object name to coordinates (CDS Sesame, Harvard mirror first; CDS is unreachable from some networks)
- `POST /api/ai-analyze`: conservative Gemini interpretation of A, B, Difference, and measured signal statistics

On startup the server warms the cache for every field-guide preset in the background, because IRSA
takes anywhere from 4 to 40 s to answer a search. After warm-up the presets open in a few seconds,
which matters for a live demo.

**Fast pixel access.** The IRSA cutout service takes about 25 s per image. Instead, we read the public AWS
bucket `nasa-irsa-spherex` directly with HTTP Range requests: one request for the header, one for just the
needed rows of the `IMAGE` extension (about 1 MB instead of 70 MB). That takes about 4 s per frame on a
cold cache and is instant after that.

## Data and credits

- SPHEREx Quick Release Level 2 images: NASA/IPAC Infrared Science Archive (IRSA), AWS Open Data
- JPL Small-Body Identification API (SSD/CNEOS)
- Aladin Lite (CDS, Strasbourg); 2MASS, AllWISE and DSS HiPS mirrored by IRSA

## Ideas / next steps

- "Report a mover" button: save user-marked candidates (citizen science), export to MPC format
- Automatic mover detection: find unpaired + / − blobs in the difference image and list them
- Light curves: brightness of a clicked star across all exposures (variable stars, YSOs)
- Per-exposure SPHEREx spectrum of a clicked source (102 colors)
- Deploy: one small VM or Render/Fly.io (needs Python; the `cache/` folder can live on a volume)
