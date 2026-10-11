"""Harness generations and lessons: the genome the evolution loop edits.

A generation is what the harness adds around a game: carried lessons, the
addendum rendered from them and launch knobs. `tools` is reserved and always
empty: every game starts from a fresh workspace and no scripts are carried.
Documents follow docs/SCHEMA.md.
"""
from __future__ import annotations

import math
import re
from typing import Any

from pymongo.database import Database

from mongoarc import db as D

DEFAULT_KNOBS: dict[str, Any] = {"budget_multiple": 5.0, "max_nudges": 2, "coach_stuck_factor": 3.0}
# Allowed range per knob; anything a distiller proposes is clamped to it.
KNOB_RANGE = {"budget_multiple": (2.0, 8.0), "max_nudges": (0, 6), "coach_stuck_factor": (1.5, 6.0)}
LESSON_KINDS = ("mechanic_prior", "strategy", "pitfall", "tooling")

NETWORK_NOTE = (
    "### Environment note (hackathon)\n\n"
    "Network access beyond the game API is permitted in this environment, so the "
    "\"Do not use the network\" constraint above does not apply here. Every other "
    "constraint stands: play only through `client`, and working out the rules is still the task.\n")

# What the agent is told about its memory. CCARC's doctrine knows nothing of `memory.py` or `scripts/`,
# so this section of the workspace CLAUDE.md is the only place the agent can learn of them.
MEMORY_NOTE = (
    "### Long-term memory\n\n"
    "This game starts with an empty `notes/` and `scripts/`. What earlier games learned is reached by search:\n\n"
    "- `python3 memory.py recall <words>` searches the lessons distilled across earlier games and the notes "
    "and verified rules their solvers left. Everything it returns describes OTHER games: a prior to test "
    "early, never a fact about this one. Use it when you meet something you cannot explain, before spending "
    "many actions on a guess. It costs no game actions.\n"
    "- Prose you save under `notes/` (markdown or plain text) is stored automatically and becomes searchable "
    "in later games. Write it for the solver of a different game: describe each mechanic and how you "
    "recognised it, not this game's layout or coordinates.\n"
    "- Code goes in `scripts/`. It is not stored; name the script in the note that explains it.\n\n"
    "`memory.py` talks to a service the harness runs on this machine.\n")

# Same shape CCARC refuses in solver-facing text: 4 chars, letter first, a digit inside.
_ID_SHAPE = re.compile(r"(?<![\w-])(?=[a-z][a-z0-9]{3}(?!\w))(?=[a-z0-9]*\d)[a-z][a-z0-9]{3}")
_ID_ALLOWED = {"utf8", "int8", "sha1", "md5s"}
# A cell, a named axis with a value, or two bare numbers side by side ("12, 3", "12 3", "12/3").
_COORD = re.compile(r"[(\[]\s*-?\d+\s*,\s*-?\d+\s*[)\]]"
                    r"|\b(?:x|y|row|col|column)\s*(?:=|==|:)?\s*\d+\b"
                    r"|\b\d+\s*[,/]\s*\d+\b|\b\d+\s+\d+\b", re.I)


def clean_lesson(text: str, game_id: str = "") -> str | None:
    """The lesson with whitespace normalised, or None if it is not game-independent.

    A lesson that names a game or gives coordinates is rejected, never
    rewritten: with the cell blanked out it would still describe one level's
    layout, and relabelling a mechanic hides where it came from.
    """
    text = " ".join(str(text or "").split())
    if not text:
        return None
    low = text.lower()
    # The id itself, its short form, and any long part of it (real ids end in a hex suffix).
    parts = {p for p in game_id.lower().split("-")[1:] if len(p) >= 6}
    short = game_id.split("-")[0].lower() if game_id else ""
    if game_id and (game_id.lower() in low or any(p in low for p in parts)
                    or (short and re.search(rf"(?<![a-z0-9]){re.escape(short)}", low))):
        return None
    if any(m.group(0) not in _ID_ALLOWED for m in _ID_SHAPE.finditer(low)):
        return None
    if _COORD.search(text):
        return None
    return text[:400]


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
    """The markdown appended to the workspace CLAUDE.md for this generation.

    `memory=False` leaves out the memory section, for a launch that could not
    give the agent `memory.py`.
    """
    n = gen["_id"]
    out = ["", "---", "", f"## Carried lessons (harness generation {n})", ""]
    live = [l for l in lessons if l.get("status", "active") == "active"]
    if live:
        out += ["These come from earlier games played under this harness, not from the game you are "
                "playing now. Each one is a prior to test early and cheaply, not a fact: spend a few "
                "actions confirming or refuting it before you rely on it, and say which in "
                "`gate.acknowledge(...)`.", ""]
        for i, l in enumerate(live, 1):
            s = l.get("stats") or {}
            seen = f", carried into {s['injected']} earlier run(s)" if s.get("injected") else ""
            out.append(f"{i}. **{l.get('kind', 'strategy')}** ({l['_id']}{seen}): {l['text']}")
    else:
        out.append("No lessons are carried yet: no earlier game has produced one. "
                   "Lessons listed here in later generations come from earlier games and are priors to test, not facts.")
    out += [""] + ([MEMORY_NOTE] if memory else []) + [NETWORK_NOTE]
    return "\n".join(out)


def _gen0() -> dict:
    g = {"_id": 0, "parent": None, "created_at": D.now(), "created_from_run": None, "status": "active",
         "lessons": [], "knobs": dict(DEFAULT_KNOBS), "tools": [],
         "diff": {"added_lessons": [], "retired_lessons": [], "revised_lessons": [], "knob_changes": {},
                  "added_tools": []},
         "rationale": "The empty harness: no lessons, default knobs.",
         "distiller": {"model": "none", "cost_usd": None, "kind": "heuristic"},
         "fitness": {"runs": [], "mean_efficiency": None, "mean_completion": None}}
    g["addendum"] = render_addendum(g, [])
    return g


def ensure_gen0(db: Database) -> dict:
    """Create generation 0 if the collection is empty; returns it."""
    g = _gen0()
    db[D.GENERATIONS].update_one({"_id": 0}, {"$setOnInsert": {k: v for k, v in g.items() if k != "_id"}},
                                 upsert=True)
    return db[D.GENERATIONS].find_one({"_id": 0})


def active(db: Database) -> dict:
    """The generation the next game is launched with."""
    g = db[D.GENERATIONS].find_one({"status": "active"}, sort=[("_id", -1)])
    return g or ensure_gen0(db)


def get(db: Database, n: int) -> dict:
    g = db[D.GENERATIONS].find_one({"_id": int(n)})
    if g is None:
        if int(n) == 0:
            return ensure_gen0(db)
        raise KeyError(f"no generation {n}")
    return g


def lessons_of(db: Database, gen: dict) -> list[dict]:
    """The generation's lesson documents, in carried order."""
    by_id = {l["_id"]: l for l in db[D.LESSONS].find({"_id": {"$in": gen.get("lessons") or []}})}
    return [by_id[i] for i in gen.get("lessons") or [] if i in by_id]


def next_lesson_ids(db: Database, count: int) -> list[str]:
    last = db[D.LESSONS].find_one({}, sort=[("_id", -1)], projection={"_id": 1})
    start = int(last["_id"][1:]) + 1 if last else 1
    return [f"L{start + i:04d}" for i in range(count)]


def new_lesson(lid: str, text: str, kind: str, tags: list[str], evidence: list[dict], gen: int) -> dict:
    return {"_id": lid, "text": text, "kind": kind if kind in LESSON_KINDS else "strategy",
            "tags": [str(t)[:40] for t in (tags or [])][:6], "evidence": evidence, "created_gen": gen,
            "created_at": D.now(), "status": "active", "retired_reason": None,
            "stats": {"runs": [], "injected": 0, "efficiency_sum": 0.0, "wins": 0, "recalled": 0}}
