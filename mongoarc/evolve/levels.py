"""The level evolver: the harness improves itself each time the agent clears a level.

One per run, started by the launcher (never for a control run). Two threads:

- the watcher polls the workspace `trace.state.json` (the runs document if the
  file is unreadable). The moment the number of cleared levels rises it tells
  the agent, through CCARC's harness outbox, to search its memory before its
  first action on the new level, and queues the cleared level;
- the worker takes cleared levels one at a time, in order: waits briefly for the
  rule book the agent commits at the level gate, records what the level cost on
  the generation it was played under, distils this run's evidence into the
  run's next generation, rewrites the lessons section of the workspace
  CLAUDE.md, and sends the new lessons into the live session.

Nothing here reads or writes another run. A failure is logged and never
reaches the game.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
from pathlib import Path

from pymongo.database import Database

from mongoarc import db as D
from mongoarc.evolve import genome
from mongoarc.evolve.coach import harness_event
from mongoarc.evolve.distill import distill_level, level_metrics

BOOK_WAIT_S = 45.0     # for the rule book version committed at the level gate
BOOK_WAIT_LAST_S = 6.0  # after the final level no gate follows, and after a stop the agent is gone

# Sent the moment a level is cleared, which is before the agent has passed the level gate and before the
# level has been distilled: measured on a real run, a search made right then came back empty. So the note
# puts the gate first (the rule book committed there is searchable as soon as the collector stores it) and
# says that the lessons follow.
NUDGE = ("Harness note: level {done} is cleared and you are now on level {new}. First record what level {done} "
         "established with `gate.acknowledge(...)`. Then, before your first action on this level, run "
         "`python3 memory.py recall <words for what you see in the new frame>`: the harness memory for this game "
         "holds your notes, the rules you committed at the gates, and the lessons distilled from the levels "
         "cleared so far. It costs no game actions. The lessons from level {done} are being distilled now and "
         "will reach you in a separate note within seconds.")


def write_section(workspace: Path, addendum: str) -> bool:
    """Replace the marked lessons section of the workspace CLAUDE.md, atomically. True if the file changed."""
    doc = Path(workspace) / "CLAUDE.md"
    old = doc.read_bytes().decode("utf-8")      # bytes, not text mode: line endings outside the markers stay as they are
    new = genome.replace_section(old, addendum)
    if new == old:
        return False
    tmp = doc.with_name(f".CLAUDE.md.{os.getpid()}.tmp")
    tmp.write_bytes(new.encode("utf-8"))
    os.replace(tmp, doc)
    return True


class LevelEvolver:
    """Watches one run and gives it a new generation per cleared level."""

    def __init__(self, db: Database, run_id: str, game_id: str, out_dir: Path, workspace: Path,
                 baseline: list[int] | None, kind: str = "claude", model: str | None = None,
                 interval: float = 0.4, memory: bool = True, say=None, book_wait: float = BOOK_WAIT_S):
        self.db, self.run_id, self.game_id = db, run_id, game_id
        self.out_dir, self.workspace = Path(out_dir), Path(workspace)
        self.baseline, self.kind, self.model = list(baseline or []), kind, model
        self.interval, self.memory, self._say, self.book_wait = interval, memory, say, book_wait
        gen = genome.active(db, run_id)
        self.seen = int((gen or {}).get("k") or 0)          # cleared levels already turned into generations
        self.stop_event = threading.Event()
        self.jobs: queue.Queue = queue.Queue()
        self.watcher = threading.Thread(target=self._watch, daemon=True, name="mongoarc-level-watch")
        self.worker = threading.Thread(target=self._work, daemon=True, name="mongoarc-level-distill")

    # -- lifecycle -----------------------------------------------------------------------------------------

    def start(self) -> None:
        self.watcher.start()
        self.worker.start()

    def stop(self, timeout: float = 300.0) -> bool:
        """Stop watching, finish the levels already cleared (one at a time), and return. True if all were done."""
        self.stop_event.set()
        self.watcher.join(timeout=10)
        self.worker.join(timeout=timeout)
        return not self.worker.is_alive()

    # -- watching ------------------------------------------------------------------------------------------

    def state(self) -> dict:
        try:
            st = json.loads((self.workspace / "trace.state.json").read_text(encoding="utf-8"))
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        run = self.db[D.RUNS].find_one({"_id": self.run_id}, {"level": 1, "state": 1, "level_costs": 1,
                                                              "win_levels": 1}) or {}
        return {k: run[k] for k in ("level", "state", "level_costs", "win_levels") if run.get(k) is not None}

    def poll(self) -> int:
        """One look at the run; queues every newly cleared level. Returns how many were queued."""
        st = self.state()
        cleared, queued = int(st.get("level") or 0), 0
        while self.seen < cleared:
            level = self.seen
            self.seen += 1
            costs = st.get("level_costs") or []
            win = int(st.get("win_levels") or len(self.baseline) or 0)
            last = st.get("state") in ("WIN", "GAME_OVER") or (win and level + 1 >= win)
            # Only the newest boundary gets a nudge: the agent is on that level now, not on a skipped one.
            if not last and self.memory and self.seen == cleared and not self.stop_event.is_set():
                self.nudge(level, st)
            self.jobs.put({"level": level, "last": bool(last),
                           "actions": int(costs[level]) if level < len(costs) else None})
            queued += 1
        return queued

    def nudge(self, level: int, st: dict) -> None:
        text = NUDGE.format(done=level, new=level + 1)
        try:
            sent = self.send(text)
            harness_event(self.db, self.run_id, "memory_nudge", text, action_n=int(st.get("actions_used") or 0) - 1,
                          level=level + 1, delivered=sent)
        except Exception as exc:  # noqa: BLE001
            self.log(f"memory nudge for level {level + 1} failed: {type(exc).__name__}: {exc}")

    def _watch(self) -> None:
        while True:
            stopping = self.stop_event.is_set()   # read first: one last look after the stop catches the final level
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 -- the evolver must never take a game down
                self.log(f"watch: {type(exc).__name__}: {exc}")
            if stopping:
                break
            self.stop_event.wait(self.interval)
        self.jobs.put(None)

    # -- distilling ----------------------------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self.evolve(job)
            except Exception as exc:  # noqa: BLE001
                self.log(f"level {job.get('level')}: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
                try:
                    harness_event(self.db, self.run_id, "evolution_skipped",
                                  f"level {job.get('level')} produced no generation: {type(exc).__name__}: "
                                  f"{str(exc)[:300]}", level=int(job.get("level") or 0))
                except Exception:  # noqa: BLE001
                    pass

    def wait_for_book(self, level: int, limit: float) -> bool:
        """Until a rule book version with an entry for `level` is in MongoDB, or `limit` seconds pass."""
        end = time.monotonic() + limit
        q = {"run_id": self.run_id, "$or": [{"verified.level": level}, {"refuted.level": level}]}
        while True:
            if self.db[D.RULEBOOKS].count_documents(q, limit=1):
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            if self.stop_event.is_set():          # the agent is gone: give the collector its last pass, no more
                end = min(end, time.monotonic() + BOOK_WAIT_LAST_S)
            time.sleep(min(0.5, max(0.05, left)))

    def actions_on(self, level: int) -> int | None:
        """Actions spent on a level, from the stored ledger (an action counts for the level it was taken from)."""
        from mongoarc.evolve.evaluate import _counts_from_db
        counts, _ = _counts_from_db(self.db, self.run_id)
        return counts.get(level)

    def evolve(self, job: dict) -> dict:
        """Steps (b)-(g) for one cleared level. Returns the new generation."""
        db, run_id, level = self.db, self.run_id, int(job["level"])
        t0 = time.monotonic()
        had_book = self.wait_for_book(level, min(self.book_wait, BOOK_WAIT_LAST_S) if job.get("last")
                                      else self.book_wait)
        waited = round(time.monotonic() - t0, 1)
        run = db[D.RUNS].find_one({"_id": run_id}, {"baseline_actions": 1, "n_actions": 1}) or {}
        base = run.get("baseline_actions") or self.baseline
        actions = job.get("actions")
        if actions is None:
            actions = self.actions_on(level)
        m = level_metrics(level, actions, base[level] if level < len(base) else None)
        # (c) what the level cost, on the generation it was played under.
        parent = genome.active(db, run_id) or genome.ensure_run_gen0(db, run_id, self.game_id, self.memory)
        db[D.GENERATIONS].update_one({"_id": parent["_id"]}, {"$set": {"played": m}})
        # (d) + (e)
        t1 = time.monotonic()
        gen = distill_level(db, run_id, level, m, kind=self.kind, model=self.model, memory=self.memory)
        took = round(time.monotonic() - t1, 1)
        d = gen["diff"]
        lessons = [l for l in genome.lessons_of(db, gen) if l.get("status") == "active"]
        changed = [l for l in lessons if l["_id"] in d["added_lessons"] or l["_id"] in d["revised_lessons"]]
        where = {"action_n": int(run.get("n_actions") or 0) - 1, "level": level}
        eff = "n/a" if m["efficiency"] is None else f"{m['efficiency']:.2f}"
        harness_event(db, run_id, "generation_created",
                      (f"generation {gen['k']} after level {level} ({m['actions']} actions, efficiency {eff}): "
                       f"+{len(d['added_lessons'])} lessons, -{len(d['retired_lessons'])} retired, "
                       f"{len(d['revised_lessons'])} revised, knobs {d['knob_changes'] or 'unchanged'}. "
                       f"{gen['rationale']}")[:1500],
                      d["added_lessons"], generation=gen["_id"], k=gen["k"], distiller=gen["distiller"],
                      level_metrics=m, rulebook_seen=had_book, waited_s=waited, distill_s=took, **where)
        # (f) the copy Claude Code re-injects after a compaction.
        try:
            wrote = write_section(self.workspace, gen["addendum"])
        except (OSError, ValueError) as exc:
            wrote = False
            self.log(f"CLAUDE.md not updated after level {level}: {type(exc).__name__}: {exc}")
        # (g) the live session, unless the game is over or nothing changed.
        sent, text = False, "\n".join(f"{l['_id']} [{l['kind']}] {l['text']}" for l in lessons) or "no lessons yet"
        if changed and not job.get("last") and not self.stop_event.is_set():
            note = [f"Harness note: lessons from level {level} of this game (now also in CLAUDE.md under "
                    f"\"Lessons from the levels you have cleared\"). They hold on the levels still ahead:"]
            note += [f"- ({l['_id']}, {l['kind']}) {l['text']}" for l in changed]
            if d["retired_lessons"]:
                note.append(f"Dropped as wrong or superseded: {', '.join(d['retired_lessons'])}.")
            if self.memory:
                note.append("`python3 memory.py recall <words>` now returns these together with your rules and notes.")
            sent = self.send("\n".join(note))
        harness_event(db, run_id, "lessons_injected", text, [l["_id"] for l in lessons], generation=gen["_id"],
                      k=gen["k"], items=[{"id": l["_id"], "kind": l["kind"], "text": l["text"]} for l in lessons],
                      changed=[l["_id"] for l in changed], delivered=sent, claude_md=wrote, **where)
        self.log(f"level {level} cleared in {m['actions']} actions (efficiency {eff}) -> generation k={gen['k']} "
                 f"(+{len(d['added_lessons'])} lessons, {gen['distiller'].get('kind')}, {took}s"
                 f"{'' if had_book else ', no gate rule book for it'})")
        return gen

    # -- plumbing ------------------------------------------------------------------------------------------

    def send(self, text: str) -> bool:
        if self._say is not None:
            return bool(self._say(text))
        from ccarc.arc_3 import outbox
        return outbox.say(text, kind="note", env={"CCARC3_HARNESS_OUTBOX": str(self.out_dir / "harness_outbox.jsonl")})

    def log(self, text: str) -> None:
        print(f"    evolver: {text}", flush=True)
