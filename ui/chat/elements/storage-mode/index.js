'use strict';

// Chat element: storage_mode — toggle between browser-authority (IndexedDB)
// and normal server-backed storage. Only shown when the router is available.

export function html(cfg = {}) {
  return `<button type="button" class="chat-storage-mode-btn" title="Click to toggle storage mode: Browser (local) ↔ Server (remote)" data-element-name="storage_mode">Server</button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-storage-mode-btn';
}

export function destroy(el) {}

export function style() { return ''; }
