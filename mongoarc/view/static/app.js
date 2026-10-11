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

// PAGE is small on purpose: a real ARC-AGI-3 action carries a dozen or more 64x64 boards, so 100 rows are
// several megabytes and seconds of waiting before the first board. 25 rows put a board on screen in about a second.
const FRAME_MS = 60, STEP_GAP_MS = 220, PAGE = 25, EV_PAGE = 500, SPEEDS = [1, 2, 4, 8, 16];
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
  actions: [], early: [], stats: [], statTail: null, statN: -1, statsOk: true, base: 0, loadedTo: -1, pos: -1,
  events: [], lastSeq: 0, feedStart: 0, feedEnd: 0, feedNone: true, feedPinned: true,
  rulebooks: [], rbKey: null, rbDirty: true, rbLoaded: false, evLoaded: false, liveInit: false, settled: false,
  lessons: [], gens: [], evoEvents: [], evoPartial: false, evoFor: null,  // the selected run's own generations, lessons, memory searches
  evoSig: '', evoDirty: true, evoPending: false, evoAgain: false, evoKey: '', sig: {},
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
  const rows = S.early.slice();  // a run joined part-way: the presses before its first loaded board count too
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
    for (const a of list) {
      if (!Number.isInteger(a.n) || a.n >= upto) continue;
      pushStats(a);
      S.early.push({ action: a.action, params: a.params });  // what was pressed, for the pad counters
    }
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
  Object.assign(S, { runId: id, run: null, actions: [], early: [], stats: [], statTail: null, statN: -1, statsOk: true,
    base: 0, loadedTo: -1, pos: -1, events: [], lastSeq: 0, feedStart: 0, feedEnd: 0, feedNone: true, feedPinned: true,
    rulebooks: [], rbKey: null, rbDirty: true, rbLoaded: false, evLoaded: false, settled: false, pollN: 0, sig: {} });
  if (mode) S.mode = mode;
  if (S.evoFor !== id) { S.gens = []; S.lessons = []; S.evoEvents = []; S.evoPartial = false; S.evoFor = null; markEvolution(); }
  AC.reset();
  MB.at = 0;  // this run's notes on the next tick, not a second and a half later
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
// The run's generations as the page holds them are behind (another run was selected, or a level was just cleared and
// runs.gen_k moved on): fetch them now, not on the next slow tick.
function ensureEvolution(r) {
  if (!r) return;
  const key = r._id + ':' + r.gen_k;  // asked once per run and generation; the slow tick covers everything else
  if (S.evoKey === key && (S.evoFor === r._id || S.evoPending)) return;
  if (S.evoPending) { S.evoAgain = true; return; }
  S.evoKey = key; S.evoPending = true;
  refreshEvolution().catch(console.warn).finally(() => {
    S.evoPending = false;
    if (S.evoAgain) { S.evoAgain = false; ensureEvolution(evoRun()); }
  });
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
      if (Number.isInteger(r.gen_k) && S.evoFor === id && !S.gens.some((g) => g.k === r.gen_k)) ensureEvolution(r);
    }
    let got = 0;
    // The feed and the rule book first: they are small, and must not wait behind megabytes of boards.
    for (let page = 0; page < 5; page++) {
      const list = await api(`/api/runs/${enc(id)}/events?after_seq=${S.lastSeq}&limit=${EV_PAGE}`);
      if (ep !== S.epoch) return;
      ingestEvents(list); got += list.length;
      if (list.length < EV_PAGE) break;
    }
    S.evLoaded = true;
    if (S.rbDirty || n % 6 === 0) {
      const rb = await api(`/api/runs/${enc(id)}/rulebooks`);
      if (ep !== S.epoch) return;
      S.rulebooks = rb; S.rbDirty = false; S.rbLoaded = true;  // syncRulebook redraws only when what it shows has changed
    }
    syncFeed(); syncRulebook();
    for (let page = 0; page < 8; page++) {
      const list = await api(`/api/runs/${enc(id)}/actions?after_n=${S.loadedTo}&limit=${PAGE}`);
      if (ep !== S.epoch) return;
      ingestActions(list); got += list.length;
      afterActions();
      if (list.length < PAGE) break;
    }
    S.settled = got === 0 && runSettled();
    renderHud(); renderHarness(); syncScrub(); syncFeed(); syncRulebook(); renderMode(); markEvolution();
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
  // Of the games in flight, one with per-level memory comes before a control run: it is the one that evolves.
  const alive = S.runs.filter((r, i) => r.status === 'running' && !dead(r, i));
  const running = alive.find((r) => r.role !== 'control') || alive[0];
  const pick = running || S.runs[0];
  if (!S.runs.length && !S.runId) clearBoard('No runs yet. Launch a game and it will appear here.');
  if (S.follow && pick && (S.runId == null || (running && running._id !== S.runId && S.mode === 'live'))) {
    return selectRun(pick._id, S.mode === 'auto' ? 'live' : S.mode);
  }
}
// Generations and lessons belong to one run: nothing is read from, or shown for, any other game.
async function refreshEvolution() {
  const rid = S.runId;
  if (!rid) { S.gens = []; S.lessons = []; S.evoEvents = []; S.evoFor = null; return markEvolution(); }
  // The memory searches are read for the evolution tab only (and once per run, so the tab opens with them).
  const wantEv = S.tab === 'evolution' || S.evoFor !== rid;
  const [gens, lessons, evs] = await Promise.all([api(`/api/generations?run_id=${enc(rid)}`), api(`/api/lessons?run_id=${enc(rid)}`),
    wantEv ? api(`/api/runs/${enc(rid)}/events?kind=recall,harness&limit=2000`) : null]);
  if (rid !== S.runId) return;
  // Filtered here as well: a server from before the per-run design ignores run_id and kind and answers with everything.
  S.gens = gens.filter((g) => g.run_id === rid).sort((a, b) => (a.k || 0) - (b.k || 0));
  S.lessons = lessons.filter((l) => l.run_id === rid);
  if (evs) {
    S.evoEvents = evs.filter((e) => e.kind === 'recall' || e.kind === 'harness');
    S.evoPartial = evs.length >= 2000 && S.evoEvents.length < evs.length;
  }
  S.evoFor = rid;
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
  if (!once('hud', [S.runId, r && r.status, r && r.gen_k, r && r.role, r && r.memory_tool, S.pos, v.level, v.used, v.deaths, v.costs, v.N])) return;
  $('#gameId').textContent = r ? r.game_id || S.runId : 'no run';
  $('#gameId').title = S.runId || '';
  const chip = $('#statusChip');
  chip.textContent = r ? r.status || '' : ''; chip.className = 'chip ' + ((r && r.status) || '');
  // The run's CURRENT generation: it moves on each time a level is cleared.
  $('#genChip').textContent = r && Number.isInteger(r.gen_k) ? 'GEN ' + r.gen_k : '';
  $('#genChip').title = 'current harness generation of this run: one per cleared level';
  $('#roleChip').textContent = isControl(r) ? 'CONTROL' : '';
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
  if (!once('picker', [S.runs.map((r) => [r._id, r.status, r.gen_k, r.role]), S.runId, S.follow])) return;
  const sel = $('#runSel');
  sel.replaceChildren();
  const auto = el('option', null, S.runs.length ? `auto: ${S.follow && S.runId ? S.runId : 'newest running'}` : 'no runs yet');
  auto.value = ''; sel.append(auto);
  for (const r of S.runs) {
    const o = el('option', null, `${r._id}  [${r.status || '?'}${r.role === 'control' ? ', control' : Number.isInteger(r.gen_k) ? ', gen ' + r.gen_k : ''}]`);
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
  // Redrawing returns the panel to its first new rule, so it happens only when the content differs: a reader
  // scrolling the rule book during a live game keeps their place between polls.
  const key = rb ? [rb._id, rb.version, S.rulebooks.length, (rb.verified || []).length, (rb.refuted || []).length, (rb.open_questions || []).length].join(':')
    : 'none:' + S.rulebooks.length + ':' + S.rbLoaded;
  if (key === S.rbKey) return;
  S.rbKey = key;
  const box = $('#rulebook');
  box.replaceChildren();
  $('#rbVersion').textContent = rb ? `version ${rb.version} of ${S.rulebooks.length}, at action ${rb.action_n}` : '';
  if (!rb) {
    return void box.append(el('div', 'none', S.run && !S.rbLoaded ? 'Loading the rule book.'
      : S.rulebooks.length ? 'No rules recorded up to this action.'
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
const MB = { banks: [], counts: {}, notes: [], at: 0, busy: false, rid: null };
async function pollMemory() {
  if (MB.busy || S.tab !== 'live' || Date.now() - MB.at < 1500) return;
  MB.busy = true; MB.at = Date.now();
  try {
    const rid = S.runId;
    const banks = await api('/api/memory/banks' + (rid ? `?run_id=${enc(rid)}` : ''));
    // Memory is per game: the counts are this run's documents where the server reports them (run_count).
    const own = !!rid && banks.every((bk) => Number.isInteger(bk.run_count)), cnt = (bk) => (own ? bk.run_count : bk.count);
    const notes = rid ? await api(`/api/memory/notes?run_id=${enc(rid)}&limit=20`) : [];
    if (rid !== S.runId) { MB.at = 0; return; }  // the run changed while this was in flight: ask again for the new one
    if (MB.rid !== rid) { MB.rid = rid; MB.counts = {}; }
    $('#memBanksInfo').textContent = own ? "this run's documents; click a bank to open them" : 'all runs; click a bank to open its documents';
    const box = $('#memBanks');
    if (!box.children.length || banks.length !== MB.banks.length) {
      box.replaceChildren(...banks.map((bk) => {
        const row = el('button', 'mb mb-' + bk.name);
        row.append(el('b', null, String(cnt(bk))), el('span', null, { rulebooks: 'rule books', generations: 'harness gens' }[bk.name] || bk.name));
        row.onclick = () => { setTab('memory'); if (window.MemoryTab) { MemoryTab.setRun(S.runId || ''); MemoryTab.open(bk.name); } };
        return row;
      }));
    }
    banks.forEach((bk, i) => {
      const row = box.children[i], n = row.firstChild;
      if (MB.counts[bk.name] !== undefined && MB.counts[bk.name] !== cnt(bk)) { row.classList.remove('pulse'); void row.offsetWidth; row.classList.add('pulse'); }
      MB.counts[bk.name] = cnt(bk); n.textContent = String(cnt(bk));
      row.title = (bk.description || '') + (own ? `  ${bk.run_count} of this run, ${bk.count} in all runs.` : '');
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
    case 'harness': {
      const gk = Number.isInteger(e.k) ? e.k : null, ids = e.lessons || [];
      label = 'HARNESS · ' + (e.what || 'event');
      body.textContent = e.text || '';
      if (e.what === 'memory_nudge') {  // the harness told the agent to search its memory before acting on a new level
        d.classList.add('nudge');
        label = 'HARNESS → AGENT · SEARCH YOUR MEMORY';
        if (!e.text) body.textContent = 'Search your memory before the first action of this level.';
      } else if (e.what === 'generation_created') {
        label = 'HARNESS · NEW GENERATION' + (gk == null ? '' : ' · GEN ' + gk);
        if (ids.length) body.append(el('div', 'ids', ids.map((x) => '+' + x).join('  ')));
      } else if (e.what === 'lessons_injected') {
        label = 'HARNESS · LESSONS INJECTED' + (gk == null ? '' : ' · GEN ' + gk);
        const items = e.items || [];
        if (items.length) {
          const changed = e.changed || [];  // the event's text repeats the lessons; the list below is the readable form
          body.textContent = changed.length ? `changed at this level: ${changed.join(', ')}` : '';
          const list = el('div', 'inj');
          for (const it of items) { const row = el('div'); row.append(el('span', 'id', it.id || ''), el('span', 'kind', it.kind || 'lesson'), it.text || ''); list.append(row); }
          body.append(list);
        } else if (ids.length) body.append(el('div', 'ids', ids.join('  ')));
      } else if (ids.length) body.append(el('div', 'ids', ids.join('  ')));
      break;
    }
    case 'recall': {  // the agent searched this run's memory: its notes, its rule book, its level lessons
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
      : !S.evLoaded ? 'Loading the agent feed.' : ev.length ? 'No events up to this action.' : 'No events yet for this run.'));
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

/* ---------- tab 2: how the harness changed inside ONE game (the run in the RUN picker) ---------- */
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
const ratioX = (v) => (typeof v === 'number' && isFinite(v) ? v.toFixed(2) + 'x' : '-');
const isControl = (r) => !!r && (r.role === 'control' || r.memory_tool === false);
function evoRun() { return S.run && S.run._id === S.runId ? S.run : S.runs.find((x) => x._id === S.runId) || null; }
// One row per level of a run: what the harness recorded when the level was cleared (generations), else the
// run's metrics, else the collector's live counters. ratio = actions spent / human baseline; lower is better.
function levelRows(r, gens) {
  if (!r) return [];
  const base = r.baseline_actions || [], m = r.metrics || {}, ml = m.levels || [], costs = r.level_costs || [];
  const rec = new Map();
  for (const g of gens || []) for (const x of [g.level_metrics, g.played]) if (x && Number.isInteger(x.level)) rec.set(x.level, x);
  const cur = Number.isInteger(r.level) ? r.level : m.levels_cleared || 0;
  const N = Math.max(r.win_levels || 0, m.win_levels || 0, base.length, ml.length);
  const rows = [];
  for (let i = 0; i < N; i++) {
    const x = ml.find((v) => v.level === i), g = rec.get(i);
    const cleared = i < cur || !!(x && x.cleared);
    const actions = cleared ? (g && g.actions) || (x && x.actions) || costs[i] || 0 : costs[i] || (x && x.actions) || 0;
    const baseline = (g && g.baseline) || (x && x.baseline) || base[i] || null;
    rows.push({ level: i, actions, baseline, cleared, playing: !cleared && i === cur && r.status === 'running',
      ratio: actions && baseline ? actions / baseline : null });
  }
  return rows;
}
// The reference for a run with per-level memory: the most recent finished control run of the same game.
function controlFor(r) {
  if (!r || isControl(r)) return null;
  return S.runs.find((x) => x._id !== r._id && x.role === 'control' && x.game_id === r.game_id && x.status !== 'running') || null;
}
// Did the agent ever retrieve from its memory, and when? Recall events are written by the memory gateway on
// every search; memory_nudge events by the harness each time it tells the agent to search at a new level.
function memoryUse() {
  const by = new Map(), at = (lv) => { if (!by.has(lv)) by.set(lv, { searches: [], nudges: 0 }); return by.get(lv); };
  let searches = 0, nudges = 0;
  for (const e of S.evoEvents) {
    const lv = Number.isInteger(e.level) ? e.level : 0;
    if (e.kind === 'recall') { at(lv).searches.push(e); searches++; }
    else if (e.kind === 'harness' && e.what === 'memory_nudge') { at(lv).nudges++; nudges++; }
  }
  return { by, searches, nudges, levels: [...by.values()].filter((v) => v.searches.length).length };
}
function markEvolution() {
  const r = evoRun(), c = controlFor(r);
  const live = (x) => x && [x._id, x.status, x.role, x.level, x.gen_k, x.level_costs, x.metrics && x.metrics.levels];
  const sig = JSON.stringify([S.runId, S.evoFor, S.gens, S.lessons, S.evoEvents.map((e) => e.seq), S.evoPartial, live(r), live(c)]);
  if (sig === S.evoSig) return;
  S.evoSig = sig; S.evoDirty = true;
  if (S.tab === 'evolution') renderEvolution();
}
function renderEvolution() {
  S.evoDirty = false;
  const r = evoRun(), rows = levelRows(r, S.gens), use = memoryUse();
  const scope = $('#evoScope');
  scope.replaceChildren();
  if (r) {
    scope.append(el('b', null, r.game_id || r._id), el('span', 'rid', r._id), el('span', 'chip ' + (r.status || ''), r.status || ''));
    if (isControl(r)) scope.append(el('span', 'chip ctl', 'CONTROL'));
  } else scope.append(el('span', 'rid', S.runs.length ? 'No run selected: pick one in the RUN picker.' : 'No runs yet.'));
  renderTiles(r, rows, use); renderGens(r, rows); renderChart(r, rows, use); renderLessons(r);
}
function renderTiles(r, rows, use) {
  const box = $('#tiles'), active = S.gens.find((g) => g.status === 'active') || S.gens[S.gens.length - 1];
  const tile = (num, lbl, cls) => { const t = el('div', 'tile'), n = el('div', 'num' + (cls ? ' ' + cls : '')); n.append(...[].concat(num)); t.append(n, el('div', 'lbl', lbl)); return t; };
  const live = S.lessons.filter((l) => l.status === 'active').length, ctl = isControl(r);
  const done = rows.filter((x) => x.cleared), scored = done.filter((x) => x.ratio != null);
  box.replaceChildren(
    tile(active && Number.isInteger(active.k) ? 'GEN ' + active.k : '-', ctl ? 'GENERATION: A CONTROL RUN STAYS ON 0' : 'ACTIVE GENERATION OF THIS RUN'),
    tile(String(live), `ACTIVE LESSONS, ${S.lessons.length - live} RETIRED`),
    tile([String(done.length), el('small', null, rows.length ? ` of ${rows.length}` : '')], 'LEVELS CLEARED'));
  if (scored.length >= 2) {  // like for like inside one game: the first cleared level against the most recent one
    const a = scored[0], b = scored[scored.length - 1];
    box.append(tile([el('small', null, `L${a.level} ${ratioX(a.ratio)} → `), el('span', b.ratio <= a.ratio ? 'up' : 'down', `L${b.level} ${ratioX(b.ratio)}`)],
      'ACTIONS PER LEVEL, OF HUMAN BASELINE: FIRST → LATEST CLEARED', 's2'));
  } else box.append(tile(scored.length ? `L${scored[0].level} ${ratioX(scored[0].ratio)}` : '-', 'ACTIONS PER LEVEL, OF HUMAN BASELINE', 's2'));
  if (ctl) box.append(tile('none', 'MEMORY SEARCHES: NO MEMORY TOOL IN A CONTROL RUN', 's2'));
  else {
    const n = el('span', use.searches ? '' : 'down', String(use.searches));
    box.append(tile([n, el('small', null, ` at ${use.levels} of ${rows.length || '?'} levels`)],
      `MEMORY SEARCHES BY THE AGENT · ${use.nudges} HARNESS NUDGE${use.nudges === 1 ? '' : 'S'}` + (S.evoPartial ? ' · FIRST 2000 EVENTS ONLY' : '')));
  }
}
function idSpans(ids, cls, sign) {
  return (ids || []).map((id) => {
    const s = el('span', cls, sign + id), l = S.lessons.find((x) => x._id === id);
    if (l) s.title = l.text;
    return s;
  });
}
function renderGens(r, rows) {
  const box = $('#genList');
  box.replaceChildren();
  if (!r) return void box.append(el('div', 'none', 'No run selected.'));
  if (S.evoFor !== S.runId) return void box.append(el('div', 'none', 'Loading the generations of this run.'));
  if (isControl(r)) {
    box.append(el('div', 'evonote ctl', 'Control run: launched with --no-memory. It has no memory tool, learns no level lessons and stays on generation 0, so nothing evolves here. It is the reference that runs with per-level memory are compared against.'));
  }
  if (!S.gens.length) {
    const old = r.generation != null && !Number.isInteger(r.gen_k);
    return void box.append(el('div', 'evonote', 'No per-level generations recorded for this run.'
      + (old ? ' It was recorded under the earlier design, where a generation belonged to a whole game; that record is not drawn here.'
        : isControl(r) ? '' : ' Generation 0 is written at launch, and a new generation each time a level is cleared.')));
  }
  for (const g of [...S.gens].sort((a, b) => (b.k || 0) - (a.k || 0))) {
    const c = el('div', 'gcard ' + (g.status || '')), hd = el('div', 'hd'), diff = g.diff || {}, lm = g.level_metrics, pl = g.played;
    c.style.setProperty('--gc', genColor(g.k));
    hd.append(el('span', 'g', 'GEN ' + (Number.isInteger(g.k) ? g.k : '?')), el('span', 'chip ' + (g.status || ''), g.status || '?'),
      el('span', 'when', Number.isInteger(g.level) ? 'after level ' + g.level : 'at launch'));
    const from = el('div', 'played from');
    if (lm) {  // the level whose clearing produced this generation
      const x = lm.baseline ? lm.actions / lm.baseline : null;
      from.append(el('span', 'k', 'DISTILLED FROM'), `level ${lm.level}: ${lm.actions} actions / baseline ${lm.baseline == null ? '?' : lm.baseline}`,
        el('small', null, 'efficiency ' + fmt(lm.efficiency)), el('b', x == null ? '' : x <= 1 ? 'good' : x <= 2 ? 'over' : 'bad', x == null ? '' : ratioX(x)));
    }
    const sub = el('div', 'sub'), ds = g.distiller || {};
    sub.append(`${(g.lessons || []).length} lesson${(g.lessons || []).length === 1 ? '' : 's'} active`);
    if (ds.kind && ds.kind !== 'none') sub.append(` · distilled by ${ds.kind}${ds.model && ds.model !== 'none' ? ' (' + ds.model + ')' : ''}${typeof ds.cost_usd === 'number' && ds.cost_usd ? ', $' + ds.cost_usd.toFixed(3) : ''}`);
    const d = el('div', 'diff');
    const line = (k, nodes) => { if (nodes.length) { const row = el('div'), v = el('span', 'vals'); v.append(...nodes); row.append(el('span', 'k', k), v); d.append(row); } };
    line('ADDED', idSpans(diff.added_lessons, 'add', '+'));
    line('REVISED', idSpans(diff.revised_lessons, 'rev', '~'));
    line('RETIRED', idSpans(diff.retired_lessons, 'del', '-'));
    line('KNOBS', Object.entries(diff.knob_changes || {}).map(([k, v]) => el('span', 'knob', `${k} ${Array.isArray(v) ? v[0] + ' → ' + v[1] : v}`)));
    if (!d.childElementCount) line('DIFF', [el('span', null, g.k ? 'no change to lessons or knobs' : 'none: the harness as launched, with empty memory')]);
    c.append(hd, sub);
    if (lm) c.append(from);
    c.append(d);
    if (g.rationale) c.append(el('p', null, g.rationale));
    // How the level played UNDER this generation went: level k.
    const p = el('div', 'played'), row = rows[g.k];
    if (pl) {
      const x = pl.baseline ? pl.actions / pl.baseline : null;
      p.append(el('span', 'k', 'PLAYED UNDER IT'), `level ${pl.level}: ${pl.actions} actions / baseline ${pl.baseline == null ? '?' : pl.baseline}`,
        el('b', x == null ? '' : x <= 1 ? 'good' : x <= 2 ? 'over' : 'bad', x == null ? '' : ratioX(x)));
    } else if (row && row.playing) p.append(el('span', 'k', 'PLAYED UNDER IT'), `level ${row.level}: in play, ${row.actions} actions so far` + (row.baseline ? ` / baseline ${row.baseline}` : ''));
    else if (row && !row.cleared && r.status !== 'running') p.append(el('span', 'k', 'PLAYED UNDER IT'), `level ${row.level}: not cleared` + (row.actions ? ` after ${row.actions} actions` : ''));
    else if (!row && Number.isInteger(g.k) && rows.length && g.k >= rows.length) p.append(el('span', 'k', 'PLAYED UNDER IT'), 'nothing: the game was over, no level is left');
    if (p.childElementCount) c.append(p);
    if (g.addendum) {  // the lessons section of the workspace CLAUDE.md under this generation
      const det = el('details', 'add'), n = g.addendum.split('\n').length;
      det.append(el('summary', null, `LESSONS SECTION OF CLAUDE.md, ${n} LINE${n === 1 ? '' : 'S'}`), el('pre', null, g.addendum));
      c.append(det);
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
function renderChart(r, rows, use) {
  const box = $('#chart'), W = box.clientWidth, H = box.clientHeight, leg = $('#chartLegend');
  box.replaceChildren(); leg.replaceChildren();
  if (!r) return void box.append(el('div', 'none', S.runs.length ? 'No run selected.' : 'No runs yet.'));
  if (!rows.length) return void box.append(el('div', 'none', 'No level data recorded for this run yet.'));
  if (W < 50 || H < 50) { S.evoDirty = true; return; }
  const ctl = controlFor(r), crow = ctl ? levelRows(ctl, []) : [], self = isControl(r);
  const perLevel = !self && S.gens.length > 0;  // level i was played under generation i of this run
  const tone = (i) => (perLevel ? genColor(i) : '#b3bdca');
  const item = (cls, style, text) => { const s = el('span'), i = el('i', cls); if (style) i.style.background = style; s.append(i, text); return s; };
  leg.append(self ? item('ring', null, 'this run (control, no memory)') : item('dot', '#b3bdca', 'this run (per-level memory)'));
  if (ctl) { const s = item('ring', null, 'control (no memory) '); s.append(runLink(ctl._id)); leg.append(s); }
  else if (!self) leg.append(el('span', 'mute', 'no finished control run of this game in the database'));
  if (perLevel) {
    const ks = rows.filter((x) => x.cleared || x.playing).map((x) => x.level);
    if (ks.length) { const s = el('span', 'gk'); s.append('played under', ...ks.map((k) => item('dot', genColor(k), 'GEN ' + k))); leg.append(s); }
  }
  // A level a finished run never cleared is an X at the top; a level a running game has not reached yet is left empty.
  const failed = (v, run) => !v.cleared && !v.playing && run.status !== 'running';
  if (rows.some((v) => failed(v, r))) leg.append(item('x' + (self ? ' c' : ''), null, self ? 'level not cleared' : 'this run: level not cleared'));
  if (ctl && crow.slice(0, rows.length).some((v) => failed(v, ctl))) leg.append(item('x c', null, 'control: level not cleared'));
  if (!self) leg.append(el('span', 'mute', 'RECALLS: memory searches the agent made on that level; nudged = the harness told it to search'));
  const L = 78, R = 16, T = 16, B = 60, pw = W - L - R, ph = H - T - B, N = rows.length, slot = pw / N;
  let top = 1.5;
  for (const x of [...rows, ...crow]) if (x.ratio != null && (x.cleared || x.playing)) top = Math.max(top, x.ratio * 1.14);
  top = Math.ceil(top * 2) / 2;
  const step = top <= 2 ? 0.5 : top <= 5 ? 1 : Math.ceil(top / 5);
  const x = (i) => L + (i + 0.5) * slot, y = (v) => T + (1 - clamp(v / top, 0, 1)) * ph;
  const s = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': 'Actions spent on each level of this game as a multiple of the human baseline; lower is better' });
  for (let v = 0; v <= top + 1e-9; v += step) {
    s.append(svg('line', { x1: L, x2: L + pw, y1: y(v), y2: y(v), stroke: v ? '#1b232e' : '#3a4656', 'stroke-width': 1 }),
      svg('text', { x: L - 8, y: y(v) + 4, 'text-anchor': 'end' }, v.toFixed(v % 1 ? 1 : 0) + 'x'));
  }
  s.append(svg('line', { x1: L, x2: L + pw, y1: y(1), y2: y(1), stroke: '#f1f4f8', 'stroke-width': 1.5, opacity: 0.75 }),
    svg('text', { x: L + pw - 4, y: y(1) - 6, 'text-anchor': 'end', class: 't' }, 'human baseline'));
  const pts = (list) => list.filter((v) => v.cleared && v.ratio != null).map((v) => `${x(v.level)},${y(v.ratio)}`).join(' ');
  if (crow.length) s.append(svg('polyline', { points: pts(crow.slice(0, N)), fill: 'none', stroke: '#8a94a3', 'stroke-width': 1.5, 'stroke-dasharray': '5 4' }));
  s.append(svg('polyline', { points: pts(rows), fill: 'none', stroke: '#8a94a3', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-dasharray': self ? '5 4' : 'none' }));
  const cross = (cx, cy, col, w) => { for (const d of [1, -1]) s.append(svg('line', { x1: cx - 5, x2: cx + 5, y1: cy - 5 * d, y2: cy + 5 * d, stroke: col, 'stroke-width': w, 'stroke-linecap': 'round' })); };
  for (const v of crow.slice(0, N)) {  // the control run, drawn first so this run's points sit on top
    if (v.cleared && v.ratio != null) s.append(svg('circle', { cx: x(v.level), cy: y(v.ratio), r: 5.5, fill: '#0e1218', stroke: '#b3bdca', 'stroke-width': 2.5 }));
    else if (failed(v, ctl)) cross(x(v.level) + (rows[v.level] && failed(rows[v.level], r) ? 13 : 0), T + 9, '#b3bdca', 2);
  }
  const rowY = [T + ph + 20, T + ph + 36, T + ph + 52];
  s.append(svg('text', { x: L - 8, y: rowY[0], 'text-anchor': 'end', class: 't' }, 'LEVEL'),
    svg('text', { x: L - 8, y: rowY[1], 'text-anchor': 'end', class: 't' }, 'ACTIONS'),
    svg('text', { x: L - 8, y: rowY[2], 'text-anchor': 'end', class: 't' }, 'RECALLS'));
  rows.forEach((v, i) => {
    const cx = x(i), col = tone(v.level), u = use.by.get(i) || { searches: [], nudges: 0 }, c = crow[i];
    if (v.cleared && v.ratio != null) {
      const cy = y(v.ratio);
      if (self) s.append(svg('circle', { cx, cy, r: 5.5, fill: '#0e1218', stroke: '#b3bdca', 'stroke-width': 2.5 }));
      else s.append(svg('circle', { cx, cy, r: 6.5, fill: col, stroke: '#0e1218', 'stroke-width': 2 }));
      // The figure goes on the side away from the control run's point, so the two do not sit on each other.
      const below = (c && c.cleared && c.ratio != null && c.ratio >= v.ratio && cy + 22 < T + ph) || cy - 12 < T + 10;
      s.append(svg('text', { x: cx, y: below ? cy + 20 : cy - 11, 'text-anchor': 'middle', class: 'v' }, ratioX(v.ratio)));
    } else if (v.playing) {
      const cy = v.ratio != null ? y(v.ratio) : y(0);
      s.append(svg('circle', { cx, cy, r: 6.5, fill: 'none', stroke: col, 'stroke-width': 2.5, 'stroke-dasharray': '3 3' }),
        svg('text', { x: cx, y: cy - 12, 'text-anchor': 'middle', class: 't' }, 'in play'));
    } else if (failed(v, r)) cross(cx - (c && failed(c, ctl) ? 13 : 0), T + 9, self ? '#b3bdca' : '#F93C31', 2.5);
    s.append(svg('text', { x: cx, y: rowY[0], 'text-anchor': 'middle', class: 't' }, 'L' + i),
      svg('text', { x: cx, y: rowY[1], 'text-anchor': 'middle' }, v.actions || v.cleared ? `${v.actions} / ${v.baseline == null ? '?' : v.baseline}` : '-'));
    const reached = v.cleared || v.playing || v.actions > 0;
    const st = self ? 'no tool' : !reached ? '-' : String(u.searches.length) + (u.nudges ? ' (nudged)' : '');
    s.append(svg('text', { x: cx, y: rowY[2], 'text-anchor': 'middle', class: self || !reached ? '' : u.searches.length ? 'ok' : 'z' }, st));
    const hit = svg('rect', { x: L + i * slot, y: T, width: slot, height: ph + B, class: 'hit' });
    hit.addEventListener('mousemove', (e) => showTip(e, `level ${i}` + (self ? ' · control run, generation 0' : perLevel ? ` · played under GEN ${i}` : '') + '\n'
      + (v.cleared ? `${v.actions} actions / human baseline ${v.baseline == null ? '?' : v.baseline} = ${ratioX(v.ratio)}`
        : v.playing ? `in play: ${v.actions} actions so far` + (v.baseline ? ` / human baseline ${v.baseline}` : '')
        : v.actions ? `not cleared after ${v.actions} actions` : r.status === 'running' ? 'not reached yet' : 'not reached')
      + (self ? '' : `\nmemory searches by the agent on this level: ${u.searches.length}` + (u.nudges ? `, after ${u.nudges} harness nudge${u.nudges === 1 ? '' : 's'}` : ', no harness nudge recorded')
        + u.searches.map((q) => `\n  "${String(q.query || '').slice(0, 44)}"`).join(''))
      + (c ? `\ncontrol (no memory): ` + (c.cleared ? `${c.actions} actions = ${ratioX(c.ratio)}` : c.actions ? `not cleared after ${c.actions} actions` : 'not reached') : '')));
    hit.addEventListener('mouseleave', hideTip);
    s.append(hit);
  });
  s.append(svg('text', { x: 12, y: T + ph / 2, 'text-anchor': 'middle', class: 't', transform: `rotate(-90 12 ${T + ph / 2})` }, 'ACTIONS / BASELINE'));
  box.append(s);
}
function showTip(e, text) {
  const t = $('#tip');
  t.textContent = text; t.hidden = false;
  t.style.left = Math.min(e.clientX + 14, innerWidth - t.offsetWidth - 8) + 'px';
  t.style.top = Math.max(8, e.clientY - t.offsetHeight - 10) + 'px';
}
function hideTip() { $('#tip').hidden = true; }
function renderLessons(r) {
  const box = $('#lessons');
  box.replaceChildren();
  $('#lessonInfo').textContent = S.lessons.length ? 'what each cleared level taught, used on the later levels of this game only' : '';
  if (!S.lessons.length) {
    return void box.append(el('div', 'none', !r ? 'No run selected.'
      : isControl(r) ? 'None: a control run has no memory and learns no level lessons.'
      : 'No lessons recorded for this run. The harness distils them each time a level is cleared, for the later levels of the same game.'));
  }
  const revised = new Map();
  for (const g of S.gens) for (const id of (g.diff || {}).revised_lessons || []) revised.set(id, g.k);
  const t = el('table', 'lt'), hr = el('tr');
  for (const [h, c] of [['ID'], ['LEARNED'], ['LESSON'], ['STATUS'], ['EVIDENCE  level / rule'], ['RECALLED', 'n']]) hr.append(el('th', c, h));
  t.append(hr);
  for (const l of S.lessons) {
    const tr = el('tr', l.status === 'retired' ? 'retired' : ''), st = l.stats || {};
    const txt = el('td', 'txt');
    if (l.kind) txt.append(el('span', 'kind', l.kind));
    txt.append(el('span', 'body', l.text || ''));
    if (revised.has(l._id)) txt.append(el('span', 'why rev', 'revised in GEN ' + revised.get(l._id)));
    if (l.retired_reason) txt.append(el('span', 'why', 'retired: ' + l.retired_reason));
    const status = el('td'); status.append(el('span', 'chip ' + (l.status || ''), l.status || ''));
    const evd = el('td', 'evd');
    for (const e of l.evidence || []) evd.append(el('div', 'what', `${Number.isInteger(e.level) ? 'L' + e.level : ''}${e.rule ? ' / ' + e.rule : ''}`));
    tr.append(el('td', 'id', l._id), el('td', 'm', Number.isInteger(l.level) ? 'after L' + l.level : '-'), txt, status, evd,
      el('td', 'm n', st.recalled == null ? '-' : String(st.recalled)));
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
  $('#pickerWrap').hidden = tab === 'memory';  // the evolution tab is about the run in the picker
  $('#modeSeg').hidden = tab !== 'live';
  if (tab === 'evolution' && S.runId && S.evoFor !== S.runId) ensureEvolution(evoRun());
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
  for (const b of $('#tabs').children) b.onclick = () => {
    // Memory is per game: the tab opens on the selected run's documents (its own "all runs" button lifts the filter).
    if (b.dataset.tab === 'memory' && window.MemoryTab && S.runId) MemoryTab.setRun(S.runId);
    setTab(b.dataset.tab);
  };
  for (const b of $('#modeSeg').children) b.onclick = () => setMode(b.dataset.mode);
  for (const b of $('#speedSeg').children) b.onclick = () => {
    // A narrow transport (720p) shows only the current speed: that one button steps to the next speed.
    const compact = [...$('#speedSeg').children].some((x) => x !== b && x.offsetParent === null);
    S.speed = compact ? SPEEDS[(SPEEDS.indexOf(S.speed) + 1) % SPEEDS.length] : +b.dataset.speed;
    renderMode(); syncUrl();
  };
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
  if (window.MemoryTab) {
    MemoryTab.mount($('#tab-memory'));
    if (S.tab === 'memory' && q.get('run') && !q.get('run_id')) MemoryTab.setRun(q.get('run'));
  }
  addEventListener('mongoarc:open-run', (e) => { if (e.detail && e.detail.run_id) openRun(e.detail.run_id); });
  bind(); setTab(S.tab); renderMode(); renderHud(); renderHarness(); syncFeed(); syncRulebook();
  for (;;) {  // the palette comes from db.PALETTE; nothing is drawn without it
    try { setPalette(await api('/api/palette')); setConn(true); break; } catch (e) { setConn(false); await sleep(1000); }
  }
  loop();
}
boot();
