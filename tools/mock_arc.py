"""Minimal local stand-in for the ARC-AGI-3 HTTP API (loopback only).
Toy game: 16x16 logical board rendered 4x into 64x64; ACTION1-4 move a block;
touching the target clears the level; 3 levels -> WIN."""
import json, sys, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
GAME = "mk01-toy"; LEVELS = 3
TARGETS = [(12, 3), (2, 13), (14, 14)]
S = {}  # guid -> state
def board(st):
    g = [[5]*64 for _ in range(64)]
    tx, ty = TARGETS[min(st["lvl"], LEVELS-1)]
    for cx, cy, col in ((tx, ty, 14), (st["x"], st["y"], 9)):
        for yy in range(cy*4, cy*4+4):
            for xx in range(cx*4, cx*4+4):
                g[yy][xx] = col
    return g
def frame(st, aid, data, full=False):
    return {"game_id": GAME, "guid": st["guid"], "frame": [board(st)], "state": st["state"],
            "levels_completed": st["lvl"], "win_levels": LEVELS,
            "action_input": {"id": aid, "data": data}, "available_actions": [1,2,3,4],
            "full_reset": full}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.path == "/api/games":
            return self._send(200, [{"game_id": GAME, "title": "toy", "tags": ["keyboard"], "baseline_actions": [10, 12, 14]}])
        if self.path.startswith("/api/scorecard/"):
            return self._send(403, {"error": "cannot get scorecard that is in competition mode"})
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/scorecard/open":
            return self._send(200, {"card_id": "card-" + uuid.uuid4().hex[:12]})
        if self.path.startswith("/api/cmd/"):
            name = self.path.rsplit("/", 1)[1]; aid = 0 if name == "RESET" else int(name[6:])
            guid = body.get("guid")
            if aid == 0 or guid not in S:
                st = {"guid": guid or uuid.uuid4().hex, "x": 1, "y": 1, "lvl": S.get(guid, {}).get("lvl", 0), "state": "NOT_FINISHED"}
                S[st["guid"]] = st
                return self._send(200, frame(st, 0, {}))
            st = S[guid]
            dx, dy = {1: (0,-1), 2: (0,1), 3: (-1,0), 4: (1,0)}.get(aid, (0,0))
            st["x"] = max(0, min(15, st["x"]+dx)); st["y"] = max(0, min(15, st["y"]+dy))
            if (st["x"], st["y"]) == TARGETS[st["lvl"]]:
                st["lvl"] += 1; st["x"], st["y"] = 1, 1
                if st["lvl"] >= LEVELS: st["state"] = "WIN"; st["lvl"] = LEVELS
            data = {k: body[k] for k in ("x","y") if k in body}
            return self._send(200, frame(st, aid, data))
        return self._send(404, {"error": "not found"})
port = int(sys.argv[1]); ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
