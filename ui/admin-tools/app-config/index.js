'use strict';

/**
 * App Config — orchestrator module.
 *
 * Wires together all tab modules (import/export pattern).
 * Called by files.js (startAppConfig, stopAppConfig) and tabs.js (initAppConfig).
 */

import { initNav, _showSection, getActiveSection, onSectionShow } from './nav.js';

import { init as initDataSettings, load as loadDataSettings } from './data-settings/data-settings.js';
import { init as initAgentSettings, load as loadAgentSettings, stop as stopAgentSettings } from './agent-settings/agent-settings.js';
import { init as initAppSettings, load as loadAppSettings } from './app-settings/app-settings.js';
// The former Automation + Event Sources tabs are gone — both engine panels now
// live inside Agent Settings → "Automation Engine" and are driven by
// agent-settings.js, so they no longer need wiring here.

let _initialized = false;
let _active = false;

// Section id → its data loader. Used to lazy-load a tab's data the moment it
// becomes visible, instead of loading all three on every panel open.
const _sectionLoaders = {
  'data-settings': loadDataSettings,
  'app-settings': loadAppSettings,
  'agent-settings': loadAgentSettings,
};

/** Called once on page load — sets up all event listeners. */
export function initAppConfig() {
  initNav();
  initDataSettings();
  initAgentSettings();
  initAppSettings();
  // Lazy loading: fetch a section's data only when it's shown. Opening App
  // Config used to eagerly load all three sections at once — including the
  // heavy Agent Settings (dozens of DB-backed calls) even when the visible tab
  // was Data or App Settings. Now the visible tab loads on show (below) and the
  // other two load the first time the admin switches to them.
  onSectionShow(section => {
    const fn = _sectionLoaders[section];
    if (fn) fn();
  });
  _initialized = true;
}

/** Called when the App Config tab becomes active — loads the visible section. */
export async function startAppConfig() {
  _active = true;
  // _showSection fires the onSectionShow hook, which loads the now-visible
  // section's data. The other two tabs stay unloaded until first shown.
  _showSection(getActiveSection() || 'agent-settings');
}

/** Called when leaving the App Config tab. */
export function stopAppConfig() {
  _active = false;
  stopAgentSettings();
}