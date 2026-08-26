'use strict';

/**
 * Settings — orchestrator for the JSON-driven config area (Instances →
 * "This device" → Settings).
 *
 * Everything is driven by settings-index.json:
 *   • the landing LIST is rendered from the index's groups/sections,
 *   • each section's PAGE is opened/closed by id (page partials are injected
 *     into #settings-page-slot-* by the partial loader),
 *   • the unified SEARCH opens every matching section page directly,
 *   • the page-assistant pill maps hovered data-pa-area groups to their
 *     app-prompts page key via each page's data-pa-page.
 *
 * Lifecycle mirrors the old app-config/index.js (initSettings /
 * startSettings / stopSettings); the view boots them via settings-view.js
 * (window.initSettings etc., called by instances.js and tabs.js).
 */

import { _qs, _esc } from './utils.js';
import { createPageAssistant } from './page-assistant.js';
import { createSettingsSearch } from './settings-search.js';
import { init as initDataSettings, load as loadDataSettings } from './data-settings/data-settings.js';
import { init as initAgentSettings, load as loadAgentSettings, stop as stopAgentSettings } from './agent-settings/agent-settings.js';
import { init as initAppSettings, load as loadAppSettings } from './app-settings/app-settings.js';
import { init as initBrowserSessions, load as loadBrowserSessions } from './browser-session-management.js';
import { init as initEntitlements, load as loadEntitlements } from './entitlements/entitlements.js';
import { readInstanceCache, writeInstanceCache } from '../instance-cache.js';

const INDEX_URL = '/ui/main-panel/instances/settings/settings-index.json';

let _initialized = false;
let _active = false;
let _pa = null;
let _search = null;
let _index = null;   // settings-index.json payload
let _openId = null;  // section id of the currently-open page (null = list view)
let _initPromise = null;

function _allSections() {
  return (_index?.groups || []).flatMap(group => group.sections || []);
}

function _sectionById(id) {
  return _allSections().find(section => section.id === id) || null;
}

function _pageElement(id) {
  return document.getElementById('settings-page-' + id);
}

// ── View modes: list ↔ page ↔ search ──────────────────────────────────────
function _showList() {
  const list = _qs('settings-list');
  const topbar = _qs('settings-topbar');
  if (list) list.hidden = false;
  if (topbar) topbar.hidden = true;
  for (const section of _allSections()) {
    const page = _pageElement(section.id);
    if (page) page.hidden = true;
  }
  _openId = null;
}

function _showPage(id) {
  const section = _sectionById(id);
  if (!section) return;
  const list = _qs('settings-list');
  const topbar = _qs('settings-topbar');
  const title = _qs('settings-topbar-title');
  if (list) list.hidden = true;
  if (topbar) topbar.hidden = false;
  if (title) title.textContent = section.title;
  for (const s of _allSections()) {
    const page = _pageElement(s.id);
    if (page) page.hidden = (s.id !== id);
  }
  _openId = id;
}

/** Search deactivated — restore whichever surface was showing before. */
function _restoreSurface() {
  if (_openId && _pageElement(_openId)) _showPage(_openId);
  else _showList();
}

/** Open one section page (optionally scrolled/anchored to a group inside it).
 *  A section may live on another section's page (the integration categories
 *  render inside Agent Tools) — the index's `page` / `anchor` fields resolve
 *  where it actually is. */
export function openPage(id, anchor = null) {
  const section = _sectionById(id);
  if (!section) return;
  const pageId = section.page || section.id;
  const targetAnchor = anchor || section.anchor || null;
  _search?.clear();          // leaves search mode; _restoreSurface shows the old surface
  _showPage(pageId);
  requestAnimationFrame(() => {
    let target = null;
    if (targetAnchor) {
      // Integration categories live inside the Agent Tools page as group rows
      // (#ac-group-<id>, built by admin-ability-table.js) — or, for legacy
      // markup, as collapsible category sections (#ac-cat-<id>).
      target = document.getElementById('ac-group-' + targetAnchor)
        || document.getElementById('ac-cat-' + targetAnchor)
        || document.getElementById('ac-group-body-' + targetAnchor);
    }
    if (target) {
      target.scrollIntoView({ block: 'start', behavior: 'smooth' });
    } else {
      const page = _pageElement(pageId);
      const scroller = page?.closest('#app-config-content');
      if (scroller) scroller.scrollTop = 0;
    }
  });
}

/** Back to the landing list. */
export function closePage() {
  _search?.clear();
  _showList();
}

// ── Landing list (rendered from settings-index.json) ──────────────────────
function _renderList() {
  const list = _qs('settings-list');
  if (!list || !_index) return;
  list.innerHTML = _index.groups.map(group => `
    <div class="settings-group">
      <div class="settings-group-label">
        <i data-lucide="${_esc(group.icon || 'circle')}" class="lucide-icon ac-ico-14"></i>
        <span>${_esc(group.label)}</span>
      </div>
      ${group.sections.map(section => `
        <button type="button" class="settings-item"
                data-open="${_esc(section.id)}" data-anchor="${_esc(section.anchor || '')}">
          <span class="settings-item-icon">
            <i data-lucide="${_esc(section.icon || 'circle')}" class="lucide-icon ac-ico-16"></i>
          </span>
          <span class="settings-item-body">
            <span class="settings-item-title">${_esc(section.title)}</span>
            <span class="settings-item-desc">${_esc(section.description || '')}</span>
          </span>
          <span class="settings-item-chev">
            <i data-lucide="chevron-right" class="lucide-icon ac-ico-16"></i>
          </span>
        </button>`).join('')}
    </div>`).join('');

  list.addEventListener('click', (e) => {
    const item = e.target.closest('.settings-item');
    if (!item) return;
    openPage(item.dataset.open, item.dataset.anchor || null);
  });

  // Icons render dynamically after boot, so refresh lucide for this subtree.
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons();
  }
}

// ── Unified search/chat pill ──────────────────────────────────────────────
function _initUnifiedPageAssistant() {
  const input = _qs('ac-unified-pa-input');
  const send = _qs('ac-unified-pa-send');
  const voice = _qs('ac-unified-pa-voice');
  const row = _qs('ac-unified-pa-bar-row');
  const previewBar = _qs('ac-unified-pa-preview-bar');
  const content = _qs('app-config-content');
  if (!input || !send || !content) return;

  _search = createSettingsSearch({
    root: content,
    empty: _qs('settings-search-empty'),
    list: _qs('settings-list'),
    topbar: _qs('settings-topbar'),
    getPages: () => [...content.querySelectorAll('.settings-page')],
    pageMeta: new Map(_allSections().map(section => [section.id, section])),
    onDeactivate: _restoreSurface,
  });

  _pa = createPageAssistant({
    page: 'app_settings',  // default; adapts via resolvePage below
    section: content,
    input,
    // Each .settings-page carries data-pa-page (data_settings | app_settings |
    // agent_settings) so the hovered page maps to its app-prompts page key.
    resolvePage: (areaElement) =>
      areaElement.closest('.settings-page')?.dataset.paPage || 'app_settings',
    fallbackPlaceholder: 'ask how to configure something or customize the app',
    send,
    voice,
    row,
    previewBar,
    onInput: (query) => _search?.filter(query),
    onSend: ({ message, attachmentIds }) => {
      // Import dynamically to avoid circular dependencies.
      import('../../../chat-widget/js/chat-widget.js').then(({ spawnWebagentPageChat }) => {
        spawnWebagentPageChat({
          message,
          attachmentIds,
          page: 'app_config',
          context: _pa ? _pa.currentArea() : null,
        });
      });
    },
  });
  _pa.init();
}

// ── Public lifecycle (mirrors the old app-config exports) ─────────────────
/** Called once at boot — loads the index, wires the section modules, renders
 *  the list and creates the unified search/chat pill. Idempotent: concurrent
 *  callers (tabs.js at boot + instances.js on tab open) share one in-flight
 *  promise so the section modules are never initialised twice. */
export function initSettings() {
  if (_initialized) return _initPromise;
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    // The index is static settings chrome, but it previously blocked every cold
    // Settings-tab open on a network fetch. Use the tenant IDB row immediately
    // and revalidate it in the background; first-ever loads still await fetch.
    _index = readInstanceCache('settings:index:v2') || null;
    const refreshIndex = fetch(INDEX_URL, { cache: 'no-store' })
      .then(async (res) => {
        if (!res.ok) return null;
        const fresh = await res.json();
        writeInstanceCache('settings:index:v2', fresh, 24 * 60 * 60 * 1000);
        return fresh;
      })
      .catch(() => null);
    if (!_index) {
      _index = await refreshIndex;
    } else {
      refreshIndex.then((fresh) => { if (fresh) _index = fresh; });
    }
    if (!_index) {
      console.error('[settings] Failed to load ' + INDEX_URL);
      return;
    }
    initDataSettings();
    initAgentSettings();
    initAppSettings();
    initBrowserSessions();
    initEntitlements();
    _renderList();
    // Page chrome: the topbar's back button returns to the landing list.
    _qs('settings-back-btn')?.addEventListener('click', closePage);
    _initUnifiedPageAssistant();
    _initialized = true;
  })();
  return _initPromise;
}

/** Called when the Settings view becomes active — loads data for all pages. */
export async function startSettings({ preserveScroll = false } = {}) {
  // Instances periodically rebuilds its outer detail card as fleet state changes.
  // The settings container itself is persistent and is re-parented into the new
  // card, so a remount must not reload every section and overwrite in-progress
  // form edits. Mark the lifecycle active before awaiting initialization so two
  // overlapping remounts cannot both pass the guard.
  if (_active) return;
  _active = true;
  try {
    if (!_initialized) await initSettings();
  } catch (error) {
    _active = false;
    throw error;
  }
  // The view may have been stopped while its first initialization was pending.
  if (!_active) return;
  loadDataSettings();
  loadAppSettings();
  loadBrowserSessions();
  loadAgentSettings();
  loadEntitlements();
}

/** Called when leaving the Settings view. */
export function stopSettings() {
  _active = false;
  stopAgentSettings();
}
