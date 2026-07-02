/* device-picker.js — the shared "target device" picker for the multi-device fleet.
 *
 * One source of truth for (a) fetching the list of WebAgent instances ("devices")
 * that share this database and (b) rendering the little floating menu used to pick
 * one. Reused by THREE surfaces so they stay consistent:
 *   • the chat composer target pill            (ui/chat-side-panel/js/chat-ui.js)
 *   • the automations "Run on" column          (ui/main-panel/automations/js/automations.js)
 *   • the sessions device badge (label lookup)  (ui/main-panel/sessions/js/sessions-page.js)
 *
 * Public API on window.DevicePicker:
 *   load(force)            -> Promise<device[]>      (cached ~8s; [{instance_id,label,online,last_seen,is_self}])
 *   labelFor(instanceId)   -> string                 ('This device' for '', else the device's label)
 *   open(anchorEl, opts)   -> opens the floating menu near anchorEl
 *   closeAll()             -> dismiss any open menu
 *
 * open() opts: { currentInstance, currentOffline, showOffline, includeThisDevice,
 *                title, force, onSelect(sel) }   where sel = {instance_id,label,is_self,offline}
 *
 * KEEP: window.DevicePicker is the cross-surface contract. Backed by GET
 * /api/v1/devices (app/api/devices.py). Styling lives in ui/shared/css/app1.css
 * under "Shared target-device picker".
 */
(function () {
  'use strict';

  const PICKER = {};
  let _cache = null;
  let _cacheAt = 0;
  const CACHE_MS = 8000;

  function _apiPath(p) { return (window.apiPath ? window.apiPath(p) : p); }
  function _auth() { try { return window.authHeaders ? window.authHeaders() : {}; } catch (_) { return {}; } }
  function _uid() { return (window.app && window.app.currentUserId) || ''; }
  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function load(force) {
    const now = Date.now();
    if (!force && _cache && (now - _cacheAt) < CACHE_MS) return _cache;
    try {
      const url = _apiPath('/api/v1/devices?user_id=' + encodeURIComponent(_uid()));
      const res = await fetch(url, { headers: { ..._auth() } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      _cache = Array.isArray(data.devices) ? data.devices : [];
      _cacheAt = now;
    } catch (e) {
      console.warn('[DevicePicker] load failed:', e);
      _cache = _cache || [];
    }
    return _cache;
  }

  function labelFor(instanceId) {
    if (!instanceId) return 'This device';
    const d = (_cache || []).find(x => x.instance_id === instanceId);
    return d ? (d.label || instanceId) : instanceId;
  }

  function _fmtSeen(iso) {
    if (!iso) return '';
    try {
      const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
      if (s < 60) return 'just now';
      if (s < 3600) return Math.floor(s / 60) + 'm ago';
      if (s < 86400) return Math.floor(s / 3600) + 'h ago';
      return Math.floor(s / 86400) + 'd ago';
    } catch (_) { return ''; }
  }

  function _onKey(e) { if (e.key === 'Escape') closeAll(); }

  function closeAll() {
    document.querySelectorAll('.device-picker-pop, .device-picker-backdrop').forEach(el => el.remove());
    document.removeEventListener('keydown', _onKey, true);
  }

  async function open(anchor, opts) {
    opts = opts || {};
    closeAll();
    const devices = await load(opts.force);

    const showOffline = !!opts.showOffline;
    let offlineSel = (opts.currentOffline === 'skip') ? 'skip' : 'wait';
    const curInstance = opts.currentInstance || '';
    const includeThis = opts.includeThisDevice !== false;

    const selfDev = devices.find(d => d.is_self);
    const others = devices.filter(d => !d.is_self);

    let rows = '';
    if (includeThis) {
      const on = !curInstance;
      rows += `<button type="button" class="device-picker-row${on ? ' selected' : ''}" data-instance="" data-self="1">
        <span class="dp-dot online"></span>
        <span class="dp-label">This device${selfDev && selfDev.label ? '<span class="dp-sub">' + _esc(selfDev.label) + '</span>' : ''}</span>
        ${on ? '<i data-lucide="check" class="dp-check"></i>' : ''}
      </button>`;
    }
    for (const d of others) {
      const on = curInstance && d.instance_id === curInstance;
      const sub = d.online ? 'online' : ('offline' + (d.last_seen ? ' · ' + _fmtSeen(d.last_seen) : ''));
      rows += `<button type="button" class="device-picker-row${on ? ' selected' : ''}" data-instance="${_esc(d.instance_id)}" data-label="${_esc(d.label || '')}">
        <span class="dp-dot ${d.online ? 'online' : 'offline'}"></span>
        <span class="dp-label">${_esc(d.label || d.instance_id)}<span class="dp-sub">${sub}</span></span>
        ${on ? '<i data-lucide="check" class="dp-check"></i>' : ''}
      </button>`;
    }
    if (!others.length) {
      rows += `<div class="device-picker-empty">${includeThis ? 'No other devices yet' : 'No other devices online'}</div>`;
    }

    let offlineHtml = '';
    if (showOffline) {
      offlineHtml = `<div class="device-picker-offline">
        <div class="dp-offline-title">If the chosen device is offline</div>
        <div class="dp-offline-seg">
          <button type="button" class="dp-off-opt${offlineSel === 'wait' ? ' on' : ''}" data-off="wait">Wait</button>
          <button type="button" class="dp-off-opt${offlineSel === 'skip' ? ' on' : ''}" data-off="skip">Skip</button>
        </div>
      </div>`;
    }

    const pop = document.createElement('div');
    pop.className = 'device-picker-pop';
    pop.innerHTML =
      `<div class="device-picker-head">${_esc(opts.title || 'Run on')}</div>
       <div class="device-picker-list">${rows}</div>${offlineHtml}`;

    const backdrop = document.createElement('div');
    backdrop.className = 'device-picker-backdrop';
    backdrop.addEventListener('click', closeAll);

    document.body.appendChild(backdrop);
    document.body.appendChild(pop);

    // Anchor the menu: prefer above, flip below if there's no room; clamp to viewport.
    const r = anchor.getBoundingClientRect();
    pop.style.visibility = 'hidden';
    requestAnimationFrame(() => {
      const pw = pop.offsetWidth, ph = pop.offsetHeight;
      let top = r.top - ph - 8;
      if (top < 8) top = r.bottom + 8;
      let left = r.left;
      if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
      if (left < 8) left = 8;
      pop.style.top = Math.round(top) + 'px';
      pop.style.left = Math.round(left) + 'px';
      pop.style.visibility = 'visible';
    });

    if (showOffline) {
      pop.querySelectorAll('.dp-off-opt').forEach(btn => {
        btn.addEventListener('click', (ev) => {
          ev.stopPropagation();
          offlineSel = btn.getAttribute('data-off') === 'skip' ? 'skip' : 'wait';
          pop.querySelectorAll('.dp-off-opt').forEach(b => b.classList.toggle('on', b === btn));
        });
      });
    }

    pop.querySelectorAll('.device-picker-row').forEach(row => {
      row.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const instance = row.getAttribute('data-instance') || '';
        const isSelf = row.getAttribute('data-self') === '1' || !instance;
        const label = instance ? (row.getAttribute('data-label') || instance) : 'This device';
        try {
          if (typeof opts.onSelect === 'function') {
            opts.onSelect({ instance_id: instance, label: label, is_self: isSelf, offline: offlineSel });
          }
        } finally {
          closeAll();
        }
      });
    });

    document.addEventListener('keydown', _onKey, true);
    if (window.lucide && window.lucide.createIcons) { try { window.lucide.createIcons(); } catch (_) {} }
  }

  PICKER.load = load;
  PICKER.labelFor = labelFor;
  PICKER.open = open;
  PICKER.closeAll = closeAll;
  window.DevicePicker = PICKER;
})();
