'use strict';

/**
 * App Config navigation — scroll-based section switching.
 *
 * All three sections (Data, App, Agent Settings) are rendered on a single
 * scrollable page. The tab bar acts as a navigation aid: clicking a tab
 * scrolls to that section, and scrolling updates which tab is highlighted
 * to reflect the current position.
 *
 * Exports:
 *   initNav()
 *   getActiveSection()
 *   _showSection(section) — scrolls to a section (also exported)
 *   updateActiveSectionFromScroll() — call from scroll handler to update tab highlight
 */

import { _qs, _setIntStatus } from './utils.js';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';

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

// Standalone App Config scrolls #app-config-content. Inside Instances,
// Configuration deliberately flows into the page's one .inst-grid scroller.
function _scrollContainer() {
  const content = _qs('app-config-content');
  return content?.closest('.inst-grid.carousel') || content;
}

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

// All sections are always visible (no display:none). This function scrolls
// to a section and updates the tab highlight.
export function _showSection(section) {
  const el = _qs('ac-section-' + section);
  const scroller = _scrollContainer();
  if (el && scroller) {
    const scrollerRect = scroller.getBoundingClientRect();
    const target = scroller.scrollTop + (el.getBoundingClientRect().top - scrollerRect.top);
    scroller.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
  }
  
  _activeSection = section;
  localStorage.setItem(_SECTION_KEY, section);
  _setNavActive(section);
  
  // Fire the per-section lifecycle hook (registered by each tab module)
  if (_sectionHooks[section]) _sectionHooks[section]();
  // Notify broadcast subscribers (lazy data-load on first show)
  _showListeners.forEach(fn => { try { fn(section); } catch (_) {} });
}

// Determine which section is currently visible based on scroll position
function _getSectionFromScroll() {
  const scroller = _scrollContainer();
  if (!scroller) return _activeSection;
  
  const scrollerRect = scroller.getBoundingClientRect();
  const scrollerTop = scrollerRect.top;
  const scrollerHeight = scrollerRect.height;
  
  // Check each section's position relative to the scroller viewport
  for (const section of _VALID_SECTIONS) {
    const el = _qs('ac-section-' + section);
    if (!el) continue;
    
    const rect = el.getBoundingClientRect();
    const elTop = rect.top - scrollerTop;
    const elBottom = rect.bottom - scrollerTop;
    
    // Section is "active" if it's visible in the viewport (at least partially)
    // Use a threshold: section becomes active when its top is above the midpoint
    // of the scroller, or when enough of it is visible
    if (elTop <= scrollerHeight * 0.5 && elBottom > scrollerHeight * 0.2) {
      return section;
    }
  }
  
  // Fallback: if nothing matches, use the first section that's above the current position
  for (const section of _VALID_SECTIONS) {
    const el = _qs('ac-section-' + section);
    if (!el) continue;
    const rect = el.getBoundingClientRect();
    if (rect.bottom > scrollerRect.top) {
      return section;
    }
  }
  
  return _VALID_SECTIONS[0];
}

// Update the active tab based on current scroll position
export function updateActiveSectionFromScroll() {
  const section = _getSectionFromScroll();
  if (section !== _activeSection) {
    _activeSection = section;
    localStorage.setItem(_SECTION_KEY, section);
    _setNavActive(section);
    // Notify listeners of section change (for page-assistant placeholder updates)
    _sectionChangeListeners.forEach(fn => { try { fn(section); } catch (_) {} });
  }
}

// Listeners for section changes (page-assistant uses this to update placeholder)
const _sectionChangeListeners = [];
export function onSectionChange(fn) {
  _sectionChangeListeners.push(fn);
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
    applyRubberBand(tabBar);

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

  // Set up scroll listener for tab highlighting
  const scroller = _scrollContainer();
  if (scroller) {
    let scrollRAF = null;
    scroller.addEventListener('scroll', () => {
      if (scrollRAF) return;
      scrollRAF = requestAnimationFrame(() => {
        scrollRAF = null;
        updateActiveSectionFromScroll();
      });
    }, { passive: true });
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
