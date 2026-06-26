'use strict';

/**
 * Shared "a session changed" notifier.
 *
 * One small app-wide signal so the places that LIST sessions (the chat side
 * panel's session list + the admin Sessions table) stay in sync no matter
 * where a session is mutated. Whenever a session is deleted, recycled,
 * restored, renamed, pinned, or hidden — from the chat header, a session-row
 * menu, or the Sessions page toolbar — the mutating code calls
 * notifySessionsChanged(); every listener re-fetches its own view.
 *
 * Built on a plain window CustomEvent (same pattern as `webagent-auth-expired`)
 * so there's no shared state to keep consistent — just fire-and-forget. Only
 * MUTATIONS dispatch; listeners merely re-read, so there is no feedback loop.
 */

export const SESSIONS_CHANGED = 'webagent-sessions-changed';

// Fire after a successful session mutation. `detail` is optional context
// (e.g. { action: 'delete', ids: [...] }) — listeners may ignore it and just
// reload.
export function notifySessionsChanged(detail = {}) {
  try {
    window.dispatchEvent(new CustomEvent(SESSIONS_CHANGED, { detail }));
  } catch (_) { /* CustomEvent unsupported — ignore */ }
}

// Subscribe to session-change notifications. Returns an unsubscribe fn.
export function onSessionsChanged(cb) {
  if (typeof cb !== 'function') return () => {};
  window.addEventListener(SESSIONS_CHANGED, cb);
  return () => window.removeEventListener(SESSIONS_CHANGED, cb);
}
