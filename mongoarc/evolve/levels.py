"""The level watcher: the one thing the harness still says during a game.

One per memory run, started by the launcher (never for a control run). A single
thread polls the workspace `trace.state.json` (the runs document if the file is
unreadable). Each time the number of cleared levels rises while the game is
still in progress it sends ONE message through CCARC's harness outbox, telling
the agent to save what the level taught it and to search its memory before its
first action on the new level, and logs a `memory_nudge` harness event.

Nothing is distilled here and nothing is written but that event: the lessons
are the agent's own notes (`memory.py`). A failure is logged and never reaches
the game.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pymongo.database import Database

from mongoarc import db as D
from mongoarc.evolve.coach import harness_event

# The whole message. The commands are the ones the doctrine gives (mongoarc/assets/ARC3_DOCTRINE.md).
NUDGE = ('Level {done} cleared. You are on level {new}{total}.\n'
         'Save what that level taught you: python3 memory.py save "<title>" "<text>" '
         '(update or delete any note it proved wrong).\n'
         'Then, before your first action on this level, search your memory: python3 memory.py recall "<question>".')
OVER = ("WIN", "GAME_OVER")


def nudge_text(cleared: int, win_levels: int | None = None) -> str | None:
    """The message for a game with `cleared` levels cleared, or None if nothing is to be said.

    Nothing before the first level is cleared and nothing after the final one.
    The total is left out when it is unknown.
    """
    cleared, total = int(cleared), int(win_levels or 0)
    if cleared <= 0 or (total and cleared >= total):
        return None
    return NUDGE.format(done=cleared, new=cleared + 1, total=f" of {total}" if total else "")   # levels count from 1


class LevelWatcher:
    """Watches one run and tells its agent, once per level boundary, to save and to search."""

    def __init__(self, db: Database, run_id: str, out_dir: Path, workspace: Path, baseline: list[int] | None = None,
                 interval: float = 0.4, say=None):
        self.db, self.run_id = db, run_id
        self.out_dir, self.workspace = Path(out_dir), Path(workspace)
        self.baseline, self.interval, self._say = list(baseline or []), interval, say
        try:                                      # a resumed run: the levels it had cleared get no second message
            self.seen = int(self.state().get("level") or 0)
        except Exception:  # noqa: BLE001
            self.seen = 0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True, name="mongoarc-level-watch")

    # -- lifecycle -----------------------------------------------------------------------------------------

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop watching. True once the thread is gone."""
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    # -- watching ------------------------------------------------------------------------------------------

    def state(self) -> dict:
        try:
            st = json.loads((self.workspace / "trace.state.json").read_text(encoding="utf-8"))
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        run = self.db[D.RUNS].find_one({"_id": self.run_id}, {"level": 1, "state": 1, "win_levels": 1}) or {}
        return {k: run[k] for k in ("level", "state", "win_levels") if run.get(k) is not None}

    def poll(self) -> str | None:
        """One look at the run. Returns the message sent, or None.

        `seen` only ever rises, so a boundary is announced once however often the
        state is read, and several levels cleared between two looks get one
        message, for the level the agent is on now.
        """
        st = self.state()
        cleared = int(st.get("level") or 0)
        if cleared <= self.seen:
            return None
        self.seen = cleared
        if st.get("state") in OVER or self.stop_event.is_set():   # the game is over: nobody is left to tell
            return None
        text = nudge_text(cleared, st.get("win_levels") or len(self.baseline))
        if text is None:
            return None
        sent = False
        try:
            sent = bool(self.send(text))
        except Exception as exc:  # noqa: BLE001
            self.log(f"message for level {cleared} not delivered: {type(exc).__name__}: {exc}")
        harness_event(self.db, self.run_id, "memory_nudge", text, action_n=int(st.get("actions_used") or 0) - 1,
                      level=cleared, delivered=sent)
        return text

    def _watch(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
            except Exception as exc:  # noqa: BLE001 -- the watcher must never take a game down
                self.log(f"{type(exc).__name__}: {exc}")
            self.stop_event.wait(self.interval)

    # -- plumbing ------------------------------------------------------------------------------------------

    def send(self, text: str) -> bool:
        if self._say is not None:
            return bool(self._say(text))
        from ccarc.arc_3 import outbox
        return outbox.say(text, kind="note", env={"CCARC3_HARNESS_OUTBOX": str(self.out_dir / "harness_outbox.jsonl")})

    def log(self, text: str) -> None:
        print(f"    level watcher: {text}", flush=True)
