'use strict';

/**
 * App Settings tab — global feature toggles, Main Panel Pages, Admin Panel
 * Pages, boot animation/startup settings.
 */

import { apiPath } from '../../../shared/js/config.js';
import { app } from '../../../shared/js/state.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js';
import { _fetch, _qs, _esc } from '../utils.js';
// Unified save-status indicator (spinner → ✓ / ⚠-with-revert, centred ON TOP of
// the control). One save language for every toggle / dropdown / input across the
// app config panels — see docs/claude/ui-guidance.md "Unified save-status
// indicator". Replaces the older inline `.agents-autosave-check` tick here.
import { _markSaving, _flashSaveCheck } from '../../../shared/js/dom-utils.js';
// Palette token metadata, preset themes and colour math — the SHARED single
// source (ui/shared/js/appearance-editor.js), also imported by the per-user theme
// editor (ui/shared/js/appearance-me.js) so the two editors can never drift.
// Aliased to the historical underscore names this file already uses.
import {
  TOKEN_META as _TOKEN_META,
  APP_DEFAULTS as _APP_DEFAULTS,
  PRESETS as _PRESETS,
  FOLLOW_ALPHA as _FOLLOW_ALPHA,
  asHex as _asHex,
  splitColor as _splitColor,
  combineColor as _combineColor,
} from '../../../shared/js/appearance-editor.js';
import { registerSectionHook } from '../nav.js';
import { initStickyNav } from '../sticky-nav.js';
import { openIconPicker } from '../../../shared/js/icon-picker.js';
import { spawnWebagentPageChat } from '../../../chat-widget/js/chat-widget.js';
// Page-assistant pill context engine (shared with Agent Settings). It owns the
// hovered-area placeholder swap, the typewriter suggestions, Tab-to-fill and the
// context-wrapped message; here it runs in COMPOSER mode (it also owns this page's
// static #ac-pa-* pill's send/Enter/voice/uploads). See ../page-assistant.js.
import { createPageAssistant } from '../page-assistant.js';
// App Functions table — the background app services (Session Namer, Context
// Control compaction, Render Recorder) split out of the ability tables. Rendered
// from the ability catalog's `app_functions` list. See ./app-functions.js.
import { initAppFunctions } from './app-functions.js';
// LIVE theme switch (Light · Dark · System) in the Design header reuses the
// user-dropdown's theme logic so the two controls never diverge. setTheme owns
// apply + persist; highlightThemeOption syncs every `.theme-option` on the page.
import { setTheme, getSavedTheme, highlightThemeOption } from '../../../shared/js/user-panel.js';
// The Deploy card moved to its own "Data Settings" tab (renamed "Deployment") —
// its wiring now lives in ../data-settings/data-settings.js, not here.

// ───────────────────────────────────────────────────────────────────────────

function _initAppSettings() {
  const saveBtn = _qs('ac-app-settings-save');
  if (saveBtn) saveBtn.addEventListener('click', _saveAppSettings);
  const gpBtn = _qs('ac-global-prompt-save');
  if (gpBtn) gpBtn.addEventListener('click', _saveGlobalPrompt);
  _initBootAnimation();
  _initBootMobileMode();
  // Startup & Boot rows are now STANDARD config rows — the toggle / dropdown
  // sits inline on the right, no expandable body — so they need no row wiring
  // here (the selects/checkboxes are wired by their _initBoot* handlers).
  // Remote Access moved to the Data Settings tab (below the Database card) — its
  // two expandable rows are now wired in ../data-settings/data-settings.js.
  // Advanced group — the two expandable rows moved here from the retired Data
  // Management tab (Config Files + Stream Buffer). Same head-click expand wiring;
  // their bodies' control ids are owned by ui/shared/js/storage.js.
  _wireBootRow('ac-row-configfiles', null);
  _wireBootRow('ac-row-streambuffer', null);
  // The Deploy card's rows + wiring moved to the Data Settings tab
  // (../data-settings/data-settings.js).
  _initBootTooltip();
  _initBootSplash();
  _initAllowUserAppearance();
  _wireBootRow('ac-row-multitenant', null);
  _initMultiTenant();
  // Advanced row inside Model Settings — expandable compact row with checkboxes
  _wireAdvancedRow();
  _initMainPanelPages();
  _initAdminPanelPages();
  _initAppFunctions();
  _initAppearance();
  _initPageAssistant();
}

// App Functions table — background app services (not agent abilities). Rendered
// from the ability catalog's `app_functions` list into #ac-app-functions-list.
function _initAppFunctions() {
  initAppFunctions().catch(() => { /* non-fatal — the rest of the page still loads */ });
}

// -- Page-assistant chat pill --------------------------------------------------
// The floating "advanced chat pill" at the bottom of App Settings. Typing hints,
// the typewriter suggestions, Tab-to-fill and the context-wrapped message are all
// owned by the shared page-assistant engine (../page-assistant.js), which here
// runs in COMPOSER mode so it also owns this page's static #ac-pa-* pill's
// send / Enter / voice / file-uploads. The prompt it hands WebAgent changes with
// the section the mouse is over (the data-pa-area groups in app-settings.html);
// on send it opens a floating WebAgent (the manager) chat via spawnWebagentPageChat.
// Text catalog (placeholders + idea hints + prompts) lives in
// app/defaults/app-prompts.json -> page_assistants.app_settings (edit + reload).
let _pa = null;

function _initPageAssistant() {
  if (_pa) return;
  _pa = createPageAssistant({
    page: 'app_settings',
    section: _qs('ac-section-app-settings'),
    input: _qs('ac-pa-input'),
    send: _qs('ac-pa-send'),
    voice: _qs('ac-pa-voice'),
    row: _qs('ac-pa-bar-row'),
    previewBar: _qs('ac-pa-preview-bar'),
    pending: [],
    wireComposer: true,
    onSend: spawnWebagentPageChat,
  });
  _pa.init();
}

// Wire one Startup & Boot row: clicking the head expands/collapses it, and the
// right-hand status shows the currently selected option's label.
function _wireBootRow(rowId, selectId) {
  const row = _qs(rowId);
  if (!row) return;
  const head = row.querySelector(':scope > .ac-ability-row');
  head?.addEventListener('click', (e) => {
    // Don't toggle when interacting with the control itself.
    if (e.target.closest('select, input, button, a, label')) return;
    row.classList.toggle('expanded');
  });
}

// Wire the model action row. Two independent icon buttons; the row itself is
// NOT clickable:
//  • the Advanced (sliders) button toggles the advanced-options body below it;
//  • the `+` button toggles the add-new-model configurator form above the row
//    (the table's `ac-cfg-collapsed` class hides the 4 configurator rows).
function _wireAdvancedRow() {
  const row = _qs('ac-model-advanced-row');
  if (!row) return;
  const advToggle = _qs('ac-model-advanced-toggle');
  advToggle?.addEventListener('click', () => { row.classList.toggle('expanded'); });
  const addToggle = _qs('ac-model-add-toggle');
  const table = _qs('ac-model-config');
  addToggle?.addEventListener('click', () => {
    table?.classList.toggle('ac-cfg-collapsed');
  });
}

// ── Main Panel Pages & Admin Panel Pages (catalog-driven) ──────────────────
// SISTER-PANEL: AGENT-ABILITY-TABLE — these two lists are sister panels and must
// stay mirrored in design. Both render from the SAME page catalog
// (GET /api/v1/pages/catalog, built from drop-in ui/<page>/page.json + admin
// overrides) and persist admin changes SERVER-SIDE (data/config/
// main-panel-pages.json + admin-panel-pages.json) so every user sees the admin's
// chosen order, names, icons and visibility. There is NO hardcoded page list
// here anymore — dropping a ui/<page>/page.json folder makes the page appear.

let _pageCatalog = { main: [], admin: [] };

async function _fetchPageCatalog(force) {
  try {
    if (force && typeof window.__reloadPagesCatalog === 'function') {
      _pageCatalog = await window.__reloadPagesCatalog();
    } else if (typeof window.__loadPagesCatalog === 'function') {
      _pageCatalog = await window.__loadPagesCatalog();
    } else {
      const r = await fetch('/api/v1/pages/catalog', { cache: 'no-store' });
      _pageCatalog = await r.json();
    }
  } catch (e) {
    console.warn('app-settings: page catalog fetch failed', e);
  }
  if (!_pageCatalog || !Array.isArray(_pageCatalog.main)) _pageCatalog = { main: [], admin: [] };
  return _pageCatalog;
}

async function _savePagesOrder(scope, orderedIds) {
  const pages = {};
  orderedIds.forEach((id, idx) => { pages[id] = idx; });
  try {
    await fetch(`/admin/integrations/pages/${scope}/order`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages }),
    });
  } catch (e) { console.warn('save page order failed', e); }
}

async function _savePageOverride(scope, id, body) {
  try {
    const res = await fetch(`/admin/integrations/pages/${scope}/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return res.ok;
  } catch (e) { console.warn('save page override failed', e); return false; }
}

// Re-apply the live UI for a scope after a change: main rebuilds the header tab
// strip from the fresh catalog; admin re-applies the sidebar strip order/visibility.
function _applyLive(scope) {
  if (scope === 'main') {
    if (typeof window.__buildHeader === 'function') {
      try { window.__buildHeader(_pageCatalog.main); } catch (_) {}
    }
  } else {
    if (typeof window.__applyAdminPanelOrder === 'function') {
      try { window.__applyAdminPanelOrder(); } catch (_) {}
    }
  }
}

const _PANEL_CFG = {
  main:  { listId: 'ac-main-panel-list',  where: 'header' },
  admin: { listId: 'ac-admin-panel-list', where: 'sidebar' },
};

// 3-state page visibility, mirroring the abilities table's `.ac-tri` look AND
// feel: clicking a third of the track jumps the knob to that detent (left → Off,
// middle → Signed-in only, right → Always visible) rather than cycling.
//   • off  — hidden from the strip for everyone
//   • auth — shown only once the visitor is signed in
//   • all  — always visible (the default)
// A LOCKED page (Admin Tools / Admin Configuration) can never be turned off — it
// would lock the user out of the app's config — so its toggle skips "off" and
// cycles only between Signed-in only and Always visible (`_VIS_STATES_LOCKED`).
const _VIS_STATES = ['off', 'auth', 'all'];
const _VIS_STATES_LOCKED = ['auth', 'all'];

function _visState(page) {
  // Prefer the catalog's canonical `visibility`; fall back to the legacy
  // boolean `hidden` for an older catalog payload.
  if (page.visibility === 'auth' || page.visibility === 'off' || page.visibility === 'all') {
    return page.visibility;
  }
  return page.hidden ? 'off' : 'all';
}

function _visTitle(state, where, locked) {
  const tail = locked ? ' — this page can’t be fully hidden' : '';
  if (state === 'off')  return `Off — hidden from the ${where} for everyone`;
  if (state === 'auth') return `Signed-in only — shown in the ${where} once signed in${tail}`;
  return `Always visible in the ${where}${tail}`;
}

function _renderPanelList(scope) {
  const cfg = _PANEL_CFG[scope];
  const list = _qs(cfg.listId);
  if (!list) return;

  const pages = (_pageCatalog[scope] || []);
  list.innerHTML = '';

  pages.forEach((page) => {
    const id = page.id;
    const isLocked = !!page.locked;

    const item = document.createElement('div');
    item.className = 'ac-main-panel-item ac-ability-row' + (isLocked ? ' ac-mp-locked' : '');
    item.draggable = !isLocked;
    item.dataset.pageId = id;

    // Drag handle
    const handle = document.createElement('span');
    handle.className = 'ac-mp-drag-handle';
    handle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/></svg>';

    // Icon button — opens the shared searchable icon picker
    const iconBtn = document.createElement('button');
    iconBtn.type = 'button';
    iconBtn.className = 'ac-ability-icon ac-mp-icon-btn';
    iconBtn.title = 'Change icon';
    iconBtn.innerHTML = `<i data-lucide="${_esc(page.icon || 'square')}" class="lucide-icon" style="width:18px;height:18px;"></i>`;
    iconBtn.addEventListener('click', async () => {
      const chosen = await openIconPicker({ current: page.icon || '', title: 'Choose a page icon' });
      if (!chosen || chosen === page.icon) return;
      // Optimistic + in-place (no full re-render) so the save overlay survives on
      // this button — spinner → ✓ over the icon, or ⚠ on failure (icon unchanged).
      _markSaving(iconBtn);
      const ok = await _savePageOverride(scope, id, { icon: chosen });
      if (ok === false) { _flashSaveCheck(iconBtn, false); return; }
      page.icon = chosen;
      iconBtn.innerHTML = `<i data-lucide="${_esc(chosen)}" class="lucide-icon" style="width:18px;height:18px;"></i>`;
      if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
      await _fetchPageCatalog(true);
      _applyLive(scope);
      _flashSaveCheck(iconBtn, true);
    });

    // Editable name
    const label = document.createElement('div');
    label.className = 'ac-ability-label';
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'ac-mp-name-input';
    nameInput.value = page.label || id;
    nameInput.setAttribute('aria-label', 'Page name');
    const _commitName = async () => {
      const v = nameInput.value.trim();
      if (v === (page.label || '')) return;
      // Optimistic + in-place (no full re-render) so the overlay survives on the
      // name field — spinner → ✓, or ⚠ + revert to the prior name on failure.
      _markSaving(label);
      const ok = await _savePageOverride(scope, id, { label: v });
      if (ok === false) {
        nameInput.value = page.label || id;   // revert
        _flashSaveCheck(label, false);
        return;
      }
      page.label = v;
      await _fetchPageCatalog(true);
      _applyLive(scope);
      _flashSaveCheck(label, true);
    };
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); nameInput.blur(); }
    });
    nameInput.addEventListener('blur', _commitName);
    // Don't let typing/selecting in the field start a drag.
    nameInput.addEventListener('mousedown', (e) => e.stopPropagation());
    label.appendChild(nameInput);

    // 3-state visibility toggle (Off · Signed-in only · Always visible) — built
    // to mirror the abilities table's `.ac-tri`. A locked page (Admin Tools /
    // Admin Configuration) stays interactive but skips the "off" (leftmost)
    // position — turning it off would hide the only door to the app's config — so
    // it cycles only between Signed-in only and Always visible. The unusable off
    // zone is greyed out via the `ac-mp-tri-nolock` class (CSS in app3.css).
    const states = isLocked ? _VIS_STATES_LOCKED : _VIS_STATES;
    let curVis = _visState(page);
    if (isLocked && curVis === 'off') curVis = 'all';  // defensive: locked is never off
    const tri = document.createElement('button');
    tri.type = 'button';
    tri.className = 'ac-mp-tri' + (isLocked ? ' ac-mp-tri-nolock' : '');
    tri.dataset.state = curVis;
    tri.setAttribute('aria-label', 'Page visibility');
    tri.title = _visTitle(curVis, cfg.where, isLocked);
    tri.innerHTML = '<span class="ac-mp-tri-knob"></span>';
    tri.addEventListener('click', async (e) => {
      // Pick the detent by WHERE the click landed — left/middle/right third of the
      // track maps to off/auth/all — exactly like the abilities `.ac-tri`, instead
      // of blindly cycling to the next state. The track has three knob positions
      // (off 2px · auth 20px · all 38px in a 54px pill), so split it into thirds.
      const prev = tri.dataset.state;
      const r = tri.getBoundingClientRect();
      const frac = (e.clientX - r.left) / r.width;
      let next = frac < 1 / 3 ? 'off' : (frac < 2 / 3 ? 'auth' : 'all');
      // A locked page can't sit in the blocked "off" zone — a click there snaps to
      // the nearest allowed detent (Signed-in only) rather than turning it off.
      if (!states.includes(next)) next = states[0];
      // Clicking the zone the knob is already in is a no-op (nothing to save).
      if (next === prev) return;
      // Optimistic, like the abilities `.ac-tri`: move the knob immediately and
      // keep it there. We do NOT re-render the whole list (that would rebuild this
      // row from the catalog and snap the knob back before the save lands); only a
      // genuine save FAILURE reverts. Keep the in-memory page in sync so a later
      // full re-render agrees.
      tri.dataset.state = next;
      tri.title = _visTitle(next, cfg.where, isLocked);
      page.visibility = next;
      _markSaving(tri);
      const ok = await _savePageOverride(scope, id, { visibility: next });
      if (ok === false) {
        tri.dataset.state = prev;
        tri.title = _visTitle(prev, cfg.where, isLocked);
        page.visibility = prev;
        _flashSaveCheck(tri, false);
        return;
      }
      // Refresh the cached catalog + live header/sidebar strip from the server,
      // but leave this list's DOM as-is so the knob the admin just set stays put.
      await _fetchPageCatalog(true);
      _applyLive(scope);
      _flashSaveCheck(tri, true);
    });

    item.appendChild(handle);
    item.appendChild(iconBtn);
    item.appendChild(label);
    item.appendChild(tri);

    // Drag-and-drop reorder
    if (!isLocked) {
      item.addEventListener('dragstart', (e) => {
        item.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', id);
      });
      item.addEventListener('dragend', () => {
        item.classList.remove('dragging');
        list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
      });
    }
    item.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
      item.classList.add('drag-over');
    });
    item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
    item.addEventListener('drop', async (e) => {
      e.preventDefault();
      item.classList.remove('drag-over');
      const fromId = e.dataTransfer.getData('text/plain');
      const toId = id;
      if (!fromId || fromId === toId) return;

      // Build the new id order from the current rendered order.
      const ids = pages.map(p => p.id);
      const fromIdx = ids.indexOf(fromId);
      if (fromIdx < 0 || ids.indexOf(toId) < 0) return;
      ids.splice(fromIdx, 1);
      const toIdx = ids.indexOf(toId);
      ids.splice(toIdx, 0, fromId);

      await _savePagesOrder(scope, ids);
      await _fetchPageCatalog(true);
      _renderPanelList(scope);
      _applyLive(scope);
    });

    list.appendChild(item);
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

async function _initMainPanelPages() {
  await _fetchPageCatalog(false);
  _renderPanelList('main');
}

async function _initAdminPanelPages() {
  await _fetchPageCatalog(false);
  _renderPanelList('admin');
}

// Re-apply the admin sidebar strip from the catalog. The strip's view-switch
// buttons are now BUILT from the `admin` catalog by __buildAdminStrip (in
// header-build.js) — the same drop-in source the main header uses — so order,
// label, icon and hidden all flow from one place and a freshly-dropped admin
// view appears with no edits here. Exposed so files.js can (re)build the strip
// on Admin Tools init and so an App Settings edit can refresh it live.
function _applyAdminPanelOrder() {
  // Prefer the global boot catalog (set by header-build.js) so this also works
  // when called from files.js before App Settings has been opened; fall back to
  // the panel's own copy.
  const adminPages = (window.__pagesCatalog && Array.isArray(window.__pagesCatalog.admin))
    ? window.__pagesCatalog.admin
    : (_pageCatalog.admin || []);
  if (typeof window.__buildAdminStrip === 'function') {
    try { window.__buildAdminStrip(adminPages); } catch (_) {}
  }
}
window.__applyAdminPanelOrder = _applyAdminPanelOrder;

// In-app tour / hint bubbles master switch — an app-WIDE setting saved
// server-side (hints_enabled in app-settings.json), mirroring the Welcome screen
// switch. The checkbox state is loaded in _loadAppSettings; flipping it autosaves
// via the merge-friendly POST /admin/settings/app, updates the tutorial module's
// cached gate (webagent.hintsEnabled.v1) and dispatches tutorial-admin-changed so
// the live tour shows/hides at once. Each user's own "show app tour" preference
// (Manage Account) layers on top; that account row hides when this is off.
function _initBootTooltip() {
  const chk = _qs('ac-boot-show-tooltips');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hints_enabled: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Keep the tutorial module's cached gate + live tour in sync.
      try { localStorage.setItem('webagent.hintsEnabled.v1', chk.checked ? '1' : '0'); } catch (_) {}
      document.dispatchEvent(new CustomEvent('tutorial-admin-changed'));
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save hints_enabled failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Welcome landing page master switch — an app-WIDE setting saved server-side
// (splash_enabled in app-settings.json), unlike the per-browser boot rows above.
// When on, app/main.py serves the crawlable welcome landing at the front door (/)
// to new visitors and routes the app to /app; when off, the app loads directly at
// /. The checkbox state is loaded in _loadAppSettings; flipping it autosaves via
// the merge-friendly POST /admin/settings/app (server keeps every other key). The
// per-device "show for me" preference is separate (Manage Account + the landing's
// own checkbox, both via window.WA_SPLASH → the wa_seen_splash skip cookie).
// Whether the welcome-page folder actually has content to render. The front door
// (app/main.py → _render_landing_page) checks the SAME file before serving the
// landing, so a build that stripped ui/splash/splash-page/ has nothing to enable.
// We probe that file (HEAD) so the toggle can sit OFF and explain why, instead of
// being a switch with nothing behind it.
let _splashContentAvailable = true;

async function _checkSplashContent() {
  try {
    const r = await fetch('/ui/splash/splash-page/splash-page.html', { method: 'HEAD', cache: 'no-store' });
    _splashContentAvailable = r.ok;
  } catch (_) {
    _splashContentAvailable = true;   // fail-open: a probe glitch shouldn't lock the toggle
  }
  return _splashContentAvailable;
}

// Force the toggle off + show an inline note when there's no welcome page to show.
function _reflectSplashContent(chk) {
  const row = chk.closest('.ac-ability-row');
  if (!row) return;
  let note = row.querySelector('.ac-splash-missing-note');
  if (_splashContentAvailable) { if (note) note.remove(); return; }
  chk.checked = false;
  if (!note) {
    note = document.createElement('div');
    note.className = 'ac-ability-desc ac-splash-missing-note';
    note.style.marginTop = '4px';
    note.textContent = '⚠ No welcome page found — add content to ui/splash/splash-page to enable this.';
    (row.querySelector('.ac-ability-label') || row).appendChild(note);
  }
}

function _initBootSplash() {
  const chk = _qs('ac-boot-splash-enabled');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    // Nothing to turn on: the welcome-page folder was stripped from this build.
    // Revert and tell the admin exactly what to add.
    if (chk.checked && !_splashContentAvailable) {
      chk.checked = false;
      _flashSaveCheck(wrap, false, 'Add a welcome page to ui/splash/splash-page first');
      return;
    }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ splash_enabled: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save splash_enabled failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Allow per-user themes master switch — an app-WIDE setting saved server-side
// (allow_user_appearance in app-settings.json), mirroring the Welcome screen /
// Show tooltips switches. The checkbox state is loaded in _loadAppSettings;
// flipping it autosaves via the merge-friendly POST /admin/settings/app (server
// keeps every other key). When on, /api/v1/auth/ui-config layers each signed-in
// user's saved overrides on top of the global theme and the account page
// (ui/shared/js/appearance-me.js) shows its "My appearance" editor.
function _initAllowUserAppearance() {
  const chk = _qs('ac-allow-user-appearance');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allow_user_appearance: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save allow_user_appearance failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Multi-tenant data master switch — app-WIDE server-side setting (multi_tenant
// in app-settings.json), mirroring the Allow-per-user-themes switch above.
// Flipping it autosaves via the merge-friendly POST /admin/settings/app. When on,
// each user's interaction data routes to their own database (they connect one from
// their account page via ui/shared/js/tenant-db.js); backend routing lives in
// app/db/router.py + app/db/tenant.py. Off = the single shared database (default).
function _reflectMultiTenant(chk) {
  const lbl = _qs('ac-multitenant-label');
  const badge = _qs('ac-multitenant-badge');
  const on = !!chk.checked;
  if (lbl) lbl.textContent = on ? 'On — each user uses their own database' : 'Off — single shared database';
  if (badge) badge.textContent = on ? 'On' : 'Off';
}

function _initMultiTenant() {
  const chk = _qs('ac-multitenant');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ multi_tenant: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _reflectMultiTenant(chk);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save multi_tenant failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _reflectMultiTenant(chk);
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Allowed values for the boot dropdowns. Declared BEFORE the init functions
// that reference them so the symbols are defined regardless of load/eval order
// (strict-mode declaration-before-use discipline — see CLAUDE.md / Codebase
// Admin skill). Declaring these after _initBootAnimation previously caused a
// temporal-dead-zone ReferenceError when this code was concatenated into a
// single bundle that ran initAppConfig before reaching the line.
const _BOOT_ANIM_ALLOWED = ['chat-slide-right', 'chat-slide-left', 'no-animation'];
const _BOOT_MOBILE_ALLOWED = ['chat-first', 'memory'];

function _initBootAnimation() {
  const sel = _qs('ac-boot-animation');
  if (!sel) return;
  const ctl = sel.closest('.ac-config-control');
  let saved = localStorage.getItem('bootAnimation');
  if (!_BOOT_ANIM_ALLOWED.includes(saved)) saved = 'chat-slide-right';
  sel.value = saved;
  sel.addEventListener('change', () => {
    const val = _BOOT_ANIM_ALLOWED.includes(sel.value) ? sel.value : 'chat-slide-right';
    // Per-browser save (localStorage, synchronous) — confirm with the same
    // overlay so every dropdown reads the same way; the held spinner makes the
    // instant write perceptible before the ✓.
    _markSaving(ctl);
    localStorage.setItem('bootAnimation', val);
    _flashSaveCheck(ctl, true);
  });
}

function _initBootMobileMode() {
  const sel = _qs('ac-boot-mobile-mode');
  if (!sel) return;
  const ctl = sel.closest('.ac-config-control');
  let saved = localStorage.getItem('bootMobileMode');
  if (!_BOOT_MOBILE_ALLOWED.includes(saved)) saved = 'chat-first';
  sel.value = saved;
  sel.addEventListener('change', () => {
    const val = _BOOT_MOBILE_ALLOWED.includes(sel.value) ? sel.value : 'chat-first';
    _markSaving(ctl);
    localStorage.setItem('bootMobileMode', val);
    _flashSaveCheck(ctl, true);
  });
}

async function _loadAppSettings(data) {
  try {
    // Reuse the caller's pre-fetched payload when provided (load() fetches it
    // once for both consumers); only fetch here when called standalone.
    if (!data) {
      const res = await _fetch(apiPath('/admin/settings/app'));
      if (!res.ok) return;
      data = await res.json();
    }
    const cb = _qs('ac-extend-llm-to-agents');
    if (cb) cb.checked = data.extend_llm_to_agents !== false;
    const sp = _qs('ac-boot-splash-enabled');
    if (sp) {
      sp.checked = data.splash_enabled !== false;
      await _checkSplashContent();
      _reflectSplashContent(sp);
    }
    const tt = _qs('ac-boot-show-tooltips');
    if (tt) tt.checked = data.hints_enabled !== false;
    const au = _qs('ac-allow-user-appearance');
    if (au) au.checked = data.allow_user_appearance === true;
    const mt = _qs('ac-multitenant');
    if (mt) { mt.checked = data.multi_tenant === true; _reflectMultiTenant(mt); }
    const gp = _qs('ac-global-system-prompt');
    if (gp) gp.value = data.global_system_prompt || '';
  } catch (e) {
    console.warn('app-config: could not load app settings', e);
  }
}

// ── Appearance: combined theme palette · background · fonts ────────────────
// One panel, two mirrored theme cards (Light + Dark). Each card holds: preset
// chips (one-click full palettes), the animated-background select, and the full
// palette of colour swatches. A shared card holds the theme-independent fonts +
// border thickness. Every control auto-saves per theme to /admin/settings/app
// (merged), previews live in the admin's own window, and applies app-wide for
// every visitor via the public /api/v1/auth/ui-config:
//   • palette + border + fonts → ui/shared/js/appearance.js (WA_APPEARANCE)
//   • background                → ui/background/_engine/manager.js (WA_BG)
// All keys can also be hand-edited in data/config/app-settings.json.
//
// The swatch order + which token each maps to is the SINGLE SOURCE OF TRUTH in
// appearance.js (window.WA_APPEARANCE.tokens); here we only attach a friendly
// label + icon. Saved-value key for a token+theme is `<base>_<theme>` (border's
// is the historical `border_color_<theme>` — same shape since base = border_color).

// _TOKEN_META (label + icon per token), _APP_DEFAULTS (per-theme palette
// defaults) and _FOLLOW_ALPHA (the blank-bubble panel tint) now live in the
// SHARED ui/shared/js/appearance-editor.js and are imported at the top of this
// file (aliased to these names). Edit them there so the admin + per-user editors
// stay identical.

// The colour a `follow` token (user/agent bubble, chat pill) actually shows when
// left blank: the LIVE Panel swatch at the theme's bubble alpha. Reads the built
// Panel swatch input when present (so it tracks a drag in progress), else the
// saved value, else the design default — mirroring the index.css color-mix on
// --bg-elev so the table never shows a frozen stock colour.
function _followDefault(theme) {
  const sw = _qs(`ac-sw-${theme}-surface_panel`);
  const panelHex = sw
    ? sw.value
    : _asHex(_appValues[_keyFor('surface_panel', theme)], _APP_DEFAULTS[theme].surface_panel);
  return _combineColor(panelHex, _FOLLOW_ALPHA[theme]);
}

// The effective default for any token under a theme — what the swatch shows when
// the saved value is blank. Follow tokens derive from the live Panel colour;
// every other token uses its design-system default.
function _effectiveDefault(base, theme) {
  const meta = _TOKEN_META[base];
  if (meta && meta.follow) return _followDefault(theme);
  return _APP_DEFAULTS[theme][base] || '#000000';
}

// Keep the blank (theme-following) bubble/pill swatches in sync with the Panel
// swatch — called when Panel changes so the table stays truthful. A pinned
// bubble (non-blank saved value) is left untouched.
function _syncFollowSwatches(theme) {
  _tokenOrder().forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta || !meta.follow) return;
    if (_appValues[_keyFor(base, theme)]) return;   // pinned → leave alone
    const split = _splitColor(_followDefault(theme), '#000000');
    const sw = _qs(`ac-sw-${theme}-${base}`);
    if (sw) sw.value = split.hex;
    const al = _qs(`ac-al-${theme}-${base}`);
    if (al) al.value = split.alpha;
  });
}

// _PRESETS (the one-click preset themes) now lives in the SHARED
// ui/shared/js/appearance-editor.js and is imported at the top of this file. Add
// or edit a preset there so the admin + per-user editors offer the same chips.
// (The admin can still snapshot a custom theme via "Add theme" — see
// _addCustomTheme below; custom themes are admin-only and stored in app settings,
// NOT in the shared preset list.)

// Module state: the live appearance values (loaded from the server), the
// background catalog, and the per-theme background choice.
let _appValues = {};                          // every appearance key → value
let _bgCatalog = [];
let _bgChoice = { dark: 'stargaze', light: 'bullet-grid' };
let _customThemes = [];                        // user-saved theme chips (both modes)

// Saved-value key for a token base under a theme.
function _keyFor(base, theme) { return `${base}_${theme}`; }

// The colour helpers _asHex / _splitColor / _combineColor now live in the SHARED
// ui/shared/js/appearance-editor.js and are imported at the top of this file
// (aliased to these names), so the per-user editor uses the identical maths.

// Live-preview a patch in the admin's own window (no save).
function _previewAppearance(patch) {
  try {
    if (window.WA_APPEARANCE && typeof window.WA_APPEARANCE.setOverrides === 'function') {
      window.WA_APPEARANCE.setOverrides(patch);
    }
  } catch (_) { /* preview is best-effort */ }
}

// Persist a patch (merged server-side) and confirm the save with the unified
// on-top overlay over `ctrl` (the control's container — a swatch row's
// `.ac-theme-row-ctl`, a select's `.ac-config-control`, or the presets host):
// spinner → green ✓, or orange ⚠ on failure. Appearance has no clean prior value
// to revert to here (the live preview already applied), so a failure just flags
// ⚠ — the next successful save re-syncs the server.
async function _saveAppearance(patch, ctrl) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  Object.assign(_appValues, patch);
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (ctrl) _flashSaveCheck(ctrl, true);
  } catch (e) {
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
  }
}

// Build (or rebuild) one theme row's expanded body: preset chips + background
// row + a colour swatch per palette token, all inside its .ac-ability-body
// (the shared ability-table expandable region).
function _buildThemeCard(theme) {
  const body = _qs('ac-theme-body-' + theme);
  if (!body) return;
  body.innerHTML = '';

  // ── Preset chips (top of the expanded body) ──
  const presetsHost = document.createElement('div');
  presetsHost.className = 'ac-theme-presets';
  presetsHost.id = 'ac-presets-' + theme;
  body.appendChild(presetsHost);
  _PRESETS.concat(_customThemes).forEach((p) => {
    const isCustom = typeof p.id === 'string' && p.id.indexOf('custom-') === 0;
    const merged = { ..._APP_DEFAULTS[theme], ...(p[theme] || {}) };
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ac-preset-chip' + (isCustom ? ' ac-preset-custom' : '');
    chip.dataset.preset = p.id;
    chip.innerHTML =
      `<span class="ac-preset-dot" style="background:${_esc(_asHex(merged.accent, '#888'))};"></span>${_esc(p.name)}` +
      (isCustom ? '<span class="ac-preset-del" title="Delete theme" aria-label="Delete theme">&times;</span>' : '');
    chip.addEventListener('click', (e) => {
      if (e.target && e.target.classList.contains('ac-preset-del')) {
        e.stopPropagation();
        _deleteCustomTheme(p.id);
        return;
      }
      _applyPreset(theme, p);
    });
    presetsHost.appendChild(chip);
  });
  // "Add theme" — snapshot the current colours (both modes) as a new chip.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'ac-preset-add';
  addBtn.title = 'Save the current colours as a new theme';
  addBtn.innerHTML = '<i data-lucide="plus" class="lucide-icon" style="width:13px;height:13px;"></i>Add theme';
  addBtn.addEventListener('click', () => _addCustomTheme());
  presetsHost.appendChild(addBtn);
  // Preset / custom-theme saves confirm with the unified overlay over the whole
  // presets strip (`ac-presets-<theme>`) — no separate tick span.

  // ── Background select row + a swatch row per palette token ──

  // Background select row.
  const bgRow = document.createElement('div');
  bgRow.className = 'ac-theme-row';
  bgRow.innerHTML =
    '<span class="ac-theme-row-label"><i data-lucide="image" class="lucide-icon" style="width:15px;height:15px;color:var(--fg-3);"></i>Background</span>' +
    '<span class="ac-theme-row-ctl">' +
      `<select id="ac-bg-${theme}" class="ac-input"></select>` +
    '</span>';
  body.appendChild(bgRow);

  // Colour swatch rows — order from WA_APPEARANCE.tokens (single source of truth).
  const order = (window.WA_APPEARANCE && Array.isArray(window.WA_APPEARANCE.tokens))
    ? window.WA_APPEARANCE.tokens
    : Object.keys(_TOKEN_META);
  order.forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta) return;
    const key = _keyFor(base, theme);
    const fallback = _effectiveDefault(base, theme);
    // A blank saved value seeds the swatch from `fallback` (which carries the
    // follow-token's panel tint + alpha), so bubbles/pill show the live theme.
    const split = _splitColor(_appValues[key] || fallback, _asHex(fallback, '#000000'));
    const row = document.createElement('div');
    row.className = 'ac-theme-row';
    // Alpha-capable tokens (surfaces + bubbles) get an opacity slider after the
    // swatch; the two combine into a #rrggbb[aa] value.
    const alphaCtl = meta.alpha
      ? `<input type="range" class="ac-swatch-alpha" id="ac-al-${theme}-${base}" min="0" max="100" step="1" value="${split.alpha}" title="Opacity" aria-label="${_esc(meta.label)} opacity">`
      : '';
    row.innerHTML =
      `<span class="ac-theme-row-label"><i data-lucide="${_esc(meta.icon)}" class="lucide-icon" style="width:15px;height:15px;color:var(--fg-3);"></i>${_esc(meta.label)}</span>` +
      '<span class="ac-theme-row-ctl">' +
        alphaCtl +
        `<input type="color" class="ac-swatch" id="ac-sw-${theme}-${base}" value="${_esc(split.hex)}">` +
      '</span>';
    body.appendChild(row);

    const input = row.querySelector('input[type="color"]');
    const alpha = row.querySelector('input[type="range"]');
    // The unified save overlay sits ON TOP of the swatch+slider cluster.
    const ctl = row.querySelector('.ac-theme-row-ctl');
    // Current stored value = swatch colour combined with the opacity slider
    // (no slider → plain hex).
    const curVal = () => (alpha ? _combineColor(input.value, alpha.value) : input.value);
    // Drag = live preview only; release/commit = save + green tick.
    const preview = () => {
      _previewAppearance({ [key]: curVal() });
      if (base === 'accent') _refreshPresetActive(theme, input.value);
      if (base === 'surface_panel') _syncFollowSwatches(theme);
    };
    const commit = () => {
      const v = curVal();
      _previewAppearance({ [key]: v });
      _saveAppearance({ [key]: v }, ctl);
      if (base === 'accent') _refreshPresetActive(theme, input.value);
      if (base === 'surface_panel') _syncFollowSwatches(theme);
    };
    input.addEventListener('input', preview);
    input.addEventListener('change', commit);
    if (alpha) {
      alpha.addEventListener('input', preview);
      alpha.addEventListener('change', commit);
    }
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

// The palette token order (single source of truth = appearance.js).
function _tokenOrder() {
  return (window.WA_APPEARANCE && Array.isArray(window.WA_APPEARANCE.tokens))
    ? window.WA_APPEARANCE.tokens
    : Object.keys(_TOKEN_META);
}

// Apply a theme (built-in preset OR custom) to one mode — a COMPLETE RESET of
// every palette token for that mode, wiping any per-swatch / opacity / bubble
// customisation. For each token: use the theme's explicit value if it has one;
// otherwise a `follow` token (bubbles) resets to BLANK (= track the theme) and
// every other token resets to its design-system default. Swatch + opacity
// slider are synced to match.
function _applyPreset(theme, preset) {
  const pv = (preset && preset[theme]) || {};
  const patch = {};
  _tokenOrder().forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta) return;
    const key = _keyFor(base, theme);
    let v;
    if (Object.prototype.hasOwnProperty.call(pv, base)) v = pv[base];
    else if (meta.follow) v = '';
    else v = _APP_DEFAULTS[theme][base] || '';
    patch[key] = v;
    const disp = v || _effectiveDefault(base, theme);
    const split = _splitColor(disp, _asHex(disp, '#000000'));
    const sw = _qs(`ac-sw-${theme}-${base}`);
    if (sw) sw.value = split.hex;
    const al = _qs(`ac-al-${theme}-${base}`);
    if (al) al.value = split.alpha;
  });
  _previewAppearance(patch);
  _saveAppearance(patch, _qs('ac-presets-' + theme));
  _refreshPresetActive(theme, pv.accent || _APP_DEFAULTS[theme].accent);
}

// ── Custom themes (the "Add theme" button) ────────────────────────────────
// Snapshot the CURRENT palette of one mode into a { <token>: value } object —
// exactly what's applied now, including opacity (#rrggbbaa) and any bubble
// override (or '' when a bubble is following the theme).
function _snapshotTheme(theme) {
  const obj = {};
  _tokenOrder().forEach((base) => {
    if (!_TOKEN_META[base]) return;
    const cur = _appValues[_keyFor(base, theme)];
    obj[base] = (typeof cur === 'string')
      ? cur
      : (_TOKEN_META[base].follow ? '' : (_APP_DEFAULTS[theme][base] || ''));
  });
  return obj;
}

function _saveCustomThemes() {
  const v = JSON.stringify(_customThemes);
  _appValues.custom_themes = v;
  _saveAppearance({ custom_themes: v }, _qs('ac-presets-dark') || _qs('ac-presets-light'));
}

// Save the current colours (BOTH modes) as a new named theme chip.
function _addCustomTheme() {
  const name = (window.prompt('Name this theme:', 'My theme') || '').trim();
  if (!name) return;
  _customThemes.push({
    id: 'custom-' + Date.now(),
    name: name.slice(0, 40),
    dark: _snapshotTheme('dark'),
    light: _snapshotTheme('light'),
  });
  _saveCustomThemes();
  _buildThemeCard('light');
  _buildThemeCard('dark');
}

function _deleteCustomTheme(id) {
  _customThemes = _customThemes.filter((t) => t.id !== id);
  _saveCustomThemes();
  _buildThemeCard('light');
  _buildThemeCard('dark');
}

// Highlight the preset whose accent matches the current accent for this theme.
function _refreshPresetActive(theme, accentHex) {
  const host = _qs('ac-presets-' + theme);
  if (!host) return;
  const cur = String(accentHex || '').toLowerCase();
  host.querySelectorAll('.ac-preset-chip').forEach((chip) => {
    const p = _PRESETS.concat(_customThemes).find((x) => x.id === chip.dataset.preset);
    if (!p) return;
    const presetAccent = _asHex((p[theme] && p[theme].accent) || _APP_DEFAULTS[theme].accent, '');
    chip.classList.toggle('active', presetAccent.toLowerCase() === cur);
  });
}

// Populate a background <select> from the catalog + the built-in "None".
function _populateBgSelect(sel, cat, current) {
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = 'none';
  none.textContent = 'None — plain background';
  sel.appendChild(none);
  cat.forEach((bg) => {
    const o = document.createElement('option');
    o.value = bg.id;
    o.textContent = bg.name + (bg.status && bg.status !== 'stable' ? ` (${bg.status})` : '');
    sel.appendChild(o);
  });
  const known = current === 'none' || cat.some((b) => b.id === current);
  sel.value = known ? current : 'none';
}

// Save the per-theme background choice (sends BOTH so WA_BG.setChoice is whole).
function _saveBackgroundChoice(theme, ctrl) {
  const sel = _qs('ac-bg-' + theme);
  if (!sel) return;
  _bgChoice[theme] = sel.value;
  try {
    if (window.WA_BG && typeof window.WA_BG.setChoice === 'function') {
      window.WA_BG.setChoice({ dark: _bgChoice.dark, light: _bgChoice.light });
    }
  } catch (_) {}
  _saveAppearance({ [`background_${theme}`]: sel.value }, ctrl);
}

// Wire the two background selects (built into the theme cards by _buildThemeCard).
function _wireBackgroundSelects() {
  ['dark', 'light'].forEach((theme) => {
    const sel = _qs('ac-bg-' + theme);
    if (!sel) return;
    _populateBgSelect(sel, _bgCatalog, _bgChoice[theme]);
    // Overlay sits on the select's control cluster (`.ac-theme-row-ctl`).
    sel.addEventListener('change', () => _saveBackgroundChoice(theme, sel.closest('.ac-theme-row-ctl')));
  });
}

// Wire a single appearance <select>/<input>: live-preview + save (with the
// green-tick indicator) on change. Shared by the shared-controls and chat-layout
// groups below, which previously inlined byte-identical wire(id, key) closures.
function _wireAppearanceSelect(id, key) {
  const el = _qs(id);
  if (!el) return;
  el.addEventListener('change', () => {
    _previewAppearance({ [key]: el.value });
    _saveAppearance({ [key]: el.value }, el.closest('.ac-config-control'));
  });
}

// Wire the shared (theme-independent) controls: border thickness + fonts.
function _wireSharedControls() {
  _wireAppearanceSelect('ac-border-width', 'border_width');
  _wireAppearanceSelect('ac-font-sans', 'font_sans');
  _wireAppearanceSelect('ac-font-mono', 'font_mono');
}

// Wire the Chat panel layout selects (the separate "Chat panel" group). Both are
// app-wide appearance keys saved via _saveAppearance: `chat_position` (right/left)
// injects a #stage flex rule LIVE via _previewAppearance; `chat_default_visible`
// (visible/hidden) only seeds the first-visit desktop default (read at boot by
// index.html __chatVisibilityDefault), so it has no live effect on the admin's own
// already-toggled chat — just save + the green tick.
function _wireChatLayout() {
  _wireAppearanceSelect('ac-chat-position', 'chat_position');
  _wireAppearanceSelect('ac-chat-default-visible', 'chat_default_visible');
}

// Add a "Custom" option if a saved select value isn't one of the presets, so a
// hand-edited JSON value round-trips intact.
function _selectOrCustom(sel, value) {
  if (!sel || !value) return;
  const has = Array.from(sel.options).some((o) => o.value === value);
  if (!has) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = 'Custom (from settings file)';
    sel.appendChild(o);
  }
  sel.value = value;
}

function _initAppearance() {
  // The row bodies are built lazily in _loadAppearance (after values + catalog
  // load), so the swatches reflect saved colours. Shared controls are static.
  _wireSharedControls();
  _wireChatLayout();
  // Make the two theme rows expand/collapse like the ability-table rows
  // (clicking a control inside the body never toggles — see _wireBootRow).
  _wireBootRow('ac-theme-row-light', null);
  _wireBootRow('ac-theme-row-dark', null);
  _initThemeSwitch();
}

// Wire the LIVE theme switch in the Design header (Light · Dark · System). It is
// the SAME per-browser toggle as the user-menu dropdown — clicking calls the
// shared setTheme (apply to <body> + persist + sync every `.theme-option`
// highlight). Wired here (not by user-panel.js's boot-time pass) because this
// admin page mounts after app boot, so those buttons don't exist yet at boot.
function _initThemeSwitch() {
  const btns = document.querySelectorAll('.ac-theme-switch .theme-option');
  if (!btns.length) return;
  btns.forEach((btn) => {
    if (btn.dataset.wired) return;        // guard against re-init double-binding
    btn.dataset.wired = '1';
    // pointerdown matches the dropdown's handler — more reliable on small targets.
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      setTheme(btn.dataset.theme);
    });
  });
  // Reflect the visitor's saved choice on these freshly-mounted buttons.
  highlightThemeOption(getSavedTheme());
}

// Load saved values + background catalog, then build the two theme cards and
// fill the shared controls.
async function _loadAppearance(data, bgData) {
  // Saved appearance values. Reuse the caller's pre-fetched payload when given
  // (load() shares one /admin/settings/app fetch across both consumers);
  // `bgData === undefined` means "not passed" → fetch the catalog ourselves.
  const _bgPassed = arguments.length >= 2;
  try {
    if (data) {
      _appValues = data;
    } else {
      const r = await _fetch(apiPath('/admin/settings/app'));
      if (r.ok) _appValues = await r.json();
    }
  } catch (e) {
    console.warn('app-config: appearance values load failed', e);
  }
  if (_appValues.background_dark) _bgChoice.dark = _appValues.background_dark;
  if (_appValues.background_light) _bgChoice.light = _appValues.background_light;

  // User-saved custom themes (JSON array string).
  _customThemes = [];
  try {
    const arr = JSON.parse(_appValues.custom_themes || '[]');
    if (Array.isArray(arr)) _customThemes = arr.filter((t) => t && t.id && t.name);
  } catch (e) { /* keep none */ }

  // Background catalog (for the per-theme selects). Reuse the pre-fetched list
  // when load() passed it; otherwise fetch standalone.
  try {
    if (_bgPassed) {
      _bgCatalog = Array.isArray(bgData?.backgrounds) ? bgData.backgrounds : [];
    } else {
      const cr = await _fetch(apiPath('/admin/settings/backgrounds'));
      if (cr.ok) { const d = await cr.json(); _bgCatalog = Array.isArray(d.backgrounds) ? d.backgrounds : []; }
    }
  } catch (e) {
    console.warn('app-config: background catalog load failed', e);
  }

  // Build the mirrored theme cards + wire their background selects.
  _buildThemeCard('light');
  _buildThemeCard('dark');
  _wireBackgroundSelects();
  _refreshPresetActive('light', _asHex(_appValues.accent_light, _APP_DEFAULTS.light.accent));
  _refreshPresetActive('dark', _asHex(_appValues.accent_dark, _APP_DEFAULTS.dark.accent));

  // Shared controls (fonts + border thickness).
  _selectOrCustom(_qs('ac-border-width'), _appValues.border_width || '1px');
  _selectOrCustom(_qs('ac-font-sans'), _appValues.font_sans || "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif");
  _selectOrCustom(_qs('ac-font-mono'), _appValues.font_mono || "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace");

  // Chat panel layout (separate "Chat panel" group).
  const cp = _qs('ac-chat-position');
  if (cp) cp.value = (_appValues.chat_position === 'left') ? 'left' : 'right';
  const cdv = _qs('ac-chat-default-visible');
  if (cdv) cdv.value = (_appValues.chat_default_visible === 'hidden') ? 'hidden' : 'visible';
}

// ── Public API ───────────────────────────────────────────────────────────

export function init() {
  _initAppSettings();
  // Sticky section navigator (shared — see ../sticky-nav.js). Re-measure each
  // time App Settings is shown: at init() the section may be hidden (everything
  // measures 0), so the section hook is where the offsets actually land.
  // mobileCarousel: on a phone this section shows a horizontal heading-chip strip
  // sub-header instead of the stacked sticky headings (see ../sticky-nav.js).
  initStickyNav('app-settings', { mobileCarousel: true });
  registerSectionHook('app-settings', () => initStickyNav('app-settings', { mobileCarousel: true }));
}

export async function load() {
  // Fetch the shared /admin/settings/app payload ONCE and the background catalog
  // in parallel, then hand both to the two consumers. Previously _loadAppSettings
  // and _loadAppearance each did their own /admin/settings/app fetch (the endpoint
  // was hit 2-3× per open) and ran strictly serially.
  const appP = _fetch(apiPath('/admin/settings/app'))
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const bgP = _fetch(apiPath('/admin/settings/backgrounds'))
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const [appData, bgData] = await Promise.all([appP, bgP]);
  await _loadAppSettings(appData);
  await _loadAppearance(appData, bgData);
}

async function _saveGlobalPrompt() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const ta = _qs('ac-global-system-prompt');
  const btn = _qs('ac-global-prompt-save');
  _markSaving(btn);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ global_system_prompt: ta ? ta.value : '' }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(btn, true);
  } catch (e) {
    _flashSaveCheck(btn, false, e.message);
  }
}

async function _saveAppSettings() {
  const cb = _qs('ac-extend-llm-to-agents');
  const btn = _qs('ac-app-settings-save');
  _markSaving(btn);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        extend_llm_to_agents: cb ? cb.checked : true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(btn, true);
  } catch (e) {
    _flashSaveCheck(btn, false, e.message);
  }
}

