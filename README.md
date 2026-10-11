# MongoARC

**A harness that rewrites itself between levels.** An agent (Claude Code) plays an [ARC-AGI-3](https://three.arcprize.org) game it has never seen. Everything it does is recorded in MongoDB Atlas. Each time it clears a level, the harness reads that record, distils what will matter on the levels still to come, and builds the next *generation* of itself: the lessons section of the agent's prompt, its launch settings, and what the agent's memory search returns.

Built for the Harness Engineering & Model Wrangling hackathon (SF Tech Week, 2026-10-11), problem statement 1, *Recursive Harnessing*.

## Why per level, and not per game

ARC-AGI-3 environments are designed to be novel: what an agent works out in one game is not supposed to carry into another. What it works out on one *level* does carry into the later levels of the same game, which reuse the mechanics over harder layouts. So the unit of self-improvement here is the level. Every game starts in a brand-new workspace with an empty memory and its own generation 0; nothing crosses from one game to the next.

## The loop

```
   launch a game  →  generation 0 of this run: empty lessons, default settings
                            │
                            ▼
   ARC-AGI-3 game  ◄──►  Claude Code agent  ──(memory.py recall)──►  memory gateway ─┐
                            │ writes trace, reasoning, rule book, notes/              │
                            ▼                                                        ▼
                        collector  ───────────────►  MongoDB Atlas  ◄────────────────┘
                                                     runs · actions · events · rulebooks · notes
            level k cleared ┌───────────────────────────┘
                            ▼
      score the level (actions spent against the human baseline)
                            ▼
      distil (Claude reads this run's rule book, notes and per-level scores)
                            ▼
      generation k+1 of this run:  rewrite the lessons section of the agent's prompt,
      send the change into the live session, tell the agent to search its memory
                            │
                            └──►  the agent plays level k+1 under generation k+1
```

What changes from one generation to the next:

| Part of the harness | How it evolves |
|---|---|
| **Rules** | Lessons about this game, distilled from the rule book the agent commits at each level gate, with the evidence (level, rule) each came from. Lessons are retired or revised as later levels contradict them. |
| **Context policy** | The lessons section of the workspace `CLAUDE.md` is rewritten in place at every level. Claude Code re-injects that file after a context compaction, so the distilled lessons survive it. A coach watches the action count on the current level and, when the agent is stuck, sends the most relevant of this run's lessons into the live session. |
| **Guardrails** | Launch settings (action budget multiple, nudge count, the coach's stuck threshold) are part of the generation and can be changed by the distiller. |
| **Memory** | The agent's `memory.py recall <words>` searches only this run: its own notes, the rules it verified, and the lessons from the levels it has cleared. At every level boundary the harness prompts the agent to search before it acts on the new level. Prose the agent writes under `notes/` is mirrored into MongoDB as it is written, edited or deleted; code under `scripts/` is never stored. |

The hard metric is the per-level curve inside a game: actions spent on a level against that level's human baseline.

## How MongoDB Atlas is used

MongoDB is the harness's memory of record; nothing about a run lives only on disk.

- `actions`: every move with its 64x64 frames, so any game can be replayed frame by frame.
- `events`: one ordered timeline per run of the agent's reasoning, code, tool results, rule-book changes, harness interventions and memory searches.
- `rulebooks`: a snapshot each time the agent commits what it has verified or refuted.
- `notes` (text-indexed): the agent's own prose, one document per file under `notes/`, updated in place when the file changes and deleted when the file is deleted.
- `lessons` (text-indexed) and `generations`: the evolving harness itself, per run, with diffs, rationale and per-level scores.
- The agent reaches memory through a loopback gateway (`memory.py recall` runs `$text` searches scoped to its own run), so it never holds database credentials.
- The dashboard's Memory tab is fed by a MongoDB change stream, so a document appears, changes or disappears on screen as it does in the database.

Schema: [docs/SCHEMA.md](docs/SCHEMA.md).

## The dashboard

`python -m mongoarc.view` serves a page that reads only from MongoDB.

- **Live play:** the game board as it is played, or replayed at any speed; an on-screen controller that lights the button the agent pressed and marks each pixel it clicks; the agent's reasoning and code; the rule book growing at each level; the memory banks with live counts and the notes this run has written.
- **Evolution:** for one game, each generation's diff and the distiller's rationale, and actions against the human baseline level by level, with a no-memory control run of the same game overlaid when there is one.
- **Memory:** the four memory banks (lessons, notes, rule books, generations). Open one to read its documents, raw JSON included, updating as MongoDB reports each insert, update and delete.

A walkthrough for a demo is in [docs/DEMO.md](docs/DEMO.md).

## Run it

Requirements: Python 3.11+, `pip install pymongo fastapi uvicorn numpy pillow`, a MongoDB connection string, and the `claude` CLI for real agents.

```bash
git clone --recurse-submodules https://github.com/dastin359/MongoARC && cd MongoARC
cp .env.example .env          # set MONGODB_URI (and ARC_API_KEY for real games)
python -m mongoarc.view --port 8787 &
```

Zero cost (a scripted stand-in agent on a bundled toy game, heuristic distiller):

```bash
python3 tools/mock_arc.py 18765 &
python -m mongoarc.launch --game mk01-toy --out-dir runs/demo-fake --agent fake \
    --arc-root http://127.0.0.1:18765 --collect --distiller heuristic
```

A real agent on a toy game, with Claude as the distiller:

```bash
python -m mongoarc.launch --game mk05-doors --out-dir runs/demo-toy --agent claude \
    --model claude-sonnet-5-5 --arc-root http://127.0.0.1:18765 --collect
```

A real ARC-AGI-3 game (needs `ARC_API_KEY`; list ids with `PYTHONPATH=CCARC/src python3 -m ccarc arc-3 games`):

```bash
python -m mongoarc.launch --game sb26-7fbdac44 --out-dir runs/sb26 --agent claude \
    --model claude-opus-5-5 --collect --timeout 1500
```

A control run of the same game (no memory tool, no lessons, no evolution) for comparison:

```bash
python -m mongoarc.launch --game sb26-7fbdac44 --out-dir runs/sb26-control --agent claude \
    --model claude-opus-5-5 --no-memory --role control --collect --timeout 1500
```

## What is new here and what is not

- **Not new:** [CCARC](https://github.com/dastin359/CCARC), the driver that runs Claude Code against the ARC-AGI-3 API. It is included as an unmodified git submodule and is prior work. MongoARC wraps it from the outside; no file in it is changed. The agent's prompt is changed only in each game's workspace copy.
- **Built during the hackathon:** everything else in this repository: the collector, the MongoDB schema, the per-level evolution loop (score, distil, rewrite, inject), the mid-game coach, the agent memory gateway and tool, the notes sync, the dashboard, and the toy-game suite used to exercise the loop at no cost.

The toy games in `tools/mock_arc.py` are not ARC-AGI-3. They share its API shape and have several levels that reuse mechanics, which is what the loop needs to be exercised.

For the hackathon the CCARC sandbox guards (network isolation, filesystem confinement) are turned off, and the prompt says so. Results produced this way are demonstrations, not benchmark scores.
