'use strict';

/**
 * StorageAdapter — routes all data operations between browser-authority
 * (IndexedDB) and normal (server API) mode.
 *
 * Server-published capabilities and routing select the mode at boot; callers
 * cannot use a URL or direct assignment to upgrade storage authority.
 *
 * The chat *send* path is fundamentally different between modes (SSE stream
 * vs outbox+WS+reconcile), so that gets a conditional branch in chat-send.js
 * rather than a thin wrapper here.
 *
 * All data methods return the same shapes the existing chat UI expects,
 * so callers need no mode-specific branches.
 */

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import browserRouter from './browser-router.js';
import defaultSessionDB from './indexeddb.js';
import { setAttachmentOwnerScope } from '../../../shared/js/attachments-idb.js';
import {
  configureBrowserStoragePolicy,
  DISABLED,
  MEMORY_ONLY,
  PERSISTENT_CACHE,
} from '../../../shared/js/browser-storage-policy.js';

// ── Mode ───────────────────────────────────────────────────────────────────

const MODE_NORMAL = 'normal';
const MODE_BROWSER = 'browser';
const MODE_HYBRID = 'hybrid';

let _mode = MODE_NORMAL;
let _cleanupTimer = null;
let _capabilities = {
  browser_authority: false,
  browser_session_cache: false,
};
let _cachePolicy = {
  schema_version: 2,
  metadata_ttl_seconds: 300,
  transcript_ttl_seconds: 900,
  run_state_ttl_seconds: 3600,
  generated_html_ttl_seconds: 86400,
  max_bytes: 50 * 1024 * 1024,
  persistence_mode: PERSISTENT_CACHE,
  policy_epoch: 0,
};

export const storageAdapter = {

  /** 'normal' (server API), 'browser' (IndexedDB), or 'hybrid' (server auth + IDB cache) */
  get mode() { return _mode; },
  set mode(v) {
    if (v !== MODE_NORMAL && v !== MODE_BROWSER && v !== MODE_HYBRID) return;
    if (v === MODE_BROWSER && !_capabilities.browser_authority) return;
    if (v === MODE_HYBRID && !_capabilities.browser_session_cache) return;
    _mode = v;
  },

  get isBrowser() { return _mode === MODE_BROWSER; },
  get isNormal() { return _mode === MODE_NORMAL; },
  get isHybrid() { return _mode === MODE_HYBRID; },
  get capabilities() { return { ..._capabilities }; },
  get browserStoragePolicy() { return { ..._cachePolicy }; },
  get persistenceMode() { return _cachePolicy.persistence_mode; },
  get canUseBrowserAuthority() { return _capabilities.browser_authority; },
  get canUseBrowserCache() { return _capabilities.browser_session_cache; },

  // ── Initialisation ────────────────────────────────────────────────────

  /**
   * Boot into browser mode for a given agent. Fetches + caches config.
   * Must be called once (e.g. at app boot) before using other methods.
   * @param {string} agentId
   */
  async initBrowser(agentId) {
    if (!_capabilities.browser_authority) {
      throw new Error('Browser authority is disabled by server policy');
    }
    await browserRouter.init(agentId);
    _startLifecycleSweep();
    _mode = MODE_BROWSER;
  },

  /**
   * Fetch the storage routing config from the server and auto-select
   * browser mode if session_data is set to "browser".
   * Called at boot — if the server routing says browser, the adapter
   * enters browser mode without needing a URL parameter.
   * @param {string} agentId
   * @returns {Promise<string>} 'browser' or 'normal'
   */
  async autoSelectMode(agentId) {
    // A URL may force the safer server mode, but can never upgrade authority.
    const urlParam = new URLSearchParams(window.location.search).get('storage');
    if (urlParam === 'server') {
      _mode = MODE_NORMAL;
      return 'normal';
    }

    // Otherwise, ask the server for the routing config
    try {
      const resp = await fetch(apiPath('/api/v1/browser/routing'), {
        headers: authHeaders(),
      });
      if (resp.ok) {
        const data = await resp.json();
        const routing = data.routing || {};
        _capabilities = {
          browser_authority: data.capabilities?.browser_authority === true,
          browser_session_cache: data.capabilities?.browser_session_cache === true,
        };
        _cachePolicy = { ..._cachePolicy, ...(data.cache_policy || {}) };
        if (!data.cache_scope) throw new Error('Server did not issue a browser cache scope');
        defaultSessionDB.setOwnerScope(data.cache_scope);
        defaultSessionDB.configureLifecyclePolicy(_cachePolicy);
        setAttachmentOwnerScope(data.cache_scope);
        const persistenceMode = [
          PERSISTENT_CACHE, MEMORY_ONLY, DISABLED,
        ].includes(_cachePolicy.persistence_mode)
          ? _cachePolicy.persistence_mode
          : DISABLED;
        configureBrowserStoragePolicy({
          mode: persistenceMode,
          ownerScope: data.cache_scope,
          policyEpoch: _cachePolicy.policy_epoch,
          schemaVersion: _cachePolicy.schema_version,
          maxBytes: _cachePolicy.max_bytes,
        });
        if (persistenceMode !== PERSISTENT_CACHE) {
          _capabilities.browser_authority = false;
          _capabilities.browser_session_cache = false;
          // Policy transition cleanup is coordinated and reported. The normal
          // server chat remains available in memory_only mode.
          const { purgeBrowserData } = await import('../../../shared/js/browser-lifecycle.js');
          const purge = await purgeBrowserData(data.cache_scope);
          window.dispatchEvent(new CustomEvent('webagent-browser-storage-purge', {
            detail: { reason: 'policy-transition', ...purge },
          }));
          if (!purge.complete) {
            console.warn('[StorageAdapter] Browser policy transition purge is incomplete', purge);
          }
          _mode = MODE_NORMAL;
          return 'normal';
        }
        if (_capabilities.browser_authority && routing.session_data === 'browser') {
          await this.initBrowser(agentId);
          return 'browser';
        }
        if (_capabilities.browser_session_cache && routing.session_cache === 'browser') {
          await this.initHybrid(agentId);
          return 'hybrid';
        }
      }
    } catch (e) {
      console.warn('[StorageAdapter] Could not fetch routing config:', e);
    }

    _mode = MODE_NORMAL;
    return 'normal';
  },

  /**
   * Boot into hybrid mode. Opens IndexedDB for read caching but keeps
   * the server as the write authority.
   * @param {string} agentId
   */
  async initHybrid(agentId) {
    if (!_capabilities.browser_session_cache) {
      throw new Error('Browser session cache is disabled by server policy');
    }
    // Warm up IndexedDB without starting a full browser session.
    // We don't call browserRouter.init() — that would enter browser mode.
    // We just ensure the DB is open and our stores exist.
    await browserRouter._ensureReady();
    await defaultSessionDB.enforceCachePolicy({ maxBytes: _cachePolicy.max_bytes });
    _startLifecycleSweep();
    _mode = MODE_HYBRID;
  },

  /**
   * Switch back to normal (server) mode.
   */
  switchToNormal() {
    _mode = MODE_NORMAL;
  },

  // ── Sessions ─────────────────────────────────────────────────────────

  /**
   * Fetch the session list. Returns the same shape as the existing server API.
   * @param {string} userId — ignored in browser mode
   * @param {Object} [opts]
   * @param {boolean} [opts.includeHidden]
   * @returns {Promise<Array<Object>>} sessions
   */
  async listSessions(userId, opts = {}) {
    if (_mode === MODE_BROWSER) {
      const sessions = await browserRouter.listSessions();
      return _shapeSessions(sessions);
    }

    if (_mode === MODE_HYBRID) {
      // Try IndexedDB first for an instant render
      const cached = await defaultSessionDB.listSessions();
      const now = Date.now();
      const valid = cached.filter(row =>
        row._authority === 'server' &&
        Number(row.cache_schema_version || 0) === Number(_cachePolicy.schema_version) &&
        !!row.content_hash &&
        Date.parse(row.cache_expires_at || 0) > now
      );
      if (valid.length > 0) {
        // Fire background refresh for freshness
        _refreshSessionListFromServer(userId).catch(() => {});
        return _shapeSessions(valid);
      }
      // Cache empty — fetch from server and cache
      const fromServer = await _fetchSessionListFromServer(userId, opts);
      await _cacheSessionList(fromServer);
      return fromServer;
    }

    // Normal mode — existing server API call (mirrors session-list.js)
    return _fetchSessionListFromServer(userId, opts);
  },

  /**
   * Delete a session.
   * @param {string} sessionId
   * @returns {Promise<{ok: boolean, error?: string}>}
   */
  async deleteSession(sessionId) {
    if (_mode === MODE_BROWSER) {
      try {
        await browserRouter.deleteSession(sessionId);
        return { ok: true };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    }

    // Server-first then remove from local cache (hybrid + normal)
    try {
      const res = await fetch(
        apiPath('/api/v1/db/sessions/' + encodeURIComponent(sessionId) + '?db=user.db'),
        { method: 'DELETE', headers: { ...authHeaders() } }
      );
      if (!res.ok) {
        const body = await res.text().catch(() => '');
        return { ok: false, error: `Server error (${res.status}): ${body.substring(0, 120)}` };
      }
      // In hybrid mode, also remove from IndexedDB cache
      if (_mode === MODE_HYBRID) {
        try { await defaultSessionDB.deleteCachedSession(sessionId); } catch (_) {}
      }
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e.message || 'Network error' };
    }
  },

  /**
   * Patch session metadata (pin, title, etc).
   * @param {string} sessionId
   * @param {Object} body
   * @returns {Promise<Object>}
   */
  async patchSession(sessionId, body) {
    if (_mode === MODE_BROWSER) {
      await browserRouter.updateSession(sessionId, body);
      return { ok: true };
    }

    // Server-first then update local cache (hybrid + normal)
    const res = await fetch(
      apiPath('/api/v1/db/sessions/' + encodeURIComponent(sessionId) + '?db=user.db'),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      }
    );
    // In hybrid mode, also update IndexedDB cache
    if (_mode === MODE_HYBRID && res.ok) {
      try { await defaultSessionDB.updateSession(sessionId, body); } catch (_) {}
    }
    return await res.json();
  },

  /**
   * Interrupt a running session turn. No-op in browser mode.
   * @param {string} sessionId
   */
  async interruptSession(sessionId) {
    if (_mode === MODE_BROWSER) return;
    fetch(apiPath('/api/v1/chat/interrupt'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
  },

  /**
   * Refresh session metadata from the shared authority.
   * No-op in browser mode (no remote authority).
   * @param {string} userId
   * @returns {Promise<boolean>}
   */
  async refreshSessionMetadata(userId) {
    if (_mode === MODE_BROWSER) return false;

    try {
      let url = `/api/v1/db/sessions/refresh?db=user.db&user_id=${encodeURIComponent(userId)}`;
      const res = await fetch(apiPath(url), { method: 'POST', headers: authHeaders() });
      if (!res.ok) return false;
      const data = await res.json();
      return !!data.changed;
    } catch (e) {
      console.warn('[StorageAdapter] refreshSessionMetadata failed:', e);
      return false;
    }
  },

  /**
   * Create a new session.
   * In browser mode, creates in IndexedDB. In normal mode, returns the id
   * (session created server-side on first message).
   * @param {Object} opts — { agent_id, title, id }
   * @returns {Promise<string>} session id
   */
  async createSession(opts = {}) {
    if (_mode === MODE_BROWSER) {
      return browserRouter.createSession(opts);
    }
    return opts.id || '';
  },

  // ── Interactions / Messages ─────────────────────────────────────────

  /**
   * Fetch interactions (messages) for a session.
   * Returns the same shape as GET /api/v1/db/session-messages.
   * @param {string} sessionId
   * @param {number} limit
   * @param {Object} [opts]
   * @param {string} [opts.beforeId]
   * @param {string} [opts.afterId]
   * @param {string} [opts.aroundId]
   * @returns {Promise<{messages: Array, has_more?: boolean}>}
   */
  async getInteractions(sessionId, limit, opts = {}) {
    if (_mode === MODE_BROWSER) {
      // Browser authority holds the WHOLE transcript locally, so every window
      // (including the jump-nav modes at_start / nearest_user_before_id) can be
      // sliced client-side. This also reports accurate has_more/has_newer, which
      // the jump nav needs to know whether still-older rows exist.
      const all = await browserRouter.getInteractions(sessionId, Infinity);
      const { messages } = _shapeInteractions(all);
      return _windowInteractionsClientSide(messages, limit, opts);
    }

    if (_mode === MODE_HYBRID) {
      // A persistent transcript is a hit only after the server confirms its
      // exact revision/hash. TTL and "cache warm" are never authority proofs.
      const cached = await defaultSessionDB.getInteractions(sessionId, limit);
      const manifest = await defaultSessionDB.getSession(sessionId);
      const validManifest =
        manifest?._authority === 'server' &&
        Number(manifest.cache_schema_version || 0) === Number(_cachePolicy.schema_version) &&
        !!manifest.content_hash &&
        !!manifest.cache_payload_hash &&
        manifest.cache_payload_hash === await _hashCachedInteractions(cached) &&
        Date.parse(manifest.cache_expires_at || 0) > Date.now() &&
        !opts.beforeId && !opts.afterId && !opts.aroundId &&
        !opts.atStart && !opts.nearestUserBeforeId &&
        cached.length >= Math.min(
          Number(manifest.interaction_count || cached.length),
          Number(limit || cached.length),
        );
      if (validManifest && cached.length > 0) {
        const validation = await _fetchInteractionsFromServer(sessionId, 1, {
          manifestOnly: true,
          knownRevision: Number(manifest.authority_revision || 0),
          knownHash: manifest.content_hash,
        });
        if (validation.not_modified === true) {
          await _updateCachedManifest(sessionId, validation.manifest);
          return {
            ..._shapeInteractions(cached),
            manifest: validation.manifest,
            cache_status: 'validated-hit',
          };
        }
      }
      // Cache miss/stale/corrupt — replace it from the authoritative response.
      const data = await _fetchInteractionsFromServer(sessionId, limit, opts);
      try {
        await defaultSessionDB.clearInteractions(sessionId);
        await defaultSessionDB.addInteractions(sessionId, data.messages || []);
        const stored = await defaultSessionDB.getInteractions(sessionId, limit);
        await _updateCachedManifest(sessionId, data.manifest, stored);
      } catch (_) { /* best-effort */ }
      return data;
    }

    // Normal mode — existing server API call
    return _fetchInteractionsFromServer(sessionId, limit, opts);
  },
};

function _startLifecycleSweep() {
  if (_cleanupTimer !== null) return;
  _cleanupTimer = window.setInterval(async () => {
    try {
      const detail = await defaultSessionDB.enforceLifecyclePolicy();
      window.dispatchEvent(new CustomEvent('webagent-browser-storage-cleanup', { detail }));
    } catch (error) {
      window.dispatchEvent(new CustomEvent('webagent-browser-storage-cleanup', {
        detail: {
          last_cleanup_at: new Date().toISOString(),
          rows_removed: 0,
          bytes_removed: 0,
          errors: [String(error?.message || error)],
        },
      }));
    }
  }, 60_000);
}

// ── Internal helpers (shared across modes) ──────────────────────────────────

function _shapeSessions(sessions) {
  return sessions.map(s => ({
    id: s.id,
    title: s.title || 'New Session',
    agent_id: s.agent_id || null,
    agent_name: s.agent_name || '',
    agent_icon: s.agent_icon || '',
    agent_engine: s.agent_engine || '',
    created_at: s.created_at,
    updated_at: s.updated_at || s.created_at,
    activity_at: s.activity_at || null,
    pinned: !!s.pinned,
    sort_order: s.sort_order || null,
    hidden: !!s.hidden,
    run_status: s.run_status || null,
    has_unread: !!s.has_unread,
    child_count: s.child_count || 0,
    interaction_count: Number(s.interaction_count || 0),
    authority_revision: Number(s.authority_revision || s.revision || 0),
    content_hash: s.content_hash || '',
    cache_schema_version: Number(s.cache_schema_version || 0),
    cache_expires_at: s.cache_expires_at || null,
    _authority: s._authority || null,
  }));
}

async function _fetchSessionListFromServer(userId, opts = {}) {
  // limit=0 = no cap, so the session list matches the Sessions page and the
  // chat-header dropdown (both fetch the full list).
  let url = `/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(userId)}&limit=0`;
  if (opts.includeHidden) url += '&include_hidden=1';
  const res = await fetch(apiPath(url), { headers: authHeaders() });
  const data = await res.json();
  return _shapeSessions(
    (data.sessions || []).map(s => ({
      ...s,
      sort_order: Number.isFinite(s.sort_order) ? s.sort_order : null,
    }))
  );
}

async function _cacheSessionList(sessions) {
  // Cache sessions in IndexedDB via direct DB access (browserRouter methods
  // are mode-guarded, but the DB is always available).
  await defaultSessionDB.ready();
  for (const s of sessions) {
    const revision = Number(s.authority_revision || s.revision || 0);
    if (!revision || !s.content_hash) continue;
    const cacheFields = {
      authority_revision: revision,
      content_hash: s.content_hash,
      cache_schema_version: Number(_cachePolicy.schema_version),
      cache_expires_at: new Date(
        Date.now() + Number(_cachePolicy.metadata_ttl_seconds) * 1000,
      ).toISOString(),
      last_accessed_at: new Date().toISOString(),
      _authority: 'server',
      _dirty: false,
      interaction_count: Number(s.interaction_count || 0),
    };
    try {
      await defaultSessionDB.createSession({
        id: s.id,
        agent_id: s.agent_id || '',
        title: s.title || '',
        created_at: s.created_at,
        updated_at: s.updated_at,
        pinned: s.pinned,
        hidden: s.hidden,
        sort_order: s.sort_order,
        ...cacheFields,
      });
    } catch (_) {
      // Already exists — update instead
      try { await defaultSessionDB.updateSession(s.id, {
        title: s.title || '',
        updated_at: s.updated_at,
        pinned: s.pinned,
        hidden: s.hidden,
        sort_order: s.sort_order,
        ...cacheFields,
      }); } catch (__) { /* best-effort */ }
    }
  }
}

async function _refreshSessionListFromServer(userId) {
  try {
    const fromServer = await _fetchSessionListFromServer(userId);
    await _cacheSessionList(fromServer);
    // Dispatch event so the UI can re-render
    window.dispatchEvent(new CustomEvent('sessions-delta', {
      detail: { sessions: fromServer },
    }));
  } catch (_) { /* background — silence failures */ }
}

/**
 * Client-side windowing for browser-authority mode, where the full transcript
 * lives in IndexedDB and the server API isn't consulted. Mirrors the server's
 * `/session-messages` cursor semantics: newest-N by default, plus at_start,
 * nearest_user_before_id, around_id, before_id and after_id windows. `messages`
 * must be sorted oldest-first (session_seq ascending).
 */
function _windowInteractionsClientSide(messages, limit, opts = {}) {
  const lim = limit || 20;
  const sorted = [...messages].sort((a, b) => {
    const sa = Number(a.session_seq ?? 0), sb = Number(b.session_seq ?? 0);
    if (sa !== sb) return sa - sb;
    return String(a.created_at || '').localeCompare(String(b.created_at || ''));
  });

  if (opts.atStart) {
    const slice = sorted.slice(0, lim);
    return { messages: slice, has_more: false, has_newer: slice.length < sorted.length };
  }
  if (opts.nearestUserBeforeId) {
    const idx = sorted.findIndex(m => m.id === opts.nearestUserBeforeId);
    const end = idx === -1 ? sorted.length : idx;
    const user = [...sorted.slice(0, end)].reverse().find(m => m.role === 'user');
    return { messages: user ? [user] : [], has_more: false, has_newer: false };
  }
  if (opts.aroundId) {
    const idx = sorted.findIndex(m => m.id === opts.aroundId);
    if (idx === -1) {
      const slice = sorted.slice(-lim);
      return { messages: slice, has_more: sorted.length > lim, has_newer: false };
    }
    const start = Math.max(0, idx - lim);
    const end = Math.min(sorted.length, idx + lim + 1);
    return {
      messages: sorted.slice(start, end),
      has_more: start > 0,
      has_newer: end < sorted.length,
    };
  }
  if (opts.beforeId) {
    const idx = sorted.findIndex(m => m.id === opts.beforeId);
    const end = idx === -1 ? sorted.length : idx;
    const start = Math.max(0, end - lim);
    return { messages: sorted.slice(start, end), has_more: start > 0, has_newer: idx !== -1 };
  }
  if (opts.afterId) {
    const idx = sorted.findIndex(m => m.id === opts.afterId);
    const start = idx === -1 ? 0 : idx + 1;
    return {
      messages: sorted.slice(start, start + lim),
      has_more: idx !== -1,
      has_newer: start + lim < sorted.length,
    };
  }
  // Default: newest N.
  const slice = sorted.slice(-lim);
  return { messages: slice, has_more: sorted.length > lim, has_newer: false };
}

function _shapeInteractions(interactions) {
  return {
    messages: interactions.map(ix => ({
      id: ix.id,
      session_id: ix.session_id,
      role: ix.role,
      content: ix.content,
      tool_name: ix.tool_name,
      output: ix.output,
      session_seq: ix.session_seq,
      created_at: ix.created_at,
      metadata: ix.metadata,
      status: ix.status,
    })),
    has_more: false,
  };
}

async function _fetchInteractionsFromServer(sessionId, limit, opts = {}) {
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}`);
  if (opts.light !== false) url += '&light=1';
  if (opts.beforeId) url += `&before_id=${encodeURIComponent(opts.beforeId)}`;
  if (opts.afterId) url += `&after_id=${encodeURIComponent(opts.afterId)}`;
  if (opts.aroundId) url += `&around_id=${encodeURIComponent(opts.aroundId)}`;
  if (opts.atStart) url += '&at_start=1';
  if (opts.nearestUserBeforeId) url += `&nearest_user_before_id=${encodeURIComponent(opts.nearestUserBeforeId)}`;
  if (typeof opts.afterSeq === 'number') url += `&after_seq=${opts.afterSeq}`;
  if (opts.manifestOnly) url += '&manifest_only=true';
  if (typeof opts.knownRevision === 'number') {
    url += `&known_revision=${encodeURIComponent(opts.knownRevision)}`;
  }
  if (opts.knownHash) url += `&known_hash=${encodeURIComponent(opts.knownHash)}`;
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`Session history request failed (${res.status})`);
  return await res.json();
}

async function _hashCachedInteractions(rows) {
  const canonical = (rows || []).map(row => ({
    id: row.id || '',
    session_id: row.session_id || '',
    role: row.role || '',
    content: row.content || '',
    tool_name: row.tool_name || null,
    tool_call_id: row.tool_call_id || null,
    output: row.output || null,
    session_seq: Number(row.session_seq || 0),
    created_at: row.created_at || '',
    metadata: row.metadata || null,
    status: row.status || null,
    parent_id: row.parent_id || null,
  }));
  const bytes = new TextEncoder().encode(JSON.stringify(canonical));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
}

async function _updateCachedManifest(sessionId, manifest, cachedRows = null) {
  if (!manifest?.content_hash) return;
  const changes = {
    authority_revision: Number(manifest.authority_revision || 0),
    content_hash: manifest.content_hash,
    cache_schema_version: Number(manifest.cache_schema_version || _cachePolicy.schema_version),
    interaction_count: Number(manifest.interaction_count || 0),
    cache_expires_at: new Date(
      Date.now() + Number(_cachePolicy.transcript_ttl_seconds) * 1000,
    ).toISOString(),
    last_accessed_at: new Date().toISOString(),
    _authority: 'server',
    _dirty: false,
  };
  if (cachedRows) {
    changes.cache_payload_hash = await _hashCachedInteractions(cachedRows);
    changes.cached_message_count = cachedRows.length;
  }
  await defaultSessionDB.updateSession(sessionId, changes);
}

async function _syncInteractionsFromServer(sessionId) {
  // Fetch only messages newer than what we have cached
  try {
    const cached = await defaultSessionDB.getInteractions(sessionId);
    let maxSeq = 0;
    for (const m of cached) {
      if (typeof m.session_seq === 'number' && m.session_seq > maxSeq) {
        maxSeq = m.session_seq;
      }
    }
    // Fetch only rows with session_seq > maxSeq via the server filter
    const data = await _fetchInteractionsFromServer(sessionId, 200, { afterSeq: maxSeq });
    const newMsgs = data.messages || [];
    if (newMsgs.length > 0) {
      await defaultSessionDB.addInteractions(sessionId, newMsgs);
      // Dispatch delta event so the UI can merge new messages
      window.dispatchEvent(new CustomEvent('messages-delta', {
        detail: { sessionId, messages: newMsgs },
      }));
    }
  } catch (_) { /* background — silence failures */ }
}
