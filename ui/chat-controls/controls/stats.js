'use strict';

// Chat control: stats — shows ONE stat at a time (token-bar / model-ctx / cost)
// in the stats carousel strip. Click the chevrons to cycle.
// Persists choice in localStorage, defaults to cost.
// Uses !important to stay in control when chat-activity.js toggles style.display.
//
// Targets: pill — init() called from chat-controls-config.js after pill layout.

const STATS_KEY = 'webagent_stat_view';
const STAT_IDS = ['chat-token-bar', 'chat-model-ctx', 'chat-cost'];
const _OLD_TO_ID = { 'cost': 'chat-cost', 'token-bar': 'chat-token-bar', 'model-ctx': 'chat-model-ctx' };

let _stripEl = null;          // cached strip reference so helpers don't re-query
let _activeId = 'chat-cost';  // current active stat id

function _load() {
  try {
    let v = localStorage.getItem(STATS_KEY);
    if (!v) return 'chat-cost';
    if (_OLD_TO_ID[v]) v = _OLD_TO_ID[v];
    if (STAT_IDS.includes(v)) return v;
  } catch (_) {}
  return 'chat-cost';
}

function _save(activeId) {
  try { localStorage.setItem(STATS_KEY, activeId); } catch (_) {}
}

/** Hide all stats except `activeId`. Uses !important so chat-activity.js inline
 *  toggles (cost update, ctx refresh, etc.) can't override us. */
function _showOnly(activeId) {
  if (!_stripEl) return;
  for (const id of STAT_IDS) {
    const el = _stripEl.querySelector(`#${id}`);
    if (!el) continue;
    if (id === activeId) {
      // Remove our forced override so the element gets its natural (CSS-defined)
      // display, which is typically flex. chat-activity may hide it still if
      // data is missing (cost $0, ctx unknown), which is fine.
      el.style.removeProperty('display');
    } else {
      el.style.setProperty('display', 'none', 'important');
    }
  }
}

function _nextExisting(from) {
  const idx = STAT_IDS.indexOf(from);
  if (idx === -1) return STAT_IDS[0];
  if (!_stripEl) return from;
  const n = STAT_IDS.length;
  for (let i = 1; i < n; i++) {
    const c = STAT_IDS[(idx + i) % n];
    if (_stripEl.querySelector(`#${c}`)) return c;
  }
  return from;
}

function _prevExisting(from) {
  const idx = STAT_IDS.indexOf(from);
  if (idx === -1) return STAT_IDS[0];
  if (!_stripEl) return from;
  const n = STAT_IDS.length;
  for (let i = 1; i < n; i++) {
    const c = STAT_IDS[(idx - i + n) % n];
    if (_stripEl.querySelector(`#${c}`)) return c;
  }
  return from;
}

export function init() {
  _stripEl = document.getElementById('chat-pill-stats-strip');
  if (!_stripEl) return;

  _activeId = _load();

  // Validate: fall back to first existing stat if saved one isn't in DOM
  if (!_stripEl.querySelector(`#${_activeId}`)) {
    _activeId = _nextExisting('chat-cost');
  }

  _showOnly(_activeId);

  // Wire chevrons to cycle through stats (stopPropagation so model-picker click
  // handler on #chat-pill-stats doesn't fire when clicking the buttons).
  const chevLeft = document.querySelector('.chat-stats-chev.left');
  const chevRight = document.querySelector('.chat-stats-chev.right');

  if (chevLeft) chevLeft.addEventListener('click', _onChevLeft);
  if (chevRight) chevRight.addEventListener('click', _onChevRight);

  // Store cleanup reference
  _stripEl._statsCleanup = () => {
    if (chevLeft) chevLeft.removeEventListener('click', _onChevLeft);
    if (chevRight) chevRight.removeEventListener('click', _onChevRight);
    for (const id of STAT_IDS) {
      const el = _stripEl.querySelector(`#${id}`);
      if (el) el.style.removeProperty('display');
    }
  };
}

function _onChevLeft(e) {
  e.stopPropagation();
  if (!_stripEl) return;
  _activeId = _prevExisting(_activeId);
  _showOnly(_activeId);
  _save(_activeId);
}

function _onChevRight(e) {
  e.stopPropagation();
  if (!_stripEl) return;
  _activeId = _nextExisting(_activeId);
  _showOnly(_activeId);
  _save(_activeId);
}

export function destroy() {
  if (_stripEl && typeof _stripEl._statsCleanup === 'function') {
    _stripEl._statsCleanup();
    delete _stripEl._statsCleanup;
  }
  _stripEl = null;
}

export function style() { return ''; }
