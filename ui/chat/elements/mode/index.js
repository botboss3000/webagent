'use strict';

// Chat element: mode — toggle execution mode: Ask → Plan → Auto

export function html(cfg = {}) {
  return `<button type="button" class="chat-mode-btn" title="Click to toggle execution mode: Ask → Plan → Auto → Ask" data-element-name="mode">Ask</button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-mode-btn';
}

export function destroy(el) {}

export function style() { return ''; }
