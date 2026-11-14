"""SPHEREx Time Machine backend.

Finds SPHEREx Level-2 exposures covering a sky position (IRSA TAP), reads only the
needed pixel rows straight from the public AWS bucket (HTTP Range requests), and
resamples every exposure onto one common tangent-plane grid so frames from
different epochs line up pixel-for-pixel and can be blinked or differenced.
"""
import base64
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field
from scipy.ndimage import gaussian_filter, label, map_coordinates, maximum_filter, median_filter

ROOT = Path(__file__).resolve().parent.parent
if os.getenv("VERCEL"):
    CACHE = Path(tempfile.gettempdir()) / "spherex-cache"
else:
    CACHE = Path(os.getenv("SPHEREX_CACHE_DIR", str(ROOT / "cache")))
CACHE.mkdir(parents=True, exist_ok=True)

TAP = "https://irsa.ipac.caltech.edu/TAP/sync"
S3 = "https://nasa-irsa-spherex.s3.amazonaws.com/"
SBIDENT = "https://ssd-api.jpl.nasa.gov/sb_ident.api"
SESAME_MIRRORS = [  # CDS name resolver and its Harvard mirror (CDS is unreachable from some networks)
    "https://vizier.cfa.harvard.edu/viz-bin/nph-sesame",
    "https://cds.unistra.fr/cgi-bin/nph-sesame",
]
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
PIX_ARCSEC = 6.15  # SPHEREx native pixel scale
EPOCH_GAP_DAYS = 40  # exposures separated by more than this start a new epoch

DETECTORS = {
    "D1": (0.75, 1.09), "D2": (1.10, 1.62), "D3": (1.63, 2.41),
    "D4": (2.42, 3.82), "D5": (3.83, 4.41), "D6": (4.42, 5.00),
}

app = FastAPI(title="SPHEREx Time Machine")
_local = threading.local()


def session():
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def cache_path(kind, key, ext):
    h = hashlib.sha1(key.encode()).hexdigest()[:20]
    d = CACHE / kind
    d.mkdir(exist_ok=True)
    return d / f"{h}.{ext}"


# ---------------------------------------------------------------- search

def tap(query):
    r = session().post(TAP, data={"QUERY": query, "FORMAT": "csv"}, timeout=180)
    if r.status_code != 200 or r.text.lstrip().startswith("<"):
        raise HTTPException(502, "IRSA TAP query failed")
    return list(csv.DictReader(io.StringIO(r.text)))


@app.get("/api/frames")
def frames(ra: float, dec: float):
    """All SPHEREx exposures whose footprint contains (ra, dec), grouped into epochs."""
    key = f"{ra:.4f},{dec:.4f}"
    cp = cache_path("search", key, "json")
    if cp.exists():
        return JSONResponse(json.loads(cp.read_text()))

    # The CAOM plane table has the footprint polygon, so one spatial query + join returns
    # metadata and file paths together (~6 s, vs. obscore + per-ID lookups at 30 s+)
    obs = tap(f"""SELECT p.obs_publisher_did, p.energy_bandpassname, p.time_bounds_lower,
p.energy_bounds_lower, p.energy_bounds_upper, a.uri
FROM spherex.plane p JOIN spherex.artifact a ON a.planeid = p.planeid
WHERE a.producttype = 'science' AND CONTAINS(POINT('ICRS', {ra}, {dec}), p.poly) = 1""")

    rows = []
    for x in obs:
        uri = x["uri"]
        if not uri.endswith(".fits"):
            continue
        mjd = float(x["time_bounds_lower"])
        rows.append({
            "id": x["obs_publisher_did"].split("?", 1)[1],
            "det": x["energy_bandpassname"].replace("SPHEREx-", ""),
            "mjd": mjd,
            "date": Time(mjd, format="mjd").isot[:19].replace("T", " "),
            "wmin": round(float(x["energy_bounds_lower"]) * 1e6, 3),
            "wmax": round(float(x["energy_bounds_upper"]) * 1e6, 3),
            "key": uri.removeprefix("ibe/data/spherex/"),
        })
    rows.sort(key=lambda r: r["mjd"])

    epoch, last = 0, None
    for x in rows:
        if last is not None and x["mjd"] - last > EPOCH_GAP_DAYS:
            epoch += 1
        x["epoch"] = epoch
        last = x["mjd"]

    out = {"ra": ra, "dec": dec, "count": len(rows), "epochs": epoch + 1 if rows else 0,
           "detectors": DETECTORS, "frames": rows}
    cp.write_text(json.dumps(out))
    return out


# ---------------------------------------------------------------- pixel access

def read_header(url):
    """Fetch the primary + IMAGE extension headers; return (header, data byte offset)."""
    cp = cache_path("header", url, "fits")
    if cp.exists():
        buf = cp.read_bytes()
    else:
        buf = session().get(url, headers={"Range": "bytes=0-57599"}, timeout=60).content
        cp.write_bytes(buf)

    def one(off):
        i = off
        while i + 2880 <= len(buf):
            blk = buf[i:i + 2880]
            i += 2880
            for j in range(0, 2880, 80):
                if blk[j:j + 3] == b"END" and not blk[j + 3:j + 80].strip():
                    return fits.Header.fromstring(buf[off:i].decode("ascii")), i
        raise HTTPException(502, "FITS header not found")

    _, o = one(0)
    return one(o)


def read_rows(url, data_off, nx, y0, y1):
    """Read full-width rows [y0, y1) of the big-endian float32 IMAGE extension."""
    a, b = data_off + y0 * nx * 4, data_off + y1 * nx * 4 - 1
    raw = session().get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=120).content
    return np.frombuffer(raw, ">f4").reshape(-1, nx).astype(np.float32)


_wave_tables = {}


def wave_table(url, det):
    """SPHEREx's linear-variable filter: wavelength is a smooth function of detector pixel,
    tabulated on an 11x11 grid in the WCS-WAVE extension (same for every exposure of a detector)."""
    if det in _wave_tables:
        return _wave_tables[det]
    cp = CACHE / f"wave_{det}.npz"
    if not cp.exists():
        tail = session().get(url, headers={"Range": "bytes=-28800"}, timeout=60).content
        i = tail.rfind(b"XTENSION= 'BINTABLE'")
        if i < 0:
            raise HTTPException(502, "Wavelength table not found")
        prim = fits.PrimaryHDU().header.tostring().encode("ascii")
        with fits.open(io.BytesIO(prim + tail[i:])) as h:
            t = h[1].data
            np.savez(cp, x=t["X"][0], y=t["Y"][0], v=t["VALUES"][0][..., 0])
    z = np.load(cp)
    _wave_tables[det] = (z["x"], z["y"], z["v"])
    return _wave_tables[det]


def wavelength_at(key, det, ra, dec):
    url = S3 + key
    hdr, _ = read_header(url)
    x, y = WCS(hdr).world_to_pixel_values(ra, dec)
    gx, gy, v = wave_table(url, det)
    # table pixel coords are 1-based; v is indexed [y, x]
    fx = np.interp(x + 1, gx, np.arange(len(gx)))
    fy = np.interp(y + 1, gy, np.arange(len(gy)))
    return float(map_coordinates(v, [[fy], [fx]], order=1, mode="nearest")[0])


@app.get("/api/wavelengths")
def wavelengths(ra: float, dec: float, det: str):
    """Wavelength (µm) that each exposure of one detector saw at the target position."""
    data = frames(ra, dec)
    if isinstance(data, JSONResponse):
        data = json.loads(data.body)
    fs = [f for f in data["frames"] if f["det"] == det]
    if fs:
        wave_table(S3 + fs[0]["key"], det)

    def one(f):
        try:
            return f["key"], round(wavelength_at(f["key"], det, ra, dec), 4)
        except Exception:
            return f["key"], None

    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(one, fs))


def target_wcs(ra, dec, npix, scale_arcsec):
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra, dec]
    w.wcs.crpix = [(npix + 1) / 2, (npix + 1) / 2]
    w.wcs.cdelt = [-scale_arcsec / 3600, scale_arcsec / 3600]
    return w


def aligned(key, ra, dec, fov_arcmin, npix):
    """Exposure resampled onto the common grid, background subtracted. Cached as .npy."""
    cp = cache_path("aligned", f"{key}|{ra:.5f}|{dec:.5f}|{fov_arcmin}|{npix}", "npy")
    if cp.exists():
        return np.load(cp)

    url = S3 + key
    hdr, off = read_header(url)
    src = WCS(hdr)
    nx, ny = hdr["NAXIS1"], hdr["NAXIS2"]

    tw = target_wcs(ra, dec, npix, fov_arcmin * 60 / npix)
    yy, xx = np.mgrid[0:npix, 0:npix]
    wra, wdec = tw.pixel_to_world_values(xx, yy)
    sx, sy = src.world_to_pixel_values(wra, wdec)

    margin = 2
    y0 = max(int(math.floor(np.nanmin(sy))) - margin, 0)
    y1 = min(int(math.ceil(np.nanmax(sy))) + margin + 1, ny)
    if y1 - y0 < 4:
        raise HTTPException(404, "Position is outside this exposure")
    block = read_rows(url, off, nx, y0, y1)

    # Masked pixels are NaN. Inside a saturated star core, fill from the brightest
    # neighbour so the star doesn't become a black hole; everywhere else (cosmic-ray
    # hits, bad pixels) use the local median, so masks don't turn into fake sources.
    bad = ~np.isfinite(block)
    if bad.any():
        med = float(np.nanmedian(block))
        sig = robust_sigma(block)
        nb_max = maximum_filter(np.where(bad, -np.inf, block), size=7)
        nb_med = median_filter(np.where(bad, med, block), size=5)
        fill = np.where(nb_max > med + 50 * sig, nb_max, nb_med)
        block = np.where(bad, fill, block)
        block = np.where(np.isfinite(block), block, med)
    # Dead / unflagged bad pixels show up as dark specks: nothing on the sky is far
    # below its surroundings, so replace strong negative outliers with the local median
    local = median_filter(block, size=3)
    dead = block < local - 5 * robust_sigma(block - local)
    block = np.where(dead, local, block)
    img = map_coordinates(block, [sy - y0, sx], order=1, mode="constant", cval=np.nan)
    inside = (sx >= 0) & (sx <= nx - 1) & (sy >= 0) & (sy <= ny - 1)
    img[~inside] = np.nan
    img -= np.nanmedian(img)  # remove zodiacal + sky background level

    np.save(cp, img.astype(np.float32))
    return img


def robust_sigma(a):
    a = a[np.isfinite(a)]
    if a.size == 0:
        return 1.0
    return float(1.4826 * np.median(np.abs(a - np.median(a)))) or 1.0


def to_png(rgb_or_gray):
    buf = io.BytesIO()
    Image.fromarray(rgb_or_gray).save(buf, "PNG", optimize=True)
    return buf.getvalue()


def stretch(img, lo=-2.0, hi=60.0, soft=3.0):
    """asinh stretch in units of the image noise: faint sources visible, bright ones not clipped."""
    s = robust_sigma(img)
    z = np.clip(img / s, lo, hi)
    v = (np.arcsinh(z / soft) - np.arcsinh(lo / soft)) / (np.arcsinh(hi / soft) - np.arcsinh(lo / soft))
    out = (np.nan_to_num(v, nan=0.0) * 255).astype(np.uint8)
    return out


def diverging(d):
    """Blue = brighter in A, green = brighter in B (matches the A/B colors in the UI)."""
    s = robust_sigma(d)
    z = np.clip(np.nan_to_num(d / s), -8, 8) / 8
    pos, neg = np.clip(z, 0, 1)[..., None], np.clip(-z, 0, 1)[..., None]
    base = np.array([11, 13, 16], np.float32)
    b_col = np.array([61, 220, 151], np.float32)
    a_col = np.array([90, 169, 255], np.float32)
    rgb = base + pos * (b_col - base) + neg * (a_col - base)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def comparison_data(a, b, ra, dec, fov, npix=256):
    """Return PSF-matched A/B images, their difference, and the fitted flux scale."""
    sig = PIX_ARCSEC / (fov * 60 / npix) * 0.6
    ia = gaussian_filter(np.nan_to_num(aligned(a, ra, dec, fov, npix)), sig)
    ib = gaussian_filter(np.nan_to_num(aligned(b, ra, dec, fov, npix)), sig)
    bright = np.isfinite(ia) & np.isfinite(ib) & (ia > 5 * robust_sigma(ia))
    k = float(np.median(ib[bright] / ia[bright])) if bright.sum() > 20 else 1.0
    k = min(max(k, 0.3), 3.0)
    return ia, ib / k, ib - k * ia, k


def change_metrics(d):
    """Deterministic evidence supplied alongside the images to the AI."""
    s = robust_sigma(d)
    z = np.nan_to_num(d / s)
    mask = np.abs(z) >= 5
    labs, count = label(mask)
    areas = np.bincount(labs.ravel())[1:] if count else np.array([], dtype=int)
    significant = areas[areas >= 2]
    return {
        "noise_sigma": round(s, 6),
        "max_positive_sigma": round(float(np.max(z)), 2),
        "max_negative_sigma": round(float(np.min(z)), 2),
        "pixels_above_5_sigma": int(mask.sum()),
        "regions_above_5_sigma": int(significant.size),
        "largest_region_pixels": int(significant.max()) if significant.size else 0,
    }


@app.get("/api/image")
def image(key: str, ra: float, dec: float, fov: float = Query(12, gt=1, le=60), npix: int = Query(256, ge=64, le=512)):
    img = aligned(key, ra, dec, fov, npix)
    if not np.isfinite(img).any():
        raise HTTPException(404, "No data at this position")
    return Response(to_png(stretch(img)), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/diff")
def diff(a: str, b: str, ra: float, dec: float, fov: float = Query(12, gt=1, le=60), npix: int = Query(256, ge=64, le=512)):
    # SPHEREx pixels are 6" and undersample the PSF, which also changes across the focal
    # plane. Blur both to a common resolution (~native pixel) so steady stars cancel.
    _, _, delta, _ = comparison_data(a, b, ra, dec, fov, npix)
    return Response(to_png(diverging(delta)), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- AI-assisted interpretation

class AIAnalyzeRequest(BaseModel):
    a: str
    b: str
    ra: float = Field(ge=0, lt=360)
    dec: float = Field(ge=-90, le=90)
    fov: float = Field(default=12, gt=1, le=60)
    date_a: str = ""
    date_b: str = ""
    mjd_a: float | None = None
    mjd_b: float | None = None
    wave_a: float | None = None
    wave_b: float | None = None


AI_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "classification": {"type": "STRING", "enum": [
            "possible_motion", "brightness_change", "known_object",
            "likely_artifact", "no_significant_change", "inconclusive"
        ]},
        "confidence": {"type": "NUMBER"},
        "summary": {"type": "STRING"},
        "evidence": {"type": "ARRAY", "items": {"type": "STRING"}},
        "known_object_match": {"type": "BOOLEAN"},
        "limitations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "next_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["classification", "confidence", "summary", "evidence",
                 "known_object_match", "limitations", "next_steps"],
}


def _gemini_image(png):
    return {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}


@app.post("/api/ai-analyze")
def ai_analyze(req: AIAnalyzeRequest):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(503, "AI analysis is not configured. Set GEMINI_API_KEY on the server.")

    cache_key = json.dumps(req.dict(), sort_keys=True) + "|" + GEMINI_MODEL
    cp = cache_path("ai", cache_key, "json")
    if cp.exists():
        return JSONResponse(json.loads(cp.read_text(encoding="utf-8")))

    try:
        ia, ib, delta, flux_scale = comparison_data(req.a, req.b, req.ra, req.dec, req.fov)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, f"Could not prepare the comparison: {exc}") from exc

    metrics = change_metrics(delta)
    wavelength_gap = None
    if req.wave_a is not None and req.wave_b is not None:
        wavelength_gap = abs(req.wave_b - req.wave_a)
    known_objects = []
    catalog_checked = False
    for mjd in (req.mjd_a, req.mjd_b):
        if mjd is None:
            continue
        sso_cache = cache_path("sso", f"{req.ra:.4f}|{req.dec:.4f}|{mjd:.4f}|{req.fov}", "json")
        if sso_cache.exists():
            catalog_checked = True
            known_objects.extend(json.loads(sso_cache.read_text()).get("objects", []))
    metadata = {
        "coordinates_deg": {"ra": req.ra, "dec": req.dec},
        "field_of_view_arcmin": req.fov,
        "frame_a": {"date": req.date_a, "wavelength_um": req.wave_a},
        "frame_b": {"date": req.date_b, "wavelength_um": req.wave_b},
        "wavelength_gap_um": wavelength_gap,
        "flux_scale_b_over_a": round(flux_scale, 4),
        "difference_metrics": metrics,
        "jpl_catalog_checked": catalog_checked,
        "known_jpl_objects": known_objects[:25],
    }
    prompt = """You are assisting with a NASA SPHEREx educational time-domain viewer.
Analyze three supplied panels in order: aligned frame A, aligned frame B, and a color difference B-A.
In the difference image, green means brighter in B and blue means brighter in A. A nearby blue/green
pair may indicate motion, while a same-position residual may indicate brightness change. Be conservative:
PSF mismatch, detector masks, saturation, noise, imperfect alignment, and differing wavelengths can all
create artifacts. Never claim a discovery and never identify an object without catalog evidence.
Only use known_object when jpl_catalog_checked is true and the supplied catalog supports it.
Use only the allowed classification values. Confidence must be between 0 and 1. Keep the summary and
each list item concise and understandable to a general audience. If wavelengths differ by more than
0.02 micrometers, explicitly make that a limitation. Metadata follows:\n""" + json.dumps(metadata)

    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"text": "Frame A"}, _gemini_image(to_png(stretch(ia))),
            {"text": "Frame B"}, _gemini_image(to_png(stretch(ib))),
            {"text": "Difference B-A"}, _gemini_image(to_png(diverging(delta))),
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "responseSchema": AI_SCHEMA,
        },
    }
    try:
        r = session().post(GEMINI_API.format(model=GEMINI_MODEL), params={"key": api_key}, json=payload, timeout=90)
        if not r.ok:
            message = r.json().get("error", {}).get("message") if "json" in r.headers.get("content-type", "") else None
            raise HTTPException(502, message or "Google AI request failed")
        response_data = r.json()
        candidates = response_data.get("candidates") or []
        if not candidates:
            feedback = response_data.get("promptFeedback") or {}
            reason = feedback.get("blockReason") or "no candidate was returned"
            raise HTTPException(502, f"Google AI could not produce an analysis: {reason}")

        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text_parts = [part.get("text", "") for part in parts if part.get("text")]
        if not text_parts:
            reason = candidate.get("finishReason") or "empty response"
            raise HTTPException(502, f"Google AI returned no analysis text: {reason}")

        response_text = "\n".join(text_parts).strip()
        if response_text.startswith("```"):
            response_text = response_text.removeprefix("```json").removeprefix("```")
            response_text = response_text.removesuffix("```").strip()
        result = json.loads(response_text)
        result["confidence"] = max(0.0, min(1.0, float(result["confidence"])))
        result["metrics"] = metrics
        result["model"] = GEMINI_MODEL
        result["disclaimer"] = "AI-assisted interpretation — not a confirmed scientific discovery."
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, "Could not connect to the Google AI service.") from exc
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(502, "The AI service returned an unreadable result.") from exc

    cp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


# ---------------------------------------------------------------- known moving objects

@app.get("/api/solar-system")
def solar_system(ra: float, dec: float, mjd: float, fov: float = 12):
    """Known asteroids and comets inside the field at the exposure time (JPL SB Identification)."""
    cp = cache_path("sso", f"{ra:.4f}|{dec:.4f}|{mjd:.4f}|{fov}", "json")
    if cp.exists():
        return JSONResponse(json.loads(cp.read_text()))

    def sexa(v, hours):
        if hours:
            v /= 15
        sign = "-" if v < 0 else ""
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        s = (v - d - m / 60) * 3600
        return f"{sign}{d:02d}-{m:02d}-{s:05.2f}"

    hw = fov / 60 / 2
    params = {
        "obs-time": f"{mjd + 2400000.5:.5f}",
        "fov-ra-center": sexa(ra, True), "fov-dec-center": sexa(dec, False),
        "fov-ra-hwidth": f"{hw / max(math.cos(math.radians(dec)), 0.05):.4f}",
        "fov-dec-hwidth": f"{hw:.4f}", "two-pass": "true", "mpc-code": "500",
    }
    def query(kind):
        try:
            # JPL's two-pass search is precise but slow (often over a minute)
            j = session().get(SBIDENT, params={**params, "sb-kind": kind}, timeout=240).json()
        except Exception:
            return None
        found = []
        fields = j.get("fields_second") or []
        for row in j.get("data_second_pass") or []:
            d = dict(zip(fields, row))
            try:
                dra = float(d['Dist. from center RA (")'])
                ddec = float(d['Dist. from center Dec (")'])
            except (KeyError, ValueError):
                continue
            found.append({
                "name": d.get("Object name"), "kind": "comet" if kind == "c" else "asteroid",
                "dx": dra, "dy": ddec, "vmag": d.get("Visual magnitude (V)"),
                "rate_ra": d.get('RA rate ("/h)'), "rate_dec": d.get('Dec rate ("/h)'),
            })
        return found

    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(query, ("a", "c")))
    if all(r is None for r in results):
        raise HTTPException(502, "JPL Small-Body Identification service did not answer")
    out = [o for r in results if r for o in r]
    res = {"objects": out}
    cp.write_text(json.dumps(res))
    return res


@app.get("/api/resolve")
def resolve(name: str):
    """Object name -> RA/Dec via CDS Sesame (it blocks browser CORS, so we proxy it)."""
    q = requests.utils.quote(name.strip())
    answered = False
    for mirror in SESAME_MIRRORS:
        try:
            txt = session().get(f"{mirror}/-oI/SNV?{q}", timeout=8).text
        except Exception:
            continue  # mirror down or blocked: try the next one
        answered = True
        for line in txt.splitlines():
            if line.startswith("%J "):
                ra, dec = line.split()[1:3]
                return {"name": name, "ra": float(ra), "dec": float(dec)}
        break
    if not answered:
        raise HTTPException(502, "Name resolver unavailable")
    raise HTTPException(404, "Object not found")


# ---------------------------------------------------------------- cache warm-up

# Same fields as the field guide in web/app.js. IRSA can take 30 s+ per search, so the
# server fills the cache for them in the background and the presets open instantly.
PRESETS = [(83.822, -5.391), (180.0, 0.0), (56.75, 24.117), (266.417, -29.008),
           (10.685, 41.269), (270.0, 66.561), (161.265, -59.685)]


def _warm():
    for ra, dec in PRESETS:
        try:
            wavelengths(round(ra, 5), round(dec, 5), "D2")
        except Exception:
            pass


if not os.getenv("VERCEL"):
    @app.on_event("startup")
    def warm_cache():
        threading.Thread(target=_warm, daemon=True).start()


# ---------------------------------------------------------------- frontend

app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
