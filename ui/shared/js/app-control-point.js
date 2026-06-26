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
// Canvas/Agents use: app.startWebagentSession() → app.chatInput → app.chatSend
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
import { icon, claudeMark } from './icons.js';
import { ICON_PICKER_ICONS } from './icon-picker.js';
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

// ── Always available ─────────────────────────────────────────────────────────
// The point-and-share panel is an APP-LEVEL helper, not a per-chat feature, so it
// is available no matter which agent you're chatting with — deliberately NOT gated
// on the current chat agent's abilities (window.__currentAgentAbilities). Its
// message is dropped into the chat box of whatever agent is active (or sent
// outright when auto_send is on) — see _send. Kept as a
// one-line function so this is the single, obvious place to re-introduce a gate
// later if ever wanted.
function _enabled() {
  return true;
}

// ── Current page ──────────────────────────────────────────────────────────────
// Friendly name of the active main-panel tab, for the grounding message.
function _activePage() {
  let val = '';
  try {
    const active = document.querySelector('.tab-content.active');
    if (active && active.id && active.id.indexOf('tab-') === 0) val = active.id.slice(4);
  } catch (_) { /* ignore */ }
  if (!val) { try { val = localStorage.getItem('lastActiveTab') || ''; } catch (_) { /* ignore */ } }
  let label = '';
  try {
    const btn = document.querySelector('#main-tabs .main-tab[data-value="' + val + '"]');
    if (btn) label = (btn.textContent || '').replace(/\s+/g, ' ').trim();
  } catch (_) { /* ignore */ }
  const MAP = { browser: 'Browser', canvas: 'Canvas', agents: 'Agents', sessions: 'Sessions', automations: 'Automations', wiki: 'Wiki', account: 'Account', 'admin-tools': 'Admin Tools' };
  return label || MAP[val] || (val || 'app');
}

// ── Describe what was clicked ────────────────────────────────────────────────
// Walk up to the nearest meaningful element and produce a human label + a short
// descriptor (its role / kind), so the agent knows exactly what "this" is.
function _describeTarget(el) {
  const SEL = 'button, a, [role], input, select, textarea, label, summary, li, ' +
    'h1, h2, h3, h4, h5, h6, .main-tab, .agent-card, [aria-label], [title], [data-act]';
  let node = el;
  let hit = null;
  for (let i = 0; node && node !== document.body && i < 6; i++) {
    if (node.nodeType === 1 && node.matches && node.matches(SEL)) { hit = node; break; }
    node = node.parentElement;
  }
  const target = hit || (el && el.nodeType === 1 ? el : null);
  if (!target) return { label: 'the page', descriptor: 'area', el: null };

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const attr = (n) => (target.getAttribute ? target.getAttribute(n) : '') || '';
  const label =
    clean(attr('aria-label') || attr('title')) ||
    (target.tagName === 'IMG' ? clean(attr('alt')) : '') ||
    clean(attr('placeholder')) ||
    clean(target.textContent) ||
    clean(target.id) ||
    clean(attr('data-act')) ||
    target.tagName.toLowerCase();

  const tag = target.tagName.toLowerCase();
  const TAGNAME = { a: 'link', button: 'button', select: 'dropdown', textarea: 'text field', img: 'image', li: 'list item' };
  let descriptor = attr('role') || TAGNAME[tag] || tag;
  if (tag === 'input') descriptor = (attr('type') || 'text').toLowerCase() + ' input';

  return { label: label || tag, descriptor, el: target };
}

// ── Technical fingerprint ────────────────────────────────────────────────────
// Beyond the human label, capture what the agent needs to locate the exact
// element in the source and reason about its styling: a short locator path, the
// computed look (colour / size / font), a slice of the markup, and which region
// of the app it sits in. This is what lets "this colour is wrong" / "make this
// bigger" / "add a page here" land on the right thing with no extra typing.

// Short CSS-style locator back to the element. Stops at the first ancestor with
// an id (ids are unique), keeps 1–2 classes per level, disambiguates repeats
// with :nth-of-type, and caps at 4 levels so it stays readable.
function _cssPath(el) {
  if (!el || el.nodeType !== 1) return '';
  const parts = [];
  let node = el;
  for (let i = 0; node && node.nodeType === 1 && node !== document.body && i < 4; i++) {
    if (node.id) { parts.unshift('#' + node.id); break; }
    let seg = node.tagName.toLowerCase();
    const cls = (typeof node.className === 'string')
      ? node.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) seg += '.' + cls.join('.');
    const parent = node.parentElement;
    if (parent) {
      const sameTag = Array.prototype.filter.call(parent.children, (c) => c.tagName === node.tagName);
      if (sameTag.length > 1) seg += ':nth-of-type(' + (sameTag.indexOf(node) + 1) + ')';
    }
    parts.unshift(seg);
    node = node.parentElement;
  }
  return parts.join(' > ');
}

// Human-readable summary of the element's *actual* rendered style, so a
// "the colour/size is wrong" report carries the concrete before-values.
function _styleSummary(el) {
  try {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const family = (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim();
    return 'text ' + cs.color +
      ', background ' + cs.backgroundColor +
      ', font ' + cs.fontSize + '/' + cs.fontWeight + (family ? ' ' + family : '') +
      ', box ' + Math.round(r.width) + '×' + Math.round(r.height) + 'px';
  } catch (_) { return ''; }
}

// A trimmed slice of the element's own markup (whitespace collapsed, capped).
function _htmlSlice(el) {
  try {
    let h = (el.outerHTML || '').replace(/\s+/g, ' ').trim();
    if (h.length > 240) h = h.slice(0, 240) + '…';
    return h;
  } catch (_) { return ''; }
}

// Which broad region the click landed in — the cue that makes "add a page here"
// (header) vs "fix this control" (main / side panel) unambiguous for the agent.
function _regionOf(el) {
  try {
    if (!el || typeof el.closest !== 'function') return 'main area';
    const MAP = [
      ['[role="dialog"], .modal, .dialog, .popup-menu', 'dialog / popup'],
      ['#main-header', 'header / top navigation bar'],
      ['footer, [role="contentinfo"], .app-footer', 'footer'],
      ['#chat-panel', 'chat side panel'],
      ['#main-panel, main, [role="main"], .tab-content', 'main content area'],
    ];
    for (let i = 0; i < MAP.length; i++) {
      if (el.closest(MAP[i][0])) return MAP[i][1];
    }
  } catch (_) { /* ignore */ }
  return 'main area';
}

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
// in shared/ and must not import chat-side-panel internals, so it reads the same
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
  _fillAgentRow('agent-here', current || webagent, 'webAgent');
  _fillAgentRow('agent-new', webagent, 'webAgent');
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
  const d = _describeTarget(target);
  const el = d.el;
  _ctx = {
    label: d.label,
    descriptor: d.descriptor,
    x: Math.round(x),
    y: Math.round(y),
    page: _activePage(),
    region: _regionOf(el),
    selector: _cssPath(el) || '(unknown)',
    styles: _styleSummary(el) || '(unavailable)',
    html: _htmlSlice(el) || '(unavailable)',
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
      title: (webagent && webagent.name) || 'webAgent',
      iconName: 'sparkles',
      ensureAgent: app.startWebagentSession,
      initialMessage: body,
      appControl,
    });
    w.open();
  } catch (_) { /* widget could not be opened — nothing else to do */ }
}

// ── Should this spot defer to the native / built-in behaviour? ───────────────
// Bails (→ leave default) for editable fields, our own panel, and the surfaces
// that own their right-click: the Browser page (its own point-and-ask) and
// Admin Tools (file / DB menus + the embedded terminal, which lives inside
// #tab-admin-tools).
function _bailTarget(t) {
  if (!t || t.nodeType !== 1 || typeof t.closest !== 'function') return true;
  if (t.closest('input, textarea, select') || t.isContentEditable) return true;
  if (t.closest('#tab-browser, #tab-admin-tools')) return true;
  if (_panel && _panel.contains(t)) return true;
  return false;
}

// ── Boot ──────────────────────────────────────────────────────────────────────
export function initAppControlPoint() {
  // CAPTURE phase: run before any element-level contextmenu handler, so nothing
  // can stopPropagation past us and leave the native menu showing. We only
  // preventDefault on the spots we actually handle.
  document.addEventListener('contextmenu', (e) => {
    if (!_enabled()) return;                  // ability off → native menu
    const t = e.target;
    if (_bailTarget(t)) return;
    e.preventDefault();
    _open(e.clientX, e.clientY, t, false);
  }, true);
}
