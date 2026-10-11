#!/usr/bin/env python3
"""Memory for THIS game: a vector database with semantic retrieval.

  python3 memory.py recall "<question>"         semantic search over this game's notes
  python3 memory.py list                        everything stored for this game so far
  python3 memory.py save "<title>" "<text>"     add a note; prints its id (N1, N2, ...)
  python3 memory.py update <id> "<text>"        replace a note that needs correcting
  python3 memory.py delete <id>                 remove a note that turned out wrong

recall takes a question in plain language and returns the notes closest to it
in meaning, closest first. It finds notes that share no words with the question.

For save and update, leave the text out (or give "-") and it is read from
standard input, which is the easy way to store several lines:

  python3 memory.py save "level 2 mechanics" <<'EOF'
  Blue blocks slide until they hit a wall.
  scripts/bfs.py finds the shortest push sequence.
  EOF

Everything in it comes from this game and nothing from any other. Search it
before your first action on every level. After every level, save what you
verified, and update or delete what the level proved wrong.

The only local files you keep are code, in scripts/, and throwaway images or
dumps, in scratch/. When a note depends on a script, name the script's path in
the note.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = os.environ.get("MONGOARC_MEMORY_URL", "http://127.0.0.1:8790")
RUN = os.environ.get("MONGOARC_RUN_ID", "")
MAX_SEND = 100000       # characters; the memory keeps the first 20000 of a note

_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open     # loopback only: never via a proxy


def call(path, params=None, body=None):
    """(True, the gateway's answer) or (False, a one-line reason)."""
    url = URL.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    try:
        with _open(req, timeout=20) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as exc:       # a refusal carries its reason in the body
        try:
            out = json.loads(exc.read())
        except Exception:
            out = {"error": "memory unavailable: HTTP %s" % exc.code}
    except Exception as exc:
        return False, "memory unavailable: %s" % exc
    if not isinstance(out, dict):
        return False, "memory unavailable: unexpected answer"
    if "error" in out:
        return False, " ".join(str(out["error"]).split())
    if not ({"notes", "short", "ok"} & set(out)):   # something else is answering on that port
        return False, "memory unavailable: unexpected answer"
    return True, out


def text_from(args):
    """The text given on the command line, or standard input when it is "-" or left out. None if there is none."""
    if args and args != ["-"]:
        return " ".join(args)
    if sys.stdin is None or sys.stdin.isatty():
        return None
    try:                                         # an open pipe nobody writes to must not hang the call
        import select
        if not select.select([sys.stdin], [], [], 5)[0]:
            return None
    except Exception:
        pass
    raw =sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def note_head(n):
    if not n.get("id"):                          # mirrored from a file by an older habit: readable, not editable here
        return "NOTE from a file (%s):" % n.get("path")
    bits = []
    if (n.get("version") or 1) != 1:
        bits.append("version %s" % n["version"])
    if n.get("level") is not None:
        bits.append("saved on level %s" % n["level"])
    if n.get("score") is not None:
        bits.append("match %.2f" % n["score"])
    return 'NOTE %s "%s"%s:' % (n["id"], n.get("title") or "", " (%s)" % ", ".join(bits) if bits else "")


NOTHING = 'nothing saved yet for this game: python3 memory.py save "<title>" "<text>" adds a note'


def recall(words):
    ok, out = call("/recall", {"run": RUN, "q": " ".join(words), "n": 5})
    if not ok:
        print(out)
        return 1
    notes, stored = out.get("notes") or [], out.get("stored") or 0
    if not stored:
        print(NOTHING)
        return 0
    if not notes:
        print("no note matched: %s stored (python3 memory.py list shows them all)" % stored)
        return 0
    how = {"atlas_vector_search": "closest in meaning first", "text": "keyword match", "recent": "most recent first"}
    print("%d of %d note(s), %s:" % (len(notes), stored, how.get(out.get("engine"), "")))
    for n in notes:
        print("\n" + note_head(n))
        print(n.get("text") or "")
    return 0


def list_all():
    ok, out = call("/list", {"run": RUN})
    if not ok:
        print(out)
        return 1
    notes = out.get("notes") or []
    if not notes:
        print(NOTHING)
        return 0
    print("NOTES: %d" % len(notes))
    for n in notes:
        print("\n" + note_head(n))
        print(n.get("text") or "")
    return 0


def write(op, args):
    if op == "delete":
        if len(args) != 1:
            print("not deleted: give one note id, for example: python3 memory.py delete N3")
            return 2
        ok, out = call("/delete", body={"run": RUN, "id": args[0]})
        print("deleted %s" % out["short"] if ok else "not deleted: %s" % out)
        return 0 if ok else 1
    done, usage = (("saved", 'python3 memory.py save "<title>" "<text>"') if op == "save"
                   else ("updated", 'python3 memory.py update <id> "<text>"'))
    if not args:
        print("not %s: usage: %s" % (done, usage))
        return 2
    text = text_from(args[1:])
    if text is None:
        print("not %s: no text given (usage: %s, or pipe the text in)" % (done, usage))
        return 2
    body = {"run": RUN, "text": text[:MAX_SEND]}
    body["title" if op == "save" else "id"] = args[0]
    ok, out = call("/" + op, body=body)
    if not ok:
        print("not %s: %s" % (done, out))
        return 1
    cut = " (text cut to its first 20000 characters)" if out.get("cut") else ""
    print("%s %s%s%s" % (done, out["short"], " (version %s)" % out["version"] if op == "update" else "", cut))
    return 0


def main(argv):
    try:
        sys.stdout.reconfigure(errors="replace")     # a terminal that cannot show a character must not lose the answer
    except Exception:
        pass
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd in ("recall", "search"):
        return recall(args)
    if cmd == "list":
        return list_all()
    if cmd in ("save", "update", "delete"):
        return write(cmd, args)
    print(__doc__)
    return 0 if cmd in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
