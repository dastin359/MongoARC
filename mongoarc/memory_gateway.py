"""Loopback gateway between a game agent and the harness's memory in MongoDB.

The agent gets a tiny CLI in its workspace (agent_tools/memory.py) that talks
to this server, so it can search what the earlier levels of its own game left
in MongoDB without ever holding the database credentials. A search sees one
run only: nothing crosses from one game or run to another. Every search is
logged on the run's timeline, which is how the live view shows the agent
consulting its memory and how the harness learns which lessons get used.

Writing is not done here: the agent writes prose under notes/ in its workspace
and the collector mirrors those files into the `notes` collection.

  GET /recall?run=<run_id>&q=<words>&n=5

usage: python -m mongoarc.memory_gateway [--port 8790]
"""
from __future__ import annotations

import argparse
import json
import re
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


def _words(query: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}


def recall(db, run_id: str, query: str, n: int = 5) -> dict:
    """What THIS run remembers that matches the query: nothing from any other run or game.

    Three banks, all scoped to `run_id`: the lessons the harness distilled each
    time a level was cleared, the notes the agent wrote, and the rules in its
    latest rule book. When the words match nothing, the most recent lessons and
    rules come back anyway, so a search is never empty once the run has memory.
    """
    lessons: list[dict] = []
    notes: list[dict] = []
    rules: list[dict] = []
    if run_id:
        score = {"score": {"$meta": "textScore"}}
        by_score = [("score", {"$meta": "textScore"})]
        mine = {"run_id": run_id, "status": "active"}
        fields = {"text": 1, "kind": 1, "level": 1}
        if query.strip():
            text = {"$text": {"$search": query}}
            lessons = list(db[D.LESSONS].find({**mine, **text}, {**fields, **score}).sort(by_score).limit(n))
            notes = list(db[D.NOTES].find({"run_id": run_id, **text},
                                          {"text": 1, "path": 1, "refs": 1, **score}).sort(by_score).limit(3))
        if not lessons:                           # nothing matched: the newest lessons of this run
            lessons = list(db[D.LESSONS].find(mine, fields).sort("_id", -1).limit(n))
        if not notes and not query.strip():
            notes = list(db[D.NOTES].find({"run_id": run_id}, {"text": 1, "path": 1, "refs": 1}).limit(2))
        book = db[D.RULEBOOKS].find_one({"run_id": run_id}, sort=[("version", -1)]) or {}
        words = _words(query)
        for status in ("verified", "refuted"):
            entries = [r for r in book.get(status) or [] if isinstance(r, dict) and isinstance(r.get("rule"), str)]
            hits = [r for r in entries if words & _words(r["rule"])]
            for r in (hits or entries[::-1])[:6 if status == "verified" else 3]:
                rules.append({"status": status, "level": r.get("level") if isinstance(r.get("level"), int) else None,
                              "rule": r["rule"][:400]})
        ids = [r["_id"] for r in lessons]
        if ids:
            db[D.LESSONS].update_many({"_id": {"$in": ids}, "run_id": run_id}, {"$inc": {"stats.recalled": 1}})
        _log(db, run_id, "recall", query=query, lessons=ids, notes=[x["_id"] for x in notes], rules=len(rules),
             text=f"agent searched its memory for {query!r}: {len(ids)} lesson(s), {len(notes)} note(s), "
                  f"{len(rules)} rule(s) from this game's earlier levels")
    return {"lessons": [{"id": r["_id"], "kind": r.get("kind"), "level": r.get("level"), "text": r.get("text")}
                        for r in lessons],
            "notes": [{"path": x.get("path"), "refs": x.get("refs") or [], "text": (x.get("text") or "")[:1200]}
                      for x in notes],
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
