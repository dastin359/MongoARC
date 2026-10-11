#!/usr/bin/env python3
"""Record a silent, captioned demo video (H.264 MP4) of the mongoARC dashboards.

    python3 scripts/record_demo.py --out .local/demo.mp4 \
        [--arc http://127.0.0.1:8788] [--toys http://127.0.0.1:8787] [--seconds 60] [--size 1920x1080]

It starts its own headless Chrome (own profile under .local/, own random DevTools port), drives it
over the DevTools protocol, screenshots the page at a steady rate and pipes the frames to ffmpeg.
It only reads: HTTP GETs against the dashboards, nothing else. The Chrome it started is the only
process it stops.

Storyboard (docs/DEMO.md), scaled to --seconds:
  title card, LIVE PLAY replay of the best finished real game, LIVE PLAY following a running game,
  MEMORY tab (notes, lessons, one raw document), EVOLUTION on --arc, EVOLUTION on --toys.
Run ids are picked from /api/runs when the recording starts. A segment whose kind of run does not
exist hands its time to its neighbour (no running game: the replay runs longer).

Page loads are not recorded: the clock of the video is the frame count, so the file is exactly
--seconds long while the recording itself takes roughly 25 s longer.
"""
import argparse
import base64
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import websocket  # websocket-client

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEEDS = [1, 2, 4, 8, 16]
FINISHED = ("won", "finished", "lost", "stopped")

CAPTIONS = {
    "title": "mongoARC: a harness that rewrites itself between games",
    "replay": "A Claude agent plays a real ARC-AGI-3 game. Every move, frame and thought is recorded in MongoDB Atlas.",
    "live": "Live: the pad shows the button it pressed; a crosshair shows the pixel it clicked.",
    "memory": "Its memory lives in MongoDB: notes it writes, rules it verified, lessons distilled across games. Updated by change stream.",
    "evo_arc": "After each game the harness rewrites itself: a new generation, with the reason and the evidence.",
    "evo_toys": "Each generation is scored on a held-out game. If it does worse than its parent, it is reverted.",
}
# Seconds of a 60 s video; scaled to --seconds.
WEIGHTS = {"title": 2, "replay": 15, "live": 8, "memory": 13, "evo_arc": 14, "evo_toys": 8}


def log(*a):
    print("[record_demo]", *a, file=sys.stderr, flush=True)


def get_json(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # a dashboard that is down is a fallback, not a crash
        log("GET failed:", url, "->", e)
        return None


def free_port():
    for _ in range(50):
        port = random.randint(9601, 60000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port found")


class CDP:
    """Minimal synchronous DevTools client. No domain is enabled, so no events queue up."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        """Evaluate in the page; returns the value, or None if the page is mid-navigation or the code threw."""
        try:
            r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=False)
        except Exception:
            return None
        if r.get("exceptionDetails"):
            return None
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def caption_script(text, w, h):
    """JS run at document start: reserves a strip at the bottom of the page and puts the caption in it,
    so the caption never sits on top of the board, the pad or any panel."""
    cap = max(60, int(round(h * 0.093 / 2)) * 2)
    fs = max(16, round(min(w / 1920.0, h / 1080.0) * 30))
    css = (
        "html{height:calc(100%% - %(cap)dpx)!important}"
        ".stage{--board:min(calc(100vh - %(cap)dpx - 44px - 20px - 38px - 44px - 24px - var(--pad-h)),52vw)!important}"
        "#__demo_cap{position:fixed;left:0;right:0;bottom:0;height:%(cap)dpx;z-index:2147483647;box-sizing:border-box;"
        "display:flex;align-items:center;justify-content:center;padding:0 %(pad)dpx;background:#05080b;"
        "border-top:3px solid #00ED64;color:#fff;font:600 %(fs)dpx/1.25 -apple-system,'Helvetica Neue',Arial,sans-serif;"
        "text-align:center;text-wrap:balance}"
    ) % {"cap": cap, "pad": round(w * 0.03), "fs": fs}
    return """(function(){
  if (window.top !== window || !/^https?:$/.test(location.protocol)) return;
  var st = document.createElement('style'); st.id = '__demo_cap_style'; st.textContent = %s;
  function addStyle(){ var r = document.head || document.documentElement; if (!r) return false; r.appendChild(st); return true; }
  if (!addStyle()) { var mo = new MutationObserver(function(){ if (addStyle()) mo.disconnect(); }); mo.observe(document, {childList:true, subtree:true}); }
  function addBar(){ if (document.getElementById('__demo_cap')) return; var d = document.createElement('div'); d.id = '__demo_cap'; d.textContent = %s; document.body.appendChild(d); window.dispatchEvent(new Event('resize')); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', addBar); else addBar();
})();""" % (json.dumps(css), json.dumps(text))


def title_url(text, w, h):
    head, _, tail = text.partition(": ")
    html = (
        "<!doctype html><meta charset=utf-8><body style=\"margin:0;height:100vh;display:flex;flex-direction:column;"
        "align-items:center;justify-content:center;gap:%(g)dpx;background:#05080b;color:#fff;"
        "font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;text-align:center\">"
        "<div style=\"font:700 %(a)dpx/1 'SF Mono',Menlo,monospace;letter-spacing:.02em\">mongo<span style=\"color:#00ED64\">ARC</span></div>"
        "<div style=\"font-weight:500;font-size:%(b)dpx;max-width:80vw\">%(t)s</div>"
        "<div style=\"font-size:%(c)dpx;color:#8a94a3\">ARC-AGI-3 &middot; Claude &middot; MongoDB Atlas</div></body>"
    ) % {"g": round(h * 0.035), "a": round(h * 0.13), "b": round(h * 0.045), "c": round(h * 0.026),
         "t": (tail or head)[:1].upper() + (tail or head)[1:]}
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


# ---------- choosing what to show ----------
def pick_runs(base):
    runs = get_json(base + "/api/runs") or []
    done = [r for r in runs if r.get("status") in FINISHED]
    live = [r for r in runs if r.get("status") == "running"]
    done.sort(key=lambda r: (r.get("role") == "train", r.get("level") or 0, r.get("status") == "won", r.get("n_actions") or 0), reverse=True)
    live.sort(key=lambda r: (r.get("n_actions") or 0), reverse=True)
    return (done[0] if done else None), (live[0] if live else None)


def bank_counts(base):
    return {b.get("name"): b.get("count") or 0 for b in (get_json(base + "/api/memory/banks") or []) if isinstance(b, dict)}


JS_BOARD_READY = "(function(){var e=document.querySelector('#boardEmpty');return !!e&&e.hidden})()"
JS_POS = ("(function(){var t=(document.querySelector('#posLabel')||{}).textContent||'';var p=t.split('/');"
          "return [parseInt(p[0])||0,parseInt(p[1])||0,(document.querySelector('#btnPlay')||{}).textContent||'']})()")


def click(sel):
    return "(function(){var e=document.querySelector(%s);if(e){e.click();return true}return false})()" % json.dumps(sel)


class Recorder:
    def __init__(self, args):
        self.a = args
        self.w, self.h = args.w, args.h
        self.fps = args.fps
        self.chrome = None
        self.cdp = None
        self.ff = None
        self.profile = None
        self.cap_id = None
        self.frames = 0
        self.dups = 0
        self.shot_ms = []

    # ----- processes -----
    def start_chrome(self):
        local = os.path.join(ROOT, ".local")
        os.makedirs(local, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="record-demo-chrome-", dir=local)
        port = free_port()
        cmd = [self.a.chrome, "--headless=new", "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
               "--user-data-dir=" + self.profile, "--window-size=%d,%d" % (self.w, self.h), "--force-device-scale-factor=1",
               "--hide-scrollbars", "--mute-audio", "--no-first-run", "--no-default-browser-check",
               "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
               "--disable-backgrounding-occluded-windows", "--disable-extensions", "about:blank"]
        # Its own session, so stopping it can never reach anyone else's Chrome.
        self.chrome = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        log("chrome pid %d, devtools port %d, profile %s" % (self.chrome.pid, port, self.profile))
        ws = None
        t_end = time.time() + 20
        while time.time() < t_end and not ws:
            if self.chrome.poll() is not None:
                raise RuntimeError("Chrome exited at start (code %s)" % self.chrome.returncode)
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=1) as r:
                    for t in json.loads(r.read()):
                        if t.get("type") == "page":
                            ws = t["webSocketDebuggerUrl"]
                            break
            except Exception:
                pass
            if not ws:
                time.sleep(0.2)
        if not ws:
            raise RuntimeError("Chrome DevTools did not come up on port %d" % port)
        self.cdp = CDP(ws)
        self.cdp.call("Emulation.setDeviceMetricsOverride", width=self.w, height=self.h, deviceScaleFactor=1, mobile=False)

    def stop_chrome(self):
        if self.cdp:
            self.cdp.close()
        p = self.chrome
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)  # p is the leader of the session this script created
            except Exception:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()
                p.wait(timeout=5)
        if p:
            try:  # helpers that outlived the browser process, same process group only
                os.killpg(p.pid, signal.SIGKILL)
            except Exception:
                pass
        if self.profile and os.path.isdir(self.profile):
            shutil.rmtree(self.profile, ignore_errors=True)

    def start_ffmpeg(self, total_frames):
        out = os.path.abspath(self.a.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        self.fflog = tempfile.TemporaryFile()
        vf = "scale=%d:%d:flags=lanczos:out_range=tv:out_color_matrix=bt709,format=yuv420p" % (self.w, self.h)
        cmd = [self.a.ffmpeg, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(self.fps), "-c:v", "mjpeg", "-i", "-",
               "-an",
               "-vf", vf, "-r", "30" if 30 % self.fps == 0 else str(self.fps),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
               "-movflags", "+faststart", out]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=self.fflog, stderr=subprocess.STDOUT)

    def finish_ffmpeg(self):
        if not self.ff:
            return 1
        try:
            self.ff.stdin.close()
        except Exception:
            pass
        try:
            rc = self.ff.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.ff.kill()
            rc = -9
        self.fflog.seek(0)
        err = self.fflog.read().decode("utf-8", "replace").strip()
        if err:
            log("ffmpeg:", err[-1500:])
        return rc

    # ----- page -----
    def set_caption(self, text):
        if self.cap_id:
            try:
                self.cdp.call("Page.removeScriptToEvaluateOnNewDocument", identifier=self.cap_id)
            except Exception:
                pass
            self.cap_id = None
        if text:
            self.cap_id = self.cdp.call("Page.addScriptToEvaluateOnNewDocument", source=caption_script(text, self.w, self.h))["identifier"]

    def goto(self, url, ready_js=None, settle=1.5, timeout=10.0):
        """Navigate, then wait real time: for the document, then for ready_js (bounded), then a fixed settle.
        Nothing here waits for the network to go idle (the MEMORY tab holds an event stream open)."""
        self.cdp.call("Page.navigate", url=url)
        t_end = time.time() + timeout
        time.sleep(0.4)
        while time.time() < t_end:
            if self.cdp.js("document.readyState") in ("interactive", "complete") and self.cdp.js("location.href") not in (None, "about:blank"):
                break
            time.sleep(0.15)
        ok = True
        if ready_js:
            ok = False
            while time.time() < t_end:
                if self.cdp.js(ready_js):
                    ok = True
                    break
                time.sleep(0.2)
        time.sleep(settle)
        return ok

    def shot(self):
        t = time.monotonic()
        r = self.cdp.call("Page.captureScreenshot", format="jpeg", quality=92, optimizeForSpeed=True)
        self.shot_ms.append((time.monotonic() - t) * 1000)
        return base64.b64decode(r["data"])

    def record(self, name, nframes, actions=(), tick=None):
        """Capture nframes at self.fps in real time. actions: [(seconds_in, js)]. tick(elapsed, total) runs about once a second."""
        if nframes <= 0:
            return
        total = nframes / float(self.fps)
        log("recording %-9s %5.1f s" % (name, total))
        pending = sorted(actions, key=lambda x: x[0])
        t0 = time.monotonic()
        next_tick = 1.0
        i = 0
        while i < nframes:
            now = time.monotonic() - t0
            while pending and now >= pending[0][0]:
                self.cdp.js(pending.pop(0)[1])
            if tick and now >= next_tick:
                next_tick += 1.0
                try:
                    tick(now, total)
                except Exception as e:
                    log("tick error:", e)
            wait = i / float(self.fps) - (time.monotonic() - t0)
            if wait > 0:
                time.sleep(wait)
            frame = self.shot()
            self.ff.stdin.write(frame)
            i += 1
            behind = int((time.monotonic() - t0) * self.fps) - i  # slots missed by a slow capture: repeat the frame
            while behind > 0 and i < nframes:
                self.ff.stdin.write(frame)
                i += 1
                behind -= 1
                self.dups += 1
        self.frames += nframes

    # ----- segments -----
    def replay_tick(self, state):
        """Keeps the replay ending (on the final board) about 1.5 s before the segment does, by choosing among
        the page's own speed buttons from the measured pace; restarts a replay that ended far too early."""
        def tick(now, total):
            v = self.cdp.js(JS_POS)
            if not v:
                return
            pos, last, btn = v
            left = total - now
            if btn == "PLAY":  # not playing: not started yet, or it reached the end
                state["idle"] = state.get("idle", 0) + 1
                if pos < last or (state["idle"] >= 2 and left > 5):
                    self.cdp.js(click("#btnPlay"))
                    state["idle"] = 0
                return
            state["idle"] = 0
            prev = state.get("prev")
            state["prev"] = (now, pos)
            if self.a.replay_speed or not prev or pos <= prev[1] or now <= prev[0]:
                return
            per_1x = (pos - prev[1]) / (now - prev[0]) / state["speed"]  # actions per second at 1x
            want = (last - pos) / max(0.5, left - 1.5) / max(per_1x, 1e-6)
            best = min(SPEEDS, key=lambda s: abs((s / want) - 1) if s >= want else abs((want / s) - 1))
            if best != state["speed"]:
                state["speed"] = best
                self.cdp.js(click('#speedSeg [data-speed="%d"]' % best))
        return tick

    def run(self):
        a = self.a
        fps = self.fps
        arc_done, arc_live = pick_runs(a.arc)
        toy_done, toy_live = pick_runs(a.toys)
        arc_banks, toy_banks = bank_counts(a.arc), bank_counts(a.toys)
        log("arc finished run: %s | arc running run: %s" % ((arc_done or {}).get("_id"), (arc_live or {}).get("_id")))

        secs = {k: v * a.seconds / 60.0 for k, v in WEIGHTS.items()}
        if a.no_title:
            secs["replay"] += secs.pop("title")
        replay_base, replay_run = (a.arc, arc_done) if arc_done else ((a.toys, toy_done) if toy_done else (a.arc, None))
        if not replay_run and not arc_live:
            log("no finished and no running run anywhere: LIVE PLAY will show the idle page")
        if not arc_live or not replay_run:  # one LIVE PLAY segment instead of two
            if replay_run:
                secs["replay"] += secs.pop("live")
            else:
                secs["live"] = secs.pop("replay") + secs.get("live", 0)
        mem_base = a.arc if (arc_banks.get("notes") or not toy_banks.get("notes")) else a.toys
        mem_banks = arc_banks if mem_base == a.arc else toy_banks
        log("memory tab from %s (notes %s, lessons %s)" % (mem_base, mem_banks.get("notes"), mem_banks.get("lessons")))

        order = [k for k in ("title", "replay", "live", "memory", "evo_arc", "evo_toys") if k in secs]
        total_frames = int(round(a.seconds * fps))
        frames, acc, used = {}, 0.0, 0
        for k in order:  # cumulative rounding: the parts add up to exactly total_frames
            acc += secs[k]
            frames[k] = max(0, min(total_frames, int(round(acc * fps))) - used)
            used += frames[k]
        frames[order[-1]] += total_frames - used

        self.start_chrome()
        self.start_ffmpeg(total_frames)
        t_start = time.time()

        if "title" in frames:
            self.goto(title_url(CAPTIONS["title"], self.w, self.h), settle=0.6)
            self.record("title", frames["title"])

        if "replay" in frames:
            self.set_caption(CAPTIONS["replay"])
            n = replay_run.get("n_actions") or 0
            dur = frames["replay"] / float(fps)
            want = n * 0.3 / max(1.0, dur - 2.0)  # about 0.3 s per action at 1x
            speed = a.replay_speed or min(SPEEDS, key=lambda s: abs(s - want))
            url = "%s/?run=%s&mode=replay&speed=%d" % (replay_base, urllib.parse.quote(replay_run["_id"]), speed)
            if not self.goto(url, JS_BOARD_READY, settle=2.0):
                log("replay: the board did not report ready in time, recording anyway")
            st = {"speed": speed}
            self.record("replay", frames["replay"], actions=[(0.5, click("#btnPlay"))], tick=self.replay_tick(st))

        if "live" in frames:
            self.set_caption(CAPTIONS["live"] if arc_live else CAPTIONS["replay"])
            url = a.arc + ("/?run=" + urllib.parse.quote(arc_live["_id"]) if arc_live else "/")
            if not self.goto(url, JS_BOARD_READY, settle=2.5):
                log("live: the board did not report ready in time, recording anyway")
            self.record("live", frames["live"])

        if "memory" in frames:
            self.set_caption(CAPTIONS["memory"])
            first = "notes" if mem_banks.get("notes") else "rulebooks"
            second = "lessons" if mem_banks.get("lessons") else "rulebooks"
            self.goto("%s/?tab=memory&bank=%s" % (mem_base, first), "!!document.querySelector('.mem-card')", settle=1.5)
            d = frames["memory"] / float(fps)
            acts = [(d * 0.36, click('.mem-tile[data-bank="%s"]' % second)),
                    (d * 0.62, click(".mem-list .mem-card .mem-rawbtn"))]
            self.record("memory", frames["memory"], actions=acts)

        scroll = ("(function(){var e=document.querySelector(%s);if(e&&e.scrollHeight>e.clientHeight+20)"
                  "e.scrollBy({top:Math.min(e.scrollHeight-e.clientHeight,e.clientHeight*%s),behavior:'smooth'})})()")
        if "evo_arc" in frames:
            self.set_caption(CAPTIONS["evo_arc"])
            self.goto(a.arc + "/?tab=evolution", "document.querySelector('#genList').children.length>0", settle=2.0)
            d = frames["evo_arc"] / float(fps)
            acts = [(d * 0.40, scroll % (json.dumps("#genList"), "0.6")), (d * 0.68, scroll % (json.dumps("#lessons"), "0.7"))]
            self.record("evo_arc", frames["evo_arc"], actions=acts)

        if "evo_toys" in frames:
            self.set_caption(CAPTIONS["evo_toys"])
            self.goto(a.toys + "/?tab=evolution", "document.querySelector('#genList').children.length>0", settle=2.0)
            d = frames["evo_toys"] / float(fps)
            to_reverted = ("(function(){var l=document.querySelector('#genList');if(!l)return;for(var i=0;i<l.children.length;i++){"
                           "if(/reverted/i.test(l.children[i].textContent)){l.children[i].scrollIntoView({block:'center',behavior:'smooth'});return true}}})()")
            self.record("evo_toys", frames["evo_toys"], actions=[(d * 0.3, to_reverted)])

        took = time.time() - t_start
        ms = sorted(self.shot_ms)
        log("captured %d frames in %.1f s wall (%d repeated to keep time; screenshot median %.0f ms, p95 %.0f ms)"
            % (self.frames, took, self.dups, ms[len(ms) // 2], ms[int(len(ms) * 0.95)]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output .mp4")
    ap.add_argument("--arc", default="http://127.0.0.1:8788", help="dashboard on the real ARC-AGI-3 database")
    ap.add_argument("--toys", default="http://127.0.0.1:8787", help="dashboard on the toy suite with held-out validation")
    ap.add_argument("--seconds", type=float, default=60.0, help="length of the video")
    ap.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT")
    ap.add_argument("--fps", type=int, default=10, help="capture rate (the file is written at 30 fps when this divides 30)")
    ap.add_argument("--replay-speed", type=int, default=0, choices=[0] + SPEEDS, help="fix the replay speed (0: adapt so the game ends with the segment)")
    ap.add_argument("--no-title", action="store_true", help="skip the 2 s title card")
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--ffmpeg", default=FFMPEG if os.path.exists(FFMPEG) else (shutil.which("ffmpeg") or FFMPEG))
    args = ap.parse_args()
    try:
        w, h = (int(x) for x in args.size.lower().split("x"))
    except ValueError:
        ap.error("--size must look like 1920x1080")
    args.w, args.h = w - w % 2, h - h % 2  # H.264 4:2:0 needs even sides
    args.arc, args.toys = args.arc.rstrip("/"), args.toys.rstrip("/")
    if args.fps < 1 or args.seconds <= 0:
        ap.error("--fps and --seconds must be positive")

    rec = Recorder(args)

    def on_signal(signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    rc = 1
    try:
        rec.run()
        rc = 0
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        ffrc = rec.finish_ffmpeg()
        rec.stop_chrome()
        log("chrome stopped, profile removed")
    if rc == 0 and ffrc != 0:
        log("ffmpeg failed with code", ffrc)
        rc = 2
    if rc == 0:
        log("wrote", os.path.abspath(args.out))
    return rc


if __name__ == "__main__":
    sys.exit(main())
