# MongoARC

**A harness that rewrites itself between games.** An agent (Claude Code) plays [ARC-AGI-3](https://three.arcprize.org) games it has never seen. Everything it does is recorded in MongoDB Atlas. After each game the harness reads that record, distils what transferred, and builds the next *generation* of itself: the lessons it carries, the section it adds to the agent's prompt, and its launch knobs. A generation that scores worse than its parent on the hard metric is reverted.

Built for the Harness Engineering & Model Wrangling hackathon (SF Tech Week, 2026-10-11), problem statement 1, *Recursive Harnessing*.

## The loop

```
            ┌──────────────── generation N (MongoDB: generations, lessons) ───────────────┐
            │  carried lessons · prompt addendum · knobs                                  │
            └───────────────┬─────────────────────────────────────────────────────────────┘
                            │ launch: inject into the game workspace
                            ▼
   ARC-AGI-3 game  ◄──►  Claude Code agent  ──(memory.py recall)─────────►  memory gateway ─┐
                            │ writes trace, reasoning, rule book, notes/                   │
                            ▼                                                              ▼
                        collector  ───────────────►  MongoDB Atlas  ◄──────────────────────┘
                                                     runs · actions · events · rulebooks · notes
                            ┌───────────────────────────┘
                            ▼
      evaluate (actions per level vs. human baseline)  →  select (revert if worse than parent)
                            ▼
      distil (Claude reads the rule book, metrics, the agent's notes)  →  generation N+1
```

What changes from one generation to the next:

| Part of the harness | How it evolves |
|---|---|
| **Rules** | Game-independent lessons distilled from each game's rule book, with the evidence (run, level, rule) each came from. Lessons are retired or revised as evidence accumulates. |
| **Context policy** | The addendum appended to the agent's prompt is regenerated from the active lessons. Mid-game, a coach watches the action count per level and, when the agent is stuck, sends the most relevant lessons into the live session. |
| **Guardrails** | Launch knobs (action budget multiple, nudge count, the coach's stuck threshold) are part of the generation and can be changed by the distiller. |
| **Memory** | Every game starts in a brand-new workspace: empty `notes/`, empty `scripts/`, no rule book. Nothing is copied between games. What carries over lives in MongoDB and is reached by query: the agent's `memory.py recall <words>` searches distilled lessons plus the notes and verified rules of earlier games. The prose an agent writes under `notes/` is mirrored into MongoDB as it is written, edited or deleted; code under `scripts/` stays local to its game and is never stored. |

## How MongoDB Atlas is used

MongoDB is the harness's memory of record; nothing about a run lives only on disk.

- `actions`: every move with its 64x64 frames, so any game can be replayed frame by frame.
- `events`: one ordered timeline per run of the agent's reasoning, code, tool results, rule-book changes, harness interventions, and memory lookups.
- `rulebooks`: a snapshot each time the agent commits what it has verified or refuted about a game.
- `notes` (text-indexed): the agent's own prose, one document per file under `notes/`, updated in place when the file changes and deleted when the file is deleted.
- `lessons` (text-indexed) and `generations`: the evolving harness itself, with diffs, rationale and fitness.
- The agent reaches memory through a loopback gateway (`memory.py recall <words>` runs `$text` searches over lessons, notes and rule books), so it never holds database credentials.
- The live view's Memory tab is fed by a MongoDB change stream, so a document appears, changes or disappears on screen as it does in the database.

Schema: [docs/SCHEMA.md](docs/SCHEMA.md).

## The live view

`python -m mongoarc.view` serves a page that reads only from MongoDB: the game board as it is played (or replayed at any speed), the agent's reasoning and code beside it, the rule book growing at each level, the lessons this run was launched with, and an Evolution tab showing each generation's diff and the efficiency of the runs played under it.

## Run it

Requirements: Python 3.11+, `pip install pymongo fastapi uvicorn numpy pillow`, a MongoDB connection string, and the `claude` CLI for real agents.

```bash
git clone --recurse-submodules https://github.com/dastin359/MongoARC && cd MongoARC
cp .env.example .env          # set MONGODB_URI (and ARC_API_KEY for real games)
```

Zero cost (a scripted stand-in agent on the bundled toy games, heuristic distiller):

```bash
python3 tools/mock_arc.py 18765 &
python -m mongoarc.view --port 8787 &
python -m mongoarc.evolve run --games mk01-toy,mk01-toy,mk01-toy --agent fake \
    --arc-root http://127.0.0.1:18765 --label demo --distiller heuristic
```

A real agent on the toy games, with Claude as the distiller:

```bash
python -m mongoarc.evolve run --games mk02-keys,mk03-lava,mk04-ice,mk05-doors --agent claude \
    --arc-root http://127.0.0.1:18765 --label toys --distiller claude
```

Real ARC-AGI-3 games (needs `ARC_API_KEY`; list ids with `PYTHONPATH=CCARC/src python3 -m ccarc arc-3 games`):

```bash
python -m mongoarc.evolve run --games sb26-7fbdac44,vc33-5430563c --agent claude --label real --distiller claude
```

## What is new here and what is not

- **Not new:** [CCARC](https://github.com/dastin359/CCARC), the driver that runs Claude Code against the ARC-AGI-3 API. It is included as an unmodified git submodule and is prior work. MongoARC wraps it from the outside; no file in it is changed.
- **Built during the hackathon:** everything else in this repository: the collector, the MongoDB schema, the evolution loop (evaluate, select, distil, launch), the mid-game coach, the agent memory gateway and tool, the notes sync, the live view, and the toy-game suite used to exercise the loop at no cost.

The toy games in `tools/mock_arc.py` are not ARC-AGI-3. They share the API shape and one property that matters for testing the loop: different games obey common hidden conventions, so a lesson from one is a useful prior in the next.

For the hackathon the CCARC sandbox guards (network isolation, filesystem confinement) are turned off, and the prompt addendum tells the agent so. Results produced this way are demonstrations, not benchmark scores.
