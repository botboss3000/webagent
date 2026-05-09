'use strict';

/**
 * Master login overlay for the left panel (terminal, stream, loop, database, github).
 * Chat on the right side stays accessible without login.
 */

let overlayEl = null;
let restrictedOverlayEl = null;

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

/** Check if the current user is admin (user_id === "admin_default"). */
export function isAdmin() {
  return localStorage.getItem('auth_user_id') === 'admin_default';
}

/** Show a "RESTRICTED ACCESS" overlay modal for non-admin users. */
export function showRestrictedModal() {
  hideRestrictedModal();

  const side = document.getElementById('terminal-side');
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
      <div style="font-size:40px; margin-bottom:12px;">🚫</div>
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
    const side = document.getElementById('terminal-side');
    if (side) side.style.position = '';
  }
  restrictedOverlayEl = null;
}

/** Show the login overlay over the entire left panel. Hides all tab content. */
export function showLeftOverlay() {
  hideLeftOverlay();

  const side = document.getElementById('terminal-side');
  if (!side) return;

  // Hide all tab content
  side.querySelectorAll('.tab-content').forEach(el => el.style.display = 'none');
  // Also hide github viewer if present
  const ghViewer = document.getElementById('gh-viewer');
  if (ghViewer) ghViewer.style.display = 'none';

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
      <h2 style="margin:0 0 4px 0; font-size:20px; font-weight:600; color:#7dcfff;">🔐 webAgent</h2>
      <p style="margin:0 0 20px 0; font-size:13px; color:#a9b1d6;">
        Sign in to access your database
      </p>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Username</label>
        <input type="text" id="left-login-user" value="admin" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Password</label>
        <input type="password" id="left-login-pass" value="admin" style="
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
      <h2 style="margin:0 0 4px 0; font-size:20px; font-weight:600; color:#7dcfff;">🔐 Create Account</h2>
      <p style="margin:0 0 20px 0; font-size:13px; color:#a9b1d6;">
        Register a new account
      </p>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Username</label>
        <input type="text" id="left-reg-user" value="" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Display Name (optional)</label>
        <input type="text" id="left-reg-display" value="" placeholder="Your name" style="
          width:100%; padding:9px 12px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:14px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Password</label>
        <input type="password" id="left-reg-pass" value="" style="
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

  // Enter key on register password field
  document.getElementById('left-reg-pass')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doRegister();
  });

  setTimeout(() => document.getElementById('left-login-pass')?.focus(), 100);
}

/** Hide the login overlay and show tab content. */
export function hideLeftOverlay() {
  if (overlayEl && overlayEl.parentNode) {
    overlayEl.parentNode.removeChild(overlayEl);
    const side = document.getElementById('terminal-side');
    if (side) side.style.position = '';
  }
  overlayEl = null;

  // Show tab content again (only the currently active one)
  const side2 = document.getElementById('terminal-side');
  if (side2) {
    side2.querySelectorAll('.tab-content').forEach(el => {
      el.style.display = el.classList.contains('active') ? '' : 'none';
    });
    const ghViewer = document.getElementById('gh-viewer');
    if (ghViewer) ghViewer.style.display = '';
  }
}

/** Attempt login with the overlay form. */
async function doLogin() {
  const username = document.getElementById('left-login-user').value.trim();
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
      body: JSON.stringify({ username, password, remember_me: rememberMe }),
    });
    if (!res.ok) {
      error.style.display = 'block';
      error.textContent = 'Invalid username or password';
      btn.disabled = false;
      loading.style.display = 'none';
      return;
    }
    const data = await res.json();

    localStorage.setItem('auth_token', data.access_token);
    localStorage.setItem('auth_username', data.username);
    localStorage.setItem('auth_user_id', data.user_id);
    localStorage.setItem('auth_display_name', data.display_name);
    if (data.remember_token) {
      localStorage.setItem('remember_token', data.remember_token);
    }

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
  const username = document.getElementById('left-reg-user').value.trim();
  const password = document.getElementById('left-reg-pass').value;
  const displayName = document.getElementById('left-reg-display').value.trim();
  const btn = document.getElementById('left-reg-btn');
  const error = document.getElementById('left-reg-error');
  const loading = document.getElementById('left-reg-loading');

  error.style.display = 'none';

  if (!username) {
    error.textContent = 'Username is required';
    error.style.display = 'block';
    return;
  }
  if (!password || password.length < 4) {
    error.textContent = 'Password must be at least 4 characters';
    error.style.display = 'block';
    return;
  }

  btn.disabled = true;
  loading.style.display = 'block';

  try {
    const res = await fetch('/api/v1/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, display_name: displayName }),
    });
    if (!res.ok) {
      let msg = 'Registration failed';
      if (res.status === 409) msg = 'Username already exists';
      error.textContent = msg;
      error.style.display = 'block';
      btn.disabled = false;
      loading.style.display = 'none';
      return;
    }
    const data = await res.json();

    localStorage.setItem('auth_token', data.access_token);
    localStorage.setItem('auth_username', data.username);
    localStorage.setItem('auth_user_id', data.user_id);
    localStorage.setItem('auth_display_name', data.display_name);

    hideLeftOverlay();
    window.location.reload();
  } catch (err) {
    error.textContent = 'Connection error. Is the server running?';
    error.style.display = 'block';
    btn.disabled = false;
    loading.style.display = 'none';
  }
}
