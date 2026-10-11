"""Harness events on a run's timeline.

The mid-game coach that lived here is gone (the harness no longer picks or
sends lessons); what is left is the one writer every harness module shares.
"""
from __future__ import annotations

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
