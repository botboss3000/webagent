// sendToAgent.js — the reverse direction of the "App Control" ability.
//
// App Control lets the agent push the user's screen around (set_app_view).
// This is the other way: the USER hands the agent context about what they're
// looking at. On long-press (or right-click as a desktop fallback) a small
// floating "Send to agent" button appears near the cursor. While it's up,
// the container it will capture is outlined (and any text the user highlighted
// stays selected), so the user sees exactly what context will be attached.
//
// The gesture is GATED on the current agent having ``app_control`` enabled
// (stored on window.__currentAgentAbilities). Agents without it don't trigger.
//
// Long-press respects elements that already have their own long-press gesture
// (marked via [data-has-long-press]): those are skipped so the user's existing
// interactions (rename, pin, icon pick, terminal copy/paste) keep working.
//
// Clicking the button does NOT send — it *stages* a short context note
// ([view: … | element: "…" | selected: "…"]) at the front of the chat box and
// focuses the input, so the user adds their question and sends it themselves.
//
// Purely front-end: the context rides along as ordinary message text, so no
// backend or ability change is needed. The button + highlight look live in
// ui/css/app1.css under the "Send to agent" block, using design-system tokens
// so they track dark/light.

import { app } from './state.js';

const BTN_ID = 'send-to-agent-fab';
const HILITE_CLASS = 's2a-highlight';
const HIDE_AFTER_MS = 6000;

// Long-press parameters (match ordering.js conventions)
const _LONG_PRESS_MS = 500;
const _LONG_PRESS_MOVE = 8;  // px movement cancels the pending long-press

let _activeEl = null;   // the container currently outlined
let _hideTimer = null;
let _rippleTimer = null;

// ── Ability gating ──────────────────────────────────────────────────────────

function _appControlEnabled() {
  const set = window.__currentAgentAbilities;
  return set && set.has('app_control');
}

// ── Teardown ───────────────────────────────────────────────────────────────

function _clearHighlight() {
  if (_activeEl) {
    try { _activeEl.classList.remove(HILITE_CLASS); } catch (_) { /* ignore */ }
    _activeEl = null;
  }
}

function _removeRipple() {
  if (_rippleTimer) { clearTimeout(_rippleTimer); _rippleTimer = null; }
  document.getElementById('s2a-ripple')?.remove();
}

function _removeButton() {
  if (_hideTimer) { clearTimeout(_hideTimer); _hideTimer = null; }
  const b = document.getElementById(BTN_ID);
  if (b) b.remove();
  _clearHighlight();
  _removeRipple();
  document.removeEventListener('mousedown', _onOutside, true);
  document.removeEventListener('scroll', _removeButton, true);
}

function _onOutside(ev) {
  const b = document.getElementById(BTN_ID);
  if (b && !b.contains(ev.target)) _removeButton();
}

// ── Surfaces where we never trigger ─────────────────────────────────────────
// Elements that already own a long-press (renaming, pinning, icon picking,
// terminal copy/paste) are marked with [data-has-long-press] by their own
// handler. We also skip inputs, terminals, links, and images.

function _inExcludedZone(target) {
  if (!target || !target.closest) return false;
  return !!target.closest(
    '[data-has-long-press], ' +
    'input, textarea, [contenteditable=""], [contenteditable="true"], ' +
    '.xterm, .terminal, .terminal-frame, [data-terminal], ' +
    '.file-tree, a[href], img, ' +
    '#' + BTN_ID,
  );
}

// ── Context capture ────────────────────────────────────────────────────────

function _clean(s) {
  return (s || '').replace(/\s+/g, ' ').trim();
}

// The friendly name of the active main view, read straight from the hidden
// <select id="main-tab-select"> option label (Pages, Agents, Web, Terminal…).
function _currentView() {
  const sel = document.getElementById('main-tab-select');
  if (sel && sel.selectedIndex >= 0 && sel.options[sel.selectedIndex]) {
    return _clean(sel.options[sel.selectedIndex].text);
  }
  return '';
}

const _INTERACTIVE_TAGS = ['BUTTON', 'A', 'SUMMARY', 'LABEL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'TH', 'LI', 'OPTION'];

// A short, human label for a node: prefer an explicit aria-label / title /
// alt; otherwise take the text only from interactive or leaf elements (never
// the whole body of a big container).
function _nodeLabel(node) {
  if (!node || !node.getAttribute) return '';
  const aria = node.getAttribute('aria-label');
  if (_clean(aria)) return _clean(aria);
  const title = node.getAttribute('title');
  if (_clean(title)) return _clean(title);
  if (node.tagName === 'IMG') {
    const alt = node.getAttribute('alt');
    if (_clean(alt)) return _clean(alt);
  }
  const isBtnRole = node.getAttribute('role') === 'button';
  const leaf = node.children && node.children.length === 0;
  if (_INTERACTIVE_TAGS.includes(node.tagName) || isBtnRole || leaf) {
    return _clean(node.textContent);
  }
  return '';
}

function _nodeRole(node) {
  if (!node || !node.tagName) return '';
  if (node.tagName === 'BUTTON' || node.getAttribute('role') === 'button') return 'button';
  if (node.tagName === 'A') return 'link';
  if (/^H[1-6]$/.test(node.tagName)) return 'heading';
  if (node.tagName === 'SUMMARY') return 'section';
  if (node.tagName === 'LI') return 'item';
  if (node.tagName === 'TH') return 'column';
  if (node.tagName === 'LABEL') return 'field';
  return '';
}

// Walk up a few ancestors from the click target to find the nearest element
// that has a meaningful label. Returns null when nothing readable is nearby.
function _findLabeled(el) {
  let node = el;
  let hops = 0;
  while (node && node !== document.body && hops < 6) {
    if (_nodeLabel(node)) return node;
    node = node.parentElement;
    hops++;
  }
  return null;
}

function _describeNode(node) {
  const label = node ? _nodeLabel(node) : '';
  if (!label) return '';
  const text = label.length > 60 ? label.slice(0, 57) + '…' : label;
  const role = _nodeRole(node);
  return role ? `"${text}" ${role}` : `"${text}"`;
}

// ── Staging into the chat box ──────────────────────────────────────────────

function _buildPrefix(view, element, selected) {
  const parts = [];
  if (view) parts.push('view: ' + view);
  if (element) parts.push('element: ' + element);
  if (selected) {
    const s = selected.length > 120 ? selected.slice(0, 117) + '…' : selected;
    parts.push('selected: "' + s + '"');
  }
  if (!parts.length) return '';
  return '[' + parts.join(' | ') + '] ';
}

// A one-line summary used as the button's hover tooltip.
function _summarize(view, element, selected) {
  const bits = [];
  if (view) bits.push(view);
  if (element) bits.push(element);
  if (selected) bits.push('selected text');
  return 'Send to agent — ' + bits.join(' · ');
}

function _stage(prefix) {
  if (!prefix) return;
  // Make sure the chat panel is visible (it may be hidden by App Control).
  if (typeof window.__applyChatVisible === 'function') {
    try { window.__applyChatVisible(true); } catch (_) { /* ignore */ }
  }
  const input = app.chatInput;
  if (!input) return;
  const cur = input.value || '';
  input.value = cur ? prefix + cur : prefix;
  // Reuse every existing input listener (mic→send swap, auto-resize, draft
  // save) instead of re-implementing them — just fire the input event.
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  const end = input.value.length;
  try { input.setSelectionRange(end, end); } catch (_) { /* ignore */ }
}

// ── Suppress the trailing click after a long-press fires ───────────────────
// Mirrors the pattern in ordering.js — prevents the synthesized click from
// also triggering whatever element the user was pressing.

function _suppressNextClick() {
  const handler = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    document.removeEventListener('click', handler, true);
  };
  document.addEventListener('click', handler, true);
  setTimeout(() => document.removeEventListener('click', handler, true), 350);
}

// ── The floating button ────────────────────────────────────────────────────

function _showButton(x, y, target) {
  _removeButton();

  const view = _currentView();
  // The container we'll capture + outline: the nearest labelled ancestor, or
  // the click target itself when nothing nearby is labelled.
  const container = _findLabeled(target) || target;
  const element = _describeNode(container);
  let selected = '';
  try { selected = _clean(window.getSelection().toString()); } catch (_) { /* ignore */ }
  const prefix = _buildPrefix(view, element, selected);
  if (!prefix) return;

  // Outline the container so the user sees what context is attached.
  if (container && container.classList) {
    container.classList.add(HILITE_CLASS);
    _activeEl = container;
  }

  const btn = document.createElement('button');
  btn.className = 's2a-fab';
  btn.id = BTN_ID;
  btn.type = 'button';
  btn.title = _summarize(view, element, selected);
  const i = document.createElement('i');
  i.setAttribute('data-lucide', 'send');
  btn.appendChild(i);
  const lbl = document.createElement('span');
  lbl.textContent = 'Send to agent';
  btn.appendChild(lbl);
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _stage(prefix);
    _removeButton();
  });
  document.body.appendChild(btn);

  // Position the button near the cursor. Since this fires on long-press (not
  // contextmenu), there's no native menu competing — position right below so
  // the cursor is just above the button.
  const r = btn.getBoundingClientRect();
  const w = r.width || 150;
  const h = r.height || 32;
  let top = y + 14;
  if (top + h + 8 > window.innerHeight) top = y - h - 10;
  let left = x + 10;
  left = Math.max(8, Math.min(window.innerWidth - w - 8, left));
  top = Math.max(8, Math.min(window.innerHeight - h - 8, top));
  btn.style.top = top + 'px';
  btn.style.left = left + 'px';

  if (window.lucide) {
    window.lucide.createIcons({ nodes: Array.from(btn.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }

  _hideTimer = setTimeout(_removeButton, HIDE_AFTER_MS);
  setTimeout(() => {
    document.addEventListener('mousedown', _onOutside, true);
    document.addEventListener('scroll', _removeButton, true);
  }, 0);
}

// ── Visual progress ripple ─────────────────────────────────────────────────

function _showRipple(x, y) {
  _removeRipple();
  const el = document.createElement('div');
  el.id = 's2a-ripple';
  el.className = 's2a-ripple-ring';
  el.style.left = (x - 16) + 'px';
  el.style.top = (y - 16) + 'px';
  document.body.appendChild(el);
  _rippleTimer = setTimeout(() => _removeRipple(), 600);
}

// ── Long-press detector ────────────────────────────────────────────────────
// Fires on pointerdown. If the pointer stays still (≤8px) for 500ms, and the
// target isn't excluded, and app_control is enabled for the current agent,
// the "Send to agent" button appears.

let _lpTimer = null;
let _lpTarget = null;
let _lpX = 0;
let _lpY = 0;

function _cancelLongPress() {
  if (_lpTimer) { clearTimeout(_lpTimer); _lpTimer = null; }
  _lpTarget = null;
  _removeRipple();
}

function _onLongPressStart(e) {
  // Only primary button (left click / touch / pen) — right/middle click goes
  // to the contextmenu fallback below.
  if (e.pointerType === 'mouse' && e.button !== 0) return;

  // Gate: app_control must be enabled on the current agent.
  if (!_appControlEnabled()) return;

  // Gate: a chat input must exist (we need somewhere to stage).
  if (!app.chatInput) return;

  // Gate: must not be in an excluded zone (own long-press, input, terminal…).
  if (_inExcludedZone(e.target)) return;

  _cancelLongPress();
  _lpTarget = e.target;
  _lpX = e.clientX;
  _lpY = e.clientY;
  _lpTimer = setTimeout(() => {
    _lpTimer = null;
    const target = _lpTarget;
    _lpTarget = null;
    if (!target) return;
    _removeRipple();
    _suppressNextClick();
    _showButton(_lpX, _lpY, target);
  }, _LONG_PRESS_MS);

  // Show the ripple progress after 300ms of still hold (but before full 500ms)
  _rippleTimer = setTimeout(() => {
    _rippleTimer = null;
    if (_lpTimer) _showRipple(_lpX, _lpY);
  }, 280);
}

function _onLongPressMove(e) {
  if (!_lpTimer || !_lpTarget) return;
  if (Math.abs(e.clientX - _lpX) > _LONG_PRESS_MOVE ||
      Math.abs(e.clientY - _lpY) > _LONG_PRESS_MOVE) {
    _cancelLongPress();
  }
}

function _onLongPressEnd() {
  _cancelLongPress();
}

// ── Right-click fallback (desktop) ─────────────────────────────────────────
// For users who habitually right-click, we still show the button alongside
// the native menu — but only when app_control is enabled. Long-press is the
// primary path; right-click still works as a convenience.

function _onContextMenu(e) {
  // Gate: app_control must be enabled.
  if (!_appControlEnabled()) return;
  // Gate: must have a chat input.
  if (!app.chatInput) return;
  // Gate: skip excluded zones.
  if (_inExcludedZone(e.target)) return;
  // Never suppress the native menu — just add the button alongside it.
  _showButton(e.clientX, e.clientY, e.target);
  // Cancel any in-flight long-press (the right-click may come during a hold).
  _cancelLongPress();
}

// ── Wire-up ────────────────────────────────────────────────────────────────

export function initSendToAgent() {
  // Primary: long-press (works on touch and mouse — no native menu conflict).
  document.addEventListener('pointerdown', _onLongPressStart);
  document.addEventListener('pointermove', _onLongPressMove, { passive: true });
  document.addEventListener('pointerup', _onLongPressEnd);
  document.addEventListener('pointercancel', _onLongPressEnd);

  // Fallback: right-click still shows the button alongside the native menu.
  document.addEventListener('contextmenu', _onContextMenu);
}