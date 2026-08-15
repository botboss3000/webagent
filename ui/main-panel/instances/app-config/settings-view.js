'use strict';

/**
 * Settings (App Config) — drop-in admin view entry (embedded in Instances).
 *
 * No longer a standalone admin page — the App Config UI is only available
 * as the Configuration tab inside the Instances page's "This device" tile.
 * The view is booted via window.initAppConfig() / window.startAppConfig()
 * called from instances.js's _onSettingsTabRendered().
 *
 * tabs.js `_ensureAdminInit` still calls initAppConfig() at boot to attach
 * listeners. startView/stopView are exposed for the module interface but
 * the standalone page descriptor (page.json) has been removed.
 *
 * App Config bundles several settings sections whose data/pollers live in
 * sibling modules (Data Management, Remote Access, Tunnel Link, Storage,
 * Billing). They are started together because they share the one view.
 */

import { initAppConfig, startAppConfig, stopAppConfig } from './index.js';
import { startDataManagement } from '../../../shared/js/data-management.js';
import { startRemoteAccess } from '../../../shared/js/remote-access.js';
import { startTunnelLink } from '../../../shared/js/tunnel-link.js';
import { startStorageUi } from '../../../shared/js/storage.js';
import { startBilling, stopBilling } from '../../../shared/js/billing.js';

// Expose app config functions globally so other views (e.g. Instances Settings tab) can use them
window.initAppConfig = initAppConfig;
window.startAppConfig = startAppConfig;
window.stopAppConfig = stopAppConfig;

export function startView() {
  try { startAppConfig(); } catch (_) {}
  try { startDataManagement(); } catch (_) {}
  try { startRemoteAccess(); } catch (_) {}
  try { startTunnelLink(); } catch (_) {}
  try { startStorageUi(); } catch (_) {}
  try { startBilling(); } catch (_) {}
}

export function stopView() {
  try { stopAppConfig(); } catch (_) {}
  try { stopBilling(); } catch (_) {}
}
