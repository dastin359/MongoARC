"""Score a run from what the collector stored, and read its per-level curve."""
from __future__ import annotations

from typing import Any, Iterable

from pymongo.database import Database

from mongoarc import db as D


def last_play_start(rows: Iterable[dict]) -> int:
    """`n` of the row that opened the final play (the last full reset), else the first row's."""
    rows = list(rows)
    cut = rows[0]["n"] if rows else 0
    for r in rows:
        if r.get("full_reset") or r.get("new_play"):
            cut = r["n"]
    return cut


def level_counts(rows: Iterable[dict]) -> tuple[dict[int, int], int]:
    """(actions per level, levels cleared) for the final play, in plain Python.

    An action counts toward the level it was taken from (the previous row's
    level); the RESET that opens the play is not counted. Reference for the
    aggregation in `_counts_from_db`, and what the tests check it against.
    """
    rows = sorted(rows, key=lambda r: r["n"])
    cut = last_play_start(rows)
    kept = [r for r in rows if r["n"] >= cut]
    counts: dict[int, int] = {}
    prev = 0
    for i, r in enumerate(kept):
        if r["action"] == "RESET" and (i == 0 or r.get("full_reset")):
            prev = r["level"]
            continue
        counts[prev] = counts.get(prev, 0) + 1
        prev = r["level"]
    return counts, max((r["level"] for r in kept), default=0)


def _counts_from_db(db: Database, run_id: str) -> tuple[dict[int, int], int]:
    """The same count as `level_counts`, as one aggregation over `actions`."""
    acts = db[D.ACTIONS]
    first = acts.find_one({"run_id": run_id}, sort=[("n", 1)], projection={"n": 1})
    if first is None:
        return {}, 0
    reset = acts.find_one({"run_id": run_id, "$or": [{"full_reset": True}, {"new_play": True}]},
                          sort=[("n", -1)], projection={"n": 1})
    cut = reset["n"] if reset else first["n"]
    rows = acts.aggregate([
        {"$match": {"run_id": run_id, "n": {"$gte": cut}}},
        {"$setWindowFields": {"partitionBy": "$run_id", "sortBy": {"n": 1}, "output": {
            "from_level": {"$shift": {"output": "$level", "by": -1, "default": 0}}}}},
        {"$facet": {
            "per_level": [
                {"$match": {"$nor": [{"action": "RESET", "$or": [{"n": cut}, {"full_reset": True}]}]}},
                {"$group": {"_id": "$from_level", "actions": {"$sum": 1}}}],
            "cleared": [{"$group": {"_id": None, "level": {"$max": "$level"}}}]}}])
    out = next(iter(rows))
    counts = {int(r["_id"]): int(r["actions"]) for r in out["per_level"]}
    return counts, int(out["cleared"][0]["level"]) if out["cleared"] else 0


def build_metrics(counts: dict[int, int], cleared: int, baseline: list[int] | None,
                  win_levels: int | None) -> dict[str, Any]:
    """`runs.metrics` per SCHEMA.md from per-level action counts."""
    baseline = list(baseline or [])
    n = int(win_levels or len(baseline) or cleared)
    levels, effs = [], []
    for i in range(n):
        acts, base, done = counts.get(i, 0), (baseline[i] if i < len(baseline) else None), i < cleared
        eff = None
        if base is not None:
            eff = round(min(1.0, base / acts), 4) if done and acts > 0 else 0.0
            effs.append(eff)
        levels.append({"level": i, "actions": acts, "baseline": base, "cleared": done, "efficiency": eff})
    return {"levels": levels, "levels_cleared": min(cleared, n), "win_levels": n,
            "actions_total": sum(counts.values()), "baseline_total": sum(baseline) if baseline else None,
            "efficiency": round(sum(effs) / len(effs), 4) if effs else None,
            "completion": round(min(cleared, n) / n, 4) if n else 0.0}


def evaluate_run(db: Database, run_id: str) -> dict[str, Any]:
    """Compute and store `runs.metrics` for one run."""
    run = db[D.RUNS].find_one({"_id": run_id}, projection={"baseline_actions": 1, "win_levels": 1})
    if run is None:
        raise KeyError(f"no run {run_id}")
    counts, cleared = _counts_from_db(db, run_id)
    metrics = build_metrics(counts, cleared, run.get("baseline_actions"), run.get("win_levels"))
    db[D.RUNS].update_one({"_id": run_id}, {"$set": {"metrics": metrics}})
    return metrics


def level_curve(db: Database, run_id: str) -> list[dict[str, Any]]:
    """The hard metric: per level of one game, actions against the baseline and the generation it was played under.

    Read from the run's own generation lineage (`played` is filled in when the
    level is cleared), so a level still in progress is not in the curve.
    """
    out = []
    for g in db[D.GENERATIONS].find({"run_id": run_id, "played": {"$ne": None}}).sort("k", 1):
        out.append({**g["played"], "k": g.get("k"), "generation": g["_id"], "lessons": len(g.get("lessons") or [])})
    return out
