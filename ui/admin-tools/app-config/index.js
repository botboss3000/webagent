'use strict';

/**
 * App Config — orchestrator module.
 *
 * Wires together all tab modules (import/export pattern).
 * Called by files.js (startAppConfig, stopAppConfig) and tabs.js (initAppConfig).
 */

import { initNav, _showSection, getActiveSection } from './nav.js';

import { init as initAgentSettings, load as loadAgentSettings, stop as stopAgentSettings } from './agent-settings/agent-settings.js';
import { init as initDataManagement, load as loadDataManagement } from './database/data-management.js';
import { init as initOptimizerStats, load as loadOptimizerStats } from './optimizer/optimizer-stats.js';
import { init as initAppSettings, load as loadAppSettings } from './app-settings/app-settings.js';
import { init as initUsers, load as loadUsers } from './user-management/users.js';
// The former Automation + Event Sources tabs are gone — both engine panels now
// live inside Agent Settings → "Automation Engine" and are driven by
// agent-settings.js, so they no longer need wiring here.

let _initialized = false;
let _active = false;

/** Called once on page load — sets up all event listeners. */
export function initAppConfig() {
  initNav();
  initAgentSettings();
  initDataManagement();
  initOptimizerStats();
  initAppSettings();
  initUsers();
  _initialized = true;
}

/** Called when the App Config tab becomes active — loads fresh data. */
export async function startAppConfig() {
  _active = true;
  _showSection(getActiveSection() || 'agent-settings');

  // Load all sections in parallel (non-blocking)
  loadAgentSettings();
  loadDataManagement();
  loadOptimizerStats();
  loadAppSettings();
  loadUsers();
}

/** Called when leaving the App Config tab. */
export function stopAppConfig() {
  _active = false;
  stopAgentSettings();
}