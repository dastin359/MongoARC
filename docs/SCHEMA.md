# MongoDB schema

Database: `MONGO_DB` (default `mongoarc`). Names, indexes and the frame codec live in `mongoarc/db.py`; import them, do not retype them.

The game agent never holds database credentials. It reaches its long-term memory through a loopback gateway (`mongoarc.memory_gateway`, tool `memory.py` in its workspace) that searches `lessons`, `notes` and `rulebooks` and appends a `recall` event per search. It writes memory only by saving prose under `notes/`, which the collector mirrors. Three harness components share the database:

- the **collector** tails each run directory and writes `runs`, `actions`, `events`, `rulebooks`, `notes`;
- the **evolution loop** reads those, and writes `lessons`, `generations` and the `generation`/`metrics` fields of `runs`;
- the **live view** only reads.

All timestamps are timezone-aware UTC `datetime`s (`db.now()`).

## Run identity

A run is one game played in one attempt directory (`<out_dir>`; the workspace is `<out_dir>/<game_id>/`). One attempt directory holds exactly one game.

The launcher writes `<out_dir>/mongoarc_run.json` before the game starts:

```json
{"run_id": "g3-mk02-walls-20261011T141502", "game_id": "mk02-walls", "generation": 3,
 "lessons_used": ["L0004", "L0007"], "agent": "claude", "model": "claude-opus-5", "effort": "high",
 "baseline_actions": [10, 12, 14], "label": "evolve-1"}
```

The collector takes `run_id` and the other fields from that marker. With no marker it derives `run_id` as `<out_dir name>-<game_id>` and leaves `generation` null.

## `runs`: one document per run

```
_id              str   run_id
game_id          str
out_dir          str   absolute path of the attempt directory
label            str|null   the evolve session this run belongs to
agent            "claude" | "fake"
model, effort    str|null
generation       int|null   harness generation the workspace was built from
lessons_used     [str]      lesson ids injected into this run's CLAUDE.md
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
metrics          object|null   written by the evolution loop (see below)
cursors          object  collector bookkeeping (file offsets); ignore
```

`status`: `running` while the game is in flight; `won` on state WIN; when the driver exits without a win it is `finished` (cleared at least one level) or `lost` (none); `stopped` if the collector saw the run go stale. The launcher sets the final status when the driver returns.

`metrics` (written by `mongoarc.evolve`):

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
level_cleared bool   level > previous row's level
new_play      bool   full_reset or level dropped
death         bool   this row entered GAME_OVER
wasted        bool   frames == []
observed_at   datetime  when the collector saw the row (rows carry no timestamp)
```

## `events`: the run's timeline, everything that is not a board

One ordered feed per run. `seq` is a per-run counter issued by the collector and shared with `actions.seq`, so merging the two by `seq` gives the true order.

```
_id      "<run_id>:<seq>"
run_id   str
seq      int
at       datetime
kind     see below
action_n int   n of the latest action row ingested when this event was seen (-1 before the first)
level    int   level at that moment
...payload by kind
```

| kind | payload | source |
|---|---|---|
| `thinking` | `text` | stream assistant thinking block |
| `text` | `text` | stream assistant text block |
| `tool_use` | `tool` (name), `tool_use_id`, `command` (Bash command or a short rendering of the input), `description` | stream tool_use block |
| `tool_result` | `tool_use_id`, `text` (truncated to 4000 chars), `is_error` | stream user tool_result |
| `user_msg` | `text` | a user message the harness sent into the session (live mode replays them) |
| `system` | `subtype`, `text` | stream system events (init, api_retry) |
| `result` | `cost_usd`, `turns`, `duration_ms`, `is_error`, `text` | stream result event |
| `outbox` | `outbox_kind` (level_cleared, note, ...), `text` | `<out_dir>/harness_outbox.jsonl` |
| `rulebook` | `version`, `added` ({verified, refuted, open_questions}) | rules.json changed |
| `harness` | `what` (e.g. `lessons_injected`, `coach_note`, `run_started`, `run_ended`), `text`, `lessons` ([ids]) | written by the launcher, not the collector |
| `recall` | `query`, `lessons` ([ids] returned), `notes` ([ids] returned), `text` | the agent searched long-term memory (`memory.py recall ...`); written by `mongoarc.memory_gateway` |

The launcher writes `harness` events itself. To keep `seq` unique it does not pick a number: it calls `mongoarc.db.next_seq(db, run_id, count=1)`, an atomic `$inc` on `runs.last_seq` that returns the first reserved number (the first seq of a run is 1). The collector issues its sequence numbers the same way.

## `rulebooks`: one snapshot per change of rules.json

```
_id      "<run_id>:<version>"
run_id, version (int, 1-based), at, action_n, level
verified [{rule, scope, level, note}]
refuted  [{rule, scope, level, note}]
open_questions [str]
added    {"verified": [...], "refuted": [...], "open_questions": [...]}   entries new in this version
```

## `notes`: the agent's own written memory, one document per text file

The agent keeps prose in `<workspace>/notes/` (markdown or plain text) and code in `<workspace>/scripts/`. Only the prose is pushed: the collector mirrors every text file under `notes/` into this collection and keeps it in sync. A changed file updates its document in place (`version` + 1); a deleted file deletes its document. Scripts are never pushed. A note refers to a script by its filename, which the collector extracts into `refs`. Text index on `text` and `path`.

```
_id        "<run_id>:<path relative to the workspace>"     e.g. "g01-mk03-lava-...:notes/mechanics.md"
run_id, game_id, generation (int|null)
path       str   relative to the workspace, always under notes/
text       str   file content (truncated to 20000 chars)
refs       [str] script filenames mentioned in the text (e.g. "scripts/bfs.py")
version    int   1 on first sight, +1 on every content change
created_at, updated_at   datetime
```

Text files are those ending in .md, .txt, .markdown, or with no extension, that decode as UTF-8; everything else under `notes/` (PNG, .py, .json, .jsonl) is skipped.

## Memory banks

The live view presents four collections as the harness's memory banks (`db.MEMORY_BANKS`): `lessons` (distilled, cross-game), `notes` (the agent's prose, per game), `rulebooks` (what each game's agent verified and refuted) and `generations` (the harness's own versions). All are plain collections: documents are inserted, updated in place and deleted as the memory changes.

## `lessons`: the harness's long-term memory

Game-independent statements distilled from rule books and outcomes. Text index on `text` and `tags`.

```
_id         "L0001"...
text        str   the lesson, one or two sentences, names no game and no coordinates
kind        "mechanic_prior" | "strategy" | "pitfall" | "tooling"
tags        [str]
evidence    [{run_id, game_id, level, rule}]   what it was distilled from
created_gen int   generation that introduced it
created_at  datetime
status      "active" | "retired"
retired_reason str|null
stats       {"runs": [run_id], "injected": int, "efficiency_sum": float, "wins": int,
             "recalled": int}   # recalled: times the agent pulled it through memory.py
```

## `generations`: versions of the harness itself

A generation is everything the harness adds around a game: the lessons it carries, the doctrine addendum built from them, the launch knobs, and helper tools promoted from earlier solvers' notes.

```
_id          int   0, 1, 2, ...   (generation 0 is the empty harness: no lessons)
parent       int|null
created_at   datetime
created_from_run  str|null   the run whose evidence produced this generation
status       "active" | "superseded" | "reverted"
lessons      [str]  lesson ids carried
addendum     str    the markdown appended to the workspace CLAUDE.md
knobs        {"budget_multiple": float, "max_nudges": int, "coach_stuck_factor": float, ...}
tools        []   reserved; always empty: every game starts from a fresh workspace and no scripts are carried over
diff         {"added_lessons": [ids], "retired_lessons": [ids], "revised_lessons": [ids],
              "knob_changes": {name: [old, new]}, "added_tools": [names]}
rationale    str   the distiller's explanation of the change
distiller    {"model": str, "cost_usd": number|null, "kind": "claude" | "heuristic"}
fitness      {"runs": [run_id], "mean_efficiency": float|null, "mean_completion": float|null}
```

Exactly one generation has `status: "active"`; it is the one the next game is launched with.
