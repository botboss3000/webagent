'use strict';

// Gen UI toolbar — the Gen UI tab's footer control row.
//
// DROP-IN / SWAPPABLE: this file owns the ENTIRE footer — its markup, look, and
// behaviour — so a user can redesign the toolbar without touching the base
// genui rendering code in genui.js. The two talk only through a small `ctx`
// bridge passed to initGenuiToolbar() and a `syncPages()` handle the core calls
// whenever page state changes. To restyle: edit this file + genui-toolbar.css.
// To reshape the row: edit _buildToolbar() below (it builds the footer DOM into
// the empty #genui-footer mount in genui.html).
//
// What's here: a COMPACT chat pill with a left-side (+) attach button, then a
// CHEVRON-ONLY genui selector (the current genui is shown highlighted INSIDE
// the popup, no longer spelled out on the trigger) plus the New Gen UI (+) and
// Refresh Gen UI buttons. Submitting the pill (Enter / send) opens a floating
// chat-widget (ui/chat-widget) seeded with that first message AND any attached
// files, talking to the agent that MAINTAINS this page — WebAgent under the
// page's agent_context, via ctx.buildTaggedPrompt — NOT the manager-role
// WebAgent the Agents-page pill talks to. The pill replaces the old ✦ star.
//
// The pill placeholder names the maintaining agent ("Chat with <agent> about
// this…"); the name is resolved live from the user's WebAgent agent (see
// _resolveAgentName / _setAgentPlaceholder), so a renamed agent shows through.
// Attachments use an ISOLATED pending list + preview bar (never the global
// composer's), mirroring the ability-search pill — see _bindChatPill.
//
// The ctx the core hands us:
//   getPages()                  -> the user's page list
//   getCurrentPage()            -> the displayed page (or null)
//   selectPage(slug)            -> switch to + render a page
//   newPage()                   -> open the new-page dialog
//   refresh()                   -> reload ONLY the current genui
//   renamePage(slug, title)     -> Promise<bool>  (core does the network + state)
//   deletePage(slug)            -> Promise        (core does the network + reload)
//   buildTaggedPrompt(text)     -> Promise<string> (genui handoff tag for chat)
//   updateStatus(text, type)    -> status line

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { createChatWidget } from '../../../chat-widget/js/chat-widget.js';
import { startSpeechDictation, uploadAndPreview, wireChatPillUploads } from '../../../shared/js/attachments.js';
import { advanceDeleteBtn } from '../../../shared/js/delete-control.js';

// ── Module state ───────────────────────────────────────────────────────────────

// The bridge to the genui core (set by initGenuiToolbar).
let _ctx = null;

// One floating chat-widget at a time (opened by the chat pill). See _openChat.
let _genuiChat = null;
// Which agent the open widget is talking to — so switching to a genui owned by
// a DIFFERENT agent re-opens the widget against the right agent (no leakage).
let _genuiChatAgentId = null;

// Isolated attachment pending list for the pill's (+) button — NEVER the global
// composer's pendingAttachments. Files ride the chat-widget's first message.
const _pendingAtts = [];

// Cached map of the user's agents { id: {name, template_id, …} }, used to turn a
// genui's owning agent_id into a display name + to fall back to the default
// WebAgent. Resolved once via _loadAgents (see _resolveGenuiAgentName/Id).
let _agentsById = null;

// Tiny lucide-style icon helper for menu rows. Lucide rewrites <i> tags at boot,
// but rows are created after boot — we inline the SVGs so they render without
// needing another rewrite pass.
const _ICON_SVGS = {
  'more-vertical': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>',
  'pencil': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/></svg>',
  'trash-2': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>',
};
function _icon(name) { return _ICON_SVGS[name] || ''; }

// ── Public API ──────────────────────────────────────────────────────────────

// Build + wire the toolbar. Returns a handle the core uses to keep the page
// selector in sync; safe to call once (idempotent guard below).
export function initGenuiToolbar(ctx) {
  _ctx = ctx;
  const footer = document.getElementById('genui-footer');
  if (!footer) return { syncPages() {} };

  _buildToolbar(footer);

  const newBtn     = document.getElementById('genui-new-page-btn');
  const refreshBtn = document.getElementById('genui-refresh-btn');
  if (newBtn)     newBtn.addEventListener('click', () => _ctx.newPage());
  if (refreshBtn) refreshBtn.addEventListener('click', () => _ctx.refresh());

  _bindChatPill();
  _bindPageDropdown();
  _setAgentPlaceholder();   // name the maintaining agent in the pill placeholder

  return { syncPages };
}

// Re-render the page selector from the core's current state. The core calls this
// after loading pages, switching pages, renaming/deleting, or a render_visual /
// create_genui event. Always marks the dropdown "loaded" so it isn't left
// click-dead on an empty/failed load.
function syncPages() {
  _renderPageRows();
  _markPageDropdownLoaded();
  _setAgentPlaceholder();   // refresh once identity is known (no-op once cached)
}

// ── Toolbar markup (the design surface — restyle/reshape freely) ─────────────

function _buildToolbar(footer) {
  footer.innerHTML =
    '<div id="genui-page-nav">' +
      '<div id="genui-page-label">' +
        // The pill column: an isolated attachment preview bar sits ABOVE the
        // compact pill (the footer hugs the bottom, so previews grow upward),
        // with a hidden file input the (+) attach button drives.
        '<div class="genui-pill-col">' +
          '<div id="genui-chat-preview" class="genui-chat-preview" style="display:none;"></div>' +
          // The chat pill — opts into the shared single-line pill (.chat-pill
          // .chat-pill-1line, app1.css) + the floating-glass skin (#genui-prompt-row,
          // index.css); compact-height trim lives in genui-toolbar.css. The left
          // (+) attaches files (like the browser pill); submitting opens the page's
          // chat-widget seeded with the text + files (see _bindChatPill / _openChat).
          '<div id="genui-prompt-row" class="chat-pill chat-pill-1line">' +
            '<button id="genui-chat-attach" type="button" class="chat-pill-attach" title="Attach files"><i data-lucide="plus"></i></button>' +
            '<textarea id="genui-chat-input" class="chat-pill-input" rows="1" autocomplete="off" placeholder="Chat about this page…"></textarea>' +
            '<button id="genui-chat-voice" type="button" class="chat-pill-voice" title="Voice dictation"><i data-lucide="mic"></i></button>' +
            '<button id="genui-chat-send" type="button" class="chat-pill-send" title="Send message"><i data-lucide="send"></i></button>' +
          '</div>' +
          '<input id="genui-chat-file" type="file" multiple style="display:none;">' +
        '</div>' +
        // Page controls grouped so they stay on ONE sub-row when the toolbar
        // stacks (single row → pill on top, this cluster below) on a narrow panel.
        // The selector is now CHEVRON-ONLY — the current genui shows highlighted
        // inside the popup, not spelled out on the trigger.
        '<div class="genui-page-controls">' +
          '<div id="genui-page-dropdown" class="session-dropdown">' +
            '<button id="genui-page-dropdown-trigger" type="button" class="session-dropdown-trigger" title="Switch genui">' +
              '<i data-lucide="chevron-down"></i>' +
            '</button>' +
            '<div id="genui-page-dropdown-menu" class="session-dropdown-menu" hidden></div>' +
          '</div>' +
          '<button id="genui-new-page-btn" title="New Gen UI" class="header-plus-btn"><i data-lucide="plus"></i></button>' +
          '<button id="genui-refresh-btn" title="Refresh Gen UI" class="header-plus-btn"><i data-lucide="refresh-cw"></i></button>' +
        '</div>' +
      '</div>' +
    '</div>';
}

// ── Chat pill → talk to this page's agent ────────────────────────────────────

// Bind the compact chat pill: type → arm the send glyph (shared .has-text swap),
// Enter / send-click → open the chat-widget seeded with that first message. Voice
// dictates into this input; force send-only when speech isn't available.
function _bindChatPill() {
  const pill     = document.getElementById('genui-prompt-row');
  const input    = document.getElementById('genui-chat-input');
  const sendBtn  = document.getElementById('genui-chat-send');
  const voiceBtn = document.getElementById('genui-chat-voice');
  const attachBtn = document.getElementById('genui-chat-attach');
  const fileInput = document.getElementById('genui-chat-file');
  const previewBar = document.getElementById('genui-chat-preview');
  if (!pill || !input) return;

  input.addEventListener('input', () => _updateArmed());   // swaps mic glyph → send
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _submitChatPill(); }
  });
  if (sendBtn) sendBtn.addEventListener('click', () => _submitChatPill());

  // ATTACH (+) → isolated upload (own picker + drag/paste), routed to THIS pill's
  // pending list so the files ride the chat-widget's first message — never the
  // global composer. Mirrors buildAbilitySearchPill in dom-utils.js.
  const uploadOpts = {
    previewBar,
    pending: _pendingAtts,
    onChange: () => {
      if (previewBar) previewBar.style.display = _pendingAtts.length ? 'flex' : 'none';
      _updateArmed();
    },
  };
  if (attachBtn && fileInput) {
    attachBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
      const files = Array.from(fileInput.files);
      fileInput.value = '';
      for (const file of files) await uploadAndPreview(file, uploadOpts);
    });
  }
  wireChatPillUploads(pill, input, uploadOpts);

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR && voiceBtn) voiceBtn.addEventListener('click', () => startSpeechDictation(voiceBtn, input));
  else pill.classList.add('no-voice');
}

// Arm the send glyph (mic → send) when there's text OR a pending attachment.
function _updateArmed() {
  const pill  = document.getElementById('genui-prompt-row');
  const input = document.getElementById('genui-chat-input');
  if (!pill || !input) return;
  pill.classList.toggle('has-text', !!input.value.trim() || _pendingAtts.length > 0);
}

// Drop the pill's pending attachments + clear the preview bar (after a send).
function _clearGenuiAtts() {
  _pendingAtts.length = 0;
  const previewBar = document.getElementById('genui-chat-preview');
  if (previewBar) { previewBar.innerHTML = ''; previewBar.style.display = 'none'; }
}

// Send the pill's text → open (or reuse) the page's chat-widget seeded with it,
// then clear the pill. The widget tags each message with the genui handoff so it
// reaches the page's maintaining agent (see _openChat).
function _submitChatPill() {
  const input = document.getElementById('genui-chat-input');
  if (!input) return;
  const text = input.value.trim();
  const attachmentIds = _pendingAtts.map(a => a.attachment_id).filter(Boolean);
  if (!text && !attachmentIds.length) return;
  input.value = '';
  _clearGenuiAtts();
  _updateArmed();
  _openChat(text, attachmentIds);
}

// One floating chat-widget at a time, talking to the agent that MAINTAINS this
// page: the underlying agent is WebAgent, but every message the user types is
// wrapped with the genui handoff tag (via ctx.buildTaggedPrompt) so it carries
// the CURRENT page's slug + agent_context — i.e. WebAgent acting as this page's
// agent, NOT the manager-role WebAgent the Agents-page pill uses. The tag tracks
// whichever page is current when the message is sent. Reusing the shared
// createChatWidget gives live streaming, Continue/Stop, and the mini reply pill
// for free. If a widget is already open, restore it and send into it so the
// conversation continues; otherwise open a fresh one seeded with `initialMessage`.
async function _openChat(initialMessage, attachmentIds) {
  if (!app.currentUserId) { _ctx.updateStatus('Sign in to chat with this page', 'error'); return; }
  const msg = (initialMessage || '').trim();
  const atts = Array.isArray(attachmentIds) ? attachmentIds.filter(Boolean) : [];

  // Talk to the agent that OWNS this genui (its agent_id), falling back to the
  // user's WebAgent when the genui isn't linked yet.
  const agentId = await _resolveGenuiAgentId();

  // If a widget is already open but for a DIFFERENT agent (the user switched to a
  // genui owned by another agent), close it so the message can't go to the wrong
  // agent — a fresh widget opens against the new owner below.
  if (_genuiChat && _genuiChatAgentId && agentId && _genuiChatAgentId !== agentId) {
    try { _genuiChat.close(); } catch (_) {}   // onClose nulls our refs
    _genuiChat = null;
    _genuiChatAgentId = null;
  }
  if (_genuiChat) {
    _genuiChat.restore();
    if (msg || atts.length) _genuiChat.send(msg, atts);
    return;
  }
  const cur = _ctx.getCurrentPage();
  _genuiChatAgentId = agentId;
  _genuiChat = createChatWidget({
    title: cur ? cur.title : 'Gen UI',
    iconName: 'sparkle',
    ensureAgent: () => Promise.resolve(agentId),
    transformMessage: (text) => _ctx.buildTaggedPrompt(text),
    initialMessage: msg,
    initialAttachmentIds: atts,
    onClose: () => { _genuiChat = null; _genuiChatAgentId = null; },
  });
  _genuiChat.open();
}

// ── Page dropdown (mirrors the web-chat session dropdown) ─────────────────────

function _closePageActionsPopup() {
  const open = document.querySelector('.session-row-actions[data-source="genui-page"]');
  if (open) open.remove();
}

function _openPageMenu() {
  const menu = document.getElementById('genui-page-dropdown-menu');
  const dd   = document.getElementById('genui-page-dropdown');
  if (!menu) return;
  menu.hidden = false;
  if (dd) dd.classList.add('open');
}

function _closePageMenu() {
  const menu = document.getElementById('genui-page-dropdown-menu');
  const dd   = document.getElementById('genui-page-dropdown');
  if (!menu) return;
  menu.hidden = true;
  if (dd) dd.classList.remove('open');
  _closePageActionsPopup();
}

// Load + cache the user's agents as an id→agent map. Cached after the first hit;
// returns null (and doesn't cache) until a signed-in user is known, so callers
// can retry cheaply. A renamed agent shows its new name automatically next load.
async function _loadAgents() {
  if (_agentsById) return _agentsById;
  if (!app.currentUserId) return null;
  try {
    const res = await fetch(apiPath('/api/v1/agents?user_id=' + encodeURIComponent(app.currentUserId)), { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json();
      const map = {};
      for (const a of (data.agents || [])) if (a && a.id) map[a.id] = a;
      _agentsById = map;
    }
  } catch (_) { /* leave the default placeholder */ }
  return _agentsById;
}

// The default WebAgent (template_id 'default') from the cached agents map — the
// fallback owner for genui no agent has rendered into yet.
function _defaultAgent(map) {
  return map ? Object.values(map).find(a => a.template_id === 'default') : null;
}

// Resolve the display name of the agent that OWNS the current genui (its
// agent_id), falling back to the default WebAgent when the genui has no owner
// yet (e.g. a brand-new user-made genui). '' when nothing resolves.
async function _resolveGenuiAgentName() {
  const map = await _loadAgents();
  if (!map) return '';
  const cur = _ctx.getCurrentPage();
  const owner = cur && cur.agent_id && map[cur.agent_id];
  const agent = owner || _defaultAgent(map);
  return (agent && agent.name) || '';
}

// Resolve the agent id the chat should talk to: the current genui's owner, or
// the user's WebAgent when the genui isn't linked to an agent yet.
async function _resolveGenuiAgentId() {
  const cur = _ctx.getCurrentPage();
  const map = await _loadAgents();
  if (cur && cur.agent_id && map && map[cur.agent_id]) return cur.agent_id;
  return app.ensureWebagentAgent(app.currentUserId);
}

// Set the pill placeholder to "Chat with <agent> about this…" once the owning
// agent resolves; leaves the markup default ("Chat about this page…") otherwise.
// Called on each page switch (syncPages), so it tracks the current genui.
async function _setAgentPlaceholder() {
  const input = document.getElementById('genui-chat-input');
  if (!input) return;
  const name = await _resolveGenuiAgentName();
  if (name) input.placeholder = 'Chat with ' + name + ' about this…';
}

// Mark the dropdown "loaded" so the shared .session-dropdown CSS lifts the
// pointer-events:none / dimmed state off the trigger (mirrors the header session
// dropdown). Without this the page selector is permanently click-dead.
function _markPageDropdownLoaded() {
  const dd = document.getElementById('genui-page-dropdown');
  if (dd) dd.dataset.loaded = 'true';
}

function _renderPageRows() {
  const menu = document.getElementById('genui-page-dropdown-menu');
  if (!menu) return;
  const pages = _ctx.getPages();
  const currentPage = _ctx.getCurrentPage();
  menu.innerHTML = '';
  if (!pages.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = 'No pages yet';
    menu.appendChild(empty);
    return;
  }
  for (const p of pages) {
    const row = document.createElement('div');
    row.className = 'session-row' + (currentPage && p.slug === currentPage.slug ? ' selected' : '');
    row.dataset.slug = p.slug;
    const safeTitle = (p.title || p.slug).replace(/</g, '&lt;');
    row.innerHTML =
      '<span class="session-row-title" title="' + p.slug + '">' + safeTitle + '</span>' +
      '<button class="session-row-kebab" title="Page actions" data-slug="' + p.slug + '">' +
        _icon('more-vertical') +
      '</button>';
    menu.appendChild(row);
  }
}

function _openPageRowActions(slug, row) {
  _closePageActionsPopup();
  const page = _ctx.getPages().find(p => p.slug === slug);
  if (!page) return;
  const popup = document.createElement('div');
  popup.className = 'session-row-actions';
  popup.dataset.id = slug;
  popup.dataset.source = 'genui-page';
  const isHome = page.slug === 'home';
  popup.innerHTML =
    '<button class="session-row-action" data-action="rename">' + _icon('pencil') + ' Rename</button>' +
    (isHome ? '' : '<button class="session-row-action danger" data-action="delete">' + _icon('trash-2') + ' Delete</button>');
  document.body.appendChild(popup);
  // Position next to the kebab, right-aligned, clamped to viewport.
  const kebab = row.querySelector('.session-row-kebab');
  const kb = kebab.getBoundingClientRect();
  const pw = popup.offsetWidth;
  const ph = popup.offsetHeight;
  let left = kb.right - pw;
  let top  = kb.bottom + 4;
  if (left < 4) left = 4;
  if (left + pw > window.innerWidth - 4) left = window.innerWidth - pw - 4;
  if (top + ph > window.innerHeight - 4) top = kb.top - ph - 4;
  popup.style.left = left + 'px';
  popup.style.top  = top + 'px';
  popup.addEventListener('click', (e) => {
    e.stopPropagation();
    const actionBtn = e.target.closest('.session-row-action');
    if (!actionBtn) return;
    const action = actionBtn.dataset.action;
    if (action === 'rename') {
      _closePageActionsPopup();
      _startRenamePage(slug, row);
      return;
    }
    if (action === 'delete') {
      // Two-click hazard confirm — the SHARED delete-control affordance used by
      // the chat session menu + Agents page, NOT a browser confirm() popup.
      // First click arms the trash into the hazard icon with a "Click again to
      // delete" tooltip; the second click runs the delete. The popup is recreated
      // fresh on every kebab open, so the armed state never persists.
      advanceDeleteBtn(actionBtn, {
        armTitle: 'Click again to delete',
        onConfirm: async () => { await _ctx.deletePage(slug); _closePageActionsPopup(); },
      });
    }
  });
}

// ╔═╗ RENAME-FIELD PATTERN  ════════════════════════════════════════════════════╗
// ║ Inline rename: create <input>, replace title, Enter/Escape/blur commit.    ║
// ║ Duplicated in sessions.js (startRename & _headerRenameSession) and files.js.║
// ║ Mirror fixes across all copies.                                            ║
// ╚══════════════════════════════════════════════════════════════════════════════╝
function _startRenamePage(slug, row) {
  const titleEl = row.querySelector('.session-row-title');
  if (!titleEl) return;
  const page = _ctx.getPages().find(p => p.slug === slug);
  const current = (page && page.title) || '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-row-title-input';
  input.value = current;
  input.maxLength = 60;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== current) {
      await _ctx.renamePage(slug, newTitle);   // core does the network + state update
    }
    syncPages();                               // re-render (new title, or restore on cancel)
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')      { e.preventDefault(); finish(true);  }
    else if (e.key === 'Escape'){ e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

let _pageDropdownBound = false;
function _bindPageDropdown() {
  if (_pageDropdownBound) return;
  const trigger = document.getElementById('genui-page-dropdown-trigger');
  const menu    = document.getElementById('genui-page-dropdown-menu');
  const dd      = document.getElementById('genui-page-dropdown');
  if (!trigger || !menu || !dd) return;
  _pageDropdownBound = true;

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.hidden) _openPageMenu(); else _closePageMenu();
  });

  menu.addEventListener('click', (e) => {
    e.stopPropagation();
    const kebab = e.target.closest('.session-row-kebab');
    if (kebab) {
      const row  = kebab.closest('.session-row');
      const slug = row && row.dataset.slug;
      const existing = document.querySelector('.session-row-actions[data-source="genui-page"][data-id="' + slug + '"]');
      if (existing) { _closePageActionsPopup(); return; }
      if (slug) _openPageRowActions(slug, row);
      return;
    }
    // Row body click → switch page (but not when an inline rename input is focused)
    if (e.target.closest('.session-row-title-input')) return;
    const row = e.target.closest('.session-row');
    if (row && row.dataset.slug) {
      _ctx.selectPage(row.dataset.slug);
      _closePageMenu();
    }
  });

  document.addEventListener('click', (e) => {
    if (!dd.contains(e.target)) _closePageMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) _closePageMenu();
  });
}
