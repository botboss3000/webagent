'use strict';

// Screen Wake Lock — keeps the device screen on while the tab is visible.
// Uses the Screen Wake Lock API (navigator.wakeLock.request("screen")).
// The lock is automatically released when the tab is hidden and re-acquired
// on visibilitychange, which is the standard web-platform pattern.
//
// Supported: Chrome 84+, Edge 84+, Firefox 126+, Safari/iOS 16.4+.
// Silent no-op when unavailable (non-secure context, old browser).
//
// Import: import { enableWakeLock, disableWakeLock, isWakeLockActive, initWakeLock } from './screen-wake.js';

let _lock = null;
let _wanted = false;          // true when the user wants the screen to stay on
let _listening = false;

// ── Re-acquire (visibilitychange handler) ────────────────────────────────

async function _reacquire() {
  if (!_wanted) return;
  if (_lock !== null) return;   // still held — can happen on quick hide/show
  try {
    if (!('wakeLock' in navigator)) return;
    _lock = await navigator.wakeLock.request('screen');
    _lock.addEventListener('release', () => { _lock = null; });
    _dispatch(true);
  } catch (_) {
    // Permission denied, browser not supported, or tab not visible
    _dispatch(false);
  }
}

function _startListening() {
  if (_listening) return;
  _listening = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      _reacquire();
    }
  });
}

// ── Public API ────────────────────────────────────────────────────────────

/** Request the screen stay on. Re-acquires automatically after tab hide/show. */
export async function enableWakeLock() {
  if (!('wakeLock' in navigator)) return false;
  _wanted = true;
  _startListening();
  await _reacquire();
  return _lock !== null;
}

/** Release the wake lock and stop automatic re-acquisition. */
export async function disableWakeLock() {
  _wanted = false;
  try {
    if (_lock) {
      await _lock.release();
      _lock = null;
    }
  } catch (_) {
    _lock = null;
  }
  _dispatch(false);
}

/** Whether a wake lock is currently held / wanted. */
export function isWakeLockActive() {
  return _wanted && _lock !== null;
}

export function isWakeLockSupported() {
  return 'wakeLock' in navigator;
}

// ── Initialisation (call once at boot) ────────────────────────────────────

export function initWakeLock() {
  _startListening();
}

// ── Internal ──────────────────────────────────────────────────────────────

function _dispatch(active) {
  try {
    window.dispatchEvent(new CustomEvent('webagent-wakelock-change', {
      detail: { active: active && _lock !== null },
    }));
  } catch (_) {}
}
