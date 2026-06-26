'use strict';

/**
 * Manage Account tab — profile/password/delete self-service.
 *
 * The tab renders three sections: profile (email + display name), password
 * (current + new + confirm), and danger zone (delete account with password
 * confirmation). All calls go to /api/v1/auth/* endpoints.
 */

import { renderAvatar } from './user-avatar.js';
import { _esc } from './dom-utils.js';
import { getActive, updateActive, removeAccount, listAccounts, upsertAccount } from './accounts.js';
import { getPrefs, setGlobalEnabled, hintsGloballyEnabled } from '../../tutorials/js/tutorial.js';
// "My appearance" per-user theme editor. Self-fills its slot only when the admin
// has enabled per-user themes (App Settings → Appearance); otherwise removes it.
import { mountMyAppearance } from './appearance-me.js';

let _wired = false;

function _authHeaders() {
  const a = getActive();
  return a && a.access_token ? { 'Authorization': `Bearer ${a.access_token}` } : {};
}

function _setStatus(el, msg, kind) {
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'error' ? 'var(--danger)' : (kind === 'ok' ? 'var(--success)' : 'var(--fg-2)');
  el.style.display = msg ? 'block' : 'none';
}

export function initAccount() {
  // The tab is re-rendered on startAccount() (whenever the user opens it).
  // Don't re-render in-place on account changes — that would wipe inline
  // status messages right after a save. The header avatar/dropdown handles
  // its own refresh via sessions.js's listener.
}

export function startAccount() {
  renderTab();
}

function stopAccount() {
  // nothing to tear down
}

function renderTab() {
  const tab = document.getElementById('tab-account');
  if (!tab) return;
  const active = getActive();

  tab.innerHTML = '';

  const wrap = document.createElement('div');
  wrap.className = 'account-tab-wrap';

  // Header with avatar + identity
  const header = document.createElement('div');
  header.className = 'account-header';
  if (active) header.appendChild(renderAvatar(active, 'xl'));
  const idBlock = document.createElement('div');
  idBlock.className = 'account-header-id';
  idBlock.innerHTML = `
    <div class="account-header-name">${_esc(active?.display_name || 'Signed out')}</div>
    <div class="account-header-email">${_esc(active?.username || '')}</div>
  `;
  header.appendChild(idBlock);
  wrap.appendChild(header);

  if (!active) {
    const msg = document.createElement('div');
    msg.className = 'account-empty';
    msg.textContent = 'You are not signed in. Sign in to manage your account.';
    wrap.appendChild(msg);
    tab.appendChild(wrap);
    return;
  }

  // ── Profile section ──
  const profile = document.createElement('section');
  profile.className = 'account-section';
  profile.innerHTML = `
    <h3 class="account-section-title">Profile</h3>
    <div class="account-field">
      <label for="account-email">Email / username</label>
      <input id="account-email" type="email" name="username" autocomplete="username" value="${escapeAttr(active.username || '')}">
    </div>
    <div class="account-field">
      <label for="account-display">Display name</label>
      <input id="account-display" type="text" name="name" autocomplete="name" value="${escapeAttr(active.display_name || '')}">
    </div>
    <div class="account-row">
      <button id="account-save-profile" class="account-btn account-btn-primary">Save changes</button>
      <span id="account-profile-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  wrap.appendChild(profile);

  // ── Password section ──
  const pw = document.createElement('section');
  pw.className = 'account-section';
  pw.innerHTML = `
    <h3 class="account-section-title">Password</h3>
    <div class="account-field">
      <label for="account-curpass">Current password</label>
      <input id="account-curpass" type="password" autocomplete="current-password">
    </div>
    <div class="account-field">
      <label for="account-newpass">New password</label>
      <input id="account-newpass" type="password" autocomplete="new-password">
    </div>
    <div class="account-field">
      <label for="account-newpass2">Confirm new password</label>
      <input id="account-newpass2" type="password" autocomplete="new-password">
    </div>
    <div class="account-row">
      <button id="account-change-password" class="account-btn account-btn-primary">Change password</button>
      <span id="account-password-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  wrap.appendChild(pw);

  // ── Preferences ──
  const prefs = document.createElement('section');
  prefs.className = 'account-section';
  // The welcome-screen row appears only when the splash plugin is installed AND
  // the admin hasn't disabled it app-wide (window.WA_SPLASH, set by
  // ui/splash/splash-page). It and the landing's own "Don't show this again"
  // checkbox drive the SAME per-device flag (the wa_seen_splash skip cookie), so
  // they stay linked. Drop-in safe: delete the splash folder → no WA_SPLASH →
  // this row simply doesn't render.
  const _splash = window.WA_SPLASH;
  const splashRow = (_splash && _splash.enabled()) ? `
    <label class="account-pref-row">
      <input type="checkbox" id="account-splash-show">
      <span class="account-pref-text">
        <span class="account-pref-title">Show welcome screen</span>
        <span class="account-pref-desc">The welcome landing page shown at the front door before the app. Turn off to go straight to the app on this device; turn back on to see the welcome page again.</span>
      </span>
    </label>
  ` : '';
  // The app tour row only appears when the admin hasn't disabled hints app-wide
  // (App Settings → Startup & Boot → Show tooltips). Same pattern as the splash
  // row above: a globally-off feature shows no per-user control.
  const tourRow = hintsGloballyEnabled() ? `
    <label class="account-pref-row">
      <input type="checkbox" id="account-tutorial-global">
      <span class="account-pref-text">
        <span class="account-pref-title">Show app tour</span>
        <span class="account-pref-desc">Numbered hover popovers that walk you through the main features of the app — chat, agents, pages and admin tools. Turn off once you’re comfortable; turn back on to replay.</span>
      </span>
    </label>
  ` : '';
  prefs.innerHTML = `
    <h3 class="account-section-title">Preferences</h3>
    ${tourRow}
    ${splashRow}
  `;
  // Only show the Preferences section if at least one control is available.
  if (tourRow || splashRow) wrap.appendChild(prefs);

  // ── Appearance (per-user theme editor) ──
  // A positioned slot that mountMyAppearance() fills when the admin has enabled
  // per-user themes, or removes when off. Kept here (before Danger zone) so the
  // section lands in the right place even though it loads asynchronously.
  const appearanceSlot = document.createElement('section');
  appearanceSlot.className = 'account-section';
  appearanceSlot.id = 'account-appearance-slot';
  wrap.appendChild(appearanceSlot);

  // ── Danger zone ──
  const danger = document.createElement('section');
  danger.className = 'account-section account-danger-zone';
  danger.innerHTML = `
    <h3 class="account-section-title">Danger zone</h3>
    <p class="account-danger-desc">Deleting your account is permanent. Your sessions and data cannot be recovered.</p>
    <button id="account-delete-trigger" class="account-btn account-btn-danger">Delete account…</button>
    <div id="account-delete-confirm" class="account-delete-confirm" style="display:none;">
      <div class="account-field">
        <label for="account-delete-pass">Type your password to confirm</label>
        <input id="account-delete-pass" type="password" autocomplete="current-password">
      </div>
      <label class="account-delete-ack">
        <input type="checkbox" id="account-delete-ack">
        I understand this action cannot be undone.
      </label>
      <div class="account-row">
        <button id="account-delete-confirm-btn" class="account-btn account-btn-danger" disabled>Permanently delete</button>
        <button id="account-delete-cancel" class="account-btn">Cancel</button>
        <span id="account-delete-status" class="account-status" style="display:none;"></span>
      </div>
    </div>
  `;
  wrap.appendChild(danger);

  tab.appendChild(wrap);

  wireHandlers();

  // Fill (or remove) the per-user theme editor now that the slot is in the DOM.
  mountMyAppearance(document.getElementById('account-appearance-slot'));
}

function wireHandlers() {
  const profileBtn = document.getElementById('account-save-profile');
  if (profileBtn) profileBtn.addEventListener('click', onSaveProfile);

  const pwBtn = document.getElementById('account-change-password');
  if (pwBtn) pwBtn.addEventListener('click', onChangePassword);

  const delTrigger = document.getElementById('account-delete-trigger');
  if (delTrigger) delTrigger.addEventListener('click', () => {
    document.getElementById('account-delete-confirm').style.display = 'block';
    delTrigger.style.display = 'none';
  });

  const delCancel = document.getElementById('account-delete-cancel');
  if (delCancel) delCancel.addEventListener('click', () => {
    document.getElementById('account-delete-confirm').style.display = 'none';
    document.getElementById('account-delete-trigger').style.display = '';
    document.getElementById('account-delete-pass').value = '';
    document.getElementById('account-delete-ack').checked = false;
    document.getElementById('account-delete-confirm-btn').disabled = true;
    _setStatus(document.getElementById('account-delete-status'), '', '');
  });

  const ack = document.getElementById('account-delete-ack');
  const pass = document.getElementById('account-delete-pass');
  function evalDeleteEnable() {
    document.getElementById('account-delete-confirm-btn').disabled =
      !(ack.checked && pass.value.length > 0);
  }
  if (ack) ack.addEventListener('change', evalDeleteEnable);
  if (pass) pass.addEventListener('input', evalDeleteEnable);

  const delBtn = document.getElementById('account-delete-confirm-btn');
  if (delBtn) delBtn.addEventListener('click', onDeleteAccount);

  const tutorialGlobal = document.getElementById('account-tutorial-global');
  if (tutorialGlobal) {
    tutorialGlobal.checked = getPrefs().globalEnabled;
    tutorialGlobal.addEventListener('change', () => {
      setGlobalEnabled(tutorialGlobal.checked);
    });
    // Keep the toggle in sync when the popover's "Hide all hints" flips it.
    document.addEventListener('tutorial-prefs-changed', (e) => {
      if (!document.body.contains(tutorialGlobal)) return;
      tutorialGlobal.checked = !!(e.detail && e.detail.enabled);
    });
  }

  // Welcome-screen toggle — bound to the splash plugin's per-device flag via
  // window.WA_SPLASH (same flag as the splash's own "Don't show this again").
  const splashShow = document.getElementById('account-splash-show');
  if (splashShow && window.WA_SPLASH) {
    splashShow.checked = window.WA_SPLASH.isShown();
    splashShow.addEventListener('change', () => {
      window.WA_SPLASH.setShown(splashShow.checked);
    });
    // Stay in sync if the splash's own checkbox flips the same flag.
    window.addEventListener('wa-splash-pref-changed', (e) => {
      if (!document.body.contains(splashShow)) return;
      splashShow.checked = !!(e.detail && e.detail.shown);
    });
  }
}

async function onSaveProfile() {
  const status = document.getElementById('account-profile-status');
  const emailEl = document.getElementById('account-email');
  const dispEl = document.getElementById('account-display');
  const email = emailEl.value.trim();
  const display = dispEl.value.trim();
  const active = getActive();
  if (!active) return;

  const body = {};
  if (email !== active.username) body.email = email;
  if (display !== active.display_name) body.display_name = display;
  if (Object.keys(body).length === 0) {
    _setStatus(status, 'No changes to save', '');
    return;
  }

  _setStatus(status, 'Saving…', '');
  try {
    const res = await fetch('/api/v1/auth/me', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to update profile', 'error');
      return;
    }
    const data = await res.json();
    upsertAccount({
      ...active,
      username: data.username,
      display_name: data.display_name,
      access_token: data.access_token,
    });
    _setStatus(status, 'Saved', 'ok');
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

async function onChangePassword() {
  const status = document.getElementById('account-password-status');
  const cur = document.getElementById('account-curpass').value;
  const np = document.getElementById('account-newpass').value;
  const np2 = document.getElementById('account-newpass2').value;
  if (!cur || !np) {
    _setStatus(status, 'Fill in both password fields', 'error');
    return;
  }
  if (np !== np2) {
    _setStatus(status, 'New passwords do not match', 'error');
    return;
  }
  if (np.length < 4) {
    _setStatus(status, 'New password must be at least 4 characters', 'error');
    return;
  }
  _setStatus(status, 'Changing password…', '');
  try {
    const res = await fetch('/api/v1/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ current_password: cur, new_password: np }),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to change password', 'error');
      return;
    }
    const data = await res.json();
    const active = getActive();
    if (active && data.remember_token) {
      upsertAccount({ ...active, remember_token: data.remember_token });
    }
    document.getElementById('account-curpass').value = '';
    document.getElementById('account-newpass').value = '';
    document.getElementById('account-newpass2').value = '';
    _setStatus(status, 'Password changed', 'ok');
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

async function onDeleteAccount() {
  const status = document.getElementById('account-delete-status');
  const pass = document.getElementById('account-delete-pass').value;
  const active = getActive();
  if (!active || !pass) return;

  _setStatus(status, 'Deleting…', '');
  try {
    const res = await fetch('/api/v1/auth/me', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ password: pass }),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to delete account', 'error');
      return;
    }
    removeAccount(active.user_id);
    // Reload — another account becomes active, or the login overlay appears.
    window.location.reload();
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

async function _detail(res) {
  try {
    const d = await res.json();
    return d && d.detail ? String(d.detail) : '';
  } catch (_) { return ''; }
}

// Attribute-safe escaping: canonical _esc handles & < >, then escape " for use
// inside a double-quoted attribute value.
function escapeAttr(s) {
  return _esc(s).replace(/"/g, '&quot;');
}
