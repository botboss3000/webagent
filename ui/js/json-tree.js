'use strict';

// ── Collapsible JSON tree renderer ──
// Shared between stream view and database viewer

function escapeHtml(str) {
  if (!str) return '';
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderJsonTree(data, indent) {
  if (indent === undefined) indent = 0;
  const padInner = '  '.repeat(indent + 1);

  if (data === null) return '<span class="json-null">null</span>';
  if (typeof data === 'boolean') return `<span class="json-bool">${data}</span>`;
  if (typeof data === 'number') return `<span class="json-num">${data}</span>`;
  if (typeof data === 'string') {
    return `<span class="json-str">"${escapeHtml(data)}"</span>`;
  }

  if (Array.isArray(data)) {
    if (data.length === 0) return '<span class="json-punc">[</span><span class="json-punc">]</span>';
    const items = data.map((v, i) => {
      const val = renderJsonTree(v, indent + 1);
      const comma = i < data.length - 1 ? '<span class="json-punc">,</span>' : '';
      return `<div class="json-line">${padInner}${val}${comma}</div>`;
    }).join('');
    return `<span class="json-block">\n<span class="json-toggle">▼</span><span class="json-punc">[</span><span class="json-children">\n${items}\n${'  '.repeat(indent)}</span><span class="json-punc">]</span>\n</span>`;
  }

  if (typeof data === 'object') {
    const keys = Object.keys(data);
    if (keys.length === 0) return '<span class="json-punc">{</span><span class="json-punc">}</span>';
    const items = keys.map((k, i) => {
      const key = `<span class="json-key">"${escapeHtml(k)}"</span>`;
      const val = renderJsonTree(data[k], indent + 1);
      const comma = i < keys.length - 1 ? '<span class="json-punc">,</span>' : '';
      return `<div class="json-line">${padInner}${key}<span class="json-punc">:</span> ${val}${comma}</div>`;
    }).join('');
    return `<span class="json-block">\n<span class="json-toggle">▼</span><span class="json-punc">{</span><span class="json-children">\n${items}\n${'  '.repeat(indent)}</span><span class="json-punc">}</span>\n</span>`;
  }

  return escapeHtml(String(data));
}

/**
 * Parse raw string as JSON and return collapsible HTML tree.
 * Returns null if not valid JSON.
 */
export function formatJsonAsHtml(raw) {
  try {
    const parsed = JSON.parse(raw);
    return `<div class="json-root">${renderJsonTree(parsed, 0)}</div>`;
  } catch (e) {
    return null;
  }
}

/**
 * Attach a single click listener for .json-toggle elements.
 * Safe to call multiple times — uses event delegation on document.
 */
export function initJsonToggle() {
  // Only attach once
  if (document._jsonToggleAttached) return;
  document._jsonToggleAttached = true;

  document.addEventListener('click', (e) => {
    const toggle = e.target.closest('.json-toggle');
    if (!toggle) return;
    const block = toggle.closest('.json-block');
    if (!block) return;
    const wasCollapsed = block.classList.toggle('json-collapsed');
    toggle.textContent = wasCollapsed ? '▶' : '▼';
  });
}
