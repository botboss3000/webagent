'use strict';

// Chat control: voice — record voice input

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="chat-pill-voice" title="Record voice" data-controls-name="voice">
    <i data-lucide="mic" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-voice-btn';
}

export function destroy(el) {}

export function style() { return ''; }
