"""Loopback gateway between a game agent and its memory in MongoDB.

The agent gets a tiny CLI in its workspace (agent_tools/memory.py) that talks
to this server, so it can read and write its memory without ever holding the
database credentials. Every call is scoped to ONE run: the run id is in every
query, so a run can never read, change or delete what another run or game
stored.

Memory is the database, not files. The agent's prose lives in the `notes`
collection and is written here, directly: `save` inserts a note, `update`
replaces its text in place (version + 1, the previous text kept in a short
`revisions` list) and `delete` removes it. Only code is kept as files, in the
workspace's scripts/; a note names the scripts it depends on (`refs`).

Retrieval is MongoDB's: `recall` runs an Atlas Vector Search with automated
embedding, so Atlas embeds each note when it is saved and embeds the question
when it is asked. Every search and every write is logged on the run's timeline,
which is how the dashboard shows the agent using its memory.

  GET  /recall?run=<run_id>&q=<question>&n=5
  GET  /list?run=<run_id>
  POST /save      {"run": <run_id>, "title": ..., "text": ...}
  POST /update    {"run": <run_id>, "id": "N3" | "<run_id>:mem/3", "text": ...}
  POST /delete    {"run": <run_id>, "id": "N3" | "<run_id>:mem/3"}

usage: python -m mongoarc.memory_gateway [--port 8790]
"""
from __future__ import annotations

import time
import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pymongo import ReturnDocument

from . import db as D
from .notes_sync import MAX_CHARS, extract_refs

DEFAULT_PORT = 8790
MAX_TITLE = 120                 # a title is one line
MAX_REVISIONS = 5               # previous texts kept on a note
MAX_BODY = 1 << 20              # bytes of one POST
_SHORT = re.compile(r"[Nn]?(\d{1,9})")


class Refused(ValueError):
    """A memory call the gateway will not carry out; the message is the one-line reason the agent reads."""


def _where(db, run_id: str) -> dict:
    run = db[D.RUNS].find_one({"_id": run_id}, {"n_actions": 1, "level": 1}) or {}
    return {"action_n": int(run.get("n_actions") or 0) - 1, "level": int(run.get("level") or 0)}


def _log(db, run_id: str, kind: str, **payload) -> None:
    seq = D.next_seq(db, run_id)
    db[D.EVENTS].insert_one({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": D.now(),
                             "kind": kind, **_where(db, run_id), **payload})


def _words(query: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}


def _one_line(text, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def short_id(path: str | None) -> str | None:
    """"N3" for a note saved through the tool (path "mem/3"); None for one mirrored from a file."""
    path = path or ""
    return "N" + path[4:] if path.startswith("mem/") and path[4:].isdigit() else None


def _resolve(run_id: str, ref) -> str:
    """The `_id` a note reference means inside this run: "N3", "3", "mem/3" or a full `_id`."""
    ref = str(ref or "").strip()
    m = _SHORT.fullmatch(ref)
    if m:
        return f"{run_id}:mem/{int(m.group(1))}"
    return f"{run_id}:{ref}" if ref.startswith(("mem/", "notes/")) else ref


def _text(text) -> str:
    text = str(text or "").replace("\r\n", "\n").strip()
    if not text:
        raise Refused("nothing to store: the text is empty")
    return text[:MAX_CHARS]


def _no_such(db, run_id: str, note_id: str, ref) -> Refused:
    """Why a write found no note of THIS run to act on."""
    if not _one_line(ref, 80):
        return Refused("which note? give its id, for example N3 (python3 memory.py list shows them)")
    doc = db[D.NOTES].find_one({"_id": note_id, "run_id": run_id}, {"path": 1}) if run_id else None
    if doc:
        return Refused(f"{doc.get('path')} is a file mirrored into memory, not a note saved with memory.py: "
                       "it cannot be changed here")
    return Refused(f"no note {_one_line(ref, 80)} in this game's memory (python3 memory.py list shows the ids)")


def _note_out(doc: dict, cap: int) -> dict:
    return {"id": short_id(doc.get("path")), "title": doc.get("title"), "version": doc.get("version"),
            "path": doc.get("path"), "refs": doc.get("refs") or [], "text": (doc.get("text") or "")[:cap]}


def save(db, run_id: str, title: str, text: str) -> str:
    """Add a note to THIS run's memory; returns its `_id`, "<run_id>:mem/<n>".

    `n` comes from a counter on the run's own document ($inc, so two saves at
    once never share a number). A run the harness never registered is refused.
    """
    run_id = str(run_id or "")
    title = _one_line(title, MAX_TITLE)
    if not title:
        raise Refused('a note needs a title: python3 memory.py save "<title>" "<text>"')
    text = _text(text)
    run = db[D.RUNS].find_one_and_update(
        {"_id": run_id}, {"$inc": {"note_seq": 1}}, return_document=ReturnDocument.AFTER,
        projection={"note_seq": 1, "game_id": 1, "generation": 1, "level": 1}) if run_id else None
    if not run:
        raise Refused("this run is not known to the memory (MONGOARC_RUN_ID is missing or wrong)")
    n = int(run["note_seq"])
    note_id, at = f"{run_id}:mem/{n}", D.now()
    db[D.NOTES].insert_one({
        "_id": note_id, "run_id": run_id, "game_id": run.get("game_id"), "generation": run.get("generation"),
        "level": int(run.get("level") or 0), "path": f"mem/{n}", "title": title, "text": text,
        "refs": extract_refs(text), "source": "tool", "version": 1, "created_at": at, "updated_at": at})
    _log(db, run_id, "memory_write", op="save", note=note_id, title=title,
         text=f'agent saved note N{n} "{title}": {_one_line(text, 160)}')
    return note_id


def update(db, run_id: str, note_id: str, text: str) -> int:
    """Replace the text of one of THIS run's notes in place; returns its new version.

    The text it replaces is appended to `revisions` ([{version, text, at}], `at`
    being when that version was written), of which the last five are kept.
    """
    run_id = str(run_id or "")
    text = _text(text)
    nid = _resolve(run_id, note_id)
    old = {"version": {"$ifNull": ["$version", 1]}, "text": "$text",
           "at": {"$ifNull": ["$updated_at", "$created_at"]}}
    doc = db[D.NOTES].find_one_and_update(
        {"_id": nid, "run_id": run_id, "source": "tool"},
        # One pipeline stage: every expression reads the document as it was, so `old` is the text being replaced.
        # $literal keeps a note that happens to start with "$" from being read as a field path.
        [{"$set": {"revisions": {"$slice": [{"$concatArrays": [{"$ifNull": ["$revisions", []]}, [old]]},
                                            -MAX_REVISIONS]},
                   "text": {"$literal": text}, "refs": {"$literal": extract_refs(text)},
                   "version": {"$add": [{"$ifNull": ["$version", 1]}, 1]}, "updated_at": D.now()}}],
        projection={"title": 1, "version": 1, "path": 1},
        return_document=ReturnDocument.AFTER) if run_id else None
    if not doc:
        raise _no_such(db, run_id, nid, note_id)
    version, title = int(doc["version"]), doc.get("title") or ""
    _log(db, run_id, "memory_write", op="update", note=nid, title=title, version=version,
         text=f'agent updated note {short_id(doc.get("path"))} "{title}" (version {version}): {_one_line(text, 160)}')
    return version


def delete(db, run_id: str, note_id: str) -> str:
    """Delete one of THIS run's notes; returns the `_id` that was removed."""
    run_id = str(run_id or "")
    nid = _resolve(run_id, note_id)
    doc = db[D.NOTES].find_one_and_delete({"_id": nid, "run_id": run_id, "source": "tool"}) if run_id else None
    if not doc:
        raise _no_such(db, run_id, nid, note_id)
    title = doc.get("title") or ""
    _log(db, run_id, "memory_write", op="delete", note=nid, title=title,
         text=f'agent deleted note {short_id(doc.get("path"))} "{title}": {_one_line(doc.get("text"), 160)}')
    return nid


NOTE_FIELDS = {"text": 1, "path": 1, "refs": 1, "title": 1, "version": 1, "level": 1}


def list_memory(db, run_id: str) -> dict:
    """Every note stored for THIS run, in the order they were saved. Logged as a memory read (op "list")."""
    run_id = str(run_id or "")
    notes: list[dict] = []
    if run_id:
        docs = list(db[D.NOTES].find({"run_id": run_id}, NOTE_FIELDS))
        docs.sort(key=lambda d: (0, int(d["path"][4:]), "") if short_id(d.get("path")) else (1, 0, d.get("path") or ""))
        notes = [_note_out(d, MAX_CHARS) for d in docs]
        _log(db, run_id, "recall", op="list", query="", notes=[d["_id"] for d in docs], engine="list",
             text=f"agent listed its memory: {len(notes)} note(s)")
    return {"notes": notes}


def _semantic(db, run_id: str, query: str, n: int) -> list[dict]:
    """Atlas Vector Search with automated embedding: MongoDB embeds the question and ranks this run's notes."""
    return list(db[D.NOTES].aggregate([
        {"$vectorSearch": {"index": D.VECTOR_INDEX, "path": "text", "query": {"text": query},
                           "numCandidates": max(50, n * 10), "limit": n, "filter": {"run_id": run_id}}},
        {"$project": {**NOTE_FIELDS, "score": {"$meta": "vectorSearchScore"}}}]))


def _keyword(db, run_id: str, query: str, n: int) -> list[dict]:
    """The text index, for a server without Atlas Vector Search (a plain local mongod) or an index still building."""
    notes = list(db[D.NOTES].find({"run_id": run_id, "$text": {"$search": query}},
                                  {**NOTE_FIELDS, "score": {"$meta": "textScore"}})
                 .sort([("score", {"$meta": "textScore"})]).limit(n))
    words = _words(query)
    if len(notes) < n and words:                  # a title is not in the text index: match its words directly
        notes += list(db[D.NOTES].find(
            {"run_id": run_id, "_id": {"$nin": [x["_id"] for x in notes]},
             "title": {"$regex": "|".join(sorted(words)), "$options": "i"}},
            NOTE_FIELDS).sort("updated_at", -1).limit(n - len(notes)))
    return notes


INDEX_WAIT_S = 6.0      # longest a search waits for notes that are stored but not yet embedded


def recall(db, run_id: str, query: str, n: int = 5) -> dict:
    """The notes of THIS run closest in meaning to the question: nothing from any other run or game.

    MongoDB does the retrieval. Each note's raw text is embedded by Atlas when it
    is saved (automated embedding), and the question is embedded by Atlas too, so
    this is raw text in and a plain-language question out. Each note comes back
    with its short id ("N3") and title, which is what `update` and `delete` take.
    """
    from pymongo.errors import PyMongoError
    run_id, query = str(run_id or ""), " ".join(str(query or "").split())
    notes: list[dict] = []
    engine, stored = "none", 0
    if run_id:
        stored = db[D.NOTES].count_documents({"run_id": run_id})
        if stored and query:
            try:
                notes, engine = _semantic(db, run_id, query, n), "atlas_vector_search"
                waited = 0.0                      # Atlas embeds a note a moment after it is saved: a search made
                while len(notes) < min(n, stored) and waited < INDEX_WAIT_S:   # right after a save waits for it
                    time.sleep(0.5); waited += 0.5
                    notes = _semantic(db, run_id, query, n)
            except PyMongoError:
                notes, engine = _keyword(db, run_id, query, n), "text"
        elif stored:                              # no question: the most recently touched notes
            notes, engine = list(db[D.NOTES].find({"run_id": run_id}, NOTE_FIELDS).sort("updated_at", -1).limit(n)), "recent"
        _log(db, run_id, "recall", query=query, notes=[x["_id"] for x in notes], engine=engine, stored=stored,
             text=f"agent searched its memory: {query!r} -> {len(notes)} of {stored} note(s)")
    out = []
    for x in notes:
        o = _note_out(x, 2000)
        o["level"] = x.get("level")
        if isinstance(x.get("score"), (int, float)):
            o["score"] = round(float(x["score"]), 3)
        out.append(o)
    return {"notes": out, "engine": engine, "stored": stored}


class _Handler(BaseHTTPRequestHandler):
    db = None                                   # serve(port, db) pins one; otherwise the project database

    def log_message(self, *a): pass

    def _db(self):
        return self.db if self.db is not None else D.get_db()

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
                return self._send(200, recall(self._db(), q.get("run", ""), q.get("q", ""), int(q.get("n", 5))))
            if url.path == "/list":
                return self._send(200, list_memory(self._db(), q.get("run", "")))
            if url.path == "/health":
                return self._send(200, {"ok": True})
        except Exception as exc:  # the agent must get an answer, not a hung call
            return self._send(500, {"error": str(exc)[:300]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                return self._send(413, {"error": "the text is too long to store"})
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise Refused("the request is not a JSON object")
            db, run, ref, raw = self._db(), str(body.get("run") or ""), body.get("id"), body.get("text")
            cut = isinstance(raw, str) and len(raw.replace("\r\n", "\n").strip()) > MAX_CHARS
            short = lambda nid: "N" + nid.rsplit("/", 1)[-1]     # a written note's _id always ends in mem/<n>
            if url.path == "/save":
                nid = save(db, run, body.get("title"), raw)
                return self._send(200, {"id": nid, "short": short(nid), "version": 1, "cut": cut})
            if url.path == "/update":
                version = update(db, run, ref, raw)
                nid = _resolve(run, ref)
                return self._send(200, {"id": nid, "short": short(nid), "version": version, "cut": cut})
            if url.path == "/delete":
                nid = delete(db, run, ref)
                return self._send(200, {"id": nid, "short": short(nid)})
        except Refused as exc:
            return self._send(400, {"error": str(exc)})
        except Exception as exc:  # the agent must get an answer, not a hung call
            return self._send(500, {"error": str(exc)[:300]})
        self._send(404, {"error": "not found"})


def serve(port: int = DEFAULT_PORT, db=None) -> ThreadingHTTPServer:
    """Start the gateway on a daemon thread; returns the server (call .shutdown() to stop).

    `db` pins the database the handlers use (tests); by default it is the project database.
    """
    handler = _Handler if db is None else type("_Handler", (_Handler,), {"db": db})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
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
