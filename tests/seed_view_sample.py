#!/usr/bin/env python3
"""Dev-only sample data for the live view: six runs, six lessons, three generations.

The boards are the seeded fixture run's (seed-mk01-toy); wasted moves, timelines, lessons and
generations are invented so every part of the page has something to draw. Idempotent.
usage: MONGODB_URI=... MONGO_DB=mongoarc_seed python3 tests/seed_view_sample.py
"""
import datetime as dt
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mongoarc import db as D

SRC = "seed-mk01-toy"
BASELINE = [10, 12, 14]
KNOBS0 = {"budget_multiple": 5.0, "max_nudges": 0, "coach_stuck_factor": 3.0}
# run_id, generation, lessons_used, wasted moves per real move, level to die in
RUNS = [
    ("sample-g0-mk01-toy-1", 0, [], 1.3, None),
    ("sample-g0-mk01-toy-2", 0, [], 1.0, 2),
    ("sample-g1-mk01-toy-1", 1, ["L0001", "L0002", "L0003", "L0006"], 0.6, None),
    ("sample-g1-mk01-toy-2", 1, ["L0001", "L0002", "L0003", "L0006"], 0.45, None),
    ("sample-g2-mk01-toy-1", 2, ["L0001", "L0002", "L0003", "L0004", "L0005"], 0.2, None),
    ("sample-g2-mk01-toy-2", 2, ["L0001", "L0002", "L0003", "L0004", "L0005"], 0.05, None),
]
LESSONS = [
    ("L0001", "mechanic_prior", 1, "A directional action that leaves the board unchanged means a wall or a boundary: "
     "do not repeat it from the same position.", ["walls", "movement"], 0, "moving into a wall wastes the action"),
    ("L0002", "strategy", 1, "Before spending the budget, probe each available action once from the start position "
     "and diff the frames to learn what it does.", ["exploration"], 0, "each action moves the avatar one cell"),
    ("L0003", "pitfall", 1, "A bar that shrinks on every action is a move budget; when it empties the play ends, "
     "so plan the route before moving.", ["budget", "death"], 2, "the top bar is a step counter"),
    ("L0004", "strategy", 2, "Once the goal object is identified, search for the shortest path on the grid instead "
     "of stepping greedily toward it.", ["planning", "pathfinding"], 1, "the goal cell ends the level"),
    ("L0005", "tooling", 2, "Keep a frame-diff helper in notes/ and call it after every batch of actions; reading "
     "raw grids by eye costs turns.", ["tooling"], 1, "random walk"),
    ("L0006", "mechanic_prior", 1, "Random exploration is enough to clear early levels.", ["exploration"], 0,
     "level 0 cleared by random play"),
]
THOUGHTS = ["Nothing moved on the last two actions, so that direction is blocked here.",
            "The avatar is the single cell that changes between frames; the goal is the static coloured block.",
            "Checking whether the level counter advanced before spending more actions.",
            "The remaining distance is mostly vertical, so vertical moves first.",
            "Diffing the last frame against the one before to confirm the move landed."]
SAYS = ["Probing the available actions.", "Moving toward the goal.", "Recording what this level established.",
        "Continuing with the next batch."]
RULES = [({"rule": "each directional action moves the avatar one cell", "scope": "game"},
          None, "what does the shrinking bar at the top count"),
         ({"rule": "reaching the goal block clears the level", "scope": "game"},
          {"rule": "the goal moves between actions", "scope": "game"}, "do walls change between levels"),
         ({"rule": "walls block movement and the blocked action is wasted", "scope": "game"},
          {"rule": "diagonal moves exist", "scope": "game"}, "is there a shorter route through the gap")]


def build_run(d, base, run_id, gen, lessons, waste, die_level, t0):
    rng = random.Random(run_id)
    rows, events, books = [], [], []
    clock = {"seq": 0, "t": t0}

    def tick(sec=0.4):
        clock["seq"] += 1
        clock["t"] += dt.timedelta(seconds=sec)
        return clock["seq"], clock["t"]

    def ev(kind, **kw):
        seq, at = tick(0.2)
        events.append({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": at, "kind": kind,
                       "action_n": len(rows) - 1, "level": rows[-1]["level"] if rows else 0, **kw})

    def row(action, level, state, frames, params=None, full_reset=False, death=False):
        seq, at = tick()
        n = len(rows)
        rows.append({"_id": f"{run_id}:{n}", "run_id": run_id, "n": n, "i": n, "seq": seq, "level": level,
                     "action": action, "params": params or {}, "state": state, "full_reset": full_reset,
                     "available_actions": ["ACTION1", "ACTION2", "ACTION3", "ACTION4", "ACTION6"],
                     "frames": frames, "level_cleared": bool(rows) and level > rows[-1]["level"],
                     "new_play": full_reset, "death": death, "wasted": not frames, "observed_at": at})

    def ack(level):
        v = len(books) + 1
        ok, bad, q = RULES[level % len(RULES)]
        added = {"verified": [{**ok, "level": level, "note": f"level {level} cleared"}],
                 "refuted": [{**bad, "level": level, "note": "tested, did not hold"}] if bad else [],
                 "open_questions": [f"level {level}: UNTESTED - {q}"]}
        prev = books[-1] if books else {"verified": [], "refuted": [], "open_questions": []}
        seq, at = tick(0.2)
        books.append({"_id": f"{run_id}:{v}", "run_id": run_id, "version": v, "at": at, "action_n": len(rows) - 1,
                      "level": rows[-1]["level"], **{k: prev[k] + added[k] for k in added}, "added": added})
        events.append({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": at, "kind": "rulebook",
                       "action_n": len(rows) - 1, "level": rows[-1]["level"], "version": v, "added": added})

    ev("harness", what="run_started", lessons=[],
       text=f"Launching mk01-toy with harness generation {gen}: {len(lessons)} lessons carried.")
    if lessons:
        ev("harness", what="lessons_injected", lessons=lessons,
           text=f"{len(lessons)} lessons appended to the workspace CLAUDE.md.")
    ev("system", subtype="init", text="session started")
    batch, tool_n, in_level, dead, coached = 0, 0, 0, False, False
    for a in base:
        if batch == 0:
            tool_n += 1
            ev("thinking", text=rng.choice(THOUGHTS))
            ev("text", text=rng.choice(SAYS))
            cmd = ["python3 -c '", "from session import client, gate"]
            if tool_n % 3 == 1:  # a long command, to exercise the collapsed block
                cmd += [f"plan.append({rng.randint(1, 4)})  # step {i}" for i in range(14)]
            cmd += ["for a in plan:", "    client.act(a)", "print(client.status())'"]
            ev("tool_use", tool="Bash", tool_use_id=f"toolu_sample_{tool_n}", command="\n".join(cmd),
               description="Play the next batch of actions")
        prev_level = rows[-1]["level"] if rows else 0
        prev_board = next((r["frames"][-1] for r in reversed(rows) if r["frames"]), None)
        if a["n"] > 0:
            for _ in range(int(waste) + (rng.random() < waste % 1)):
                if rng.random() < 0.2:
                    row("ACTION6", prev_level, "NOT_FINISHED", [], {"x": rng.randrange(64), "y": rng.randrange(64)})
                else:
                    row(rng.choice(["ACTION1", "ACTION3"]), prev_level, "NOT_FINISHED", [])
        in_level = in_level + 1 if a["level"] == prev_level else 0
        dead = die_level is not None and a["level"] == die_level and in_level >= 8
        frames = list(a["frames"])
        if prev_board and frames and a["n"] % 3 == 0:  # a two-frame animation
            frames = [prev_board] + frames
        row(a["action"], a["level"], "GAME_OVER" if dead else a["state"], frames, a["params"],
            bool(a["full_reset"]), dead)
        batch += 1
        cleared = rows[-1]["level_cleared"]
        if batch >= 8 or cleared or dead or a is base[-1]:
            batch = 0
            lvl = rows[-1]["level"]
            if tool_n % 5 == 0 and not cleared:
                ev("tool_result", tool_use_id=f"toolu_sample_{tool_n}", is_error=True,
                   text="Traceback (most recent call last):\n  File \"<string>\", line 4, in <module>\n"
                        "ccarc.arc_3.ActionRefused: ACTION5 is not available on this level")
            else:
                ev("tool_result", tool_use_id=f"toolu_sample_{tool_n}", is_error=False,
                   text=f"mk01-toy: level {lvl}/3 state={rows[-1]['state']} actions={len(rows) - 1}")
            if cleared:
                msg = f"Level {lvl - 1} cleared - you are on level {lvl}/3.\nRecord what level {lvl - 1} established with gate.acknowledge(...)."
                ev("outbox", outbox_kind="level_cleared", text=msg)
                ev("user_msg", text=msg)
                ack(lvl - 1)
            elif waste >= 1 and not coached and len(rows) > 30:
                coached = True
                ev("harness", what="coach_note", lessons=[],
                   text="Coach: 12 of the last 20 actions left the board unchanged. Diff frames before repeating a move.")
        if dead:
            break
    won = rows[-1]["state"] == "WIN"
    ev("result", cost_usd=round(0.02 * len(rows), 2), turns=tool_n * 2, duration_ms=len(rows) * 400,
       is_error=False, text="done")
    ev("harness", what="run_ended", lessons=[],
       text=f"Run ended: {'won' if won else 'game over'} after {len(rows) - 1} actions.")

    costs, level = [], 0
    for r in rows[1:]:
        costs += [0] * (level + 1 - len(costs))
        costs[level] += 1
        level = r["level"]
    per = []
    for i, b in enumerate(BASELINE):
        n, ok = (costs[i] if i < len(costs) else 0), i < level
        per.append({"level": i, "actions": n, "baseline": b, "cleared": ok,
                    "efficiency": round(min(1.0, b / n), 2) if ok and n else 0.0})
    metrics = {"levels": per, "levels_cleared": level, "win_levels": 3, "actions_total": len(rows) - 1,
               "baseline_total": sum(BASELINE), "efficiency": round(sum(p["efficiency"] for p in per) / 3, 3),
               "completion": round(level / 3, 3)}
    end = clock["t"]
    doc = {"_id": run_id, "game_id": "mk01-toy", "out_dir": f"/sample/{run_id}", "label": "sample",
           "agent": "fake", "model": "fake", "effort": None, "generation": gen, "lessons_used": lessons,
           "baseline_actions": BASELINE, "status": "won" if won else "finished" if level else "lost",
           "started_at": t0, "updated_at": end, "ended_at": end, "state": rows[-1]["state"], "level": level,
           "win_levels": 3, "actions_used": len(rows) - 1, "level_costs": costs, "gate_pending": None,
           "deaths": sum(r["death"] for r in rows), "full_resets": 0,
           "wasted_actions": sum(r["wasted"] for r in rows), "n_actions": len(rows), "last_seq": clock["seq"],
           "cost_usd": round(0.02 * len(rows), 2), "turns": tool_n * 2, "result": None, "metrics": metrics,
           "cursors": {}}
    for c in (D.ACTIONS, D.EVENTS, D.RULEBOOKS):
        d[c].delete_many({"run_id": run_id})
    d[D.RUNS].replace_one({"_id": run_id}, doc, upsert=True)
    d[D.ACTIONS].insert_many(rows)
    d[D.EVENTS].insert_many(events)
    if books:
        d[D.RULEBOOKS].insert_many(books)
    return doc


def main():
    name = os.environ.get("MONGO_DB", "")
    if not os.environ.get("MONGODB_URI") or not any(s in name for s in ("seed", "test", "dev")):
        sys.exit("set MONGODB_URI and a dev MONGO_DB (its name must contain seed, test or dev)")
    d = D.get_db()
    D.ensure_indexes(d)
    base = list(d[D.ACTIONS].find({"run_id": SRC}).sort("n", 1))
    if not base:
        sys.exit(f"{SRC} is not in {name}; load it first with scripts/seed_fixture.py")
    t0 = D.now() - dt.timedelta(hours=2)
    runs = [build_run(d, base, rid, gen, ls, waste, die, t0 + dt.timedelta(minutes=12 * i))
            for i, (rid, gen, ls, waste, die) in enumerate(RUNS)]
    by_gen = {g: [r for r in runs if r["generation"] == g] for g in (0, 1, 2)}

    for lid, kind, gen, text, tags, level, rule in LESSONS:
        used = [r for r in runs if lid in r["lessons_used"]]
        retired = lid == "L0006"
        d[D.LESSONS].replace_one({"_id": lid}, {
            "_id": lid, "text": text, "kind": kind, "tags": tags,
            "evidence": [{"run_id": by_gen[gen - 1][-1]["_id"], "game_id": "mk01-toy", "level": level, "rule": rule}],
            "created_gen": gen, "created_at": by_gen[gen - 1][-1]["ended_at"],
            "status": "retired" if retired else "active",
            "retired_reason": "Contradicted by later runs: random play cost three times the baseline." if retired else None,
            "stats": {"runs": [r["_id"] for r in used], "injected": len(used),
                      "efficiency_sum": round(sum(r["metrics"]["efficiency"] for r in used), 3),
                      "wins": sum(r["status"] == "won" for r in used)}}, upsert=True)

    def fitness(g):
        rs = by_gen[g]
        return {"runs": [r["_id"] for r in rs],
                "mean_efficiency": round(sum(r["metrics"]["efficiency"] for r in rs) / len(rs), 3),
                "mean_completion": round(sum(r["metrics"]["completion"] for r in rs) / len(rs), 3)}

    no_diff = {"added_lessons": [], "retired_lessons": [], "revised_lessons": [], "knob_changes": {}, "added_tools": []}
    tool = {"name": "frame_diff", "source_run": by_gen[1][-1]["_id"],
            "why": "Two earlier solvers each rewrote the same diff helper in notes/.",
            "code": "def frame_diff(a, b):\n    return [(x, y) for y in range(64) for x in range(64) if a[y][x] != b[y][x]]\n"}
    gens = [
        {"_id": 0, "parent": None, "created_from_run": None, "status": "superseded", "lessons": [],
         "addendum": "", "knobs": KNOBS0, "tools": [], "diff": no_diff,
         "rationale": "The empty harness: stock doctrine, no carried lessons. This is the baseline to beat.",
         "distiller": {"model": "none", "cost_usd": None, "kind": "heuristic"}},
        {"_id": 1, "parent": 0, "created_from_run": by_gen[0][-1]["_id"], "status": "superseded",
         "lessons": ["L0001", "L0002", "L0003", "L0006"],
         "addendum": "## Lessons from earlier games\n- " + "\n- ".join(l[3] for l in LESSONS if l[2] == 1),
         "knobs": {**KNOBS0, "max_nudges": 2}, "tools": [],
         "diff": {**no_diff, "added_lessons": ["L0001", "L0002", "L0003", "L0006"], "knob_changes": {"max_nudges": [0, 2]}},
         "rationale": "Both baseline runs spent more than half their actions on moves that changed nothing, and one "
                      "died to the step counter. Added priors about walls and budgets and allowed two nudges.",
         "distiller": {"model": "claude-opus-5", "cost_usd": 0.41, "kind": "claude"}},
        {"_id": 2, "parent": 1, "created_from_run": by_gen[1][-1]["_id"], "status": "active",
         "lessons": ["L0001", "L0002", "L0003", "L0004", "L0005"],
         "addendum": "## Lessons from earlier games\n- " + "\n- ".join(l[3] for l in LESSONS if l[0] != "L0006"),
         "knobs": {**KNOBS0, "max_nudges": 2, "coach_stuck_factor": 2.0, "budget_multiple": 4.0}, "tools": [tool],
         "diff": {"added_lessons": ["L0004", "L0005"], "retired_lessons": ["L0006"], "revised_lessons": ["L0002"],
                  "knob_changes": {"coach_stuck_factor": [3.0, 2.0], "budget_multiple": [5.0, 4.0]},
                  "added_tools": ["frame_diff"]},
         "rationale": "Wasted moves fell, but the solvers still walked greedily and rebuilt the same diff helper. "
                      "Added path search and promoted the helper; retired the random-play prior, which the "
                      "evidence now contradicts; the coach steps in sooner.",
         "distiller": {"model": "claude-opus-5", "cost_usd": 0.56, "kind": "claude"}},
    ]
    for g in gens:
        g["created_at"] = by_gen[g["_id"]][0]["started_at"] - dt.timedelta(minutes=2)
        g["fitness"] = fitness(g["_id"])
        d[D.GENERATIONS].replace_one({"_id": g["_id"]}, g, upsert=True)
    for r in runs:
        m = r["metrics"]
        print(f"{r['_id']:24s} gen {r['generation']} {r['status']:9s} actions {r['actions_used']:4d} "
              f"efficiency {m['efficiency']:.3f} completion {m['completion']:.2f}")
    print("lessons", d[D.LESSONS].count_documents({}), "generations", d[D.GENERATIONS].count_documents({}))


if __name__ == "__main__":
    main()
