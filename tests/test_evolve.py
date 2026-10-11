"""Evaluate and the heuristic distiller, against a throwaway database on the local dev MongoDB.

Never touches MONGODB_URI: the server is MONGOARC_TEST_URI (default: the local replica set).
"""
import os
import uuid

import pytest
from pymongo import MongoClient

from mongoarc import db as D
from mongoarc.evolve import genome
from mongoarc.evolve.distill import apply_proposal, distill, gather_evidence, parse_json_object
from mongoarc.evolve.evaluate import build_metrics, credit, evaluate_run, level_counts, update_fitness
from mongoarc.evolve.coach import harness_event
from mongoarc.evolve.loop import select, unusable

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


def seed_run(db, run_id, gen=0, spec=SPEC, baseline=(2, 10, 6), lessons=(), status="finished", game="zz09-maze"):
    db[D.RUNS].insert_one({"_id": run_id, "game_id": game, "out_dir": "/nonexistent", "generation": gen,
                           "lessons_used": list(lessons), "baseline_actions": list(baseline), "win_levels": 3,
                           "status": status, "started_at": D.now(), "last_seq": len(spec)})
    db[D.ACTIONS].insert_many(rows(run_id, spec))


def test_evaluate_run_matches_reference(db):
    seed_run(db, "r1")
    m = evaluate_run(db, "r1")
    assert [l["actions"] for l in m["levels"]] == [4, 5, 2]
    assert m["efficiency"] == 0.5 and db[D.RUNS].find_one({"_id": "r1"})["metrics"] == m
    genome.ensure_gen0(db)
    assert update_fitness(db, 0) == {"runs": ["r1"], "mean_efficiency": 0.5, "mean_completion": 0.6667}


def test_clean_lesson():
    assert genome.clean_lesson("Push blocks onto targets to clear a level.", "zz09-maze")
    assert genome.clean_lesson("In zz09 the door opens.", "zz09-maze") is None
    assert genome.clean_lesson("In ab12 the door opens.", "zz09-maze") is None      # any game-shaped id
    assert genome.clean_lesson("The 7fbdac44 game has ice.", "sb26-7fbdac44") is None   # the id's hex suffix
    # coordinates are rejected, never rewritten: a blanked-out cell still describes one level's layout
    for text in ("Click x=12 first.", "The exit sits at (12, 3).", "Click [12,3] first.", "The target is at 12,3.",
                 "Click cell 12 3 to open.", "Go to row 3 then column 5.", "ACTION6 at 32 32 opens the door."):
        assert genome.clean_lesson(text, "zz09-maze") is None, text
    for text in ("Try each of the 4 actions once, then compare frames.", "The 64x64 board can hide a 3x3 pattern.",
                 "Coordinates are (x, y) with the origin at the top-left."):
        assert genome.clean_lesson(text, "zz09-maze") == text


def test_addendum_shape():
    lesson = {"_id": "L0001", "text": "Walls block movement.", "kind": "mechanic_prior", "status": "active",
              "stats": {"injected": 2}}
    # `tools` is reserved and always empty (SCHEMA.md); a stale entry must not reach the prompt
    text = genome.render_addendum({"_id": 4, "tools": [{"name": "bfs", "code": "", "why": "path search"}]}, [lesson])
    assert "## Carried lessons (harness generation 4)" in text and "prior to test" in text
    assert "earlier games" in text and "L0001" in text and "toolkit" not in text and "bfs" not in text
    # the agent can only use the memory it is told about: recall, notes/ for prose, scripts/ for code
    assert "python3 memory.py recall" in text and "`notes/`" in text and "`scripts/`" in text
    assert "Network access beyond the game API is permitted" in text
    assert "Network access" in genome.render_addendum({"_id": 0}, [])
    bare = genome.render_addendum({"_id": 4}, [lesson], memory=False)
    assert "memory.py" not in bare and "L0001" in bare and "Network access" in bare
    retired = genome.render_addendum({"_id": 4}, [{**lesson, "status": "retired"}])
    assert "L0001" not in retired and "No lessons are carried yet" in retired


def test_heuristic_distill(db):
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    db[D.RULEBOOKS].insert_one({"_id": "r1:1", "run_id": "r1", "version": 1, "open_questions": [],
                                "verified": [{"rule": "Walls block movement", "level": 0},
                                             {"rule": "walls block movement.", "level": 1},     # duplicate
                                             {"rule": "the key is at (3, 4) in zz09", "level": 1}],
                                "refuted": [{"rule": "Colour decides direction", "level": 1}]})
    g1 = distill(db, "r1", kind="heuristic")
    assert g1["_id"] == 1 and g1["parent"] == 0 and g1["status"] == "active" and g1["created_from_run"] == "r1"
    assert db[D.GENERATIONS].find_one({"_id": 0})["status"] == "superseded"
    assert db[D.GENERATIONS].count_documents({"status": "active"}) == 1
    lessons = genome.lessons_of(db, g1)
    assert g1["diff"]["added_lessons"] == [l["_id"] for l in lessons] and lessons[0]["_id"] == "L0001"
    texts = " ".join(l["text"] for l in lessons)
    assert "Walls block movement" in texts and "Colour decides direction" in texts
    assert "zz09" not in texts and "(3, 4)" not in texts and texts.count("alls block movement") == 1
    assert lessons[0]["evidence"][0] == {"run_id": "r1", "game_id": "zz09-maze", "level": 0,
                                         "rule": "Walls block movement"}
    assert {l["kind"] for l in lessons} >= {"mechanic_prior", "pitfall"}
    assert "## Carried lessons (harness generation 1)" in g1["addendum"] and lessons[0]["text"] in g1["addendum"]
    assert g1["diff"]["knob_changes"]["coach_stuck_factor"] == [3.0, 2.5]             # efficiency 0.5 < 0.6

    # a second run under generation 1 carrying those lessons: credit, and no duplicate lessons
    seed_run(db, "r2", gen=1, lessons=g1["lessons"], status="won")
    db[D.RULEBOOKS].insert_one({"_id": "r2:1", "run_id": "r2", "version": 1, "open_questions": [],
                                "verified": [{"rule": "Walls block movement", "level": 0}], "refuted": []})
    evaluate_run(db, "r2")
    assert credit(db, "r2") == g1["lessons"] and credit(db, "r2") == []                # once per run
    s = db[D.LESSONS].find_one({"_id": "L0001"})["stats"]
    assert s == {"runs": ["r2"], "injected": 1, "efficiency_sum": 0.5, "wins": 1, "recalled": 0}
    g2 = distill(db, "r2", kind="heuristic")
    new = [l["text"] for l in db[D.LESSONS].find({"_id": {"$in": g2["diff"]["added_lessons"]}})]
    assert g2["parent"] == 1 and not any("Walls block" in t for t in new)
    assert set(g1["lessons"]) <= set(g2["lessons"])


def test_apply_proposal_validates(db):
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    ev = gather_evidence(db, "r1")
    g = apply_proposal(db, ev, {
        "add_lessons": [{"text": "On zz09-maze go left.", "kind": "strategy"},
                        {"text": "Test each action once before planning.", "kind": "nonsense", "tags": ["probe"],
                         "evidence": [{"level": 1, "rule": "probe"}]}],
        "retire_lessons": [{"id": "L9999", "reason": "unknown id"}],
        "knob_changes": {"budget_multiple": 99, "bogus": 1}, "rationale": "x"}, {"model": "m", "kind": "claude"})
    assert len(g["diff"]["added_lessons"]) == 1 and g["diff"]["retired_lessons"] == []
    l = db[D.LESSONS].find_one({"_id": g["diff"]["added_lessons"][0]})
    assert l["kind"] == "strategy" and l["created_gen"] == 1 and l["evidence"][0]["run_id"] == "r1"
    assert g["diff"]["knob_changes"] == {"budget_multiple": [5.0, 8.0]} and "rejected" in g["rationale"]
    assert genome.launch_settings(g["knobs"])["env"] == {"CCARC3_MAX_NUDGES": "2"}


WORSE = [("RESET", 0, False)] + [("ACTION1", 0, False)] * 9 + [("ACTION1", 1, False)]


def test_select_reverts_worse_child(db):
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    evaluate_run(db, "r1"); update_fitness(db, 0)
    distill(db, "r1", kind="heuristic")
    seed_run(db, "r2", gen=1, spec=WORSE)
    evaluate_run(db, "r2"); update_fitness(db, 1)
    out = select(db)
    assert out and out["reverted"] == 1 and out["reactivated"] == 0 and out["games"] == ["zz09-maze"]
    assert genome.active(db)["_id"] == 0 and db[D.GENERATIONS].find_one({"_id": 1})["status"] == "reverted"
    g2 = distill(db, "r2", kind="heuristic")
    assert g2["_id"] == 2 and g2["parent"] == 0 and db[D.GENERATIONS].count_documents({"status": "active"}) == 1


def test_revert_undoes_lesson_changes(db):
    """What a reverted generation added is retired; what it retired or reworded comes back with the parent."""
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    evaluate_run(db, "r1"); update_fitness(db, 0)
    ev = gather_evidence(db, "r1")
    g1 = apply_proposal(db, ev, {"add_lessons": [{"text": "Walls block movement.", "kind": "mechanic_prior"},
                                                 {"text": "Keys open doors of their colour.", "kind": "mechanic_prior"}]},
                        {"model": "m", "cost_usd": None, "kind": "claude"})
    keep, reword = g1["lessons"]
    seed_run(db, "r2", gen=1)
    evaluate_run(db, "r2"); update_fitness(db, 1)
    assert select(db) is None                                                        # same score: no revert
    g2 = apply_proposal(db, gather_evidence(db, "r2"), {
        "add_lessons": [{"text": "Probe unfamiliar objects from a safe position.", "kind": "pitfall"}],
        "retire_lessons": [{"id": keep, "reason": "misled"}],
        "revise_lessons": [{"id": reword, "text": "A key opens the door that shares its colour."}],
        "knob_changes": {"max_nudges": 5}}, {"model": "m", "cost_usd": None, "kind": "claude"})
    new = g2["diff"]["added_lessons"][0]
    assert db[D.LESSONS].find_one({"_id": keep})["status"] == "retired" and g2["lessons"] == [reword, new]
    seed_run(db, "r3", gen=2, spec=WORSE)
    evaluate_run(db, "r3"); update_fitness(db, 2)
    out = select(db)
    assert out and out["reverted"] == 2 and out["reactivated"] == 1
    active = genome.active(db)
    assert active["_id"] == 1 and db[D.GENERATIONS].count_documents({"status": "active"}) == 1
    # the parent carries exactly what it carried before, word for word
    assert [l["status"] for l in genome.lessons_of(db, active)] == ["active", "active"]
    assert db[D.LESSONS].find_one({"_id": keep})["retired_reason"] is None
    assert db[D.LESSONS].find_one({"_id": reword})["text"] == "Keys open doors of their colour."
    assert all(l["text"] in active["addendum"] for l in genome.lessons_of(db, active))
    # the reverted generation's own lesson is retired with the reason, so no query for active lessons finds it
    gone = db[D.LESSONS].find_one({"_id": new})
    assert gone["status"] == "retired" and "reverted" in gone["retired_reason"] and gone["retired_gen"] == 2
    assert new not in [l["_id"] for l in db[D.LESSONS].find({"status": "active"})]
    # ...and the next generation neither carries it nor gets it back under a new id
    g3 = apply_proposal(db, gather_evidence(db, "r3"), {"add_lessons": [
        {"text": "Probe unfamiliar objects from a safe position.", "kind": "pitfall"}]},
        {"model": "m", "cost_usd": None, "kind": "claude"})
    assert g3["parent"] == 1 and g3["lessons"] == [keep, reword] and g3["diff"]["added_lessons"] == []
    assert "repeats" in g3["rationale"] and g3["knobs"]["max_nudges"] == 2           # the knob change went too


def test_select_needs_a_game_in_common(db):
    """A harder next game is not evidence against the generation that played it."""
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    evaluate_run(db, "r1"); update_fitness(db, 0)
    distill(db, "r1", kind="heuristic")
    seed_run(db, "r2", gen=1, spec=WORSE, game="yy08-ice")
    evaluate_run(db, "r2"); update_fitness(db, 1)
    assert db[D.GENERATIONS].find_one({"_id": 1})["fitness"]["mean_efficiency"] < 0.2
    assert select(db) is None and genome.active(db)["_id"] == 1
    seed_run(db, "r3", gen=1, spec=WORSE)                                           # now on the parent's game
    evaluate_run(db, "r3"); update_fitness(db, 1)
    assert select(db)["games"] == ["zz09-maze"] and genome.active(db)["_id"] == 0


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


def test_proposals_and_rule_books_of_any_shape(db):
    """Nothing a model or a hand-edited rules.json can contain may raise: distilling must not stop the loop."""
    genome.ensure_gen0(db)
    seed_run(db, "r1")
    ev = gather_evidence(db, "r1")
    meta = {"model": "m", "cost_usd": None, "kind": "claude"}
    for bad in ({"add_lessons": "a string", "knob_changes": ["budget_multiple", 3]},
                {"add_lessons": {"text": "x"}, "retire_lessons": "L0001", "revise_lessons": 3},
                {"add_lessons": [{"text": None, "kind": ["x"], "tags": "t", "evidence": "e"}, 5, None,
                                 {"text": {"a": 1}}]},
                {"retire_lessons": [{"id": ["L0001"]}, "L0001"], "revise_lessons": [{"id": None, "text": 5}],
                 "knob_changes": {"max_nudges": "3", "budget_multiple": None, "coach_stuck_factor": float("nan")}},
                {"rationale": {"why": "a dict"}, "promote_tools": [{"name": "x", "path": "notes/x.py"}]}, []):
        g = apply_proposal(db, ev, bad, meta)
        assert g["status"] == "active" and g["diff"]["added_lessons"] == [] and g["diff"]["knob_changes"] == {}
        assert g["tools"] == [] and g["diff"]["added_tools"] == []                   # reserved, always empty
    assert genome.active(db)["knobs"] == genome.DEFAULT_KNOBS
    for i, book in enumerate(({"verified": ["Doors need a key"], "refuted": "none", "open_questions": [1, None]},
                              {"verified": {"rule": "x"}, "refuted": [None, 7], "open_questions": "UNTESTED"},
                              {"verified": [{"rule": ["a", "b"]}, {"note": "no rule"}], "open_questions": None})):
        db[D.RULEBOOKS].insert_one({"_id": f"r1:{i + 1}", "run_id": "r1", "version": i + 1, **book})
        db[D.EVENTS].insert_one({"_id": f"r1:t{i}", "run_id": "r1", "seq": 100 + i, "kind": "text", "text": None})
        g = distill(db, "r1", kind="heuristic")
        texts = [db[D.LESSONS].find_one({"_id": l})["text"] for l in g["diff"]["added_lessons"]]
        assert all("['a'" not in t for t in texts)
        if i == 0:
            assert "Held in an earlier game: Doors need a key." in texts              # a bare string is a rule


def test_notes_are_evidence(db, tmp_path):
    """The agent's prose under notes/ reaches the distiller; code and images do not."""
    seed_run(db, "r1")
    ws = tmp_path / "zz09-maze"
    (ws / "notes" / "deep").mkdir(parents=True)
    (ws / "scripts").mkdir()
    (ws / "notes" / "mechanics.md").write_text("Pushing a block into a wall does nothing.", encoding="utf-8")
    (ws / "notes" / "deep" / "plan.txt").write_text("See scripts/bfs.py", encoding="utf-8")
    (ws / "notes" / "probe.py").write_text("print(1)", encoding="utf-8")
    (ws / "notes" / "now.png").write_bytes(b"\x89PNG\x00\xff")
    (ws / "scripts" / "bfs.py").write_text("def bfs(): pass", encoding="utf-8")
    db[D.RUNS].update_one({"_id": "r1"}, {"$set": {"out_dir": str(tmp_path)}})
    genome.ensure_gen0(db)
    notes = gather_evidence(db, "r1")["agent_notes"]
    assert [n["path"] for n in notes] == ["notes/deep/plan.txt", "notes/mechanics.md"]
    assert notes[1]["text"] == "Pushing a block into a wall does nothing."
    # with the directory gone, the mirrored `notes` bank is used
    db[D.RUNS].update_one({"_id": "r1"}, {"$set": {"out_dir": "/nonexistent"}})
    db[D.NOTES].insert_one({"_id": "r1:notes/a.md", "run_id": "r1", "path": "notes/a.md", "text": "From the bank."})
    assert gather_evidence(db, "r1")["agent_notes"] == [{"path": "notes/a.md", "text": "From the bank."}]


def test_launcher_keeps_the_database_from_the_agent(db, tmp_path):
    """Run ids are claimed atomically, and the child gets its memory URL but no database settings."""
    from mongoarc import launch
    fields = {"game_id": "zz09-maze", "status": "running"}
    assert launch.claim_run_id(db, "g0-zz09-maze-T", fields) == "g0-zz09-maze-T"
    assert launch.claim_run_id(db, "g0-zz09-maze-T", fields) == "g0-zz09-maze-T-2"
    assert launch.claim_run_id(db, "g0-zz09-maze-T", fields) == "g0-zz09-maze-T-3"
    doc = db[D.RUNS].find_one({"_id": "g0-zz09-maze-T-2"})
    assert doc["metrics"] is None and doc["n_actions"] == 0 and doc["last_seq"] == 0
    env = launch.child_env({"MONGODB_URI": "mongodb://u:p@h", "MONGO_DB": "x", "MONGOARC_TEST_URI": "y",
                            "ARC_API_KEY": "k", "PATH": "/bin"}, "g0-zz09-maze-T", "http://127.0.0.1:1234")
    assert env == {"ARC_API_KEY": "k", "PATH": "/bin", "MONGOARC_RUN_ID": "g0-zz09-maze-T",
                   "MONGOARC_MEMORY_URL": "http://127.0.0.1:1234"}
    assert "MONGOARC_MEMORY_URL" not in launch.child_env({"MONGOARC_MEMORY_URL": "stale"}, "r", None)
    (tmp_path / "CLAUDE.md").write_text("doctrine\n", encoding="utf-8")
    launch.apply_generation(tmp_path, {"_id": 1, "tools": [{"name": "old", "code": "x"}]}, "ADDENDUM", memory_tool=True)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "doctrine\nADDENDUM"
    assert (tmp_path / "scripts").is_dir() and "recall" in (tmp_path / "memory.py").read_text(encoding="utf-8")
    assert not (tmp_path / "notes").exists()                                         # nothing is carried over


def test_parse_json_object():
    assert parse_json_object('Here:\n```json\n{"a": {"b": 1}}\n```\ndone') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        parse_json_object("no json")
