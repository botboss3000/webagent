'use strict';

// Chat element: top_nav — jump-to-top / jump-to-last-user FAB buttons

export function html(cfg = {}) {
  return `<div data-element-name="top_nav" aria-label="Jump to top of conversation">
    <button class="chat-nav-fab" data-id="scroll-top" title="Jump to the start of the session" aria-label="Jump to the start of the session" type="button">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 11 12 6 17 11"/><polyline points="7 18 12 13 17 18"/></svg>
    </button>
    <button class="chat-nav-fab" data-id="scroll-lastuser" title="Jump up to the previous message you sent" aria-label="Jump up to the previous message you sent" type="button">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="7 14 12 9 17 14"/></svg>
    </button>
  </div>`;
}

export function init(el, cfg = {}) {
  const top = el.querySelector('[data-id="scroll-top"]');
  const last = el.querySelector('[data-id="scroll-lastuser"]');
  if (top) top.id = 'chat-scroll-top-btn';
  if (last) last.id = 'chat-scroll-lastuser-btn';
}

export function destroy(el) {}

export function style() { return ''; }
