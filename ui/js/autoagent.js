'use strict';

import { app } from './state.js';
import { apiPath } from './config.js';

// ── State ──
let autoAgentActive = false;

// ── Init ──
export function initAutoAgent() {
  app._autoAgentHandler = handleEvent;

  // Wire prompt input
  const input = document.getElementById('autoagent-prompt-input');
  const sendBtn = document.getElementById('autoagent-send-btn');
  if (!input || !sendBtn) return;

  sendBtn.addEventListener('click', () => sendPrompt());
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendPrompt();
    }
  });
}

// ── Start (called when tab activates) ──
export function startAutoAgent() {
  autoAgentActive = true;
  syncIframe();
}

// ── Stop (called when tab deactivates) ──
export function stopAutoAgent() {
  autoAgentActive = false;
}

// ── Session changed ──
export function autoAgentSessionChanged() {
  if (!autoAgentActive) return;
  showPlaceholder();
}

// ── Handle WebSocket events ──
function handleEvent(event) {
  if (!autoAgentActive) return;

  // Detect render_visual tool result
  const isToolResult = event.type === 'tool_result' || event.type === 'tool_call';
  const toolName = event.tool || event.tool_name || '';

  if (isToolResult && toolName === 'render_visual') {
    if (event.type === 'tool_result') {
      // Tool completed — load the iframe
      try {
        const result = typeof event.result === 'string'
          ? JSON.parse(event.result)
          : event.result;

        if (result && result.status === 'ok' && result.path) {
          showIframe(result.path, result.title || 'Visualization');
        }
      } catch (e) {
        // If result is not JSON, check if it's a path string
        if (event.result && typeof event.result === 'string' && event.result.startsWith('/visuals/')) {
          showIframe(event.result, 'Visualization');
        }
      }
    } else if (event.type === 'tool_call') {
      // Tool started — show loading
      showLoading();
    }
  }

  // Detect pipeline events for progress
  if (event.type === 'pipeline') {
    if (event.step === 'llm_call_start') {
      updateStatus('UI Agent thinking...');
    } else if (event.step === 'execute_start' || event.step === 'execute_batch_start') {
      updateStatus('Rendering visualization...');
    } else if (event.step === 'turn_end' || event.type === 'response') {
      // Tool should have updated iframe by now
    }
  }

  // Detect errors
  if (event.type === 'error') {
    showError(event.error || event.message || 'Unknown error');
  }
}

// ── Send prompt via SSE ──
async function sendPrompt() {
  const input = document.getElementById('autoagent-prompt-input');
  const sendBtn = document.getElementById('autoagent-send-btn');
  if (!input || !sendBtn) return;

  const text = input.value.trim();
  if (!text) return;

  // Tag with agent identity
  const taggedPrompt = `[User → UI Agent]: ${text}`;

  input.value = '';
  sendBtn.disabled = true;
  showLoading();
  updateStatus('Sending to UI Agent...');

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

    if (!resp.ok) {
      showError(`Server error: ${resp.status}`);
      return;
    }

    // Read SSE stream to completion (agent loop runs, WS events update iframe)
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

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
            if (data.done) {
              updateStatus(data.reply ? 'Complete' : 'Finished');
            }
          } catch (e) { /* ignore parse errors */ }
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

// ── UI state helpers ──
function showIframe(path, title) {
  const iframe = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading = document.getElementById('autoagent-loading');

  if (placeholder) placeholder.style.display = 'none';
  if (loading) loading.style.display = 'none';

  if (iframe) {
    // Add cache-busting query param for re-renders
    const url = path + (path.includes('?') ? '&' : '?') + '_t=' + Date.now();
    iframe.src = url;
    iframe.style.display = 'block';
    iframe.title = title || 'Visualization';
  }

  updateStatus(title || 'Rendered');
}

function showLoading() {
  const iframe = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading = document.getElementById('autoagent-loading');

  if (iframe) iframe.style.display = 'none';
  if (placeholder) placeholder.style.display = 'none';
  if (loading) loading.style.display = 'flex';
}

function showPlaceholder() {
  const iframe = document.getElementById('autoagent-iframe');
  const placeholder = document.getElementById('autoagent-placeholder');
  const loading = document.getElementById('autoagent-loading');

  if (iframe) { iframe.style.display = 'none'; iframe.src = ''; }
  if (placeholder) placeholder.style.display = 'flex';
  if (loading) loading.style.display = 'none';
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
  status.className = 'aa-status';
  if (type === 'error') status.classList.add('aa-status-error');
}

// ── Sync iframe from latest interaction on activate ──
async function syncIframe() {
  const userId = app.currentUserId;
  const sessionId = app.currentSessionId;
  if (!userId || !sessionId) return;

  try {
    const url = apiPath(
      `/api/v1/db/stream/interactions?user_id=${encodeURIComponent(userId)}&session_id=${encodeURIComponent(sessionId)}`
    );
    const res = await fetch(url);
    const data = await res.json();
    const rows = data.interactions || [];

    // Find most recent render_visual tool result
    for (let i = rows.length - 1; i >= 0; i--) {
      const row = rows[i];
      if (row.role !== 'tool' || row.tool_name !== 'render_visual') continue;

      try {
        const result = JSON.parse(row.content || '{}');
        if (result.status === 'ok' && result.path) {
          showIframe(result.path, result.title);
          return;
        }
      } catch (e) {
        // Not JSON
      }
    }

    // No render found
    showPlaceholder();
  } catch (e) {
    showPlaceholder();
  }
}
