/* Live memory browser: the four memory banks, straight from MongoDB, updating as they change.

   window.MemoryTab = { mount(rootEl), setActive(bool) }
     mount(rootEl)    render everything inside rootEl (once)
     setActive(true)  open the event stream (/api/memory/stream) and refresh
     setActive(false) close the stream
   Extras: MemoryTab.open(bank), MemoryTab.setRun(run_id | ""), MemoryTab.apiBase (default "").
   A click on a run id dispatches CustomEvent "mongoarc:open-run" on window, detail {run_id}.
   Query parameters read on mount: ?bank=notes  ?raw=1  ?q=text  ?run_id=...  */
(function () {
  "use strict";

  var ACCENT = { lessons: "#FFDC00", notes: "#1E93FF", rulebooks: "#4FCC30", generations: "#A356D6" };
  var ORDER = ["lessons", "notes", "rulebooks", "generations"];
  var HINT = { lessons: "$text", notes: "$text", rulebooks: "JSON regex", generations: "JSON regex" };
  var PAST = { insert: "inserted", update: "updated", replace: "replaced", "delete": "deleted", refresh: "changed" };
  var LIMIT = 100, MARK_MS = 9000, GONE_MS = 2000, FEED_MAX = 40;

  var S = {
    root: null, el: {}, active: false, banks: [], bank: "lessons", q: "", run: "", allVersions: false, rawAll: false,
    cards: new Map(), loaded: false, dirty: false, seq: 0, es: null, conn: "idle", polling: false, retry: null,
    last: null, lastAt: {}, feed: [], runs: {}, runsAt: 0, timers: {}, insertedAt: 0
  };
  var MemoryTab = { mount: mount, setActive: setActive, open: openBank, setRun: setRun, apiBase: "" };

  /* ---------- small helpers ---------- */
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 2) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function clock(iso) {
    var d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false });
  }
  function when(iso, pre) {
    if (!iso) return "";
    return '<time class="mem-ts" data-mem-ago="' + esc(iso) + '" data-mem-pre="' + esc(pre || "") + '" title="' + esc(iso) + '">' +
      esc((pre ? pre + " " : "") + ago(iso)) + "</time>";
  }
  function chip(text, cls) { return text == null || text === "" ? "" : '<span class="mem-chip ' + (cls || "") + '">' + esc(text) + "</span>"; }
  function kv(k, v) { return v == null ? "" : '<span class="mem-kv">' + esc(k) + " <b>" + esc(v) + "</b></span>"; }
  function run(id) { return id ? '<a class="mem-run" data-run="' + esc(id) + '" title="open this run">' + esc(id) + "</a>" : ""; }
  function stat(v, label) { return "<span><b>" + esc(v == null ? "0" : v) + "</b>" + esc(label) + "</span>"; }
  function sec(title, html, n) {
    return '<div class="mem-sec"><h4>' + esc(title) + (n == null ? "" : "<b>" + esc(n) + "</b>") + "</h4>" + html + "</div>";
  }
  function num(v, digits) { return typeof v === "number" ? v.toFixed(digits == null ? 2 : digits) : "n/a"; }
  function shortId(ev) {  // notes are keyed "<run_id>:<path>": in a narrow line the path says more than the run
    var id = String(ev.id), i = id.indexOf(":");
    if (i > 0 && ev.bank === "notes") return id.slice(i + 1) + "  " + id.slice(0, i);
    if (i > 0 && ev.bank === "rulebooks") return "v" + id.slice(i + 1) + "  " + id.slice(0, i);  // "<run_id>:<version>"
    return id;
  }
  function tail(s, n) { s = String(s); return s.length > n ? "\u2026" + s.slice(s.length - n + 1) : s; }
  function api(path) {
    return fetch((MemoryTab.apiBase || "") + path, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error(r.status === 503 ? "database unavailable" : "HTTP " + r.status);
      return r.json();
    });
  }
  function hl(json) {  // JSON syntax colours, built from the escaped text
    var re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g, out = "", last = 0, m;
    while ((m = re.exec(json))) {
      out += esc(json.slice(last, m.index));
      if (m[1]) out += '<span class="' + (m[2] ? "mem-jk" : "mem-js") + '">' + esc(m[1]) + "</span>" + (m[2] || "");
      else out += '<span class="mem-jn">' + esc(m[0]) + "</span>";
      last = re.lastIndex;
    }
    return out + esc(json.slice(last));
  }

  /* ---------- per-bank readable views: {head, body} ---------- */
  var VIEW = {
    lessons: function (d) {
      var s = d.stats || {}, ev = d.evidence || [];
      return {
        head: '<span class="mem-id">' + esc(d._id) + "</span>" + chip(d.kind) + chip(d.status, "mem-st-" + d.status) +
          kv("created in gen", d.created_gen) + (d.tags || []).map(function (t) { return '<span class="mem-tag">#' + esc(t) + "</span>"; }).join("") +
          when(d.created_at),
        body: '<p class="mem-big">' + esc(d.text) + "</p>" +
          (d.retired_reason ? '<p class="mem-why"><b>RETIRED</b>' + esc(d.retired_reason) + "</p>" : "") +
          '<div class="mem-stats">' + stat(s.injected, "injected") + stat(s.wins, "wins") +
          stat(s.injected ? num(s.efficiency_sum / s.injected) : "n/a", "mean efficiency") + stat(s.recalled, "recalled") +
          stat((s.runs || []).length, "runs") + "</div>" +
          sec("EVIDENCE  game / level / rule", ev.length ? ev.map(function (e) {
            return '<div class="mem-evd"><span class="mem-eg">' + esc(e.game_id) + '</span><i>/</i><span class="mem-el">level ' + esc(e.level) +
              '</span><i>/</i><span class="mem-er">' + esc(e.rule) + "</span>" + run(e.run_id) + "</div>";
          }).join("") : '<div class="mem-none">none recorded</div>')
      };
    },
    notes: function (d) {
      var refs = d.refs || [];
      return {
        head: '<span class="mem-id">' + esc(d.path || d._id) + "</span>" + chip(d.game_id, "mem-game") +
          (d.version == null ? "" : '<span class="mem-ver">v' + esc(d.version) + "</span>") + run(d.run_id) +
          (d.generation == null ? "" : kv("gen", d.generation)) + when(d.updated_at, "updated"),
        body: '<pre class="mem-prose">' + esc(d.text) + "</pre>" +
          sec("REFS  scripts this note points at", refs.length ? '<div class="mem-chips">' + refs.map(function (r) {
            return '<span class="mem-ref">' + esc(r) + "</span>";
          }).join("") + "</div>" : '<div class="mem-none">none</div>')
      };
    },
    rulebooks: function (d) {
      var r = S.runs[d.run_id] || {}, added = d.added || {};
      function text(it) { return typeof it === "string" ? it : (it && it.rule) || ""; }
      function isNew(kind, it) {
        return d.version > 1 && (added[kind] || []).some(function (a) { return text(a) === text(it); });
      }
      function col(kind, cls, title) {
        var items = d[kind] || [];
        return '<div class="mem-rcol ' + cls + '"><h4>' + title + "<b>" + items.length + "</b></h4>" + (items.length ? items.map(function (it) {
          var plain = typeof it === "string";
          return '<div class="mem-rule' + (isNew(kind, it) ? " mem-rnew" : "") + '">' +
            (plain || it.level == null ? "" : '<span class="mem-lv">L' + esc(it.level) + "</span>") +
            '<span class="mem-rt"><span>' + esc(text(it)) + "</span>" + (!plain && it.note ? "<small>" + esc(it.note) + "</small>" : "") + "</span></div>";
        }).join("") : '<div class="mem-none">none</div>') + "</div>";
      }
      return {
        head: '<span class="mem-id">' + esc(r.game_id || "rule book") + "</span>" + '<span class="mem-ver">v' + esc(d.version) + "</span>" +
          run(d.run_id) + kv("level", d.level) + kv("after action", d.action_n) + when(d.at),
        body: '<div class="mem-rb">' + col("verified", "mem-v", "VERIFIED") + col("refuted", "mem-r", "REFUTED") +
          col("open_questions", "mem-o", "OPEN QUESTIONS") + "</div>"
      };
    },
    generations: function (d) {
      var diff = d.diff || {}, fit = d.fitness || {}, dist = d.distiller || {}, knobs = d.knobs || {}, rows = [];
      function ids(list, cls, sign) {
        return (list || []).map(function (x) { return '<span class="' + cls + '">' + sign + esc(x) + "</span>"; }).join("");
      }
      function row(k, html) { if (html) rows.push('<div><span class="mem-dk">' + k + '</span><span class="mem-dv">' + html + "</span></div>"); }
      row("ADDED", ids(diff.added_lessons, "mem-add", "+"));
      row("RETIRED", ids(diff.retired_lessons, "mem-rem", "-"));
      row("REVISED", ids(diff.revised_lessons, "mem-rev", "~"));
      row("KNOBS", Object.keys(diff.knob_changes || {}).map(function (k) {
        var c = diff.knob_changes[k] || [];
        return '<span class="mem-knob">' + esc(k) + " " + esc(c[0]) + " \u2192 " + esc(c[1]) + "</span>";
      }).join(""));
      row("TOOLS", ids(diff.added_tools, "mem-add", "+"));
      var lessons = d.lessons || [], runs = fit.runs || [];
      return {
        head: '<span class="mem-id">GEN ' + esc(d._id) + "</span>" + chip(d.status, "mem-st-" + d.status) +
          kv("parent", d.parent == null ? "none" : "gen " + d.parent) +
          (d.created_from_run ? '<span class="mem-kv">built from</span>' + run(d.created_from_run) : "") + when(d.created_at),
        body: '<div class="mem-gen"><div>' +
          sec("DIFF  against the parent", rows.length ? '<div class="mem-diff">' + rows.join("") + "</div>" : '<div class="mem-none">no change: the starting point</div>') +
          sec("RATIONALE", '<p class="mem-ratio">' + esc(d.rationale || "") + "</p>") + "</div><div>" +
          sec("LESSONS CARRIED", lessons.length ? '<div class="mem-chips">' + lessons.map(function (l) {
            return '<span class="mem-lid">' + esc(l) + "</span>";
          }).join("") + "</div>" : '<div class="mem-none">none</div>', lessons.length) +
          sec("FITNESS", '<div class="mem-stats">' + stat(num(fit.mean_efficiency), "mean efficiency") +
            stat(num(fit.mean_completion), "mean completion") + stat(runs.length, "runs") + "</div>" +
            (runs.length ? '<div class="mem-runs">' + runs.map(run).join("") + "</div>" : "")) +
          sec("KNOBS  /  DISTILLER", '<div class="mem-stats">' + Object.keys(knobs).map(function (k) { return stat(knobs[k], k); }).join("") +
            "</div>" + '<div class="mem-kv">' + esc(dist.kind || "") + " " + esc(dist.model || "") +
            (dist.cost_usd == null ? "" : "  $" + esc(dist.cost_usd)) + ((d.tools || []).length ? "  tools: " + esc(d.tools.map(function (t) { return t.name; }).join(", ")) : "") + "</div>") +
          "</div></div>"
      };
    }
  };

  /* ---------- cards ---------- */
  function rawHtml(doc) {
    return '<div class="mem-rawwrap"><div class="mem-rawcap"><span>db.' + esc(S.bank) + ".findOne({ _id: " + esc(JSON.stringify(doc._id)) +
      ' })</span><em>as stored in MongoDB</em></div><pre class="mem-raw">' + hl(JSON.stringify(doc, null, 2)) + "</pre></div>";
  }
  function paint(c) {
    var v = VIEW[S.bank](c.doc);
    c.el.classList.toggle("mem-rawopen", c.raw);
    c.el.innerHTML = '<div class="mem-chead">' + v.head + '<span class="mem-grow"></span>' +
      (c.mark ? '<span class="mem-mark mem-mark-' + c.mark.kind + '">' + esc(c.mark.text) + "</span>" : "") +
      '<button type="button" class="mem-btn mem-rawbtn' + (c.raw ? " mem-on" : "") + '">RAW DOCUMENT</button></div>' +
      '<div class="mem-cbody"><div class="mem-view">' + v.body + "</div>" + (c.raw ? rawHtml(c.doc) : "") + "</div>";
  }
  function makeCard(doc) {
    var el = document.createElement("article");
    var c = { id: String(doc._id), doc: doc, el: el, raw: S.rawAll, mark: null, timer: null, gone: false };
    el.className = "mem-card";
    el.dataset.id = c.id;
    S.cards.set(c.id, c);
    paint(c);
    return c;
  }
  function mark(c, kind, text) {
    clearTimeout(c.timer);
    c.mark = { kind: kind, text: text };
    paint(c);
    c.el.classList.remove("mem-new", "mem-upd", "mem-del");
    void c.el.offsetWidth;  // restart the animation when the same card changes twice
    c.el.classList.add("mem-" + kind);
    if (kind !== "del") c.timer = setTimeout(function () {
      c.mark = null;
      c.el.classList.remove("mem-new", "mem-upd");
      var m = c.el.querySelector(".mem-mark");
      if (m) m.remove();
    }, MARK_MS);
  }
  function reveal(c) {
    if (S.el.list.matches(":hover")) return;  // someone is reading: do not move the list under them
    if (Date.now() - S.insertedAt < 2500) return;  // a new card just slid in at the top: keep it in view
    var a = c.el.getBoundingClientRect(), b = S.el.list.getBoundingClientRect();
    if (a.bottom < b.top + 48 || a.top > b.bottom - 48) c.el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  function forget(c) {
    clearTimeout(c.timer);
    c.el.remove();
    if (S.cards.get(c.id) === c) S.cards.delete(c.id);
  }
  function insertCard(doc, label) {
    var old = S.cards.get(String(doc._id));
    if (old) forget(old);
    var none = S.el.list.querySelector(".mem-empty");
    if (none) none.remove();
    var c = makeCard(doc);
    S.el.list.insertBefore(c.el, S.el.list.firstChild);
    mark(c, "new", label || "NEW");
    S.insertedAt = Date.now();
    if (!S.el.list.matches(":hover")) S.el.list.scrollTop = 0;
  }
  function updateCard(c, doc, label) {
    c.doc = doc;
    mark(c, "upd", label || "UPDATED");
    reveal(c);
  }
  function deleteCard(c) {
    if (c.gone) return;
    c.gone = true;
    mark(c, "del", "DELETED");
    reveal(c);
    var list = S.el.list, bank = S.bank;
    setTimeout(function () {
      c.el.classList.add("mem-out");
      setTimeout(function () {
        var here = c.el.isConnected;
        forget(c);
        if (here && bank === S.bank && !S.cards.size) showEmpty();
        paintHead();
      }, 340);
    }, GONE_MS);
  }
  function updLabel(old, doc, fields) {
    if (old && old.version != null && doc.version != null && old.version !== doc.version) return "UPDATED  v" + old.version + " \u2192 v" + doc.version;
    return "UPDATED" + (fields && fields.length ? "  " + fields.slice(0, 3).join(", ") : "");
  }
  // rulebooks, latest-per-run view: a newer snapshot of a run replaces that run's card in place.
  function supersede(doc) {
    if (S.bank !== "rulebooks" || S.allVersions) return false;
    var prev = null;
    S.cards.forEach(function (o) { if (!o.gone && o.doc.run_id === doc.run_id && o.id !== String(doc._id)) prev = o; });
    if (!prev) return false;
    if ((prev.doc.version || 0) > (doc.version || 0)) return true;  // an older snapshot: not shown in this view
    var label = "NEW VERSION  v" + prev.doc.version + " \u2192 v" + doc.version;
    S.cards.delete(prev.id);
    prev.id = String(doc._id);
    prev.el.dataset.id = prev.id;
    S.cards.set(prev.id, prev);
    updateCard(prev, doc, label);
    return true;
  }
  function showEmpty(err) {
    var b = S.bank, filtered = S.q || S.run;
    S.el.list.innerHTML = err
      ? '<div class="mem-empty mem-err"><b>' + esc(err) + "</b>Retrying every two seconds.</div>"
      : '<div class="mem-empty"><b>' + (filtered ? "No document in db." + esc(b) + " matches." : "db." + esc(b) + " is empty.") + "</b>" +
        (filtered ? "Clear the search to see the whole bank." : "Documents appear here the moment the agent or the harness writes one.") + "</div>";
  }
  function renderAll(docs) {
    S.cards.forEach(function (c) { clearTimeout(c.timer); });
    S.cards = new Map();
    S.el.list.innerHTML = "";
    S.el.list.scrollTop = 0;
    if (!docs.length) return showEmpty();
    var frag = document.createDocumentFragment();
    docs.forEach(function (d) { frag.appendChild(makeCard(d).el); });
    S.el.list.appendChild(frag);
  }
  function reconcile(docs) {  // diff a fresh listing against the cards on screen, with the same animations
    var seen = {};
    docs.forEach(function (d) { seen[String(d._id)] = 1; });
    docs.forEach(function (d) { if (!S.cards.has(String(d._id))) supersede(d); });
    Array.from(S.cards.values()).forEach(function (c) { if (!seen[c.id]) deleteCard(c); });
    for (var i = docs.length - 1; i >= 0; i--) {  // oldest first, so the newest insert ends on top
      var d = docs[i], c = S.cards.get(String(d._id));
      if (!c || c.gone) insertCard(d, "NEW");
      else if (JSON.stringify(c.doc) !== JSON.stringify(d)) updateCard(c, d, updLabel(c.doc, d));
    }
  }

  /* ---------- loading ---------- */
  function loadBanks() {
    return api("/api/memory/banks").then(function (banks) {
      S.banks = banks;
      paintTiles();
      paintHead();
    }).catch(function () { /* the list shows the error */ });
  }
  function loadRuns() {
    S.runsAt = Date.now();
    return api("/api/memory/runs").then(function (runs) {
      S.runs = runs || {};
      if (S.bank === "rulebooks") S.cards.forEach(function (c) { if (!c.mark) paint(c); });
    }).catch(function () {});
  }
  function loadList(fresh) {
    var bank = S.bank, token = ++S.seq;
    S.dirty = false;
    var url = "/api/memory/" + bank + "?limit=" + LIMIT + (S.q ? "&q=" + encodeURIComponent(S.q) : "") +
      (S.run ? "&run_id=" + encodeURIComponent(S.run) : "") + (bank === "rulebooks" && S.allVersions ? "&all_versions=1" : "");
    return api(url).then(function (docs) {
      if (token !== S.seq) return;
      if (fresh || !S.loaded) renderAll(docs); else reconcile(docs);
      S.loaded = true;
      paintHead();
      if (S.dirty) loadList(false);  // a change arrived while this listing was in flight
    }).catch(function (e) {
      if (token !== S.seq) return;
      S.loaded = false;
      S.cards = new Map();
      showEmpty(String(e.message || e));
      clearTimeout(S.timers.err);
      S.timers.err = setTimeout(function () { if (S.active && !S.loaded) refresh(); }, 2000);
    });
  }
  function refresh() { loadBanks(); loadRuns(); loadList(false); }
  function later(name, fn, ms) { clearTimeout(S.timers[name]); S.timers[name] = setTimeout(fn, ms); }

  /* ---------- the event stream ---------- */
  function setConn(c) { S.conn = c; paintStatus(); }
  function disconnect() {
    clearTimeout(S.retry);
    if (S.es) { S.es.onopen = S.es.onmessage = S.es.onerror = null; S.es.close(); S.es = null; }
  }
  function connect() {
    disconnect();
    setConn("connecting");
    var es;
    try { es = S.es = new EventSource((MemoryTab.apiBase || "") + "/api/memory/stream"); }
    catch (e) { S.retry = setTimeout(connect, 2000); return setConn("reconnecting"); }
    es.onopen = function () { S.polling = false; setConn("live"); refresh(); };  // catch up on anything written while away
    es.addEventListener("hello", function (m) {
      try { S.polling = JSON.parse(m.data).mode === "poll"; } catch (e) { return; }
      paintStatus();
    });
    es.onmessage = function (m) {
      var ev;
      try { ev = JSON.parse(m.data); } catch (e) { return; }
      onChange(ev);
    };
    es.onerror = function () {
      if (S.es !== es) return;
      setConn("reconnecting");
      if (es.readyState === 2) {  // closed for good (e.g. the server answered with an error): retry ourselves
        disconnect();
        S.retry = setTimeout(function () { if (S.active) connect(); }, 2000);
      }
    };
  }
  function onChange(ev) {
    if (!ev || ORDER.indexOf(ev.bank) < 0) return;
    ev.at = ev.at || new Date().toISOString();
    S.lastAt[ev.bank] = ev.at;
    S.last = ev;
    if (ev.op === "refresh") S.polling = true;
    S.feed.unshift(ev);
    S.feed.length = Math.min(S.feed.length, FEED_MAX);
    pulse(ev.bank);
    paintFeed();
    paintStatus();
    later("banks", loadBanks, 120);
    if (ev.bank !== S.bank) return;
    if (!S.loaded) { S.dirty = true; return; }
    if (ev.op === "refresh" || S.q || S.run) return later("list", function () { loadList(false); }, 150);
    var id = String(ev.id), c = S.cards.get(id), doc = ev.doc;
    if (ev.op === "delete") { if (c) deleteCard(c); return; }
    if (!doc) return;
    if (doc.run_id && !S.runs[doc.run_id] && Date.now() - S.runsAt > 3000) loadRuns();
    if (c && !c.gone) updateCard(c, doc, updLabel(c.doc, doc, ev.fields));
    else if (!supersede(doc)) insertCard(doc, ev.op === "insert" ? "NEW" : "UPDATED");
    paintHead();
  }

  /* ---------- chrome: tiles, feed, status, head ---------- */
  function pulse(bank) {
    var t = S.el.tiles.querySelector('[data-bank="' + bank + '"]');
    if (!t) return;
    t.classList.remove("mem-ping");
    void t.offsetWidth;
    t.classList.add("mem-ping");
    clearTimeout(S.timers["ping-" + bank]);
    S.timers["ping-" + bank] = setTimeout(function () { t.classList.remove("mem-ping"); }, 1400);
  }
  function paintTiles() {
    var box = S.el.tiles;
    S.banks.forEach(function (b) {
      var t = box.querySelector('[data-bank="' + b.name + '"]');
      if (!t) {
        t = document.createElement("button");
        t.type = "button";
        t.className = "mem-tile";
        t.dataset.bank = b.name;
        t.style.setProperty("--mem-acc", ACCENT[b.name] || "#fff");
        t.innerHTML = '<span class="mem-tname"><i class="mem-dot"></i>' + esc(b.label || b.name) + '</span><span class="mem-count"></span>' +
          '<span class="mem-tdesc">' + esc(b.description) + '</span><span class="mem-tlast"></span>';
        box.appendChild(t);
      }
      t.classList.toggle("mem-on", b.name === S.bank);
      var n = t.querySelector(".mem-count");
      if (n.textContent !== String(b.count)) n.textContent = b.count;
      var at = S.lastAt[b.name] || b.updated_at, last = t.querySelector(".mem-tlast");
      if (last.dataset.at !== String(at)) {
        last.dataset.at = String(at);
        last.innerHTML = at ? when(at, "last change") + " \u00b7 " + esc(clock(at)) : "no documents yet";
      }
    });
  }
  function paintFeed() {
    S.el.feed.innerHTML = S.feed.length ? S.feed.map(function (ev) {
      return '<div class="mem-fl" data-bank="' + esc(ev.bank) + '" style="--mem-acc:' + ACCENT[ev.bank] + '"><time>' + esc(clock(ev.at)) +
        '</time><b class="mem-op-' + esc(ev.op) + '">' + esc(String(ev.op).toUpperCase()) + "</b><span><em>" + esc(ev.bank) + "</em> " +
        esc(ev.id == null ? "" : shortId(ev)) + "</span></div>";
    }).join("") : '<div class="mem-feednone">Every insert, update and delete in the four banks is listed here as MongoDB reports it.</div>';
  }
  function paintStatus() {
    var label = { idle: "PAUSED", connecting: "CONNECTING", live: "LIVE", reconnecting: "RECONNECTING" }[S.conn];
    var src = S.conn === "reconnecting" ? "stream dropped, retrying" : S.polling ? "polling MongoDB every second" : "MongoDB change stream";
    var ev = S.last, last;
    if (ev) {
      last = '<span class="mem-last" style="--mem-acc:' + ACCENT[ev.bank] + '"><em>' + esc(ev.bank) + "</em>" +
        (ev.id == null ? "" : " / " + esc(tail(ev.id, 64))) + ' <strong class="mem-op-' + esc(ev.op) + '">' + esc(PAST[ev.op] || ev.op) +
        "</strong>, " + when(ev.at).replace('class="mem-ts"', 'class="mem-ts mem-tsl"') + "</span>";
    } else {
      last = '<span class="mem-last mem-dim">no change since this page opened</span>';
    }
    S.el.status.className = "mem-status mem-conn-" + S.conn;
    S.el.status.innerHTML = "<i></i><b>" + label + '</b><span class="mem-src">' + src + "</span>" + last;
  }
  function paintHead() {
    var b = S.banks.filter(function (x) { return x.name === S.bank; })[0] || { name: S.bank, description: "", count: null };
    var shown = 0;
    S.cards.forEach(function (c) { if (!c.gone) shown++; });
    var total = b.count == null ? "" : b.count;
    var what = S.bank === "rulebooks" && !S.allVersions ? "latest of " + shown + " runs \u00b7 " + total + " docs"
      : shown + " of " + total + " docs" + (S.q ? " match" : "");
    S.root.firstElementChild.style.setProperty("--mem-acc", ACCENT[S.bank] || "#fff");
    S.el.title.innerHTML = "<small>db.</small>" + esc(S.bank);
    S.el.sub.innerHTML = "<b>" + esc(S.loaded ? what : "loading") + "</b><span>" + esc(b.description || "") + "</span>";
    S.el.search.placeholder = "search " + S.bank + " (" + HINT[S.bank] + ")";
    S.el.seg.hidden = S.bank !== "rulebooks";
    S.el.seg.children[0].classList.toggle("mem-on", !S.allVersions);
    S.el.seg.children[1].classList.toggle("mem-on", S.allVersions);
    S.el.rawAll.classList.toggle("mem-on", S.rawAll);
    S.el.runf.hidden = !S.run;
    S.el.runf.firstElementChild.textContent = "run " + S.run;
    S.el.tiles.querySelectorAll(".mem-tile").forEach(function (t) { t.classList.toggle("mem-on", t.dataset.bank === S.bank); });
  }
  function tick() {
    if (!S.root || !S.active) return;
    S.root.querySelectorAll("[data-mem-ago]").forEach(function (t) {
      var text = (t.dataset.memPre ? t.dataset.memPre + " " : "") + ago(t.dataset.memAgo);
      if (t.textContent !== text) t.textContent = text;
    });
  }

  /* ---------- public ---------- */
  function openBank(name) {
    if (ORDER.indexOf(name) < 0) return;
    S.bank = name;
    S.q = "";
    S.loaded = false;
    if (!S.root) return;
    S.el.search.value = "";
    S.cards.forEach(function (c) { clearTimeout(c.timer); });
    S.cards = new Map();
    S.el.list.innerHTML = "";
    paintHead();
    if (S.active) loadList(true);
  }
  function setRun(runId) {
    S.run = runId || "";
    S.loaded = false;
    if (!S.root) return;
    paintHead();
    if (S.active) loadList(true);
  }
  function setActive(on) {
    on = !!on;
    if (on === S.active || !S.root) { S.active = on; return; }
    S.active = on;
    if (on) { connect(); refresh(); } else { disconnect(); setConn("idle"); }
  }
  function mount(rootEl) {
    if (S.root === rootEl) return;
    S.root = rootEl;
    rootEl.innerHTML =
      '<div class="mem-root"><aside class="mem-rail"><div class="mem-tiles" style="display:contents"></div>' +
      '<div class="mem-feed"><h3>CHANGE STREAM</h3><div class="mem-feedlist"></div></div></aside>' +
      '<section class="mem-main"><div class="mem-status"></div><div class="mem-head"><span class="mem-title"></span>' +
      '<span class="mem-sub"></span><span class="mem-grow"></span><span class="mem-runf" hidden><span></span>' +
      '<button type="button" class="mem-runx" title="show every run">\u00d7</button></span>' +
      '<input class="mem-search" type="search" spellcheck="false" autocomplete="off" aria-label="Search this memory bank">' +
      '<span class="mem-seg" hidden><button type="button" class="mem-btn" data-all="0">LATEST PER RUN</button>' +
      '<button type="button" class="mem-btn" data-all="1">ALL VERSIONS</button></span>' +
      '<button type="button" class="mem-btn mem-rawall" title="show the stored JSON of every document">RAW JSON</button></div>' +
      '<div class="mem-list"></div></section></div>';
    var q = function (sel) { return rootEl.querySelector(sel); };
    S.el = { tiles: q(".mem-tiles"), feed: q(".mem-feedlist"), status: q(".mem-status"), title: q(".mem-title"), sub: q(".mem-sub"),
      search: q(".mem-search"), seg: q(".mem-seg"), rawAll: q(".mem-rawall"), runf: q(".mem-runf"), list: q(".mem-list") };

    var p = new URLSearchParams(location.search);
    if (ORDER.indexOf(p.get("bank")) >= 0) S.bank = p.get("bank");
    S.rawAll = /^(1|true|yes)$/.test(p.get("raw") || "");
    S.allVersions = /^(1|true|yes)$/.test(p.get("all_versions") || "");
    S.run = p.get("run_id") || "";
    S.q = p.get("q") || "";
    S.el.search.value = S.q;

    rootEl.addEventListener("click", function (e) {
      var t = e.target.closest(".mem-run, .mem-rawbtn, .mem-tile, .mem-fl, .mem-rawall, .mem-seg .mem-btn, .mem-runx");
      if (!t || !rootEl.contains(t)) return;
      if (t.classList.contains("mem-run")) {
        e.preventDefault();
        window.dispatchEvent(new CustomEvent("mongoarc:open-run", { detail: { run_id: t.dataset.run } }));
      } else if (t.classList.contains("mem-rawbtn")) {
        var c = S.cards.get(t.closest(".mem-card").dataset.id);
        if (c) { c.raw = !c.raw; paint(c); }
      } else if (t.classList.contains("mem-tile") || t.classList.contains("mem-fl")) {
        if (t.dataset.bank !== S.bank || !t.classList.contains("mem-fl")) openBank(t.dataset.bank);
      } else if (t.classList.contains("mem-rawall")) {
        S.rawAll = !S.rawAll;
        S.cards.forEach(function (c) { c.raw = S.rawAll; paint(c); });
        paintHead();
      } else if (t.classList.contains("mem-runx")) {
        setRun("");
      } else {
        S.allVersions = t.dataset.all === "1";
        S.loaded = false;
        paintHead();
        loadList(true);
      }
    });
    S.el.search.addEventListener("input", function () {
      later("search", function () {
        var v = S.el.search.value.trim();
        if (v === S.q) return;
        S.q = v;
        S.loaded = false;
        loadList(true);
      }, 220);
    });
    paintStatus();
    paintFeed();
    paintHead();
    setInterval(tick, 1000);
    if (S.active) { connect(); refresh(); }
  }

  window.MemoryTab = MemoryTab;
})();
