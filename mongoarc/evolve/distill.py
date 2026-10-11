"""Turn one cleared level into the run's next harness generation.

distill_level(db, run_id, level, metrics, kind) reads what MongoDB recorded for
THIS run so far (rule book, notes, what each level cost, the run's lessons),
asks a distiller ("claude": one real CLI call; "heuristic": no model) what
carries to the later levels of the same game, validates the proposal and stores
the generation plus its lesson documents. Nothing from another run is read and
nothing written here reaches another run. A proposal that cannot be used,
whatever the reason, falls back to the heuristic: distilling must never stop a
game.
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

MAX_ADD = 3            # lessons one cleared level may add
MAX_NOTES = 6          # of the agent's notes shown to the distiller
NOTE_CHARS = 1500      # of each note
NOTE_SUFFIXES = (".md", ".txt", ".markdown", "")
DEFAULT_MODEL = "claude-sonnet-5-5"
CLI_TIMEOUT = 150.0    # levels can be a minute apart: a distiller slower than this is not worth waiting for


def level_metrics(level: int, actions: int | None, baseline: int | None) -> dict:
    """{level, actions, baseline, efficiency}: efficiency = min(1, baseline/actions), None with no baseline."""
    eff = None
    if baseline and actions:
        eff = round(min(1.0, float(baseline) / float(actions)), 4)
    return {"level": int(level), "actions": None if actions is None else int(actions),
            "baseline": None if baseline is None else int(baseline), "efficiency": eff}


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
    """The agent's own prose for this run: the `notes` bank, else the files under notes/ as they stand."""
    docs = [{"path": n.get("path"), "text": str(n.get("text") or "")[:NOTE_CHARS]}
            for n in db[D.NOTES].find({"run_id": run_id}, {"path": 1, "text": 1}).sort("path", 1).limit(MAX_NOTES)]
    if docs:
        return docs
    root = ws / "notes"
    for p in sorted(root.rglob("*")) if root.is_dir() else []:   # no collector running: read the directory
        if len(docs) >= MAX_NOTES:
            break
        if p.is_file() and p.suffix.lower() in NOTE_SUFFIXES and not p.name.startswith("."):
            try:
                text = p.read_bytes()[:NOTE_CHARS * 4].decode("utf-8", errors="ignore")[:NOTE_CHARS]
            except OSError:
                continue
            if text.strip():
                docs.append({"path": str(p.relative_to(ws)), "text": text})
    return docs


def gather_evidence(db: Database, run_id: str, level: int, metrics: dict | None = None) -> dict[str, Any]:
    """Everything the distiller is shown after `level` was cleared. All of it belongs to this run."""
    run = db[D.RUNS].find_one({"_id": run_id})
    if run is None:
        raise KeyError(f"no run {run_id}")
    gens = genome.lineage(db, run_id)
    gen = genome.active(db, run_id) or {}
    per_level = [g["played"] for g in gens if g.get("played")]
    if metrics and not any(m.get("level") == metrics.get("level") for m in per_level):
        per_level.append(metrics)
    book = db[D.RULEBOOKS].find_one({"run_id": run_id}, sort=[("version", -1)]) or {}
    counts = {int(c["_id"]): c for c in db[D.ACTIONS].aggregate([
        {"$match": {"run_id": run_id}},
        {"$group": {"_id": "$level", "deaths": {"$sum": {"$cond": ["$death", 1, 0]}},
                    "wasted": {"$sum": {"$cond": ["$wasted", 1, 0]}}}}]) if isinstance(c.get("_id"), int)}
    recalls = [{"level": e.get("level"), "query": str(e.get("query") or "")[:200]} for e in db[D.EVENTS].find(
        {"run_id": run_id, "kind": "recall"}).sort("seq", 1).limit(30)]
    ws = Path(run.get("out_dir") or "") / run["game_id"]
    carried = gen.get("lessons") or []
    lessons = [{"id": l["_id"], "text": l.get("text", ""), "kind": l.get("kind"), "level": l.get("level"),
                "recalled": int((l.get("stats") or {}).get("recalled") or 0)}
               for l in genome.lessons_of(db, gen) if l.get("status") == "active"]
    dropped = [{"id": l["_id"], "text": l.get("text", ""), "reason": l.get("retired_reason")}
               for l in db[D.LESSONS].find({"run_id": run_id, "status": "retired",
                                            "_id": {"$nin": carried}}).sort("_id", -1).limit(10)]
    win = run.get("win_levels") or len(run.get("baseline_actions") or []) or None
    return {"run_id": run_id, "game_id": run["game_id"], "workspace": str(ws),
            "level_cleared": int(level), "levels_in_game": win,
            "levels_left": None if win is None else max(0, int(win) - int(level) - 1),
            "level_metrics": metrics, "levels_so_far": per_level,
            "deaths_on_level": int((counts.get(int(level)) or {}).get("deaths") or 0),
            "wasted_on_level": int((counts.get(int(level)) or {}).get("wasted") or 0),
            "rulebook": {"verified": _rules(book.get("verified")), "refuted": _rules(book.get("refuted")),
                         "open_questions": [q[:300] for q in book.get("open_questions") or []
                                            if isinstance(q, str)] if isinstance(book.get("open_questions"), list)
                         else []},
            "agent_notes": _notes(db, run_id, ws), "memory_searches": recalls,
            "generation_k": gen.get("k"), "knobs": gen.get("knobs") or dict(genome.DEFAULT_KNOBS),
            "lessons": lessons, "retired_lessons": dropped}


def propose_heuristic(db: Database, ev: dict) -> dict:
    """A proposal with no model: promote the rules the agent committed for this level, add one outcome lesson."""
    run_id, level = ev["run_id"], ev["level_cleared"]
    known: set[str] = set()
    for l in db[D.LESSONS].find({"run_id": run_id}, {"text": 1, "evidence": 1}):
        known.add(genome.norm(l.get("text", "")))
        known |= {genome.norm(e.get("rule", "")) for e in l.get("evidence") or [] if isinstance(e, dict)}
    add, why = [], []
    for kind, key, lead, cap in (("mechanic_prior", "verified", "Verified on level {n}", 2),
                                 ("pitfall", "refuted", "Assumed and found false on level {n}", 1)):
        # The rules committed for the level just cleared first, then any older one not yet a lesson.
        entries = sorted(ev["rulebook"][key], key=lambda r: (r.get("level") != level, -(r.get("level") or 0)))
        taken = 0
        for r in entries:
            rule = " ".join(str(r.get("rule", "")).split())
            at = r.get("level") if isinstance(r.get("level"), int) else level
            text = genome.clean_lesson(f"{lead.format(n=at)}: {rule.rstrip('.')}.")
            if not rule or taken >= cap or genome.norm(rule) in known or not text:
                continue
            known.add(genome.norm(rule))
            taken += 1
            add.append({"text": text, "kind": kind, "tags": [key, "rulebook"], "evidence": [{"level": at, "rule": rule}]})
    if add:
        why.append(f"promoted {len(add)} rule(s) from the rule book")
    eff = (ev.get("level_metrics") or {}).get("efficiency")
    outcomes = [
        ("pitfall", "death", ev["deaths_on_level"] > 0,
         "A death on an earlier level of this game cost that level's progress: when an object you have not "
         "touched before appears, probe it from a position that is cheap to return to before committing a long "
         "sequence."),
        ("pitfall", "no-op", ev["wasted_on_level"] > 0,
         "Actions that left the board unchanged still counted on an earlier level of this game: after one "
         "no-op, do not repeat that action from the same state."),
        ("strategy", "planning", eff is not None and eff < 0.5,
         "An earlier level of this game cost far more actions than a direct solution: the mechanics carry over "
         "and only the layout changes, so read the new frame, plan the whole route with the rules already "
         "verified, and only then move."),
        ("strategy", "level-boundary", any("UNTESTED" in q for q in ev["rulebook"]["open_questions"]),
         "A level of this game was crossed with rules still marked untested: on the next level, settle one of "
         "them with a single cheap action early instead of carrying the doubt."),
    ]
    for kind, tag, applies, text in outcomes:
        if applies and genome.norm(text) not in known and len(add) < MAX_ADD:
            add.append({"text": text, "kind": kind, "tags": [tag, "outcome"],
                        "evidence": [{"level": level, "rule": f"outcome: {tag}"}]})
            why.append(f"added an outcome lesson ({tag})")
            break
    knobs = {}
    factor = float(ev["knobs"].get("coach_stuck_factor", 3.0))
    default = float(genome.DEFAULT_KNOBS["coach_stuck_factor"])
    if eff is not None and eff < 0.5:
        knobs["coach_stuck_factor"] = factor - 0.5      # coach earlier while play is inefficient
    elif eff is not None and eff > 0.9 and factor < default:
        knobs["coach_stuck_factor"] = min(default, factor + 0.5)   # efficient again: back toward the default
    m = ev.get("level_metrics") or {}
    rationale = (f"Heuristic distillation of level {level} ({m.get('actions')} actions, efficiency "
                 f"{'n/a' if eff is None else f'{eff:.2f}'}): "
                 + ("; ".join(why) if why else "no rule or outcome that is not already a lesson") + ".")
    return {"add_lessons": add, "retire_lessons": [], "revise_lessons": [], "knob_changes": knobs,
            "rationale": rationale}


PROMPT = """You maintain the working memory of a harness that is playing ONE unfamiliar grid video game \
(ARC-AGI-3) right now. The game has several levels. The agent has just cleared level {level} (0-based) and \
{left} level(s) remain. Later levels of the same game reuse the mechanics of earlier ones with a new, usually \
harder layout, often adding one new element. Decide what the agent must carry into the NEXT levels of THIS game.

The evidence below is everything recorded for this game so far and nothing else: what each cleared level cost \
against its baseline (efficiency = baseline / actions, 1.0 is as good as the reference), the rule book the agent \
committed at the level gates (verified, refuted, open), the agent's own notes, what it searched its memory for, \
and the lessons already in force.

Rules for lessons:
- Be concrete about THIS game: what each object looks like (colour, shape, size) and does, what each action \
does (ACTION1 .. ACTION7), what is fatal, what has to happen in which order, what the win condition is. A \
concrete statement the agent can act on in its first moves is worth more than general advice.
- Never hard-code one level's layout: no coordinates, no cell positions, no fixed move sequences. Layouts \
change between levels; mechanics do not.
- Do not state the baseline figures or the efficiency numbers in a lesson (the agent is not shown them). You \
may say in words that a level was wasteful and what would have saved the actions.
- Prefer what would have saved the most actions on the level just cleared, and what the rule book still marks \
open or untested and should be settled early on the next level.
- One or two sentences each. kind is one of: mechanic_prior, strategy, pitfall, tooling.
- Add at most {max_add}. At most {max_active} lessons may be in force: to add beyond that, retire or merge \
(revise) existing ones. Do not restate a lesson already in force; revise it instead when this level sharpened \
or corrected it. Retire one this level showed to be wrong.
- knob_changes may set coach_stuck_factor ({cf}; the harness sends a hint when a level has cost more than \
this multiple of its baseline): only with a reason.

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


def build_prompt(ev: dict) -> str:
    shown = {k: v for k, v in ev.items() if k not in ("workspace",)}
    lo, hi = genome.KNOB_RANGE["coach_stuck_factor"]
    return PROMPT.format(level=ev["level_cleared"], left="an unknown number of" if ev.get("levels_left") is None
                         else ev["levels_left"], max_add=MAX_ADD, max_active=genome.MAX_ACTIVE, cf=f"{lo}..{hi}",
                         evidence=json.dumps(D.jsonable(shown), indent=1)[:40000])


def propose_claude(ev: dict, model: str | None = None, timeout: float = CLI_TIMEOUT) -> tuple[dict, dict]:
    """One real CLI call. Returns (proposal, {"model", "cost_usd", "seconds"})."""
    model = model or os.environ.get("MONGOARC_DISTILL_MODEL") or DEFAULT_MODEL
    cwd = D.REPO / ".local" / "distill" / f"{ev['run_id']}-L{ev['level_cleared']}-{D.now().strftime('%Y%m%dT%H%M%S%f')}"
    cwd.mkdir(parents=True, exist_ok=False)
    t0 = time.monotonic()
    proc = subprocess.run([str(D.REPO / "tools" / "claude_login.sh"), "-p", build_prompt(ev), "--model", model,
                           "--output-format", "json"], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"distiller CLI exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}")
    reply = json.loads(proc.stdout)
    if isinstance(reply, list):                  # some CLI versions print the event list
        reply = next(e for e in reversed(reply) if isinstance(e, dict) and e.get("type") == "result")
    if reply.get("is_error"):
        raise RuntimeError(f"distiller CLI error: {str(reply.get('result'))[:300]}")
    return parse_json_object(reply.get("result")), {"model": model, "cost_usd": reply.get("total_cost_usd"),
                                                    "seconds": round(time.monotonic() - t0, 1)}


def _dicts(value: Any) -> list[dict]:
    """The dict items of a list a model was asked for; anything else yields nothing."""
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def apply_proposal(db: Database, ev: dict, proposal: dict, distiller: dict, memory: bool = True) -> dict:
    """Validate a proposal against the run's active generation and store the run's next generation."""
    for attempt in range(8):
        try:
            return _apply(db, ev, proposal if isinstance(proposal, dict) else {}, distiller, memory)
        except DuplicateKeyError:                # another run took the generation or lesson id: recount
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise AssertionError("unreachable")


def _apply(db: Database, ev: dict, proposal: dict, distiller: dict, memory: bool) -> dict:
    run_id, game, level = ev["run_id"], ev["game_id"], int(ev["level_cleared"])
    parent = genome.active(db, run_id) or genome.ensure_run_gen0(db, run_id, game, memory)
    carried = [l["_id"] for l in genome.lessons_of(db, parent) if l.get("status") == "active"]
    rejected, repeats, over = [], 0, 0

    retired = {}
    for r in _dicts(proposal.get("retire_lessons")):
        if isinstance(r.get("id"), str) and r["id"] in carried:
            retired[r["id"]] = str(r.get("reason") or "retired by the distiller")[:300]
    revised = {}
    for r in _dicts(proposal.get("revise_lessons")):
        if isinstance(r.get("id"), str) and r["id"] in carried and r["id"] not in retired:
            text = genome.clean_lesson(r.get("text"))
            if text:
                revised[r["id"]] = text
            else:
                rejected.append(str(r.get("text"))[:120])
    kept = [i for i in carried if i not in retired]
    # Every lesson this run ever stored, in force or not: a retired one must not come back unnoticed.
    seen = {genome.norm(l.get("text", "")) for l in db[D.LESSONS].find({"run_id": run_id}, {"text": 1})}
    seen |= {genome.norm(t) for t in revised.values()}
    added = []
    for a in _dicts(proposal.get("add_lessons")):
        text = genome.clean_lesson(a.get("text"))
        if not text:
            rejected.append(str(a.get("text"))[:120])
            continue
        if genome.norm(text) in seen:
            repeats += 1
            continue
        if len(added) >= MAX_ADD or len(kept) + len(added) >= genome.MAX_ACTIVE:
            over += 1
            continue
        seen.add(genome.norm(text))
        evid = [{"level": e.get("level") if isinstance(e.get("level"), int) else level,
                 "rule": str(e.get("rule") or "")[:300]} for e in _dicts(a.get("evidence"))][:5]
        added.append((text, a.get("kind") if isinstance(a.get("kind"), str) else None,
                      [t for t in a["tags"] if isinstance(t, str)] if isinstance(a.get("tags"), list) else [],
                      evid or [{"level": level, "rule": ""}]))

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
        rationale += f" [{len(rejected)} proposed lesson(s) rejected: empty, or pinned to one level's coordinates]"
    if repeats:
        rationale += f" [{repeats} proposed lesson(s) dropped as repeats of stored ones]"
    if over:
        rationale += f" [{over} proposed lesson(s) dropped: {MAX_ADD} per level, {genome.MAX_ACTIVE} in force]"
    gen_id = genome.next_gen_id(db)
    docs = [genome.new_lesson(lid, run_id, game, level, t, k, tags, evid, gen_id)
            for lid, (t, k, tags, evid) in zip(genome.next_lesson_ids(db, len(added)), added)]
    gen = {"_id": gen_id, "run_id": run_id, "game_id": game, "k": int(parent.get("k") or 0) + 1, "level": level,
           "parent": parent["_id"], "status": "active", "created_at": D.now(),
           "lessons": kept + [d["_id"] for d in docs], "knobs": knobs,
           "diff": {"added_lessons": [d["_id"] for d in docs], "retired_lessons": list(retired),
                    "revised_lessons": list(revised), "knob_changes": changes},
           "rationale": rationale, "distiller": distiller,
           "level_metrics": ev.get("level_metrics"), "played": None}

    def write(session=None):
        if docs:
            db[D.LESSONS].insert_many(docs, session=session)
        for lid, reason in retired.items():
            db[D.LESSONS].update_one({"_id": lid, "run_id": run_id},
                                     {"$set": {"status": "retired", "retired_reason": reason,
                                               "retired_gen": gen_id}}, session=session)
        for lid, text in revised.items():
            old = db[D.LESSONS].find_one({"_id": lid}, {"text": 1}, session=session)
            db[D.LESSONS].update_one({"_id": lid, "run_id": run_id}, {"$set": {"text": text}, "$push": {
                "revisions": {"gen": gen_id, "level": level, "was": old["text"]}}}, session=session)
        gen["addendum"] = genome.render_addendum(
            gen, [l for l in (db[D.LESSONS].find_one({"_id": i}, session=session) for i in gen["lessons"]) if l],
            memory=memory)
        db[D.GENERATIONS].update_many({"run_id": run_id, "status": "active"},
                                      {"$set": {"status": "superseded"}}, session=session)
        db[D.GENERATIONS].insert_one(gen, session=session)
        db[D.RUNS].update_one({"_id": run_id}, {"$set": {"generation": gen_id, "gen_k": gen["k"],
                                                         "lessons_used": gen["lessons"]}}, session=session)

    # One transaction, so the run never has zero or two active generations.
    try:
        with db.client.start_session() as s:
            s.with_transaction(write)
    except OperationFailure as exc:
        if exc.code != 20:                       # 20: transactions need a replica set
            raise
        write()
    return db[D.GENERATIONS].find_one({"_id": gen_id})


def distill_level(db: Database, run_id: str, level: int, metrics: dict | None = None, kind: str = "claude",
                  model: str | None = None, memory: bool = True) -> dict:
    """Build, store and return the run's next generation from the level it has just cleared."""
    ev = gather_evidence(db, run_id, level, metrics)
    heuristic = {"model": "none", "cost_usd": None, "kind": "heuristic"}
    if kind == "claude":
        try:
            proposal, meta = propose_claude(ev, model)
            return apply_proposal(db, ev, proposal, {**meta, "kind": "claude"}, memory)
        except Exception as exc:  # noqa: BLE001 -- a failed call or an unusable reply: use the heuristic
            heuristic["fallback_from"] = f"claude: {type(exc).__name__}: {str(exc)[:200]}"
            print(f"    distill: the claude distiller failed ({type(exc).__name__}); using the heuristic", flush=True)
    return apply_proposal(db, ev, propose_heuristic(db, ev), heuristic, memory)
