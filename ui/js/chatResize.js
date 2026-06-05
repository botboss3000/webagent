'use strict';

const STORAGE_KEY = 'chatPanelWidth';
const MIN_WIDTH = 280;

export function initChatResize() {
  const handle = document.getElementById('chat-resize-handle');
  const chatPanel = document.getElementById('chat-panel');
  if (!handle || !chatPanel) return;

  const MOBILE_BP = 800;
  const applySavedWidth = () => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return;
    const w = parseInt(saved, 10);
    if (isNaN(w)) return;
    if (window.innerWidth <= MOBILE_BP) {
      chatPanel.style.width = '';
    } else {
      chatPanel.style.width = w + 'px';
    }
  };
  applySavedWidth();
  window.addEventListener('resize', applySavedWidth);

  // Programmatic width set for the App-control ability (agent-driven, via a
  // `ui_command` WebSocket event). Clamped to the same bounds as a drag and
  // persisted, so it survives reloads. No-op on mobile (chat is full-width).
  window.__setChatPanelWidth = function (px) {
    const w = parseInt(px, 10);
    if (isNaN(w)) return;
    if (window.innerWidth <= MOBILE_BP) return;
    const maxWidth = window.innerWidth - 250;
    const clamped = Math.min(maxWidth, Math.max(MIN_WIDTH, w));
    chatPanel.style.width = clamped + 'px';
    try { localStorage.setItem(STORAGE_KEY, String(clamped)); } catch (_) {}
  };

  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startWidth = chatPanel.getBoundingClientRect().width;
    handle.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const maxWidth = window.innerWidth - 250;
    const newWidth = Math.min(maxWidth, Math.max(MIN_WIDTH, startWidth + (startX - e.clientX)));
    chatPanel.style.width = newWidth + 'px';
  });

  document.addEventListener('mouseup', (e) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    localStorage.setItem(STORAGE_KEY, parseInt(chatPanel.style.width, 10));
  });
}
