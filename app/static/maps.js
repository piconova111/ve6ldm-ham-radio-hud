'use strict';
/* The station display's three map views, drawn on canvases from world.json and world.png:
     flat   the everyday world map: the same dots as before, now lit by the sun, with the
            gray line glowing where it's sunrise or sunset,
     globe  a 3D globe that swings round to show the path to a station,
     beam   a map centred on the station: every straight line from the middle is a beam
            heading, and the distance from the middle is the real distance.
   hud.js says what to show (Maps.setTarget, Maps.setSpots, ...) and which view (Maps.setView).
   If the land data can't load, Maps.available() stays false and the old map is kept. */

const Maps = (() => {
  const RAD = Math.PI / 180, TAU = 2 * Math.PI;
  const VFD = [103, 209, 255], AMBER = [255, 178, 74], WHITE = [230, 237, 246], GREEN = [74, 222, 128];
  const NIGHT_TINT = [6, 12, 24];
  const MONO = '"B612 Mono", "DejaVu Sans Mono", monospace';
  const BEAM_WIDTH = 60;   // degrees; about the width of a hex beam's main lobe
  const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const st = {
    ready: false, view: 'flat', home: null, homeV: null, target: null, spots: [], heard: [],
    iss: null, issOpen: false, sun: [1, 0, 0], sunLL: [0, 0], hint: null, zones: [], zonesShown: false,
    sat: null, openings: [],   // the satellite whose card is open; where the band is open from here
  };
  let world = null;      // { colors: [[r,g,b]...], mask, mw, mh, flat, islands }
  let box = null;        // the .map element
  const layers = {};     // flat / globe / beam: { canvas, ctx, w, h }
  let dpr = 1;

  /* ---------- small maths ---------- */
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const vec = (lat, lon) => {
    const p = lat * RAD, l = lon * RAD, c = Math.cos(p);
    return [c * Math.cos(l), c * Math.sin(l), Math.sin(p)];
  };
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n]; };
  const latLon = (v) => [Math.asin(clamp(v[2], -1, 1)) / RAD, Math.atan2(v[1], v[0]) / RAD];
  function slerp(a, b, t) {
    const om = Math.acos(clamp(dot(a, b), -1, 1));
    if (om < 1e-6) return a.slice();
    const s = Math.sin(om), k1 = Math.sin((1 - t) * om) / s, k2 = Math.sin(t * om) / s;
    return [k1 * a[0] + k2 * b[0], k1 * a[1] + k2 * b[1], k1 * a[2] + k2 * b[2]];
  }
  function basis(v) {   // east and north at a point, as unit vectors
    const [lat, lon] = latLon(v);
    const p = lat * RAD, l = lon * RAD;
    return { E: [-Math.sin(l), Math.cos(l), 0], N: [-Math.sin(p) * Math.cos(l), -Math.sin(p) * Math.sin(l), Math.cos(p)] };
  }
  const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const rgb = (c, a = 1) => `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${a})`;
  const fmtKm = (n) => `${Math.round(n).toLocaleString('en-CA')} km`;

  function sunPosition(when) {   // the same formula as hud.js
    const d = when.getTime() / 86400000 + 2440587.5 - 2451545.0;
    const g = (357.529 + 0.98560028 * d) * RAD;
    const L = (280.459 + 0.98564736 * d + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * RAD;
    const e = (23.439 - 0.00000036 * d) * RAD;
    const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / RAD;
    const lat = Math.asin(Math.sin(e) * Math.sin(L)) / RAD;
    const gmst = (((18.697374558 + 24.06570982441908 * d) % 24) + 24) % 24;
    const lon = ((((ra - gmst * 15 + 180) % 360) + 360) % 360) - 180;
    return [lat, lon];
  }
  function updateSun() {
    st.sunLL = sunPosition(new Date());
    st.sun = vec(st.sunLL[0], st.sunLL[1]);
  }

  /* ---------- land data ---------- */
  function landAt(lat, lon) {
    let c = Math.floor((lon + 180) * world.per) % world.mw;
    if (c < 0) c += world.mw;
    const r = clamp(Math.floor((90 - lat) * world.per), 0, world.mh - 1);
    return world.mask[r * world.mw + c];
  }

  async function loadWorld() {
    const res = await fetch('/static/world.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error('world.json ' + res.status);
    const data = await res.json();
    const img = new Image();
    img.src = '/static/' + data.mask.src;
    await img.decode();
    const c = document.createElement('canvas');
    c.width = data.mask.w;
    c.height = data.mask.h;
    const x = c.getContext('2d', { willReadFrequently: true });
    x.drawImage(img, 0, 0);
    const px = x.getImageData(0, 0, c.width, c.height).data;
    const mask = new Uint8Array(c.width * c.height);
    for (let i = 0; i < mask.length; i++) mask[i] = Math.round(px[i * 4] / data.mask.level);
    const colors = data.colors.map((h) => (h ? [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) : [23, 37, 57]));
    return { colors, mask, mw: c.width, mh: c.height, per: data.mask.per_degree, flat: data.flat, islands: data.islands };
  }

  /* ---------- dot colours, in buckets so each colour is filled once ---------- */
  // Brightness runs from 0 (the far edge of the globe at night) to 1 (full daylight) in
  // STEPS steps; the gray-line glow has 3 levels.
  const STEPS = 12;
  const palette = new Map();
  function shade(cont, step, glow) {
    const key = cont * 64 + step * 4 + glow;
    let c = palette.get(key);
    if (!c) {
      const b = step / (STEPS - 1);
      const base = world.colors[cont];
      let col = mix(NIGHT_TINT, base, b);
      if (glow) col = mix(col, AMBER, (cont ? 0.42 : 0.3) * glow / 2);
      c = rgb(col);
      palette.set(key, c);
    }
    return c;
  }
  // How lit a place is (night = floor, up to 1 in daylight) and its gray-line glow (0, 1 or 2),
  // from d = the sine of the sun's height above the horizon there.
  function lightOf(d, floor = 0.42) {
    const day = floor + (1 - floor) * smooth(-0.2, 0.06, d);
    const g = 1 - Math.abs(d + 0.03) / 0.085;
    return [day, g > 0.55 ? 2 : g > 0 ? 1 : 0];
  }

  class Buckets {
    constructor() { this.map = new Map(); }
    reset() { for (const b of this.map.values()) b.n = 0; }
    add(color, x, y, r) {
      let b = this.map.get(color);
      if (!b) { b = { n: 0, p: new Float32Array(3 * 256) }; this.map.set(color, b); }
      if (b.n + 3 > b.p.length) { const p = new Float32Array(b.p.length * 2); p.set(b.p); b.p = p; }
      b.p[b.n++] = x; b.p[b.n++] = y; b.p[b.n++] = r;
    }
    draw(ctx) {
      for (const [color, b] of this.map) {
        if (!b.n) continue;
        const p = b.p;
        ctx.fillStyle = color;
        ctx.beginPath();
        for (let i = 0; i < b.n; i += 3) {
          ctx.moveTo(p[i] + p[i + 2], p[i + 1]);
          ctx.arc(p[i], p[i + 1], p[i + 2], 0, TAU);
        }
        ctx.fill();
      }
    }
  }

  /* ---------- canvases ---------- */
  function makeLayer(name) {
    const canvas = document.getElementById(name + '-canvas');
    const layer = { canvas, ctx: canvas.getContext('2d'), w: 0, h: 0, dirty: true };
    layers[name] = layer;
    return layer;
  }
  function fit(layer) {
    const r = box.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (w === layer.w && h === layer.h && layer.dpr === dpr) return false;
    layer.w = w; layer.h = h; layer.dpr = dpr;
    layer.canvas.width = Math.round(w * dpr);
    layer.canvas.height = Math.round(h * dpr);
    layer.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }
  function label(ctx, text, x, y, color, align = 'left', size = 15) {
    ctx.font = `700 ${size}px ${MONO}`;
    ctx.textAlign = align;
    ctx.textBaseline = 'alphabetic';
    ctx.lineJoin = 'round';
    ctx.lineWidth = 4;
    ctx.strokeStyle = 'rgba(10,19,34,0.92)';
    ctx.strokeText(text, x, y);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }
  function ring(ctx, x, y, r, color, width = 2) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, TAU);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }
  function disc(ctx, x, y, r, color) {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, TAU);
    ctx.fillStyle = color;
    ctx.fill();
  }
  function glowDot(ctx, x, y, r, c, a = 0.35) {
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, rgb(c, a));
    g.addColorStop(1, rgb(c, 0));
    ctx.fillStyle = g;
    ctx.fillRect(x - r, y - r, 2 * r, 2 * r);
  }
  function homeMark(ctx, x, y, call, lx = 16, ly = -14) {
    glowDot(ctx, x, y, 26, AMBER, 0.3);
    ring(ctx, x, y, 11, rgb(AMBER));
    disc(ctx, x, y, 5, rgb(AMBER));
    if (call) label(ctx, call, x + lx, y + ly, rgb(AMBER), lx < 0 ? 'right' : 'left');
  }
  function targetMark(ctx, x, y, call, pulse, lx = 14, ly = 24) {
    glowDot(ctx, x, y, 24, VFD, 0.35);
    if (pulse > 0 && pulse < 1) ring(ctx, x, y, 10 + 26 * pulse, rgb(VFD, 0.7 * (1 - pulse)), 2);
    ring(ctx, x, y, 10, rgb(VFD));
    disc(ctx, x, y, 4.5, rgb(VFD));
    if (call) label(ctx, call, x + lx, y + ly, rgb(WHITE), lx < 0 ? 'right' : 'left');
  }
  function issIcon(ctx, x, y, open, name = 'ISS') {
    ctx.fillStyle = rgb(WHITE);
    ctx.fillRect(x - 9, y - 2, 18, 4);
    ctx.fillRect(x - 2, y - 6, 4, 12);
    if (open) ring(ctx, x, y, 10, rgb(WHITE));
    label(ctx, name, x + 12, y - 8, rgb(WHITE));
  }
  function satIcon(ctx, x, y, name) {   // a small satellite: a body with two solar panels
    ctx.fillStyle = rgb(WHITE);
    ctx.fillRect(x - 3, y - 3, 6, 6);
    ctx.fillRect(x - 11, y - 1.5, 6, 3);
    ctx.fillRect(x + 5, y - 1.5, 6, 3);
    ring(ctx, x, y, 10, rgb(WHITE));
    label(ctx, name, x + 13, y - 9, rgb(WHITE));
  }
  // A CQ zone number: red, or a filled red badge once the zone is in the log.
  let zoneHits = [];
  let zoneClick = null;
  function zoneMark(ctx, x, y, z) {
    ctx.font = `700 13px ${MONO}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    if (z.worked) {
      disc(ctx, x, y, 10.5, 'rgba(224,114,125,0.92)');
      ctx.fillStyle = '#240D12';
      ctx.fillText(String(z.zone), x, y + 0.5);
    } else {
      ctx.lineJoin = 'round';
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(10,19,34,0.95)';
      ctx.strokeText(String(z.zone), x, y + 0.5);
      ctx.fillStyle = '#E0727D';
      ctx.fillText(String(z.zone), x, y + 0.5);
    }
    zoneHits.push({ zone: z.zone, x, y });
  }
  function clearOf(x, y, marks) { return marks.every(([mx, my]) => (mx - x) ** 2 + (my - y) ** 2 > 30 * 30); }
  function zoneAt(x, y) {
    let best = null, bd = 14 * 14;
    for (const h of zoneHits) {
      const d = (h.x - x) ** 2 + (h.y - y) ** 2;
      if (d < bd) { bd = d; best = h.zone; }
    }
    return best;
  }

  function sunIcon(ctx, x, y) {
    ctx.strokeStyle = rgb(AMBER);
    ctx.lineWidth = 2;
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (let i = 0; i < 8; i++) {
      const a = i * Math.PI / 4;
      ctx.moveTo(x + 9 * Math.cos(a), y + 9 * Math.sin(a));
      ctx.lineTo(x + 13 * Math.cos(a), y + 13 * Math.sin(a));
    }
    ctx.stroke();
    disc(ctx, x, y, 6, rgb(AMBER));
  }

  /* ---------- flat map ---------- */
  // The same grid of dots as map.svg (Miller projection, left edge 170 W, 84 N to 60 S).
  const flat = { pts: null, buckets: new Buckets() };
  function flatSetup() {
    const f = world.flat;
    const ym = (lat) => 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * RAD));
    const YT = ym(84), K = f.w / TAU;
    const n = f.cols * f.rows;
    flat.pts = { x: new Float32Array(n), y: new Float32Array(n), v: new Float32Array(3 * n), c: new Uint8Array(n) };
    for (let r = 0, i = 0; r < f.rows; r++) {
      for (let col = 0; col < f.cols; col++, i++) {
        const x = f.x0 + col * f.step, y = f.y0 + r * f.step;
        const lat = (Math.atan(Math.exp((YT - y / K) / 1.25)) - Math.PI / 4) / 0.4 / RAD;
        const lon = -170 + x * 360 / f.w;
        const v = vec(lat, lon);
        flat.pts.x[i] = x; flat.pts.y[i] = y; flat.pts.c[i] = f.grid.charCodeAt(i) - 48;
        flat.pts.v.set(v, 3 * i);
      }
    }
  }
  function drawFlat() {
    const L = layers.flat;
    const ctx = L.ctx, f = world.flat;
    ctx.clearRect(0, 0, L.w, L.h);
    const s = Math.min(L.w / f.w, L.h / f.h);
    const ox = (L.w - f.w * s) / 2, oy = (L.h - f.h * s) / 2;
    // Soft night shade under the dots, from a small picture of the sunlight stretched to fit.
    const sw = 148, sh = 76;
    const img = new ImageData(sw, sh);
    const P = flat.pts, S = st.sun;
    for (let r = 0; r < sh; r++) {
      for (let c = 0; c < sw; c++) {
        const gi = Math.min(f.rows - 1, Math.round(r * (f.rows - 1) / (sh - 1))) * f.cols
          + Math.min(f.cols - 1, Math.round(c * (f.cols - 1) / (sw - 1)));
        const d = P.v[3 * gi] * S[0] + P.v[3 * gi + 1] * S[1] + P.v[3 * gi + 2] * S[2];
        const night = 1 - smooth(-0.2, 0.04, d);
        const k = 4 * (r * sw + c);
        img.data[k] = 2; img.data[k + 1] = 6; img.data[k + 2] = 13;
        img.data[k + 3] = Math.round(night * 0.5 * 255);
      }
    }
    const tmp = scratch(sw, sh);
    tmp.getContext('2d').putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(tmp, ox, oy, f.w * s, f.h * s);
    // A faint grid every 30 degrees of latitude and longitude.
    const ym = (lat) => 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * RAD));
    const YT = ym(84), K = f.w / TAU;
    ctx.lineWidth = 1;
    for (let lon = -150; lon <= 180; lon += 30) {
      const x = ox + ((((lon + 170) % 360) + 360) % 360) * f.w / 360 * s;
      ctx.strokeStyle = 'rgba(120,160,215,0.08)';
      ctx.beginPath();
      ctx.moveTo(x, oy);
      ctx.lineTo(x, oy + f.h * s);
      ctx.stroke();
    }
    for (const lat of [-30, 0, 30, 60]) {
      const y = oy + K * (YT - ym(lat)) * s;
      ctx.strokeStyle = lat === 0 ? 'rgba(120,160,215,0.15)' : 'rgba(120,160,215,0.08)';
      ctx.beginPath();
      ctx.moveTo(ox, y);
      ctx.lineTo(ox + f.w * s, y);
      ctx.stroke();
    }
    // The dots.
    const B = flat.buckets;
    B.reset();
    const n = P.c.length;
    const rl = 2.6 * s, rs = 1.25 * s;
    for (let i = 0; i < n; i++) {
      const c = P.c[i];
      const d = P.v[3 * i] * S[0] + P.v[3 * i + 1] * S[1] + P.v[3 * i + 2] * S[2];
      const [b, g] = lightOf(d);
      const step = Math.round(b * (STEPS - 1));
      B.add(shade(c, step, g), ox + P.x[i] * s, oy + P.y[i] * s, c ? rl : (g ? rs * 1.25 : rs));
    }
    B.draw(ctx);
    L.dirty = false;
  }
  const scratches = new Map();
  function scratch(w, h) {
    const key = w + 'x' + h;
    let c = scratches.get(key);
    if (!c) { c = document.createElement('canvas'); c.width = w; c.height = h; scratches.set(key, c); }
    return c;
  }

  /* ---------- globe ---------- */
  const globe = {
    pts: null, cont: null, n: 0, built: 0, buckets: new Buckets(),
    center: null, from: null, to: null, t0: 0, dur: 1,
    arcT0: 0, pulseT0: 0, drag: null, press: null, vel: [0, 0], stars: null,
  };
  const PITCH = 5.6;   // pixels between dots in the middle of the globe

  function globeSize(L) {
    const R = Math.round(Math.min(L.h * 0.47, L.w * 0.32));
    return { R, cx: L.w / 2, cy: L.h / 2 + 2 };
  }
  function buildLattice(R) {
    const n = Math.round(4 * Math.PI * (R / PITCH) ** 2);
    if (globe.pts && Math.abs(n - globe.built) / n < 0.15) return;
    const ga = Math.PI * (3 - Math.sqrt(5));
    const xs = [], cs = [];
    const bins = new Map();
    const binKey = (lat, lon) => Math.floor((lat + 90) / 2) * 200 + Math.floor((lon + 180) / 2);
    for (let i = 0; i < n; i++) {
      const z = 1 - (2 * i + 1) / n, r = Math.sqrt(1 - z * z), a = i * ga;
      const x = r * Math.cos(a), y = r * Math.sin(a);
      const lat = Math.asin(z) / RAD, lon = Math.atan2(y, x) / RAD;
      const c = landAt(lat, lon);
      if (!c) continue;
      xs.push(x, y, z);
      cs.push(c);
      const k = binKey(lat, lon);
      if (!bins.has(k)) bins.set(k, []);
      bins.get(k).push([x, y, z]);
    }
    // Small islands that fell between the dots get a dot of their own.
    const gap = Math.cos(0.75 * Math.sqrt(4 * Math.PI / n));
    for (const [lat, lon, c] of world.islands) {
      const v = vec(lat, lon);
      let near = false;
      for (let dy = -1; dy <= 1 && !near; dy++) {
        for (let dx = -1; dx <= 1 && !near; dx++) {
          const list = bins.get(binKey(lat + 2 * dy, ((lon + 2 * dx + 540) % 360) - 180));
          if (list) near = list.some((p) => dot(p, v) > gap);
        }
      }
      if (!near) { xs.push(v[0], v[1], v[2]); cs.push(c || 6); }
    }
    globe.pts = new Float32Array(xs);
    globe.cont = Uint8Array.from(cs);
    globe.n = cs.length;
    globe.built = n;
  }
  function makeStars(L) {
    const c = document.createElement('canvas');
    c.width = L.canvas.width; c.height = L.canvas.height;
    const x = c.getContext('2d');
    x.scale(dpr, dpr);
    let seed = 7;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    for (let i = 0; i < 260; i++) {
      const sx = rnd() * L.w, sy = rnd() * L.h, a = 0.08 + 0.4 * rnd() ** 3, r = rnd() < 0.08 ? 1.3 : 0.8;
      x.fillStyle = `rgba(198,211,227,${a.toFixed(3)})`;
      x.beginPath();
      x.arc(sx, sy, r, 0, TAU);
      x.fill();
    }
    globe.stars = c;
  }

  // Where the globe should face to show a path: the path's middle, moved off the path a
  // little toward the equator so the path shows as a curve rather than a straight line.
  function pathView(A, B) {
    const om = Math.acos(clamp(dot(A, B), -1, 1));
    let M = norm([A[0] + B[0], A[1] + B[1], A[2] + B[2]]);
    if (om > Math.PI - 0.01) M = norm(cross(A, [0, 0, 1]));
    let n = cross(A, B);
    if (Math.hypot(n[0], n[1], n[2]) < 1e-6) return M;
    n = norm(n);
    const s = n[2] > 0 ? -1 : 1;
    const delta = 26 * RAD * Math.min(1, om / (30 * RAD)) * clamp((170 * RAD - om) / (80 * RAD), 0.2, 1);
    const C = [M[0] * Math.cos(delta) + s * n[0] * Math.sin(delta), M[1] * Math.cos(delta) + s * n[1] * Math.sin(delta),
      M[2] * Math.cos(delta) + s * n[2] * Math.sin(delta)];
    return norm(C);
  }
  function homeView() { return vec(clamp(st.home.lat - 10, -80, 80), st.home.lon); }

  function turnTo(C, ms = 1700) {
    globe.drag = null;
    globe.vel = [0, 0];
    if (!globe.center || REDUCED || st.view !== 'globe') {
      globe.center = C;
      globe.from = null;
      return requestDraw();
    }
    globe.from = globe.center;
    globe.to = C;
    globe.t0 = performance.now();
    const ang = Math.acos(clamp(dot(globe.from, C), -1, 1));
    globe.dur = ms * clamp(0.45 + ang / 2, 0.45, 1);
    requestDraw();
  }

  function cam(L) {
    const { R, cx, cy } = globeSize(L);
    const F = globe.center;
    const { E, N } = basis(F);
    return { F, E, N, R, cx, cy };
  }
  const projV = (C, v) => {
    const x = dot(v, C.E), y = dot(v, C.N), z = dot(v, C.F);
    return [C.cx + C.R * x, C.cy - C.R * y, z, x, y];
  };

  function drawOcean(ctx, C) {
    const n = 96, img = new ImageData(n, n);
    const sv = [dot(st.sun, C.E), dot(st.sun, C.N), dot(st.sun, C.F)];   // the sun, in view terms
    const hl = norm([sv[0], sv[1], sv[2] + 1]);
    for (let j = 0; j < n; j++) {
      const y = 1 - (2 * j + 1) / n;
      for (let i = 0; i < n; i++) {
        const x = (2 * i + 1) / n - 1, rr = x * x + y * y;
        const k = 4 * (j * n + i);
        if (rr > 1.04) { img.data[k + 3] = 0; continue; }
        const z = Math.sqrt(Math.max(0, 1 - rr));
        const d = x * sv[0] + y * sv[1] + z * sv[2];
        const lit = smooth(-0.22, 0.12, d);
        let r = 7 + 13 * lit, g = 14 + 30 * lit, b = 27 + 44 * lit;
        const limb = 0.62 + 0.38 * z;
        r *= limb; g *= limb; b *= limb;
        const rim = Math.pow(1 - z, 3) * (0.35 + 0.65 * lit);
        r += 40 * rim; g += 110 * rim; b += 160 * rim;
        const spec = Math.pow(Math.max(0, x * hl[0] + y * hl[1] + z * hl[2]), 60) * 0.5 * lit;
        r += 90 * spec; g += 120 * spec; b += 150 * spec;
        img.data[k] = r; img.data[k + 1] = g; img.data[k + 2] = b; img.data[k + 3] = 255;
      }
    }
    const tmp = scratch(n, n);
    tmp.getContext('2d').putImageData(img, 0, 0);
    ctx.save();
    ctx.beginPath();
    ctx.arc(C.cx, C.cy, C.R, 0, TAU);
    ctx.clip();
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(tmp, C.cx - C.R, C.cy - C.R, 2 * C.R, 2 * C.R);
    ctx.restore();
  }

  function drawGraticule(ctx, C) {
    ctx.lineWidth = 1;
    const line = (pts, color) => {
      ctx.strokeStyle = color;
      ctx.beginPath();
      let on = false;
      for (const v of pts) {
        const p = projV(C, v);
        if (p[2] > 0.0) { if (on) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); on = true; } else on = false;
      }
      ctx.stroke();
    };
    for (let lon = -180; lon < 180; lon += 30) {
      const pts = [];
      for (let lat = -90; lat <= 90; lat += 3) pts.push(vec(lat, lon));
      line(pts, 'rgba(120,160,215,0.10)');
    }
    for (let lat = -60; lat <= 60; lat += 30) {
      const pts = [];
      for (let lon = -180; lon <= 180; lon += 3) pts.push(vec(lat, lon));
      line(pts, lat === 0 ? 'rgba(120,160,215,0.18)' : 'rgba(120,160,215,0.10)');
    }
  }

  function drawGlobeDots(ctx, C) {
    const B = globe.buckets;
    B.reset();
    const P = globe.pts, n = globe.n, S = st.sun;
    const E = C.E, N = C.N, F = C.F, R = C.R;
    const r0 = Math.max(1.1, PITCH * 0.36);
    for (let i = 0; i < n; i++) {
      const px = P[3 * i], py = P[3 * i + 1], pz = P[3 * i + 2];
      const z = px * F[0] + py * F[1] + pz * F[2];
      if (z <= 0.02) continue;
      const x = px * E[0] + py * E[1] + pz * E[2];
      const y = px * N[0] + py * N[1] + pz * N[2];
      const d = px * S[0] + py * S[1] + pz * S[2];
      const [b, g] = lightOf(d, 0.4);
      const limb = 0.45 + 0.55 * Math.sqrt(z);
      const step = clamp(Math.round(b * limb * (STEPS - 1)), 0, STEPS - 1);
      B.add(shade(globe.cont[i], step, z > 0.25 ? g : 0), C.cx + R * x, C.cy - R * y, r0 * (0.45 + 0.55 * Math.sqrt(z)));
    }
    B.draw(ctx);
  }

  // The gray line: where the sun is rising or setting.
  function drawTerminator(ctx, C) {
    const S = st.sun;
    const U = norm(Math.abs(S[2]) > 0.95 ? cross(S, [1, 0, 0]) : cross(S, [0, 0, 1]));
    const V = cross(S, U);
    const pts = [];
    for (let a = 0; a <= 360; a += 3) {
      const c = Math.cos(a * RAD), si = Math.sin(a * RAD);
      pts.push([c * U[0] + si * V[0], c * U[1] + si * V[1], c * U[2] + si * V[2]]);
    }
    strokeRun(ctx, C, pts, rgb(AMBER, 0.12), 7);
    strokeRun(ctx, C, pts, rgb(AMBER, 0.55), 1.5);
  }

  // A visible run of points on or above the globe: hidden only when behind it.
  function strokeRun(ctx, C, pts, color, width, dash) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    let on = false;
    for (const v of pts) {
      const p = projV(C, v);
      const hidden = p[2] < 0 && p[3] * p[3] + p[4] * p[4] < 1;
      if (!hidden) { if (on) ctx.lineTo(p[0], p[1]); else ctx.moveTo(p[0], p[1]); on = true; } else on = false;
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function pathPoints(A, B, lift) {
    const om = Math.acos(clamp(dot(A, B), -1, 1));
    const n = Math.max(24, Math.round(om / RAD * 1.2));
    const H = lift ? 0.035 + 0.13 * om / Math.PI : 0;
    const pts = [];
    for (let i = 0; i <= n; i++) {
      const t = i / n, v = slerp(A, B, t), h = 1 + H * Math.sin(Math.PI * t);
      pts.push([v[0] * h, v[1] * h, v[2] * h]);
    }
    return pts;
  }

  function drawGlobePath(ctx, C, now) {
    const T = st.target;
    if (!T) return;
    const A = st.homeV, B = T.v;
    const prog = REDUCED ? 1 : clamp((now - globe.arcT0) / 1300, 0, 1);
    const ground = pathPoints(A, B, false);
    strokeRun(ctx, C, ground, 'rgba(230,237,246,0.42)', 1.5, [2, 5]);
    const air = pathPoints(A, B, true);
    const upto = Math.max(1, Math.round(ease(prog) * (air.length - 1)));
    const shown = air.slice(0, upto + 1);
    strokeRun(ctx, C, shown, rgb(VFD, 0.14), 10);
    strokeRun(ctx, C, shown, rgb(VFD, 0.28), 5);
    // The core line, amber at our end to blue at theirs, in 12 bands of colour.
    const last = air.length - 1, bands = 12;
    for (let k = 0; k < bands; k++) {
      const a = Math.floor(k * last / bands), b = Math.min(shown.length - 1, Math.ceil((k + 1) * last / bands));
      if (b <= a) break;
      strokeRun(ctx, C, shown.slice(a, b + 1), rgb(mix(AMBER, VFD, Math.min(1, ((k + 0.5) / bands) * 1.6))), 2.5);
    }
    if (prog < 1) {
      const p = projV(C, shown[shown.length - 1]);
      if (!(p[2] < 0 && p[3] * p[3] + p[4] * p[4] < 1)) { glowDot(ctx, p[0], p[1], 16, WHITE, 0.6); disc(ctx, p[0], p[1], 3, rgb(WHITE)); }
    }
    return prog;
  }

  function drawGlobeMarks(ctx, C, now) {
    const vis = (v) => { const p = projV(C, v); return p[2] > 0.03 ? p : null; };
    zoneHits = [];
    if (st.zonesShown) {
      const marks = [projV(C, st.homeV)];
      if (st.target) marks.push(projV(C, st.target.v));
      for (const z of st.zones) {
        const p = projV(C, z.v);
        if (p[2] > 0.2 && clearOf(p[0], p[1], marks)) zoneMark(ctx, p[0], p[1], z);
      }
    }
    for (const h of st.heard) strokeRun(ctx, C, pathPoints(st.homeV, h.v, false), rgb(AMBER, 0.6), 1.5);
    for (const s of st.spots) {
      const p = vis(s.v);
      if (p) {
        glowDot(ctx, p[0], p[1], 10, VFD, 0.3);
        disc(ctx, p[0], p[1], 4, rgb(VFD));
        if (s.fresh) ring(ctx, p[0], p[1], 8, rgb(AMBER), 2);
      }
    }
    for (const h of st.heard) {
      const p = vis(h.v);
      if (p) ring(ctx, p[0], p[1], 6, rgb(AMBER), 2);
    }
    for (const o of st.openings) {   // on top: these were asked for
      const p = vis(o.v);
      if (p) { disc(ctx, p[0], p[1], 5, 'rgba(6,18,31,0.9)'); disc(ctx, p[0], p[1], 3.6, rgb(GREEN)); }
    }
    // The sun, where it's overhead.
    const sp = vis(st.sun);
    if (sp) sunIcon(ctx, sp[0], sp[1]);
    // The ISS and the circle it can be heard in.
    const iss = st.iss;
    if (iss) {
      const p = vis(iss.v);
      if (p) {
        strokeRun(ctx, C, iss.track, 'rgba(230,237,246,0.3)', 1.5, [2, 4]);
        strokeRun(ctx, C, iss.foot, 'rgba(230,237,246,0.75)', 1.5, [5, 4]);
        issIcon(ctx, p[0], p[1], st.issOpen);
      }
    }
    const sat = st.sat;
    if (sat) {
      strokeRun(ctx, C, sat.track, 'rgba(230,237,246,0.3)', 1.5, [2, 4]);
      strokeRun(ctx, C, sat.foot, 'rgba(230,237,246,0.75)', 1.5, [5, 4]);
      const p = vis(sat.v);
      if (p) satIcon(ctx, p[0], p[1], sat.name);
    }
    const hp = projV(C, st.homeV);
    const T = st.target;
    let tp = null;
    if (T) tp = projV(C, T.v);
    if (hp[2] > 0) {
      const left = tp && tp[0] > hp[0];
      homeMark(ctx, hp[0], hp[1], st.home.call, left ? -16 : 16, -14);
    }
    if (T && tp[2] > 0) {
      const pulse = REDUCED ? 0 : (now - globe.pulseT0) / 1100;
      const right = tp[0] >= hp[0];
      targetMark(ctx, tp[0], tp[1], T.call, pulse > 0 && pulse < 2 ? pulse % 1 : 0, right ? 14 : -14, 24);
    }
  }

  function drawGlobe(now) {
    const L = layers.globe;
    const ctx = L.ctx;
    if (!globe.center) globe.center = homeView();
    let busy = false;
    if (globe.from) {
      const t = clamp((now - globe.t0) / globe.dur, 0, 1);
      globe.center = norm(slerp(globe.from, globe.to, ease(t)));
      if (t >= 1) globe.from = null; else busy = true;
    } else if (!globe.drag && (Math.abs(globe.vel[0]) > 0.02 || Math.abs(globe.vel[1]) > 0.02)) {
      spinBy(globe.vel[0], globe.vel[1]);
      globe.vel[0] *= 0.93; globe.vel[1] *= 0.93;
      busy = true;
    }
    const C = cam(L);
    ctx.clearRect(0, 0, L.w, L.h);
    if (!globe.stars || globe.stars.width !== L.canvas.width || globe.stars.height !== L.canvas.height) makeStars(L);
    ctx.drawImage(globe.stars, 0, 0, L.w, L.h);
    // The atmosphere's glow around the edge.
    const halo = ctx.createRadialGradient(C.cx, C.cy, C.R * 0.92, C.cx, C.cy, C.R * 1.22);
    halo.addColorStop(0, 'rgba(103,209,255,0.30)');
    halo.addColorStop(0.35, 'rgba(103,209,255,0.10)');
    halo.addColorStop(1, 'rgba(103,209,255,0)');
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(C.cx, C.cy, C.R * 1.22, 0, TAU);
    ctx.fill();
    drawOcean(ctx, C);
    drawGraticule(ctx, C);
    drawGlobeDots(ctx, C);
    drawTerminator(ctx, C);
    ring(ctx, C.cx, C.cy, C.R, 'rgba(103,209,255,0.35)', 1);
    const prog = drawGlobePath(ctx, C, now);
    if (prog !== undefined && prog < 1) busy = true;
    drawGlobeMarks(ctx, C, now);
    if (st.target && now - globe.pulseT0 < 2200) busy = true;
    L.dirty = false;
    return busy;
  }

  function spinBy(dx, dy) {   // pixels of drag -> turn the globe
    const L = layers.globe, { R } = globeSize(L);
    const [lat, lon] = latLon(globe.center);
    globe.center = vec(clamp(lat + dy / R / RAD * 0.9, -85, 85), lon - dx / R / RAD * 0.9);
  }

  function setupDrag() {
    const c = layers.globe.canvas;
    c.addEventListener('pointerdown', (e) => {
      globe.from = null;
      globe.press = { x: e.clientX, y: e.clientY };
      globe.drag = { x: e.clientX, y: e.clientY, t: performance.now() };
      globe.vel = [0, 0];
      c.setPointerCapture(e.pointerId);
      c.classList.add('dragging');
    });
    c.addEventListener('pointermove', (e) => {
      if (!globe.drag) {
        const r = c.getBoundingClientRect();
        c.style.cursor = zoneAt(e.clientX - r.left, e.clientY - r.top) ? 'pointer' : '';
        return;
      }
      const dx = e.clientX - globe.drag.x, dy = e.clientY - globe.drag.y;
      const now = performance.now(), dt = Math.max(8, now - globe.drag.t);
      spinBy(dx, dy);
      globe.vel = [dx * 16 / dt, dy * 16 / dt];
      globe.drag = { x: e.clientX, y: e.clientY, t: now };
      requestDraw();
    });
    const end = (e) => {
      if (!globe.drag) return;
      if (performance.now() - globe.drag.t > 120) globe.vel = [0, 0];
      globe.drag = null;
      c.classList.remove('dragging');
      if (e.type === 'pointerup' && globe.press && Math.hypot(e.clientX - globe.press.x, e.clientY - globe.press.y) < 5) {
        globe.vel = [0, 0];
        const r = c.getBoundingClientRect(), zone = zoneAt(e.clientX - r.left, e.clientY - r.top);
        if (zone && zoneClick) zoneClick(zone);
      }
      requestDraw();
    };
    c.addEventListener('pointerup', end);
    c.addEventListener('pointercancel', end);
  }

  /* ---------- beam map ---------- */
  const beam = { base: null, frame: null, grid: null, hover: null, wedgeT0: 0, size: '' };
  function beamSize(L) {
    const R = Math.floor(Math.min(L.h / 2 - 30, L.w / 2 - 40));
    return { R, cx: Math.round(L.w / 2), cy: Math.round(L.h / 2) };
  }
  function beamBasis() {
    const H = st.homeV, { E, N } = basis(H);
    return { H, E, N };
  }
  function beamProj(G, v) {   // a point on the earth -> x, y on the beam map
    const c = Math.acos(clamp(dot(v, G.H), -1, 1));
    const b = Math.atan2(dot(v, G.E), dot(v, G.N));
    const r = G.R * c / Math.PI;
    return [G.cx + r * Math.sin(b), G.cy - r * Math.cos(b), b, c];
  }
  function beamGeom(L) {
    const s = beamSize(L);
    return { ...s, ...beamBasis() };
  }
  function buildBeamGrid(G) {
    const pitch = 5.4;
    const pts = [];
    for (let j = -Math.ceil(G.R / (pitch * 0.866)); j * pitch * 0.866 <= G.R; j++) {
      const y = j * pitch * 0.866;
      const off = (j & 1) ? pitch / 2 : 0;
      for (let x = -G.R - off; x <= G.R; x += pitch) {
        const xx = x + off, r = Math.hypot(xx, y);
        if (r > G.R - 1.5) continue;
        const b = Math.atan2(xx, -y), c = Math.PI * r / G.R;
        const v = [
          Math.cos(c) * G.H[0] + Math.sin(c) * (Math.sin(b) * G.E[0] + Math.cos(b) * G.N[0]),
          Math.cos(c) * G.H[1] + Math.sin(c) * (Math.sin(b) * G.E[1] + Math.cos(b) * G.N[1]),
          Math.cos(c) * G.H[2] + Math.sin(c) * (Math.sin(b) * G.E[2] + Math.cos(b) * G.N[2]),
        ];
        const [lat, lon] = latLon(v);
        pts.push({ x: G.cx + xx, y: G.cy + y, v, c: landAt(lat, lon) });
      }
    }
    // Small islands land on the nearest dot.
    const key = (x, y) => Math.round(x) + ',' + Math.round(y);
    const index = new Map(pts.map((p, i) => [key(p.x, p.y), i]));
    for (const [lat, lon, c] of world.islands) {
      const [x, y] = beamProj(G, vec(lat, lon));
      let best = -1, bd = 1e9;
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const i = index.get(key(x + dx, y + dy));
          if (i === undefined) continue;
          const d = (pts[i].x - x) ** 2 + (pts[i].y - y) ** 2;
          if (d < bd) { bd = d; best = i; }
        }
      }
      if (best >= 0 && !pts[best].c) pts[best].c = c || 6;
    }
    beam.grid = pts;
  }
  function drawBeamFrame(L, G) {   // rings, spokes and the compass round the edge: drawn once per size
    const c = document.createElement('canvas');
    c.width = L.canvas.width; c.height = L.canvas.height;
    const x = c.getContext('2d');
    x.scale(dpr, dpr);
    // Spokes every 30 degrees.
    x.strokeStyle = 'rgba(157,176,200,0.13)';
    x.lineWidth = 1;
    x.beginPath();
    for (let a = 0; a < 360; a += 30) {
      const s = Math.sin(a * RAD), co = Math.cos(a * RAD);
      x.moveTo(G.cx + 14 * s, G.cy - 14 * co);
      x.lineTo(G.cx + G.R * s, G.cy - G.R * co);
    }
    x.stroke();
    // Distance rings every 5,000 km (their labels are drawn with the path, out of its way).
    for (const km of [5000, 10000, 15000]) {
      const r = G.R * (km / 6371) / Math.PI;
      x.setLineDash([3, 5]);
      ring(x, G.cx, G.cy, r, 'rgba(157,176,200,0.30)', 1);
      x.setLineDash([]);
    }
    // The rim, with a tick every 5 degrees and a number every 30.
    ring(x, G.cx, G.cy, G.R, 'rgba(103,209,255,0.45)', 1.5);
    x.strokeStyle = 'rgba(157,176,200,0.6)';
    x.beginPath();
    for (let a = 0; a < 360; a += 5) {
      const len = a % 30 === 0 ? 9 : a % 10 === 0 ? 6 : 3;
      const s = Math.sin(a * RAD), co = Math.cos(a * RAD);
      x.moveTo(G.cx + (G.R + 2) * s, G.cy - (G.R + 2) * co);
      x.lineTo(G.cx + (G.R + 2 + len) * s, G.cy - (G.R + 2 + len) * co);
    }
    x.stroke();
    const names = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' };
    for (let a = 0; a < 360; a += 30) {
      const s = Math.sin(a * RAD), co = Math.cos(a * RAD), rr = G.R + 22;
      x.font = `700 ${names[a] ? 15 : 13}px ${MONO}`;
      x.textAlign = 'center';
      x.textBaseline = 'middle';
      x.fillStyle = names[a] ? rgb(WHITE) : 'rgba(157,176,200,0.95)';
      x.fillText(names[a] || `${a}°`, G.cx + rr * s, G.cy - rr * co);
    }
    beam.frame = c;
  }
  function drawBeamBase(L, G) {   // the dots, lit by the sun: redrawn every minute
    if (!beam.base || beam.base.width !== L.canvas.width || beam.base.height !== L.canvas.height) {
      beam.base = document.createElement('canvas');
      beam.base.width = L.canvas.width; beam.base.height = L.canvas.height;
    }
    const x = beam.base.getContext('2d');
    x.setTransform(1, 0, 0, 1, 0, 0);
    x.clearRect(0, 0, beam.base.width, beam.base.height);
    x.scale(dpr, dpr);
    // The disc itself, a little lighter than the panel.
    const bg = x.createRadialGradient(G.cx, G.cy, 0, G.cx, G.cy, G.R);
    bg.addColorStop(0, 'rgba(18,34,58,0.85)');
    bg.addColorStop(1, 'rgba(10,20,36,0.85)');
    x.fillStyle = bg;
    x.beginPath();
    x.arc(G.cx, G.cy, G.R, 0, TAU);
    x.fill();
    const B = new Buckets(), S = st.sun;
    for (const p of beam.grid) {
      const [b, g] = lightOf(dot(p.v, S), 0.55);
      const step = Math.round(b * (STEPS - 1));
      B.add(shade(p.c, step, g), p.x, p.y, p.c ? 2.05 : (g ? 0.95 : 0.75));
    }
    B.draw(x);
    x.drawImage(beam.frame, 0, 0, L.w, L.h);
  }
  function drawBeam(now) {
    const L = layers.beam, ctx = L.ctx;
    const G = beamGeom(L);
    const size = `${L.w}x${L.h}x${dpr}|${st.home.lat},${st.home.lon}`;
    if (beam.size !== size || !beam.grid) {
      beam.size = size;
      buildBeamGrid(G);
      drawBeamFrame(L, G);
      beam.baseSun = null;
    }
    if (beam.baseSun !== st.sunLL.join()) { drawBeamBase(L, G); beam.baseSun = st.sunLL.join(); }
    ctx.clearRect(0, 0, L.w, L.h);
    ctx.drawImage(beam.base, 0, 0, L.w, L.h);
    let busy = false;
    const T = st.target;
    zoneHits = [];
    if (st.zonesShown) {
      const marks = [[G.cx, G.cy]];
      if (T) marks.push(beamProj(G, T.v));
      ctx.globalAlpha = 0.85;
      for (const z of st.zones) {
        const [x, y] = beamProj(G, z.v);
        if (clearOf(x, y, marks)) zoneMark(ctx, x, y, z);
      }
      ctx.globalAlpha = 1;
    }
    if (T) {
      const grow = REDUCED ? 1 : ease(clamp((now - beam.wedgeT0) / 700, 0, 1));
      if (grow < 1) busy = true;
      const b = T.bearing * RAD, half = BEAM_WIDTH / 2 * RAD, R = G.R * grow;
      // The beam's main lobe.
      const wedge = ctx.createRadialGradient(G.cx, G.cy, 0, G.cx, G.cy, G.R);
      wedge.addColorStop(0, rgb(VFD, 0.26));
      wedge.addColorStop(1, rgb(VFD, 0.05));
      ctx.fillStyle = wedge;
      ctx.beginPath();
      ctx.moveTo(G.cx, G.cy);
      ctx.arc(G.cx, G.cy, R, b - half - Math.PI / 2, b + half - Math.PI / 2);
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = rgb(VFD, 0.35);
      ctx.lineWidth = 1;
      ctx.stroke();
      // The long path, straight out the back.
      const lb = b + Math.PI;
      ctx.setLineDash([6, 6]);
      ctx.strokeStyle = rgb(AMBER, 0.7);
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(G.cx + 16 * Math.sin(lb), G.cy - 16 * Math.cos(lb));
      ctx.lineTo(G.cx + R * Math.sin(lb), G.cy - R * Math.cos(lb));
      ctx.stroke();
      // The short path: solid to the station, dotted on to the far side of the world.
      const [tx, ty] = beamProj(G, T.v);
      const tr = Math.hypot(tx - G.cx, ty - G.cy);
      ctx.strokeStyle = rgb(VFD, 0.55);
      ctx.setLineDash([2, 5]);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(tx, ty);
      ctx.lineTo(G.cx + R * Math.sin(b), G.cy - R * Math.cos(b));
      ctx.stroke();
      ctx.setLineDash([]);
      const reach = Math.min(tr, R);
      ctx.strokeStyle = rgb(VFD, 0.25);
      ctx.lineWidth = 8;
      ctx.beginPath();
      ctx.moveTo(G.cx, G.cy);
      ctx.lineTo(G.cx + reach * Math.sin(b), G.cy - reach * Math.cos(b));
      ctx.stroke();
      ctx.strokeStyle = rgb(VFD);
      ctx.lineWidth = 2.5;
      ctx.stroke();
      // Headings at the rim.
      const tag = (ang, text, color) => {
        const s = Math.sin(ang), co = Math.cos(ang), rr = G.R + 46;
        ctx.font = `700 15px ${MONO}`;
        const w = ctx.measureText(text).width + 16;
        const x = clamp(G.cx + rr * s, w / 2 + 2, L.w - w / 2 - 2), y = clamp(G.cy - rr * co, 14, L.h - 14);
        ctx.fillStyle = 'rgba(10,19,34,0.9)';
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.roundRect(x - w / 2, y - 13, w, 26, 7);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(text, x, y + 1);
      };
      if (grow >= 1) {
        tag(b, `${Math.round(T.bearing)}°`, rgb(VFD));
        tag(lb, `Long path ${Math.round(T.long_path)}°`, rgb(AMBER));
      }
      if (grow >= 1 || tr < R) targetMark(ctx, tx, ty, T.call, 0, Math.sin(b) < 0 ? -14 : 14, 24);
    }
    if (st.openings.length) {   // a fan of lines out from the middle: the directions the band is open in
      ctx.strokeStyle = rgb(GREEN, 0.38);
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      for (const o of st.openings) {
        const [x, y] = beamProj(G, o.v);
        ctx.moveTo(G.cx, G.cy);
        ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    for (const h of st.heard) {   // a straight line out from the middle is the path itself
      const [x, y] = beamProj(G, h.v);
      ctx.strokeStyle = rgb(AMBER, 0.6);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(G.cx, G.cy);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    for (const s of st.spots) {
      const [x, y] = beamProj(G, s.v);
      glowDot(ctx, x, y, 9, VFD, 0.3);
      disc(ctx, x, y, 3.5, rgb(VFD));
      if (s.fresh) ring(ctx, x, y, 7.5, rgb(AMBER), 2);
    }
    for (const h of st.heard) {
      const [x, y] = beamProj(G, h.v);
      ring(ctx, x, y, 6, rgb(AMBER), 2);
    }
    for (const o of st.openings) {   // on top of the spots
      const [x, y] = beamProj(G, o.v);
      disc(ctx, x, y, 5, 'rgba(6,18,31,0.9)');
      disc(ctx, x, y, 3.6, rgb(GREEN));
    }
    if (st.iss) {
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = 'rgba(230,237,246,0.75)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let prev = null;
      for (const v of st.iss.foot) {
        const [x, y] = beamProj(G, v);
        if (prev && Math.hypot(x - prev[0], y - prev[1]) < G.R * 0.5) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        prev = [x, y];
      }
      ctx.stroke();
      ctx.setLineDash([]);
      const [ix, iy] = beamProj(G, st.iss.v);
      issIcon(ctx, ix, iy, st.issOpen);
    }
    if (st.sat) {
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = 'rgba(230,237,246,0.75)';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let prev = null;
      for (const v of st.sat.foot) {
        const [x, y] = beamProj(G, v);
        if (prev && Math.hypot(x - prev[0], y - prev[1]) < G.R * 0.5) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        prev = [x, y];
      }
      ctx.stroke();
      ctx.setLineDash([]);
      const [sx, sy] = beamProj(G, st.sat.v);
      satIcon(ctx, sx, sy, st.sat.name);
    }
    // Ring labels, along whichever of a few headings is furthest from the path and its long path.
    const busyAngles = T ? [T.bearing, T.bearing + 180] : [];
    if (st.hint) busyAngles.push(beamProj(G, st.hint.v)[2] / RAD);
    const gapTo = (a) => Math.min(...busyAngles.map((b) => Math.abs(((a - b) % 360 + 540) % 360 - 180)), 360);
    const ringAt = [200, 160, 290, 110, 340].reduce((best, a) => (gapTo(a) > gapTo(best) + 25 ? a : best), 200) * RAD;
    for (const km of [5000, 10000, 15000]) {
      const r = G.R * (km / 6371) / Math.PI;
      ctx.save();
      ctx.translate(G.cx + r * Math.sin(ringAt), G.cy - r * Math.cos(ringAt));
      label(ctx, fmtKm(km), 0, 4, 'rgba(198,211,227,0.9)', 'center', 12);
      ctx.restore();
    }
    const away = T ? Math.sin(T.bearing * RAD) >= 0 : false;
    homeMark(ctx, G.cx, G.cy, st.home.call, away ? -16 : 16, T && Math.cos(T.bearing * RAD) < 0 ? -14 : 26);
    if (st.hint) {
      const [hx, hy, hb] = beamProj(G, st.hint.v);
      ctx.strokeStyle = rgb(WHITE, 0.8);
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(G.cx + 16 * Math.sin(hb), G.cy - 16 * Math.cos(hb));
      ctx.lineTo(hx, hy);
      ctx.stroke();
      ctx.setLineDash([]);
      ring(ctx, hx, hy, 6, rgb(WHITE), 2);
      label(ctx, st.hint.name, hx + (Math.sin(hb) < 0 ? -12 : 12), hy - 10, rgb(WHITE), Math.sin(hb) < 0 ? 'right' : 'left', 14);
    }
    if (beam.hover) {
      const { x, y } = beam.hover;
      const dx = x - G.cx, dy = y - G.cy, r = Math.hypot(dx, dy);
      if (r <= G.R && r > 6) {
        const brg = ((Math.atan2(dx, -dy) / RAD) + 360) % 360;
        const km = r / G.R * Math.PI * 6371;
        ctx.strokeStyle = 'rgba(230,237,246,0.35)';
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 4]);
        ctx.beginPath();
        ctx.moveTo(G.cx, G.cy);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.setLineDash([]);
        const text = `${Math.round(brg)}°  ${fmtKm(km)}`;
        label(ctx, text, x + (x > G.cx ? -12 : 12), y - 12, rgb(WHITE), x > G.cx ? 'right' : 'left', 14);
      }
    }
    L.dirty = false;
    return busy;
  }
  function setupHover() {
    const c = layers.beam.canvas;
    c.addEventListener('pointermove', (e) => {
      const r = c.getBoundingClientRect();
      beam.hover = { x: e.clientX - r.left, y: e.clientY - r.top };
      c.style.cursor = zoneAt(beam.hover.x, beam.hover.y) ? 'pointer' : '';
      requestDraw();
    });
    c.addEventListener('pointerleave', () => { beam.hover = null; requestDraw(); });
    c.addEventListener('click', (e) => {
      const r = c.getBoundingClientRect(), zone = zoneAt(e.clientX - r.left, e.clientY - r.top);
      if (zone && zoneClick) zoneClick(zone);
    });
  }

  /* ---------- drawing loop: runs only while something moves ---------- */
  let frame = 0;
  function requestDraw() {
    if (!st.ready || frame) return;
    frame = requestAnimationFrame(render);
  }
  const timing = { flat: [], globe: [], beam: [] };   // how long recent drawings took (ms), for checking speed
  function render(now) {
    frame = 0;
    let busy = false;
    const t0 = performance.now(), view = st.view;
    if (view === 'flat' && layers.flat.dirty) drawFlat();
    else if (view === 'globe') busy = drawGlobe(now);
    else if (view === 'beam') busy = drawBeam(now);
    else return;
    const list = timing[view];
    list.push(performance.now() - t0);
    if (list.length > 120) list.shift();
    if (busy) requestDraw();
  }
  function speed() {
    const out = {};
    for (const [k, list] of Object.entries(timing)) {
      if (!list.length) continue;
      const sorted = [...list].sort((a, b) => a - b);
      out[k] = { frames: list.length, median_ms: +sorted[sorted.length >> 1].toFixed(1), worst_ms: +sorted[sorted.length - 1].toFixed(1) };
    }
    return out;
  }
  function resized() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    for (const L of Object.values(layers)) if (fit(L)) L.dirty = true;
    if (globe.pts) buildLattice(globeSize(layers.globe).R);
    requestDraw();
  }

  /* ---------- what hud.js calls ---------- */
  async function init(home) {
    box = document.getElementById('map');
    st.home = home;
    st.homeV = vec(home.lat, home.lon);
    updateSun();
    try {
      world = await loadWorld();
      if (document.fonts && document.fonts.load) {
        await Promise.race([document.fonts.load(`700 15px ${MONO}`), new Promise((r) => setTimeout(r, 2500))]);
      }
    } catch (err) {
      console.warn('maps: the land data did not load, so the original map stays', err);
      return false;
    }
    for (const name of ['flat', 'globe', 'beam']) makeLayer(name);
    dpr = Math.min(2, window.devicePixelRatio || 1);
    for (const L of Object.values(layers)) fit(L);
    flatSetup();
    buildLattice(globeSize(layers.globe).R);
    setupDrag();
    setupHover();
    new ResizeObserver(resized).observe(box);
    st.ready = true;
    box.classList.add('lit');
    setInterval(() => { updateSun(); layers.flat.dirty = true; requestDraw(); }, 60 * 1000);
    requestDraw();
    return true;
  }

  function setView(view) {
    if (!st.ready) return;
    const was = st.view;
    st.view = view;
    box.dataset.view = view;
    if (view === 'globe' && was !== 'globe') {
      // Arrive facing the station, then swing round to the path.
      globe.center = homeView();
      if (st.target) { globe.arcT0 = performance.now() + 500; globe.pulseT0 = globe.arcT0 + 1300; turnTo(pathView(st.homeV, st.target.v)); }
    }
    if (view === 'beam' && was !== 'beam') beam.wedgeT0 = performance.now();
    if (view === 'flat') layers.flat.dirty = true;
    requestDraw();
  }

  function setTarget(t) {
    const before = st.target;
    st.target = t && t.lat !== null && t.lat !== undefined ? { ...t, v: vec(t.lat, t.lon) } : null;
    if (!st.ready) return;
    if (st.target && (!before || before.call !== st.target.call || before.lat !== st.target.lat)) {
      const now = performance.now();
      globe.arcT0 = now + (st.view === 'globe' ? 450 : 0);
      globe.pulseT0 = globe.arcT0 + 1300;
      beam.wedgeT0 = now;
      if (st.view === 'globe') turnTo(pathView(st.homeV, st.target.v));
    }
    requestDraw();
  }

  // Heading (degrees) and distance (km) from the station to a place, along the short path.
  function pathTo(lat, lon) {
    const v = vec(lat, lon), G = beamBasis();
    const b = (Math.atan2(dot(v, G.E), dot(v, G.N)) / RAD + 360) % 360;
    return { bearing: b, km: Math.acos(clamp(dot(v, G.H), -1, 1)) * 6371 };
  }
  // zones: [{zone, lat, lon, worked}] where each CQ zone's number goes; shown: whether to draw them.
  function setZones(zones, shown) {
    st.zones = zones.map((z) => ({ zone: z.zone, worked: z.worked, v: vec(z.lat, z.lon) }));
    st.zonesShown = shown;
    requestDraw();
  }
  function onZone(fn) { zoneClick = fn; }

  function setHint(place) {
    st.hint = place ? { name: place.name, v: vec(place.lat, place.lon) } : null;
    requestDraw();
  }

  function focusHome() { if (st.ready && st.view === 'globe') turnTo(homeView()); }
  function focusIss() {
    if (st.ready && st.view === 'globe' && st.iss) turnTo(vec(clamp(st.iss.lat - 8, -80, 80), st.iss.lon));
  }

  function setSpots(list) {
    // fresh: a country that isn't in the log yet, drawn with an amber ring
    st.spots = (list || []).filter((s) => s.lat !== null && s.lat !== undefined)
      .map((s) => ({ call: s.call, v: vec(s.lat, s.lon), fresh: s.new === 'country' }));
    requestDraw();
  }
  // points: [[lat, lon, ...], ...] where stations are hearing the station's area (the band picked), or [] to clear.
  function setOpenings(points) {
    st.openings = (points || []).map((p) => ({ v: vec(p[0], p[1]) }));
    requestDraw();
  }
  // The FM satellite whose card is open: snap from /api/sat, foot round the circle it can be heard in.
  function setSat(snap, foot, name) {
    if (!snap || !snap.ok) { st.sat = null; requestDraw(); return; }
    st.sat = {
      name, lat: snap.now.lat, lon: snap.now.lon, v: vec(snap.now.lat, snap.now.lon),
      foot: foot.map(([la, lo]) => vec(la, lo)),
      track: (snap.track || []).slice(10).map(([la, lo]) => vec(la, lo)),
    };
    requestDraw();
  }
  function focusSat() {
    if (st.ready && st.view === 'globe' && st.sat) turnTo(vec(clamp(st.sat.lat - 8, -80, 80), st.sat.lon));
  }
  function setHeard(list) {
    st.heard = (list || []).filter((h) => h.lat !== null && h.lat !== undefined).map((h) => ({ v: vec(h.lat, h.lon) }));
    requestDraw();
  }
  // snap: the ISS reading from /api/iss; foot: [[lat, lon], ...] round the circle it can be heard in.
  function setIss(snap, foot, open) {
    if (!snap || !snap.ok) { st.iss = null; requestDraw(); return; }
    st.iss = {
      lat: snap.now.lat, lon: snap.now.lon, v: vec(snap.now.lat, snap.now.lon),
      foot: foot.map(([la, lo]) => vec(la, lo)),
      track: (snap.track || []).slice(10).map(([la, lo]) => vec(la, lo)),
    };
    st.issOpen = !!open;
    requestDraw();
  }

  return {
    init, setView, setTarget, setSpots, setHeard, setIss, focusHome, focusIss, pathTo, setHint, setZones, onZone,
    setOpenings, setSat, focusSat,
    available: () => st.ready, view: () => st.view, speed,
  };
})();
