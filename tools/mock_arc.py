"""Local stand-in for the ARC-AGI-3 HTTP API (loopback only): a small suite of toy games.

Built for developing and demonstrating the harness at zero ARC cost. It is NOT
ARC-AGI-3: the games are far simpler. What it shares with the real thing is the
API shape and the property the self-improvement loop needs, namely that games
differ but obey common hidden conventions, so a lesson learned in one game is a
useful prior in the next:

  colour 9  (blue)    the avatar; ACTION1-4 move it up/down/left/right
  colour 14 (green)   an active goal; reaching it clears the level
  colour 3  (grey)    an inactive goal; it turns green once the key is collected
  colour 11 (yellow)  a key
  colour 8  (red)     lava; entering it is GAME_OVER (RESET restarts the level)
  colour 4  (dark)    a wall
  colour 12 (orange)  a closed door; colour 6 (magenta) a switch, toggled by clicking it (ACTION6)

Boards are 16x16 cells drawn 4x into the 64x64 frame. Each game's
`baseline_actions` is 1.3x the shortest solution per level (found by search),
standing in for the human baseline of the real API.

usage: python3 tools/mock_arc.py <port>
"""
import json
import math
import sys
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BG, WALL, AVATAR, GOAL, GOAL_OFF, KEY, LAVA, DOOR, SWITCH = 5, 4, 9, 14, 3, 11, 8, 12, 6
MOVES = {1: (0, -1), 2: (0, 1), 3: (-1, 0), 4: (1, 0)}


def _open(n=16):
    return ["." * n for _ in range(n)]


def _put(rows, x, y, ch):
    rows[y] = rows[y][:x] + ch + rows[y][x + 1:]
    return rows


def _toy(tx, ty):
    return _put(_put(_open(), 1, 1, "P"), tx, ty, "G")


LEVELS = {
    # mk01: reach the goal. Nothing else on the board.
    "mk01-toy": dict(title="toy", tags=["keyboard"], slide=False, levels=[_toy(12, 3), _toy(2, 13), _toy(14, 14)]),
    # mk02: walls, and the goal is inactive until the key is collected.
    "mk02-keys": dict(title="keys", tags=["keyboard"], slide=False, levels=[
        ["................", ".P......#.......", "........#.......", "........#...G...", "........#.......", "................",
         "................", "................", "................", "................", "...K............", "................",
         "................", "................", "................", "................"],
        ["................", ".P...#..........", ".....#..........", ".....#....K.....", ".....#..........", ".....######.....",
         "................", "................", "..G.............", "................", "................", "................",
         "................", "................", "................", "................"],
        ["................", ".P..............", "##############..", "................", "..K.............", "................",
         "..##############", "................", "................", "............G...", "................", "................",
         "................", "................", "................", "................"],
    ]),
    # mk03: lava kills. Later levels add the key.
    "mk03-lava": dict(title="lava", tags=["keyboard"], slide=False, levels=[
        ["................", ".P....L.........", "......L.........", "......L.....G...", "......L.........", "......L.........",
         "................", "................", "................", "................", "................", "................",
         "................", "................", "................", "................"],
        ["................", ".P..............", "LLLLLLLLLLLL....", "................", "................", "....LLLLLLLLLLLL",
         "................", "................", ".G..............", "................", "................", "................",
         "................", "................", "................", "................"],
        ["................", ".P...L..........", ".....L....K.....", ".....L..........", ".....LLLLLLLL...", "................",
         "................", "..LLLLLLLLLLLLLL", "................", "................", ".............G..", "................",
         "................", "................", "................", "................"],
    ]),
    # mk04: ice. A move slides until something stops it; sliding into lava still kills.
    "mk04-ice": dict(title="ice", tags=["keyboard"], slide=True, levels=[
        ["................", ".P..............", "................", "................", "................", "................",
         "................", "................", "................", "................", "................", "................",
         "................", "................", "...............G", "................"],
        ["................", ".P..............", "................", "................", "................", "................",
         "................", "................", "................", ".....G..........", "................", "................",
         "................", "................", "................", "....#..........."],
        ["................", ".P..............", "................", "................", "................", "................",
         "................", "................", "...............K", "................", "................", "................",
         "...............#", "................", "................", "......G........L"],
    ]),
    # mk05: a door blocks the way; clicking the switch opens it.
    "mk05-doors": dict(title="doors", tags=["keyboard_click"], slide=False, levels=[
        ["................", ".P.....#........", ".......#........", ".......D.....G..", ".......#........", ".......#........",
         ".......#........", ".......#........", ".......#........", ".......#........", ".......#........", ".......#........",
         ".......#........", "..S....#........", ".......#........", ".......#........"],
        ["................", ".P..............", "................", "########D#######", "................", "............S...",
         "................", "................", "................", "................", "................", "..K.............",
         "................", "................", "..............G.", "................"],
        ["................", ".P...#..........", ".....#..........", ".....D....L.....", ".....#....L.....", ".....#....L.....",
         "..S..#....L...G.", ".....#....L.....", ".....#....L.....", ".....#..........", ".....#..........", ".....#..........",
         ".....#....K.....", ".....#..........", ".....#..........", ".....#.........."],
    ]),
}


def _parse(rows):
    lv = dict(walls=set(), lava=set(), doors=set(), switches=set(), key=None, goal=None, start=(1, 1))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch == "#": lv["walls"].add((x, y))
            elif ch == "L": lv["lava"].add((x, y))
            elif ch == "D": lv["doors"].add((x, y))
            elif ch == "S": lv["switches"].add((x, y))
            elif ch == "K": lv["key"] = (x, y)
            elif ch == "G": lv["goal"] = (x, y)
            elif ch == "P": lv["start"] = (x, y)
    return lv


GAMES = {gid: dict(g, parsed=[_parse(r) for r in g["levels"]]) for gid, g in LEVELS.items()}


def _blocked(lv, pos, doors_open):
    x, y = pos
    return not (0 <= x < 16 and 0 <= y < 16) or pos in lv["walls"] or (pos in lv["doors"] and not doors_open)


def step(game, lv, pos, has_key, doors_open, aid, click=None):
    """One action -> (positions passed through, has_key, doors_open, outcome), outcome in None | 'dead' | 'clear'."""
    path, outcome = [], None
    if aid == 6:
        if click and (click[0] // 4, click[1] // 4) in lv["switches"]:
            doors_open = not doors_open
        return [pos], has_key, doors_open, None
    if aid not in MOVES:
        return [pos], has_key, doors_open, None
    dx, dy = MOVES[aid]
    while True:
        nxt = (pos[0] + dx, pos[1] + dy)
        if _blocked(lv, nxt, doors_open):
            break
        pos = nxt
        path.append(pos)
        if pos in lv["lava"]:
            outcome = "dead"
            break
        if lv["key"] == pos:
            has_key = True
        if pos == lv["goal"] and (has_key or lv["key"] is None):
            outcome = "clear"
            break
        if not game["slide"]:
            break
    return path or [pos], has_key, doors_open, outcome


def shortest(game, lv):
    """Fewest actions to clear a level (breadth-first over position, key, door state)."""
    acts = [1, 2, 3, 4] + ([6] if lv["switches"] else [])
    click = next(iter(lv["switches"]), None)
    start = (lv["start"], False, False)
    seen, queue = {start}, deque([(start, 0)])
    while queue:
        (pos, key, doors), d = queue.popleft()
        for a in acts:
            path, k2, d2, out = step(game, lv, pos, key, doors, a, (click[0] * 4, click[1] * 4) if click else None)
            if out == "clear":
                return d + 1
            if out == "dead":
                continue
            s = (path[-1], k2, d2)
            if s not in seen:
                seen.add(s)
                queue.append((s, d + 1))
    raise ValueError("unsolvable level")


for _g in GAMES.values():
    _g["baseline"] = [math.ceil(1.3 * shortest(_g, lv)) for lv in _g["parsed"]]
    _g["actions"] = [1, 2, 3, 4] + ([6] if any(lv["switches"] for lv in _g["parsed"]) else [])

S = {}  # guid -> state


def board(st, pos=None):
    game = GAMES[st["game"]]
    lv = game["parsed"][min(st["lvl"], len(game["parsed"]) - 1)]
    cells = {p: WALL for p in lv["walls"]}
    cells.update({p: LAVA for p in lv["lava"]})
    cells.update({p: SWITCH for p in lv["switches"]})
    if not st["doors"]:
        cells.update({p: DOOR for p in lv["doors"]})
    if lv["key"] and not st["key"]:
        cells[lv["key"]] = KEY
    cells[lv["goal"]] = GOAL if (st["key"] or lv["key"] is None) else GOAL_OFF
    cells[pos or st["pos"]] = AVATAR
    g = [[BG] * 64 for _ in range(64)]
    for (cx, cy), col in cells.items():
        for yy in range(cy * 4, cy * 4 + 4):
            for xx in range(cx * 4, cx * 4 + 4):
                g[yy][xx] = col
    return g


def start_level(st):
    lv = GAMES[st["game"]]["parsed"][st["lvl"]]
    st.update(pos=lv["start"], key=False, doors=False, state="NOT_FINISHED")


def frame(st, aid, data, frames=None):
    game = GAMES[st["game"]]
    return {"game_id": st["game"], "guid": st["guid"], "frame": frames or [board(st)], "state": st["state"],
            "levels_completed": st["lvl"], "win_levels": len(game["parsed"]),
            "action_input": {"id": aid, "data": data}, "available_actions": game["actions"], "full_reset": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/games":
            return self._send(200, [{"game_id": gid, "title": g["title"], "tags": g["tags"], "baseline_actions": g["baseline"]}
                                    for gid, g in GAMES.items()])
        if self.path.startswith("/api/scorecard/"):
            return self._send(403, {"error": "cannot get scorecard that is in competition mode"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/scorecard/open":
            return self._send(200, {"card_id": "card-" + uuid.uuid4().hex[:12]})
        if not self.path.startswith("/api/cmd/"):
            return self._send(404, {"error": "not found"})
        name = self.path.rsplit("/", 1)[1]
        aid = 0 if name == "RESET" else int(name[6:])
        guid = body.get("guid")
        if aid == 0 or guid not in S:
            gid = body.get("game_id") or S.get(guid, {}).get("game")
            if gid not in GAMES:
                return self._send(400, {"error": f"unknown game {gid}"})
            st = {"guid": guid or uuid.uuid4().hex, "game": gid, "lvl": S.get(guid, {}).get("lvl", 0)}
            if st["lvl"] >= len(GAMES[gid]["parsed"]):
                st["lvl"] = 0
            start_level(st)
            S[st["guid"]] = st
            return self._send(200, frame(st, 0, {}))
        st = S[guid]
        game = GAMES[st["game"]]
        data = {k: body[k] for k in ("x", "y") if k in body}
        if st["state"] != "NOT_FINISHED":
            return self._send(200, frame(st, aid, data))
        lv = game["parsed"][st["lvl"]]
        click = (int(body.get("x", -1)), int(body.get("y", -1))) if aid == 6 else None
        path, st["key"], st["doors"], outcome = step(game, lv, st["pos"], st["key"], st["doors"], aid, click)
        frames = [board(st, p) for p in path] if len(path) > 1 else None
        st["pos"] = path[-1]
        if outcome == "dead":
            st["state"] = "GAME_OVER"
        elif outcome == "clear":
            st["lvl"] += 1
            if st["lvl"] >= len(game["parsed"]):
                st["state"] = "WIN"
                frames = [board(dict(st, lvl=st["lvl"] - 1))]
            else:
                start_level(st)
                frames = None
        return self._send(200, frame(st, aid, data, frames))


if __name__ == "__main__":
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
