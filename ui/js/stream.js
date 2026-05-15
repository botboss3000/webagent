'use strict';

import { app } from './state.js';
import { formatJsonAsHtml, initJsonToggle } from './json-tree.js';
import { apiPath } from './config.js';

let streamInterval = null;
let lastTimestamp = '';
let streamActive = false;

// Tracks whether each role:tool combo is group-collapsed
const collapsedGroups = new Map(); // key = "role:tool"

function groupKey(role, tool) {
  return role + ':' + (tool || '');
}

// ── Filter state ──
const FILTERS = {
  roles: new Set(),       // visible roles (e.g. 'user', 'assistant', 'tool')
  tools: new Set(),       // visible tool names
  knownTools: new Set(),  // all tool names seen so far (for building filter UI)
};

function initFilters() {
  FILTERS.roles = new Set(['user', 'assistant', 'tool']);
  FILTERS.tools = new Set();  // empty = show all tools (when tool role is visible)
  FILTERS.knownTools = new Set();
  collapsedGroups.clear();
}

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
  const parts = ts.split(' ');
  return parts[1] || ts;
}

// ── Check entry visibility based on current filters ──
function entryVisible(role, toolName) {
  if (!FILTERS.roles.has(role)) return false;
  if (role === 'tool' && toolName && FILTERS.tools.size > 0) {
    return FILTERS.tools.has(toolName);
  }
  return true;
}

// ── Update hidden class on all entries ──
function applyFilters() {
  document.querySelectorAll('#stream-list .str-entry').forEach(el => {
    const role = el.dataset.role || '';
    const tool = el.dataset.tool || '';
    const visible = entryVisible(role, tool);
    el.classList.toggle('str-hidden', !visible);
  });
  updateFilterCounts();
}

// ── Rebuild filter panel UI ──
function renderFilterPanel() {
  const inner = document.getElementById('stream-filter-panel-inner');
  if (!inner) return;

  const roleEntries = [
    ['user', 'User', '#7dcfff'],
    ['assistant', 'Assistant', '#9ece6a'],
    ['tool', 'Tool', '#e0af68'],
  ];

  let html = '<div style="display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;">';

  // Role checkboxes
  html += '<span style="color:#565f89;font-size:11px;font-weight:600;margin-right:4px;">Roles:</span>';
  for (const [key, label, color] of roleEntries) {
    const checked = FILTERS.roles.has(key) ? 'checked' : '';
    html += `<label class="str-flt-label" style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;color:#a9b1d6;font-size:12px;">
      <input type="checkbox" class="str-flt-role" data-role="${key}" ${checked} style="accent-color:${color};">
      <span style="color:${color}">${label}</span>
    </label>`;
  }

  // Tool checkboxes (only if tool role is visible)
  if (FILTERS.roles.has('tool') && FILTERS.knownTools.size > 0) {
    html += `<span style="color:#565f89;font-size:11px;font-weight:600;margin:0 4px 0 12px;">Tools:</span>`;
    const sorted = [...FILTERS.knownTools].sort();
    for (const tool of sorted) {
      // When tools set is empty, all are visible — show as checked
      const checked = FILTERS.tools.size === 0 || FILTERS.tools.has(tool) ? 'checked' : '';
      html += `<label class="str-flt-label" style="display:inline-flex;align-items:center;gap:4px;cursor:pointer;color:#a9b1d6;font-size:12px;">
        <input type="checkbox" class="str-flt-tool" data-tool="${tool}" ${checked} style="accent-color:#e0af68;">
        <span>🔧 ${tool}</span>
      </label>`;
    }
  }

  // Quick controls
  html += `<span style="margin-left:12px;">
    <button id="str-flt-all" style="background:none;border:none;color:#7dcfff;cursor:pointer;font-size:11px;font-family:inherit;">All</button>
    <button id="str-flt-none" style="background:none;border:none;color:#565f89;cursor:pointer;font-size:11px;font-family:inherit;">None</button>
  </span>`;

  html += '</div>';
  inner.innerHTML = html;

  // ── Wire up role checkboxes ──
  inner.querySelectorAll('.str-flt-role').forEach(cb => {
    cb.addEventListener('change', () => {
      const role = cb.dataset.role;
      if (cb.checked) FILTERS.roles.add(role);
      else FILTERS.roles.delete(role);
      applyFilters();
      renderFilterPanel(); // re-render to show/hide tool section
    });
  });

  // ── Wire up tool checkboxes ──
  inner.querySelectorAll('.str-flt-tool').forEach(cb => {
    cb.addEventListener('change', () => {
      const tool = cb.dataset.tool;
      if (cb.checked) FILTERS.tools.add(tool);
      else FILTERS.tools.delete(tool);
      applyFilters();
    });
  });

  // ── All / None buttons ──
  const allBtn = document.getElementById('str-flt-all');
  const noneBtn = document.getElementById('str-flt-none');
  if (allBtn) {
    allBtn.addEventListener('click', () => {
      FILTERS.roles = new Set(['user', 'assistant', 'tool']);
      FILTERS.tools = new Set();
      applyFilters();
      renderFilterPanel();
    });
  }
  if (noneBtn) {
    noneBtn.addEventListener('click', () => {
      FILTERS.roles = new Set();
      FILTERS.tools = new Set();
      applyFilters();
      renderFilterPanel();
    });
  }

  updateFilterCounts();
}

// ── Update visible count next to filter button ──
function updateFilterCounts() {
  const total = document.querySelectorAll('#stream-list .str-entry').length;
  const visible = document.querySelectorAll('#stream-list .str-entry:not(.str-hidden)').length;
  const btn = document.getElementById('stream-filter-btn');
  if (btn) {
    if (visible < total) {
      btn.textContent = `⚙ Filters (${visible}/${total})`;
      btn.style.color = '#e0af68';
    } else {
      btn.textContent = '⚙ Filters';
      btn.style.color = '#565f89';
    }
  }
}

// ── Render interactions, apply filters, track known tools ──
function renderInteractions(rows) {
  const list = document.getElementById('stream-list');
  const noData = list.querySelector('.db-hint');
  if (noData) noData.remove();

  for (const row of rows) {
    lastTimestamp = row.created_at;

    const content = row.content || '';
    const toolName = row.tool_name || '';
    const role = row.role || 'unknown';

    // Track known tool names
    if (toolName) FILTERS.knownTools.add(toolName);

    const entry = document.createElement('div');
    entry.className = 'str-entry';
    entry.dataset.role = role;
    entry.dataset.tool = toolName;

    // Apply current filter visibility
    if (!entryVisible(role, toolName)) {
      entry.classList.add('str-hidden');
    }

    const headerParts = [
      `<button class="str-coll-btn" title="Collapse this">▶</button>`,
      `<button class="str-coll-all-btn" title="Collapse all ${role}${toolName ? '/' + toolName : ''}">❯❯❯❯</button>`,
    ];

    const inputData = row.input || null;
    const inputBtn = inputData
      ? `<button class="str-input-btn" title="View input data">📥</button>`
      : '';

    // Tool entries: minimal — no bubble, just content with a small header
    if (role === 'tool') {
      entry.innerHTML = `
        <div class="str-header">
          ${headerParts.join('\n          ')}
          ${inputBtn}
          <span class="str-tool" style="color:#e0af68;font-size:10px;">🔧 ${toolName || 'tool'}</span>
          <span class="str-time">${fmtTimestamp(row.created_at)}</span>
          <span class="str-session" title="${row.session_id || ''}">${(row.session_id || '').substring(0, 8)}</span>
        </div>
        <div class="str-body">${renderContent(content)}</div>
      `;
    } else {
      entry.innerHTML = `
        <div class="str-header">
          ${headerParts.join('\n          ')}
          ${inputBtn}
          ${roleLabel(role)}
          <span class="str-time">${fmtTimestamp(row.created_at)}</span>
          ${toolName ? `<span class="str-tool">🔧 ${toolName}</span>` : ''}
          <span class="str-session" title="${row.session_id || ''}">${(row.session_id || '').substring(0, 8)}</span>
        </div>
        <div class="str-body">${renderContent(content)}</div>
      `;
    }
    list.appendChild(entry);

    // ── Wire input button ──
    const inpBtn = entry.querySelector('.str-input-btn');
    if (inpBtn && inputData) {
      inpBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleInput(entry, inputData);
      });
    }

    // ── Wire collapse buttons ──
    const collBtn = entry.querySelector('.str-coll-btn');
    const collAllBtn = entry.querySelector('.str-coll-all-btn');
    if (collBtn) {
      collBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        entry.classList.toggle('str-collapsed');
        collBtn.textContent = entry.classList.contains('str-collapsed') ? '▶' : '▼';
      });
    }
    if (collAllBtn) {
      collAllBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const r = entry.dataset.role;
        const t = entry.dataset.tool;
        const key = groupKey(r, t);
        // Toggle group state
        const wasCollapsed = collapsedGroups.get(key) || false;
        const nowCollapsed = !wasCollapsed;
        collapsedGroups.set(key, nowCollapsed);
        document.querySelectorAll('#stream-list .str-entry').forEach(el => {
          if (el.dataset.role === r && el.dataset.tool === t) {
            el.classList.toggle('str-collapsed', nowCollapsed);
            const btn = el.querySelector('.str-coll-btn');
            if (btn) btn.textContent = nowCollapsed ? '▶' : '▼';
          }
        });
      });
    }
  }

  list.scrollTop = list.scrollHeight;
  updateFilterCounts();
  renderFilterPanel(); // rebuild filter UI with new tool names
}

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderContent(text) {
  // Try full text as JSON first
  try {
    const asHtml = formatJsonAsHtml(text);
    if (asHtml) return asHtml;
  } catch (e) { /* fall through */ }

  // Detect "[Tool calls: <json>]" embedded anywhere in text
  // Uses greedy .+ for inner JSON to handle nested brackets
  const tcMatch = text.match(/(.*?)\[Tool calls: (\[.+\])\](.*)/s);
  if (tcMatch) {
    const prefix = tcMatch[1];
    const jsonStr = tcMatch[2];

    // Log tool calls to console for debugging
    try {
      const toolCalls = JSON.parse(jsonStr);
      console.log('[Tool Calls]', toolCalls);
    } catch (e) {
      console.log('[Tool Calls - unparseable]', jsonStr);
    }

    // Return only the text before tool calls (hide from display)
    return escapeHtml(prefix);
  }

  return escapeHtml(text);
}

function toggleInput(entry, rawData) {
  let section = entry.querySelector('.str-input-section');
  if (section) {
    section.remove();
    entry.querySelector('.str-input-btn').style.color = '#3b4261';
    return;
  }

  section = document.createElement('div');
  section.className = 'str-input-section';

  const asHtml = formatJsonAsHtml(rawData);
  if (asHtml) {
    section.innerHTML = asHtml;
  } else {
    section.innerHTML = `<pre class="str-input-pre">${escapeHtml(rawData)}</pre>`;
  }

  const header = entry.querySelector('.str-header');
  const body = entry.querySelector('.str-body');
  if (header && body) {
    header.parentNode.insertBefore(section, body);
  } else {
    entry.appendChild(section);
  }
  entry.querySelector('.str-input-btn').style.color = '#7dcfff';
}

async function poll() {
  if (!streamActive) return;
  try {
    const sid = app.currentSessionId;
    const url = apiPath(`/api/v1/db/stream/interactions?since=${encodeURIComponent(lastTimestamp)}&db=local.db${sid ? '&session_id=' + encodeURIComponent(sid) : ''}`);
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
  initFilters();

  const list = document.getElementById('stream-list');
  list.innerHTML = '<div class="db-hint" style="padding:20px;font-size:11px;">Connecting…</div>';
  lastTimestamp = '';
  document.getElementById('stream-status').textContent = '● live';

  renderFilterPanel();

  const initialSid = app.currentSessionId;
  const initialUrl = apiPath(`/api/v1/db/stream/interactions?since=&db=local.db${initialSid ? '&session_id=' + encodeURIComponent(initialSid) : ''}`);
  fetch(initialUrl)
    .then(r => r.json())
    .then(data => {
      const list = document.getElementById('stream-list');
      list.innerHTML = '';
      if (data.interactions && data.interactions.length > 0) {
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

  streamInterval = setInterval(poll, 1000);
}

export function stopStream() {
  streamActive = false;
  if (streamInterval) {
    clearInterval(streamInterval);
    streamInterval = null;
  }
  document.getElementById('stream-status').textContent = '⏸ paused';
}

// ── Called when session changes externally (from sessions.js) ──
export function streamSessionChanged() {
  const wasActive = streamActive;
  // Stop current poll, clear state, restart fresh with new session
  if (streamInterval) {
    clearInterval(streamInterval);
    streamInterval = null;
  }
  const list = document.getElementById('stream-list');
  if (list) {
    list.innerHTML = '<div class="db-hint" style="padding:20px;font-size:11px;">Session changed — loading…</div>';
  }
  lastTimestamp = '';
  initFilters();
  if (wasActive) {
    startStream();
  }
}

export function initStream() {
  // Global JSON toggle delegation
  initJsonToggle();

  // Toggle filter panel
  document.getElementById('stream-filter-btn').addEventListener('click', () => {
    const panel = document.getElementById('stream-filter-panel');
    const shown = panel.style.display !== 'none';
    panel.style.display = shown ? 'none' : 'block';
    if (!shown) renderFilterPanel();
  });

  document.getElementById('stream-clear').addEventListener('click', () => {
    const list = document.getElementById('stream-list');
    list.innerHTML = '<div class="db-hint">Cleared. New interactions will appear here.</div>';
    lastTimestamp = '';
    initFilters();
    renderFilterPanel();
    updateFilterCounts();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && streamInterval) {
      if (streamActive) poll();
    }
  });
}
