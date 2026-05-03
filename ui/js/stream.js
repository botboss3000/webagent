'use strict';

import { app } from './state.js';

let streamInterval = null;
let lastTimestamp = '';
let streamActive = false;

// ── Colour per role ──
const ROLE_COLORS = {
  user:      { badge: '#7dcfff', bg: '#7dcfff08', name: 'User' },
  assistant: { badge: '#9ece6a', bg: '#9ece6a08', name: 'Assistant' },
  tool:      { badge: '#e0af68', bg: '#e0af6808', name: 'Tool' },
};

function roleLabel(role) {
  const cfg = ROLE_COLORS[role] || { badge: '#565f89', bg: '#565f8908', name: role };
  return `<span class="str-role" style="color:${cfg.badge}">${cfg.name}</span>`;
}

function fmtTimestamp(ts) {
  if (!ts) return '';
  // "2026-05-03 12:54:46" → "12:54:46"
  const parts = ts.split(' ');
  return parts[1] || ts;
}

function renderInteractions(rows) {
  const list = document.getElementById('stream-list');
  const noData = list.querySelector('.db-hint');
  if (noData) noData.remove();

  for (const row of rows) {
    lastTimestamp = row.created_at;

    const content = row.content || '';
    const toolName = row.tool_name || '';
    const role = row.role || 'unknown';

    // Truncate long content for display
    const displayContent = content.length > 500
      ? content.substring(0, 500) + '…'
      : content;

    const entry = document.createElement('div');
    entry.className = 'str-entry';
    entry.innerHTML = `
      <div class="str-header">
        ${roleLabel(role)}
        <span class="str-time">${fmtTimestamp(row.created_at)}</span>
        ${toolName ? `<span class="str-tool">🔧 ${toolName}</span>` : ''}
        <span class="str-session" title="${row.session_id || ''}">${(row.session_id || '').substring(0, 8)}</span>
      </div>
      <div class="str-body">${escapeHtml(displayContent)}</div>
    `;
    list.appendChild(entry);
  }

  // Auto-scroll to bottom
  list.scrollTop = list.scrollHeight;
}

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function poll() {
  if (!streamActive) return;
  try {
    const url = `/api/v1/db/stream/interactions?since=${encodeURIComponent(lastTimestamp)}&db=local_webagent.db`;
    const res = await fetch(url);
    const data = await res.json();
    if (data.interactions && data.interactions.length > 0) {
      renderInteractions(data.interactions);
    }
  } catch (e) {
    console.error('Stream poll error:', e);
  }
}

export function startStream() {
  if (streamInterval) return;
  streamActive = true;
  // Fetch initial batch (no since — gets last 50)
  const list = document.getElementById('stream-list');
  list.innerHTML = '<div class="db-hint" style="padding:20px;font-size:11px;">Connecting…</div>';
  lastTimestamp = '';
  document.getElementById('stream-status').textContent = '● live';

  // Initial fetch
  fetch('/api/v1/db/stream/interactions?since=&db=local_webagent.db')
    .then(r => r.json())
    .then(data => {
      const list = document.getElementById('stream-list');
      list.innerHTML = '';
      if (data.interactions && data.interactions.length > 0) {
        // Set lastTimestamp to the most recent row's created_at
        lastTimestamp = data.interactions[data.interactions.length - 1].created_at;
        renderInteractions(data.interactions);
      } else {
        list.innerHTML = '<div class="db-hint">No interactions yet</div>';
      }
    })
    .catch(e => {
      document.getElementById('stream-list').innerHTML =
        `<div class="db-hint">Error: ${e.message}</div>`;
    });

  streamInterval = setInterval(poll, 2000);
}

export function stopStream() {
  streamActive = false;
  if (streamInterval) {
    clearInterval(streamInterval);
    streamInterval = null;
  }
  document.getElementById('stream-status').textContent = '⏸ paused';
}

export function initStream() {
  document.getElementById('stream-clear').addEventListener('click', () => {
    const list = document.getElementById('stream-list');
    list.innerHTML = '<div class="db-hint">Cleared. New interactions will appear here.</div>';
    lastTimestamp = '';
  });

  // auto-stop on visibility lost (not strictly needed, but nice)
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && streamInterval) {
      // Re-fetch on return
      if (streamActive) poll();
    }
  });
}
