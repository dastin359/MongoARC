"""Turn one finished run's evidence into the next harness generation.

distill(db, run_id, kind) reads the run from MongoDB, asks a distiller
("claude": one real CLI call; "heuristic": no model) for a proposal, validates
it, and stores the new generation plus its lesson documents. A proposal that
cannot be used, whatever the reason, falls back to the heuristic: distilling
must never stop the loop.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure

from mongoarc import db as D
from mongoarc.evolve import genome

MAX_ADD = 4            # lessons a single run may add
MAX_NOTES = 6          # of the agent's notes shown to the distiller
NOTE_CHARS = 1500      # of each note
NOTE_SUFFIXES = (".md", ".txt", ".markdown", "")

# Outcome-driven lessons the heuristic distiller can add, first applicable first.
_TEMPLATES = [
    ("pitfall", ["death", "hazard"], lambda e: e["deaths"] > 0,
     "A death costs the whole level's progress: when an unfamiliar object appears, probe it from a position "
     "that is cheap to return to before committing a long action sequence."),
    ("pitfall", ["wasted", "no-op"], lambda e: e["wasted_actions"] > 0,
     "Actions that leave the board unchanged still count against the score: after one no-op, do not repeat "
     "that action from the same state."),
    ("strategy", ["hypotheses", "level-boundary"], lambda e: any("UNTESTED" in q for q in e["rulebook"]["open_questions"]),
     "Earlier solvers crossed level boundaries with rules still untested: in the first actions of a new level, "
     "test one carried rule directly instead of assuming it transfers."),
    ("strategy", ["planning", "efficiency"],
     lambda e: len(c := [l for l in e["metrics"]["levels"] if l["cleared"] and l["efficiency"] is not None]) > 1
     and c[-1]["efficiency"] < c[0]["efficiency"] - 0.1,
     "Later levels tend to reuse an earlier mechanic over a longer route: once the mechanic is known, plan the "
     "whole path from the frame before moving rather than re-exploring."),
    ("strategy", ["exploration", "efficiency"],
     lambda e: any(l["cleared"] and (l["efficiency"] or 0) < 0.5 for l in e["metrics"]["levels"]),
     "When a level has cost far more than a direct solution would, stop acting and write down the cheapest "
     "experiment that separates the two leading hypotheses before spending more actions."),
    ("strategy", ["completion"], lambda e: e["metrics"]["completion"] < 1.0,
     "An unfinished game scores zero on every level not reached: when progress stalls, try each available "
     "action once from the current state and compare frames before going deeper on one idea."),
]


def _rules(entries: Any) -> list[dict]:
    """Rule-book entries as [{rule, level}], whatever shape the agent left rules.json in."""
    out = []
    for r in entries if isinstance(entries, list) else []:
        if isinstance(r, str):
            r = {"rule": r}
        if not isinstance(r, dict) or not isinstance(r.get("rule"), str) or not r["rule"].strip():
            continue
        out.append({"rule": " ".join(r["rule"].split())[:300],
                    "level": r["level"] if isinstance(r.get("level"), int) else None})
    return out


def _notes(db: Database, run_id: str, ws: Path) -> list[dict]:
    """The agent's own prose for this run: the files under notes/ as the game left them, else the `notes` bank."""
    docs: list[dict] = []
    root = ws / "notes"
    for p in sorted(root.rglob("*")) if root.is_dir() else []:
        if len(docs) >= MAX_NOTES:
            break
        if p.is_file() and p.suffix.lower() in NOTE_SUFFIXES and not p.name.startswith("."):
            try:
                text = p.read_bytes()[:NOTE_CHARS * 4].decode("utf-8", errors="ignore")[:NOTE_CHARS]
            except OSError:
                continue
            if text.strip():
                docs.append({"path": str(p.relative_to(ws)), "text": text})
    if docs:
        return docs
    return [{"path": n.get("path"), "text": str(n.get("text") or "")[:NOTE_CHARS]}   # the directory is gone
            for n in db[D.NOTES].find({"run_id": run_id}, {"path": 1, "text": 1}).sort("path", 1).limit(MAX_NOTES)]


def gather_evidence(db: Database, run_id: str) -> dict[str, Any]:
    """Everything the distiller is shown about one run, read from MongoDB (and notes/ as a fallback)."""
    from mongoarc.evolve.evaluate import evaluate_run
    run = db[D.RUNS].find_one({"_id": run_id})
    if run is None:
        raise KeyError(f"no run {run_id}")
    metrics = run.get("metrics") or evaluate_run(db, run_id)
    book = db[D.RULEBOOKS].find_one({"run_id": run_id}, sort=[("version", -1)]) or {}
    counts = next(iter(db[D.ACTIONS].aggregate([
        {"$match": {"run_id": run_id}},
        {"$group": {"_id": None, "deaths": {"$sum": {"$cond": ["$death", 1, 0]}},
                    "wasted": {"$sum": {"$cond": ["$wasted", 1, 0]}},
                    "full_resets": {"$sum": {"$cond": ["$full_reset", 1, 0]}}}}])), {})
    texts = [str(e.get("text") or "")[:1500] for e in db[D.EVENTS].find(
        {"run_id": run_id, "kind": "text"}).sort("seq", -1).limit(4)][::-1]
    recalls = [str(e.get("query") or "")[:200] for e in db[D.EVENTS].find(
        {"run_id": run_id, "kind": "recall"}).sort("seq", 1).limit(20)]
    ws = Path(run.get("out_dir") or "") / run["game_id"]
    gen = genome.active(db)
    carried = gen.get("lessons") or []
    lessons = [{"id": l["_id"], "text": l.get("text", ""), "kind": l.get("kind"), "status": l.get("status"),
                "used_in_this_run": l["_id"] in (run.get("lessons_used") or []),
                "stats": {"injected": int(s.get("injected") or 0), "wins": int(s.get("wins") or 0),
                          "recalled": int(s.get("recalled") or 0),
                          "mean_efficiency": round(float(s.get("efficiency_sum") or 0.0) / s["injected"], 3)
                          if s.get("injected") else None}}
               for l in genome.lessons_of(db, gen) for s in [l.get("stats") or {}]]
    # Lessons no longer carried: shown so a distiller does not hand the same thing back unnoticed.
    dropped = [{"id": l["_id"], "text": l.get("text", ""), "reason": l.get("retired_reason")}
               for l in db[D.LESSONS].find({"status": "retired", "_id": {"$nin": carried}}).sort("_id", -1).limit(10)]
    parent = db[D.GENERATIONS].find_one({"_id": gen.get("parent")}) if gen.get("parent") is not None else None
    return {"run_id": run_id, "game_id": run["game_id"], "status": run.get("status"), "workspace": str(ws),
            "generation": run.get("generation"), "metrics": metrics,
            "deaths": int(counts.get("deaths") or run.get("deaths") or 0),
            "wasted_actions": int(counts.get("wasted") or run.get("wasted_actions") or 0),
            "full_resets": int(counts.get("full_resets") or run.get("full_resets") or 0),
            "rulebook": {"verified": _rules(book.get("verified")), "refuted": _rules(book.get("refuted")),
                         "open_questions": [q[:300] for q in book.get("open_questions") or []
                                            if isinstance(q, str)] if isinstance(book.get("open_questions"), list)
                         else []},
            "agent_summary": texts, "agent_notes": _notes(db, run_id, ws), "memory_searches": recalls,
            "active_generation": gen["_id"], "knobs": gen.get("knobs") or dict(genome.DEFAULT_KNOBS),
            "fitness": gen.get("fitness"), "parent_fitness": (parent or {}).get("fitness"),
            "lessons": lessons, "retired_lessons": dropped}


def propose_heuristic(db: Database, ev: dict) -> dict:
    """A proposal with no model: promote verified rules, add one outcome lesson, tune the coach."""
    known = {genome.norm(l.get("text", "")) for l in db[D.LESSONS].find({}, {"text": 1})}
    known |= {genome.norm(e.get("rule", "")) for l in db[D.LESSONS].find({}, {"evidence": 1})
              for e in l.get("evidence") or []}
    add, why = [], []
    for kind, key, lead, cap in (("mechanic_prior", "verified", "Held in an earlier game", 2),
                                 ("pitfall", "refuted", "Assumed and found false in an earlier game", 1)):
        taken = 0
        for r in ev["rulebook"][key]:
            rule = " ".join(str(r.get("rule", "")).split())
            text = f"{lead}: {rule.rstrip('.')}."
            # A game-specific rule must not use up the cap: it would be rejected on apply.
            if not rule or genome.norm(rule) in known or taken >= cap \
                    or genome.clean_lesson(text, ev["game_id"]) != text:
                continue
            known.add(genome.norm(rule))
            taken += 1
            add.append({"text": text, "kind": kind, "tags": [key, "rulebook"],
                        "evidence": [{"level": r.get("level"), "rule": rule}]})
    if add:
        why.append(f"promoted {len(add)} new rule(s) from the run's rule book")
    for kind, tags, applies, text in _TEMPLATES:
        if genome.norm(text) not in known and applies(ev):
            worst = min((l for l in ev["metrics"]["levels"] if l["efficiency"] is not None),
                        key=lambda l: l["efficiency"], default={"level": None})
            add.append({"text": text, "kind": kind, "tags": tags,
                        "evidence": [{"level": worst["level"], "rule": f"outcome: {tags[0]}"}]})
            why.append(f"added an outcome lesson ({tags[0]})")
            break
    # Retire a lesson that has been carried twice or more and scores under the parent generation: the
    # worst one only. One bad game lowers every carried lesson's record at once, and retiring them all
    # on it would empty the harness's memory in a single step.
    retire = []
    floor = (ev.get("parent_fitness") or {}).get("mean_efficiency")
    poor = [l for l in ev["lessons"] if l.get("status") == "active" and floor is not None
            and l["stats"]["injected"] >= 2 and l["stats"]["mean_efficiency"] is not None
            and l["stats"]["mean_efficiency"] < floor - 0.1]
    for l in sorted(poor, key=lambda l: (l["stats"]["mean_efficiency"], l["id"]))[:1]:
        s = l["stats"]
        retire.append({"id": l["id"], "reason": f"mean efficiency {s['mean_efficiency']} over "
                                                f"{s['injected']} runs, under the parent's {floor}"})
    eff, knobs = ev["metrics"].get("efficiency"), {}
    factor = float(ev["knobs"].get("coach_stuck_factor", 3.0))
    if eff is not None and eff < 0.6:
        knobs["coach_stuck_factor"] = factor - 0.5      # coach earlier while play is inefficient
    elif eff is not None and eff > 0.9:
        knobs["coach_stuck_factor"] = factor + 0.5
    if ev["status"] != "won" and ev["metrics"]["completion"] < 1.0:
        knobs["max_nudges"] = int(ev["knobs"].get("max_nudges", 0)) + 1
    eff_s = "n/a" if eff is None else f"{eff:.2f}"
    rationale = (f"Heuristic distillation of {ev['run_id']} (efficiency {eff_s}, "
                 f"{ev['metrics']['levels_cleared']}/{ev['metrics']['win_levels']} levels): "
                 + ("; ".join(why) if why else "no rule or outcome that is not already carried") + ".")
    return {"add_lessons": add, "retire_lessons": retire, "revise_lessons": [], "knob_changes": knobs,
            "rationale": rationale}


PROMPT = """You maintain the long-term memory of a harness that plays unfamiliar grid video games \
(ARC-AGI-3). One game has just finished. From the evidence below, decide how the harness should change \
before the NEXT, DIFFERENT game.

The evidence holds the run's score per level, the rule book the agent committed (verified, refuted, open), \
the agent's own notes (agent_notes), what it searched its memory for, and the lessons the harness carries.

Rules for lessons:
- A lesson must transfer to a game the agent has never seen. Never name a game, a level layout, a colour \
index tied to one game, or any coordinates.
- One or two sentences, phrased as a prior to test early, not a fact.
- kind is one of: mechanic_prior, strategy, pitfall, tooling.
- Add at most {max_add}. Prefer none over a vague one. Do not restate a lesson already carried, and do not \
hand back one listed under retired_lessons unless this run gives new evidence for it.
- Retire a carried lesson only if this run's evidence shows it misled or its record is poor.
- knob_changes may set budget_multiple ({bm}), max_nudges ({mn}), coach_stuck_factor ({cf}): only with a reason.

Reply with a single JSON object and nothing else:
{{"add_lessons":[{{"text":"","kind":"","tags":[""],"evidence":[{{"level":0,"rule":""}}]}}],
"retire_lessons":[{{"id":"","reason":""}}],"revise_lessons":[{{"id":"","text":""}}],
"knob_changes":{{}},"rationale":""}}

EVIDENCE:
{evidence}
"""


def parse_json_object(text: str) -> dict:
    """The first JSON object in a model reply, tolerating code fences and surrounding prose."""
    text = re.sub(r"```(?:json)?", "", str(text or "")).strip()
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in the reply")


def propose_claude(ev: dict, model: str | None = None, timeout: float = 600.0) -> tuple[dict, dict]:
    """One real CLI call. Returns (proposal, {"model", "cost_usd"})."""
    model = model or os.environ.get("MONGOARC_DISTILL_MODEL") or "claude-opus-5"
    shown = {k: v for k, v in ev.items() if k not in ("workspace",)}
    lo_hi = {k: f"{lo}..{hi}" for k, (lo, hi) in genome.KNOB_RANGE.items()}
    prompt = PROMPT.format(max_add=MAX_ADD, bm=lo_hi["budget_multiple"], mn=lo_hi["max_nudges"],
                           cf=lo_hi["coach_stuck_factor"],
                           evidence=json.dumps(D.jsonable(shown), indent=1)[:60000])
    cwd = D.REPO / ".local" / "distill" / f"{ev['run_id']}-{D.now().strftime('%Y%m%dT%H%M%S%f')}"
    cwd.mkdir(parents=True, exist_ok=False)
    proc = subprocess.run([str(D.REPO / "tools" / "claude_login.sh"), "-p", prompt, "--model", model,
                           "--output-format", "json"], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"distiller CLI exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
    reply = json.loads(proc.stdout)
    if isinstance(reply, list):                  # some CLI versions print the event list
        reply = next(e for e in reversed(reply) if isinstance(e, dict) and e.get("type") == "result")
    if reply.get("is_error"):
        raise RuntimeError(f"distiller CLI error: {str(reply.get('result'))[:300]}")
    return parse_json_object(reply.get("result")), {"model": model, "cost_usd": reply.get("total_cost_usd")}


def _dicts(value: Any) -> list[dict]:
    """The dict items of a list a model was asked for; anything else yields nothing."""
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def apply_proposal(db: Database, ev: dict, proposal: dict, distiller: dict) -> dict:
    """Validate a proposal against the active generation and store the child generation."""
    for attempt in range(5):
        try:
            return _apply(db, ev, proposal if isinstance(proposal, dict) else {}, distiller)
        except DuplicateKeyError:                # another loop took the generation or lesson id: recount
            if attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _apply(db: Database, ev: dict, proposal: dict, distiller: dict) -> dict:
    parent = genome.active(db)
    gen_id = db[D.GENERATIONS].find_one({}, sort=[("_id", -1)], projection={"_id": 1})["_id"] + 1
    carried = list(parent.get("lessons") or [])
    game, rejected, repeats = ev["game_id"], [], 0

    retired = {}
    for r in _dicts(proposal.get("retire_lessons")):
        if isinstance(r.get("id"), str) and r["id"] in carried:
            retired[r["id"]] = str(r.get("reason") or "retired by the distiller")[:300]
    revised = {}
    for r in _dicts(proposal.get("revise_lessons")):
        if isinstance(r.get("id"), str) and r["id"] in carried and r["id"] not in retired:
            text = genome.clean_lesson(r.get("text") if isinstance(r.get("text"), str) else "", game)
            if text:
                revised[r["id"]] = text
            else:
                rejected.append(str(r.get("text"))[:120])
    # Every lesson ever stored, carried or not: one retired by a revert must not come back unnoticed.
    seen = {genome.norm(l.get("text", "")) for l in db[D.LESSONS].find({}, {"text": 1})}
    added = []
    for a in _dicts(proposal.get("add_lessons")):
        if len(added) >= MAX_ADD:
            break
        text = genome.clean_lesson(a.get("text") if isinstance(a.get("text"), str) else "", game)
        if not text:
            rejected.append(str(a.get("text"))[:120])
            continue
        if genome.norm(text) in seen:
            repeats += 1
            continue
        seen.add(genome.norm(text))
        evid = [{"run_id": ev["run_id"], "game_id": game,
                 "level": e.get("level") if isinstance(e.get("level"), int) else None,
                 "rule": str(e.get("rule") or "")[:300]}
                for e in _dicts(a.get("evidence"))][:5]
        added.append((text, a.get("kind") if isinstance(a.get("kind"), str) else None,
                      [t for t in a["tags"] if isinstance(t, str)] if isinstance(a.get("tags"), list) else [],
                      evid or [{"run_id": ev["run_id"], "game_id": game, "level": None, "rule": ""}]))
    docs = [genome.new_lesson(lid, t, k, tags, evid, gen_id)
            for lid, (t, k, tags, evid) in zip(genome.next_lesson_ids(db, len(added)), added)]

    knobs, changes = {**genome.DEFAULT_KNOBS, **(parent.get("knobs") or {})}, {}
    proposed = proposal.get("knob_changes")
    for name, value in (proposed.items() if isinstance(proposed, dict) else ()):
        if name in genome.KNOB_RANGE and isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                new = genome.clamp_knob(name, value)
            except (ValueError, OverflowError):
                continue
            if new != knobs[name]:
                changes[name] = [knobs[name], new]
                knobs[name] = new

    rationale = str(proposal.get("rationale") or "")[:2000]
    if rejected:
        rationale += f" [{len(rejected)} proposed lesson(s) rejected as game-specific]"
    if repeats:
        rationale += f" [{repeats} proposed lesson(s) dropped as repeats of stored ones]"
    # `tools` is reserved and always empty (docs/SCHEMA.md): no script is carried from one game to the next.
    gen = {"_id": gen_id, "parent": parent["_id"], "created_at": D.now(), "created_from_run": ev["run_id"],
           "status": "active", "lessons": [i for i in carried if i not in retired] + [d["_id"] for d in docs],
           "knobs": knobs, "tools": [],
           "diff": {"added_lessons": [d["_id"] for d in docs], "retired_lessons": list(retired),
                    "revised_lessons": list(revised), "knob_changes": changes, "added_tools": []},
           "rationale": rationale, "distiller": distiller,
           "fitness": {"runs": [], "mean_efficiency": None, "mean_completion": None}}

    def write(session=None):
        if docs:
            db[D.LESSONS].insert_many(docs, session=session)
        for lid, reason in retired.items():
            db[D.LESSONS].update_one({"_id": lid}, {"$set": {"status": "retired", "retired_reason": reason,
                                                             "retired_gen": gen_id}}, session=session)
        for lid, text in revised.items():
            old = db[D.LESSONS].find_one({"_id": lid}, {"text": 1}, session=session)
            db[D.LESSONS].update_one({"_id": lid}, {"$set": {"text": text}, "$push": {
                "revisions": {"gen": gen_id, "was": old["text"]}}}, session=session)
        gen["addendum"] = genome.render_addendum(
            gen, [l for l in (db[D.LESSONS].find_one({"_id": i}, session=session) for i in gen["lessons"]) if l])
        db[D.GENERATIONS].update_many({"status": "active"}, {"$set": {"status": "superseded"}}, session=session)
        db[D.GENERATIONS].insert_one(gen, session=session)

    # One transaction, so there is never a moment with zero or two active generations.
    try:
        with db.client.start_session() as s:
            s.with_transaction(write)
    except OperationFailure as exc:
        if exc.code != 20:                       # 20: transactions need a replica set
            raise
        write()
    return db[D.GENERATIONS].find_one({"_id": gen_id})


def distill(db: Database, run_id: str, kind: str = "claude", model: str | None = None) -> dict:
    """Build, store and return the next generation from one run's evidence."""
    genome.ensure_gen0(db)
    ev = gather_evidence(db, run_id)
    heuristic = {"model": "none", "cost_usd": None, "kind": "heuristic"}
    if kind == "claude":
        try:
            proposal, meta = propose_claude(ev, model)
            return apply_proposal(db, ev, proposal, {**meta, "kind": "claude"})
        except Exception as exc:  # noqa: BLE001 -- a failed call or an unusable reply: use the heuristic
            heuristic["fallback_from"] = f"claude: {type(exc).__name__}: {str(exc)[:200]}"
            print(f"    distill: the claude distiller failed ({type(exc).__name__}); using the heuristic", flush=True)
    return apply_proposal(db, ev, propose_heuristic(db, ev), heuristic)
