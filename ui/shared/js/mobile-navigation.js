'use strict';

// Optional compact main navigation. The generated header remains the source of
// truth for page order, visibility, active state, and click behaviour; this
// module presents those same controls in a vertical menu when mobile_mode=true.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

let _started = false;
const USER_MENU_OPENING_EVENT = 'mobile-nav:user-menu-opening';

function _isSourceHidden(source) {
  if (!source || source.hidden || source.style.display === 'none') return true;
  if (source.dataset.value === 'admin-tools' && !document.body.classList.contains('is-admin')) return true;
  const group = source.closest('.main-tab-group');
  return !!(group && group.style.display === 'none');
}

function _addIcon(target, source, fallback) {
  const sourceIcon = source?.querySelector('[data-lucide], svg');
  const iconName = sourceIcon?.getAttribute('data-lucide') || fallback;
  const icon = document.createElement('i');
  icon.className = 'lucide-icon';
  icon.setAttribute('data-lucide', iconName || 'square');
  target.appendChild(icon);
}

function _menuButton(source, label, kind) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'mobile-nav-item';
  button.dataset.mobileNavKind = kind;
  if (source?.dataset.value) button.dataset.value = source.dataset.value;
  if (source?.classList.contains('active')) button.classList.add('active');
  if (source?.classList.contains('has-errors')) button.classList.add('has-errors');
  _addIcon(button, source, kind === 'debug' ? 'bug' : 'cloud-download');
  const text = document.createElement('span');
  text.textContent = label;
  button.appendChild(text);
  if (kind === 'update') {
    const sourceBadge = source?.querySelector('.update-available-badge');
    if (sourceBadge && !sourceBadge.hidden && sourceBadge.textContent) {
      const badge = document.createElement('span');
      badge.className = 'mobile-nav-badge';
      badge.textContent = sourceBadge.textContent;
      button.appendChild(badge);
    }
  }
  button.addEventListener('click', () => {
    source?.click();
    _closeMenu();
  });
  return button;
}

function _closeMenu() {
  const toggle = document.getElementById('mobile-nav-toggle');
  const menu = document.getElementById('mobile-nav-menu');
  if (!toggle || !menu) return;
  menu.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-label', 'Open navigation');
}

function _syncMenu() {
  const items = document.getElementById('mobile-nav-items');
  if (!items || !document.body.classList.contains('mobile-mode')) return;
  items.replaceChildren();

  document.querySelectorAll('#main-tabs .main-tab[data-value]').forEach((source) => {
    if (_isSourceHidden(source)) return;
    const label = source.textContent.trim() || source.dataset.value;
    items.appendChild(_menuButton(source, label, 'tab'));
  });

  const debug = document.getElementById('debug-console-toggle');
  const update = document.getElementById('update-available-btn');
  if (debug || (update && !update.hidden)) {
    const divider = document.createElement('div');
    divider.className = 'mobile-nav-divider';
    divider.setAttribute('role', 'separator');
    items.appendChild(divider);
  }
  if (debug) items.appendChild(_menuButton(debug, 'Debug', 'debug'));
  if (update && !update.hidden) items.appendChild(_menuButton(update, 'Update available', 'update'));

  // icons.js observes newly inserted data-lucide placeholders and renders them
  // on the next animation frame. Do not invoke Lucide from this rebuild path:
  // a renderer callback here can mutate the source strip while it is being
  // mirrored and turn menu synchronization into a feedback loop.
}

function _setMobileNavigationActive(active) {
  const toggle = document.getElementById('mobile-nav-toggle');
  const menu = document.getElementById('mobile-nav-menu');
  const items = document.getElementById('mobile-nav-items');
  const footer = document.getElementById('mobile-nav-footer');
  const tabs = document.getElementById('main-tabs');
  const user = document.getElementById('user-dropdown');
  if (!toggle || !menu || !items || !footer || !tabs) return;

  document.body.classList.toggle('mobile-mode', active);
  toggle.hidden = !active;
  if (active) {
    if (user) footer.appendChild(user);
    _syncMenu();
    return;
  }

  _closeMenu();
  items.replaceChildren();
  if (user && user.parentElement !== tabs) {
    const firstGeneratedControl = tabs.querySelector('[data-generated="1"]');
    tabs.insertBefore(user, firstGeneratedControl);
  }
}

// ── Back-to-main-page button (left of the header, next to the hamburger) ─
// The label next to the nav toggle returns to whatever main panel page was
// opened most recently, riding the SAME saved state tabs.js maintains
// (localStorage 'lastActiveTab', written on every page activation). It shows
// that page's label; with no saved page (or when the saved page is gated /
// hidden / no longer exists) it falls back to the default: "Agents" — the
// combined Agents + Sessions page.
function _resolveBackTarget() {
  let saved = null;
  try { saved = localStorage.getItem('lastActiveTab'); } catch (_) {}
  if (saved) {
    const tabs = document.querySelectorAll('#main-tabs .main-tab[data-value]');
    for (const t of tabs) {
      if (t.dataset.value === saved && !_isSourceHidden(t)) {
        const label = (t.textContent || '').trim() || t.dataset.value;
        return { id: t.dataset.value, label };
      }
    }
  }
  return { id: 'agents', label: 'Agents' };
}

function _updateBackLabel() {
  const label = document.getElementById('mobile-back-main-btn');
  if (!label) return;
  const target = _resolveBackTarget();
  label.textContent = target.label;
}

function _enableMobileNavigation() {
  const toggle = document.getElementById('mobile-nav-toggle');
  const menu = document.getElementById('mobile-nav-menu');
  if (!toggle || !menu) return;

  // Wire the back-to-main-page button: tapping it opens the resolved
  // last-page target (Agents by default), mirroring a user picking that
  // page from the dropdown so visibility gating and mobile chat-hiding
  // behave identically (window.__setMainTab is the canonical page switcher
  // exposed by tabs.js). The span ships aria-hidden="true" (it was a
  // decorative brand) — clear that now that it is interactive.
  const backBtn = document.getElementById('mobile-back-main-btn');
  if (backBtn) {
    const openTarget = () => {
      // Resolve fresh at click time so the target can never drift from the
      // label currently shown (kept in sync by _updateBackLabel below).
      const target = _resolveBackTarget();
      if (typeof window.__setMainTab === 'function') {
        window.__setMainTab(target.id);
      } else {
        const tab = document.querySelector('#main-tabs .main-tab[data-value="' + target.id + '"]');
        if (tab && !tab.hidden) tab.click();
      }
    };
    backBtn.removeAttribute('aria-hidden');
    backBtn.setAttribute('role', 'button');
    backBtn.setAttribute('tabindex', '0');
    backBtn.addEventListener('click', openTarget);
    backBtn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openTarget();
      }
    });
  }

  // Keep the label in step with navigation. Tab clicks and mobile-menu picks
  // funnel through the hidden #main-tab-select's change event (index.html
  // delegates tab clicks to it), which fires AFTER tabs.js's own change
  // listener has written lastActiveTab — so the button re-reads fresh state.
  // Agent-driven switches call window.__setMainTab directly (no change
  // event), so wrap it as well. tabs.js also activates pages directly on
  // 'admin-status-loaded' (deferred Admin Tools restore / first landing) —
  // re-sync then too. Initial state is set by _updateBackLabel() below.
  _updateBackLabel();
  const select = document.getElementById('main-tab-select');
  if (select) select.addEventListener('change', _updateBackLabel);
  const setMainTab = window.__setMainTab;
  if (typeof setMainTab === 'function') {
    window.__setMainTab = function (view) {
      setMainTab(view);
      _updateBackLabel();
    };
  }
  window.addEventListener('admin-status-loaded', _updateBackLabel);

  _setMobileNavigationActive(true);

  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    const opening = menu.hidden;
    menu.hidden = !opening;
    toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
    toggle.setAttribute('aria-label', opening ? 'Close navigation' : 'Open navigation');
    if (opening) _syncMenu();
  });
  menu.addEventListener('click', (event) => event.stopPropagation());

  // Close on outside press/click. Registered in the CAPTURE phase: many app
  // elements (chat message sections, turn gutters, action buttons) call
  // e.stopPropagation() in their own bubble-phase click handlers, which would
  // swallow the click before it reached a bubble-phase document listener and
  // leave the panel stuck open. pointerdown also dismisses on touch before the
  // tap resolves. Mirrors user-panel.js's outside-dismiss pattern.
  const _isOutsideMobileNav = (e) =>
    !menu.contains(e.target) && !toggle.contains(e.target);
  document.addEventListener('pointerdown', (e) => { if (_isOutsideMobileNav(e)) _closeMenu(); }, true);
  document.addEventListener('click', (e) => { if (_isOutsideMobileNav(e)) _closeMenu(); }, true);
  document.addEventListener(USER_MENU_OPENING_EVENT, _closeMenu);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') _closeMenu();
  });

  // Do not mirror the source strip with MutationObservers. The source tabs and
  // their Lucide SVGs change classes/children during boot; rebuilding the menu
  // in response can feed those mutations back into the same rendering cycle
  // and peg the browser. _syncMenu() runs immediately above and every time the
  // menu opens, so visibility, active state, admin state, and update badges are
  // fresh at the point the user can see or use them.
}

export async function initMobileNavigation() {
  if (_started) return;
  _started = true;
  // The header is kept empty (body.header-pending, set in index.html) until
  // this config resolves, so mobile users never see the desktop tab strip
  // flash before the mobile layout applies. Remove the gate on EVERY exit
  // path — success, HTTP failure, and exception alike.
  const headerReady = () => document.body.classList.remove('header-pending');
  try {
    const response = await fetch(apiPath('/api/v1/auth/ui-config'), { headers: authHeaders() });
    if (!response.ok) { headerReady(); return; }
    const config = await response.json();
    // Build the final layout before opening the header gate. On mobile this
    // moves the account control into the sidebar, creates the menu entries,
    // and activates the hamburger before any cached desktop tabs may paint.
    if (config.mobile_mode === true) _enableMobileNavigation();
    headerReady();
  } catch (_) {
    headerReady();
    // The existing carousel is the safe fallback when config is unavailable.
  }
}
