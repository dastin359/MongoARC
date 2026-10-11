"""Launch one game through the unmodified CCARC driver, with the agent's memory in MongoDB.

    python -m mongoarc.launch --game G --out-dir D [--agent fake|claude] [--arc-root URL] [--model M]
        [--effort E] [--timeout S] [--label L] [--pause 0.2] [--no-memory] [--dry-run]

The harness launches, records, serves memory, prompts the agent at level
boundaries, and shows it all. It does not distil anything: the agent writes its
own lessons into this run's notes with `memory.py` and searches them back.

A memory run (the default) registers the run in MongoDB and wraps CCARC's
build_workspace in process: the workspace CLAUDE.md becomes CCARC's preamble
followed by mongoARC's doctrine (mongoarc/assets/ARC3_DOCTRINE.md) and nothing
else, the workspace gets `memory.py`, `scripts/` and `scratch/`, CCARC's level
gate and its own level-cleared message are switched off in the agent's
environment, and a loopback memory gateway serves this run only. While the game
runs, a watcher (evolve/levels.py) sends one message at each level boundary.
Every run starts with an empty memory; nothing crosses between games.

A control run (--no-memory) is plain CCARC: its doctrine, its level gate, its
announcement, no memory.py. Nothing under CCARC/ is edited either way.
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
from mongoarc.evolve.coach import harness_event

REPO = D.REPO
STUB_CFG = REPO / ".local" / "stub-cfg"
STUB_CREDENTIAL = '{"claudeAiOauth":{"accessToken":"stub","refreshToken":"stub","expiresAt":4102444800000}}\n'
MEMORY_TOOL = REPO / "mongoarc" / "agent_tools" / "memory.py"
# What the agent of a memory run is told: CCARC's doctrine with the memory model changed (prose in MongoDB
# through memory.py, code in scripts/, throwaway files in scratch/). CCARC's own text is only read, to find it.
DOCTRINE = REPO / "mongoarc" / "assets" / "ARC3_DOCTRINE.md"
CCARC_DOCTRINE = REPO / "CCARC" / "src" / "ccarc" / "arc_3" / "assets" / "CCARC3_DOCTRINE.md"
# Directories the mongoARC doctrine names: code the agent keeps, and throwaway images and dumps.
MEMORY_RUN_DIRS = ("scripts", "scratch")
# The agent never holds database credentials (docs/SCHEMA.md): these stay in the harness process.
HARNESS_ONLY_ENV = ("MONGODB_URI", "MONGO_DB", "MONGOARC_TEST_URI")
# Never printed, logged or stored; only their presence is reported.
SECRET_ENV = ("ARC_API_KEY", "ARCPRIZE_API_KEY", "MONGODB_URI", "MONGOARC_CLAUDE_OAUTH_TOKEN",
              "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# CCARC settings every launch uses (they were the defaults of the retired per-level knobs).
BUDGET_MULTIPLE = 5.0
MAX_NUDGES = 2
# A memory run has no rule book, so CCARC's level gate (the refusal of a new level's first action until
# `gate.acknowledge(...)`) and CCARC's own level-cleared message (which names that call) are switched off.
# Both are read with os.environ in the agent's own python processes (gate.LevelGate.check and
# client._announce_level), whose environment is the workspace env the driver builds: see `child_env`.
GATE_ENV = ("CCARC3_LEVEL_GATE", "CCARC3_LEVEL_ANNOUNCE")
# The two places the docstring of the generated workspace session.py mentions the gate: (old, new).
SESSION_DOC_EDITS = (("    from session import client, gate, arc\n", "    from session import client, arc\n"),
                     (" The gate holds the first\naction of a new level until you call ``gate.acknowledge(...)``.", ""))
# Child settings shown as values by --dry-run (none of them is a secret).
SHOWN_ENV = ("CLAUDE_BINARY", "CLAUDE_CONFIG_DIR", "CCARC3_ARC_ROOT", "CCARC3_LIVE", "CCARC3_NETGUARD",
             "CCARC3_CONFINE", "CCARC3_CREDENTIAL_PROBE", "CCARC3_MAX_NUDGES", "CCARC3_HIDE_BASELINES",
             "CCARC3_LEVEL_GATE", "CCARC3_LEVEL_ANNOUNCE", "FAKE_POLICY", "FAKE_PAUSE_S", "MONGOARC_RUN_ID", "MONGOARC_MEMORY_URL")


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


def prepare_env(agent: str, arc_root: str | None, pause: float) -> None:
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
    e["CCARC3_MAX_NUDGES"] = str(MAX_NUDGES)
    src = str(REPO / "CCARC" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def swap_doctrine(text: str) -> tuple[str, str]:
    """A CLAUDE.md as CCARC wrote it, with CCARC's doctrine replaced by mongoARC's.

    Returns (text, outcome). CCARC's doctrine is located by its exact text and
    nothing else: "swapped" if it was there (everything before and after it is
    returned byte for byte), "already" if mongoARC's doctrine is in place
    (nothing to do, so calling this twice is the same as once), "not-found" if
    neither is (a CCARC whose doctrine changed, or one line of it redacted):
    the text comes back untouched rather than cut at a guessed boundary.
    """
    ours = DOCTRINE.read_text(encoding="utf-8")
    theirs = CCARC_DOCTRINE.read_text(encoding="utf-8")   # read as CCARC reads it, so it is the string CCARC wrote
    if theirs and text.count(theirs) == 1:
        return text.replace(theirs, ours), "swapped"
    if ours and ours in text:
        return text, "already"
    return text, "not-found"


def doctrine_in(text: str) -> str:
    """Which doctrine a CLAUDE.md holds: "mongoarc", "ccarc" or "unknown"."""
    for name, path in (("mongoarc", DOCTRINE), ("ccarc", CCARC_DOCTRINE)):
        try:
            if path.read_text(encoding="utf-8") in text:
                return name
        except OSError:
            pass
    return "unknown"


def patch_session_doc(text: str) -> tuple[str, str]:
    """A workspace session.py with the two gate mentions taken out of its docstring.

    Returns (text, outcome): "patched" if both were there (each exactly once, in
    the docstring; the code below it is returned byte for byte), "already" if the
    docstring names no gate call, "not-found" otherwise: the text comes back
    untouched.
    """
    end = text.find('"""', 3) if text.startswith('"""') else -1
    if end < 0:
        return text, "not-found"
    doc, code = text[:end], text[end:]
    if any(doc.count(old) != 1 for old, _ in SESSION_DOC_EDITS):
        return text, "not-found" if "gate.acknowledge" in doc or "client, gate" in doc else "already"
    for old, new in SESSION_DOC_EDITS:
        doc = doc.replace(old, new)
    return doc + code, "patched"


def prepare_workspace(workspace: Path, memory_tool: bool = False) -> None:
    """Turn the workspace CCARC built into a memory run's: doctrine, session.py docstring, layout, `memory.py`.

    With the memory tool: CCARC's doctrine in CLAUDE.md is replaced by mongoARC's
    and nothing is added, so the file is CCARC's preamble followed by
    mongoarc/assets/ARC3_DOCTRINE.md; the docstring of session.py stops naming
    the gate; `scripts/` and `scratch/` are created and `memory.py` is copied in.
    Calling it twice is the same as calling it once.

    Without it (a control run) nothing is touched: the workspace is CCARC's.
    """
    if not memory_tool:
        return
    workspace = Path(workspace)
    doc = workspace / "CLAUDE.md"
    old = doc.read_bytes().decode("utf-8")       # bytes, not text mode: what is not replaced stays byte for byte
    try:
        text, outcome = swap_doctrine(old)
    except OSError as exc:
        text, outcome = old, f"unreadable ({type(exc).__name__}: {exc})"
    if outcome not in ("swapped", "already"):
        print(f"    WARNING: the mongoARC doctrine was NOT put into {doc} ({outcome}): CCARC's doctrine was not "
              f"found in it word for word, so CLAUDE.md is left as CCARC wrote it and still describes "
              f"rules.json and the level gate instead of memory.py. Compare {CCARC_DOCTRINE} with {DOCTRINE}.",
              flush=True)
    if text != old:
        doc.write_bytes(text.encode("utf-8"))
    sess = workspace / "session.py"
    try:
        src = sess.read_bytes().decode("utf-8")
    except OSError:
        src = None
    if src is not None:
        new, outcome = patch_session_doc(src)
        if outcome == "patched":
            sess.write_bytes(new.encode("utf-8"))
        elif outcome == "not-found":
            print(f"    WARNING: the docstring of {sess} still mentions the level gate (CCARC's template changed); "
                  f"the file is left as CCARC wrote it.", flush=True)
    for name in MEMORY_RUN_DIRS:
        (workspace / name).mkdir(exist_ok=True)
    shutil.copyfile(MEMORY_TOOL, workspace / "memory.py")


def child_env(env: dict, run_id: str, memory_url: str | None, workspace: Path | None = None) -> dict:
    """Make CCARC's child environment fit for the agent: no database settings, its run id, its memory URL.

    With a memory URL (a memory run) CCARC's level gate and level-cleared message
    are off; without one (a control run) both are left at CCARC's default, on.
    """
    for k in HARNESS_ONLY_ENV:
        env.pop(k, None)
    env["MONGOARC_RUN_ID"] = run_id
    if memory_url:
        env["MONGOARC_MEMORY_URL"] = memory_url
    else:
        env.pop("MONGOARC_MEMORY_URL", None)
    for k in GATE_ENV:
        if memory_url:
            env[k] = "0"
        else:
            env.pop(k, None)
    if memory_url and workspace is not None:
        # The doctrine sends code to scripts/. A script run from there has scripts/ as its import root, so
        # `from session import client` fails until the agent patches sys.path; putting the workspace on
        # PYTHONPATH removes that wasted step. (A control run is left exactly as CCARC builds it.)
        root = str(Path(workspace).resolve())
        parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and p != root]
        env["PYTHONPATH"] = os.pathsep.join([root] + parts)
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


def _marker_run(marker_path: Path, game: str) -> str | None:
    """The run id an earlier launch left in this attempt directory, if it was for the same game."""
    try:
        old = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return old.get("run_id") if isinstance(old, dict) and old.get("game_id") == game else None


def launch(args: argparse.Namespace) -> dict:
    db = D.get_db()
    D.ensure_indexes(db)
    dry = bool(getattr(args, "dry_run", False))
    if args.role == "control":                    # a control run is plain CCARC
        args.no_memory = True
    use_memory = not args.no_memory
    out_dir = Path(args.out_dir).resolve()
    marker_path = out_dir / D.RUN_MARKER
    # A relaunch into the same attempt directory resumes that run (and the notes it has saved).
    run_id = args.run_id or _marker_run(marker_path, args.game)
    prepare_env(args.agent, args.arc_root, args.pause)

    from ccarc.arc_3 import session as S
    from ccarc.arc_3.client import GameInfo, list_games

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

    resumed = False
    t0 = D.now()
    fields = {"game_id": args.game, "out_dir": str(out_dir), "label": args.label, "agent": args.agent,
              "model": model, "effort": effort, "baseline_actions": baseline or None, "status": "running",
              "started_at": t0, "updated_at": t0, "ended_at": None, "win_levels": len(baseline) or None,
              "memory_tool": use_memory, "role": args.role or ("train" if use_memory else "control")}
    base_id = f"{args.game}-{t0.strftime('%Y%m%dT%H%M%S')}"
    if dry:
        run_id = run_id or base_id
    elif run_id:
        resumed = True
        keep = () if baseline else ("baseline_actions", "win_levels")   # a failed lookup must not blank them
        db[D.RUNS].update_one({"_id": run_id}, {"$set": {k: v for k, v in fields.items() if k not in keep},
                                                "$setOnInsert": {"n_actions": 0, "result": None, "metrics": None}},
                              upsert=True)
    else:                                         # before the marker, so two launches never share an id
        run_id = claim_run_id(db, base_id, fields)
    marker = {"run_id": run_id, "game_id": args.game, "agent": args.agent, "model": model, "effort": effort,
              "baseline_actions": baseline or None, "label": args.label}

    if lookup_error and not dry:                 # the launch failed before a game existed: mark it and stop
        if not (resumed and marker_path.exists()):
            _write_marker(marker_path, marker)
        result = {"game_id": args.game, "error": lookup_error}
        db[D.RUNS].update_one({"_id": run_id}, {"$set": {"status": "lost", "ended_at": D.now(),
                                                         "updated_at": D.now(), "result": result}})
        harness_event(db, run_id, "run_ended", f"lost: the game never started ({lookup_error})", status="lost",
                      error=lookup_error, launch_error=lookup_error)
        out = {"run_id": run_id, "status": "lost", "out_dir": str(out_dir), "result": result}
        print(json.dumps(out, indent=2), flush=True)
        return out

    if use_memory and not dry:                    # one look, never a wait: the index builds while the game runs
        try:
            vector = D.ensure_vector_index(db)
        except Exception as exc:  # noqa: BLE001 -- a game is never held up or failed by the search index
            vector = f"unknown ({type(exc).__name__})"
        print(f"    memory: vector index {vector}"
              + (" (no Atlas Vector Search on this server; memory search uses the text index)"
                 if vector == "unsupported" else ""), flush=True)

    memory, memory_url = start_memory() if use_memory else (None, None)
    memory_on = memory is not None                # without a gateway the run is plain CCARC, gate included
    for k in GATE_ENV:                            # the driver's own environment agrees with the child's
        if memory_on:
            os.environ[k] = "0"
        else:
            os.environ.pop(k, None)

    original = S.build_workspace

    def build_workspace(config, info=None, **kw):
        ws = original(config, info, **kw)
        prepare_workspace(ws.root, memory_tool=memory_on)
        child_env(ws.env, run_id, memory_url, ws.root)
        return ws

    config = S.Ccarc3Config(game_id=args.game, out_dir=out_dir, model=args.model or S.DEFAULT_MODEL,
                            effort=args.effort or S.DEFAULT_EFFORT, budget_multiple=BUDGET_MULTIPLE,
                            wall_clock_timeout_s=float(args.timeout), clock_is_infrastructure=False)

    if dry:
        report = {"dry_run": True, "run_id": run_id, "game_id": args.game, "agent": args.agent, "model": model,
                  "effort": effort, "out_dir": str(out_dir), "baseline_levels": len(baseline),
                  "game_lookup": lookup_error or "ok", "memory_tool": memory_on,
                  "level_gate": "off" if memory_on else "on", "level_messages": memory_on,
                  "budget_multiple": BUDGET_MULTIPLE}
        try:
            ws = build_workspace(config, info or GameInfo(game_id=args.game))
            prompt = (ws.root / "CLAUDE.md").read_bytes().decode("utf-8")
            report["claude_md"] = str(ws.root / "CLAUDE.md")
            report["claude_md_lines"] = len(prompt.splitlines())
            report["doctrine"] = doctrine_in(prompt)
            report["nothing_after_doctrine"] = prompt.endswith(
                (DOCTRINE if memory_on else CCARC_DOCTRINE).read_text(encoding="utf-8"))
            report["cli"] = [a if len(a) <= 120 else a[:117] + "..." for a in S.build_cli_args(ws)]
            report["child_env"] = {k: ws.env.get(k) for k in SHOWN_ENV}
            report["child_env_present"] = {k: k in ws.env for k in SECRET_ENV + HARNESS_ONLY_ENV}
        finally:
            if memory is not None:
                memory.shutdown()
        print(redact(json.dumps(report, indent=2)), flush=True)
        return {"status": "dry-run", **report}

    watcher, collector, error, result = None, None, None, None
    try:                                          # from here on the run exists: whatever happens, it gets an ending
        _write_marker(marker_path, marker)
        harness_event(db, run_id, "run_started",
                      f"{args.game} {'resumed' if resumed else 'launched'} (agent {args.agent}, "
                      + ("memory run: the agent keeps its own notes through memory.py; no level gate)" if memory_on
                         else "control: plain CCARC, no memory tool)"), memory_tool=memory_on)
        S.build_workspace = build_workspace      # run_game resolves the name at call time
        collector = _start_collector(db, out_dir) if args.collect else None
        if memory_on:
            try:
                from mongoarc.evolve.levels import LevelWatcher
                watcher = LevelWatcher(db, run_id, out_dir, workspace, baseline)
                watcher.start()
            except Exception as exc:  # noqa: BLE001 -- the level messages must never cost a game
                watcher = None
                print(f"    level watcher not started ({type(exc).__name__}: {exc})", flush=True)
        result = S.run_game(config, info)
    except BaseException as exc:  # noqa: BLE001 -- the run document must not be left "running"
        error = exc
        result = {"game_id": args.game, "error": redact(f"{type(exc).__name__}: {exc}")[:1000]}
    finally:
        _ignore_signals()                         # a second Ctrl-C must not cut the ending short
        S.build_workspace = original
        if watcher is not None:
            try:
                watcher.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"    level watcher: {type(exc).__name__}: {exc}", flush=True)
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
    out = {"run_id": run_id, "status": status, "memory_tool": memory_on, "out_dir": str(out_dir),
           "result": D.jsonable(stored)}
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
    p.add_argument("--gen", type=int, default=None,
                   help="ignored (there are no harness generations any more)")
    p.add_argument("--distiller", choices=("claude", "heuristic"), default="claude",
                   help="ignored: the harness no longer distils lessons, the agent writes its own")
    p.add_argument("--distill-model", default=None,
                   help="ignored, like --distiller")
    p.add_argument("--agent", choices=("fake", "claude"), default="claude")
    p.add_argument("--arc-root", default=None, help="ARC API root; unset means the real service")
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--timeout", type=float, default=3600.0, help="wall-clock limit in seconds")
    p.add_argument("--label", default=None)
    p.add_argument("--pause", type=float, default=0.2, help="fake agent: seconds between actions")
    p.add_argument("--run-id", default=None)
    p.add_argument("--coach-interval", type=float, default=1.0, help="ignored: there is no mid-game coach")
    p.add_argument("--collect", action="store_true", help="also run the collector in this process")
    p.add_argument("--role", choices=("train", "validation", "control"), default=None,
                   help="train: a memory run (default). control: the baseline arm, implies --no-memory. "
                        "validation is accepted for old callers and behaves as train.")
    p.add_argument("--no-memory", action="store_true",
                   help="control arm: plain CCARC (its doctrine, its level gate and level-cleared message, "
                        "no memory.py, no level-boundary messages from the harness)")
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
