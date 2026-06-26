'use strict';

/**
 * Login helpers — the actual sign-in form lives in the header user dropdown.
 * This module provides access-mode helpers and a show/hide that opens/closes
 * that dropdown instead of a full-page modal overlay.
 */

let _accessMode = 'admin_approval';
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
      _accessMode = data.access_mode || 'admin_approval';
    }
  } catch (e) { /* keep default */ }
  _accessModeFetched = true;
  try {
    window.dispatchEvent(new CustomEvent('access-mode-loaded', {
      detail: { access_mode: _accessMode },
    }));
  } catch {}
  return _accessMode;
}

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

let _isAdmin = false;
let _adminFetched = false;

/** Read the cached admin status (resolved by fetchAdminStatus). */
export function isAdmin() {
  return _isAdmin;
}

/** Whether the admin status has been resolved against the server at least once. */
function adminStatusReady() {
  return _adminFetched;
}

/**
 * Resolve whether the current user is an admin by asking the server.
 *
 * The JWT carries no is_admin claim, so this hits the public check-access
 * probe (app/api/files.py → GET /api/v1/files/check-access), which looks up
 * user_profiles.is_admin for the signed-in user_id. Result is cached and a
 * `admin-status-loaded` event is dispatched so the header can show/hide the
 * Admin Tools tab. Anyone without a valid admin profile resolves to false.
 */
export async function fetchAdminStatus() {
  let result = false;
  try {
    // Probe the server when we hold a token OR the app is in "open" access mode.
    // Open mode auto-trusts the request as the bootstrap admin server-side (the
    // auth middleware), but over a Cloudflare Tunnel there's no JWT to mint
    // (open-login only mints for local + same-network/LAN devices, never a
    // public tunnel exit) — so gating purely on a token would leave the owner
    // unable to see Admin Tools. check-access honours open mode and reports
    // is_admin there, so probe it regardless of token in that case.
    if (getAuthToken() || getAccessMode() === 'open') {
      const res = await fetch('/api/v1/files/check-access', { headers: authHeaders() });
      if (res.ok) {
        const data = await res.json();
        result = !!data.is_admin;
      }
    }
  } catch (e) { /* default false */ }
  _isAdmin = result;
  _adminFetched = true;
  try {
    window.dispatchEvent(new CustomEvent('admin-status-loaded', { detail: { is_admin: result } }));
  } catch {}
  return result;
}

/** No-op: restrictions have been removed. */
export function showRestrictedModal() {}

/** No-op: restrictions have been removed. */
function hideRestrictedModal() {}

/** Show the login form — opens the existing header user dropdown instead of a modal. */
export function showLeftOverlay() {
  hideLeftOverlay();
  const trigger = document.getElementById('top-user-id');
  if (trigger) trigger.click();
}

/** Hide the login overlay — closes the user dropdown and cleans up any leftover overlay. */
function hideLeftOverlay() {
  // Close the user dropdown if open
  const userDropdown = document.getElementById('user-dropdown');
  const menu = document.getElementById('user-dropdown-menu');
  if (userDropdown && menu && menu.style.display === 'block') {
    menu.style.display = 'none';
    userDropdown.classList.remove('open');
  }
  // Remove any leftover overlay element that may have been created before this change
  const overlayEl = document.getElementById('left-login-overlay');
  if (overlayEl && overlayEl.parentNode) {
    overlayEl.parentNode.removeChild(overlayEl);
    const side = document.getElementById('main-panel');
    if (side) side.style.position = '';
  }
}