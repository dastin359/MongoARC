#!/usr/bin/env python3
"""Dev-only sample data for the live view, in the shape of the memory design. Everything here is INVENTED.

The agent's memory is a MongoDB vector database it writes itself with memory.py; the harness only launches,
records, serves the memory and prompts the agent at level boundaries. Three runs:

  mem-ls20-...  role train, won, 6 of 6 levels: 9 notes saved across the levels (N1 corrected to version 3,
                N4 to version 2, N2 deleted on level 3), a memory_write event per save/update/delete,
                7 recall events, 5 harness memory_nudge events
  ctl-ls20-...  role control (memory_tool false), same game, 4 of 6 levels, no memory at all
  mem-ft09-...  role train, a different game, running, on level 2

Action rows carry no boards (the page's counters need the rows, not the frames). The whole database named by
MONGO_DB is cleared first, so the name must say it is a dev database.
usage: MONGODB_URI=mongodb://127.0.0.1:27117/?directConnection=true MONGO_DB=mongoarc_dev_view2 python3 tests/seed_memory_runs.py
"""
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mongoarc import db as D

T0 = dt.datetime(2026, 10, 11, 21, 15, tzinfo=dt.timezone.utc)
LS20, FT09 = "ls20-016295f7601e", "ft09-b8377d4b7815"
TRAIN, CTRL, LIVE = f"mem-{LS20}-20261011T211500", f"ctl-{LS20}-20261011T214800", f"mem-{FT09}-20261011T222000"
NUDGE = ("Level {p} cleared in {a} actions (human baseline {b}). Save what you learned to memory before you go on "
         "(python3 memory.py save \"<title>\" \"<text>\"), then search it for level {n}: python3 memory.py recall \"<question>\".")

# Per level, in order: ("save", n, title, text, refs) | ("update", n, text) | ("delete", n) | ("recall", question, [n, ...])
LS20_MEMORY = {
    0: [("save", 1, "How the block moves", "ACTION1 to ACTION4 move the block one cell up, down, left and right.", []),
        ("save", 2, "Probe every action first", "At the start of a level press each available action once before planning a route.", [])],
    1: [("recall", "what do the four actions do and what stops the block", [1, 2]),
        ("update", 1, "ACTION1 to ACTION4 move the block one cell up, down, left and right. A move into a wall changes nothing and still costs an action."),
        ("save", 3, "The bar at the top is a step counter", "The bar loses one segment per action and the level restarts when it is empty. A purple cell refills it, once per level.", []),
        ("save", 4, "Key first, then the door", "Walk to the key-shaped cell first, then straight to the door.", [])],
    2: [("recall", "how many moves do I get before the level restarts", [3, 1]),
        ("recall", "what opens the door", [4, 3]),
        ("save", 5, "The key must match the door", "The key drawn in the bottom-left corner must match the door in shape and colour. Stepping on the door with the wrong key costs the action and nothing happens.", [])],
    3: [("recall", "the door did not open, why", [5, 4, 3]),
        ("delete", 2),
        ("save", 6, "Plan the whole route before moving", "Count the steps of the route against the segments left on the bar; go through the refill cell when the count is short.", ["route.py"])],
    4: [("recall", "how do I change the shape or colour of the key", [5, 4]),
        ("update", 4, "Walk to the key-shaped cell first. If the key does not match the door yet, turn it on a pad before going to the door."),
        ("save", 7, "Pads change the key", "The cross-shaped pad turns the key a quarter turn each time the block steps on it; the square pad cycles its colour.", []),
        ("update", 1, "ACTION1 to ACTION4 move the block one cell up, down, left and right. A move into a wall changes nothing and still costs an action. The block slides over pads without stopping."),
        ("save", 8, "Shortest route script", "route.py finds the shortest route by breadth-first search over the parsed grid. Run it at the start of a level instead of reading the grid by eye.", ["route.py", "parse_grid.py"])],
    5: [("recall", "fastest way to find a route through the walls", [8, 6, 3]),
        ("recall", "which pad rotates the key and which one recolours it", [7, 5]),
        ("save", 9, "Order of a level", "Parse the grid, read the door, fix the key on the pads, refill if the count is short, then walk the route from route.py.", ["route.py"])],
}
FT09_MEMORY = {
    0: [("save", 1, "A click cycles the colour", "ACTION6 on a cell cycles its colour through three states. The level is cleared when the cells around the centre match the pattern in the corner.", [])],
    1: [("recall", "what does clicking a cell do", [1]),
        ("save", 2, "Clicks flip the neighbours from level 1", "From level 1 a click also flips the four neighbours of the cell: work from the top row down so finished rows are not disturbed.", []),
        ("save", 3, "Count before clicking", "Count the wrong cells before the first click and solve on paper; every stray click has to be undone with two more.", ["count_cells.py"])],
    2: [("recall", "in what order should I click so finished rows stay finished", [2, 3])],
}


def build(run_id, game, role, base, costs, cleared, status, t0, memory_plan):
    """The documents of one sample run, keyed by collection. costs[i] = actions spent on level i."""
    memory = role != "control"
    actions, events, notes = [], [], {}
    seq, n, t = 0, -1, t0

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

    def nid(k):
        return f"{run_id}:mem/{k}"

    def memory_step(level, step):
        op = step[0]
        if op == "recall":
            _, question, hits = step
            hits = [k for k in hits if nid(k) in notes]
            ev("tool_use", level, tool="Bash", tool_use_id=f"tu{seq}", command=f'python3 memory.py recall "{question}"',
               description="Search this game's memory")
            ev("recall", level, query=question, notes=[nid(k) for k in hits], engine="atlas_vector_search",
               text=f"agent searched its memory for {question!r}: {len(hits)} note(s) came back")
            return
        k = step[1]
        if op == "save":
            _, _, title, text, refs = step
            notes[nid(k)] = {"_id": nid(k), "run_id": run_id, "game_id": game, "level": level, "title": title, "text": text,
                             "refs": refs, "source": "tool", "version": 1, "revisions": [], "created_at": t, "updated_at": t}
            ev("tool_use", level, tool="Bash", tool_use_id=f"tu{seq}", command=f'python3 memory.py save "{title}" "{text}"',
               description="Save a lesson to memory")
            ev("memory_write", level, op="save", note=nid(k), title=title, text=text)
        elif op == "update":
            d = notes[nid(k)]
            d["revisions"].append({"version": d["version"], "text": d["text"], "at": d["updated_at"]})
            d["version"] += 1
            d["text"] = step[2]
            ev("tool_use", level, tool="Bash", tool_use_id=f"tu{seq}", command=f'python3 memory.py update mem/{k} "{step[2]}"',
               description="Correct a note in memory")
            d["updated_at"] = t
            ev("memory_write", level, op="update", note=nid(k), title=d["title"], text=step[2])
        else:
            d = notes.pop(nid(k))
            ev("tool_use", level, tool="Bash", tool_use_id=f"tu{seq}", command=f"python3 memory.py delete mem/{k}",
               description="Delete a note that turned out wrong")
            ev("memory_write", level, op="delete", note=nid(k), title=d["title"], text=d["text"])

    ev("system", 0, subtype="init", text=f"session started in workspace for {game}")
    act("RESET", 0)
    ev("harness", 0, what="run_started", text=f"{game}: memory is empty" if memory else f"{game}: control run, no memory tool")
    for lv, cost in enumerate(costs):
        done = lv < cleared
        plan = memory_plan.get(lv, [])
        for step in [s for s in plan if s[0] == "recall"]:  # searches come first, before the level's actions
            memory_step(lv, step)
        ev("thinking", lv, text="Reading the board before spending actions: where is the block, where is the goal, what is new on this level.")
        ev("text", lv, text=f"Level {lv}: planning the route, then playing it.")
        writes = [s for s in plan if s[0] != "recall"]
        for i in range(cost):
            last = done and i == cost - 1
            win = last and lv == len(base) - 1
            if not last and writes and i == max(1, cost - 2):  # the level's lessons, written just before it is cleared
                for step in writes:
                    memory_step(lv, step)
                writes = []
            act(f"ACTION{1 + (i * 7 + lv) % 4}", lv + 1 if last else lv, "WIN" if win else "NOT_FINISHED", last, wasted=(i % 9 == 4))
        if not done:
            ev("text", lv, text=f"Level {lv}: {cost} actions so far.")
            break
        if memory and lv + 1 < len(base):
            text = NUDGE.format(p=lv, a=cost, b=base[lv], n=lv + 1)
            ev("harness", lv + 1, what="memory_nudge", text=text)
    running = status == "running"
    if not running:
        ev("harness", cleared, what="run_ended", text=f"{status}: {cleared} of {len(base)} levels cleared")
    levels = [{"level": i, "actions": costs[i] if i < len(costs) else 0, "baseline": b, "cleared": i < cleared,
               "efficiency": round(min(1.0, b / costs[i]), 3) if i < cleared else 0.0} for i, b in enumerate(base)]
    used = sum(costs)
    run = {
        "_id": run_id, "game_id": game, "out_dir": f"/tmp/mongoarc-sample/{run_id}", "label": "memory-sample", "agent": "claude",
        "model": "claude-opus-4-5", "effort": "high", "role": role, "memory_tool": memory,
        "baseline_actions": base, "status": status, "started_at": t0, "updated_at": t, "ended_at": None if running else t,
        "state": "WIN" if cleared == len(base) else "NOT_FINISHED", "level": cleared, "win_levels": len(base),
        "actions_used": used + 1, "level_costs": list(costs), "deaths": 0, "full_resets": 0,
        "wasted_actions": sum(1 for a in actions if a["wasted"]), "n_actions": len(actions), "last_seq": seq,
        "cost_usd": round(0.9 + used * 0.011, 2), "turns": 40 + used // 3, "result": None,
        "metrics": {"levels": levels, "levels_cleared": cleared, "win_levels": len(base), "actions_total": used,
                    "baseline_total": sum(base), "efficiency": round(sum(x["efficiency"] for x in levels) / len(base), 3),
                    "completion": round(cleared / len(base), 3)}}
    return {D.RUNS: [run], D.ACTIONS: actions, D.EVENTS: events, D.NOTES: list(notes.values())}


def main() -> None:
    name = os.environ.get("MONGO_DB", "")
    if not os.environ.get("MONGODB_URI") or "dev" not in name:
        sys.exit("set MONGODB_URI and a MONGO_DB whose name contains 'dev': this script clears the whole database")
    db = D.get_db()
    D.ensure_indexes(db)
    base_ls = [29, 41, 172, 49, 53, 62]
    parts = [
        build(TRAIN, LS20, "train", base_ls, [47, 49, 181, 41, 39, 34], 6, "won", T0, LS20_MEMORY),
        build(CTRL, LS20, "control", base_ls, [51, 78, 268, 96, 265], 4, "finished", T0 + dt.timedelta(minutes=33), {}),
        build(LIVE, FT09, "train", [18, 24, 37, 45, 52, 66], [31, 22, 9], 2, "running", T0 + dt.timedelta(minutes=65), FT09_MEMORY),
    ]
    # The earlier design's collections are emptied too: this sample holds no generations, lessons or rule books.
    for coll in (D.RUNS, D.ACTIONS, D.EVENTS, D.NOTES, D.RULEBOOKS, D.GENERATIONS, D.LESSONS):
        db[coll].delete_many({})
        docs = [d for p in parts for d in p.get(coll, [])]
        if docs:
            db[coll].insert_many(docs)
        print(f"{name}.{coll}: {len(docs)}")
    # The running sample run has to look alive to the page's "killed game" check.
    db[D.RUNS].update_one({"_id": LIVE}, {"$set": {"updated_at": D.now()}})
    for rid in (TRAIN, CTRL, LIVE):
        kinds: dict = {}
        for e in db[D.EVENTS].find({"run_id": rid, "kind": {"$in": ["memory_write", "recall", "harness"]}}):
            k = e["kind"] if e["kind"] != "harness" else "harness:" + e["what"]
            k = k + ":" + e["op"] if e["kind"] == "memory_write" else k
            kinds[k] = kinds.get(k, 0) + 1
        print(rid, "notes", db[D.NOTES].count_documents({"run_id": rid}), kinds)


if __name__ == "__main__":
    main()
