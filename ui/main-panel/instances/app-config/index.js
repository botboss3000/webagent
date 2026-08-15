'use strict';

/**
 * App Config — orchestrator module.
 *
 * Wires together all tab modules (import/export pattern).
 * Called by files.js (startAppConfig, stopAppConfig) and tabs.js (initAppConfig).
 *
 * All three sections (Data, App, Agent Settings) are rendered on a single
 * scrollable page. The tab bar acts as navigation: clicking a tab scrolls to
 * that section, and scrolling updates which tab is highlighted.
 */

import { initNav, _showSection, getActiveSection, onSectionShow } from './nav.js';
import { createPageAssistant } from './page-assistant.js';
import { createAppConfigSearch } from './app-config-search.js';
import { initStickyNav } from './sticky-nav.js';

import { init as initDataSettings, load as loadDataSettings } from './data-settings/data-settings.js';
import { init as initAgentSettings, load as loadAgentSettings, stop as stopAgentSettings } from './agent-settings/agent-settings.js';
import { init as initAppSettings, load as loadAppSettings } from './app-settings/app-settings.js';

let _initialized = false;
let _active = false;
let _unifiedPA = null;
let _unifiedSearch = null;

/** Initialize the unified page-assistant chat pill */
function _initUnifiedPageAssistant() {
  const input = document.getElementById('ac-unified-pa-input');
  const send = document.getElementById('ac-unified-pa-send');
  const attach = document.getElementById('ac-unified-pa-attach');
  const voice = document.getElementById('ac-unified-pa-voice');
  const row = document.getElementById('ac-unified-pa-bar-row');
  const previewBar = document.getElementById('ac-unified-pa-preview-bar');
  
  if (!input || !send) return;

  _unifiedSearch = createAppConfigSearch({
    root: document.getElementById('app-config-content'),
    empty: document.getElementById('ac-unified-search-empty'),
  });
  
  _unifiedPA = createPageAssistant({
    page: 'app_settings',  // Use app_settings as default, will adapt based on section
    section: document.getElementById('app-config-content'),
    input,
    resolvePage: (areaElement) => ({
      'ac-section-data-settings': 'data_settings',
      'ac-section-app-settings': 'app_settings',
      'ac-section-agent-settings': 'agent_settings',
    })[areaElement.closest('.ac-section')?.id] || 'app_settings',
    fallbackPlaceholder: 'ask how to configure something or customize the app',
    send,
    voice,
    row,
    previewBar,
    onInput: (query) => _unifiedSearch?.filter(query),
    onSend: ({ message, attachmentIds }) => {
      // Import dynamically to avoid circular dependencies
      import('../../../chat-widget/js/chat-widget.js').then(({ spawnWebagentPageChat }) => {
        spawnWebagentPageChat({
          message,
          attachmentIds,
          page: 'app_config',
          context: _unifiedPA ? _unifiedPA.currentArea() : null
        });
      });
    }
  });
  _unifiedPA.init();
}

/** Called once on page load — sets up all event listeners. */
export function initAppConfig() {
  if (_initialized) return;
  initNav();
  initDataSettings();
  initAgentSettings();
  initAppSettings();
  _initUnifiedPageAssistant();
  _initialized = true;
}

/** Called when the App Config tab becomes active — loads all sections since
    they're all visible on the single scrollable page. */
export async function startAppConfig({ preserveScroll = false } = {}) {
  _active = true;
  // Load all three sections since they're all visible now
  loadDataSettings();
  loadAppSettings();
  loadAgentSettings();

  // Configuration can be moved into the Instances page after initialization.
  // Rebind navigation to whichever element owns scrolling in the visible layout.
  requestAnimationFrame(() => {
    for (const id of ['data-settings', 'app-settings', 'agent-settings']) {
      initStickyNav(id, { mobileCarousel: true });
    }
  });
  
  // Standalone App Config restores its last section. When embedded in Instances,
  // the parent page owns scrolling and must not be moved on tab activation.
  const section = getActiveSection() || 'data-settings';
  if (!preserveScroll) _showSection(section);
}

/** Called when leaving the App Config tab. */
export function stopAppConfig() {
  _active = false;
  stopAgentSettings();
}
