'use strict';

// ── PRESENTATION-MODE START ──
// This entire file exists to support a read-only demo walkthrough of the
// Admin Tools area for anonymous + non-admin visitors. Delete the file
// and grep the repo for "PRESENTATION-MODE" to find the rest of the
// touchpoints. See docs/presentation-mode.md for the removal checklist.

/**
 * Single-purpose UX gate: when active, intercepts mutating clicks/inputs
 * inside the Admin Tools and Agents tabs, disables their controls, mounts
 * a banner at the top of Admin Tools, and shows a floating tooltip near
 * the cursor on each blocked click. Server-side enforcement is unchanged.
 */

const TOOLTIP_MSG =
  "Read-only — presentation demo. Fork this app to your own repo to enable editing.";

let _enabled = false;
let _bannerEl = null;
let _tooltipEl = null;
let _tooltipTimer = null;
let _clickHandler = null;
let _focusHandler = null;

// Selectors that REMAIN interactive in presentation mode. Everything else
// inside the gated tab(s) gets blocked + tooltipped on click.
const SAFE_NAV_SELECTOR = [
  // Main top-level tab strip (lets the user move between Pages / Agents / Admin Tools)
  '#main-tabs .main-tab',
  '#main-tab-select',
  // Admin Tools sidebar view switcher
  '.files-sidebar-strip button',
  '.files-sidebar-strip [data-view]',
  // App Config section collapse/expand + card toggles
  '.ac-section-header',
  '#ac-um-access-header',
  '#ac-um-users-header',
  '.ac-card-header',
  '.ac-card-label',
  // Read-only mode pills (git mode pills, loop level pills, etc.)
  '.fg-main-mode-pill',
  '.loop-filter-btn',
  '.ac-tabs button',
  // Search inputs stay typeable
  'input[type="search"]',
  '.conn-search-input',
  '#ac-int-search',
  '#ac-settings-model-search',
  // Header user menu / theme toggle / dismiss buttons live outside but be safe
  '.user-dropdown-trigger',
  '.user-dropdown-menu *',
  '.theme-option',
  '#presentation-banner-dismiss',
  // The header chat toggle and chat resize handle
  '#chat-toggle-btn',
  '#chat-resize-handle',
].join(',');

const GATED_ROOTS = ['#tab-admin-tools', '#tab-agents'];

function _matchesSafeNav(el) {
  if (!el || el.nodeType !== 1) return false;
  return !!el.closest(SAFE_NAV_SELECTOR);
}

function _inGatedRoot(el) {
  if (!el || el.nodeType !== 1) return false;
  for (const sel of GATED_ROOTS) {
    if (el.closest(sel)) return true;
  }
  return false;
}

function _mountTooltip() {
  if (_tooltipEl) return _tooltipEl;
  _tooltipEl = document.createElement('div');
  _tooltipEl.id = 'presentation-tooltip';
  _tooltipEl.textContent = TOOLTIP_MSG;
  document.body.appendChild(_tooltipEl);
  return _tooltipEl;
}

function _showTooltip(x, y) {
  const el = _mountTooltip();
  // Position with a small offset; clamp to viewport
  const pad = 12;
  el.style.left = Math.min(x + pad, window.innerWidth - 300) + 'px';
  el.style.top = Math.min(y + pad, window.innerHeight - 80) + 'px';
  el.classList.add('visible');
  if (_tooltipTimer) clearTimeout(_tooltipTimer);
  _tooltipTimer = setTimeout(() => {
    el.classList.remove('visible');
  }, 2200);
}

function _mountBanner() {
  if (_bannerEl) return _bannerEl;
  const host = document.getElementById('tab-admin-tools');
  if (!host) return null;
  _bannerEl = document.createElement('div');
  _bannerEl.id = 'presentation-banner';
  _bannerEl.className = 'presentation-banner';
  _bannerEl.innerHTML =
    '<strong>Presentation mode</strong> — you\'re viewing Admin Tools as a ' +
    'read-only demo. Sign in as an admin (or fork this app to your own repo) ' +
    'to enable full editing.';
  host.insertBefore(_bannerEl, host.firstChild);
  return _bannerEl;
}

function _onClickCapture(e) {
  if (!_enabled) return;
  const t = e.target;
  if (!_inGatedRoot(t)) return;
  if (_matchesSafeNav(t)) return;
  // Block everything else with a tooltip
  e.preventDefault();
  e.stopPropagation();
  _showTooltip(e.clientX, e.clientY);
}

function _onFocusCapture(e) {
  if (!_enabled) return;
  const t = e.target;
  if (!_inGatedRoot(t)) return;
  // Allow focus on safe-nav controls (which includes search inputs)
  if (_matchesSafeNav(t)) return;
  const tag = t.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || t.isContentEditable) {
    t.blur();
    const r = t.getBoundingClientRect();
    _showTooltip(r.left + r.width / 2, r.top);
  }
}

/**
 * Apply the disabled flag to mutating buttons/inputs inside a freshly-mounted
 * sub-view. Safe-nav controls are skipped so navigation keeps working.
 * Sub-views call this after their first render so re-rendered controls
 * get re-disabled.
 */
export function applyPresentationGate(root) {
  if (!_enabled) return;
  const scope = root || document.getElementById('tab-admin-tools');
  if (!scope) return;
  const ctrls = scope.querySelectorAll('button, input, textarea, select');
  for (const el of ctrls) {
    if (_matchesSafeNav(el)) continue;
    // Preserve the original disabled state on a data attr so disablePresentationMode
    // can restore it cleanly.
    if (el.dataset.presOriginalDisabled === undefined) {
      el.dataset.presOriginalDisabled = el.disabled ? '1' : '0';
    }
    el.disabled = true;
  }
}

/** Called from main.js / files.js when a non-admin visitor enters a gated area. */
export function enablePresentationMode() {
  if (_enabled) return;
  _enabled = true;
  document.body.classList.add('presentation-mode');
  _mountBanner();
  _mountTooltip();
  if (!_clickHandler) {
    _clickHandler = _onClickCapture;
    document.addEventListener('click', _clickHandler, true);
  }
  if (!_focusHandler) {
    _focusHandler = _onFocusCapture;
    document.addEventListener('focusin', _focusHandler, true);
  }
  // Disable controls inside both gated tabs (idempotent)
  for (const sel of GATED_ROOTS) {
    const root = document.querySelector(sel);
    if (root) applyPresentationGate(root);
  }
  // Specifically blank/disable the agent builder chat bar
  const chatIds = [
    'agent-builder-bar-input',
    'agent-builder-bar-send',
    'agent-builder-bar-attach',
    'agent-builder-bar-voice',
  ];
  for (const id of chatIds) {
    const el = document.getElementById(id);
    if (el) {
      el.disabled = true;
      if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
        el.placeholder = 'Disabled in presentation mode';
      }
    }
  }
}

/** Inverse — used if an admin signs in mid-session. */
export function disablePresentationMode() {
  if (!_enabled) return;
  _enabled = false;
  document.body.classList.remove('presentation-mode');
  if (_clickHandler) {
    document.removeEventListener('click', _clickHandler, true);
    _clickHandler = null;
  }
  if (_focusHandler) {
    document.removeEventListener('focusin', _focusHandler, true);
    _focusHandler = null;
  }
  if (_bannerEl && _bannerEl.parentNode) _bannerEl.parentNode.removeChild(_bannerEl);
  _bannerEl = null;
  if (_tooltipEl && _tooltipEl.parentNode) _tooltipEl.parentNode.removeChild(_tooltipEl);
  _tooltipEl = null;
  // Restore previously-disabled state
  document.querySelectorAll('[data-pres-original-disabled]').forEach(el => {
    el.disabled = el.dataset.presOriginalDisabled === '1';
    delete el.dataset.presOriginalDisabled;
  });
}

/** Query whether the local UI is currently gated. */
export function isPresentationActive() { return _enabled; }
// ── PRESENTATION-MODE END ──
