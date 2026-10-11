"""Launch one game through the unmodified CCARC driver, under a harness generation.

    python -m mongoarc.launch --game G --out-dir D [--gen N] [--agent fake|claude]
        [--arc-root URL] [--model M] [--effort E] [--timeout S] [--label L] [--pause 0.2] [--dry-run]

Registers the run in MongoDB before the game starts, appends the generation's
addendum to the workspace CLAUDE.md by wrapping CCARC's build_workspace in
process, gives the agent `memory.py` and a loopback memory gateway, runs the
game, then records how it ended. Nothing under CCARC/ is edited: the agent's
prompt is changed in the workspace copy only.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.request
from pathlib import Path

from pymongo.errors import DuplicateKeyError

from mongoarc import db as D
from mongoarc.evolve import genome
from mongoarc.evolve.coach import Coach, harness_event

REPO = D.REPO
STUB_CFG = REPO / ".local" / "stub-cfg"
STUB_CREDENTIAL = '{"claudeAiOauth":{"accessToken":"stub","refreshToken":"stub","expiresAt":4102444800000}}\n'
MEMORY_TOOL = REPO / "mongoarc" / "agent_tools" / "memory.py"
# The agent never holds database credentials (docs/SCHEMA.md): these stay in the harness process.
HARNESS_ONLY_ENV = ("MONGODB_URI", "MONGO_DB", "MONGOARC_TEST_URI")
# Never printed, logged or stored; only their presence is reported.
SECRET_ENV = ("ARC_API_KEY", "ARCPRIZE_API_KEY", "MONGODB_URI", "MONGOARC_CLAUDE_OAUTH_TOKEN",
              "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# Child settings shown as values by --dry-run (none of them is a secret).
SHOWN_ENV = ("CLAUDE_BINARY", "CLAUDE_CONFIG_DIR", "CCARC3_ARC_ROOT", "CCARC3_LIVE", "CCARC3_NETGUARD",
             "CCARC3_CONFINE", "CCARC3_CREDENTIAL_PROBE", "CCARC3_MAX_NUDGES", "CCARC3_HIDE_BASELINES",
             "FAKE_POLICY", "FAKE_PAUSE_S", "MONGOARC_RUN_ID", "MONGOARC_MEMORY_URL")


def redact(text: str) -> str:
    """`text` with the value of every secret environment variable replaced by its name."""
    for k in SECRET_ENV:
        v = os.environ.get(k)
        if v and len(v) >= 6:
            text = text.replace(v, f"<{k}>")
    return text


def _ensure_mock(arc_root: str) -> None:
    """Start tools/mock_arc.py if --arc-root is a loopback URL nothing answers on."""
    if not arc_root.startswith("http://127.0.0.1:"):
        return
    try:
        urllib.request.urlopen(f"{arc_root}/api/games", timeout=1).read()
        return
    except OSError:
        pass
    port = arc_root.rsplit(":", 1)[1].strip("/")
    subprocess.Popen([sys.executable, str(REPO / "tools" / "mock_arc.py"), port], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1.0)


def prepare_env(agent: str, arc_root: str | None, knobs: dict, pause: float) -> None:
    """Set the process environment CCARC reads. Must run before `ccarc` is imported."""
    D.load_env()
    e = os.environ
    # The stub only satisfies CCARC's file-exists check on macOS (login lives in the Keychain).
    STUB_CFG.mkdir(parents=True, exist_ok=True)
    if not (STUB_CFG / ".credentials.json").exists():
        (STUB_CFG / ".credentials.json").write_text(STUB_CREDENTIAL, encoding="utf-8")
    e["CLAUDE_CONFIG_DIR"] = str(STUB_CFG)
    e.pop("CLAUDE_CODE_SESSION_ID", None)
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    for k, v in (("CCARC3_NETGUARD", "0"), ("CCARC3_CONFINE", "0"), ("CCARC3_LIVE", "1"),
                 ("CCARC3_CREDENTIAL_PROBE", "0")):
        e.setdefault(k, v)
    if arc_root:
        e["CCARC3_ARC_ROOT"] = arc_root.rstrip("/")
        e.pop("ARC_API_KEY", None)               # a local root needs no key; keep the real one out of the child
        _ensure_mock(e["CCARC3_ARC_ROOT"])
    else:
        e.pop("CCARC3_ARC_ROOT", None)
    if agent == "fake":
        e["CLAUDE_BINARY"] = str(REPO / "tools" / "fake_claude.py")
        e.setdefault("FAKE_POLICY", "seek")
        e["FAKE_PAUSE_S"] = str(pause)
    else:
        e["CLAUDE_BINARY"] = str(REPO / "tools" / "claude_login.sh")
    e.update(genome.launch_settings(knobs)["env"])
    src = str(REPO / "CCARC" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def apply_generation(workspace: Path, gen: dict, addendum: str, memory_tool: bool = False) -> None:
    """Append the addendum to CLAUDE.md and lay out what it describes: `scripts/` and `memory.py`.

    Nothing else is copied in: every game starts from a fresh workspace, and
    what carries over is reached through memory.py (docs/SCHEMA.md).
    """
    workspace = Path(workspace)
    doc = workspace / "CLAUDE.md"
    doc.write_text(doc.read_text(encoding="utf-8").rstrip("\n") + "\n" + addendum, encoding="utf-8")
    (workspace / "scripts").mkdir(exist_ok=True)
    if memory_tool:
        shutil.copyfile(MEMORY_TOOL, workspace / "memory.py")


def child_env(env: dict, run_id: str, memory_url: str | None) -> dict:
    """Make CCARC's child environment fit for the agent: no database settings, its run id, its memory URL."""
    for k in HARNESS_ONLY_ENV:
        env.pop(k, None)
    env["MONGOARC_RUN_ID"] = run_id
    if memory_url:
        env["MONGOARC_MEMORY_URL"] = memory_url
    else:
        env.pop("MONGOARC_MEMORY_URL", None)
    return env


def start_memory():
    """A memory gateway for this run on a free loopback port: (server, url), or (None, None)."""
    try:
        if not MEMORY_TOOL.is_file():
            raise FileNotFoundError(MEMORY_TOOL.name)
        from mongoarc import memory_gateway
        server = memory_gateway.serve(0)         # port 0: one gateway per launch, so runs never share one
        return server, f"http://127.0.0.1:{server.server_address[1]}"
    except Exception as exc:  # noqa: BLE001 -- a game can be played without long-term memory
        print(f"    memory gateway unavailable ({type(exc).__name__}: {exc}); playing without memory.py", flush=True)
        return None, None


def final_status(result: dict) -> str:
    if result.get("won"):
        return "won"
    return "finished" if int(result.get("levels_reached") or 0) > 0 else "lost"


def claim_run_id(db, base: str, fields: dict) -> str:
    """Insert the run document under `base`, or `base-2`, `base-3`... if that id is taken."""
    for i in range(1, 100):
        run_id = base if i == 1 else f"{base}-{i}"
        try:
            db[D.RUNS].insert_one({"_id": run_id, **fields, "n_actions": 0, "last_seq": 0, "result": None,
                                   "metrics": None})
            return run_id
        except DuplicateKeyError:
            continue
    raise RuntimeError(f"no free run id from {base}")


def _write_marker(path: Path, marker: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def launch(args: argparse.Namespace) -> dict:
    db = D.get_db()
    D.ensure_indexes(db)
    gen = genome.get(db, args.gen) if args.gen is not None else genome.active(db)
    prepare_env(args.agent, args.arc_root, gen.get("knobs") or {}, args.pause)

    from ccarc.arc_3 import session as S
    from ccarc.arc_3.client import GameInfo, list_games

    dry = bool(getattr(args, "dry_run", False))
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    workspace = out_dir / args.game
    model = args.model or ("fake" if args.agent == "fake" else S.DEFAULT_MODEL)
    effort = None if args.agent == "fake" else (args.effort or S.DEFAULT_EFFORT)

    lookup_error = None
    try:
        info = next((g for g in list_games() if g.game_id == args.game), None)
        if info is None:
            lookup_error = f"no such game: {args.game}"
    except Exception as exc:  # noqa: BLE001 -- no key, no network, API down: the launch fails, the caller goes on
        info, lookup_error = None, redact(f"game list unavailable: {type(exc).__name__}: {exc}")[:500]
    baseline = [int(b) for b in info.baseline_actions] if info else []

    # Lessons that name this game would be the agent reading about its own earlier attempts.
    carried = genome.lessons_of(db, gen)
    lessons = [l for l in carried if l.get("status") == "active" and genome.clean_lesson(l["text"], args.game)]
    lesson_ids = [l["_id"] for l in lessons]

    marker_path = out_dir / D.RUN_MARKER
    run_id, resumed = args.run_id, False
    if not run_id and marker_path.exists():      # relaunch into the same attempt dir resumes the run
        try:
            old = json.loads(marker_path.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        run_id = old.get("run_id") if old.get("game_id") == args.game else None
    t0 = D.now()
    fields = {"game_id": args.game, "out_dir": str(out_dir), "label": args.label, "agent": args.agent,
              "model": model, "effort": effort, "generation": gen["_id"], "lessons_used": lesson_ids,
              "baseline_actions": baseline or None, "status": "running", "started_at": t0, "updated_at": t0,
              "ended_at": None, "win_levels": len(baseline) or None}
    base_id = f"g{gen['_id']}-{args.game}-{t0.strftime('%Y%m%dT%H%M%S')}"
    if dry:
        run_id = run_id or base_id
    elif run_id:
        resumed = True
        keep = () if baseline else ("baseline_actions", "win_levels")   # a failed lookup must not blank them
        db[D.RUNS].update_one({"_id": run_id}, {"$set": {k: v for k, v in fields.items() if k not in keep},
                                                "$setOnInsert": {"n_actions": 0, "result": None, "metrics": None}},
                              upsert=True)
    else:
        run_id = claim_run_id(db, base_id, fields)   # before the marker, so two launches never share an id
    marker = {"run_id": run_id, "game_id": args.game, "generation": gen["_id"], "lessons_used": lesson_ids,
              "agent": args.agent, "model": model, "effort": effort, "baseline_actions": baseline or None,
              "label": args.label}

    if lookup_error and not dry:                 # the launch failed before a game existed: mark it and stop
        if not (resumed and marker_path.exists()):
            _write_marker(marker_path, marker)
        result = {"game_id": args.game, "error": lookup_error}
        db[D.RUNS].update_one({"_id": run_id}, {"$set": {"status": "lost", "ended_at": D.now(),
                                                         "updated_at": D.now(), "result": result}})
        harness_event(db, run_id, "run_ended", f"lost: the game never started ({lookup_error})", status="lost",
                      error=lookup_error, launch_error=lookup_error, generation=gen["_id"])
        out = {"run_id": run_id, "generation": gen["_id"], "status": "lost", "lessons_used": lesson_ids,
               "out_dir": str(out_dir), "result": result}
        print(json.dumps(out, indent=2), flush=True)
        return out

    memory, memory_url = start_memory()
    addendum = genome.render_addendum(gen, lessons, memory=memory is not None)

    original = S.build_workspace

    def build_workspace(config, info=None, **kw):
        ws = original(config, info, **kw)
        apply_generation(ws.root, gen, addendum, memory_tool=memory is not None)
        child_env(ws.env, run_id, memory_url)
        return ws

    settings = genome.launch_settings(gen.get("knobs"))
    config = S.Ccarc3Config(game_id=args.game, out_dir=out_dir, model=args.model or S.DEFAULT_MODEL,
                            effort=args.effort or S.DEFAULT_EFFORT,
                            budget_multiple=settings["budget_multiple"],
                            wall_clock_timeout_s=float(args.timeout), clock_is_infrastructure=False)

    if dry:
        report = {"dry_run": True, "run_id": run_id, "game_id": args.game, "generation": gen["_id"],
                  "lessons_used": lesson_ids, "agent": args.agent, "model": model, "effort": effort,
                  "out_dir": str(out_dir), "baseline_levels": len(baseline), "game_lookup": lookup_error or "ok",
                  "memory_tool": memory is not None, "knobs": settings}
        try:
            ws = build_workspace(config, info or GameInfo(game_id=args.game))
            report["claude_md"] = str(ws.root / "CLAUDE.md")
            report["addendum_in_claude_md"] = (ws.root / "CLAUDE.md").read_text(encoding="utf-8").endswith(addendum)
            report["cli"] = [a if len(a) <= 120 else a[:117] + "..." for a in S.build_cli_args(ws)]
            report["child_env"] = {k: ws.env.get(k) for k in SHOWN_ENV}
            report["child_env_present"] = {k: k in ws.env for k in SECRET_ENV + HARNESS_ONLY_ENV}
        finally:
            if memory is not None:
                memory.shutdown()
        print(redact(json.dumps(report, indent=2)), flush=True)
        return {"status": "dry-run", **report}

    coach, collector, error, result = None, None, None, None
    try:                                          # from here on the run exists: whatever happens, it gets an ending
        # Keep the stored addendum equal to what a run under this generation is given.
        if len(lessons) == len(carried) and addendum != gen.get("addendum"):
            db[D.GENERATIONS].update_one({"_id": gen["_id"]}, {"$set": {"addendum": addendum}})
        _write_marker(marker_path, marker)
        harness_event(db, run_id, "run_started",
                      f"{args.game} {'resumed' if resumed else 'launched'} under harness generation {gen['_id']} "
                      f"({len(lesson_ids)} lessons, agent {args.agent})",
                      knobs=gen.get("knobs"), generation=gen["_id"])
        harness_event(db, run_id, "lessons_injected",
                      "\n".join(f"{l['_id']} [{l['kind']}] {l['text']}" for l in lessons) or "no lessons carried",
                      lesson_ids, generation=gen["_id"],
                      items=[{"id": l["_id"], "kind": l["kind"], "text": l["text"]} for l in lessons])
        S.build_workspace = build_workspace      # run_game resolves the name at call time
        coach = Coach(db, run_id, out_dir, workspace, lesson_ids, baseline, settings["coach_stuck_factor"],
                      interval=args.coach_interval)
        collector = _start_collector(db, out_dir) if args.collect else None
        coach.start()
        result = S.run_game(config, info)
    except BaseException as exc:  # noqa: BLE001 -- the run document must not be left "running"
        error = exc
        result = {"game_id": args.game, "error": redact(f"{type(exc).__name__}: {exc}")[:1000]}
    finally:
        _ignore_signals()                         # a second Ctrl-C must not cut the ending short
        S.build_workspace = original
        if coach is not None:
            coach.stop()
        if memory is not None:
            memory.shutdown()

    stored = result
    try:                                          # the schema stores the workspace result.json verbatim
        stored = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if error is not None:
        print(redact("".join(traceback.format_exception(error))), file=sys.stderr, flush=True)
        stored = {**stored, "error": result["error"]}
    status = final_status(stored)
    upd = {"status": status, "ended_at": D.now(), "updated_at": D.now(), "result": D.jsonable(stored)}
    for k in ("cost_usd", "turns"):
        if stored.get(k) is not None:
            upd[k] = stored[k]
    db[D.RUNS].update_one({"_id": run_id}, {"$set": upd})
    # `launch_error` marks a run the launcher itself failed or was stopped on, as opposed to an `error`
    # CCARC wrote into result.json. It lives on the event because the collector may replace runs.result.
    extra = {"error": stored["error"]} if stored.get("error") else {}
    if error is not None:
        extra["launch_error"] = result["error"]
    harness_event(db, run_id, "run_ended",
                  f"{status}: {stored.get('levels_reached', 0)} level(s) in {stored.get('actions_used', 0)} actions"
                  + (f" ({stored['error']})" if stored.get("error") else ""),
                  action_n=int(stored.get("trace_rows") or 0) - 1, level=int(stored.get("levels_reached") or 0),
                  status=status, **extra)
    if collector is not None:
        collector[0].set()                        # run_forever makes one final pass after the stop
        collector[1].join(timeout=30)
    out = {"run_id": run_id, "generation": gen["_id"], "status": status, "lessons_used": lesson_ids,
           "out_dir": str(out_dir), "result": D.jsonable(stored)}
    print(json.dumps(out, indent=2), flush=True)
    if error is not None and not isinstance(error, Exception):
        raise error                               # KeyboardInterrupt / SystemExit
    return out


def _start_collector(db, out_dir: Path):
    """Optional in-process collector for a standalone launch (the evolve loop runs its own)."""
    import threading
    try:
        from mongoarc.collector import Collector
    except Exception as exc:  # noqa: BLE001
        print(f"    collector unavailable ({type(exc).__name__}); run it separately", flush=True)
        return None
    stop = threading.Event()
    # The attempt directory itself, not its parent: a standalone launch must not ingest its neighbours.
    t = threading.Thread(target=Collector(db, out_dir).run_forever, args=(0.5, stop), daemon=True)
    t.start()
    return stop, t


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m mongoarc.launch", description=__doc__.split("\n\n")[0])
    p.add_argument("--game", required=True)
    p.add_argument("--out-dir", required=True, help="attempt directory (one game per directory)")
    p.add_argument("--gen", type=int, default=None, help="harness generation (default: the active one)")
    p.add_argument("--agent", choices=("fake", "claude"), default="claude")
    p.add_argument("--arc-root", default=None, help="ARC API root; unset means the real service")
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--timeout", type=float, default=3600.0, help="wall-clock limit in seconds")
    p.add_argument("--label", default=None)
    p.add_argument("--pause", type=float, default=0.2, help="fake agent: seconds between actions")
    p.add_argument("--run-id", default=None)
    p.add_argument("--coach-interval", type=float, default=1.0)
    p.add_argument("--collect", action="store_true", help="also run the collector in this process")
    p.add_argument("--dry-run", action="store_true",
                   help="build the workspace and print what the agent would be given (prompt path, child "
                        "environment with secrets shown as present/absent); no game, nothing written to MongoDB")
    return p


def _terminate(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def _ignore_signals() -> None:
    import threading
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _terminate)     # a killed launcher still records how its run ended
    out = launch(build_parser().parse_args(argv))
    return 0 if out["status"] in ("won", "finished", "dry-run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
