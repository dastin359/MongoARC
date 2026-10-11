"""Play games one after another, each from scratch. Evolution happens inside each game, per level.

There is no cross-game step any more: every launch creates its own generation
lineage and evolves it each time a level is cleared (evolve/levels.py). This
module only sequences the launches, keeps a collector running, and scores each
finished run.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from pymongo.database import Database

from mongoarc import db as D
from mongoarc.evolve import genome
from mongoarc.evolve.coach import harness_event
from mongoarc.evolve.evaluate import evaluate_run, level_curve


def start_collector(db: Database, runs_root: Path, interval: float = 0.3):
    """Tail runs_root in a background thread. Returns (stop_event, thread), or None if unavailable."""
    try:
        from mongoarc.collector import Collector
    except Exception as exc:  # noqa: BLE001
        print(f"[evolve] collector unavailable ({type(exc).__name__}: {exc}); each launch collects for itself",
              flush=True)
        return None
    stop = threading.Event()
    t = threading.Thread(target=Collector(db, runs_root).run_forever, args=(interval, stop), daemon=True,
                         name="mongoarc-collector")
    t.start()
    return stop, t


def _trace_rows(attempt: Path, game: str) -> int:
    try:
        with (attempt / game / "trace.jsonl").open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def wait_ingested(db: Database, run_id: str, attempt: Path, game: str, timeout: float = 30.0,
                  settle: float = 0.7) -> bool:
    """Block until the collector has stored every ledger row of a finished run."""
    want, end = _trace_rows(attempt, game), time.monotonic() + timeout
    while time.monotonic() < end:
        if db[D.ACTIONS].count_documents({"run_id": run_id}) >= want:
            time.sleep(settle)                    # two more passes: a rule book saved after the last row
            return True
        time.sleep(0.3)
    return False


def unusable(db: Database, run_id: str) -> str | None:
    """Why a run says nothing about the harness, or None if it was really played.

    A launch that failed, a run that was interrupted and a run with no action
    are outages, not results, and are not scored.
    """
    run = db[D.RUNS].find_one({"_id": run_id}, {"result": 1, "status": 1}) or {}
    ended = db[D.EVENTS].find_one({"run_id": run_id, "kind": "harness", "what": "run_ended",
                                   "launch_error": {"$exists": True}}, sort=[("seq", -1)])
    if ended:
        return f"the launch failed ({str(ended['launch_error'])[:200]})"
    if run.get("status") in ("running", "stopped"):
        return "the launcher did not finish the run"
    result = run.get("result") or {}
    # CCARC marks interruptions with `error`; a solver that gave up is a result, an interrupted one is not.
    if result.get("error") and not result.get("gave_up"):
        return f"the run was interrupted ({str(result['error'])[:200]})"
    if db[D.ACTIONS].count_documents({"run_id": run_id, "action": {"$ne": "RESET"}}, limit=1) == 0:
        return "no action was played"
    return None


def _stop(proc: subprocess.Popen, grace: float = 45.0) -> None:
    """Interrupt a launcher and give it time to record how its run ended."""
    if proc.poll() is None:
        try:
            proc.wait(timeout=1.0)                # a terminal Ctrl-C reached it already
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=grace)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):   # out of patience, or a second Ctrl-C
        proc.kill()
        proc.wait()


def _play(cmd: list[str], log_path: Path, limit: float) -> int | None:
    """Run one launcher to the end; None if it had to be stopped after `limit` seconds."""
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, cwd=D.REPO, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ))
        try:
            return proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            _stop(proc)
            return None
        except BaseException:                     # Ctrl-C or SIGTERM: never leave the game running behind us
            _stop(proc)
            raise


def _close_run(db: Database, attempt: Path, why: str) -> str | None:
    """After the launcher is gone: a run it left `running` is marked stopped. Returns the run id, if any."""
    try:
        run_id = json.loads((attempt / D.RUN_MARKER).read_text(encoding="utf-8"))["run_id"]
    except (OSError, ValueError, KeyError):
        return None
    r = db[D.RUNS].update_one({"_id": run_id, "status": "running"},
                              {"$set": {"status": "stopped", "ended_at": D.now(), "updated_at": D.now()}})
    if r.modified_count:
        harness_event(db, run_id, "run_ended", f"stopped: {why}", status="stopped", launch_error=why)
    return run_id


def _sigterm(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def _curve_text(curve: list[dict]) -> str:
    return "  ".join(f"L{c['level']}:{c['actions']}/{c['baseline']}"
                     + ("" if c.get("efficiency") is None else f"={c['efficiency']:.2f}") + f"@k{c['k']}"
                     for c in curve) or "(no level cleared)"


def run(games: list[str], *, agent: str = "claude", arc_root: str | None = None, label: str = "evolve",
        runs_root: str | None = None, distiller: str = "claude", repeat: int = 1, pause: float = 0.2,
        model: str | None = None, effort: str | None = None, timeout: float = 3600.0,
        distill_model: str | None = None, no_memory: bool = False, **ignored) -> list[dict]:
    """Play `games` in order, `repeat` times. Each launch is independent and evolves per level by itself.

    Old cross-game options (margin, validate_on, val_repeat) are accepted and ignored.
    """
    db = D.get_db()
    D.ensure_indexes(db)
    root = Path(runs_root or f"runs/{label}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    collector = start_collector(db, root)
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _sigterm) if main_thread else None
    out, attempt = [], None
    launch_opts = ["--agent", agent, "--pause", str(pause), "--distiller", distiller]
    for flag, val in (("--arc-root", arc_root), ("--model", model), ("--effort", effort),
                      ("--distill-model", distill_model)):
        if val:
            launch_opts += [flag, val]
    if no_memory:
        launch_opts += ["--no-memory"]
    try:
        for k in range(repeat):
            for game in games:
                if collector is not None and not collector[1].is_alive():
                    print("[evolve] the collector thread died; restarting it", flush=True)
                    collector = start_collector(db, root)
                n = k
                while True:                       # never resume into an earlier attempt, never share one
                    attempt = root / f"{label}-{game}-{n}"
                    try:
                        attempt.mkdir(parents=True)
                        break
                    except FileExistsError:
                        n += 1
                cmd = [sys.executable, "-m", "mongoarc.launch", "--game", game, "--out-dir", str(attempt),
                       "--label", label, "--timeout", str(timeout)] + launch_opts \
                    + (["--collect"] if collector is None else [])
                print(f"[evolve] playing {game} from scratch in {attempt.name}", flush=True)
                code = _play(cmd, attempt / "driver.log", float(timeout) + 900.0)
                why = "the launcher was stopped after running past its time limit" if code is None \
                    else f"the launcher exited ({code}) without recording an ending"
                run_id, attempt_done = _close_run(db, attempt, why), attempt
                attempt = None
                if run_id is None:
                    print(f"[evolve] {game}: the launcher left no run marker (exit {code}); see "
                          f"{attempt_done / 'driver.log'}", flush=True)
                    out.append({"game": game, "exit": code, "error": f"launch failed (exit {code})"})
                    continue
                problem = unusable(db, run_id)
                if problem is None and not wait_ingested(db, run_id, attempt_done, game):
                    problem = "the collector had not stored the whole run after 30 s"
                if problem is None:
                    try:
                        metrics = evaluate_run(db, run_id)
                    except Exception as exc:  # noqa: BLE001 -- one game's bookkeeping must not end the session
                        problem = f"scoring failed ({type(exc).__name__}: {str(exc)[:200]})"
                if problem is not None:
                    print(f"[evolve] {run_id}: not scored, {problem} (see {attempt_done / 'driver.log'})", flush=True)
                    harness_event(db, run_id, "evolution_skipped", f"this run was not scored: {problem}")
                    out.append({"game": game, "exit": code, "run_id": run_id, "error": problem})
                    continue
                curve = level_curve(db, run_id)
                last = genome.active(db, run_id) or {}
                print(f"[evolve] {run_id}: {metrics['levels_cleared']}/{metrics['win_levels']} levels, "
                      f"{metrics['actions_total']} actions, efficiency {metrics['efficiency']}, "
                      f"generation k={last.get('k')} ({len(last.get('lessons') or [])} lessons)  "
                      f"{_curve_text(curve)}", flush=True)
                out.append({"game": game, "exit": code, "run_id": run_id, "metrics": metrics, "curve": curve,
                            "gen_k": last.get("k"), "lessons": last.get("lessons") or []})
    except KeyboardInterrupt:
        if attempt is not None:                   # interrupted mid-game: the launcher has been stopped by now
            _close_run(db, attempt, "the evolve loop was interrupted")
        raise
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if collector is not None:
            collector[0].set()
            collector[1].join(timeout=10)
    return out


def status(db: Database, run_id: str | None = None, limit: int = 20) -> str:
    """Per run: its generation lineage (k, level, lessons, what the level played under it cost) and its lessons."""
    q = {"_id": run_id} if run_id else {}
    runs = list(db[D.RUNS].find(q, {"game_id": 1, "status": 1, "role": 1, "started_at": 1, "gen_k": 1,
                                    "metrics.efficiency": 1}).sort("started_at", -1).limit(limit))[::-1]
    fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
    lines = []
    for r in runs:
        eff = ((r.get("metrics") or {}).get("efficiency"))
        lines.append(f"run {r['_id']}  game {r.get('game_id')}  {r.get('status')}  role {r.get('role') or '-'}  "
                     f"efficiency {fmt(eff)}")
        gens = genome.lineage(db, r["_id"])
        if not gens:
            lines.append("    (no generations: launched before per-level evolution)")
        else:
            lines.append(f"    {'k':>2} {'gen':>4} {'after':>5} {'status':10} {'lessons':>7} {'plays':>5} "
                         f"{'actions':>7} {'base':>5} {'eff':>6}  {'distiller':9}  diff")
        for g in gens:
            d, pl = g.get("diff") or {}, g.get("played") or {}
            diff = " ".join(x for x in (
                f"+{','.join(d.get('added_lessons') or [])}" if d.get("added_lessons") else "",
                f"-{','.join(d.get('retired_lessons') or [])}" if d.get("retired_lessons") else "",
                f"~{','.join(d.get('revised_lessons') or [])}" if d.get("revised_lessons") else "",
                " ".join(f"{k}:{a}->{b}" for k, (a, b) in (d.get("knob_changes") or {}).items())) if x)
            show = lambda v: "-" if v is None else str(v)  # noqa: E731
            lines.append(f"    {g.get('k', 0):>2} {g['_id']:>4} "
                         f"{'-' if g.get('level') is None else 'L' + str(g['level']):>5} {g.get('status', ''):10} "
                         f"{len(g.get('lessons') or []):>7} {('L' + str(pl['level'])) if pl else '-':>5} "
                         f"{show(pl.get('actions')):>7} {show(pl.get('baseline')):>5} {fmt(pl.get('efficiency')):>6}  "
                         f"{(g.get('distiller') or {}).get('kind', ''):9}  {diff or '(no change)'}")
        for l in db[D.LESSONS].find({"run_id": r["_id"]}).sort("_id", 1):
            lines.append(f"    {l['_id']:6} L{l.get('level')} {l.get('kind', ''):15} {l.get('status', ''):8} "
                         f"recalled {int((l.get('stats') or {}).get('recalled') or 0)}  {l.get('text', '')[:120]}")
        lines.append("")
    return "\n".join(lines) if lines else "no runs"
