"""Mid-game coach: when a level drags on, remind the agent of its most relevant carried lessons.

Runs as a thread inside the launcher. It speaks through CCARC's own harness
outbox, which the live driver forwards to the agent as a user message.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from pymongo.database import Database

from mongoarc import db as D


def harness_event(db: Database, run_id: str, what: str, text: str = "", lessons: list[str] | None = None,
                  action_n: int = -1, level: int = 0, **extra) -> int:
    """Write one `harness` event on the run's timeline; returns its seq."""
    seq = D.next_seq(db, run_id)
    db[D.EVENTS].insert_one({"_id": f"{run_id}:{seq}", "run_id": run_id, "seq": seq, "at": D.now(),
                             "kind": "harness", "action_n": action_n, "level": level, "what": what,
                             "text": text, "lessons": lessons or [], **extra})
    return seq


def pick_lessons(db: Database, run_id: str, lesson_ids: list[str], limit: int = 2) -> list[dict]:
    """Up to `limit` carried lessons: text-matched to the run's rule book, else best by record."""
    if not lesson_ids:
        return []
    scope = {"_id": {"$in": lesson_ids}, "status": "active"}
    book = db[D.RULEBOOKS].find_one({"run_id": run_id}, sort=[("version", -1)])
    if book:
        words = " ".join([r.get("rule", "") for r in book.get("verified", []) + book.get("refuted", [])]
                         + list(book.get("open_questions", [])))[:1000]
        if words.strip():
            hits = list(db[D.LESSONS].find({**scope, "$text": {"$search": words}},
                                           {"score": {"$meta": "textScore"}, "text": 1, "kind": 1})
                        .sort([("score", {"$meta": "textScore"})]).limit(limit))
            if hits:
                return hits
    return list(db[D.LESSONS].aggregate([
        {"$match": scope},
        {"$addFields": {"mean_eff": {"$cond": [{"$gt": ["$stats.injected", 0]},
                                               {"$divide": ["$stats.efficiency_sum", "$stats.injected"]}, 0]}}},
        {"$sort": {"mean_eff": -1, "stats.wins": -1, "_id": 1}}, {"$limit": limit},
        {"$project": {"text": 1, "kind": 1}}]))


class Coach(threading.Thread):
    """Watches trace.state.json and sends at most one note per level."""

    def __init__(self, db: Database, run_id: str, out_dir: Path, workspace: Path, lesson_ids: list[str],
                 baseline: list[int], factor: float = 3.0, interval: float = 1.0, say=None):
        super().__init__(daemon=True, name="mongoarc-coach")
        self.db, self.run_id, self.out_dir, self.workspace = db, run_id, Path(out_dir), Path(workspace)
        self.lesson_ids, self.baseline, self.factor, self.interval = lesson_ids, list(baseline or []), factor, interval
        self.noted: set[int] = set()
        self.stop_event = threading.Event()
        self._say = say

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 -- the coach must never take a game down
                print(f"    coach: {type(exc).__name__}: {exc}", flush=True)

    def check(self) -> bool:
        """One poll; True if a note was sent."""
        try:
            st = json.loads((self.workspace / "trace.state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        level, spent = int(st.get("level", 0)), int(st.get("level_actions", 0))
        if st.get("state") in ("WIN", "GAME_OVER") or level in self.noted or level >= len(self.baseline):
            return False
        if spent <= self.factor * self.baseline[level]:
            return False
        self.noted.add(level)
        picked = pick_lessons(self.db, self.run_id, self.lesson_ids)
        # No figures: the per-level baseline is withheld from the agent on purpose.
        lines = ["Harness note: this level is taking long. Stop acting for a moment, re-read `rules.json`, "
                 "and choose the cheapest experiment that would separate your leading hypotheses."]
        if picked:
            lines.append("Carried lessons that may apply (priors from earlier games, test before relying on them):")
            lines += [f"- {l['text']}" for l in picked]
        text = "\n".join(lines)
        sent = self._send(text)
        # Same position rule as every other event: n of the latest ledger row the collector has stored.
        stored = int((self.db[D.RUNS].find_one({"_id": self.run_id}, {"n_actions": 1}) or {}).get("n_actions") or 0)
        harness_event(self.db, self.run_id, "coach_note", text, [l["_id"] for l in picked],
                      action_n=stored - 1 if stored else int(st.get("actions_used", 0)), level=level,
                      level_actions=spent, delivered=sent)
        return True

    def _send(self, text: str) -> bool:
        if self._say is not None:
            return bool(self._say(text))
        from ccarc.arc_3 import outbox
        return outbox.say(text, kind="note",
                          env={"CCARC3_HARNESS_OUTBOX": str(self.out_dir / "harness_outbox.jsonl")})
