'use strict';

/**
 * App Config navigation — tab bar scroll and section switching.
 *
 * Exports:
 *   initNav()
 *   getActiveSection()
 *   _showSection(section) — also exported for tab modules to navigate programmatically
 */

import { _qs, _setIntStatus } from './utils.js';

// ── Module state ──────────────────────────────────────────────────────────
const _SECTION_KEY = 'appConfig_activeSection';
const _VALID_SECTIONS = ['data-settings', 'app-settings', 'agent-settings'];
// Validate the saved section against the current list — a removed tab (e.g. the
// old "git" Git Providers tab, the "automation"/"events" tabs folded into Agent
// Settings → Automation Engine, or the retired "database" Data Management tab
// whose rows moved to Data Settings + App Settings → Advanced) must not leave a
// returning user on a blank panel.
const _savedSection = localStorage.getItem(_SECTION_KEY);
let _activeSection = _VALID_SECTIONS.includes(_savedSection) ? _savedSection : 'agent-settings';

export function getActiveSection() { return _activeSection; }
function setActiveSection(s) { _activeSection = s; }

// ── Section lifecycle hooks ──────────────────────────────────────────────
// Tab modules register a callback that fires each time their section is
// shown, so nav.js doesn't need to know about specific tab internals.
const _sectionHooks = {};
export function registerSectionHook(section, fn) {
  _sectionHooks[section] = fn;
}

// Broadcast subscribers — every registered fn is called with the section id
// each time a section is shown. Unlike registerSectionHook (one callback per
// section), this is a fan-out list, used by the orchestrator to lazy-load a
// tab's data the moment it becomes visible instead of eagerly on panel open.
const _showListeners = [];
export function onSectionShow(fn) { _showListeners.push(fn); }

// ─────────────────────────────────────────────────────────────────────────
// ── Sidebar nav + scroll highlighting ────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

export function _showSection(section) {
  _VALID_SECTIONS.forEach(id => {
    const el = _qs('ac-section-' + id);
    if (el) el.classList.toggle('active', id === section);
  });
  _activeSection = section;
  localStorage.setItem(_SECTION_KEY, section);
  _setNavActive(section);
  if (section === 'app-settings' && typeof window.__refreshRemoteAccess === 'function') window.__refreshRemoteAccess();
  if (section === 'app-settings' && typeof window.__refreshTunnelLink === 'function') window.__refreshTunnelLink();
  // Deploy card + App Access card live on the Data Settings tab — refresh both
  // when that tab shows (each re-reads its own persisted state).
  if (section === 'data-settings' && typeof window.__refreshDeploy === 'function') window.__refreshDeploy();
  if (section === 'data-settings' && typeof window.__refreshAppAccess === 'function') window.__refreshAppAccess();
  if (section === 'data-settings' && typeof window.__refreshSocialAuth === 'function') window.__refreshSocialAuth();
  // Fire the per-section lifecycle hook (registered by each tab module)
  if (_sectionHooks[section]) _sectionHooks[section]();
  // Notify broadcast subscribers (lazy data-load on first show)
  _showListeners.forEach(fn => { try { fn(section); } catch (_) {} });
}

function _setNavActive(section) {
  const tabBar = _qs('app-config-tabs');
  if (!tabBar) return;
  let active;
  tabBar.querySelectorAll('.ac-tab').forEach(t => {
    const isActive = t.dataset.section === section;
    t.classList.toggle('active', isActive);
    if (isActive) active = t;
  });
  if (active) active.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
}

function _initNav() {
  const tabBar   = _qs('app-config-tabs');
  const tabWrap  = _qs('app-config-tabs-wrap');
  const chevLeft = _qs('app-config-tabs-chev-left');
  const chevRight= _qs('app-config-tabs-chev-right');
  if (!tabBar || !tabWrap) return;

  tabBar.querySelectorAll('.ac-tab').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      _showSection(btn.dataset.section);
      btn.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    });
  });

  if (chevLeft && chevRight) {
    const updateChevrons = () => {
      const overflow = tabBar.scrollWidth - tabBar.clientWidth > 1;
      const canLeft  = overflow && tabBar.scrollLeft > 1;
      const canRight = overflow && tabBar.scrollLeft < tabBar.scrollWidth - tabBar.clientWidth - 1;
      tabWrap.classList.toggle('has-overflow', overflow);
      // Mirror the agents carousel: drive both the chevron visibility AND the
      // edge-fade mask (see .ac-tabs-chev in app3.css) off these wrap classes.
      tabWrap.classList.toggle('can-scroll-left', canLeft);
      tabWrap.classList.toggle('can-scroll-right', canRight);
      chevLeft.classList.toggle('visible', canLeft);
      chevRight.classList.toggle('visible', canRight);
    };
    const scrollStep = () => Math.max(80, Math.floor(tabBar.clientWidth * 0.6));
    chevLeft.addEventListener('click', () => tabBar.scrollBy({ left: -scrollStep(), behavior: 'smooth' }));
    chevRight.addEventListener('click', () => tabBar.scrollBy({ left: scrollStep(), behavior: 'smooth' }));
    tabBar.addEventListener('scroll', updateChevrons, { passive: true });

    requestAnimationFrame(() => {
      updateChevrons();
      const active = tabBar.querySelector('.ac-tab.active');
      if (active) active.scrollIntoView({ inline: 'center', block: 'nearest' });
    });
    if (typeof ResizeObserver !== 'undefined') {
      let roPending = false;
      const ro = new ResizeObserver(() => {
        if (roPending) return;
        roPending = true;
        requestAnimationFrame(() => { roPending = false; updateChevrons(); });
      });
      ro.observe(tabBar);
      ro.observe(tabWrap);
    }
    window.addEventListener('resize', updateChevrons);
  }

  // "GitHub tab" links inside the page
  document.querySelectorAll('.ac-tab-link[data-tab]').forEach(el => {
    el.addEventListener('click', () => {
      const tabSel = _qs('main-tab-select');
      if (tabSel) {
        tabSel.value = el.dataset.tab;
        tabSel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  });
}

export function initNav() {
  _initNav();
}


