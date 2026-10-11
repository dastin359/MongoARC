'use strict';
// mongoARC live view. Vanilla JS, no build step; everything shown comes from the read-only /api.

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const fmt = (v, d = 2) => (typeof v === 'number' && isFinite(v) ? v.toFixed(d) : '-');
const enc = encodeURIComponent;
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw Object.assign(new Error(r.status + ' ' + path), { status: r.status });
  return r.json();
}

const FRAME_MS = 60, STEP_GAP_MS = 220, PAGE = 100, EV_PAGE = 500, SPEEDS = [1, 2, 4, 8, 16];
// ARC palette accents, ordered so neighbouring generations stay distinct under colour-vision deficiency.
const GEN_COLORS = ['#1E93FF', '#FFDC00', '#E53AA3', '#4FCC30', '#A356D6', '#FF851B'];
const genColor = (g) => (Number.isInteger(g) ? GEN_COLORS[g % GEN_COLORS.length] : '#8a94a3');

const q = new URLSearchParams(location.search);
const S = {
  tab: ['evolution', 'memory'].includes(q.get('tab')) ? q.get('tab') : 'live',
  mode: ['live', 'replay'].includes(q.get('mode')) ? q.get('mode') : (q.get('run') ? 'auto' : 'live'),
  speed: SPEEDS.includes(+q.get('speed')) ? +q.get('speed') : 1,
  playing: q.get('autoplay') === '1',
  follow: !q.get('run'), wantRun: q.get('run'),
  wantPos: /^\d+$/.test(q.get('n') || '') ? +q.get('n') : null,
  runs: [], runsLoaded: false, runId: null, run: null,
  actions: [], stats: [], statTail: null, statN: -1, statsOk: true, base: 0, loadedTo: -1, pos: -1,
  events: [], lastSeq: 0, feedStart: 0, feedEnd: 0, feedNone: true, feedPinned: true,
  rulebooks: [], rbKey: null, rbDirty: true, liveInit: false, settled: false,
  lessons: [], gens: [], evoSig: '', evoDirty: true, evoPending: false, sig: {},
  epoch: 0,   // bumped when the run or mode changes; stale async work checks it
  play: 0,    // bumped on every seek; stale animation callbacks check it
  stepOnce: false, polling: false, pollN: 0,
};

/* ---------- board ---------- */
const cv = $('#board'), ctx = cv.getContext('2d');
const HEXV = new Uint8Array(128);
for (let i = 0; i < 16; i++) {
  HEXV['0123456789abcdef'.charCodeAt(i)] = i;
  HEXV['0123456789ABCDEF'.charCodeAt(i)] = i;
}
let rgb = [], img = null;

function setPalette(p) {
  rgb = p.map((h) => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]);
}
function drawFrame(hex) {
  const side = Math.round(Math.sqrt(hex.length)) || 64;
  if (!img || cv.width !== side) { cv.width = cv.height = side; img = ctx.createImageData(side, side); }
  const d = img.data;
  for (let i = 0, p = 0; i < side * side; i++, p += 4) {
    const c = rgb[HEXV[hex.charCodeAt(i) & 127]] || rgb[5] || [0, 0, 0];
    d[p] = c[0]; d[p + 1] = c[1]; d[p + 2] = c[2]; d[p + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  $('#boardEmpty').hidden = true;
}
function clearBoard(text) {
  $('#boardEmpty').textContent = text;
  $('#boardEmpty').hidden = false;
  $('#marker').hidden = true;
  $('#flash').className = 'flash';
}
function drawBoardAt(n) {
  for (let k = n; k >= S.base; k--) {  // a wasted action has no frame: the board carries over
    const f = S.actions[k] && S.actions[k].frames;
    if (f && f.length) return drawFrame(f[f.length - 1]);
  }
  if (S.run) clearBoard(S.loadedTo < 0 ? `Waiting for the first action of ${S.runId}.` : 'No board at this position.');
}
function showMarker(a) {
  const m = $('#marker'), p = a && a.params;
  if (!a || a.action !== 'ACTION6' || !p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) { m.hidden = true; return; }
  const side = cv.width || 64;
  m.style.left = ((p.x + 0.5) / side) * 100 + '%';
  m.style.top = ((p.y + 0.5) / side) * 100 + '%';
  m.hidden = true; void m.offsetWidth; m.hidden = false;
}
function flash(kind, text) {
  const f = $('#flash');
  f.className = 'flash'; void f.offsetWidth;
  f.textContent = text; f.className = 'flash ' + kind;
}

const AC = window.ArcController || new Proxy({}, { get: () => () => {} });

/* ---------- playback ---------- */
let timer = null, busy = false;

function stopPlay() { S.play++; busy = false; clearTimeout(timer); timer = null; }
function kick() { if (!busy && timer == null) timer = setTimeout(stepPlay, 0); }
function runSettled() { return !!S.run && S.run.status !== 'running' && S.loadedTo >= (S.run.n_actions || 0) - 1; }
function nextIndex() {
  let n = S.pos + 1;
  while (n <= S.loadedTo && !S.actions[n]) n++;
  return n <= S.loadedTo ? n : -1;
}
function stepPlay() {
  timer = null;
  const live = S.mode === 'live';
  if (!live && !S.playing && !S.stepOnce) return;
  const n = nextIndex();
  if (n < 0) {
    S.stepOnce = false;
    if (!live && S.playing && runSettled()) setPlaying(false);
    return;
  }
  const backlog = S.loadedTo - S.pos;
  if (live && backlog > 60) return seek(S.loadedTo - 4);  // far behind: jump, then play the last few
  const speed = live ? (backlog > 3 ? Math.min(24, backlog) : 1) : S.speed;
  const tok = S.play;
  busy = true;
  animate(S.actions[n], speed, tok, () => {
    busy = false; S.pos = n; onPos(true);
    const once = S.stepOnce; S.stepOnce = false;
    if (live || (S.playing && !once)) timer = setTimeout(stepPlay, STEP_GAP_MS / speed);
  });
}
function animate(a, speed, tok, done) {
  const frames = a.frames || [], dt = FRAME_MS / speed;
  const finish = () => { if (tok === S.play) done(); };
  showMarker(a);
  AC.setSpeed(speed); AC.setAvailable(a.available_actions); AC.press(a.action, a.params);
  if (!frames.length) return void setTimeout(finish, dt);
  if (frames.length === 1 || dt < 5) { drawFrame(frames[frames.length - 1]); return void setTimeout(finish, 0); }
  let i = 0;
  (function next() {
    if (tok !== S.play) return;
    drawFrame(frames[i++]);
    if (i < frames.length) setTimeout(next, dt); else done();
  })();
}
function syncController() {  // rebuild pad counters, tape and last click for the current position, without animating
  const rows = [];
  for (let k = Math.max(S.base, 0); k <= S.pos; k++) if (S.actions[k]) rows.push(S.actions[k]);
  AC.load(rows);
  const a = S.actions[S.pos]; if (a) AC.setAvailable(a.available_actions);
}
function seek(n) {
  stopPlay();
  S.pos = clamp(n, S.base - 1, S.loadedTo);
  drawBoardAt(S.pos);
  showMarker(S.actions[S.pos]);
  syncController();
  onPos(false);
  if (S.mode === 'live' || S.playing) kick();
}
function setPlaying(on) {
  S.playing = on;
  $('#btnPlay').textContent = on ? 'PAUSE' : 'PLAY';
  if (!on) return;
  if (runSettled() && S.pos >= S.loadedTo) seek(S.base - 1); else kick();
}
function onPos(played) {
  const a = S.actions[S.pos];
  if (played && a) {
    if (a.state === 'WIN') flash('win', 'WIN');
    else if (a.level_cleared) flash('clear', `LEVEL ${a.level - 1} CLEARED`);
    else if (a.death) flash('death', 'DEATH');
  } else if (!played) $('#flash').className = 'flash';
  renderHud(); syncScrub(); syncFeed(); syncRulebook();
}

/* ---------- data ---------- */
function pushStats(a) {  // counters after row a.n; rows must arrive in order from n = 0
  if (a.n !== S.statN + 1) { if (a.n > S.statN) S.statsOk = false; return; }
  const prev = S.statTail;
  const opener = a.action === 'RESET' && (!prev || a.full_reset || a.new_play);
  let costs = prev && !a.new_play ? prev.costs : [];
  const from = prev && !a.new_play ? prev.level : 0;
  let used = prev ? prev.used : 0;
  if (!opener) { costs = costs.slice(); costs[from] = (costs[from] || 0) + 1; used++; }
  S.statN = a.n;
  S.statTail = S.stats[a.n] = { level: a.level, state: a.state, used, costs, deaths: (prev ? prev.deaths : 0) + (a.death ? 1 : 0) };
}
function ingestActions(list) {
  for (const a of list) {
    if (!Number.isInteger(a.n) || a.n <= S.loadedTo) continue;
    S.actions[a.n] = a; S.loadedTo = a.n;
    pushStats(a);
  }
}
// Rows 0..upto-1 without their boards. A run joined part-way shows only its last few boards, but the
// counters (actions used, per-level costs, deaths) need every row, so live and replay agree.
async function loadHistory(ep, id, upto) {
  let after = -1;
  while (after < upto - 1) {
    const list = await api(`/api/runs/${enc(id)}/actions?after_n=${after}&limit=${Math.min(500, upto - 1 - after)}&frames=0`);
    if (ep !== S.epoch) return;
    if (!list.length) break;
    for (const a of list) if (Number.isInteger(a.n) && a.n < upto) pushStats(a);
    after = list[list.length - 1].n;
  }
  if (S.statN !== upto - 1) S.statsOk = false;
}
function ingestEvents(list) {
  for (const e of list) {
    if (!(e.seq > S.lastSeq)) continue;
    const tail = S.events.length ? S.events[S.events.length - 1]._n : -1;
    e._n = Number.isInteger(e.action_n) ? Math.max(tail, e.action_n) : tail;  // keep the feed monotonic
    S.events.push(e); S.lastSeq = e.seq;
    if (e.kind === 'rulebook') S.rbDirty = true;
  }
}

async function selectRun(id, mode) {
  const ep = ++S.epoch;
  stopPlay();
  Object.assign(S, { runId: id, run: null, actions: [], stats: [], statTail: null, statN: -1, statsOk: true,
    base: 0, loadedTo: -1, pos: -1, events: [], lastSeq: 0, feedStart: 0, feedEnd: 0, feedNone: true, feedPinned: true,
    rulebooks: [], rbKey: null, rbDirty: true, settled: false, pollN: 0, sig: {} });
  if (mode) S.mode = mode;
  AC.reset();
  $('#feedIn').replaceChildren();
  clearBoard(`Loading ${id}`);
  let r, base = 0;
  try {
    r = await api(`/api/runs/${enc(id)}`);
    if (ep !== S.epoch) return;
    if (S.mode === 'auto') S.mode = r.status === 'running' ? 'live' : 'replay';
    if (S.mode === 'live') {  // tail a run in flight: its last few boards, and every earlier row without its board
      base = Math.max(0, (r.n_actions || 0) - 12);
      if (base > 0) await loadHistory(ep, id, base);
    }
  } catch (e) {
    if (ep !== S.epoch) return;
    S.runId = null;
    if (e.status !== 404) {  // a failed request, not a missing run: leave the run unselected so the next tick retries
      if (!S.follow) S.wantRun = id;
      clearBoard(`Could not load ${id}. Retrying.`);
      throw e;
    }
    S.follow = true;
    clearBoard(`Run not found: ${id}`);
    return;
  }
  if (ep !== S.epoch) return;
  if (S.mode === 'live') {
    S.base = base;
    S.lastSeq = Math.max(0, (r.last_seq || 0) - 250);
    S.liveInit = true;
  }
  S.loadedTo = S.pos = S.base - 1;
  S.run = r;
  clearBoard(`Waiting for the first action of ${id}.`);
  renderMode(); renderPicker(); renderHud(); renderHarness(); syncFeed(); syncRulebook(); syncUrl();
  ensureEvolution(r);
  await pollRun();
}
// A run launched under a generation or with lessons the page has not loaded yet: fetch them now, not on the next slow tick.
function ensureEvolution(r) {
  if (!r || !Number.isInteger(r.generation)) return;
  const known = new Set(S.lessons.map((l) => l._id));
  if (S.gens.some((g) => g._id === r.generation) && (r.lessons_used || []).every((id) => known.has(id))) return;
  S.evoPending = true;
  renderHarness();
  refreshEvolution().catch(console.warn).finally(() => { S.evoPending = false; renderHarness(); });
}

async function pollRun() {
  if (S.polling || !S.runId || !S.run) return;
  S.polling = true;
  const ep = S.epoch, id = S.runId, n = S.pollN++;
  try {
    if (S.settled && n % 8) return;
    if (n > 0) {
      const r = await api(`/api/runs/${enc(id)}`);
      if (ep !== S.epoch) return;
      S.run = r;
    }
    let got = 0;
    for (let page = 0; page < 5; page++) {
      const list = await api(`/api/runs/${enc(id)}/actions?after_n=${S.loadedTo}&limit=${PAGE}`);
      if (ep !== S.epoch) return;
      ingestActions(list); got += list.length;
      afterActions();
      if (list.length < PAGE) break;
    }
    for (let page = 0; page < 5; page++) {
      const list = await api(`/api/runs/${enc(id)}/events?after_seq=${S.lastSeq}&limit=${EV_PAGE}`);
      if (ep !== S.epoch) return;
      ingestEvents(list); got += list.length;
      if (list.length < EV_PAGE) break;
    }
    if (S.rbDirty || n % 6 === 0) {
      const rb = await api(`/api/runs/${enc(id)}/rulebooks`);
      if (ep !== S.epoch) return;
      S.rulebooks = rb; S.rbDirty = false; S.rbKey = null;
    }
    S.settled = got === 0 && runSettled();
    renderHud(); renderHarness(); syncScrub(); syncFeed(); syncRulebook(); renderMode();
  } finally { S.polling = false; }
}
function afterActions() {
  if (S.loadedTo < 0) return;
  if (S.mode === 'live' && S.liveInit) { S.liveInit = false; return seek(S.loadedTo); }
  if (S.mode === 'replay' && S.wantPos != null) {
    if (S.loadedTo < S.wantPos && !runSettled()) return syncScrub();  // not paged in yet
    const n = S.wantPos; S.wantPos = null;
    return seek(n);
  }
  if (S.mode === 'replay' && S.pos < 0 && !S.playing && !busy) return seek(0);
  syncScrub(); kick();
}

async function refreshRuns() {
  S.runs = await api('/api/runs');
  S.runsLoaded = true;
  renderPicker();
  markEvolution();
  if (S.wantRun) { const id = S.wantRun; S.wantRun = null; return selectRun(id); }
  // A run still marked running that has not moved since a later run started (and ended) is a killed game, not a live one.
  const at = (v) => Date.parse(v) || 0;
  const dead = (r, i) => S.runs.slice(0, i).some((s) => s.status !== 'running' && at(r.updated_at) < at(s.started_at));
  const running = S.runs.find((r, i) => r.status === 'running' && !dead(r, i));
  const pick = running || S.runs[0];
  if (!S.runs.length && !S.runId) clearBoard('No runs yet. Launch a game and it will appear here.');
  if (S.follow && pick && (S.runId == null || (running && running._id !== S.runId && S.mode === 'live'))) {
    return selectRun(pick._id, S.mode === 'auto' ? 'live' : S.mode);
  }
}
async function refreshEvolution() {
  const [gens, lessons] = await Promise.all([api('/api/generations'), api('/api/lessons')]);
  S.gens = gens; S.lessons = lessons;
  markEvolution(); renderHarness();
}
let tickN = 0;
async function tick() {
  pollMemory();
  const n = tickN++;
  if (n % 6 === 0 || !S.runId) await refreshRuns();
  if (n % 10 === 0) await refreshEvolution();
  if (S.runId && S.run) await pollRun();
}
async function loop() {
  for (;;) {
    try { await tick(); setConn(true); } catch (e) { setConn(false); console.warn(e); }
    await sleep(S.mode === 'live' ? 400 : 700);
  }
}
function setConn(ok) { $('#conn').className = 'conn ' + (ok ? 'ok' : 'bad'); $('#conn span').textContent = ok ? 'MONGODB' : 'OFFLINE'; }

/* ---------- tab 1 rendering ---------- */
function once(key, sig) {  // true when `sig` differs from the last render of `key`
  const s = typeof sig === 'string' ? sig : JSON.stringify(sig);
  if (S.sig[key] === s) return false;
  S.sig[key] = s; return true;
}
function hudView() {
  const r = S.run || {}, a = S.actions[S.pos], st = S.statsOk ? S.stats[S.pos] : null;
  const N = r.win_levels || (r.baseline_actions || []).length || 0;
  if (!S.statsOk) {  // the earlier rows could not be read: fall back to the collector's own counters
    return { N, a, level: a ? a.level : r.level || 0, used: Math.max(0, (r.actions_used || 0) - 1 - (r.full_resets || 0)),
      deaths: r.deaths || 0, costs: r.level_costs || [], state: a ? a.state : r.state };
  }
  if (!st) return { N, a, level: 0, used: 0, deaths: 0, costs: [], state: 'NOT_PLAYED' };
  return { N, a, level: st.level, used: st.used, deaths: st.deaths, costs: st.costs, state: st.state };
}
function renderHud() {
  const r = S.run, v = hudView();
  if (!once('hud', [S.runId, r && r.status, r && r.generation, S.pos, v.level, v.used, v.deaths, v.costs, v.N])) return;
  $('#gameId').textContent = r ? r.game_id || S.runId : 'no run';
  $('#gameId').title = S.runId || '';
  const chip = $('#statusChip');
  chip.textContent = r ? r.status || '' : ''; chip.className = 'chip ' + ((r && r.status) || '');
  $('#genChip').textContent = r && Number.isInteger(r.generation) ? 'GEN ' + r.generation : '';
  const la = $('#lastAct');
  la.replaceChildren();
  if (v.a) {
    const p = v.a.params || {};
    la.append(el('small', null, 'LAST'), v.a.action + (Number.isFinite(p.x) ? ` (${p.x},${p.y})` : ''));
    if (v.a.wasted) la.append(el('i', null, 'no change'));
  }
  const lv = $('#hudLevel');
  lv.replaceChildren(String(v.level), el('small', null, v.N ? '/' + v.N : ''));
  $('#hudActions').textContent = r ? v.used : '-';
  $('#hudDeaths').textContent = r ? v.deaths : '-';
  $('#hudDeaths').className = 'num sm' + (v.deaths ? ' hot' : '');
  const chips = $('#levelChips');
  chips.replaceChildren();
  for (let i = 0; i < v.N; i++) {
    chips.append(el('span', i < v.level ? 'done' : i === v.level && v.state !== 'WIN' ? 'cur' : '', 'L' + i));
  }
  renderBars(v);
}
function renderBars(v) {
  const box = $('#levelBars'), base = (S.run && S.run.baseline_actions) || [];
  const n = Math.max(v.N, v.costs.length);
  box.replaceChildren();
  if (!n) return void box.append(el('div', 'none', 'No level data yet.'));
  let scale = 1;
  for (let i = 0; i < n; i++) scale = Math.max(scale, v.costs[i] || 0, base[i] || 0);
  scale *= 1.06;
  for (let i = 0; i < n; i++) {
    const used = v.costs[i] || 0, b = base[i], cleared = i < v.level, cur = i === v.level && v.state !== 'WIN';
    const row = el('div', 'lvrow'), track = el('div', 'track');
    const cls = cur ? 'cur' : !cleared ? '' : !b || used <= b ? 'good' : used <= 2 * b ? 'over' : 'bad';
    if (used) { const f = el('div', 'fill ' + cls); f.style.width = (used / scale) * 100 + '%'; track.append(f); }
    if (b) { const t = el('div', 'base'); t.style.left = (b / scale) * 100 + '%'; t.title = 'human baseline ' + b; track.append(t); }
    const val = el('div', 'val', String(used));
    // The multiple of the baseline is a verdict on a finished level; on the level in play it shows only once it is over.
    if (b) val.append(el('small', null, ` / ${b}  `), used && (cleared || used > b) ? (used / b).toFixed(2) + 'x' : '-');
    row.append(el('b', null, 'L' + i), track, val);
    box.append(row);
  }
}
function syncScrub() {
  const last = Math.max(S.loadedTo, ((S.run && S.run.n_actions) || 0) - 1, 0);
  const s = $('#scrub');
  s.max = last; s.value = Math.max(0, S.pos);
  $('#posLabel').textContent = `${Math.max(0, S.pos)} / ${last}`;
}
function renderMode() {
  const live = S.mode === 'live', r = S.run;
  for (const b of $('#modeSeg').children) b.classList.toggle('on', b.dataset.mode === S.mode);
  for (const b of $('#speedSeg').children) b.classList.toggle('on', +b.dataset.speed === S.speed);
  $('#liveBar').hidden = !live; $('#replayBar').hidden = live;
  $('#btnPlay').textContent = S.playing ? 'PAUSE' : 'PLAY';
  if (!live) return;
  const running = r && r.status === 'running', backlog = S.loadedTo - S.pos;
  $('#liveBar').classList.toggle('idle', !running);
  $('#liveInfo').textContent = !r ? 'waiting for a run'
    : running ? `action ${Math.max(0, S.loadedTo)}` + (backlog > 3 ? `, catching up (${backlog} queued)` : ', caught up')
    : `run ${r.status}: final board shown. Switch to REPLAY to scrub.`;
}
function renderPicker() {
  if (!once('picker', [S.runs.map((r) => [r._id, r.status]), S.runId, S.follow])) return;
  const sel = $('#runSel');
  sel.replaceChildren();
  const auto = el('option', null, S.runs.length ? `auto: ${S.follow && S.runId ? S.runId : 'newest running'}` : 'no runs yet');
  auto.value = ''; sel.append(auto);
  for (const r of S.runs) {
    const o = el('option', null, `${r._id}  [${r.status || '?'}${Number.isInteger(r.generation) ? ', gen ' + r.generation : ''}]`);
    o.value = r._id; sel.append(o);
  }
  sel.value = S.follow ? '' : S.runId || '';
}

function ruleRow(e, cls, isNew) {
  const d = el('div', `rule ${cls}${isNew ? ' new' : ''}`);
  if (typeof e === 'string') { d.append(el('span', 'txt', e)); return d; }
  if (Number.isInteger(e.level)) d.append(el('span', 'lv', 'L' + e.level));
  const t = el('span', 'txt', e.rule || JSON.stringify(e));
  if (e.note && e.note !== e.rule) t.append(el('span', 'note', e.note));
  d.append(t);
  return d;
}
const ruleKey = (e) => (typeof e === 'string' ? e : JSON.stringify([e.rule, e.level]));
function syncRulebook() {
  let rb = null;
  if (S.mode === 'live') rb = S.rulebooks[S.rulebooks.length - 1] || null;
  else for (const b of S.rulebooks) if ((Number.isInteger(b.action_n) ? b.action_n : -1) <= S.pos) rb = b;
  const key = rb ? rb._id : 'none:' + S.rulebooks.length;
  if (key === S.rbKey) return;
  S.rbKey = key;
  const box = $('#rulebook');
  box.replaceChildren();
  $('#rbVersion').textContent = rb ? `version ${rb.version} of ${S.rulebooks.length}, at action ${rb.action_n}` : '';
  if (!rb) {
    return void box.append(el('div', 'none', S.rulebooks.length ? 'No rules recorded up to this action.'
      : 'No rule book yet. The agent records what it verified each time it clears a level.'));
  }
  const wrap = el('div', 'rb'), added = rb.added || {};
  for (const [field, cls, h, title] of [['verified', 'verified', 'v', 'VERIFIED'], ['refuted', 'refuted', 'r', 'REFUTED'],
    ['open_questions', 'open', 'o', 'OPEN QUESTIONS']]) {
    const list = rb[field] || [], fresh = new Set((added[field] || []).map(ruleKey));
    wrap.append(el('h3', h, `${title}  ${list.length}`));
    for (const e of list) wrap.append(ruleRow(e, cls, fresh.has(ruleKey(e))));
  }
  box.append(wrap);
  const fresh = box.querySelector('.new');  // bring what this version added into view
  box.scrollTop = fresh ? Math.max(0, fresh.getBoundingClientRect().top - box.getBoundingClientRect().top - 30) : 0;
}
function lessonRow(id) {
  const l = S.lessons.find((x) => x._id === id), d = el('div', 'lesson');
  d.append(el('span', 'id', id));
  if (!l) { d.append(el('span', 'note', S.evoPending ? 'loading' : 'not in the lessons collection')); return d; }
  d.append(el('span', 'kind', l.kind || 'lesson'), l.text || '');
  return d;
}
function renderHarness() {}  // the panel it filled was replaced by the memory panel below

/* ---------- memory panel: the banks, live, and what this run has written ---------- */
const MB = { banks: [], counts: {}, notes: [], at: 0, busy: false };
async function pollMemory() {
  if (MB.busy || S.tab !== 'live' || Date.now() - MB.at < 1500) return;
  MB.busy = true; MB.at = Date.now();
  try {
    const banks = await api('/api/memory/banks');
    const notes = S.runId ? await api(`/api/memory/notes?run_id=${enc(S.runId)}&limit=20`) : [];
    const box = $('#memBanks');
    if (!box.children.length || banks.length !== MB.banks.length) {
      box.replaceChildren(...banks.map((bk) => {
        const row = el('button', 'mb mb-' + bk.name);
        row.append(el('b', null, String(bk.count)), el('span', null, { rulebooks: 'rule books', generations: 'harness gens' }[bk.name] || bk.name));
        row.title = bk.description || '';
        row.onclick = () => { setTab('memory'); if (window.MemoryTab) { MemoryTab.setRun(''); MemoryTab.open(bk.name); } };
        return row;
      }));
    }
    banks.forEach((bk, i) => {
      const row = box.children[i], n = row.firstChild;
      if (MB.counts[bk.name] !== undefined && MB.counts[bk.name] !== bk.count) { row.classList.remove('pulse'); void row.offsetWidth; row.classList.add('pulse'); }
      MB.counts[bk.name] = bk.count; n.textContent = String(bk.count);
    });
    MB.banks = banks;
    const sig = JSON.stringify(notes.map((x) => [x._id, x.version]));
    if (sig !== MB.sig) {
      MB.sig = sig;
      const nb = $('#memNotes');
      $('#memNotesInfo').textContent = notes.length ? `${notes.length} document${notes.length === 1 ? '' : 's'} in db.notes` : '';
      nb.replaceChildren();
      if (!notes.length) nb.append(el('div', 'none', S.runId ? 'Nothing written yet. Prose the agent saves under notes/ appears here as a MongoDB document, and is updated or deleted with the file.' : 'No run selected.'));
      for (const x of notes) {
        const d = el('div', 'mnote'), h = el('div', 'mh');
        h.append(el('b', null, x.path), el('span', null, 'v' + x.version));
        d.append(h, el('pre', null, x.text || ''));
        d.onclick = () => { setTab('memory'); if (window.MemoryTab) { MemoryTab.open('notes'); MemoryTab.setRun(S.runId); } };
        nb.append(d);
      }
    }
  } catch (e) { /* the next poll retries */ } finally { MB.busy = false; }
}

/* ---------- feed ---------- */
function codeBlock(text) {
  const frag = document.createDocumentFragment(), pre = el('pre', null, text || '');
  const lines = (text || '').split('\n').length;
  frag.append(pre);
  if (lines > 12 || (text || '').length > 1200) {
    const closed = lines > 12 ? `SHOW ALL ${lines} LINES` : 'SHOW ALL';
    const b = el('button', 'more', closed);
    pre.classList.add('clip');
    b.onclick = () => { b.textContent = pre.classList.toggle('clip') ? closed : 'COLLAPSE'; };
    frag.append(b);
  }
  return frag;
}
function renderEvent(e) {
  const d = el('div', 'ev ' + e.kind), meta = el('div', 'meta'), body = el('div', 'body');
  let label = String(e.kind || 'event').toUpperCase();
  switch (e.kind) {
    case 'thinking': body.textContent = e.text || ''; break;
    case 'text': label = 'AGENT'; body.textContent = e.text || ''; break;
    case 'tool_use':
      label = String(e.tool || 'tool').toUpperCase();
      if (e.description) body.append(el('div', 'desc', e.description));
      body.append(codeBlock(e.command)); break;
    case 'tool_result':
      label = e.is_error ? 'RESULT · ERROR' : 'RESULT';
      if (e.is_error) d.classList.add('err');
      body.append(codeBlock(e.text)); break;
    case 'outbox': case 'user_msg':
      d.classList.add('h2a');
      label = 'HARNESS → AGENT' + (e.outbox_kind ? ' · ' + e.outbox_kind : '');
      body.textContent = e.text || ''; break;
    case 'rulebook': {
      label = 'RULE BOOK v' + e.version;
      const a = e.added || {};
      for (const [k, name] of [['verified', 'VERIFIED'], ['refuted', 'REFUTED'], ['open_questions', 'OPEN']]) {
        for (const x of a[k] || []) { const r = el('div', null, typeof x === 'string' ? x : x.rule); r.dataset.k = name; body.append(r); }
      }
      if (!body.childElementCount) body.textContent = 'no new entries';
      break;
    }
    case 'harness':
      label = 'HARNESS · ' + (e.what || 'event');
      body.textContent = e.text || '';
      if ((e.lessons || []).length) body.append(el('div', 'ids', e.lessons.join('  ')));
      break;
    case 'recall': {  // the agent searched the harness's long-term memory
      d.classList.add('mem');
      label = 'MEMORY RECALL';
      if (e.query) body.append(el('div', 'q', e.query));
      if (e.text) body.append(el('div', null, e.text));
      const ids = [...(e.lessons || []), ...(e.notes || [])];
      if (ids.length) body.append(el('div', 'ids', ids.join('  ')));
      break;
    }
    case 'system': label = 'SYSTEM · ' + (e.subtype || ''); body.textContent = e.text || ''; break;
    case 'result':
      label = 'TURN RESULT';
      body.textContent = [e.turns != null ? e.turns + ' turns' : null, e.cost_usd != null ? '$' + fmt(e.cost_usd) : null,
        e.duration_ms != null ? (e.duration_ms / 1000).toFixed(1) + ' s' : null, e.is_error ? 'error' : null].filter(Boolean).join(' · ');
      break;
    default: body.textContent = e.text || '';
  }
  meta.append(el('b', null, label), el('span', null, '#' + e.seq));
  // An event stamped earlier than the one before it (a writer that left action_n at its default) has no
  // trustworthy position of its own: show neither its action nor its level.
  const placed = Number.isInteger(e.action_n) && e.action_n === e._n;
  if (placed && e.action_n >= 0) meta.append(el('span', null, 'action ' + e.action_n));
  if (placed && Number.isInteger(e.level)) meta.append(el('span', null, 'L' + e.level));
  d.append(meta, body);
  return d;
}
function syncFeed() {
  const feed = $('#feed'), box = $('#feedIn'), ev = S.events;
  let end = Math.min(S.feedEnd, ev.length);
  if (S.mode !== 'replay') end = ev.length;
  else {
    while (end < ev.length && ev[end]._n <= S.pos) end++;
    while (end > 0 && ev[end - 1]._n > S.pos) end--;
  }
  $('#feedInfo').textContent = ev.length ? `${end} of ${ev.length} events` : '';
  if (end === 0) {
    S.feedStart = S.feedEnd = 0; S.feedNone = true;
    return void box.replaceChildren(el('div', 'none', !S.run ? 'No run selected.'
      : ev.length ? 'No events up to this action.' : 'No events yet for this run.'));
  }
  if (end === S.feedEnd && !S.feedNone) return pinFeed();
  if (S.feedNone || end < S.feedEnd || end - S.feedEnd > 200) {  // jumped: rebuild the tail
    box.replaceChildren();
    S.feedStart = Math.max(0, end - 150); S.feedEnd = S.feedStart; S.feedNone = false; S.feedPinned = true;
  }
  // A reader who has scrolled up keeps their place: note where the last entry sits, and put it back after appending.
  const ref = S.feedPinned ? null : box.lastElementChild, y0 = ref ? ref.getBoundingClientRect().top : 0;
  for (let i = S.feedEnd; i < end; i++) box.append(renderEvent(ev[i]));
  S.feedEnd = end;
  while (box.childElementCount > 400) { box.firstChild.remove(); S.feedStart++; }
  if (ref && ref.isConnected) feed.scrollTop += ref.getBoundingClientRect().top - y0;
  pinFeed();
}
// The feed is a reversed column (style.css), so it rests at its end and stays there through any reflow: late
// web fonts, a resize, a block expanding. The page only has to return it there after the reader has been away.
let feedTop = 0;  // the feed's scrollTop as the page last set or saw it
function pinFeed() {
  const feed = $('#feed');
  if (!S.feedPinned) return;
  feed.scrollTop = feed.scrollHeight;  // clamps to the end in either scroll coordinate system
  feedTop = feed.scrollTop;
}
function onFeedScroll() {
  const feed = $('#feed'), top = feed.scrollTop;
  const below = $('#feedIn').getBoundingClientRect().bottom - feed.getBoundingClientRect().bottom;  // content past the bottom edge
  if (below < 60) S.feedPinned = true;
  else if (top < feedTop - 4) S.feedPinned = false;  // only the reader moving up releases the pin
  feedTop = top;
}

/* ---------- tab 2: evolution ---------- */
function runLink(id, text) {
  const a = el('a', 'run', text || id);
  a.title = 'Replay ' + id;
  a.onclick = () => openRun(id);
  return a;
}
function openRun(id) {
  S.follow = false; S.playing = true;
  setTab('live');
  selectRun(id, 'replay').catch(console.warn);
}
function scoredRuns() {
  return S.runs.filter((r) => r.metrics && typeof r.metrics.efficiency === 'number')
    .sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)) || String(a._id).localeCompare(String(b._id)));
}
const ROLE = { validation: 'held-out validation run', control: 'control run: no memory, no lessons', train: 'training run' };
function genMeans(runs) {  // [{gen, mean, n}] in generation order
  const m = new Map();
  for (const r of runs) {
    const k = Number.isInteger(r.generation) ? r.generation : null;
    const s = m.get(k) || { gen: k, sum: 0, n: 0 };
    s.sum += r.metrics.efficiency; s.n++; m.set(k, s);
  }
  return [...m.values()].filter((s) => s.gen != null).sort((a, b) => a.gen - b.gen).map((s) => ({ ...s, mean: s.sum / s.n }));
}
function markEvolution() {
  const sig = JSON.stringify([S.gens, S.lessons, S.runs.map((r) => [r._id, r.generation, r.status, r.role, r.metrics && r.metrics.efficiency, r.metrics && r.metrics.completion])]);
  if (sig === S.evoSig) return;
  S.evoSig = sig; S.evoDirty = true;
  if (S.tab === 'evolution') renderEvolution();
}
function renderEvolution() {
  S.evoDirty = false;
  const runs = scoredRuns();
  renderTiles(runs); renderGens(); renderChart(runs); renderLessons();
}
function renderTiles(runs) {
  const box = $('#tiles'), active = S.gens.find((g) => g.status === 'active'), means = genMeans(runs);
  const tile = (num, lbl) => { const t = el('div', 'tile'), n = el('div', 'num'); n.append(...[].concat(num)); t.append(n, el('div', 'lbl', lbl)); return t; };
  const live = S.lessons.filter((l) => l.status === 'active').length;
  box.replaceChildren(
    tile(active ? 'G' + active._id : '-', 'ACTIVE GENERATION'),
    tile(String(live), `ACTIVE LESSONS, ${S.lessons.length - live} RETIRED`),
    tile([String(runs.length), el('small', null, ` of ${S.runs.length} runs`)], 'RUNS SCORED'));
  const held = genMeans(runs.filter((r) => r.role === 'validation'));
  const reverted = S.gens.filter((g) => g.status === 'reverted').length;
  if (held.length >= 2) {  // the same held-out games under every generation: the only like-for-like figure
    const a = held[0], b = held.find((h) => active && h.gen === active._id) || held[held.length - 1];
    box.append(tile([el('small', null, fmt(a.mean) + ' → '), el('span', b.mean >= a.mean ? 'up' : 'down', fmt(b.mean))],
      `HELD-OUT EFFICIENCY G${a.gen} → G${b.gen}` + (reverted ? `, ${reverted} GEN REVERTED` : '')));
  } else if (means.length >= 2) {
    const a = means[0], b = means[means.length - 1];
    box.append(tile([el('small', null, fmt(a.mean) + ' → '), el('span', b.mean >= a.mean ? 'up' : 'down', fmt(b.mean))],
      `MEAN EFFICIENCY G${a.gen} → G${b.gen}`));
  } else box.append(tile(means.length ? fmt(means[0].mean) : '-', 'MEAN EFFICIENCY'));
}
function idSpans(ids, cls, sign) {
  return (ids || []).map((id) => {
    const s = el('span', cls, sign + id), l = S.lessons.find((x) => x._id === id);
    if (l) s.title = l.text;
    return s;
  });
}
function renderGens() {
  const box = $('#genList');
  box.replaceChildren();
  if (!S.gens.length) {
    return void box.append(el('div', 'none', 'No generations yet. Generation 0, the bare harness, appears when the evolution loop starts; each finished game can produce the next one.'));
  }
  for (const g of [...S.gens].sort((a, b) => b._id - a._id)) {
    const c = el('div', 'gcard ' + (g.status || '')), hd = el('div', 'hd'), fit = g.fitness || {}, diff = g.diff || {};
    c.style.setProperty('--gc', genColor(g._id));
    hd.append(el('span', 'g', 'GEN ' + g._id), el('span', 'chip ' + (g.status || ''), g.status || '?'));
    const f = el('span', 'fit');
    f.append(el('small', null, 'efficiency '), fmt(fit.mean_efficiency), el('small', null, '  completion '), fmt(fit.mean_completion));
    hd.append(f);
    const sub = el('div', 'sub');
    sub.append(`parent ${g.parent == null ? 'none' : 'gen ' + g.parent} · ${(g.lessons || []).length} lessons · from run `,
      g.created_from_run ? runLink(g.created_from_run) : 'none');
    if (g.distiller && g.distiller.kind) sub.append(` · distilled by ${g.distiller.kind}${g.distiller.model && g.distiller.model !== 'none' ? ' (' + g.distiller.model + ')' : ''}`);
    const d = el('div', 'diff');
    const line = (k, nodes) => { if (nodes.length) { const r = el('div'), v = el('span', 'vals'); v.append(...nodes); r.append(el('span', 'k', k), v); d.append(r); } };
    line('LESSONS', [...idSpans(diff.added_lessons, 'add', '+'), ...idSpans(diff.revised_lessons, 'rev', '~'), ...idSpans(diff.retired_lessons, 'del', '-')]);
    line('KNOBS', Object.entries(diff.knob_changes || {}).map(([k, v]) => el('span', 'knob', `${k} ${Array.isArray(v) ? v[0] + ' → ' + v[1] : v}`)));
    line('TOOLS', (diff.added_tools || []).map((t) => el('span', 'add', '+' + t)));
    if (!d.childElementCount) line('DIFF', [el('span', null, 'no changes: the baseline harness')]);
    c.append(hd, sub, d);
    if (g.rationale) c.append(el('p', null, g.rationale));
    if (g.addendum) {  // what this generation appends to the agent prompt
      const det = el('details', 'add'), n = g.addendum.split('\n').length;
      det.append(el('summary', null, `PROMPT ADDENDUM, ${n} LINE${n === 1 ? '' : 'S'}`), el('pre', null, g.addendum));
      c.append(det);
    }
    if ((fit.runs || []).length) {
      const rs = el('div', 'runs');
      rs.append(...fit.runs.map((id) => runLink(id)));
      c.append(rs);
    }
    box.append(c);
  }
}
const NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, text) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  return e;
}
function renderChart(runs) {
  const box = $('#chart'), W = box.clientWidth, H = box.clientHeight, leg = $('#chartLegend');
  box.replaceChildren(); leg.replaceChildren();
  if (!runs.length) {
    return void box.append(el('div', 'none', S.runs.length ? 'No scored runs yet. The evolution loop writes metrics when a game ends.' : 'No runs yet.'));
  }
  if (W < 50 || H < 50) { S.evoDirty = true; return; }
  const gens = [...new Set(runs.map((r) => (Number.isInteger(r.generation) ? r.generation : null)))];
  const item = (cls, style, text) => { const s = el('span'), i = el('i', cls); if (style) i.style.background = style; s.append(i, text); return s; };
  leg.append(el('span', null, 'efficiency:'), ...gens.map((g) => item('dot', genColor(g), g == null ? 'no generation' : 'gen ' + g)),
    item('bar', null, 'completion: levels cleared / total'), item('dash', null, 'generation mean'));
  const roles = new Set(runs.map((r) => r.role));
  if (roles.has('validation')) leg.append(item('dia', null, 'held-out validation run'));
  if (roles.has('control')) leg.append(item('ring', null, 'control: no memory, no lessons'));
  const L = 62, R = 14, T = 22, B = 62, pw = W - L - R, ph = H - T - B, slot = pw / runs.length;
  const x = (i) => L + (i + 0.5) * slot, y = (v) => T + (1 - clamp(v, 0, 1)) * ph;
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Run efficiency and completion in launch order, grouped by harness generation' });
  let start = 0;
  for (let i = 1; i <= runs.length; i++) {  // one band per consecutive stretch of a generation
    if (i < runs.length && runs[i].generation === runs[start].generation) continue;
    const g = runs[start].generation, x0 = L + start * slot, w = (i - start) * slot, col = genColor(g);
    const mean = runs.slice(start, i).reduce((a, r) => a + r.metrics.efficiency, 0) / (i - start);
    const head = svg('text', { x: x0 + 20, y: T - 7 });
    head.append(svg('tspan', { class: 't' }, Number.isInteger(g) ? 'GEN ' + g : 'NO GEN'));
    if (w > 150) head.append(svg('tspan', { dx: 10 }, 'mean '), svg('tspan', { class: 'v' }, fmt(mean)));
    s.append(svg('rect', { x: x0, y: T, width: w, height: ph, fill: col, opacity: 0.07 }),
      svg('rect', { x: x0 + 6, y: T - 16, width: 9, height: 9, fill: col }), head);
    if (start) s.append(svg('line', { x1: x0, x2: x0, y1: T - 18, y2: T + ph, stroke: '#3a4656', 'stroke-width': 1 }));
    s.append(svg('line', { x1: x0 + 4, x2: x0 + w - 4, y1: y(mean), y2: y(mean), stroke: '#b3bdca', 'stroke-width': 1.5, 'stroke-dasharray': '5 4' }));
    start = i;
  }
  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    s.append(svg('line', { x1: L, x2: L + pw, y1: y(v), y2: y(v), stroke: v ? '#1b232e' : '#3a4656', 'stroke-width': 1 }),
      svg('text', { x: L - 8, y: y(v) + 4, 'text-anchor': 'end' }, v.toFixed(2)));
  }
  runs.forEach((r, i) => {
    const c = r.metrics.completion;
    if (typeof c === 'number' && c > 0) s.append(svg('rect', { x: x(i) - 3, y: y(c), width: 6, height: y(0) - y(c), fill: '#2b3644', rx: 2 }));
  });
  s.append(svg('polyline', { points: runs.map((r, i) => `${x(i)},${y(r.metrics.efficiency)}`).join(' '), fill: 'none', stroke: '#8a94a3', 'stroke-width': 2, 'stroke-linejoin': 'round' }));
  const every = Math.ceil(runs.length / Math.max(1, Math.floor(pw / 38)));
  s.append(svg('text', { x: L - 8, y: T + ph + 21, 'text-anchor': 'end', class: 't' }, 'RUN'),
    svg('text', { x: L - 8, y: T + ph + 37, 'text-anchor': 'end', class: 't' }, 'LEVELS'),
    svg('text', { x: L - 8, y: T + ph + 53, 'text-anchor': 'end', class: 't' }, 'GAME'));
  runs.forEach((r, i) => {
    const m = r.metrics;
    const cx = x(i), cy = y(m.efficiency), col = genColor(r.generation);
    if (r.role === 'validation') s.append(svg('rect', { x: cx - 6.5, y: cy - 6.5, width: 13, height: 13, fill: col, stroke: '#fff', 'stroke-width': 2, transform: `rotate(45 ${cx} ${cy})` }));
    else if (r.role === 'control') s.append(svg('circle', { cx, cy, r: 6, fill: '#0e1218', stroke: col, 'stroke-width': 3 }));
    else s.append(svg('circle', { cx, cy, r: 6.5, fill: col, stroke: '#0e1218', 'stroke-width': 2 }));
    if (i % every === 0) {
      s.append(svg('text', { x: cx, y: T + ph + 53, 'text-anchor': 'middle', class: 'g' }, String(r.game_id || '').split('-')[0]));
      s.append(svg('text', { x: x(i), y: T + ph + 21, 'text-anchor': 'middle' }, String(i + 1)));
      if (m.win_levels) s.append(svg('text', { x: x(i), y: T + ph + 37, 'text-anchor': 'middle', class: m.levels_cleared < m.win_levels ? 'v' : '' }, `${m.levels_cleared}/${m.win_levels}`));
    }
    const hit = svg('rect', { x: L + i * slot, y: T, width: slot, height: ph + B, class: 'hit' });
    hit.addEventListener('mousemove', (e) => showTip(e, `${r._id}\n${r.game_id || ''} · ${Number.isInteger(r.generation) ? 'gen ' + r.generation : 'no generation'} · ${ROLE[r.role] || 'training run'} · ${r.status}\n`
      + `efficiency  ${fmt(m.efficiency)}\ncompletion  ${fmt(m.completion)}  (${m.levels_cleared}/${m.win_levels} levels)\n`
      + `actions     ${m.actions_total} against a baseline of ${m.baseline_total}\nclick to replay`));
    hit.addEventListener('mouseleave', hideTip);
    hit.addEventListener('click', () => { hideTip(); openRun(r._id); });
    s.append(hit);
  });
  s.append(svg('text', { x: 12, y: T + ph / 2, 'text-anchor': 'middle', class: 't', transform: `rotate(-90 12 ${T + ph / 2})` }, 'EFFICIENCY'));
  box.append(s);
}
function showTip(e, text) {
  const t = $('#tip');
  t.textContent = text; t.hidden = false;
  t.style.left = Math.min(e.clientX + 14, innerWidth - t.offsetWidth - 8) + 'px';
  t.style.top = Math.max(8, e.clientY - t.offsetHeight - 10) + 'px';
}
function hideTip() { $('#tip').hidden = true; }
function renderLessons() {
  const box = $('#lessons');
  box.replaceChildren();
  $('#lessonInfo').textContent = S.lessons.length ? 'game-independent statements the harness carries between games' : '';
  if (!S.lessons.length) return void box.append(el('div', 'none', 'No lessons yet. The distiller writes them from rule books and outcomes after a game ends.'));
  const t = el('table', 'lt'), hr = el('tr');
  for (const [h, c] of [['ID'], ['LESSON'], ['STATUS'], ['GEN'], ['EVIDENCE'], ['INJECTED', 'n'], ['MEAN EFF.', 'n']]) hr.append(el('th', c, h));
  t.append(hr);
  for (const l of S.lessons) {
    const tr = el('tr', l.status === 'retired' ? 'retired' : ''), st = l.stats || {}, runs = st.runs || [];
    const txt = el('td', 'txt');
    if (l.kind) txt.append(el('span', 'kind', l.kind));
    txt.append(el('span', 'body', l.text || ''));
    if (l.retired_reason) txt.append(el('span', 'why', 'retired: ' + l.retired_reason));
    const status = el('td'); status.append(el('span', 'chip ' + (l.status || ''), l.status || ''));
    const evd = el('td', 'evd');
    for (const e of l.evidence || []) {
      const d = el('div');
      d.append(el('span', 'what', `${e.game_id || ''}${Number.isInteger(e.level) ? ' L' + e.level : ''}${e.rule ? ': ' + e.rule : ''}`));
      if (e.run_id) d.append(runLink(e.run_id));
      evd.append(d);
    }
    const denom = runs.length || st.injected || 0;
    tr.append(el('td', 'id', l._id), txt, status, el('td', 'm', l.created_gen == null ? '-' : String(l.created_gen)), evd,
      el('td', 'm n', String(st.injected == null ? runs.length : st.injected)),
      el('td', 'm n', denom && typeof st.efficiency_sum === 'number' ? fmt(st.efficiency_sum / denom) : '-'));
    t.append(tr);
  }
  box.append(t);
}

/* ---------- shell ---------- */
function setTab(tab) {
  S.tab = tab;
  $('#tab-live').hidden = tab !== 'live';
  $('#tab-evolution').hidden = tab !== 'evolution';
  $('#tab-memory').hidden = tab !== 'memory';
  if (window.MemoryTab) MemoryTab.setActive(tab === 'memory');
  $('#pickerWrap').hidden = $('#modeSeg').hidden = tab !== 'live';
  for (const b of $('#tabs').children) b.classList.toggle('on', b.dataset.tab === tab);
  if (tab === 'evolution') renderEvolution();
  syncUrl();
}
function syncUrl() {
  const p = new URLSearchParams();
  if (S.tab !== 'live') p.set('tab', S.tab);
  if (!S.follow && S.runId) p.set('run', S.runId);
  if (S.mode === 'replay') { p.set('mode', 'replay'); if (S.speed !== 1) p.set('speed', S.speed); }
  const s = p.toString();
  history.replaceState(null, '', s ? '?' + s : location.pathname);
}
function setMode(mode) {
  if (mode === S.mode) return;
  S.playing = false;
  if (S.runId) selectRun(S.runId, mode).catch(console.warn);
  else { S.mode = mode; renderMode(); syncUrl(); }
}
function step(dir) {
  if (S.mode !== 'replay') return;
  if (S.playing) setPlaying(false);
  if (dir < 0) return seek(Math.max(S.base, S.pos - 1));  // never before the first board
  if (busy) return;
  S.stepOnce = true; kick();
}
function bind() {
  for (const b of $('#tabs').children) b.onclick = () => setTab(b.dataset.tab);
  for (const b of $('#modeSeg').children) b.onclick = () => setMode(b.dataset.mode);
  for (const b of $('#speedSeg').children) b.onclick = () => { S.speed = +b.dataset.speed; renderMode(); syncUrl(); };
  $('#runSel').onchange = (e) => {
    const id = e.target.value;
    S.follow = !id;
    if (id) selectRun(id, S.mode).catch(console.warn);
    else { S.runId = null; S.epoch++; stopPlay(); refreshRuns().catch(console.warn); }
  };
  $('#btnPlay').onclick = () => setPlaying(!S.playing);
  $('#btnFirst').onclick = () => { setPlaying(false); seek(0); };
  $('#btnBack').onclick = () => step(-1);
  $('#btnFwd').onclick = () => step(1);
  $('#scrub').oninput = (e) => seek(+e.target.value);
  addEventListener('keydown', (e) => {
    if (S.tab !== 'live' || S.mode !== 'replay' || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === ' ') { e.preventDefault(); setPlaying(!S.playing); }
    else if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
  });
  $('#feed').addEventListener('scroll', onFeedScroll, { passive: true });
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', pinFeed);  // web fonts change every line's height
  let rz = null;
  addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => { pinFeed(); if (S.tab === 'evolution') renderEvolution(); }, 120); });
}
async function boot() {
  AC.mountOverlay($('#boardWrap')); AC.mountPad($('#ctlPad'), { layout: 'row' }); AC.mountTape($('#ctlTape'), { max: 12 });
  if (window.MemoryTab) MemoryTab.mount($('#tab-memory'));
  addEventListener('mongoarc:open-run', (e) => { if (e.detail && e.detail.run_id) openRun(e.detail.run_id); });
  bind(); setTab(S.tab); renderMode(); renderHud(); renderHarness(); syncFeed(); syncRulebook();
  for (;;) {  // the palette comes from db.PALETTE; nothing is drawn without it
    try { setPalette(await api('/api/palette')); setConn(true); break; } catch (e) { setConn(false); await sleep(1000); }
  }
  loop();
}
boot();
