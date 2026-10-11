/* mongoARC on-screen controller.
 *
 * window.ArcController shows what the agent pressed, the way a stream overlay shows a player's
 * controller: a pad (d-pad ACTION1..4, ACTION5, ACTION7, RESET, a click indicator for ACTION6),
 * an overlay that marks a click on the 64x64 board, and a tape of the most recent actions.
 *
 *   mountPad(el, {layout: "column" | "row", bare: false})
 *   mountOverlay(wrapEl)            wrapEl wraps the board canvas
 *   mountTape(el, {max: 14})
 *   setAvailable(list)              dims what the game does not offer; null/[] means "unknown, dim nothing"
 *   press(action, params)           the one call per action; ACTION6 takes {x, y}
 *   clickAt(x, y) / clearClick()    the board mark alone
 *   reset()                         clears counters, tape and overlay (viewer switched run)
 *   setSpeed(multiplier)            shortens the animations when playback runs fast
 * Extras, not needed for wiring:
 *   load(rows)                      rebuild state from [{action, params}] without animating (seek/scrub)
 *   freeze(fraction | null)         hold animations at a fixed point, for screenshots
 *   state()                         a copy of the counters, tape and last action
 *
 * Buttons carry the action number and the conventional glyph only; a game may give them any meaning.
 * Nothing here uses timers: the decay is CSS, so presses cannot pile up. No call throws.
 */
(function (global) {
  'use strict';
  if (global.ArcController && global.ArcController.__ctl) return;

  var FLASH_MS = 400, RIPPLE_MS = 1200, TAPE_MAX = 14, TAPE_KEEP = 64, SIDE = 64;

  function svg(inner) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + inner + '</svg>'; }
  var STROKE = ' fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"';
  var GLYPH = {
    up: svg('<path d="M12 2.5 21.5 14H15.5V21.5H8.5V14H2.5z"/>'),
    down: svg('<path d="M12 21.5 2.5 10H8.5V2.5H15.5V10H21.5z"/>'),
    left: svg('<path d="M2.5 12 14 2.5V8.5H21.5V15.5H14V21.5z"/>'),
    right: svg('<path d="M21.5 12 10 21.5V15.5H2.5V8.5H10V2.5z"/>'),
    space: svg('<path d="M2 8H5.5V14H18.5V8H22V17.5H2z"/>'),
    undo: svg('<path d="M9 13.5 3.5 8 9 2.5"' + STROKE + '/><path d="M4.5 8H14a6.2 6.2 0 0 1 0 12.4H8"' + STROKE + '/>')
  };
  var BUTTONS = {
    ACTION1: { tag: 'A1', cls: 'up', glyph: 'up', hint: 'conventionally up' },
    ACTION2: { tag: 'A2', cls: 'down', glyph: 'down', hint: 'conventionally down' },
    ACTION3: { tag: 'A3', cls: 'left', glyph: 'left', hint: 'conventionally left' },
    ACTION4: { tag: 'A4', cls: 'right', glyph: 'right', hint: 'conventionally right' },
    ACTION5: { tag: 'A5', cls: 'a5', glyph: 'space', hint: 'conventionally space / interact' },
    ACTION6: { tag: 'A6', cls: 'a6', glyph: null, hint: 'click at a pixel of the board' },
    ACTION7: { tag: 'A7', cls: 'a7', glyph: 'undo', hint: 'conventionally undo' },
    RESET: { tag: 'RESET', cls: 'reset', glyph: null, hint: 'restart the level or the game' }
  };

  var S = {
    counts: {},        // action name -> presses
    tape: [],          // {a, known, x, y}
    last: null,        // name of the last pressed button, null if none or unknown
    lastClick: null,   // {x, y} | {x: null, y: null}: what the indicator shows
    mark: null,        // {x, y, cx, cy, side}: what the overlay shows
    avail: null,       // {name: true} or null when unknown
    speed: 1,
    frozen: null
  };
  var pads = [], overlays = [], tapes = [];
  var warned = {};

  /* ---------- helpers ---------- */
  function h(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function resolve(el) {
    if (typeof el === 'string') { try { el = document.querySelector(el); } catch (e) { el = null; } }
    return el && el.nodeType === 1 ? el : null;
  }
  function norm(a) {
    if (typeof a === 'number' && isFinite(a)) return a === 0 ? 'RESET' : 'ACTION' + a;
    if (a == null) return '';
    var s = String(a).trim().toUpperCase();
    if (/^\d+$/.test(s)) return s === '0' ? 'RESET' : 'ACTION' + Number(s);
    return s;
  }
  function coords(p) {
    if (typeof p === 'string') { try { p = JSON.parse(p); } catch (e) { return null; } }
    if (Array.isArray(p)) p = { x: p[0], y: p[1] };
    if (!p || typeof p !== 'object' || p.x == null || p.y == null) return null;
    var x = Number(p.x), y = Number(p.y);
    if (!isFinite(x) || !isFinite(y)) return null;
    return { x: Math.round(x), y: Math.round(y) };
  }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function fmtCount(n) { return !n ? '' : n < 1000 ? '×' + n : '×' + (n / 1000).toFixed(n < 10000 ? 1 : 0) + 'k'; }
  function clickText() {
    var c = S.lastClick;
    if (!c) return 'click x=-- y=--';
    return c.x == null ? 'click x=? y=?' : 'click x=' + c.x + ' y=' + c.y;
  }
  function dress(root) {
    var m = S.speed;
    root.style.setProperty('--ctl-flash-ms', Math.round(Math.max(70, FLASH_MS / m)) + 'ms');
    root.style.setProperty('--ctl-ripple-ms', Math.round(Math.max(180, RIPPLE_MS / m)) + 'ms');
    root.classList.toggle('ctl-frozen', S.frozen != null);
    if (S.frozen != null) root.style.setProperty('--ctl-freeze', String(S.frozen)); else root.style.removeProperty('--ctl-freeze');
  }
  function eachRoot(fn) {
    pads.concat(overlays, tapes).forEach(function (m) { fn(m.root); });
  }
  function unmount(list, host) {
    return list.filter(function (m) {
      if (m.host !== host) return true;
      if (m.root.parentNode) m.root.parentNode.removeChild(m.root);
      return false;
    });
  }
  function restart(el, cls) { el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); }

  /* ---------- pad ---------- */
  function keyHTML(name) {
    var d = BUTTONS[name];
    return '<div class="ctl-btn ctl-b-' + d.cls + '" data-ctl="' + name + '" title="' + name + ' (' + d.hint + ')">' +
      (name === 'RESET' ? '<span class="ctl-word">RESET</span>' : '<span class="ctl-tag">' + d.tag + '</span>' + GLYPH[d.glyph]) +
      '<span class="ctl-n"></span></div>';
  }
  function padHTML() {
    return '<div class="ctl-pad-in">' +
      '<div class="ctl-dpad">' + keyHTML('ACTION1') + keyHTML('ACTION3') + '<div class="ctl-hub"></div>' + keyHTML('ACTION4') + keyHTML('ACTION2') + '</div>' +
      '<div class="ctl-side">' + keyHTML('ACTION5') + keyHTML('ACTION7') + keyHTML('RESET') + '</div>' +
      '<div class="ctl-btn ctl-clickind" data-ctl="ACTION6" title="ACTION6 (' + BUTTONS.ACTION6.hint + ')">' +
        '<span class="ctl-mini"><i class="ctl-mini-h"></i><i class="ctl-mini-v"></i><i class="ctl-mini-dot"></i></span>' +
        '<span class="ctl-tag">A6</span><span class="ctl-clicktxt"></span><span class="ctl-n"></span></div>' +
      '</div>';
  }
  function syncClickInd(p) {
    var b = p.btn.ACTION6; if (!b) return;
    var c = S.lastClick, has = !!c && c.x != null;
    b.classList.toggle('ctl-has', !!c);
    p.clickTxt.textContent = clickText();
    var side = (c && c.side) || SIDE;
    p.mini.style.setProperty('--ctl-mx', has ? String(clamp((c.x + 0.5) / side * 100, 14, 86)) : '50');
    p.mini.style.setProperty('--ctl-my', has ? String(clamp((c.y + 0.5) / side * 100, 14, 86)) : '50');
  }
  function syncPad(p) {
    Object.keys(p.btn).forEach(function (name) {
      var b = p.btn[name];
      b.classList.remove('ctl-hit');
      b.classList.toggle('ctl-last', S.last === name);
      b.classList.toggle('ctl-off', !!S.avail && name !== 'RESET' && !S.avail[name]);
      p.count[name].textContent = fmtCount(S.counts[name] || 0);
    });
    syncClickInd(p);
  }
  function mountPad(el, opts) {
    el = resolve(el); if (!el) return null;
    opts = opts || {};
    pads = unmount(pads, el);
    var row = opts.layout === 'row';
    var root = h('div', 'ctl-root ctl-pad ' + (row ? 'ctl-row' : 'ctl-col') + (opts.bare ? ' ctl-bare' : ''), padHTML());
    var p = { host: el, root: root, btn: {}, count: {} };
    Array.prototype.forEach.call(root.querySelectorAll('[data-ctl]'), function (b) {
      var name = b.getAttribute('data-ctl');
      p.btn[name] = b; p.count[name] = b.querySelector('.ctl-n');
    });
    p.clickTxt = root.querySelector('.ctl-clicktxt');
    p.mini = root.querySelector('.ctl-mini');
    dress(root); syncPad(p);
    el.appendChild(root);
    pads.push(p);
    return root;
  }

  /* ---------- overlay ---------- */
  function drawMark(o, still) {
    while (o.root.firstChild) o.root.removeChild(o.root.firstChild);
    var c = S.mark; if (!c) return;
    var m = h('div', 'ctl-mark' + (still ? ' ctl-still' : ''),
      '<i class="ctl-xh ctl-xa"></i><i class="ctl-xh ctl-xb"></i><i class="ctl-xv ctl-xa"></i><i class="ctl-xv ctl-xb"></i>' +
      '<i class="ctl-burst"></i><i class="ctl-ring"></i><i class="ctl-ring ctl-ring2"></i><i class="ctl-cell"></i>');
    m.style.setProperty('--ctl-x', String(c.cx));
    m.style.setProperty('--ctl-y', String(c.cy));
    m.style.setProperty('--ctl-side', String(c.side));
    var lab = h('b', 'ctl-coord');
    lab.textContent = 'x=' + c.x + ' y=' + c.y;
    // the label sits diagonally off the cell, on whichever side keeps it inside the board
    var right = c.cx < c.side * 0.55, below = c.cy < c.side * 0.78, off = ' + 16px)';
    if (right) lab.style.left = 'calc(' + ((c.cx + 1) / c.side * 100) + '%' + off;
    else lab.style.right = 'calc(' + ((c.side - c.cx) / c.side * 100) + '%' + off;
    if (below) lab.style.top = 'calc(' + ((c.cy + 1) / c.side * 100) + '%' + off;
    else lab.style.bottom = 'calc(' + ((c.side - c.cy) / c.side * 100) + '%' + off;
    m.appendChild(lab);
    o.root.appendChild(m);
  }
  function mountOverlay(wrapEl) {
    var wrap = resolve(wrapEl); if (!wrap) return null;
    overlays = unmount(overlays, wrap);
    try { if (global.getComputedStyle(wrap).position === 'static') wrap.style.position = 'relative'; } catch (e) { /* detached */ }
    var root = h('div', 'ctl-root ctl-overlay');
    var o = { host: wrap, root: root };
    dress(root);
    wrap.appendChild(root);
    overlays.push(o);
    drawMark(o, true);
    return root;
  }
  function setMark(x, y, side) {
    x = Number(x); y = Number(y);
    if (!isFinite(x) || !isFinite(y)) { S.mark = null; return; }
    side = Number(side) > 0 ? Math.round(Number(side)) : SIDE;
    x = Math.round(x); y = Math.round(y);
    S.mark = { x: x, y: y, cx: clamp(x, 0, side - 1), cy: clamp(y, 0, side - 1), side: side };
  }
  function clickAt(x, y, side) {
    setMark(x, y, side);
    overlays.forEach(function (o) { drawMark(o, false); });
  }
  function clearClick() {
    S.mark = null;
    overlays.forEach(function (o) { drawMark(o, true); });
  }

  /* ---------- tape ---------- */
  function chip(e, isNew, animate) {
    var c = h('span', 'ctl-chip');
    var d = e.known ? BUTTONS[e.a] : null;
    if (e.a === 'ACTION6') {
      c.className += ' ctl-c-click';
      c.appendChild(h('i', 'ctl-dot'));
      c.appendChild(document.createTextNode(e.x == null ? '?' : e.x + ',' + e.y));
      c.title = 'ACTION6' + (e.x == null ? '' : ' x=' + e.x + ' y=' + e.y);
    } else if (e.a === 'RESET') {
      c.className += ' ctl-c-reset'; c.textContent = 'R'; c.title = 'RESET';
    } else if (d && d.glyph) {
      c.innerHTML = GLYPH[d.glyph]; c.title = e.a;
    } else {
      c.className += ' ctl-c-unknown'; c.textContent = '?'; c.title = e.a || 'unknown action';
    }
    if (isNew) c.className += ' ctl-new';
    if (animate) c.className += ' ctl-in';
    return c;
  }
  function measureTape(t) { t.root.classList.toggle('ctl-over', t.run.scrollWidth > t.root.clientWidth + 1); }
  function syncTape(t) {
    t.run.textContent = '';
    var rows = S.tape.slice(-t.max);
    if (!rows.length) t.run.appendChild(h('span', 'ctl-tape-none', 'NO ACTIONS YET'));
    rows.forEach(function (e, i) { t.run.appendChild(chip(e, i === rows.length - 1, false)); });
    measureTape(t);
  }
  function pushTape(t, e) {
    var none = t.run.querySelector('.ctl-tape-none'); if (none) t.run.removeChild(none);
    var prev = t.run.querySelector('.ctl-new'); if (prev) prev.classList.remove('ctl-new', 'ctl-in');
    t.run.appendChild(chip(e, true, true));
    while (t.run.children.length > t.max) t.run.removeChild(t.run.firstChild);
    measureTape(t);
  }
  function mountTape(el, opts) {
    el = resolve(el); if (!el) return null;
    opts = opts || {};
    tapes = unmount(tapes, el);
    var root = h('div', 'ctl-root ctl-tape'), run = h('div', 'ctl-tape-run');
    root.appendChild(run);
    var t = { host: el, root: root, run: run, max: clamp(Math.round(Number(opts.max)) || TAPE_MAX, 1, TAPE_KEEP) };
    dress(root);
    el.appendChild(root);
    tapes.push(t);
    syncTape(t);
    return root;
  }

  /* ---------- state changes ---------- */
  function record(action, params) {
    if (action && typeof action === 'object') { if (params === undefined) params = action.params; action = action.action; }
    var name = norm(action), known = Object.prototype.hasOwnProperty.call(BUTTONS, name);
    var e = { a: name, known: known, x: null, y: null };
    if (known) S.counts[name] = (S.counts[name] || 0) + 1;
    S.last = known ? name : null;
    S.mark = null;
    if (name === 'ACTION6') {
      var xy = coords(params);
      if (xy) {
        e.x = xy.x; e.y = xy.y;
        var side = params && typeof params === 'object' && Number(params.side) > 0 ? Number(params.side) : SIDE;
        S.lastClick = { x: xy.x, y: xy.y, side: side };
        setMark(xy.x, xy.y, side);
      } else S.lastClick = { x: null, y: null };
    }
    S.tape.push(e);
    if (S.tape.length > TAPE_KEEP) S.tape.shift();
    return e;
  }
  function syncAll() {
    pads.forEach(syncPad);
    tapes.forEach(syncTape);
    overlays.forEach(function (o) { drawMark(o, true); });
  }
  function press(action, params) {
    var e = record(action, params), name = e.a;
    pads.forEach(function (p) {
      Object.keys(p.btn).forEach(function (k) { if (k !== name) p.btn[k].classList.remove('ctl-hit', 'ctl-last'); });
      var b = p.btn[name]; if (!b) return;
      p.count[name].textContent = fmtCount(S.counts[name]);
      if (name === 'ACTION6') syncClickInd(p);
      b.classList.add('ctl-last');
      restart(b, 'ctl-hit');
    });
    tapes.forEach(function (t) { pushTape(t, e); });
    // a click replaces the mark; any other action removes the faint one left by the last click
    overlays.forEach(function (o) { drawMark(o, false); });
  }
  function load(rows) {
    S.counts = {}; S.tape = []; S.last = null; S.lastClick = null; S.mark = null;
    (Array.isArray(rows) ? rows : []).forEach(function (r) { if (r != null) record(r); });
    syncAll();
  }
  function reset() { load([]); }
  function setAvailable(list) {
    var map = null;
    if (Array.isArray(list) && list.length) {
      map = {};
      list.forEach(function (a) { map[norm(a)] = true; });
    }
    S.avail = map;
    pads.forEach(syncAvail);
  }
  function syncAvail(p) {
    Object.keys(p.btn).forEach(function (name) {
      p.btn[name].classList.toggle('ctl-off', !!S.avail && name !== 'RESET' && !S.avail[name]);
    });
  }
  function setSpeed(m) {
    m = Number(m);
    S.speed = isFinite(m) && m > 1 ? m : 1;
    eachRoot(dress);
  }
  function freeze(fraction) {
    var f = Number(fraction);
    S.frozen = fraction == null || fraction === false || !isFinite(f) ? null : clamp(f, 0, 1);
    eachRoot(dress);
  }
  function state() {
    var counts = {}; Object.keys(S.counts).forEach(function (k) { counts[k] = S.counts[k]; });
    return {
      counts: counts, last: S.last, speed: S.speed,
      lastClick: S.lastClick ? { x: S.lastClick.x, y: S.lastClick.y } : null,
      mark: S.mark ? { x: S.mark.x, y: S.mark.y } : null,
      available: S.avail ? Object.keys(S.avail) : null,
      tape: S.tape.map(function (e) { return { action: e.a, x: e.x, y: e.y }; }),
      mounted: { pads: pads.length, overlays: overlays.length, tapes: tapes.length }
    };
  }

  // The controller is decoration on a live view: a bad argument must never break the page around it.
  function safe(name, fn) {
    return function () {
      try { return fn.apply(null, arguments); }
      catch (err) {
        if (!warned[name] && global.console) { warned[name] = true; global.console.warn('ArcController.' + name + ' failed:', err); }
        return null;
      }
    };
  }
  var api = { __ctl: 1 };
  var fns = { mountPad: mountPad, mountOverlay: mountOverlay, mountTape: mountTape, setAvailable: setAvailable, press: press,
    clickAt: clickAt, clearClick: clearClick, reset: reset, setSpeed: setSpeed, load: load, freeze: freeze, state: state };
  Object.keys(fns).forEach(function (k) { api[k] = safe(k, fns[k]); });
  global.ArcController = api;
})(window);
