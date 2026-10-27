/* The tester checklist. Every tick, choice and note is sent to the station app as soon as
   it's made (notes a moment after typing stops), and kept in ~/station/data/checklist.json. */
const $ = (id) => document.getElementById(id);
const state = { data: null, show: 'all', pending: new Map() };
const SVG = 'http://www.w3.org/2000/svg';

async function get(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
async function post(body, keepalive = false) {
  const r = await fetch('/api/checklist', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive,
  });
  const reply = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(reply.error || String(r.status));
  return reply;
}

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};
function icon(paths, cls) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', '0 0 20 20');
  svg.setAttribute('aria-hidden', 'true');
  if (cls) svg.setAttribute('class', cls);
  for (const [d, extra] of paths) {
    const p = document.createElementNS(SVG, 'path');
    p.setAttribute('d', d);
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', (extra && extra.w) || '1.8');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    if (extra && extra.cls) p.setAttribute('class', extra.cls);
    svg.append(p);
  }
  return svg;
}
const BOX = [['M5 2.5 H15 A2.5 2.5 0 0 1 17.5 5 V15 A2.5 2.5 0 0 1 15 17.5 H5 A2.5 2.5 0 0 1 2.5 15 V5 A2.5 2.5 0 0 1 5 2.5 Z'],
  ['M6 10.2 L8.8 13 L14.2 7.2', { w: '2.2', cls: 'check' }]];
const THUMB = [['M6.5 9 L9.5 2.8 C10.7 2.8 11.5 3.8 11.2 5 L10.6 7.5 H15.4 C16.6 7.5 17.4 8.6 17.1 9.7 L15.6 15.3 C15.4 16.1 14.7 16.7 13.8 16.7 H6.5 Z'],
  ['M2.8 9 H6.5 V16.7 H2.8 Z']];
const PENCIL = [['M12.8 3.6 L16.4 7.2 L7.2 16.4 L3 17 L3.6 12.8 Z'], ['M11 5.4 L14.6 9']];

function timeText(t) {
  const when = new Date(t * 1000);
  const today = new Date().toDateString() === when.toDateString();
  const clock = when.toLocaleTimeString('en-CA', { hour: 'numeric', minute: '2-digit' });
  return today ? clock : `${when.toLocaleDateString('en-CA', { month: 'short', day: 'numeric' })}, ${clock}`;
}

function response(id) { return state.data.responses[id] || {}; }

function paint(id) {
  const card = $(`item-${id}`);
  if (!card) return;
  const r = response(id);
  const found = !!(r.found || r.tried), tried = !!r.tried;
  card.querySelector('[data-field="found"]').setAttribute('aria-pressed', String(found));
  card.querySelector('[data-field="tried"]').setAttribute('aria-pressed', String(tried));
  card.querySelector('[data-verdict="like"]').setAttribute('aria-pressed', String(r.verdict === 'like'));
  card.querySelector('[data-verdict="change"]').setAttribute('aria-pressed', String(r.verdict === 'change'));
  card.querySelector('.done-mark').style.display = tried ? '' : 'none';   // an svg has no hidden property
  card.dataset.tried = String(tried);
  card.dataset.verdict = r.verdict || '';
}

function paintSummary() {
  const s = state.data.summary;
  $('bar-fill').style.width = `${Math.round((100 * s.tried) / Math.max(1, s.items))}%`;
  const parts = [`Tried ${s.tried} of ${s.items}`];
  if (s.found > s.tried) parts.push(`found ${s.found}`);
  if (s.liked) parts.push(`${s.liked} liked`);
  if (s.change) parts.push(`${s.change} ${s.change === 1 ? 'needs' : 'need'} a change`);
  $('progress-text').textContent = parts.join(' · ');
}

function applyFilter() {
  let shown = 0;
  for (const section of document.querySelectorAll('.section-block')) {
    let any = false;
    for (const card of section.querySelectorAll('.item')) {
      const keep = state.show === 'all' || (state.show === 'todo' && card.dataset.tried !== 'true')
        || (state.show === 'change' && card.dataset.verdict === 'change');
      card.hidden = !keep;
      if (keep) { any = true; shown++; }
    }
    section.hidden = !any;
  }
  const empty = $('empty');
  empty.hidden = shown > 0;
  empty.textContent = state.show === 'todo' ? 'Everything has been tried. Thank you!' : 'Nothing is marked "Needs a change".';
  for (const b of document.querySelectorAll('.filters button')) b.setAttribute('aria-pressed', String(b.dataset.show === state.show));
}

function setSaved(node, ok, t) {
  node.classList.toggle('bad', !ok);
  node.textContent = ok ? `Saved ${timeText(t || Date.now() / 1000)}` : 'Not saved yet. Is the station display running?';
}

async function save(id, change, savedNode) {
  try {
    const reply = await post(Object.assign({ id }, change));
    if (id === 'general') state.data.general = reply.response;
    else state.data.responses[id] = reply.response;
    state.data.summary = reply.summary;
    if (id !== 'general') paint(id);
    paintSummary();
    applyFilter();
    setSaved(savedNode, true, reply.response.updated);
    return true;
  } catch (err) {
    setSaved(savedNode, false);
    return false;
  }
}

// Notes are saved a moment after typing stops, when the box is left, and when the window closes.
function watchNote(id, area, savedNode) {
  const flush = (keepalive) => {
    const job = state.pending.get(id);
    if (!job) return;
    clearTimeout(job.timer);
    state.pending.delete(id);
    if (keepalive) post({ id, comment: area.value }, true).catch(() => {});
    else save(id, { comment: area.value }, savedNode);
  };
  area.addEventListener('input', () => {
    const job = state.pending.get(id) || {};
    clearTimeout(job.timer);
    job.flush = flush;
    job.timer = setTimeout(() => flush(false), 900);
    state.pending.set(id, job);
    savedNode.classList.remove('bad');
    savedNode.textContent = 'Typing…';
  });
  area.addEventListener('blur', () => flush(false));
}

function itemCard(item) {
  const r = response(item.id);
  const card = el('article', 'item');
  card.id = `item-${item.id}`;
  const head = el('div', 'item-head');
  const mark = icon([['M4 10.5 L8.2 14.5 L16 5.5', { w: '2.6' }]], 'done-mark');
  head.append(el('h3', '', item.title), mark);
  if (item.new) head.append(el('span', 'tag new', 'New'));
  if (item.when) head.append(el('span', 'tag wait', item.when));
  const where = el('p', 'where');
  where.append(el('span', 'label', 'Where'), item.where);
  const how = el('p', 'try');
  how.append(el('span', 'label', 'Try it'), item.try);
  const controls = el('div', 'controls');
  const ticks = el('div', 'ticks');
  const saved = el('p', 'saved');
  saved.setAttribute('aria-live', 'polite');
  for (const [field, text] of [['found', 'Found it'], ['tried', 'Tried it']]) {
    const b = el('button', 'tick');
    b.type = 'button';
    b.dataset.field = field;
    b.append(icon(BOX, 'box'), text);
    b.addEventListener('click', () => {
      const cur = response(item.id);
      const on = b.getAttribute('aria-pressed') !== 'true';
      // Tried it means found it; taking Found it away takes Tried it away too.
      const change = field === 'tried' ? { tried: on } : (on ? { found: true } : { found: false, tried: false });
      if (field === 'tried' && on && !cur.found) change.found = true;
      save(item.id, change, saved);
    });
    ticks.append(b);
  }
  const verdict = el('div', 'verdict');
  verdict.setAttribute('role', 'group');
  verdict.setAttribute('aria-label', `What do you think of ${item.title}?`);
  for (const [value, text, paths] of [['like', 'Like it', THUMB], ['change', 'Needs a change', PENCIL]]) {
    const b = el('button', `vote ${value}`);
    b.type = 'button';
    b.dataset.verdict = value;
    b.append(icon(paths), text);
    b.addEventListener('click', () => {
      const cur = response(item.id);
      save(item.id, { verdict: cur.verdict === value ? '' : value }, saved);
      if (value === 'change' && cur.verdict !== 'change') card.querySelector('textarea').focus();
    });
    verdict.append(b);
  }
  controls.append(ticks, verdict);
  const label = el('label', 'note-label', 'Notes');
  label.htmlFor = `note-${item.id}`;
  const area = el('textarea');
  area.id = `note-${item.id}`;
  area.rows = 2;
  area.maxLength = 2000;
  area.placeholder = 'Anything you’d change, or anything that didn’t work?';
  area.value = r.comment || '';
  if (r.updated) saved.textContent = `Saved ${timeText(r.updated)}`;
  watchNote(item.id, area, saved);
  card.append(head, where, how, controls, label, area, saved);
  return card;
}

function render() {
  const list = $('list');
  list.replaceChildren();
  for (const section of state.data.sections) {
    const block = el('section', 'section-block');
    block.append(el('h2', 'section', section.title));
    for (const item of section.items) block.append(itemCard(item));
    list.append(block);
  }
  for (const section of state.data.sections) for (const item of section.items) paint(item.id);
  const general = $('general');
  general.value = state.data.general.comment || '';
  if (state.data.general.updated) $('general-saved').textContent = `Saved ${timeText(state.data.general.updated)}`;
  $('general-box').hidden = false;
  paintSummary();
  applyFilter();
}

async function start() {
  try {
    const [data, cfg] = await Promise.all([get('/api/checklist'), get('/api/config').catch(() => ({}))]);
    state.data = data;
    if (cfg.operator) $('who').textContent = cfg.operator;
    if (cfg.callsign) $('kicker').textContent = `${cfg.callsign} station`;
    if (cfg.helper) $('helper-line').textContent = `, ready for ${cfg.helper} to read`;
  } catch (err) {
    $('progress-text').textContent = 'The station app isn’t answering. Check that the station display is running, then press F5 to try again.';
    return;
  }
  render();
  watchNote('general', $('general'), $('general-saved'));
  for (const b of document.querySelectorAll('.filters button')) {
    b.addEventListener('click', () => { state.show = b.dataset.show; applyFilter(); });
  }
  const flushAll = () => { for (const job of [...state.pending.values()]) job.flush(true); };
  window.addEventListener('pagehide', flushAll);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushAll(); });
}

start();
