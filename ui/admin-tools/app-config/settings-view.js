'use strict';

/**
 * Settings (App Config) — drop-in admin view entry.
 *
 * Mirrors every other admin view: the page descriptor (page.json) points
 * `entry`/`start`/`stop` here, and files.js drives this view through the same
 * dynamic dispatch (_startDynAdminView / _stopDynAdminView) it uses for all
 * drop-in admin views — no inline `if (view === 'settings')` branch.
 *
 * The boot-time wiring stays where it belongs: tabs.js `_ensureAdminInit`
 * calls initAppConfig() once to attach listeners, and main.js calls
 * relocateAdminToolsContainers() once to move #app-config-container into this
 * view's #files-settings-main host. Those are shared admin setup steps (the
 * Data Management view relies on the same relocate), so they are NOT repeated
 * here. startView only does the per-activation work: load each section's data
 * and start its pollers; stopView quiets them on leave.
 *
 * App Config bundles several settings sections whose data/pollers live in
 * sibling modules (Data Management, Remote Access, Tunnel Link, Storage,
 * Billing). They are started together because they share the one Settings view.
 */

import { startAppConfig, stopAppConfig } from './index.js';
import { startDataManagement } from '../../shared/js/data-management.js';
import { startRemoteAccess } from '../../shared/js/remote-access.js';
import { startTunnelLink } from '../../shared/js/tunnel-link.js';
import { startStorageUi } from '../../shared/js/storage.js';
import { startBilling, stopBilling } from '../../shared/js/billing.js';

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
