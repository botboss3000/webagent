'use strict';

/*
 * page-access-gate.js — inline "sign in to view this page" gate.
 *
 * When a visitor navigates to a main page their access level can't see (a
 * registered-only page while signed out, or an off/admins-only page), the
 * navigation layer (ui/shared/js/tabs.js) calls showPageAccessGate(pageId)
 * INSTEAD of the page's own start hook — so the page is never mounted and its
 * gated endpoints are never hit. This renders the SAME email/password sign-in the
 * corner user menu uses (ui/shared/js/user-panel.js), but centred IN the main
 * panel where the visitor tried to go, rather than in a dropdown. On success the
 * page reloads and the visibility rule (ui/shared/js/header-build.js
 * __pageHiddenByVisibility) re-runs, revealing the page if they now qualify.
 *
 * Mounts into #main-panel (index.html). Peers: tabs.js (the only caller),
 * accounts.js (token store / multi-account), header-build.js (the rule mirrored).
 */

import { upsertAccount } from './accounts.js';

const GATE_ID = 'page-access-gate';

// A real member holds a member token whose user_id is NOT an anon_* guest. An
// Open-Registration guest carries a token too, but it's an anonymous identity —
// treated as logged-out here so registered-only pages still gate them. Mirrors
// header-build.js isSignedIn() so the gate and the tab-hiding rule agree.
function _isMember() {
  try {
    if (!localStorage.getItem('auth_token')) return false;
    const uid = localStorage.getItem('auth_user_id') || '';
    return uid.indexOf('anon_') !== 0;
  } catch (_) { return false; }
}

// Human label for the gated page from the live catalog (falls back to the cached
// copy, then to a generic phrase) so the message names the page they tried to open.
function _pageLabel(pageId) {
  try {
    const cat = window.__pagesCatalog || (window.__readPagesCache && window.__readPagesCache());
    const list = (cat && Array.isArray(cat.main)) ? cat.main : [];
    const p = list.find((x) => x && x.id === pageId);
    if (p && p.label) return String(p.label);
  } catch (_) {}
  return 'this page';
}

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

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

const _INPUT_STYLE = 'width:100%;padding:10px 12px;background:var(--bg-0);'
  + 'border:var(--border-width) solid var(--border);border-radius:8px;color:var(--fg-1);'
  + 'font-size:14px;font-family:inherit;outline:none;box-sizing:border-box;';

// The sign-in card markup. title + subtext are escaped here, so callers pass
// plain text. Same fields the corner-menu form uses (#pag-* ids, scoped to the
// host below so two cards in the DOM never collide).
function _cardHTML(title, subtext) {
  return `
    <div style="background:var(--bg-elev);border:var(--border-width) solid var(--border);
                border-radius:14px;padding:30px 28px;width:360px;max-width:100%;
                box-shadow:0 8px 32px rgba(var(--shadow-rgb),0.5);
                display:flex;flex-direction:column;gap:12px;text-align:left;">
      <div style="width:46px;height:46px;border-radius:12px;display:flex;align-items:center;
                  justify-content:center;background:rgba(var(--brand-rgb),0.14);color:var(--brand);">
        <i data-lucide="lock" style="width:22px;height:22px;"></i>
      </div>
      <div style="font-size:19px;font-weight:700;color:var(--fg-1);">${_esc(title)}</div>
      <div style="font-size:13px;color:var(--fg-3);line-height:1.5;margin-bottom:4px;">${_esc(subtext)}</div>
      <input type="email" id="pag-email" placeholder="Email" autocomplete="username" style="${_INPUT_STYLE}">
      <input type="password" id="pag-pass" placeholder="Password" autocomplete="current-password" style="${_INPUT_STYLE}">
      <div style="display:flex;align-items:center;gap:7px;">
        <input type="checkbox" id="pag-remember" checked style="accent-color:var(--brand);margin:0;width:15px;height:15px;cursor:pointer;">
        <label for="pag-remember" style="font-size:12px;color:var(--fg-3);cursor:pointer;">Remember me</label>
      </div>
      <button id="pag-btn" style="width:100%;padding:11px;background:var(--brand);border:none;
              border-radius:8px;color:var(--bg-0);font-size:15px;font-weight:700;cursor:pointer;
              margin-top:2px;">Sign In</button>
      <div id="pag-error" style="color:var(--danger);font-size:12px;display:none;"></div>
      <div id="pag-loading" style="color:var(--fg-3);font-size:12px;text-align:center;display:none;">Signing in…</div>
    </div>
  `;
}

// Attach the login behaviour to a host that already holds the card markup.
// Queries are host-scoped (not document.getElementById) so the page gate and the
// app-wide restricted overlay can each carry a card without their ids clashing.
function _wireSignIn(host) {
  const emailInput = host.querySelector('#pag-email');
  const passInput = host.querySelector('#pag-pass');
  const rememberInput = host.querySelector('#pag-remember');
  const loginBtn = host.querySelector('#pag-btn');
  const errorEl = host.querySelector('#pag-error');
  const loadingEl = host.querySelector('#pag-loading');
  if (!emailInput || !passInput || !loginBtn) return;

  async function doLogin() {
    const email = emailInput.value.trim();
    const password = passInput.value;
    const rememberMe = rememberInput.checked;
    errorEl.style.display = 'none';
    loginBtn.disabled = true;
    loadingEl.style.display = 'block';
    try {
      const res = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, remember_me: rememberMe }),
      });
      if (!res.ok) {
        errorEl.textContent = res.status === 403 ? 'Account pending approval' : 'Invalid email or password';
        errorEl.style.display = 'block';
        loginBtn.disabled = false;
        loadingEl.style.display = 'none';
        return;
      }
      const data = await res.json();
      upsertAccount({
        user_id: data.user_id,
        username: data.username,
        display_name: data.display_name,
        access_token: data.access_token,
        remember_token: data.remember_token || '',
      });
      // Reload so the gate re-runs against the new identity. For a page gate the
      // ?tab=<pageId> tabs.js left in the URL returns the visitor to the page;
      // for the app-wide private lock the reload simply reveals the app once a
      // valid token exists (or re-locks with "pending approval" if not).
      window.location.reload();
    } catch (err) {
      errorEl.textContent = 'Connection error';
      errorEl.style.display = 'block';
      loginBtn.disabled = false;
      loadingEl.style.display = 'none';
    }
  }

  loginBtn.addEventListener('click', doLogin);
  passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin(); });
  emailInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') passInput.focus(); });
}

/**
 * Render the sign-in card into `host` and wire it. Shared by the per-page gate
 * (over #main-panel) and the app-wide Restricted Access overlay (#app-restricted-
 * overlay, main.js _applyAppRestricted) so the Private-mode lock screen shows the
 * real login form rather than a button that opens the corner dropdown.
 */
export function mountSignInForm(host, opts) {
  if (!host) return;
  opts = opts || {};
  host.innerHTML = _cardHTML(opts.title || 'Sign in to continue', opts.subtext || '');
  _wireSignIn(host);
  try { if (window.lucide && window.lucide.createIcons) window.lucide.createIcons(); } catch (_) {}
}

/** Show the inline sign-in gate for `pageId` over the main panel. */
export function showPageAccessGate(pageId) {
  const el = _ensureGate();
  if (!el) return;
  const member = _isMember();
  const label = _pageLabel(pageId);
  const who = (localStorage.getItem('auth_display_name') || '').trim();
  const sub = member
    ? `You're signed in${who ? ' as ' + who : ''}, but the ${label} page isn't available to your account. Sign in with a different account to continue.`
    : `The ${label} page is available to signed-in members. Sign in to view it.`;
  mountSignInForm(el, { title: 'Sign in to continue', subtext: sub });
  el.style.display = 'flex';
}
