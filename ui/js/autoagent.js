'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';

// ── Module state ───────────────────────────────────────────────────────────────

let autoAgentActive = false;

// Currently displayed page: { slug, title, agent_context, url }
let currentPage = null;

// All loaded pages for the current user
let pages = [];

// ── Init & lifecycle ──────────────────────────────────────────────────────────

export function initAutoAgent() {
  app._autoAgentHandler = handleEvent;

  const input   = document.getElementById('autoagent-prompt-input');
  const sendBtn = document.getElementById('autoagent-send-btn');
  const newBtn  = document.getElementById('autoagent-new-page-btn');
  const delBtn  = document.getElementById('autoagent-delete-page-btn');
  const select  = document.getElementById('autoagent-page-select');

  if (!input || !sendBtn) return;

  sendBtn.addEventListener('click', () => sendPrompt());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendPrompt(); }
  });

  if (newBtn)  newBtn.addEventListener('click', () => showNewPageDialog());
  if (delBtn)  delBtn.addEventListener('click', () => confirmDeletePage());
  if (select)  select.addEventListener('change', () => switchToPage(select.value));

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
    populateDropdown();

    // Try to keep the current page selected, otherwise default to home
    const target = currentPage
      ? pages.find(p => p.slug === currentPage.slug)
      : pages.find(p => p.slug === 'home') || pages[0];

    if (target) {
      currentPage = target;
      syncDropdownSelection();
      showIframe(target.url, target.title);
      updateDeleteButton();
    } else {
      showPlaceholder();
    }
  } catch (e) {
    showPlaceholder();
    updateStatus('Failed to load pages: ' + e.message, 'error');
  }
}

function populateDropdown() {
  const select = document.getElementById('autoagent-page-select');
  if (!select) return;
  select.innerHTML = '';
  pages.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.slug;
    opt.textContent = p.title;
    select.appendChild(opt);
  });
}

function syncDropdownSelection() {
  const select = document.getElementById('autoagent-page-select');
  if (select && currentPage) select.value = currentPage.slug;
}

function switchToPage(slug) {
  const page = pages.find(p => p.slug === slug);
  if (!page) return;
  currentPage = page;
  showIframe(page.url, page.title);
  updateDeleteButton();
  updateStatus('');
}

function updateDeleteButton() {
  const delBtn = document.getElementById('autoagent-delete-page-btn');
  if (delBtn) delBtn.disabled = !currentPage || currentPage.slug === 'home';
}

// ── Sending prompts ───────────────────────────────────────────────────────────

async function sendPrompt() {
  const input   = document.getElementById('autoagent-prompt-input');
  const sendBtn = document.getElementById('autoagent-send-btn');
  if (!input || !sendBtn) return;

  const text = input.value.trim();
  if (!text) return;

  // Tag the prompt with the current page context so the agent knows its role
  const pageLabel   = currentPage ? currentPage.slug : 'home';
  const pageTitle   = currentPage ? currentPage.title : 'Home';
  const agentCtx    = currentPage ? (currentPage.agent_context || '') : '';
  const taggedPrompt = agentCtx
    ? `[User → UI Agent → Page: "${pageTitle}" | Context: "${agentCtx}"]: ${text}`
    : `[User → UI Agent → Page: "${pageTitle}"]: ${text}`;

  input.value = '';
  sendBtn.disabled = true;
  showLoading();
  updateStatus('Sending to page agent...');

  const payload = {
    message: taggedPrompt,
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
  };

  try {
    const resp = await fetch(apiPath('/api/v1/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) { showError(`Server error: ${resp.status}`); return; }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer    = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6));
            if (data.done) updateStatus(data.reply ? 'Complete' : 'Finished');
          } catch (_) { /* ignore */ }
        }
      }
    }
    sendBtn.disabled = false;
    updateStatus('Ready');
  } catch (e) {
    sendBtn.disabled = false;
    showError(`Failed to send: ${e.message}`);
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
              if (p) { currentPage = p; syncDropdownSelection(); showIframe(p.url, p.title); }
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
          if (p) { currentPage = p; syncDropdownSelection(); showIframe(p.url, p.title); updateDeleteButton(); }
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
      if (p) { currentPage = p; syncDropdownSelection(); showIframe(p.url, p.title); updateDeleteButton(); }
    } else {
      updateStatus('Could not create page: ' + (data.detail || data.message || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Failed to create page: ' + e.message, 'error');
  }
}

// ── Delete page ───────────────────────────────────────────────────────────────

async function confirmDeletePage() {
  if (!currentPage || currentPage.slug === 'home') return;
  const confirmed = window.confirm(`Delete the page "${currentPage.title}"? This cannot be undone.`);
  if (!confirmed) return;

  try {
    const res = await fetch(
      apiPath(`/api/v1/pages/${encodeURIComponent(currentPage.slug)}?user_id=${encodeURIComponent(app.currentUserId)}`),
      { method: 'DELETE' },
    );
    const data = await res.json();
    if (data.status === 'ok') {
      currentPage = null;
      await loadPages();
    } else {
      updateStatus('Could not delete page: ' + (data.detail || data.message || 'error'), 'error');
    }
  } catch (e) {
    updateStatus('Failed to delete page: ' + e.message, 'error');
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
  }
  updateStatus(title || 'Ready');
}

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
