"""Live view: a read-only JSON API over MongoDB plus the static page."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pymongo.errors import PyMongoError

from mongoarc import db as D
from mongoarc.view.memory_api import router as memory_router

STATIC = Path(__file__).resolve().parent / "static"
# Kept out of the run list: large, and nothing in the list view needs them.
HEAVY = {"result": 0, "cursors": 0}
NO_CACHE = {"Cache-Control": "no-store"}

class _Static(StaticFiles):
    """Static files that the browser revalidates on every load (ETag, 304), so an edit shows on a plain refresh."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app = FastAPI(title="mongoARC live view", docs_url=None, redoc_url=None)
app.mount("/static", _Static(directory=STATIC), name="static")
app.include_router(memory_router)


def _db():
    return D.get_db()


def _json(data) -> JSONResponse:
    return JSONResponse(D.jsonable(data), headers=NO_CACHE)


@app.exception_handler(PyMongoError)
def _mongo_down(_request, exc: PyMongoError) -> JSONResponse:
    # The class name only: driver messages can carry the connection string.
    return JSONResponse({"error": "database unavailable", "type": type(exc).__name__}, status_code=503)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers=NO_CACHE)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    # A 16x16 yellow square on black, so the browser console stays free of a 404.
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#07090d"/>'
           '<rect x="3" y="3" width="10" height="10" fill="#FFDC00"/></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/palette")
def palette():
    return _json(D.PALETTE)


@app.get("/api/runs")
def runs(limit: int = Query(200, ge=1, le=1000)):
    """Newest first, without the heavy fields."""
    cur = _db()[D.RUNS].find({}, HEAVY).sort([("started_at", -1), ("_id", -1)]).limit(limit)
    return _json(list(cur))


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    doc = _db()[D.RUNS].find_one({"_id": run_id}, {"cursors": 0})
    if doc is None:
        raise HTTPException(404, f"no such run: {run_id}")
    return _json(doc)


@app.get("/api/runs/{run_id}/actions")
def actions(run_id: str, after_n: int = -1, limit: int = Query(200, ge=1, le=500),
            frames: int = Query(1, ge=0, le=1)):
    """Ledger rows with n > after_n, ascending. frames=0 leaves the boards out: small rows for the
    counters of a run joined part-way, where only the last few boards are needed."""
    proj = None if frames else {"frames": 0, "available_actions": 0}
    cur = _db()[D.ACTIONS].find({"run_id": run_id, "n": {"$gt": after_n}}, proj).sort("n", 1).limit(limit)
    return _json(list(cur))


@app.get("/api/runs/{run_id}/events")
def events(run_id: str, after_seq: int = 0, limit: int = Query(500, ge=1, le=2000), kind: str = ""):
    """Timeline events with seq > after_seq, ascending. kind=recall,memory_write,harness keeps only those
    kinds: how the page lists, per level, what the agent wrote to its memory, what it searched for, and
    the prompts the harness sent it at level boundaries."""
    match: dict = {"run_id": run_id, "seq": {"$gt": after_seq}}
    kinds = [k for k in kind.split(",") if k]
    if kinds:
        match["kind"] = {"$in": kinds}
    cur = _db()[D.EVENTS].find(match).sort("seq", 1).limit(limit)
    return _json(list(cur))


@app.get("/api/runs/{run_id}/rulebooks")
def rulebooks(run_id: str):
    """Only runs recorded under the earlier design have any; the page then shows them in place of notes."""
    return _json(list(_db()[D.RULEBOOKS].find({"run_id": run_id}).sort("version", 1)))
