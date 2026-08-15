'use strict';

// Optional compact main navigation. The generated header remains the source of
// truth for page order, visibility, active state, and click behaviour; this
// module presents those same controls in a vertical menu when mobile_mode=true.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

let _started = false;
const MOBILE_VIEWPORT = '(max-width: 800px)';
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

function _enableMobileNavigation() {
  const toggle = document.getElementById('mobile-nav-toggle');
  const menu = document.getElementById('mobile-nav-menu');
  if (!toggle || !menu) return;

  const viewport = window.matchMedia(MOBILE_VIEWPORT);
  const applyViewport = () => _setMobileNavigationActive(viewport.matches);
  applyViewport();
  viewport.addEventListener('change', applyViewport);

  toggle.addEventListener('click', (event) => {
    event.stopPropagation();
    const opening = menu.hidden;
    menu.hidden = !opening;
    toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
    toggle.setAttribute('aria-label', opening ? 'Close navigation' : 'Open navigation');
    if (opening) _syncMenu();
  });
  menu.addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', _closeMenu);
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
    // Reveal + switch layout in the same synchronous block — no intermediate
    // paint, so the desktop strip can't flash in the gap.
    headerReady();
    if (config.mobile_mode === true) _enableMobileNavigation();
  } catch (_) {
    headerReady();
    // The existing carousel is the safe fallback when config is unavailable.
  }
}
