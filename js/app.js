/* Flagstaff Air Quality animated map */
"use strict";

const SPECIES_INFO = {
  pm25: { label: "PM2.5", units: "µg/m³" },
  pm10: { label: "PM10", units: "µg/m³" },
  pm1: { label: "PM1.0", units: "µg/m³" },
};

// EPA AQI categories (2024 PM2.5 breakpoints), hues tuned for map legibility.
const EPA_CATEGORIES = [
  { name: "Good", color: "#00b050" },
  { name: "Moderate", color: "#f2c200" },
  { name: "Unhealthy for Sensitive Groups", color: "#f97a00" },
  { name: "Unhealthy", color: "#e33030" },
  { name: "Very Unhealthy", color: "#8f3f97" },
  { name: "Hazardous", color: "#7e0023" },
];
const BREAKPOINTS = {
  pm25: [9.0, 35.4, 55.4, 125.4, 225.4],
  pm10: [54, 154, 254, 354, 424],
  // PM1.0 has no official EPA breakpoints; reuse the PM2.5 ones
  // (conservative, since PM1.0 is a subset of PM2.5 mass).
  pm1: [9.0, 35.4, 55.4, 125.4, 225.4],
};

const SURFACE_ALPHA = 0.45;
const GRID_PX = 6;
const FRAME_MS = 160;
const MAX_PLAUSIBLE_PM = 1500;
const MERGE_DIST_M = 30;       // co-located sensors are averaged before kriging
// variance-based fade: full opacity below lo, transparent above hi
// (fractions of the kriging std dev at the sill). Kept high so the surface
// spans the whole basin, fading only well beyond the network.
const FADE_SIGMA_LO = 0.9;
const FADE_SIGMA_HI = 1.02;
// fallback if data/variogram.json is missing
const VARIOGRAM_DEFAULT = { model: "exponential", nugget: 0.15, psill: 0.85, range_m: 4000 };
let variogram = null;

const state = {
  species: "pm25",
  window: "24h",
  frame: 0,
  playing: false,
  frames: null,        // current window's frames file
  sensorsMeta: {},
  markers: {},
  framesCache: {},
};

/* ---------- color scales ---------- */

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function lerpRgb(a, b, t) {
  return [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t));
}

function categoryIndex(species, v) {
  const bps = BREAKPOINTS[species];
  for (let i = 0; i < bps.length; i++) if (v <= bps[i]) return i;
  return bps.length;
}

// Continuous color for the interpolated surface; category anchors for
// PM2.5/PM10, ramp steps for PM1.0.
function surfaceColor(species, v) {
  const anchors = EPA_CATEGORIES.map((c) => hexToRgb(c.color));
  const values = [0, ...BREAKPOINTS[species]];
  if (v <= values[0]) return anchors[0];
  for (let i = 1; i < values.length; i++) {
    if (v <= values[i]) {
      const t = (v - values[i - 1]) / (values[i] - values[i - 1]);
      return lerpRgb(anchors[i - 1], anchors[i], t);
    }
  }
  return anchors[anchors.length - 1];
}

// Discrete color for sensor dots (category identity for PM2.5/PM10).
function dotColor(species, v) {
  if (v == null) return "#9a9a9a";
  return EPA_CATEGORIES[categoryIndex(species, v)].color;
}

/* ---------- map setup ---------- */

const map = L.map("map", { zoomControl: true, attributionControl: true });
map.setView([35.1983, -111.6513], 11);
map.attributionControl.setPrefix(false);

/* theme: system preference unless the user picked one with the toggle */
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const savedTheme = localStorage.getItem("theme");
if (savedTheme === "light" || savedTheme === "dark") {
  document.documentElement.dataset.theme = savedTheme;
}
// #light / #dark in the URL wins for this visit (not persisted)
const hashTheme = location.hash.match(/\b(light|dark)\b/);
if (hashTheme) document.documentElement.dataset.theme = hashTheme[1];
function isDark() {
  const t = document.documentElement.dataset.theme;
  return t ? t === "dark" : darkQuery.matches;
}
function applyTheme() {
  document.getElementById("theme-btn").textContent = isDark() ? "☀" : "☾";
  setBasemap();
  rebuildMarkers();
}
document.getElementById("theme-btn").addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  applyTheme();
});
darkQuery.addEventListener("change", () => {
  if (!document.documentElement.dataset.theme) applyTheme();
});

let baseLayer = null;
function setBasemap() {
  const style = isDark() ? "dark_all" : "light_all";
  if (baseLayer) map.removeLayer(baseLayer);
  baseLayer = L.tileLayer(
    `https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`,
    {
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, &copy; <a href="https://carto.com/attributions">CARTO</a> | Data: PurpleAir, Air Quality Egg',
      maxZoom: 18,
    }
  ).addTo(map);
}
setBasemap();
document.getElementById("theme-btn").textContent = isDark() ? "☀" : "☾";

/* ---------- ordinary kriging ---------- */

function gammaParams() {
  return (variogram && variogram[state.species]) || VARIOGRAM_DEFAULT;
}

// Gauss-Jordan inverse with partial pivoting; returns null if singular.
function invertMatrix(A, m) {
  const aug = new Float64Array(m * 2 * m);
  for (let i = 0; i < m; i++) {
    for (let j = 0; j < m; j++) aug[i * 2 * m + j] = A[i * m + j];
    aug[i * 2 * m + m + i] = 1;
  }
  for (let col = 0; col < m; col++) {
    let piv = col;
    for (let r = col + 1; r < m; r++) {
      if (Math.abs(aug[r * 2 * m + col]) > Math.abs(aug[piv * 2 * m + col])) piv = r;
    }
    const pv = aug[piv * 2 * m + col];
    if (Math.abs(pv) < 1e-12) return null;
    if (piv !== col) {
      for (let j = 0; j < 2 * m; j++) {
        const t = aug[col * 2 * m + j];
        aug[col * 2 * m + j] = aug[piv * 2 * m + j];
        aug[piv * 2 * m + j] = t;
      }
    }
    const inv = 1 / aug[col * 2 * m + col];
    for (let j = 0; j < 2 * m; j++) aug[col * 2 * m + j] *= inv;
    for (let r = 0; r < m; r++) {
      if (r === col) continue;
      const f = aug[r * 2 * m + col];
      if (f === 0) continue;
      for (let j = 0; j < 2 * m; j++) aug[r * 2 * m + j] -= f * aug[col * 2 * m + j];
    }
  }
  const out = new Float64Array(m * m);
  for (let i = 0; i < m; i++)
    for (let j = 0; j < m; j++) out[i * m + j] = aug[i * 2 * m + m + j];
  return out;
}

const KrigingOverlay = L.Layer.extend({
  onAdd(m) {
    this._canvas = L.DomUtil.create("canvas", "leaflet-layer");
    this._canvas.style.pointerEvents = "none";
    m.getPanes().overlayPane.appendChild(this._canvas);
    m.on("moveend zoomend resize", this.redraw, this);
    this.redraw();
    return this;
  },
  onRemove(m) {
    m.off("moveend zoomend resize", this.redraw, this);
    this._canvas.remove();
  },
  redraw() {
    const m = this._map;
    if (!m) return;
    const size = m.getSize();
    const topLeft = m.containerPointToLayerPoint([0, 0]);
    L.DomUtil.setPosition(this._canvas, topLeft);
    this._canvas.width = size.x;
    this._canvas.height = size.y;

    const ctx = this._canvas.getContext("2d");
    ctx.clearRect(0, 0, size.x, size.y);
    let pts = currentSensorPoints();
    if (pts.length === 0) return;

    // meters per pixel at map center
    const lat = m.getCenter().lat;
    const mpp =
      (40075016.686 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, m.getZoom() + 8);

    // merge co-located sensors (identical rows make the OK system singular)
    const merged = [];
    for (const p of pts) {
      const twin = merged.find(
        (q) => Math.hypot(p.x - q.x, p.y - q.y) * mpp < MERGE_DIST_M
      );
      if (twin) {
        twin.v = (twin.v * twin.n + p.v) / (twin.n + 1);
        twin.n += 1;
      } else {
        merged.push({ x: p.x, y: p.y, v: p.v, n: 1 });
      }
    }
    pts = merged;

    const { nugget, psill, range_m } = gammaParams();
    const sill = nugget + psill;
    const gamma = (dm) => (dm <= 0 ? 0 : nugget + psill * (1 - Math.exp((-3 * dm) / range_m)));
    const sigmaSill = Math.sqrt(sill);
    const sigLo = FADE_SIGMA_LO * sigmaSill;
    const sigHi = FADE_SIGMA_HI * sigmaSill;

    const n = pts.length;
    const gw = Math.ceil(size.x / GRID_PX);
    const gh = Math.ceil(size.y / GRID_PX);
    const off = document.createElement("canvas");
    off.width = gw;
    off.height = gh;
    const octx = off.getContext("2d");
    const img = octx.createImageData(gw, gh);
    const d = img.data;

    // ordinary kriging system: [[gamma_ij, 1],[1, 0]], inverted once per frame
    let Ainv = null;
    const mdim = n + 1;
    if (n >= 2) {
      const A = new Float64Array(mdim * mdim);
      for (let i = 0; i < n; i++) {
        for (let j = 0; j < n; j++) {
          A[i * mdim + j] =
            i === j ? 0 : gamma(Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y) * mpp);
        }
        A[i * mdim + n] = 1;
        A[n * mdim + i] = 1;
      }
      Ainv = invertMatrix(A, mdim);
    }

    const b = new Float64Array(mdim);
    const w = new Float64Array(mdim);
    for (let gy = 0; gy < gh; gy++) {
      for (let gx = 0; gx < gw; gx++) {
        const px = gx * GRID_PX + GRID_PX / 2;
        const py = gy * GRID_PX + GRID_PX / 2;
        let pred, sigma;
        if (Ainv) {
          for (let i = 0; i < n; i++) {
            b[i] = gamma(Math.hypot(px - pts[i].x, py - pts[i].y) * mpp);
          }
          b[n] = 1;
          pred = 0;
          let variance = 0;
          for (let i = 0; i < mdim; i++) {
            let wi = 0;
            for (let j = 0; j < mdim; j++) wi += Ainv[i * mdim + j] * b[j];
            w[i] = wi;
            variance += wi * b[i];
            if (i < n) pred += wi * pts[i].v;
          }
          sigma = Math.sqrt(Math.max(variance, 0));
        } else {
          // single station: flat field, uncertainty grows with distance
          pred = pts[0].v;
          sigma = Math.sqrt(gamma(Math.hypot(px - pts[0].x, py - pts[0].y) * mpp));
        }
        let fade = 1;
        if (sigma > sigLo) {
          if (sigma >= sigHi) continue;
          const t = (sigma - sigLo) / (sigHi - sigLo);
          fade = 1 - t * t * (3 - 2 * t); // smoothstep
        }
        pred = Math.min(Math.max(pred, 0), MAX_PLAUSIBLE_PM);
        const [r, g, bl] = surfaceColor(state.species, pred);
        const i4 = (gy * gw + gx) * 4;
        d[i4] = r;
        d[i4 + 1] = g;
        d[i4 + 2] = bl;
        d[i4 + 3] = Math.round(255 * SURFACE_ALPHA * fade);
      }
    }
    octx.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(off, 0, 0, size.x, size.y);
  },
});
const surface = new KrigingOverlay().addTo(map);

function currentSensorPoints() {
  const f = state.frames;
  if (!f) return [];
  const vals = f.values[state.species][state.frame];
  const pts = [];
  f.sensors.forEach((sid, i) => {
    const meta = state.sensorsMeta[sid];
    const v = vals[i];
    if (!meta || meta.lat == null || v == null) return;
    const cp = map.latLngToContainerPoint([meta.lat, meta.lon]);
    pts.push({ x: cp.x, y: cp.y, v });
  });
  return pts;
}

/* ---------- sensor markers ---------- */

function currentValue(sid) {
  const f = state.frames;
  if (!f) return null;
  const i = f.sensors.indexOf(sid);
  return i >= 0 ? f.values[state.species][state.frame][i] : null;
}

function tooltipHtml(sid) {
  const meta = state.sensorsMeta[sid] || {};
  const netName = meta.network === "egg" ? "Air Quality Egg" : "PurpleAir (calibrated)";
  const f = state.frames;
  const i = f ? f.sensors.indexOf(sid) : -1;
  let rows = "";
  for (const sp of ["pm1", "pm25", "pm10"]) {
    const v = i >= 0 ? f.values[sp][state.frame][i] : null;
    rows += `${SPECIES_INFO[sp].label}: <b>${v == null ? "–" : v.toFixed(1)}</b> µg/m³<br>`;
  }
  return `<div class="sensor-tooltip"><span class="name">${meta.name || sid}</span><br>
    <span class="net">${netName}</span><br>${rows}</div>`;
}

function rebuildMarkers() {
  for (const sid in state.markers) state.markers[sid].remove();
  state.markers = {};
  if (!state.frames) return;
  for (const sid of state.frames.sensors) {
    const meta = state.sensorsMeta[sid];
    if (!meta || meta.lat == null) continue;
    const isEgg = meta.network === "egg";
    const marker = L.circleMarker([meta.lat, meta.lon], {
      radius: isEgg ? 9 : 7,
      weight: isEgg ? 3 : 1.2,
      color: isEgg ? (isDark() ? "#1a1a19" : "#ffffff") : "rgba(0,0,0,0.45)",
      fillOpacity: 0.95,
      fillColor: "#9a9a9a",
    }).addTo(map);
    marker.bindTooltip(() => tooltipHtml(sid), { sticky: true });
    state.markers[sid] = marker;
  }
  styleMarkers();
}

function styleMarkers() {
  for (const sid in state.markers) {
    const v = currentValue(sid);
    state.markers[sid].setStyle({
      fillColor: dotColor(state.species, v),
      fillOpacity: v == null ? 0.25 : 0.95,
    });
  }
}

/* ---------- legend ---------- */

function buildLegend() {
  const el = document.getElementById("legend");
  const sp = state.species;
  let html = `<div class="legend-title">${SPECIES_INFO[sp].label} (µg/m³)</div>`;
  const bps = [0, ...BREAKPOINTS[sp]];
  EPA_CATEGORIES.forEach((c, i) => {
    const lo = bps[i];
    const hi = bps[i + 1];
    const range = hi == null ? `${lo}+` : `${lo}–${hi}`;
    const shortName = c.name.replace("Unhealthy for Sensitive Groups", "Sensitive groups");
    html += `<div class="row"><span class="swatch" style="background:${c.color}"></span>
      <span>${shortName} <span style="opacity:.7">${range}</span></span></div>`;
  });
  if (sp === "pm1") {
    html += `<div class="row" style="opacity:.7"><span>PM2.5 breakpoints (no PM1.0 standard)</span></div>`;
  }
  html += `<hr>
    <div class="row net-row"><span class="swatch"></span><span>PurpleAir</span></div>
    <div class="row net-row egg"><span class="swatch"></span><span>Air Quality Egg</span></div>`;
  el.innerHTML = html;
}

/* ---------- time controls & animation ---------- */

const slider = document.getElementById("time-slider");
const playBtn = document.getElementById("play-btn");
const timeLabel = document.getElementById("time-label");

function formatTime(isoTs) {
  return new Date(isoTs).toLocaleString("en-US", {
    timeZone: "America/Phoenix",
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function setFrame(i, redrawSurface = true) {
  if (!state.frames) return;
  state.frame = Math.max(0, Math.min(i, state.frames.times.length - 1));
  slider.value = state.frame;
  timeLabel.textContent = formatTime(state.frames.times[state.frame]);
  styleMarkers();
  if (redrawSurface) surface.redraw();
}

let playTimer = null;
function setPlaying(on) {
  state.playing = on;
  playBtn.textContent = on ? "❚❚" : "▶";
  if (playTimer) clearInterval(playTimer);
  playTimer = null;
  if (on) {
    playTimer = setInterval(() => {
      const next = state.frame + 1;
      setFrame(next >= state.frames.times.length ? 0 : next);
    }, FRAME_MS);
  }
}
playBtn.addEventListener("click", () => setPlaying(!state.playing));
slider.addEventListener("input", () => {
  setPlaying(false);
  setFrame(Number(slider.value));
});

/* ---------- data loading ---------- */

async function fetchJson(url) {
  const resp = await fetch(url, { cache: "no-cache" });
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  return resp.json();
}

async function loadWindow(win) {
  if (!state.framesCache[win]) {
    state.framesCache[win] = await fetchJson(`data/frames_${win}.json`);
  }
  state.window = win;
  state.frames = state.framesCache[win];
  slider.max = state.frames.times.length - 1;
  rebuildMarkers();
  setFrame(state.frames.times.length - 1);
  updateStatusNote();
}

function updateStatusNote() {
  const el = document.getElementById("status-note");
  const f = state.frames;
  if (!f) return;
  const ageMin = (Date.now() - new Date(f.generated_at).getTime()) / 60000;
  if (ageMin > 45) {
    el.hidden = false;
    el.textContent = `Data last updated ${
      ageMin > 120 ? Math.round(ageMin / 60) + " h" : Math.round(ageMin) + " min"
    } ago.`;
  } else {
    el.hidden = true;
  }
}

/* ---------- toggles & about ---------- */

function wireToggle(containerId, attr, onChange) {
  const box = document.getElementById(containerId);
  box.addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    box.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    onChange(btn.dataset[attr]);
  });
}
wireToggle("species-toggle", "species", (sp) => {
  state.species = sp;
  buildLegend();
  styleMarkers();
  surface.redraw();
});
wireToggle("window-toggle", "window", (win) => {
  const wasPlaying = state.playing;
  setPlaying(false);
  loadWindow(win).then(() => setPlaying(wasPlaying)).catch(showError);
});

const aboutDialog = document.getElementById("about-dialog");
document.getElementById("about-btn").addEventListener("click", async () => {
  aboutDialog.showModal();
  try {
    const cal = await fetchJson("data/calibration.json");
    const el = document.getElementById("calibration-info");
    let rows = "";
    for (const sp of ["pm1", "pm25", "pm10"]) {
      const c = cal[sp] || {};
      rows += `<tr><td>${SPECIES_INFO[sp].label}</td>
        <td>${c.apply ? `y = ${c.slope}x ${c.intercept >= 0 ? "+" : "−"} ${Math.abs(c.intercept)}` : "identity"}</td>
        <td>${c.r2 != null ? "R² " + c.r2 : ""}</td><td>${c.n != null ? "n=" + c.n : ""}</td></tr>`;
    }
    el.innerHTML = `<table><tr><th>Species</th><th>Local fit</th><th></th><th></th></tr>${rows}</table>
      <p>${cal.fitted_at ? "Last fit: " + formatTime(cal.fitted_at) : "No local fit yet (seed calibration)."}</p>`;
  } catch {
    /* calibration info is optional */
  }
});

function showError(err) {
  const el = document.getElementById("status-note");
  el.hidden = false;
  el.textContent = `Problem loading data: ${err.message}`;
}

/* ---------- init ---------- */

(async function init() {
  // shareable state in the hash, e.g. #pm1/7d
  const hashParts = location.hash.replace("#", "").split("/");
  for (const part of hashParts) {
    if (SPECIES_INFO[part]) state.species = part;
    if (["24h", "48h", "7d"].includes(part)) state.window = part;
  }
  document.querySelectorAll("#species-toggle button").forEach((b) =>
    b.classList.toggle("active", b.dataset.species === state.species)
  );
  document.querySelectorAll("#window-toggle button").forEach((b) =>
    b.classList.toggle("active", b.dataset.window === state.window)
  );
  variogram = await fetchJson("data/variogram.json").catch(() => null);
  try {
    const meta = await fetchJson("data/sensors.json");
    state.sensorsMeta = meta.sensors || {};
    buildLegend();
    await loadWindow(state.window);
    const coords = Object.values(state.sensorsMeta).filter((m) => m.lat != null);
    if (coords.length) {
      map.fitBounds(coords.map((m) => [m.lat, m.lon]), { padding: [40, 40], maxZoom: 12 });
    }
    setPlaying(true);
  } catch (err) {
    showError(err);
  }
})();
