"""The agent's memory tool: memory.py -> loopback gateway -> MongoDB, and the file mirror beside it.

They need the local MongoDB and use a throwaway database. MONGOARC_TEST_URI overrides the
server; MONGODB_URI is never read, so a production URI in the environment cannot be hit by
accident. The gateway is the real HTTP server (memory_gateway.serve(0, db)) and the tool is
run as the agent runs it: a subprocess with MONGOARC_MEMORY_URL and MONGOARC_RUN_ID set.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pymongo
import pytest
from pymongo.errors import PyMongoError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mongoarc import db as D  # noqa: E402
from mongoarc import memory_gateway as G  # noqa: E402
from mongoarc.notes_sync import NotesSync  # noqa: E402

URI = os.environ.get("MONGOARC_TEST_URI", "mongodb://127.0.0.1:27117/?directConnection=true")
TOOL = REPO / "mongoarc" / "agent_tools" / "memory.py"
A, B = "g1-zz09-maze-A", "g1-yy08-ice-B"


@pytest.fixture()
def db():
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError:
        pytest.skip("local MongoDB is not reachable")
    name = f"mongoarc_dev_mem_test_{uuid.uuid4().hex[:10]}"
    D.ensure_indexes(client[name])
    for rid, game, level in ((A, "zz09-maze", 2), (B, "yy08-ice", 0)):
        client[name][D.RUNS].insert_one({"_id": rid, "game_id": game, "generation": f"{rid}:g1", "level": level,
                                         "n_actions": 7, "last_seq": 0, "status": "running"})
    yield client[name]
    client.drop_database(name)
    client.close()


@pytest.fixture()
def url(db):
    server = G.serve(0, db)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def http(url, path, body=None):
    """(status, JSON) of one gateway call; a POST when there is a body."""
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url + path, data=data), timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def tool(url, run, *args, stdin=None):
    """Run memory.py as the agent does. Its environment holds no database setting at all."""
    env = {"PATH": os.environ.get("PATH", ""), "MONGOARC_MEMORY_URL": url, "MONGOARC_RUN_ID": run}
    feed = {"stdin": subprocess.DEVNULL} if stdin is None else {"input": stdin}
    return subprocess.run([sys.executable, str(TOOL), *args], env=env, text=True, capture_output=True, timeout=30,
                          **feed)


def writes(db, run):
    return [(e["op"], e["note"]) for e in db[D.EVENTS].find({"run_id": run, "kind": "memory_write"}).sort("seq", 1)]


def test_note_document_and_functions(db):
    """save / update / delete write the documented shape, one event each, numbered per run."""
    nid = G.save(db, A, "  mechanics \n of doors ", "A yellow key opens the exit. See scripts/bfs.py and probe.py.")
    assert nid == f"{A}:mem/1"
    doc = db[D.NOTES].find_one({"_id": nid})
    assert doc["created_at"] == doc["updated_at"] and doc.pop("created_at") and doc.pop("updated_at")
    assert doc == {"_id": f"{A}:mem/1", "run_id": A, "game_id": "zz09-maze", "generation": f"{A}:g1", "level": 2,
                   "path": "mem/1", "title": "mechanics of doors", "source": "tool", "version": 1,
                   "text": "A yellow key opens the exit. See scripts/bfs.py and probe.py.",
                   "refs": ["scripts/bfs.py", "probe.py"]}
    assert G.save(db, A, "second", "x") == f"{A}:mem/2" and G.save(db, B, "other game", "y") == f"{B}:mem/1"
    ev = db[D.EVENTS].find_one({"run_id": A, "kind": "memory_write"}, sort=[("seq", 1)])
    assert (ev["op"], ev["note"], ev["title"], ev["level"], ev["action_n"]) == ("save", nid, "mechanics of doors", 2, 6)
    assert ev["text"] == 'agent saved note N1 "mechanics of doors": A yellow key opens the exit. See scripts/bfs.py and probe.py.'

    # update: in place, version + 1, the replaced text kept, the last five only; "$..." stays literal text
    assert G.update(db, A, "N1", "$text is now v2, run.sh") == 2
    doc = db[D.NOTES].find_one({"_id": nid})
    assert (doc["text"], doc["version"], doc["refs"], doc["title"]) == ("$text is now v2, run.sh", 2, ["run.sh"],
                                                                        "mechanics of doors")
    assert doc["updated_at"] >= doc["created_at"]
    assert [(r["version"], r["text"]) for r in doc["revisions"]] == [
        (1, "A yellow key opens the exit. See scripts/bfs.py and probe.py.")]
    assert doc["revisions"][0]["at"] == doc["created_at"]
    for i in range(3, 9):
        assert G.update(db, A, nid if i % 2 else "n1", f"text v{i}") == i       # full _id or short, any case
    doc = db[D.NOTES].find_one({"_id": nid})
    assert doc["version"] == 8 and doc["text"] == "text v8"
    assert [(r["version"], r["text"]) for r in doc["revisions"]] == [(i, f"text v{i}") for i in range(3, 8)]

    assert G.delete(db, A, "N2") == f"{A}:mem/2" and db[D.NOTES].find_one({"_id": f"{A}:mem/2"}) is None
    assert G.save(db, A, "third", "z") == f"{A}:mem/3"                          # a number is never reused
    assert writes(db, A) == [("save", f"{A}:mem/1"), ("save", f"{A}:mem/2")] + [("update", nid)] * 7 + [
        ("delete", f"{A}:mem/2"), ("save", f"{A}:mem/3")]
    assert writes(db, B) == [("save", f"{B}:mem/1")]

    # refusals write nothing and log nothing
    before = db[D.EVENTS].count_documents({}), db[D.NOTES].count_documents({}), db[D.RUNS].count_documents({})
    for call in (lambda: G.save(db, "no-such-run", "t", "x"), lambda: G.save(db, "", "t", "x"),
                 lambda: G.save(db, A, "", "x"), lambda: G.save(db, A, "t", "  \n "),
                 lambda: G.update(db, A, "N1", ""), lambda: G.update(db, A, "N99", "x"),
                 lambda: G.delete(db, A, "N99"), lambda: G.delete(db, A, ""), lambda: G.delete(db, "", "N1")):
        with pytest.raises(G.Refused):
            call()
    assert before == (db[D.EVENTS].count_documents({}), db[D.NOTES].count_documents({}),
                      db[D.RUNS].count_documents({}))
    assert len(G.save(db, A, "long", "x" * 30000)) and len(db[D.NOTES].find_one({"_id": f"{A}:mem/4"})["text"]) == 20000


def test_ids_are_allocated_without_races(db):
    ids: list[str] = []
    threads = [threading.Thread(target=lambda i=i: ids.append(G.save(db, A, f"t{i}", f"text {i}"))) for i in range(24)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert sorted(ids, key=lambda s: int(s.rsplit("/", 1)[1])) == [f"{A}:mem/{n}" for n in range(1, 25)]
    assert db[D.NOTES].count_documents({"run_id": A}) == 24


def test_http_round_trip(db, url):
    """save / recall / list / update / delete through the real HTTP gateway. Memory is notes, and only notes.

    A plain local mongod has no Atlas Vector Search, so recall answers through its
    text-index fallback here ("engine": "text"); on Atlas the same call is a
    $vectorSearch with automated embedding.
    """
    assert http(url, "/health") == (200, {"ok": True})
    code, out = http(url, "/save", {"run": A, "title": "ice physics", "text": "Blocks slide until a wall stops them."})
    assert (code, out) == (200, {"id": f"{A}:mem/1", "short": "N1", "version": 1, "cut": False})

    code, out = http(url, f"/recall?run={A}&q=blocks+slide")                    # found by its text
    assert code == 200 and out["engine"] == "text" and out["stored"] == 1
    assert [(n["id"], n["title"], n["version"], n["level"], n["text"]) for n in out["notes"]] == [
        ("N1", "ice physics", 1, 2, "Blocks slide until a wall stops them.")]
    assert set(out) == {"notes", "engine", "stored"}                            # no rules, no lessons: notes only
    code, out = http(url, f"/recall?run={A}&q=physics")                         # found by its title
    assert [n["id"] for n in out["notes"]] == ["N1"]
    code, out = http(url, f"/recall?run={A}&q=zzzz")                            # nothing matches, but one is stored
    assert out["notes"] == [] and out["stored"] == 1
    assert http(url, f"/recall?run={A}&q=")[1]["engine"] == "recent"            # no question: the newest notes

    code, out = http(url, "/update", {"run": A, "id": "N1", "text": "Blocks slide until a wall or another block stops them."})
    assert (code, out["short"], out["version"]) == (200, "N1", 2)
    doc = db[D.NOTES].find_one({"_id": f"{A}:mem/1"})
    assert doc["version"] == 2 and doc["revisions"][0]["text"] == "Blocks slide until a wall stops them."
    assert http(url, f"/list?run={A}")[1] == {"notes": [{"id": "N1", "title": "ice physics", "version": 2, "path": "mem/1",
                                                         "refs": [], "text": "Blocks slide until a wall or another block stops them."}]}
    assert http(url, "/delete", {"run": A, "id": "N1"}) == (200, {"id": f"{A}:mem/1", "short": "N1"})
    assert db[D.NOTES].count_documents({}) == 0 and http(url, f"/list?run={A}")[1] == {"notes": []}
    assert writes(db, A) == [("save", f"{A}:mem/1"), ("update", f"{A}:mem/1"), ("delete", f"{A}:mem/1")]
    recalls = list(db[D.EVENTS].find({"run_id": A, "kind": "recall"}).sort("seq", 1))
    assert [e.get("engine") for e in recalls] == ["text", "text", "text", "recent", "list", "list"]
    assert recalls[0]["query"] == "blocks slide" and recalls[0]["notes"] == [f"{A}:mem/1"]
    code, out = http(url, "/save", {"run": A, "title": "", "text": "x"})
    assert code == 400 and "title" in out["error"]


def test_a_run_cannot_reach_another_runs_note(db, url):
    """Run B cannot see, update or delete run A's note, by short id or by full _id, in process, over HTTP or by CLI."""
    nid = G.save(db, A, "secret of the maze", "The purple lever opens the vault.")
    kept = db[D.NOTES].find_one({"_id": nid})
    events = db[D.EVENTS].count_documents({"kind": "memory_write"})

    assert G.list_memory(db, B)["notes"] == [] and http(url, f"/list?run={B}")[1]["notes"] == []
    for q in ("purple lever vault", "secret maze", ""):
        assert G.recall(db, B, q)["notes"] == [] and http(url, f"/recall?run={B}&q={q.replace(' ', '+')}")[1]["notes"] == []
    for ref in (nid, "N1", "1", "mem/1"):
        with pytest.raises(G.Refused, match="no note"):
            G.update(db, B, ref, "overwritten by another run")
        with pytest.raises(G.Refused, match="no note"):
            G.delete(db, B, ref)
        code, out = http(url, "/update", {"run": B, "id": ref, "text": "overwritten by another run"})
        assert code == 400 and "no note" in out["error"]
        code, out = http(url, "/delete", {"run": B, "id": ref})
        assert code == 400 and "no note" in out["error"]
        for args in (("update", ref, "overwritten by another run"), ("delete", ref)):
            p = tool(url, B, *args)
            assert p.returncode == 1 and "no note" in p.stdout and len(p.stdout.splitlines()) == 1
    listed = tool(url, B, "list")
    assert listed.returncode == 0 and "purple" not in listed.stdout and "secret" not in listed.stdout
    assert "purple" not in tool(url, B, "recall", "purple", "lever").stdout

    assert db[D.NOTES].find_one({"_id": nid}) == kept                           # untouched, byte for byte
    assert db[D.NOTES].count_documents({}) == 1
    assert db[D.EVENTS].count_documents({"kind": "memory_write"}) == events     # a refusal is not a write
    # B's own N1 is a different document
    assert G.save(db, B, "mine", "Ice is slippery.") == f"{B}:mem/1" and G.update(db, B, "N1", "Ice is very slippery.") == 2
    assert db[D.NOTES].find_one({"_id": nid}) == kept


def test_cli_round_trip(db, url):
    """The tool as the agent runs it: a subprocess that knows only the gateway URL and its run id."""
    p = tool(url, A, "recall", "anything", "saved?")
    assert p.returncode == 0 and "nothing saved yet" in p.stdout
    p = tool(url, A, "save", "hazards", "Red bars are lethal on contact.")
    assert (p.returncode, p.stdout.strip()) == (0, "saved N1")
    p = tool(url, A, "save", "level 2 mechanics", stdin="Blue blocks slide until they hit a wall.\nscripts/bfs.py finds the path.\n")
    assert (p.returncode, p.stdout.strip()) == (0, "saved N2")
    assert db[D.NOTES].find_one({"_id": f"{A}:mem/2"})["refs"] == ["scripts/bfs.py"]

    p = tool(url, A, "recall", "lethal", "bars")
    assert p.returncode == 0 and 'NOTE N1 "hazards"' in p.stdout and "Red bars are lethal on contact." in p.stdout
    assert "1 of 2 note(s), keyword match" in p.stdout
    p = tool(url, A, "recall", "zzzz")
    assert p.returncode == 0 and "no note matched: 2 stored" in p.stdout

    p = tool(url, A, "update", "N1", "Red bars are lethal; a death costs the actions spent on the level.")
    assert (p.returncode, p.stdout.strip()) == (0, "updated N1 (version 2)")
    p = tool(url, A, "list")
    assert p.returncode == 0 and p.stdout.startswith("NOTES: 2") and 'NOTE N1 "hazards" (version 2' in p.stdout
    assert 'NOTE N2 "level 2 mechanics"' in p.stdout and "RULES" not in p.stdout and "LESSONS" not in p.stdout
    p = tool(url, A, "delete", "N2")
    assert (p.returncode, p.stdout.strip()) == (0, "deleted N2")
    assert tool(url, A, "list").stdout.startswith("NOTES: 1")
    p = tool(url, A, "delete", "N9")
    assert p.returncode == 1 and "no note" in p.stdout
    p = tool(url, A, "save", "untitled")
    assert p.returncode != 0 and "not saved" in p.stdout
    assert tool(url, A, "--help").returncode == 0 and "semantic" in tool(url, A, "--help").stdout
    p = tool("http://127.0.0.1:9", A, "list")                                   # nothing listening
    assert p.returncode == 1 and "memory unavailable" in p.stdout
    assert writes(db, A) == [("save", f"{A}:mem/1"), ("save", f"{A}:mem/2"), ("update", f"{A}:mem/1"), ("delete", f"{A}:mem/2")]


def test_file_mirror_never_touches_tool_notes(db, tmp_path):
    """A note saved through the tool survives the notes/ mirror: empty notes/, no notes/, a restart, a real deletion."""
    nid = G.save(db, A, "kept in the database", "Nothing on disk backs this note. scripts/solve.py")
    kept = db[D.NOTES].find_one({"_id": nid})

    empty = tmp_path / "empty" / "zz09-maze"
    (empty / "notes").mkdir(parents=True)
    (empty / "scripts").mkdir()
    (empty / "scripts" / "solve.py").write_text("print(1)", encoding="utf-8")
    bare = tmp_path / "bare" / "zz09-maze"
    bare.mkdir(parents=True)
    for ws in (empty, bare):
        for _ in range(2):                                     # a fresh process, then the steady state
            sync = NotesSync(db, settle=0)
            assert sync.sync(A, "zz09-maze", 1, ws) == 0 and sync.sync(A, "zz09-maze", 1, ws) == 0
            assert db[D.NOTES].find_one({"_id": nid}) == kept
    assert db[D.NOTES].count_documents({}) == 1

    # a file under notes/ is still mirrored and still deleted with its file; the tool's note stays through both
    sync = NotesSync(db, settle=0)
    (empty / "notes" / "old.md").write_text("An old habit.", encoding="utf-8")
    assert sync.sync(A, "zz09-maze", 1, empty) == 1
    assert {d["_id"] for d in db[D.NOTES].find({})} == {nid, f"{A}:notes/old.md"}
    assert G.list_memory(db, A)["notes"][1] == {"id": None, "title": None, "version": 1, "path": "notes/old.md",
                                                "refs": [], "text": "An old habit."}
    with pytest.raises(G.Refused, match="is a file"):          # the mirror owns that one: the tool leaves it alone
        G.delete(db, A, "notes/old.md")
    assert NotesSync(db, settle=0).sync(A, "zz09-maze", 1, empty) == 0           # restart: nothing rewritten
    (empty / "notes" / "old.md").unlink()
    assert sync.sync(A, "zz09-maze", 1, empty) == 1
    assert list(db[D.NOTES].find({})) == [kept]
    assert G.update(db, A, "N1", "Still here.") == 2 and NotesSync(db, settle=0).sync(A, "zz09-maze", 1, bare) == 0
    assert db[D.NOTES].find_one({"_id": nid})["text"] == "Still here."
