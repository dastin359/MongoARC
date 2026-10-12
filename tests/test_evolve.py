"""The launcher, the level watcher and the scoring, against a throwaway database on the local dev MongoDB.

Never touches MONGODB_URI: the server is MONGOARC_TEST_URI (default: the local replica set).
"""
import ast
import json
import os
import uuid

import pytest
from pymongo import MongoClient

from mongoarc import db as D
from mongoarc.evolve.coach import harness_event
from mongoarc.evolve.evaluate import build_metrics, evaluate_run, level_curve, level_counts
from mongoarc.evolve.levels import LevelWatcher, nudge_text
from mongoarc.evolve.loop import status, unusable

URI = os.environ.get("MONGOARC_TEST_URI", "mongodb://127.0.0.1:27117/?directConnection=true")


@pytest.fixture()
def db():
    client = MongoClient(URI, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except Exception:  # noqa: BLE001
        pytest.skip("no local MongoDB")
    name = f"mongoarc_test_c_{uuid.uuid4().hex[:8]}"
    d = client[name]
    D.ensure_indexes(d)
    yield d
    client.drop_database(name)


def rows(run_id, spec):
    """spec: (action, level_after, full_reset) per ledger row."""
    out, prev = [], None
    for n, (action, level, full) in enumerate(spec):
        out.append({"_id": f"{run_id}:{n}", "run_id": run_id, "n": n, "i": n, "seq": n + 1, "level": level,
                    "action": action, "params": {}, "state": "NOT_FINISHED", "full_reset": full,
                    "available_actions": [], "frames": [], "level_cleared": prev is not None and level > prev,
                    "new_play": full or (prev is not None and level < prev), "death": False, "wasted": False})
        prev = level
    return out


# Play 1: RESET, 3 actions on level 0 (the third clears it), 2 on level 1, then a full reset.
# Play 2: 4 actions on level 0 (clears), 5 on level 1 (clears), 2 on level 2 (not cleared).
SPEC = ([("RESET", 0, False)] + [("ACTION1", 0, False)] * 2 + [("ACTION1", 1, False)] + [("ACTION2", 1, False)] * 2
        + [("RESET", 0, True)]
        + [("ACTION1", 0, False)] * 3 + [("ACTION1", 1, False)]
        + [("ACTION2", 1, False)] * 4 + [("ACTION2", 2, False)]
        + [("ACTION3", 2, False)] * 2)


def test_level_counts_last_play_only():
    counts, cleared = level_counts(rows("r", SPEC))
    assert counts == {0: 4, 1: 5, 2: 2} and cleared == 2
    # without the reset, the first play alone
    counts, cleared = level_counts(rows("r", SPEC[:6]))
    assert counts == {0: 3, 1: 2} and cleared == 1


def test_build_metrics_efficiency():
    m = build_metrics({0: 4, 1: 5, 2: 2}, 2, [2, 10, 6], 3)
    assert [l["efficiency"] for l in m["levels"]] == [0.5, 1.0, 0.0]
    assert [l["cleared"] for l in m["levels"]] == [True, True, False]
    assert m["efficiency"] == 0.5 and m["completion"] == round(2 / 3, 4)
    assert m["actions_total"] == 11 and m["baseline_total"] == 18 and m["levels_cleared"] == 2


def seed_run(db, run_id, spec=SPEC, baseline=(2, 10, 6), status="finished", game="zz09-maze", out_dir="/nonexistent"):
    db[D.RUNS].insert_one({"_id": run_id, "game_id": game, "out_dir": str(out_dir), "baseline_actions": list(baseline),
                           "win_levels": 3, "status": status, "started_at": D.now(), "last_seq": len(spec)})
    db[D.ACTIONS].insert_many(rows(run_id, spec))


def test_evaluate_run_matches_reference(db):
    seed_run(db, "r1")
    m = evaluate_run(db, "r1")
    assert [l["actions"] for l in m["levels"]] == [4, 5, 2]
    assert m["efficiency"] == 0.5 and db[D.RUNS].find_one({"_id": "r1"})["metrics"] == m


def test_level_curve_and_status(db):
    """Per-level action counts come from the ledger: no generation is read or written."""
    seed_run(db, "r1")
    assert level_curve(db, "r1") == [{"level": 0, "actions": 4, "baseline": 2, "efficiency": 0.5},
                                     {"level": 1, "actions": 5, "baseline": 10, "efficiency": 1.0}]
    assert level_curve(db, "nope") == []
    db[D.NOTES].insert_one({"_id": "r1:mem/1", "run_id": "r1", "title": "t", "text": "x"})
    text = status(db)
    assert "run r1  game zz09-maze  finished" in text and "levels 2/3  actions 11  efficiency 0.500  notes 1" in text
    assert "L0:4/2*=0.50  L1:5/10*=1.00  L2:2/6" in text
    assert "generation" not in text and "lesson" not in text
    assert status(db, "nope") == "no runs"
    assert db[D.GENERATIONS].count_documents({}) == 0 and db[D.LESSONS].count_documents({}) == 0


def test_unusable_runs(db):
    """Failed launches, interrupted runs and runs with no action are not evidence."""
    seed_run(db, "ok")
    assert unusable(db, "ok") is None
    db[D.RUNS].update_one({"_id": "ok"}, {"$set": {"result": {"error": "solver stopped ... gave up", "gave_up": True}}})
    assert unusable(db, "ok") is None                                                # giving up is a result
    db[D.RUNS].update_one({"_id": "ok"}, {"$set": {"result": {"error": "solver killed by signal 9"}}})
    assert "interrupted" in unusable(db, "ok")
    seed_run(db, "empty", spec=[("RESET", 0, False)])
    assert unusable(db, "empty") == "no action was played"
    seed_run(db, "boom")
    harness_event(db, "boom", "run_ended", "lost", launch_error="RuntimeError: refusing to launch")
    assert "the launch failed" in unusable(db, "boom")
    seed_run(db, "hung", status="running")
    assert "did not finish" in unusable(db, "hung")


# The opening of a workspace CLAUDE.md as CCARC writes it (session._workspace_preamble), then a newline, then
# its doctrine (session._workspace_claude_md).
PREAMBLE = "You are playing an unfamiliar video game through an API. Working out its rules\n**is** the task.\n"
SAVE, RECALL = 'python3 memory.py save "<title>" "<text>"', 'python3 memory.py recall "<question>"'


def test_launcher_keeps_the_database_from_the_agent(db):
    """Run ids are claimed atomically; the child gets its memory URL and no database settings; no generation."""
    from mongoarc import launch
    fields = {"game_id": "zz09-maze", "status": "running"}
    assert launch.claim_run_id(db, "zz09-maze-T", fields) == "zz09-maze-T"
    assert launch.claim_run_id(db, "zz09-maze-T", fields) == "zz09-maze-T-2"
    doc = db[D.RUNS].find_one({"_id": "zz09-maze-T-2"})
    assert doc["metrics"] is None and doc["n_actions"] == 0 and doc["last_seq"] == 0
    assert not {"generation", "gen_k", "lessons_used"} & set(doc)
    # a memory run: the memory URL, and CCARC's level gate and its own level-cleared message switched off
    env = launch.child_env({"MONGODB_URI": "mongodb://u:p@h", "MONGO_DB": "x", "MONGOARC_TEST_URI": "y",
                            "ARC_API_KEY": "k", "PATH": "/bin"}, "zz09-maze-T", "http://127.0.0.1:1234")
    assert env == {"ARC_API_KEY": "k", "PATH": "/bin", "MONGOARC_RUN_ID": "zz09-maze-T",
                   "MONGOARC_MEMORY_URL": "http://127.0.0.1:1234", "CCARC3_LEVEL_GATE": "0",
                   "CCARC3_LEVEL_ANNOUNCE": "0"}
    # a control run: no memory URL, and both left at CCARC's default (on) whatever the launcher inherited
    ctl = launch.child_env({"MONGOARC_MEMORY_URL": "stale", "CCARC3_LEVEL_GATE": "0", "CCARC3_LEVEL_ANNOUNCE": "0"},
                           "r", None)
    assert ctl == {"MONGOARC_RUN_ID": "r"}
    assert {"CCARC3_LEVEL_GATE", "CCARC3_LEVEL_ANNOUNCE"} <= set(launch.SHOWN_ENV)
    # old command lines still parse; what they asked for is ignored
    args = launch.build_parser().parse_args(["--game", "g", "--out-dir", "d", "--gen", "7", "--distiller", "heuristic",
                                             "--distill-model", "m", "--coach-interval", "2"])
    assert (args.gen, args.distiller, args.distill_model) == (7, "heuristic", "m")
    src = (D.REPO / "mongoarc" / "launch.py").read_text(encoding="utf-8")
    assert "genome" not in src and "distill_level" not in src and "Coach" not in src


def test_memory_run_claude_md_is_preamble_plus_doctrine(tmp_path, capsys):
    """A memory run reads CCARC's preamble, then mongoARC's doctrine, and nothing else."""
    from mongoarc import launch
    theirs = launch.CCARC_DOCTRINE.read_text(encoding="utf-8")
    ours = launch.DOCTRINE.read_text(encoding="utf-8")
    built = PREAMBLE + "\n" + theirs                                                # what CCARC puts in the workspace
    assert SAVE in ours and RECALL in ours                                           # the commands the level message uses
    ws = tmp_path / "memory-run"
    ws.mkdir()
    (ws / "CLAUDE.md").write_bytes(built.encode("utf-8"))
    launch.prepare_workspace(ws, memory_tool=True)
    raw = (ws / "CLAUDE.md").read_bytes()
    assert raw == (PREAMBLE + "\n" + ours).encode("utf-8")                          # exactly, to the last byte
    text = raw.decode("utf-8")
    assert theirs not in text and "<!--" not in text and "mongoarc:lessons" not in text
    assert "Lessons from the levels" not in text and "harness generation" not in text
    assert launch.doctrine_in(text) == "mongoarc" and launch.doctrine_in(built) == "ccarc"
    assert sorted(p.name for p in ws.iterdir()) == ["CLAUDE.md", "memory.py", "scratch", "scripts"]
    assert (ws / "memory.py").read_bytes() == launch.MEMORY_TOOL.read_bytes()
    # idempotent: again on the same file, and again after CCARC has rebuilt the file (a resume)
    launch.prepare_workspace(ws, memory_tool=True)
    assert (ws / "CLAUDE.md").read_bytes() == raw
    (ws / "CLAUDE.md").write_bytes(built.encode("utf-8"))
    launch.prepare_workspace(ws, memory_tool=True)
    assert (ws / "CLAUDE.md").read_bytes() == raw
    assert launch.swap_doctrine(built) == (PREAMBLE + "\n" + ours, "swapped")
    assert launch.swap_doctrine(text) == (text, "already")
    assert "WARNING" not in capsys.readouterr().out
    # a CCARC whose doctrine is not the text we know: never guess. Its file stays as it is, and that is said
    other = PREAMBLE + "\n" + theirs[:200] + "one changed word " + theirs[200:]
    assert launch.swap_doctrine(other) == (other, "not-found")
    (ws / "CLAUDE.md").write_bytes(other.encode("utf-8"))
    launch.prepare_workspace(ws, memory_tool=True)
    assert (ws / "CLAUDE.md").read_bytes() == other.encode("utf-8")
    out = capsys.readouterr().out
    assert out.count("WARNING: the mongoARC doctrine was NOT put into") == 1 and "not-found" in out


def test_control_run_keeps_the_ccarc_workspace(tmp_path, capsys):
    """A control run: CCARC's doctrine, CCARC's session.py, no memory.py, nothing added."""
    from mongoarc import launch
    built = PREAMBLE + "\n" + launch.CCARC_DOCTRINE.read_text(encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_bytes(built.encode("utf-8"))
    (tmp_path / "session.py").write_text(session_template(), encoding="utf-8")
    launch.prepare_workspace(tmp_path, memory_tool=False)
    assert (tmp_path / "CLAUDE.md").read_bytes() == built.encode("utf-8")
    assert (tmp_path / "session.py").read_text(encoding="utf-8") == session_template()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["CLAUDE.md", "session.py"]
    assert launch.doctrine_in(built) == "ccarc" and capsys.readouterr().out == ""


def session_template() -> str:
    """The workspace session.py as CCARC writes it (its SESSION_TEMPLATE, read without importing CCARC)."""
    src = (D.REPO / "CCARC" / "src" / "ccarc" / "arc_3" / "session.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "SESSION_TEMPLATE")
    return ast.literal_eval(node.value).replace("{game_id}", "zz09-maze")


def test_session_docstring_patch(tmp_path, capsys):
    """The agent of a memory run is not told about a gate; no code line of session.py changes."""
    from mongoarc import launch
    src = session_template()
    assert "    from session import client, gate, arc\n" in src and "``gate.acknowledge(...)``" in src
    new, outcome = launch.patch_session_doc(src)
    assert outcome == "patched"
    end = new.index('"""', 3)
    doc = new[:end]
    assert "    from session import client, arc\n" in doc and "gate.acknowledge" not in doc
    assert "client, gate" not in doc and "The gate holds" not in doc
    assert doc.endswith("Every action is recorded to trace.jsonl automatically.\n")
    assert new[end:] == src[src.index('"""', 3):]                                  # the code, byte for byte
    assert "LevelGate" in new[end:]                                                  # the module still defines gate
    assert launch.patch_session_doc(new) == (new, "already")                         # twice is the same as once
    odd = src.replace("until you call ``gate.acknowledge(...)``", "until ``gate.acknowledge(...)`` is called")
    assert launch.patch_session_doc(odd) == (odd, "not-found")                       # half a match: untouched
    assert launch.patch_session_doc("import os\n") == ("import os\n", "not-found")
    # in a workspace: patched for a memory run, and a template we do not know is left alone with a warning
    (tmp_path / "CLAUDE.md").write_text(PREAMBLE + "\n" + launch.CCARC_DOCTRINE.read_text(encoding="utf-8"),
                                        encoding="utf-8")
    (tmp_path / "session.py").write_text(src, encoding="utf-8")
    launch.prepare_workspace(tmp_path, memory_tool=True)
    assert (tmp_path / "session.py").read_text(encoding="utf-8") == new and capsys.readouterr().out == ""
    (tmp_path / "session.py").write_text(odd, encoding="utf-8")
    launch.prepare_workspace(tmp_path, memory_tool=True)
    assert (tmp_path / "session.py").read_text(encoding="utf-8") == odd
    assert "still mentions the level gate" in capsys.readouterr().out


def state(ws, level, st="NOT_FINISHED", win=3, actions=0):
    body = {"level": level, "state": st, "actions_used": actions}
    if win:
        body["win_levels"] = win
    (ws / "trace.state.json").write_text(json.dumps(body), encoding="utf-8")


def test_nudge_text():
    assert nudge_text(1, 3) == ("Level 1 cleared. You are on level 2 of 3.\n"
                                f"Save what that level taught you: {SAVE} (update or delete any note it proved wrong).\n"
                                f"Then, before your first action on this level, search your memory: {RECALL}.")
    assert nudge_text(2, 3).startswith("Level 2 cleared. You are on level 3 of 3.\n")
    assert nudge_text(2, None).startswith("Level 2 cleared. You are on level 3.\n")  # total unknown: no "of N"
    assert nudge_text(2, 0).split("\n")[1:] == nudge_text(1, 3).split("\n")[1:]
    assert nudge_text(3, 3) is None and nudge_text(4, 3) is None                     # nothing after the final level
    assert nudge_text(0, 3) is None
    assert "gate" not in nudge_text(1, 3) and "rules.json" not in nudge_text(1, 3)


def test_level_watcher(db, tmp_path):
    """One message and one memory_nudge event per level boundary while the game is on; none after the last level."""
    seed_run(db, "r1", out_dir=tmp_path)
    ws = tmp_path / "zz09-maze"
    ws.mkdir()
    said = []
    w = LevelWatcher(db, "r1", tmp_path, ws, [2, 10, 6], say=lambda t: said.append(t) or True)
    assert w.seen == 0 and w.poll() is None                                          # no state file yet
    state(ws, 0)
    assert w.poll() is None and said == []
    state(ws, 1, actions=4)
    assert w.poll() == nudge_text(1, 3) and w.poll() is None                         # once per boundary
    state(ws, 2, actions=9)
    assert w.poll() == nudge_text(2, 3)
    assert said == [nudge_text(1, 3), nudge_text(2, 3)]
    state(ws, 3, "WIN", actions=15)
    assert w.poll() is None and len(said) == 2                                       # the final level: nothing
    ev = list(db[D.EVENTS].find({"run_id": "r1", "what": "memory_nudge"}).sort("seq", 1))
    assert [(e["level"], e["text"], e["delivered"], e["action_n"], e["kind"]) for e in ev] == [
        (1, said[0], True, 3, "harness"), (2, said[1], True, 8, "harness")]
    assert db[D.GENERATIONS].count_documents({}) == 0 and db[D.LESSONS].count_documents({}) == 0
    assert not (ws / "CLAUDE.md").exists()                                           # the watcher writes no file
    # total unknown: the baseline's length is used, and with neither the "of N" is left out
    state(ws, 1, win=None)
    assert LevelWatcher(db, "r1", tmp_path, ws, [], say=lambda t: True).seen == 1    # a resume: not said twice
    (ws / "trace.state.json").unlink()
    bare = LevelWatcher(db, "r1", tmp_path, ws, [], say=lambda t: said.append(t) or True)
    state(ws, 1, win=None)
    assert bare.poll().startswith("Level 1 cleared. You are on level 2.\n")
    # the game ended on the boundary (GAME_OVER), or the launcher is stopping: nothing is sent
    state(ws, 2, "GAME_OVER")
    assert bare.poll() is None
    # a failing outbox never raises: the event says the message was not delivered
    bad = LevelWatcher(db, "r1", tmp_path, ws, [2, 10, 6, 4], say=lambda t: 1 / 0)
    state(ws, 3, win=4)
    assert bad.poll() == nudge_text(3, 4)
    assert db[D.EVENTS].find_one({"run_id": "r1", "what": "memory_nudge", "level": 3})["delivered"] is False


def test_level_watcher_thread_stops_cleanly(db, tmp_path):
    seed_run(db, "r1", out_dir=tmp_path)
    ws = tmp_path / "zz09-maze"
    ws.mkdir()
    said = []
    w = LevelWatcher(db, "r1", tmp_path, ws, [2, 10, 6], interval=0.05, say=lambda t: said.append(t) or True)
    w.start()
    state(ws, 1)
    for _ in range(100):
        if said:
            break
        w.stop_event.wait(0.05)
    assert w.stop() is True and not w.thread.is_alive() and said == [nudge_text(1, 3)]
    assert LevelWatcher(db, "r1", tmp_path, ws, [2, 10, 6]).stop() is True          # stopped without being started
