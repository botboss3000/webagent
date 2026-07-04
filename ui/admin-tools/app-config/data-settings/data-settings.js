'use strict';

/**
 * Data Settings tab — data-oriented app configuration. Groups: DEPLOYMENT (moved
 * here out of App Settings), APP ACCESS (access mode + social sign-in), DATABASE
 * (the storage/secrets/encryption rows moved here out of the Data Management
 * page), and REMOTE ACCESS (the Wi-Fi / tunnel card, moved here out of App
 * Settings — sits directly below DATABASE). More data-related settings groups
 * can be added over time.
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
// initDns moved with the domain card to the Instances page's New Deployment tab
// (ui/admin-tools/instances/new-deployment/new-deployment.js). Not used here now.
import { initAppAccess } from './app-access.js';
import { initSocialAuth } from './social-auth.js';
import { initDangerZone } from './danger-zone.js';
import { initMediaCacheSettings } from './media-cache-settings.js';

// Wire one expandable row: clicking the head expands/collapses it, except when
// the click lands on a control inside the head. Same behaviour as App Settings'
// _wireBootRow (the Deployment rows were wired there before this tab existed).
// -- Page-assistant chat pill --------------------------------------------------
// The floating "advanced chat pill" at the bottom of Data Settings (a composer
// twin of App Settings' pill). The shared page-assistant engine (../page-
// assistant.js) runs in COMPOSER mode here too, owning this page's static
// #ac-ds-pa-* pill's send / Enter / voice / file-uploads. The prompt it hands
// WebAgent changes with the section the mouse is over (the data-pa-area groups
// in data-settings.html: deployment · app_access · database · remote_access); on send it opens a
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
//   Note: we walk ALL `.ac-row`s in the card, not just direct `.ac-list` children,
//   so the two rows now nested inside the "User Data" group (#ac-row-userdata →
//   GenUI Storage + Chat Attachments) get their own expand handler too. Each row's
//   handler binds to its OWN `:scope > .ac-ability-row` head (the group head for
//   the group row, each member's head for the members), so a click on a nested
//   member never bubbles to the group head, and vice-versa.
function _wireDatabaseRows() {
  const card = _qs('ac-database-card');
  if (!card) return;
  for (const row of card.querySelectorAll('.ac-row')) {
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
  // The DEPLOYMENT card was REMOVED from this page. Its local-deployments list —
  // this app's hold-to-restart + port editor, plus any sibling checkouts on this
  // machine — MOVED to the Admin Tools → Instances page, into the "This device"
  // tile's Overview as a "Server" section (ui/admin-tools/instances/instances.js).
  // The "+ New deployment" flow had already moved to that page's "New instance"
  // tile. So deploy.js's repo/target/instances elements are all absent here now.
  //
  // initDeploy() is STILL called — but ONLY for the Export / Import setup-bundle
  // rows, which live in the DATABASE card's "Data Migration" row: initDeploy's
  // _initBootRows wires their Generate/Copy/Preview/Accept actions by id. Every
  // other element it looks for (cloud form, deploy target, #ac-deploy-instances)
  // is absent on this page, so its `?.` guards make those a no-op (and _loadInstances
  // returns early with no #ac-deploy-instances host). The boot rows' head-expand is
  // handled by _wireDatabaseRows() below (it walks every `.ac-row` in
  // #ac-database-card, incl. these nested ones) — NOT wired here, or the two
  // handlers would cancel each other out.
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
  // Remote Access card — moved here from App Settings, sits directly below the
  // Database card. Its own `.ac-category-group` (#ac-ra-card), so it is NOT
  // covered by _wireDatabaseRows (scoped to #ac-database-card); wire its two
  // expandable rows here. The bodies' control ids are owned by remote-access.js,
  // which loads + fetches on App-Config open via settings-view.js — unchanged.
  _wireBootRow('ac-ra-row-sn');
  _wireBootRow('ac-ra-row-net');
  // Memory Cache (RAM) row — On/Off + memory budget for the in-browser media
  // cache (media-cache.js). Owns its own /admin/settings/app load + auto-save and
  // live preview via WA_APPEARANCE. Row expand/collapse comes from _wireDatabaseRows
  // above (the row lives in the same DATABASE ability-list). See ./media-cache-settings.js.
  initMediaCacheSettings();
  // Danger Zone card (very bottom) — reset selected data groups (self-restart +
  // boot-time wipe) or delete the whole install. Owns its own /admin/storage/reset*
  // fetch + confirm dialogs; sets window.__refreshDangerZone (called on section-show
  // in nav.js) so the last-reset banner appears after the reboot. See ./danger-zone.js.
  initDangerZone();
  // Page-assistant chat pill (bottom of the section) — steered toward helping the
  // admin get users signed in. Owns the static #ac-ds-pa-* composer; the per-area
  // placeholder swaps follow the data-pa-area groups above. See ../page-assistant.js.
  _initPageAssistant();
  // Sticky section navigator (shared — see ../sticky-nav.js). Re-measure each time
  // this section is shown (hidden at init() → everything measures 0).
  // mobileCarousel: on a phone this section shows a horizontal heading-chip strip
  // sub-header instead of the stacked sticky headings (see ../sticky-nav.js).
  initStickyNav('data-settings', { mobileCarousel: true });
  registerSectionHook('data-settings', () => initStickyNav('data-settings', { mobileCarousel: true }));
}

export function load() {
  // Nothing to load here yet. initDeploy() (in init above) sets window.__refreshDeploy,
  // which nav.js calls on section-show for a fresh deploy catalog — now only relevant
  // to the Export/Import setup-bundle bars, since the local-deployments list moved to
  // the Instances page's "This device" tile.
}
