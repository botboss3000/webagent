'use strict';

/**
 * Data Settings tab — data-oriented app configuration. Groups: DEPLOYMENT (moved
 * here out of App Settings), APP ACCESS (access mode + social sign-in), and
 * DATABASE (the storage/secrets/encryption rows moved here out of the Data
 * Management page). More data-related settings groups can be added over time.
 *
 * Mirrors the other App Config section modules (Data Management, App Settings): a
 * section partial (data-settings.html) fills the #ac-section-slot-data-settings
 * slot, and this module wires it. The Deployment card's own /admin/deploy/*
 * fetching + NDJSON deploy stream live in the sibling deploy.js (initDeploy),
 * which sets window.__refreshDeploy (called on section-show in nav.js).
 *
 * The DATABASE group's row CONTROLS are driven by ui/shared/js/storage.js +
 * ui/shared/js/data-management.js (they bind by element id, not by section, and
 * load on App-Config open via settings-view.js) — this module only wires those
 * rows' expand/collapse, in _wireDatabaseRows() below.
 */

import { _qs } from '../utils.js';
import { registerSectionHook } from '../nav.js';
import { initStickyNav } from '../sticky-nav.js';
import { createPageAssistant } from '../page-assistant.js';
import { spawnWebagentPageChat } from '../../../chat-widget/js/chat-widget.js';
import { initDeploy } from './deploy.js';
import { initAppAccess } from './app-access.js';
import { initSocialAuth } from './social-auth.js';

// Wire one expandable row: clicking the head expands/collapses it, except when
// the click lands on a control inside the head. Same behaviour as App Settings'
// _wireBootRow (the Deployment rows were wired there before this tab existed).
// -- Page-assistant chat pill --------------------------------------------------
// The floating "advanced chat pill" at the bottom of Data Settings (a composer
// twin of App Settings' pill). The shared page-assistant engine (../page-
// assistant.js) runs in COMPOSER mode here too, owning this page's static
// #ac-ds-pa-* pill's send / Enter / voice / file-uploads. The prompt it hands
// WebAgent changes with the section the mouse is over (the data-pa-area groups
// in data-settings.html: deployment · app_access · database); on send it opens a
// floating WebAgent (the manager) chat via spawnWebagentPageChat. The whole
// assistant is steered toward helping the admin get people SIGNED IN to this
// instance. Text catalog (placeholders + idea hints + prompts) lives in
// app/defaults/app-prompts.json -> page_assistants.data_settings (edit + reload).
let _pa = null;

function _initPageAssistant() {
  if (_pa) return;
  _pa = createPageAssistant({
    page: 'data_settings',
    section: _qs('ac-section-data-settings'),
    input: _qs('ac-ds-pa-input'),
    send: _qs('ac-ds-pa-send'),
    voice: _qs('ac-ds-pa-voice'),
    row: _qs('ac-ds-pa-bar-row'),
    previewBar: _qs('ac-ds-pa-preview-bar'),
    pending: [],
    wireComposer: true,
    onSend: spawnWebagentPageChat,
  });
  _pa.init();
}

function _wireBootRow(rowId) {
  const row = _qs(rowId);
  if (!row) return;
  const head = row.querySelector(':scope > .ac-ability-row');
  head?.addEventListener('click', (e) => {
    if (e.target.closest('select, input, button, a, label')) return;
    row.classList.toggle('expanded');
  });
}

// Wire the DATABASE group's ability-table rows to expand/collapse — a mirror of
// data-management.js `_wireRows` (these rows were moved here from that page).
// Scoped to #ac-database-card so it never touches the Deployment/App-Access rows.
// The exclusion list matches Data Management, including `.ac-tri`: the Encryption
// and Hybrid rows carry their toggle IN the header, so a click on it must run its
// own storage.js handler, never collapse the row.
function _wireDatabaseRows() {
  const card = _qs('ac-database-card');
  if (!card) return;
  for (const row of card.querySelectorAll('.ac-list > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    if (!head || head.dataset.rowWired) continue;
    head.dataset.rowWired = '1';
    head.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label, pre, textarea, summary, .ac-tri')) return;
      row.classList.toggle('expanded');
    });
  }
}

export function init() {
  // Deployment card — expandable rows. A static "Current deployment" info bar (no
  // expand), then a "+ New deployment" group that opens to reveal the four install
  // targets: the cloud deploy row + the three manual install rows (Linux/Termux,
  // Windows, macOS). Their body ids are owned by deploy.js (initDeploy below wires
  // the buttons, fetch + each row's live command/QR + the Current-deployment bar).
  _wireBootRow('ac-deploy-new-row');
  _wireBootRow('ac-deploy-register-row');
  _wireBootRow('ac-deploy-row');
  _wireBootRow('ac-deploy-phone-row');
  _wireBootRow('ac-deploy-win-row');
  _wireBootRow('ac-deploy-mac-row');
  initDeploy();
  // App Access card — the access-mode radios (moved out of the Users page). Owns
  // its own /admin/settings/app load + auto-save; sets window.__refreshAppAccess
  // (called on section-show in nav.js). See ./app-access.js.
  initAppAccess();
  // Social sign-in providers, nested in the App Access card (moved out of the
  // Users page). Owns its own load + save; sets window.__refreshSocialAuth
  // (called on section-show in nav.js). See ./social-auth.js.
  initSocialAuth();
  // Database card — expand/collapse for the storage/secrets/encryption rows moved
  // here from Data Management. Their control drivers (storage.js, data-management.js)
  // bind by id and load on App-Config open (settings-view.js); we only wire the rows.
  _wireDatabaseRows();
  // Page-assistant chat pill (bottom of the section) — steered toward helping the
  // admin get users signed in. Owns the static #ac-ds-pa-* composer; the per-area
  // placeholder swaps follow the data-pa-area groups above. See ../page-assistant.js.
  _initPageAssistant();
  // Sticky section navigator (shared — see ../sticky-nav.js). Re-measure each time
  // this section is shown (hidden at init() → everything measures 0).
  initStickyNav('data-settings');
  registerSectionHook('data-settings', () => initStickyNav('data-settings'));
}

export function load() {
  // The Deployment content loads itself: initDeploy() paints the local-deployments
  // list straight away and sets window.__refreshDeploy, which nav.js calls on
  // section-show for a fresh catalog. Nothing else to load here yet.
}
