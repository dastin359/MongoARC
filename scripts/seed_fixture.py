#!/usr/bin/env python3
"""Load one finished run directory into MongoDB in a single pass (no tailing).

A reference implementation of docs/SCHEMA.md, used to give the view and the
evolution loop data to develop against. The real ingester is mongoarc.collector.
usage: MONGODB_URI=... MONGO_DB=... scripts/seed_fixture.py <out_dir> [run_id] [generation]
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mongoarc import db as D

out = Path(sys.argv[1]).resolve()
ws = next(p for p in out.iterdir() if p.is_dir() and (p / "trace.jsonl").exists())
game = ws.name
run_id = sys.argv[2] if len(sys.argv) > 2 else f"{out.name}-{game}"
gen = int(sys.argv[3]) if len(sys.argv) > 3 else None
d = D.get_db(); D.ensure_indexes(d)
for c in (D.ACTIONS, D.EVENTS, D.RULEBOOKS):
    d[c].delete_many({"run_id": run_id})
d[D.RUNS].delete_one({"_id": run_id})
state = json.loads((ws / "trace.state.json").read_text())
result = json.loads((ws / "result.json").read_text()) if (ws / "result.json").exists() else None
t0 = D.now()
d[D.RUNS].insert_one({"_id": run_id, "game_id": game, "out_dir": str(out), "label": "seed", "agent": "fake",
    "model": "fake", "effort": None, "generation": gen, "lessons_used": [], "baseline_actions": [10, 12, 14],
    "status": "running", "started_at": t0, "updated_at": t0, "ended_at": None, "last_seq": 0, "n_actions": 0})
prev, n, acts = None, 0, []
for line in (ws / "trace.jsonl").read_text().splitlines():
    r = json.loads(line)
    lvl = r["level"]
    acts.append({"_id": f"{run_id}:{n}", "run_id": run_id, "n": n, "i": r["i"], "seq": D.next_seq(d, run_id),
        "level": lvl, "action": r["action"], "params": r.get("params") or {}, "state": r["state"],
        "full_reset": bool(r.get("full_reset")), "available_actions": r.get("available_actions") or [],
        "frames": [D.pack_grid(g) for g in r["frames"]],
        "level_cleared": prev is not None and lvl > prev["level"],
        "new_play": bool(r.get("full_reset")) or (prev is not None and lvl < prev["level"]),
        "death": r["state"] == "GAME_OVER" and (prev is None or prev["state"] != "GAME_OVER"),
        "wasted": not r["frames"], "observed_at": D.now()})
    prev, n = r, n + 1
d[D.ACTIONS].insert_many(acts)
def ev(kind, **kw):
    seq = D.next_seq(d, run_id)
    d[D.EVENTS].insert_one({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": D.now(), "kind": kind,
                            "action_n": n - 1, "level": state["level"], **kw})
for line in (ws / "stream.jsonl").read_text().splitlines():
    try: e = json.loads(line)
    except ValueError: continue
    msg = e.get("message") or {}
    for b in msg.get("content") or [] if isinstance(msg.get("content"), list) else []:
        t = b.get("type")
        if t == "thinking": ev("thinking", text=b.get("thinking", ""))
        elif t == "text": ev("user_msg" if e["type"] == "user" else "text", text=b.get("text", ""))
        elif t == "tool_use":
            i = b.get("input") or {}
            ev("tool_use", tool=b.get("name"), tool_use_id=b.get("id"), command=i.get("command") or json.dumps(i)[:2000], description=i.get("description"))
        elif t == "tool_result":
            c = b.get("content"); c = c if isinstance(c, str) else "\n".join(x.get("text", "") for x in c or [])
            ev("tool_result", tool_use_id=b.get("tool_use_id"), text=c[:4000], is_error=bool(b.get("is_error")))
    if e.get("type") == "result":
        ev("result", cost_usd=e.get("total_cost_usd"), turns=e.get("num_turns"), duration_ms=e.get("duration_ms"), is_error=e.get("is_error"), text=str(e.get("result"))[:2000])
ob = out / "harness_outbox.jsonl"
for line in ob.read_text().splitlines() if ob.exists() else []:
    o = json.loads(line); ev("outbox", outbox_kind=o.get("kind"), text=o.get("text"))
if (ws / "rules.json").exists():
    rb = json.loads((ws / "rules.json").read_text())
    d[D.RULEBOOKS].insert_one({"_id": f"{run_id}:1", "run_id": run_id, "version": 1, "at": D.now(), "action_n": n - 1,
        "level": state["level"], **{k: rb.get(k, []) for k in ("verified", "refuted", "open_questions")},
        "added": {k: rb.get(k, []) for k in ("verified", "refuted", "open_questions")}})
    ev("rulebook", version=1, added={k: rb.get(k, []) for k in ("verified", "refuted", "open_questions")})
won = state.get("state") == "WIN"
d[D.RUNS].update_one({"_id": run_id}, {"$set": {"status": "won" if won else "finished", "ended_at": D.now(), "updated_at": D.now(),
    "state": state.get("state"), "level": state.get("level"), "win_levels": state.get("win_levels"),
    "actions_used": state.get("actions_used"), "level_costs": state.get("level_costs"), "gate_pending": state.get("gate_pending"),
    "deaths": (result or {}).get("deaths", 0), "full_resets": state.get("full_resets", 0), "wasted_actions": state.get("wasted_actions", 0),
    "n_actions": n, "cost_usd": (result or {}).get("cost_usd"), "turns": (result or {}).get("turns"), "result": result}})
print(run_id, "actions", n, "events", d[D.EVENTS].count_documents({"run_id": run_id}))
