"""Collector tests. They need the local MongoDB and use a throwaway database.

MONGOARC_TEST_URI overrides the server; MONGODB_URI is never read, so a production URI in
the environment cannot be hit by accident.
"""
import collections
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pymongo
import pytest
from pymongo import monitoring
from pymongo.errors import AutoReconnect, PyMongoError

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mongoarc import db as D  # noqa: E402
from mongoarc.collector import Collector, stream_events  # noqa: E402

URI = os.environ.get("MONGOARC_TEST_URI", "mongodb://127.0.0.1:27117/?directConnection=true")
FIXTURE = REPO / "runs" / "fixture1"
WS = FIXTURE / "mk01-toy"
RID = "fixture1-mk01-toy"


@pytest.fixture()
def db():
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError:
        pytest.skip("local MongoDB is not reachable")
    name = f"mongoarc_test_{uuid.uuid4().hex[:10]}"
    yield client[name]
    client.drop_database(name)
    client.close()


def counts(db, rid):
    return tuple(db[c].count_documents({"run_id": rid}) for c in (D.ACTIONS, D.EVENTS, D.RULEBOOKS))


def kinds(db, rid):
    return collections.Counter(e["kind"] for e in db[D.EVENTS].find({"run_id": rid}, {"kind": 1}))


def all_seqs(db, rid):
    return [d["seq"] for c in (D.ACTIONS, D.EVENTS) for d in db[c].find({"run_id": rid}, {"seq": 1})]


def run_doc(db, rid):
    """The run document without the collector's lease, which changes hands on a restart."""
    doc = db[D.RUNS].find_one({"_id": rid})
    for k in ("owner", "owner_at"):
        doc["cursors"].pop(k, None)
    return doc


def restart(c):
    """A collector that exits cleanly and its successor over the same root."""
    c.close()
    return Collector(c.db, c.root)


def timeline(db, rid):
    """The run's merged timeline in seq order: "a<n>" for actions, the kind for events."""
    items = [(a["seq"], f"a{a['n']}") for a in db[D.ACTIONS].find({"run_id": rid})]
    items += [(e["seq"], e["kind"]) for e in db[D.EVENTS].find({"run_id": rid})]
    return [name for _, name in sorted(items)]


def test_fixture_once(db):
    assert Collector(db, FIXTURE).poll_once() == 100          # 53 actions + 46 events + 1 rulebook

    rows = [json.loads(line) for line in (WS / "trace.jsonl").read_text().splitlines()]
    acts = list(db[D.ACTIONS].find({"run_id": RID}).sort("n", 1))
    assert [a["n"] for a in acts] == list(range(53))
    assert [a["_id"] for a in acts] == [f"{RID}:{n}" for n in range(53)]
    cleared = [n for n in range(1, 53) if rows[n]["level"] > rows[n - 1]["level"]]
    assert cleared == [13, 26, 52]
    assert [a["n"] for a in acts if a["level_cleared"]] == cleared
    assert not any(a["new_play"] or a["death"] or a["wasted"] for a in acts)
    for a, r in zip(acts, rows):
        assert (a["i"], a["level"], a["action"], a["state"]) == (r["i"], r["level"], r["action"], r["state"])
        assert [D.unpack_grid(f) for f in a["frames"]] == r["frames"]

    k = kinds(db, RID)
    assert k == {"system": 5, "thinking": 8, "text": 8, "tool_use": 8, "tool_result": 8, "result": 5,
                 "outbox": 3, "rulebook": 1}
    n_outbox = len((FIXTURE / "harness_outbox.jsonl").read_text().splitlines())
    assert k["outbox"] == n_outbox
    tool = db[D.EVENTS].find_one({"run_id": RID, "kind": "tool_use"})
    assert tool["tool"] == "Bash" and tool["command"].startswith("python3 -c") and tool["description"] == "batch 0"
    assert tool["_id"] == f"{RID}:{tool['seq']}"

    seqs = all_seqs(db, RID)
    assert len(seqs) == len(set(seqs)) == 99
    # The stream is sequenced before the trace, so the first tool_use precedes every action.
    assert tool["seq"] < min(a["seq"] for a in acts)

    book = db[D.RULEBOOKS].find_one({"_id": f"{RID}:1"})
    rules = json.loads((WS / "rules.json").read_text())
    assert book["verified"] == rules["verified"] and book["open_questions"] == rules["open_questions"]
    assert book["added"] == {key: rules[key] for key in ("verified", "refuted", "open_questions")}

    run = db[D.RUNS].find_one({"_id": RID})
    assert (run["status"], run["state"], run["level"], run["win_levels"]) == ("won", "WIN", 3, 3)
    assert (run["n_actions"], run["last_seq"], run["level_costs"]) == (53, 99, [13, 13, 26])
    assert (run["turns"], run["cost_usd"], run["deaths"]) == (30, 0.0, 0)
    assert run["result"]["won"] is True and run["ended_at"] is not None
    assert run["game_id"] == "mk01-toy" and run["out_dir"] == str(FIXTURE) and run["generation"] is None
    # A directory ingested after the fact started when it was launched, not when it was found.
    launched = (WS / "meta.json").stat().st_mtime
    assert abs(run["started_at"].replace(tzinfo=D.now().tzinfo).timestamp() - launched) < 1


def test_second_pass_and_restart_write_nothing(db, tmp_path):
    c = Collector(db, FIXTURE)
    assert c.poll_once() == 100
    before = counts(db, RID), run_doc(db, RID)
    assert c.poll_once() == 0
    c = restart(c)                                            # a restarted collector resumes from runs.cursors
    assert c.poll_once() == 0 and all(r.owned for r in c.runs.values())
    c.close()
    (tmp_path / "fixture1").symlink_to(FIXTURE)               # same run found as a child of the watched root
    c = Collector(db, tmp_path)
    assert c.poll_once() == 0 and all(r.owned for r in c.runs.values())
    assert (counts(db, RID), run_doc(db, RID)) == before


def test_cli_once_twice(db):
    env = {**os.environ, "MONGODB_URI": URI, "MONGO_DB": db.name}
    cmd = [sys.executable, "-m", "mongoarc.collector", "--watch", str(FIXTURE), "--once", "-q"]
    first = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, check=True)
    second = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, check=True)
    assert first.stdout.startswith("collector: 100 documents written")
    assert second.stdout.startswith("collector: 0 documents written")
    assert counts(db, RID) == (53, 46, 1)


def test_failed_pass_is_recovered(db, monkeypatch):
    """A pass that dies after inserting events must not leave duplicates behind."""
    real = pymongo.collection.Collection.bulk_write
    calls = []

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise AutoReconnect("simulated network drop")
        return real(self, *a, **kw)

    monkeypatch.setattr(pymongo.collection.Collection, "bulk_write", flaky)
    logged = []
    c = Collector(db, FIXTURE, log=logged.append)
    assert c.poll_once() == 0 and "AutoReconnect" in logged[0]
    assert counts(db, RID) == (0, 46, 0)                      # events landed, the cursor did not
    assert c.poll_once() == 100
    assert counts(db, RID) == (53, 46, 1)
    seqs = all_seqs(db, RID)
    assert len(seqs) == len(set(seqs))


def _attempt(root: Path, name="att1", marker=None):
    out = root / name
    ws = out / "mk01-toy"
    ws.mkdir(parents=True)
    (ws / "session.py").write_text("# stub\n")
    (ws / "meta.json").write_text(json.dumps({"game_id": "mk01-toy", "model": "meta-model", "effort": "high"}))
    if marker:
        (out / D.RUN_MARKER).write_text(json.dumps(marker))
    return out, ws


def _replace(path: Path, text: str):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def test_live_tail(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    stream = (WS / "stream.jsonl").read_bytes().splitlines()
    rid = "g1-live"
    out, ws = _attempt(tmp_path, marker={"run_id": rid, "game_id": "mk01-toy", "generation": 9, "label": "marker"})
    # The launcher created the run first and has already written one harness event.
    t0 = D.now()
    db[D.RUNS].insert_one({"_id": rid, "generation": 1, "lessons_used": ["L0001"], "label": "evolve-1",
                           "agent": "fake", "model": "launch-model", "baseline_actions": [10, 12, 14],
                           "started_at": t0, "status": "running", "last_seq": 0})
    seq = D.next_seq(db, rid)
    db[D.EVENTS].insert_one({"_id": f"{rid}:{seq}", "run_id": rid, "seq": seq, "at": t0, "kind": "harness",
                             "what": "run_started", "action_n": -1, "level": 0})
    c = Collector(db, tmp_path)

    # Trace: a row is consumed only once its newline is written.
    trace = ws / "trace.jsonl"
    half = len(rows[5]) // 2
    trace.write_bytes(b"".join(r + b"\n" for r in rows[:5]) + rows[5][:half])
    assert c.poll_once() == 5
    with trace.open("ab") as f:
        f.write(rows[5][half:] + b"\n")
    assert c.poll_once() == 1
    assert counts(db, rid)[0] == 6

    # Stream: non-JSON lines are skipped; events carry the latest ingested action.
    live = ws / "stream.jsonl"
    live.write_bytes(stream[0] + b"\n" + b"warning: stderr noise\n" + stream[1] + b"\n")
    assert c.poll_once() == 4                                 # system + thinking + text + tool_use
    ev = db[D.EVENTS].find_one({"run_id": rid, "kind": "tool_use"})
    assert (ev["action_n"], ev["level"]) == (5, 0)

    # Restart: nothing is re-read.
    c = restart(c)
    assert c.poll_once() == 0

    # Rotation: the old inode is finished under its new name, the new file starts at 0.
    with live.open("ab") as f:
        f.write(stream[2] + b"\n")
    live.rename(ws / "stream.1.jsonl")
    live.write_bytes(stream[3] + b"\n" + stream[4] + b"\n")
    assert c.poll_once() == 5                                 # tool_result | thinking text tool_use | tool_result
    assert kinds(db, rid) == {"harness": 1, "system": 1, "thinking": 2, "text": 2, "tool_use": 2, "tool_result": 2}
    results = list(db[D.EVENTS].find({"run_id": rid, "kind": "tool_result"}).sort("seq", 1))
    assert [r["tool_use_id"] for r in results] == ["toolu_fake_1_0", "toolu_fake_1_1"]
    c = restart(c)
    assert c.poll_once() == 0

    # Rule book: a version and an event per content change, with the diff.
    a, b = ({"rule": r, "scope": "game", "level": i, "note": ""} for i, r in enumerate(("walls block", "ice slides")))
    _replace(ws / "rules.json", json.dumps({"verified": [a], "refuted": [], "open_questions": ["q0"]}))
    assert c.poll_once() == 2
    _replace(ws / "rules.json", json.dumps({"verified": [a, b], "refuted": [], "open_questions": ["q0"]}))
    c = restart(c)                               # the diff survives a restart
    assert c.poll_once() == 2
    _replace(ws / "rules.json", json.dumps({"verified": [a, b], "refuted": [], "open_questions": ["q0"]}))
    assert c.poll_once() == 0                                 # same content, new inode
    books = list(db[D.RULEBOOKS].find({"run_id": rid}).sort("version", 1))
    assert [bk["version"] for bk in books] == [1, 2]
    assert books[0]["added"] == {"verified": [a], "refuted": [], "open_questions": ["q0"]}
    assert books[1]["added"] == {"verified": [b], "refuted": [], "open_questions": []}
    assert books[1]["verified"] == [a, b] and books[1]["action_n"] == 5
    rb_events = list(db[D.EVENTS].find({"run_id": rid, "kind": "rulebook"}).sort("seq", 1))
    assert [(e["version"], e["added"]["verified"]) for e in rb_events] == [(1, [a]), (2, [b])]

    # Outbox: tailed, and read again from 0 when it is unlinked and recreated.
    box = out / "harness_outbox.jsonl"
    box.write_text('{"at":1.0,"kind":"level_cleared","text":"one"}\n{"at":2.0,"kind":"note","text":"two"}\n')
    assert c.poll_once() == 2
    box.unlink()
    box.write_text('{"at":3.0,"kind":"level_cleared","text":"three"}\n')
    assert c.poll_once() == 1
    texts = [e["text"] for e in db[D.EVENTS].find({"run_id": rid, "kind": "outbox"}).sort("seq", 1)]
    assert texts == ["one", "two", "three"]

    # State file -> run fields; the launcher's fields are left alone.
    _replace(ws / "trace.state.json", json.dumps({"state": "NOT_FINISHED", "level": 1, "win_levels": 3,
                                                  "actions_used": 6, "level_costs": [6], "gate_pending": 1}))
    assert c.poll_once() == 0
    run = db[D.RUNS].find_one({"_id": rid})
    assert (run["status"], run["state"], run["level"], run["win_levels"], run["gate_pending"]) == (
        "running", "NOT_FINISHED", 1, 3, 1)
    assert (run["generation"], run["lessons_used"], run["label"], run["agent"], run["model"]) == (
        1, ["L0001"], "evolve-1", "fake", "launch-model")
    assert run["baseline_actions"] == [10, 12, 14] and run["n_actions"] == 6 and run["effort"] == "high"
    assert abs((run["started_at"].replace(tzinfo=t0.tzinfo) - t0).total_seconds()) < 0.01
    assert run["game_id"] == "mk01-toy" and run["out_dir"] == str(out)

    # A replaced trace is re-read from 0 and upserted by n; rows past its end are dropped.
    _replace(trace, b"".join(r + b"\n" for r in rows[:3]).decode())
    assert c.poll_once() == 3
    acts = list(db[D.ACTIONS].find({"run_id": rid}).sort("n", 1))
    assert [x["n"] for x in acts] == [0, 1, 2]

    # WIN -> won. Sequence numbers never collide with the launcher's.
    _replace(ws / "trace.state.json", json.dumps({"state": "WIN", "level": 3, "win_levels": 3}))
    assert c.poll_once() == 0
    run = db[D.RUNS].find_one({"_id": rid})
    assert run["status"] == "won" and run["ended_at"] is not None
    seqs = all_seqs(db, rid)
    assert len(seqs) == len(set(seqs)) and max(seqs) == run["last_seq"]
    assert db[D.EVENTS].count_documents({"run_id": rid, "kind": "harness"}) == 1


def test_run_id_without_marker_and_stale(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path, name="attX")
    rid = "attX-mk01-toy"
    (ws / "trace.jsonl").write_bytes(rows[0] + b"\n")
    c = Collector(db, out, stale_after=0.0)                   # watch the attempt directory itself
    assert c.poll_once() == 1
    run = db[D.RUNS].find_one({"_id": rid})
    assert run["status"] == "stopped" and run["generation"] is None and run["model"] == "meta-model"
    with (ws / "trace.jsonl").open("ab") as f:
        f.write(rows[1] + b"\n")
    assert c.poll_once() == 1
    assert db[D.RUNS].find_one({"_id": rid})["status"] == "running"


def test_new_attempt_directory_appears(db, tmp_path):
    c = Collector(db, tmp_path)
    assert c.poll_once() == 0
    out, ws = _attempt(tmp_path, name="late", marker={"run_id": "late-run", "game_id": "mk01-toy", "generation": 2,
                                                      "lessons_used": ["L0003"], "agent": "fake", "label": "e"})
    assert c.poll_once() == 0
    run = db[D.RUNS].find_one({"_id": "late-run"})
    assert (run["generation"], run["lessons_used"], run["agent"], run["status"]) == (2, ["L0003"], "fake", "running")
    (ws / "trace.jsonl").write_bytes((WS / "trace.jsonl").read_bytes().splitlines()[0] + b"\n")
    assert c.poll_once() == 1


def test_stream_events_shapes():
    user = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Level 0 cleared"}]}}
    assert stream_events(user) == [{"kind": "user_msg", "text": "Level 0 cleared"}]
    assert stream_events({"type": "user", "message": {"content": "plain"}}) == [{"kind": "user_msg", "text": "plain"}]
    res = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                    "content": [{"type": "text", "text": "x" * 5000}]}]}}
    (ev,) = stream_events(res)
    assert ev["kind"] == "tool_result" and len(ev["text"]) == 4000 and ev["is_error"] is True
    write = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Write", "input": {"file_path": "notes/a.md", "content": "hi"}}]}}
    (ev,) = stream_events(write)
    assert ev["tool"] == "Write" and json.loads(ev["command"])["file_path"] == "notes/a.md"
    retry = {"type": "system", "subtype": "api_retry", "attempt": 2, "max_retries": 10, "error": "overloaded"}
    assert stream_events(retry) == [{"kind": "system", "subtype": "api_retry", "text": "attempt 2/10: overloaded"}]
    # Telemetry the real CLI streams many times a second is not a timeline event.
    assert stream_events({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5}) == []
    assert stream_events({"type": "rate_limit_event", "rate_limit_info": {}}) == []
    assert stream_events({"type": "system", "subtype": "init", "model": "m", "session_id": "s"}) == [
        {"kind": "system", "subtype": "init", "text": "model=m session=s"}]
    done = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "duration_ms": 9,
            "total_cost_usd": 1.5, "result": "done"}
    assert stream_events(done) == [{"kind": "result", "cost_usd": 1.5, "turns": 6, "duration_ms": 9,
                                    "is_error": False, "text": "done"}]


def _lines(*events):
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def _use(tid, tool="Bash"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": tool, "input": {"command": "python3 -c play", "description": tid}}]}}


def _result(tid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def _append(path: Path, data: bytes):
    with path.open("ab") as f:
        f.write(data)


def test_real_stream_telemetry_is_skipped(db, tmp_path):
    """The real CLI writes hundreds of system/thinking_tokens lines a minute; none reaches the timeline."""
    out, ws = _attempt(tmp_path)
    noise = [{"type": "system", "subtype": "thinking_tokens", "estimated_tokens": i, "uuid": str(i)} for i in range(300)]
    (ws / "stream.jsonl").write_bytes(_lines(
        {"type": "system", "subtype": "init", "model": "claude-opus-5", "session_id": "s1"},
        {"type": "user", "isReplay": True, "timestamp": "2026-10-11T21:00:06.244Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Play the game."}]}},
        *noise[:150], {"type": "rate_limit_event", "rate_limit_info": {}},
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "look", "signature": "x"}]}},
        *noise[150:], _use("toolu_1")))
    c = Collector(db, tmp_path)
    assert c.poll_once() == 4
    assert [e["kind"] for e in db[D.EVENTS].find({"run_id": "att1-mk01-toy"}).sort("seq", 1)] == [
        "system", "user_msg", "thinking", "tool_use"]
    assert c.poll_once() == 0


def test_bad_input_does_not_stop_the_collector(db, tmp_path):
    """Text BSON refuses and rows that are not grids are stored in a safe form, not raised."""
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path, name="odd")
    broken = json.loads(rows[1])
    broken["frames"] = "not a grid"
    (ws / "trace.jsonl").write_bytes(rows[0] + b"\n" + json.dumps(broken).encode() + b"\n" + rows[2] + b"\n")
    # A lone surrogate is legal JSON (an emoji cut in half by output truncation) but not legal UTF-8.
    (ws / "stream.jsonl").write_bytes(
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"cut \\ud83d here"}]}}\n'
        b'{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t","content":12345}]}}\n')
    (ws / "rules.json").write_text('{"verified": [{"rule": "big \\udfff", "level": 99999999999999999999}]}')
    (out / "harness_outbox.jsonl").write_text('{"at": 1.0, "kind": "note", "text": "half \\ud83d"}\n')
    out2, ws2 = _attempt(tmp_path, name="fine")
    (ws2 / "trace.jsonl").write_bytes(rows[0] + b"\n")
    logged = []
    c = Collector(db, tmp_path, log=logged.append)
    assert c.poll_once() == 3 + 4 + 1 + 1                     # odd: 3 rows, 4 events, 1 rule book; fine: 1 row
    assert not [line for line in logged if "Error" in line]
    acts = list(db[D.ACTIONS].find({"run_id": "odd-mk01-toy"}).sort("n", 1))
    assert [(a["n"], a["wasted"], len(a["frames"])) for a in acts] == [(0, False, 1), (1, True, 0), (2, False, 1)]
    text = db[D.EVENTS].find_one({"run_id": "odd-mk01-toy", "kind": "text"})["text"]
    assert text.startswith("cut ") and text.endswith(" here") and "\ud83d" not in text
    assert db[D.EVENTS].find_one({"run_id": "odd-mk01-toy", "kind": "tool_result"})["text"] == ""
    assert db[D.RULEBOOKS].find_one({"_id": "odd-mk01-toy:1"})["verified"][0]["level"] == "99999999999999999999"
    assert db[D.ACTIONS].count_documents({"run_id": "fine-mk01-toy"}) == 1
    assert c.poll_once() == 0


def test_error_in_one_run_does_not_stop_the_others(db, tmp_path, monkeypatch):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    for name in ("a-bad", "b-good"):
        out, ws = _attempt(tmp_path, name=name)
        (ws / "trace.jsonl").write_bytes(rows[0] + b"\n")
    from mongoarc import collector as M
    real = M._Run._action

    def explode(self, n, r, *a):
        if self.id.startswith("a-bad"):
            raise RuntimeError("unexpected")
        return real(self, n, r, *a)

    monkeypatch.setattr(M._Run, "_action", explode)
    logged = []
    c = Collector(db, tmp_path, log=logged.append)
    assert c.poll_once() == 1                                 # the good run is ingested, the bad one is reported
    assert "a-bad: RuntimeError: unexpected" in logged[0]
    assert db[D.ACTIONS].count_documents({"run_id": "b-good-mk01-toy"}) == 1
    c.poll_once()
    c.poll_once()                                             # now backing off: not attempted, not logged again
    assert len([line for line in logged if "RuntimeError" in line]) == 2
    monkeypatch.setattr(M._Run, "_action", real)
    assert c.poll_once(final=True) == 1                       # a final pass retries at once
    assert db[D.ACTIONS].count_documents({"run_id": "a-bad-mk01-toy"}) == 1


def test_rows_are_sequenced_inside_their_tool_call(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path)
    rid = "att1-mk01-toy"
    trace, live = ws / "trace.jsonl", ws / "stream.jsonl"
    c = Collector(db, tmp_path)
    live.write_bytes(_lines(_use("t1")))
    assert c.poll_once() == 1
    # The last rows of a call and its result arrive in one pass: the rows come first.
    trace.write_bytes(rows[0] + b"\n" + rows[1] + b"\n")
    _append(live, _lines(_result("t1")))
    assert c.poll_once() == 3
    assert timeline(db, rid) == ["tool_use", "a0", "a1", "tool_result"]
    res = db[D.EVENTS].find_one({"run_id": rid, "kind": "tool_result"})
    assert (res["action_n"], res["tool_use_id"]) == (1, "t1")
    # A whole call inside one pass: its rows sit between the tool_use and the tool_result.
    _append(live, _lines({"type": "assistant", "message": {"content": [{"type": "text", "text": "next"}]}},
                         _use("t2"), _result("t2")))
    _append(trace, rows[2] + b"\n")
    assert c.poll_once() == 4
    assert timeline(db, rid)[4:] == ["text", "tool_use", "a2", "tool_result"]
    # A result, then a new call, then rows: they may belong to the new call, so they follow its tool_use.
    _append(live, _lines(_use("t3")))
    assert c.poll_once() == 1
    _append(live, _lines(_result("t3"), _use("t4")))
    _append(trace, rows[3] + b"\n")
    assert c.poll_once() == 3
    assert timeline(db, rid)[8:] == ["tool_use", "tool_result", "tool_use", "a3"]
    # A call that is not an action tool does not anchor rows; the open Bash call still does after a restart.
    c = restart(c)
    _append(live, _lines(_use("r1", tool="Read"), _result("r1"), _result("t4")))
    _append(trace, rows[4] + b"\n")
    (out / "harness_outbox.jsonl").write_text('{"at":1.0,"kind":"level_cleared","text":"cleared"}\n')
    assert c.poll_once() == 5
    assert timeline(db, rid)[12:] == ["tool_use", "tool_result", "a4", "outbox", "tool_result"]
    seqs = all_seqs(db, rid)
    assert len(seqs) == len(set(seqs)) and c.poll_once() == 0


def test_two_collectors_on_one_run_do_not_duplicate(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path)
    rid = "att1-mk01-toy"
    said = []
    a, b = Collector(db, tmp_path), Collector(db, tmp_path, log=said.append)
    (ws / "trace.jsonl").write_bytes(rows[0] + b"\n")
    (ws / "stream.jsonl").write_bytes(_lines(_use("t1")))
    assert a.poll_once() == 2 and b.poll_once() == 0
    assert said == [f"{rid}: another collector is writing this run; standing by"]
    for i in (1, 2, 3):
        _append(ws / "trace.jsonl", rows[i] + b"\n")
        _append(ws / "stream.jsonl", _lines(_result(f"t{i}"), _use(f"t{i + 1}")))
        assert b.poll_once(final=True) == 0                   # even when it asks first
        assert a.poll_once() == 3
    assert counts(db, rid) == (4, 7, 0)
    # The writer exits cleanly: the other one continues from the saved cursors.
    a.close()
    _append(ws / "trace.jsonl", rows[4] + b"\n")
    assert b.poll_once(final=True) == 1 and said[-2] == f"{rid}: took over"
    assert counts(db, rid) == (5, 7, 0)
    # The writer is killed: its lease names a process that no longer exists on this host.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    db[D.RUNS].update_one({"_id": rid}, {"$set": {"cursors.owner": f"{socket.gethostname()}:{dead.pid}:feedbeef",
                                                  "cursors.owner_at": D.now()}})
    _append(ws / "trace.jsonl", rows[5] + b"\n")
    assert b.poll_once() == 0 and b.poll_once(final=True) == 1
    # A writer elsewhere that went silent: its lease runs out.
    db[D.RUNS].update_one({"_id": rid}, {"$set": {"cursors.owner": "elsewhere:1:feedbeef", "cursors.owner_at": D.now()}})
    _append(ws / "trace.jsonl", rows[6] + b"\n")
    assert b.poll_once() == 0 and b.poll_once(final=True) == 0
    patient = Collector(db, tmp_path, lease=0.0)
    assert patient.poll_once() == 1
    assert counts(db, rid) == (7, 7, 0)
    seqs = all_seqs(db, rid)
    assert len(seqs) == len(set(seqs))
    assert [x["n"] for x in db[D.ACTIONS].find({"run_id": rid}).sort("n", 1)] == list(range(7))


def test_database_reset_under_a_running_collector(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path)
    rid = "att1-mk01-toy"
    (ws / "trace.jsonl").write_bytes(rows[0] + b"\n" + rows[1] + b"\n")
    c = Collector(db, tmp_path)
    assert c.poll_once() == 2
    for name in (D.RUNS, D.ACTIONS, D.EVENTS):
        db[name].delete_many({})
    _append(ws / "trace.jsonl", rows[2] + b"\n")
    assert c.poll_once() == 0                                 # the write is refused: the run document is gone
    assert c.poll_once(final=True) == 0                       # noticed; the directory is registered again
    assert c.poll_once() == 3
    assert [x["n"] for x in db[D.ACTIONS].find({"run_id": rid}).sort("n", 1)] == [0, 1, 2]


def test_notes_are_mirrored(db, tmp_path):
    out, ws = _attempt(tmp_path, marker={"run_id": "g2-notes", "game_id": "mk01-toy", "generation": 2})
    (ws / "notes").mkdir()
    (ws / "notes" / "mechanics.md").write_text("Walls block movement. See scripts/bfs.py.\n")
    (ws / "notes" / "l0.png").write_bytes(b"\x89PNG\x00")
    (ws / "notes" / "probe.py").write_text("print(1)\n")
    c = Collector(db, tmp_path)
    assert c.poll_once() == 1
    note = db[D.NOTES].find_one({"_id": "g2-notes:notes/mechanics.md"})
    assert (note["run_id"], note["game_id"], note["generation"], note["path"]) == (
        "g2-notes", "mk01-toy", 2, "notes/mechanics.md")
    assert note["text"].startswith("Walls block") and note["refs"] == ["scripts/bfs.py"] and note["version"] == 1
    assert db[D.NOTES].count_documents({}) == 1
    c = restart(c)
    assert c.poll_once() == 0
    assert db[D.NOTES].find_one({"_id": note["_id"]})["version"] == 1


class _Commands(monitoring.CommandListener):
    def __init__(self):
        self.names = []

    def started(self, event):
        self.names.append(event.command_name)

    def succeeded(self, event):
        pass

    def failed(self, event):
        pass


def test_round_trips_per_pass(db, tmp_path):
    rows = (WS / "trace.jsonl").read_bytes().splitlines()
    out, ws = _attempt(tmp_path)
    (ws / "trace.jsonl").write_bytes(rows[0] + b"\n")
    (ws / "stream.jsonl").write_bytes(_lines(_use("t1")))
    log = _Commands()
    client = pymongo.MongoClient(URI, serverSelectionTimeoutMS=2000, event_listeners=[log])
    try:
        c = Collector(client[db.name], tmp_path)
        c.poll_once()
        log.names.clear()
        assert c.poll_once() == 0 and c.poll_once() == 0 and log.names == []       # an idle pass costs nothing
        _append(ws / "trace.jsonl", rows[1] + b"\n" + rows[2] + b"\n")
        _append(ws / "stream.jsonl", _lines(_result("t1"), _use("t2")))
        _replace(ws / "trace.state.json", json.dumps({"state": "NOT_FINISHED", "level": 0, "win_levels": 3}))
        _replace(ws / "rules.json", json.dumps({"verified": [], "refuted": [], "open_questions": ["q"]}))
        assert c.poll_once() == 6                             # 2 rows, 2 stream events, rule book + its event
        # reserve the block, events, actions, rule book, run document
        assert sorted(log.names) == ["findAndModify", "insert", "update", "update", "update"]
    finally:
        client.close()
