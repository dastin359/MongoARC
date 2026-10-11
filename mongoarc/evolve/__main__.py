"""python -m mongoarc.evolve {run,status,init,evaluate}"""
from __future__ import annotations

import argparse
import json

from mongoarc import db as D
from mongoarc.evolve import loop
from mongoarc.evolve.evaluate import evaluate_run, level_curve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m mongoarc.evolve",
                                description="Play games one after another, each from an empty memory, and score them.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="play games one after another, each from scratch, and score each")
    r.add_argument("--games", required=True, help="comma-separated game ids")
    r.add_argument("--agent", choices=("fake", "claude"), default="claude")
    r.add_argument("--arc-root", default=None)
    r.add_argument("--label", default="evolve")
    r.add_argument("--runs-root", default=None, help="default runs/<label>")
    # The harness no longer distils lessons (the agent writes its own); accepted and ignored for old command lines.
    r.add_argument("--distiller", choices=("claude", "heuristic"), default="claude", help=argparse.SUPPRESS)
    r.add_argument("--distill-model", default=None, help=argparse.SUPPRESS)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--pause", type=float, default=0.2)
    r.add_argument("--model", default=None)
    r.add_argument("--effort", default=None)
    r.add_argument("--timeout", type=float, default=3600.0)
    r.add_argument("--no-memory", action="store_true", help="control arm: plain CCARC, no memory tool")
    # Cross-game selection is gone (nothing transfers between games); the old flags are accepted and ignored.
    r.add_argument("--margin", type=float, default=None, help=argparse.SUPPRESS)
    r.add_argument("--validate", default=None, help=argparse.SUPPRESS)
    r.add_argument("--val-repeat", type=int, default=None, help=argparse.SUPPRESS)
    s = sub.add_parser("status", help="per run: status, notes saved, and the actions each level cost")
    s.add_argument("--run", default=None)
    s.add_argument("--limit", type=int, default=20, help="most recent runs shown")
    sub.add_parser("init", help="create indexes")
    e = sub.add_parser("evaluate", help="score one run and print its per-level curve")
    e.add_argument("--run", required=True)
    a = p.parse_args(argv)

    if a.cmd == "run":
        if a.validate or a.margin is not None or a.val_repeat is not None:
            print("[evolve] --validate/--margin/--val-repeat are ignored: nothing transfers between games, "
                  "so there is no cross-game generation to validate", flush=True)
        try:
            steps = loop.run([g.strip() for g in a.games.split(",") if g.strip()], agent=a.agent,
                             arc_root=a.arc_root, label=a.label, runs_root=a.runs_root, repeat=a.repeat,
                             pause=a.pause, model=a.model, effort=a.effort, timeout=a.timeout,
                             no_memory=a.no_memory)
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
        print("indexes ensured")
    elif a.cmd == "status":
        print(loop.status(db, a.run, a.limit))
    elif a.cmd == "evaluate":
        out = {"metrics": evaluate_run(db, a.run), "curve": level_curve(db, a.run)}
        print(json.dumps(D.jsonable(out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
