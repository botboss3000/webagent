'use strict';

const STORAGE_KEY = 'chatPanelWidth';
const MIN_WIDTH = 280;

export function initChatResize() {
  const handle = document.getElementById('chat-resize-handle');
  const chatSide = document.getElementById('chat-side');
  if (!handle || !chatSide) return;

  const MOBILE_BP = 800;
  const applySavedWidth = () => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (!saved) return;
    const w = parseInt(saved, 10);
    if (isNaN(w)) return;
    if (window.innerWidth <= MOBILE_BP) {
      chatSide.style.width = '';
    } else {
      chatSide.style.width = w + 'px';
    }
  };
  applySavedWidth();
  window.addEventListener('resize', applySavedWidth);

  let dragging = false;
  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    startWidth = chatSide.getBoundingClientRect().width;
    handle.classList.add('resizing');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const maxWidth = window.innerWidth - 250;
    const newWidth = Math.min(maxWidth, Math.max(MIN_WIDTH, startWidth + (startX - e.clientX)));
    chatSide.style.width = newWidth + 'px';
  });

  document.addEventListener('mouseup', (e) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    localStorage.setItem(STORAGE_KEY, parseInt(chatSide.style.width, 10));
  });
}
