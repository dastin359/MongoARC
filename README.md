# MongoARC

**A Claude Code agent that plays ARC-AGI-3 with its memory in MongoDB Atlas, and a live view of everything it does.** The agent plays a game it has never seen, level by level. What it learns on one level it writes into a vector database, in its own words, and searches by meaning before it acts on the next. The memory outlives the agent's context window, so what it worked out on level 1 is still there on level 8.

Built for the Harness Engineering & Model Wrangling hackathon (SF Tech Week, 2026-10-11).

## Why memory is per game

ARC-AGI-3 environments are designed to be novel: what an agent works out in one game is not supposed to carry into another. What it works out on one *level* does carry into the later levels of the same game, which reuse the mechanics over harder layouts. So every game starts in a brand-new workspace with an empty memory, and nothing crosses from one game to the next.

## How it works

```
   ARC-AGI-3 game  ◄──►  Claude Code agent
                            │  python3 memory.py save / update / delete / list
                            │  python3 memory.py recall "<question>"
                            ▼
                     memory gateway (loopback; holds the database connection)
                            ▼
                     MongoDB Atlas ── notes ── Vector Search with automated embedding
                            ▲
   collector ───────────────┘  runs · actions · events        ◄── dashboard reads all of it
```

- **The agent writes its own lessons.** `memory.py save "<title>" "<text>"` stores a note; `update` corrects one in place and keeps the earlier wording; `delete` removes one that turned out wrong. Notes are plain prose.
- **MongoDB does the retrieval.** `memory.py recall "<question>"` is an Atlas Vector Search with automated embedding: Atlas embeds each note when it is saved and embeds the question when it is asked, so the harness sends raw text and gets back the notes closest in meaning. A note that shares no words with the question is still found.
- **The agent never holds database credentials.** `memory.py` is a small script in its workspace that calls a loopback gateway started for that run; the gateway holds the Atlas connection and restricts every call to that run.
- **The harness prompts at level boundaries.** When a level is cleared it sends one message into the live session: save what that level taught you, then search your memory before acting on the new one.
- **Only code is kept as files.** Scripts go in `scripts/`, throwaway images in `scratch/`; a note names the script it depends on.

The agent's instructions are the workspace `CLAUDE.md`: CCARC's two-line preamble followed by [`mongoarc/assets/ARC3_DOCTRINE.md`](mongoarc/assets/ARC3_DOCTRINE.md), which is CCARC's ARC-AGI-3 doctrine with its level-gate and local-file sections replaced by a Memory section. CCARC's level gate (which refuses the next action until the agent records a rule book in a local file) is switched off.

## How MongoDB Atlas is used

MongoDB is the memory of record; nothing about a run lives only on disk.

- `notes`: the agent's memory, one document per note, with version and revision history. An Atlas Vector Search index with automated embedding (`voyage-4`) and a filter on `run_id` serves the semantic search.
- `actions`: every move with its 64x64 frames, so any game can be replayed frame by frame.
- `events`: one ordered timeline per run of the agent's reasoning, code, tool results, harness messages, and every memory write and search.
- `runs`: one document per game with its per-level action counts against the human baseline.
- The dashboard's Memory tab follows `notes` through a change stream, so a note appears, changes or disappears on screen as it does in the database.

Schema: [docs/SCHEMA.md](docs/SCHEMA.md).

## The dashboard

`python -m mongoarc.view` serves a page that reads only from MongoDB.

- **Live play:** the game board as it is played, or replayed at any speed; an on-screen controller that lights the button the agent pressed and marks each pixel it clicks; the agent's reasoning and code; the notes it has saved; and each memory search highlighted as it happens.
- **Levels:** for one game, actions spent on each level against the human baseline, with the memory writes and searches made on that level.
- **Memory:** the notes bank. Open it to read the documents, raw JSON included, updating live.

Columns can be resized by dragging, and the text size follows the window (or the A- / A+ control). A walkthrough for a demo is in [docs/DEMO.md](docs/DEMO.md).

## Run it

Requirements: Python 3.11+, `pip install pymongo fastapi uvicorn numpy pillow`, a MongoDB Atlas connection string, and the `claude` CLI for real agents.

```bash
git clone --recurse-submodules https://github.com/dastin359/MongoARC && cd MongoARC
cp .env.example .env          # set MONGODB_URI (and ARC_API_KEY for real games)
python -m mongoarc.view --port 8787 &
```

A real ARC-AGI-3 game (needs `ARC_API_KEY`; list ids with `PYTHONPATH=CCARC/src python3 -m ccarc arc-3 games`):

```bash
python -m mongoarc.launch --game sb26-7fbdac44 --out-dir runs/sb26 --agent claude \
    --model claude-opus-5-5 --collect --timeout 1500
```

At no cost, a scripted stand-in agent on a bundled toy game (it exercises the launcher, the collector and the dashboard, but does not use the memory tool):

```bash
python3 tools/mock_arc.py 18765 &
python -m mongoarc.launch --game mk01-toy --out-dir runs/demo-fake --agent fake \
    --arc-root http://127.0.0.1:18765 --collect
```

Semantic search needs Atlas. On a plain local `mongod`, `recall` falls back to the text index.

## Result from the hackathon run

One real ARC-AGI-3 game, CD82 (6 levels), played by Claude Opus 5.5 with this memory. It is one run, shown to demonstrate the mechanism working on the real benchmark, not to claim an efficiency gain.

| | Result | Actions per level (human baseline 55, 8, 41, 21, 23, 23) | Total |
|---|---|---|---|
| With MongoDB memory | won 6/6 | 14, 6, 18, 14, 13, 16 | 81 |

The agent saved 7 notes (one after every level, plus a note of the game's rules), and searched its memory 5 times, on 4 of the 6 levels. Each search after the first went through Atlas Vector Search and returned the notes it had saved on earlier levels. No note was updated or deleted in this game.
