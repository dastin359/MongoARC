#!/usr/bin/env python3
"""Dev-only sample data in the per-level shape, for the live view. Everything here is INVENTED.

Three runs, written exactly per the shared data contract (generations and lessons carry run_id; one
generation lineage per run, a new generation each time a level is cleared):

  lv-ls20-...   role train, finished, 6 of 6 levels, generations k=0..6, 8 lessons (2 retired, 1 revised)
  ctl-ls20-...  role control (--no-memory), same game, 4 of 6 levels, generation k=0 only, no lessons
  lv-ft09-...   role train, a different game, running, on level 2, generations k=0..2, 3 lessons

Action rows carry no boards (the page's counters need the rows, not the frames). The whole database
named by MONGO_DB is cleared first, so the name must say it is a dev database.
usage: MONGODB_URI=mongodb://127.0.0.1:27117/?directConnection=true MONGO_DB=mongoarc_dev_view python3 tests/seed_levels_sample.py
"""
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mongoarc import db as D

KNOBS = {"budget_multiple": 5.0, "max_nudges": 2, "coach_stuck_factor": 3.0}
T0 = dt.datetime(2026, 10, 11, 21, 15, tzinfo=dt.timezone.utc)
LS20, FT09 = "ls20-016295f7601e", "ft09-b8377d4b7815"
TRAIN, CTRL, LIVE = f"lv-{LS20}-20261011T211500", f"ctl-{LS20}-20261011T214800", f"lv-{FT09}-20261011T222000"
NUDGE = ("Level {n} starts now. Before your first action, search your memory for what this game has already "
         "taught you: python3 memory.py recall <words for what you see>.")

# id, level learned, kind, text, tags, evidence rule, retired at k (or None), retired reason
LS20_LESSONS = [
    ("L0001", 0, "mechanic_prior", "ACTION1 to ACTION4 move the block one cell up, down, left and right. A move into a "
     "wall changes nothing and still costs an action.", ["movement", "walls"], "moving into a wall wastes the action", None, None),
    ("L0002", 0, "strategy", "Probe every available action once at the start of a level before planning a route.",
     ["exploration"], "each action moves the block one cell", 3,
     "The actions kept their meaning on levels 1 and 2: probing again cost 8 actions per level for nothing."),
    ("L0003", 1, "mechanic_prior", "The bar at the top loses one segment per action and the level restarts when it is "
     "empty. A purple cell refills it, once per level.", ["budget", "restart"], "the top bar is a step counter", None, None),
    ("L0004", 1, "strategy", "Walk to the key-shaped cell first, then straight to the door.", ["route"],
     "touching the key cell changes the key in the corner", 5,
     "On level 4 the key has to be turned on the rotator pad first; walking straight to the door wasted 21 actions."),
    ("L0005", 2, "pitfall", "The key drawn in the bottom-left corner must match the door in shape and colour. Stepping "
     "on the door with the wrong key costs the action and nothing happens.", ["key", "door"], "the door opens only for a matching key", None, None),
    ("L0006", 3, "strategy", "Plan the whole route on the grid before moving: count its steps against the segments "
     "left on the bar and route through the refill cell when the count is short.", ["planning", "budget"],
     "the level restarts when the bar is empty", None, None),
    ("L0007", 4, "mechanic_prior", "The cross-shaped pad turns the key a quarter turn each time the block steps on it; "
     "the square pad cycles its colour.", ["key", "pads"], "the cross pad rotates the key", None, None),
    ("L0008", 5, "tooling", "notes/route.py finds the shortest route by breadth-first search over the parsed grid. Run "
     "it at the start of a level instead of reading the grid by eye.", ["tooling", "pathfinding"], "walls are the grey cells", None, None),
]
FT09_LESSONS = [
    ("L0009", 0, "mechanic_prior", "ACTION6 on a cell cycles its colour. The level is cleared when the cells around "
     "the centre match the reference pattern in the corner.", ["click", "pattern"], "a click cycles the cell colour", None, None),
    ("L0010", 1, "pitfall", "From level 1 a click also flips the four neighbours of the cell: work from the top row "
     "down so finished rows are not disturbed.", ["click", "neighbours"], "a click flips the neighbours too", None, None),
    ("L0011", 1, "strategy", "Count the wrong cells before the first click and solve on paper; every stray click has "
     "to be undone with two more.", ["planning"], "colours cycle through three states", None, None),
]
# k: (added, retired, revised, knob changes, rationale)
LS20_DIFFS = {
    1: (["L0001", "L0002"], [], [], {}, "Level 0 cost 47 actions against a baseline of 29: 14 of them were moves into "
        "walls. The rule book now says what each action does, so the next levels should not pay to find out again."),
    2: (["L0003", "L0004"], [], [], {}, "Level 1 was restarted once because the bar ran out. The agent's notes name the "
        "bar and the key cell; both are stated as lessons for the levels ahead."),
    3: (["L0005"], ["L0002"], [], {"coach_stuck_factor": [3.0, 2.5]}, "Level 2 went 9 actions over its baseline, most of "
        "them on a door that would not open. Probing every action again was pure cost, so that lesson is retired; "
        "the coach now steps in sooner."),
    4: (["L0006"], [], ["L0003"], {}, "Level 3 was cleared under its baseline. The refill cell was used twice and only "
        "the first use worked, so the lesson about the bar now says so."),
    5: (["L0007"], ["L0004"], [], {}, "Level 4 introduced the pads. The old route lesson sent the agent to the door "
        "with an unturned key, so it is retired and the pad mechanics are recorded."),
    6: (["L0008"], [], [], {}, "Last level cleared at 0.55 of the human baseline. The route script the agent wrote is "
        "recorded; no level is left in this game to play under this generation."),
}
FT09_DIFFS = {
    1: (["L0009"], [], [], {}, "Level 0 took 31 actions against a baseline of 18: most clicks were spent learning that "
        "a click cycles a colour."),
    2: (["L0010", "L0011"], [], [], {}, "Level 1 was cleared in 22 actions against 24. The neighbour flip is new on "
        "this level and cost four undo clicks; both points are stated for level 2."),
}
RULES = {
    LS20: ["ACTION1..4 move the block one cell", "the top bar is a step counter", "the door opens only for a matching key",
           "a purple cell refills the bar once", "the cross pad rotates the key", "the square pad cycles the key colour"],
    FT09: ["ACTION6 on a cell cycles its colour", "a click flips the four neighbours too", "colours cycle through three states"],
}


def level_metric(level, actions, baseline):
    return {"level": level, "actions": actions, "baseline": baseline,
            "efficiency": round(min(1.0, baseline / actions), 3) if baseline and actions else None}


def build(run_id, game, role, base, costs, cleared, status, t0, gen_ids, diffs, lessons, recalls, model):
    """One run with its action rows, timeline, rule books, notes, generations and lessons."""
    memory = role != "control"
    actions, events, books, notes, gens, lesson_docs = [], [], [], [], [], []
    seq, n, t = 0, -1, t0
    by_id = {x[0]: x for x in lessons}

    def ev(kind, level, **payload):
        nonlocal seq, t
        seq += 1
        t += dt.timedelta(seconds=7)
        events.append({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": t, "kind": kind,
                       "action_n": n, "level": level, **payload})

    def act(action, level, state="NOT_FINISHED", cleared_now=False, wasted=False):
        nonlocal seq, n, t
        seq += 1
        n += 1
        t += dt.timedelta(seconds=4)
        actions.append({"_id": f"{run_id}:{n:06d}", "run_id": run_id, "n": n, "seq": seq, "at": t, "action": action,
                        "params": {}, "level": level, "state": state, "level_cleared": cleared_now, "death": False,
                        "wasted": wasted, "full_reset": action == "RESET", "new_play": False, "frames": [],
                        "available_actions": [1, 2, 3, 4]})

    def harness(what, level, text, ids=None, **extra):
        ev("harness", level, what=what, text=text, lessons=ids or [], **extra)

    def addendum(ids):
        if not ids:
            return ""
        return "## Lessons from the levels of this game cleared so far\n" + "\n".join(f"- [{i}] {by_id[i][3]}" for i in ids)

    def generation(k, level, active):
        added, retired, revised, knobs, why = diffs.get(k, ([], [], [], {}, "The bare harness: nothing has been learned in this game yet."))
        kn = dict(KNOBS)
        for j in range(1, k + 1):
            for name, (_, new) in diffs.get(j, ([], [], [], {}, ""))[3].items():
                kn[name] = new
        gens.append({
            "_id": gen_ids[k], "run_id": run_id, "game_id": game, "k": k, "level": level,
            "parent": gen_ids[k - 1] if k else None, "status": "superseded", "created_at": t, "lessons": list(active),
            "addendum": addendum(active), "knobs": kn,
            "diff": {"added_lessons": added, "retired_lessons": retired, "revised_lessons": revised, "knob_changes": knobs},
            "rationale": why,
            "distiller": {"model": model if k else "none", "cost_usd": round(0.011 + 0.002 * k, 3) if k else 0.0, "kind": "llm" if k else "none"},
            "level_metrics": level_metric(level, costs[level], base[level]) if k else None, "played": None})

    ev("system", 0, subtype="init", text=f"session started in workspace for {game}")
    act("RESET", 0)
    active: list[str] = []
    generation(0, None, active)
    harness("run_started", 0, f"{game}: generation 0, no lessons yet" if memory else f"{game}: control run, no memory tool")
    for lv, cost in enumerate(costs):
        done = lv < cleared
        if memory and lv == 0:
            ev("user_msg", 0, text=NUDGE.format(n=0))
        for i in range(recalls.get(lv, 0)):
            q = ["door key bar", "walls route refill", "key shape colour door", "pad rotate key", "route shortest"][(lv + i) % 5]
            ev("tool_use", lv, tool="Bash", tool_use_id=f"tu{seq}", command=f"python3 memory.py recall {q}", description="Search this game's memory")
            hit = [x for x in active][:3]
            ev("recall", lv, query=q, lessons=hit, notes=[f"{run_id}:notes/mechanics.md"] if lv else [],
               text=f"agent searched memory for {q!r}: {len(hit)} lesson(s), {1 if lv else 0} note(s), {min(lv, 3)} rule(s) from this game")
        ev("thinking", lv, text="Reading the board before spending actions: where is the block, where is the goal, what is new on this level.")
        ev("text", lv, text=f"Level {lv}: planning the route, then playing it.")
        for i in range(cost):
            last = done and i == cost - 1
            win = last and lv == len(base) - 1
            act(f"ACTION{1 + (i * 7 + lv) % 4}", lv + 1 if last else lv, "WIN" if win else "NOT_FINISHED", last, wasted=(i % 9 == 4))
        if not done:
            ev("text", lv, text=f"Level {lv} is not cleared after {cost} actions; the budget for this level is spent.")
            break
        ev("outbox", lv + 1, outbox_kind="level_cleared", text=f"Level {lv} cleared in {cost} actions (human baseline {base[lv]}). "
           "Record what you verified in rules.json before you go on.")
        rules = RULES[game][:min(len(RULES[game]), lv + 1)]
        added = [{"rule": rules[-1], "level": lv}] if lv < len(RULES[game]) else []
        books.append({"_id": f"{run_id}:{lv + 1}", "run_id": run_id, "version": lv + 1, "level": lv + 1, "action_n": n, "at": t,
                      "verified": [{"rule": r, "level": j} for j, r in enumerate(rules)], "refuted": [],
                      "open_questions": ["does the layout repeat on later levels"], "added": {"verified": added, "refuted": [], "open_questions": []}})
        ev("rulebook", lv + 1, version=lv + 1, added={"verified": added, "refuted": [], "open_questions": []})
        if not memory:
            continue
        k = lv + 1
        add, ret, rev, _, why = diffs[k]
        active = [x for x in active if x not in ret] + add
        gens[-1]["played"] = level_metric(lv, cost, base[lv])
        generation(k, lv, active)
        for lid in add:
            x = by_id[lid]
            lesson_docs.append({"_id": lid, "run_id": run_id, "game_id": game, "level": x[1], "text": x[3], "kind": x[2],
                                "tags": x[4], "evidence": [{"level": x[1], "rule": x[5]}], "created_gen": gen_ids[k],
                                "created_at": t, "status": "active", "retired_reason": None, "stats": {"recalled": 0}})
        for lid in ret:
            d = next(x for x in lesson_docs if x["_id"] == lid)
            d["status"], d["retired_reason"] = "retired", by_id[lid][7]
        ratio = cost / base[lv]
        harness("generation_created", k, f"generation {k} after level {lv}: {cost} actions against a baseline of {base[lv]} "
                f"({ratio:.2f}x); +{len(add)} lesson(s), {len(ret)} retired, {len(rev)} revised. {why}", add, generation=gen_ids[k], k=k)
        if lv + 1 < len(base):
            items = [{"id": i, "kind": by_id[i][2], "text": by_id[i][3]} for i in active]
            harness("lessons_injected", k, f"{len(active)} lesson(s) written into the workspace CLAUDE.md and sent into the session",
                    list(active), generation=gen_ids[k], k=k, items=items)
            harness("memory_nudge", k, NUDGE.format(n=k))
            ev("user_msg", k, text=NUDGE.format(n=k))
    gens[-1]["status"] = "active"
    for e in events:  # recalled counts as the gateway would have left them
        if e["kind"] == "recall":
            for lid in e["lessons"]:
                next(x for x in lesson_docs if x["_id"] == lid)["stats"]["recalled"] += 1
    if memory:
        notes.append({"_id": f"{run_id}:notes/mechanics.md", "run_id": run_id, "game_id": game, "path": "notes/mechanics.md",
                      "text": "# What this game does\n" + "\n".join(f"- {r}" for r in RULES[game][:max(1, cleared)]),
                      "version": max(1, cleared), "refs": [], "generation": gens[-1]["_id"], "updated_at": t})
    running = status == "running"
    if not running:
        harness("run_ended", cleared, f"{status}: {cleared} of {len(base)} levels cleared")
    levels = [{"level": i, "actions": costs[i] if i < len(costs) else 0, "baseline": b, "cleared": i < cleared,
               "efficiency": round(min(1.0, b / costs[i]), 3) if i < cleared else 0.0} for i, b in enumerate(base)]
    used = sum(costs)
    run = {
        "_id": run_id, "game_id": game, "out_dir": f"/tmp/mongoarc-sample/{run_id}", "label": "levels-sample", "agent": "claude",
        "model": "claude-opus-4-5", "effort": "high", "role": role, "memory_tool": memory,
        "generation": gens[-1]["_id"], "gen_k": gens[-1]["k"], "lessons_used": list(gens[-1]["lessons"]),
        "baseline_actions": base, "status": status, "started_at": t0, "updated_at": t, "ended_at": None if running else t,
        "state": "WIN" if cleared == len(base) else "NOT_FINISHED", "level": cleared, "win_levels": len(base),
        "actions_used": used + 1, "level_costs": list(costs), "gate_pending": None, "deaths": 0, "full_resets": 0,
        "wasted_actions": sum(1 for a in actions if a["wasted"]), "n_actions": len(actions), "last_seq": seq,
        "cost_usd": round(0.9 + used * 0.011, 2), "turns": 40 + used // 3, "result": None,
        "metrics": {"levels": levels, "levels_cleared": cleared, "win_levels": len(base), "actions_total": used,
                    "baseline_total": sum(base), "efficiency": round(sum(x["efficiency"] for x in levels) / len(base), 3),
                    "completion": round(cleared / len(base), 3)}}
    return {D.RUNS: [run], D.ACTIONS: actions, D.EVENTS: events, D.RULEBOOKS: books, D.NOTES: notes,
            D.GENERATIONS: gens, D.LESSONS: lesson_docs}


def main() -> None:
    name = os.environ.get("MONGO_DB", "")
    if not os.environ.get("MONGODB_URI") or "dev" not in name:
        sys.exit("set MONGODB_URI and a MONGO_DB whose name contains 'dev': this script clears the whole database")
    db = D.get_db()
    D.ensure_indexes(db)
    base_ls = [29, 41, 172, 49, 53, 62]
    parts = [
        build(TRAIN, LS20, "train", base_ls, [47, 49, 181, 41, 39, 34], 6, "won", T0, list(range(1, 8)), LS20_DIFFS,
              LS20_LESSONS, {0: 1, 1: 1, 2: 1, 4: 2}, "claude-haiku-4-5"),
        build(CTRL, LS20, "control", base_ls, [51, 78, 268, 96, 265], 4, "finished", T0 + dt.timedelta(minutes=33), [8], {},
              [], {}, "none"),
        build(LIVE, FT09, "train", [18, 24, 37, 45, 52, 66], [31, 22, 9], 2, "running", T0 + dt.timedelta(minutes=65),
              [9, 10, 11], FT09_DIFFS, FT09_LESSONS, {0: 1, 1: 1}, "claude-haiku-4-5"),
    ]
    for coll in (D.RUNS, D.ACTIONS, D.EVENTS, D.RULEBOOKS, D.NOTES, D.GENERATIONS, D.LESSONS):
        db[coll].delete_many({})
        docs = [d for p in parts for d in p[coll]]
        if docs:
            db[coll].insert_many(docs)
        print(f"{name}.{coll}: {len(docs)}")
    # The running sample run has to look alive to the page's "killed game" check.
    db[D.RUNS].update_one({"_id": LIVE}, {"$set": {"updated_at": D.now()}})
    print("runs:", TRAIN, CTRL, LIVE)


if __name__ == "__main__":
    main()
