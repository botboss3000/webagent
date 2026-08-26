'use strict';

export const PERSISTENT_CACHE = 'persistent_cache';
export const MEMORY_ONLY = 'memory_only';
export const DISABLED = 'disabled';

const MODES = new Set([PERSISTENT_CACHE, MEMORY_ONLY, DISABLED]);
let _mode = PERSISTENT_CACHE;
let _ownerScope = '';
let _policyEpoch = 0;
let _schemaVersion = 0;
let _maxBytes = 512 * 1024 * 1024;

export function getBrowserStorageMode() {
  return _mode;
}

export function getBrowserStorageContext() {
  return {
    mode: _mode,
    owner_scope: _ownerScope,
    policy_epoch: _policyEpoch,
    schema_version: _schemaVersion,
    max_bytes: _maxBytes,
  };
}

export function browserPersistenceAllowed() {
  return _mode === PERSISTENT_CACHE;
}

export function configureBrowserStoragePolicy({
  mode = PERSISTENT_CACHE,
  ownerScope = '',
  policyEpoch = 0,
  schemaVersion = 0,
  maxBytes = 512 * 1024 * 1024,
} = {}) {
  const normalized = MODES.has(mode) ? mode : DISABLED;
  const previous = _mode;
  _mode = normalized;
  _ownerScope = String(ownerScope || '').replace(/[^A-Za-z0-9_-]/g, '');
  _policyEpoch = Math.max(0, Number(policyEpoch) || 0);
  _schemaVersion = Math.max(0, Number(schemaVersion) || 0);
  _maxBytes = Math.max(0, Number(maxBytes) || 0);

  // Synchronous listeners can retain the active account in process memory
  // before the durable mirrors are removed.
  window.dispatchEvent(new CustomEvent('webagent-browser-storage-policy', {
    detail: { ...getBrowserStorageContext(), previous_mode: previous },
  }));

  if (_mode !== PERSISTENT_CACHE) {
    for (const key of [
      'webagent_accounts', 'webagent_active_user_id', 'auth_token',
      'auth_username', 'auth_user_id', 'auth_display_name', 'remember_token',
      'anonUserId', 'terminalUserId',
    ]) {
      try { localStorage.removeItem(key); } catch (_) {}
    }
  }
  return getBrowserStorageContext();
}

export async function assertBrowserCapacity(additionalBytes) {
  if (!browserPersistenceAllowed()) return;
  const requested = Math.max(0, Number(additionalBytes) || 0);
  if (requested > _maxBytes) {
    throw new DOMException('Browser storage policy quota exceeded', 'QuotaExceededError');
  }
  if (navigator.storage?.estimate) {
    const estimate = await navigator.storage.estimate();
    const usage = Math.max(0, Number(estimate.usage) || 0);
    const browserQuota = Math.max(0, Number(estimate.quota) || 0);
    if ((browserQuota && usage + requested > browserQuota)
        || usage + requested > _maxBytes) {
      throw new DOMException('Browser storage policy quota exceeded', 'QuotaExceededError');
    }
  }
}
