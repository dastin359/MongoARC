"""The evolution loop: play a game, score it, select, distil the next generation, repeat."""
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
from pymongo.errors import OperationFailure

from mongoarc import db as D
from mongoarc.evolve import genome
from mongoarc.evolve.coach import harness_event
from mongoarc.evolve.distill import distill
from mongoarc.evolve.evaluate import credit, evaluate_run, update_fitness

REVERT_MARGIN = 0.05
_LAUNCHER_FIELDS = ("label", "agent", "model", "effort", "generation", "lessons_used", "baseline_actions",
                    "started_at", "status", "ended_at", "result", "out_dir")


def start_collector(db: Database, runs_root: Path, interval: float = 0.3):
    """Tail runs_root in a background thread. Returns (stop_event, thread), or None if unavailable."""
    try:
        from mongoarc.collector import Collector
    except Exception as exc:  # noqa: BLE001 -- the module is written in parallel; fall back to one-pass seeding
        print(f"[evolve] collector unavailable ({type(exc).__name__}: {exc}); seeding each run after it ends",
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
    """Why a run is no evidence about the harness, or None if it was really played.

    A launch that failed, a run that was interrupted and a run with no action
    say nothing about the generation they ran under. Scoring them would revert
    that generation and mark its lessons down for an outage.
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


def seed_after(db: Database, run_id: str, attempt: Path, gen: int) -> None:
    """Fallback ingest: load the finished run with scripts/seed_fixture.py, keeping the launcher's fields."""
    keep = db[D.RUNS].find_one({"_id": run_id}) or {}
    events = list(db[D.EVENTS].find({"run_id": run_id, "kind": "harness"}).sort("seq", 1))
    subprocess.run([sys.executable, str(D.REPO / "scripts" / "seed_fixture.py"), str(attempt), run_id, str(gen)],
                   cwd=D.REPO, check=True, stdout=subprocess.DEVNULL)
    db[D.RUNS].update_one({"_id": run_id}, {"$set": {k: keep[k] for k in _LAUNCHER_FIELDS if k in keep}})
    for e in events:                              # the seeder clears the timeline; put harness events back
        seq = D.next_seq(db, run_id)
        db[D.EVENTS].insert_one({**e, "_id": f"{run_id}:{seq}", "seq": seq})


def _efficiency_by_game(db: Database, gen: int) -> dict[str, list[float]]:
    """Efficiency of every scored run of a generation, grouped by game."""
    out: dict[str, list[float]] = {}
    for r in db[D.RUNS].find({"generation": gen, "metrics.efficiency": {"$ne": None}},
                             {"game_id": 1, "metrics.efficiency": 1}):
        out.setdefault(r.get("game_id"), []).append(float(r["metrics"]["efficiency"]))
    return out


def _undo_lessons(db: Database, gen: dict, why: str, session=None) -> None:
    """Put the lessons back as they were before `gen`: what it added goes, what it retired or reworded returns."""
    d, gid = gen.get("diff") or {}, gen["_id"]
    db[D.LESSONS].update_many(
        {"_id": {"$in": d.get("added_lessons") or []}, "status": "active"},
        {"$set": {"status": "retired", "retired_gen": gid,
                  "retired_reason": f"introduced by generation {gid}, which was reverted: {why}"[:300]}},
        session=session)
    db[D.LESSONS].update_many(
        {"_id": {"$in": d.get("retired_lessons") or []}, "retired_gen": gid},
        {"$set": {"status": "active", "retired_reason": None}, "$unset": {"retired_gen": ""}}, session=session)
    for lid in d.get("revised_lessons") or []:
        l = db[D.LESSONS].find_one({"_id": lid}, {"text": 1, "revisions": 1}, session=session) or {}
        was = next((r for r in reversed(l.get("revisions") or [])
                    if r.get("gen") == gid and not r.get("reverted")), None)
        if was:
            db[D.LESSONS].update_one({"_id": lid}, {"$set": {"text": was["was"]}, "$push": {
                "revisions": {"gen": gid, "was": l.get("text"), "reverted": True}}}, session=session)


def select(db: Database, margin: float = REVERT_MARGIN) -> dict | None:
    """Revert the active generation if it scores under its parent by more than `margin`.

    Like is compared with like: only games both generations have played count.
    Games differ far more in difficulty than `margin`, so with no game in
    common there is no evidence either way and nothing is reverted.
    Reverting undoes the generation's lesson changes as well, so the parent
    comes back exactly as it was and nothing the reverted generation added
    stays active.
    """
    gen = genome.active(db)
    if gen.get("parent") is None or not (gen.get("fitness") or {}).get("runs"):
        return None
    parent = db[D.GENERATIONS].find_one({"_id": gen["parent"]})
    if parent is None:
        return None
    # Held-out validation runs, when there are any, are the only fair comparison: training runs are on
    # different games per generation and control runs are a different arm.
    mine = _efficiency_by_game_role(db, gen["_id"], "validation") or _efficiency_by_game(db, gen["_id"])
    theirs = (_efficiency_by_game_role(db, parent["_id"], "validation")
              or _efficiency_by_game(db, parent["_id"]))
    shared = sorted(g for g in mine if g in theirs)
    if not shared:
        return None
    mean = lambda by: sum(sum(by[g]) / len(by[g]) for g in shared) / len(shared)  # noqa: E731 -- per game, then games
    a, b = round(mean(mine), 4), round(mean(theirs), 4)
    if a >= b - margin:
        return None
    why = (f"mean efficiency {a} against parent generation {parent['_id']}'s {b} on "
           f"{', '.join(str(g) for g in shared)} (margin {margin})")

    def swap(session=None):
        db[D.GENERATIONS].update_one({"_id": gen["_id"]}, {"$set": {
            "status": "reverted", "reverted_at": D.now(), "reverted_reason": why}}, session=session)
        db[D.GENERATIONS].update_many({"status": "active"}, {"$set": {"status": "superseded"}}, session=session)
        db[D.GENERATIONS].update_one({"_id": parent["_id"]}, {"$set": {"status": "active"}}, session=session)
        _undo_lessons(db, gen, why, session)

    try:
        with db.client.start_session() as s:
            s.with_transaction(swap)
    except OperationFailure as exc:
        if exc.code != 20:                       # 20: transactions need a replica set
            raise
        swap()
    return {"reverted": gen["_id"], "reactivated": parent["_id"], "why": why, "games": shared}


def validate(db: Database, gen: int, games: list[str], root: Path, label: str, repeat: int,
             launch_opts: list[str], timeout: float) -> list[str]:
    """Score generation `gen` on the held-out games it has not yet played `repeat` times.

    Validation runs are evaluated and nothing else: they are never distilled
    and never reach later games' memory, so the games stay held out. Returns
    the ids of the runs played.
    """
    played = []
    for game in games:
        have = len(_efficiency_by_game_role(db, gen, "validation").get(game, []))
        for k in range(have, repeat):
            n = k
            while True:
                attempt = root / f"{label}-val-g{gen:02d}-{game}-{n}"
                try:
                    attempt.mkdir(parents=True)
                    break
                except FileExistsError:
                    n += 1
            cmd = [sys.executable, "-m", "mongoarc.launch", "--game", game, "--out-dir", str(attempt), "--gen", str(gen),
                   "--label", f"{label}-val", "--role", "validation", "--timeout", str(timeout)] + launch_opts
            print(f"[evolve] generation {gen}: validating on held-out {game} ({k + 1}/{repeat})", flush=True)
            code = _play(cmd, attempt / "driver.log", float(timeout) + 600.0)
            run_id = _close_run(db, attempt, f"the launcher exited ({code}) without recording an ending")
            if run_id is None or unusable(db, run_id) or not wait_ingested(db, run_id, attempt, game):
                print(f"[evolve] validation run on {game} unusable; skipped", flush=True)
                continue
            m = evaluate_run(db, run_id)
            update_fitness(db, gen)
            played.append(run_id)
            print(f"[evolve] {run_id}: held-out {m['levels_cleared']}/{m['win_levels']} levels, "
                  f"{m['actions_total']} actions, efficiency {m['efficiency']}", flush=True)
    return played


def _efficiency_by_game_role(db: Database, gen: int, role: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for r in db[D.RUNS].find({"generation": gen, "role": role, "metrics.efficiency": {"$ne": None}},
                             {"game_id": 1, "metrics.efficiency": 1}):
        out.setdefault(r.get("game_id"), []).append(float(r["metrics"]["efficiency"]))
    return out


def after_game(db: Database, run_id: str, distiller: str, margin: float = REVERT_MARGIN,
               distill_model: str | None = None) -> dict:
    """Score a finished run, credit its lessons, select, and distil the next generation."""
    run = db[D.RUNS].find_one({"_id": run_id}, {"generation": 1})
    metrics = evaluate_run(db, run_id)
    credited = credit(db, run_id)
    fitness = update_fitness(db, run["generation"]) if run.get("generation") is not None else None
    reverted = select(db, margin)
    if reverted:
        harness_event(db, run_id, "generation_reverted", f"generation {reverted['reverted']} reverted: "
                      f"{reverted['why']}", generation=reverted["reactivated"])
    new = distill(db, run_id, kind=distiller, model=distill_model)
    d = new["diff"]
    harness_event(db, run_id, "generation_created",
                  f"generation {new['_id']} from this run: +{len(d['added_lessons'])} lessons, "
                  f"-{len(d['retired_lessons'])} retired, {len(d['revised_lessons'])} revised, "
                  f"knobs {d['knob_changes'] or 'unchanged'}. {new['rationale']}"[:1500],
                  d["added_lessons"], generation=new["_id"])
    return {"run_id": run_id, "metrics": metrics, "credited": credited, "fitness": fitness,
            "reverted": reverted, "next_generation": new["_id"], "diff": d, "distiller": new["distiller"]}


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


def run(games: list[str], *, agent: str = "claude", arc_root: str | None = None, label: str = "evolve",
        runs_root: str | None = None, distiller: str = "claude", repeat: int = 1, pause: float = 0.2,
        model: str | None = None, effort: str | None = None, timeout: float = 3600.0,
        margin: float = REVERT_MARGIN, distill_model: str | None = None,
        validate_on: list[str] | None = None, val_repeat: int = 1) -> list[dict]:
    db = D.get_db()
    D.ensure_indexes(db)
    genome.ensure_gen0(db)
    root = Path(runs_root or f"runs/{label}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    collector = start_collector(db, root)
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _sigterm) if main_thread else None
    out, attempt = [], None
    launch_opts = ["--agent", agent, "--pause", str(pause)]
    for flag, val in (("--arc-root", arc_root), ("--model", model), ("--effort", effort)):
        if val:
            launch_opts += [flag, val]
    try:
        for k in range(repeat):
            for game in games:
                if collector is not None and not collector[1].is_alive():
                    print("[evolve] the collector thread died; restarting it", flush=True)
                    collector = start_collector(db, root)
                gen = genome.active(db)["_id"]
                if validate_on:                   # the incumbent needs a held-out score to be compared against
                    validate(db, gen, validate_on, root, label, val_repeat, launch_opts, timeout)
                n = k
                while True:                       # never resume into an earlier attempt, never share one
                    attempt = root / f"{label}-g{gen:02d}-{game}-{n}"
                    try:
                        attempt.mkdir(parents=True)
                        break
                    except FileExistsError:
                        n += 1
                cmd = [sys.executable, "-m", "mongoarc.launch", "--game", game, "--out-dir", str(attempt),
                       "--gen", str(gen), "--label", label, "--timeout", str(timeout)] + launch_opts
                print(f"[evolve] generation {gen}: playing {game} in {attempt.name}", flush=True)
                code = _play(cmd, attempt / "driver.log", float(timeout) + 600.0)
                why = "the launcher was stopped after running past its time limit" if code is None \
                    else f"the launcher exited ({code}) without recording an ending"
                run_id, attempt_done = _close_run(db, attempt, why), attempt
                attempt = None
                if run_id is None:
                    print(f"[evolve] {game}: the launcher left no run marker (exit {code}); see "
                          f"{attempt_done / 'driver.log'}", flush=True)
                    out.append({"game": game, "generation": gen, "exit": code,
                                "error": f"launch failed (exit {code})"})
                    continue
                problem = unusable(db, run_id)
                if problem is None and collector is None:
                    seed_after(db, run_id, attempt_done, gen)
                elif problem is None and not wait_ingested(db, run_id, attempt_done, game):
                    problem = "the collector had not stored the whole run after 30 s"
                if problem is None:
                    try:
                        step = after_game(db, run_id, distiller, margin, distill_model)
                    except Exception as exc:  # noqa: BLE001 -- one game's bookkeeping must not end the session
                        problem = f"scoring or distilling failed ({type(exc).__name__}: {str(exc)[:200]})"
                if problem is not None:
                    # Nothing is scored and nothing changes: the next game runs under the same generation.
                    print(f"[evolve] {run_id}: not used as evidence, {problem}; generation {gen} stays "
                          f"(see {attempt_done / 'driver.log'})", flush=True)
                    harness_event(db, run_id, "evolution_skipped",
                                  f"this run was not scored and produced no generation: {problem}", generation=gen)
                    out.append({"game": game, "generation": gen, "exit": code, "run_id": run_id, "error": problem})
                    continue
                m = step["metrics"]
                print(f"[evolve] {run_id}: {m['levels_cleared']}/{m['win_levels']} levels, "
                      f"{m['actions_total']} actions, efficiency {m['efficiency']} -> generation "
                      f"{step['next_generation']} (+{len(step['diff']['added_lessons'])} lessons"
                      + (f", reverted generation {step['reverted']['reverted']}" if step["reverted"] else "")
                      + ")", flush=True)
                out.append({"game": game, "generation": gen, "exit": code, **step})
                if validate_on:
                    # The candidate is kept only if it does not score worse than its parent on games
                    # neither of them learned from.
                    cand = step["next_generation"]
                    last = validate(db, cand, validate_on, root, label, val_repeat, launch_opts, timeout)
                    undone = select(db, margin)
                    if undone:
                        print(f"[evolve] generation {undone['reverted']} REVERTED on held-out games: "
                              f"{undone['why']}", flush=True)
                        harness_event(db, (last or [run_id])[-1], "generation_reverted",
                                      f"generation {undone['reverted']} reverted: {undone['why']}",
                                      generation=undone["reactivated"])
                        out[-1]["reverted_on_validation"] = undone
                    else:
                        print(f"[evolve] generation {cand} kept after held-out validation", flush=True)
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


def status(db: Database) -> str:
    """Generations, their fitness and the lessons, as a text table."""
    lines = [f"{'gen':>3} {'parent':>6} {'status':10} {'lessons':>7} {'tools':>5} {'runs':>4} {'eff':>6} "
             f"{'compl':>6}  {'distiller':9}  diff"]
    for g in db[D.GENERATIONS].find().sort("_id", 1):
        f, d = g.get("fitness") or {}, g.get("diff") or {}
        diff = " ".join(x for x in (
            f"+{','.join(d.get('added_lessons') or [])}" if d.get("added_lessons") else "",
            f"-{','.join(d.get('retired_lessons') or [])}" if d.get("retired_lessons") else "",
            f"~{','.join(d.get('revised_lessons') or [])}" if d.get("revised_lessons") else "",
            " ".join(f"{k}:{a}->{b}" for k, (a, b) in (d.get("knob_changes") or {}).items()),
            f"tools+{','.join(d.get('added_tools') or [])}" if d.get("added_tools") else "") if x)
        fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
        lines.append(f"{g['_id']:>3} {str(g.get('parent')) if g.get('parent') is not None else '-':>6} "
                     f"{g.get('status', ''):10} {len(g.get('lessons') or []):>7} {len(g.get('tools') or []):>5} "
                     f"{len(f.get('runs') or []):>4} {fmt(f.get('mean_efficiency')):>6} "
                     f"{fmt(f.get('mean_completion')):>6}  {(g.get('distiller') or {}).get('kind', ''):9}  "
                     f"{diff or '(no change)'}")
    lines.append("")
    lines.append(f"{'id':6} {'kind':15} {'status':8} {'gen':>3} {'inj':>3} {'wins':>4} {'eff':>6}  text")
    for l in db[D.LESSONS].find().sort("_id", 1):
        s = l.get("stats") or {}
        eff = f"{s['efficiency_sum'] / s['injected']:.3f}" if s.get("injected") else "-"
        lines.append(f"{l['_id']:6} {l.get('kind', ''):15} {l.get('status', ''):8} {l.get('created_gen', 0):>3} "
                     f"{s.get('injected', 0):>3} {s.get('wins', 0):>4} {eff:>6}  {l.get('text', '')[:110]}")
    return "\n".join(lines)
