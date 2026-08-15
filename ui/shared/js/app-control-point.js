'use strict';

// App-control point-and-share — right-click anywhere in the app to point at a UI
// element and hand it to the agent chat. Opens a small floating panel at the
// cursor: two destination rows — "Send to current chat" (the agent you're chatting
// with) and "Send to new chat widget" (a fresh WebAgent session), each showing the
// target agent's icon + name — above a multi-line message box built from the shared
// .chat-pill .chat-pill-1line skin (ui/shared/css/app1.css). On send it drops a
// grounded message into the chat naming the clicked element, its position, and the
// current page.
//
// This is the USER-facing half of the App Control ability; the AGENT-facing half
// is set_app_view (plugins/abilities/Core/app_control/). The panel is ALWAYS
// available — an app-level helper, NOT gated on the current chat agent.
//
// On send, ONLY the user's typed words go into the chat pill (a clean, editable
// message). The technical fingerprint (clicked element label/role, page + region,
// CSS locator, computed style, markup, cursor x/y) is staged on
// `app.pendingAppControl`; the next send carries it as `app_control` (see
// chat-send.js), and the backend (app/api/chat.py _maybe_emit_app_control) turns it
// into its own foldable `app_control` tool chip AND folds it to the agent for that
// turn — so the chat bubble stays just the words while the agent still gets exactly
// what was clicked. By default it drops the words into the box for the user to
// review and press Send; flipping app-prompts.json's
// ui_handoffs.app_control_point.auto_send makes it send immediately to whatever
// agent is active (default "WebAgent" as fallback). The handoff reuses the path
// Gen UI/Agents use: app.startWebagentSession() → app.chatInput → app.chatSend
// (ui/shared/js/state.js). The fingerprint/chip wording lives in
// app/defaults/app-prompts.json (ui_handoffs.app_control_point.template), applied
// server-side.
//
// Wired once at boot from ui/shared/js/main.js (initAppControlPoint). Styles:
// ui/shared/css/app-control-point.css.
//
// REMOVE-WHEN: the App Control ability is dropped from the ability catalog.

import { app } from './state.js';
import { apiPath } from './config.js';
import { icon, claudeMark, codexMark } from './icons.js';
import { ICON_PICKER_ICONS } from './icon-picker.js';
import { describeTarget, cssPath, styleSummary, htmlSlice, regionOf, activePage } from './element-fingerprint.js';
import { createChatWidget } from '../../chat-widget/js/chat-widget.js';

// Default message body when the user types nothing into the optional box.
const DEFAULT_BODY = 'What is this?';

// Whether sending actually fires the message or just drops it into the chat box
// for the user to review and send. Backend-overridable via app-prompts.json
// (ui_handoffs.app_control_point.auto_send); this is only the fallback default.
const DEFAULT_AUTO_SEND = false;

let _panel = null;                  // floating panel element (built once, reused)
let _inputEl = null;                // the chat-pill textarea
let _chipEl = null;                 // detected-target chip
let _ctx = null;                    // { label, descriptor, x, y, page } for the open
let _cfgPromise = null;             // cached fetch of the handoff config (template + auto_send)

// ── App-wide on/off gate ─────────────────────────────────────────────────────
// The point-and-share panel is an APP-LEVEL helper, not a per-chat feature, so it
// is NOT gated on the current chat agent's abilities (window.__currentAgentAbilities).
// It IS gated on one app-wide admin switch: App Settings → App Functions → "App
// control quick message" (app_control_quick_message in app-settings.json), served
// to every visitor via the public /api/v1/auth/ui-config and read once at boot
// (see initAppControlPoint). Defaults ON — the panel has always been available, so
// a missing/unreachable config keeps it on (fail-open). When on, its message drops
// into the chat box of whatever agent is active (or sends outright when auto_send
// is on) — see _send.
let _enabledFlag = true;

function _enabled() {
  return _enabledFlag;
}

// Fetch the app-wide on/off flag once from the public ui-config. Sends the auth
// token when present so it behaves identically on authenticated pages (mirrors
// appearance.js). Best-effort: any failure leaves the flag at its ON default.
function _loadEnabledFlag() {
  let headers = {};
  try {
    const t = localStorage.getItem('auth_token');
    if (t) headers = { Authorization: 'Bearer ' + t };
  } catch (_) { /* ignore */ }
  fetch(apiPath('/api/v1/auth/ui-config'), { headers })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (cfg && typeof cfg.app_control_quick_message === 'boolean') {
        _enabledFlag = cfg.app_control_quick_message;
      }
    })
    .catch(() => { /* keep the ON default */ });
}

// (element fingerprint helpers moved to shared/element-fingerprint.js)

// Fetch the editable handoff config once (cached): the auto_send flag (whether
// sending fires the message or just drops the words into the chat box for the user
// to review/edit and send). Lives in app-prompts.json
// (ui_handoffs.app_control_point.auto_send) — backend-adjustable, no UI. Falls back
// to the built-in default if the config is unreachable or omits it. (The fingerprint
// chip wording — .template — is applied server-side, so it isn't read here.)
function _loadConfig() {
  if (_cfgPromise) return _cfgPromise;
  _cfgPromise = (async () => {
    let autoSend = DEFAULT_AUTO_SEND;
    try {
      const res = await fetch(apiPath('/api/v1/app-prompts'));
      if (res.ok) {
        const data = await res.json();
        const h = data && data.ui_handoffs && data.ui_handoffs.app_control_point;
        if (h && typeof h.auto_send === 'boolean') autoSend = h.auto_send;
      }
    } catch (_) { /* unreachable config — use default */ }
    return { autoSend };
  })();
  return _cfgPromise;
}

// ── Destination agents ──────────────────────────────────────────────────────
// The two rows each name the agent the message would land on. App-control lives
// in shared/ and must not import chat internals, so it reads the same
// agent list the chat selector populates (window.__agentsSharedData).
function _agents() {
  try {
    const d = window.__agentsSharedData;
    if (d && Array.isArray(d.agents)) return d.agents;
  } catch (_) { /* ignore */ }
  return [];
}

// Icon HTML for an agent record — Lucide glyph, the Claude spark for claude_code
// engines, or an emoji/text fallback. Mirrors the chat panel's _agentIconHtml so
// the rows match what the selector shows.
function _agentIcon(a, size) {
  const px = size || '14px';
  const n = parseFloat(px) || 14;
  const name = (a && a.icon) || '';
  const engine = (a && a.engine) || '';
  if (engine === 'claude_code' && (!name || name === 'sparkles')) return claudeMark({ size: px });
  if (engine === 'codex' && (!name || name === 'code-2')) return codexMark({ size: px });
  if (!name) return icon('bot', { size: px });
  if (ICON_PICKER_ICONS.includes(name)) return icon(name, { size: px });
  return '<span style="font-size:' + n + 'px;line-height:1">' + name.replace(/</g, '&lt;') + '</span>';
}

// Fill one row's "agent icon + name" line for the given agent record.
function _fillAgentRow(role, a, fallbackName) {
  if (!_panel) return;
  const wrap = _panel.querySelector('[data-role="' + role + '"]');
  if (!wrap) return;
  const iconEl = wrap.querySelector('.ac-point-row-agent-icon');
  const nameEl = wrap.querySelector('.ac-point-row-agent-name');
  if (iconEl) iconEl.innerHTML = _agentIcon(a, '14px');
  if (nameEl) nameEl.textContent = (a && a.name) || fallbackName;
}

// Point the two destination rows at their target agents: row 1 → the agent the
// user is currently chatting with; row 2 → the default WebAgent (template_id
// 'default'), the agent a fresh "new chat widget" session spins up.
function _populateAgentRows() {
  const list = _agents();
  const current = list.find((a) => a.id === app.currentAgentId) || null;
  const webagent = list.find((a) => a.template_id === 'default') || null;
  _fillAgentRow('agent-here', current || webagent, 'WebAgent');
  _fillAgentRow('agent-new', webagent, 'WebAgent');
}

// ── Panel ─────────────────────────────────────────────────────────────────────
function _buildPanel() {
  if (_panel) return _panel;
  const p = document.createElement('div');
  p.id = 'ac-point-panel';
  p.className = 'ac-point-panel';
  p.setAttribute('role', 'dialog');
  p.setAttribute('aria-label', 'Point and ask the agent');

  // Two destination rows — each is a one-click send. The trailing icon + name on
  // each is filled per-open by _populateAgentRows (which agent the message lands
  // on). data-act drives the handler below.
  const row = (act, lead, title, role) => (
    '<button type="button" class="ac-point-row" data-act="' + act + '">' +
    '<span class="ac-point-row-lead" aria-hidden="true">' + icon(lead, { size: '16px' }) + '</span>' +
    '<span class="ac-point-row-body">' +
    '<span class="ac-point-row-title">' + title + '</span>' +
    '<span class="ac-point-row-agent" data-role="' + role + '">' +
    '<span class="ac-point-row-agent-icon" aria-hidden="true"></span>' +
    '<span class="ac-point-row-agent-name"></span>' +
    '</span>' +
    '</span>' +
    '<span class="ac-point-row-go" aria-hidden="true">' + icon('arrow-right', { size: '15px' }) + '</span>' +
    '</button>'
  );

  p.innerHTML =
    '<div class="ac-point-head">' +
    '<span class="ac-point-head-icon" aria-hidden="true">' + icon('mouse-pointer-click', { size: '15px' }) + '</span>' +
    '<span class="ac-point-head-title">Tell the agent about this</span>' +
    '</div>' +
    '<div class="ac-point-chip" id="ac-point-chip"></div>' +
    '<div class="ac-point-rows">' +
    row('send-here', 'message-square', 'Send to current chat', 'agent-here') +
    row('send-new', 'sparkles', 'Send to new chat widget', 'agent-new') +
    '</div>' +
    '<div class="ac-point-pill chat-pill chat-pill-1line no-voice">' +
    '<textarea class="chat-pill-input ac-point-input" rows="4" placeholder="Add a message (optional)…" autocomplete="off"></textarea>' +
    '<button type="button" class="chat-pill-send ac-point-send" title="Send to current chat">' + icon('send', { size: '20px' }) + '</button>' +
    '</div>';

  document.body.appendChild(p);
  _panel = p;
  _chipEl = p.querySelector('#ac-point-chip');
  _inputEl = p.querySelector('.ac-point-input');

  // Destination rows: "Send to current chat" → the active chat; "Send to new chat
  // widget" → a fresh WebAgent session. Both carry the optional typed message.
  p.querySelectorAll('.ac-point-row').forEach((rowEl) => {
    rowEl.addEventListener('click', () => {
      const act = rowEl.dataset.act;
      if (act === 'send-new') _sendNew(_inputEl ? _inputEl.value : '');
      else _sendHere(_inputEl ? _inputEl.value : '');
    });
  });

  // Message box: Enter sends to the current chat (Shift+Enter newlines), Escape closes.
  _inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _sendHere(_inputEl.value); }
    else if (e.key === 'Escape') { e.preventDefault(); _close(); }
  });
  _inputEl.addEventListener('input', () => {
    const pill = p.querySelector('.ac-point-pill');
    if (pill) pill.classList.toggle('has-text', !!_inputEl.value.trim());
  });
  p.querySelector('.ac-point-send').addEventListener('click', () => _sendHere(_inputEl ? _inputEl.value : ''));

  return p;
}

function _open(x, y, target, viaTouch) {
  _buildPanel();
  const d = describeTarget(target);
  const el = d.el;
  _ctx = {
    label: d.label,
    descriptor: d.descriptor,
    x: Math.round(x),
    y: Math.round(y),
    page: activePage(),
    region: regionOf(el),
    selector: cssPath(el) || '(unknown)',
    styles: styleSummary(el) || '(unavailable)',
    html: htmlSlice(el) || '(unavailable)',
  };
  if (_chipEl) _chipEl.textContent = '“' + _ctx.label + '” · ' + _ctx.region;
  _populateAgentRows();
  if (_inputEl) {
    _inputEl.value = '';
    const pill = _panel.querySelector('.ac-point-pill');
    if (pill) pill.classList.remove('has-text');
  }

  _panel.classList.add('open');
  // Position after layout so width/height are measurable; clamp to the viewport.
  _panel.style.left = '0px';
  _panel.style.top = '0px';
  requestAnimationFrame(() => {
    if (!_panel) return;
    const r = _panel.getBoundingClientRect();
    const pad = 8;
    let left = x;
    let top = y;
    if (left + r.width + pad > window.innerWidth) left = Math.max(pad, window.innerWidth - r.width - pad);
    if (top + r.height + pad > window.innerHeight) top = Math.max(pad, window.innerHeight - r.height - pad);
    _panel.style.left = left + 'px';
    _panel.style.top = top + 'px';
  });

  // Auto-focus the message box on desktop; skip on touch so the on-screen
  // keyboard doesn't immediately cover the destination rows.
  if (_inputEl && !viaTouch) setTimeout(() => { try { _inputEl.focus(); } catch (_) { /* ignore */ } }, 0);
  // pointerdown (not mousedown) so an outside TAP closes it on touch too.
  document.addEventListener('pointerdown', _onOutside, true);
  document.addEventListener('keydown', _onKey, true);
}

function _onOutside(e) {
  if (_panel && !_panel.contains(e.target)) _close();
}

function _onKey(e) {
  if (e.key === 'Escape') _close();
}

function _close() {
  if (_panel) _panel.classList.remove('open');
  document.removeEventListener('pointerdown', _onOutside, true);
  document.removeEventListener('keydown', _onKey, true);
  _ctx = null;
}

// ── "Send to current chat" — stage the fingerprint + words for the CURRENT chat.
// Only the user's words go into the chat pill (a clean, editable message); the
// technical fingerprint is staged on `app.pendingAppControl` so the next send
// carries it alongside as `app_control` (see chat-send.js) — the backend turns it
// into a foldable `app_control` tool chip and folds it to the agent for that turn
// (app/api/chat.py _maybe_emit_app_control). Honours the auto_send config: OFF
// (default) drops the words into the box for the user to review/edit and press
// Send; ON spins up the default WebAgent if no chat is open yet, then sends.
async function _sendHere(customText) {
  const ctx = _ctx;                 // snapshot — an outside-click during the
  if (!ctx) { _close(); return; }   // config await must not null it out
  const body = (customText && customText.trim()) || DEFAULT_BODY;
  const cfg = await _loadConfig();
  app.pendingAppControl = {
    intent: 'Point and ask',
    label: ctx.label,
    descriptor: ctx.descriptor,
    region: ctx.region,
    page: ctx.page,
    selector: ctx.selector,
    styles: ctx.styles,
    html: ctx.html,
    x: ctx.x,
    y: ctx.y,
    text: body,
  };
  _close();
  if (cfg.autoSend) {
    try {
      if (!app.currentAgentId && typeof app.startWebagentSession === 'function') {
        await app.startWebagentSession();
      }
    } catch (_) { /* fall through — send into whatever's there */ }
  }
  // Reveal the chat panel (mobile hides it; desktop is usually already showing).
  try { if (typeof window.__applyChatVisible === 'function') window.__applyChatVisible(true); } catch (_) { /* ignore */ }
  if (app.chatInput) {
    app.chatInput.value = body;
    try { app.chatInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) { /* ignore */ }
    if (cfg.autoSend) {
      if (app.chatSend) app.chatSend.click();
    } else {
      // Review-first: focus the box and put the cursor at the end.
      try {
        app.chatInput.focus();
        const n = app.chatInput.value.length;
        if (typeof app.chatInput.setSelectionRange === 'function') app.chatInput.setSelectionRange(n, n);
      } catch (_) { /* ignore */ }
    }
  }
}

// ── "Send to new chat widget" — open a fresh FLOATING chat-widget (NOT the side
// panel), seeded with the message + the fingerprint, and send immediately
// (choosing a new chat is itself the decision to send). The widget talks to the
// default WebAgent the manager (the 'agent-new' row's agent); its session is
// resolved by app.startWebagentSession via the widget's ensureAgent hook. The
// fingerprint rides the widget's FIRST message via the `appControl` option, so the
// backend renders the same foldable app_control tool chip and folds it to the agent
// for that turn (app/api/chat.py _maybe_emit_app_control) — see
// ui/chat-widget/js/chat-widget.js.
function _sendNew(customText) {
  const ctx = _ctx;
  if (!ctx) { _close(); return; }
  const body = (customText && customText.trim()) || DEFAULT_BODY;
  const appControl = {
    intent: 'Point and ask',
    label: ctx.label,
    descriptor: ctx.descriptor,
    region: ctx.region,
    page: ctx.page,
    selector: ctx.selector,
    styles: ctx.styles,
    html: ctx.html,
    x: ctx.x,
    y: ctx.y,
    text: body,
  };
  // Title/icon for the widget header — the default WebAgent record (matches the
  // 'agent-new' row); the session itself is resolved by startWebagentSession.
  const webagent = _agents().find((a) => a.template_id === 'default') || null;
  _close();
  try {
    const w = createChatWidget({
      title: (webagent && webagent.name) || 'WebAgent',
      iconName: 'sparkles',
      ensureAgent: app.startWebagentSession,
      initialMessage: body,
      appControl,
    });
    w.open();
  } catch (_) { /* widget could not be opened — nothing else to do */ }
}

// ── Should this spot defer to the native / built-in behaviour? ───────────────
// Bails (→ leave default) for editable fields, our own panel, and the few
// surfaces that genuinely OWN their right-click:
//   • the Browser page — its own point-and-ask drives the remote browser;
//   • the Admin Tools FILE TREE (#files-tree) — per-row rename/delete/… menu;
//   • any embedded terminal (.files-terminal-pane / .xterm) — native (desktop)
//     or long-press (touch) copy / paste.
// NOTE: this used to bail on the WHOLE #tab-admin-tools tab, which wrongly killed
// point-and-share across every Admin Tools sub-page (App Settings, Cloud VMs,
// Deploy, DB viewer, Source Control). Those are ordinary main panels with no
// right-click menu of their own, so they now get the panel like everywhere else —
// only the file tree and terminals (which DO own right-click) still defer.
function _bailTarget(t) {
  if (!t || t.nodeType !== 1 || typeof t.closest !== 'function') return true;
  if (t.closest('input, textarea, select') || t.isContentEditable) return true;
  if (t.closest('[data-has-long-press], #tab-browser, #files-tree, .files-terminal-pane, .xterm')) return true;
  if (_panel && _panel.contains(t)) return true;
  return false;
}

// ── Is the press landing on real, selectable text? ──────────────────────────
// When a long-press (touch) or right-click lands directly on prose the user is
// trying to read/copy, we defer to the browser: let the word get highlighted and
// the native long-press / right-click callout menu appear instead of popping our
// panel. Deliberately narrow — only true when the point resolves to a non-blank
// character in a user-selectable text node, so buttons, icons, and chrome that
// merely CONTAIN a label still open the panel as before.
function _overSelectableText(x, y) {
  // An existing, non-collapsed selection means the user is already working with
  // highlighted text — always defer to the native menu.
  try {
    const sel = window.getSelection && window.getSelection();
    if (sel && !sel.isCollapsed && String(sel).trim()) return true;
  } catch (_) { /* ignore */ }

  // Resolve the exact text node + offset under the pointer.
  let node = null, offset = -1;
  try {
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(x, y);
      if (r) { node = r.startContainer; offset = r.startOffset; }
    } else if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      if (p) { node = p.offsetNode; offset = p.offset; }
    }
  } catch (_) { /* ignore */ }

  if (!node || node.nodeType !== 3) return false;   // not a text node → no text here
  const data = node.data || '';
  // Require a non-whitespace character adjacent to the caret so the point is
  // genuinely ON a word, not snapped to the edge of a whitespace-only run.
  if (!((data[offset] || '') + (data[offset - 1] || '')).trim()) return false;

  // Respect user-select:none (buttons/icons/chrome that only LOOK like text).
  const host = node.parentElement;
  if (host) {
    try {
      const cs = getComputedStyle(host);
      if ((cs.userSelect || cs.webkitUserSelect) === 'none') return false;
    } catch (_) { /* ignore */ }
  }
  return true;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
export function initAppControlPoint() {
  // Read the app-wide on/off switch (App Settings → App Functions → "App control
  // quick message") once at boot. A contextmenu/long-press fires well after this
  // resolves; until it does the flag stays at its ON default, so nothing is lost.
  _loadEnabledFlag();
  // CAPTURE phase: run before any element-level contextmenu handler, so nothing
  // can stopPropagation past us and leave the native menu showing. We only
  // preventDefault on the spots we actually handle.
  document.addEventListener('contextmenu', (e) => {
    if (!_enabled()) return;                  // ability off → native menu
    const t = e.target;
    if (_bailTarget(t)) return;
    // Long-press / right-click directly on selectable text → let the browser
    // highlight it and show its native callout menu instead of our panel.
    if (_overSelectableText(e.clientX, e.clientY)) return;
    e.preventDefault();
    _open(e.clientX, e.clientY, t, false);
  }, true);
}
