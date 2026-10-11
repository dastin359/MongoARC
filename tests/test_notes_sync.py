"""Notes memory bank tests. They need the local MongoDB and use a throwaway database.

MONGOARC_TEST_URI overrides the server; MONGODB_URI is never read, so a production URI in
the environment cannot be hit by accident.
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pymongo
import pytest
from pymongo import monitoring
from pymongo.errors import AutoReconnect, PyMongoError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mongoarc import db as D  # noqa: E402
from mongoarc.notes_sync import MAX_CHARS, NotesSync, extract_refs, is_note_name, script_path  # noqa: E402

URI = os.environ.get("MONGOARC_TEST_URI", "mongodb://127.0.0.1:27117/?directConnection=true")
RID = "g2-mk02-keys-20261011T150000"
GAME = "mk02-keys"


class Commands(monitoring.CommandListener):
    """Every command the client sends, by name."""

    def __init__(self):
        self.names: list[str] = []

    def started(self, event):
        self.names.append(event.command_name)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


@pytest.fixture()
def env():
    """(db, command log) on a throwaway database."""
    log = Commands()
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=2000, event_listeners=[log])
    try:
        client.admin.command("ping")
    except PyMongoError:
        pytest.skip("local MongoDB is not reachable")
    name = f"mongoarc_dev_n_test_{uuid.uuid4().hex[:10]}"
    D.ensure_indexes(client[name])
    log.names.clear()
    yield client[name], log
    client.drop_database(name)
    client.close()


@pytest.fixture()
def ws(tmp_path):
    """<out_dir>/<game_id> with two notes and one script."""
    w = tmp_path / "attempt" / GAME
    (w / "notes").mkdir(parents=True)
    (w / "scripts").mkdir()
    (w / "notes" / "mechanics.md").write_text(
        "# Mechanics\nThe goal stays grey until the key is taken. Path search: scripts/bfs.py\n", encoding="utf-8")
    (w / "notes" / "plan.txt").write_text("level 2: key first, then goal\n", encoding="utf-8")
    (w / "scripts" / "bfs.py").write_text("print('bfs')\n", encoding="utf-8")
    return w


def docs(db):
    return {d["path"]: d for d in db[D.NOTES].find({"run_id": RID})}


def touch_later(path: Path, seconds: float = 5.0) -> None:
    s = path.stat()
    os.utime(path, ns=(s.st_atime_ns, s.st_mtime_ns + int(seconds * 1e9)))


def age(path: Path, seconds: float = 60.0) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---- the sync contract ------------------------------------------------------------------------

def test_first_sync_inserts(env, ws):
    db, _ = env
    assert NotesSync(db).sync(RID, GAME, 2, ws) == 2
    got = docs(db)
    assert sorted(got) == ["notes/mechanics.md", "notes/plan.txt"]
    m = got["notes/mechanics.md"]
    assert m["_id"] == f"{RID}:notes/mechanics.md"
    assert (m["run_id"], m["game_id"], m["generation"], m["version"]) == (RID, GAME, 2, 1)
    assert m["text"] == (ws / "notes" / "mechanics.md").read_text(encoding="utf-8")
    assert m["refs"] == ["scripts/bfs.py"]
    assert m["created_at"] == m["updated_at"]
    assert len(m["sha"]) == 64
    assert set(m) == {"_id", "run_id", "game_id", "generation", "path", "text", "refs", "sha", "version",
                      "created_at", "updated_at"}
    assert got["notes/plan.txt"]["refs"] == []
    # the text index finds it
    assert [d["path"] for d in db[D.NOTES].find({"$text": {"$search": "grey"}})] == ["notes/mechanics.md"]


def test_unchanged_directory_issues_no_command(env, ws):
    db, log = env
    sync = NotesSync(db)
    assert sync.sync(RID, GAME, 2, ws) == 2
    log.names.clear()
    for _ in range(25):
        assert sync.sync(RID, GAME, 2, ws) == 0
    assert log.names == []


def test_edit_updates_in_place(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    before = docs(db)["notes/mechanics.md"]
    time.sleep(0.02)
    (ws / "notes" / "mechanics.md").write_text("# Mechanics\nKey first. See bfs.py and scripts/replay.sh\n",
                                               encoding="utf-8")
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 1
    assert log.names == ["update"]                       # one bulk_write, one command
    got = docs(db)
    after = got["notes/mechanics.md"]
    assert len(got) == 2 and after["_id"] == before["_id"]
    assert after["version"] == 2
    assert after["updated_at"] > before["updated_at"]
    assert after["created_at"] == before["created_at"]
    assert after["text"].startswith("# Mechanics\nKey first.")
    assert after["refs"] == ["bfs.py", "scripts/replay.sh"]
    assert after["sha"] != before["sha"]
    assert got["notes/plan.txt"]["version"] == 1         # the untouched note was not rewritten
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0 and log.names == []


def test_same_size_edit_is_seen(env, ws):
    db, _ = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    p = ws / "notes" / "plan.txt"
    p.write_text("level 3: key first, then goal\n", encoding="utf-8")      # one character differs
    touch_later(p)
    assert sync.sync(RID, GAME, 2, ws) == 1
    assert docs(db)["notes/plan.txt"]["text"].startswith("level 3")
    assert docs(db)["notes/plan.txt"]["version"] == 2


def test_touch_without_change_does_not_bump(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    touch_later(ws / "notes" / "plan.txt")               # mtime moves, bytes do not
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0
    assert log.names == [] and docs(db)["notes/plan.txt"]["version"] == 1


def test_deleted_file_deletes_document(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    (ws / "notes" / "plan.txt").unlink()
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 1
    assert log.names == ["delete"]
    assert sorted(docs(db)) == ["notes/mechanics.md"]
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0 and log.names == []


def test_insert_update_delete_in_one_bulk_write(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    (ws / "notes" / "plan.txt").unlink()
    (ws / "notes" / "mechanics.md").write_text("rewritten\n", encoding="utf-8")
    (ws / "notes" / "sub").mkdir()
    (ws / "notes" / "sub" / "ideas").write_text("no extension, still prose\n", encoding="utf-8")
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 3
    assert sorted(log.names) == ["delete", "update"]     # one unordered bulk_write: one command per op type
    got = docs(db)
    assert sorted(got) == ["notes/mechanics.md", "notes/sub/ideas"]
    assert (got["notes/mechanics.md"]["version"], got["notes/sub/ideas"]["version"]) == (2, 1)


def test_restart_writes_nothing_and_keeps_versions(env, ws):
    db, log = env
    first = NotesSync(db)
    first.sync(RID, GAME, 2, ws)
    (ws / "notes" / "plan.txt").write_text("level 2: key, door, goal\n", encoding="utf-8")
    first.sync(RID, GAME, 2, ws)
    before = docs(db)
    assert {p: d["version"] for p, d in before.items()} == {"notes/mechanics.md": 1, "notes/plan.txt": 2}

    log.names.clear()
    second = NotesSync(db)                               # a new process over the same directory
    assert second.sync(RID, GAME, 2, ws) == 0
    assert log.names == ["find"]                         # it only loaded the stored hashes
    assert second.sync(RID, GAME, 2, ws) == 0
    assert log.names == ["find"]                         # and never asked again
    log.names.clear()
    assert docs(db) == before                            # same versions, same updated_at, same everything


def test_restart_picks_up_what_changed_while_down(env, ws):
    db, _ = env
    NotesSync(db).sync(RID, GAME, 2, ws)
    (ws / "notes" / "plan.txt").unlink()
    (ws / "notes" / "mechanics.md").write_text("edited while the collector was down\n", encoding="utf-8")
    assert NotesSync(db).sync(RID, GAME, 2, ws) == 2
    got = docs(db)
    assert sorted(got) == ["notes/mechanics.md"] and got["notes/mechanics.md"]["version"] == 2


def test_runs_are_independent(env, ws, tmp_path):
    db, _ = env
    other = tmp_path / "attempt2" / "mk03-lava"
    (other / "notes").mkdir(parents=True)
    (other / "notes" / "mechanics.md").write_text("red kills\n", encoding="utf-8")
    sync = NotesSync(db)
    assert sync.sync(RID, GAME, 2, ws) == 2
    assert sync.sync("g2-mk03-lava-x", "mk03-lava", None, other) == 1
    (ws / "notes" / "plan.txt").unlink()
    assert sync.sync(RID, GAME, 2, ws) == 1
    assert sync.sync("g2-mk03-lava-x", "mk03-lava", None, other) == 0
    lava = db[D.NOTES].find_one({"_id": "g2-mk03-lava-x:notes/mechanics.md"})
    assert lava["generation"] is None and lava["game_id"] == "mk03-lava" and lava["version"] == 1
    assert db[D.NOTES].count_documents({}) == 2


# ---- what is and is not a note ----------------------------------------------------------------

def test_only_prose_under_notes_is_pushed(env, ws, tmp_path):
    db, log = env
    n = ws / "notes"
    (n / "board.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    (n / "solver.py").write_text("print(1)\n", encoding="utf-8")
    (n / "state.json").write_text("{}\n", encoding="utf-8")
    (n / "trace.jsonl").write_text('{"i": 0}\n', encoding="utf-8")
    (n / "trace_copy.jsonl").write_text('{"i": 0}\n', encoding="utf-8")
    (n / "tr_0001").write_text('{"i": 0}\n', encoding="utf-8")
    (n / "tr_backup.txt").write_text('{"i": 0}\n', encoding="utf-8")
    (n / "latin1.md").write_bytes("caf\xe9 au lait".encode("latin-1"))         # .md, but not UTF-8
    (n / "blob").write_bytes(b"\x00\x01\x02\x03binary with no extension")
    (n / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (n / "toolkit").mkdir()
    (n / "toolkit" / "pathfind.py").write_text("def bfs(): ...\n", encoding="utf-8")
    (n / "toolkit" / "README").write_text("promoted tools: pathfind.py\n", encoding="utf-8")
    (n / "deep.markdown").write_text("café ✓\n", encoding="utf-8")
    (ws / "README.md").write_text("outside notes/\n", encoding="utf-8")
    (ws / "scripts" / "notes.md").write_text("outside notes/\n", encoding="utf-8")

    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.md").write_text("must never be mirrored\n", encoding="utf-8")
    os.symlink(secret / "key.md", n / "leak.md")                               # file link out of the workspace
    os.symlink(secret, n / "leakdir")                                          # directory link out of it
    os.symlink(n / "plan.txt", n / "alias.txt")                                # link that stays inside

    sync = NotesSync(db)
    assert sync.sync(RID, GAME, 2, ws) == 5
    got = docs(db)
    assert sorted(got) == ["notes/alias.txt", "notes/deep.markdown", "notes/mechanics.md", "notes/plan.txt",
                           "notes/toolkit/README"]
    assert got["notes/deep.markdown"]["text"] == "café ✓\n"
    assert got["notes/toolkit/README"]["refs"] == ["pathfind.py"]
    assert not db[D.NOTES].count_documents({"text": {"$regex": "never be mirrored"}})
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0 and log.names == []                # skipped files are not re-read


def test_notes_directory_linked_out_of_the_workspace_is_ignored(env, tmp_path):
    db, _ = env
    w = tmp_path / "attempt" / GAME
    w.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.md").write_text("not ours\n", encoding="utf-8")
    os.symlink(elsewhere, w / "notes")
    assert NotesSync(db).sync(RID, GAME, 2, w) == 0
    assert docs(db) == {}


def test_text_is_capped(env, ws):
    db, _ = env
    (ws / "notes" / "long.md").write_text("x" * (MAX_CHARS + 500) + " scripts/tail.py", encoding="utf-8")
    NotesSync(db).sync(RID, GAME, 2, ws)
    d = docs(db)["notes/long.md"]
    assert len(d["text"]) == MAX_CHARS
    assert d["refs"] == ["scripts/tail.py"]              # refs come from the whole file


def test_missing_workspace_keeps_memory_and_missing_notes_dir_clears_it(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    moved = ws.with_name("moved")
    ws.rename(moved)                                     # the attempt directory was archived
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0
    assert NotesSync(db).sync(RID, GAME, 2, ws) == 0
    assert log.names == [] and len(docs(db)) == 2
    moved.rename(ws)
    for p in (ws / "notes").iterdir():
        p.unlink()
    (ws / "notes").rmdir()                               # the agent removed its notes
    assert sync.sync(RID, GAME, 2, ws) == 2
    assert docs(db) == {}


def test_note_caught_mid_rewrite_is_not_dropped(env, ws):
    db, log = env
    sync = NotesSync(db)
    sync.sync(RID, GAME, 2, ws)
    p = ws / "notes" / "plan.txt"
    p.write_bytes(b"")                                   # truncated; the content has not landed yet
    (ws / "notes" / "new.md").write_bytes(b"")           # created; nothing written yet
    log.names.clear()
    assert sync.sync(RID, GAME, 2, ws) == 0
    assert log.names == [] and docs(db)["notes/plan.txt"]["version"] == 1
    p.write_text("level 2: rewritten\n", encoding="utf-8")
    (ws / "notes" / "new.md").write_text("now it has content\n", encoding="utf-8")
    assert sync.sync(RID, GAME, 2, ws) == 2
    got = docs(db)
    assert (got["notes/plan.txt"]["version"], got["notes/new.md"]["version"]) == (2, 1)
    # a file that stays empty is a real (empty) note once it has settled
    p.write_bytes(b"")
    age(p)
    assert sync.sync(RID, GAME, 2, ws) == 1
    assert docs(db)["notes/plan.txt"]["text"] == "" and docs(db)["notes/plan.txt"]["version"] == 3
    # and a note that turns into a binary file is dropped once it has settled
    p.write_bytes(b"\xff\xfe\x00\x01")
    assert sync.sync(RID, GAME, 2, ws) == 0 and "notes/plan.txt" in docs(db)
    age(p)
    assert sync.sync(RID, GAME, 2, ws) == 1 and "notes/plan.txt" not in docs(db)


def test_failed_write_is_retried_without_double_bump(env, ws, monkeypatch):
    db, _ = env
    sync = NotesSync(db)
    real = sync.col.bulk_write
    calls = []

    def flaky(ops, **kw):
        calls.append(len(ops))
        if len(calls) == 1:
            real(ops, **kw)                              # it landed, but the reply was lost
            raise AutoReconnect("connection reset")
        return real(ops, **kw)

    monkeypatch.setattr(sync.col, "bulk_write", flaky)
    with pytest.raises(AutoReconnect):
        sync.sync(RID, GAME, 2, ws)
    assert sync.sync(RID, GAME, 2, ws) == 0              # reloaded the hashes: nothing left to write
    assert calls == [2]
    assert {d["version"] for d in docs(db).values()} == {1}


# ---- refs and scripts -------------------------------------------------------------------------

def test_is_note_name():
    for name in ("mechanics.md", "plan.txt", "x.markdown", "README", "IDEAS.MD", "level-2 plan.md"):
        assert is_note_name(name), name
    for name in ("board.png", "solver.py", "state.json", "trace.jsonl", "trace_2.jsonl", "tr_0001", "tr_x.md",
                 ".hidden", ".DS_Store", "notes.md~", "a.sh", "x.csv"):
        assert not is_note_name(name), name


def test_extract_refs():
    text = (
        "Path search lives in scripts/bfs.py (run it as `python3 scripts/bfs.py 3`).\n"
        "Replay with ./scripts/replay.sh, then solver.py; bfs.py again.\n"
        "Also /abs/ws/scripts/tools/grid_diff.py and scripts/levels.json.\n"
        "Not scripts: a.python, e.g. the push, scripts/ alone, x.pyc, README.md.\n"
        "Later: scripts/solver.py."
    )
    assert extract_refs(text) == ["scripts/bfs.py", "scripts/replay.sh", "scripts/solver.py",
                                  "scripts/tools/grid_diff.py", "scripts/levels.json"]
    assert extract_refs("no code here") == []
    assert extract_refs("memory.py recall, then session.py.") == ["memory.py", "session.py"]
    assert extract_refs("see notes/toolkit/pathfind.py") == ["pathfind.py"]


def test_script_path(env, ws, tmp_path):
    db, _ = env
    out = ws.parent
    db[D.RUNS].insert_one({"_id": RID, "game_id": GAME, "out_dir": str(out)})
    NotesSync(db).sync(RID, GAME, 2, ws)
    (ws / "session.py").write_text("# driver\n", encoding="utf-8")
    (ws / "notes" / "toolkit").mkdir()
    (ws / "notes" / "toolkit" / "pathfind.py").write_text("# kit\n", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("# outside\n", encoding="utf-8")
    (out / "sibling.py").write_text("# in the attempt directory, not the workspace\n", encoding="utf-8")
    os.symlink(tmp_path / "outside.py", ws / "scripts" / "link.py")

    bfs = (ws / "scripts" / "bfs.py").resolve()
    note_id = f"{RID}:notes/mechanics.md"
    assert script_path(db, note_id, "scripts/bfs.py") == bfs             # by note id, as stored in refs
    assert script_path(db, RID, "scripts/bfs.py") == bfs                 # by run id
    assert script_path(db, RID, "bfs.py") == bfs                         # bare name: scripts/ first
    assert script_path(db, RID, "./scripts/bfs.py") == bfs
    assert script_path(db, RID, "session.py") == (ws / "session.py").resolve()
    assert script_path(db, RID, "pathfind.py") == (ws / "notes" / "toolkit" / "pathfind.py").resolve()
    for ref in docs(db)["notes/mechanics.md"]["refs"]:
        assert script_path(db, note_id, ref) == bfs

    assert script_path(db, RID, "scripts/missing.py") is None            # not on disk
    assert script_path(db, RID, "scripts") is None                       # a directory
    assert script_path(db, RID, "../sibling.py") is None                 # .. out of the workspace
    assert script_path(db, RID, "scripts/../../sibling.py") is None
    assert script_path(db, RID, "../../outside.py") is None
    assert script_path(db, RID, str(tmp_path / "outside.py")) is None    # absolute path
    assert script_path(db, RID, "scripts/link.py") is None               # symlink out of the workspace
    assert script_path(db, RID, "link.py") is None
    assert script_path(db, RID, ".env") is None                          # hidden files are never served
    assert script_path(db, RID, "") is None
    assert script_path(db, "no-such-run", "scripts/bfs.py") is None
    assert script_path(db, "no-such-run:notes/x.md", "scripts/bfs.py") is None
