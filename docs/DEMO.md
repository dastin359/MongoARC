# Demo guide

Three dashboards read three MongoDB databases on the same Atlas cluster:

| URL | Database | What it shows |
|---|---|---|
| `http://127.0.0.1:8788` | `mongoarc_arc3` | Real ARC-AGI-3 games, Opus agent, evolving harness plus a no-memory control arm |
| `http://127.0.0.1:8787` | `mongoarc_val` | Toy suite with held-out validation: every generation is scored on a game nobody learned from |
| `http://127.0.0.1:8789` | `mongoarc` | First toy pass and its control runs |

Start a dashboard for any database with `MONGO_DB=<name> python -m mongoarc.view --port <port>`.

## One-minute video

1. **Live play (15 s).** Open `:8788`. The board is a real ARC-AGI-3 game being played now. Point at the controller under the board lighting up as the agent presses a button, the crosshair when it clicks a pixel, and the agent's reasoning and code scrolling on the right.
2. **Memory (15 s).** Click a bank tile in the "Memory in MongoDB" panel (or the MEMORY tab). Open `notes`, then `lessons`. Toggle "raw document" on one card to show the stored JSON. The status line says "live, MongoDB change stream"; documents appear and change as the agent writes.
3. **Evolution (20 s).** Open the EVOLUTION tab. Each card is a version of the harness: what it added, why (the distiller's rationale), and how the runs under it scored. Point at one lesson and its evidence (game, level, rule it came from).
4. **The hard metric (10 s).** On `:8787`, EVOLUTION tab: diamonds are held-out validation runs. A generation that scores worse than its parent on the held-out game is marked REVERTED and its lessons are retired.

## Three-minute live demo

- Say first: "The product is a harness that rewrites itself between games. This page is how you watch it do that."
- Replay beats live when a game is between moves: pick any finished run in the RUN menu, press REPLAY, set 8x.
- `?run=<id>&mode=replay&speed=8&autoplay=1` deep-links a replay; `?tab=evolution` and `?tab=memory` open the other tabs.

## What to say about results

State them as they are. Toy games are solved near-optimally by the agent without any help, so lessons have little room to improve efficiency there; what the toy suite demonstrates is the loop itself, including generations being rejected on a held-out game. The real ARC-AGI-3 runs are a handful of games with one run per condition: they show the harness working end to end on the real benchmark, not a measured improvement.
