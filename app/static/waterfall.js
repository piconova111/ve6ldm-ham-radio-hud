'use strict';
// The waterfall page, on the computer's own screen: the band around the waterfall's radio (the station's first
// radio), from the SDR on its IF. Lines come from /api/waterfall already placed on the radio's own frequencies;
// this page colours them, scrolls them down, slides the picture when the dial turns, and marks where the radio
// listens and transmits. Clicking tunes the radio. Nothing here transmits.
// Opened as /listen (from another computer, through "station listen"), it also plays the sound from the SDR:
// the radio's own frequency, or wherever is clicked. There a click only picks what you hear: it never tunes the radio.
(() => {
  const $ = (id) => document.getElementById(id);
  const LISTEN = location.pathname === '/listen';
  const main = $('wf-main'), spec = $('wf-spec'), falls = $('wf-falls'), over = $('wf-over');
  const sctx = spec.getContext('2d'), fctx = falls.getContext('2d', { willReadFrequently: false }), octx = over.getContext('2d');
  const scratch = document.createElement('canvas'), xctx = scratch.getContext('2d');

  const ROW = 2;          // CSS pixels for each line (ten lines a second: half a minute down the screen)
  const GUTTER = 14;      // CSS pixels for the transmit bar down the left
  const LABELS = 46;      // CSS pixels at the top of the spectrum for callsigns
  const SCALE = 28;       // CSS pixels at the bottom of the spectrum for the frequency scale
  const SPANS = ['20000', '50000', '100000', '200000', 'band'];
  const C = { ground: '#0A1322', panel: '#121F33', grid: 'rgba(43, 64, 97, 0.55)', text: '#E6EDF6', soft: '#C6D3E3',
              muted: '#9DB0C8', dim: '#7F93AD', vfd: '#67D1FF', amber: '#FFB24A', red: '#E5484D', trace: '#8FDBFF',
              listen: '#4ADE80' };
  // One blue, dark to light: the noise stays dark and stations come up bright. Red is kept for transmitting.
  const STOPS = [[0, [10, 19, 34]], [0.22, [15, 44, 79]], [0.48, [26, 90, 146]], [0.72, [63, 163, 221]],
                 [0.88, [143, 219, 255]], [1, [242, 251, 255]]];
  const LUT = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < STOPS.length - 2 && t > STOPS[k + 1][0]) k++;
    const [t0, c0] = STOPS[k], [t1, c1] = STOPS[k + 1];
    for (let j = 0; j < 3; j++) LUT[i * 3 + j] = Math.round(c0[j] + (c1[j] - c0[j]) * (t - t0) / (t1 - t0));
  }

  const S = {
    span: '50000', lo: 0, hi: 0, seq: 0, dial: null, mode: 'USB', width: 2400, tx: false, split: null, radio: '',
    ok: false, on: true, status: null, bands: [], voice: [], spots: [], spotsBand: null, spotsAt: 0, started: Date.now(),
    rows: [], avg: null, floor: null, range: 30, hover: null, labels: [], dpr: 1, w: 0, h: 0, specH: 0, lastLine: 0,
    refill: true,   // draw the whole picture again from the lines the app keeps (opening, zooming, a new band)
    gaps: [],       // strips a turn of the dial uncovered, to fill in from the lines the app keeps
    gen: 0,         // counts whole redraws, so a strip asked for before one isn't painted after it
    listeners: 0,   // people listening from other computers (a sign shows on the station's own screen)
  };
  window.wfState = S;   // to look into from the browser's console (and for the tests)
  try {
    const saved = localStorage.getItem('wfSpan');
    if (SPANS.includes(saved)) S.span = saved;
  } catch (err) { /* no storage here: start at 50 kHz */ }

  // ---------- small helpers ----------
  function fmtFreq(hz) {   // 14.243.20, the way the radio shows it
    const mhz = Math.floor(hz / 1e6), khz = Math.floor((hz % 1e6) / 1000), tens = Math.floor((hz % 1000) / 10);
    return `${mhz}.${String(khz).padStart(3, '0')}.${String(tens).padStart(2, '0')}`;
  }
  const fmtK = (khz) => String(Math.round(khz * 10) / 10);
  const theRadio = () => (S.radio === 'Practice' ? 'the practice radio' : S.radio ? 'the ' + S.radio : 'the radio');
  const cap = (text) => text.charAt(0).toUpperCase() + text.slice(1);
  const bandOf = (hz) => S.bands.find((b) => hz >= b.lo && hz <= b.hi) || null;
  const toX = (hz) => (hz - S.lo) / (S.hi - S.lo) * S.w;
  const toHz = (x) => S.lo + x / S.w * (S.hi - S.lo);
  function sideband(s) {   // as on the station card: the spot's own, or the usual one for the band
    if (s.mode === 'USB' || s.mode === 'LSB') return s.mode;
    return s.freq_khz >= 10000 || (s.freq_khz >= 5250 && s.freq_khz <= 5450) ? 'USB' : 'LSB';
  }
  function passband(f, mode, width) {   // where the radio's filter would be, listening on f
    const w = width || 2400;
    const low = Math.max(100, 1500 - w / 2);   // an SSB filter sits about 1.5 kHz from the frequency
    if (mode === 'USB' || mode === 'PKTUSB') return [f + low, f + low + w];
    if (mode === 'LSB' || mode === 'PKTLSB') return [f - low - w, f - low];
    return [f - w / 2, f + w / 2];
  }
  function splitWord(sp) {
    return sp.khz ? `${sp.dir === 'up' ? 'UP' : 'DN'}${fmtK(sp.khz)}${sp.to ? '-' + fmtK(sp.to) : ''}` : (sp.dir === 'up' ? 'UP' : 'DN');
  }
  function offsetText(hz) {
    return hz ? `${fmtK(Math.abs(hz) / 1000)} kHz ${hz > 0 ? 'up' : 'down'}` : 'on the same frequency';
  }
  function decode(text) {
    const raw = atob(text), out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }
  let toastTimer = null;
  function toast(text) {
    const box = $('wf-toast');
    box.textContent = text;
    box.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.hidden = true; }, 6000);
  }

  // ---------- sizes ----------
  function setSize(c, w, h, top) {
    c.style.width = w + 'px';
    c.style.height = h + 'px';
    c.style.top = top + 'px';
    c.width = Math.round(w * S.dpr);
    c.height = Math.round(h * S.dpr);
  }
  function clearFalls() {
    fctx.fillStyle = C.ground;
    fctx.fillRect(0, 0, falls.width, falls.height);
  }
  function resize() {
    const r = main.getBoundingClientRect();
    S.dpr = window.devicePixelRatio || 1;
    S.w = Math.max(200, Math.floor(r.width));
    S.h = Math.max(240, Math.floor(r.height));
    S.specH = Math.round(Math.min(Math.max(S.h * 0.3, 190), 330));
    setSize(spec, S.w, S.specH, 0);
    setSize(falls, S.w, S.h - S.specH, S.specH);
    setSize(over, S.w, S.h, 0);
    scratch.width = falls.width;
    scratch.height = falls.height;
    clearFalls();
    S.rows = [];
    S.avg = null;
    S.lo = S.hi = 0;   // the picture starts again at the new size,
    S.refill = true;   // ...from the last half-minute the app has kept
    S.gaps = [];
    S.gen += 1;
  }

  // ---------- which frequencies are on show ----------
  function wantView() {
    if (S.dial === null) return null;
    if (S.span === 'band') {
      const b = bandOf(S.dial);
      if (b) {
        const pad = Math.max((b.hi - b.lo) * 0.04, 5000);
        return [b.lo - pad, b.hi + pad];
      }
      return [S.dial - 100000, S.dial + 100000];   // outside the ham bands: 200 kHz around the dial
    }
    const span = Number(S.span);
    return [S.dial - span / 2, S.dial + span / 2];
  }
  // Moves the picture to show lo..hi. A small turn of the dial slides it sideways (a turn smaller than a
  // pixel waits until it adds up to one, so it never drifts from the frequencies it's drawn for). A zoom, a
  // new band or a big jump draws it again from the lines the app keeps, placed for the new frequencies.
  function moveView(lo, hi) {
    const W = falls.width;
    if (S.hi > S.lo && !S.refill && Math.abs((hi - lo) - (S.hi - S.lo)) < 1) {   // same width: a turn of the dial
      const hzpp = (S.hi - S.lo) / W;
      const px = Math.round((lo - S.lo) / hzpp);
      if (px === 0) return;
      if (Math.abs(px) <= W / 4) {
        xctx.clearRect(0, 0, W, falls.height);
        xctx.drawImage(falls, 0, 0);
        clearFalls();
        fctx.imageSmoothingEnabled = false;
        fctx.drawImage(scratch, -px, 0);
        S.lo += px * hzpp;
        S.hi += px * hzpp;
        S.avg = null;
        // The edge the picture slid away from is empty for the older lines; the app still has them.
        const gap = Math.abs(px) * hzpp;
        S.gaps.push(px > 0 ? { lo: S.hi - gap, hi: S.hi, upTo: S.seq, gen: S.gen }
          : { lo: S.lo, hi: S.lo + gap, upTo: S.seq, gen: S.gen });
        return;
      }
    }
    clearFalls();
    S.lo = lo;
    S.hi = hi;
    S.avg = null;
    S.refill = true;
    S.gaps = [];
    S.gen += 1;
  }

  // ---------- lines ----------
  // One line's colours, W pixels of RGBA. The noise sits just under the bottom of the colours, so it stays
  // dark and stations stand out.
  function colours(bytes, d, at, W) {
    const n = bytes.length, lo = (S.floor === null ? -100 : S.floor) + 1, span = S.range;
    for (let x = 0; x < W; x++) {
      const v = bytes[Math.min(n - 1, Math.floor(x * n / W))];
      let k = 0;
      if (v) k = Math.max(0, Math.min(255, Math.round((v - 200 - lo) / span * 255)));
      const i = at + x * 4;
      d[i] = LUT[k * 3];
      d[i + 1] = LUT[k * 3 + 1];
      d[i + 2] = LUT[k * 3 + 2];
      d[i + 3] = 255;
    }
  }
  let rowImg = null;
  function drawLine(bytes) {   // a new line at the top; everything else down a line
    const W = falls.width, H = falls.height, rh = Math.max(1, Math.round(ROW * S.dpr));
    xctx.clearRect(0, 0, W, H);
    xctx.drawImage(falls, 0, 0);
    fctx.imageSmoothingEnabled = false;
    fctx.drawImage(scratch, 0, 0, W, H - rh, 0, rh, W, H - rh);
    if (!rowImg || rowImg.width !== W) rowImg = fctx.createImageData(W, 1);
    colours(bytes, rowImg.data, 0, W);
    for (let y = 0; y < rh; y++) fctx.putImageData(rowImg, 0, y);
  }
  function drawBatch(lines) {   // the whole picture again, from lines oldest first
    const W = falls.width, H = falls.height, rh = Math.max(1, Math.round(ROW * S.dpr));
    const img = fctx.createImageData(W, H), d = img.data;
    for (let i = 0; i < d.length; i += 4) { d[i] = 10; d[i + 1] = 19; d[i + 2] = 34; d[i + 3] = 255; }
    const row = new Uint8ClampedArray(W * 4);
    for (let k = 0; k < lines.length; k++) {
      const y0 = k * rh;
      if (y0 >= H) break;
      colours(lines[lines.length - 1 - k].bytes, row, 0, W);
      for (let y = y0; y < Math.min(H, y0 + rh); y++) d.set(row, y * W * 4);
    }
    fctx.putImageData(img, 0, 0);
  }
  // n values down to w pixels, keeping the strongest under each (the app gives at least 16)
  function shrink(bytes, w) {
    const n = bytes.length, out = new Uint8Array(w);
    for (let x = 0; x < w; x++) {
      const a = Math.floor(x * n / w), b = Math.max(Math.floor((x + 1) * n / w), a + 1);
      let m = 0;
      for (let i = a; i < b; i++) if (bytes[i] > m) m = bytes[i];
      out[x] = m;
    }
    return out;
  }
  // Paints a strip a turn of the dial uncovered, row by row, from the lines the app keeps.
  async function fillGap(g) {
    const W = falls.width, H = falls.height, lo0 = S.lo, hzpp = (S.hi - S.lo) / W;
    if (g.gen !== S.gen || !(hzpp > 0)) return;
    const x0 = Math.max(0, Math.round((g.lo - S.lo) / hzpp)), x1 = Math.min(W, Math.round((g.hi - S.lo) / hzpp));
    const w = x1 - x0;
    if (w < 1) return;   // it has slid off the picture since
    const n = Math.max(16, w), lo = S.lo + x0 * hzpp, hi = S.lo + x1 * hzpp;
    const res = await fetch(`/api/waterfall?since=0&lo=${lo}&hi=${hi}&n=${n}&limit=400`, { cache: 'no-store' });
    const r = await res.json();
    // Anything that moved the picture meanwhile (a resize) means the strip isn't where it was.
    if (g.gen !== S.gen || S.lo !== lo0 || falls.width !== W || !r.lines) return;
    const where = new Map(S.rows.map((row, i) => [row.seq, i]));
    const rh = Math.max(1, Math.round(ROW * S.dpr)), img = fctx.createImageData(w, 1);
    for (const line of r.lines) {
      if (line.seq > g.upTo) continue;   // lines since the turn were drawn whole
      const i = where.get(line.seq);
      if (i === undefined) continue;
      let bytes = decode(line.data);
      if (bytes.length !== n) continue;
      if (n !== w) bytes = shrink(bytes, w);
      colours(bytes, img.data, 0, w);
      for (let y = i * rh; y < Math.min(H, (i + 1) * rh); y++) fctx.putImageData(img, x0, y);
    }
  }
  // Brightness that looks after itself: the noise sits near the bottom of the colours, strong stations near the top.
  function levels(bytes, tx) {
    if (tx) return;   // the radio's own signal would throw it off; keep what it was
    const hist = new Uint32Array(256);
    let count = 0;
    for (let i = 0; i < bytes.length; i++) if (bytes[i]) { hist[bytes[i]]++; count++; }
    if (count < 50) return;
    const at = (q) => {
      let c = 0;
      for (let v = 1; v < 256; v++) { c += hist[v]; if (c >= q * count) return v - 200; }
      return 55;
    };
    const floor = at(0.2), range = Math.max(20, Math.min(60, at(0.995) - floor + 6));
    if (S.floor === null) { S.floor = floor; S.range = range; return; }
    S.floor += (floor - S.floor) * 0.08;
    S.range += (range - S.range) * 0.05;
  }
  function average(bytes) {   // the spectrum line: a little smoothed, so it doesn't flicker
    const n = bytes.length;
    const fresh = !S.avg || S.avg.length !== n;
    if (fresh) S.avg = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const v = bytes[i];
      if (!v) { S.avg[i] = NaN; continue; }
      const db = v - 200;
      S.avg[i] = fresh || Number.isNaN(S.avg[i]) ? db : S.avg[i] + (db - S.avg[i]) * 0.25;
    }
  }

  // ---------- drawing ----------
  function vline(ctx, x, y0, y1) {
    ctx.beginPath();
    ctx.moveTo(x, y0);
    ctx.lineTo(x, y1);
    ctx.stroke();
  }
  function shadeOutOfBand(ctx, y0, y1) {   // outside the band the dial is in: darker
    const b = S.dial !== null ? bandOf(S.dial) : null;
    if (!b) return;
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    const a = toX(b.lo), z = toX(b.hi);
    if (a > 0) ctx.fillRect(0, y0, Math.min(a, S.w), y1 - y0);
    if (z < S.w) ctx.fillRect(Math.max(z, 0), y0, S.w - Math.max(z, 0), y1 - y0);
  }
  function drawSpectrum() {
    const W = S.w, H = S.specH, top = LABELS, bottom = H - SCALE;
    sctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    sctx.fillStyle = C.ground;
    sctx.fillRect(0, 0, W, H);
    if (!(S.hi > S.lo)) return;
    shadeOutOfBand(sctx, top, bottom);
    const lo = S.floor !== null ? S.floor - 8 : -120, hi = lo + S.range + 16;
    const yOf = (db) => bottom - (db - lo) / (hi - lo) * (bottom - top);
    sctx.strokeStyle = C.grid;
    sctx.lineWidth = 1;
    for (let db = Math.ceil(lo / 10) * 10; db < hi; db += 10) {
      const y = Math.round(yOf(db)) + 0.5;
      sctx.beginPath();
      sctx.moveTo(0, y);
      sctx.lineTo(W, y);
      sctx.stroke();
    }
    // the voice part of the band: dotted edges
    sctx.save();
    sctx.setLineDash([2, 4]);
    sctx.strokeStyle = 'rgba(157, 176, 200, 0.55)';
    for (const [a, b] of S.voice) {
      for (const f of [a, b]) if (f > S.lo && f < S.hi) vline(sctx, Math.round(toX(f)) + 0.5, top, bottom);
    }
    sctx.restore();
    // where the radio's filter is
    if (S.ok && S.dial !== null) {
      const [a, b] = passband(S.dial, S.mode, S.width);
      sctx.fillStyle = 'rgba(103, 209, 255, 0.16)';
      sctx.fillRect(toX(a), top, Math.max(1, toX(b) - toX(a)), bottom - top);
    }
    // what you're listening to (on the listening page): green
    const lw = LISTEN ? listenWhere() : null;
    if (lw) {
      const [a, b] = listenPass(lw[0], lw[1]);
      sctx.fillStyle = A.on ? 'rgba(74, 222, 128, 0.22)' : 'rgba(74, 222, 128, 0.10)';
      sctx.fillRect(toX(a), top, Math.max(1, toX(b) - toX(a)), bottom - top);
    }
    // the spectrum
    if (S.avg) {
      const n = S.avg.length;
      sctx.beginPath();
      let pen = false;
      for (let i = 0; i < n; i++) {
        const v = S.avg[i];
        if (Number.isNaN(v)) { pen = false; continue; }
        const x = (i + 0.5) / n * W, y = Math.max(top, Math.min(bottom, yOf(v)));
        if (pen) sctx.lineTo(x, y); else { sctx.moveTo(x, y); pen = true; }
      }
      sctx.strokeStyle = C.trace;
      sctx.lineWidth = 1.5;
      sctx.stroke();
      sctx.lineTo(W, bottom);   // a faint fill under it
      sctx.lineTo(0, bottom);
      sctx.fillStyle = 'rgba(103, 209, 255, 0.07)';
      sctx.fill();
    }
    drawLabels(top);
    drawScale(bottom);
  }
  function drawLabels(top) {
    S.labels = [];
    const spots = S.spots.filter((s) => s.freq_khz * 1000 > S.lo && s.freq_khz * 1000 < S.hi)
      .sort((a, b) => a.freq_khz - b.freq_khz);
    sctx.font = "bold 14px 'B612 Mono', 'DejaVu Sans Mono', monospace";
    sctx.textBaseline = 'middle';
    const ends = [-1e9, -1e9];   // where the last label in each row ends
    for (const s of spots) {
      const x = toX(s.freq_khz * 1000);
      const text = s.call + (s.split ? ' ' + splitWord(s.split) : '');
      const w = sctx.measureText(text).width + 12;
      const left = Math.max(2, Math.min(S.w - w - 2, x - w / 2));
      const row = ends.findIndex((end) => left > end + 4);
      sctx.strokeStyle = 'rgba(198, 211, 227, 0.4)';
      sctx.lineWidth = 1;
      if (row < 0) {   // no room for the name: just a mark where they are
        vline(sctx, Math.round(x) + 0.5, top - 8, top + 6);
        continue;
      }
      const y = 4 + row * 20 + 9;
      ends[row] = left + w;
      vline(sctx, Math.round(x) + 0.5, y + 9, top + 6);
      sctx.fillStyle = 'rgba(18, 31, 51, 0.94)';
      sctx.strokeStyle = s.new ? C.amber : 'rgba(103, 209, 255, 0.55)';
      sctx.beginPath();
      if (sctx.roundRect) sctx.roundRect(left, y - 9, w, 18, 4); else sctx.rect(left, y - 9, w, 18);
      sctx.fill();
      sctx.stroke();
      sctx.fillStyle = s.new ? C.amber : C.text;
      sctx.fillText(text, left + 6, y + 1);
      S.labels.push({ x0: left, x1: left + w, y0: y - 9, y1: y + 9, spot: s });
    }
  }
  function drawScale(y0) {
    const span = S.hi - S.lo, W = S.w;
    sctx.fillStyle = C.panel;
    sctx.fillRect(0, y0, W, SCALE);
    const step = [500, 1000, 2000, 5000, 10000, 20000, 25000, 50000, 100000, 200000].find((s) => s / span * W >= 110) || 500000;
    sctx.font = "13px 'B612 Mono', 'DejaVu Sans Mono', monospace";
    sctx.textAlign = 'center';
    sctx.textBaseline = 'alphabetic';
    sctx.strokeStyle = C.dim;
    sctx.fillStyle = C.muted;
    for (let f = Math.ceil(S.lo / step) * step; f <= S.hi; f += step) {
      const x = Math.round(toX(f)) + 0.5;
      vline(sctx, x, y0, y0 + 6);
      sctx.fillText((f / 1e6).toFixed(step < 1000 ? 4 : 3), x, y0 + 21);
    }
    sctx.textAlign = 'left';
  }
  function drawOverlay() {
    const W = S.w, H = S.h, sh = S.specH;
    octx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    octx.clearRect(0, 0, W, H);
    if (!(S.hi > S.lo)) return;
    shadeOutOfBand(octx, sh, H);
    // transmissions: a red bar down the left and a red tint across, with where it transmitted
    let runTop = -1;
    for (let i = 0; i <= S.rows.length; i++) {
      const r = S.rows[i];
      if (r && r.tx) {
        if (runTop < 0) runTop = i;
        continue;
      }
      if (runTop >= 0) {
        const y0 = sh + runTop * ROW, y1 = Math.min(H, sh + i * ROW);
        octx.fillStyle = 'rgba(229, 72, 77, 0.12)';
        octx.fillRect(GUTTER, y0, W - GUTTER, y1 - y0);
        octx.fillStyle = C.red;
        octx.fillRect(0, y0, GUTTER, y1 - y0);
        const hz = S.rows[runTop].txHz;
        const text = 'TX' + (hz ? ' ' + fmtFreq(hz) : '');
        octx.font = "bold 13px 'B612 Mono', 'DejaVu Sans Mono', monospace";
        octx.textBaseline = 'top';
        const w = octx.measureText(text).width + 10;
        octx.fillStyle = 'rgba(10, 19, 34, 0.85)';
        octx.fillRect(GUTTER + 4, y0 + 3, w, 18);
        octx.fillStyle = '#FF8A8D';
        octx.fillText(text, GUTTER + 9, y0 + 6);
        runTop = -1;
      }
    }
    // where the radio listens
    if (S.ok && S.dial !== null) {
      octx.strokeStyle = 'rgba(103, 209, 255, 0.9)';
      octx.lineWidth = 1;
      vline(octx, Math.round(toX(S.dial)) + 0.5, LABELS, H);
    }
    // where you listen, when it isn't the radio's own frequency (the listening page)
    const lw = LISTEN && !A.follow ? listenWhere() : null;
    if (lw) {
      const x = Math.round(toX(lw[0])) + 0.5;
      octx.save();
      octx.strokeStyle = A.on ? C.listen : 'rgba(74, 222, 128, 0.5)';
      octx.lineWidth = 1.5;
      vline(octx, x, LABELS, H);
      octx.restore();
      const text = `LISTENING ${fmtFreq(lw[0])} ${lw[1]}`;
      octx.font = "bold 13px 'B612 Mono', 'DejaVu Sans Mono', monospace";
      octx.textBaseline = 'top';
      const w = octx.measureText(text).width + 10;
      const left = Math.max(2, Math.min(W - w - 2, x + 6));
      octx.fillStyle = 'rgba(10, 19, 34, 0.9)';
      octx.fillRect(left, LABELS + 26, w, 18);
      octx.fillStyle = A.on ? C.listen : 'rgba(74, 222, 128, 0.7)';
      octx.fillText(text, left + 5, LABELS + 29);
    }
    // where it transmits: red while it does, amber dashes when split puts it somewhere else
    const splitHz = S.ok && S.split && S.split.on && S.split.tx_hz ? S.split.tx_hz : null;
    if (S.ok && (S.tx || splitHz)) {
      const f = splitHz || S.dial, x = Math.round(toX(f)) + 0.5;
      octx.save();
      octx.lineWidth = S.tx ? 2 : 1.5;
      octx.strokeStyle = S.tx ? C.red : C.amber;
      if (!S.tx) octx.setLineDash([6, 5]);
      vline(octx, x, LABELS, H);
      octx.restore();
      const text = S.tx ? `TX ${fmtFreq(f)}` : `TX ${fmtFreq(f)} · ${offsetText(f - S.dial)}`;
      octx.font = "bold 13px 'B612 Mono', 'DejaVu Sans Mono', monospace";
      octx.textBaseline = 'top';
      const w = octx.measureText(text).width + 10;
      const left = Math.max(2, Math.min(W - w - 2, x + 6));
      octx.fillStyle = 'rgba(10, 19, 34, 0.9)';
      octx.fillRect(left, LABELS + 4, w, 18);
      octx.fillStyle = S.tx ? '#FF8A8D' : C.amber;
      octx.fillText(text, left + 5, LABELS + 7);
    }
    // the pointer: where a click would put the radio's frequency, and where its filter would land (on the
    // listening page: what a click would let you hear)
    const hv = S.hover;
    if (hv && !hv.label && hv.y >= LABELS) {
      const f = Math.round(toHz(hv.x) / 10) * 10, x = Math.round(hv.x) + 0.5;
      const kind = LISTEN ? (A.picked || usualKind(f)) : null;
      const [a, b] = LISTEN ? listenPass(f, kind) : passband(f, S.mode, S.width);
      octx.fillStyle = 'rgba(242, 251, 255, 0.10)';
      octx.fillRect(toX(a), LABELS, Math.max(1, toX(b) - toX(a)), sh - SCALE - LABELS);
      octx.strokeStyle = 'rgba(242, 251, 255, 0.55)';
      octx.lineWidth = 1;
      vline(octx, x, LABELS, H);
      const text = LISTEN ? `${fmtFreq(f)} ${kind} · click to listen` : fmtFreq(f) + (S.mode ? ' ' + S.mode : '');
      octx.font = "bold 14px 'B612 Mono', 'DejaVu Sans Mono', monospace";
      octx.textBaseline = 'top';
      const w = octx.measureText(text).width + 12;
      const left = Math.max(2, Math.min(W - w - 2, hv.x + 10));
      octx.fillStyle = 'rgba(10, 19, 34, 0.92)';
      octx.fillRect(left, sh + 6, w, 22);
      octx.fillStyle = C.text;
      octx.fillText(text, left + 6, sh + 10);
    }
  }

  // ---------- the header, the footer and messages ----------
  function header() {
    $('wf-name').textContent = S.radio || 'Radio';
    $('wf-freq').textContent = S.ok && S.dial ? fmtFreq(S.dial) : 'not answering';
    $('wf-mode').textContent = S.ok ? (S.mode || '') : '';
    $('wf-onair').classList.toggle('lit', S.ok && S.tx);
    const sp = S.ok && S.split && S.split.on && S.split.tx_hz ? S.split : null;
    const chip = $('wf-split');
    chip.hidden = !sp;
    if (sp) {
      const b = document.createElement('b');
      b.textContent = 'SPLIT';
      chip.replaceChildren(b, `TX ${fmtFreq(sp.tx_hz)}`);
    }
    // On the station's own screen: a sign while someone listens from another computer.
    const ears = $('wf-ears');
    ears.hidden = LISTEN || !S.listeners;
    if (!ears.hidden) $('wf-ears-text').textContent = S.listeners === 1 ? 'Remote listener' : `${S.listeners} remote listeners`;
    // (The station's window is found by this title: keep it as it is there.)
    document.title = LISTEN ? `Listening · ${S.radio || 'Radio'}` : S.radio ? `Waterfall · ${S.radio}` : 'Waterfall';
  }
  function footer() {
    const st = S.status || {};
    let text;
    if (!S.on) text = 'The waterfall is switched off.';
    else if (st.problem) text = `The waterfall: ${st.problem}.`;
    else if (!st.running) text = 'Starting the waterfall…';
    else {
      text = `Live from ${theRadio()}’s IF, ${st.lines_per_s || 10} lines a second. Picture orientation: ${st.orientation_how}.`;
      const tr = st.track;   // checked against shortwave broadcast stations; small drift corrected by itself
      if (tr && typeof tr.off_hz === 'number') {
        const n = `${tr.stations} broadcast station${tr.stations === 1 ? '' : 's'}`;
        text += Math.abs(tr.off_hz) <= 30 ? ` On frequency (checked on ${n} at ${tr.when}).`
          : ` ${Math.abs(tr.off_hz)} Hz off at ${tr.when} (${n}); lining it up.`;
      }
    }
    $('wf-status').textContent = text;
  }
  function message() {
    const st = S.status || {};
    let text = '';
    if (!S.on) text = 'The waterfall is switched off (start = no under [waterfall] in config.ini).';
    else if (st.problem) text = `The waterfall can’t show the band: ${st.problem}.`;
    else if (!S.ok) text = `${cap(theRadio())} isn’t answering, so the waterfall can’t follow its dial.`;
    else if (!S.lastLine && Date.now() - S.started > 4000) text = 'Waiting for the waterfall’s receiver…';
    $('wf-msg').textContent = text;
    $('wf-msg').hidden = !text;
  }

  // ---------- spots ----------
  async function spotsMaybe() {
    const b = S.dial !== null ? bandOf(S.dial) : null;
    if (!b) { S.spots = []; return; }
    if (b.name === S.spotsBand && Date.now() - S.spotsAt < 20000) return;
    S.spotsBand = b.name;
    S.spotsAt = Date.now();
    try {
      const r = await (await fetch(`/api/spots?band=${encodeURIComponent(b.name)}&voice=0`, { cache: 'no-store' })).json();
      S.spots = (r.spots || []).filter((s) => typeof s.freq_khz === 'number');
    } catch (err) { /* keep the ones we had */ }
  }
  function showTip(s, x, y) {
    const tip = $('wf-tip');
    const line = (cls, text) => { const span = document.createElement('span'); span.className = cls; span.textContent = text; return span; };
    const br = () => document.createElement('br');
    const parts = [line('tip-call', s.call), (s.region || s.country) ? ` · ${s.region || s.country}` : '', br(),
      line('tip-freq', `On ${fmtFreq(Math.round(s.freq_khz * 1000))} ${sideband(s)}`), br()];
    if (s.split) {
      parts.push(line('tip-split', s.split.khz ? `Listening ${s.split.dir} ${fmtK(s.split.khz)}${s.split.to ? ' to ' + fmtK(s.split.to) : ''}`
        + (s.split.tx_khz && !s.split.why ? `: transmit on ${fmtFreq(Math.round(s.split.tx_khz * 1000))}` : s.split.why ? ` (${s.split.why})` : '')
        : `Listening ${s.split.dir}`), br());
    }
    const note = (s.comment || '').trim();
    parts.push(`Spotted by ${s.spotter} ${s.age_min < 1 ? 'just now' : s.age_min + ' min ago'}${note ? `: “${note.length > 40 ? note.slice(0, 39) + '…' : note}”` : ''}`, br(),
      line('tip-hint', LISTEN ? 'Click to listen to them.'
        : `Click to tune ${theRadio()} to them${s.split && s.split.tx_khz && !s.split.why ? ', with split' : ''}.`));
    tip.replaceChildren(...parts);
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(6, Math.min(S.w - w - 6, x + 14)) + 'px';
    tip.style.top = Math.min(S.h - h - 6, y + 16) + 'px';
  }
  const hideTip = () => { $('wf-tip').hidden = true; };

  // ---------- tuning ----------
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  const who = (r) => (r.radio ? 'The ' + r.radio : 'The radio');
  function trouble(r) {
    if (r.no_antenna) return `${who(r)} has no ${r.no_antenna.replace('m', ' m')} antenna.`;
    return `${who(r)} didn’t tune: ${r.why || 'it isn’t answering'}.`;
  }
  async function tuneTo(f) {
    const hz = Math.round(f / 10) * 10;
    try {
      const r = await post('/api/rig/tune', { freq_hz: hz, radio: 'radio', keep_split: true });
      toast(r.ok ? `${who(r)} is listening on ${fmtFreq(hz)}.` : trouble(r));
    } catch (err) {
      toast('The station app isn’t answering.');
    }
  }
  async function tuneSpot(s) {
    const hz = Math.round(s.freq_khz * 1000), mode = sideband(s);
    const dx = s.split && s.split.tx_khz && !s.split.why ? Math.round(s.split.tx_khz * 1000) : null;
    try {
      const r = await post('/api/rig/tune', { freq_hz: hz, mode, split_tx_hz: dx, radio: 'radio' });
      const sp = r.split;
      let text = trouble(r);
      if (r.ok && sp && sp.set) {
        text = `${who(r)} is listening on ${fmtFreq(hz)} ${mode} and transmitting on ${fmtFreq(sp.tx_hz)} (split, ${offsetText(sp.tx_hz - hz)}).`;
      } else if (r.ok) {
        text = `${who(r)} is tuned to ${fmtFreq(hz)} ${mode}.`;
        if (sp && dx) text += ` Split isn’t set: ${sp.why || 'the radio didn’t take it'}.`;
        if (sp && sp.off === true) text += ' Split is off.';
        if (sp && sp.off === false) text += ' Split is still on: turn it off on the radio.';
      }
      toast(text);
    } catch (err) {
      toast('The station app isn’t answering.');
    }
  }

  // ---------- listening (the page opened as /listen, from another computer) ----------
  // The sound is made at the station from the waterfall's own receiver (station/listen.py) and comes here as
  // 16-bit samples, 12,000 a second, in frames: a byte for the kind (A sound, J news), three for the length.
  const A = {
    on: false, ctx: null, node: null, gain: null, id: null, ctrl: null, retry: null, connecting: false,
    follow: true, freq: null, kind: null, picked: null, fine: 0, vol: 70,
    info: {}, stats: null, err: null, level: 0, recent: [], started: 0,
  };
  window.wfListen = A;   // to look into from the browser's console (and for the tests)
  // What each kind of sound lets through, from the frequency (as the station's receive filters do; CW: the
  // signal on the frequency itself, heard as a 700 Hz note).
  const PASS = { USB: [200, 2900], LSB: [-2900, -200], AM: [-4500, 4500], CW: [-250, 250], CWR: [-250, 250] };
  const listenPass = (f, kind) => { const p = PASS[kind] || PASS.USB; return [f + p[0], f + p[1]]; };
  const usualKind = (hz) => (hz >= 10e6 || (hz >= 5.25e6 && hz <= 5.45e6) ? 'USB' : 'LSB');
  function kindOf(mode) {   // what the station plays for one of the radio's modes (null: not played, FM)
    const m = (mode || '').toUpperCase();
    if (['USB', 'PKTUSB', 'RTTYR'].includes(m)) return 'USB';
    if (['LSB', 'PKTLSB', 'RTTY'].includes(m)) return 'LSB';
    return ['CW', 'CWR', 'AM'].includes(m) ? m : null;
  }
  function listenWhere() {   // [the frequency heard, the kind of sound], or null
    if (A.follow) {
      if (!S.ok || S.dial === null) return null;
      const f = S.tx && S.split && S.split.on && S.split.tx_hz ? S.split.tx_hz : S.dial;
      return [f, (A.on && A.info.follow && A.info.kind) || kindOf(S.mode) || usualKind(f)];
    }
    return A.freq ? [A.freq, A.kind || usualKind(A.freq)] : null;
  }
  function setVolume(v) {
    A.vol = v;
    if (A.gain) A.gain.gain.value = Math.pow(v / 100, 2) * 2;
    try { localStorage.setItem('lsVol', String(v)); } catch (err) { /* fine without */ }
  }
  async function listenStart() {
    if (A.on) return;
    A.on = true;
    A.err = null;
    A.started = Date.now();
    listenShow();
    try {
      if (!A.ctx) {
        // At 12,000 samples a second the browser itself turns the sound into the speakers' rate, smoothly.
        try { A.ctx = new AudioContext({ sampleRate: 12000 }); } catch (err) { A.ctx = new AudioContext(); }
        await A.ctx.audioWorklet.addModule('/static/listen-worklet.js');
        A.node = new AudioWorkletNode(A.ctx, 'station-player',
          { numberOfInputs: 0, outputChannelCount: [1], processorOptions: { inRate: 12000 } });
        A.gain = A.ctx.createGain();
        A.node.connect(A.gain).connect(A.ctx.destination);
        A.node.port.onmessage = (e) => { A.stats = e.data; };
        setVolume(A.vol);
      }
      await A.ctx.resume();
    } catch (err) {
      A.on = false;
      A.err = `This browser can’t play the sound (${(err && err.message) || err}).`;
      listenShow();
      return;
    }
    listenStream();
  }
  function listenStop() {
    A.on = false;
    clearTimeout(A.retry);
    if (A.ctrl) A.ctrl.abort();
    A.ctrl = null;
    A.id = null;
    A.connecting = false;
    A.info = {};
    A.err = null;
    A.level = 0;
    if (A.node) A.node.port.postMessage({ flush: true });
    if (A.ctx) A.ctx.suspend();
    listenShow();
    drawSpectrum();
    drawOverlay();
  }
  function join(a, b) {
    if (!a.length) return b;
    const out = new Uint8Array(a.length + b.length);
    out.set(a);
    out.set(b, a.length);
    return out;
  }
  async function listenStream() {
    clearTimeout(A.retry);
    if (!A.on) return;
    const ctrl = new AbortController();
    A.ctrl = ctrl;
    A.id = null;
    A.connecting = true;
    listenShow();
    const q = new URLSearchParams();
    if (!A.follow && A.freq) { q.set('freq', String(A.freq)); q.set('mode', A.kind || usualKind(A.freq)); }
    if (A.fine) q.set('fine', String(A.fine));
    try {
      const res = await fetch('/api/listen?' + q.toString(), { headers: { 'X-Station': 'listen' }, cache: 'no-store', signal: ctrl.signal });
      if (!res.ok) {
        let why = `the station answered ${res.status}`;
        try { why = (await res.json()).error || why; } catch (err) { /* keep the number */ }
        throw Object.assign(new Error(why), { said: true });
      }
      const reader = res.body.getReader(), text = new TextDecoder();
      let buf = new Uint8Array(0);
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf = join(buf, value);
        let at = 0;
        while (buf.length - at >= 4) {
          const len = (buf[at + 1] << 16) | (buf[at + 2] << 8) | buf[at + 3];
          if (buf.length - at - 4 < len) break;
          const body = buf.subarray(at + 4, at + 4 + len);
          if (buf[at] === 65) listenSound(body);
          else if (buf[at] === 74) listenNews(JSON.parse(text.decode(body)));
          at += 4 + len;
        }
        if (at) buf = buf.slice(at);
      }
      throw new Error('the station stopped sending');
    } catch (err) {
      if (ctrl !== A.ctrl || !A.on) return;   // stopped, or started again meanwhile
      A.connecting = false;
      A.id = null;
      A.err = err.said ? `${err.message} Trying again…` : 'The sound stopped (the connection to the station dropped). Trying again…';
      listenShow();
      A.retry = setTimeout(listenStream, err.said ? 5000 : 2000);
    }
  }
  function listenNews(j) {
    if (j.id) {   // the first: the station is sending
      A.id = j.id;
      A.connecting = false;
      A.err = null;
    } else {
      A.info = j;
    }
    listenShow();
  }
  function listenSound(bytes) {
    const n = bytes.length >> 1, f = new Float32Array(n), dv = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
    let sum = 0;
    for (let i = 0; i < n; i++) {
      const v = dv.getInt16(i * 2, true) / 32768;
      f[i] = v;
      sum += v * v;
    }
    A.level = Math.sqrt(sum / Math.max(1, n));
    A.recent.push(f.slice());   // the last two seconds, for the level (and for looking into)
    if (A.recent.length > 20) A.recent.shift();
    if (A.node) A.node.port.postMessage(f, [f.buffer]);
    const db = A.level > 0 ? 20 * Math.log10(A.level) : -80;
    $('ls-meter').style.width = Math.max(0, Math.min(100, (db + 50) * 2.2)) + '%';
  }
  async function listenSet(change) {
    Object.assign(A, change);
    if (!A.follow && A.freq && !A.kind) A.kind = usualKind(A.freq);
    listenShow();
    drawSpectrum();
    drawOverlay();
    if (!A.on || !A.id) return;   // not listening, or still connecting (it starts with these)
    try {
      await post('/api/listen/tune', { id: A.id, follow: A.follow, freq_hz: A.follow ? null : A.freq,
                                       mode: A.follow ? null : A.kind, fine_hz: A.fine });
    } catch (err) {   // the station has forgotten this listener: start again, with these
      if (A.on) { if (A.ctrl) A.ctrl.abort(); listenStream(); }
    }
  }
  function listenHere(f, kind) {   // a click on the waterfall or a spot: listen there (it starts listening too)
    listenSet({ follow: false, freq: f, kind, fine: 0 });
    if (!A.on) listenStart();
  }
  function listenStatus() {
    const b = (text) => { const el = document.createElement('b'); el.textContent = text; return el; };
    const radio = theRadio(), i = A.info || {};
    if (A.err) return [A.err];
    if (!A.on) {
      if (!A.follow && A.freq) return ['Press Listen to hear ', b(`${fmtFreq(A.freq)} ${A.kind}`), ', or click another signal.'];
      return [`Press Listen to hear ${radio}, or click a signal on the waterfall to hear it.`];
    }
    if (A.connecting || !A.id) return ['Connecting to the station…'];
    if (i.note) return [i.note];
    if (!i.kind && !i.heard_hz) return ['Waiting for the sound…'];
    if (A.stats && !A.stats.playing && Date.now() - A.started > 2000) return ['Filling the buffer (the connection is slow)…'];
    const snr = typeof i.snr_db === 'number' ? ` · ${Math.round(i.snr_db)} dB over the noise` : '';
    if (i.follow) {
      if (!i.heard_hz) return [`Waiting for ${radio}’s frequency…`];
      return [`Hearing ${radio}${i.tx ? ' transmitting' : ''} on `, b(`${fmtFreq(i.heard_hz)} ${i.kind}`), snr];
    }
    if (i.tx) return [`${cap(radio)} is transmitting, so its own signal is all that comes through: pick Follow to hear it.`];
    return ['Hearing ', b(`${fmtFreq(i.heard_hz || A.freq)} ${i.kind || A.kind}`),
      S.ok && S.dial ? ` · ${radio} is on ${fmtFreq(S.dial)}` : '', snr];
  }
  function listenShow() {
    if (!LISTEN) return;
    $('ls-play').setAttribute('aria-pressed', String(A.on));
    $('ls-play-text').textContent = A.on ? 'Stop' : 'Listen';
    for (const btn of $('ls-what').querySelectorAll('button')) btn.setAttribute('aria-pressed', String((btn.dataset.follow === '1') === A.follow));
    const kind = A.follow ? ((A.on && A.info.kind) || kindOf(S.mode)) : A.kind;
    for (const btn of $('ls-kinds').querySelectorAll('button')) {
      btn.disabled = A.follow;   // following, the radio's own mode decides
      btn.setAttribute('aria-pressed', String(btn.dataset.kind === (kind === 'CWR' ? 'CW' : kind)));
    }
    $('ls-fine').textContent = `${A.fine > 0 ? '+' : A.fine < 0 ? '−' : ''}${Math.abs(A.fine)} Hz`;
    for (const el of document.querySelectorAll('.ls-radio')) el.textContent = S.radio || 'radio';
    const st = $('ls-status');
    st.classList.toggle('trouble', !!(A.err || (A.on && A.info && A.info.note)));
    st.replaceChildren(...listenStatus());
    if (!A.on) $('ls-meter').style.width = '0%';
  }
  function listenInit() {
    $('wf-listen').hidden = false;
    $('key-listen').hidden = false;
    $('key-listen-text').hidden = false;
    try {
      const saved = localStorage.getItem('lsVol');
      if (saved !== null && Number(saved) >= 0 && Number(saved) <= 100) A.vol = Number(saved);
    } catch (err) { /* the usual volume */ }
    $('ls-vol').value = String(A.vol);
    $('ls-vol').addEventListener('input', (e) => setVolume(Number(e.target.value)));
    $('ls-play').addEventListener('click', () => (A.on ? listenStop() : listenStart()));
    $('ls-what').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-follow]');
      if (!btn) return;
      if (btn.dataset.follow === '1') { listenSet({ follow: true, fine: 0 }); return; }
      if (!A.follow) return;
      const w = listenWhere();   // carry on with what's heard now, until a signal is clicked
      listenSet(w ? { follow: false, freq: w[0], kind: w[1] === 'CWR' ? 'CW' : w[1] } : { follow: false });
    });
    $('ls-kinds').addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-kind]');
      if (!btn || A.follow) return;
      listenSet({ kind: btn.dataset.kind, picked: btn.dataset.kind });
    });
    const nudge = (sign, e) => listenSet({ fine: Math.max(-1000, Math.min(1000, A.fine + sign * (e.shiftKey ? 50 : 10))) });
    $('ls-down').addEventListener('click', (e) => nudge(-1, e));
    $('ls-up').addEventListener('click', (e) => nudge(1, e));
    $('ls-fine').addEventListener('dblclick', () => listenSet({ fine: 0 }));
    listenShow();
  }

  // ---------- the pointer ----------
  over.addEventListener('pointermove', (e) => {
    const r = over.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const label = S.labels.find((l) => x >= l.x0 && x <= l.x1 && y >= l.y0 && y <= l.y1) || null;
    over.classList.toggle('on-label', !!label);
    S.hover = { x, y, label };
    if (label) showTip(label.spot, x, y); else hideTip();
    drawOverlay();
  });
  over.addEventListener('pointerleave', () => {
    S.hover = null;
    hideTip();
    drawOverlay();
  });
  over.addEventListener('click', (e) => {
    if (!(S.hi > S.lo)) return;
    const r = over.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const label = S.labels.find((l) => x >= l.x0 && x <= l.x1 && y >= l.y0 && y <= l.y1);
    if (LISTEN) {   // the listening page: a click picks what you hear; the radio stays where it is
      if (label) listenHere(Math.round(label.spot.freq_khz * 1000), sideband(label.spot));
      else if (y >= LABELS) {
        const f = Math.round(toHz(x) / 10) * 10;
        listenHere(f, A.picked || usualKind(f));
      }
      return;
    }
    if (label) tuneSpot(label.spot);
    else if (y >= LABELS) tuneTo(toHz(x));
  });

  // ---------- zoom ----------
  function showZoom() {
    for (const b of $('wf-zoom').querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.span === S.span));
  }
  $('wf-zoom').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-span]');
    if (!b) return;
    S.span = b.dataset.span;
    try { localStorage.setItem('wfSpan', S.span); } catch (err) { /* fine without */ }
    showZoom();
    const v = wantView();
    if (v) moveView(v[0], v[1]);
    drawSpectrum();
    drawOverlay();
  });

  // ---------- FT8: opens WSJT-X on this screen, brings it to the front, or puts it away ----------
  // Only on the station's own screen (not /listen, from another computer), and only once WSJT-X is installed
  // with FT8 turned on. The light on it shows WSJT-X is open.
  const FT8 = { shown: false, open: false, busy: false };
  function ft8Show() {
    const b = $('wf-ft8');
    b.hidden = !FT8.shown;
    b.disabled = FT8.busy;
    b.classList.toggle('open', FT8.open);
    b.title = FT8.open ? 'WSJT-X is open: brings it to the front, or puts it away' : 'Opens WSJT-X, for FT8, on this screen';
  }
  async function ft8Check() {
    try {
      const r = await (await fetch('/api/ft8', { cache: 'no-store' })).json();
      FT8.shown = !!(r.on && r.installed);
      FT8.open = !!r.wsjtx;
    } catch (err) { /* keep what it shows; the loop says if the app isn't answering */ }
    ft8Show();
  }
  async function ft8Press() {
    if (FT8.busy) return;
    FT8.busy = true;
    ft8Show();
    try {
      const r = await post('/api/ft8/show', {});
      if (!r.ok) toast(r.error || 'WSJT-X didn’t open.');
      else if (r.action === 'front') toast('Bringing WSJT-X to the front…');
      else if (r.action === 'away') toast('WSJT-X put away. It keeps listening, and the map keeps filling in. FT8 brings it back.');
      else toast('Opening WSJT-X… it takes a few seconds.');
    } catch (err) {
      toast('The station app isn’t answering, so WSJT-X didn’t open.');
    }
    setTimeout(() => { FT8.busy = false; ft8Check(); }, 3000);
  }

  // ---------- the loop: ten times a second ----------
  function apply(r, n) {
    S.on = r.on !== false;
    S.status = r.status || null;
    S.listeners = (r.status && r.status.listening) || 0;
    const rad = r.radio || {};
    S.ok = !!rad.ok;
    if (rad.name) S.radio = rad.name;
    if (rad.ok) {
      S.dial = rad.freq_hz;
      S.mode = rad.mode || S.mode;
      S.width = rad.width_hz || S.width || 2400;
      S.tx = !!rad.tx;
      S.split = rad.split || null;
    }
    if (S.on && S.hi > S.lo && r.lines) {
      const got = [];
      for (const line of r.lines) {
        const bytes = decode(line.data);
        if (bytes.length !== n) continue;
        levels(bytes, line.tx);
        average(bytes);
        got.push({ bytes, tx: line.tx, txHz: line.tx_hz, seq: line.seq });
      }
      if (S.refill) {
        drawBatch(got);
        S.rows = got.map((g) => ({ tx: g.tx, txHz: g.txHz, seq: g.seq })).reverse();
        S.refill = false;
        S.gaps = [];
        S.gen += 1;
      } else {
        for (const g of got) {
          drawLine(g.bytes);
          S.rows.unshift({ tx: g.tx, txHz: g.txHz, seq: g.seq });
        }
      }
      S.rows.length = Math.min(S.rows.length, Math.ceil((S.h - S.specH) / ROW) + 1);
      if (got.length) S.lastLine = Date.now();
    }
    if (typeof r.seq === 'number' && S.hi > S.lo) S.seq = r.seq;
  }
  async function tick() {
    let wait = 100;
    try {
      const v = wantView();
      if (v) moveView(v[0], v[1]);
      const have = S.hi > S.lo;
      const lo = have ? S.lo : 14000000, hi = have ? S.hi : 14350000;
      const n = Math.min(4096, falls.width);
      const since = have && !S.refill ? S.seq : 0, limit = S.refill ? 400 : 40;
      const res = await fetch(`/api/waterfall?since=${since}&lo=${lo}&hi=${hi}&n=${n}&limit=${limit}`, { cache: 'no-store' });
      apply(await res.json(), n);
      if (S.gaps.length && !S.refill) await fillGap(S.gaps.shift());
    } catch (err) {
      S.status = { problem: 'the station app isn’t answering' };
      wait = 2000;
    }
    header();
    footer();
    message();
    listenShow();
    drawSpectrum();
    drawOverlay();
    spotsMaybe();
    setTimeout(tick, wait);
  }

  async function start() {
    if (LISTEN) listenInit();   // first: the listening bar takes some of the height the picture is sized for
    resize();
    showZoom();
    try {
      const info = await (await fetch('/api/waterfall/info', { cache: 'no-store' })).json();
      S.bands = info.bands || [];
      S.voice = info.voice || [];
    } catch (err) { /* the loop will say so */ }
    let timer = null;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(resize, 200);
    });
    if (!LISTEN) {
      $('wf-ft8').addEventListener('click', ft8Press);
      ft8Check();
      setInterval(ft8Check, 10000);
    }
    tick();
  }
  start();
})();
