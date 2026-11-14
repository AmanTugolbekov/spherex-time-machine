// SPHEREx Time Machine frontend
import A from "https://cdn.jsdelivr.net/npm/aladin-lite@3.6.5/dist/aladin.js";
const PRESETS = [
  { name: "Orion Nebula", note: "star nursery", ra: 83.822, dec: -5.391 },
  { name: "Ecliptic field", note: "asteroid highway", ra: 180.0, dec: 0.0 },
  { name: "Pleiades", note: "young cluster", ra: 56.75, dec: 24.117 },
  { name: "Galactic Center", note: "crowded core", ra: 266.417, dec: -29.008 },
  { name: "Andromeda", note: "nearest big galaxy", ra: 10.685, dec: 41.269 },
  { name: "North Ecliptic Pole", note: "deep field, seen most often", ra: 270.0, dec: 66.561 },
  { name: "Eta Carinae", note: "unstable giant star", ra: 161.265, dec: -59.685 },
];
const DET_COLORS = { D1: "#8fb4ff", D2: "#6fc6ff", D3: "#55d2e8", D4: "#4fd9c2", D5: "#46dca0", D6: "#7fe07a" };

const $ = (s) => document.querySelector(s);
const state = {
  ra: null, dec: null, data: null, det: null, frames: [], a: null, b: null,
  mode: "blink", timer: null, fov: 12, sso: {}, aiSignature: null,
};
let aladin, marker;

// ------------------------------------------------------------------ sky map
A.init.then(() => {
  aladin = A.aladin("#aladin", {
    survey: "https://irsa.ipac.caltech.edu/data/hips/CDS/2MASS/Color", fov: 3, target: "83.822 -5.391", projection: "AIT",
    showReticle: true, reticleColor: "#3ddc97", showCooGrid: false, showFullscreenControl: false,
    showLayersControl: false, showGotoControl: false, showShareControl: false, log: false,
  });
  marker = A.graphicOverlay({ color: "#3ddc97", lineWidth: 2 });
  aladin.addOverlay(marker);
  aladin.on("click", (e) => {
    if (!e) return;
    let ra = e.ra, dec = e.dec;
    if (ra === undefined && e.x !== undefined) [ra, dec] = aladin.pix2world(e.x, e.y);
    if (ra !== undefined) setTarget(ra, dec, false);
  });
});

$("#survey").addEventListener("change", (e) => aladin.setImageSurvey(e.target.value));

function drawMarker() {
  if (!marker || state.ra === null) return;
  marker.removeAll();
  const r = state.fov / 60 / 2;
  const c = Math.cos((state.dec * Math.PI) / 180) || 1;
  marker.add(A.polygon([
    [state.ra - r / c, state.dec - r], [state.ra + r / c, state.dec - r],
    [state.ra + r / c, state.dec + r], [state.ra - r / c, state.dec + r],
  ]));
}

function setTarget(ra, dec, load) {
  state.ra = (ra + 360) % 360;
  state.dec = dec;
  $("#target").textContent = `RA ${state.ra.toFixed(4)}°  Dec ${state.dec >= 0 ? "+" : ""}${state.dec.toFixed(4)}°`;
  drawMarker();
  if (load) loadFrames();
  else status("New spot selected. Press “Load SPHEREx images here” to see it.", "hint");
}

// ------------------------------------------------------------------ presets & search
const presetBox = $("#presets");
PRESETS.forEach((p) => {
  const b = document.createElement("button");
  b.innerHTML = `${p.name}<small>${p.note}</small>`;
  b.onclick = () => {
    aladin?.gotoRaDec(p.ra, p.dec);
    aladin?.setFoV(2);
    setTarget(p.ra, p.dec, true);
  };
  presetBox.appendChild(b);
});

// "83.82 -5.39" (degrees) or "05 35 17.3 -05 23 28" / "05:35:17.3 -05:23:28" (hours, degrees)
function parseCoords(q) {
  const t = q.replace(/−/g, "-").replace(/[hdms°'"′″:,]/g, " ").trim().split(/\s+/);
  if (!t.every((x) => /^[+-]?\d+(\.\d+)?$/.test(x))) return null;
  if (t.length === 2) return [+t[0], +t[1]];
  if (t.length !== 6) return null;
  const ra = (+t[0] + t[1] / 60 + t[2] / 3600) * 15;
  const sign = t[3].startsWith("-") ? -1 : 1;
  return [ra, sign * (Math.abs(+t[3]) + t[4] / 60 + t[5] / 3600)];
}

$("#search").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  const coords = parseCoords(q);
  if (coords) {
    const [ra, dec] = coords;
    if (!(ra >= 0 && ra < 360 && dec >= -90 && dec <= 90)) {
      return status("Coordinates out of range: RA must be 0–360° and Dec −90…+90°.", "err");
    }
    aladin.gotoRaDec(ra, dec);
    aladin.setFoV(2);
    return setTarget(ra, dec, true);
  }
  status(`Looking up “${q}”…`, "busy");
  fetch(`/api/resolve?name=${encodeURIComponent(q)}`)
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then(({ ra, dec }) => {
      aladin.gotoRaDec(ra, dec);
      aladin.setFoV(2);
      setTarget(ra, dec, true);
    })
    .catch(() => status(`Could not find “${q}”. Try another name or RA Dec in degrees.`, "err"));
});

$("#loadHere").onclick = () => {
  if (state.ra === null) {
    const [ra, dec] = aladin.getRaDec();
    setTarget(ra, dec, false);
  }
  loadFrames();
};

$("#helpBtn").onclick = () => $("#help").showModal();

// ------------------------------------------------------------------ data
let statusTicker = null;
function status(msg, cls = "") {
  const s = $("#status");
  clearInterval(statusTicker);
  s.hidden = !msg;
  s.textContent = msg || "";
  s.className = "status " + cls;
  if (cls === "busy") {
    // the archive can take half a minute; a running clock shows nothing is stuck
    const t0 = Date.now();
    statusTicker = setInterval(() => {
      const sec = Math.round((Date.now() - t0) / 1000);
      s.textContent = `${msg} ${sec} s${sec > 15 ? " (the archive is busy; new spots can take up to a minute)" : ""}`;
    }, 1000);
  }
}

async function loadFrames() {
  if (state.ra === null) return;
  stopTimer();
  $("#controls").hidden = true;
  $("#stage").hidden = true;
  $("#aiPanel").hidden = true;
  status("Asking the NASA/IPAC archive which SPHEREx images cover this spot…", "busy");
  try {
    const r = await fetch(`/api/frames?ra=${state.ra.toFixed(5)}&dec=${state.dec.toFixed(5)}`);
    if (!r.ok) throw new Error(await r.text());
    state.data = await r.json();
  } catch (err) {
    return status("The archive did not answer. Please try again in a moment.", "err");
  }
  const d = state.data;
  if (!d.count) return status("SPHEREx has not released images of this spot yet. Try another place.", "err");
  status(`Found ${d.count} SPHEREx exposures of this spot across ${d.epochs} sky survey${d.epochs > 1 ? "s" : ""}.`);
  buildDetectors();
  $("#controls").hidden = false;
  $("#stage").hidden = false;
  $("#aiPanel").hidden = false;
}

function buildDetectors() {
  const box = $("#dets");
  box.innerHTML = "";
  const counts = {};
  state.data.frames.forEach((f) => (counts[f.det] = (counts[f.det] || 0) + 1));
  let best = null;
  Object.entries(state.data.detectors).forEach(([det, [lo, hi]]) => {
    const b = document.createElement("button");
    b.innerHTML = `<span class="swatch" style="background:${DET_COLORS[det]}"></span>${lo}–${hi} µm`;
    b.title = `Detector ${det}: ${counts[det] || 0} exposures`;
    b.disabled = !counts[det];
    b.dataset.det = det;
    b.onclick = () => selectDetector(det);
    box.appendChild(b);
    if (counts[det] && (!best || det === "D2")) best = det;
  });
  selectDetector(state.det && counts[state.det] ? state.det : best);
}

async function selectDetector(det) {
  state.det = det;
  document.querySelectorAll("#dets button").forEach((b) => b.classList.toggle("on", b.dataset.det === det));
  const fs = state.data.frames.filter((f) => f.det === det);
  state.frames = fs;

  // SPHEREx's filter changes color across the detector, so each exposure saw this spot at
  // a different wavelength. Fetch them so A and B can be compared at the same color.
  if (!fs[0].wave) {
    status("Working out which color each exposure saw here…", "busy");
    try {
      const r = await fetch(`/api/wavelengths?ra=${state.ra.toFixed(5)}&dec=${state.dec.toFixed(5)}&det=${det}`);
      const w = await r.json();
      fs.forEach((f) => (f.wave = w[f.key] ?? null));
    } catch { /* wavelengths are a nicety; carry on without them */ }
    if (state.det !== det) return;
    status(`Found ${state.data.count} SPHEREx exposures of this spot across ${state.data.epochs} sky survey${state.data.epochs > 1 ? "s" : ""}.`);
  }

  const lastEpoch = fs[fs.length - 1].epoch;
  state.a = fs[0];
  state.b = lastEpoch > 0 ? closestWave(state.a, lastEpoch) : fs[fs.length - 1];
  buildSelects();
  buildTimeline();
  render();
}

function closestWave(ref, epoch) {
  const cands = state.frames.filter((f) => f.epoch === epoch && f !== ref);
  if (!cands.length) return ref;
  if (ref.wave == null) return cands[0];
  return cands.reduce((best, f) => (Math.abs((f.wave ?? 99) - ref.wave) < Math.abs((best.wave ?? 99) - ref.wave) ? f : best));
}

function label(f) {
  const w = f.wave ? ` · ${f.wave.toFixed(3)} µm` : "";
  return `${f.date.slice(0, 16)} · S${f.epoch + 1}${w}`;
}

function matchB() {
  if (!state.a) return;
  const eps = [...new Set(state.frames.map((f) => f.epoch))].filter((e) => e !== state.a.epoch);
  const ep = eps.includes(state.b.epoch) ? state.b.epoch : eps[eps.length - 1] ?? state.a.epoch;
  state.b = closestWave(state.a, ep);
  $("#selB").value = state.frames.indexOf(state.b);
  buildTimeline();
  render();
}
$("#matchB").onclick = matchB;

function buildSelects() {
  ["A", "B"].forEach((k) => {
    const sel = $("#sel" + k);
    sel.innerHTML = state.frames.map((f, i) => `<option value="${i}">${label(f)}</option>`).join("");
    sel.value = state.frames.indexOf(k === "A" ? state.a : state.b);
    sel.onchange = () => {
      state[k.toLowerCase()] = state.frames[+sel.value];
      buildTimeline();
      render();
    };
  });
}

function buildTimeline() {
  const tl = $("#timeline");
  tl.innerHTML = "";
  const fs = state.frames;
  // compress the gaps between surveys so each survey gets an equal slice of the bar
  const epochs = [...new Set(fs.map((f) => f.epoch))];
  const slice = 100 / epochs.length;
  // vertical position = wavelength, so exposures at the same color line up horizontally
  const ws = fs.map((f) => f.wave).filter((w) => w != null);
  const wlo = Math.min(...ws), whi = Math.max(...ws);
  const yOf = (f) => (f.wave == null || whi <= wlo ? 50 : ((f.wave - wlo) / (whi - wlo)) * 100);
  tl.title = ws.length ? `Dots: each exposure. Left→right: time. Bottom→top: wavelength ${wlo.toFixed(2)}–${whi.toFixed(2)} µm` : "";
  epochs.forEach((ep, i) => {
    const efs = fs.filter((f) => f.epoch === ep);
    const t0 = efs[0].mjd, t1 = efs[efs.length - 1].mjd;
    const head = document.createElement("span");
    head.className = "ep";
    head.style.left = `${slice * (i + 0.5)}%`;
    head.innerHTML = `<span class="long">Survey ${ep + 1} · </span><span class="short">S${ep + 1} · </span>${efs[0].date.slice(0, 7)}`;
    tl.appendChild(head);
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.style.left = `${slice * i}%`;
      tl.appendChild(sep);
    }
    efs.forEach((f) => {
      const x = slice * i + slice * (0.08 + 0.84 * (t1 > t0 ? (f.mjd - t0) / (t1 - t0) : 0.5));
      const dot = document.createElement("button");
      dot.className = "dot" + (f === state.a ? " a" : "") + (f === state.b ? " b" : "");
      dot.style.left = `${x}%`;
      dot.style.bottom = `calc(6px + ${yOf(f) / 100} * (100% - 30px))`;
      dot.title = `${label(f)}\nclick = set B, shift+click = set A`;
      dot.onclick = (e) => {
        state[e.shiftKey ? "a" : "b"] = f;
        $("#selA").value = fs.indexOf(state.a);
        $("#selB").value = fs.indexOf(state.b);
        buildTimeline();
        render();
      };
      tl.appendChild(dot);
    });
  });
}

// ------------------------------------------------------------------ rendering
const imgURL = (f) => `/api/image?key=${encodeURIComponent(f.key)}&ra=${state.ra.toFixed(5)}&dec=${state.dec.toFixed(5)}&fov=${state.fov}&v=4`;
const diffURL = (a, b) => `/api/diff?a=${encodeURIComponent(a.key)}&b=${encodeURIComponent(b.key)}&ra=${state.ra.toFixed(5)}&dec=${state.dec.toFixed(5)}&fov=${state.fov}&v=4`;

function setFrame(el, url, caption, f) {
  const img = el.querySelector("img");
  el.querySelector(".cap").textContent = caption;
  el.dataset.mjd = f ? f.mjd : "";
  if (img.dataset.src === url) return drawOverlay(el);
  img.dataset.src = url;
  el.classList.add("loading");
  img.onload = () => { el.classList.remove("loading"); drawOverlay(el); };
  img.onerror = () => { el.classList.remove("loading"); el.querySelector(".cap").textContent = caption + " · no data here"; };
  img.src = url;
}

function stopTimer() {
  clearInterval(state.timer);
  state.timer = null;
}

function render() {
  if (!state.a) return;
  stopTimer();
  const fA = $("#fA"), fB = $("#fB"), frames = $("#frames"), swipe = $("#swipe");
  const { a, b, mode } = state;
  const signature = `${a.key}|${b.key}|${state.ra}|${state.dec}|${state.fov}`;
  if (state.aiSignature && state.aiSignature !== signature) resetAI();
  frames.classList.toggle("side", mode === "side");
  swipe.hidden = mode !== "swipe";
  fB.style.clipPath = "";
  fB.style.visibility = "visible";
  fA.style.visibility = "visible";
  document.querySelectorAll("#modes button").forEach((x) => x.classList.toggle("on", x.dataset.mode === mode));
  const speed = () => +$("#speed").value;
  const legend = $("#legend");

  if (mode === "diff") {
    setFrame(fA, diffURL(a, b), `B − A`, null);
    fB.style.visibility = "hidden";
    legend.innerHTML = `<span class="sw b"></span>brighter in B &nbsp; <span class="sw a"></span>brighter in A. Moving objects show up as green–blue pairs; steady stars vanish.`;
    return;
  }
  if (mode === "movie") {
    // one exposure per sky survey, plus any extra frames from the same survey if there is only one
    const eps = [...new Set(state.frames.map((f) => f.epoch))];
    let seq = eps.map((ep) => (ep === state.a.epoch ? state.a : closestWave(state.a, ep)));
    if (seq.length < 2) seq = state.frames.slice(0, 8);
    fB.style.visibility = "hidden";
    seq.forEach((f) => (new Image().src = imgURL(f))); // preload
    let i = 0;
    const step = () => {
      const f = seq[i % seq.length];
      setFrame(fA, imgURL(f), `${f.date.slice(0, 10)} · survey ${f.epoch + 1}`, f);
      i++;
    };
    step();
    state.timer = setInterval(step, speed() * 1.5);
    legend.textContent = `Playing ${seq.length} snapshots, one from each six-month SPHEREx sky survey.`;
    return;
  }

  setFrame(fA, imgURL(a), `A · ${a.date.slice(0, 16)}`, a);
  setFrame(fB, imgURL(b), `B · ${b.date.slice(0, 16)}`, b);
  const dt = Math.abs(b.mjd - a.mjd);
  let gap = (dt > 2 ? `${dt.toFixed(0)} days` : `${(dt * 24).toFixed(1)} hours`) + " apart";
  if (a.wave && b.wave) {
    const dw = Math.abs(a.wave - b.wave);
    gap += dw < 0.02 ? ` at the same wavelength (${a.wave.toFixed(2)} µm)` : `, at ${a.wave.toFixed(2)} vs ${b.wave.toFixed(2)} µm (different colors, so brightness may differ too; try “Match color”)`;
  }

  if (mode === "blink") {
    let on = false;
    fB.style.visibility = "hidden";
    state.timer = setInterval(() => {
      on = !on;
      fB.style.visibility = on ? "visible" : "hidden";
    }, speed());
    legend.textContent = `Blinking two images taken ${gap}. Anything that jumps or flickers is moving or changing.`;
  } else if (mode === "swipe") {
    const upd = () => (fB.style.clipPath = `inset(0 0 0 ${swipe.value}%)`);
    swipe.oninput = upd;
    upd();
    legend.textContent = `Drag the slider to wipe between A (left) and B (right), taken ${gap}.`;
  } else {
    legend.textContent = `A and B side by side, taken ${gap}.`;
  }
}

$("#modes").addEventListener("click", (e) => {
  const m = e.target.closest("button")?.dataset.mode;
  if (!m) return;
  state.mode = m;
  render();
});
$("#speed").addEventListener("change", render);
$("#fov").addEventListener("change", (e) => {
  state.fov = +e.target.value;
  drawMarker();
  render();
});
$("#showGrid").addEventListener("change", () => document.querySelectorAll(".frame").forEach(drawOverlay));
$("#showSSO").addEventListener("change", () => document.querySelectorAll(".frame").forEach(drawOverlay));
window.addEventListener("resize", () => document.querySelectorAll(".frame").forEach(drawOverlay));

// ------------------------------------------------------------------ AI change analysis
const AI_LABELS = {
  possible_motion: "Possible motion",
  brightness_change: "Brightness change",
  known_object: "Known object",
  likely_artifact: "Likely artifact",
  no_significant_change: "No significant change",
  inconclusive: "Inconclusive",
};

function resetAI() {
  state.aiSignature = null;
  $("#aiEmpty").textContent = "Choose two frames, preferably at the same wavelength, then run the analysis.";
  $("#aiEmpty").classList.remove("err");
  $("#aiEmpty").hidden = false;
  $("#aiBusy").hidden = true;
  $("#aiResult").hidden = true;
  $("#analyzeAI").disabled = false;
}

function fillAIList(selector, items) {
  $(selector).innerHTML = (items?.length ? items : ["None reported."])
    .map((x) => `<li>${String(x).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}</li>`)
    .join("");
}

$("#analyzeAI").onclick = async () => {
  if (!state.a || !state.b) return;
  const button = $("#analyzeAI");
  button.disabled = true;
  $("#aiEmpty").hidden = true;
  $("#aiResult").hidden = true;
  $("#aiBusy").hidden = false;
  const signature = `${state.a.key}|${state.b.key}|${state.ra}|${state.dec}|${state.fov}`;
  try {
    const r = await fetch("/api/ai-analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        a: state.a.key, b: state.b.key, ra: state.ra, dec: state.dec, fov: state.fov,
        date_a: state.a.date, date_b: state.b.date,
        mjd_a: state.a.mjd, mjd_b: state.b.mjd,
        wave_a: state.a.wave ?? null, wave_b: state.b.wave ?? null,
      }),
    });
    if (!r.ok) {
      let message = "AI analysis is temporarily unavailable.";
      try { message = (await r.json()).detail || message; } catch {}
      throw new Error(message);
    }
    const result = await r.json();
    if (signature !== `${state.a.key}|${state.b.key}|${state.ra}|${state.dec}|${state.fov}`) return resetAI();
    state.aiSignature = signature;
    const badge = $("#aiClass");
    badge.textContent = AI_LABELS[result.classification] || result.classification;
    badge.className = `ai-badge ${result.classification}`;
    $("#aiConfidence").textContent = `${Math.round(result.confidence * 100)}% confidence`;
    $("#aiSummary").textContent = result.summary;
    fillAIList("#aiEvidence", result.evidence);
    fillAIList("#aiLimits", result.limitations);
    fillAIList("#aiNext", result.next_steps);
    $("#aiDisclaimer").textContent = result.disclaimer;
    $("#aiResult").hidden = false;
  } catch (err) {
    $("#aiEmpty").textContent = err.message;
    $("#aiEmpty").classList.add("err");
    $("#aiEmpty").hidden = false;
  } finally {
    $("#aiBusy").hidden = true;
    button.disabled = false;
  }
};

// ------------------------------------------------------------------ overlays
async function knownObjects(mjd) {
  const key = `${state.ra.toFixed(4)}|${state.dec.toFixed(4)}|${(+mjd).toFixed(4)}|${state.fov}`;
  if (!state.sso[key]) {
    state.sso[key] = fetch(`/api/solar-system?ra=${state.ra}&dec=${state.dec}&mjd=${mjd}&fov=${state.fov}`)
      .then((r) => (r.ok ? r.json() : { objects: [] }))
      .catch(() => ({ objects: [] }));
  }
  return (await state.sso[key]).objects;
}

async function drawOverlay(el) {
  const cv = el.querySelector("canvas");
  const r = el.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = r.width * dpr;
  cv.height = r.height * dpr;
  const g = cv.getContext("2d");
  g.scale(dpr, dpr);
  const W = r.width, H = r.height, cx = W / 2, cy = H / 2;

  if ($("#showGrid").checked) {
    g.strokeStyle = "rgba(255,255,255,.6)";
    g.lineWidth = 1.5;
    [[cx - 26, cy, cx - 10, cy], [cx + 10, cy, cx + 26, cy], [cx, cy - 26, cx, cy - 10], [cx, cy + 10, cx, cy + 26]]
      .forEach(([x1, y1, x2, y2]) => { g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke(); });
  }
  if (!$("#showSSO").checked || !el.dataset.mjd) return;
  const mjd = el.dataset.mjd;
  g.font = "11px JetBrains Mono, monospace";
  g.fillStyle = "#ffffff";
  g.fillText("Checking JPL for known asteroids…", 10, H - 12);
  const objs = await knownObjects(mjd);
  if (el.dataset.mjd !== mjd) return; // frame changed while we waited
  g.clearRect(0, H - 28, W, 28);
  g.fillStyle = "#ffffff";
  g.fillText(objs.length ? `${objs.length} known asteroid${objs.length > 1 ? "s" : ""} (JPL)` : "No known asteroids here", 10, H - 12);
  const pxPerArcsec = W / (state.fov * 60);
  g.font = "11px JetBrains Mono, monospace";
  objs.forEach((o) => {
    const x = cx - o.dx * pxPerArcsec; // east is left on the sky
    const y = cy - o.dy * pxPerArcsec;
    if (x < 0 || y < 0 || x > W || y > H) return;
    g.strokeStyle = "#ffffff";
    g.setLineDash(o.kind === "comet" ? [3, 3] : []);
    g.lineWidth = 1.5;
    g.beginPath();
    g.arc(x, y, 9, 0, Math.PI * 2);
    g.stroke();
    g.fillStyle = g.strokeStyle;
    const text = `${o.name}${o.vmag ? "  V" + o.vmag : ""}`;
    const tw = g.measureText(text).width;
    // keep labels inside the frame: flip to the left of the circle near the right edge
    const tx = x + 12 + tw > W - 4 ? x - 12 - tw : x + 12;
    g.fillText(text, Math.max(4, tx), Math.max(12, y - 8));
  });
}
