'use strict';

/**
 * App Config navigation — tab bar scroll, section switching, features catalog.
 *
 * Exports:
 *   initNav()
 *   showSection(section)
 *   getActiveSection()
 *   setActiveSection(s)
 *   renderFeaturesCatalog()
 *   _showSection(section) — also exported for tab modules to navigate programmatically
 *   _qs, _esc, _fetch    — re-exported from utils for convenience
 */

import { apiPath } from '../../shared/js/config.js';
import { app } from '../../shared/js/state.js';
import { isAdmin } from '../../shared/js/left-login.js';
import { _fetch, _qs, _esc, _setIntStatus } from './utils.js';
import { initStickyNav } from './sticky-nav.js';

// ── Module state ──────────────────────────────────────────────────────────
const _SECTION_KEY = 'appConfig_activeSection';
const _VALID_SECTIONS = ['app-settings', 'agent-settings', 'user-management', 'database', 'optimizer', 'features'];
// Validate the saved section against the current list — a removed tab (e.g. the
// old "git" Git Providers tab, or the "automation"/"events" tabs folded into
// Agent Settings → Automation Engine) must not leave a returning user on a blank panel.
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
  if (section === 'app-settings' && typeof window.__refreshDeploy === 'function') window.__refreshDeploy();
  if (section === 'features') _renderFeaturesCatalog();
  // Fire the per-section lifecycle hook (registered by each tab module)
  if (_sectionHooks[section]) _sectionHooks[section]();
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

// ─────────────────────────────────────────────────────────────────────────
// ── Features (discovery catalog) ──────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

const _FEATURE_STATUS_STYLE = {
  stable:       { label: 'Stable',       color: 'var(--success)' },
  beta:         { label: 'Beta',         color: 'var(--warning)' },
  experimental: { label: 'Experimental', color: 'var(--danger)' },
  unknown:      { label: 'Unmarked',     color: 'var(--fg-4)' },
};
const _FEATURE_CAT_ICON = {
  integration: 'plug', event_source: 'radio', channel: 'message-square',
  connector: 'database', scheduler: 'clock', encryption: 'lock',
  payment: 'credit-card', secrets: 'key-round', storage: 'hard-drive',
  tool: 'wrench', ability: 'sparkles',
};
const _FEATURE_CAT_LABEL = {
  integration: 'Integrations', event_source: 'Event sources', channel: 'Communication channels',
  connector: 'Data connectors', scheduler: 'Scheduler providers', encryption: 'Encryption methods',
  payment: 'Payment processors', secrets: 'Secrets vaults', storage: 'Storage backends',
  tool: 'Tools', ability: 'Abilities',
};
const _FEATURE_CAT_ORDER = ['integration', 'event_source', 'channel', 'connector', 'scheduler', 'encryption', 'payment', 'secrets', 'storage', 'tool', 'ability'];

function _featChip(label, n, color) {
  return `<span style="border:1px solid var(--border);border-radius:10px;padding:2px 9px;color:var(--fg-2);">` +
    `<span style="color:${color || 'var(--fg-3)'};font-weight:600;">${n == null ? 0 : n}</span> ` +
    `<span style="color:var(--fg-4);">${_esc(label)}</span></span>`;
}

function _featureRow(f) {
  const st = _FEATURE_STATUS_STYLE[f.status] || _FEATURE_STATUS_STYLE.unknown;
  const reqs = (f.requires && f.requires.length)
    ? ` <span style="color:var(--fg-4);">· needs ${_esc(f.requires.join(', '))}</span>` : '';
  const desc = (f.summary ? _esc(f.summary) : '<span style="color:var(--fg-4);">no description yet</span>') + reqs;
  const errTag = f.error
    ? ` <span style="color:var(--danger);font-size:10px;" title="${_esc(f.error)}">import error</span>` : '';
  const incIcon = f.included
    ? '<span title="included in the active edition" style="color:var(--success);font-size:13px;">&#9679;</span>'
    : `<span title="${_esc(f.reason || 'excluded')}" style="color:var(--fg-4);font-size:13px;">&#9675;</span>`;
  return `<div class="ac-ability-row"${f.included ? '' : ' style="opacity:.62;"'}>` +
    `<i data-lucide="${_FEATURE_CAT_ICON[f.category] || 'box'}" class="lucide-icon ac-ability-icon"></i>` +
    `<div class="ac-ability-label">` +
      `<div class="ac-ability-name">${_esc(f.display_name)}${errTag}</div>` +
      `<div class="ac-ability-desc">${desc}</div>` +
    `</div>` +
    `<div class="ac-ability-status" style="display:flex;align-items:center;gap:10px;">` +
      `<span style="font-size:10px;font-weight:600;color:${st.color};border:1px solid var(--border);border-radius:10px;padding:1px 8px;white-space:nowrap;">${st.label}</span>` +
      incIcon +
    `</div></div>`;
}

async function _renderFeaturesCatalog() {
  const banner = _qs('ac-features-banner');
  const list = _qs('ac-features-list');
  if (!banner || !list) return;
  if (typeof isAdmin === 'function' && !isAdmin()) {
    banner.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Platform admin access required.</div>';
    list.innerHTML = '';
    return;
  }
  banner.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Loading feature catalog…</div>';
  list.innerHTML = '';

  let data;
  try {
    const res = await _fetch(apiPath('/api/v1/features'));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    data = await res.json();
  } catch (e) {
    banner.innerHTML = `<div style="color:var(--danger);font-size:13px;">Could not load feature catalog: ${_esc(String(e))}</div>`;
    return;
  }

  const c = data.counts || {};
  const bs = c.by_status || {};
  const editions = Object.keys(data.editions || {}).map(_esc).join(' &middot; ');
  banner.innerHTML =
    `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">` +
      `<span style="font-size:13px;color:var(--fg-2);">Active edition</span>` +
      `<span style="font-weight:600;color:var(--accent);font-size:14px;">${_esc(data.active_edition)}</span>` +
      `<span style="margin-left:auto;font-size:11px;color:var(--fg-4);">Report only — nothing is gated yet</span>` +
    `</div>` +
    `<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;font-size:12px;">` +
      _featChip('Total', c.total) +
      _featChip('In this edition', c.included, 'var(--success)') +
      _featChip('Stable', bs.stable || 0, 'var(--success)') +
      _featChip('Beta', bs.beta || 0, 'var(--warning)') +
      _featChip('Experimental', bs.experimental || 0, 'var(--danger)') +
      _featChip('Unmarked', bs.unknown || 0, 'var(--fg-4)') +
      _featChip('Drop-in', c.drop_in) +
    `</div>` +
    `<div style="margin-top:8px;font-size:11px;color:var(--fg-4);">Editions defined: ${editions || '—'}. Set the active edition with the WEBAGENT_EDITION env var.</div>`;

  const feats = (data.features || []).slice();
  const groups = {};
  feats.forEach(f => { (groups[f.category] = groups[f.category] || []).push(f); });
  const cats = Object.keys(groups).sort((a, b) => {
    const ia = _FEATURE_CAT_ORDER.indexOf(a), ib = _FEATURE_CAT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  // Each category is a standard always-open `.ac-category-group` so the shared
  // sticky section navigator (see ./sticky-nav.js — opted in via `ac-stickynav`
  // on this section) pins every category heading and makes it click-to-jump.
  let html = '';
  cats.forEach(cat => {
    const items = groups[cat];
    const dropIn = items.length && items[0].drop_in;
    const tag = dropIn
      ? '<span style="font-size:10px;color:var(--success);border:1px solid var(--border);border-radius:10px;padding:1px 7px;" title="add/remove by dropping or deleting a file">drop-in</span>'
      : '<span style="font-size:10px;color:var(--fg-4);border:1px solid var(--border);border-radius:10px;padding:1px 7px;" title="adding/removing needs a central registry edit today">registry</span>';
    html +=
      `<div class="ac-category-group">` +
        `<div class="ac-category-summary">` +
          `<i data-lucide="${_FEATURE_CAT_ICON[cat] || 'box'}" class="lucide-icon" style="width:15px;height:15px;color:var(--accent);"></i>` +
          `<span class="ac-category-title">${_esc(_FEATURE_CAT_LABEL[cat] || cat)}</span>` +
          `<span class="ac-category-count">${items.length}</span>` +
          tag +
        `</div>` +
        `<div class="ac-category-body">` +
          `<div class="ac-list">${items.map(_featureRow).join('')}</div>` +
        `</div>` +
      `</div>`;
  });
  list.innerHTML = html;
  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
  // Re-measure the sticky navigator now the category headings exist.
  initStickyNav('features');
}


