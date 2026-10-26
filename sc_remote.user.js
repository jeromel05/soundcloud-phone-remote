// ==UserScript==
// @name         SC Remote bridge
// @namespace    sc-remote.local
// @version      2.1
// @description  Lets remote_sc.py show SoundCloud and Bandcamp on your phone and control playback.
// @match        https://soundcloud.com/*
// @match        https://bandcamp.com/*
// @match        https://*.bandcamp.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      127.0.0.1
// @noframes
// @updateURL    http://127.0.0.1:__PORT__/sc-remote.user.js
// @downloadURL  http://127.0.0.1:__PORT__/sc-remote.user.js
// ==/UserScript==

/*
 * Three parts:
 *  - pageMain() runs inside the SoundCloud page itself (injected <script>), where it
 *    can reach SoundCloud's internal play manager. It reports state and runs commands.
 *  - bandcampMain() does the same on Bandcamp, where the page's own DOM (player
 *    buttons, track list, <audio>) is all it needs.
 *  - The rest runs in the userscript sandbox and relays between the page and the
 *    local remote_sc.py server (GM_xmlhttpRequest isn't subject to the page's CORS).
 * They only exchange JSON strings via DOM events, which works the same in
 * Violentmonkey, Tampermonkey and Greasemonkey, in Firefox and Chrome.
 */

function pageMain() {
  'use strict';
  let req = null, pm = null;

  // SoundCloud is a webpack app; borrow its module require to find the play manager.
  function playManager() {
    if (pm) return pm;
    const wj = window.webpackJsonp;
    if (!req && wj && wj.push) {
      const id = 'scRemote' + Math.random().toString(36).slice(2);
      try { wj.push([[id], { [id]: (m, e, r) => { req = r; } }, [[id]]]); } catch (e) { /* not ready */ }
    }
    if (!req || !req.c) return null;
    for (const m of Object.values(req.c)) {
      for (const e of [m && m.exports, m && m.exports && m.exports.default]) {
        if (e && typeof e.toggleCurrent === 'function' && typeof e.getQueue === 'function'
            && typeof e.setCurrentItem === 'function' && typeof e.getCurrentSound === 'function') {
          return (pm = e);
        }
      }
    }
    return null;
  }

  function artwork(a, size) {
    const url = a.artwork_url || (a.user && a.user.avatar_url) || '';
    return url.replace(/-(large|t\d+x\d+|crop|original)\.(jpg|png)/, '-' + size + '.$2');
  }

  function track(sound) {
    const a = sound.attributes || {};
    let artist = '';
    try { artist = sound.getDisplayArtist(); } catch (e) { /* older build */ }
    return {
      id: a.id || sound.id,
      title: a.title || '',
      artist: artist || (a.user && a.user.username) || '',
      art: artwork(a, 't120x120'),
      duration: (a.duration || 0) / 1000,
    };
  }

  function state() {
    const p = playManager();
    if (!p) return { ready: false };
    const models = (p.getQueue() || {}).models || [];
    const current = models.indexOf(p.getCurrentQueueItem());
    const start = Math.max(0, current - 3);
    const s = {
      ready: true,
      playing: p.isPlaying(),
      // "allowed" once the tab was clicked or autoplay is permitted for the site;
      // otherwise the browser refuses to start audio from our commands
      autoplay: navigator.getAutoplayPolicy ? navigator.getAutoplayPolicy('mediaelement') : null,
      current,
      queueLength: models.length,
      queue: models.slice(start, start + 60).map((it, k) => Object.assign(track(it.sound), { index: start + k })),
    };
    const sound = p.getCurrentSound();
    if (sound) {
      const like = document.querySelector('.playbackSoundBadge__like');
      Object.assign(s, track(sound), {
        art: artwork(sound.attributes || {}, 't500x500'),
        position: sound.currentTime() / 1000,
        liked: like ? like.classList.contains('sc-button-selected') : null,
      });
    }
    return s;
  }

  function run(c) {
    const p = playManager();
    if (!p) return;
    const opts = { userInitiated: true };
    if (c.cmd === 'playpause') p.toggleCurrent(opts);
    else if (c.cmd === 'next') p.playNext(opts);
    else if (c.cmd === 'prev') p.playPrev(opts);
    else if (c.cmd === 'seek') { const ms = Math.max(0, c.pos * 1000); p.seekCurrentTo(() => ms); }
    else if (c.cmd === 'play') {
      const models = p.getQueue().models;
      let item = models[c.index];
      if (!item || (c.id && item.sound.id !== c.id)) item = models.find(m => m.sound.id === c.id);  // queue shifted
      if (item) p.setCurrentItem(item, opts);
    } else if (c.cmd === 'like') {
      const b = document.querySelector('.playbackSoundBadge__like');
      if (b) b.click();
    }
  }

  let done = 0;  // last command finished, reported back as "ack"
  function report() {
    let s;
    try { s = state(); } catch (e) { s = { ready: false, error: String(e) }; }
    s.ack = done;
    document.dispatchEvent(new CustomEvent('scremote-state', { detail: JSON.stringify(s) }));
  }
  document.addEventListener('scremote-poke', report);  // the relay asks; we answer right away

  const liked = () => {
    const b = document.querySelector('.playbackSoundBadge__like');
    return b ? b.classList.contains('sc-button-selected') : null;
  };

  document.addEventListener('scremote-cmd', e => {
    let c;
    try { c = JSON.parse(e.detail); } catch (err) { return; }
    const before = liked();
    try { run(c); } catch (err) { console.warn('[sc-remote]', err); }
    if (c.cmd === 'like') {
      // the heart only flips once SoundCloud's server confirms; wait for it
      const t0 = Date.now();
      const wait = () => (liked() !== before || Date.now() - t0 > 1800) ? (done = c.seq) : setTimeout(wait, 100);
      wait();
    } else {
      done = c.seq;
    }
  });
}

function bandcampMain() {
  'use strict';
  const q = (s, root) => (root || document).querySelector(s);
  const qa = (s, root) => [...(root || document).querySelectorAll(s)];
  const text = e => (e ? e.textContent.trim().replace(/\s+/g, ' ') : '');
  const secs = t => { const p = (t || '').trim().split(':').map(Number); return p.length && !p.some(isNaN) ? p.reduce((s, x) => s * 60 + x, 0) : 0; };
  const cover = u => (u || '').replace(/_\d+\.(jpg|png)$/, '_5.$1');  // _5 = 700px
  const audio = () => qa('audio').find(a => a.currentSrc) || null;
  let tralbum = null;  // album/track data Bandcamp embeds in the page
  try { const el = q('[data-tralbum]'); if (el) tralbum = JSON.parse(el.dataset.tralbum); } catch (e) { /* none */ }
  let blocked = false;  // the browser refused to start audio we asked for

  // album/track pages have the inline player; fan/collection pages the bottom one
  const kind = () => (q('.inline_player') ? 'inline' : q('.carousel-player') ? 'carousel' : null);
  const buttons = () => (kind() === 'inline'
    ? { pp: '.inline_player .playbutton', next: '.inline_player .nextbutton', prev: '.inline_player .prevbutton' }
    : { pp: '.carousel-player .playpause', next: '.carousel-player .next', prev: '.carousel-player .prev' });
  const rows = () => qa('#track_table .track_row_view');

  // "Blu Boi, KARENINA - Vow" by "Blu Boi, KARENINA" -> "Vow"
  const tidy = (title, artist) => (artist && title.startsWith(artist + ' - ') ? title.slice(artist.length + 3) : title);

  function current() {
    const info = (tralbum && tralbum.trackinfo) || [];
    const i = rows().findIndex(r => r.classList.contains('current_track'));
    if (i >= 0) return i;
    const shown = text(q('.inline_player .title'));  // before anything played: the featured track
    const j = info.findIndex(t => t.title === shown);
    return j >= 0 ? j : (info.length === 1 ? 0 : -1);
  }

  function state() {
    const k = kind();
    if (!k) return { ready: false };
    const a = audio();
    const playing = !!a && !a.paused && !a.ended;
    if (playing) blocked = false;
    const s = { ready: true, playing, position: a ? a.currentTime : 0, autoplay: blocked ? 'disallowed' : 'allowed', queue: [], current: -1, queueLength: 0 };
    const dur = a && isFinite(a.duration) && a.duration > 0 ? a.duration : 0;
    if (k === 'inline') {
      const info = (tralbum && tralbum.trackinfo) || [];
      const albumArtist = (tralbum && tralbum.artist) || text(q('#name-section h3 span a'));
      const i = current(), t = info[i] || {};
      const artist = t.artist || albumArtist;
      Object.assign(s, {
        title: tidy(t.title || text(q('.inline_player .title')), artist),
        artist,
        album: (tralbum && tralbum.current && tralbum.current.title) || text(q('#name-section .trackTitle')),
        art: cover((q('#tralbumArt img') || {}).src || (q('meta[property="og:image"]') || {}).content),
        duration: dur || t.duration || secs(text(q('.inline_player .time_total'))),
        current: i,
        queueLength: rows().length,
        queue: rows().slice(0, 100).map((r, n) => {
          const ti = info[n] || {};
          const ar = ti.artist && ti.artist !== albumArtist ? ti.artist : '';
          return { index: n, num: n + 1, id: ti.id || null, art: '', artist: ar,
                   title: tidy(ti.title || text(q('.track-title', r)), ti.artist || ''),
                   duration: ti.duration || secs(text(q('.time', r))) };
        }),
      });
    } else {
      Object.assign(s, {
        title: (text(q('.carousel-player .info-progress .title')) || text(q('.carousel-player .now-playing .title'))).replace(/^\d+\.\s+/, ''),  // "2. Vow" -> "Vow"
        artist: text(q('.carousel-player .now-playing .artist span')),
        album: text(q('.carousel-player .now-playing .info .title')),
        art: cover((q('.carousel-player .now-playing img') || {}).src),
        duration: dur,
      });
    }
    return s;
  }

  function click(sel) { const el = q(sel); if (el) el.click(); }
  function checkStarted() {  // a paused <audio> a second after "play" = the browser blocked it
    setTimeout(() => { const a = audio(); if (!a || a.paused) blocked = true; }, 1200);
  }

  function run(c) {
    const a = audio(), b = buttons();
    if (c.cmd === 'playpause') {
      const starting = !a || a.paused, pp = q(b.pp);
      if (starting && a && pp && pp.classList.contains('busy')) {
        a.play().catch(() => { blocked = true; });  // Bandcamp's button sticks on "busy" after a refused start
      } else click(b.pp);
      if (starting) checkStarted();
    } else if (c.cmd === 'next') click(b.next);
    else if (c.cmd === 'prev') click(b.prev);
    else if (c.cmd === 'seek') {
      // just after a track change <audio> may not know its length yet; use the album data's
      const d = a && isFinite(a.duration) && a.duration > 0 ? a.duration : ((((tralbum && tralbum.trackinfo) || [])[current()]) || {}).duration;
      if (!a || !d) return;
      const go = () => { a.currentTime = Math.max(0, Math.min(c.pos, d - 1)); };
      // while a track is still loading, Bandcamp would overwrite the position; seek once it can play
      if (a.readyState >= 2) go(); else a.addEventListener('canplay', go, { once: true });
    }
    else if (c.cmd === 'play') {
      const info = (tralbum && tralbum.trackinfo) || [];
      let i = c.index;
      if (c.id && (info[i] || {}).id !== c.id) i = info.findIndex(t => t.id === c.id);  // list changed
      const row = rows()[i];
      if (!row) return;
      if (i === current()) { if (!a || a.paused) { click(b.pp); checkStarted(); } return; }  // clicking it again would pause
      const ps = q('.play_status', row);
      if (ps) { ps.click(); checkStarted(); }
    }
  }

  let done = 0;  // last command finished, reported back as "ack"
  function report() {
    let s;
    try { s = state(); } catch (e) { s = { ready: false, error: String(e) }; }
    s.ack = done;
    document.dispatchEvent(new CustomEvent('scremote-state', { detail: JSON.stringify(s) }));
  }
  document.addEventListener('scremote-poke', report);  // the relay asks; we answer right away

  document.addEventListener('scremote-cmd', e => {
    let c;
    try { c = JSON.parse(e.detail); } catch (err) { return; }
    try { run(c); } catch (err) { console.warn('[sc-remote]', err); }
    done = c.seq;
  });
}

(function () {
  'use strict';
  if (window.top !== window) return;  // only the main page, never ad/login iframes
  const SITE = /(^|\.)bandcamp\.com$/.test(location.hostname) ? 'bandcamp' : 'soundcloud';
  const SERVER = 'http://127.0.0.1:__PORT__';
  const TAB = Math.random().toString(36).slice(2, 10);
  const gmRequest = typeof GM_xmlhttpRequest === 'function' ? GM_xmlhttpRequest
    : (typeof GM !== 'undefined' && GM.xmlHttpRequest);
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  function http(method, path, body, timeout) {
    return new Promise(resolve => {
      gmRequest({
        method, url: SERVER + path, data: body, timeout: timeout || 5000,
        headers: Object.assign({ 'X-SC-Remote': '1' }, body ? { 'Content-Type': 'application/json' } : {}),
        onload: r => resolve(r.status === 200 ? r.responseText : null),
        onerror: () => resolve(null),
        ontimeout: () => resolve(null),
      });
    });
  }

  function runAll(cmds) {
    for (const c of cmds || []) document.dispatchEvent(new CustomEvent('scremote-cmd', { detail: JSON.stringify(c) }));
  }
  const parse = r => { try { return JSON.parse(r); } catch (e) { return null; } };

  // Ask the site module for its state right now (DOM events run synchronously,
  // even between the page and the userscript sandbox).
  function snapshot() {
    let st = null;
    const grab = e => { st = e.detail; };
    document.addEventListener('scremote-state', grab);
    document.dispatchEvent(new CustomEvent('scremote-poke'));
    document.removeEventListener('scremote-state', grab);
    const s = parse(st) || { ready: false };  // module not loaded yet
    s.site = SITE;
    s.doc = document.title.replace(/^[▶►︎️\s]+/, '');  // lets the server spot this tab in macOS Now Playing
    return JSON.stringify(s);
  }

  // One request at a time: send our state; the server holds the reply for up to
  // a second, or answers at once with a command from the phone; repeat. Paced by
  // network replies rather than timers, so it keeps going in background tabs
  // (whose timers Chrome slows to once a minute), and it never has two requests
  // open (Tampermonkey in Chrome runs a script's requests one after another).
  async function loop() {
    for (;;) {
      const t0 = Date.now();
      const r = parse(await http('POST', '/bridge/state?hold=1&tab=' + TAB, snapshot(), 10000));
      if (!r) { await sleep(3000); continue; }  // server not running (yet)
      if (r.cmds && r.cmds.length) {
        runAll(r.cmds);
        await sleep(350);  // let the player settle, then report back (that confirms the command)
      } else if (Date.now() - t0 < 500) {
        await sleep(1000);  // an older server answers at once; don't spin
      }
    }
  }

  if (SITE === 'bandcamp') {
    bandcampMain();  // Bandcamp's DOM is enough; no need to enter the page itself
  } else {
    const s = document.createElement('script');
    s.textContent = '(' + pageMain + ')();';
    (document.head || document.documentElement).appendChild(s);
    s.remove();
  }
  loop();
})();
