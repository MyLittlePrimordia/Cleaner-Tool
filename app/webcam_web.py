"""
Webcam Test engine (Tools tab) — zero dependency, driver-agnostic.

Why this shape instead of DirectShow
------------------------------------
The previous engine drove DirectShow (quartz.dll) through raw ctypes
vtable calls and drew into a Tk HWND. It black-screened on real
hardware (OBSBOT Meet SE, Windows 11 LTSC) and reported the failure as
"in use by another program", which was untrue. DirectShow is a
deprecated 1990s capture stack: modern UVC cameras reach it only
through the MF->DShow bridge, which is where format negotiation for
MJPEG/NV12 cameras commonly falls over. Getting that right per-camera
is an unbounded amount of work, and every wrong guess is a black
rectangle for a user we cannot debug.

So the camera is opened the way every working webcam tester on the
internet opens one: `getUserMedia` inside a browser. The browser owns
a maintained MediaFoundation pipeline, per-camera quirk handling, the
permission prompt, and the release-on-close semantics — all of which
we would otherwise have to reimplement badly.

To use it without shipping a dependency, this module starts a tiny
stdlib HTTP server bound to 127.0.0.1, serves ONE page from memory
(nothing is ever written to disk), and opens the user's own browser at
it. The loopback origin matters: `getUserMedia` is gated behind a
secure context, and browsers treat http://127.0.0.1 as trustworthy
while refusing file:// — which is exactly why "just open an HTML file"
does not work here.

Dependencies added: none. http.server, socket, secrets, threading,
subprocess, winreg, webbrowser are all stdlib. Nothing is installed,
nothing is bundled, nothing is written to disk.

Lifecycle / camera release
--------------------------
The page holds the camera; the Tk dialog holds nothing. The two stay
in sync over the loopback link:

  * page -> server: a heartbeat every second while the tab is open
  * page -> server: a `bye` beacon on pagehide (window closed)
  * server -> page: when the server stops answering, the page stops
    its tracks and tells the user the test ended

So closing the browser window releases the camera (tracks stopped on
pagehide, and the browser tears the capture down with the document
anyway), and closing the Tk dialog kills the server, which makes the
page stop its own tracks within a second. Either end can go first and
the camera still ends up free for OBS/Discord/Teams.

Hardening
---------
Bound to 127.0.0.1 only (never 0.0.0.0), every path is prefixed with a
128-bit single-use token, the Host header is pinned to loopback (DNS
rebinding), and the server is torn down as soon as the dialog closes.
"""

from __future__ import annotations

import secrets
import socket
import threading
import time

# Fixed preferred port: a browser's camera permission is remembered per
# ORIGIN, and the origin includes the port. Keeping the port stable
# means users who tick "remember this decision" are not re-prompted on
# every test. Falls back to an ephemeral port if it is taken.
_PREFERRED_PORT = 47653

# How long after the last heartbeat we consider the page gone.
_BEAT_TIMEOUT_S = 4.0
# Grace period after launch before a missing heartbeat counts (cold
# browser starts on a slow disk can take a while).
_LAUNCH_GRACE_S = 25.0


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #
# Written from scratch for this app. The addpipe/webcam-tester project
# that inspired this approach is AGPL-3.0; none of its code, markup or
# CSS is reproduced here, only the (public, standard) idea of calling
# getUserMedia from a secure context. That keeps Cleaner Tool's license
# untouched.

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Webcam Test - Cleaner Tool</title>
<style>
  :root{
    --bg:#0D1117; --card:#151B24; --surface:#1C2430; --hair:#232B38;
    --text:#EAEFF5; --sub:#7C8797; --ok:#3ED598; --warn:#F2B84B;
    --bad:#F2555F; --accent:#38BDF8;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{
    background:var(--bg); color:var(--text);
    font-family:"Segoe UI",system-ui,sans-serif;
    display:flex; align-items:center; justify-content:center;
    padding:20px;
  }
  .wrap{width:100%; max-width:840px}
  h1{font-size:17px; font-weight:600; margin-bottom:2px}
  .sub{font-size:12px; color:var(--sub); margin-bottom:14px}
  .stage{
    position:relative; width:100%; aspect-ratio:16/9;
    background:#000; border:1px solid var(--hair); border-radius:12px;
    overflow:hidden; display:flex; align-items:center; justify-content:center;
  }
  video{width:100%; height:100%; object-fit:contain; display:none; background:#000}
  video.on{display:block}
  .placeholder{
    text-align:center; color:var(--sub); font-size:13px; padding:0 30px;
    line-height:1.7;
  }
  .placeholder .big{font-size:38px; display:block; margin-bottom:8px; opacity:.5}
  .controls{display:flex; gap:10px; align-items:center; margin-top:14px; flex-wrap:wrap}
  button{
    font:600 13px/1 "Segoe UI",system-ui,sans-serif; color:#0B0E13;
    background:var(--ok); border:0; border-radius:9px;
    padding:12px 22px; cursor:pointer; transition:filter .15s;
  }
  button:hover:not(:disabled){filter:brightness(1.1)}
  button:disabled{opacity:.5; cursor:default}
  button.stop{background:var(--bad); color:#fff}
  select{
    background:var(--surface); color:var(--text); border:1px solid var(--hair);
    border-radius:9px; padding:11px 10px; font:400 12px "Segoe UI",sans-serif;
    max-width:300px;
  }
  .hide{display:none !important}
  .stats{
    display:flex; gap:8px; margin-top:12px; flex-wrap:nowrap; overflow:hidden;
  }
  .stat{
    flex:1 1 0; min-width:0; background:var(--card); border:1px solid var(--hair);
    border-radius:10px; padding:9px 11px;
  }
  .stat .k{
    font-size:10px; letter-spacing:.07em; text-transform:uppercase;
    color:var(--sub); margin-bottom:3px; white-space:nowrap;
  }
  .stat .v{
    font-size:15px; font-weight:600; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis;
  }
  .msg{
    margin-top:12px; font-size:13px; line-height:1.6; border-radius:10px;
    padding:11px 13px; border:1px solid var(--hair); background:var(--card);
    color:var(--sub);
  }
  .msg.bad{border-color:var(--bad); color:#FFD9DC}
  .msg.good{border-color:var(--ok); color:#CFF4E4}
  .msg.warn{border-color:var(--warn); color:#F7E3BD}
  .msg b{color:var(--text)}
</style>
</head>
<body>
<div class="wrap">
  <h1>Webcam Test</h1>
  <div class="sub">Nothing is recorded, uploaded or saved. The preview runs on your PC only.</div>

  <div class="stage">
    <video id="v" playsinline autoplay muted></video>
    <div class="placeholder" id="ph">
      <span class="big">&#128247;</span>
      Press <b>Start camera</b> below.<br>
      Your browser will ask for permission &mdash; choose <b>Allow</b>.
    </div>
  </div>

  <div class="controls">
    <button id="btn">Start camera</button>
    <select id="pick" class="hide"></select>
  </div>

  <div class="stats hide" id="stats">
    <div class="stat"><div class="k">Camera</div><div class="v" id="s-name">&mdash;</div></div>
    <div class="stat"><div class="k">Resolution</div><div class="v" id="s-res">&mdash;</div></div>
    <div class="stat"><div class="k">Megapixels</div><div class="v" id="s-mp">&mdash;</div></div>
    <div class="stat"><div class="k">Shape</div><div class="v" id="s-ar">&mdash;</div></div>
    <div class="stat"><div class="k">Frames / sec</div><div class="v" id="s-fps">&mdash;</div></div>
  </div>

  <div class="msg" id="msg">Waiting to start.</div>
</div>

<script>
(function(){
  "use strict";
  var BASE = window.location.pathname.replace(/\/+$/, "");
  var v = document.getElementById("v");
  var ph = document.getElementById("ph");
  var btn = document.getElementById("btn");
  var pick = document.getElementById("pick");
  var stats = document.getElementById("stats");
  var msg = document.getElementById("msg");
  var stream = null, fpsHandle = null, frames = [], ended = false;

  function say(text, kind){
    msg.innerHTML = text;
    msg.className = "msg" + (kind ? " " + kind : "");
  }
  function gcd(a, b){ return b ? gcd(b, a % b) : a; }
  function shape(w, h){
    if(!w || !h){ return "\u2014"; }
    var g = gcd(w, h), a = w / g, b = h / g;
    if(a > 40 || b > 40){ return (w / h).toFixed(2) + " : 1"; }
    return a + " : " + b;
  }

  // ---- live FPS -----------------------------------------------------
  // requestVideoFrameCallback fires once per frame the camera actually
  // delivers, so this is measured throughput, not the number the driver
  // claims. Browsers without it fall back to the reported rate.
  function startFps(track){
    var el = document.getElementById("s-fps");
    if(typeof v.requestVideoFrameCallback !== "function"){
      var s = track.getSettings ? track.getSettings() : {};
      el.textContent = s.frameRate ? Math.round(s.frameRate) + " (reported)" : "\u2014";
      return;
    }
    frames = [];
    var step = function(){
      var now = performance.now();
      frames.push(now);
      while(frames.length && now - frames[0] > 1000){ frames.shift(); }
      if(frames.length > 1){
        var span = (frames[frames.length - 1] - frames[0]) / 1000;
        if(span > 0.2){ el.textContent = ((frames.length - 1) / span).toFixed(1); }
      }
      if(stream){ fpsHandle = v.requestVideoFrameCallback(step); }
    };
    fpsHandle = v.requestVideoFrameCallback(step);
  }

  function fillStats(track){
    var s = track.getSettings ? track.getSettings() : {};
    var w = s.width || v.videoWidth || 0;
    var h = s.height || v.videoHeight || 0;
    document.getElementById("s-name").textContent = track.label || "Camera";
    document.getElementById("s-name").title = track.label || "";
    document.getElementById("s-res").textContent = w && h ? w + " \u00d7 " + h : "\u2014";
    document.getElementById("s-mp").textContent =
      w && h ? ((w * h) / 1000000).toFixed(1) + " MP" : "\u2014";
    document.getElementById("s-ar").textContent = shape(w, h);
    stats.classList.remove("hide");
  }

  // ---- device list --------------------------------------------------
  // Labels are blank until permission is granted, so this runs after
  // the first successful getUserMedia, never before.
  function listCameras(currentId){
    if(!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices){ return; }
    navigator.mediaDevices.enumerateDevices().then(function(list){
      var cams = list.filter(function(d){ return d.kind === "videoinput"; });
      if(cams.length < 2){ pick.classList.add("hide"); return; }
      pick.innerHTML = "";
      cams.forEach(function(d, i){
        var o = document.createElement("option");
        o.value = d.deviceId;
        o.textContent = d.label || ("Camera " + (i + 1));
        if(d.deviceId === currentId){ o.selected = true; }
        pick.appendChild(o);
      });
      pick.classList.remove("hide");
    }).catch(function(){});
  }

  // ---- start / stop -------------------------------------------------
  function stop(quiet, keepPicker){
    if(fpsHandle && typeof v.cancelVideoFrameCallback === "function"){
      try{ v.cancelVideoFrameCallback(fpsHandle); }catch(e){}
    }
    fpsHandle = null;
    if(stream){
      stream.getTracks().forEach(function(t){ try{ t.stop(); }catch(e){} });
    }
    stream = null;
    try{ v.srcObject = null; }catch(e){}
    v.classList.remove("on");
    ph.style.display = "";
    stats.classList.add("hide");
    if(!keepPicker){ pick.classList.add("hide"); }
    btn.textContent = "Start camera";
    btn.className = "";
    if(!quiet){ say("Camera stopped and released. Other apps can use it again."); }
  }

  function explain(err){
    var n = (err && err.name) || "";
    if(n === "NotAllowedError" || n === "PermissionDeniedError"){
      return ["<b>Permission was blocked.</b> Allow camera access for this page " +
              "(camera icon in the window's title bar), or check Windows " +
              "Settings &rsaquo; Privacy &amp; security &rsaquo; Camera and make sure " +
              "<b>Let desktop apps access your camera</b> is on.", "bad"];
    }
    if(n === "NotFoundError" || n === "DevicesNotFoundError"){
      return ["<b>No camera found.</b> Windows is not reporting any video device. " +
              "Check the USB cable, then look in Device Manager under Cameras.", "bad"];
    }
    if(n === "NotReadableError" || n === "TrackStartError"){
      return ["<b>The camera is busy.</b> Windows handed it to another app " +
              "(OBS, Discord, Teams, Zoom, the OBSBOT utility...). Close that " +
              "app and press Start again.", "bad"];
    }
    if(n === "OverconstrainedError"){
      return ["<b>That camera cannot do the requested format.</b> Press Start " +
              "again to retry at its default resolution.", "warn"];
    }
    if(n === "SecurityError"){
      return ["<b>Blocked by browser security.</b> This page must be opened by " +
              "Cleaner Tool, not from a saved file.", "bad"];
    }
    if(n === "AbortError"){
      return ["<b>The camera driver stopped responding.</b> Unplug and replug " +
              "the camera, or run Repair &rsaquo; Restart Camera in Cleaner Tool.", "bad"];
    }
    return ["<b>Could not start the camera.</b> " +
            (n || (err && err.message) || "Unknown error") + ".", "bad"];
  }

  function start(deviceId){
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
      say("<b>This browser is too old.</b> It has no camera API. Try Edge, " +
          "Chrome or Firefox.", "bad");
      return;
    }
    btn.disabled = true;
    say("Waiting for permission\u2026 choose <b>Allow</b> in the prompt.");
    var want = deviceId
      ? { deviceId: { exact: deviceId } }
      : { width: { ideal: 1920 }, height: { ideal: 1080 } };

    var go = function(constraints, isRetry){
      return navigator.mediaDevices.getUserMedia({ video: constraints, audio: false })
        .then(function(s){
          stream = s;
          v.srcObject = s;
          v.classList.add("on");
          ph.style.display = "none";
          btn.disabled = false;
          btn.textContent = "Stop camera";
          btn.className = "stop";
          var track = s.getVideoTracks()[0];
          var wire = function(){ fillStats(track); startFps(track); };
          if(v.readyState >= 2){ wire(); } else { v.onloadedmetadata = wire; }
          track.addEventListener("ended", function(){
            if(!ended){ stop(true); say("<b>The camera disconnected.</b> " +
              "Windows dropped the device \u2014 check the USB cable.", "bad"); }
          });
          listCameras(track.getSettings ? track.getSettings().deviceId : "");
          say("<b>Live.</b> If you can see yourself, your camera works. " +
              "Close this window when you are done \u2014 that releases it.", "good");
        })
        .catch(function(err){
          if(err && err.name === "OverconstrainedError" && !isRetry){
            return go(true, true);
          }
          btn.disabled = false;
          var r = explain(err);
          say(r[0], r[1]);
        });
    };
    go(want, false);
  }

  btn.addEventListener("click", function(){
    if(stream){ stop(false); } else { start(pick.value || null); }
  });
  pick.addEventListener("change", function(){
    if(stream){ stop(true, true); start(pick.value); }
  });

  // ---- link to Cleaner Tool -----------------------------------------
  // Heartbeat both proves the window is still open and detects the app
  // closing the dialog. Either way the camera never outlives the test.
  function beat(){
    if(ended){ return; }
    fetch(BASE + "/beat", { cache: "no-store" }).catch(function(){
      ended = true;
      stop(true);
      btn.disabled = true;
      say("<b>Test ended in Cleaner Tool.</b> The camera has been released. " +
          "You can close this window.", "warn");
    });
  }
  setInterval(beat, 1000);
  beat();

  window.addEventListener("pagehide", function(){
    ended = true;
    stop(true);
    try{ navigator.sendBeacon(BASE + "/bye", ""); }catch(e){}
  });
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Loopback server
# --------------------------------------------------------------------------- #

class WebcamTestServer:
    """One-page loopback HTTP server for the webcam test. Never raises.

    start() -> URL string, or "" if a socket could not be bound.
    stop()  -> idempotent teardown.
    """

    def __init__(self):
        self._httpd = None
        self._thread = None
        self._token = secrets.token_urlsafe(16)
        self._port = 0
        self._started_at = 0.0
        self._last_beat = 0.0
        self._said_bye = False
        self._seen = False

    # -- lifecycle ---------------------------------------------------- #

    def start(self):
        try:
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        except Exception:
            return ""

        outer = self
        page = _PAGE.encode("utf-8")

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "CleanerToolWebcamTest/1.0"

            def log_message(self, *_a):
                pass  # never spam stderr / the frozen console

            def _deny(self, code=404):
                try:
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception:
                    pass

            def _host_ok(self):
                # DNS-rebinding guard: only loopback Host headers are served.
                host = (self.headers.get("Host") or "").split(":")[0].strip()
                return host in ("127.0.0.1", "localhost", "[::1]", "::1", "")

            def do_GET(self):
                if not self._host_ok():
                    return self._deny(403)
                path = (self.path or "").split("?")[0].rstrip("/")
                root = "/" + outer._token
                if path == root:
                    outer._seen = True
                    outer._last_beat = time.monotonic()
                    body = page
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.end_headers()
                        self.wfile.write(body)
                    except Exception:
                        pass
                    return
                if path == root + "/beat":
                    outer._seen = True
                    outer._last_beat = time.monotonic()
                    return self._deny(204)
                if path == root + "/bye":
                    outer._said_bye = True
                    return self._deny(204)
                return self._deny(404)

            def do_POST(self):
                # sendBeacon posts; drain the body so the socket closes clean.
                if not self._host_ok():
                    return self._deny(403)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 0:
                        self.rfile.read(min(n, 4096))
                except Exception:
                    pass
                if (self.path or "").split("?")[0].rstrip("/") \
                        == "/" + outer._token + "/bye":
                    outer._said_bye = True
                    return self._deny(204)
                return self._deny(404)

        httpd = None
        for port in (_PREFERRED_PORT, 0):
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
                break
            except OSError:
                httpd = None
                continue
            except Exception:
                httpd = None
                break
        if httpd is None:
            return ""

        try:
            httpd.daemon_threads = True
            self._httpd = httpd
            self._port = httpd.server_address[1]
            self._thread = threading.Thread(
                target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                daemon=True, name="webcam-test-http")
            self._thread.start()
        except Exception:
            self.stop()
            return ""

        self._started_at = time.monotonic()
        self._last_beat = 0.0
        self._said_bye = False
        self._seen = False
        return "http://127.0.0.1:%d/%s/" % (self._port, self._token)

    def stop(self):
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        self._thread = None
        self._started_at = 0.0

    # -- state -------------------------------------------------------- #

    @property
    def running(self):
        return self._httpd is not None

    def page_open(self):
        """True while the test page is still alive on the other end.

        False once the user closes the browser window (bye beacon, or
        the heartbeat simply stops)."""
        if self._httpd is None:
            return False
        if self._said_bye:
            return False
        now = time.monotonic()
        if not self._seen:
            # Browser still starting up.
            return (now - self._started_at) < _LAUNCH_GRACE_S
        return (now - self._last_beat) < _BEAT_TIMEOUT_S

    def page_reached(self):
        """True once the browser actually loaded the page."""
        return bool(self._seen)


# --------------------------------------------------------------------------- #
# Browser launch
# --------------------------------------------------------------------------- #

_APP_PATHS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
)
# Chromium family first: --app= gives a clean frameless popup window with
# no tabs or address bar, which is what this test should look like.
_CHROMIUM_EXES = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe",
                  "opera.exe")


def _find_chromium():
    """Full path to an installed Chromium browser, or "". Never raises."""
    try:
        import os
        import winreg
    except Exception:
        return ""
    for exe in _CHROMIUM_EXES:
        for base in _APP_PATHS:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, base + "\\" + exe) as key:
                        path, _t = winreg.QueryValueEx(key, "")
                    path = str(path or "").strip('"')
                    if path and os.path.isfile(path):
                        return path
                except OSError:
                    continue
                except Exception:
                    continue
    return ""


def open_test_window(url):
    """Show `url` in the user's browser. Returns a short status word:

        "app"     - opened as a clean popup window (Chromium --app)
        "browser" - opened as a normal tab in the default browser
        ""        - no browser available at all
    """
    exe = _find_chromium()
    if exe:
        try:
            import subprocess
            flags = 0
            try:
                flags = subprocess.CREATE_NO_WINDOW
            except Exception:
                flags = 0
            subprocess.Popen(
                [exe, "--app=" + url, "--window-size=900,760"],
                close_fds=True, creationflags=flags)
            return "app"
        except Exception:
            pass
    try:
        import webbrowser
        if webbrowser.open(url, new=1, autoraise=True):
            return "browser"
    except Exception:
        pass
    try:
        import os
        os.startfile(url)  # noqa: S606 - loopback URL we just built
        return "browser"
    except Exception:
        return ""
