"""Harness generations and lessons, one lineage per run.

A run is one game. It starts at generation k=0 with no lessons; each time a
level is cleared the level evolver (levels.py) stores generation k+1 for that
run. Nothing here is shared between runs: a lesson learned in one game is not
supposed to transfer to another, only to the later levels of the same game.
"""
from __future__ import annotations

import math
import re
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from mongoarc import db as D

DEFAULT_KNOBS: dict[str, Any] = {"budget_multiple": 5.0, "max_nudges": 2, "coach_stuck_factor": 3.0}
# Allowed range per knob; anything a distiller proposes is clamped to it.
KNOB_RANGE = {"budget_multiple": (2.0, 8.0), "max_nudges": (0, 6), "coach_stuck_factor": (1.5, 6.0)}
LESSON_KINDS = ("mechanic_prior", "strategy", "pitfall", "tooling")
MAX_ACTIVE = 10        # active lessons per run
LESSON_CHARS = 500

# The lessons section of the workspace CLAUDE.md sits between these two lines, so it can be replaced in place.
START_MARK = "<!-- mongoarc:lessons:start -->"
END_MARK = "<!-- mongoarc:lessons:end -->"

# The marked section holds the lessons and nothing else. How the agent uses its memory (`memory.py`), where
# code goes (`scripts/`) and the network rule are stated once, in the doctrine the launcher puts above it
# (mongoarc/assets/ARC3_DOCTRINE.md): a second copy here would drift from it.

# A cell, or two bare numbers side by side: one level's layout, which the next level will not share.
_COORD = re.compile(r"[(\[]\s*-?\d+\s*,\s*-?\d+\s*[)\]]|\b(?:x|y|row|col|column)\s*(?:=|==)\s*\d+\b", re.I)


def clean_lesson(text: Any) -> str | None:
    """The lesson with whitespace normalised, or None if it is empty or pins a level's coordinates.

    A lesson is about one game and should be concrete about it (objects,
    colours, what each action does). Only a hard-coded cell is refused: layouts
    change between levels, so a coordinate is wrong on the next one.
    """
    text = " ".join(str(text or "").split()) if isinstance(text, str) else ""
    if not text or _COORD.search(text):
        return None
    return text[:LESSON_CHARS]


def norm(text: str) -> str:
    """Comparison key for spotting duplicate lessons."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def clamp_knob(name: str, value: Any) -> Any:
    """The value moved into the knob's allowed range. Raises ValueError for NaN or infinity."""
    lo, hi = KNOB_RANGE[name]
    if not math.isfinite(float(value)):
        raise ValueError(f"{name}: {value!r} is not a finite number")
    v = min(hi, max(lo, float(value)))
    return int(round(v)) if name == "max_nudges" else round(v, 2)


def launch_settings(knobs: dict | None) -> dict[str, Any]:
    """Knobs as launch settings: Ccarc3Config fields, child env, coach threshold."""
    k = {**DEFAULT_KNOBS, **(knobs or {})}
    return {"budget_multiple": float(k["budget_multiple"]),
            "env": {"CCARC3_MAX_NUDGES": str(int(k["max_nudges"]))},
            "coach_stuck_factor": float(k["coach_stuck_factor"])}


def render_addendum(gen: dict, lessons: list[dict], memory: bool = True) -> str:
    """The lessons section appended to the workspace CLAUDE.md, between START_MARK and END_MARK.

    Lessons only: the heading, a short explanation and the numbered lessons, or
    the sentence saying they will appear. `memory=False` is a launch that
    promises the agent no lessons (a control run): with no lesson to show, the
    marked section is empty (see `has_content`).
    """
    k = int(gen.get("k") or 0)
    live = [l for l in lessons if l.get("status", "active") == "active"]
    out = ["", "---", "", START_MARK]
    if live or memory:                            # a control run is promised no lessons: it gets none
        out += [f"## Lessons from the levels you have cleared (harness generation {k})", ""]
    if live:
        out += ["The harness distilled these from what you recorded on the levels of THIS game you have already "
                "cleared (what you committed at each level gate, the notes you saved, and what each level cost). "
                "They are about this game: use them on the level in front of you. The layout changes from level "
                "to level, so re-read the frame before reusing a route.", ""]
        for i, l in enumerate(live, 1):
            out.append(f"{i}. **{l.get('kind', 'strategy')}** ({l['_id']}, after level {l.get('level')}): "
                       f"{l['text']}")
    elif memory:
        out.append("No lessons yet: this game has just started. Lessons will appear here as levels are cleared, "
                   "and they will be about this game only.")
    out += ["", END_MARK, ""]
    return "\n".join(out)


def has_content(addendum: str | None) -> bool:
    """Whether the marked section of `addendum` holds anything for the agent to read."""
    text = addendum or ""
    a, b = text.find(START_MARK), text.find(END_MARK)
    if a < 0 or b < a:
        return bool(text.strip())
    return bool(text[a + len(START_MARK):b].strip())


def replace_section(doc: str, addendum: str) -> str:
    """`doc` with its marked lessons section replaced by the one in `addendum`; nothing else changes.

    With no markers in `doc`, the addendum is appended.
    """
    a, b = doc.find(START_MARK), doc.find(END_MARK)
    x, y = addendum.find(START_MARK), addendum.find(END_MARK)
    if x < 0 or y < x:
        raise ValueError("the addendum has no marked lessons section")
    if a < 0 or b < a:
        return doc.rstrip("\n") + "\n" + addendum
    return doc[:a] + addendum[x:y] + doc[b:]


def section_of(doc: str) -> str | None:
    """The marked lessons section of a document, markers included; None if it has none."""
    a, b = doc.find(START_MARK), doc.find(END_MARK)
    return doc[a:b + len(END_MARK)] if 0 <= a < b else None


def next_gen_id(db: Database) -> int:
    last = db[D.GENERATIONS].find_one({"_id": {"$type": "number"}}, sort=[("_id", -1)], projection={"_id": 1})
    return int(last["_id"]) + 1 if last else 0


def gen0(run_id: str, game_id: str, memory: bool = True) -> dict:
    """Generation k=0 of a run, not yet stored (and without an `_id`)."""
    g = {"run_id": run_id, "game_id": game_id, "k": 0, "level": None, "parent": None, "status": "active",
         "created_at": D.now(), "lessons": [], "knobs": dict(DEFAULT_KNOBS),
         "diff": {"added_lessons": [], "retired_lessons": [], "revised_lessons": [], "knob_changes": {}},
         "rationale": "The game starts with an empty memory: no lessons, default knobs.",
         "distiller": {"model": "none", "cost_usd": None, "kind": "heuristic"},
         "level_metrics": None, "played": None}
    g["addendum"] = render_addendum(g, [], memory=memory)
    return g


def insert_generation(db: Database, gen: dict, session=None) -> dict:
    """Store a generation under the next free id (max(_id)+1), retrying if another launch took it."""
    for _ in range(50):
        gen["_id"] = next_gen_id(db)
        try:
            db[D.GENERATIONS].insert_one(gen, session=session)
            return gen
        except DuplicateKeyError:
            if session is not None:              # inside a transaction: let the caller retry it whole
                raise
    raise RuntimeError("no free generation id")


def ensure_run_gen0(db: Database, run_id: str, game_id: str, memory: bool = True) -> dict:
    """The run's generation 0, created if the run has no generation yet."""
    g = db[D.GENERATIONS].find_one({"run_id": run_id, "k": 0})
    if g is None:
        g = insert_generation(db, gen0(run_id, game_id, memory))
    return g


def active(db: Database, run_id: str) -> dict | None:
    """The run's active generation (the one its current level is played under), or None."""
    return (db[D.GENERATIONS].find_one({"run_id": run_id, "status": "active"}, sort=[("k", -1)])
            or db[D.GENERATIONS].find_one({"run_id": run_id}, sort=[("k", -1)]))


def lineage(db: Database, run_id: str) -> list[dict]:
    return list(db[D.GENERATIONS].find({"run_id": run_id}).sort("k", 1))


def lessons_of(db: Database, gen: dict | None) -> list[dict]:
    """The generation's lesson documents, in order."""
    ids = (gen or {}).get("lessons") or []
    by_id = {l["_id"]: l for l in db[D.LESSONS].find({"_id": {"$in": ids}})}
    return [by_id[i] for i in ids if i in by_id]


def active_lessons(db: Database, run_id: str) -> list[dict]:
    """The run's lessons as they stand now (they change every time a level is cleared)."""
    return [l for l in lessons_of(db, active(db, run_id)) if l.get("status") == "active"]


def next_lesson_ids(db: Database, count: int) -> list[str]:
    last = db[D.LESSONS].find_one({"_id": {"$regex": r"^L\d+$"}}, sort=[("_id", -1)], projection={"_id": 1})
    start = int(last["_id"][1:]) + 1 if last else 1
    return [f"L{start + i:04d}" for i in range(count)]


def new_lesson(lid: str, run_id: str, game_id: str, level: int, text: str, kind: str | None, tags: list[str],
               evidence: list[dict], gen: int | None) -> dict:
    return {"_id": lid, "run_id": run_id, "game_id": game_id, "level": int(level), "text": text,
            "kind": kind if kind in LESSON_KINDS else "strategy",
            "tags": [str(t)[:40] for t in (tags or [])][:6], "evidence": evidence, "created_gen": gen,
            "created_at": D.now(), "status": "active", "retired_reason": None, "stats": {"recalled": 0}}
