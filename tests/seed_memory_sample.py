#!/usr/bin/env python3
"""Dev-only sample data for the live memory browser: all four memory banks, per docs/SCHEMA.md.

6 lessons, 4 notes (with refs), 3 rulebook snapshots (two runs), 3 generations, plus the three
`runs` documents they point at (so a card can name the game of a rule book). The four banks are
emptied first, so the script is idempotent and also resets a database after a stream test.
usage: MONGODB_URI=... MONGO_DB=mongoarc_dev_m python3 tests/seed_memory_sample.py
"""
import datetime as dt
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mongoarc import db as D

KNOBS0 = {"budget_multiple": 5.0, "max_nudges": 0, "coach_stuck_factor": 3.0}
R_KEYS = "g0-mk02-keys-20261011T131502"
R_LAVA = "g1-mk03-lava-20261011T134120"
R_ICE = "g2-mk04-ice-20261011T140847"
# run_id, game, generation, lessons_used, status, levels cleared, actions, baseline, efficiency
RUNS = [
    (R_KEYS, "mk02-keys", 0, [], "finished", 2, 96, [9, 14, 21], 0.31),
    (R_LAVA, "mk03-lava", 1, ["L0001", "L0002", "L0003"], "won", 3, 71, [8, 13, 18], 0.58),
    (R_ICE, "mk04-ice", 2, ["L0001", "L0002", "L0003", "L0004", "L0005"], "won", 3, 49, [7, 12, 16], 0.74),
]
# id, kind, created_gen, text, tags, (run, game, level, rule), status
LESSONS = [
    ("L0001", "mechanic_prior", 1, "A directional action that leaves the board unchanged means a wall or a "
     "boundary: do not repeat it from the same position.", ["walls", "movement"],
     (R_KEYS, "mk02-keys", 0, "moving into a dark cell wastes the action"), "active"),
    ("L0002", "strategy", 1, "Before spending the budget, probe each available action once from the start "
     "position and diff the frames to learn what it does.", ["exploration", "probing"],
     (R_KEYS, "mk02-keys", 0, "ACTION1-4 move the blue cell one step up, down, left, right"), "active"),
    ("L0003", "mechanic_prior", 1, "A goal that looks dull or grey is inactive: look for an object to collect "
     "first, then return to the goal.", ["goal", "key", "ordering"],
     (R_KEYS, "mk02-keys", 1, "the goal turns green only after the yellow cell is collected"), "active"),
    ("L0004", "pitfall", 2, "A cell colour that ended the play once will end it every time: mark it as lethal "
     "in the path search instead of testing it again.", ["death", "hazard"],
     (R_LAVA, "mk03-lava", 0, "entering a red cell is GAME_OVER"), "active"),
    ("L0005", "tooling", 2, "Keep a breadth-first path search in scripts/ and reuse it on every level; planning "
     "by eye costs more actions than it saves.", ["tooling", "pathfinding"],
     (R_LAVA, "mk03-lava", 2, "shortest route found by search beat the greedy walk by 9 actions"), "active"),
    ("L0006", "strategy", 1, "Walking straight toward the goal is enough to clear early levels.", ["exploration"],
     (R_KEYS, "mk02-keys", 0, "level 0 cleared by a direct walk"), "retired"),
]
NOTES = [
    (R_KEYS, "mk02-keys", 0, "notes/mechanics.md", 3, 31,
     "# mk02-keys: mechanics\n\n- The avatar is the single blue cell (colour 9). ACTION1-4 move it one cell "
     "up / down / left / right. A move into a dark cell (colour 4) changes nothing.\n"
     "- The grey block does nothing when I step on it. After I walked over the yellow cell "
     "it turned green, and stepping on it then cleared the level.\n"
     "- So: yellow first, then the goal. Checked on levels 0 and 1.\n\n"
     "Frame diffs come from scripts/frame_diff.py; the route on level 1 was planned by hand."),
    (R_LAVA, "mk03-lava", 1, "notes/mechanics.md", 4, 18,
     "# mk03-lava: mechanics\n\n- Same movement as before: blue avatar, four directions, dark cells are walls.\n"
     "- Red cells (colour 8) are lethal. One step in is GAME_OVER and RESET restarts the level.\n"
     "- Level 2 adds the grey goal and a yellow cell again: collect, then go to the goal.\n\n"
     "scripts/bfs.py treats red as blocked and returns the shortest action list; "
     "scripts/frame_diff.py confirms each step landed."),
    (R_LAVA, "mk03-lava", 1, "notes/deaths.md", 2, 17,
     "# Deaths\n\n1. Level 0, action 6: stepped right into a red cell at the corridor mouth. Lethal on contact, "
     "no warning frame.\n2. Level 1, action 19: the plan from scripts/bfs.py was built before I marked red as "
     "blocked. Fixed the cost map; no deaths since."),
    (R_ICE, "mk04-ice", 2, "notes/plan.md", 2, 4,
     "# mk04-ice: plan\n\nMovement is different here: the avatar keeps sliding until it hits a wall, so one "
     "action can cross the whole board.\n\n- Probe each direction once and diff the frames (scripts/frame_diff.py).\n"
     "- Model a move as 'slide to the last free cell' and search over stop positions with "
     "scripts/slide_bfs.py.\n- Open question: does the yellow cell stop a slide, or do I pass over it and collect it?"),
]
V_KEYS = [{"rule": "ACTION1-4 move the blue cell one step up, down, left, right", "scope": "game", "level": 0,
           "note": "probed each action once"},
          {"rule": "moving into a dark cell wastes the action", "scope": "game", "level": 0, "note": "board unchanged"}]
RULEBOOKS = [
    (R_KEYS, 1, 0, 14, 44, V_KEYS, [], ["level 0: UNTESTED - what does the grey block do"],
     {"verified": V_KEYS, "refuted": [], "open_questions": ["level 0: UNTESTED - what does the grey block do"]}),
    (R_KEYS, 2, 1, 52, 36,
     V_KEYS + [{"rule": "the goal turns green only after the yellow cell is collected", "scope": "game", "level": 1,
                "note": "stepped on grey first: nothing; after yellow: level cleared"}],
     [{"rule": "the grey block is a wall", "scope": "game", "level": 1, "note": "the avatar can stand on it"}],
     ["level 2: UNTESTED - is the yellow cell always reachable without crossing the goal"],
     {"verified": [{"rule": "the goal turns green only after the yellow cell is collected", "scope": "game",
                    "level": 1, "note": "stepped on grey first: nothing; after yellow: level cleared"}],
      "refuted": [{"rule": "the grey block is a wall", "scope": "game", "level": 1,
                   "note": "the avatar can stand on it"}],
      "open_questions": ["level 2: UNTESTED - is the yellow cell always reachable without crossing the goal"]}),
    (R_LAVA, 1, 0, 11, 20,
     [{"rule": "entering a red cell is GAME_OVER", "scope": "game", "level": 0, "note": "died once at action 6"},
      {"rule": "RESET restarts the current level, not the game", "scope": "game", "level": 0, "note": "level counter kept"}],
     [{"rule": "red cells only cost extra actions", "scope": "game", "level": 0, "note": "the play ended instead"}],
     ["level 1: UNTESTED - do red cells move between actions"], None),
]


def main():
    name = os.environ.get("MONGO_DB", "")
    if not os.environ.get("MONGODB_URI") or not any(s in name for s in ("seed", "test", "dev")):
        sys.exit("set MONGODB_URI and a dev MONGO_DB (its name must contain seed, test or dev)")
    d = D.get_db()
    D.ensure_indexes(d)
    for bank in D.MEMORY_BANKS:
        d[bank].delete_many({})
    now = D.now()

    def ago(minutes):
        return now - dt.timedelta(minutes=minutes)

    started = {R_KEYS: ago(58), R_LAVA: ago(33), R_ICE: ago(9)}
    for run_id, game, gen, lessons, status, cleared, actions, baseline, eff in RUNS:
        t0 = started[run_id]
        end = None if run_id == R_ICE else t0 + dt.timedelta(minutes=19)
        d[D.RUNS].replace_one({"_id": run_id}, {
            "_id": run_id, "game_id": game, "out_dir": f"/sample/{run_id}", "label": "memory-sample",
            "agent": "fake", "model": "fake", "effort": None, "generation": gen, "lessons_used": lessons,
            "baseline_actions": baseline, "status": "running" if end is None else status,
            "started_at": t0, "updated_at": end or now, "ended_at": end,
            "state": "NOT_FINISHED" if end is None else "WIN" if status == "won" else "GAME_OVER",
            "level": cleared, "win_levels": 3, "actions_used": actions, "level_costs": [], "gate_pending": None,
            "deaths": 0, "full_resets": 0, "wasted_actions": 0, "n_actions": 0, "last_seq": 0,
            "cost_usd": None, "turns": None, "result": None, "cursors": {},
            "metrics": {"levels": [], "levels_cleared": cleared, "win_levels": 3, "actions_total": actions,
                        "baseline_total": sum(baseline), "efficiency": eff, "completion": round(cleared / 3, 3)}},
            upsert=True)

    used = {lid: [r for r in RUNS if lid in r[3]] for lid, *_ in LESSONS}
    for i, (lid, kind, gen, text, tags, (run_id, game, level, rule), status) in enumerate(LESSONS):
        runs = used[lid] if lid != "L0006" else [RUNS[1]]
        d[D.LESSONS].insert_one({
            "_id": lid, "text": text, "kind": kind, "tags": tags,
            "evidence": [{"run_id": run_id, "game_id": game, "level": level, "rule": rule}],
            "created_gen": gen, "created_at": ago(36 if gen == 1 else 12) + dt.timedelta(seconds=i),
            "status": status,
            "retired_reason": ("Contradicted by later runs: the direct walk died in lava and cost three times "
                               "the baseline.") if status == "retired" else None,
            "stats": {"runs": [r[0] for r in runs], "injected": len(runs),
                      "efficiency_sum": round(sum(r[8] for r in runs), 3),
                      "wins": sum(r[4] == "won" for r in runs), "recalled": (i * 2) % 5}})

    for run_id, game, gen, path, version, minutes, text in NOTES:
        d[D.NOTES].insert_one({
            "_id": f"{run_id}:{path}", "run_id": run_id, "game_id": game, "generation": gen, "path": path,
            "text": text, "refs": sorted(set(re.findall(r"scripts/[\w.]+\.py", text))),
            "version": version, "created_at": ago(minutes + 9), "updated_at": ago(minutes)})

    for run_id, version, level, action_n, minutes, verified, refuted, questions, added in RULEBOOKS:
        d[D.RULEBOOKS].insert_one({
            "_id": f"{run_id}:{version}", "run_id": run_id, "version": version, "at": ago(minutes),
            "action_n": action_n, "level": level, "verified": verified, "refuted": refuted,
            "open_questions": questions,
            "added": added or {"verified": verified, "refuted": refuted, "open_questions": questions}})

    no_diff = {"added_lessons": [], "retired_lessons": [], "revised_lessons": [], "knob_changes": {}, "added_tools": []}
    text_of = {l[0]: l[3] for l in LESSONS}
    bfs = {"name": "bfs", "source_run": R_LAVA, "why": "The lava solver wrote a path search that the next game needs too.",
           "code": "def bfs(grid, start, goal, blocked):\n    ...\n"}
    gens = [
        {"_id": 0, "parent": None, "created_at": ago(60), "created_from_run": None, "status": "superseded",
         "lessons": [], "addendum": "", "knobs": KNOBS0, "tools": [], "diff": no_diff,
         "rationale": "The empty harness: stock doctrine, no carried lessons. This is the baseline to beat.",
         "distiller": {"model": "none", "cost_usd": None, "kind": "heuristic"},
         "fitness": {"runs": [R_KEYS], "mean_efficiency": 0.31, "mean_completion": 0.667}},
        {"_id": 1, "parent": 0, "created_at": ago(35), "created_from_run": R_KEYS, "status": "superseded",
         "lessons": ["L0001", "L0002", "L0003", "L0006"],
         "addendum": "## Lessons from earlier games\n- " + "\n- ".join(text_of[i] for i in ("L0001", "L0002", "L0003", "L0006")),
         "knobs": {**KNOBS0, "max_nudges": 2}, "tools": [],
         "diff": {**no_diff, "added_lessons": ["L0001", "L0002", "L0003", "L0006"], "knob_changes": {"max_nudges": [0, 2]}},
         "rationale": "The baseline run spent more than half its actions on moves that changed nothing and never "
                      "found that the goal needed the key. Added priors about walls, probing and inactive goals, "
                      "and allowed two nudges.",
         "distiller": {"model": "claude-opus-5", "cost_usd": 0.41, "kind": "claude"},
         "fitness": {"runs": [R_LAVA], "mean_efficiency": 0.58, "mean_completion": 1.0}},
        {"_id": 2, "parent": 1, "created_at": ago(11), "created_from_run": R_LAVA, "status": "active",
         "lessons": ["L0001", "L0002", "L0003", "L0004", "L0005"],
         "addendum": "## Lessons from earlier games\n- " + "\n- ".join(text_of[i] for i in ("L0001", "L0002", "L0003", "L0004", "L0005")),
         "knobs": {**KNOBS0, "max_nudges": 2, "coach_stuck_factor": 2.0, "budget_multiple": 4.0}, "tools": [bfs],
         "diff": {"added_lessons": ["L0004", "L0005"], "retired_lessons": ["L0006"], "revised_lessons": ["L0002"],
                  "knob_changes": {"coach_stuck_factor": [3.0, 2.0], "budget_multiple": [5.0, 4.0]},
                  "added_tools": ["bfs"]},
         "rationale": "The lava run won but died twice walking straight at the goal, then wrote its own path "
                      "search. Promoted the search to a carried tool, added the lethal-colour prior, retired the "
                      "direct-walk lesson the evidence now contradicts, and the coach steps in sooner.",
         "distiller": {"model": "claude-opus-5", "cost_usd": 0.56, "kind": "claude"},
         "fitness": {"runs": [R_ICE], "mean_efficiency": 0.74, "mean_completion": 1.0}},
    ]
    d[D.GENERATIONS].insert_many(gens)
    print(name, {bank: d[bank].count_documents({}) for bank in D.MEMORY_BANKS}, "runs", d[D.RUNS].count_documents({}))


if __name__ == "__main__":
    main()
