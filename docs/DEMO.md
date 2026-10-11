# Demo guide

One dashboard, reading one MongoDB database on Atlas:

```bash
MONGO_DB=mongoarc_lv python -m mongoarc.view --port 8787      # http://127.0.0.1:8787
```

Pick a game in the RUN menu (or leave it on "auto" to follow the newest running game). All three tabs then show that game.

## One-minute video

1. **Live play (20 s).** The board is a real ARC-AGI-3 game. Point at the controller under the board lighting up as the agent presses a button, the crosshair when it clicks a pixel, and the agent's reasoning and code on the right. In the feed, the highlighted "harness → agent" entries are the harness acting: at each level boundary it tells the agent to search its memory and sends the lessons it just distilled.
2. **Memory (15 s).** Click a bank in the "Memory in MongoDB" panel, or the MEMORY tab. Open `notes`, then `lessons`. Toggle "raw document" on a card to show the JSON stored in MongoDB. The status line says "live, MongoDB change stream": documents appear and change as the agent writes.
3. **Evolution (25 s).** The EVOLUTION tab shows how the harness changed during this one game. Each card is a generation, created when a level was cleared: what it added or retired, why (the distiller's rationale), and how the next level went under it. The chart is the hard metric: actions spent on each level against the human baseline, with a no-memory control run of the same game overlaid when there is one.

## Three-minute live demo

- Say first: "The product is a harness that rewrites itself between levels of a game. This page is how you watch it do that."
- Replay beats live when a game is between moves: choose a finished run in the RUN menu, press REPLAY, set 8x.
- Deep links: `?run=<id>&mode=replay&speed=8&autoplay=1` replays a run; `?tab=evolution&run=<id>` and `?tab=memory` open the other tabs.
- Why per level: ARC-AGI-3 games are built so that knowledge does not transfer between games, while later levels of one game reuse its mechanics. Every game here starts with an empty memory.

## What to say about results

State them as they are. The underlying agent already clears these games in far fewer actions than the human baseline, so there is little room for the per-level lessons to lower action counts further, and one run per condition cannot show a difference anyway. What the runs demonstrate is the mechanism working end to end on the real benchmark: the harness producing a new generation at every level, the agent searching its memory at every level boundary, and all of it readable live from MongoDB.
