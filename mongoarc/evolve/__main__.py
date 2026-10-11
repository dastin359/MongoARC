"""python -m mongoarc.evolve {run,status,init,distill,evaluate}"""
from __future__ import annotations

import argparse
import json

from mongoarc import db as D
from mongoarc.evolve import genome, loop
from mongoarc.evolve.distill import distill
from mongoarc.evolve.evaluate import credit, evaluate_run, update_fitness


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mongoarc.evolve", description="The self-improving harness loop.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play games in order, evolving the harness after each")
    r.add_argument("--games", required=True, help="comma-separated game ids")
    r.add_argument("--agent", choices=("fake", "claude"), default="claude")
    r.add_argument("--arc-root", default=None)
    r.add_argument("--label", default="evolve")
    r.add_argument("--runs-root", default=None, help="default runs/<label>")
    r.add_argument("--distiller", choices=("claude", "heuristic"), default="claude")
    r.add_argument("--distill-model", default=None, help="default $MONGOARC_DISTILL_MODEL or claude-opus-5")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--pause", type=float, default=0.2)
    r.add_argument("--model", default=None)
    r.add_argument("--effort", default=None)
    r.add_argument("--timeout", type=float, default=3600.0)
    r.add_argument("--margin", type=float, default=loop.REVERT_MARGIN)
    sub.add_parser("status", help="generations, fitness and lessons")
    sub.add_parser("init", help="create indexes and generation 0")
    d = sub.add_parser("distill", help="build the next generation from one run")
    d.add_argument("--run", required=True)
    d.add_argument("--kind", choices=("claude", "heuristic"), default="claude")
    d.add_argument("--model", default=None)
    e = sub.add_parser("evaluate", help="score one run and refresh its generation's fitness")
    e.add_argument("--run", required=True)
    a = p.parse_args(argv)

    if a.cmd == "run":
        try:
            steps = loop.run([g.strip() for g in a.games.split(",") if g.strip()], agent=a.agent,
                             arc_root=a.arc_root, label=a.label, runs_root=a.runs_root, distiller=a.distiller,
                             repeat=a.repeat, pause=a.pause, model=a.model, effort=a.effort, timeout=a.timeout,
                             margin=a.margin, distill_model=a.distill_model)
        except KeyboardInterrupt:
            print("[evolve] interrupted; the game in flight was stopped and its run marked", flush=True)
            print(loop.status(D.get_db()))
            return 130
        print(loop.status(D.get_db()))
        failed = [s for s in steps if "error" in s]
        for s in failed:
            print(f"[evolve] not evolved: {s['game']} ({s.get('run_id', 'no run')}): {s['error']}")
        return 0 if steps and not failed else 1
    db = D.get_db()
    D.ensure_indexes(db)
    if a.cmd == "init":
        g = genome.ensure_gen0(db)
        print(f"indexes ensured; generation 0 is {g['status']}; active generation {genome.active(db)['_id']}")
    elif a.cmd == "status":
        print(loop.status(db))
    elif a.cmd == "evaluate":
        m = evaluate_run(db, a.run)
        gen = (db[D.RUNS].find_one({"_id": a.run}, {"generation": 1}) or {}).get("generation")
        out = {"metrics": m, "credited": credit(db, a.run),
               "fitness": update_fitness(db, gen) if gen is not None else None}
        print(json.dumps(D.jsonable(out), indent=2))
    elif a.cmd == "distill":
        g = distill(db, a.run, kind=a.kind, model=a.model)
        g.pop("addendum", None)
        g["tools"] = [t["name"] for t in g.get("tools") or []]
        print(json.dumps(D.jsonable(g), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
