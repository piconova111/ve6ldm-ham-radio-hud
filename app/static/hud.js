'use strict';
/* The station display (the ham radio station HUD). Talks only to the station app on this computer. */

const $ = (id) => document.getElementById(id);

const api = {
  async get(path) {
    const res = await fetch(path, { cache: 'no-store' });
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  },
  async post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  },
};

const state = {
  cfg: null, version: null, boot: null, card: null, lookupSeq: 0, needle: 0, undoId: null, toastTimer: null,
  band: '20m', bandChosen: false, voice: true, tab: 'spots', heardTop: null,
  iss: null, issOpen: false, issAlerted: new Set(), favLoaded: false,
  sats: null, satId: null, satDetail: null, satTimer: null,   // the satellite card
  openings: null, openBand: null, openTimer: null, heard: [], radio: null, listen: null,
  mapView: 'flat',   // the map picked; a look-up shows the globe for a while if the flat map is showing
};
const VIEWS_MAP = ['flat', 'globe', 'beam'];
// The HF bands the radios have antennas for (from the settings): the spot list's buttons, band openings, listening.
let BANDS = ['80m', '40m', '30m', '20m', '17m', '15m', '12m', '10m'];

/* ---------- Map projection (must match map.svg: Miller, left edge 170° W, 84° N to 60° S) ---------- */
const MAP_W = 1180, MAP_H = 601, LON0 = -170;
const RAD = Math.PI / 180;
const ym = (lat) => 1.25 * Math.log(Math.tan(Math.PI / 4 + 0.4 * lat * RAD));
const YT = ym(84), K = MAP_W / (2 * Math.PI);
const px = (lon) => ((((lon - LON0) % 360) + 360) % 360) * MAP_W / 360;
const py = (lat) => K * (YT - ym(Math.max(-60, Math.min(84, lat))));
const f1 = (n) => n.toFixed(1);

/* ---------- Time ---------- */
function timeIn(tz, opts, locale = 'en-CA', when = new Date()) {
  return new Intl.DateTimeFormat(locale, { timeZone: tz, ...opts }).format(when);
}
function tzAbbr(tz, when) {
  const part = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'short' })
    .formatToParts(when).find((p) => p.type === 'timeZoneName');
  return part ? part.value : '';
}
function localTz() { return (state.cfg && state.cfg.timezone) || Intl.DateTimeFormat().resolvedOptions().timeZone; }

function tickClocks() {
  const now = new Date();
  const tz = localTz();
  $('utc-time').textContent = timeIn('UTC', { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }, 'en-GB', now);
  $('utc-date').textContent = 'UTC, ' + timeIn('UTC', { month: 'short', day: 'numeric' }, 'en-US', now);
  $('local-time').textContent = timeIn(tz, { hour: 'numeric', minute: '2-digit' }, 'en-CA', now);
  $('local-label').textContent = `${(state.cfg && state.cfg.place) || 'Local'}, ${tzAbbr(tz, now)}`;
  const hour = Number(timeIn(tz, { hour: '2-digit', hourCycle: 'h23' }, 'en-GB', now));
  const hello = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
  $('greeting').textContent = state.cfg && state.cfg.operator ? `${hello}, ${state.cfg.operator}` : hello;
  showSpecialDay(now);
  const card = state.card;
  if (card && card.timezone) {
    $('c-time').textContent = timeIn(card.timezone, { hour: 'numeric', minute: '2-digit' }, 'en-CA', now);
    $('c-time').classList.toggle('long', $('c-time').textContent.length >= 10);
    $('c-day').textContent = timeIn(card.timezone, { weekday: 'long' }, 'en-CA', now);
  }
}

/* ---------- Gray line and sun ---------- */
function sunPosition(when) {
  const d = when.getTime() / 86400000 + 2440587.5 - 2451545.0;   // days since J2000
  const g = (357.529 + 0.98560028 * d) * RAD;
  const L = (280.459 + 0.98564736 * d + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * RAD;
  const e = (23.439 - 0.00000036 * d) * RAD;
  const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / RAD;
  const lat = Math.asin(Math.sin(e) * Math.sin(L)) / RAD;
  const gmst = (((18.697374558 + 24.06570982441908 * d) % 24) + 24) % 24;
  const lon = ((((ra - gmst * 15 + 180) % 360) + 360) % 360) - 180;
  return { lat, lon };
}

function drawGrayLine() {
  const sun = sunPosition(new Date());
  const dec = Math.abs(sun.lat) < 0.05 ? (sun.lat < 0 ? -0.05 : 0.05) : sun.lat;
  const pts = [];
  let stroke = '', prev = null, prevIn = false;
  for (let lon = LON0; lon <= LON0 + 360; lon += 2) {
    const lat = Math.atan(-Math.cos((lon - sun.lon) * RAD) / Math.tan(dec * RAD)) / RAD;
    const pt = `${f1((lon - LON0) * MAP_W / 360)} ${f1(py(lat))}`;
    pts.push(pt);
    // Draw the gold line only where it crosses the map, not where it runs along the edges.
    const inside = lat > -60 && lat < 84;
    if (inside) stroke += prevIn ? ` L${pt}` : prev ? ` M${prev} L${pt}` : ` M${pt}`;
    else if (prevIn) stroke += ` L${pt}`;
    prev = pt;
    prevIn = inside;
  }
  const line = 'M' + pts.join(' L');
  $('terminator').setAttribute('d', stroke.trim());
  $('terminator-glow').setAttribute('d', stroke.trim());
  $('night').setAttribute('d', line + (dec < 0 ? ` L${MAP_W} 0 L0 0 Z` : ` L${MAP_W} ${MAP_H} L0 ${MAP_H} Z`));
  $('sun').setAttribute('transform', `translate(${f1(px(sun.lon))} ${f1(py(sun.lat))})`);
}

/* ---------- Station and paths on the map ---------- */
function toVec(lat, lon) {
  return [Math.cos(lat * RAD) * Math.cos(lon * RAD), Math.cos(lat * RAD) * Math.sin(lon * RAD), Math.sin(lat * RAD)];
}

function greatCircle(lat1, lon1, lat2, lon2) {
  const a = toVec(lat1, lon1), b = toVec(lat2, lon2);
  const om = Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
  if (Math.sin(om) < 1e-6) return '';
  let d = '', prev = null;
  for (let i = 0; i <= 96; i++) {
    const t = i / 96;
    const s1 = Math.sin((1 - t) * om) / Math.sin(om), s2 = Math.sin(t * om) / Math.sin(om);
    const v = [s1 * a[0] + s2 * b[0], s1 * a[1] + s2 * b[1], s1 * a[2] + s2 * b[2]];
    const x = px(Math.atan2(v[1], v[0]) / RAD);
    const y = py(Math.asin(Math.max(-1, Math.min(1, v[2]))) / RAD);
    d += `${prev === null || Math.abs(x - prev) > MAP_W / 2 ? 'M' : 'L'}${f1(x)} ${f1(y)} `;
    prev = x;
  }
  return d.trim();
}

function drawStation() {
  const cfg = state.cfg;
  const x = px(cfg.lon), y = py(cfg.lat);
  for (const id of ['qth-ring', 'qth-dot', 'qth-pulse', 'qth-glow']) {
    $(id).setAttribute('cx', f1(x));
    $(id).setAttribute('cy', f1(y));
  }
  $('qth-label').setAttribute('x', f1(x + 16));
  $('qth-label').setAttribute('y', f1(y - 14));
  $('qth-label').textContent = cfg.callsign;
  $('zones').style.display = cfg.show_zones ? '' : 'none';
  $('zones-pill').hidden = !cfg.show_zones;
  $('loc-warn').hidden = cfg.location_set;
}

/* ---------- Map views: flat map, globe, beam map (drawn by maps.js) ---------- */
function showMapView(view) {
  if (!Maps.available()) return;
  Maps.setView(view);
  for (const b of $('views').querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.view === view));
  $('legend-sun').hidden = view === 'beam';
}

function pickMapView(view) {
  state.mapView = view;
  try { localStorage.setItem('mapView', view); } catch (err) { /* only a convenience */ }
  showMapView(view);
  if (view === 'globe' && !state.card) Maps.focusHome();
}

// Places for the list of beam headings beside the beam map.
const BEAM_PLACES = [
  { name: 'Alaska', lat: 61.22, lon: -149.9 }, { name: 'Hawaii', lat: 21.31, lon: -157.86 },
  { name: 'New Zealand', lat: -36.85, lon: 174.76 }, { name: 'Australia', lat: -33.87, lon: 151.21 },
  { name: 'Japan', lat: 35.68, lon: 139.69 }, { name: 'China', lat: 39.9, lon: 116.4 },
  { name: 'India', lat: 28.61, lon: 77.21 }, { name: 'Moscow', lat: 55.76, lon: 37.62 },
  { name: 'Germany', lat: 52.52, lon: 13.4 }, { name: 'Italy', lat: 41.9, lon: 12.5 },
  { name: 'England', lat: 51.51, lon: -0.13 }, { name: 'South Africa', lat: -26.2, lon: 28.05 },
  { name: 'Brazil', lat: -23.55, lon: -46.63 }, { name: 'Argentina', lat: -34.6, lon: -58.38 },
  { name: 'Caribbean', lat: 18.47, lon: -66.11 },
];

function renderBeamList() {
  $('beam-list-title').textContent = `Beam headings from ${state.cfg.callsign}`;
  const rows = BEAM_PLACES.map((p) => ({ ...p, ...Maps.pathTo(p.lat, p.lon) })).sort((a, b) => a.bearing - b.bearing);
  const list = $('beam-list');
  list.replaceChildren();
  for (const p of rows) {
    const item = document.createElement('li');
    item.dataset.bearing = String(p.bearing);
    item.append(cell('bl-deg', `${Math.round(p.bearing)}°`), cell('bl-name', p.name), cell('bl-km', `${fmtNum(Math.round(p.km / 100) * 100)} km`));
    item.addEventListener('pointerenter', () => Maps.setHint(p));
    item.addEventListener('pointerleave', () => Maps.setHint(null));
    list.append(item);
  }
  markBeamList(state.card ? state.card.bearing : null);
}

// Places inside the beam (within 30 degrees of the heading to the station looked up) light up.
function markBeamList(bearing) {
  for (const item of $('beam-list').children) {
    const off = bearing === null ? 999 : Math.abs(((Number(item.dataset.bearing) - bearing) % 360 + 540) % 360 - 180);
    item.classList.toggle('in-beam', off <= 30);
  }
}

async function startMaps() {
  const cfg = state.cfg;
  const ok = await Maps.init({ lat: cfg.lat, lon: cfg.lon, call: cfg.callsign });
  if (!ok) return;   // the land data didn't load: the original flat map stays
  renderBeamList();
  let saved = 'flat';
  try { saved = localStorage.getItem('mapView') || 'flat'; } catch (err) { /* only a convenience */ }
  state.mapView = VIEWS_MAP.includes(saved) ? saved : 'flat';
  $('views').hidden = false;
  showMapView(state.card && state.mapView === 'flat' ? 'globe' : state.mapView);
}

function showOnMap(card) {
  const marks = ['sel-dot', 'sel-ring', 'sel-label'];
  if (!card || !state.cfg) {
    $('gc-path').setAttribute('d', '');
    $('gc-glow').setAttribute('d', '');
    for (const id of marks) $(id).setAttribute('opacity', '0');
    $('legend-path').textContent = 'Path to a station';
    Maps.setTarget(null);
    markBeamList(null);
    if (Maps.available() && Maps.view() !== state.mapView) showMapView(state.mapView);
    return;
  }
  markBeamList(card.bearing);
  Maps.setTarget({ lat: card.lat, lon: card.lon, call: card.call, bearing: card.bearing,
    long_path: card.long_path, distance_km: card.distance_km });
  if (Maps.available() && state.cfg.globe_on_lookup !== false && state.mapView === 'flat' && Maps.view() === 'flat') showMapView('globe');
  const x = px(card.lon), y = py(card.lat);
  $('gc-path').setAttribute('d', greatCircle(state.cfg.lat, state.cfg.lon, card.lat, card.lon));
  $('gc-glow').setAttribute('d', $('gc-path').getAttribute('d'));
  for (const id of ['sel-dot', 'sel-ring']) {
    $(id).setAttribute('cx', f1(x));
    $(id).setAttribute('cy', f1(y));
  }
  const label = $('sel-label');
  const right = x > 1080;
  label.textContent = card.call;
  label.setAttribute('x', f1(right ? x - 14 : x + 14));
  label.setAttribute('y', f1(y + 24));
  label.setAttribute('text-anchor', right ? 'end' : 'start');
  for (const id of marks) $(id).setAttribute('opacity', '1');
  $('legend-path').textContent = `Path to ${card.call}`;
}

function clearCard() {
  $('search').value = '';
  state.card = null;
  showView('card-empty');
  showOnMap(null);
  if (Maps.available() && state.mapView === 'globe') Maps.focusHome();
  $('search').focus();
}

/* ---------- Callsign card ---------- */
const VIEWS = ['card-empty', 'card-loading', 'card-found', 'card-self', 'card-missing', 'card-iss'];
function showView(id) {
  for (const v of VIEWS) $(v).hidden = v !== id;
  $('card-close').hidden = id === 'card-empty' || id === 'card-loading';
  if (id !== 'card-iss' && (state.issOpen || state.satId)) {
    state.issOpen = false;
    $('iss-ring').setAttribute('opacity', '0');
    drawIss(state.iss);
    closeSat();
  }
}

const fmtNum = (n) => Math.round(n).toLocaleString('en-CA');
const bandLabel = (b) => (b ? b.replace(/(\d)(c?m)$/, '$1 $2') : '');

function historyText(card) {
  const h = card.history || { count: 0 };
  if (!h.count || !h.last) return `No contacts with ${card.call} in your log yet.`;
  const date = timeIn(localTz(), { month: 'short', day: 'numeric', year: 'numeric' }, 'en-CA', new Date(h.last.time_on));
  const what = [bandLabel(h.last.band), h.last.mode].filter(Boolean).join(' ');
  const times = h.count === 1 ? 'once' : `${h.count} times`;
  return `In your log ${times}. Last on ${date}${what ? ', ' + what : ''}.`;
}

function turnNeedle(id, key, bearing) {
  const delta = ((((bearing - state[key]) % 360) + 540) % 360) - 180;   // turn the short way round
  state[key] += delta;
  $(id).style.transform = `rotate(${state[key]}deg)`;
}
function pointNeedle(bearing) { turnNeedle('c-needle', 'needle', bearing); }

function defaultLogBand(spot) {
  if (state.radioOk && state.radioBand) return state.radioBand;
  if (spot && spot.band) return spot.band;
  try { return localStorage.getItem('logBand') || '20m'; } catch (err) { return '20m'; }
}

function setLogBand(band) {
  const select = $('log-band');
  if ([...select.options].some((o) => o.value === band)) select.value = band;
}

// The log's band list: the bands the antennas cover, plus 2 m and 70 cm (satellites and FM).
function buildLogBands() {
  const select = $('log-band');
  const keep = select.value;
  const bands = BANDS.concat(['2m', '70cm'].filter((b) => !BANDS.includes(b)));
  select.replaceChildren(...bands.map((b) => Object.assign(document.createElement('option'), { value: b, textContent: bandLabel(b) })));
  if (bands.includes(keep)) select.value = keep;
}

function renderFound(card, spot) {
  state.card = card;
  card.spot = spot || null;
  state.logBandTouched = false;
  setLogBand(defaultLogBand(card.spot));
  const tune = $('btn-tune');
  tune.hidden = !spot || !state.radioOk;
  if (spot) tune.textContent = `Tune to ${spot.freq_khz.toFixed(1).replace(/\.0$/, '')}`;
  $('c-call').textContent = card.call;
  const where = [card.qth, card.region, card.country].filter((s, i, all) => s && all.indexOf(s) === i);
  $('c-country').textContent = where.join(', ');
  $('c-name').textContent = card.name || '';
  $('c-name').hidden = !card.name;
  const meta = $('c-meta');
  meta.replaceChildren();
  const parts = [];
  if (card.grid) parts.push(`Grid ${card.grid}`);
  if (card.cq) parts.push(zoneLink(card.cq, `CQ ${card.cq}`));
  if (card.itu) parts.push(`ITU ${card.itu}`);
  parts.forEach((part, i) => meta.append(i ? ', ' : '', part));
  renderZoneLine(card);
  pointNeedle(card.bearing);
  $('c-beam').textContent = `${Math.round(card.bearing)}°`;
  $('c-long').textContent = `Long path ${Math.round(card.long_path)}°`;
  $('c-dist').textContent = `${fmtNum(card.distance_km)} km`;
  $('c-dist').classList.toggle('long', card.distance_km >= 10000);   // five digits don't fit at full size
  $('c-miles').textContent = `${fmtNum(card.distance_km * 0.621371)} miles`;
  if (!card.timezone) {
    $('c-time').textContent = '—';
    $('c-day').textContent = '';
  }
  $('c-history').textContent = historyText(card);
  renderLookups('c-links', card.call, card.lookups);
  const note = $('c-note');
  note.hidden = card.precise;
  note.textContent = card.precise ? '' : `Pointing at the middle of ${card.region || card.country}.`;
  showView('card-found');
  showOnMap(card);
  tickClocks();
}

function renderSelf(card) {
  state.card = null;
  $('s-call').textContent = card.call;
  $('s-name').textContent = card.full_name || (state.cfg && state.cfg.operator) || '';
  const where = card.grid ? `grid ${card.grid}` : 'grid square not set yet';
  $('s-place').textContent = card.place ? `${card.place}, ${where}` : where.charAt(0).toUpperCase() + where.slice(1);
  if (card.cq) $('s-zones').replaceChildren(zoneLink(card.cq, `CQ zone ${card.cq}`), card.itu ? `, ITU zone ${card.itu}` : '');
  else $('s-zones').replaceChildren();
  $('s-licence').textContent = card.licence || '';
  $('s-licence').hidden = !card.licence;
  $('s-note').textContent = card.note || '';
  $('s-note').hidden = !card.note;
  renderLookups('s-links', card.call, card.lookups);
  showView('card-self');
  showOnMap(null);
  Maps.focusHome();
}

function renderMissing(call, message) {
  state.card = null;
  $('m-call').textContent = call;
  $('m-text').textContent = message;
  showView('card-missing');
  showOnMap(null);
}

async function lookUp(raw, spot) {
  const call = (raw || '').trim().toUpperCase();
  if (!call) return;
  const seq = ++state.lookupSeq;
  $('loading-call').textContent = call;
  showView('card-loading');
  try {
    const card = await api.get('/api/lookup?call=' + encodeURIComponent(call));
    if (seq !== state.lookupSeq) return;   // a newer look-up has started
    if (!card.found) renderMissing(card.call || call, card.message);
    else if (card.self) renderSelf(card);
    else renderFound(card, spot);
  } catch (err) {
    if (seq !== state.lookupSeq) return;
    renderMissing(call, 'The station app isn’t answering right now. It restarts on its own, so try again in a moment.');
  }
}

/* ---------- Logging ---------- */
function showToast(text, undoId, alert = false) {
  $('toast').classList.toggle('alert', alert === true);
  $('toast').classList.toggle('celebrate', alert === 'celebrate');
  $('toast-text').textContent = text;
  $('toast-undo').hidden = !undoId;
  state.undoId = undoId || null;
  $('toast').hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $('toast').hidden = true; state.undoId = null; }, alert ? 25000 : 12000);
}

async function logContact() {
  const card = state.card;
  if (!card) return;
  const button = $('btn-log');
  button.disabled = true;
  try {
    const q = await api.post('/api/log', {
      call: card.call, name: card.name, qth: card.qth, grid: card.grid, distance_km: card.distance_km,
      country: card.country, cq: card.cq, itu: card.itu,
      spot_freq_hz: card.spot ? Math.round(card.spot.freq_khz * 1000) : null,
      mode: card.spot ? (card.spot.freq_khz < 10000 ? 'LSB' : 'USB') : null,
      band: $('log-band').value,
    });
    try { localStorage.setItem('logBand', $('log-band').value); } catch (err) { /* only a convenience */ }
    const at = timeIn(localTz(), { hour: 'numeric', minute: '2-digit' }, 'en-CA', new Date(q.time_on));
    const what = [bandLabel(q.band), q.mode].filter(Boolean).join(' ');
    const party = celebration(q);
    if (party) showToast(party, q.id, 'celebrate');
    else showToast(what ? `Logged ${q.call} at ${at} on ${what}.` : `Logged ${q.call} at ${at}`, q.id);
    card.history = { count: ((card.history && card.history.count) || 0) + 1, last: q };
    $('c-history').textContent = historyText(card);
    refreshLog();
  } catch (err) {
    showToast('That contact didn’t save. Try again in a moment.');
  } finally {
    button.disabled = false;
  }
}

async function undoLog() {
  const id = state.undoId;
  if (!id) return;
  try {
    await api.post('/api/log/delete', { id });
  } catch (err) {
    showToast('Couldn’t undo. The contact is still in the log.');
    return;
  }
  $('toast').hidden = true;
  state.undoId = null;
  refreshLog();
  if (state.card) lookUp(state.card.call);
}

function cell(className, text) {
  const span = document.createElement('span');
  span.className = className;
  span.textContent = text;
  return span;
}

function renderRecent(rows) {
  const list = $('recent-list');
  list.replaceChildren();
  $('recent-empty').hidden = rows.length > 0;
  list.hidden = rows.length === 0;
  const today = timeIn(localTz(), { year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA');
  for (const q of rows) {
    const when = new Date(q.time_on);
    const day = timeIn(localTz(), { year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA', when);
    const label = day === today
      ? timeIn(localTz(), { hour: 'numeric', minute: '2-digit' }, 'en-CA', when)
      : timeIn(localTz(), { month: 'short', day: 'numeric' }, 'en-CA', when);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'row qso';
    button.append(
      cell('r-when', label),
      cell('r-call', q.call),
      cell('r-what', [bandLabel(q.band), q.mode].filter(Boolean).join(' ')),
      cell('r-country', q.country || ''),
    );
    button.addEventListener('click', () => lookUp(q.call));
    const item = document.createElement('li');
    item.append(button);
    list.append(item);
  }
}

async function refreshLog() {
  try {
    const [recent, stats] = await Promise.all([api.get('/api/log/recent?limit=20'), api.get('/api/log/stats')]);
    renderRecent(recent);
    $('log-stats').textContent = `${plural(stats.total, 'contact', 'contacts')}, ${plural(stats.countries, 'country', 'countries')}, ${plural(stats.zones || 0, 'zone', 'zones')}, ${fmtNum(stats.this_month)} this month`;
  } catch (err) { /* keep showing what we have */ }
  refreshZones();
}

/* ---------- CQ zones: what the red numbers on the map mean, and which are in the log ---------- */
const lonOf = (x) => LON0 + x * 360 / MAP_W;
const latOf = (y) => (Math.atan(Math.exp((YT - y / K) / 1.25)) - Math.PI / 4) / 0.4 / RAD;

async function loadZones() {
  if (state.zoneInfo) return true;
  try {
    const data = await api.get('/static/zones.json');
    state.zoneInfo = new Map(data.zones.map((z) => [z.zone, z]));
    return true;
  } catch (err) { return false; }
}
const zoneWorked = (n) => !!(state.zonesLog && state.zonesLog[String(n)]);

async function refreshZones() {
  try { state.zonesLog = (await api.get('/api/log/zones')).zones || {}; } catch (err) { return; }
  markZones();
  if (!$('stats-dlg').hidden && state.statsTab === 'zones') renderZones(state.zonePicked);
  if (state.card) renderZoneLine(state.card);
}

// The numbers on the flat map become buttons; the globe and beam map get the same numbers.
function setupZoneLabels() {
  const NS = 'http://www.w3.org/2000/svg';
  state.zoneSpots = [];
  for (const text of [...$('zones').querySelectorAll('text')]) {
    const n = Number(text.textContent), x = Number(text.getAttribute('x')), y = Number(text.getAttribute('y'));
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'zone-label');
    g.setAttribute('data-zone', String(n));
    const c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', String(x));
    c.setAttribute('cy', String(y - 4.5));
    c.setAttribute('r', '11');
    text.replaceWith(g);
    g.append(c, text);
    const title = document.createElementNS(NS, 'title');
    title.textContent = `CQ zone ${n}`;
    g.append(title);
    g.addEventListener('click', () => openZones(n));
    state.zoneSpots.push({ zone: n, lat: latOf(y - 4.5), lon: lonOf(x) });
  }
  Maps.onZone(openZones);
}

function markZones() {
  for (const g of $('zones').querySelectorAll('.zone-label')) g.classList.toggle('worked', zoneWorked(g.getAttribute('data-zone')));
  if (state.zoneSpots) Maps.setZones(state.zoneSpots.map((z) => ({ ...z, worked: zoneWorked(z.zone) })), !!(state.cfg && state.cfg.show_zones));
}

function zoneLink(n, text) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'zone-link';
  b.textContent = text;
  b.title = `What is CQ zone ${n}?`;
  b.addEventListener('click', () => openZones(n));
  return b;
}

function renderZoneLine(card) {
  const line = $('c-zone');
  const z = state.zoneInfo && card.cq ? state.zoneInfo.get(card.cq) : null;
  line.hidden = !z || !state.zonesLog || zoneWorked(card.cq);
  if (!line.hidden) line.textContent = `New zone for your log: ${card.cq}, ${z.short}.`;
}

// The red numbers, the zone links and the CQ zones pill open the stats at the CQ zones tab.
function openZones(n) { openStats('zones', n); }

async function showZonesTab(n) {
  if (!(await loadZones())) { $('zone-detail').textContent = 'The zone list didn’t load. Try again in a moment.'; return; }
  renderZones(n || state.zonePicked || (state.cfg && state.cfg.cq_zone) || 1);
  refreshZones();
}

function renderZones(picked) {
  state.zonePicked = picked;
  const home = (state.cfg && state.cfg.cq_zone) || null;
  $('zones-home').textContent = home ? String(home) : '?';
  const done = Object.keys(state.zonesLog || {}).length;
  $('zones-done').textContent = String(done);
  $('zones-bar').style.width = `${(done / 40) * 100}%`;
  const grid = $('zones-grid');
  if (!grid.children.length) {
    for (const z of state.zoneInfo.values()) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'zone-tile';
      b.dataset.zone = String(z.zone);
      const mark = cell('zt-mark', '');
      mark.innerHTML = '<svg viewBox="0 0 16 16" aria-label="in your log"><path d="M3 8.5 L6.5 12 L13 4.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
      b.append(cell('zt-num', String(z.zone)), cell('zt-name', z.short), mark);
      b.addEventListener('click', () => renderZones(z.zone));
      const item = document.createElement('li');
      item.append(b);
      grid.append(item);
    }
  }
  for (const b of grid.querySelectorAll('.zone-tile')) {
    const n = Number(b.dataset.zone);
    b.classList.toggle('worked', zoneWorked(n));
    b.classList.toggle('home', n === home);
    b.setAttribute('aria-pressed', String(n === picked));
    b.querySelector('.zt-mark').hidden = !zoneWorked(n);
    b.title = n === home ? 'Your own zone' : (zoneWorked(n) ? 'In your log' : '');
  }
  renderZoneDetail(picked, home);
}

function renderZoneDetail(n, home) {
  const z = state.zoneInfo.get(n);
  const box = $('zone-detail');
  box.replaceChildren();
  if (!z) return;
  const para = (className, text) => Object.assign(document.createElement('p'), { className, textContent: text });
  const head = document.createElement('div');
  head.className = 'zd-head';
  head.append(cell('zd-num', String(z.zone)),
    Object.assign(document.createElement('h3'), { className: 'zd-name', textContent: z.short }));
  const prefixes = document.createElement('p');
  prefixes.className = 'zd-prefixes';
  prefixes.append(cell('zd-label', 'Callsigns start with'), cell('zd-pre', z.prefixes));
  box.append(head, para('zd-official', `Official name: ${z.name}`), para('zd-areas', z.areas), prefixes);
  if (n !== home && Maps.available() && z.lat !== undefined) {
    const p = Maps.pathTo(z.lat, z.lon);
    const deg = Math.round(p.bearing / 5) * 5 % 360;
    box.append(para('zd-where', `From here: beam heading about ${deg}°, roughly ${fmtNum(Math.round(p.km / 500) * 500)} km to the middle of the zone.`));
  }
  const w = state.zonesLog && state.zonesLog[String(n)];
  let log;
  if (w) {
    const when = timeIn(localTz(), { month: 'short', day: 'numeric', year: 'numeric' }, 'en-CA', new Date(w.last_time));
    log = para('zd-log worked', `✓ In your log: ${w.count === 1 ? 'one contact' : `${fmtNum(w.count)} contacts`}, the latest with ${w.last_call} on ${when}.`);
  } else if (n === home) {
    log = para('zd-log', 'This is your own zone: contacts with other stations in it count for it too.');
  } else {
    log = para('zd-log', state.zonesLog ? 'Not in your log yet.' : '');
  }
  box.append(log);
}

/* ---------- Stats: the log in numbers (Overview, Countries, CQ zones) ---------- */
const CONTINENTS = ['NA', 'SA', 'EU', 'AF', 'AS', 'OC'];
const CONT_NAMES = { NA: 'North America', SA: 'South America', EU: 'Europe', AF: 'Africa', AS: 'Asia', OC: 'Oceania' };
const STATS_TABS = ['overview', 'countries', 'zones'];
const plural = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : many}`;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function meter(fraction, extra) {
  const track = el('div', 'meter-track' + (extra ? ' ' + extra : ''));
  const fill = el('span');
  fill.style.width = `${Math.max(0, Math.min(1, fraction)) * 100}%`;
  fill.hidden = fraction <= 0;
  track.append(fill);
  return track;
}

function openStats(tab = 'overview', zone) {
  if (!$('fav').hidden) closeFavourites();
  $('stats-dlg').hidden = false;
  showStatsTab(tab, zone);
  $('stats-close').focus();
}

function closeStats() {
  $('stats-dlg').hidden = true;
  hideTip();
  $('search').focus();
}

function showStatsTab(tab, zone) {
  state.statsTab = tab;
  for (const name of STATS_TABS) {
    const on = name === tab;
    $(`st-tab-${name}`).setAttribute('aria-selected', String(on));
    $(`st-tab-${name}`).tabIndex = on ? 0 : -1;
    $(`st-${name}`).hidden = !on;
  }
  hideTip();
  if (tab === 'overview') loadStats();
  else if (tab === 'countries') loadCountries();
  else showZonesTab(zone);
}

// A small box with the exact numbers, beside the pointer, for the bars and columns.
function tipOn(node, text) {
  node.addEventListener('pointerenter', (e) => showTip(text, e));
  node.addEventListener('pointermove', placeTip);
  node.addEventListener('pointerleave', hideTip);
}
function showTip(text, e) {
  const tip = $('chart-tip');
  tip.textContent = text;
  tip.hidden = false;
  placeTip(e);
}
function placeTip(e) {
  const tip = $('chart-tip');
  if (tip.hidden) return;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  const x = e.clientX + 16 + w > innerWidth - 8 ? e.clientX - 16 - w : e.clientX + 16;
  const y = e.clientY + 18 + h > innerHeight - 8 ? e.clientY - 12 - h : e.clientY + 18;
  tip.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
}
function hideTip() { $('chart-tip').hidden = true; }

async function loadStats() {
  let s;
  try { s = await api.get('/api/stats'); } catch (err) {
    $('st-kpis').replaceChildren(el('p', 'st-empty', 'Your stats didn’t load. Try again in a moment.'));
    return;
  }
  renderKpis(s);
  renderBandBars(s.bands);
  renderContinents(s.countries.by_continent);
  renderMonths(s.months, s.first);
  renderNewOnes(s.new_ones);
}

function kpi(label, value, unit, sub, bar, barClass) {
  const tile = el('div', 'kpi');
  const big = el('div', 'kpi-value', value);
  if (unit) big.append(el('small', '', unit));
  tile.append(el('div', 'kpi-label', label), big);
  if (bar !== undefined) tile.append(meter(bar, barClass));
  const line = el('div', 'kpi-sub', sub);
  line.title = sub;
  tile.append(line);
  return tile;
}

function renderKpis(s) {
  const first = s.first ? new Date(s.first) : null;
  const since = first ? timeIn(localTz(), first.getFullYear() === new Date().getFullYear()
    ? { month: 'short', day: 'numeric' } : { month: 'short', year: 'numeric' }, 'en-CA', first) : '';
  const c = s.countries.worked, z = s.zones.worked, conts = s.continents.worked;
  const missing = CONTINENTS.filter((k) => !conts.includes(k));
  const top = s.bands.reduce((a, b) => (!a || b.contacts > a.contacts ? b : a), null);
  const far = s.furthest;
  $('st-kpis').replaceChildren(
    kpi('Contacts', fmtNum(s.total), '', s.total ? `${fmtNum(s.this_month)} this month · since ${since}` : 'None logged yet'),
    kpi('Countries', fmtNum(c), `of ${s.countries.total}`, c >= 100 ? '100 or more: enough for DXCC' : `${100 - c} more for DXCC (100)`, c / 100),
    kpi('CQ zones', String(z), 'of 40', z >= 40 ? 'All 40: enough for WAZ' : `${40 - z} more for WAZ`, z / 40, 'zone'),
    kpi('Continents', String(conts.length), 'of 6', missing.length ? `${missing.length} more for WAC` : 'All six: enough for WAC', conts.length / 6),
    kpi('Bands', String(s.bands.length), '', top ? `Most contacts on ${bandLabel(top.band)}` : 'None yet'),
    kpi('Furthest contact', far ? fmtNum(far.km) : '–', far ? 'km' : '', far ? `${far.call}${far.country ? ', ' + far.country : ''}` : 'None yet'),
  );
}

function renderBandBars(bands) {
  const box = $('st-bands');
  box.replaceChildren();
  if (!bands.length) { box.append(el('p', 'st-empty', 'Nothing logged yet.')); return; }
  const most = Math.max(...bands.map((b) => b.contacts));
  for (const b of bands) {
    const row = el('div', 'hbar');
    const track = el('div', 'hbar-track');
    const fill = el('div', 'hbar-fill');
    fill.style.width = `${(b.contacts / most) * 100}%`;
    track.append(fill);
    const value = el('span', 'hbar-value', fmtNum(b.contacts));
    value.append(el('small', '', ` · ${plural(b.countries, 'country', 'countries')}`));
    row.append(el('span', 'hbar-name', bandLabel(b.band)), track, value);
    tipOn(row, `${bandLabel(b.band)}: ${plural(b.contacts, 'contact', 'contacts')} with ${plural(b.countries, 'country', 'countries')}`);
    box.append(row);
  }
}

function renderContinents(by) {
  const box = $('st-continents');
  box.replaceChildren();
  for (const k of CONTINENTS) {
    const d = by[k] || { worked: 0, total: 0 };
    const row = el('div', 'mrow');
    const value = el('span', 'mrow-value', String(d.worked));
    value.append(el('small', '', ` of ${d.total}`));
    row.append(el('span', 'mrow-name', CONT_NAMES[k]), meter(d.total ? d.worked / d.total : 0), value);
    tipOn(row, `${CONT_NAMES[k]}: ${d.worked} of its ${d.total} countries in your log`);
    box.append(row);
  }
}

function renderMonths(months, first) {
  const box = $('st-months');
  box.replaceChildren();
  // Months before the log started are left off, but at least six are shown.
  let from = 0;
  if (first) {
    const i = months.findIndex((m) => m.month >= first.slice(0, 7));
    if (i > 0) from = Math.min(i, months.length - 6);
  }
  const shown = months.slice(from);
  box.style.gridTemplateColumns = `repeat(${shown.length}, minmax(0, 1fr))`;
  const most = Math.max(1, ...shown.map((m) => m.contacts));
  const peak = shown.reduce((a, m) => (m.contacts > a.contacts ? m : a), shown[0]);
  shown.forEach((m, i) => {
    const date = new Date(`${m.month}-15T12:00:00Z`);
    const col = el('div', 'col');
    const plot = el('div', 'col-plot');
    const share = m.contacts / most * 85;   // room above the tallest column for its number
    const fill = el('div', 'col-fill' + (m.contacts ? '' : ' zero'));
    if (m.contacts) fill.style.height = `${share}%`;
    plot.append(fill);
    if (m.contacts && (m === peak || i === shown.length - 1)) {   // numbers on the busiest month and this one
      const value = el('span', 'col-value', fmtNum(m.contacts));
      value.style.bottom = `calc(${share}% + 0.25rem)`;
      plot.append(value);
    }
    col.append(plot, el('span', 'col-name', timeIn('UTC', { month: 'short' }, 'en-CA', date).replace('.', '')));
    tipOn(col, `${timeIn('UTC', { month: 'long', year: 'numeric' }, 'en-CA', date)}: ${plural(m.contacts, 'contact', 'contacts')}`);
    box.append(col);
  });
}

function renderNewOnes(items) {
  const list = $('st-new');
  list.replaceChildren();
  if (!items.length) { list.append(el('li', 'st-empty', 'Each new country and CQ zone you log shows up here.')); return; }
  for (const f of items) {
    const item = document.createElement('li');
    const what = el('span', 'new-what');
    const zone = f.kind === 'zone' ? Number(f.name.replace(/\D/g, '')) : 0;
    const info = zone && state.zoneInfo ? state.zoneInfo.get(zone) : null;
    what.append(el('span', `badge ${f.kind}`, f.kind === 'zone' ? 'ZONE' : 'COUNTRY'),
      f.kind === 'zone' ? `${zone}${info ? ', ' + info.short : ''}` : f.name);
    item.append(el('span', 'new-when', timeIn(localTz(), { month: 'short', day: 'numeric' }, 'en-CA', new Date(f.time))),
      what, el('span', 'new-call', f.call));
    list.append(item);
  }
}

async function loadCountries() {
  let data;
  try { data = await api.get('/api/countries'); } catch (err) {
    if (!state.countries) $('ct-groups').replaceChildren(el('p', 'soon', 'The country list didn’t load. Try again in a moment.'));
    return;
  }
  state.countries = data;
  renderCountries();
}

function renderCountries() {
  const data = state.countries;
  if (!data) return;
  const all = data.countries;
  const done = all.filter((c) => c.worked).length;
  $('ct-done').textContent = fmtNum(done);
  $('ct-total').textContent = fmtNum(all.length);
  $('ct-bar').style.width = `${(done / Math.max(1, all.length)) * 100}%`;
  const show = state.ctShow || 'all';
  for (const b of document.querySelectorAll('.ct-filter button')) b.setAttribute('aria-pressed', String(b.dataset.show === show));
  const box = $('ct-groups');
  const scroll = box.scrollTop;
  box.replaceChildren();
  for (const k of CONTINENTS) {
    const here = all.filter((c) => c.continent === k);
    const list = here.filter((c) => show === 'all' || (show === 'worked') === !!c.worked);
    const group = el('section', 'ct-group');
    const title = el('h3', '', CONT_NAMES[k]);
    title.append(el('small', '', `${here.filter((c) => c.worked).length} of ${here.length} in your log`));
    group.append(title);
    if (!list.length) {
      group.append(el('p', 'st-empty', show === 'worked' ? 'None in your log yet.' : 'All of them are in your log!'));
    } else {
      const chips = el('div', 'ct-chips');
      for (const c of list) {
        const b = el('button', 'ct-chip' + (c.worked ? ' worked' : '') + (c.name === data.home ? ' home' : ''), c.name);
        b.type = 'button';
        b.dataset.name = c.name;
        b.setAttribute('aria-pressed', String(c.name === state.ctPicked));
        b.addEventListener('click', () => pickCountry(c.name));
        chips.append(b);
      }
      group.append(chips);
    }
    box.append(group);
  }
  box.scrollTop = scroll;
  renderCountryDetail();
}

function pickCountry(name) {
  state.ctPicked = name;
  for (const b of $('ct-groups').querySelectorAll('.ct-chip')) b.setAttribute('aria-pressed', String(b.dataset.name === name));
  renderCountryDetail();
}

function renderCountryDetail() {
  const box = $('ct-detail');
  box.replaceChildren();
  const data = state.countries;
  const c = data && data.countries.find((x) => x.name === state.ctPicked);
  if (!c) {
    box.append(el('p', 'zd-areas', 'Pick a country to see its beam heading, its CQ zone and what’s in your log.'),
      el('p', 'zd-official', 'Filled in: in your log. Outlined in amber: your own country.'));
    return;
  }
  const head = el('div', 'zd-head');
  head.append(el('span', 'zd-num ct-pre', c.prefix), el('h3', 'zd-name', c.name));
  const where = el('p', 'zd-official', `${CONT_NAMES[c.continent] || c.continent} · `);
  where.append(zoneLink(c.cq, `CQ zone ${c.cq}`), ` · callsigns start with ${c.prefix}`);
  box.append(head, where);
  if (c.name !== data.home && Maps.available()) {
    const p = Maps.pathTo(c.lat, c.lon);
    box.append(el('p', 'zd-where', `From here: beam heading ${Math.round(p.bearing)}°, about ${fmtNum(Math.round(p.km / 100) * 100)} km (to the middle of the country).`));
  }
  const w = c.worked;
  if (w) {
    const when = timeIn(localTz(), { month: 'short', day: 'numeric', year: 'numeric' }, 'en-CA', new Date(w.last_time));
    const bands = (w.bands || []).map(bandLabel).join(', ');
    box.append(el('p', 'zd-log worked', `✓ In your log: ${w.count === 1 ? 'one contact' : `${fmtNum(w.count)} contacts`}${bands ? ` on ${bands}` : ''}, the latest with ${w.last_call} on ${when}.`));
  } else if (c.name === data.home) {
    box.append(el('p', 'zd-log', 'Your own country. A contact with another station here counts for it.'));
  } else {
    box.append(el('p', 'zd-log', 'Not in your log yet.'));
  }
}

/* ---------- Alerts: storms near the house, and space weather ---------- */
const ALERT_RANK = { warning: 0, watch: 1, advisory: 2, statement: 3 };
const ALERT_ICONS = {
  lightning: (c) => `<svg viewBox="0 0 24 24"><path d="M13.5 2 L5 13.5 H11 L9.5 22 L19 9.5 H12.5 Z" fill="${c}"></path></svg>`,
  weather: (c) => `<svg viewBox="0 0 24 24"><path d="M12 3 L22 20 H2 Z" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"></path><path d="M12 9.5 V14" stroke="${c}" stroke-width="2.2" stroke-linecap="round"></path><circle cx="12" cy="17" r="1.3" fill="${c}"></circle></svg>`,
  space: (c) => `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4.5" fill="${c}"></circle><path d="M12 2.5 V5 M12 19 V21.5 M2.5 12 H5 M19 12 H21.5 M5.3 5.3 L7 7 M17 17 L18.7 18.7 M5.3 18.7 L7 17 M17 7 L18.7 5.3" stroke="${c}" stroke-width="1.8" stroke-linecap="round"></path></svg>`,
};
const alertKey = (a) => `${a.id}|${a.title}|${a.updated || ''}`;

function loadSet(name) {
  try { return new Set(JSON.parse(localStorage.getItem(name) || '[]')); } catch (err) { return new Set(); }
}
function saveSet(name, set) {
  try { localStorage.setItem(name, JSON.stringify([...set])); } catch (err) { /* only a convenience */ }
}

async function refreshAlerts() {
  let data;
  try { data = await api.get('/api/alerts'); } catch (err) { return; }
  // Thunderstorms first (they come with the antenna reminder), then warnings, watches and advisories; weather before space.
  state.alerts = (data.alerts || []).slice().sort((a, b) => (b.lightning ? 1 : 0) - (a.lightning ? 1 : 0)
    || (ALERT_RANK[a.level] ?? 9) - (ALERT_RANK[b.level] ?? 9)
    || (a.kind === 'weather' ? 0 : 1) - (b.kind === 'weather' ? 0 : 1));
  if (!state.alertsHidden) state.alertsHidden = loadSet('alertsHidden');
  if (!state.alertsToasted) state.alertsToasted = loadSet('alertsToasted');
  if (data.ready) {   // forget alerts that have ended
    const now = new Set(state.alerts.map(alertKey));
    for (const set of [state.alertsHidden, state.alertsToasted]) for (const k of [...set]) if (!now.has(k)) set.delete(k);
    saveSet('alertsHidden', state.alertsHidden);
  }
  const fresh = state.alerts.find((a) => a.kind === 'weather' && !state.alertsToasted.has(alertKey(a)));
  if (fresh) {   // a new weather alert gets one message as well as the banner
    state.alertsToasted.add(alertKey(fresh));
    showToast(`Weather alert for your area: ${fresh.title}.${fresh.lightning ? ' Disconnect the antennas.' : ''}`, null, true);
  }
  saveSet('alertsToasted', state.alertsToasted);
  renderAlerts();
  renderSpaceChip(data);
}

// The alerts with a banner (minor space weather only shows on the chip), less the ones hidden.
const bannerAlerts = () => (state.alerts || []).filter((a) => a.banner !== false && !state.alertsHidden.has(alertKey(a)));

function renderAlerts() {
  const shown = bannerAlerts();
  const bar = $('alert-bar');
  bar.hidden = !shown.length;
  if (!shown.length) { state.alertShown = null; return; }
  state.alertAt = Math.min(state.alertAt || 0, shown.length - 1);
  const a = shown[state.alertAt];
  state.alertShown = a;
  bar.className = `alert-bar ${a.kind === 'space' ? 'space' : a.level}`;
  const colour = a.kind === 'space' ? '#A78BFA' : a.level === 'warning' ? '#FF7A7E' : '#FFB24A';
  $('alert-icon').innerHTML = (a.lightning ? ALERT_ICONS.lightning : ALERT_ICONS[a.kind] || ALERT_ICONS.weather)(colour);
  $('alert-title').textContent = a.kind === 'weather' ? `${a.title} for your area` : a.title;
  $('alert-count').textContent = [shown.length > 1 ? `${state.alertAt + 1} of ${shown.length}` : '',
    a.issued ? `issued ${a.issued}` : ''].filter(Boolean).join(' · ');
  $('alert-hint').hidden = !a.lightning;
  $('alert-hint').textContent = 'Lightning risk: disconnect the antennas from the radio.';
  $('alert-text').textContent = a.text || '';
  $('alert-text').title = a.text || '';
  $('alert-next').hidden = shown.length < 2;
  $('alert-more').textContent = a.kind === 'space' ? 'More from NOAA' : 'Full warning';
}

function renderSpaceChip(data) {
  const chip = $('space-chip');
  chip.hidden = !data.space;   // space weather turned off in the settings
  if (chip.hidden) return;
  const space = (data.alerts || []).filter((a) => a.kind === 'space');
  chip.textContent = space.length ? space.map((a) => a.short).join(' · ') : 'Space weather quiet';
  chip.classList.toggle('active', space.length > 0);
  chip.classList.toggle('severe', space.some((a) => a.severe));
  chip.title = `${data.space} Click for NOAA’s space weather page.`;
}

function hideAlert() {
  if (!state.alertShown) return;
  state.alertsHidden.add(alertKey(state.alertShown));
  saveSet('alertsHidden', state.alertsHidden);
  renderAlerts();
}

/* ---------- Favourites: the station's ham websites, opened in the web browser ---------- */
async function loadFavourites() {
  let data;
  try { data = await api.get('/api/favourites'); } catch (err) { return; }   // tried again when opened
  const box = $('fav-groups');
  box.replaceChildren();
  for (const group of data.groups || []) {
    const section = document.createElement('section');
    section.className = 'fav-group';
    const title = document.createElement('h3');
    title.textContent = group.group;
    const list = document.createElement('ul');
    for (const link of group.links) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'fav-link';
      button.append(cell('fav-name', link.name), cell('fav-site', link.site));
      button.addEventListener('click', () => openLink({ id: link.id }));
      const item = document.createElement('li');
      item.append(button);
      list.append(item);
    }
    section.append(title, list);
    box.append(section);
  }
  state.favLoaded = box.children.length > 0;
}

function openFavourites() {
  if (!state.favLoaded) loadFavourites();
  $('fav').hidden = false;
  $('fav-close').focus();
}

function closeFavourites() {
  $('fav').hidden = true;
  $('search').focus();
}

async function openLink(body) {
  try {
    const r = await api.post('/api/open', body);
    if (!r.ok) { showToast('The web browser didn’t open. Try again in a moment.'); return; }
    if (!$('fav').hidden) closeFavourites();
    showToast(`${r.name} is opening in a browser window. Close it to come back here.`);
  } catch (err) {
    showToast('That didn’t open. Try again in a moment.');
  }
}

function renderLookups(id, call, lookups) {
  const box = $(id);
  box.replaceChildren();
  box.hidden = !lookups || !lookups.length;
  if (box.hidden) return;
  for (const lookup of lookups) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = lookup.short;
    button.title = `Look up ${call} ${lookup.where}`;
    button.addEventListener('click', () => openLink({ id: lookup.id, call }));
    box.append(button);
  }
}

/* ---------- Coming up ---------- */
async function refreshEvents() {
  try {
    const events = await api.get('/api/contests');
    const box = $('events');
    box.replaceChildren();
    for (const ev of events.slice(0, 2)) {
      const opts = { weekday: 'long', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
      const start = timeIn(localTz(), opts, 'en-CA', new Date(ev.start));
      const end = timeIn(localTz(), opts, 'en-CA', new Date(ev.end));
      const div = document.createElement('div');
      div.className = 'event';
      div.append(
        cell('event-name', ev.name),
        cell('event-when', ev.on_now ? `On now, until ${end}` : `${start} to ${end}`),
        cell('event-note', ev.note),
      );
      box.append(div);
    }
  } catch (err) { /* try again next hour */ }
}

/* ---------- Radio ---------- */
function fmtFreq(hz) {
  const mhz = Math.floor(hz / 1e6), khz = Math.floor((hz % 1e6) / 1000), tens = Math.floor((hz % 1000) / 10);
  return `${mhz}.${String(khz).padStart(3, '0')}.${String(tens).padStart(2, '0')}`;
}

function renderMeter(db) {
  if (db === null || db === undefined) { $('meter-wrap').hidden = true; return; }
  const s = Math.max(0, Math.min(9, Math.round((db + 54) / 6)));
  const over = db > 0 ? Math.min(3, Math.ceil(db / 20)) : 0;
  [...$('meter').children].forEach((seg, i) => seg.classList.toggle('on', i < s + over));
  $('meter-label').textContent = db > 0 ? `S9 +${Math.round(db / 10) * 10}` : `S${s}`;
  $('meter-wrap').hidden = false;
}

// What the radio box says when the radio isn't answering, by the reason the station app gives.
const RADIO_HINTS = {
  quiet: ['Not answering', 'Is the radio switched on?'],
  cable: ['Cable unplugged', 'Check the radio’s USB cable'],
  link: ['Starting up', 'The radio link is starting'],
  setup: ['Not set up yet', 'Run the setup helper once'],
  app: ['Not connected', 'The station app is restarting'],
};

function renderTx(r) {
  const power = r.power_w !== null && r.power_w !== undefined ? `${Math.round(r.power_w)} W`
    : (r.power_set_w ? `${r.power_set_w} W set` : '');
  $('tx-power').textContent = power;
  const rf = r.power_w === null || r.power_w === undefined || r.power_w >= 5;   // SWR means little with no power out
  const swr = $('tx-swr');
  swr.hidden = !(rf && r.swr);
  if (!swr.hidden) {
    swr.textContent = `SWR ${r.swr.toFixed(1)}`;
    swr.classList.toggle('fair', r.swr >= 1.5 && r.swr < 2.5);
    swr.classList.toggle('high', r.swr >= 2.5);
  }
  // A high SWR for three readings in a row (about three seconds) gets a warning, at most once a minute.
  const high = rf && r.swr >= 3 && (r.power_w === null || r.power_w === undefined || r.power_w >= 10);
  state.swrHigh = high ? (state.swrHigh || 0) + 1 : 0;
  if (state.swrHigh >= 3 && Date.now() - (state.swrWarned || 0) > 60000) {
    state.swrWarned = Date.now();
    showToast(`High SWR (${r.swr.toFixed(1)}) while transmitting on ${fmtFreq(r.freq_hz)}. Check the antenna or the tuner.`, null, true);
  }
}

async function pollRadio() {
  if (state.radioBusy) return;   // the last reading hasn't come back yet
  state.radioBusy = true;
  let r = { ok: false, reason: 'app' };
  try { r = await api.get('/api/rig?from=display'); } catch (err) { /* treated as not connected */ } finally { state.radioBusy = false; }
  state.radio = r;
  if (r.name) $('radio-name').textContent = r.name;
  renderRadioSwap(r);
  $('radio').classList.toggle('off', !r.ok);
  const [why, hint] = r.ok ? ['', ''] : (RADIO_HINTS[r.set_up === false ? 'setup' : r.reason] || RADIO_HINTS.quiet);
  $('radio-freq').textContent = r.ok ? fmtFreq(r.freq_hz) : why;
  $('radio-hint').textContent = hint;
  $('radio-hint').hidden = !!r.ok;
  $('radio-mode').textContent = r.ok ? (r.mode || '') : '';
  const tx = r.ok && r.tx === true;
  $('radio').classList.toggle('tx', tx);
  $('radio-sub').hidden = !r.ok;
  $('radio-tx').hidden = !tx;
  if (tx) renderTx(r); else state.swrHigh = 0;
  $('on-air').hidden = !r.ok || (r.tx !== true && r.tx !== false);   // hidden if the radio can't say
  $('on-air').classList.toggle('lit', tx);
  renderMeter(r.ok && !tx ? r.s_db : null);   // while transmitting the meter shows output, not signals
  state.radioOk = !!r.ok;
  state.radioBand = r.ok ? bandFor(r.freq_hz) : null;
  if (state.card && state.card.spot) $('btn-tune').hidden = !state.radioOk;
  if (state.card && !state.logBandTouched && state.radioBand) setLogBand(state.radioBand);
  if (r.ok && !state.bandChosen) {
    const band = bandFor(r.freq_hz);
    if (band && BANDS.includes(band) && band !== state.band) { state.band = band; refreshSpots(); }
  }
}

// With both radios on, the name of the one showing is a button that shows the other one instead.
// (The box follows the radio in use by itself: the one transmitting, or the one tuned last.)
function renderRadioSwap(r) {
  const button = $('radio-name');
  const other = (r.others || []).find((o) => o.ok);
  button.disabled = !other;
  button.classList.toggle('swap', !!other);
  button.dataset.radio = other ? other.radio : '';
  const at = other ? [other.freq_hz ? fmtFreq(other.freq_hz) : '', other.mode || ''].filter(Boolean).join(' ') : '';
  button.title = other ? `The ${other.name} is on too${at ? ` (${at})` : ''}. Click to show it here instead.` : '';
  if (other) button.setAttribute('aria-label', `${r.name}. Show the ${other.name} instead`);
  else button.removeAttribute('aria-label');
}

async function pickOtherRadio() {
  const key = $('radio-name').dataset.radio;
  if (!key) return;
  try { await api.post('/api/rig/pick', { radio: key }); } catch (err) { /* the next reading shows what's what */ }
  pollRadio();
}

function bandFor(hz) {
  const edges = [[1.8e6, 2e6, '160m'], [3.5e6, 4e6, '80m'], [7e6, 7.3e6, '40m'], [10.1e6, 10.15e6, '30m'], [14e6, 14.35e6, '20m'],
    [18.068e6, 18.168e6, '17m'], [21e6, 21.45e6, '15m'], [24.89e6, 24.99e6, '12m'], [28e6, 29.7e6, '10m'], [50e6, 54e6, '6m'], [144e6, 148e6, '2m']];
  const hit = edges.find(([lo, hi]) => hz >= lo && hz <= hi);
  return hit ? hit[2] : null;
}

/* ---------- Personal touches ---------- */
// The welcome banner (welcome.js): once after the computer starts. The display remembers which start
// it last showed it for, so a reload or an update doesn't show it again. ?welcome=1 shows it anyway.
function maybeWelcome() {
  const cfg = state.cfg;
  const asked = /[?&]welcome=1(&|$)/.test(window.location.search);
  if (!asked && (!cfg.welcome || typeof cfg.uptime_s !== 'number' || cfg.uptime_s > 20 * 60)) return;
  let seen = null;
  try { seen = localStorage.getItem('welcomeBoot'); } catch (err) { /* only a convenience */ }
  if (!asked && seen === String(cfg.system_boot)) return;
  try { localStorage.setItem('welcomeBoot', String(cfg.system_boot)); } catch (err) { /* only a convenience */ }
  Welcome.play(cfg);
}

// The tester checklist: a link under the greeting with how many things are left to try.
async function refreshChecklist() {
  const link = $('checklist-link');
  if (!state.cfg || !state.cfg.checklist) { link.hidden = true; return; }
  let data;
  try { data = await api.get('/api/checklist'); } catch (err) { return; }
  const s = data.summary;
  link.hidden = false;
  $('checklist-count').textContent = s.left ? `\u00b7 ${s.left} to try` : '\u00b7 all tried';
  const helper = state.cfg.helper ? ` for ${state.cfg.helper}` : '';
  link.title = `Things to try on the station display, with your notes${helper}. Tried ${s.tried} of ${s.items}.`;
}

async function openChecklist() {
  try {
    const r = await api.post('/api/open', { id: 'checklist' });
    showToast(r.ok ? 'The tester checklist is opening in a browser window.' : 'The checklist didn\u2019t open. Try again in a moment.');
  } catch (err) {
    showToast('The checklist didn\u2019t open. Try again in a moment.');
  }
}

function celebration(q) {
  const c = q.celebrate;
  if (!c) return null;
  const name = state.cfg && state.cfg.operator ? state.cfg.operator : '';
  const milestone = c.total === 100 || c.total === 250 || c.total === 500 || (c.total >= 1000 && c.total % 1000 === 0);
  const zone = c.new_zone ? ` Zone ${c.zone} makes ${c.zones} of the 40 CQ zones.` : '';
  if (c.total === 1) return `Logged ${q.call}, the very first contact in your new log!`;
  if (c.new_country && milestone) return `Logged ${q.call}: a new country and contact number ${fmtNum(c.total)}! ${q.country} is number ${c.countries} in your log.${zone}`;
  if (c.new_country) return `Logged ${q.call}, and that's a new country! ${q.country} is number ${c.countries} in your log.${zone}`;
  if (c.new_zone) return `Logged ${q.call}, and that's a new CQ zone! Zone ${c.zone} makes ${c.zones} of 40.`;
  if (milestone) return `Logged ${q.call}. That's contact number ${fmtNum(c.total)}. Congratulations${name ? ', ' + name : ''}!`;
  return null;
}

function specialDay(now) {
  const cfg = state.cfg;
  if (!cfg) return null;
  const tz = localTz();
  const today = timeIn(tz, { month: '2-digit', day: '2-digit' }, 'en-CA', now);      // "03-22"
  const year = Number(timeIn(tz, { year: 'numeric' }, 'en-CA', now));
  const name = cfg.operator || '';
  let birthday = (cfg.birthday || '').trim();
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  if (birthday === '02-29' && !leap) birthday = '02-28';
  if (birthday && today === birthday) {
    return { line: `Happy birthday${name ? ', ' + name : ''}!`, toast: `Happy birthday${name ? ', ' + name : ''}! Hope the bands are kind to you today.` };
  }
  const licensed = (cfg.licensed || '').trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(licensed) && today === licensed.slice(5)) {
    const years = year - Number(licensed.slice(0, 4));
    if (years > 0) {
      const plural = years === 1 ? 'year' : 'years';
      return { line: `${years} ${plural} on the air today!`, toast: `Happy licence anniversary${name ? ', ' + name : ''}! ${years} ${plural} on the air today.` };
    }
  }
  return null;
}

function showSpecialDay(now) {
  const day = specialDay(now);
  const line = $('greeting-special');
  line.hidden = !day;
  line.textContent = day ? day.line : '';
  const stamp = day ? timeIn(localTz(), { year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA', now) : null;
  if (day && state.specialShown !== stamp) {
    state.specialShown = stamp;
    showToast(day.toast, null, 'celebrate');
  }
}

/* ---------- Spots from the DX cluster ---------- */
const markPath = (points, r) => points.map(([x, y]) => `M${f1(x - r)} ${f1(y)} a${r} ${r} 0 1 0 ${2 * r} 0 a${r} ${r} 0 1 0 ${-2 * r} 0`).join(' ');
const bandText = (b) => b.replace('m', ' m');

function renderChips(counts) {
  const box = $('band-chips');
  if (!box.children.length) {
    for (const b of BANDS) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.dataset.band = b;
      chip.setAttribute('aria-label', bandText(b));
      chip.append(b.replace('m', ''), cell('count', ''));
      chip.addEventListener('click', () => { state.band = b; state.bandChosen = true; refreshSpots(); });
      box.append(chip);
    }
  }
  for (const chip of box.children) {
    const n = counts[chip.dataset.band] || 0;
    chip.setAttribute('aria-pressed', String(chip.dataset.band === state.band));
    chip.classList.toggle('quiet', n === 0);
    chip.querySelector('.count').textContent = n ? String(n) : '';
  }
}

const NEW_TEXT = { country: 'A new country for your log', zone: 'A new CQ zone for your log', band: 'New on this band for your log' };
const NEW_BADGE = { country: 'NEW', zone: 'ZONE', band: 'BAND' };

function countryCell(s) {
  const span = cell('r-country', '');
  if (s.new) {
    const badge = cell('badge ' + s.new, NEW_BADGE[s.new]);
    badge.title = NEW_TEXT[s.new];
    span.append(badge);
  }
  span.append(s.region || s.country || '');
  return span;
}

function renderSpots(data) {
  renderChips(data.counts || {});
  const list = $('spot-list');
  list.replaceChildren();
  const spots = data.spots || [];
  const picked = state.card && state.card.spot ? state.card.call : null;
  for (const s of spots) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'row spot' + (s.call === picked ? ' picked' : '');
    row.append(
      cell('r-when', s.age_min < 1 ? 'now' : `${s.age_min} min`),
      cell('r-call', s.call),
      cell('r-what', s.freq_khz.toFixed(1)),
      countryCell(s),
      cell('r-beam', s.bearing !== undefined ? `${s.bearing}°` : ''),
    );
    row.title = (s.new ? NEW_TEXT[s.new] + '. ' : '') + (s.comment ? `${s.comment} (spotted by ${s.spotter})` : `Spotted by ${s.spotter}`);
    row.addEventListener('click', () => lookUp(s.call, s));
    const item = document.createElement('li');
    item.append(row);
    list.append(item);
  }
  const empty = $('spots-empty');
  empty.hidden = spots.length > 0;
  list.hidden = spots.length === 0;
  if (!spots.length) {
    empty.textContent = data.status === 'no callsign'
      ? 'Live spots start once your callsign is set: callsign under [station] in config.ini, then restart the app.'
      : state.voice && state.band === '30m'
      ? '30 m is CW and digital only, so there are no voice spots there.'
      : `Quiet on ${bandText(state.band)} right now. New spots appear here as they come in.`;
  }
  $('legend-spots').textContent = `Spotted on ${bandText(state.band)}`;
  const placed = spots.filter((s) => s.lat !== null).map((s) => [px(s.lon), py(s.lat)]);
  $('spot-marks').setAttribute('d', markPath(placed, 4.5));
  $('spot-glow').setAttribute('d', markPath(placed, 9));
  const fresh = spots.filter((s) => s.lat !== null && s.new === 'country');
  $('spot-new').setAttribute('d', markPath(fresh.map((s) => [px(s.lon), py(s.lat)]), 8));
  $('legend-new').hidden = !fresh.length;
  Maps.setSpots(spots);
  const status = { connected: `Live from ${data.node || state.cfg.cluster}. Showing the last 30 minutes.`,
    connecting: 'Connecting to the DX cluster…', 'logging in': 'Connecting to the DX cluster…',
    reconnecting: 'Reconnecting to the DX cluster…',
    starting: 'Connecting to the DX cluster…', off: 'Spots are turned off in the settings.',
    'no callsign': 'The DX cluster needs your callsign to sign in.' };
  $('cluster-status').textContent = status[data.status] || '';
}

async function refreshSpots() {
  try {
    const band = encodeURIComponent(state.band);
    renderSpots(await api.get(`/api/spots?band=${band}&voice=${state.voice ? 1 : 0}`));
  } catch (err) { /* keep what is showing */ }
}

async function tuneToSpot() {
  const spot = state.card && state.card.spot;
  if (!spot) return;
  const mode = spot.freq_khz < 10000 ? 'LSB' : 'USB';
  const hz = Math.round(spot.freq_khz * 1000);
  try {
    const r = await api.post('/api/rig/tune', { freq_hz: hz, mode });
    let text = 'The radio isn’t connected right now.';
    if (r.ok) {
      text = `${r.radio ? 'The ' + r.radio : 'The radio'} is tuned to ${fmtFreq(hz)} ${mode}${r.note ? ', since ' + r.note : ''}.`;
    } else if (r.no_antenna) {
      const other = ((state.cfg && state.cfg.radios) || []).find((x) => x.set_up && x.name !== r.radio && (x.bands || []).includes(r.no_antenna));
      text = `The ${r.radio} has no ${bandLabel(r.no_antenna)} antenna${other ? `. Switch on the ${other.name} and try again` : ''}.`;
    }
    showToast(text);
    pollRadio();
  } catch (err) {
    showToast('The radio isn’t connected right now.');
  }
}

/* ---------- Band conditions (N0NBH) ---------- */
async function refreshConditions() {
  let c = { ok: false };
  try { c = await api.get('/api/conditions'); } catch (err) { /* keep waiting */ }
  $('cond-live').hidden = !c.ok;
  $('cond-wait').hidden = !!c.ok;
  if (!c.ok) { $('cond-foot').textContent = ''; return; }
  const numbers = $('cond-numbers');
  numbers.replaceChildren('Solar flux ', Object.assign(document.createElement('b'), { textContent: c.sfi }),
    ', A index ', Object.assign(document.createElement('b'), { textContent: c.a }),
    ', K index ', Object.assign(document.createElement('b'), { textContent: c.k }));
  const table = $('cond-table');
  table.replaceChildren(cell('head', ''), cell('head', 'Day'), cell('head', 'Night'));
  for (const b of c.bands) {
    table.append(cell('band', b.name),
      cell('rating-' + b.day.toLowerCase(), b.day), cell('rating-' + b.night.toLowerCase(), b.night));
  }
  $('cond-foot').textContent = c.updated
    ? `From N0NBH, updated ${timeIn(localTz(), { hour: 'numeric', minute: '2-digit' }, 'en-CA', new Date(c.updated))}`
    : 'From N0NBH';
}

/* ---------- Who can hear you: band openings from PSK Reporter, and who heard the station ---------- */
const BAND_ORDER = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m', '6m', '2m', '70cm'];
const CONT_SHORT = { NA: 'N. America', SA: 'S. America', EU: 'Europe', AF: 'Africa', AS: 'Asia', OC: 'Oceania' };
const agoText = (ms) => {
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} hr ago`;
  return `${Math.round(mins / 1440)} days ago`;
};

async function refreshOpenings() {
  try { state.openings = await api.get('/api/openings'); } catch (err) { return; }
  renderOpenings();
  renderHeard();
}

function renderOpenings() {
  const o = state.openings || { on: true, connected: false, bands: [] };
  const list = $('open-list');
  list.replaceChildren();
  const busiest = o.bands.filter((b) => b.receivers > 0 && BANDS.includes(b.band))   // only bands with antennas
    .sort((a, b) => b.receivers - a.receivers).slice(0, 4);
  const shown = busiest.sort((a, b) => BAND_ORDER.indexOf(a.band) - BAND_ORDER.indexOf(b.band));
  for (const b of shown) {
    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-pressed', String(state.openBand === b.band));
    const where = b.continents.slice(0, 3).map(([k]) => CONT_SHORT[k] || k).join(', ');
    button.append(cell('o-band', bandLabel(b.band)), cell('o-where', where || 'nearby'), cell('o-count', `${fmtNum(b.receivers)} hearing`));
    button.title = `${bandLabel(b.band)}: ${plural(b.receivers, 'station is', 'stations are')} hearing ${plural(b.senders, 'station', 'stations')} near you. `
      + `The farthest, ${b.farthest.call}${b.farthest.country ? ' in ' + b.farthest.country : ''}, is ${fmtNum(b.farthest.km)} km away. Click to see them on the map.`;
    button.addEventListener('click', () => (state.openBand === b.band ? hideOpenings() : showOpenings(b.band)));
    const item = document.createElement('li');
    item.append(button);
    list.append(item);
  }
  list.hidden = !shown.length;
  $('open-sub').hidden = !shown.length;
  const empty = $('open-empty');
  empty.hidden = shown.length > 0;
  if (!shown.length) {
    empty.textContent = !o.on ? 'Band openings are switched off in the settings.'
      : o.connected ? 'Listening to PSK Reporter. Bands that are open from your area show here as stations near you are heard.'
        : 'Connecting to PSK Reporter. It keeps trying by itself.';
  }
  if (state.openBand) {
    if (o.bands.some((b) => b.band === state.openBand)) showOpenings(state.openBand, true);   // keep the map current
    else hideOpenings();
  }
}

// Green dots on the map where the band picked is hearing stations near the station.
function showOpenings(band, keepTimer) {
  const b = state.openings && state.openings.bands.find((x) => x.band === band);
  if (!b) { hideOpenings(); return; }
  state.openBand = band;
  Maps.setOpenings(b.points);
  $('open-marks').setAttribute('d', markPath(b.points.map(([la, lo]) => [px(lo), py(la)]), 3.4));
  $('open-chip-text').textContent = `${plural(b.receivers, 'station', 'stations')} hearing your area on ${bandLabel(band)}, as far as ${fmtNum(b.farthest.km)} km`;
  $('open-chip').hidden = false;
  for (const button of $('open-list').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.querySelector('.o-band').textContent === bandLabel(band)));
  }
  if (!keepTimer) {
    clearTimeout(state.openTimer);
    state.openTimer = setTimeout(hideOpenings, 5 * 60000);
  }
}

function hideOpenings() {
  state.openBand = null;
  clearTimeout(state.openTimer);
  Maps.setOpenings([]);
  $('open-marks').setAttribute('d', '');
  $('open-chip').hidden = true;
  for (const button of $('open-list').querySelectorAll('button')) button.setAttribute('aria-pressed', 'false');
}

async function refreshHeard() {
  let rows;
  try { rows = await api.get('/api/heard'); } catch (err) { return; }
  state.heard = rows;
  renderHeard();
  const top = rows.length ? `${rows[0].spotter} ${rows[0].time}` : '';
  if (state.heardTop !== null && top && top !== state.heardTop) {
    const h = rows[0];
    const where = h.region || h.country;
    const far = h.distance_km ? `, ${fmtNum(h.distance_km)} km away` : '';
    showToast(`${h.spotter}${where ? ' in ' + where : ''} just spotted you on ${h.freq_khz.toFixed(1)}${far}.`, null, true);
  }
  state.heardTop = top;
}

// Spots of the station from the DX cluster, and (once it runs FT8) PSK Reporter's decodes of it: listed, and drawn as paths.
function renderHeard() {
  const rows = state.heard || [];
  const own = (state.openings && state.openings.own) || [];
  const list = $('heard-list');
  list.replaceChildren();
  const now = Date.now();
  if (own.length) {
    const far = own.reduce((a, r) => (r.km > a.km ? r : a), own[0]);
    const count = new Set(own.map((r) => r.call)).size;
    const item = el('li', 'psk');
    item.append(cell('heard-psk', `Your ${own[0].mode || 'digital'} signal was decoded by ${plural(count, 'station', 'stations')} today, `
      + `as far as ${far.call}${far.country ? ' in ' + far.country : ''} (${fmtNum(far.km)} km).`));
    list.append(item);
  }
  for (const h of rows.slice(0, own.length ? 1 : 2)) {
    const item = document.createElement('li');
    item.append(cell('heard-call', h.spotter), cell('heard-where', [h.region || h.country, h.freq_khz.toFixed(1)].filter(Boolean).join(', ')),
      cell('heard-ago', agoText(now - new Date(h.time).getTime())));
    item.title = `${h.spotter} spotted you on the DX cluster`;
    list.append(item);
  }
  $('heard-empty').hidden = rows.length > 0 || own.length > 0;
  const decoders = own.filter((r) => now / 1000 - r.time < 2 * 3600).sort((a, b) => b.km - a.km).slice(0, 20);
  const points = rows.filter((h) => h.lat !== null && h.lat !== undefined).map((h) => [h.lat, h.lon])
    .concat(decoders.map((r) => [r.lat, r.lon]));
  $('heard-marks').setAttribute('d', markPath(points.map(([la, lo]) => [px(lo), py(la)]), 6));
  $('heard-paths').setAttribute('d', state.cfg ? points.map(([la, lo]) => greatCircle(state.cfg.lat, state.cfg.lon, la, lo)).join(' ') : '');
  Maps.setHeard(points.map(([lat, lon]) => ({ lat, lon })));
}

/* ---------- Listen to yourself on a receiver far away (KiwiSDR) ---------- */
const HF_BANDS = [[1.8e6, 2e6, '160m'], [3.5e6, 4e6, '80m'], [5.25e6, 5.45e6, '60m'], [7e6, 7.3e6, '40m'], [10.1e6, 10.15e6, '30m'],
  [14e6, 14.35e6, '20m'], [18.068e6, 18.168e6, '17m'], [21e6, 21.45e6, '15m'], [24.89e6, 24.99e6, '12m'], [28e6, 29.7e6, '10m']];
const LISTEN_BANDS = { '80m': [3800000, 'LSB'], '40m': [7200000, 'LSB'], '20m': [14250000, 'USB'], '17m': [18140000, 'USB'],
  '15m': [21300000, 'USB'], '12m': [24950000, 'USB'], '10m': [28400000, 'USB'] };
const hfBand = (hz) => (HF_BANDS.find(([lo, hi]) => hz >= lo && hz <= hi) || [])[2] || null;
const listenBands = () => {   // the voice bands with antennas
  const mine = Object.keys(LISTEN_BANDS).filter((b) => BANDS.includes(b));
  return mine.length ? mine : Object.keys(LISTEN_BANDS);
};
const mhzText = (hz) => (hz / 1e6).toFixed(3);

async function openListen(pick) {
  if (!$('fav').hidden) closeFavourites();
  const r = state.radio;
  let band = pick, freq, mode, fromRadio = false;
  if (!band && r && r.ok && r.freq_hz && hfBand(r.freq_hz)) {
    freq = r.freq_hz;
    mode = r.mode || 'USB';
    band = hfBand(freq);
    fromRadio = true;
  } else {
    const choices = listenBands();
    if (!choices.includes(band)) band = (state.listen && choices.includes(state.listen.band) && state.listen.band) || (choices.includes('20m') ? '20m' : choices[0]);
    [freq, mode] = LISTEN_BANDS[band];
  }
  const card = state.card && state.card.bearing !== undefined ? state.card : null;
  state.listen = { band, freq, mode };
  $('listen-dlg').hidden = false;
  const chips = $('listen-bands');
  chips.replaceChildren();
  for (const b of listenBands()) {
    const chip = el('button', '', bandLabel(b));
    chip.type = 'button';
    chip.setAttribute('aria-pressed', String(b === band));
    chip.addEventListener('click', () => openListen(b));
    chips.append(chip);
  }
  const toward = card ? `, the ones toward ${card.call} first` : '';
  $('listen-sub').textContent = fromRadio
    ? `Your radio is on ${mhzText(freq)} MHz ${mode}. These receivers are a good distance away for ${bandLabel(band)}${toward}.`
    : r && r.ok ? `Receivers open on ${mhzText(freq)} MHz ${mode}. Tune your radio there too (or pick its band below)${toward}.`
      : `Your radio isn't connected, so pick a band. Receivers open on ${mhzText(freq)} MHz ${mode}${toward}.`;
  $('listen-list').replaceChildren();
  $('listen-foot').textContent = '';
  const wait = $('listen-wait');
  wait.hidden = false;
  wait.textContent = 'Finding receivers…';
  $('listen-close').focus();
  let data;
  try {
    data = await api.get(`/api/kiwi?freq=${freq}&mode=${encodeURIComponent(mode)}${card ? `&bearing=${Math.round(card.bearing)}` : ''}`);
  } catch (err) {
    wait.textContent = 'The receiver list didn’t load. Try again in a moment.';
    return;
  }
  if (!state.listen || state.listen.freq !== freq || $('listen-dlg').hidden) return;   // changed or closed meanwhile
  renderListen(data);
}

function renderListen(data) {
  const list = $('listen-list');
  list.replaceChildren();
  const found = data.ok ? data.receivers : [];
  const wait = $('listen-wait');
  wait.hidden = found.length > 0;
  if (!found.length) wait.textContent = `No free receivers found for ${bandLabel(state.listen.band)} right now. Try again in a few minutes.`;
  for (const r of found) {
    const item = el('li', 'listen-row' + (r.in_beam ? ' beam' : ''));
    const info = el('div');
    const name = el('div', 'l-name', r.name || r.loc);
    if (r.in_beam) name.append(el('span', 'l-tag', 'IN YOUR BEAM'));
    info.append(name, el('div', 'l-where', `${r.loc ? r.loc + ' · ' : ''}${fmtNum(r.km)} km to the ${r.dir} (${r.bearing}°)`),
      el('div', 'l-more', `${r.users} of ${r.users_max} listening now${r.antenna ? ' · antenna: ' + r.antenna : ''}`));
    const go = el('button', 'l-go', 'Listen');
    go.type = 'button';
    go.addEventListener('click', () => openLink({ id: `kiwi:${r.id}`, freq_hz: state.listen.freq, mode: state.listen.mode }));
    item.append(info, go);
    list.append(item);
  }
  $('listen-foot').textContent = data.ok && data.listed ? `From the public KiwiSDR list (${data.from}): ${fmtNum(data.listed)} receivers.` : '';
}

function closeListen() {
  $('listen-dlg').hidden = true;
  state.listen = null;
  $('search').focus();
}

/* ---------- Satellites: the ISS and the FM satellites ---------- */
function destPoint(lat, lon, bearingDeg, dist) {   // dist in radians of arc
  const p1 = lat * RAD, l1 = lon * RAD, b = bearingDeg * RAD;
  const p2 = Math.asin(Math.sin(p1) * Math.cos(dist) + Math.cos(p1) * Math.sin(dist) * Math.cos(b));
  const l2 = l1 + Math.atan2(Math.sin(b) * Math.sin(dist) * Math.cos(p1), Math.cos(dist) - Math.sin(p1) * Math.sin(p2));
  return [p2 / RAD, ((l2 / RAD + 540) % 360) - 180];
}

function polyline(points) {   // [[lat, lon], ...] -> SVG path, broken where it wraps round the map
  let d = '', prev = null;
  for (const [la, lo] of points) {
    const x = px(lo), y = py(la);
    d += `${prev === null || Math.abs(x - prev) > MAP_W / 2 ? 'M' : 'L'}${f1(x)} ${f1(y)} `;
    prev = x;
  }
  return d.trim();
}

function footRing(n) {   // the circle on the ground a satellite can be heard in
  const reach = Math.acos(6378.137 / (6378.137 + n.alt_km));
  const ring = [];
  for (let b = 0; b <= 360; b += 6) ring.push(destPoint(n.lat, n.lon, b, reach));
  return ring;
}

function drawFoot(prefix, snap) {   // footprint, ground track and marker on the flat map
  const n = snap.now;
  const foot = polyline(footRing(n));
  const wraps = foot.indexOf('M', 1) !== -1;
  $(`${prefix}-foot`).setAttribute('d', wraps ? foot : foot + ' Z');
  $(`${prefix}-foot`).setAttribute('fill-opacity', wraps ? '0' : (n.in_range ? '0.16' : '0.06'));
  $(`${prefix}-track`).setAttribute('d', polyline(snap.track.slice(10)));
  $(`${prefix}-mark`).setAttribute('transform', `translate(${f1(px(n.lon))} ${f1(py(n.lat))})`);
}

function drawIss(snap) {
  const layer = $('iss-layer');
  if (!snap || !snap.ok) { layer.setAttribute('opacity', '0'); Maps.setIss(null); return; }
  Maps.setIss(snap, footRing(snap.now), state.issOpen);
  drawFoot('iss', snap);
  $('iss-ring').setAttribute('opacity', state.issOpen ? '1' : '0');
  layer.setAttribute('opacity', '1');
}

// The FM satellite whose card is open (the ISS is always on the map).
function drawSat(d) {
  const layer = $('sat-layer');
  if (!d || d.id === 'ISS' || !d.snap || !d.snap.ok) { layer.setAttribute('opacity', '0'); Maps.setSat(null); return; }
  Maps.setSat(d.snap, footRing(d.snap.now), d.id);
  drawFoot('sat', d.snap);
  $('sat-label').textContent = d.id;
  layer.setAttribute('opacity', '1');
}

function passTime(iso) {
  const when = new Date(iso);
  const dayOf = (d) => timeIn(localTz(), { year: 'numeric', month: '2-digit', day: '2-digit' }, 'en-CA', d);
  const time = timeIn(localTz(), { hour: 'numeric', minute: '2-digit' }, 'en-CA', when);
  return dayOf(when) === dayOf(new Date()) ? time : `${timeIn(localTz(), { weekday: 'short' }, 'en-CA', when)} ${time}`;
}

function untilText(ms) {
  const mins = Math.max(1, Math.round(ms / 60000));
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60), m = mins % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

const fmtMHz = (mhz) => mhz.toFixed(3);
const step5 = (mhz) => Math.round(mhz * 200) / 200;   // FM radios tune in 5 kHz steps

async function refreshSats() {
  try { state.sats = await api.get('/api/sats'); } catch (err) { return; }
  renderSatPasses();
  checkSatAlerts();
}

function livePass() {   // a pass happening now, FM satellites first
  const now = Date.now();
  const up = ((state.sats && state.sats.passes) || []).filter((p) => new Date(p.aos).getTime() <= now && now <= new Date(p.los).getTime());
  return up.find((p) => p.sat !== 'ISS') || up[0] || null;
}

function renderSatPasses() {
  const list = $('pass-list');
  list.replaceChildren();
  const data = state.sats;
  const now = Date.now();
  const upcoming = data ? data.passes.filter((p) => new Date(p.aos).getTime() > now) : [];
  const live = livePass();
  const dark = new Set((data && data.sunlight_only) || []);
  const shown = upcoming.slice(0, live ? 3 : 4);
  list.classList.toggle('days', shown.some((p) => /^[A-Z][a-z]+\.? /.test(passTime(p.aos))));   // "Fri 1:56 p.m.": room for the day
  for (const p of shown) {
    const button = document.createElement('button');
    button.type = 'button';
    const dir = cell('p-dir', `${p.aos_dir} to ${p.los_dir}`);
    if (p.sat === 'ISS' && p.visible) dir.append(', ', cell('p-eye', 'visible'));
    if (dark.has(p.sat) && !p.sunlit) dir.append(', ', cell('p-dark', 'in shadow'));
    button.append(cell('p-when', passTime(p.aos)), cell('p-sat', p.sat), cell('p-el', `${p.max_el}°`), dir);
    button.title = `${p.sat} rises at ${passTime(p.aos)} in the ${p.aos_dir}, up to ${p.max_el}°, for ${p.minutes} minutes`;
    button.addEventListener('click', () => openSat(p.sat));
    const item = document.createElement('li');
    item.append(button);
    list.append(item);
  }
  const wait = $('pass-wait');
  wait.hidden = !!(data && upcoming.length);
  if (data && data.ready && !upcoming.length) wait.textContent = 'No passes above 10° in the next two days.';
  const line = $('iss-now');
  line.hidden = !live;
  if (live) {
    line.textContent = `${live.sat} is up now: rose in the ${live.aos_dir}, sets in the ${live.los_dir} in ${untilText(new Date(live.los) - now)}.`;
    line.onclick = () => openSat(live.sat);
  }
  $('pass-foot').hidden = !!live;
}

function setMetric(n, label, value, sub) {
  $(`i-l${n}`).textContent = label;
  $(`i-v${n}`).textContent = value;
  $(`i-s${n}`).textContent = sub;
}

function skyXY(az, el) {
  const r = 60 * (90 - Math.max(0, Math.min(90, el))) / 90, a = az * RAD;
  return [66 + r * Math.sin(a), 66 - r * Math.cos(a)];
}

// The pass drawn on a sky chart: the edge is the horizon, the middle is straight up, north at the top.
function drawSky(pass, now) {
  const track = pass && pass.track ? pass.track : [];
  const pts = track.map(([az, el]) => skyXY(az, el));
  $('i-sky-track').setAttribute('d', pts.map(([x, y], i) => `${i ? 'L' : 'M'}${f1(x)} ${f1(y)}`).join(' '));
  const aos = $('i-sky-aos'), los = $('i-sky-los'), dot = $('i-sky-now');
  if (pts.length > 1) {
    aos.setAttribute('cx', f1(pts[0][0]));
    aos.setAttribute('cy', f1(pts[0][1]));
    const [x2, y2] = pts[pts.length - 1], [x1, y1] = pts[pts.length - 2];
    const a = Math.atan2(y2 - y1, x2 - x1);
    const back = (d, side) => [x2 - d * Math.cos(a) + side * Math.sin(a), y2 - d * Math.sin(a) - side * Math.cos(a)];
    const [bx1, by1] = back(9, 5), [bx2, by2] = back(9, -5);
    los.setAttribute('d', `M${f1(x2)} ${f1(y2)} L${f1(bx1)} ${f1(by1)} L${f1(bx2)} ${f1(by2)} Z`);
    aos.removeAttribute('hidden');
    $('i-sky').setAttribute('aria-label', `${pass.sat || ''} rises in the ${pass.aos_dir}, reaches ${pass.max_el}° in the ${pass.peak_dir}, and sets in the ${pass.los_dir}`);
  } else {
    aos.setAttribute('hidden', '');
    los.setAttribute('d', '');
  }
  dot.hidden = !(now && now.el > 0);
  if (!dot.hidden) {
    const [x, y] = skyXY(now.az, now.el);
    dot.setAttribute('cx', f1(x));
    dot.setAttribute('cy', f1(y));
  }
}

function renderFreqs(d, live) {
  const box = $('i-freqs');
  box.replaceChildren();
  const row = (label, value, sub) => box.append(cell('f-label', label), cell('f-value', value), cell('f-sub', sub || ''));
  if (d.id === 'ISS') {
    row('Voice', '145.800 FM', 'the crew and SSTV pictures, when they’re on');
    row('Repeater', `${fmtMHz(d.down)} FM`, `listen here; talk on ${fmtMHz(d.up)}, tone ${d.tone} Hz`);
    return;
  }
  const s = d.steps;   // Doppler: the 70 cm side moves about 10 kHz during a pass, so tune it in 5 kHz steps
  const downMoves = d.down > 400, upMoves = d.up > 400;
  row('Listen', `${fmtMHz(d.down)} FM`, s && downMoves ? `${fmtMHz(step5(s[0].down))} rising, ${fmtMHz(step5(s[2].down))} setting` : '');
  row('Talk', `${fmtMHz(d.up)} FM`, `tone ${d.tone} Hz${s && upMoves ? `; ${fmtMHz(step5(s[0].up))} rising, ${fmtMHz(step5(s[2].up))} setting` : ''}`);
  if (live && d.tune) {
    box.append(cell('f-now', downMoves ? `Tune now: listen on ${fmtMHz(step5(d.tune.down))}` : `Tune now: talk on ${fmtMHz(step5(d.tune.up))}`));
  }
}

function renderAmsat(d) {
  const line = $('i-amsat');
  const s = d.status;
  const now = Date.now();
  line.classList.remove('good');
  if (!s || (!s.last_heard && !s.last_report)) { line.textContent = 'No reports on AMSAT’s status page in the last 4 days.'; return; }
  if (s.last_heard) {
    line.textContent = `AMSAT: heard ${agoText(now - new Date(s.last_heard).getTime())} (${s.heard} heard, ${s.not_heard} not heard in 4 days).`;
    line.classList.toggle('good', now - new Date(s.last_heard).getTime() < 72 * 3600 * 1000);
  } else {
    line.textContent = `AMSAT: not heard lately (last report ${agoText(now - new Date(s.last_report.time).getTime())}: “${s.last_report.report.toLowerCase()}”).`;
  }
}

function renderSatPicker(id) {
  const box = $('i-picker');
  const ids = (state.sats && state.sats.sats) || ['ISS'];
  if (box.children.length !== ids.length) {
    box.replaceChildren();
    for (const sat of ids) {
      const b = el('button', '', sat);
      b.type = 'button';
      b.dataset.sat = sat;
      b.addEventListener('click', () => openSat(sat));
      box.append(b);
    }
  }
  for (const b of box.children) b.setAttribute('aria-pressed', String(b.dataset.sat === id));
}

function renderSatCard(d) {
  const snap = d.snap;
  $('i-name').textContent = d.id;
  $('i-full').textContent = d.id === 'ISS' ? 'International Space Station' : `${d.full}, FM repeater`;
  renderSatPicker(d.id);
  const status = $('i-status'), eye = $('i-eye');
  eye.hidden = true;
  $('i-listen').textContent = d.note || '';
  renderAmsat(d);
  if (!snap || !snap.ok) {
    status.textContent = `Waiting for ${d.id}'s orbit data. It arrives within a few minutes when the internet is connected.`;
    status.classList.remove('live');
    for (const n of [1, 2, 3]) setMetric(n, '', '', '');
    drawSky(null, null);
    renderFreqs(d, false);
    return;
  }
  const n = snap.now;
  const lat = `${Math.abs(n.lat).toFixed(0)}° ${n.lat >= 0 ? 'N' : 'S'}`, lon = `${Math.abs(n.lon).toFixed(0)}° ${n.lon >= 0 ? 'E' : 'W'}`;
  $('i-name').title = `Now over ${lat}, ${lon}, ${fmtNum(n.alt_km)} km up, ${fmtNum(n.speed_kmh)} km/h`;
  const now = Date.now();
  const current = snap.passes.find((p) => new Date(p.aos).getTime() <= now && now <= new Date(p.los).getTime()) || null;
  const next = snap.passes.find((p) => new Date(p.aos).getTime() > now) || null;
  if (n.in_range) {
    status.textContent = d.id === 'ISS' ? 'In range now. Listen on 145.800 FM.' : `In range now. Listen on ${fmtMHz(d.down > 400 && d.tune ? step5(d.tune.down) : d.down)} FM.`;
    status.classList.add('live');
    setMetric(1, 'Look toward', n.dir, `${n.az}° on the compass`);
    setMetric(2, 'Height in the sky', `${Math.max(0, Math.round(n.el))}°`, current ? `highest ${current.max_el}°` : '');
    setMetric(3, 'Sets in', current ? untilText(new Date(current.los) - now) : '—', current ? `in the ${current.los_dir}` : '');
    drawSky(current, n);
  } else if (next) {
    status.textContent = `Next pass ${passTime(next.aos)}, in ${untilText(new Date(next.aos) - now)}.`;
    status.classList.remove('live');
    setMetric(1, 'Rises in the', next.aos_dir, `${next.aos_az}° on the compass`);
    setMetric(2, 'Highest', `${next.max_el}°`, `toward the ${next.peak_dir}`);
    setMetric(3, 'Lasts', `${next.minutes} min`, `sets in the ${next.los_dir}`);
    drawSky(next, null);
    if (d.id === 'ISS' && next.visible) {
      eye.hidden = false;
      eye.textContent = `You can see this pass with your own eyes. Look ${next.aos_dir} at ${passTime(next.aos)} for a bright, steady star crossing the sky.`;
    } else if (d.sunlight_only) {
      eye.hidden = false;
      eye.textContent = next.sunlit ? `This pass is in sunlight, so ${d.id} should be on.` : `This pass is in Earth's shadow, so ${d.id} will probably be off.`;
    }
  } else {
    status.textContent = 'No passes above 10° in the next two days.';
    status.classList.remove('live');
    for (const k of [1, 2, 3]) setMetric(k, '', '', '');
    drawSky(null, null);
  }
  renderFreqs(d, n.in_range);
}

async function refreshSatDetail() {
  clearTimeout(state.satTimer);
  const id = state.satId;
  if (!id) return;
  let d = null;
  try { d = await api.get(`/api/sat?id=${encodeURIComponent(id)}`); } catch (err) { /* tried again shortly */ }
  if (state.satId !== id) return;   // closed, or another satellite picked meanwhile
  if (d) {
    state.satDetail = d;
    renderSatCard(d);
    drawSat(d);
  }
  const live = d && d.snap && d.snap.ok && d.snap.now.in_range;
  state.satTimer = setTimeout(refreshSatDetail, live ? 2000 : 15000);
}

async function openSat(id) {
  state.card = null;
  showOnMap(null);
  showView('card-iss');
  const first = state.satId !== id;
  state.satId = id;
  state.issOpen = id === 'ISS';
  drawIss(state.iss);
  if (first) drawSat(null);
  await refreshSatDetail();
  if (id === 'ISS') Maps.focusIss(); else Maps.focusSat();
}

function openIss() { openSat('ISS'); }

function closeSat() {
  clearTimeout(state.satTimer);
  state.satId = null;
  state.satDetail = null;
  drawSat(null);
}

function checkIssAlert(snap) {
  if (!snap || !snap.ok) return;
  const now = Date.now();
  for (const p of snap.passes) {
    const lead = new Date(p.aos).getTime() - now;
    if (lead > 0 && lead <= 5 * 60000 && !state.issAlerted.has(p.aos)) {
      state.issAlerted.add(p.aos);
      showToast(`The ISS rises in ${untilText(lead)} in the ${p.aos_dir}, up to ${p.max_el}°. Listen on 145.800 FM.${p.visible ? ' You may see it too.' : ''}`, null, true);
    }
  }
}

// A heads-up for good FM satellite passes (30° and up, by day, satellites heard lately), 5 minutes before.
function checkSatAlerts() {
  const data = state.sats;
  if (!data) return;
  const now = Date.now();
  const hour = Number(timeIn(localTz(), { hour: '2-digit', hourCycle: 'h23' }, 'en-GB'));
  if (hour < 7 || hour >= 22) return;
  for (const p of data.passes) {
    if (p.sat === 'ISS' || p.max_el < 30) continue;
    const info = (data.info || {})[p.sat] || {};
    const heard = info.last_heard && now - new Date(info.last_heard).getTime() < 72 * 3600 * 1000;
    if (!heard || (info.sunlight_only && !p.sunlit)) continue;
    const lead = new Date(p.aos).getTime() - now;
    const key = `${p.sat} ${p.aos}`;
    if (lead > 0 && lead <= 5 * 60000 && !state.issAlerted.has(key)) {
      state.issAlerted.add(key);
      showToast(`${p.sat} rises in ${untilText(lead)} in the ${p.aos_dir}, up to ${p.max_el}°. Listen on ${fmtMHz(info.down)} FM, talk on ${fmtMHz(info.up)} with a ${info.tone} Hz tone.`, null, true);
    }
  }
}

async function refreshIss() {
  try { state.iss = await api.get('/api/iss'); } catch (err) { return; }
  drawIss(state.iss);
  checkIssAlert(state.iss);
}

/* ---------- Tabs: On the air / My log ---------- */
function showTab(name) {
  state.tab = name;
  for (const [tab, panel] of [['tab-spots', 'panel-spots'], ['tab-log', 'panel-log']]) {
    const on = panel === `panel-${name}`;
    $(tab).setAttribute('aria-selected', String(on));
    $(panel).hidden = !on;
  }
  $('voice-toggle').hidden = name !== 'spots';
}

/* ---------- Updates: reload the page when a new version is installed ---------- */
async function checkVersion() {
  try {
    const h = await api.get('/api/health');
    if ((state.version && h.version !== state.version) || (state.boot && h.boot !== state.boot)) location.reload();
    state.boot = state.boot || h.boot;
  } catch (err) { /* the app may be restarting */ }
}

/* ---------- Keyboard: start typing a callsign anywhere ---------- */
function setupInput() {
  const input = $('search');
  $('search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    lookUp(input.value);
    input.select();
  });
  input.addEventListener('input', () => {
    const clean = input.value.toUpperCase().replace(/[^A-Z0-9/]/g, '');
    if (clean !== input.value) input.value = clean;
  });
  document.addEventListener('keydown', (e) => {
    if (!$('stats-dlg').hidden) {
      if (e.key === 'Escape') { closeStats(); return; }
      if (/^[a-z0-9/]$/i.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) closeStats();   // typing a callsign
      else return;
    }
    if (!$('fav').hidden) {
      if (e.key === 'Escape') { closeFavourites(); return; }
      if (/^[a-z0-9/]$/i.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) closeFavourites();   // typing a callsign
      else return;
    }
    if (!$('listen-dlg').hidden) {
      if (e.key === 'Escape') { closeListen(); return; }
      if (/^[a-z0-9/]$/i.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) closeListen();   // typing a callsign
      else return;
    }
    if (e.key === 'Escape') {
      if (!$('card-close').hidden) clearCard();
      else if (state.openBand) hideOpenings();
      else { input.value = ''; input.focus(); }
      return;
    }
    if (e.target === input || e.ctrlKey || e.altKey || e.metaKey) return;
    if (/^[a-z0-9/]$/i.test(e.key)) {
      e.preventDefault();
      input.value = e.key.toUpperCase();
      input.focus();
    }
  });
  $('btn-log').addEventListener('click', logContact);
  $('btn-tune').addEventListener('click', tuneToSpot);
  $('my-call').addEventListener('click', () => { if (state.cfg) Welcome.play(state.cfg); });
  $('checklist-link').addEventListener('click', openChecklist);
  $('radio-name').addEventListener('click', pickOtherRadio);
  $('log-band').addEventListener('change', () => { state.logBandTouched = true; });
  $('iss-mark').addEventListener('click', openIss);
  $('iss-show').addEventListener('click', openIss);
  $('tab-spots').addEventListener('click', () => showTab('spots'));
  $('tab-log').addEventListener('click', () => showTab('log'));
  $('voice-toggle').addEventListener('click', () => {
    state.voice = !state.voice;
    $('voice-toggle').setAttribute('aria-pressed', String(state.voice));
    refreshSpots();
  });
  $('toast-undo').addEventListener('click', undoLog);
  $('btn-fav').addEventListener('click', openFavourites);
  $('fav-close').addEventListener('click', closeFavourites);
  $('fav').addEventListener('click', (e) => { if (e.target === $('fav')) closeFavourites(); });
  $('btn-clear').addEventListener('click', clearCard);
  $('zones-pill').addEventListener('click', () => openStats('zones', state.cfg && state.cfg.cq_zone));   // starts at the home zone
  $('btn-stats').addEventListener('click', () => openStats('overview'));
  $('log-stats-btn').addEventListener('click', () => openStats('overview'));
  $('stats-close').addEventListener('click', closeStats);
  $('stats-dlg').addEventListener('click', (e) => { if (e.target === $('stats-dlg')) closeStats(); });
  for (const name of STATS_TABS) $(`st-tab-${name}`).addEventListener('click', () => showStatsTab(name));
  $('stats-dlg').querySelector('.stats-tabs').addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const next = STATS_TABS[(STATS_TABS.indexOf(state.statsTab) + (e.key === 'ArrowRight' ? 1 : STATS_TABS.length - 1)) % STATS_TABS.length];
    showStatsTab(next);
    $(`st-tab-${next}`).focus();
  });
  for (const b of document.querySelectorAll('.ct-filter button')) {
    b.addEventListener('click', () => { state.ctShow = b.dataset.show; $('ct-groups').scrollTop = 0; renderCountries(); });
  }
  $('alert-more').addEventListener('click', () => { if (state.alertShown) openLink({ id: `alert:${state.alertShown.id}` }); });
  $('alert-next').addEventListener('click', () => {
    state.alertAt = ((state.alertAt || 0) + 1) % Math.max(1, bannerAlerts().length);
    renderAlerts();
  });
  $('alert-close').addEventListener('click', hideAlert);
  $('btn-listen').addEventListener('click', () => openListen());
  $('listen-close').addEventListener('click', closeListen);
  $('listen-dlg').addEventListener('click', (e) => { if (e.target === $('listen-dlg')) closeListen(); });
  $('open-chip-close').addEventListener('click', hideOpenings);
  $('space-chip').addEventListener('click', () => openLink({ id: 'alert:space' }));
  $('card-close').addEventListener('click', clearCard);
  for (const b of $('views').querySelectorAll('button')) b.addEventListener('click', () => pickMapView(b.dataset.view));
}

async function start() {
  try {
    state.cfg = await api.get('/api/config');
  } catch (err) {
    setTimeout(start, 3000);   // the app is still starting
    return;
  }
  state.version = state.cfg.version;
  maybeWelcome();
  if (Array.isArray(state.cfg.bands) && state.cfg.bands.length) BANDS = state.cfg.bands;
  if (!BANDS.includes(state.band)) state.band = BANDS.includes('20m') ? '20m' : BANDS[0];
  buildLogBands();
  state.voice = state.cfg.voice_only;
  $('voice-toggle').setAttribute('aria-pressed', String(state.voice));
  $('my-call').textContent = state.cfg.callsign;
  tickClocks();
  drawStation();
  drawGrayLine();
  setupZoneLabels();
  loadZones().then(markZones);
  startMaps();
  pollRadio();
  refreshLog();
  refreshEvents();
  refreshSpots();
  refreshHeard();
  refreshConditions();
  refreshAlerts();
  refreshIss();
  refreshSats();
  refreshOpenings();
  loadFavourites();
  refreshChecklist();
  checkVersion();
  setInterval(refreshIss, 10 * 1000);
  setInterval(refreshSats, 60 * 1000);
  setInterval(refreshOpenings, 30 * 1000);
  setInterval(refreshSpots, 20 * 1000);
  setInterval(refreshHeard, 30 * 1000);
  setInterval(refreshConditions, 10 * 60 * 1000);
  setInterval(refreshAlerts, 60 * 1000);
  setInterval(drawGrayLine, 30 * 1000);
  setInterval(pollRadio, 1000);
  setInterval(refreshLog, 60 * 1000);
  setInterval(refreshEvents, 60 * 60 * 1000);
  setInterval(checkVersion, 15 * 1000);
  setInterval(refreshChecklist, 60 * 1000);
}

setupInput();
tickClocks();
setInterval(tickClocks, 1000);
start();
