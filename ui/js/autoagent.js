'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';
import { wireChatPillUploads } from './attachments.js';

// ── Module state ───────────────────────────────────────────────────────────────

let autoAgentActive = false;

// Currently displayed page: { slug, title, agent_context, url }
let currentPage = null;

// All loaded pages for the current user
let pages = [];

// Tiny lucide-style icon helper for menu rows. Lucide rewrites <i> tags at
// boot, but rows are created after boot — we inline the SVGs so they render
// without needing another rewrite pass.
const _ICON_SVGS = {
  'more-vertical': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>',
  'pencil': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/></svg>',
  'trash-2': '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/></svg>',
};
function _icon(name) { return _ICON_SVGS[name] || ''; }

// ── Init & lifecycle ──────────────────────────────────────────────────────────

// CHAT-PILL-SYNC: this init wires the Pages tab's chat pill. The same wiring
// pattern (has-text toggle, attach/voice forwarders, enter-to-send) lives in
// ui/js/chat.js (web chat) and ui/js/agents.js (_bindAgentBuilderBar). Shared
// CSS for all three pills is in ui/css/app1.css under ".chat-pill".
export function initAutoAgent() {
  app._autoAgentHandler = handleEvent;

  const input    = document.getElementById('autoagent-prompt-input');
  const row      = document.getElementById('autoagent-prompt-row');
  const sendBtn  = document.getElementById('autoagent-send-btn');
  const attachBtn = document.getElementById('autoagent-attach-btn');
  const voiceBtn  = document.getElementById('autoagent-voice-btn');
  const newBtn  = document.getElementById('autoagent-new-page-btn');

  if (!input || !sendBtn) return;

  sendBtn.addEventListener('click', () => sendPrompt());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
  });

  // Swap voice/send button visibility based on whether the textarea has
  // content. Mirrors #agent-builder-bar-row and #chat-input-row behavior.
  if (row) {
    const sync = () => {
      row.classList.toggle('has-text', input.value.trim().length > 0);
    };
    input.addEventListener('input', sync);
  }

  // Forward attach and voice to the main chat composer's existing handlers
  // so we don't duplicate the file-picker / recorder logic.
  if (attachBtn) {
    attachBtn.addEventListener('click', () => {
      const mainAttach = document.getElementById('chat-attach-btn');
      if (mainAttach) mainAttach.click();
    });
  }
  if (voiceBtn) {
    voiceBtn.addEventListener('click', () => {
      const mainVoice = document.getElementById('chat-voice-btn');
      if (mainVoice) mainVoice.click();
    });
  }

  // Paste images + drop files onto this pill. Uploads land in the main chat
  // preview bar, matching the attach-button forwarding above; the prompt
  // submits via the main chat send so attachment_ids ride along.
  wireChatPillUploads(row, input);

  if (newBtn) newBtn.addEventListener('click', () => showNewPageDialog());

  _bindFooterToggle();
  _bindPageDropdown();

  // New-page dialog confirm/cancel
  const dialogConfirm = document.getElementById('aa-dialog-confirm');
  const dialogCancel  = document.getElementById('aa-dialog-cancel');
  const dialogInput   = document.getElementById('aa-dialog-input');
  if (dialogConfirm) dialogConfirm.addEventListener('click', () => submitNewPage());
  if (dialogCancel)  dialogCancel.addEventListener('click', () => hideNewPageDialog());
  if (dialogInput)   dialogInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitNewPage();
    if (e.key === 'Escape') hideNewPageDialog();
  });
}

// ── Footer toggle (chevron at top edge of the footer) ────────────────────────

function _bindFooterToggle() {
  const collapseBtn = document.getElementById('autoagent-footer-toggle');
  const expandBtn   = document.getElementById('autoagent-footer-expand');
  const footer      = document.getElementById('autoagent-footer');
  if (!footer || (!collapseBtn && !expandBtn)) return;

  function setCollapsed(collapsed) {
    footer.classList.toggle('aa-collapsed', collapsed);
    if (collapseBtn) collapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    if (expandBtn) {
      if (collapsed) expandBtn.removeAttribute('hidden');
      else expandBtn.setAttribute('hidden', '');
    }
  }

  if (collapseBtn) collapseBtn.addEventListener('click', () => setCollapsed(true));
  if (expandBtn)   expandBtn.addEventListener('click',   () => setCollapsed(false));
}

// ── Page dropdown (mirrors web-chat session dropdown) ─────────────────────────

function _closePageActionsPopup() {
  const open = document.querySelector('.session-row-actions[data-source="aa-page"]');
  if (open) open.remove();
}

function _openPageMenu() {
  const menu = document.getElementById('autoagent-page-dropdown-menu');
  const dd   = document.getElementById('autoagent-page-dropdown');
  if (!menu) return;
  menu.hidden = false;
  if (dd) dd.classList.add('open');
}

function _closePageMenu() {
  const menu = document.getElementById('autoagent-page-dropdown-menu');
  const dd   = document.getElementById('autoagent-page-dropdown');
  if (!menu) return;
  menu.hidden = true;
  if (dd) dd.classList.remove('open');
  _closePageActionsPopup();
}

function _setPageTriggerLabel() {
  const label = document.getElementById('autoagent-page-dropdown-label');
  if (!label) return;
  label.textContent = currentPage ? currentPage.title : '—';
}

function _renderPageRows() {
  const menu = document.getElementById('autoagent-page-dropdown-menu');
  if (!menu) return;
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
  const page = pages.find(p => p.slug === slug);
  if (!page) return;
  const popup = document.createElement('div');
  popup.className = 'session-row-actions';
  popup.dataset.id = slug;
  popup.dataset.source = 'aa-page';
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
    _closePageActionsPopup();
    if (action === 'rename') _startRenamePage(slug, row);
    else if (action === 'delete') _confirmDeletePageBySlug(slug);
  });
}

function _startRenamePage(slug, row) {
  const titleEl = row.querySelector('.session-row-title');
  if (!titleEl) return;
  const page = pages.find(p => p.slug === slug);
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
      await _renamePage(slug, newTitle);
    } else {
      _renderPageRows();
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')      { e.preventDefault(); finish(true);  }
    else if (e.key === 'Escape'){ e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

async function _renamePage(slug, newTitle) {
  if (!app.currentUserId) return;
  try {
    const res = await fetch(
      apiPath('/api/v1/pages/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      },
    );
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      const page = pages.find(p => p.slug === slug);
      if (page) page.title = newTitle;
      if (currentPage && currentPage.slug === slug) currentPage.title = newTitle;
      _renderPageRows();
      _setPageTriggerLabel();
    } else {
      updateStatus('Rename failed: ' + (data.detail || 'error'), 'error');
      _renderPageRows();
    }
  } catch (e) {
    updateStatus('Rename failed: ' + e.message, 'error');
    _renderPageRows();
  }
}

function _confirmDeletePageBySlug(slug) {
  const page = pages.find(p => p.slug === slug);
  if (!page || page.slug === 'home') return;
  const confirmed = window.confirm('Delete the page "' + page.title + '"? This cannot be undone.');
  if (!confirmed) return;
  _deletePage(slug);
}

async function _deletePage(slug) {
  try {
    const res = await fetch(
      apiPath('/api/v1/pages/' + encodeURIComponent(slug) + '?user_id=' + encodeURIComponent(app.currentUserId)),
      { method: 'DELETE' },
    );
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.status === 'ok') {
      if (currentPage && currentPage.slug === slug) currentPage = null;
      await loadPages();
    } else {
      updateStatus('Delete failed: ' + (data.detail || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Delete failed: ' + e.message, 'error');
  }
}

let _pageDropdownBound = false;
function _bindPageDropdown() {
  if (_pageDropdownBound) return;
  const trigger = document.getElementById('autoagent-page-dropdown-trigger');
  const menu    = document.getElementById('autoagent-page-dropdown-menu');
  const dd      = document.getElementById('autoagent-page-dropdown');
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
      const existing = document.querySelector('.session-row-actions[data-source="aa-page"][data-id="' + slug + '"]');
      if (existing) { _closePageActionsPopup(); return; }
      if (slug) _openPageRowActions(slug, row);
      return;
    }
    // Row body click → switch page (but not when an inline rename input is focused)
    if (e.target.closest('.session-row-title-input')) return;
    const row = e.target.closest('.session-row');
    if (row && row.dataset.slug) {
      switchToPage(row.dataset.slug);
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

export function startAutoAgent() {
  autoAgentActive = true;
  loadPages();
}

export function stopAutoAgent() {
  autoAgentActive = false;
}

export function autoAgentSessionChanged() {
  if (!autoAgentActive) return;
  // Reset view but keep the page list; the pages are per-user not per-session
  loadPages();
}

// ── Page loading ──────────────────────────────────────────────────────────────

async function loadPages() {
  const userId = app.currentUserId;
  if (!userId) { showPlaceholder(); return; }

  try {
    const res  = await fetch(apiPath(`/api/v1/pages?user_id=${encodeURIComponent(userId)}`));
    const data = await res.json();
    pages = data.pages || [];

    // Try to keep the current page selected, otherwise default to home
    const target = currentPage
      ? pages.find(p => p.slug === currentPage.slug)
      : pages.find(p => p.slug === 'home') || pages[0];

    if (target) {
      currentPage = target;
      showIframe(target.url, target.title);
    } else {
      currentPage = null;
      showPlaceholder();
    }
    _renderPageRows();
    _setPageTriggerLabel();
  } catch (e) {
    showPlaceholder();
    updateStatus('Failed to load pages: ' + e.message, 'error');
  }
}

function switchToPage(slug) {
  const page = pages.find(p => p.slug === slug);
  if (!page) return;
  currentPage = page;
  showIframe(page.url, page.title);
  _renderPageRows();
  _setPageTriggerLabel();
  updateStatus('');
}

// ── Sending prompts ───────────────────────────────────────────────────────────

async function sendPrompt() {
  const input   = document.getElementById('autoagent-prompt-input');
  const sendBtn = document.getElementById('autoagent-send-btn');
  if (!input || !sendBtn) return;

  const text = input.value.trim();
  if (!text) return;

  if (!app.currentUserId) {
    updateStatus('Sign in to send a prompt', 'error');
    return;
  }

  // Tag the prompt with the current page context so the webAgent knows
  // which page it is editing.
  const pageSlug = currentPage ? currentPage.slug : 'home';
  const agentCtx = currentPage ? (currentPage.agent_context || '') : '';
  const taggedPrompt = agentCtx
    ? `[User → UI Agent → Page: "${pageSlug}" | Context: "${agentCtx}"]: ${text}`
    : `[User → UI Agent → Page: "${pageSlug}"]: ${text}`;

  sendBtn.disabled = true;
  updateStatus('Starting webAgent...');

  // Hand the conversation off to the shared webAgent in a fresh session.
  try {
    await app.startWebagentSession();
  } catch (e) {
    sendBtn.disabled = false;
    updateStatus(`Could not start webAgent: ${e.message}`, 'error');
    return;
  }

  // Clear the Pages tab input and drive the main chat input.
  input.value = '';
  const row = document.getElementById('autoagent-prompt-row');
  if (row) row.classList.remove('has-text');
  sendBtn.disabled = false;
  updateStatus('webAgent is on the chat →');

  if (app.chatInput && app.chatSend) {
    app.chatInput.value = taggedPrompt;
    // Notify the chat's input listener so the send button enables and
    // the input row picks up the `has-text` class.
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    app.chatSend.click();
  }
}

// ── WebSocket event handler ───────────────────────────────────────────────────

function handleEvent(event) {
  if (!autoAgentActive) return;

  const isToolEvent = event.type === 'tool_result' || event.type === 'tool_call';
  const toolName    = event.tool || event.tool_name || '';

  if (isToolEvent && toolName === 'render_visual') {
    if (event.type === 'tool_call') {
      showLoading();
      updateStatus('Rendering page...');
    } else if (event.type === 'tool_result') {
      try {
        const result = typeof event.result === 'string'
          ? JSON.parse(event.result)
          : event.result;
        if (result && result.status === 'ok' && result.path) {
          // If the rendered page matches current, reload it
          // If it's a different page (agent created/updated a different one), refresh list
          const renderedSlug = result.page_name || 'home';
          if (currentPage && currentPage.slug === renderedSlug) {
            showIframe(result.path, result.title || currentPage.title);
          } else {
            // Refresh page list to pick up any new pages, then switch to the rendered one
            loadPages().then(() => {
              const p = pages.find(q => q.slug === renderedSlug);
              if (p) {
                currentPage = p;
                showIframe(p.url, p.title);
                _renderPageRows();
                _setPageTriggerLabel();
              }
            });
          }
        }
      } catch (_) { /* ignore parse errors */ }
    }
  }

  // Handle create_page tool results — refresh page list
  if (isToolEvent && toolName === 'create_page' && event.type === 'tool_result') {
    try {
      const result = typeof event.result === 'string'
        ? JSON.parse(event.result)
        : event.result;
      if (result && result.status === 'ok' && result.page) {
        loadPages().then(() => {
          const newSlug = result.page.slug;
          const p = pages.find(q => q.slug === newSlug);
          if (p) {
            currentPage = p;
            showIframe(p.url, p.title);
            _renderPageRows();
            _setPageTriggerLabel();
          }
        });
      }
    } catch (_) { /* ignore */ }
  }

  // Handle delete_page tool results — refresh page list
  if (isToolEvent && toolName === 'delete_page' && event.type === 'tool_result') {
    loadPages();
  }

  if (event.type === 'pipeline') {
    if (event.step === 'llm_call_start') {
      updateStatus('Page agent thinking...');
    } else if (event.step === 'execute_start' || event.step === 'execute_batch_start') {
      updateStatus('Building page...');
    }
  }

  if (event.type === 'error') {
    showError(event.error || event.message || 'Unknown error');
  }
}

// ── New page dialog ───────────────────────────────────────────────────────────

function showNewPageDialog() {
  const dialog = document.getElementById('aa-new-page-dialog');
  const input  = document.getElementById('aa-dialog-input');
  if (!dialog) return;
  dialog.style.display = 'flex';
  if (input) { input.value = ''; input.focus(); }
}

function hideNewPageDialog() {
  const dialog = document.getElementById('aa-new-page-dialog');
  if (dialog) dialog.style.display = 'none';
}

async function submitNewPage() {
  const input = document.getElementById('aa-dialog-input');
  if (!input) return;
  const title = input.value.trim();
  if (!title) return;

  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'page';
  hideNewPageDialog();

  try {
    const res = await fetch(apiPath('/api/v1/pages'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: app.currentUserId,
        slug,
        title,
      }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      await loadPages();
      const p = pages.find(q => q.slug === data.page.slug);
      if (p) {
        currentPage = p;
        showIframe(p.url, p.title);
        _renderPageRows();
        _setPageTriggerLabel();
      }
    } else {
      updateStatus('Could not create page: ' + (data.detail || data.message || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Failed to create page: ' + e.message, 'error');
  }
}

// ── iframe / display helpers ──────────────────────────────────────────────────

function showIframe(path, title) {
  const iframe      = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading     = document.getElementById('autoagent-loading');

  if (placeholder) placeholder.style.display = 'none';
  if (loading)     loading.style.display     = 'none';

  if (iframe) {
    const url = path + (path.includes('?') ? '&' : '?') + '_t=' + Date.now();
    iframe.src   = url;
    iframe.style.display = 'block';
    iframe.title = title || 'Page';
    iframe.addEventListener('load', () => postThemeToIframe(iframe), { once: true });
  }
  updateStatus(title || 'Ready');
}

function currentTheme() {
  return document.body.classList.contains('light-mode') ? 'light' : 'dark';
}

function postThemeToIframe(iframe) {
  if (!iframe || !iframe.contentWindow) return;
  try { iframe.contentWindow.postMessage({ type: 'theme', value: currentTheme() }, '*'); }
  catch (_) {}
}

// Forward theme changes to the live iframe whenever body.class toggles.
(function watchTheme() {
  if (typeof MutationObserver === 'undefined') return;
  const obs = new MutationObserver(() => {
    const iframe = document.getElementById('autoagent-iframe');
    if (iframe && iframe.style.display !== 'none') postThemeToIframe(iframe);
  });
  obs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
})();

function showLoading() {
  const iframe      = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading     = document.getElementById('autoagent-loading');

  if (iframe)      iframe.style.display      = 'none';
  if (placeholder) placeholder.style.display = 'none';
  if (loading)     loading.style.display     = 'flex';
}

function showPlaceholder() {
  const iframe      = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading     = document.getElementById('autoagent-loading');

  if (iframe)      { iframe.style.display = 'none'; iframe.src = ''; }
  if (placeholder) placeholder.style.display = 'flex';
  if (loading)     loading.style.display     = 'none';
  updateStatus('');
}

function showError(message) {
  showPlaceholder();
  updateStatus('⚠ ' + message, 'error');
}

function updateStatus(text, type) {
  const status = document.getElementById('autoagent-status');
  if (!status) return;
  status.textContent = text || '';
  status.className   = 'aa-status';
  if (type === 'error') status.classList.add('aa-status-error');
}
