/* Drag-to-resize columns. Each tab's main grid gets a thin handle between its
   columns; drag a handle to resize, double-click it to restore the default.
   Widths are remembered in this browser (localStorage). No dependencies. */
(function () {
  'use strict';
  var KEY = 'mongoarc.cols.v1', GAP = 10;
  function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { return {}; } }
  function save(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* private window: not remembered */ } }
  var saved = load();
  var q = new URLSearchParams(location.search);

  /* ---- text size: one factor for the whole page. Automatic by window width (the layout was drawn for a
     1600 px window), or set with the A- / A+ control in the header and remembered in this browser. ---- */
  var UKEY = 'mongoarc.ui.v1', manual = null, hooks = [];
  try { var sv = parseFloat(localStorage.getItem(UKEY)); if (sv >= 0.7 && sv <= 2.2) manual = sv; } catch (e) { /* no storage */ }
  if (parseFloat(q.get('ui'))) manual = parseFloat(q.get('ui'));
  function autoScale() { return Math.max(1, Math.min(1.6, Math.round(window.innerWidth / 1600 * 20) / 20)); }
  function zf() { return manual || autoScale(); }
  function applyScale() {
    document.documentElement.style.setProperty('--ui', String(zf()));
    var lab = document.getElementById('uiscaleLabel');
    if (lab) lab.textContent = Math.round(zf() * 100) + '%' + (manual ? '' : ' auto');
    hooks.forEach(function (f) { f(); });
  }
  function setScale(v) {
    manual = v == null ? null : Math.round(Math.max(0.7, Math.min(2.2, v)) * 20) / 20;
    try { if (manual) localStorage.setItem(UKEY, String(manual)); else localStorage.removeItem(UKEY); } catch (e) { /* not remembered */ }
    applyScale(); window.dispatchEvent(new Event('resize'));
  }
  function scaleControl() {
    var top = document.querySelector('header.top'), conn = document.getElementById('conn');
    if (!top || document.querySelector('.uiscale')) return;
    var box = document.createElement('div');
    box.className = 'uiscale'; box.title = 'Text size. Double-click the figure for automatic.';
    box.innerHTML = '<button type="button" aria-label="Smaller text">A-</button><span id="uiscaleLabel"></span><button type="button" aria-label="Larger text">A+</button>';
    box.children[0].onclick = function () { setScale(zf() - 0.1); };
    box.children[2].onclick = function () { setScale(zf() + 0.1); };
    box.children[1].ondblclick = function () { setScale(null); };
    top.insertBefore(box, conn || null);
    applyScale();
  }
  applyScale();
  window.addEventListener('resize', function () { if (!manual) applyScale(); });

  /* root: a grid whose direct children are the columns.
     opts: {key, defaults: [css width per column], min: [px per column], onWidth(i, px|null)} */
  function attach(root, opts) {
    if (!root || root.dataset.colsplit) return null;
    var cols = Array.prototype.filter.call(root.children, function (el) { return !el.classList.contains('colsplit'); });
    if (cols.length < 2) return null;
    root.dataset.colsplit = '1';
    var n = cols.length, w = (saved[opts.key] || []).slice(0, n - 1);
    while (w.length < n - 1) w.push(null);
    if (opts.key === q.get('colskey') && q.get('cols')) w = q.get('cols').split(',').map(function (x) { return +x || null; });
    var min = opts.min || cols.map(function () { return 200; });
    for (var i = 0; i < n - 1; i++) {
      var h = document.createElement('div');
      h.className = 'colsplit'; h.title = 'Drag to resize. Double-click to reset.';
      h.setAttribute('role', 'separator'); h.setAttribute('aria-orientation', 'vertical');
      cols[i].after(h);
      bind(h, i);
    }
    function apply() {
      var t = [];
      for (var i = 0; i < n - 1; i++) t.push(w[i] != null ? w[i] + 'px' : opts.defaults[i], GAP + 'px');
      t.push(w.some(function (x) { return x != null; }) ? 'minmax(' + min[n - 1] + 'px, 1fr)' : opts.defaults[n - 1]);
      root.style.gridTemplateColumns = t.join(' ');
      root.style.columnGap = '0';
      if (opts.onWidth) for (var j = 0; j < n - 1; j++) opts.onWidth(j, w[j]);
    }
    function total() { var cs = getComputedStyle(root); return root.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - GAP * (n - 1); }
    function bind(h, i) {
      var startX = 0, start = [];
      h.addEventListener('pointerdown', function (e) {
        if (e.button) return;
        e.preventDefault();
        start = cols.slice(0, n - 1).map(function (c) { return c.getBoundingClientRect().width / zf(); });
        w = start.map(function (v, k) { return w[k] != null || k === i ? Math.round(v) : w[k]; });
        // A column left of the dragged handle keeps the width it has now, so only the two neighbours move.
        for (var k = 0; k < i; k++) if (w[k] == null) w[k] = Math.round(start[k]);
        startX = e.clientX;
        try { h.setPointerCapture(e.pointerId); } catch (x) { /* synthetic event */ }
        h.classList.add('drag'); document.body.classList.add('colresizing');
      });
      h.addEventListener('pointermove', function (e) {
        if (!h.classList.contains('drag')) return;
        var others = 0;
        for (var k = 0; k < n - 1; k++) if (k !== i) others += (w[k] != null ? w[k] : start[k]);
        var max = total() - others - min[n - 1];
        w[i] = Math.round(Math.max(min[i], Math.min(max, start[i] + (e.clientX - startX) / zf())));
        apply();
      });
      function end(e) {
        if (!h.classList.contains('drag')) return;
        h.classList.remove('drag'); document.body.classList.remove('colresizing');
        try { h.releasePointerCapture(e.pointerId); } catch (x) { /* already released */ }
        saved[opts.key] = w; save(saved);
        window.dispatchEvent(new Event('resize'));   // charts and the feed re-measure
      }
      h.addEventListener('pointerup', end); h.addEventListener('pointercancel', end);
      h.addEventListener('dblclick', function () {
        w = w.map(function () { return null; });
        delete saved[opts.key]; save(saved); apply();
        window.dispatchEvent(new Event('resize'));
      });
    }
    apply();
    function refit() { if (opts.onWidth) for (var j = 0; j < n - 1; j++) opts.onWidth(j, w[j]); }
    window.addEventListener('resize', refit); hooks.push(refit);
    return { reset: function () { w = w.map(function () { return null; }); apply(); } };
  }

  /* LIVE PLAY: board | panels | feed. The board is square, so a stage narrower than the
     available height shrinks the board, and a wider one centres it. */
  function live() {
    var root = document.getElementById('tab-live');
    if (!root) return;
    var stage = root.querySelector('.stage'), wrap = document.getElementById('boardWrap');
    attach(root, {
      key: 'live', defaults: ['auto', 'minmax(270px, .9fr)', 'minmax(300px, 1.15fr)'], min: [260, 220, 240],
      onWidth: function (i, px) {
        if (i !== 0 || !stage || !wrap) return;
        if (px == null) { stage.style.width = ''; stage.style.removeProperty('--board'); stage.classList.remove('resized'); return; }
        if (!root.clientHeight) return;   // the tab is hidden: it is fitted when it is shown (below)
        var other = 0, kids = stage.children;
        for (var k = 0; k < kids.length; k++) if (kids[k] !== wrap) other += kids[k].offsetHeight;
        var cs = getComputedStyle(root), gap = parseFloat(getComputedStyle(stage).rowGap) || 8;
        var availH = root.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - other - gap * (kids.length - 1);
        stage.classList.add('resized');
        stage.style.width = px + 'px';
        stage.style.setProperty('--board', Math.max(140, Math.min(availH, px)) + 'px');
      },
    });
  }
  function refitOnShow() {  // a tab that was hidden when the page loaded gets its sizes when it is first shown
    var root = document.getElementById('tab-live');
    if (!root || !window.MutationObserver) return;
    new MutationObserver(function () { if (!root.hidden) setTimeout(function () { hooks.forEach(function (f) { f(); }); }, 30); })
      .observe(root, { attributes: true, attributeFilter: ['hidden'] });
  }
  /* EVOLUTION: generations | charts and lessons. */
  function evolution() {
    var root = document.getElementById('tab-evolution');
    if (root) attach(root, { key: 'evolution', defaults: ['minmax(330px, .8fr)', 'minmax(0, 1.5fr)'], min: [260, 360] });
  }
  /* MEMORY: bank tiles | documents. The tab builds itself on first use, so wait for it. */
  function memory() {
    var host = document.getElementById('tab-memory');
    if (!host) return;
    function tryAttach() {
      var root = host.querySelector('.mem-root');
      if (root) attach(root, { key: 'memory', defaults: ['clamp(250px, 21vw, 340px)', 'minmax(0, 1fr)'], min: [200, 420] });
      return !!root;
    }
    if (tryAttach()) return;
    var mo = new MutationObserver(function () { if (tryAttach()) mo.disconnect(); });
    mo.observe(host, { childList: true, subtree: true });
  }
  /* A horizontal divider between two stacked panels: drag to give one more height, double-click to reset. */
  function attachRow(top, bottom, key, minPx) {
    if (!top || !bottom || top.dataset.rowsplit) return;
    top.dataset.rowsplit = '1';
    minPx = minPx || 110;
    var h = document.createElement('div');
    h.className = 'rowsplit'; h.title = 'Drag to resize. Double-click to reset.';
    h.setAttribute('role', 'separator'); h.setAttribute('aria-orientation', 'horizontal');
    top.after(h);
    var size = saved[key] && saved[key][0] ? saved[key][0] : null, startY = 0, start = 0;
    function apply() {
      if (size == null) { top.style.flex = ''; bottom.style.flex = ''; return; }
      top.style.flex = '0 0 ' + size + 'px'; bottom.style.flex = '1 1 0';
    }
    h.addEventListener('pointerdown', function (e) {
      if (e.button) return;
      e.preventDefault();
      start = top.getBoundingClientRect().height / zf(); startY = e.clientY;
      try { h.setPointerCapture(e.pointerId); } catch (x) { /* synthetic event */ }
      h.classList.add('drag'); document.body.classList.add('rowresizing');
    });
    h.addEventListener('pointermove', function (e) {
      if (!h.classList.contains('drag')) return;
      var room = (top.getBoundingClientRect().height + bottom.getBoundingClientRect().height) / zf();
      size = Math.round(Math.max(minPx, Math.min(room - minPx, start + (e.clientY - startY) / zf())));
      apply();
    });
    function end(e) {
      if (!h.classList.contains('drag')) return;
      h.classList.remove('drag'); document.body.classList.remove('rowresizing');
      try { h.releasePointerCapture(e.pointerId); } catch (x) { /* already released */ }
      saved[key] = [size]; save(saved);
    }
    h.addEventListener('pointerup', end); h.addEventListener('pointercancel', end);
    h.addEventListener('dblclick', function () { size = null; delete saved[key]; save(saved); apply(); });
    apply();
  }
  function liveRows() {
    var side = document.querySelector('#tab-live .side');
    if (side) attachRow(side.querySelector('.membanks'), side.querySelector('.rules'), 'live.memory');
  }
  window.ColResize = { attach: attach, attachRow: attachRow, setScale: setScale, scale: zf, init: function () { scaleControl(); live(); liveRows(); evolution(); memory(); refitOnShow(); } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', window.ColResize.init);
  else window.ColResize.init();
})();
