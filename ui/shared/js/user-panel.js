'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * User Panel — header user dropdown, inline login, account switching, theme.
 *
 * Controls the #user-dropdown element in the main header: avatar, inline login
 * form, account list, theme buttons, manage/add/signout actions.
 */

import { app } from './state.js';
import { renderAvatar } from './user-avatar.js';
import {
  listAccounts,
  getActive,
  removeAccount,
  switchTo,
  onChange as onAccountsChange,
} from './accounts.js';
import { showLeftOverlay } from './left-login.js';

// ── Server-reachability guard ──────────────────────────────────────────────
// Used by setChatHeaderReachable to avoid redundant DOM writes.
let _serverReachable = true;

/**
 * Toggle the session-dropdown between normal and offline-spinner states.
 * Called by reconnect.js's health poll.
 */
export function setChatHeaderReachable(reachable) {
  reachable = reachable !== false;
  if (reachable === _serverReachable) return;
  _serverReachable = reachable;
  const dropdown = document.getElementById('session-dropdown');
  if (dropdown) {
    if (_serverReachable) dropdown.removeAttribute('data-offline');
    else dropdown.dataset.offline = 'true';
  }
}

// ── Header avatar ─────────────────────────────────────────────────────────

export async function populateUserSelect() {
  // Header avatar (letter icon) + tooltip with full username
  const slot = document.getElementById('top-user-avatar-slot');
  if (slot) {
    slot.innerHTML = '';
    const active = getActive();
    const acct = active || {
      display_name: app.currentUserId || 'None',
      username: app.currentUserId || '',
    };
    slot.appendChild(renderAvatar(acct, 'sm'));
    const trigger = document.getElementById('top-user-id');
    if (trigger) trigger.title = acct.username || acct.display_name || '';
  }
  // Re-render the dropdown contents so the current-row + other accounts stay fresh
  renderUserDropdown();
  // Refresh the agent list, which sets currentAgentId. Sessions are now
  // loaded lazily when the user first opens the session dropdown (see
  // openMenu in initSessions). Skipped while the session menu is open so
  // the user doesn't lose their place mid-click.
  if (app.currentUserId) {
    const menu = document.getElementById('session-dropdown-menu');
    const isOpen = menu && !menu.hidden;
    if (!isOpen) {
      // populateAgentSelect is set on app by sessions.js's registerSessionApi
      if (typeof app.populateAgentSelect === 'function') {
        app.populateAgentSelect(app.currentUserId);
      }
    }
  }
}

// Export as app.populateUserSelect (set in initUserPanel)

// ── User dropdown contents ────────────────────────────────────────────────

/** An Open-Registration guest holds an anon_* identity — NOT a real member.
 *  The dropdown treats such a visitor exactly like a logged-out one: it shows
 *  the inline Sign-in form instead of the account row + management actions. */
function isAnonAccount(acct) {
  return !!acct && String(acct.user_id || '').indexOf('anon_') === 0;
}

/** Render the contents of #user-dropdown-menu's dynamic sections. */
function renderUserDropdown() {
  const active = getActive();
  const all = listAccounts();
  // `member` = signed in as a real account. Anonymous guests render as logged-out.
  const member = !!active && !isAnonAccount(active);

  // Current user row (large avatar + name + email) — or inline login form
  const cur = document.getElementById('dropdown-current-row');
  if (cur) {
    cur.innerHTML = '';
    if (member) {
      // Reset any inline styles that the login form may have set
      cur.style.padding = '';
      cur.style.flexDirection = '';
      cur.style.alignItems = '';
      cur.style.gap = '';
      cur.appendChild(renderAvatar(active, 'lg'));
      const info = document.createElement('div');
      info.className = 'user-dropdown-current-info';
      const nameEl = document.createElement('div');
      nameEl.className = 'user-dropdown-current-name';
      nameEl.textContent = active.display_name || active.username || 'User';
      const emailEl = document.createElement('div');
      emailEl.className = 'user-dropdown-current-email';
      emailEl.textContent = active.username || '';
      info.appendChild(nameEl);
      info.appendChild(emailEl);
      cur.appendChild(info);
    } else {
      // Inline login form inside the dropdown — no full-page modal
      cur.style.padding = '10px 14px 6px';
      cur.style.flexDirection = 'column';
      cur.style.alignItems = 'stretch';
      cur.style.gap = '8px';
      cur.innerHTML = `
        <div style="font-size:13px;font-weight:600;color:var(--fg-2);margin-bottom:2px;">Sign in</div>
        <input type="email" id="dd-login-email" placeholder="Email" autocomplete="username" style="
          width:100%;padding:8px 10px;background:var(--bg-0);border:1px solid var(--border);
          border-radius:6px;color:var(--fg-1);font-size:13px;font-family:inherit;
          outline:none;box-sizing:border-box;
        ">
        <input type="password" id="dd-login-pass" placeholder="Password" autocomplete="current-password" style="
          width:100%;padding:8px 10px;background:var(--bg-0);border:1px solid var(--border);
          border-radius:6px;color:var(--fg-1);font-size:13px;font-family:inherit;
          outline:none;box-sizing:border-box;
        ">
        <div style="display:flex;align-items:center;gap:6px;">
          <input type="checkbox" id="dd-login-remember" checked style="accent-color:var(--brand);margin:0;">
          <label for="dd-login-remember" style="font-size:12px;color:var(--fg-3);cursor:pointer;">Remember me</label>
        </div>
        <button id="dd-login-btn" style="
          width:100%;padding:8px;background:var(--brand);border:none;border-radius:6px;
          color:var(--bg-0);font-size:14px;font-weight:700;cursor:pointer;
        ">Sign In</button>
        <div id="dd-login-error" style="color:var(--danger);font-size:12px;display:none;"></div>
        <div id="dd-login-loading" style="color:var(--fg-3);font-size:12px;text-align:center;display:none;">Signing in…</div>
      `;

      // Wire up the inline login form
      const emailInput = document.getElementById('dd-login-email');
      const passInput = document.getElementById('dd-login-pass');
      const loginBtn = document.getElementById('dd-login-btn');
      const errorEl = document.getElementById('dd-login-error');
      const loadingEl = document.getElementById('dd-login-loading');

      async function doInlineLogin() {
        const email = emailInput.value.trim();
        const password = passInput.value;
        const rememberMe = document.getElementById('dd-login-remember').checked;
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
            errorEl.style.display = 'block';
            errorEl.textContent = res.status === 403 ? 'Account pending approval' : 'Invalid email or password';
            loginBtn.disabled = false;
            loadingEl.style.display = 'none';
            return;
          }
          const data = await res.json();
          const { upsertAccount } = await import('./accounts.js');
          upsertAccount({
            user_id: data.user_id,
            username: data.username,
            display_name: data.display_name,
            access_token: data.access_token,
            remember_token: data.remember_token || '',
          });
          window.location.reload();
        } catch (err) {
          errorEl.style.display = 'block';
          errorEl.textContent = 'Connection error';
          loginBtn.disabled = false;
          loadingEl.style.display = 'none';
        }
      }

      loginBtn.addEventListener('click', doInlineLogin);
      passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doInlineLogin(); });
      emailInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') passInput.focus(); });
    }
  }

  // Other accounts (non-active)
  const list = document.getElementById('dropdown-other-accounts');
  if (list) {
    list.innerHTML = '';
    const others = all.filter((a) => !isAnonAccount(a) && (!active || a.user_id !== active.user_id));
    for (const acct of others) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'user-dropdown-account-row';
      row.dataset.userId = acct.user_id;
      row.appendChild(renderAvatar(acct, 'md'));
      const text = document.createElement('div');
      text.className = 'user-dropdown-account-info';
      text.innerHTML = `
        <div class="user-dropdown-account-name"></div>
        <div class="user-dropdown-account-email"></div>
      `;
      text.querySelector('.user-dropdown-account-name').textContent = acct.display_name || acct.username || acct.user_id;
      text.querySelector('.user-dropdown-account-email').textContent = acct.username || '';
      row.appendChild(text);
      row.addEventListener('click', async (e) => {
        e.stopPropagation();
        const ok = await switchTo(acct.user_id);
        if (ok) {
          window.location.reload();
        } else {
          // recall failed — fall back to login overlay
          showLeftOverlay();
        }
      });
      list.appendChild(row);
    }
  }

  // Toggle visibility of signed-in-only elements
  const manageBtn = document.getElementById('btn-manage-account');
  const addBtn = document.getElementById('btn-add-account');
  const signoutBtn = document.getElementById('btn-signout-header');
  const dividers = document.querySelectorAll('#user-dropdown-menu > .user-dropdown-divider');
  const otherAccounts = document.getElementById('dropdown-other-accounts');
  if (member) {
    if (manageBtn) manageBtn.style.display = '';
    if (addBtn) addBtn.style.display = '';
    if (signoutBtn) signoutBtn.style.display = '';
    dividers.forEach(d => d.style.display = '');
    if (otherAccounts) otherAccounts.style.display = '';
  } else {
    if (manageBtn) manageBtn.style.display = 'none';
    if (addBtn) addBtn.style.display = 'none';
    if (signoutBtn) signoutBtn.style.display = 'none';
    dividers.forEach(d => d.style.display = 'none');
    if (otherAccounts) otherAccounts.style.display = 'none';
  }

  // Refresh Lucide icons for any newly-inserted SVG containers
  try {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  } catch (_) {}
}

// ── Theme system ──────────────────────────────────────────────────────────

const _STORAGE_KEY = 'webagent_theme';

/** Set theme on <body>: 'light', 'dark', or 'system' (follow OS).
 *  Also updates the PWA theme-color meta tag to match. */
function applyTheme(theme) {
  const body = document.body;
  if (theme === 'light') {
    body.classList.add('light-mode');
  } else if (theme === 'dark') {
    body.classList.remove('light-mode');
  } else {
    // system — follow OS preference
    const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
    body.classList.toggle('light-mode', prefersLight);
  }
  // Sync PWA theme-color with current background
  const isLight = body.classList.contains('light-mode');
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = isLight ? '#faf5ee' : '#0d0d1a';
}

/** Highlight the matching theme button. Queries the WHOLE document, so every
 *  `.theme-option` set on the page (the user dropdown AND the App Settings →
 *  Design header switch) is kept in sync from one call. Exported so other panels
 *  can re-affirm the highlight on their own buttons when they mount. */
export function highlightThemeOption(theme) {
  document.querySelectorAll('.theme-option').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}

/** The visitor's saved theme choice ('light' | 'dark' | 'system'); 'system' on
 *  first load. Per-browser (localStorage), NOT an app-wide setting. */
export function getSavedTheme() {
  try { return localStorage.getItem(_STORAGE_KEY) || 'system'; } catch (_) { return 'system'; }
}

/** The one entry point to change the live theme: apply it to <body>, sync every
 *  `.theme-option` highlight, and persist the choice. Shared by the user-dropdown
 *  toggle and the App Settings → Design header switch so they never diverge. */
export function setTheme(theme) {
  if (!theme) return;
  applyTheme(theme);
  highlightThemeOption(theme);
  try { localStorage.setItem(_STORAGE_KEY, theme); } catch (_) {}
}

// ── Init ──────────────────────────────────────────────────────────────────

export function initUserPanel() {
  // ── Theme system ──

  // Load saved theme on init (default 'system' on first load)
  const savedTheme = getSavedTheme();
  applyTheme(savedTheme);
  highlightThemeOption(savedTheme);

  // Listen to system preference changes when in 'system' mode
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  mq.addEventListener('change', () => {
    if (getSavedTheme() === 'system') applyTheme('system');
  });

  // Wire theme buttons — pointerdown is more reliable than click for small
  // targets. Only the dropdown's buttons exist at boot; the App Settings →
  // Design header switch is wired by app-settings.js when that page mounts
  // (it reuses the exported setTheme, so both stay in sync).
  // `dataset.wired` is the SHARED guard with app-settings.js's _initThemeSwitch:
  // the Design-header buttons are in the DOM at boot, so whichever pass runs
  // first wires + flags them and the other skips — never double-bound.
  document.querySelectorAll('.theme-option').forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      setTheme(btn.dataset.theme);
    });
  });

  // ── User dropdown toggle ──
  // The trigger lives inside #main-tabs, a horizontally-scrolling carousel
  // whose overflow clips any absolutely-positioned descendant. The menu is
  // therefore position:fixed (see app1.css) and anchored under the trigger by
  // JS each time it opens, so it escapes the carousel's clip region.
  const userDropdown = document.getElementById('user-dropdown');
  const dropdownMenu = document.getElementById('user-dropdown-menu');
  const trigger = document.querySelector('.user-dropdown-trigger');

  function positionUserMenu() {
    if (!trigger || !dropdownMenu) return;
    const r = trigger.getBoundingClientRect();
    dropdownMenu.style.marginTop = '0';
    dropdownMenu.style.top = Math.round(r.bottom + 6) + 'px';
    // Clamp horizontally so the menu never spills past the viewport edges.
    const w = dropdownMenu.offsetWidth || 360;
    let left = r.left;
    const maxLeft = window.innerWidth - w - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    dropdownMenu.style.left = Math.round(left) + 'px';
  }

  function openUserMenu() {
    dropdownMenu.style.display = 'block';
    userDropdown.classList.add('open');
    positionUserMenu();
  }

  function closeUserMenu() {
    dropdownMenu.style.display = 'none';
    userDropdown.classList.remove('open');
  }

  if (trigger && dropdownMenu) {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (dropdownMenu.style.display === 'block') closeUserMenu();
      else openUserMenu();
    });

    // Close dropdown on outside click.
    // Check both the trigger wrapper AND the menu itself — the menu is a portal
    // (sibling of #main-tabs-wrap) so it is no longer a descendant of #user-dropdown.
    document.addEventListener('click', (e) => {
      if (!userDropdown.contains(e.target) && !dropdownMenu.contains(e.target)) closeUserMenu();
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && dropdownMenu.style.display === 'block') closeUserMenu();
    });

    // Re-anchor the fixed menu to the trigger if the viewport changes size.
    window.addEventListener('resize', () => {
      if (dropdownMenu.style.display === 'block') positionUserMenu();
    });
  }

  // ── Render dropdown contents (current row + other accounts) ──
  renderUserDropdown();

  // Re-render whenever the accounts list/active user changes
  onAccountsChange(() => renderUserDropdown());

  // ── Manage Account button (in dropdown) → switch to account tab ──
  const manageBtn = document.getElementById('btn-manage-account');
  if (manageBtn) {
    manageBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sel = document.getElementById('main-tab-select');
      if (sel) {
        sel.value = 'account';
        sel.dispatchEvent(new Event('change'));
      }
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
    });
  }

  // ── Add account button → show login overlay for a fresh sign-in ──
  const addBtn = document.getElementById('btn-add-account');
  if (addBtn) {
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
      showLeftOverlay();
    });
  }

  // ── Sign-out button: remove active account; switch to another if possible ──
  const signoutBtn = document.getElementById('btn-signout-header');
  if (signoutBtn) {
    signoutBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const active = getActive();
      if (active) {
        removeAccount(active.user_id);
        const remaining = listAccounts();
        if (remaining.length > 0) {
          const next = remaining[0];
          const ok = await switchTo(next.user_id);
          if (!ok) {
            // recall failed for next — fall through to full logout reload
          }
        }
      } else {
        // No tracked accounts — clear legacy keys directly
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_username');
        localStorage.removeItem('auth_user_id');
        localStorage.removeItem('auth_display_name');
        localStorage.removeItem('remember_token');
      }
      localStorage.removeItem('anonUserId');
      localStorage.removeItem('terminalUserId');
      window.location.reload();
    });
  }

  // ── Expose on app ──
  app.populateUserSelect = populateUserSelect;
}