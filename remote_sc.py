#!/usr/bin/env python3
"""
remote_sc.py - control SoundCloud on your Mac from your phone (Spotify-Connect style).

How it works
  A tiny web server on your Mac serves a remote-control page. Buttons send
  macOS media-key events (play/pause, next, previous). Browsers route these
  to the SoundCloud tab via the Media Session API. Volume uses the system
  volume. "Now playing" (title, artist, cover art, progress) is read from macOS's
  system-wide Now Playing info - the same data Control Center shows - so it
  works with any browser, including Firefox. If that private API ever breaks,
  it falls back to reading the SoundCloud tab title from Chrome/Safari.

Setup (once) - use Apple's /usr/bin/python3; cover art needs its Apple signature
  /usr/bin/python3 -m venv ~/.sc-remote
  ~/.sc-remote/bin/pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices qrcode

Run
  caffeinate -i ~/.sc-remote/bin/python remote_sc.py
  (caffeinate keeps the Mac from idle-sleeping while the remote runs)

Then scan the QR code / open the printed URL on your phone (same Wi-Fi).
Tip: "Add to Home Screen" on the phone for an app-like remote. The access
token is saved in ~/.sc_remote_token so the bookmark keeps working.

Permissions macOS will ask for
  - Accessibility for your terminal app (needed to send media keys):
    System Settings > Privacy & Security > Accessibility. Restart the script after.
    Without it the buttons silently do nothing; the phone will tell you.
  - Firewall "accept incoming connections": allow.
"""

import json
import re
import secrets
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from AppKit import NSEvent
    from Quartz import CGEventPost, kCGHIDEventTap
except ImportError:
    sys.exit("Missing PyObjC. Run: pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices")

try:
    from ApplicationServices import AXIsProcessTrusted
except ImportError:
    def AXIsProcessTrusted():
        return True  # can't check; assume OK

PORT = 8765

# --- access token (persisted so a home-screen bookmark keeps working) --------
TOKEN_FILE = Path.home() / ".sc_remote_token"
if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(8)
    TOKEN_FILE.write_text(TOKEN)
    TOKEN_FILE.chmod(0o600)

# --- media keys --------------------------------------------------------------
NX_KEYTYPE = {"playpause": 16, "next": 17, "prev": 18}
NS_SYSTEM_DEFINED = 14


def media_key(code: int) -> None:
    """Post a hardware-style media key press (down + up)."""
    for down in (True, False):
        flags = 0xA00 if down else 0xB00
        data1 = (code << 16) | ((0xA if down else 0xB) << 8)
        ev = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NS_SYSTEM_DEFINED, (0, 0), flags, 0, 0, None, 8, data1, -1
        )
        CGEventPost(kCGHIDEventTap, ev.CGEvent())


# --- AppleScript helpers -----------------------------------------------------
def osa(script: str, lang: str = "AppleScript") -> str:
    try:
        r = subprocess.run(["osascript", "-l", lang, "-e", script], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def get_volume():
    v = osa("output volume of (get volume settings)")
    return int(v) if v.isdigit() else None


def set_volume(v: int) -> None:
    osa(f"set volume output volume {max(0, min(100, v))}")


# --- now playing -------------------------------------------------------------
# Reads macOS's Now Playing info via the private MediaRemote framework. Since
# macOS 15.4 only Apple-signed processes may read it; osascript is one, so we
# run this as JXA. Needs no permissions. Also returns the system volume so a
# status poll costs a single osascript call.
NOW_PLAYING_JXA = r"""
ObjC.import('AppKit');
function run() {
  const out = {};
  const sa = Application.currentApplication();
  sa.includeStandardAdditions = true;
  try { out.volume = sa.getVolumeSettings().outputVolume; } catch (e) {}
  $.NSBundle.bundleWithPath('/System/Library/PrivateFrameworks/MediaRemote.framework/').load;
  const R = $.NSClassFromString('MRNowPlayingRequest');
  if (R.isNil()) return JSON.stringify(out);
  out.ok = true;
  const info = R.localNowPlayingItem.nowPlayingInfo;
  if (info.isNil()) return JSON.stringify(out);
  const get = k => { const v = info.valueForKey('kMRMediaRemoteNowPlayingInfo' + k); return v.isNil() ? null : v.js; };
  out.title = get('Title') || '';
  out.artist = get('Artist') || '';
  out.duration = get('Duration');
  out.art = get('ArtworkIdentifier');
  out.playing = !!R.localIsPlaying;
  const elapsed = get('ElapsedTime'), rate = get('PlaybackRate');
  const ts = info.valueForKey('kMRMediaRemoteNowPlayingInfoTimestamp');
  if (elapsed != null) {
    // elapsed is a snapshot taken at ts; extrapolate to now
    const since = ts.isNil() ? 0 : Date.now() / 1000 - ts.timeIntervalSince1970;
    out.position = elapsed + (out.playing ? (rate || 1) * since : 0);
  }
  try {
    const client = R.localNowPlayingPlayerPath.client;
    const url = $.NSWorkspace.sharedWorkspace.URLForApplicationWithBundleIdentifier(client.bundleIdentifier);
    if (!url.isNil()) out.app = $.NSFileManager.defaultManager.displayNameAtPath(url.path).js.replace(/\.app$/, '');
  } catch (e) {}
  return JSON.stringify(out);
}
"""

# Fallback: browsers whose SoundCloud tab title we can read. (app name, title property)
# Triggers an Automation permission prompt per browser.
BROWSERS = [
    ("Google Chrome", "title"),
    ("Arc", "title"),
    ("Brave Browser", "title"),
    ("Microsoft Edge", "title"),
    ("Safari", "name"),
]
INSTALLED = [(a, p) for a, p in BROWSERS if Path(f"/Applications/{a}.app").exists()]


def tab_title() -> str:
    for app, prop in INSTALLED:
        title = osa(f'''
if application "{app}" is running then
  tell application "{app}"
    repeat with w in windows
      repeat with t in tabs of w
        if (URL of t as text) contains "soundcloud.com" then return {prop} of t
      end repeat
    end repeat
  end tell
end if
return ""''')
        if title:
            title = re.sub(r"^[▶►\s]+", "", title)       # leading play glyph
            title = re.sub(r"\s*\|\s*[^|]*SoundCloud.*$", "", title)  # site suffix
            return title
    return ""


# Artwork has to be requested asynchronously, which JXA can't do, so it's
# fetched in-process. That only passes macOS's check because Apple's own
# /usr/bin/python3 is Apple-signed - with Homebrew/pyenv Python the remote
# works, just without cover art.
try:
    import objc
    import threading
    from Foundation import NSBundle

    NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/MediaRemote.framework").load()
    MRNowPlayingRequest = objc.lookUpClass("MRNowPlayingRequest")
    objc.registerMetaDataForSelector(
        b"MRNowPlayingRequest", b"requestNowPlayingItemArtworkWithCompletion:",
        {"arguments": {2: {"callable": {"retval": {"type": b"v"},
                                        "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}, 2: {"type": b"@"}}}}}},
    )
except Exception:
    MRNowPlayingRequest = None


def artwork():
    """Return (jpeg/png bytes, mime type) of the current track's cover, or None."""
    if MRNowPlayingRequest is None:
        return None
    done, res = threading.Event(), []

    def cb(art, _err):
        if art is not None and art.imageData():
            res.append(bytes(art.imageData()))
        done.set()

    try:
        MRNowPlayingRequest.alloc().init().requestNowPlayingItemArtworkWithCompletion_(cb)
    except Exception:
        return None
    if not done.wait(2) or not res:
        return None
    data = res[0]
    return data, "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def status() -> dict:
    try:
        s = json.loads(osa(NOW_PLAYING_JXA, "JavaScript"))
    except ValueError:
        s = {}
    if not s.pop("ok", False):
        s["title"] = tab_title()
    if not isinstance(s.get("volume"), int):
        s["volume"] = None
    return s


# --- web page ----------------------------------------------------------------
PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SC Remote">
<meta name="theme-color" content="#111111">
<title>SC Remote</title>
<style>
:root{--bg:#111;--fg:#f2f2f2;--mut:#888;--acc:#ff5500;--btn:#222}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;min-height:100svh;background:var(--bg);color:var(--fg);
  font:16px -apple-system,system-ui,sans-serif;display:flex;flex-direction:column;
  justify-content:center;gap:32px;padding:24px 16px calc(24px + env(safe-area-inset-bottom))}
#cover{display:block;width:min(64vw,36svh,320px);aspect-ratio:1;margin:0 auto -8px;border-radius:12px;
  object-fit:cover;background:var(--btn);box-shadow:0 8px 32px #0008}
#cover:not([src]){visibility:hidden}
#np{text-align:center;min-height:6em}
#np small{display:block;color:var(--mut);font-size:12px;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:8px}
#title{font-size:20px;font-weight:600;line-height:1.3;overflow-wrap:anywhere}
#artist{color:var(--mut);margin-top:4px;min-height:1.2em}
#prog{max-width:420px;width:100%;margin:0 auto;visibility:hidden}
#bar{height:4px;border-radius:2px;background:var(--btn);overflow:hidden}
#fill{height:100%;width:0;background:var(--acc)}
#times{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;
  margin-top:6px;font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:center;align-items:center;gap:22px}
button{border:0;border-radius:50%;background:var(--btn);color:var(--fg);width:72px;height:72px;
  display:grid;place-items:center;touch-action:manipulation;transition:transform .08s}
button:active{transform:scale(.9);filter:brightness(1.4)}
button.big{width:96px;height:96px;background:var(--acc)}
svg{width:32px;height:32px;fill:currentColor}
.vol button{width:56px;height:56px;font-size:28px;line-height:1}
#vol{min-width:56px;text-align:center;color:var(--mut);font-variant-numeric:tabular-nums}
#err{color:#f66;text-align:center;font-size:14px;min-height:1.2em}
</style></head><body>
<img id="cover" alt="" onerror="this.removeAttribute('src')">
<div id="np"><small id="src">Now playing</small><div id="title">&mdash;</div><div id="artist"></div></div>
<div id="prog"><div id="bar"><div id="fill"></div></div><div id="times"><span id="pos">0:00</span><span id="dur">0:00</span></div></div>
<div class="row">
  <button onclick="send('prev')" aria-label="Previous"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg></button>
  <button class="big" onclick="send('playpause')" aria-label="Play or pause"><svg viewBox="0 0 24 24"><path id="pp" d="M8 5v14l11-7z"/></svg></button>
  <button onclick="send('next')" aria-label="Next"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
</div>
<div class="row vol">
  <button onclick="send('voldown')" aria-label="Volume down">&minus;</button>
  <div id="vol">&ndash;</div>
  <button onclick="send('volup')" aria-label="Volume up">+</button>
</div>
<div id="err"></div>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const $ = id => document.getElementById(id);
const PLAY = 'M8 5v14l11-7z', PAUSE = 'M6 5h4v14H6zm8 0h4v14h-4z';
let track = null, at = 0, artId;  // last now-playing info + local time it was received
const fmt = s => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
function tick(){
  const ok = track && track.duration > 0 && track.position != null;
  $('prog').style.visibility = ok ? 'visible' : 'hidden';
  if (!ok) return;
  const p = Math.min(track.duration, track.position + (track.playing ? (Date.now() - at) / 1000 : 0));
  $('fill').style.width = (100 * p / track.duration) + '%';
  $('pos').textContent = fmt(p);
  $('dur').textContent = fmt(track.duration);
}
function show(s){
  if (s.volume != null) $('vol').textContent = s.volume + '%';
  if ('title' in s) {
    track = s; at = Date.now();
    $('title').textContent = s.title || '\\u2014';
    $('artist').textContent = s.artist || '';
    $('src').textContent = s.app ? 'Now playing \\u00b7 ' + s.app : 'Now playing';
    $('pp').setAttribute('d', s.playing ? PAUSE : PLAY);
    if (s.art !== artId) {  // only refetch the image when the track's artwork changes
      artId = s.art;
      if (artId) $('cover').src = '/api/art?t=' + encodeURIComponent(T) + '&id=' + encodeURIComponent(artId);
      else $('cover').removeAttribute('src');
    }
    tick();
  }
  $('err').textContent = s.error || '';
}
async function call(path, opts){
  try {
    const r = await fetch(path + '?t=' + encodeURIComponent(T), opts);
    if (r.status === 403) return $('err').textContent = 'Bad link - rescan the QR code';
    show(await r.json());
  } catch(e){ $('err').textContent = 'Mac not reachable'; }
}
function send(cmd){
  if (navigator.vibrate) navigator.vibrate(10);
  call('/api/' + cmd, {method:'POST'});
}
const poll = () => call('/api/status');
poll(); setInterval(poll, 3000); setInterval(tick, 500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
</script></body></html>"""


# --- HTTP server -------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        return secrets.compare_digest(t, TOKEN)

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._authorized():
            return self._send(403, "forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, json.dumps(status()))
        if path == "/api/art":
            art = artwork()
            return self._send(200, *art) if art else self._send(404, "no artwork", "text/plain")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return self._send(403, "forbidden", "text/plain")
        cmd = urlparse(self.path).path.removeprefix("/api/")
        if cmd in NX_KEYTYPE:
            if not AXIsProcessTrusted():
                return self._send(200, json.dumps({"error": "Mac terminal needs Accessibility permission"}))
            media_key(NX_KEYTYPE[cmd])
            time.sleep(0.4)  # let the browser update Now Playing before we report it
        elif cmd in ("volup", "voldown"):
            v = get_volume()
            if v is not None:
                set_volume(v + (6 if cmd == "volup" else -6))
        else:
            return self._send(404, "unknown command", "text/plain")
        self._send(200, json.dumps(status()))

    def log_message(self, *args):
        pass  # keep the terminal quiet


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    if not AXIsProcessTrusted():
        print("!  Your terminal lacks Accessibility permission - media keys won't work.\n"
              "   System Settings > Privacy & Security > Accessibility, enable it, then restart.\n")

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on port {PORT} ({e.strerror}). Is the remote already running?")

    host = socket.gethostname()
    if not host.endswith(".local"):
        host += ".local"
    url = f"http://{lan_ip()}:{PORT}/?t={TOKEN}"

    print("SoundCloud remote running.\n")
    print(f"  Open on your phone:  {url}")
    print(f"  Or (survives IP changes): http://{host}:{PORT}/?t={TOKEN}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print("Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
