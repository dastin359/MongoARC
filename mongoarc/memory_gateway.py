"""Loopback gateway between a game agent and the harness's memory in MongoDB.

The agent gets a tiny CLI in its workspace (agent_tools/memory.py) that talks
to this server, so it can search what earlier games left in MongoDB without
ever holding the database credentials. Every search is logged on the run's
timeline, which is how the live view shows the agent consulting its memory and
how the harness learns which lessons actually get used.

Writing is not done here: the agent writes prose under notes/ in its workspace
and the collector mirrors those files into the `notes` collection.

  GET /recall?run=<run_id>&q=<words>&n=5

usage: python -m mongoarc.memory_gateway [--port 8790]
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db as D

DEFAULT_PORT = 8790


def _where(db, run_id: str) -> dict:
    run = db[D.RUNS].find_one({"_id": run_id}, {"n_actions": 1, "level": 1}) or {}
    return {"action_n": int(run.get("n_actions") or 0) - 1, "level": int(run.get("level") or 0)}


def _log(db, run_id: str, kind: str, **payload) -> None:
    seq = D.next_seq(db, run_id)
    db[D.EVENTS].insert_one({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": D.now(),
                             "kind": kind, **_where(db, run_id), **payload})


def recall(db, run_id: str, query: str, n: int = 5) -> dict:
    """What the harness remembers that matches the query, from every bank.

    Lessons are distilled and game-independent. Notes and rules are the raw
    record of earlier games (never this run's own), so they come back labelled
    with the game they were written in.
    """
    score = {"score": {"$meta": "textScore"}}
    by_score = [("score", {"$meta": "textScore"})]
    lessons: list[dict] = []
    notes: list[dict] = []
    books: list[dict] = []
    if query.strip():
        text = {"$text": {"$search": query}}
        lessons = list(db[D.LESSONS].find({"status": "active", **text}, {"text": 1, "kind": 1, **score})
                       .sort(by_score).limit(n))
        notes = list(db[D.NOTES].find({"run_id": {"$ne": run_id}, **text},
                                      {"text": 1, "path": 1, "game_id": 1, "refs": 1, **score}).sort(by_score).limit(3))
        books = list(db[D.RULEBOOKS].find({"run_id": {"$ne": run_id}, **text},
                                          {"verified": 1, "refuted": 1, "run_id": 1, "version": 1, **score})
                     .sort(by_score).limit(8))
    if not lessons:
        lessons = list(db[D.LESSONS].find({"status": "active"}, {"text": 1, "kind": 1})
                       .sort("stats.injected", -1).limit(n))
    latest: dict[str, dict] = {}
    for b in books:  # one rule book per earlier run: its newest matching version
        if b["run_id"] not in latest or b["version"] > latest[b["run_id"]]["version"]:
            latest[b["run_id"]] = b
    games = {r["_id"]: r.get("game_id") for r in db[D.RUNS].find({"_id": {"$in": list(latest)}}, {"game_id": 1})}
    words = {w.lower() for w in query.split() if len(w) > 2}
    rules = []
    for b in list(latest.values())[:3]:
        hits = [v["rule"] for v in b.get("verified", []) if words & set(v.get("rule", "").lower().split())]
        for rule in (hits or [v["rule"] for v in b.get("verified", [])])[:4]:
            rules.append({"game": games.get(b["run_id"]), "rule": rule})
    ids = [r["_id"] for r in lessons]
    if ids:
        db[D.LESSONS].update_many({"_id": {"$in": ids}}, {"$inc": {"stats.recalled": 1}})
    if run_id:
        _log(db, run_id, "recall", query=query, lessons=ids, notes=[x["_id"] for x in notes],
             text=f"agent searched memory for {query!r}: {len(ids)} lesson(s), {len(notes)} note(s), "
                  f"{len(rules)} rule(s) from earlier games")
    return {"lessons": [{"id": r["_id"], "kind": r.get("kind"), "text": r.get("text")} for r in lessons],
            "notes": [{"game": x.get("game_id"), "path": x.get("path"), "refs": x.get("refs") or [],
                       "text": (x.get("text") or "")[:1200]} for x in notes],
            "rules": rules}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/recall":
                return self._send(200, recall(D.get_db(), q.get("run", ""), q.get("q", ""), int(q.get("n", 5))))
            if url.path == "/health":
                return self._send(200, {"ok": True})
        except Exception as exc:  # the agent must get an answer, not a hung call
            return self._send(500, {"error": str(exc)[:300]})
        self._send(404, {"error": "not found"})


def serve(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Start the gateway on a daemon thread; returns the server (call .shutdown() to stop)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="memory-gateway").start()
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    D.ensure_indexes(D.get_db())
    print(f"memory gateway on http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
