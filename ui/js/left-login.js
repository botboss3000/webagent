'use strict';

/**
 * Login helpers — the actual sign-in form lives in the header user dropdown.
 * This module provides access-mode helpers and a show/hide that opens/closes
 * that dropdown instead of a full-page modal overlay.
 */

let _accessMode = 'private';
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

/** Always returns true — restrictions have been removed. */
export function isAdmin() {
  return true;
}

/** Always resolves to admin=true. */
export async function fetchAdminStatus() {
  try {
    window.dispatchEvent(new CustomEvent('admin-status-loaded', { detail: { is_admin: true } }));
  } catch {}
  return true;
}

/** No-op: restrictions have been removed. */
export function showRestrictedModal() {}

/** No-op: restrictions have been removed. */
export function hideRestrictedModal() {}

/** Show the login form — opens the existing header user dropdown instead of a modal. */
export function showLeftOverlay() {
  hideLeftOverlay();
  const trigger = document.getElementById('top-user-id');
  if (trigger) trigger.click();
}

/** Hide the login overlay — closes the user dropdown and cleans up any leftover overlay. */
export function hideLeftOverlay() {
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