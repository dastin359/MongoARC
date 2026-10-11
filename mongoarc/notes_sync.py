"""The notes memory bank: mirror the agent's prose into MongoDB and keep it in sync.

The agent keeps prose in <workspace>/notes/ and code in <workspace>/scripts/.
Only the prose is pushed, one document per text file in the `notes` collection
(docs/SCHEMA.md). The mirror is a true sync, not an append log: an edited file
updates its document in place (version + 1) and a deleted file deletes its
document. Scripts stay on disk; a note points at them through `refs`, and
`script_path()` turns a ref back into the file.

`NotesSync.sync()` is called for every run on every collector pass, so the
steady state is free: an unchanged directory costs one scandir per directory
and one stat per candidate file, and no MongoDB command at all.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat as _stat
import time
from pathlib import Path, PurePosixPath

from pymongo import DeleteOne, UpdateOne
from pymongo.errors import PyMongoError

from . import db as D

TEXT_SUFFIXES = frozenset({".md", ".txt", ".markdown", ""})
LEDGER_COPIES = ("trace*.jsonl", "tr_*")      # copies of the action ledger are not prose
MAX_CHARS = 20000                             # stored text is cut here
MAX_BYTES = 1 << 20                           # never read more than this of one file
SETTLE = 2.0                                  # seconds; see NotesSync.settle

# scripts/<name with extension> (any path prefix), or a bare name ending in .py / .sh.
_REF = re.compile(
    r"(?<![\w.-])(?:"
    r"scripts/(?P<full>[\w.-]+(?:/[\w.-]+)*\.\w{1,8})"
    r"|(?P<bare>[\w-][\w.-]*\.(?:py|sh))"
    r")(?![\w-])")


def is_note_name(name: str) -> bool:
    """Whether a file name under notes/ can be a prose note (content is checked separately)."""
    low = name.lower()
    if low.startswith(".") or any(fnmatch.fnmatchcase(low, pat) for pat in LEDGER_COPIES):
        return False
    return os.path.splitext(low)[1] in TEXT_SUFFIXES


def extract_refs(text: str) -> list[str]:
    """Script filenames mentioned in a note, de-duplicated, in order of first mention.

    `scripts/bfs.py` (with or without a path in front) gives "scripts/bfs.py"; a bare
    `bfs.py` or `run.sh` gives the name as written. A bare name and its scripts/ form
    are one script: the scripts/ form wins and keeps the position of the first mention.
    """
    out: list[str] = []
    pos: dict[str, int] = {}
    for m in _REF.finditer(text):
        full, bare = m.group("full"), m.group("bare")
        if full:
            if full in pos:
                out[pos[full]] = "scripts/" + full
            else:
                pos[full] = len(out)
                out.append("scripts/" + full)
        elif bare not in pos:
            pos[bare] = len(out)
            out.append(bare)
    return out


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _scan(ws_real: str) -> dict[str, tuple[int, int, str]] | None:
    """Candidate note files: {path relative to the workspace: (mtime_ns, size, file to open)}.

    None when the workspace itself is gone. Stat calls only; nothing is read.
    Symlinked directories are never entered, and a symlinked file counts only if
    its target is inside the workspace.
    """
    top = os.path.join(ws_real, "notes")
    if os.path.islink(top):
        top = os.path.realpath(top)
        if not _inside(top, ws_real):
            return {}
    out: dict[str, tuple[int, int, str]] = {}
    stack = [(top, "notes")]
    while stack:
        folder, rel = stack.pop()
        try:
            entries = os.scandir(folder)
        except OSError:
            if rel == "notes" and not os.path.isdir(ws_real):
                return None
            continue
        with entries:
            for e in entries:
                name = e.name
                if name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append((e.path, f"{rel}/{name}"))
                        continue
                    if not is_note_name(name):
                        continue
                    target = e.path
                    if e.is_symlink():
                        target = os.path.realpath(target)
                        if not _inside(target, ws_real):
                            continue
                        st = os.stat(target)
                    else:
                        st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if _stat.S_ISREG(st.st_mode):
                    out[f"{rel}/{name}"] = (st.st_mtime_ns, st.st_size, target)
    return out


def _read(path: str) -> tuple[str, str] | None:
    """(sha256 of the bytes read, decoded text), or None if the file is not UTF-8 text.

    Raises OSError if it cannot be read at all.
    """
    with open(path, "rb") as f:
        raw = f.read(MAX_BYTES + 1)
    clipped = len(raw) > MAX_BYTES
    if clipped:
        raw = raw[:MAX_BYTES]
    if b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        if not (clipped and e.start >= len(raw) - 3):       # only a character cut by the clip is forgiven
            return None
        text = raw[:e.start].decode("utf-8")
    return hashlib.sha256(raw).hexdigest(), text.removeprefix("﻿")


class _State:
    """What one run looked like at the end of the last pass."""
    __slots__ = ("ws", "ws_real", "stored", "seen")

    def __init__(self, ws: str):
        self.ws = ws
        self.ws_real = os.path.realpath(ws)
        self.stored: dict[str, str | None] = {}                    # path -> sha held in MongoDB
        self.seen: dict[str, tuple[int, int, str | None]] = {}     # path -> (mtime_ns, size, sha | None = not text)


class NotesSync:
    """Keeps the `notes` collection equal to the text files under each run's notes/ directory."""

    def __init__(self, db, settle: float = SETTLE):
        self.db = db
        self.col = db[D.NOTES]
        # A note caught mid-rewrite (truncated to nothing, or momentarily unreadable or
        # undecodable) is left alone until its file has been still for this long, so a
        # 5 Hz poll cannot delete a document, or blank it, in the gap between two writes.
        self.settle = settle
        self._runs: dict[str, _State] = {}

    def forget(self, run_id: str) -> None:
        """Drop the in-memory state of a run; the next sync reloads the stored hashes."""
        self._runs.pop(run_id, None)

    def sync(self, run_id: str, game_id: str, generation: int | None, workspace) -> int:
        """Bring the run's documents in line with its notes/ directory.

        Returns the number of documents inserted, updated or deleted by this call.
        """
        ws = os.fspath(workspace)
        st = self._runs.get(run_id)
        fresh = st is None or st.ws != ws
        if fresh:
            if not os.path.isdir(ws):
                return 0
            st = _State(ws)
        files = _scan(st.ws_real)
        if files is None:
            return 0        # the workspace is gone (archived, moved): keep what was remembered
        if fresh:
            # Once per run and process: what MongoDB already holds, so a restart over an
            # unchanged directory rewrites nothing and bumps no version.
            st.stored = {d["path"]: d.get("sha")
                         for d in self.col.find({"run_id": run_id}, {"path": 1, "sha": 1}) if d.get("path")}
            self._runs[run_id] = st

        wall = time.time()
        seen: dict[str, tuple[int, int, str | None]] = {}
        keep: set[str] = set()
        changed: dict[str, tuple[str, str]] = {}
        for path, (mtime, size, target) in files.items():
            prev = st.seen.get(path)
            if prev and prev[0] == mtime and prev[1] == size and (prev[2] is None or st.stored.get(path) == prev[2]):
                seen[path] = prev                    # same bytes as last pass, and MongoDB has them
                if prev[2] is not None:
                    keep.add(path)
                continue
            young = 0 <= wall - mtime / 1e9 < self.settle
            try:
                note = _read(target)
            except OSError:
                if path in st.stored:
                    keep.add(path)
                continue                             # look again next pass
            if note is None:
                if young and path in st.stored:
                    keep.add(path)                   # a stored note mid-rewrite, not a deletion
                else:
                    seen[path] = (mtime, size, None)
                continue
            sha, text = note
            if young and not text:
                if path in st.stored:
                    keep.add(path)
                continue                             # just created or truncated: wait for the content
            seen[path] = (mtime, size, sha)
            keep.add(path)
            if st.stored.get(path) != sha:
                changed[path] = note
        st.seen = seen

        gone = [p for p in st.stored if p not in keep]
        if not changed and not gone:
            return 0
        at = D.now()
        ops: list = [
            UpdateOne({"_id": f"{run_id}:{path}"},
                      {"$set": {"text": text[:MAX_CHARS], "refs": extract_refs(text), "sha": sha, "updated_at": at},
                       "$inc": {"version": 1},
                       "$setOnInsert": {"created_at": at, "run_id": run_id, "game_id": game_id,
                                        "generation": generation, "path": path}},
                      upsert=True)
            for path, (sha, text) in changed.items()]
        ops += [DeleteOne({"_id": f"{run_id}:{path}"}) for path in gone]
        try:
            res = self.col.bulk_write(ops, ordered=False)
        except PyMongoError:
            self._runs.pop(run_id, None)             # part of it may have landed: reload next time
            raise
        for path in gone:
            del st.stored[path]
        for path, (sha, _) in changed.items():
            st.stored[path] = sha
        return res.upserted_count + res.modified_count + res.deleted_count


def script_path(db, note_id_or_run_id: str, ref: str) -> Path | None:
    """The file on disk a note's ref points at, or None.

    Takes a note `_id` ("<run_id>:notes/...") or a run id. The workspace is
    <runs.out_dir>/<runs.game_id>. A ref with a directory ("scripts/bfs.py") is
    taken relative to the workspace; a bare name is looked for in scripts/, the
    workspace root and notes/toolkit/, in that order. None if the run is unknown,
    the file does not exist, the ref names a hidden file, or the resolved path
    leaves the workspace (.., absolute paths, symlinks).
    """
    key = str(note_id_or_run_id or "")
    rid = key.split(":notes/", 1)[0] if ":notes/" in key else key
    run = db[D.RUNS].find_one({"_id": rid}, {"out_dir": 1, "game_id": 1}) if rid else None
    if not run or not run.get("out_dir") or not run.get("game_id"):
        return None
    ref = str(ref or "").strip().replace("\\", "/")
    while ref.startswith("./"):
        ref = ref[2:]
    parts = PurePosixPath(ref).parts
    if not parts or "\0" in ref or ref.startswith("/") or any(p.startswith(".") for p in parts):
        return None
    try:
        root = (Path(run["out_dir"]) / run["game_id"]).resolve()
    except OSError:
        return None
    if len(parts) > 1:
        candidates = [root.joinpath(*parts)]
    else:
        candidates = [root / "scripts" / ref, root / ref, root / "notes" / "toolkit" / ref]
    for cand in candidates:
        try:
            real = cand.resolve()
        except OSError:
            continue
        if real.is_relative_to(root) and real.is_file():
            return real
    return None
