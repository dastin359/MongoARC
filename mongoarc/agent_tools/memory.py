#!/usr/bin/env python3
"""Long-term memory shared across games (read side).

  python3 memory.py recall <words ...>     search what earlier games left behind

Returns lessons distilled across games, plus matching notes and verified rules
from earlier games. All of it describes OTHER games: treat it as priors to test
early, never as facts about this one.

Writing: anything you save as prose under notes/ (markdown or plain text) is
stored in this memory automatically and becomes searchable in later games.
Code belongs in scripts/ and is not stored; mention a script's filename in the
note that explains it.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

URL = os.environ.get("MONGOARC_MEMORY_URL", "http://127.0.0.1:8790")
RUN = os.environ.get("MONGOARC_RUN_ID", "")


def main(argv):
    if len(argv) < 1 or argv[0] not in ("recall", "search"):
        print(__doc__)
        return 2
    query = urllib.parse.urlencode({"run": RUN, "q": " ".join(argv[1:]), "n": 6})
    try:
        with urllib.request.urlopen(f"{URL}/recall?{query}", timeout=20) as r:
            out = json.loads(r.read())
    except Exception as exc:
        print("memory unavailable:", exc)
        return 1
    if "error" in out:
        print("memory unavailable:", out["error"])
        return 1
    if not (out["lessons"] or out["notes"] or out["rules"]):
        print("nothing recorded yet")
        return 0
    if out["lessons"]:
        print("LESSONS (distilled across earlier games):")
        for l in out["lessons"]:
            print(f"- [{l['id']} {l.get('kind') or ''}] {l['text']}")
    if out["rules"]:
        print("\nRULES verified in earlier games:")
        for r in out["rules"]:
            print(f"- ({r['game']}) {r['rule']}")
    for n in out["notes"]:
        print(f"\nNOTE from {n['game']} ({n['path']}):")
        print(n["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
