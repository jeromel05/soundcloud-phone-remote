#!/usr/bin/env python3
"""
remote_sc.py - control SoundCloud (and VLC) on your Mac from your phone, Spotify-Connect style.

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

  VLC
    Controlled over AppleScript: title, seeking, -10/+10 s, play/pause and
    fullscreen. When both SoundCloud and VLC are open, the phone shows a
    switcher; it also follows whichever player you start on the Mac.

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
  - Automation ("Terminal wants to control VLC"): allow, for VLC support.
  - Firewall "accept incoming connections": allow.
"""

import itertools
import json
import re
import secrets
import socket
import socketserver
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
def osa(script: str, lang: str = "AppleScript", *args: str) -> str:
    try:
        r = subprocess.run(["osascript", "-l", lang, "-e", script, *args], capture_output=True, text=True, timeout=3)
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
        note_playing("soundcloud", any(v["state"].get("playing") for v in tabs.values()
                                       if now - v["seen"] < BRIDGE_STALE))
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
    s["app"] = "SoundCloud"
    return s


# --- VLC -----------------------------------------------------------------------
# VLC is scripted over Apple Events (macOS asks once to let the terminal control
# VLC). Every call first checks VLC is running, so we never launch it.
VLC_CMDS = ("playpause", "next", "prev", "seek", "skip", "fullscreen")
VLC_JXA = r"""
function run(argv) {
  const out = {};
  const sa = Application.currentApplication();  // the Mac's volume rides along, saving a call
  sa.includeStandardAdditions = true;
  try { out.volume = sa.getVolumeSettings().outputVolume; } catch (e) {}
  let v;
  try { v = Application('org.videolan.vlc'); if (!v.running()) return JSON.stringify(out); } catch (e) { return JSON.stringify(out); }
  const [cmd, arg] = argv;
  try {
    if (cmd === 'playpause') v.play();                       // VLC's "play" toggles
    else if (cmd === 'pause') { if (v.playing()) v.play(); }
    else if (cmd === 'seek') v.currentTime = Math.round(+arg);
    else if (cmd === 'skip') v.currentTime = Math.max(0, v.currentTime() + Math.round(+arg));
    else if (cmd === 'fullscreen') v.fullscreen();
    else if (cmd === 'next') v.next();
    else if (cmd === 'prev') v.previous();
  } catch (e) {}
  if (cmd) delay(0.15);  // let VLC settle before reading its state back
  out.running = true;
  try {
    const name = v.nameOfCurrentItem();
    if (name) Object.assign(out, {name, position: v.currentTime(), duration: v.durationOfCurrentItem(),
                                  playing: v.playing(), fullscreen: v.fullscreenMode()});
  } catch (e) {}
  return JSON.stringify(out);
}
"""
FILE_TAGS = re.compile(
    r"(\d{3,4}p|4k|uhd|hdr\d*|web(rip|-?dl)?|blu-?ray|br-?rip|bd-?rip|dvd-?rip|hdtv|hd-?rip|x26[45]|h\.?26[45]"
    r"|hevc|xvid|aac\d*|ac3|dts|ddp?[\d.]*|10bit|remux|proper|repack|extended|unrated)", re.I)


def movie_title(filename: str) -> str:
    """'Big.Buck.Bunny.2008.1080p.WEBRip.x264.AAC-[GROUP].mp4' -> 'Big Buck Bunny (2008)'."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename)
    stem = re.sub(r"\[[^\]]*\]", " ", stem)                 # [release group]
    words = [w for w in re.split(r"[._\s]+", stem) if w]
    is_year = lambda w: re.fullmatch(r"(19|20)\d\d", w.strip("()"))
    out = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if out and is_year(w) and not is_year(nxt):  # "Blade.Runner.2049.2017" keeps 2049
            return f"{' '.join(out)} ({w.strip('()')})"
        if FILE_TAGS.fullmatch(w.strip("-")):
            break
        out.append(w)
    return " ".join(out).strip(" -") or filename


def vlc(cmd: str = "", arg: str = "") -> dict:
    """Run an optional command, then return VLC's state (no "name" if VLC isn't
    running or has nothing loaded) plus the Mac's volume."""
    try:
        return json.loads(osa(VLC_JXA, "JavaScript", cmd, arg) or "{}")
    except ValueError:
        return {}


def vlc_status(v: dict) -> dict:
    return {"title": movie_title(v["name"]), "artist": "", "app": "VLC", "art": None,
            "duration": v.get("duration") or 0, "position": v.get("position") or 0,
            "playing": bool(v.get("playing")), "fullscreen": bool(v.get("fullscreen"))}


# --- sources: SoundCloud (userscript), VLC, or whatever macOS says is playing --
SOURCE_NAMES = {"soundcloud": "SoundCloud", "vlc": "VLC"}
focus: dict = {"src": None}  # source shown on the phone; guarded by `bridge`
was_playing = {}             # source -> playing at last look, to spot (re)starts


def note_playing(src: str, playing) -> None:
    """The phone follows whatever starts playing. Call with `bridge` held."""
    if playing and not was_playing.get(src):
        focus["src"] = src
    was_playing[src] = bool(playing)


def gather() -> dict:
    """Look at every source once: {"tab", "vlc", "system", "avail": [...]}."""
    with bridge:
        tab = active_tab()
        sc_playing = bool(tab) and bool(tabs[tab]["state"].get("playing"))
    # each osascript call costs ~0.15s, so ask VLC and Now Playing at the same time
    got = {}
    th = threading.Thread(target=lambda: got.update(vlc=vlc()))
    th.start()
    sysst = system_status() if not tab else None  # no userscript: SoundCloud etc. via Now Playing
    th.join()
    v = got["vlc"]
    has_vlc = bool(v.get("name"))
    volume = v.get("volume") if isinstance(v.get("volume"), int) else (sysst or {}).get("volume")
    if sysst and (sysst.get("app") == "VLC" or not sysst.get("title")):
        sysst = None  # VLC is covered by its own source
    avail = [s for s, ok in (("soundcloud", tab), ("vlc", has_vlc), ("system", sysst)) if ok]
    with bridge:
        if tab:
            note_playing("soundcloud", sc_playing)
        if has_vlc:
            note_playing("vlc", v.get("playing"))
        if sysst:
            note_playing("system", sysst.get("playing"))
    return {"tab": tab, "vlc": v, "system": sysst, "avail": avail, "volume": volume,
            "playing": {"soundcloud": sc_playing, "vlc": bool(v.get("playing")),
                        "system": bool(sysst and sysst.get("playing"))}}


def chosen_source(g: dict):
    with bridge:
        f = focus["src"]
    if f in g["avail"]:
        return f
    playing = [s for s in g["avail"] if g["playing"][s]]
    return (playing or g["avail"] or [None])[0]


def status(g=None) -> dict:
    g = g or gather()
    src = chosen_source(g)
    if src == "soundcloud":
        s = bridge_status(g["tab"])
    elif src == "vlc":
        s = vlc_status(g["vlc"])
    else:
        s = g["system"] or system_status()
        src = "system"
    s["volume"] = g["volume"]
    s["source"] = src
    s["sources"] = [{"id": x, "playing": g["playing"][x],
                     "name": SOURCE_NAMES.get(x) or (g["system"] or {}).get("app") or "Now Playing"}
                    for x in g["avail"]]
    return s


# --- web page ----------------------------------------------------------------
PAGE = """<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SC Remote">
<meta name="theme-color" content="#0a2a40">
<meta name="color-scheme" content="dark">
<title>SC Remote</title>
<style>
/* Frutiger Aero-ish: deep aqua sky, frosted glass, glossy orbs. The accent
   follows the player: SoundCloud orange, VLC turquoise. */
:root{color-scheme:dark;--fg:#f4fbff;--mut:rgba(214,236,250,.66);--acc:#ff5500;--acc2:#ffa45c;
  --glass:rgba(255,255,255,.09);--edge:rgba(255,255,255,.24);--sky:#0a2a40}
body.vlc{--acc:#13c4bd;--acc2:#8af4ec}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#040d15;color:var(--fg);font:16px -apple-system,system-ui,sans-serif;
  text-shadow:0 1px 2px rgba(0,0,0,.25)}
body::before{content:"";position:fixed;inset:0;z-index:-2;
  background:radial-gradient(120% 60% at 50% -8%,color-mix(in srgb,var(--acc) 42%,transparent),transparent 62%),
    radial-gradient(80% 50% at 0% 100%,rgba(30,170,255,.22),transparent 65%),
    radial-gradient(70% 45% at 100% 85%,rgba(120,230,255,.14),transparent 60%),
    linear-gradient(180deg,var(--sky) 0%,#071a28 55%,#040d15 100%)}
body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.55;
  background:radial-gradient(circle at 12% 22%,rgba(255,255,255,.16) 0 3px,transparent 4px),
    radial-gradient(circle at 86% 14%,rgba(255,255,255,.10) 0 14px,rgba(255,255,255,.18) 15px,transparent 16px),
    radial-gradient(circle at 78% 58%,rgba(255,255,255,.07) 0 22px,rgba(255,255,255,.14) 23px,transparent 24px),
    radial-gradient(circle at 8% 70%,rgba(255,255,255,.06) 0 9px,rgba(255,255,255,.14) 10px,transparent 11px),
    radial-gradient(circle at 50% 92%,rgba(255,255,255,.12) 0 2px,transparent 3px)}
#player{min-height:100svh;display:flex;flex-direction:column;justify-content:center;gap:24px;
  padding:calc(16px + env(safe-area-inset-top)) 16px 24px}
#cover,#poster{width:min(64vw,36svh,320px);margin:0 auto;border-radius:14px;border:1px solid var(--edge);
  box-shadow:0 10px 36px rgba(0,0,0,.45),0 0 0 4px rgba(255,255,255,.04);
  -webkit-box-reflect:below 4px linear-gradient(transparent 84%,rgba(255,255,255,.10))}
#cover{display:block;aspect-ratio:1;object-fit:cover;background:var(--glass)}
#cover:not([src]){visibility:hidden}
#np{text-align:center;min-height:5.5em}
#np small{display:block;color:var(--mut);font-size:12px;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:8px}
#title{font-size:20px;font-weight:600;line-height:1.3;overflow-wrap:anywhere}
#artist{color:var(--mut);margin-top:4px;min-height:1.2em}
#prog{max-width:420px;width:100%;margin:0 auto;visibility:hidden;touch-action:none}
#prog.seek{cursor:pointer}
#bar{height:5px;border-radius:3px;margin:10px 0;position:relative;background:rgba(255,255,255,.14);
  box-shadow:inset 0 1px 2px rgba(0,0,0,.4),0 1px 0 rgba(255,255,255,.08)}
#prog.seek #bar{height:7px;border-radius:4px}
#fill{height:100%;width:0;border-radius:inherit;background:linear-gradient(180deg,var(--acc2),var(--acc));
  box-shadow:0 0 10px color-mix(in srgb,var(--acc) 70%,transparent)}
#knob{display:none;position:absolute;top:50%;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;
  background:radial-gradient(circle at 35% 30%,#fff,#dcefff 45%,#9fc3dd);box-shadow:0 2px 6px rgba(0,0,0,.4)}
#prog.seek #knob{display:block}
#times{display:flex;justify-content:space-between;color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:center;align-items:center;gap:22px}
button{position:relative;overflow:hidden;border:1px solid var(--edge);border-radius:50%;color:var(--fg);
  width:72px;height:72px;display:grid;place-items:center;touch-action:manipulation;cursor:pointer;
  transition:transform .08s,filter .15s;
  background:linear-gradient(180deg,rgba(255,255,255,.20),rgba(255,255,255,.06) 50%,rgba(255,255,255,.03) 51%,rgba(255,255,255,.10));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.45),0 6px 18px rgba(0,0,0,.35);
  -webkit-backdrop-filter:blur(14px) saturate(150%);backdrop-filter:blur(14px) saturate(150%)}
button:active{transform:scale(.92);filter:brightness(1.3)}
.row button:not(#like):not(#fs)::before{content:"";position:absolute;left:14%;right:14%;top:5%;height:44%;
  border-radius:50%;background:linear-gradient(180deg,rgba(255,255,255,.55),rgba(255,255,255,0));pointer-events:none}
button.big{width:96px;height:96px;border-color:rgba(255,255,255,.45);
  background:radial-gradient(120% 95% at 50% 0%,var(--acc2) 0%,var(--acc) 55%,color-mix(in srgb,var(--acc) 72%,#000) 100%);
  box-shadow:0 10px 30px color-mix(in srgb,var(--acc) 55%,transparent),inset 0 2px 0 rgba(255,255,255,.6),inset 0 -8px 16px rgba(0,0,0,.18)}
svg{position:relative;width:32px;height:32px;fill:currentColor;filter:drop-shadow(0 1px 1px rgba(0,0,0,.3))}
.vol button,#like{width:56px;height:56px;font-size:28px;line-height:1}
#vol{min-width:56px;text-align:center;color:var(--mut);font-variant-numeric:tabular-nums}
#like,#fs{display:none;background:none;border:0;box-shadow:none;-webkit-backdrop-filter:none;backdrop-filter:none;color:var(--mut)}
#like svg,#fs svg{width:26px;height:26px}
#like.on{color:var(--acc)}
#fs.on{color:var(--acc2)}
.sc #like,.vlc #fs{display:grid}
.sc .vol::after,.vlc .vol::after{content:"";width:56px}  /* balances the like/fullscreen button */
#back,#fwd{display:none}
.vlc #back,.vlc #fwd{display:grid}
.vlc #prev,.vlc #next{display:none}
#back svg,#fwd svg{width:36px;height:36px}
#srcs{display:flex;justify-content:center;gap:8px;flex-wrap:wrap}
#srcs:empty{display:none}
#srcs button{--c:#4fb3ff;--c2:#a9dcff;width:auto;height:auto;border-radius:999px;padding:8px 16px;font-size:14px;
  color:var(--mut);display:flex;align-items:center;gap:6px}
#srcs button[data-id=soundcloud]{--c:#ff5500;--c2:#ffa45c}
#srcs button[data-id=vlc]{--c:#13c4bd;--c2:#8af4ec}
#srcs button.on{color:#fff;font-weight:600;border-color:rgba(255,255,255,.45);
  background:linear-gradient(180deg,var(--c2),var(--c) 55%,color-mix(in srgb,var(--c) 80%,#000));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.55),0 4px 16px color-mix(in srgb,var(--c) 45%,transparent)}
#srcs button.playing::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--c);
  box-shadow:0 0 6px var(--c)}
#srcs button.on.playing::before{background:#fff;box-shadow:0 0 6px #fff}
#poster{display:none;aspect-ratio:16/10;place-items:center;color:rgba(255,255,255,.35);
  background:linear-gradient(160deg,rgba(255,255,255,.16),rgba(255,255,255,.04) 55%,color-mix(in srgb,var(--acc) 18%,transparent));
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}
#poster svg{width:64px;height:64px}
.vlc #cover{display:none}
.vlc #poster{display:grid}
#err{color:#ff8a80;text-align:center;font-size:14px;min-height:1.2em}
#hint{color:var(--mut);text-align:center;font-size:13px;margin-top:-12px}
#stop{width:auto;height:auto;border-radius:999px;padding:8px 18px;margin:0 auto;font-size:13px;color:var(--mut);
  background:rgba(255,255,255,.05);box-shadow:none}
#stopped{min-height:100svh;display:none;flex-direction:column;justify-content:center;text-align:center;padding:16px}
#stopped b{font-size:20px}
#stopped p{color:var(--mut)}
body.off #player,body.off #upnext{display:none}
body.off #stopped{display:flex}
#upnext{display:none;margin:0 12px calc(24px + env(safe-area-inset-bottom));padding:14px 8px 8px;max-width:560px;
  border-radius:20px;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.06);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 10px 30px rgba(0,0,0,.3);
  -webkit-backdrop-filter:blur(18px) saturate(140%);backdrop-filter:blur(18px) saturate(140%)}
@media (min-width:584px){#upnext{margin-left:auto;margin-right:auto}}
.sc #upnext{display:block}
#upnext h2{font-size:13px;color:var(--mut);letter-spacing:.12em;text-transform:uppercase;
  font-weight:600;margin:0 8px 8px}
#queue{list-style:none;margin:0;padding:0}
#queue li{display:flex;align-items:center;gap:12px;padding:8px;border-radius:12px;cursor:pointer}
#queue li:active{background:rgba(255,255,255,.10)}
#queue li.cur{background:color-mix(in srgb,var(--acc) 20%,transparent);box-shadow:inset 0 1px 0 rgba(255,255,255,.15)}
#queue li.past{opacity:.45}
#queue img{width:44px;height:44px;border-radius:8px;object-fit:cover;flex:none;background:var(--glass);
  border:1px solid rgba(255,255,255,.14)}
#queue div{min-width:0;flex:1}
#queue b,#queue span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#queue b{font-weight:500}
#queue li.cur b{color:var(--acc2)}
#queue span,#queue time{color:var(--mut);font-size:13px}
#queue time{font-variant-numeric:tabular-nums}
#more{color:var(--mut);font-size:13px;text-align:center;padding:12px}
</style></head><body>
<main id="player">
<nav id="srcs"></nav>
<img id="cover" alt="" onerror="this.removeAttribute('src')">
<div id="poster"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zm1 2v2h2V6zm12 0v2h2V6zM5 11v2h2v-2zm12 0v2h2v-2zM5 16v2h2v-2zm12 0v2h2v-2zM9 6v12h6V6z"/></svg></div>
<div id="np"><small id="src">Now playing</small><div id="title">&mdash;</div><div id="artist"></div></div>
<div id="prog"><div id="bar"><div id="fill"></div><div id="knob"></div></div><div id="times"><span id="pos">0:00</span><span id="dur">0:00</span></div></div>
<div class="row">
  <button id="prev" onclick="send('prev')" aria-label="Previous"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg></button>
  <button id="back" onclick="send('skip', 'by=-10')" aria-label="Back 10 seconds"><svg viewBox="0 0 24 24"><path d="M11.99 5V1l-5 5 5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6h-2c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/><text x="12" y="16.2" font-size="6.5" text-anchor="middle" font-family="-apple-system,sans-serif" font-weight="700">10</text></svg></button>
  <button class="big" onclick="send('playpause')" aria-label="Play or pause"><svg viewBox="0 0 24 24"><path id="pp" d="M8 5v14l11-7z"/></svg></button>
  <button id="next" onclick="send('next')" aria-label="Next"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/></svg></button>
  <button id="fwd" onclick="send('skip', 'by=10')" aria-label="Forward 10 seconds"><svg viewBox="0 0 24 24"><path d="M12.01 5V1l5 5-5 5V7c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z"/><text x="12" y="16.2" font-size="6.5" text-anchor="middle" font-family="-apple-system,sans-serif" font-weight="700">10</text></svg></button>
</div>
<div class="row vol">
  <button id="like" onclick="send('like')" aria-label="Like"><svg viewBox="0 0 24 24"><path d="M12 21s-7.5-4.6-9.6-9.2C.9 8.4 3 4.5 6.7 4.5c2.1 0 3.6 1.1 4.3 2.4h2c.7-1.3 2.2-2.4 4.3-2.4 3.7 0 5.8 3.9 4.3 7.3C19.5 16.4 12 21 12 21z"/></svg></button>
  <button id="fs" onclick="send('fullscreen')" aria-label="Fullscreen"><svg viewBox="0 0 24 24"><path d="M3 3h7v2H5v5H3zm11 0h7v7h-2V5h-5zM3 14h2v5h5v2H3zm16 0h2v7h-7v-2h5z"/></svg></button>
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
const fmt = s => {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, pad = n => String(n).padStart(2, '0');
  return h ? h + ':' + pad(m) + ':' + pad(s % 60) : m + ':' + pad(s % 60);
};
const source = () => track && track.source;           // 'soundcloud' | 'vlc' | 'system'
const canSeek = () => ['soundcloud', 'vlc'].includes(source());
let srcsKey;
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
    document.body.classList.toggle('sc', source() === 'soundcloud');
    document.body.classList.toggle('vlc', source() === 'vlc');
    $('prog').classList.toggle('seek', canSeek());
    renderSources(s.sources || [], s.source);
    $('title').textContent = s.title || '\\u2014';
    $('artist').textContent = s.artist || '';
    $('src').textContent = s.app ? 'Now playing \\u00b7 ' + s.app : 'Now playing';
    $('pp').setAttribute('d', s.playing ? PAUSE : PLAY);
    $('like').classList.toggle('on', !!s.liked);
    $('fs').classList.toggle('on', !!s.fullscreen);
    $('hint').textContent = source() === 'system' ? 'Basic mode \u00b7 open SoundCloud in a browser with the userscript for the queue & seeking'
      : (source() === 'soundcloud' && s.autoplay && s.autoplay !== 'allowed' && !s.playing) ? 'The browser is blocking playback \u00b7 click the SoundCloud tab once, or allow autoplay for soundcloud.com'
      : '';
    const src = safeImg(s.art);
    if (src !== artSrc) {  // only refetch the image when the artwork changes
      artSrc = src;
      if (src) $('cover').src = src.startsWith('/') ? src + '&t=' + encodeURIComponent(T) : src;
      else $('cover').removeAttribute('src');
    }
    if (source() === 'soundcloud') renderQueue(s);
    tick();
  }
  $('err').textContent = s.error || '';
}
async function call(path, opts, params){
  // give up after 5s: a sleeping Mac never answers, and hung requests would pile up
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 5000);
  try {
    const r = await fetch(path + '?t=' + encodeURIComponent(T) + (params ? '&' + params : ''), Object.assign({signal: ctl.signal}, opts));
    if (r.status === 403) return $('err').textContent = 'Bad link - rescan the QR code';
    show(await r.json());  // clears the message below once the Mac is back
  } catch(e){ $('err').textContent = 'Can\u2019t reach the Mac (asleep?) \u00b7 retrying\u2026'; }
  finally { clearTimeout(timer); }
}
function renderSources(list, cur){
  const key = JSON.stringify([cur, list]);
  if (key === srcsKey) return;
  srcsKey = key;
  const nav = $('srcs'); nav.textContent = '';
  if (list.length < 2) return;  // nothing to switch between
  for (const x of list) {
    const b = document.createElement('button');
    b.textContent = x.name;
    b.className = (x.id === cur ? 'on ' : '') + (x.playing ? 'playing' : '');
    b.dataset.id = x.id;  // per-player colour
    b.onclick = () => { if (navigator.vibrate) navigator.vibrate(10); call('/api/select', {method:'POST'}, 'src=' + encodeURIComponent(x.id)); };
    nav.append(b);
  }
}
function send(cmd, params){
  if (navigator.vibrate) navigator.vibrate(10);
  // tag every command with the player on screen, so it can't hit the other one
  if (track && track.source) params = (params ? params + '&' : '') + 'src=' + encodeURIComponent(track.source);
  if (cmd === 'like') $('like').classList.toggle('on');
  if (cmd === 'skip' && track) { track.position = Math.max(0, position() + parseFloat(params.split('by=')[1])); at = Date.now(); tick(); }
  if (cmd === 'playpause' && track) { track.position = position(); at = Date.now(); track.playing = !track.playing; $('pp').setAttribute('d', track.playing ? PAUSE : PLAY); }
  call('/api/' + cmd, {method:'POST'}, params);
}

// tap or drag the progress bar to seek (SoundCloud with the userscript, VLC)
function seekPos(e){
  const r = $('bar').getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * track.duration;
}
$('prog').addEventListener('pointerdown', e => {
  if (!canSeek() || !(track.duration > 0)) return;
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
let polling = false;
const poll = async () => { if (stopped || polling) return; polling = true; await call('/api/status'); polling = false; };
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
        if cmd in ("volup", "voldown"):
            v = get_volume()
            if v is not None:
                set_volume(v + (6 if cmd == "volup" else -6))
            return self._send(200, json.dumps(status()))
        g = gather()
        if cmd == "select":  # the phone's player switcher
            src = self._query("src")
            if src not in g["avail"]:
                return self._send(409, json.dumps({"error": "That player isn't available right now"}))
            with bridge:
                focus["src"] = src
            return self._send(200, json.dumps(status(g)))
        src = self._query("src") or chosen_source(g)  # the player the phone is showing
        try:
            if src == "soundcloud" and g["tab"] and cmd in BRIDGE_CMDS:
                c = {"cmd": cmd}
                if cmd == "seek":
                    c["pos"] = float(self._query("pos"))
                elif cmd == "play":
                    c["index"] = int(self._query("index"))
                    if self._query("id"):
                        c["id"] = int(self._query("id"))
                bridge_command(g["tab"], c)
            elif src == "vlc" and cmd in VLC_CMDS:
                if not g["vlc"].get("name"):
                    return self._send(409, json.dumps({"error": "VLC isn't playing anything"}))
                arg = ""
                if cmd == "seek":
                    end = max(0, (g["vlc"].get("duration") or 0) - 1)  # seeking past the end stops VLC
                    arg = str(round(min(max(0.0, float(self._query("pos"))), end)))
                elif cmd == "skip":
                    arg = str(round(max(-600.0, min(600.0, float(self._query("by"))))))
                v = vlc(cmd, arg)  # returns VLC's new state; no need to look again
                if v.get("name"):
                    g["vlc"], g["playing"]["vlc"] = v, bool(v.get("playing"))
                    with bridge:
                        note_playing("vlc", v.get("playing"))
                    return self._send(200, json.dumps(status(g)))
            elif cmd in NX_KEYTYPE and src != "vlc":  # basic mode: media keys
                if not AXIsProcessTrusted():
                    return self._send(200, json.dumps({"error": "Mac terminal needs Accessibility permission"}))
                media_key(NX_KEYTYPE[cmd])
                time.sleep(0.4)  # let the browser update Now Playing before we report it
            elif cmd in BRIDGE_CMDS or cmd in VLC_CMDS:
                need = "the SoundCloud userscript" if cmd in BRIDGE_CMDS else "VLC"
                return self._send(409, json.dumps({"error": f"That needs {need}"}))
            else:
                return self._send(404, "unknown command", "text/plain")
        except (TypeError, ValueError):
            return self._send(400, json.dumps({"error": "bad parameters"}))
        self._send(200, json.dumps(status()))

    def _shutdown(self):
        """The phone's "Stop remote": pause whatever plays (a Mac playing audio
        won't sleep), reply, then end serve_forever() - same as Ctrl+C."""
        g = gather()
        if g["playing"]["soundcloud"]:
            bridge_command(g["tab"], {"cmd": "playpause"})
        if g["playing"]["vlc"]:
            vlc("pause")
        if g["playing"]["system"] and AXIsProcessTrusted():
            media_key(NX_KEYTYPE["playpause"])
        self._send(200, json.dumps({"stopped": True}))
        print("\nStopped from the phone.", flush=True)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # keep the terminal quiet


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer would look up this Mac's name via reverse DNS here, which can
        # hang for ~30s when offline or just after waking. We never use the name.
        socketserver.TCPServer.server_bind(self)
        host, self.server_port = self.server_address[:2]
        self.server_name = str(host)

    def handle_error(self, request, client_address):
        # A phone locking its screen, a tab closing, or the Mac sleeping mid-request
        # just drops the connection. That's normal, not worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def lan_ip() -> str:
    """This Mac's address on the home network. Ask the Wi-Fi/Ethernet ports (en0,
    en1, ...) directly: with a VPN on, the default route goes through the tunnel,
    whose address the phone can't reach."""
    for i in range(8):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", f"en{i}"],
                                capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            ip = ""
        if ip and not ip.startswith("169.254."):  # skip self-assigned addresses
            return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_link(ip: str) -> None:
    url = f"http://{ip}:{PORT}/?t={TOKEN}"
    print(f"  Open on your phone:  {url}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass


def watch_ip() -> None:
    """A laptop can wake up with a new address (other Wi-Fi, new DHCP lease);
    the phone's link then stops working, so print the new one."""
    last = lan_ip()
    while True:
        time.sleep(15)
        ip = lan_ip()
        if ip == "127.0.0.1":  # offline / asleep: wait for the network
            continue
        if ip != last:
            print(f"\nThis Mac's network address changed ({last} -> {ip}). New link:", flush=True)
            print_link(ip)
            sys.stdout.flush()
        last = ip


def main():
    if not AXIsProcessTrusted():
        print("!  Your terminal lacks Accessibility permission - media keys (basic mode) won't work.\n"
              "   System Settings > Privacy & Security > Accessibility, enable it, then restart.\n")

    try:
        server = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on port {PORT} ({e.strerror}). Is the remote already running?")

    print("SoundCloud remote running.\n")
    print_link(lan_ip())
    print(f"  Userscript for full mode (open in your browser on this Mac):\n"
          f"    http://127.0.0.1:{PORT}/sc-remote.user.js\n"
          f"  Tip: give this Mac a fixed address in your router (FRITZ!Box: Home Network >\n"
          f"  Network > this Mac > Always assign this network device the same IPv4 address)\n"
          f"  so the phone link keeps working.\n")
    print('Ctrl+C (or "Stop remote" on the phone) to stop.')
    threading.Thread(target=watch_ip, daemon=True).start()

    try:
        server.serve_forever()  # returns when the phone asks to stop
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
