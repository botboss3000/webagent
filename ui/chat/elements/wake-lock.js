'use strict';

// Chat element: wake_lock — toggle the Screen Wake Lock so the device
// screen stays on while this tab is visible (media-site behaviour).
// See ui/shared/js/screen-wake.js for the API wrapper.

import {
  enableWakeLock,
  disableWakeLock,
  isWakeLockActive,
  isWakeLockSupported,
} from '../../shared/js/screen-wake.js';

export function html(cfg = {}) {
  const size = cfg.element_size || '16px';
  return `<button type="button" class="chat-wakelock-btn" title="Keep screen on" data-element-name="wake_lock">
    <i data-lucide="sun-moon" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-wakelock-btn';
  if (!isWakeLockSupported()) {
    // Hide when the API is unavailable (non-secure context / old browser) —
    // same pattern the control builder uses (el.hidden + inline !important).
    el.hidden = true;
    el.style.setProperty('display', 'none', 'important');
    return;
  }
  el.addEventListener('click', async () => {
    const active = isWakeLockActive();
    if (active) {
      await disableWakeLock();
    } else {
      await enableWakeLock();
    }
    _refresh(el);
  });
  window.addEventListener('webagent-wakelock-change', () => _refresh(el));
  _refresh(el);
}

export function destroy(el) {}

export function style() {
  // Active state matches the app's toggle convention (.active + accent).
  // Inline color is set by _refresh; this is a no-op kept for the element API.
  return '';
}

function _refresh(el) {
  const active = isWakeLockActive();
  el.classList.toggle('active', active);
  // The control engine styles every control with color:var(--fg-3)!important,
  // so the active accent must be set with !important to win.
  if (active) {
    el.style.setProperty('color', 'var(--accent)', 'important');
  } else {
    el.style.removeProperty('color');
  }
  el.title = active ? 'Screen wake lock active — click to release' : 'Keep screen on';
}
