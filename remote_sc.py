#!/usr/bin/env python3
"""
remote_sc.py - control SoundCloud on your Mac from your phone (Spotify-Connect style).

How it works
  A tiny web server on your Mac serves a remote-control page. Two modes:

  Full mode (with the sc_remote.user.js userscript in your browser)
    The userscript talks to SoundCloud's own player inside the tab, so the
    phone gets the cover, the "Next up" queue (tap a track to play it),
    seeking (tap/drag the progress bar) and the like button.

  Basic mode (no userscript, or no SoundCloud tab open)
    Buttons send macOS media-key events (play/pause, next, previous), which
    the browser routes to whatever is playing. "Now playing" (title, artist,
    cover, progress) comes from macOS's system-wide Now Playing info - the
    same data Control Center shows. If that private API ever breaks, it falls
    back to reading the SoundCloud tab title from Chrome/Safari.

  Volume is always the Mac's system volume.

Setup (once) - use Apple's /usr/bin/python3; cover art needs its Apple signature
  /usr/bin/python3 -m venv ~/.sc-remote
  ~/.sc-remote/bin/pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices qrcode

Run
  caffeinate -i ~/.sc-remote/bin/python remote_sc.py
  (caffeinate keeps the Mac from idle-sleeping while the remote runs)

Userscript (once, for full mode)
  1. Install the Violentmonkey add-on in your browser (Tampermonkey works too).
  2. With the remote running, open http://127.0.0.1:8765/sc-remote.user.js
     in that browser on the Mac and click Install.
  3. Reload any open SoundCloud tabs.

Then scan the QR code / open the printed URL on your phone (same Wi-Fi).
Tip: "Add to Home Screen" on the phone for an app-like remote. The access
token is saved in ~/.sc_remote_token so the bookmark keeps working.

Permissions macOS will ask for
  - Accessibility for your terminal app (only for basic mode's media keys):
    System Settings > Privacy & Security > Accessibility. Restart the script after.
    Without it the buttons do nothing in basic mode; the phone will tell you.
  - Firewall "accept incoming connections": allow.
"""

import itertools
import json
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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


def system_status() -> dict:
    try:
        s = json.loads(osa(NOW_PLAYING_JXA, "JavaScript"))
    except ValueError:
        s = {}
    if not s.pop("ok", False):
        s["title"] = tab_title()
    if not isinstance(s.get("volume"), int):
        s["volume"] = None
    if s.get("art"):
        s["art"] = "/api/art?id=" + quote(s["art"])
    return s


# --- SoundCloud bridge (sc_remote.user.js) -----------------------------------
# With the userscript installed in the browser, each SoundCloud tab reports its
# player state here every second and picks up commands from the reply; the
# active tab also long-polls so taps act instantly. That adds the queue, seeking
# and liking, and needs no Accessibility permission. Without it (or if the tab
# goes quiet) everything falls back to media keys + Now Playing.
USERSCRIPT = Path(__file__).with_name("sc_remote.user.js")
BRIDGE_STALE = 8          # seconds without a report before a tab counts as gone
BRIDGE_CMDS = ("playpause", "next", "prev", "seek", "play", "like")
tabs = {}                 # tab id -> {"state", "seen", "since", "played", "acked"}
pending = {}              # tab id -> [command, ...] waiting to be fetched
bridge = threading.Condition()
seq = itertools.count(1)


def bridge_report(tab: str, state: dict) -> None:
    now = time.time()
    with bridge:
        t = tabs.setdefault(tab, {"since": now, "played": 0, "acked": 0})
        t["state"], t["seen"] = state, now
        if state.get("playing"):
            t["played"] = now
        if isinstance(state.get("ack"), int):
            t["acked"] = max(t["acked"], state["ack"])
        for k in [k for k, v in tabs.items() if now - v["seen"] > 60]:
            tabs.pop(k)
            pending.pop(k, None)
        bridge.notify_all()


def active_tab():
    """The SoundCloud tab to show and control: the one that played most recently.
    Call with `bridge` held."""
    now = time.time()
    live = [(v["played"], bool(v["state"].get("title")), v["since"], k) for k, v in tabs.items()
            if now - v["seen"] < BRIDGE_STALE and v["state"].get("ready")]
    return max(live)[-1] if live else None


def bridge_command(tab: str, cmd: dict) -> None:
    """Hand a command to a tab and wait (briefly) until it reports the result."""
    cmd["seq"] = n = next(seq)
    cmd["at"] = time.time()
    with bridge:
        pending.setdefault(tab, []).append(cmd)
        bridge.notify_all()
        bridge.wait_for(lambda: tab not in tabs or tabs[tab]["acked"] >= n, timeout=2)


def bridge_take(tab: str, wait: float = 0) -> list:
    """Return the tab's queued commands, optionally waiting (long poll) for some."""
    with bridge:
        if wait:
            bridge.wait_for(lambda: pending.get(tab), timeout=wait)
        cmds = pending.pop(tab, [])
    # a tab that was frozen shouldn't replay old taps (e.g. three "next"s) later
    return [c for c in cmds if time.time() - c.pop("at") < 5]


def bridge_status(tab: str) -> dict:
    with bridge:
        t = tabs[tab]
        s, seen = dict(t["state"]), t["seen"]
    if s.get("playing") and s.get("position") is not None:
        s["position"] = min(s.get("duration") or 1e9, s["position"] + time.time() - seen)
    for k in ("ack", "ready", "error"):
        s.pop(k, None)
    s.setdefault("title", "")  # nothing loaded yet
    s.update(source="bridge", app="SoundCloud", volume=get_volume())
    return s


def status() -> dict:
    with bridge:
        tab = active_tab()
    return bridge_status(tab) if tab else system_status()


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
body{margin:0;background:var(--bg);color:var(--fg);font:16px -apple-system,system-ui,sans-serif}
#player{min-height:100svh;display:flex;flex-direction:column;justify-content:center;gap:24px;
  padding:calc(16px + env(safe-area-inset-top)) 16px 24px}
#cover{display:block;width:min(64vw,36svh,320px);aspect-ratio:1;margin:0 auto;border-radius:12px;
  object-fit:cover;background:var(--btn);box-shadow:0 8px 32px #0008}
#cover:not([src]){visibility:hidden}
#np{text-align:center;min-height:5.5em}
#np small{display:block;color:var(--mut);font-size:12px;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:8px}
#title{font-size:20px;font-weight:600;line-height:1.3;overflow-wrap:anywhere}
#artist{color:var(--mut);margin-top:4px;min-height:1.2em}
#prog{max-width:420px;width:100%;margin:0 auto;visibility:hidden;touch-action:none}
#prog.seek{cursor:pointer}
#bar{height:4px;border-radius:2px;background:var(--btn);margin:10px 0;position:relative}
#prog.seek #bar{height:6px;border-radius:3px}
#fill{height:100%;width:0;background:var(--acc);border-radius:inherit}
#knob{display:none;position:absolute;top:50%;width:16px;height:16px;margin:-8px 0 0 -8px;
  border-radius:50%;background:var(--fg)}
#prog.seek #knob{display:block}
#times{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:center;align-items:center;gap:22px}
button{border:0;border-radius:50%;background:var(--btn);color:var(--fg);width:72px;height:72px;
  display:grid;place-items:center;touch-action:manipulation;transition:transform .08s;cursor:pointer}
button:active{transform:scale(.9);filter:brightness(1.4)}
button.big{width:96px;height:96px;background:var(--acc)}
svg{width:32px;height:32px;fill:currentColor}
.vol button,#like{width:56px;height:56px;font-size:28px;line-height:1}
#vol{min-width:56px;text-align:center;color:var(--mut);font-variant-numeric:tabular-nums}
#like{display:none;background:none;color:var(--mut)}
#like svg{width:26px;height:26px}
#like.on{color:var(--acc)}
.bridge #like{display:grid}
.bridge .vol::after{content:"";width:56px}  /* balances the like button */
#err{color:#f66;text-align:center;font-size:14px;min-height:1.2em}
#hint{color:var(--mut);text-align:center;font-size:13px;margin-top:-12px}
#stop{width:auto;height:auto;border-radius:999px;padding:8px 18px;margin:0 auto;font-size:13px;
  background:none;color:var(--mut);border:1px solid #333}
#stopped{min-height:100svh;display:none;flex-direction:column;justify-content:center;text-align:center;padding:16px}
#stopped b{font-size:20px}
#stopped p{color:var(--mut)}
body.off #player,body.off #upnext{display:none}
body.off #stopped{display:flex}
#upnext{display:none;padding:0 8px calc(24px + env(safe-area-inset-bottom));max-width:560px;margin:0 auto}
.bridge #upnext{display:block}
#upnext h2{font-size:13px;color:var(--mut);letter-spacing:.12em;text-transform:uppercase;
  font-weight:600;margin:0 8px 8px}
#queue{list-style:none;margin:0;padding:0}
#queue li{display:flex;align-items:center;gap:12px;padding:8px;border-radius:10px;cursor:pointer}
#queue li:active{background:var(--btn)}
#queue li.cur{background:#ff55001f}
#queue li.past{opacity:.45}
#queue img{width:44px;height:44px;border-radius:6px;object-fit:cover;background:var(--btn);flex:none}
#queue div{min-width:0;flex:1}
#queue b,#queue span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#queue b{font-weight:500}
#queue li.cur b{color:var(--acc)}
#queue span,#queue time{color:var(--mut);font-size:13px}
#queue time{font-variant-numeric:tabular-nums}
#more{color:var(--mut);font-size:13px;text-align:center;padding:12px}
</style></head><body>
<main id="player">
<img id="cover" alt="" onerror="this.removeAttribute('src')">
<div id="np"><small id="src">Now playing</small><div id="title">&mdash;</div><div id="artist"></div></div>
<div id="prog"><div id="bar"><div id="fill"></div><div id="knob"></div></div><div id="times"><span id="pos">0:00</span><span id="dur">0:00</span></div></div>
<div class="row">
  <button onclick="send('prev')" aria-label="Previous"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg></button>
  <button class="big" onclick="send('playpause')" aria-label="Play or pause"><svg viewBox="0 0 24 24"><path id="pp" d="M8 5v14l11-7z"/></svg></button>
  <button onclick="send('next')" aria-label="Next"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
</div>
<div class="row vol">
  <button id="like" onclick="send('like')" aria-label="Like"><svg viewBox="0 0 24 24"><path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 3 4.5 6.7 4.5c2.1 0 3.6 1.1 4.3 2.4h2c.7-1.3 2.2-2.4 4.3-2.4 3.7 0 5.8 3.9 4.3 7.3C19.5 16.4 12 21 12 21z"/></svg></button>
  <button onclick="send('voldown')" aria-label="Volume down">&minus;</button>
  <div id="vol">&ndash;</div>
  <button onclick="send('volup')" aria-label="Volume up">+</button>
</div>
<div id="err"></div>
<div id="hint"></div>
<button id="stop" onclick="stopRemote()">Stop remote</button>
</main>
<div id="stopped"><b>Remote stopped</b><p>The Mac can go to sleep now.<br>Start the script again to reconnect.</p></div>
<section id="upnext"><h2 id="qhead">Up next</h2><ol id="queue"></ol><div id="more"></div></section>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const $ = id => document.getElementById(id);
const PLAY = 'M8 5v14l11-7z', PAUSE = 'M6 5h4v14H6zm8 0h4v14h-4z';
let track = null, at = 0, artSrc, queueKey, dragging = null;  // last status + local time it was received
const fmt = s => { s = Math.max(0, Math.floor(s || 0)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const bridged = () => track && track.source === 'bridge';
const safeImg = u => typeof u === 'string' && (u.startsWith('https://') || u.startsWith('/api/art')) ? u : '';

function position(){
  return Math.min(track.duration, track.position + (track.playing ? (Date.now() - at) / 1000 : 0));
}
function tick(){
  const ok = track && track.duration > 0 && track.position != null;
  $('prog').style.visibility = ok ? 'visible' : 'hidden';
  if (!ok) return;
  const p = dragging != null ? dragging : position();
  const pct = (100 * p / track.duration) + '%';
  $('fill').style.width = pct; $('knob').style.left = pct;
  $('pos').textContent = fmt(p);
  $('dur').textContent = fmt(track.duration);
}
function renderQueue(s){
  const q = s.queue || [];
  const key = JSON.stringify([s.current, s.queueLength, q.map(t => [t.id, t.title])]);
  if (key === queueKey) return;
  queueKey = key;
  const ol = $('queue'); ol.textContent = '';
  for (const t of q) {
    const li = document.createElement('li');
    if (t.index === s.current) li.className = 'cur';
    else if (t.index < s.current) li.className = 'past';
    const img = document.createElement('img'); img.loading = 'lazy'; img.alt = '';
    const src = safeImg(t.art); if (src) img.src = src;
    const d = document.createElement('div');
    const b = document.createElement('b'); b.textContent = t.title || '…';
    const a = document.createElement('span'); a.textContent = t.artist || '';
    d.append(b, a);
    const tm = document.createElement('time'); tm.textContent = t.duration ? fmt(t.duration) : '';
    li.append(img, d, tm);
    li.onclick = () => send('play', 'index=' + t.index + (t.id ? '&id=' + t.id : ''));
    ol.append(li);
  }
  const ahead = Math.max(0, (s.queueLength || 0) - 1 - s.current);
  $('qhead').textContent = ahead ? 'Up next \\u00b7 ' + ahead + (ahead === 1 ? ' track' : ' tracks') : 'Up next';
  const shown = q.length ? q[q.length - 1].index + 1 : 0;
  $('more').textContent = s.queueLength > shown ? '+ ' + (s.queueLength - shown) + ' more' : '';
}
function show(s){
  if (s.volume != null) $('vol').textContent = s.volume + '%';
  if ('title' in s) {
    track = s; at = Date.now();
    document.body.classList.toggle('bridge', bridged());
    $('prog').classList.toggle('seek', bridged());
    $('title').textContent = s.title || '\\u2014';
    $('artist').textContent = s.artist || '';
    $('src').textContent = s.app ? 'Now playing \\u00b7 ' + s.app : 'Now playing';
    $('pp').setAttribute('d', s.playing ? PAUSE : PLAY);
    $('like').classList.toggle('on', !!s.liked);
    $('hint').textContent = !bridged() ? 'Basic mode \u00b7 open SoundCloud in a browser with the userscript for the queue & seeking'
      : (s.autoplay && s.autoplay !== 'allowed' && !s.playing) ? 'The browser is blocking playback \u00b7 click the SoundCloud tab once, or allow autoplay for soundcloud.com'
      : '';
    const src = safeImg(s.art);
    if (src !== artSrc) {  // only refetch the image when the artwork changes
      artSrc = src;
      if (src) $('cover').src = src.startsWith('/') ? src + '&t=' + encodeURIComponent(T) : src;
      else $('cover').removeAttribute('src');
    }
    if (bridged()) renderQueue(s);
    tick();
  }
  $('err').textContent = s.error || '';
}
async function call(path, opts, params){
  try {
    const r = await fetch(path + '?t=' + encodeURIComponent(T) + (params ? '&' + params : ''), opts);
    if (r.status === 403) return $('err').textContent = 'Bad link - rescan the QR code';
    show(await r.json());
  } catch(e){ $('err').textContent = 'Mac not reachable'; }
}
function send(cmd, params){
  if (navigator.vibrate) navigator.vibrate(10);
  if (cmd === 'like') $('like').classList.toggle('on');
  if (cmd === 'playpause' && track) { track.position = position(); at = Date.now(); track.playing = !track.playing; $('pp').setAttribute('d', track.playing ? PAUSE : PLAY); }
  call('/api/' + cmd, {method:'POST'}, params);
}

// tap or drag the progress bar to seek (only with the userscript)
function seekPos(e){
  const r = $('bar').getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * track.duration;
}
$('prog').addEventListener('pointerdown', e => {
  if (!bridged() || !(track.duration > 0)) return;
  $('prog').setPointerCapture(e.pointerId); dragging = seekPos(e); tick();
});
$('prog').addEventListener('pointermove', e => { if (dragging != null) { dragging = seekPos(e); tick(); } });
$('prog').addEventListener('pointerup', e => {
  if (dragging == null) return;
  const p = seekPos(e); dragging = null;
  track.position = p; at = Date.now(); tick();
  send('seek', 'pos=' + p.toFixed(1));
});
$('prog').addEventListener('pointercancel', () => { dragging = null; tick(); });

let stopped = false;
const poll = () => { if (!stopped) call('/api/status'); };
poll(); setInterval(poll, 3000); setInterval(tick, 500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
async function stopRemote(){
  if (!confirm('Stop the remote? The music will be paused so the Mac can go to sleep.')) return;
  stopped = true;
  try { await fetch('/api/shutdown?t=' + encodeURIComponent(T), {method:'POST'}); } catch(e) {}
  document.body.classList.add('off');
}
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

    def _from_userscript(self) -> bool:
        # Bridge routes are for the browser on this Mac only. The custom header
        # can't be sent cross-origin without a CORS preflight (which we never
        # answer), so random web pages can't poke at the bridge either.
        return self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-SC-Remote") == "1"

    def _query(self, name: str, default=None):
        return parse_qs(urlparse(self.path).query).get(name, [default])[0]

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/sc-remote.user.js":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._send(403, "open this on the Mac itself", "text/plain")
            script = USERSCRIPT.read_text().replace("__PORT__", str(PORT))
            return self._send(200, script, "text/javascript; charset=utf-8")
        if path == "/bridge/commands":
            if not self._from_userscript():
                return self._send(403, "forbidden", "text/plain")
            return self._send(200, json.dumps(bridge_take(self._query("tab", ""), wait=20)))
        if not self._authorized():
            return self._send(403, "forbidden", "text/plain")
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, json.dumps(status()))
        if path == "/api/art":
            art = artwork()
            return self._send(200, *art) if art else self._send(404, "no artwork", "text/plain")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/bridge/state":
            if not self._from_userscript():
                return self._send(403, "forbidden", "text/plain")
            try:
                length = int(self.headers.get("Content-Length", 0))
                state = json.loads(self.rfile.read(length)) if 0 < length < 1_000_000 else None
            except ValueError:
                state = None
            if not isinstance(state, dict):
                return self._send(400, "bad state", "text/plain")
            tab = self._query("tab", "")
            bridge_report(tab, state)
            with bridge:
                active = active_tab() == tab
            # Only the active tab keeps a long poll open (browsers allow ~6
            # connections per host); others get commands with this reply.
            return self._send(200, json.dumps({"active": active, "cmds": bridge_take(tab)}))
        if not self._authorized():
            return self._send(403, "forbidden", "text/plain")
        cmd = path.removeprefix("/api/")
        if cmd == "shutdown":
            return self._shutdown()
        with bridge:
            tab = active_tab()
        if tab and cmd in BRIDGE_CMDS:
            c = {"cmd": cmd}
            try:
                if cmd == "seek":
                    c["pos"] = float(self._query("pos"))
                elif cmd == "play":
                    c["index"] = int(self._query("index"))
                    if self._query("id"):
                        c["id"] = int(self._query("id"))
            except (TypeError, ValueError):
                return self._send(400, json.dumps({"error": "bad parameters"}))
            bridge_command(tab, c)
        elif cmd in BRIDGE_CMDS and cmd not in NX_KEYTYPE:
            return self._send(409, json.dumps({"error": "Needs the SoundCloud userscript in the browser"}))
        elif cmd in NX_KEYTYPE:
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

    def _shutdown(self):
        """The phone's "Stop remote": pause the music (a Mac playing audio won't
        sleep), reply, then end serve_forever() - same as Ctrl+C."""
        s = status()
        if s.get("playing"):
            with bridge:
                tab = active_tab()
            if tab:
                bridge_command(tab, {"cmd": "playpause"})
            elif AXIsProcessTrusted():
                media_key(NX_KEYTYPE["playpause"])
        self._send(200, json.dumps({"stopped": True}))
        print("\nStopped from the phone.", flush=True)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

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
        print("!  Your terminal lacks Accessibility permission - media keys (basic mode) won't work.\n"
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
    print(f"  Or (survives IP changes): http://{host}:{PORT}/?t={TOKEN}")
    print(f"  Userscript for full mode (open in your browser on this Mac):\n"
          f"    http://127.0.0.1:{PORT}/sc-remote.user.js\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print('Ctrl+C (or "Stop remote" on the phone) to stop.')

    try:
        server.serve_forever()  # returns when the phone asks to stop
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
