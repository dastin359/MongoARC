#!/usr/bin/env python3
"""Record a silent, captioned demo video (H.264 MP4) of a mongoARC dashboard.

    python3 scripts/record_demo.py --out .local/demo.mp4 \
        [--arc http://127.0.0.1:8788] [--run RUN_ID] [--seconds 60] [--size 1920x1080]

It starts its own headless Chrome (own profile under .local/, own random DevTools port), drives it
over the DevTools protocol, screenshots the page at a steady rate and pipes the frames to ffmpeg.
It only reads: HTTP GETs against the dashboard, nothing else. The Chrome it started is the only
process it stops.

Storyboard, one dashboard (--arc) and one run, scaled to --seconds:
  title card (2 s), LIVE PLAY replay of the run (16 s), LIVE PLAY following a running game (8 s),
  MEMORY tab filtered to the run: notes, lessons, one raw document (16 s),
  EVOLUTION tab of the run, two captions (18 s).
The run is --run if given, else the best finished run in /api/runs at record time (role train
preferred, most levels cleared, won preferred). A segment whose kind of run does not exist hands
its time to its neighbour (no running game: the replay runs longer). --toys is accepted and unused.

Page loads are not recorded: the clock of the video is the frame count, so the file is exactly
--seconds long while the recording itself takes roughly 25 s longer.
"""
import argparse
import base64
import json
import os
import random
import select
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
    "title": "mongoARC: an agent with a MongoDB memory, playing ARC-AGI-3",
    "replay": "A Claude agent plays a real ARC-AGI-3 game. After each level it saves what it learned to MongoDB Atlas, and searches it before the next.",
    "live": "The pad shows the button it pressed; a crosshair shows the pixel it clicked.",
    "memory": "Its memory is documents in MongoDB, written in its own words. Atlas Vector Search embeds each note and each question itself.",
    "evo": "Level by level: what the agent saved and what it searched for, straight from the database.",
    "evo2": "The hard metric: actions spent on each level against the human baseline, beside a no-memory control run.",
}
# Seconds of a 60 s video; scaled to --seconds.
WEIGHTS = {"title": 2, "replay": 16, "live": 8, "memory": 16, "evo": 18}


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
    """Minimal synchronous DevTools client; events are read (and screencast frames kept) whenever it waits."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0
        self.frame = None   # newest screencast frame (base64 JPEG)
        self.frame_n = 0

    def _event(self, msg):
        if msg.get("method") == "Page.screencastFrame":
            p = msg["params"]
            self.frame = p["data"]
            self.frame_n += 1
            self.n += 1  # Chrome sends the next frame only after this; nobody waits for its reply
            self.ws.send(json.dumps({"id": self.n, "method": "Page.screencastFrameAck", "params": {"sessionId": p["sessionId"]}}))

    def pump(self, seconds):
        """Sleep for `seconds` of real time while taking in whatever Chrome sends."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if select.select([self.ws.sock], [], [], left)[0]:
                raw = self.ws.recv()
                if raw:
                    self._event(json.loads(raw))

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
            self._event(msg)

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
def pick_runs(base, want=None):
    """(the run the video is about, a running run or None, whether the first is finished or was asked for)."""
    runs = get_json(base + "/api/runs") or []
    done = [r for r in runs if r.get("status") in FINISHED]
    live = [r for r in runs if r.get("status") == "running"]
    done.sort(key=lambda r: (r.get("role") == "train", r.get("level") or 0, r.get("status") == "won", r.get("n_actions") or 0), reverse=True)
    live.sort(key=lambda r: (r.get("role") == "train", r.get("n_actions") or 0), reverse=True)
    running = live[0] if live else None
    if want:
        doc = next((r for r in runs if r.get("_id") == want), None) or get_json(base + "/api/runs/" + urllib.parse.quote(want))
        if not isinstance(doc, dict) or not doc.get("_id"):
            log("WARNING: --run %s is not known to %s; its URLs are opened anyway" % (want, base))
            doc = {"_id": want}
        return doc, running, True
    if done:
        return done[0], running, True
    return running, running, False


def bank_n(base, bank, run_id=None):
    d = get_json("%s/api/memory/%s?limit=5%s" % (base, bank, "&run_id=" + urllib.parse.quote(run_id) if run_id else ""))
    return len(d) if isinstance(d, list) else 0


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
        self.cap_src = None
        self.cast_frames = 0
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
        self.cdp.call("Page.enable")
        if self.a.capture == "screencast":
            self.cdp.call("Page.startScreencast", format="jpeg", quality=88, maxWidth=self.w, maxHeight=self.h, everyNthFrame=1)

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
        self.cap_src = caption_script(text, self.w, self.h) if text else None
        if text:
            self.cap_id = self.cdp.call("Page.addScriptToEvaluateOnNewDocument", source=caption_script(text, self.w, self.h))["identifier"]

    def goto(self, url, ready_js=None, settle=1.5, timeout=10.0):
        """Navigate, then wait real time: for the document, then for ready_js (bounded), then a fixed settle.
        Nothing here waits for the network to go idle (the MEMORY tab holds an event stream open)."""
        self.cdp.call("Page.navigate", url=url)
        t_end = time.time() + timeout
        self.cdp.pump(0.4)
        while time.time() < t_end:
            if self.cdp.js("document.readyState") in ("interactive", "complete") and self.cdp.js("location.href") not in (None, "about:blank"):
                break
            self.cdp.pump(0.15)
        if self.cap_src and not self.cdp.js("!!document.getElementById('__demo_cap')"):
            self.cdp.js(self.cap_src)  # the document-start script did not run: put the caption in now
        ok = True
        if ready_js:
            ok = False
            while time.time() < t_end:
                if self.cdp.js(ready_js):
                    ok = True
                    break
                self.cdp.pump(0.2)
        self.cdp.pump(settle)
        if self.cap_src and not self.cdp.js("!!document.getElementById('__demo_cap')"):
            log("WARNING: caption bar missing on", url)
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
        frame, seen, truth_at = None, self.cdp.frame_n, -1.0
        cast = self.a.capture == "screencast"
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
            self.cdp.pump(wait)
            now = time.monotonic() - t0
            if cast and frame is not None and self.cdp.frame_n != seen:  # the page repainted: Chrome already sent the picture
                frame, seen = base64.b64decode(self.cdp.frame), self.cdp.frame_n
                self.cast_frames += 1
            elif not cast or frame is None or now - truth_at >= 0.5:  # still page (or first frame): a real screenshot
                frame, truth_at = self.shot(), now
                seen = self.cdp.frame_n
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
        base = a.arc
        main, running, replayable = pick_runs(base, a.run)
        rid = main["_id"] if main else None
        log("run: %s (%s) | running run: %s" % (rid, (main or {}).get("status"), (running or {}).get("_id")))

        secs = {k: v * a.seconds / 60.0 for k, v in WEIGHTS.items()}
        if a.no_title:
            secs["replay"] += secs.pop("title")
        if not (main and replayable):  # nothing to replay: one LIVE PLAY segment
            if not running:
                log("no finished and no running run: LIVE PLAY will show the idle page")
            secs["live"] += secs.pop("replay")
        elif not running:  # nothing in flight: the replay runs longer
            secs["replay"] += secs.pop("live")

        mem_rid = rid
        if rid and not any(bank_n(base, b, rid) for b in ("notes", "lessons", "rulebooks")):
            log("memory: nothing stored for run %s, showing every run" % rid)
            mem_rid = None
        first = "notes" if bank_n(base, "notes", mem_rid) else "rulebooks"
        second = "lessons" if bank_n(base, "lessons", mem_rid) else ("rulebooks" if first != "rulebooks" else "generations")
        log("memory: %s then %s, run filter %s" % (first, second, mem_rid))

        order = [k for k in ("title", "replay", "live", "memory", "evo") if k in secs]
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
        q = urllib.parse.quote

        if "title" in frames:
            self.goto(title_url(CAPTIONS["title"], self.w, self.h), settle=0.6)
            self.record("title", frames["title"])

        if "replay" in frames:
            self.set_caption(CAPTIONS["replay"])
            n = main.get("n_actions") or 0
            dur = frames["replay"] / float(fps)
            want = n * 0.3 / max(1.0, dur - 2.0) if n else 4  # about 0.3 s per action at 1x
            speed = a.replay_speed or min(SPEEDS, key=lambda s: abs(s - want))
            url = "%s/?run=%s&mode=replay&speed=%d" % (base, q(rid), speed)
            # also wait (bounded) for the page's own memory panel, which fills a few seconds after the board
            ready = "(%s)&&document.querySelector('#memBanks').children.length>0" % JS_BOARD_READY
            if not self.goto(url, ready, settle=1.5, timeout=14.0):
                log("replay: the board or its memory panel was not ready in time, recording anyway")
            st = {"speed": speed}
            self.record("replay", frames["replay"], actions=[(0.5, click("#btnPlay"))], tick=self.replay_tick(st))

        if "live" in frames:
            self.set_caption(CAPTIONS["live"] if running else CAPTIONS["replay"])
            url = base + ("/?run=" + q(running["_id"]) if running else "/")  # ?run= with no mode follows a running game live
            if not self.goto(url, JS_BOARD_READY, settle=2.5):
                log("live: the board did not report ready in time, recording anyway")
            self.record("live", frames["live"])

        if "memory" in frames:
            self.set_caption(CAPTIONS["memory"])
            url = "%s/?tab=memory&bank=%s%s" % (base, first, "&run_id=" + q(mem_rid) if mem_rid else "")
            if not self.goto(url, "!!document.querySelector('.mem-card')", settle=1.5):
                log("memory: no document card appeared in time, recording anyway")
            d = frames["memory"] / float(fps)
            # RAW DOCUMENT on the first card; if the run filter left the bank empty, drop the filter first
            raw = ("(function(){function go(){var b=document.querySelector('.mem-list .mem-card .mem-rawbtn');if(b){b.click();return true}return false}"
                   "if(!go()){var x=document.querySelector('.mem-runx');if(x){x.click();setTimeout(go,900)}}})()")
            acts = [(d * 0.36, click('.mem-tile[data-bank="%s"]' % second)), (d * 0.60, raw)]
            self.record("memory", frames["memory"], actions=acts)

        if "evo" in frames:
            self.set_caption(CAPTIONS["evo"])
            url = "%s/?tab=evolution%s" % (base, "&run=" + q(rid) if rid else "")
            ready = "(function(){var m=document.querySelector('#tab-evolution');return !!m&&!m.hidden&&m.innerText.trim().length>300})()"
            if not self.goto(url, ready, settle=2.0, timeout=8.0):
                log("evolution: the tab did not fill in time, recording anyway")
            d = frames["evo"] / float(fps)
            # ids of the rebuilt tab are not relied on: scroll its first scrollable list a little, then swap the caption
            scroll = ("(function(){var l=document.querySelectorAll('#tab-evolution .scroll');for(var i=0;i<l.length;i++){var e=l[i];"
                      "if(e.scrollHeight>e.clientHeight+20){e.scrollBy({top:Math.min(e.scrollHeight-e.clientHeight,e.clientHeight*0.6),behavior:'smooth'});return true}}})()")
            swap = "(function(){var c=document.getElementById('__demo_cap');if(c)c.textContent=%s})()" % json.dumps(CAPTIONS["evo2"])
            self.record("evo", frames["evo"], actions=[(d * 0.28, scroll), (d * 0.5, swap)])

        took = time.time() - t_start
        ms = sorted(self.shot_ms) or [0]
        log("wrote %d frames in %.1f s wall: %d screencast frames, %d screenshots (median %.0f ms), %d slots repeated because a capture ran late"
            % (self.frames, took, self.cast_frames, len(self.shot_ms), ms[len(ms) // 2], self.dups))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output .mp4")
    ap.add_argument("--arc", default="http://127.0.0.1:8788", help="base URL of the dashboard to record")
    ap.add_argument("--run", default="", help="run id to show (default: the best finished run in /api/runs)")
    ap.add_argument("--toys", default="", help="accepted for compatibility, unused")
    ap.add_argument("--seconds", type=float, default=60.0, help="length of the video")
    ap.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT")
    ap.add_argument("--fps", type=int, default=10, help="capture rate (the file is written at 30 fps when this divides 30)")
    ap.add_argument("--capture", default="screencast", choices=["screencast", "screenshot"],
                    help="screencast: frames pushed by Chrome as the page repaints, screenshots while it is still; screenshot: one Page.captureScreenshot per frame (slower)")
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
    args.arc = args.arc.rstrip("/")
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
