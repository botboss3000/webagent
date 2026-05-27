'use strict';

/**
 * Master login overlay for the left panel (terminal, stream, loop, database).
 * Chat on the right side stays accessible without login.
 */

import { icon } from './icons.js';
import { upsertAccount } from './accounts.js';

let overlayEl = null;
let restrictedOverlayEl = null;

let _accessMode = 'public_anonymous';
let _accessModeFetched = false;

/** Read the app's current access_mode (cached). */
export function getAccessMode() {
  return _accessMode;
}

/** Fetch access_mode from the public auth endpoint and cache it. */
export async function fetchAccessMode() {
  try {
    const res = await fetch('/api/v1/auth/access-mode');
    if (res.ok) {
      const data = await res.json();
      _accessMode = data.access_mode || 'public_anonymous';
    }
  } catch (e) { /* keep default */ }
  _accessModeFetched = true;
  try {
    window.dispatchEvent(new CustomEvent('access-mode-loaded', { detail: { access_mode: _accessMode } }));
  } catch {}
  return _accessMode;
}

function _applyRegistrationVisibility() {
  const link = document.getElementById('left-login-register-link');
  if (link) link.style.display = (_accessMode === 'private') ? 'none' : '';
}

// Re-apply visibility whenever the mode changes from the User Management tab
window.addEventListener('access-mode-changed', e => {
  _accessMode = (e.detail && e.detail.access_mode) || _accessMode;
  _applyRegistrationVisibility();
});

/** Read auth token. */
export function getAuthToken() {
  return localStorage.getItem('auth_token');
}

/** Get auth headers for fetch calls. */
export function authHeaders() {
  const token = getAuthToken();
  if (!token) return {};
  return { 'Authorization': `Bearer ${token}` };
}

/** Build URL with ?token= for query-param auth. */
export function authUrl(url) {
  const token = getAuthToken();
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** Check if user is authenticated (has a valid token in storage). */
export function isAuthenticated() {
  return !!getAuthToken();
}

let _userIsAdmin = false;
let _adminStatusFetched = false;

/** Check if the current user is admin.
 *
 * Returns true when:
 *   - the bootstrap user_id "admin_default" is signed in (legacy shortcut), OR
 *   - the cached profile fetched via fetchAdminStatus() reports is_admin = 1.
 */
export function isAdmin() {
  if (localStorage.getItem('auth_user_id') === 'admin_default') return true;
  return _userIsAdmin;
}

/** Fetch the signed-in user's is_admin flag from /api/v1/user/profile and cache it.
 * Dispatches an 'admin-status-loaded' event when done so the tab strip
 * can update Admin Tools visibility. Anonymous visitors (no auth_token)
 * resolve to is_admin=false without a network call.
 */
export async function fetchAdminStatus() {
  const userId = localStorage.getItem('auth_user_id');
  const token = localStorage.getItem('auth_token');
  if (!userId || !token) {
    _userIsAdmin = false;
    _adminStatusFetched = true;
    try {
      window.dispatchEvent(new CustomEvent('admin-status-loaded', { detail: { is_admin: false } }));
    } catch {}
    return false;
  }
  try {
    const res = await fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(userId)}`, {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    if (res.ok) {
      const data = await res.json();
      _userIsAdmin = !!data.is_admin;
    }
  } catch (e) { /* keep default */ }
  _adminStatusFetched = true;
  try {
    window.dispatchEvent(new CustomEvent('admin-status-loaded', { detail: { is_admin: _userIsAdmin } }));
  } catch {}
  return _userIsAdmin;
}

/** Show a "RESTRICTED ACCESS" overlay modal for non-admin users. */
export function showRestrictedModal() {
  hideRestrictedModal();

  const side = document.getElementById('main-panel');
  if (!side) return;

  restrictedOverlayEl = document.createElement('div');
  restrictedOverlayEl.id = 'restricted-overlay';
  restrictedOverlayEl.style.cssText = `
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(13, 13, 26, 0.95);
    display: flex; align-items: center; justify-content: center;
    z-index: 210;
  `;
  restrictedOverlayEl.innerHTML = `
    <div style="
      background: #1a1a2e; border: 1px solid #fb4934;
      border-radius: 12px; padding: 32px 36px;
      width: 340px; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
      text-align: center;
    ">
      <div style="margin-bottom:12px; color:#fb4934;">${icon('ban', { size: '40px' })}</div>
      <h2 style="margin:0 0 8px 0; font-size:20px; font-weight:700; color:#fb4934;">RESTRICTED ACCESS</h2>
      <p style="margin:0 0 20px 0; font-size:13px; color:#a9b1d6;">
        Only admin users can access this feature.
      </p>
      <button id="restricted-close-btn" style="
        padding:10px 24px; background:#2a2a4a; border:none; border-radius:6px;
        color:#c0caf5; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit;
      ">Close</button>
    </div>
  `;

  side.style.position = 'relative';
  side.appendChild(restrictedOverlayEl);

  document.getElementById('restricted-close-btn').addEventListener('click', hideRestrictedModal);
}

/** Hide the restricted access overlay. */
export function hideRestrictedModal() {
  if (restrictedOverlayEl && restrictedOverlayEl.parentNode) {
    restrictedOverlayEl.parentNode.removeChild(restrictedOverlayEl);
    const side = document.getElementById('main-panel');
    if (side) side.style.position = '';
  }
  restrictedOverlayEl = null;
}

/** Show the login overlay over the entire left panel. Hides all tab content. */
export function showLeftOverlay() {
  hideLeftOverlay();

  const side = document.getElementById('main-panel');
  if (!side) return;

  // Hide all tab content
  side.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');

  overlayEl = document.createElement('div');
  overlayEl.id = 'left-login-overlay';
  overlayEl.style.cssText = `
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(13, 13, 26, 0.95);
    display: flex; align-items: center; justify-content: center;
    z-index: 200;
  `;
  overlayEl.innerHTML = `
    <div style="
      background: #1a1a2e; border: 1px solid #2a2a4a;
      border-radius: 12px; padding: 32px 36px;
      width: 340px; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    ">
    <div id="left-login-form">
      <h2 style="margin:0 0 4px 0; font-size:20px; font-weight:600; color:#7dcfff;">${icon('lock', { size: '18px' })} webAgent</h2>
      <p style="margin:0 0 20px 0; font-size:13px; color:#a9b1d6;">
        Sign in to access your database
      </p>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Email</label>
        <input type="email" id="left-login-user" name="username" autocomplete="username" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Password</label>
        <input type="password" id="left-login-pass" name="password" autocomplete="current-password" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        " onkeydown="if(event.key==='Enter') document.getElementById('left-login-btn').click()">
      </div>
      <div style="margin-bottom:16px;">
        <label style="display:flex; align-items:center; gap:6px; font-size:13px; color:#a9b1d6; cursor:pointer;">
          <input type="checkbox" id="left-login-remember" checked style="accent-color:#7dcfff;">
          Remember me
        </label>
      </div>
      <button id="left-login-btn" style="
        width:100%; padding:10px; background:#7dcfff; border:none; border-radius:6px;
        color:#0d0d1a; font-size:15px; font-weight:700; cursor:pointer;
      ">Sign In</button>
      <div id="left-login-error" style="color:#fb4934; font-size:13px; margin-top:10px; display:none;"></div>
      <div id="left-login-loading" style="color:#565f89; font-size:13px; margin-top:10px; text-align:center; display:none;">Signing in…</div>
      <div id="left-login-register-link" style="margin-top:14px; text-align:center; font-size:13px; color:#565f89;">
        Not registered? <a href="#" id="left-login-toggle-reg" style="color:#7dcfff; text-decoration:none; font-weight:600;">Click here</a>
      </div>
    </div>

    <!-- Registration form (hidden by default) -->
    <div id="left-register-form" style="display:none;">
      <h2 style="margin:0 0 4px 0; font-size:20px; font-weight:600; color:#7dcfff;">${icon('lock', { size: '18px' })} Create Account</h2>
      <p style="margin:0 0 20px 0; font-size:13px; color:#a9b1d6;">
        Register a new account
      </p>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Email</label>
        <input type="email" id="left-reg-user" name="email" autocomplete="email" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Display Name (optional)</label>
        <input type="text" id="left-reg-display" name="name" autocomplete="name" value="" placeholder="Your name" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Password</label>
        <input type="password" id="left-reg-pass" name="new-password" autocomplete="new-password" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Confirm Password</label>
        <input type="password" id="left-reg-pass2" name="new-password-confirm" autocomplete="new-password" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <button id="left-reg-btn" style="
        width:100%; padding:10px; background:#b8bb26; border:none; border-radius:6px;
        color:#0d0d1a; font-size:15px; font-weight:700; cursor:pointer;
      ">Register</button>
      <div id="left-reg-error" style="color:#fb4934; font-size:13px; margin-top:10px; display:none;"></div>
      <div id="left-reg-loading" style="color:#565f89; font-size:13px; margin-top:10px; text-align:center; display:none;">Registering…</div>
      <div style="margin-top:14px; text-align:center; font-size:13px; color:#565f89;">
        Already have an account? <a href="#" id="left-login-toggle-back" style="color:#7dcfff; text-decoration:none; font-weight:600;">Sign in</a>
      </div>
    </div>
  `;

  side.style.position = 'relative';
  side.appendChild(overlayEl);

  // Wire up login button
  document.getElementById('left-login-btn').addEventListener('click', () => doLogin());

  // Wire up registration toggle
  document.getElementById('left-login-toggle-reg').addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('left-login-form').style.display = 'none';
    document.getElementById('left-register-form').style.display = 'block';
    document.getElementById('left-reg-error').style.display = 'none';
    document.getElementById('left-reg-loading').style.display = 'none';
    setTimeout(() => document.getElementById('left-reg-user')?.focus(), 100);
  });

  // Wire up back-to-login toggle
  document.getElementById('left-login-toggle-back').addEventListener('click', (e) => {
    e.preventDefault();
    document.getElementById('left-register-form').style.display = 'none';
    document.getElementById('left-login-form').style.display = 'block';
    document.getElementById('left-login-error').style.display = 'none';
    document.getElementById('left-login-loading').style.display = 'none';
    setTimeout(() => document.getElementById('left-login-pass')?.focus(), 100);
  });

  // Wire up register button
  document.getElementById('left-reg-btn').addEventListener('click', () => doRegister());

  // Enter key on register password fields
  document.getElementById('left-reg-pass')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('left-reg-pass2')?.focus();
  });
  document.getElementById('left-reg-pass2')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doRegister();
  });

  _applyRegistrationVisibility();
  if (!_accessModeFetched) fetchAccessMode().then(_applyRegistrationVisibility);

  setTimeout(() => document.getElementById('left-login-pass')?.focus(), 100);
}

/** Hide the login overlay and show tab content. */
export function hideLeftOverlay() {
  if (overlayEl && overlayEl.parentNode) {
    overlayEl.parentNode.removeChild(overlayEl);
    const side = document.getElementById('main-panel');
    if (side) side.style.position = '';
  }
  overlayEl = null;

  // Show tab content again (only the currently active one)
  const side2 = document.getElementById('main-panel');
  if (side2) {
    side2.querySelectorAll('.tab-content').forEach(el => {
      el.style.display = el.classList.contains('active') ? '' : 'none';
    });
  }
}

/** Attempt login with the overlay form. */
async function doLogin() {
  const email = document.getElementById('left-login-user').value.trim();
  const password = document.getElementById('left-login-pass').value;
  const rememberMe = document.getElementById('left-login-remember').checked;
  const btn = document.getElementById('left-login-btn');
  const error = document.getElementById('left-login-error');
  const loading = document.getElementById('left-login-loading');

  error.style.display = 'none';
  btn.disabled = true;
  loading.style.display = 'block';

  try {
    const res = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, remember_me: rememberMe }),
    });
    if (!res.ok) {
      error.style.display = 'block';
      if (res.status === 403) {
        let msg = 'Account pending admin approval';
        try { const d = await res.json(); if (d.detail) msg = d.detail; } catch {}
        error.textContent = msg;
      } else {
        error.textContent = 'Invalid email or password';
      }
      btn.disabled = false;
      loading.style.display = 'none';
      return;
    }
    const data = await res.json();

    // Register the account in the multi-account store and make it active.
    // upsertAccount() also mirrors the legacy auth_* keys so existing code
    // that reads them keeps working.
    upsertAccount({
      user_id: data.user_id,
      username: data.username,
      display_name: data.display_name,
      access_token: data.access_token,
      remember_token: data.remember_token || '',
    });

    hideLeftOverlay();

    // Reload to fully initialize tabs with auth access
    window.location.reload();
  } catch (err) {
    error.textContent = 'Connection error. Is the server running?';
    error.style.display = 'block';
    btn.disabled = false;
    loading.style.display = 'none';
  }
}

/** Attempt registration with the register form. */
async function doRegister() {
  const email = document.getElementById('left-reg-user').value.trim();
  const password = document.getElementById('left-reg-pass').value;
  const confirmPassword = document.getElementById('left-reg-pass2').value;
  const displayName = document.getElementById('left-reg-display').value.trim();
  const btn = document.getElementById('left-reg-btn');
  const error = document.getElementById('left-reg-error');
  const loading = document.getElementById('left-reg-loading');

  error.style.display = 'none';

  if (!email) {
    error.textContent = 'Email is required';
    error.style.display = 'block';
    return;
  }
  if (!email.includes('@')) {
    error.textContent = 'Enter a valid email address';
    error.style.display = 'block';
    return;
  }
  if (!password || password.length < 4) {
    error.textContent = 'Password must be at least 4 characters';
    error.style.display = 'block';
    return;
  }
  if (password !== confirmPassword) {
    error.textContent = 'Passwords do not match';
    error.style.display = 'block';
    return;
  }

  btn.disabled = true;
  loading.style.display = 'block';

  try {
    const res = await fetch('/api/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, display_name: displayName }),
    });
    if (!res.ok) {
      let msg = 'Registration failed';
      if (res.status === 403) msg = 'Registration is disabled. This app is private.';
      else if (res.status === 409) msg = 'Email already registered';
      error.textContent = msg;
      error.style.display = 'block';
      btn.disabled = false;
      loading.style.display = 'none';
      return;
    }
    const data = await res.json();

    if (data.pending_approval) {
      loading.style.display = 'none';
      error.textContent = 'Account created. An administrator must approve your account before you can sign in.';
      error.style.color = '#b8bb26';
      error.style.display = 'block';
      btn.disabled = false;
      return;
    }

    upsertAccount({
      user_id: data.user_id,
      username: data.username,
      display_name: data.display_name,
      access_token: data.access_token,
      remember_token: '',
    });

    hideLeftOverlay();
    window.location.reload();
  } catch (err) {
    error.textContent = 'Connection error. Is the server running?';
    error.style.display = 'block';
    btn.disabled = false;
    loading.style.display = 'none';
  }
}
