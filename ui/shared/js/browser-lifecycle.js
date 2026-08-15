'use strict';

import sessionDB, { SESSION_DB_STORES } from '../../chat/js/storage/indexeddb.js';
import {
  clearAttachmentStorage,
  exportAttachmentStorage,
  revokeAllObjectUrls,
  setAttachmentOwnerScope,
} from './attachments-idb.js';
import { getBrowserStorageContext } from './browser-storage-policy.js';

const SESSION_PREFIX = 'webagent_session_db_';
const ATTACHMENT_PREFIX = 'webagent_attachments_';
const LEGACY_ATTACHMENT_DB = 'webagent-attachments';

function _expected(scope, { includeLegacy = true } = {}) {
  const databases = [
    `${SESSION_PREFIX}${scope}`,
    `${ATTACHMENT_PREFIX}${scope}`,
  ];
  if (includeLegacy) databases.push(LEGACY_ATTACHMENT_DB);
  return databases;
}

async function _databaseNames() {
  if (typeof indexedDB.databases !== 'function') return [];
  return (await indexedDB.databases()).map(row => row?.name).filter(Boolean);
}

async function _clearWebStorage() {
  const keys = [
    'webagent_accounts', 'webagent_active_user_id',
    'webagent_device_cache_scopes', 'auth_token', 'auth_username',
    'auth_user_id', 'auth_display_name', 'remember_token',
    'anonUserId', 'terminalUserId',
  ];
  for (const storage of [localStorage, sessionStorage]) {
    for (const key of keys) {
      try { storage.removeItem(key); } catch (_) {}
    }
  }
}

async function _clearUserCaches() {
  if (!globalThis.caches?.keys) return;
  const names = await caches.keys();
  for (const name of names) {
    if (/webagent.*(user|session|attachment|chat)/i.test(name)) {
      if (!(await caches.delete(name))) {
        throw new Error(`Cache Storage deletion failed: ${name}`);
      }
    }
  }
}

/**
 * Export all inventoried browser stores. Any failed required read makes the
 * envelope explicitly incomplete and the caller must not describe it as a
 * complete data export.
 */
export async function exportBrowserData(scope = sessionDB.ownerScope) {
  const context = getBrowserStorageContext();
  const failures = [];
  const databases = {};
  if (!scope) {
    return {
      format: 'webagent-browser-lifecycle-export',
      version: 2,
      complete: false,
      failures: [{ database: null, error: 'owner scope is not initialized' }],
      context,
      databases,
    };
  }
  sessionDB.setOwnerScope(scope);
  setAttachmentOwnerScope(scope);
  if (context.mode === 'persistent_cache') {
    try {
      const exported = await sessionDB.exportAll();
      databases[exported.database.name] = exported.database;
    } catch (error) {
      failures.push({ database: `${SESSION_PREFIX}${scope}`, error: String(error?.message || error) });
    }
  } else {
    databases[`${SESSION_PREFIX}${scope}`] = {
      name: `${SESSION_PREFIX}${scope}`,
      version: 6,
      persistence: context.mode,
      stores: Object.fromEntries(SESSION_DB_STORES.map(name => [name, []])),
    };
  }
  try {
    const exported = await exportAttachmentStorage();
    databases[exported.database.name] = exported.database;
  } catch (error) {
    failures.push({ database: `${ATTACHMENT_PREFIX}${scope}`, error: String(error?.message || error) });
  }
  return {
    format: 'webagent-browser-lifecycle-export',
    version: 2,
    complete: failures.length === 0,
    created_at: new Date().toISOString(),
    context: { ...context, owner_scope: scope },
    databases,
    failures,
  };
}

/**
 * Purge and verify every tenant browser data path. A partial result is
 * deliberately retryable and must never be used for purge acknowledgement.
 */
export async function purgeBrowserData(scope, { includeLegacy = true } = {}) {
  const context = getBrowserStorageContext();
  const expected = _expected(scope, { includeLegacy });
  const completed = [];
  const failures = [];
  if (!scope) {
    failures.push({ target: 'owner_scope', error: 'owner scope is not initialized' });
  } else {
    try {
      sessionDB.setOwnerScope(scope);
      sessionDB.close();
      await sessionDB.deleteDatabase();
      completed.push(`${SESSION_PREFIX}${scope}`);
    } catch (error) {
      failures.push({ target: `${SESSION_PREFIX}${scope}`, error: String(error?.message || error) });
    }
    try {
      setAttachmentOwnerScope(scope);
      const result = await clearAttachmentStorage({ deleteDatabase: true, includeLegacy });
      completed.push(...result.completed);
      failures.push(...result.failures);
    } catch (error) {
      failures.push({ target: `${ATTACHMENT_PREFIX}${scope}`, error: String(error?.message || error) });
    }
  }

  revokeAllObjectUrls();
  try { await _clearWebStorage(); } catch (error) {
    failures.push({ target: 'web-storage', error: String(error?.message || error) });
  }
  try { await _clearUserCaches(); } catch (error) {
    failures.push({ target: 'cache-storage', error: String(error?.message || error) });
  }

  // Deletion success events are insufficient: verify the database catalog.
  try {
    const remaining = new Set(await _databaseNames());
    for (const name of expected) {
      if (remaining.has(name)) {
        failures.push({ target: name, error: 'database still exists after deletion' });
      }
    }
  } catch (error) {
    failures.push({ target: 'indexeddb-verification', error: String(error?.message || error) });
  }
  const uniqueCompleted = [...new Set(completed)].filter(
    name => !failures.some(failure => failure.target === name),
  );
  return {
    complete: failures.length === 0 && expected.every(name => uniqueCompleted.includes(name)),
    context: { ...context, owner_scope: scope },
    expected_databases: expected,
    completed_databases: uniqueCompleted,
    failures,
  };
}
