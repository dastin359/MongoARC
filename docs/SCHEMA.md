# MongoDB schema

Database: `MONGO_DB` (default `mongoarc`). Names, indexes and the frame codec live in `mongoarc/db.py`.

The game agent never holds database credentials. It reaches its memory through a loopback gateway (`mongoarc.memory_gateway`, tool `memory.py` in its workspace) that searches the `lessons`, `notes` and `rulebooks` of its own run and appends a `recall` event per search. It writes memory only by saving prose under `notes/`, which the collector mirrors. Three harness components share the database:

- the **collector** tails each run directory and writes `runs`, `actions`, `events`, `rulebooks`, `notes`;
- the **launcher** and its **level evolver** write `lessons`, `generations`, the generation fields of `runs`, and `harness` events;
- the **dashboard** only reads.

Everything is scoped to a run. Nothing written for one run is read by another: a new game starts with an empty memory and its own generation 0.

All timestamps are timezone-aware UTC `datetime`s (`db.now()`).

## Run identity

A run is one game played in one attempt directory (`<out_dir>`; the workspace is `<out_dir>/<game_id>/`). The launcher writes `<out_dir>/mongoarc_run.json` before the game starts and the collector takes `run_id` and the launch fields from it. Run ids are `<game_id>-<UTC timestamp>` unless `--run-id` is given.

## `runs`: one document per run

```
_id              str   run_id
game_id          str
out_dir          str   absolute path of the attempt directory
label            str|null
agent            "claude" | "fake"
model, effort    str|null
role             "train" | "control"     control = launched with --no-memory: no memory tool, no level lessons
memory_tool      bool
distiller        "claude" | "heuristic" | null (control)
generation       int   _id of the run's active generation; changes each time a level is cleared
gen_k            int   that generation's position in the run's lineage (0 at launch)
lessons_used     [str] the active generation's lessons
baseline_actions [int]|null per-level human baseline action counts
status           "running" | "won" | "lost" | "finished" | "stopped"
started_at, updated_at, ended_at   datetime (ended_at null while running)
state            str   last game state: NOT_PLAYED | NOT_FINISHED | WIN | GAME_OVER
level            int   0-based index of the level being played (= levels cleared)
win_levels       int   total levels in the game
actions_used     int
level_costs      [int] actions spent per level in the current play
gate_pending     int|null
deaths, full_resets, wasted_actions   int
n_actions        int   rows ingested into `actions`
last_seq         int   highest `seq` issued for this run
cost_usd, turns  number|null   from the stream's result events
result           object|null   the workspace result.json, verbatim
metrics          object|null   written by `python -m mongoarc.evolve evaluate` or the multi-game loop
cursors          object  collector bookkeeping (file offsets); ignore
```

`metrics`:

```
{"levels": [{"level": 0, "actions": 13, "baseline": 10, "cleared": true, "efficiency": 0.77}, ...],
 "levels_cleared": 3, "win_levels": 3, "actions_total": 52, "baseline_total": 36,
 "efficiency": 0.71,          # mean over ALL levels of min(1, baseline/actions), 0 for uncleared
 "completion": 1.0}           # levels_cleared / win_levels
```

## `actions`: one document per ledger row

```
_id        "<run_id>:<n>"
run_id     str
n          int   0-based row number in trace.jsonl (monotonic; the replay key)
i          int   the client's own action index (data only)
seq        int   position on the run's shared timeline (see `events`)
level      int   levels completed after this action
action     "RESET" | "ACTION1".."ACTION7"
params     {} | {"x": int, "y": int}
state      str
full_reset bool
available_actions [str]
frames     [str]  each frame is 4096 hex digits (64x64, row-major, palette index per cell);
                  several frames are the animation of one action, the last is the resulting board;
                  [] means a wasted action (board unchanged)
level_cleared, new_play, death, wasted   bool
observed_at   datetime  when the collector saw the row (rows carry no timestamp)
```

## `events`: the run's timeline, everything that is not a board

One ordered feed per run. `seq` is a per-run counter shared with `actions.seq` and issued by `db.next_seq` (an atomic `$inc` on `runs.last_seq`), so several writers never collide.

```
_id "<run_id>:<seq>", run_id, seq, at, kind, action_n, level, ...payload by kind
```

| kind | payload | written by |
|---|---|---|
| `thinking`, `text` | `text` | collector, from the agent's stream |
| `tool_use` | `tool`, `tool_use_id`, `command`, `description` | collector |
| `tool_result` | `tool_use_id`, `text` (truncated), `is_error` | collector |
| `user_msg` | `text`: a message the harness sent into the live session | collector |
| `system`, `result` | `subtype`, `text` / `cost_usd`, `turns`, `duration_ms`, `is_error` | collector |
| `outbox` | `outbox_kind` (level_cleared, note, ...), `text` | collector, from `harness_outbox.jsonl` |
| `rulebook` | `version`, `added` ({verified, refuted, open_questions}) | collector |
| `recall` | `query`, `lessons` ([ids]), `notes` ([ids]), `rules` (count), `text` | memory gateway: the agent searched its memory |
| `harness` | `what`, `text`, `lessons`, plus the fields below | launcher, level evolver, coach |

`harness` events by `what`:

- `run_started`, `run_ended`: launch and ending of the run.
- `memory_nudge` {`level`: the level being started, `delivered`}: the harness told the agent to search its memory before acting on a new level.
- `generation_created` {`generation`, `k`, `level`: the cleared level, `lessons`: ids added, `distiller`, `level_metrics`}.
- `lessons_injected` {`generation`, `k`, `lessons`, `items`: [{id, kind, text}] all lessons active in the new generation, `changed`: ids added or revised, `delivered`, `claude_md`}.
- `coach_note`: the mid-level coach sent lessons because the agent was stuck.
- `evolution_skipped`: a failure inside the evolver, logged and never passed on to the game.

## `rulebooks`: one snapshot per change of rules.json

```
_id "<run_id>:<version>", run_id, version (1-based), at, action_n, level
verified, refuted   [{rule, scope, level, note}]
open_questions      [str]
added               entries new in this version
```

## `notes`: the agent's own written memory, one document per text file

The agent keeps prose in `<workspace>/notes/` and code in `<workspace>/scripts/`. Only the prose is pushed: the collector mirrors every text file under `notes/` and keeps it in sync. A changed file updates its document in place (`version` + 1); a deleted file deletes its document. Scripts are never stored; a note refers to a script by filename, extracted into `refs`. Text index on `text` and `path`.

```
_id        "<run_id>:<path relative to the workspace>"
run_id, game_id, generation (int|null)
path       str   always under notes/
text       str   file content (truncated to 20000 chars)
refs       [str] script filenames mentioned in the text
sha        str   hash of the file bytes, for restart-safe change detection
version    int   1 on first sight, +1 on every content change
created_at, updated_at   datetime
```

## `lessons`: what the cleared levels of one game taught

Distilled by the harness each time a level is cleared, for use on the later levels of the same game. Text index on `text` and `tags`.

```
_id         "L0001"...   (collection-wide counter)
run_id, game_id
level       int   the level after which it was learned
text        str   concrete about this game; no single level's layout or coordinates
kind        "mechanic_prior" | "strategy" | "pitfall" | "tooling"
tags        [str]
evidence    [{level, rule}]
created_gen int   generation _id that introduced it
created_at  datetime
status      "active" | "retired"
retired_reason, retired_gen
revisions   [{gen, level, was}]   earlier wordings, when a later level revised it
stats       {"recalled": int}     times the agent's memory search returned it
```

## `generations`: versions of the harness, one lineage per run

```
_id            int   unique in the collection
run_id, game_id
k              int   0 at launch, +1 each time a level is cleared
level          int|null   the level whose clearing produced this generation (null for k=0)
parent         int|null   _id of generation k-1 of the same run
status         "active" | "superseded"   exactly one active per run
created_at     datetime
lessons        [str]  lesson ids active for the run when it was created
addendum       str    the lessons section as written into the workspace CLAUDE.md
knobs          {"budget_multiple", "max_nudges", "coach_stuck_factor"}
diff           {"added_lessons", "retired_lessons", "revised_lessons", "knob_changes"}
rationale      str    the distiller's explanation
distiller      {"model", "cost_usd", "kind": "claude" | "heuristic", "seconds", "fallback_from"}
level_metrics  {"level", "actions", "baseline", "efficiency"} | null   the level that produced it
played         {"level", "actions", "baseline", "efficiency"} | null   the level played under it, filled when cleared
```

`efficiency` is `min(1, baseline / actions)`, or null when the baseline is unknown.

## Memory banks

The dashboard presents four collections as memory banks (`db.MEMORY_BANKS`): `lessons`, `notes`, `rulebooks` and `generations`. All are plain collections: documents are inserted, updated in place and deleted as the memory changes, and the Memory tab follows them through a change stream.
