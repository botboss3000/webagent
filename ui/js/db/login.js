'use strict';

/**
 * Login overlay for the DB viewer tab.
 * Shows a login form when the user tries to access the DB without auth.
 */

let loginOverlay = null;
let onLoginCallback = null;

export function setOnLogin(cb) {
  onLoginCallback = cb;
}

/** Read auth token from localStorage (returns null if missing). */
export function getAuthToken() {
  return localStorage.getItem('auth_token');
}

/** Get auth headers for DB fetches. */
export function authHeaders() {
  const token = getAuthToken();
  if (!token) return {};
  return { 'Authorization': `Bearer ${token}` };
}

/** Build a URL with ?token= for endpoints that use query-param auth. */
export function authUrl(url) {
  const token = getAuthToken();
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** Show the login overlay within the DB viewer area. */
export function showLoginOverlay() {
  hideLoginOverlay();
  const dbPanel = document.getElementById('db-panel');
  if (!dbPanel) return;

  loginOverlay = document.createElement('div');
  loginOverlay.id = 'db-login-overlay';
  loginOverlay.style.cssText = `
    position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(13, 13, 26, 0.92);
    display: flex; align-items: center; justify-content: center;
    z-index: 100;
  `;
  loginOverlay.innerHTML = `
    <div style="
      background: #1a1a2e; border: 1px solid #2a2a4a;
      border-radius: 12px; padding: 28px 32px;
      width: 300px; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    ">
      <h2 style="margin:0 0 4px 0; font-size:18px; font-weight:600; color:#7dcfff;">🔐 DB Viewer</h2>
      <p style="margin:0 0 20px 0; font-size:12px; color:#565f89;">
        Login required to view and edit database tables.
        Chat &amp; agent work without login.
      </p>
      <div style="margin-bottom:12px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Username</label>
        <input type="text" id="db-login-user" value="admin" style="
          width:100%; padding:8px 10px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:13px; font-family:inherit;
          outline:none; box-sizing:border-box;
        ">
      </div>
      <div style="margin-bottom:16px;">
        <label style="display:block; font-size:12px; font-weight:600; color:#a9b1d6; margin-bottom:4px;">Password</label>
        <input type="password" id="db-login-pass" value="admin" style="
          width:100%; padding:8px 10px; background:#0d0d1a; border:1px solid #2a2a4a;
          border-radius:6px; color:#c0caf5; font-size:13px; font-family:inherit;
          outline:none; box-sizing:border-box;
        " onkeydown="if(event.key==='Enter') document.getElementById('db-login-btn').click()">
      </div>
      <div style="margin-bottom:12px;">
        <label style="display:flex; align-items:center; gap:6px; font-size:12px; color:#a9b1d6; cursor:pointer;">
          <input type="checkbox" id="db-login-remember" checked style="accent-color:#7dcfff;">
          Remember me
        </label>
      </div>
      <button id="db-login-btn" style="
        width:100%; padding:9px; background:#7dcfff; border:none; border-radius:6px;
        color:#0d0d1a; font-size:14px; font-weight:700; cursor:pointer;
      ">Sign In</button>
      <div id="db-login-error" style="color:#fb4934; font-size:12px; margin-top:10px; display:none;"></div>
      <div id="db-login-loading" style="color:#565f89; font-size:12px; margin-top:10px; text-align:center; display:none;">Signing in…</div>
    </div>
  `;

  dbPanel.style.position = 'relative';
  dbPanel.appendChild(loginOverlay);

  // Wire up button
  document.getElementById('db-login-btn').addEventListener('click', doLogin);
  // Focus the password field
  setTimeout(() => document.getElementById('db-login-pass')?.focus(), 100);
}

export function hideLoginOverlay() {
  if (loginOverlay && loginOverlay.parentNode) {
    loginOverlay.parentNode.removeChild(loginOverlay);
    const dbPanel = document.getElementById('db-panel');
    if (dbPanel) dbPanel.style.position = '';
  }
  loginOverlay = null;
}

async function doLogin() {
  const username = document.getElementById('db-login-user').value.trim();
  const password = document.getElementById('db-login-pass').value;
  const rememberMe = document.getElementById('db-login-remember').checked;
  const btn = document.getElementById('db-login-btn');
  const error = document.getElementById('db-login-error');
  const loading = document.getElementById('db-login-loading');

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

    // Store auth
    localStorage.setItem('auth_token', data.access_token);
    localStorage.setItem('auth_username', data.username);
    localStorage.setItem('auth_user_id', data.user_id);
    localStorage.setItem('auth_display_name', data.display_name);
    if (data.remember_token) {
      localStorage.setItem('remember_token', data.remember_token);
    }

    hideLoginOverlay();

    // Re-load DB viewer
    if (typeof onLoginCallback === 'function') onLoginCallback();
  } catch (err) {
    error.textContent = 'Connection error. Is the server running?';
    error.style.display = 'block';
    btn.disabled = false;
    loading.style.display = 'none';
  }
}
