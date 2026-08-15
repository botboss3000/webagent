'use strict';

/*
 * page-access-gate.js — shows a loading spinner when a page requires sign-in.
 *
 * When a visitor navigates to a main page their access level can't see (a
 * registered-only page while signed out, or an off/admins-only page), the
 * navigation layer (ui/shared/js/tabs.js) calls showPageAccessGate(pageId)
 * instead of mounting the page. The visitor signs in through the header
 * avatar button (user-panel.js); the page reload reveals the content.
 *
 * Mounts into #main-panel (index.html). Peers: tabs.js (the only caller).
 */

const GATE_ID = 'page-access-gate';

// Lazily create the overlay inside #main-panel (which is position:relative, so
// inset:0 fills exactly the page area — never the header or the chat panel).
function _ensureGate() {
  let el = document.getElementById(GATE_ID);
  if (el) return el;
  const panel = document.getElementById('main-panel');
  if (!panel) return null;
  el = document.createElement('div');
  el.id = GATE_ID;
  el.style.cssText = 'position:absolute;inset:0;z-index:40;display:none;'
    + 'align-items:center;justify-content:center;padding:24px;overflow:auto;';
  panel.appendChild(el);
  return el;
}

/** Tear the gate down (no-op if it was never shown). Called on every normal
 *  navigation so leaving a gated page reveals the next page cleanly. */
export function hidePageAccessGate() {
  const el = document.getElementById(GATE_ID);
  if (el) el.style.display = 'none';
}

/**
 * Render a loading spinner into `host` — no text, no form.
 */
export function mountSignInForm(host) {
  if (!host) return;
  host.innerHTML = `
    <div style="display:flex;flex-direction:column;align-items:center;gap:16px;padding:48px 16px;">
      <div class="gate-spinner" style="width:34px;height:34px;border:3px solid var(--border-strong);border-top-color:var(--accent);border-radius:50%;animation:gateSpin 0.8s linear infinite;"></div>
    </div>
    <style>
      @keyframes gateSpin { to { transform: rotate(360deg); } }
      @media (prefers-reduced-motion: reduce) { .gate-spinner { animation: none !important; } }
    </style>
  `;
}

/** Show a loading spinner on the page area instead of mounting the gated page. */
export function showPageAccessGate() {
  const el = _ensureGate();
  if (!el) return;
  mountSignInForm(el);
  el.style.display = 'flex';
}
