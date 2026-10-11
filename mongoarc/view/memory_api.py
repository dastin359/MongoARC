"""Live memory browser: the agent's notes, read straight from MongoDB.

The memory is one collection, db.notes: the agent writes every note itself, Atlas embeds the text and
answers the agent's plain-language searches. `router` is a fastapi.APIRouter the live view includes:

    GET /api/memory/banks?run_id=             the bank (notes) with live counts (run_count: one run's documents)
    GET /api/memory/runs                      run_id -> {game_id, status} (labels for cards)
    GET /api/memory/stream                    Server-Sent Events fed by a MongoDB change stream
    GET /api/memory/{bank}?limit=&q=&run_id=  documents, newest first
    GET /api/memory/{bank}/{doc_id}           one document

Standalone, for development:  python -m mongoarc.view.memory_api --port 8811
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import threading
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pymongo.errors import OperationFailure, PyMongoError

from mongoarc import db as D

STATIC = Path(__file__).resolve().parent / "static"
NO_CACHE = {"Cache-Control": "no-store"}
HEARTBEAT_S = 10.0
POLL_S = 1.0
OPS = ("insert", "update", "replace", "delete")

# label, one-line description, the field that says when a document last changed.
# Collections of the earlier design (lessons, generations, rulebooks) are not banks: an old database's are not listed.
BANKS = {
    D.NOTES: ("NOTES", "What the agent wrote down, one document per note. Atlas embeds each note and answers "
                       "the agent's plain-language searches.", "updated_at"),
}
# Fields the substring fallback searches when $text finds nothing (it matches whole words only).
TEXT_FIELDS = ["text", "title", "_id", "game_id"]

router = APIRouter()
_streams = {"active": 0, "opened": 0, "mode": None}
_streams_lock = threading.Lock()


def _db():
    return D.get_db()


def _json(data, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(D.jsonable(data), headers={**NO_CACHE, **(headers or {})})


def _bank(name: str) -> str:
    if name not in D.MEMORY_BANKS:
        raise HTTPException(404, f"no such memory bank: {name} (banks: {', '.join(D.MEMORY_BANKS)})")
    return name


def _sort(bank: str) -> list[tuple[str, int]]:
    return [(BANKS[bank][2], -1), ("_id", -1)]


def _run_filter(bank: str, run_id: str) -> dict:
    return {"run_id": run_id} if run_id else {}


def _bank_state(db, bank: str) -> tuple[int, object]:
    field = BANKS[bank][2]
    newest = db[bank].find_one({field: {"$ne": None}}, {field: 1}, sort=[(field, -1)])
    return db[bank].count_documents({}), (newest or {}).get(field)


@router.get("/api/memory/banks")
def banks(run_id: str = ""):
    """count is the whole bank; with run_id each bank also says how many of its documents are that run's."""
    db = _db()
    out = []
    for name in D.MEMORY_BANKS:
        count, updated = _bank_state(db, name)
        label, description, _ = BANKS[name]
        row = {"name": name, "label": label, "description": description, "count": count, "updated_at": updated}
        if run_id:
            row["run_count"] = db[name].count_documents(_run_filter(name, run_id))
        out.append(row)
    return _json(out)


@router.get("/api/memory/actions")
def actions(run_id: str = "", limit: int = Query(500, ge=1, le=5000)):
    """Every memory action an agent took, newest first: each save, update, delete and search.

    These are the `memory_write` and `recall` events the memory gateway logs. With
    run_id, that run's only.
    """
    q: dict = {"kind": {"$in": ["memory_write", "recall"]}, "op": {"$ne": "list"}}
    if run_id:
        q["run_id"] = run_id
    cur = _db()[D.EVENTS].find(q).sort([("at", -1), ("seq", -1)]).limit(limit)
    return _json(list(cur))


@router.get("/api/memory/runs")
def run_labels(limit: int = Query(1000, ge=1, le=5000)):
    """run_id -> the few run fields a memory card shows next to a run id."""
    cur = _db()[D.RUNS].find({}, {"game_id": 1, "status": 1}).sort("started_at", -1).limit(limit)
    return _json({r["_id"]: {"game_id": r.get("game_id"), "status": r.get("status")} for r in cur})


# ---------------------------------------------------------------- change stream

def _change_event(change: dict) -> dict:
    wall = change.get("wallTime")
    if wall is None and change.get("clusterTime") is not None:
        wall = change["clusterTime"].as_datetime()
    op = change["operationType"]
    event = {"bank": change["ns"]["coll"], "op": op, "id": change["documentKey"]["_id"],
             "doc": None if op == "delete" else change.get("fullDocument"), "at": wall or D.now()}
    if op == "update":
        desc = change.get("updateDescription") or {}
        event["fields"] = sorted(set(desc.get("updatedFields") or {}) | set(desc.get("removedFields") or []))
    return D.jsonable(event)


def _poll(db, emit, stop: threading.Event, ready: threading.Event, info: dict) -> None:
    """No change streams (standalone mongod): watch each bank's count and newest change time."""
    info["mode"] = _streams["mode"] = "poll"
    seen: dict = {}
    first = True
    while not stop.is_set():
        try:
            for bank in D.MEMORY_BANKS:
                state = _bank_state(db, bank)
                if not first and seen.get(bank) != state:
                    emit(D.jsonable({"bank": bank, "op": "refresh", "at": D.now()}))
                seen[bank] = state
            first = False
        except PyMongoError:
            pass  # database briefly away: keep the stream open and try again
        ready.set()
        stop.wait(POLL_S)


def _watch(emit, stop: threading.Event, ready: threading.Event, info: dict) -> None:
    """Blocking reader, one per connected client. Runs in its own thread until `stop` is set."""
    with _streams_lock:
        _streams["active"] += 1
        _streams["opened"] += 1
    try:
        db = _db()
        pipeline = [{"$match": {"ns.coll": {"$in": list(D.MEMORY_BANKS)}, "operationType": {"$in": list(OPS)}}}]
        try:
            if os.environ.get("MONGOARC_MEMORY_POLL"):
                raise OperationFailure("change streams disabled by MONGOARC_MEMORY_POLL")
            with db.watch(pipeline, full_document="updateLookup", max_await_time_ms=500) as cs:
                info["mode"] = _streams["mode"] = "change_stream"
                ready.set()
                while not stop.is_set() and cs.alive:
                    change = cs.try_next()  # waits up to max_await_time_ms, so `stop` is seen promptly
                    if change is not None:
                        emit(_change_event(change))
        except PyMongoError:
            if not stop.is_set():
                _poll(db, emit, stop, ready, info)
    finally:
        ready.set()
        with _streams_lock:
            _streams["active"] -= 1


def _server_exiting() -> bool:
    """True once uvicorn has been told to stop.

    uvicorn waits for open connections before it exits, and an event stream never ends by
    itself, so the stream has to notice the shutdown or Ctrl-C would hang until every browser
    tab is closed. uvicorn installs its Server.handle_exit as the signal handler; that is how
    the running server is found here. Under any other server this is simply False.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        server = getattr(signal.getsignal(sig), "__self__", None)
        if getattr(server, "should_exit", False) is True:
            return True
    return False


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/api/memory/stream")
async def stream(request: Request):
    """One SSE message per change in a memory bank: {bank, op, id, doc, at}.

    op is insert | update | replace | delete (update also carries `fields`, the changed field
    names), or refresh when the server had to fall back to polling. A `hello` event opens the
    stream and says which: {"mode": "change_stream" | "poll"}.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop, ready, info = threading.Event(), threading.Event(), {"mode": None}

    def emit(event: dict) -> None:  # called from the reader thread
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:  # the loop is gone: the server is shutting down
            stop.set()

    threading.Thread(target=_watch, args=(emit, stop, ready, info), name="memory-stream", daemon=True).start()

    async def body():
        try:
            # Say "connected" only once the change stream is open, so a client that refreshes
            # on open cannot miss a write made in between.
            await asyncio.to_thread(ready.wait, 5.0)
            yield "retry: 2000\n: connected\n\n"
            # A named event, so plain `onmessage` consumers still see exactly one message per change.
            yield f"event: hello\ndata: {json.dumps({'mode': info['mode']})}\n\n"
            beat = time.monotonic() + HEARTBEAT_S
            while not _server_exiting() and not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(1.0, max(0.05, beat - time.monotonic())))
                    yield _sse(event)
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() >= beat:
                    yield ": heartbeat\n\n"
                    beat = time.monotonic() + HEARTBEAT_S
        finally:
            stop.set()

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={**NO_CACHE, "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- documents

@router.get("/api/memory/{bank}")
def documents(bank: str, limit: int = Query(100, ge=1, le=1000), q: str = "", run_id: str = ""):
    """Documents of the bank, newest change first, exactly as stored.

    q: a $text search for the page's search box (substring match when no whole word hits). This is the
    viewer's filter, not the agent's search: the agent's recall goes through Atlas Vector Search.
    """
    coll = _db()[_bank(bank)]
    base = _run_filter(bank, run_id)
    q = q.strip()
    mode = "none"
    docs: list[dict] = []
    if q:
        mode = "text"
        try:
            docs = list(coll.find({**base, "$text": {"$search": q}}).sort(_sort(bank)).limit(limit))
        except OperationFailure:  # no text index on this database yet
            docs = []
        if not docs:
            mode = "regex"
            rx = {"$regex": re.escape(q), "$options": "i"}
            match = {"$or": [{f: rx} for f in TEXT_FIELDS]}
            docs = list(coll.find({"$and": [base, match]}).sort(_sort(bank)).limit(limit))
    else:
        docs = list(coll.find(base).sort(_sort(bank)).limit(limit))
    return _json(docs, {"X-Memory-Search": mode})


@router.get("/api/memory/{bank}/{doc_id:path}")
def document(bank: str, doc_id: str):
    doc = _db()[_bank(bank)].find_one({"_id": doc_id})
    if doc is None:
        raise HTTPException(404, f"no such document in {bank}: {doc_id}")
    return _json(doc)


# ---------------------------------------------------------------- standalone

def build_app() -> FastAPI:
    """The memory browser on its own: the router, /static, and memory.html at / and /memory."""
    app = FastAPI(title="mongoARC memory browser", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(PyMongoError)
    def _mongo_down(_request, exc: PyMongoError) -> JSONResponse:
        # The class name only: driver messages can carry the connection string.
        return JSONResponse({"error": "database unavailable", "type": type(exc).__name__}, status_code=503)

    @app.get("/")
    @app.get("/memory")
    def page() -> FileResponse:
        return FileResponse(STATIC / "memory.html", headers=NO_CACHE)

    @app.get("/debug/memory-streams")
    def streams():
        return dict(_streams)

    app.include_router(router)
    return app


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m mongoarc.view.memory_api", description="mongoARC memory browser")
    ap.add_argument("--port", type=int, default=8811)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    print(f"mongoARC memory browser on http://{args.host}:{args.port}/memory", flush=True)
    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
