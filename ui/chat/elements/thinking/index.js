'use strict';

// Chat element: thinking — toggle mid-turn message visibility: Thinking ↔ ThkOff

export function html(cfg = {}) {
  return `<button type="button" class="chat-thinking-btn" title="Toggle mid-turn agent messages on/off" data-element-name="thinking"><span class="chat-thinking-label">Thinking</span><span class="chat-thinking-link" title="Edit in chat_ui.json"><i data-lucide="external-link" style="width:11px;height:11px;"></i></span></button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-thinking-btn';
}

export function destroy(el) {}

export function style() { return ''; }
