# SoundCloud phone remote

Control SoundCloud (and movies in VLC) on your Mac from your phone, Spotify-Connect style.
A small Python web server on the Mac serves a remote-control page; open it on
your phone (same Wi-Fi) by scanning the QR code printed in the terminal.

- Cover art, title, artist and a live progress bar
- Play/pause, next, previous, Mac volume
- With the userscript: SoundCloud's **Next up** queue (tap a track to play it),
  tap/drag to seek, and like/unlike
- **VLC**: movie title, seekable progress bar, −10 s / +10 s, play/pause, fullscreen
- **Switch players**: when SoundCloud and VLC are both open, tap SoundCloud | VLC
  at the top, e.g. to pause the movie and start music. The phone also follows
  whichever player you start on the Mac.
- **Stop remote** button: pauses whatever is playing and shuts the server down,
  so the Mac can go to sleep

## Setup

macOS only. Use Apple's `/usr/bin/python3`; reading cover art in basic mode
relies on its Apple signature.

```sh
/usr/bin/python3 -m venv ~/.sc-remote
~/.sc-remote/bin/pip install pyobjc-framework-Quartz pyobjc-framework-ApplicationServices qrcode
```

Run it (`caffeinate` keeps the Mac awake only while the remote runs):

```sh
caffeinate -i ~/.sc-remote/bin/python remote_sc.py
```

Scan the QR code with your phone. Tip: "Add to Home Screen" for an app-like
remote. The link contains an access token, saved in `~/.sc_remote_token`, so the
bookmark keeps working across restarts. Give the Mac a fixed address in your
router so the link never changes; if it does change, the terminal prints the new one.

Closing the lid still puts the Mac to sleep (`caffeinate -i` only blocks idle
sleep). The phone shows "Can't reach the Mac" meanwhile and reconnects by itself
after wake-up.

### Userscript (recommended)

1. Install [Violentmonkey](https://violentmonkey.github.io/) (Tampermonkey should also work, but is untested).
2. With the remote running, open <http://127.0.0.1:8765/sc-remote.user.js> in
   that browser and click **Install**.
3. Reload your SoundCloud tab.
4. Allow autoplay for soundcloud.com (Firefox: lock icon → Permissions →
   Autoplay → Allow Audio and Video). Otherwise the browser refuses to start
   playback from the remote until you've clicked the tab once. The phone tells
   you when this happens.

## How it works

**Full mode** (userscript installed, SoundCloud tab open): the userscript reaches
SoundCloud's internal play manager inside the page, reports the player state to
the server every second and runs commands from the phone. Only the tab being
controlled holds a long-poll connection open, so taps act within about half a second.

**Basic mode** (no userscript, or no SoundCloud tab): buttons send macOS media
keys, and "Now playing" comes from macOS's system-wide Now Playing info, the
same data Control Center shows. Media keys need Accessibility permission for
your terminal app (System Settings → Privacy & Security → Accessibility).

**VLC** is controlled over AppleScript; nothing to install. The first time,
macOS asks whether your terminal may control VLC: allow it. The remote never
launches VLC itself; it only shows up once VLC has something open.

The server switches between all of these automatically.

## Security

- Phone endpoints require the random access token from the QR code.
- The userscript endpoints accept only connections from the Mac itself carrying
  a custom header, so other devices and ordinary web pages can't use them.
- Plain HTTP on your local network. Intended for a trusted home Wi-Fi.
- With a VPN on, allow local network (LAN) connections in the VPN app, or the
  phone can't reach the Mac.

## Caveats

- The userscript depends on SoundCloud's internal (undocumented) code. A
  SoundCloud update can break it; the remote then falls back to basic mode.
- Basic mode uses macOS's private MediaRemote framework, which Apple may change.
- VLC's scripting doesn't expose its playlist, so movies get no "Up next" list.
- Headphones with wear detection (AirPods, Sennheiser Smart Pause, ...) pause
  anything that starts while they're not being worn.
