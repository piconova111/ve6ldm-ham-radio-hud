/* The welcome banner, shown the first time the station display opens after the computer
   starts, and whenever the callsign at the top left is clicked.

   Radio waves spread out from the station across the dotted world map (the same dots as the
   flat map), lighting the continents up as they pass, while paths reach out to far-away
   places. The callsign and the line from the settings ([display] welcome_text) come up in
   the middle. It lasts about 10 seconds; a click or a key skips it. With reduced motion
   asked for, it shows the finished picture for a few seconds instead. */
const Welcome = (() => {
  const RAD = Math.PI / 180, TAU = 2 * Math.PI;
  const REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const NIGHT = [7, 13, 24], AMBER = [255, 178, 74], VFD = [103, 209, 255], WHITE = [255, 255, 255];
  const MONO = '"B612 Mono", "DejaVu Sans Mono", monospace';
  const PLACES = [   // where the paths reach: [name, lat, lon]
    ['Japan', 35.7, 139.7], ['Australia', -33.9, 151.2], ['Germany', 52.5, 13.4], ['Finland', 60.2, 24.9],
    ['Brazil', -22.9, -43.2], ['South Africa', -33.9, 18.4], ['Hawaii', 21.3, -157.9], ['Argentina', -34.6, -58.4],
    ['Alaska', 61.2, -149.9], ['New Zealand', -41.3, 174.8],   // none under the words in the middle
  ];
  const WAVES = [0.6, 1.9, 3.2];        // seconds: when each wave leaves the station
  const TRAVEL = 6.0;                   // seconds for a wave to reach the far side of the world
  const FADE_AT = 9.3, END = 10.6;      // the banner fades out, then closes
  const REDUCED_HOLD = 6.0;
  const LEVELS = 24;                    // brightness steps for the dots
  const FRAME_MS = 31;                  // about 30 frames a second is plenty for this, and easy on the computer

  let worldPromise = null;
  let run = null;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  const mix = (a, b, t) => [0, 1, 2].map((i) => Math.round(a[i] + (b[i] - a[i]) * t));
  const rgba = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const vec = (lat, lon) => {
    const p = lat * RAD, l = lon * RAD, c = Math.cos(p);
    return [c * Math.cos(l), c * Math.sin(l), Math.sin(p)];
  };
  const angle = (a, b) => Math.acos(clamp(a[0] * b[0] + a[1] * b[1] + a[2] * b[2], -1, 1)) / RAD;

  // A wave's distance from the station, in degrees (0 to 180), or null before it leaves.
  function waveAngle(t, t0) {
    const u = (t - t0) / TRAVEL;
    if (u <= 0) return null;
    return u >= 1 ? 180 : 180 * (1 - Math.pow(1 - u, 1.6));
  }
  const arrival = (deg) => WAVES[0] + TRAVEL * (1 - Math.pow(1 - deg / 180, 1 / 1.6));

  function loadWorld() {
    if (!worldPromise) {
      worldPromise = fetch('/static/world.json', { cache: 'no-cache' })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('world.json ' + r.status))))
        .then((data) => {
          const f = data.flat;
          const colors = data.colors.map((h) => (h ? [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)) : [23, 37, 57]));
          const ym = (lat) => 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * RAD));
          const YT = ym(84), K = f.w / TAU;
          const land = [], sea = [];
          for (let r = 0, i = 0; r < f.rows; r++) {
            for (let col = 0; col < f.cols; col++, i++) {
              const x = f.x0 + col * f.step, y = f.y0 + r * f.step;
              const lat = (Math.atan(Math.exp((YT - y / K) / 1.25)) - Math.PI / 4) / 0.4 / RAD;
              const lon = -170 + x * 360 / f.w;
              const c = f.grid.charCodeAt(i) - 48;
              (c ? land : sea).push({ x, y, v: vec(lat, lon), c });
            }
          }
          return { f, colors, land, sea, ym, YT, K };
        })
        .catch(() => { worldPromise = null; return null; });   // the banner still shows, without the map
    }
    return worldPromise;
  }

  /* ---------- drawing ---------- */
  function layout() {
    const box = run.box;
    const W = box.clientWidth, H = box.clientHeight, dpr = window.devicePixelRatio || 1;
    for (const [canvas, ctx] of [[run.canvas, run.ctx], [run.landCanvas, run.land]]) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    Object.assign(run, { W, H, dpr });
    const w = run.world;
    if (!w) return;
    const s = Math.min(W / w.f.w, H / w.f.h);
    run.s = s;
    run.ox = (W - w.f.w * s) / 2;
    run.oy = (H - w.f.h * s) / 2;
    run.k = s / 1.627;   // 1 at 1920 px wide
    const home = run.home;
    // Each land dot: where it goes on the screen, and how far it is from the station.
    const n = w.land.length;
    run.sx = new Float32Array(n); run.sy = new Float32Array(n); run.ang = new Float32Array(n);
    run.col = new Uint8Array(n); run.key = new Uint16Array(n); run.order = new Uint32Array(n);
    run.shown = new Uint16Array(n).fill(0xFFFF);   // the colour each dot has on the screen now
    for (let i = 0; i < n; i++) {
      const d = w.land[i];
      run.sx[i] = run.ox + d.x * s; run.sy[i] = run.oy + d.y * s;
      run.ang[i] = angle(d.v, home.v); run.col[i] = d.c;
    }
    // The sea dots never change, so they're drawn once. (A land dot's square, cleared when it
    // changes colour, never reaches the sea dots beside it.)
    const sc = run.land;
    sc.clearRect(0, 0, W, H);
    sc.fillStyle = rgba(mix(NIGHT, w.colors[0], 0.85));
    sc.beginPath();
    const rs = 1.25 * s;
    for (const d of w.sea) {
      const x = run.ox + d.x * s, y = run.oy + d.y * s;
      sc.moveTo(x + rs, y);
      sc.arc(x, y, rs, 0, TAU);
    }
    sc.fill();
    // Colours for each continent at each brightness: dark, then its own colour, then a little whiter.
    run.palette = [];
    for (let c = 0; c < w.colors.length; c++) {
      for (let L = 0; L < LEVELS; L++) {
        const q = L / (LEVELS - 1);
        let col = mix(NIGHT, w.colors[c], Math.min(1, q / 0.8));
        if (q > 0.8) col = mix(col, WHITE, ((q - 0.8) / 0.2) * 0.38);
        run.palette[c * LEVELS + L] = rgba(col);
      }
    }
    run.counts = new Uint32Array(w.colors.length * LEVELS);
    run.starts = new Uint32Array(w.colors.length * LEVELS + 1);
    run.places = PLACES.map(([name, lat, lon]) => {
      const v = vec(lat, lon), deg = angle(home.v, v);
      return { name, lat, lon, v, deg, at: arrival(deg), x: SX(lon), y: SY(lat) };
    });
  }

  function SX(lon) { const w = run.world; return run.ox + ((((lon + 170) % 360) + 360) % 360) * w.f.w / 360 * run.s; }
  function SY(lat) { const w = run.world; return run.oy + w.K * (w.YT - w.ym(clamp(lat, -60, 84))) * run.s; }

  function slerpLatLon(a, b, om, t) {
    const so = Math.sin(om * RAD);
    if (so < 1e-6) return [Math.asin(clamp(a[2], -1, 1)) / RAD, Math.atan2(a[1], a[0]) / RAD];
    const s1 = Math.sin((1 - t) * om * RAD) / so, s2 = Math.sin(t * om * RAD) / so;
    const v = [s1 * a[0] + s2 * b[0], s1 * a[1] + s2 * b[1], s1 * a[2] + s2 * b[2]];
    return [Math.asin(clamp(v[2], -1, 1)) / RAD, Math.atan2(v[1], v[0]) / RAD];
  }

  // A line through points given as [lat, lon], broken where it wraps round the map's edge.
  function polyline(ctx, points) {
    let prev = null;
    ctx.beginPath();
    for (const [lat, lon] of points) {
      const x = SX(lon), y = SY(lat);
      if (prev === null || Math.abs(x - prev) > run.world.f.w * run.s / 2) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      prev = x;
    }
  }

  function wavefront(ctx, deg) {   // every place deg degrees from the station: a circle on the globe
    const d = deg * RAD, p1 = run.home.lat * RAD, l1 = run.home.lon * RAD;
    const pts = [];
    for (let b = 0; b <= 360; b += 2) {
      const th = b * RAD;
      const p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(th));
      const l2 = l1 + Math.atan2(Math.sin(th) * Math.sin(d) * Math.cos(p1), Math.cos(d) - Math.sin(p1) * Math.sin(p2));
      pts.push([p2 / RAD, l2 / RAD]);
    }
    const fade = Math.pow(1 - deg / 180, 0.7);
    polyline(ctx, pts);
    ctx.strokeStyle = rgba(VFD, 0.09 * fade);
    ctx.lineWidth = 7 * run.k;
    ctx.stroke();
    ctx.strokeStyle = rgba(VFD, 0.5 * fade);
    ctx.lineWidth = 1.8 * run.k;
    ctx.stroke();
  }

  function draw(t) {
    const { ctx, W, H } = run;
    ctx.clearRect(0, 0, W, H);
    const w = run.world;
    if (!w) return;
    const intro = smooth(0, 0.9, t);   // the map itself fades in with CSS
    const angles = WAVES.map((t0) => waveAngle(t, t0));
    // The land dots, brightest where a wave is passing; each wave leaves them a little brighter.
    // Only the dots whose colour changes are drawn again.
    const n = run.ang.length, nk = run.counts.length;
    const { counts, starts, key, order, col, ang, sx, sy, shown } = run;
    const land = run.land;
    const r = 2.6 * run.s, box = r + 1.5;
    counts.fill(0);
    let changed = 0;
    for (let i = 0; i < n; i++) {
      let passed = 0, boost = 0;
      for (const th of angles) {
        if (th === null) continue;
        if (th >= ang[i] + 2) passed++;
        if (th < 180) { const q = (ang[i] - th) / 7; boost += Math.exp(-q * q) * 0.85; }
      }
      const b = Math.min(1, 0.2 + 0.14 * passed + boost);
      const k = col[i] * LEVELS + Math.round(b * (LEVELS - 1));
      if (k === shown[i]) { key[i] = 0xFFFF; continue; }
      key[i] = k;
      shown[i] = k;
      counts[k]++;
      changed++;
      land.clearRect(sx[i] - box, sy[i] - box, 2 * box, 2 * box);
    }
    if (changed) {
      starts[0] = 0;
      for (let k = 0; k < nk; k++) starts[k + 1] = starts[k] + counts[k];
      const fillAt = starts.slice(0, nk);
      for (let i = 0; i < n; i++) if (key[i] !== 0xFFFF) order[fillAt[key[i]]++] = i;
      for (let k = 0; k < nk; k++) {
        if (!counts[k]) continue;
        land.fillStyle = run.palette[k];
        land.beginPath();
        for (let j = starts[k]; j < starts[k + 1]; j++) {
          const i = order[j];
          land.moveTo(sx[i] + r, sy[i]);
          land.arc(sx[i], sy[i], r, 0, TAU);
        }
        land.fill();
      }
    }
    // The wave fronts.
    for (const th of angles) if (th !== null && th > 1 && th < 179) wavefront(ctx, th);
    // Paths out to far-away places, reaching each one as the first wave does.
    const reach = angles[0];
    ctx.lineCap = 'round';
    for (const p of run.places) {
      if (reach === null) break;
      const frac = Math.min(1, reach / p.deg);
      const steps = Math.max(2, Math.ceil(72 * frac));
      const pts = [];
      for (let k = 0; k <= steps; k++) pts.push(slerpLatLon(run.home.v, p.v, p.deg, frac * k / steps));
      polyline(ctx, pts);
      ctx.strokeStyle = rgba(AMBER, 0.16);
      ctx.lineWidth = 6 * run.k;
      ctx.stroke();
      ctx.strokeStyle = rgba(AMBER, 0.8);
      ctx.lineWidth = 1.7 * run.k;
      ctx.stroke();
      if (frac < 1) {   // the tip on its way
        const [la, lo] = pts[pts.length - 1];
        ctx.fillStyle = rgba(WHITE, 0.95);
        ctx.beginPath();
        ctx.arc(SX(lo), SY(la), 2.6 * run.k, 0, TAU);
        ctx.fill();
        continue;
      }
      const since = t - p.at;
      if (since < 1.1) {   // a ping where it lands
        const q = since / 1.1;
        ctx.strokeStyle = rgba(AMBER, 0.85 * (1 - q));
        ctx.lineWidth = 2 * run.k;
        ctx.beginPath();
        ctx.arc(p.x, p.y, (4 + 26 * q) * run.k, 0, TAU);
        ctx.stroke();
      }
      ctx.fillStyle = rgba(AMBER, 1);
      ctx.beginPath();
      ctx.arc(p.x, p.y, 3.4 * run.k, 0, TAU);
      ctx.fill();
      const a = smooth(0, 0.6, since);
      ctx.font = `700 ${Math.round(14 * run.k)}px ${MONO}`;
      ctx.textBaseline = 'middle';
      const right = p.x < W - 160 * run.k;
      ctx.textAlign = right ? 'left' : 'right';
      ctx.fillStyle = rgba([198, 211, 227], 0.9 * a);
      ctx.fillText(p.name, p.x + (right ? 10 : -10) * run.k, p.y);
    }
    // The station, pulsing.
    const hx = SX(run.home.lon), hy = SY(run.home.lat);
    for (let i = 0; i < 3; i++) {
      const q = ((t - 0.3 - i * 0.4) % 1.2) / 1.2;
      if (t < 0.3 + i * 0.4) continue;
      ctx.strokeStyle = rgba(AMBER, 0.7 * (1 - q));
      ctx.lineWidth = 2 * run.k;
      ctx.beginPath();
      ctx.arc(hx, hy, (7 + 30 * q) * run.k, 0, TAU);
      ctx.stroke();
    }
    const glow = ctx.createRadialGradient(hx, hy, 0, hx, hy, 26 * run.k);
    glow.addColorStop(0, rgba(AMBER, 0.45));
    glow.addColorStop(1, rgba(AMBER, 0));
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(hx, hy, 26 * run.k, 0, TAU);
    ctx.fill();
    ctx.fillStyle = rgba(AMBER, intro);
    ctx.beginPath();
    ctx.arc(hx, hy, 5.5 * run.k, 0, TAU);
    ctx.fill();
  }

  /* ---------- showing and closing ---------- */
  function frame(now) {
    if (!run) return;
    run.raf = requestAnimationFrame(frame);
    if (now - (run.drawn || 0) < FRAME_MS) return;
    run.drawn = now;
    const t = (now - run.start) / 1000;
    draw(t);
    if (t >= FADE_AT && !run.leaving) leave(false);
    if (t >= END) close();
  }

  function leave(quick) {
    if (!run || run.leaving) return;
    run.leaving = true;
    run.box.classList.toggle('quick', quick);
    run.box.classList.add('leaving');
    clearTimeout(run.timer);
    run.timer = setTimeout(close, quick ? 450 : 1250);
  }

  function close() {
    if (!run) return;
    cancelAnimationFrame(run.raf);
    clearTimeout(run.timer);
    window.removeEventListener('keydown', run.onKey, true);
    window.removeEventListener('resize', run.onResize);
    run.box.hidden = true;
    run.box.classList.remove('on', 'play', 'leaving', 'quick');
    run = null;
  }

  async function play(cfg) {
    if (run) return;
    const box = document.getElementById('welcome');
    if (!box || !cfg) return;
    const call = document.getElementById('welcome-call');
    call.replaceChildren(...[...String(cfg.callsign || '')].map((ch, i) => {
      const span = document.createElement('span');
      span.textContent = ch;
      span.style.setProperty('--i', i);
      return span;
    }));
    call.setAttribute('aria-label', cfg.callsign || '');
    document.getElementById('welcome-line').textContent = cfg.welcome_text || '';
    const foot = document.getElementById('welcome-foot');
    foot.textContent = cfg.note || '';
    foot.hidden = !cfg.note;
    const canvas = document.getElementById('welcome-canvas');
    const landCanvas = document.getElementById('welcome-land');
    run = { box, canvas, ctx: canvas.getContext('2d'), landCanvas, land: landCanvas.getContext('2d'),
      world: null, leaving: false,
      home: { lat: cfg.lat, lon: cfg.lon, v: vec(cfg.lat, cfg.lon) } };
    const mine = run;
    run.onKey = (e) => {   // any key skips it, and doesn't reach the rest of the display
      e.preventDefault();
      e.stopImmediatePropagation();
      leave(true);
    };
    run.onResize = () => { if (run && run.world) layout(); };
    window.addEventListener('keydown', run.onKey, true);
    window.addEventListener('resize', run.onResize);
    box.onclick = () => leave(true);
    box.hidden = false;
    void box.offsetWidth;   // start the fade-in from transparent
    box.classList.add('on');
    run.world = await loadWorld();
    if (run !== mine) return;   // skipped while the map was loading
    if (run.world) layout(); else { run.W = box.clientWidth; run.H = box.clientHeight; }
    box.classList.add('play');
    if (REDUCED) {
      draw(FADE_AT);
      run.timer = setTimeout(() => leave(false), REDUCED_HOLD * 1000);
      return;
    }
    run.start = performance.now();
    run.raf = requestAnimationFrame(frame);
  }

  return { play, showing: () => !!run };
})();
