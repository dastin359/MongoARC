"""MongoDB connection, collection names, indexes and the frame codec.

Everything that reads or writes MongoDB goes through this module, so the
collector, the live view and the evolution loop agree on one schema.
The schema itself is documented in docs/SCHEMA.md.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, TEXT, MongoClient
from pymongo.database import Database

REPO = Path(__file__).resolve().parent.parent

# Collection names.
RUNS = "runs"
ACTIONS = "actions"
EVENTS = "events"
RULEBOOKS = "rulebooks"
LESSONS = "lessons"
GENERATIONS = "generations"
NOTES = "notes"

# The memory banks a viewer can open: what the harness remembers, as opposed to
# the raw record of play (actions, events).
MEMORY_BANKS = [NOTES]

# Atlas Vector Search index on the notes: Atlas embeds each note's text itself (automated
# embedding) and embeds the query too, so memory search is raw text in, plain-language question out.
VECTOR_INDEX = "notes_semantic"
VECTOR_MODEL = "voyage-4"

# ARC-AGI-3 palette, index 0..15. Index 5 is the background.
PALETTE = ["#FFFFFF", "#CCCCCC", "#999999", "#666666", "#333333", "#000000", "#E53AA3", "#FF7BCC",
           "#F93C31", "#1E93FF", "#88D8F1", "#FFDC00", "#FF851B", "#921231", "#4FCC30", "#A356D6"]

# Marker the launcher leaves in an attempt directory (the workspace's parent)
# so the collector knows which run the files belong to.
RUN_MARKER = "mongoarc_run.json"

_HEX = "0123456789abcdef"


def load_env(path: str | os.PathLike | None = None) -> None:
    """Load KEY=VALUE lines from the repo's .env without overriding the environment."""
    p = Path(path) if path else REPO / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def mongo_uri() -> str:
    load_env()
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (put it in .env; see .env.example)")
    return uri


_client: MongoClient | None = None


def get_db(uri: str | None = None, name: str | None = None) -> Database:
    """The project database. One client per process."""
    global _client
    if _client is None or uri:
        _client = MongoClient(uri or mongo_uri(), serverSelectionTimeoutMS=15000, appname="mongoarc")
    return _client[name or os.environ.get("MONGO_DB", "mongoarc")]


def ensure_indexes(db: Database) -> None:
    db[RUNS].create_index([("started_at", DESCENDING)])
    db[RUNS].create_index([("generation", ASCENDING)])
    db[ACTIONS].create_index([("run_id", ASCENDING), ("n", ASCENDING)], unique=True)
    db[ACTIONS].create_index([("run_id", ASCENDING), ("seq", ASCENDING)])
    db[EVENTS].create_index([("run_id", ASCENDING), ("seq", ASCENDING)], unique=True)
    db[EVENTS].create_index([("run_id", ASCENDING), ("kind", ASCENDING)])
    db[RULEBOOKS].create_index([("run_id", ASCENDING), ("version", ASCENDING)], unique=True)
    db[LESSONS].create_index([("text", TEXT), ("tags", TEXT)])
    db[LESSONS].create_index([("status", ASCENDING)])
    db[GENERATIONS].create_index([("status", ASCENDING)])
    db[NOTES].create_index([("text", TEXT), ("path", TEXT)])
    db[RULEBOOKS].create_index([("verified.rule", TEXT), ("refuted.rule", TEXT), ("open_questions", TEXT)])
    db[NOTES].create_index([("run_id", ASCENDING)])


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def pack_grid(grid: Iterable[Iterable[int]]) -> str:
    """A 64x64 grid of palette indices as 4096 hex digits, row-major."""
    return "".join(_HEX[int(v) & 15] for row in grid for v in row)


def unpack_grid(text: str, width: int = 64) -> list[list[int]]:
    cells = [int(c, 16) for c in text]
    return [cells[i:i + width] for i in range(0, len(cells), width)]


def jsonable(doc: Any) -> Any:
    """Make a MongoDB document safe for json.dumps (datetimes, ObjectIds)."""
    if isinstance(doc, dict):
        return {k: jsonable(v) for k, v in doc.items()}
    if isinstance(doc, (list, tuple)):
        return [jsonable(v) for v in doc]
    if isinstance(doc, _dt.datetime):
        if doc.tzinfo is None:
            doc = doc.replace(tzinfo=_dt.timezone.utc)
        return doc.isoformat()
    if isinstance(doc, (str, int, float, bool)) or doc is None:
        return doc
    return str(doc)


def next_seq(db: Database, run_id: str, count: int = 1) -> int:
    """Reserve `count` consecutive timeline positions for a run; returns the first.

    Atomic, so the collector and the launcher can both write to one run's
    timeline without colliding.
    """
    doc = db[RUNS].find_one_and_update(
        {"_id": run_id}, {"$inc": {"last_seq": count}}, upsert=True, return_document=True,
        projection={"last_seq": 1})
    return int(doc["last_seq"]) - count + 1


def ensure_vector_index(db: Database, wait_s: float = 0.0) -> str:
    """Create the notes' automated-embedding vector index if it is missing. Returns its status.

    'ready' once Atlas reports it queryable, 'building' while it is not, and
    'unsupported' on a server without Atlas Vector Search (a plain local mongod),
    where memory search falls back to the text index.
    """
    import time
    from pymongo.errors import PyMongoError
    from pymongo.operations import SearchIndexModel
    coll = db[NOTES]
    try:
        if NOTES not in db.list_collection_names():
            db.create_collection(NOTES)
        have = {i["name"]: i for i in coll.list_search_indexes()}
        if VECTOR_INDEX not in have:
            coll.create_search_index(SearchIndexModel(name=VECTOR_INDEX, type="vectorSearch", definition={"fields": [
                {"type": "autoEmbed", "modality": "text", "path": "text", "model": VECTOR_MODEL},
                {"type": "filter", "path": "run_id"}]}))
        deadline = time.time() + wait_s
        while True:
            idx = next((i for i in coll.list_search_indexes() if i["name"] == VECTOR_INDEX), {})
            if idx.get("queryable"):
                return "ready"
            if time.time() >= deadline:
                return "building"
            time.sleep(3)
    except PyMongoError:
        return "unsupported"
