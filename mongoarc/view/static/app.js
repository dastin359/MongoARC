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
const RUN_COLOR = '#1E93FF';  // every point of the selected run in the LEVELS chart

const q = new URLSearchParams(location.search);
const S = {
  tab: { levels: 'levels', evolution: 'levels', memory: 'memory' }[q.get('tab')] || 'live',  // ?tab=evolution is the tab's earlier name
  mode: ['live', 'replay'].includes(q.get('mode')) ? q.get('mode') : (q.get('run') ? 'auto' : 'live'),
  speed: SPEEDS.includes(+q.get('speed')) ? +q.get('speed') : 1,
  playing: q.get('autoplay') === '1',
  follow: !q.get('run'), wantRun: q.get('run'),
  wantPos: /^\d+$/.test(q.get('n') || '') ? +q.get('n') : null,
  runs: [], runsLoaded: false, runId: null, run: null,
  actions: [], early: [], stats: [], statTail: null, statN: -1, statsOk: true, base: 0, loadedTo: -1, pos: -1,
  events: [], lastSeq: 0, feedStart: 0, feedEnd: 0, feedNone: true, feedPinned: true,
  rulebooks: [], rbKey: null, rbDirty: true, rbLoaded: false, evLoaded: false, liveInit: false, settled: false,
  notes: [], evoEvents: [], evoPartial: false, evoFor: null,  // the selected run's notes and its memory events (writes, searches, harness prompts)
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
    // The level-boundary message is recorded three times (the harness's own event, the outbox line, and the
    // session's echo of it as a user message). Show it once, as the harness event.
    const echo = (e.kind === 'outbox' && e.outbox_kind === 'note')
      || (e.kind === 'user_msg' && /cleared\. You are on level/.test(e.text || '') && /memory\.py/.test(e.text || ''));
    if (echo) { S.lastSeq = e.seq; continue; }
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
  if (S.evoFor !== id) { S.notes = []; S.evoEvents = []; S.evoPartial = false; S.evoFor = null; markEvolution(); }
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
  renderMode(); renderPicker(); renderHud(); renderMemory(); syncFeed(); syncRulebook(); syncUrl();
  ensureEvolution(r);
  await pollRun();
}
// The notes and memory events the page holds are another run's: fetch this run's now, not on the next slow tick.
function ensureEvolution(r) {
  if (!r) return;
  if (S.evoKey === r._id && (S.evoFor === r._id || S.evoPending)) return;
  if (S.evoPending) { S.evoAgain = true; return; }
  S.evoKey = r._id; S.evoPending = true;
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
    }
    let got = 0;
    // The feed first: it is small, and must not wait behind megabytes of boards.
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
    renderHud(); renderMemory(); syncScrub(); syncFeed(); syncRulebook(); renderMode(); markEvolution();
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
  // Of the games in flight, one with memory comes before a control run.
  const alive = S.runs.filter((r, i) => r.status === 'running' && !dead(r, i));
  const running = alive.find((r) => r.role !== 'control') || alive[0];
  const pick = running || S.runs[0];
  if (!S.runs.length && !S.runId) clearBoard('No runs yet. Launch a game and it will appear here.');
  if (S.follow && pick && (S.runId == null || (running && running._id !== S.runId && S.mode === 'live'))) {
    return selectRun(pick._id, S.mode === 'auto' ? 'live' : S.mode);
  }
}
// Notes and memory events belong to one run: nothing is read from, or shown for, any other game.
async function refreshEvolution() {
  const rid = S.runId;
  if (!rid) { S.notes = []; S.evoEvents = []; S.evoFor = null; markEvolution(); return renderMemory(); }
  const [notes, evs] = await Promise.all([api(`/api/memory/notes?run_id=${enc(rid)}&limit=500`),
    api(`/api/runs/${enc(rid)}/events?kind=recall,harness,memory_write&limit=2000`)]);
  if (rid !== S.runId) return;
  // Filtered here as well: a server that ignores run_id or kind answers with everything.
  S.notes = notes.filter((x) => x.run_id === rid);
  S.evoEvents = evs.filter((e) => e.kind === 'recall' || e.kind === 'harness' || e.kind === 'memory_write');
  S.evoPartial = evs.length >= 2000;
  S.evoFor = rid;
  markEvolution(); renderMemory(); syncRulebook();
}
let tickN = 0;
async function tick() {
  const n = tickN++;
  if (n % 6 === 0 || !S.runId) await refreshRuns();
  if (S.tab !== 'memory' && n % 4 === 0) await refreshEvolution().catch(console.warn);  // the notes panels follow a live game
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
  // Only a run recorded under the earlier design carries gen_k; a run of the memory design shows no such chip.
  $('#genChip').textContent = r && Number.isInteger(r.gen_k) ? 'GEN ' + r.gen_k : '';
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
    : running ? `action ${Math.max(0, S.loadedTo)}` + (backlog > 3 ? ` · board is ${backlog} moves behind, playing them now` : " · waiting for the agent's next move")
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

/* ---------- memory panels: this run's notes, and what it last did with its memory ---------- */
const MEM_OPS = { save: 'SAVED', update: 'UPDATED', delete: 'DELETED' };
// "<run_id>:mem/3" is note N3 of its run.
const noteShort = (id) => { const m = /mem\/([^/:]+)$/.exec(String(id)); return m ? 'N' + m[1] : String(id).split(':').pop(); };
const isMemNote = (x) => x.source === 'tool' || /:mem\/[^/:]+$/.test(String(x._id));
const changedAt = (x) => Date.parse(x.updated_at || x.created_at) || 0;
// The selected run's notes, newest change first.
function runNotes() { return S.evoFor === S.runId ? S.notes.filter(isMemNote).sort((a, b) => changedAt(b) - changedAt(a)) : []; }
function openMemoryTab() {
  if (window.MemoryTab) { MemoryTab.setRun(S.runId || ''); MemoryTab.open('notes'); }
  setTab('memory');
}
function syncRulebook() {
  const notes = runNotes();
  // A run recorded under the earlier design has rule books and no memory notes: its panel stays the rule book.
  if (!notes.length && S.rulebooks.length) return syncOldRulebook();
  const key = ['notes', S.runId, S.evoFor, S.rbLoaded, notes.map((x) => x._id + '.' + x.version).join(',')].join(':');
  if (key === S.rbKey) return;
  S.rbKey = key;
  $('#rbTitle').textContent = 'MEMORY NOTES';
  $('#rbVersion').textContent = notes.length ? `${notes.length} in db.notes, newest change first` : '';
  const box = $('#rulebook');
  box.replaceChildren();
  if (!notes.length) {
    return void box.append(el('div', 'none', !S.run ? 'No run selected.'
      : isControl(S.run) ? 'None: a control run has no memory.'
      : S.evoFor !== S.runId ? 'Loading the notes of this run.'
      : 'No notes yet. Every lesson the agent saves with memory.py appears here as a MongoDB document.'));
  }
  for (const x of notes) {
    const d = el('div', 'mnote'), h = el('div', 'mh');
    h.append(el('b', null, noteShort(x._id)), el('strong', null, x.title || ''), el('span', null, 'v' + (x.version || 1)));
    d.append(h, el('pre', null, x.text || ''));
    d.onclick = openMemoryTab;
    box.append(d);
  }
  box.scrollTop = 0;
}
function renderMemory() {
  const mine = !!S.runId && S.evoFor === S.runId, notes = runNotes();
  const evs = mine ? S.evoEvents.filter((e) => e.kind === 'memory_write' || (e.kind === 'recall' && e.op !== 'list')) : [];
  if (!once('mempanel', [S.runId, mine, notes.length, evs.length, evs.length ? evs[evs.length - 1].seq : 0, isControl(S.run)])) return;
  const n = (op) => evs.filter((e) => e.kind === 'memory_write' && e.op === op).length;
  $('#memCount').textContent = mine ? String(notes.length) : '-';
  $('#memSaved').textContent = mine ? String(n('save')) : '-';
  $('#memUpdated').textContent = mine ? String(n('update')) : '-';
  $('#memDeleted').textContent = mine ? String(n('delete')) : '-';
  $('#memSearched').textContent = mine ? String(evs.filter((e) => e.kind === 'recall').length) : '-';
  $('#memBanksInfo').textContent = evs.length ? `all ${evs.length} memory events of this game, newest first` : '';
  const box = $('#memEvents');
  box.replaceChildren();
  if (!evs.length) {
    return void box.append(el('div', 'none', !S.runId ? 'No run selected.' : isControl(S.run) ? 'A control run has no memory.'
      : !mine ? 'Loading.' : 'Nothing saved or searched yet.'));
  }
  for (const e of evs.slice().reverse()) {  // every one, newest first
    const search = e.kind === 'recall', row = el('div', 'mev ' + (search ? 'search' : e.op || ''));
    const head = el('div', 'mh');
    head.append(el('b', null, search ? 'SEARCHED' : MEM_OPS[e.op] || 'WROTE'), el('span', 'lv', Number.isInteger(e.level) ? 'level ' + e.level : ''));
    if (search) {
      const ids = e.notes || [];
      head.append(el('span', 'txt', e.query ? '“' + e.query + '”' : '(no question)'));
      row.append(head, el('div', 'sub', (ids.length ? `→ ${ids.length} note${ids.length === 1 ? '' : 's'} returned:  ${ids.map(noteShort).join('  ')}` : '→ no note returned'
        + (Number.isInteger(e.stored) ? ` (${e.stored} stored)` : '')) + (e.engine === 'atlas_vector_search' ? '   ·   Atlas Vector Search' : e.engine === 'text' ? '   ·   text index' : '')));
    } else {
      head.append(el('span', 'txt', [e.note ? noteShort(e.note) : '', e.title || ''].filter(Boolean).join('  ')));
      const body = String(e.text || '').replace(/^agent (saved|updated|deleted) note [^"]*"[^"]*"(?: \(version \d+\))?:\s*/, '');
      row.append(head, el('div', 'sub', body));
    }
    row.onclick = openMemoryTab;
    box.append(row);
  }
  box.scrollTop = 0;
}

/* The rule book of a run recorded under the earlier design. */
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
function syncOldRulebook() {
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
  $('#rbTitle').textContent = 'RULE BOOK';
  $('#rbVersion').textContent = rb ? `version ${rb.version} of ${S.rulebooks.length}, at action ${rb.action_n}` : '';
  if (!rb) return void box.append(el('div', 'none', 'No rules recorded up to this action.'));
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
      label = 'HARNESS · ' + (e.what || 'event');
      body.textContent = e.text || '';
      if (e.what === 'memory_nudge') {  // what the harness tells the agent at a level boundary
        d.classList.add('nudge');
        label = 'HARNESS → AGENT · LEVEL CLEARED';
      } else if ((e.lessons || []).length) body.append(el('div', 'ids', e.lessons.join('  ')));  // a run recorded under the earlier design
      break;
    }
    case 'recall': {  // the agent asked its memory a plain-language question (memory.py recall)
      d.classList.add('mem', 'recall');
      label = 'MEMORY · SEARCH';
      const ids = [...(e.lessons || []), ...(e.notes || [])];
      if (e.query) body.append(el('div', 'q', e.query));
      body.append(el('div', 'ids', `${ids.length} note${ids.length === 1 ? '' : 's'} returned` + (ids.length ? ':  ' + ids.map(noteShort).join('  ') : '')
        + (e.engine === 'atlas_vector_search' ? '  ·  via Atlas Vector Search' : '')));
      break;
    }
    case 'memory_write': {  // the agent saved, corrected or deleted a note in this run's memory (memory.py)
      d.classList.add('mem');
      label = 'MEMORY · ' + (MEM_OPS[e.op] || 'WRITE');
      const head = [e.note ? noteShort(e.note) : '', e.title || ''].filter(Boolean).join('  ');
      if (head) body.append(el('div', 'ids', head));
      if (e.text) body.append(el('div', e.op === 'delete' ? 'gone' : null, e.text));
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
let recallTimer = null;
function recallBanner(e) {  // the agent just asked its memory a question: say so where the audience is looking
  const wrap = $('#boardWrap');
  if (!wrap || !e) return;
  let b = $('#recallBanner');
  if (!b) { b = el('div', 'recallbanner'); b.id = 'recallBanner'; wrap.append(b); }
  const n = (e.notes || []).length;
  b.replaceChildren(el('div', 'rb-h', 'AGENT IS SEARCHING ITS MEMORY'), el('div', 'rb-q', e.query ? '\u201c' + e.query + '\u201d' : '(no question)'),
    el('div', 'rb-r', `${n} note${n === 1 ? '' : 's'} returned` + (n ? ':  ' + e.notes.map(noteShort).join('  ') : '')
      + (e.engine === 'atlas_vector_search' ? '   ·   MongoDB Atlas Vector Search' : '')));
  b.classList.remove('on'); void b.offsetWidth; b.classList.add('on');
  clearTimeout(recallTimer); recallTimer = setTimeout(() => b.classList.remove('on'), 5200);
  const panel = document.querySelector('.membanks'); if (panel) { panel.classList.remove('recallpulse'); void panel.offsetWidth; panel.classList.add('recallpulse'); }
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
  const fresh = end - S.feedEnd;
  for (let i = S.feedEnd; i < end; i++) box.append(renderEvent(ev[i]));
  if (fresh > 0 && fresh <= 40) {  // stepping forward, not a jump: announce the newest memory search that just appeared
    for (let i = end - 1; i >= end - fresh; i--) if (ev[i].kind === 'recall' && ev[i].op !== 'list') { recallBanner(ev[i]); break; }
  }
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

/* ---------- tab 2, LEVELS: what the run in the RUN picker spent on each level and what it wrote to memory ---------- */
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
// One row per level of a run, from the run's metrics, else the collector's live counters.
// ratio = actions spent / human baseline; lower is better.
function levelRows(r) {
  if (!r) return [];
  const base = r.baseline_actions || [], m = r.metrics || {}, ml = m.levels || [], costs = r.level_costs || [];
  const cur = Number.isInteger(r.level) ? r.level : m.levels_cleared || 0;
  const N = Math.max(r.win_levels || 0, m.win_levels || 0, base.length, ml.length);
  const rows = [];
  for (let i = 0; i < N; i++) {
    const x = ml.find((v) => v.level === i);
    const cleared = i < cur || !!(x && x.cleared);
    const actions = cleared ? (x && x.actions) || costs[i] || 0 : costs[i] || (x && x.actions) || 0;
    const baseline = (x && x.baseline) || base[i] || null;
    rows.push({ level: i, actions, baseline, cleared, playing: !cleared && i === cur && r.status === 'running',
      ratio: actions && baseline ? actions / baseline : null });
  }
  return rows;
}
// The reference for a run with memory: the most recent finished control run of the same game.
function controlFor(r) {
  if (!r || isControl(r)) return null;
  return S.runs.find((x) => x._id !== r._id && x.role === 'control' && x.game_id === r.game_id && x.status !== 'running') || null;
}
// What the agent did with its memory, level by level. The memory gateway writes a memory_write event for every
// save, update and delete and a recall event for every search; the harness writes a memory_nudge event each
// time it prompts the agent at a level boundary.
function memoryUse() {
  const by = new Map(), ops = { save: 0, update: 0, delete: 0 };
  const at = (lv) => { if (!by.has(lv)) by.set(lv, { searches: [], writes: [], nudges: 0, ops: { save: 0, update: 0, delete: 0 } }); return by.get(lv); };
  let searches = 0, nudges = 0;
  for (const e of S.evoFor === S.runId ? S.evoEvents : []) {
    const u = at(Number.isInteger(e.level) ? e.level : 0);
    if (e.kind === 'recall') { u.searches.push(e); searches++; }
    else if (e.kind === 'memory_write') { u.writes.push(e); if (e.op in ops) { ops[e.op]++; u.ops[e.op]++; } }
    else if (e.kind === 'harness' && e.what === 'memory_nudge') { u.nudges++; nudges++; }
  }
  return { by, ops, searches, nudges, writes: ops.save + ops.update + ops.delete,
    levels: [...by.values()].filter((v) => v.searches.length).length };
}
function markEvolution() {
  const r = evoRun(), c = controlFor(r);
  const live = (x) => x && [x._id, x.status, x.role, x.level, x.level_costs, x.metrics && x.metrics.levels];
  const sig = JSON.stringify([S.runId, S.evoFor, S.notes.map((x) => [x._id, x.version]), S.evoEvents.map((e) => e.seq), S.evoPartial, live(r), live(c)]);
  if (sig === S.evoSig) return;
  S.evoSig = sig; S.evoDirty = true;
  if (S.tab === 'levels') renderEvolution();
}
function renderEvolution() {
  S.evoDirty = false;
  const r = evoRun(), rows = levelRows(r), use = memoryUse();
  const scope = $('#evoScope');
  scope.replaceChildren();
  if (r) {
    scope.append(el('b', null, r.game_id || r._id), el('span', 'rid', r._id), el('span', 'chip ' + (r.status || ''), r.status || ''));
    if (isControl(r)) scope.append(el('span', 'chip ctl', 'CONTROL'));
  } else scope.append(el('span', 'rid', S.runs.length ? 'No run selected: pick one in the RUN picker.' : 'No runs yet.'));
  renderTiles(r, rows, use); renderActivity(r, rows, use); renderChart(r, rows, use); renderNotes(r);
}
function renderTiles(r, rows, use) {
  const box = $('#tiles'), ctl = isControl(r), mine = S.evoFor === S.runId;
  const tile = (num, lbl, cls) => { const t = el('div', 'tile'), n = el('div', 'num' + (cls ? ' ' + cls : '')); n.append(...[].concat(num)); t.append(n, el('div', 'lbl', lbl)); return t; };
  const done = rows.filter((x) => x.cleared), scored = done.filter((x) => x.ratio != null);
  box.replaceChildren(tile([String(done.length), el('small', null, rows.length ? ` of ${rows.length}` : '')], 'LEVELS CLEARED'));
  if (ctl) {
    box.append(tile('none', 'NOTES IN MEMORY: A CONTROL RUN HAS NO MEMORY', 's2'), tile('none', 'MEMORY WRITES', 's2'), tile('none', 'MEMORY SEARCHES', 's2'));
  } else {
    box.append(tile(r && mine ? String(runNotes().length) : '-', 'NOTES IN MEMORY NOW'),
      tile(r && mine ? String(use.writes) : '-', `MEMORY WRITES: ${use.ops.save} SAVED · ${use.ops.update} UPDATED · ${use.ops.delete} DELETED`),
      tile(r && mine ? el('span', use.searches ? '' : 'down', String(use.searches)) : '-',
        `MEMORY SEARCHES, ON ${use.levels} OF ${rows.length || '?'} LEVELS` + (S.evoPartial ? ' · FIRST 2000 EVENTS ONLY' : '')));
  }
  if (scored.length >= 2) {  // like for like inside one game: the first cleared level against the most recent one
    const a = scored[0], b = scored[scored.length - 1];
    box.append(tile([el('small', null, `L${a.level} ${ratioX(a.ratio)} → `), el('span', b.ratio <= a.ratio ? 'up' : 'down', `L${b.level} ${ratioX(b.ratio)}`)],
      'ACTIONS OVER HUMAN BASELINE: FIRST → LATEST CLEARED LEVEL', 's2'));
  } else box.append(tile(scored.length ? `L${scored[0].level} ${ratioX(scored[0].ratio)}` : '-', 'ACTIONS OVER HUMAN BASELINE', 's2'));
}
function activityRow(e) {
  const d = el('div', 'mact ' + (e.kind === 'recall' ? 'search' : e.op || '')), h = el('div', 'h');
  if (e.kind === 'recall') {
    const ids = [...(e.lessons || []), ...(e.notes || [])];
    h.append(el('b', null, 'SEARCHED'), el('span', 't', e.query || ''));
    d.append(h, el('div', 'r', `${ids.length} note${ids.length === 1 ? '' : 's'} came back` + (ids.length ? ':  ' + ids.map(noteShort).join('  ') : '')
      + (e.engine === 'atlas_vector_search' ? '  ·  Atlas Vector Search' : '')));
  } else {
    h.append(el('b', null, MEM_OPS[e.op] || 'WROTE'), el('span', 'id', e.note ? noteShort(e.note) : ''), el('span', 't', e.title || ''));
    d.append(h);
    if (e.text) d.append(el('div', 'x', e.text));
  }
  return d;
}
function renderActivity(r, rows, use) {
  const box = $('#actList');
  box.replaceChildren();
  if (!r) return void box.append(el('div', 'none', 'No run selected.'));
  if (isControl(r)) {
    return void box.append(el('div', 'evonote ctl', 'Control run: launched with --no-memory. It has no memory, so it writes nothing down and searches nothing. It is the reference that a run with memory is compared against.'));
  }
  if (S.evoFor !== S.runId) return void box.append(el('div', 'none', 'Loading the memory activity of this run.'));
  const levels = [...use.by.keys()].filter((lv) => use.by.get(lv).writes.length || use.by.get(lv).searches.length).sort((a, b) => b - a);
  if (!levels.length) {
    return void box.append(el('div', 'evonote', 'No memory activity recorded for this run. Every note the agent saves, corrects or deletes with memory.py, and every search it makes, is listed here.'));
  }
  for (const lv of levels) {
    const u = use.by.get(lv), row = rows[lv], g = el('div', 'mgroup'), hd = el('div', 'mghd');
    hd.append(el('b', null, 'LEVEL ' + lv), el('span', null, !row ? '' : (row.cleared ? `cleared in ${row.actions} actions` : row.playing ? `in play, ${row.actions} actions so far`
      : row.actions ? `not cleared after ${row.actions} actions` : 'not played') + (row.baseline ? ` / human baseline ${row.baseline}` : '')));
    if (row && row.cleared && row.ratio != null) hd.append(el('i', row.ratio <= 1 ? 'good' : row.ratio <= 2 ? 'over' : 'bad', ratioX(row.ratio)));
    g.append(hd);
    for (const e of [...u.writes, ...u.searches].sort((a, b) => b.seq - a.seq)) g.append(activityRow(e));
    box.append(g);
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
  const ctl = controlFor(r), crow = ctl ? levelRows(ctl) : [], self = isControl(r), col = RUN_COLOR;
  const item = (cls, style, text) => { const s = el('span'), i = el('i', cls); if (style) i.style.background = style; s.append(i, text); return s; };
  leg.append(self ? item('ring', null, 'this run (control, no memory)') : item('dot', col, 'this run (with memory)'));
  if (ctl) { const s = item('ring', null, 'control (no memory) '); s.append(runLink(ctl._id)); leg.append(s); }
  // A level a finished run never cleared is an X at the top; a level a running game has not reached yet is left empty.
  const failed = (v, run) => !v.cleared && !v.playing && run.status !== 'running';
  if (rows.some((v) => failed(v, r))) leg.append(item('x' + (self ? ' c' : ''), null, self ? 'level not cleared' : 'this run: level not cleared'));
  if (ctl && crow.slice(0, rows.length).some((v) => failed(v, ctl))) leg.append(item('x c', null, 'control: level not cleared'));
  if (!self) leg.append(el('span', 'mute', 'SAVED: notes written while on that level · SEARCHES: memory searches the agent made there; prompted = the harness reminded it to search when the level began'));
  const L = 78, R = 16, T = 16, B = 76, pw = W - L - R, ph = H - T - B, N = rows.length, slot = pw / N;
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
  s.append(svg('polyline', { points: pts(rows), fill: 'none', stroke: self ? '#8a94a3' : col, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-dasharray': self ? '5 4' : 'none', opacity: self ? 1 : 0.55 }));
  const cross = (cx, cy, c, w) => { for (const d of [1, -1]) s.append(svg('line', { x1: cx - 5, x2: cx + 5, y1: cy - 5 * d, y2: cy + 5 * d, stroke: c, 'stroke-width': w, 'stroke-linecap': 'round' })); };
  for (const v of crow.slice(0, N)) {  // the control run, drawn first so this run's points sit on top
    if (v.cleared && v.ratio != null) s.append(svg('circle', { cx: x(v.level), cy: y(v.ratio), r: 5.5, fill: '#0e1218', stroke: '#b3bdca', 'stroke-width': 2.5 }));
    else if (failed(v, ctl)) cross(x(v.level) + (rows[v.level] && failed(rows[v.level], r) ? 13 : 0), T + 9, '#b3bdca', 2);
  }
  const rowY = [T + ph + 20, T + ph + 36, T + ph + 52, T + ph + 68];
  ['LEVEL', 'ACTIONS', 'SAVED', 'SEARCHES'].forEach((name, k) => s.append(svg('text', { x: L - 8, y: rowY[k], 'text-anchor': 'end', class: 't' }, name)));
  rows.forEach((v, i) => {
    const cx = x(i), u = use.by.get(i) || { searches: [], writes: [], nudges: 0, ops: { save: 0, update: 0, delete: 0 } }, c = crow[i];
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
    const saved = [[u.ops.save, 'new'], [u.ops.update, 'upd'], [u.ops.delete, 'del']].filter((p) => p[0]).map((p) => p[0] + ' ' + p[1]).join(' ');
    s.append(svg('text', { x: cx, y: rowY[2], 'text-anchor': 'middle', class: self || !reached ? '' : saved ? 'w' : v.cleared ? 'z' : '' }, self ? 'no memory' : !reached ? '-' : saved || '0'));
    const st = self ? 'no memory' : !reached ? '-' : String(u.searches.length) + (u.nudges ? ' (prompted)' : '');
    s.append(svg('text', { x: cx, y: rowY[3], 'text-anchor': 'middle', class: self || !reached || (!i && !u.searches.length) ? '' : u.searches.length ? 'ok' : 'z' }, st));  // level 0 starts with an empty memory: nothing to search
    const hit = svg('rect', { x: L + i * slot, y: T, width: slot, height: ph + B, class: 'hit' });
    hit.addEventListener('mousemove', (e) => showTip(e, `level ${i}` + (self ? ' · control run, no memory' : '') + '\n'
      + (v.cleared ? `${v.actions} actions / human baseline ${v.baseline == null ? '?' : v.baseline} = ${ratioX(v.ratio)}`
        : v.playing ? `in play: ${v.actions} actions so far` + (v.baseline ? ` / human baseline ${v.baseline}` : '')
        : v.actions ? `not cleared after ${v.actions} actions` : r.status === 'running' ? 'not reached yet' : 'not reached')
      + (self ? '' : `\nnotes written on this level: ${u.ops.save} saved, ${u.ops.update} updated, ${u.ops.delete} deleted`
        + `\nmemory searches on this level: ${u.searches.length}` + (u.nudges ? ', after a harness prompt' : '')
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
function renderNotes(r) {
  const box = $('#noteTable'), order = (x) => +(/mem\/(\d+)$/.exec(String(x._id)) || [0, 0])[1];
  const notes = runNotes().sort((a, b) => order(a) - order(b));
  box.replaceChildren();
  $('#noteInfo').textContent = notes.length ? `${notes.length} document${notes.length === 1 ? '' : 's'} in db.notes · Atlas embeds each note and answers the agent's plain-language searches` : '';
  if (!notes.length) {
    return void box.append(el('div', 'none', !r ? 'No run selected.'
      : isControl(r) ? 'None: a control run has no memory.'
      : S.evoFor !== S.runId ? 'Loading the notes of this run.'
      : 'No notes in memory for this run. The agent writes them itself with memory.py; every run starts with an empty memory.'));
  }
  const t = el('table', 'lt'), hr = el('tr');
  for (const [h, c] of [['ID'], ['TITLE'], ['SAVED ON'], ['VERSION'], ['NOTE'], ['SCRIPT REFS']]) hr.append(el('th', c, h));
  t.append(hr);
  for (const x of notes) {
    const tr = el('tr'), txt = el('td', 'txt'), refs = el('td', 'refs');
    txt.append(el('span', 'body', x.text || ''));
    for (const ref of x.refs || []) refs.append(el('span', null, ref));
    if (!(x.refs || []).length) refs.textContent = '-';
    tr.append(el('td', 'id', noteShort(x._id)), el('td', 'ttl', x.title || ''), el('td', 'm', Number.isInteger(x.level) ? 'level ' + x.level : '-'),
      el('td', 'm', 'v' + (x.version || 1)), txt, refs);
    t.append(tr);
  }
  box.append(t);
}

/* ---------- shell ---------- */
function setTab(tab) {
  S.tab = tab;
  $('#tab-live').hidden = tab !== 'live';
  $('#tab-levels').hidden = tab !== 'levels';
  $('#tab-memory').hidden = tab !== 'memory';
  if (window.MemoryTab) MemoryTab.setActive(tab === 'memory');
  $('#pickerWrap').hidden = tab === 'memory';  // the LEVELS tab is about the run in the picker
  $('#modeSeg').hidden = tab !== 'live';
  if (tab === 'levels' && S.runId && S.evoFor !== S.runId) ensureEvolution(evoRun());
  for (const b of $('#tabs').children) b.classList.toggle('on', b.dataset.tab === tab);
  if (tab === 'levels') renderEvolution();
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
  addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(() => { pinFeed(); if (S.tab === 'levels') renderEvolution(); }, 120); });
}
/* ---------- start a game from the page ---------- */
async function loadGames() {
  const sel = $('#gameSel'), btn = $('#btnLaunch'), msg = $('#launchMsg');
  if (!sel) return;
  try {
    const r = await api('/api/games');
    if (r.error || !r.games.length) { msg.textContent = r.error || 'no games'; return; }
    for (const g of r.games) {
      const o = el('option', null, `${g.game_id.split('-')[0].toUpperCase()}  ·  ${g.levels} levels`);
      o.value = g.game_id; sel.append(o);
    }
    const pick = $('#modelPick'), hidePick = () => { pick.hidden = true; };
    const start = async (m) => {
      if (!sel.value) return;
      const name = sel.value.split('-')[0].toUpperCase();
      hidePick(); btn.disabled = true; msg.textContent = `starting ${name} on ${m.label} ...`;
      try {
        const res = await fetch('/api/launch', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ game_id: sel.value, model: m.id }) });
        const out = await res.json();
        if (!res.ok) throw new Error(out.detail || 'could not start');
        msg.textContent = `started on ${out.label || m.label}; following it live`;
        S.follow = true; S.mode = 'live'; setTab('live'); $('#runSel').value = '';
        S.runId = null; S.epoch++; stopPlay(); refreshRuns().catch(console.warn);
        setTimeout(() => { msg.textContent = ''; sel.value = ''; }, 9000);
      } catch (e) { msg.textContent = String(e.message || e); btn.disabled = false; }
    };
    for (const m of (r.models && r.models.length ? r.models : [{ id: r.model, label: r.model }])) {   // START asks which model plays
      const b = el('button', null, m.label.toUpperCase());
      b.type = 'button'; b.title = m.id + (r.timeout_s ? ` · the game is stopped after ${Math.round(r.timeout_s / 60)} minutes` : ''); b.onclick = () => start(m); pick.append(b);
    }
    sel.onchange = () => { btn.disabled = !sel.value; msg.textContent = ''; hidePick(); };
    btn.onclick = (ev) => { if (!sel.value) return; ev.stopPropagation(); pick.hidden = !pick.hidden; };
    document.addEventListener('click', (ev) => { if (!pick.hidden && !pick.contains(ev.target)) hidePick(); });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') hidePick(); });
  } catch (e) { msg.textContent = 'game list unavailable'; }
}
/* An open tab reloads itself when the page's own files change on the server, so it never shows an old layout. */
const PAGE_FILES = ['index.html', 'app.js', 'style.css', 'controller.js', 'controller.css', 'memory.js', 'memory.css', 'resize.js', 'resize.css'];
async function pageSignature() {
  const tags = await Promise.all(PAGE_FILES.map((f) => fetch('/static/' + f, { method: 'HEAD', cache: 'no-store' })
    .then((r) => (r.ok ? r.headers.get('etag') || r.headers.get('last-modified') : null))));
  return tags.some((x) => !x) ? null : tags.join('|');
}
async function watchPageFiles() {
  let loaded = null;
  try { loaded = await pageSignature(); } catch (e) { return; }
  if (!loaded) return;
  setInterval(async () => {
    try { const now = await pageSignature(); if (now && now !== loaded) location.reload(); } catch (e) { /* server restarting: ask again next time */ }
  }, 6000);
}
async function boot() {
  loadGames(); watchPageFiles();
  AC.mountOverlay($('#boardWrap')); AC.mountPad($('#ctlPad'), { layout: 'row' }); AC.mountTape($('#ctlTape'), { max: 30 });
  if (window.MemoryTab) {
    MemoryTab.mount($('#tab-memory'));
    if (S.tab === 'memory' && q.get('run') && !q.get('run_id')) MemoryTab.setRun(q.get('run'));
  }
  addEventListener('mongoarc:open-run', (e) => { if (e.detail && e.detail.run_id) openRun(e.detail.run_id); });
  bind(); $('#memPanel').onclick = openMemoryTab; setTab(S.tab); renderMode(); renderHud(); renderMemory(); syncFeed(); syncRulebook();
  for (;;) {  // the palette comes from db.PALETTE; nothing is drawn without it
    try { setPalette(await api('/api/palette')); setConn(true); break; } catch (e) { setConn(false); await sleep(1000); }
  }
  loop();
}
boot();
