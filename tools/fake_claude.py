#!/usr/bin/env python3
"""Zero-cost stand-in for the `claude` CLI, for exercising CCARC end to end.
Point CCARC at it with CLAUDE_BINARY=/path/fake_claude.py. It emits stream-json
events shaped like the real CLI's (APPROXIMATE: the real ones carry more fields)
and drives the game through the workspace's own `session.py`, exactly as a
solver's `python -c` grandchildren do. Random play; acknowledges the level gate."""
import json, os, subprocess, sys, time, uuid
if "--help" in sys.argv:   # CCARC greps --help for these flags (session._supports_flag)
    print("--session-id --effort --resume --output-format --input-format --replay-user-messages"); sys.exit(0)
SID = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else str(uuid.uuid4())
BATCHES = int(os.environ.get("FAKE_BATCHES", "6")); PER = int(os.environ.get("FAKE_ACTIONS", "15"))
PAUSE = float(os.environ.get("FAKE_PAUSE_S", "0.3"))
def emit(ev): ev.setdefault("session_id", SID); print(json.dumps(ev), flush=True)
PLAY = r'''
import random, time
from session import client, gate
from ccarc.arc_3 import GateRefusal, ActionRefused
if client.state == "NOT_PLAYED" or client.actions_used == 0: client.reset()
acts = [int(a[6:]) for a in client.available_actions if a.startswith("ACTION")] or [1, 2, 3, 4]
for _ in range({n}):
    if client.state in ("WIN", "GAME_OVER"): break
    a = random.choice(acts); kw = dict(x=random.randrange(64), y=random.randrange(64)) if a == 6 else {{}}
    if {seek} and client.last is not None:   # toy-mock policy: step the colour-9 block toward the colour-14 target
        import numpy as np
        b = client.last.after; me = np.argwhere(b == 9); tg = np.argwhere(b == 14)
        if len(me) and len(tg):
            (my, mx), (ty, tx) = me.min(0), tg.min(0)
            a = 4 if tx > mx else 3 if tx < mx else 2 if ty > my else 1
    try: client.act(a, **kw)
    except GateRefusal:
        gate.acknowledge(f"level {{client.level - 1}} cleared by random play", mechanics=["random walk"], untested=["everything"])
        client.act(a, **kw)
    time.sleep({pause})
print(client.status())
'''
def turn(prompt, turn_no):
    t0 = time.time()
    emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "model": "fake", "tools": ["Bash"], "apiKeySource": "none", "permissionMode": "fake"})
    for b in range(BATCHES):
        tid = f"toolu_fake_{turn_no}_{b}"; code = PLAY.format(n=PER, pause=PAUSE, seek=os.environ.get("FAKE_POLICY") == "seek")
        emit({"type": "assistant", "message": {"role": "assistant", "model": "fake", "content": [
            {"type": "thinking", "thinking": f"Batch {b}: try {PER} random actions and see what changes."},
            {"type": "text", "text": f"Playing batch {b}."},
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "python3 -c '" + code + "'", "timeout": 600000, "description": f"batch {b}"}}]}})
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        out = (p.stdout + p.stderr)[-2000:]
        emit({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": p.returncode != 0}]}})
        if "state=WIN" in out: break
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": BATCHES, "duration_ms": int((time.time() - t0) * 1000), "total_cost_usd": 0.0, "result": "done"})
if "--input-format" in sys.argv:          # live mode: one turn per user message on stdin
    n = 0
    for line in sys.stdin:
        if line.strip(): n += 1; turn(json.loads(line), n)
else:
    turn(sys.argv[sys.argv.index("-p") + 1] if "-p" in sys.argv else "", 1)
