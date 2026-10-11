"""Tail CCARC run directories into MongoDB (schema: docs/SCHEMA.md).

usage: python -m mongoarc.collector --watch <root> [--interval 0.2] [--once] [-q]

<root> is one attempt directory or a directory of them. Each pass reads what is new in a
run's files, reserves one block of sequence numbers and writes it in a few round trips.
File cursors are saved in runs.cursors, so a restarted collector resumes where it stopped.

Several collectors may watch the same run (the evolve loop starts one in process, and this
CLI may be running next to it): a lease in runs.cursors lets exactly one of them write a
run, and the others stand by and take over when it stops.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from pymongo import ReplaceOne, ReturnDocument

from . import db as D

GUARD = 256            # bytes before a cursor that are re-checked to detect a replaced file
STALE_AFTER = 1200.0   # seconds without a file change before a running run is marked stopped
LEASE = 10.0           # seconds a run stays reserved for the collector that last wrote to it
CLAIM_EVERY = 2.0      # seconds between a standing-by collector's attempts to take a run over
STATE_KEYS = ("state", "level", "win_levels", "actions_used", "level_costs", "gate_pending",
              "full_resets", "wasted_actions")
RULE_KEYS = ("verified", "refuted", "open_questions")
# Event kinds this module writes; recovery never deletes any other kind (the launcher's).
KINDS = ["thinking", "text", "tool_use", "tool_result", "user_msg", "system", "result", "outbox", "rulebook"]
# Stream `system` subtypes worth a place on the timeline. The real CLI also emits telemetry
# (hundreds of `thinking_tokens` lines a minute) that would bury everything else.
SYSTEM_KEEP = frozenset({"init", "api_retry", "compact_boundary"})
ACTION_TOOLS = frozenset({"Bash"})     # the tools whose calls can append ledger rows
_ROTATED = re.compile(r"^stream\.(\d+)\.jsonl$")
_URI = re.compile(r"mongodb(\+srv)?://\S+")
_HOST = socket.gethostname()


def _clean(v: Any) -> Any:
    """A parsed-JSON value BSON accepts: no lone surrogates, no ints over 64 bits, no NUL in keys."""
    if isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError:      # e.g. an emoji cut in half by a truncated tool output
            v = v.encode("utf-8", "replace").decode("utf-8")
        return v
    if isinstance(v, dict):
        return {_clean(str(k)).replace("\x00", ""): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, int) and not isinstance(v, bool) and not -(1 << 63) <= v < (1 << 63):
        return str(v)
    return v


def _json(line: bytes | str) -> Any:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _load(path: Path) -> Any:
    """A JSON file, or None while it is missing or half-written."""
    try:
        return _clean(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _sig(path: Path) -> list[int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_ino, st.st_mtime_ns, st.st_size]


def _extent(path: Path) -> tuple[int, int] | None:
    """(inode, size) of a file right now: how far this pass may read it."""
    s = _sig(path)
    return (s[0], s[2]) if s else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def _tail(path: Path, pick: Callable[[int], dict | None], limit: tuple[int, int] | None = None):
    """New complete lines of an append-only file.

    `pick(inode)` returns the saved cursor for that file. Returns (lines, cursor, restarted),
    or None when the file is missing. Reading restarts at 0 when the inode changed, the file
    shrank, or the bytes just before the offset are not the ones consumed last time. An
    unterminated last line is a write in progress and is left for the next pass.
    """
    try:
        f = open(path, "rb")
    except OSError:
        return None
    with f:
        st = os.fstat(f.fileno())
        cur = pick(st.st_ino) or {}
        off = int(cur.get("offset") or 0)
        end = min(st.st_size, limit[1]) if limit and limit[0] == st.st_ino else st.st_size
        same = cur.get("ino") == st.st_ino and st.st_size >= off
        if same and end <= off:
            return [], cur, False
        head = b""
        if same and off:
            f.seek(max(0, off - GUARD))
            head = f.read(min(off, GUARD))
            same = hashlib.sha1(head).hexdigest() == cur.get("tail")
        if not same:
            off, head = 0, b""
        f.seek(off)
        data = f.read(max(0, end - off))
    cut = data.rfind(b"\n") + 1
    pos = {"ino": st.st_ino, "offset": off + cut,
           "tail": hashlib.sha1((head + data[:cut])[-GUARD:]).hexdigest()}
    return data[:cut].split(b"\n")[:-1], pos, (not same and bool(cur.get("offset")))


def _system_text(e: dict) -> str:
    if e.get("subtype") == "init":
        return f"model={e.get('model')} session={e.get('session_id')}"
    if e.get("subtype") == "api_retry":
        return f"attempt {e.get('attempt')}/{e.get('max_retries')}: {e.get('error')}"
    rest = {k: v for k, v in e.items() if k not in ("type", "subtype", "session_id", "uuid")}
    return json.dumps(rest, ensure_ascii=False, default=str)[:500]


def stream_events(e: dict) -> list[dict]:
    """The timeline events (kind + payload) carried by one stream-json line."""
    out: list[dict] = []
    t = e.get("type")
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, str) and content and t in ("user", "assistant"):
        content = [{"type": "text", "text": content}]
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "thinking":
            if b.get("thinking"):
                out.append({"kind": "thinking", "text": str(b["thinking"])})
        elif bt == "text":
            out.append({"kind": "user_msg" if t == "user" else "text", "text": str(b.get("text") or "")})
        elif bt == "tool_use":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            cmd = inp.get("command")
            out.append({"kind": "tool_use", "tool": b.get("name"), "tool_use_id": b.get("id"),
                        "command": cmd if isinstance(cmd, str) else json.dumps(inp, default=str)[:2000],
                        "description": inp.get("description")})
        elif bt == "tool_result":
            c = b.get("content")
            text = c if isinstance(c, str) else "\n".join(
                str(x.get("text") or "") for x in (c if isinstance(c, list) else []) if isinstance(x, dict))
            out.append({"kind": "tool_result", "tool_use_id": b.get("tool_use_id"),
                        "text": text[:4000], "is_error": bool(b.get("is_error"))})
    if t == "system":
        sub = e.get("subtype")
        if sub in SYSTEM_KEEP or "error" in str(sub):
            out.append({"kind": "system", "subtype": sub, "text": _system_text(e)})
    elif t == "result":
        out.append({"kind": "result", "cost_usd": e.get("total_cost_usd"), "turns": e.get("num_turns"),
                    "duration_ms": e.get("duration_ms"), "is_error": bool(e.get("is_error")),
                    "text": str(e.get("result") or "")[:2000]})
    return out


def _subdirs(d: Path) -> list[Path]:
    """Child directories, never the hidden or private ones (.cli-config, _pkg)."""
    try:
        return sorted(p for p in d.iterdir() if p.name[:1] not in "._" and p.is_dir())
    except OSError:
        return []


def _find_workspace(out: Path) -> Path | None:
    return next((p for p in _subdirs(out) if (p / "session.py").is_file() and (p / "meta.json").is_file()), None)


class _Gone(Exception):
    """The run document is no longer in MongoDB (the database was reset under us)."""


class _Run:
    """One attempt directory: its files, its cursors and its run document."""

    def __init__(self, db, out: Path, ws: Path, run_id: str, game: str, me: str, lease: float,
                 stale_after: float, say: Callable[[str], None]):
        self.db, self.out, self.ws, self.id, self.game = db, out, ws, run_id, game
        self.me, self.lease, self.stale_after, self.say = me, lease, stale_after, say
        self.cur: dict = {}
        self.status = self.state = self.generation = None
        self.owned = False                 # whether this collector holds the run's write lease
        self.standby_said = False
        self.book: dict | None = None      # last rule book content, for the "added" diff
        self.last = ""                     # one-line summary of the last pass that wrote
        self._claim_at = 0.0
        self._stale_check = 0.0
        self.dirty = False                 # sequence numbers issued past the saved cursor

    @classmethod
    def open(cls, db, out: Path, me: str, lease: float, stale_after: float, say) -> "_Run | None":
        """Register the run for an attempt directory; None while it is not ready."""
        marker: Any = {}
        if (out / D.RUN_MARKER).exists():
            marker = _load(out / D.RUN_MARKER)
            if not isinstance(marker, dict):
                return None
        ws = out / str(marker["game_id"]) if marker.get("game_id") else _find_workspace(out)
        if ws is None:
            return None
        meta = _load(ws / "meta.json")
        meta = meta if isinstance(meta, dict) else {}
        game = str(marker.get("game_id") or meta.get("game_id") or ws.name)
        run_id = str(marker.get("run_id") or f"{out.name}-{game}")
        t = D.now()
        # A directory found after the fact started when its launch files were written, not now.
        born = [s[1] / 1e9 for s in map(_sig, (out / D.RUN_MARKER, ws / "meta.json")) if s]
        started = min(t, dt.datetime.fromtimestamp(max(born), dt.timezone.utc)) if born else t
        # Fill only what is missing, so a document the launcher created first keeps its fields.
        defaults = {
            "generation": marker.get("generation"), "lessons_used": marker.get("lessons_used") or [],
            "label": marker.get("label"), "agent": marker.get("agent"),
            "model": marker.get("model", meta.get("model")), "effort": marker.get("effort", meta.get("effort")),
            "baseline_actions": marker.get("baseline_actions"), "started_at": started, "updated_at": t,
            "game_id": game, "out_dir": str(out), "status": "running", "state": "NOT_PLAYED", "level": 0,
            "actions_used": 0, "level_costs": [], "deaths": 0, "full_resets": 0, "wasted_actions": 0,
            "n_actions": 0, "last_seq": 0, "cursors": {}}
        fill = {k: {"$ifNull": ["$" + k, {"$literal": v}]} for k, v in defaults.items()}
        for k in ("ended_at", "win_levels", "gate_pending", "cost_usd", "turns", "result"):
            fill[k] = {"$ifNull": ["$" + k, None]}
        db[D.RUNS].update_one({"_id": run_id}, [{"$set": fill}], upsert=True)
        run = cls(db, out, ws, run_id, game, me, lease, stale_after, say)
        run.claim(t)
        return run

    # -- the write lease: one collector per run ---------------------------------------------

    def claim(self, at) -> bool:
        """Take the run if it is free, ours, expired, or held by a dead process on this host.

        On success the saved cursors are loaded, so a take-over continues where the last
        writer stopped.
        """
        runs = self.db[D.RUNS]
        self._claim_at = time.monotonic() + CLAIM_EVERY
        take = {"$set": {"cursors.owner": self.me, "cursors.owner_at": at}}
        want = {"cursors": 1, "status": 1, "state": 1, "last_seq": 1, "generation": 1}
        free = {"$or": [{"cursors.owner": {"$in": [None, self.me]}},
                        {"cursors.owner_at": {"$not": {"$gte": at - dt.timedelta(seconds=self.lease)}}}]}
        doc = runs.find_one_and_update({"_id": self.id, **free}, take, projection=want,
                                       return_document=ReturnDocument.AFTER)
        if doc is None:
            held = runs.find_one({"_id": self.id}, {"cursors.owner": 1})
            if held is None:
                raise _Gone(self.id)
            holder = str((held.get("cursors") or {}).get("owner") or "")
            host, _, rest = holder.rpartition(":")[0].rpartition(":")
            if host == _HOST and rest.isdigit() and int(rest) != os.getpid() and not _alive(int(rest)):
                doc = runs.find_one_and_update({"_id": self.id, "cursors.owner": holder}, take, projection=want,
                                               return_document=ReturnDocument.AFTER)
        if doc is None:
            self.owned = False
            if not self.standby_said:
                self.standby_said = True
                self.say(f"{self.id}: another collector is writing this run; standing by")
            return False
        if self.standby_said:
            self.standby_said = False
            self.say(f"{self.id}: took over")
        self.cur = doc.get("cursors") or {}
        self.status, self.state, self.generation = doc.get("status"), doc.get("state"), doc.get("generation")
        self.book = None
        # Sequence numbers issued past the saved cursor: a pass may have died half-written.
        self.dirty = int(doc.get("last_seq") or 0) > int(self.cur.get("seq") or 0)
        self.owned = True
        return True

    def release(self) -> None:
        """Give the lease up so the next collector can start at once."""
        if self.owned:
            self.owned = False
            self.db[D.RUNS].update_one({"_id": self.id, "cursors.owner": self.me},
                                       {"$set": {"cursors.owner": None}})

    def _lost(self) -> int:
        self.owned = False
        self._claim_at = time.monotonic() + CLAIM_EVERY
        return 0

    def _reserve(self, count: int, at) -> int | None:
        """Reserve `count` timeline positions as db.next_seq does, but only while we hold the lease."""
        doc = self.db[D.RUNS].find_one_and_update(
            {"_id": self.id, "cursors.owner": self.me},
            {"$inc": {"last_seq": count}, "$set": {"cursors.owner_at": at}},
            projection={"last_seq": 1}, return_document=ReturnDocument.AFTER)
        return int(doc["last_seq"]) - count + 1 if doc else None

    def _recover(self) -> None:
        """Drop what a pass wrote after the last saved cursor, so re-reading cannot duplicate it."""
        db, rid = self.db, self.id
        db[D.EVENTS].delete_many({"run_id": rid, "seq": {"$gt": int(self.cur.get("seq") or 0)},
                                  "kind": {"$in": KINDS}})
        db[D.RULEBOOKS].delete_many(
            {"run_id": rid, "version": {"$gt": int((self.cur.get("rules") or {}).get("version") or 0)}})

    # -- reading ----------------------------------------------------------------------------

    def _stream_files(self) -> list[Path]:
        """Rotated streams oldest first, then the live one."""
        try:
            names = os.listdir(self.ws)
        except OSError:
            return []
        rotated = sorted((int(m.group(1)), name) for name in names if (m := _ROTATED.match(name)))
        return [self.ws / name for _, name in rotated] + [self.ws / "stream.jsonl"]

    def _action(self, n: int, r: dict, prev_level: int | None, prev_state: str | None, at) -> dict:
        level, full = _int(r.get("level")), bool(r.get("full_reset"))
        try:
            frames = [D.pack_grid(g) for g in r.get("frames") or []]
        except (TypeError, ValueError):
            frames = []                    # not grids; the row is kept so n still counts ledger rows
        return _clean({
            "_id": f"{self.id}:{n}", "run_id": self.id, "n": n, "i": r.get("i"), "level": level,
            "action": r.get("action"), "params": r.get("params") or {}, "state": r.get("state"),
            "full_reset": full, "available_actions": r.get("available_actions") or [], "frames": frames,
            "level_cleared": prev_level is not None and level > prev_level,
            "new_play": full or (prev_level is not None and level < prev_level),
            "death": r.get("state") == "GAME_OVER" and prev_state != "GAME_OVER",
            "wasted": not frames, "observed_at": at})

    def poll(self, final: bool = False) -> int:
        """One pass over the run's files; returns the number of documents written."""
        if not self.owned and ((time.monotonic() < self._claim_at and not final) or not self.claim(D.now())):
            return 0
        if self.dirty:
            self._recover()
            self.dirty = False
        db, rid, ws = self.db, self.id, self.ws
        cur = copy.deepcopy(self.cur)
        at = D.now()
        tc = cur.setdefault("trace", {})
        sets: dict[str, Any] = {}

        # How far this pass may read the outbox and the ledger, fixed before the stream is read.
        # Rows appended after this wait for the next pass, so the tool_use that caused a row is
        # always read no later than the row. The outbox comes first because the client writes
        # "level cleared" just before the row that cleared it.
        box_limit = _extent(self.out / "harness_outbox.jsonl")
        limit = _extent(ws / "trace.jsonl")

        # 1. stream.jsonl and rotated copies, each tailed by inode so a rename changes nothing.
        sc = cur.setdefault("stream", {})
        files = sc.setdefault("files", {})
        stream: list[dict] = []
        for p in self._stream_files():
            got = _tail(p, lambda ino: files.get(str(ino)))
            if got is None:
                continue
            lines, pos, restarted = got
            key = str(pos["ino"])
            fc = files[key] = {**(files.get(key) or {}), **pos}
            if restarted:
                fc.pop("cost", None)
            for line in lines:
                e = _json(line)
                if not isinstance(e, dict):
                    continue               # stderr mixed into the stream
                stream += stream_events(_clean(e))
                if e.get("type") == "result":
                    # total_cost_usd is cumulative within one file; num_turns is per event.
                    if isinstance(e.get("total_cost_usd"), (int, float)):
                        fc["cost"] = e["total_cost_usd"]
                    if isinstance(e.get("num_turns"), int):
                        sc["turns"] = sc.get("turns", 0) + e["num_turns"]
                    costs = [f["cost"] for f in files.values() if "cost" in f]
                    sets.update(turns=sc.get("turns"), cost_usd=sum(costs) if costs else None)

        # Rows carry no timestamp, so their place among this pass's stream events is inferred:
        # a row is appended while an action tool call is open. They go after the last such
        # tool_use read in this pass and, when that call (or one left open by an earlier pass)
        # is answered in this pass, before its tool_result.
        calls = [str(x) for x in sc.get("open") or []]
        cut = None
        for j, ev in enumerate(stream):
            if ev["kind"] == "tool_use" and ev.get("tool") in ACTION_TOOLS:
                calls.append(str(ev.get("tool_use_id")))
                cut = None
            elif ev["kind"] == "tool_result" and str(ev.get("tool_use_id")) in calls:
                calls.remove(str(ev.get("tool_use_id")))
                if cut is None:
                    cut = j
        sc["open"] = calls[-16:]
        cut = len(stream) if cut is None else cut
        n0, level0 = tc.get("n", 0), tc.get("level") or 0

        # 2. trace.jsonl -> actions.
        truncate = False
        actions: list[dict] = []
        got = _tail(ws / "trace.jsonl", lambda ino: tc, limit) if limit else None
        if got:
            lines, pos, restarted = got
            if restarted:
                tc.clear()
                truncate = True
            n, level, state, deaths = tc.get("n", 0), tc.get("level"), tc.get("state"), tc.get("deaths", 0)
            for line in lines:
                row = _json(line)
                if not isinstance(row, dict):
                    continue               # corrupt row
                doc = self._action(n, row, level, state, at)
                deaths += doc["death"]
                level, state, n = doc["level"], doc["state"], n + 1
                actions.append(doc)
            tc.update(pos, n=n, level=level, state=state, deaths=deaths)
            if lines:
                sets["deaths"] = deaths
        n1, level1 = tc.get("n", 0), tc.get("level") or 0

        def event(n: int, level: int, kind: str, **payload) -> dict:
            return {"run_id": rid, "at": at, "kind": kind, "action_n": n - 1, "level": level, **payload}

        # 3. harness_outbox.jsonl -> outbox events.
        ledger: list[dict] = list(actions)     # what the client wrote: rows, then outbox, then rule book
        oc = cur.setdefault("outbox", {})
        got = _tail(self.out / "harness_outbox.jsonl", lambda ino: oc, box_limit) if box_limit else None
        if got:
            for line in got[0]:
                o = _clean(_json(line))
                if isinstance(o, dict):
                    ledger.append(event(n1, level1, "outbox", outbox_kind=o.get("kind"), text=o.get("text")))
            oc.update(got[1])

        # 4. rules.json -> a rulebooks version and a rulebook event per content change.
        rc = cur.setdefault("rules", {})
        rulebook = book = None
        sig = _sig(ws / "rules.json")
        if sig and sig != rc.get("sig"):
            raw = _load(ws / "rules.json")
            if isinstance(raw, dict):
                book = {k: raw.get(k) if isinstance(raw.get(k), list) else [] for k in RULE_KEYS}
                sha = hashlib.sha1(json.dumps(book, sort_keys=True, default=str).encode()).hexdigest()
                version = int(rc.get("version") or 0)
                if sha != rc.get("sha") and (version or any(book.values())):
                    prev = self.book
                    if prev is None and version:
                        prev = db[D.RULEBOOKS].find_one({"_id": f"{rid}:{version}"})
                    prev = prev or {}
                    added = {k: [x for x in book[k] if x not in (prev.get(k) or [])] for k in RULE_KEYS}
                    version += 1
                    rulebook = {"_id": f"{rid}:{version}", "run_id": rid, "version": version, "at": at,
                                "action_n": n1 - 1, "level": level1, **book, "added": added}
                    ledger.append(event(n1, level1, "rulebook", version=version, added=added))
                    rc.update(version=version, sha=sha)
                else:
                    book = None
                rc["sig"] = sig

        # 5. trace.state.json and result.json -> fields of the run document.
        sig = _sig(ws / "trace.state.json")
        if sig and sig != cur.get("state_sig"):
            state_doc = _load(ws / "trace.state.json")
            if isinstance(state_doc, dict):
                sets.update({k: state_doc[k] for k in STATE_KEYS if k in state_doc})
                cur["state_sig"] = sig
        sig = _sig(ws / "result.json")
        if sig and sig != cur.get("result_sig"):
            result = _load(ws / "result.json")
            if isinstance(result, dict):
                sets["result"] = result
                sets.update({k: result[k] for k in ("cost_usd", "turns", "deaths") if result.get(k) is not None})
                cur["result_sig"] = sig

        # Events and actions in the order they get sequence numbers.
        timeline = ([event(n0, level0, ev.pop("kind"), **ev) for ev in stream[:cut]] + ledger
                    + [event(n1, level1, ev.pop("kind"), **ev) for ev in stream[cut:]])

        if sets.get("state", self.state) == "WIN" and self.status != "won":
            sets.update(status="won", ended_at=at)
        changed = bool(timeline or sets or cur != self.cur)
        if not changed:
            self._check_stale(at)
            return 0
        if cur.pop("stale", None) and "status" not in sets:
            # We marked this run stopped and it moved again.
            db[D.RUNS].update_one({"_id": rid, "status": "stopped", "cursors.owner": self.me},
                                  {"$set": {"status": "running", "ended_at": None}})
            self.status = "running"

        # Write: one block of sequence numbers, one batch per collection, then the cursors.
        events = [d for d in timeline if "kind" in d]
        first = last = 0
        if timeline:
            self.dirty = True
            first = self._reserve(len(timeline), at)
            if first is None:
                return self._lost()        # another collector took the run over; it has the cursors
            last = first + len(timeline) - 1
            for j, d in enumerate(timeline):
                d["seq"] = first + j
                if "kind" in d:
                    d["_id"] = f"{rid}:{d['seq']}"
            cur["seq"] = last
            if events:
                db[D.EVENTS].insert_many(events)
            if actions:
                db[D.ACTIONS].bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in actions],
                                         ordered=False)
        if rulebook:
            db[D.RULEBOOKS].replace_one({"_id": rulebook["_id"]}, rulebook, upsert=True)
        if truncate:
            db[D.ACTIONS].delete_many({"run_id": rid, "n": {"$gte": n1}})
        cur.update(owner=self.me, owner_at=at)
        sets.update(cursors=cur, n_actions=n1, updated_at=at)
        if not db[D.RUNS].update_one({"_id": rid, "cursors.owner": self.me}, {"$set": sets}).matched_count:
            # The lease was lost while this pass was being written: take our half back.
            if events:
                db[D.EVENTS].delete_many({"run_id": rid, "seq": {"$gte": first, "$lte": last},
                                          "kind": {"$in": KINDS}})
            return self._lost()
        self.cur, self.dirty = cur, False
        self.status, self.state = sets.get("status", self.status), sets.get("state", self.state)
        if book is not None:
            self.book = book
        self._check_stale(at)
        written = len(timeline) + bool(rulebook)
        if written:
            self.last = (f"{rid}: +{len(actions)} actions +{len(events)} events"
                         + (f" +rulebook v{rulebook['version']}" if rulebook else "")
                         + f" | n={n1} level={level1} status={self.status}")
        return written

    def _check_stale(self, at) -> None:
        """Mark a running run stopped once none of its files has changed for stale_after seconds."""
        if self.status != "running" or time.monotonic() < self._stale_check:
            return
        self._stale_check = time.monotonic() + 5.0
        watched = [self.out, self.out / D.RUN_MARKER, self.out / "harness_outbox.jsonl", self.ws] + [
            self.ws / f for f in ("trace.jsonl", "trace.state.json", "stream.jsonl", "rules.json", "result.json")]
        newest = max((s[1] for s in map(_sig, watched) if s), default=0) / 1e9
        if time.time() - newest < self.stale_after:
            return
        res = self.db[D.RUNS].find_one_and_update(
            {"_id": self.id, "status": "running", "cursors.owner": self.me},
            {"$set": {"status": "stopped", "ended_at": at, "cursors.stale": True}},
            projection={"_id": 1})
        if res:
            self.cur["stale"] = True
            self.status = "stopped"
        else:   # the launcher already set a final status
            doc = self.db[D.RUNS].find_one({"_id": self.id}, {"status": 1}) or {}
            self.status = doc.get("status", self.status)


class Collector:
    """Tails every attempt directory under `root` into `db`."""

    def __init__(self, db, root, stale_after: float = STALE_AFTER, log: Callable[[str], None] | None = None,
                 lease: float = LEASE):
        self.db = db
        self.root = Path(root).resolve()
        self.stale_after = stale_after
        self.lease = lease
        self.log = log
        self.me = f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.runs: dict[Path, _Run] = {}
        self._retry: dict[Path, tuple[float, float]] = {}     # attempt dir -> (not before, last delay)
        self._notes_retry: dict[str, float] = {}
        D.ensure_indexes(db)
        try:                               # the agent's prose under notes/ -> the `notes` collection
            from .notes_sync import NotesSync
            self.notes = NotesSync(db)
        except Exception as e:  # noqa: BLE001 -- ingest must work without the notes mirror
            self.notes = None
            self._say(f"notes mirror unavailable ({type(e).__name__})")

    def _attempt_dirs(self) -> list[Path]:
        def is_attempt(d: Path) -> bool:
            return d in self.runs or (d / D.RUN_MARKER).exists() or _find_workspace(d) is not None
        root = self.root
        if is_attempt(root):
            return [root]
        return [p for p in _subdirs(root) if is_attempt(p)]

    def poll_once(self, final: bool = False) -> int:
        """One pass over every run; returns the number of documents written.

        A run whose last pass failed is retried with a growing delay; `final` retries it now.
        """
        total = 0
        for out in self._attempt_dirs():
            wait = self._retry.get(out)
            if wait and not final and time.monotonic() < wait[0]:
                continue
            try:
                run = self.runs.get(out) or _Run.open(self.db, out, self.me, self.lease, self.stale_after,
                                                      self._say)
                if run is None:
                    continue
                self.runs[out] = run
                n = run.poll(final)
            except _Gone:
                # The run document vanished: forget the cursors and ingest the directory again.
                gone = self.runs.pop(out, None)
                if gone and self.notes:
                    self.notes.forget(gone.id)
                self._say(f"{out.name}: run document is gone from the database; starting over")
                D.ensure_indexes(self.db)
                continue
            except Exception as e:  # noqa: BLE001 -- one bad run or a network blip must not stop the rest
                # The run's cursor was not saved, so the next attempt re-reads the same bytes.
                # The first retry is the next pass; after that the delay doubles up to 30 s.
                delay = min(30.0, max(0.5, wait[1] * 2)) if wait else 0.0
                self._retry[out] = (time.monotonic() + delay, delay)
                self._say(f"{out.name}: {type(e).__name__}: {_URI.sub('mongodb://***', str(e))[:200]}"
                          + (f" (retry in {delay:g}s)" if delay else ""))
                continue
            self._retry.pop(out, None)
            notes = self._sync_notes(run)
            if n or notes:
                self._say((run.last if n else f"{run.id}:") + (f" +{notes} notes" if notes else ""))
            total += n + notes
        return total

    def _sync_notes(self, run: _Run) -> int:
        if self.notes is None or not run.owned or time.monotonic() < self._notes_retry.get(run.id, 0.0):
            return 0
        try:
            return int(self.notes.sync(run.id, run.game, run.generation, run.ws) or 0)
        except Exception as e:  # noqa: BLE001 -- the mirror is secondary; never let it stop ingest
            self._notes_retry[run.id] = time.monotonic() + 5.0
            self._say(f"{run.id}: notes: {type(e).__name__}: {_URI.sub('mongodb://***', str(e))[:200]}")
            return 0

    def close(self) -> None:
        """Release every run this collector holds, so a successor does not wait for the lease."""
        for run in self.runs.values():
            try:
                run.release()
            except Exception:  # noqa: BLE001 -- best effort on the way out; the lease expires anyway
                pass

    def run_forever(self, interval: float = 0.2, stop_event: threading.Event | None = None) -> None:
        """Poll until `stop_event` is set, then do one final pass and release the runs."""
        stop = stop_event or threading.Event()
        try:
            while not stop.is_set():
                t0 = time.monotonic()
                self.poll_once()
                stop.wait(max(0.01, interval - (time.monotonic() - t0)))
            self.poll_once(final=True)
        finally:
            self.close()

    def _say(self, text: str) -> None:
        if self.log:
            self.log(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mongoarc.collector", description=__doc__.split("\n")[0])
    ap.add_argument("--watch", required=True, metavar="ROOT",
                    help="an attempt directory, or a directory whose children are attempt directories")
    ap.add_argument("--interval", type=float, default=0.2, help="seconds between passes")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("-q", "--quiet", action="store_true", help="no per-pass lines")
    a = ap.parse_args(argv)
    log = None if a.quiet else (lambda s: print(s, file=sys.stderr, flush=True))
    c = Collector(D.get_db(), a.watch, log=log)
    if a.once:
        try:
            n = c.poll_once(final=True)
            held = sum(not r.owned for r in c.runs.values())
        finally:
            c.close()
        print(f"collector: {n} documents written, {len(c.runs)} run(s) under {c.root}"
              + (f" ({held} held by another collector)" if held else ""))
        return 0
    stop = threading.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.set())
    print(f"collector: watching {c.root} every {a.interval}s", file=sys.stderr, flush=True)
    c.run_forever(a.interval, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
