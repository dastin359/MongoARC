# Demo guide

One dashboard, reading one MongoDB database on Atlas:

```bash
MONGO_DB=mongoarc_lv python -m mongoarc.view --port 8787      # http://127.0.0.1:8787
```

Pick a game in the RUN menu (or leave it on "auto" to follow the newest running game). All three tabs then show that game.

## One-minute video

1. **Live play (20 s).** The board is a real ARC-AGI-3 game. Point at the controller under the board lighting up as the agent presses a button, the crosshair when it clicks a pixel, and the agent's reasoning and code on the right.
2. **Memory in action (15 s).** In the feed, the highlighted entries are the agent using its memory: saving a note after a level, and searching in plain language before the next. The Memory Notes panel shows what it has written so far.
3. **Memory (10 s).** Open the MEMORY tab. Each card is a document in MongoDB; toggle "raw document" to show the stored JSON. The status line says "live, MongoDB change stream": notes appear, change and disappear as the agent writes.
4. **Levels (15 s).** The LEVELS tab is the hard metric: actions spent on each level against the human baseline, with the notes saved and the searches made on each level, and a no-memory control run of the same game overlaid when there is one.

## Three-minute live demo

- Say first: "The agent keeps what it learns in MongoDB, in its own words, and finds it again by meaning. This page is how you watch it do that."
- Replay beats live when a game is between moves: choose a finished run in the RUN menu, press REPLAY, set 8x.
- Deep links: `?run=<id>&mode=replay&speed=8&autoplay=1` replays a run; `?tab=levels&run=<id>` and `?tab=memory` open the other tabs.
- The retrieval is MongoDB's own: Atlas embeds each note and each question itself (Vector Search with automated embedding). The harness sends raw text.
- Why memory is per game: ARC-AGI-3 games are built so that knowledge does not transfer between games, while later levels of one game reuse its mechanics.

## What to say about results

State them as they are. The underlying agent already clears these games in far fewer actions than the human baseline, so there is little room for memory to lower action counts further, and one run per condition cannot show a difference anyway. What the runs demonstrate is the mechanism working end to end on the real benchmark: the agent writing what it learned into MongoDB, finding it again by meaning at the next level, and all of it readable live.
