# MongoDB schema

Database: `MONGO_DB` (default `mongoarc`). Names, indexes and the frame codec live in `mongoarc/db.py`.

The game agent never holds database credentials. It reads and writes its memory through a loopback gateway (`mongoarc.memory_gateway`, tool `memory.py` in its workspace), which restricts every call to the agent's own run. Three harness components share the database:

- the **collector** tails each run directory and writes `runs`, `actions`, `events`;
- the **memory gateway** writes `notes` and the `memory_write` / `recall` events, on the agent's behalf;
- the **dashboard** only reads.

Everything is scoped to a run. Nothing written for one run is read by another: a new game starts with an empty memory.

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
role             "train" | "control"     control = launched with --no-memory: plain CCARC, no memory tool
memory_tool      bool
baseline_actions [int]|null per-level human baseline action counts
status           "running" | "won" | "lost" | "finished" | "stopped"
started_at, updated_at, ended_at   datetime (ended_at null while running)
state            str   last game state: NOT_PLAYED | NOT_FINISHED | WIN | GAME_OVER
level            int   0-based index of the level being played (= levels cleared)
win_levels       int   total levels in the game
actions_used     int
level_costs      [int] actions spent per level in the current play
deaths, full_resets, wasted_actions   int
n_actions        int   rows ingested into `actions`
note_seq         int   counter behind the run's note ids (N1, N2, ...)
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
| `outbox` | `outbox_kind`, `text` | collector, from `harness_outbox.jsonl` |
| `memory_write` | `op` (save, update, delete), `note` (the note `_id`), `title`, `text` | memory gateway: the agent wrote to its memory |
| `recall` | `query`, `notes` ([ids] returned), `engine` (atlas_vector_search, text, recent, list), `stored`, `text` | memory gateway: the agent searched or listed its memory |
| `harness` | `what` (run_started, run_ended, memory_nudge), `text`, `level` | launcher |

`memory_nudge` is the one message the harness sends during a game: at each level boundary it tells the agent to save what the level taught it and to search its memory before acting on the new level.

## `notes`: the agent's memory, one document per note

Written by the agent through `memory.py` and nothing else. An Atlas Vector Search index (`db.VECTOR_INDEX`, automated embedding with `db.VECTOR_MODEL`, filter on `run_id`) is defined on `text`: Atlas embeds each note when it is written and embeds the question when the agent searches, so retrieval is raw text in and a plain-language question out. A text index on `text` is the fallback on a server without Atlas Vector Search.

```
_id        "<run_id>:mem/<n>"    n = 1, 2, ... per run; the agent sees it as N<n>
run_id, game_id
level      int   the level being played when the note was saved
path       "mem/<n>"
title      str   one line
text       str   the note (truncated to 20000 chars)
refs       [str] script filenames mentioned in the text
source     "tool"
version    int   1 when saved, +1 on every update
revisions  [{version, text, at}]   the last five earlier wordings
created_at, updated_at   datetime
```

A note is updated in place and deleted outright: `memory.py update` and `memory.py delete`. (The collector also mirrors any text file an agent leaves under `notes/` in its workspace, with `source` absent and `path` starting `notes/`; the doctrine tells the agent not to keep prose in files, and the gateway never edits those.)

## Memory bank

The dashboard presents `notes` as the memory bank (`db.MEMORY_BANKS`) and follows it through a change stream. Earlier designs of this project also wrote `rulebooks`, `lessons` and `generations`; nothing writes them now.
