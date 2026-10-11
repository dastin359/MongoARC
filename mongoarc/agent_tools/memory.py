#!/usr/bin/env python3
"""Memory for THIS game (read side).

  python3 memory.py recall <words ...>     search what this game's earlier levels left behind

Returns the lessons the harness distilled each time you cleared a level, the
rules you committed at the level gates, and your own notes. Everything in it
comes from this game and nothing from any other. Run it at the start of every
level, before your first action on it: the mechanics carry over, the layout
does not.

Writing: anything you save as prose under notes/ (markdown or plain text) is
stored in this memory as you write it. Code belongs in scripts/ and is not
stored; mention a script's filename in the note that explains it.
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
        print("nothing recorded yet for this game: memory fills as you clear levels and write notes/")
        return 0
    if out["lessons"]:
        print("LESSONS from the levels you cleared:")
        for l in out["lessons"]:
            after = "" if l.get("level") is None else f", after level {l['level']}"
            print(f"- [{l['id']} {l.get('kind') or ''}{after}] {l['text']}")
    shown = False
    for status, head in (("verified", "RULES you verified:"), ("refuted", "RULES you found false:")):
        rules = [r for r in out["rules"] if r.get("status", "verified") == status]
        if rules:
            print(("\n" if out["lessons"] or shown else "") + head)
            shown = True
            for r in rules:
                at = "" if r.get("level") is None else f"(level {r['level']}) "
                print(f"- {at}{r['rule']}")
    for n in out["notes"]:
        print(f"\nYOUR NOTES ({n['path']}):")
        print(n["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
