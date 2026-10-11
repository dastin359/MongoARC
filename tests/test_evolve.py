"""Per-level evolution inside one game, against a throwaway database on the local dev MongoDB.

Never touches MONGODB_URI: the server is MONGOARC_TEST_URI (default: the local replica set).
"""
import os
import uuid

import pytest
from pymongo import MongoClient

from mongoarc import db as D
import json

from mongoarc import memory_gateway
from mongoarc.evolve import genome
from mongoarc.evolve.coach import Coach, harness_event, pick_lessons
from mongoarc.evolve.distill import (apply_proposal, build_prompt, distill_level, gather_evidence, level_metrics,
                                     parse_json_object)
from mongoarc.evolve.evaluate import build_metrics, evaluate_run, level_curve, level_counts
from mongoarc.evolve.levels import LevelEvolver, write_section
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
    db[D.RUNS].insert_one({"_id": run_id, "game_id": game, "out_dir": str(out_dir), "generation": None,
                           "lessons_used": [], "baseline_actions": list(baseline), "win_levels": 3,
                           "status": status, "started_at": D.now(), "last_seq": len(spec)})
    db[D.ACTIONS].insert_many(rows(run_id, spec))
    return genome.ensure_run_gen0(db, run_id, game)


def book(db, run_id, version, verified=(), refuted=(), open_questions=()):
    db[D.RULEBOOKS].insert_one({"_id": f"{run_id}:{version}", "run_id": run_id, "version": version,
                                "verified": [{"rule": r, "level": l} for r, l in verified],
                                "refuted": [{"rule": r, "level": l} for r, l in refuted],
                                "open_questions": list(open_questions)})


def clear(db, run_id, level, actions, kind="heuristic"):
    """What the level evolver does for one cleared level, without the thread."""
    run = db[D.RUNS].find_one({"_id": run_id})
    m = level_metrics(level, actions, run["baseline_actions"][level])
    db[D.GENERATIONS].update_one({"_id": genome.active(db, run_id)["_id"]}, {"$set": {"played": m}})
    return distill_level(db, run_id, level, m, kind=kind)


def test_evaluate_run_matches_reference(db):
    seed_run(db, "r1")
    m = evaluate_run(db, "r1")
    assert [l["actions"] for l in m["levels"]] == [4, 5, 2]
    assert m["efficiency"] == 0.5 and db[D.RUNS].find_one({"_id": "r1"})["metrics"] == m


def test_level_metrics():
    assert level_metrics(1, 20, 10) == {"level": 1, "actions": 20, "baseline": 10, "efficiency": 0.5}
    assert level_metrics(0, 5, 10)["efficiency"] == 1.0                              # never above 1
    assert level_metrics(0, 5, None)["efficiency"] is None                           # baseline unknown
    assert level_metrics(0, None, 10) == {"level": 0, "actions": None, "baseline": 10, "efficiency": None}


def test_clean_lesson():
    # a lesson is about ONE game now: it should name the game's colours, objects and actions
    for text in ("The colour 9 (blue) block is you; ACTION1 moves it up one cell.",
                 "In this game the green tile is the exit and it only opens after the yellow key is collected.",
                 "Try each of the 4 actions once, then compare frames.", "The 64x64 board can hide a 3x3 pattern."):
        assert genome.clean_lesson(text) == text
    # ...but never one level's layout
    for text in ("The exit sits at (12, 3).", "Click [12,3] first.", "Go to x=12 then press ACTION5.", "", None, 7):
        assert genome.clean_lesson(text) is None, text


def test_addendum_shape():
    lesson = {"_id": "L0001", "text": "Walls block movement.", "kind": "mechanic_prior", "status": "active", "level": 0}
    text = genome.render_addendum({"k": 2}, [lesson])
    assert "## Lessons from the levels you have cleared (harness generation 2)" in text
    assert text.index(genome.START_MARK) < text.index("## Lessons") < text.index("L0001") < text.index(genome.END_MARK)
    assert "THIS game" in text and "after level 0" in text
    # the agent can only use the memory it is told about: recall at every level, notes/ for prose, scripts/ for code
    assert "At the start of every level, before your first action on it" in text
    assert "python3 memory.py recall <words for what you see>" in text
    assert "`notes/mechanics.md`" in text and "`scripts/`" in text
    assert "earlier games" not in text and "OTHER games" not in text                 # nothing crosses games
    assert "Network access beyond the game API is permitted" in text
    empty = genome.render_addendum({"k": 0}, [])
    assert "Lessons will appear here as levels are cleared" in empty and "Network access" in empty
    bare = genome.render_addendum({"k": 2}, [lesson], memory=False)
    assert "memory.py" not in bare and "L0001" in bare and "Network access" in bare
    control = genome.render_addendum({"k": 0}, [], memory=False)                    # the control arm: no promise of lessons
    assert "Lessons" not in control and "memory" not in control.lower() and "Network access" in control
    assert control.count(genome.START_MARK) == 1 and control.count(genome.END_MARK) == 1
    retired = genome.render_addendum({"k": 2}, [{**lesson, "status": "retired"}])
    assert "L0001" not in retired and "Lessons will appear here" in retired


def test_section_replacement_keeps_the_rest_byte_identical(tmp_path):
    """Only the text between the markers changes, however often the section is rewritten."""
    before = "# Doctrine\r\n\n  odd   spacing\twith a tab, no newline at the end of this part \u2014 unicode"
    after = "\n\ntrailing text the agent appended\n\n\n"
    g0 = genome.render_addendum({"k": 0}, [])
    (tmp_path / "CLAUDE.md").write_bytes((before + "\n" + g0 + after).encode("utf-8"))
    lesson = {"_id": "L0001", "text": "Keys open doors.", "kind": "mechanic_prior", "status": "active", "level": 0}
    for k, lessons in ((1, [lesson]), (2, [lesson, {**lesson, "_id": "L0002", "text": "Lava is fatal.", "level": 1}]),
                       (3, [])):
        add = genome.render_addendum({"k": k}, lessons)
        assert write_section(tmp_path, add) is True
        raw = (tmp_path / "CLAUDE.md").read_bytes().decode("utf-8")
        a, b = raw.index(genome.START_MARK), raw.index(genome.END_MARK) + len(genome.END_MARK)
        assert raw[:a].encode() == (before + "\n" + g0[:g0.index(genome.START_MARK)]).encode()
        assert raw[b:].encode() == (g0[g0.index(genome.END_MARK) + len(genome.END_MARK):] + after).encode()
        assert raw[a:b] == genome.section_of(add) and raw.count(genome.START_MARK) == 1
        assert f"(harness generation {k})" in raw[a:b]
        assert write_section(tmp_path, add) is False                                 # same section: file untouched
    assert not [p.name for p in tmp_path.iterdir() if p.name != "CLAUDE.md"]         # no temp file left
    # a file with no section gets one appended, and nothing before it changes
    (tmp_path / "CLAUDE.md").write_text("plain\n", encoding="utf-8")
    write_section(tmp_path, g0)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "plain\n" + g0


def test_per_run_lineage(db):
    """Each run has its own generations and lessons; two runs share nothing."""
    a0, b0 = seed_run(db, "ra"), seed_run(db, "rb", game="yy08-ice")
    assert (a0["k"], a0["level"], a0["parent"], a0["status"], a0["lessons"]) == (0, None, None, "active", [])
    assert a0["_id"] != b0["_id"] and (a0["run_id"], b0["run_id"]) == ("ra", "rb") and b0["game_id"] == "yy08-ice"
    assert a0["level_metrics"] is None and a0["played"] is None
    assert genome.ensure_run_gen0(db, "ra", "zz09-maze")["_id"] == a0["_id"]          # idempotent
    book(db, "ra", 1, verified=[("Walls block movement", 0), ("The blue block is the player", 0)],
         refuted=[("Colour decides direction", 0)], open_questions=["level 0: UNTESTED - the grey tile"])
    a1 = clear(db, "ra", 0, 4)
    assert (a1["k"], a1["level"], a1["parent"], a1["status"], a1["run_id"]) == (1, 0, a0["_id"], "active", "ra")
    assert a1["_id"] == max(a0["_id"], b0["_id"]) + 1                                # max(_id)+1, collection-wide
    assert a1["level_metrics"] == {"level": 0, "actions": 4, "baseline": 2, "efficiency": 0.5} and a1["played"] is None
    assert db[D.GENERATIONS].find_one({"_id": a0["_id"]})["played"] == a1["level_metrics"]
    assert db[D.GENERATIONS].find_one({"_id": a0["_id"]})["status"] == "superseded"
    la = genome.lessons_of(db, a1)
    assert len(la) == 3 and a1["diff"]["added_lessons"] == [l["_id"] for l in la] and la[0]["_id"] == "L0001"
    assert all((l["run_id"], l["game_id"], l["level"], l["created_gen"]) == ("ra", "zz09-maze", 0, a1["_id"])
               for l in la)
    assert la[0]["evidence"] == [{"level": 0, "rule": "Walls block movement"}] and la[0]["stats"] == {"recalled": 0}
    assert "Walls block movement" in a1["addendum"] and "(harness generation 1)" in a1["addendum"]
    run = db[D.RUNS].find_one({"_id": "ra"})
    assert (run["generation"], run["gen_k"], run["lessons_used"]) == (a1["_id"], 1, a1["lessons"])
    # the other run is untouched: still at k=0, no lessons, and its own next generation starts from nothing
    assert genome.active(db, "rb")["_id"] == b0["_id"] and genome.active_lessons(db, "rb") == []
    assert db[D.RUNS].find_one({"_id": "rb"}).get("gen_k") in (None, 0)
    book(db, "rb", 1, verified=[("Walls block movement", 0)])                         # same words, another game
    b1 = clear(db, "rb", 0, 2)
    lb = genome.lessons_of(db, b1)
    assert b1["parent"] == b0["_id"] and b1["k"] == 1 and len(lb) == 1 and lb[0]["run_id"] == "rb"
    assert lb[0]["_id"] not in a1["lessons"] and not set(a1["lessons"]) & set(b1["lessons"])
    assert [g["k"] for g in genome.lineage(db, "ra")] == [0, 1] and [g["k"] for g in genome.lineage(db, "rb")] == [0, 1]
    for rid in ("ra", "rb"):
        assert db[D.GENERATIONS].count_documents({"run_id": rid, "status": "active"}) == 1
    # level 1 of run a: nothing already a lesson is added again, the new rule is, and the curve has two points
    book(db, "ra", 2, verified=[("Walls block movement", 0), ("Keys open the door of their colour", 1)])
    a2 = clear(db, "ra", 1, 5)
    new = [l["text"] for l in db[D.LESSONS].find({"_id": {"$in": a2["diff"]["added_lessons"]}})]
    assert a2["k"] == 2 and a2["parent"] == a1["_id"] and set(a1["lessons"]) <= set(a2["lessons"])
    assert any("Keys open the door" in t for t in new) and not any("Walls block" in t for t in new)
    assert [(c["level"], c["actions"], c["efficiency"], c["k"]) for c in level_curve(db, "ra")] == [
        (0, 4, 0.5, 0), (1, 5, 1.0, 1)]
    text = status(db)
    assert "run ra" in text and "run rb" in text and "L0001" in text


def test_recall_is_run_scoped(db):
    """A search sees its own run's lessons, notes and rules, and nothing of any other run."""
    seed_run(db, "ra"), seed_run(db, "rb", game="yy08-ice")
    assert memory_gateway.recall(db, "ra", "walls key") == {"lessons": [], "notes": [], "rules": []}
    book(db, "ra", 1, verified=[("Walls block movement", 0), ("The yellow key opens the exit", 0)],
         refuted=[("Colour decides direction", 0)])
    db[D.NOTES].insert_one({"_id": "ra:notes/mechanics.md", "run_id": "ra", "game_id": "zz09-maze",
                            "path": "notes/mechanics.md", "text": "The yellow key must be collected before the exit."})
    a1 = clear(db, "ra", 0, 4)
    out = memory_gateway.recall(db, "ra", "yellow key exit")
    assert out["lessons"] and {l["id"] for l in out["lessons"]} <= set(a1["lessons"])
    assert out["lessons"][0]["level"] == 0 and "key" in out["lessons"][0]["text"]
    assert [n["path"] for n in out["notes"]] == ["notes/mechanics.md"]
    assert {"status": "verified", "level": 0, "rule": "The yellow key opens the exit"} in out["rules"]
    # words that match nothing still return the run's newest lessons and its rules
    miss = memory_gateway.recall(db, "ra", "zzzz qqqq")
    assert len(miss["lessons"]) == len(a1["lessons"]) and len(miss["rules"]) == 3 and miss["notes"] == []
    assert {r["status"] for r in miss["rules"]} == {"verified", "refuted"}
    # the second run, same database, same words: nothing
    for q in ("yellow key exit", "walls", ""):
        assert memory_gateway.recall(db, "rb", q) == {"lessons": [], "notes": [], "rules": []}
    # each search is logged once on its own run, with the ids returned, and counted on the lessons
    ev = list(db[D.EVENTS].find({"kind": "recall"}).sort("at", 1))
    assert [e["run_id"] for e in ev] == ["ra", "ra", "ra", "rb", "rb", "rb"]
    assert ev[1]["lessons"] == [l["id"] for l in out["lessons"]] and ev[1]["query"] == "yellow key exit"
    assert ev[3]["lessons"] == [] and "0 lesson(s)" in ev[3]["text"]
    hit = db[D.LESSONS].find_one({"_id": out["lessons"][0]["id"]})
    assert hit["stats"]["recalled"] == 2 and hit["run_id"] == "ra"


def test_apply_proposal_validates(db):
    seed_run(db, "r1")
    ev = gather_evidence(db, "r1", 0, level_metrics(0, 4, 2))
    assert ev["level_cleared"] == 0 and ev["levels_left"] == 2 and ev["lessons"] == []
    g = apply_proposal(db, ev, {
        "add_lessons": [{"text": "The exit is at (3, 4).", "kind": "strategy"},
                        {"text": "In zz09 the blue block is you and ACTION1 moves it up.", "kind": "nonsense",
                         "tags": ["probe"], "evidence": [{"level": 0, "rule": "probe"}]},
                        {"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}],
        "retire_lessons": [{"id": "L9999", "reason": "unknown id"}],
        "knob_changes": {"coach_stuck_factor": 99, "bogus": 1}, "rationale": "x"}, {"model": "m", "kind": "claude"})
    assert len(g["diff"]["added_lessons"]) == 3 and g["diff"]["retired_lessons"] == []   # 3 per level
    l = db[D.LESSONS].find_one({"_id": g["diff"]["added_lessons"][0]})
    assert l["kind"] == "strategy" and l["created_gen"] == g["_id"] and l["text"].startswith("In zz09")   # game named
    assert g["diff"]["knob_changes"] == {"coach_stuck_factor": [3.0, 6.0]}
    assert "rejected" in g["rationale"] and "dropped" in g["rationale"] and g["distiller"]["kind"] == "claude"
    # revise and retire touch only this run's lessons in force; at most MAX_ACTIVE stay in force
    keep, reword, drop = g["lessons"]
    for level in (1, 2):
        ev = gather_evidence(db, "r1", level, level_metrics(level, 9, 10))
        g = apply_proposal(db, ev, {"add_lessons": [{"text": f"lesson {level} {i}"} for i in range(9)],
                                    "retire_lessons": [{"id": drop, "reason": "wrong"}],
                                    "revise_lessons": [{"id": reword, "text": "sharper"}, {"id": keep, "text": "(1, 2)"}]},
                           {"model": "m", "kind": "claude"})
    assert len(g["lessons"]) <= genome.MAX_ACTIVE and drop not in g["lessons"] and g["k"] == 3
    assert db[D.LESSONS].find_one({"_id": drop})["status"] == "retired"
    assert db[D.LESSONS].find_one({"_id": reword})["text"] == "sharper"
    assert all(l["status"] == "active" for l in genome.active_lessons(db, "r1"))
    assert "sharper" in g["addendum"] and db[D.GENERATIONS].count_documents({"run_id": "r1", "status": "active"}) == 1
    assert "level_cleared" in build_prompt(ev) and "THIS game" in build_prompt(ev)


def test_proposals_and_rule_books_of_any_shape(db):
    """Nothing a model or a hand-edited rules.json can contain may raise: distilling must not stop a game."""
    seed_run(db, "r1")
    meta = {"model": "m", "cost_usd": None, "kind": "claude"}
    for bad in ({"add_lessons": "a string", "knob_changes": ["budget_multiple", 3]},
                {"add_lessons": {"text": "x"}, "retire_lessons": "L0001", "revise_lessons": 3},
                {"add_lessons": [{"text": None, "kind": ["x"], "tags": "t", "evidence": "e"}, 5, None,
                                 {"text": {"a": 1}}]},
                {"retire_lessons": [{"id": ["L0001"]}, "L0001"], "revise_lessons": [{"id": None, "text": 5}],
                 "knob_changes": {"max_nudges": "3", "budget_multiple": None, "coach_stuck_factor": float("nan")}},
                {"rationale": {"why": "a dict"}}, []):
        g = apply_proposal(db, gather_evidence(db, "r1", 0, None), bad, meta)
        assert g["status"] == "active" and g["diff"]["added_lessons"] == [] and g["diff"]["knob_changes"] == {}
    assert genome.active(db, "r1")["knobs"] == genome.DEFAULT_KNOBS
    for i, shape in enumerate(({"verified": ["Doors need a key"], "refuted": "none", "open_questions": [1, None]},
                               {"verified": {"rule": "x"}, "refuted": [None, 7], "open_questions": "UNTESTED"},
                               {"verified": [{"rule": ["a", "b"]}, {"note": "no rule"}], "open_questions": None})):
        db[D.RULEBOOKS].insert_one({"_id": f"r1:{i + 1}", "run_id": "r1", "version": i + 1, **shape})
        g = distill_level(db, "r1", 1, level_metrics(1, 5, 10), kind="heuristic")
        texts = [db[D.LESSONS].find_one({"_id": l})["text"] for l in g["diff"]["added_lessons"]]
        assert all("['a'" not in t for t in texts)
        if i == 0:
            assert "Verified on level 1: Doors need a key." in texts                  # a bare string is a rule
        memory_gateway.recall(db, "r1", "doors key")                                  # ...and a search survives it


def test_notes_are_evidence(db, tmp_path):
    """The agent's prose reaches the distiller from the notes bank, or from notes/ when no collector stored it."""
    seed_run(db, "r1", out_dir=tmp_path)
    ws = tmp_path / "zz09-maze"
    (ws / "notes" / "deep").mkdir(parents=True)
    (ws / "notes" / "mechanics.md").write_text("Pushing a block into a wall does nothing.", encoding="utf-8")
    (ws / "notes" / "deep" / "plan.txt").write_text("See scripts/bfs.py", encoding="utf-8")
    (ws / "notes" / "probe.py").write_text("print(1)", encoding="utf-8")
    (ws / "notes" / "now.png").write_bytes(b"\x89PNG\x00\xff")
    notes = gather_evidence(db, "r1", 0)["agent_notes"]
    assert [n["path"] for n in notes] == ["notes/deep/plan.txt", "notes/mechanics.md"]
    db[D.NOTES].insert_one({"_id": "r1:notes/a.md", "run_id": "r1", "path": "notes/a.md", "text": "From the bank."})
    db[D.NOTES].insert_one({"_id": "r2:notes/a.md", "run_id": "r2", "path": "notes/a.md", "text": "Another run."})
    assert gather_evidence(db, "r1", 0)["agent_notes"] == [{"path": "notes/a.md", "text": "From the bank."}]


def state(ws, level, costs, st="NOT_FINISHED"):
    (ws / "trace.state.json").write_text(json.dumps({"level": level, "level_costs": costs, "state": st,
                                                     "win_levels": 3, "actions_used": sum(costs)}), encoding="utf-8")


def test_level_evolver(db, tmp_path):
    """One generation per cleared level, in order; the nudge goes out at once; CLAUDE.md and the session get the lessons."""
    g0 = seed_run(db, "r1", out_dir=tmp_path)
    ws = tmp_path / "zz09-maze"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("doctrine\n" + g0["addendum"], encoding="utf-8")
    said = []
    ev = LevelEvolver(db, "r1", "zz09-maze", tmp_path, ws, [2, 10, 6], kind="heuristic",
                      say=lambda t: said.append(t) or True, book_wait=0.2)
    assert ev.poll() == 0                                                            # no state file yet
    state(ws, 0, [])
    assert ev.poll() == 0 and said == []
    book(db, "r1", 1, verified=[("Walls block movement", 0)])
    state(ws, 1, [4])
    assert ev.poll() == 1 and ev.poll() == 0                                         # queued once
    assert len(said) == 1 and "python3 memory.py recall" in said[0] and "level 1" in said[0]
    nudges = list(db[D.EVENTS].find({"run_id": "r1", "what": "memory_nudge"}))
    assert len(nudges) == 1 and nudges[0]["level"] == 1 and nudges[0]["delivered"] is True
    g1 = ev.evolve(ev.jobs.get_nowait())
    assert (g1["k"], g1["level"], g1["level_metrics"]["actions"], g1["level_metrics"]["efficiency"]) == (1, 0, 4, 0.5)
    assert len(said) == 2 and "Walls block movement" in said[1] and "level 0" in said[1]
    doc = (ws / "CLAUDE.md").read_text(encoding="utf-8")
    assert doc.startswith("doctrine\n") and genome.section_of(doc) == genome.section_of(g1["addendum"])
    made = db[D.EVENTS].find_one({"run_id": "r1", "what": "generation_created"})
    assert (made["generation"], made["k"], made["level"], made["lessons"]) == (g1["_id"], 1, 0, g1["diff"]["added_lessons"])
    inj = db[D.EVENTS].find_one({"run_id": "r1", "what": "lessons_injected"})
    assert inj["lessons"] == g1["lessons"] and inj["items"][0]["text"].endswith("Walls block movement.")
    assert inj["delivered"] is True and inj["claude_md"] is True and inj["k"] == 1
    # two levels cleared between two looks: both are queued in order, one nudge (for the level the agent is on),
    # and the last level of the game is distilled without a note into a session that is over
    state(ws, 3, [4, 20, 6], "WIN")
    assert ev.poll() == 2 and len(said) == 2
    for job in (ev.jobs.get_nowait(), ev.jobs.get_nowait()):
        ev.evolve(job)
    gens = genome.lineage(db, "r1")
    assert [(g["k"], g["level"]) for g in gens] == [(0, None), (1, 0), (2, 1), (3, 2)]
    assert [g["played"] and (g["played"]["level"], g["played"]["actions"], g["played"]["efficiency"]) for g in gens] == [
        (0, 4, 0.5), (1, 20, 0.5), (2, 6, 1.0), None]
    assert [g["status"] for g in gens] == ["superseded"] * 3 + ["active"] and len(said) == 2
    assert genome.section_of((ws / "CLAUDE.md").read_text(encoding="utf-8")) == genome.section_of(gens[-1]["addendum"])
    assert db[D.EVENTS].count_documents({"run_id": "r1", "what": "generation_created"}) == 3
    assert db[D.EVENTS].count_documents({"run_id": "r1", "what": "lessons_injected"}) == 3
    assert db[D.RUNS].find_one({"_id": "r1"})["gen_k"] == 3
    # a relaunch into the same run picks up where the lineage stands: nothing is distilled twice
    again = LevelEvolver(db, "r1", "zz09-maze", tmp_path, ws, [2, 10, 6], kind="heuristic", say=lambda t: True)
    assert again.seen == 3 and again.poll() == 0


def test_level_evolver_threads_stop_cleanly(db, tmp_path):
    """Started and stopped as the launcher does it: the level cleared just before the stop is still distilled."""
    seed_run(db, "r1", out_dir=tmp_path)
    ws = tmp_path / "zz09-maze"
    ws.mkdir()
    (ws / "CLAUDE.md").write_text("doctrine\n", encoding="utf-8")
    ev = LevelEvolver(db, "r1", "zz09-maze", tmp_path, ws, [2, 10, 6], kind="heuristic", interval=0.05,
                      say=lambda t: 1 / 0, book_wait=0.1)                            # a failing outbox must not matter
    ev.start()
    state(ws, 1, [4])
    assert ev.stop(timeout=30) is True and not ev.watcher.is_alive() and not ev.worker.is_alive()
    assert [g["k"] for g in genome.lineage(db, "r1")] == [0, 1]
    assert genome.section_of((ws / "CLAUDE.md").read_text(encoding="utf-8")) is not None


def test_coach_uses_the_runs_current_lessons(db, tmp_path):
    seed_run(db, "r1", out_dir=tmp_path), seed_run(db, "other", game="yy08-ice")
    book(db, "other", 1, verified=[("Ice makes you slide", 0)])
    clear(db, "other", 0, 2)
    ws = tmp_path / "zz09-maze"
    ws.mkdir()
    said = []
    coach = Coach(db, "r1", tmp_path, ws, [2, 10, 6], say=lambda t: said.append(t) or True)
    (ws / "trace.state.json").write_text(json.dumps({"level": 0, "level_actions": 7, "state": "NOT_FINISHED"}))
    assert coach.check() is True and "Ice" not in said[0] and "may apply" not in said[0]   # no lessons yet, none borrowed
    book(db, "r1", 1, verified=[("Walls block movement", 0)])
    clear(db, "r1", 0, 4)                                                             # efficiency 0.5: no knob change
    assert [l["text"] for l in pick_lessons(db, "r1")] == ["Verified on level 0: Walls block movement."]
    (ws / "trace.state.json").write_text(json.dumps({"level": 1, "level_actions": 31, "state": "NOT_FINISHED"}))
    assert coach.check() is True and "Walls block movement" in said[1] and "Ice" not in said[1]
    assert coach.check() is False                                                    # one note per level


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


def test_launcher_keeps_the_database_from_the_agent(db, tmp_path):
    """Run ids are claimed atomically, and the child gets its memory URL but no database settings."""
    from mongoarc import launch
    fields = {"game_id": "zz09-maze", "status": "running"}
    assert launch.claim_run_id(db, "zz09-maze-T", fields) == "zz09-maze-T"
    assert launch.claim_run_id(db, "zz09-maze-T", fields) == "zz09-maze-T-2"
    doc = db[D.RUNS].find_one({"_id": "zz09-maze-T-2"})
    assert doc["metrics"] is None and doc["n_actions"] == 0 and doc["last_seq"] == 0
    env = launch.child_env({"MONGODB_URI": "mongodb://u:p@h", "MONGO_DB": "x", "MONGOARC_TEST_URI": "y",
                            "ARC_API_KEY": "k", "PATH": "/bin"}, "zz09-maze-T", "http://127.0.0.1:1234")
    assert env == {"ARC_API_KEY": "k", "PATH": "/bin", "MONGOARC_RUN_ID": "zz09-maze-T",
                   "MONGOARC_MEMORY_URL": "http://127.0.0.1:1234"}
    assert "MONGOARC_MEMORY_URL" not in launch.child_env({"MONGOARC_MEMORY_URL": "stale"}, "r", None)
    (tmp_path / "CLAUDE.md").write_text("doctrine\n", encoding="utf-8")
    g0 = genome.gen0("zz09-maze-T", "zz09-maze")
    launch.apply_generation(tmp_path, g0, g0["addendum"], memory_tool=True)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "doctrine\n" + g0["addendum"]
    launch.apply_generation(tmp_path, g0, g0["addendum"], memory_tool=True)          # twice: still one section
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "doctrine\n" + g0["addendum"]
    tool = (tmp_path / "memory.py").read_text(encoding="utf-8")
    assert (tmp_path / "scripts").is_dir() and "recall" in tool and "LESSONS from the levels you cleared" in tool
    assert "RULES you verified" in tool and "YOUR NOTES" in tool and "earlier games" not in tool
    assert not (tmp_path / "notes").exists()                                         # nothing is carried over
    args = launch.build_parser().parse_args(["--game", "g", "--out-dir", "d", "--gen", "7", "--distill-model", "m"])
    assert args.distiller == "claude" and args.distill_model == "m"                  # --gen is accepted and ignored


def test_parse_json_object():
    assert parse_json_object('Here:\n```json\n{"a": {"b": 1}}\n```\ndone') == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        parse_json_object("no json")
