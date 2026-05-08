'use strict';

/**
 * Master login overlay for the left panel (terminal, stream, loop, database, github).
 * Chat on the right side stays accessible without login.
 */

let overlayEl = null;

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
      <h2 style="margin:0 0 4px 0; font-size:20px; font-weight:600; color:#7dcfff;">🔐 webAgent</h2>
      <p style="margin:0 0 6px 0; font-size:13px; color:#a9b1d6;">
        Sign in to access the left panel
      </p>
      <p style="margin:0 0 20px 0; font-size:12px; color:#565f89;">
        Terminal, Stream, Loop, Database, and GitHub.<br>
        Chat works without logging in.
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
    </div>
  `;

  side.style.position = 'relative';
  side.appendChild(overlayEl);

  // Wire up login button
  document.getElementById('left-login-btn').addEventListener('click', () => doLogin());

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
