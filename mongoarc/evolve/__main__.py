"""python -m mongoarc.evolve {run,status,init,distill,evaluate}"""
from __future__ import annotations

import argparse
import json

from mongoarc import db as D
from mongoarc.evolve import genome, loop
from mongoarc.evolve.distill import distill_level, level_metrics
from mongoarc.evolve.evaluate import evaluate_run, level_curve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mongoarc.evolve",
                                description="The harness that improves itself level by level inside each game.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play games one after another, each from scratch; each evolves per level")
    r.add_argument("--games", required=True, help="comma-separated game ids")
    r.add_argument("--agent", choices=("fake", "claude"), default="claude")
    r.add_argument("--arc-root", default=None)
    r.add_argument("--label", default="evolve")
    r.add_argument("--runs-root", default=None, help="default runs/<label>")
    r.add_argument("--distiller", choices=("claude", "heuristic"), default="claude")
    r.add_argument("--distill-model", default=None, help="default $MONGOARC_DISTILL_MODEL or claude-sonnet-5-5")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--pause", type=float, default=0.2)
    r.add_argument("--model", default=None)
    r.add_argument("--effort", default=None)
    r.add_argument("--timeout", type=float, default=3600.0)
    r.add_argument("--no-memory", action="store_true", help="control arm: no memory tool, no level lessons")
    # Cross-game selection is gone (nothing transfers between games); the old flags are accepted and ignored.
    r.add_argument("--margin", type=float, default=None, help=argparse.SUPPRESS)
    r.add_argument("--validate", default=None, help=argparse.SUPPRESS)
    r.add_argument("--val-repeat", type=int, default=None, help=argparse.SUPPRESS)
    s = sub.add_parser("status", help="per run: its generations (k, level, lessons, played efficiency)")
    s.add_argument("--run", default=None)
    s.add_argument("--limit", type=int, default=20, help="most recent runs shown")
    sub.add_parser("init", help="create indexes")
    d = sub.add_parser("distill", help="distil one cleared level of a run into its next generation (by hand)")
    d.add_argument("--run", required=True)
    d.add_argument("--level", type=int, required=True)
    d.add_argument("--kind", choices=("claude", "heuristic"), default="claude")
    d.add_argument("--model", default=None)
    e = sub.add_parser("evaluate", help="score one run and print its per-level curve")
    e.add_argument("--run", required=True)
    a = p.parse_args(argv)

    if a.cmd == "run":
        if a.validate or a.margin is not None or a.val_repeat is not None:
            print("[evolve] --validate/--margin/--val-repeat are ignored: nothing transfers between games, "
                  "so there is no cross-game generation to validate", flush=True)
        try:
            steps = loop.run([g.strip() for g in a.games.split(",") if g.strip()], agent=a.agent,
                             arc_root=a.arc_root, label=a.label, runs_root=a.runs_root, distiller=a.distiller,
                             repeat=a.repeat, pause=a.pause, model=a.model, effort=a.effort, timeout=a.timeout,
                             distill_model=a.distill_model, no_memory=a.no_memory)
        except KeyboardInterrupt:
            print("[evolve] interrupted; the game in flight was stopped and its run marked", flush=True)
            return 130
        db = D.get_db()
        for st in steps:
            if st.get("run_id"):
                print(loop.status(db, st["run_id"]))
        failed = [s for s in steps if "error" in s]
        for s in failed:
            print(f"[evolve] not scored: {s['game']} ({s.get('run_id', 'no run')}): {s['error']}")
        return 0 if steps and not failed else 1
    db = D.get_db()
    D.ensure_indexes(db)
    if a.cmd == "init":
        print("indexes ensured; generations are created per run at launch")
    elif a.cmd == "status":
        print(loop.status(db, a.run, a.limit))
    elif a.cmd == "evaluate":
        out = {"metrics": evaluate_run(db, a.run), "curve": level_curve(db, a.run)}
        print(json.dumps(D.jsonable(out), indent=2))
    elif a.cmd == "distill":
        run = db[D.RUNS].find_one({"_id": a.run}) or {}
        costs, base = run.get("level_costs") or [], run.get("baseline_actions") or []
        m = level_metrics(a.level, costs[a.level] if a.level < len(costs) else None,
                          base[a.level] if a.level < len(base) else None)
        parent = genome.active(db, a.run) or genome.ensure_run_gen0(db, a.run, run.get("game_id") or "")
        db[D.GENERATIONS].update_one({"_id": parent["_id"]}, {"$set": {"played": m}})
        g = distill_level(db, a.run, a.level, m, kind=a.kind, model=a.model)
        g.pop("addendum", None)
        print(json.dumps(D.jsonable(g), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
