// ==UserScript==
// @name         SC Remote bridge
// @namespace    sc-remote.local
// @version      1.2
// @description  Lets remote_sc.py show SoundCloud's queue on your phone and control playback.
// @match        https://soundcloud.com/*
// @grant        GM_xmlhttpRequest
// @grant        GM.xmlHttpRequest
// @connect      127.0.0.1
// @noframes
// @updateURL    http://127.0.0.1:__PORT__/sc-remote.user.js
// @downloadURL  http://127.0.0.1:__PORT__/sc-remote.user.js
// ==/UserScript==

/*
 * Two halves:
 *  - pageMain() runs inside the SoundCloud page itself (injected <script>), where it
 *    can reach SoundCloud's internal play manager. It reports state and runs commands.
 *  - The rest runs in the userscript sandbox and relays between the page and the
 *    local remote_sc.py server (GM_xmlhttpRequest isn't subject to the page's CORS).
 * The halves only exchange JSON strings via DOM events, which works the same in
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

  function report(ack) {
    let s;
    try { s = state(); } catch (e) { s = { ready: false, error: String(e) }; }
    if (ack) s.ack = ack;
    document.dispatchEvent(new CustomEvent('scremote-state', { detail: JSON.stringify(s) }));
  }

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
      const wait = () => (liked() !== before || Date.now() - t0 > 1800) ? report(c.seq) : setTimeout(wait, 100);
      wait();
    } else {
      setTimeout(() => report(c.seq), 300);  // give SoundCloud a moment to update
    }
  });
  setInterval(report, 1000);
}

(function () {
  'use strict';
  if (window.top !== window) return;  // only the main page, never ad/login iframes
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

  // Every report's reply carries any commands for this tab, and says whether
  // this is the tab the phone controls. Only that one keeps a long poll open
  // for instant response, so many SoundCloud tabs don't exhaust connections.
  let active = false, polling = false;
  document.addEventListener('scremote-state', async e => {
    const r = parse(await http('POST', '/bridge/state?tab=' + TAB, e.detail));
    active = !!(r && r.active);
    if (r) runAll(r.cmds);
    if (active && !polling) commandLoop();
  });

  async function commandLoop() {
    polling = true;
    while (active) {
      const r = await http('GET', '/bridge/commands?tab=' + TAB, null, 30000);
      if (r === null) { await sleep(3000); continue; }  // server went away
      runAll(parse(r));
    }
    polling = false;
  }

  const s = document.createElement('script');
  s.textContent = '(' + pageMain + ')();';
  (document.head || document.documentElement).appendChild(s);
  s.remove();
})();
