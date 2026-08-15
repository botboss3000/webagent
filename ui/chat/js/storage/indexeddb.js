'use strict';

/**
 * SessionDB — IndexedDB-backed session and interaction store for ephemeral
 * (browser-authority) mode.
 *
 * Phase 1: IndexedDB as authority. Sessions, interactions, and agent config
 * are all stored locally. The server is called only for LLM inference and
 * agent config fetch (which is cached here).
 *
 * Phase 2 (future): Sync engine bolt-on. A sync.js module will read dirty
 * flags set here and push selected sessions to a server-side per-user SQLite
 * or Postgres store for cross-device access.
 *
 * All methods return promises. Reads use indexed cursors for speed.
 */

import { randomUUID } from '../../../shared/js/uuid.js';
import {
  assertBrowserCapacity,
  browserPersistenceAllowed,
  getBrowserStorageMode,
} from '../../../shared/js/browser-storage-policy.js';

const DB_NAME_PREFIX = 'webagent_session_db';
const DB_VERSION = 6;
export const SESSION_DB_STORES = Object.freeze([
  'sessions', 'interactions', 'agent_config', 'session_runs', 'memories',
  'genui_pages', 'genui_html', 'user_files', 'sync_outbox', 'tool_details',
]);
let _ownerScope = '';
let _lifecyclePolicy = {
  metadata_ttl_seconds: 300,
  transcript_ttl_seconds: 900,
  run_state_ttl_seconds: 3600,
  generated_html_ttl_seconds: 86400,
};

function _dbName() {
  if (!_ownerScope) throw new Error('IndexedDB owner scope is not initialized');
  return `${DB_NAME_PREFIX}_${_ownerScope}`;
}

/**
 * Internal helper: open (or create/migrate) the IndexedDB database.
 * Schema version 1 defines three object stores:
 *
 * sessions     — session metadata (one doc per session)
 * interactions — individual chat turns (many per session)
 * agent_config — cached agent definitions from the server
 *
 * @returns {Promise<IDBDatabase>}
 */
function _openDB() {
  if (!browserPersistenceAllowed()) {
    throw new Error(`IndexedDB persistence is unavailable in ${getBrowserStorageMode()} mode`);
  }
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(_dbName(), DB_VERSION);

    req.onupgradeneeded = (ev) => {
      const db = ev.target.result;

      // ── sessions store ──────────────────────────────────────────────
      if (!db.objectStoreNames.contains('sessions')) {
        const store = db.createObjectStore('sessions', { keyPath: 'id' });
        store.createIndex('by_updated', 'updated_at', { unique: false });
        store.createIndex('by_agent', 'agent_id', { unique: false });
        store.createIndex('by_created', 'created_at', { unique: false });
      }

      // ── interactions store ──────────────────────────────────────────
      if (!db.objectStoreNames.contains('interactions')) {
        const store = db.createObjectStore('interactions', { keyPath: 'id' });
        store.createIndex('by_session_seq', ['session_id', 'session_seq'], { unique: true });
        store.createIndex('by_session_created', ['session_id', 'created_at'], { unique: false });
        store.createIndex('by_session', 'session_id', { unique: false });
      }

      // ── agent_config store ──────────────────────────────────────────
      if (!db.objectStoreNames.contains('agent_config')) {
        const store = db.createObjectStore('agent_config', { keyPath: 'id' });
        store.createIndex('by_hash', 'config_hash', { unique: true });
      }

      // ── v2: session_runs store ──────────────────────────────────────
      // Tracks in-progress / completed / interrupted agent turns so the
      // browser can auto-resume on reload and render partial output.
      if (!db.objectStoreNames.contains('session_runs')) {
        const store = db.createObjectStore('session_runs', { keyPath: 'id' });
        store.createIndex('by_session', 'session_id', { unique: false });
        store.createIndex('by_status', 'status', { unique: false });
      }

      // ── v2: memories store ──────────────────────────────────────────
      // Lightweight memory pages extracted from chat turns — keyed by slug
      // so the server-side memory_save result just lands here.
      if (!db.objectStoreNames.contains('memories')) {
        const store = db.createObjectStore('memories', { keyPath: 'slug' });
        store.createIndex('by_type', 'page_type', { unique: false });
      }

      // ── v3: genui_pages store ─────────────────────────────────────────
      // Gen UI page metadata (title, description, slug, agent_id, order).
      if (!db.objectStoreNames.contains('genui_pages')) {
        const store = db.createObjectStore('genui_pages', { keyPath: 'slug' });
        store.createIndex('by_order', 'order', { unique: false });
        store.createIndex('by_updated', 'updated_at', { unique: false });
      }

      // ── v3: genui_html store ──────────────────────────────────────────
      // The full rendered HTML of each genui page, stored separately so
      // listing metadata never loads the bulky HTML blob.
      if (!db.objectStoreNames.contains('genui_html')) {
        db.createObjectStore('genui_html', { keyPath: 'slug' });
      }

      // ── v4: user_files store ──────────────────────────────────────────
      // Per-user saved files (images, exports, reports). Each file is one
      // row keyed by its relative path (e.g. "files/report.md").
      if (!db.objectStoreNames.contains('user_files')) {
        const store = db.createObjectStore('user_files', { keyPath: 'path' });
        store.createIndex('by_room', 'room', { unique: false });
        store.createIndex('by_updated', 'updated_at', { unique: false });
      }

      // v6: durable per-session sync mutations. Tombstones live here after
      // the session/transcript rows are removed, so an interrupted sync cannot
      // resurrect a deleted session.
      if (!db.objectStoreNames.contains('sync_outbox')) {
        const store = db.createObjectStore('sync_outbox', { keyPath: 'session_id' });
        store.createIndex('by_updated', 'updated_at', { unique: false });
      }

      // ── v4: tool_details store ─────────────────────────────────────────
      // Cached /session-turn-detail responses keyed by interaction id.  When
      // the user expands a tool-call panel a second time, the full body is
      // read from IndexedDB (instant) instead of re-fetching from the server.
      if (!db.objectStoreNames.contains('tool_details')) {
        db.createObjectStore('tool_details', { keyPath: 'id' });
      }

      // v5 containment: tool arguments/results may contain secrets or personal
      // data. Purge the legacy unbounded cache; Phase 0 no longer writes it.
      if (ev.oldVersion < 5) {
        ev.target.transaction.objectStore('tool_details').clear();
      }
    };

    req.onsuccess = (ev) => resolve(ev.target.result);
    req.onerror = (ev) => reject(ev.target.error);
    req.onblocked = () => {
      console.warn('[SessionDB] IndexedDB open blocked — another tab may have an older version open');
    };
  });
}

// ─── Batching helpers (small convenience wrappers) ─────────────────────────

async function _put(db, storeName, value) {
  await assertBrowserCapacity(_valueSize(value));
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const req = store.put(value);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function _get(db, storeName, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const req = store.get(key);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error);
  });
}

function _del(db, storeName, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const req = store.delete(key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

function _getAll(db, storeName) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

function _getAllByIndex(db, storeName, indexName, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const idx = store.index(indexName);
    const req = idx.getAll(key);
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

function _getAllByKeyRange(db, storeName, indexName, range) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const idx = store.index(indexName);
    const req = idx.getAll(range);
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

function _count(db, storeName, indexName, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const idx = indexName ? store.index(indexName) : store;
    const req = idx.count(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function _clear(db, storeName) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, 'readwrite');
    const store = tx.objectStore(storeName);
    const req = store.clear();
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

/**
 * Single-session query: returns the newest `limit` interactions for a session,
 * ordered by session_seq ascending (oldest first).
 */
function _queryInteractionsBySession(db, sessionId, limit) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction('interactions', 'readonly');
    const idx = tx.objectStore('interactions').index('by_session_seq');
    const range = IDBKeyRange.bound(
      [sessionId, -Infinity],
      [sessionId, Infinity]
    );
    const req = idx.openCursor(range, 'prev'); // newest first
    const results = [];
    req.onsuccess = () => {
      const cursor = req.result;
      if (cursor && (limit === undefined || results.length < limit)) {
        results.push(cursor.value);
        cursor.continue();
      } else {
        resolve(results.reverse()); // flip back to chronological
      }
    };
    req.onerror = () => reject(req.error);
  });
}

// ─── Public API ────────────────────────────────────────────────────────────

export class SessionDB {

  constructor() {
    this._db = null;
    this._openPromise = null;
  }

  // ── Connection lifecycle ────────────────────────────────────────────

  /**
   * Ensure the database is open. Idempotent — safe to call before every
   * operation if you don't want to manage the promise yourself.
   * @returns {Promise<IDBDatabase>}
   */
  async ready() {
    if (this._db) return this._db;
    if (!this._openPromise) {
      this._openPromise = _openDB().then(db => {
        this._db = db;
        // Handle unexpected close (e.g. storage eviction)
        db.onclose = () => {
          this._db = null;
          this._openPromise = null;
        };
        db.onversionchange = () => {
          db.close();
          this._db = null;
          this._openPromise = null;
        };
        return db;
      });
    }
    return this._openPromise;
  }

  /**
   * Close the database connection explicitly.
   */
  close() {
    if (this._db) {
      this._db.close();
      this._db = null;
      this._openPromise = null;
    }
  }

  /**
   * Select the authenticated, server-issued tenant scope before opening IDB.
   * Each tenant receives a physically separate database.
   */
  setOwnerScope(scope) {
    const normalized = String(scope || '').replace(/[^A-Za-z0-9_-]/g, '');
    if (!normalized) throw new Error('Missing IndexedDB owner scope');
    if (_ownerScope && _ownerScope !== normalized) this.close();
    _ownerScope = normalized;
  }

  get ownerScope() { return _ownerScope; }

  get databaseName() { return _dbName(); }

  configureLifecyclePolicy(policy = {}) {
    for (const key of Object.keys(_lifecyclePolicy)) {
      const value = Number(policy[key]);
      if (Number.isFinite(value) && value > 0) _lifecyclePolicy[key] = value;
    }
  }

  /**
   * Delete the physical tenant database and fail visibly when another tab
   * blocks deletion. Lifecycle acknowledgement must not use clearAll(), since
   * a successful subset of store clears is not proof of complete erasure.
   */
  async deleteDatabase({ timeoutMs = 3000 } = {}) {
    const name = _dbName();
    this.close();
    await new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          reject(new Error(`Timed out deleting ${name}; another tab may be blocking it`));
        }
      }, timeoutMs);
      const request = indexedDB.deleteDatabase(name);
      request.onsuccess = () => {
        if (!settled) { settled = true; clearTimeout(timer); resolve(); }
      };
      request.onerror = () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(request.error || new Error(`Could not delete ${name}`));
        }
      };
      request.onblocked = () => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          reject(new Error(`Deletion blocked for ${name}`));
        }
      };
    });
  }

  // ── Sessions ────────────────────────────────────────────────────────

  /**
   * Return every session, sorted by updated_at descending.
   * @returns {Promise<Array>}
   */
  async listSessions() {
    const db = await this.ready();
    const all = await _getAll(db, 'sessions');
    return all.sort((a, b) => {
      // Pinned first, then by updated_at desc
      if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
      const at = Date.parse(a.updated_at || a.created_at || 0);
      const bt = Date.parse(b.updated_at || b.created_at || 0);
      return bt - at || String(a.id || '').localeCompare(String(b.id || ''));
    });
  }

  /**
   * Get a single session by id.
   * @param {string} sessionId
   * @returns {Promise<Object|null>}
   */
  async getSession(sessionId) {
    const db = await this.ready();
    return _get(db, 'sessions', sessionId);
  }

  /**
   * Create a new session and return its id.
   * Minimal required fields are stored; extra fields pass through.
   * @param {Object} session
   * @param {string} [session.id] — auto-generated if omitted
   * @param {string} session.agent_id
   * @param {string} [session.title]
   * @returns {Promise<string>} session id
   */
  async createSession(session) {
    const db = await this.ready();
    const now = new Date().toISOString();
    const sessionId = session.id || randomUUID();
    const doc = {
      id: sessionId,
      agent_id: session.agent_id || '',
      title: session.title || '',
      created_at: session.created_at || now,
      updated_at: session.updated_at || now,
      pinned: !!session.pinned,
      hidden: !!session.hidden,
      sync_level: typeof session.sync_level === 'number' ? session.sync_level : 0,
      authority_revision: Number(session.authority_revision || 0),
      local_revision: Number(session.local_revision || 0),
      synced_local_revision: Number(session.synced_local_revision || 0),
      history_token: session.history_token || null,
      content_hash: session.content_hash || '',
      cache_expires_at: session.cache_expires_at || null,
      last_accessed_at: session.last_accessed_at || now,
      _authority: session._authority || 'browser',
      // Pass through any extra fields the caller set (icon, metadata, etc.)
      ...session,
      // But never let id/created_at/updated_at be overwritten by spread
      id: sessionId,
      created_at: session.created_at || now,
      updated_at: session.updated_at || now,
    };
    await _put(db, 'sessions', doc);
    return doc.id;
  }

  /**
   * Update a session's metadata. Merges shallowly over existing fields.
   * Always bumps updated_at.
   * @param {string} sessionId
   * @param {Object} changes
   * @returns {Promise<boolean>} true if the session existed
   */
  async updateSession(sessionId, changes) {
    const db = await this.ready();
    const existing = await _get(db, 'sessions', sessionId);
    if (!existing) return false;
    const updated = {
      ...existing,
      ...changes,
      id: sessionId,          // never change the key
      updated_at: new Date().toISOString(),
    };
    await _put(db, 'sessions', updated);
    return true;
  }

  /**
   * Delete a session and all its interactions.
   * @param {string} sessionId
   */
  async deleteSession(sessionId) {
    const db = await this.ready();
    // Remove all interactions for this session
    const interactions = await _getAllByIndex(db, 'interactions', 'by_session', sessionId);
    const existing = await _get(db, 'sessions', sessionId);
    const tx = db.transaction(['interactions', 'sessions', 'sync_outbox'], 'readwrite');
    const iStore = tx.objectStore('interactions');
    for (const msg of interactions) {
      iStore.delete(msg.id);
    }
    const sStore = tx.objectStore('sessions');
    sStore.delete(sessionId);
    tx.objectStore('sync_outbox').put({
      session_id: sessionId,
      mutation_id: randomUUID(),
      operation: 'delete',
      base_server_revision: Number(existing?.server_revision || 0),
      client_revision: Number(existing?.local_revision || 0) + 1,
      updated_at: new Date().toISOString(),
    });
    await new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  /**
   * Count total sessions.
   * @returns {Promise<number>}
   */
  async countSessions() {
    const db = await this.ready();
    return _count(db, 'sessions');
  }

  // ── Interactions ────────────────────────────────────────────────────

  /**
   * Return all interactions for a session, ordered by session_seq ascending.
   * @param {string} sessionId
   * @param {number} [limit] — optionally limit to the most recent N
   * @returns {Promise<Array>}
   */
  async getInteractions(sessionId, limit) {
    const db = await this.ready();
    if (limit && limit < Infinity) {
      return _queryInteractionsBySession(db, sessionId, limit);
    }
    return _getAllByIndex(db, 'interactions', 'by_session_seq',
      IDBKeyRange.bound([sessionId, -Infinity], [sessionId, Infinity])
    );
  }

  /**
   * Append a single interaction (message) to a session.
   * session_seq is auto-assigned as max(existing) + 1 or 0.
   * @param {string} sessionId
   * @param {Object} msg — { role, content, ... }
   * @returns {Promise<Object>} the saved interaction doc with id + session_seq
   */
  async addInteraction(sessionId, msg) {
    const docs = await this.addInteractions(sessionId, [msg]);
    return docs[0];
  }

  /**
   * Append multiple interactions in a single transaction (atomic batch).
   * session_seq is auto-assigned starting from the existing max + 1.
   * @param {string} sessionId
   * @param {Array<Object>} messages
   * @returns {Promise<Array<Object>>} saved docs
   */
  async addInteractions(sessionId, messages) {
    if (!messages || messages.length === 0) return [];
    const db = await this.ready();
    const now = new Date().toISOString();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(['interactions', 'sessions'], 'readwrite');
      const iStore = tx.objectStore('interactions');
      const sStore = tx.objectStore('sessions');
      const req = iStore.index('by_session').getAll(IDBKeyRange.only(sessionId));
      let docs = [];
      req.onsuccess = () => {
        const existing = req.result || [];
        const existingIds = new Set(existing.map(row => row.id));
        const uniqueMessages = messages.filter(msg => !msg.id || !existingIds.has(msg.id));
        const maxSeq = existing.reduce(
          (m, x) => Math.max(m, Number(x.session_seq ?? -1)), -1,
        );
        docs = uniqueMessages.map((msg, i) => ({
          id: msg.id || randomUUID(),
          session_id: sessionId,
          role: msg.role || 'user',
          content: msg.content || '',
          session_seq: maxSeq + 1 + i,
          created_at: msg.created_at || now,
          tool_name: msg.tool_name || null,
          tool_call_id: msg.tool_call_id || null,
          tool_calls: msg.tool_calls || null,
          output: msg.output || null,
          parent_id: msg.parent_id || null,
          metadata: msg.metadata || null,
          status: msg.status || null,
          _streaming: !!msg._streaming,
        }));
        for (const doc of docs) iStore.put(doc);
        if (docs.length === 0) return;
        const sReq = sStore.get(sessionId);
        sReq.onsuccess = () => {
          if (!sReq.result) return;
          sStore.put({
            ...sReq.result,
            updated_at: now,
            last_accessed_at: now,
            local_revision: Number(sReq.result.local_revision || 0) + 1,
            _dirty: sReq.result._authority === 'server'
              ? !!sReq.result._dirty
              : true,
          });
        };
      };
      tx.oncomplete = () => resolve(docs);
      tx.onerror = () => reject(tx.error || new Error('Interaction transaction failed'));
      tx.onabort = () => reject(tx.error || new Error('Interaction transaction aborted'));
    });
  }

  /**
   * Update a single interaction by id (e.g. to finalize a streaming message).
   * @param {string} interactionId
   * @param {Object} changes
   * @returns {Promise<boolean>} true if the interaction existed
   */
  async updateInteraction(interactionId, changes) {
    const db = await this.ready();
    const existing = await _get(db, 'interactions', interactionId);
    if (!existing) return false;
    const updated = { ...existing, ...changes, id: interactionId };
    await _put(db, 'interactions', updated);
    return true;
  }

  /**
   * Count interactions for a session.
   * @param {string} sessionId
   * @returns {Promise<number>}
   */
  async countInteractions(sessionId) {
    const db = await this.ready();
    return _count(db, 'interactions', 'by_session', sessionId);
  }

  /**
   * Delete all interactions for a session (keeps the session itself).
   * @param {string} sessionId
   */
  async clearInteractions(sessionId) {
    const db = await this.ready();
    const interactions = await _getAllByIndex(db, 'interactions', 'by_session', sessionId);
    const tx = db.transaction('interactions', 'readwrite');
    const store = tx.objectStore('interactions');
    for (const msg of interactions) {
      store.delete(msg.id);
    }
    await new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // ── Session Runs (v2) — in-progress/interrupted/completed turns ─────

  /**
   * Start a session run. Creates a row marking the turn as 'running'.
   * @param {string} sessionId
   * @param {Object} [meta] — { agent_id, turn_id, ... }
   * @returns {Promise<string>} run id
   */
  async startRun(sessionId, meta = {}) {
    const db = await this.ready();
    const run = {
      id: randomUUID(),
      session_id: sessionId,
      status: 'running',
      started_at: new Date().toISOString(),
      agent_id: meta.agent_id || null,
      turn_id: meta.turn_id || null,
      partial_content: '',
      partial_events: [],
    };
    await _put(db, 'session_runs', run);
    return run.id;
  }

  /**
   * Append a stream event to an in-progress run.
   * @param {string} runId
   * @param {Object} event — SSE event from the agent loop
   */
  async appendRunEvent(runId, event) {
    const db = await this.ready();
    const run = await _get(db, 'session_runs', runId);
    if (!run) return;
    if (!run.partial_events) run.partial_events = [];
    run.partial_events.push(event);
    if (event.type === 'stream') {
      run.partial_content = (run.partial_content || '') + (event.content || '');
    }
    await _put(db, 'session_runs', run);
  }

  /**
   * Mark a run as completed, interrupted, or errored.
   * @param {string} runId
   * @param {string} status — 'complete' | 'interrupted' | 'error'
   * @param {Object} [meta] — { final_response, error_message, ... }
   */
  async finishRun(runId, status, meta = {}) {
    const db = await this.ready();
    const run = await _get(db, 'session_runs', runId);
    if (!run) return;
    run.status = status;
    run.finished_at = new Date().toISOString();
    run.expires_at = new Date(
      Date.now() + _lifecyclePolicy.run_state_ttl_seconds * 1000,
    ).toISOString();
    if (meta.final_response) run.final_response = meta.final_response;
    if (meta.error_message) run.error_message = meta.error_message;
    await _put(db, 'session_runs', run);
  }

  /**
   * Return the active (running) run for a session, if any.
   * @param {string} sessionId
   * @returns {Promise<Object|null>}
   */
  async getActiveRun(sessionId) {
    const db = await this.ready();
    const all = await _getAllByIndex(db, 'session_runs', 'by_session', sessionId);
    return all.find(r => r.status === 'running') || null;
  }

  /**
   * Return all runs for a session, newest first.
   * @param {string} sessionId
   * @returns {Promise<Array>}
   */
  async listRuns(sessionId) {
    const db = await this.ready();
    const all = await _getAllByIndex(db, 'session_runs', 'by_session', sessionId);
    return all.sort((a, b) =>
      Date.parse(b.started_at || '') - Date.parse(a.started_at || ''));
  }

  // ── Memories (v2) — extracted knowledge from chat turns ─────────────

  /**
   * Upsert a memory page by slug.
   * @param {Object} memory — { slug, page_type, title, summary, ... }
   */
  async upsertMemory(memory) {
    const db = await this.ready();
    const slug = memory.slug;
    const existing = await _get(db, 'memories', slug);
    const doc = {
      ...(existing || {}),
      ...memory,
      slug,
      updated_at: new Date().toISOString(),
      created_at: existing ? existing.created_at : new Date().toISOString(),
    };
    await _put(db, 'memories', doc);
  }

  /**
   * List all memories.
   * @returns {Promise<Array>}
   */
  async listMemories() {
    const db = await this.ready();
    return _getAll(db, 'memories');
  }

  // ── Agent config cache ──────────────────────────────────────────────

  /**
   * Store a cached copy of an agent's config (system prompt, tools, abilities).
   * Keyed by agent id. Pass `config_hash` for staleness checks.
   * @param {Object} config — { id (agent_id), config_hash, system_prompt, tools, abilities, ... }
   */
  async cacheAgentConfig(config) {
    const db = await this.ready();
    const doc = {
      ...config,
      id: config.id,                        // agent_id is the key
      config_hash: config.config_hash || '',
      cached_at: new Date().toISOString(),
      expires_at: new Date(
        Date.now() + _lifecyclePolicy.metadata_ttl_seconds * 1000,
      ).toISOString(),
    };
    await _put(db, 'agent_config', doc);
  }

  /**
   * Retrieve cached agent config by agent id.
   * @param {string} agentId
   * @returns {Promise<Object|null>}
   */
  async getCachedAgentConfig(agentId) {
    const db = await this.ready();
    const row = await _get(db, 'agent_config', agentId);
    if (row?.expires_at && Date.parse(row.expires_at) <= Date.now()) {
      await _del(db, 'agent_config', agentId);
      return null;
    }
    return row;
  }

  /**
   * Look up a config by its hash. Useful when the server returns a new hash
   * and we want to see if we already have it.
   * @param {string} hash
   * @returns {Promise<Object|null>}
   */
  async getConfigByHash(hash) {
    const db = await this.ready();
    const tx = db.transaction('agent_config', 'readonly');
    const idx = tx.objectStore('agent_config').index('by_hash');
    return new Promise((resolve, reject) => {
      const req = idx.get(hash);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  }

  /**
   * Remove a cached agent config.
   * @param {string} agentId
   */
  async evictAgentConfig(agentId) {
    const db = await this.ready();
    await _del(db, 'agent_config', agentId);
  }

  // ── Gen UI pages (v3) — user-created dashboards ────────────────────

  /**
   * List all genui pages, sorted by order then updated_at desc.
   * @returns {Promise<Array>}
   */
  async listGenuis() {
    const db = await this.ready();
    const all = await _getAll(db, 'genui_pages');
    return all.sort((a, b) => {
      const ao = typeof a.order === 'number' ? a.order : 0;
      const bo = typeof b.order === 'number' ? b.order : 0;
      if (ao !== bo) return ao - bo;
      const at = Date.parse(a.updated_at || a.created_at || 0);
      const bt = Date.parse(b.updated_at || b.created_at || 0);
      return bt - at;
    });
  }

  /**
   * Get a single genui page by slug.
   * @param {string} slug
   * @returns {Promise<Object|null>}
   */
  async getGenui(slug) {
    const db = await this.ready();
    return _get(db, 'genui_pages', slug);
  }

  /**
   * Save (upsert) a genui page's metadata.
   * @param {Object} page — { slug, title, description, agent_id, order, ... }
   * @returns {Promise<Object>} saved doc
   */
  async saveGenui(page) {
    const db = await this.ready();
    const slug = page.slug;
    const existing = await _get(db, 'genui_pages', slug);
    const now = new Date().toISOString();
    const doc = {
      ...(existing || {}),
      ...page,
      slug,
      created_at: existing ? existing.created_at : now,
      updated_at: now,
    };
    await _put(db, 'genui_pages', doc);
    return doc;
  }

  /**
   * Save (upsert) a genui page's HTML content.
   * @param {string} slug
   * @param {string} html — the full rendered HTML
   */
  async saveGenuiHtml(slug, html) {
    const db = await this.ready();
    await _put(db, 'genui_html', {
      slug,
      html,
      saved_at: new Date().toISOString(),
      expires_at: new Date(
        Date.now() + _lifecyclePolicy.generated_html_ttl_seconds * 1000,
      ).toISOString(),
    });
  }

  /**
   * Get a genui page's HTML content.
   * @param {string} slug
   * @returns {Promise<string|null>}
   */
  async getGenuiHtml(slug) {
    const db = await this.ready();
    const row = await _get(db, 'genui_html', slug);
    if (row?.expires_at && Date.parse(row.expires_at) <= Date.now()) {
      await _del(db, 'genui_html', slug);
      return null;
    }
    return row ? row.html : null;
  }

  /**
   * Delete a genui page (metadata + html).
   * @param {string} slug
   */
  async deleteGenui(slug) {
    const db = await this.ready();
    await Promise.all([
      _del(db, 'genui_pages', slug),
      _del(db, 'genui_html', slug),
    ]);
  }

  // ── User files (v4) — saved reports, images, exports ─────────────

  /**
   * List all saved user files, optionally filtered by room.
   * @param {string} [room] — e.g. 'files', 'screenshots'
   * @returns {Promise<Array>}
   */
  async listUserFiles(room) {
    const db = await this.ready();
    if (room) {
      return _getAllByIndex(db, 'user_files', 'by_room', room);
    }
    return _getAll(db, 'user_files');
  }

  /**
   * Get a single user file by its path.
   * @param {string} path — e.g. 'files/report.md'
   * @returns {Promise<Object|null>}
   */
  async getUserFile(path) {
    const db = await this.ready();
    return _get(db, 'user_files', path);
  }

  /**
   * Save (upsert) a user file. Keeps the bytes locally for instant access.
   * @param {Object} file — { path, room, filename, content, mime_type, size, ... }
   * @returns {Promise<Object>} saved doc
   */
  async saveUserFile(file) {
    const db = await this.ready();
    const path = file.path;
    const existing = await _get(db, 'user_files', path);
    const now = new Date().toISOString();
    const doc = {
      ...(existing || {}),
      ...file,
      path,
      room: file.room || 'files',
      created_at: existing ? existing.created_at : now,
      updated_at: now,
    };
    await _put(db, 'user_files', doc);
    return doc;
  }

  /**
   * Delete a user file.
   * @param {string} path
   */
  async deleteUserFile(path) {
    const db = await this.ready();
    await _del(db, 'user_files', path);
  }

  async listSyncOutbox() {
    const db = await this.ready();
    return _getAll(db, 'sync_outbox');
  }

  async removeSyncOutbox(sessionId, mutationId) {
    const db = await this.ready();
    const current = await _get(db, 'sync_outbox', sessionId);
    if (current && current.mutation_id === mutationId) {
      await _del(db, 'sync_outbox', sessionId);
      return true;
    }
    return false;
  }

  // ── Tool detail cache (v4) — lazy-loaded turn-detail responses ──────

  /**
   * Store a /session-turn-detail response for one interaction id.
   * @param {string} id — the assistant interaction id
   * @param {Object} detail — { output, metadata, tools: [...] }
   */
  async cacheToolDetail(id, detail) {
    void id;
    void detail;
    return false;
  }

  /**
   * Retrieve a cached tool detail by interaction id.
   * @param {string} id
   * @returns {Promise<Object|null>}
   */
  async getToolDetail(id) {
    void id;
    return null;
  }

  /**
   * Apply TTL and quota to validated server-authority cache rows. Dirty rows
   * and browser-authority transcripts are never discarded by cache eviction.
   */
  async enforceCachePolicy({ now = Date.now(), maxBytes = 50 * 1024 * 1024 } = {}) {
    const cleanup = await this.enforceLifecyclePolicy({ now });
    const sessions = await this.listSessions();
    const evictable = sessions
      .filter(row => row._authority === 'server' && !row._dirty)
      .sort((a, b) => Date.parse(a.last_accessed_at || 0) - Date.parse(b.last_accessed_at || 0));
    let evicted = 0;
    for (const row of evictable) {
      if (row.cache_expires_at && Date.parse(row.cache_expires_at) <= now) {
        await this.deleteCachedSession(row.id);
        evicted += 1;
      }
    }
    let bytes = await this.estimateSizeBytes();
    for (const row of evictable) {
      if (bytes <= maxBytes) break;
      if (!(await this.getSession(row.id))) continue;
      await this.deleteCachedSession(row.id);
      evicted += 1;
      bytes = await this.estimateSizeBytes();
    }
    return { evicted, bytes, quotaExceeded: bytes > maxBytes, cleanup };
  }

  /**
   * Sweep only TTL-safe derived/terminal rows. Pending outbox mutations,
   * memories, and user files have stronger durability and are never expired.
   */
  async enforceLifecyclePolicy({ now = Date.now(), limit = 250 } = {}) {
    const db = await this.ready();
    const before = await this.estimateSizeBytes();
    const removals = {
      agent_config: [],
      session_runs: [],
      genui_html: [],
      tool_details: [],
    };
    const errors = [];
    let rowsRemoved = 0;
    try {
      const [configs, runs, htmlRows, toolRows] = await Promise.all([
        _getAll(db, 'agent_config'),
        _getAll(db, 'session_runs'),
        _getAll(db, 'genui_html'),
        _getAll(db, 'tool_details'),
      ]);
      for (const row of configs) {
        const expiry = Date.parse(row.expires_at || 0)
          || Date.parse(row.cached_at || 0) + _lifecyclePolicy.metadata_ttl_seconds * 1000;
        if (expiry <= now && removals.agent_config.length < limit) {
          removals.agent_config.push(row.id);
        }
      }
      for (const row of runs) {
        if (row.status === 'running') continue;
        const expiry = Date.parse(row.expires_at || 0)
          || Date.parse(row.finished_at || row.started_at || 0)
            + _lifecyclePolicy.run_state_ttl_seconds * 1000;
        if (expiry <= now && removals.session_runs.length < limit) {
          removals.session_runs.push(row.id);
        }
      }
      for (const row of htmlRows) {
        const expiry = Date.parse(row.expires_at || 0)
          || Date.parse(row.saved_at || 0)
            + _lifecyclePolicy.generated_html_ttl_seconds * 1000;
        if (expiry <= now && removals.genui_html.length < limit) {
          removals.genui_html.push(row.slug);
        }
      }
      removals.tool_details.push(...toolRows.slice(0, limit).map(row => row.id));
      const names = Object.entries(removals)
        .filter(([, keys]) => keys.length)
        .map(([name]) => name);
      if (names.length) {
        await new Promise((resolve, reject) => {
          const tx = db.transaction(names, 'readwrite');
          for (const [name, keys] of Object.entries(removals)) {
            if (!keys.length) continue;
            const store = tx.objectStore(name);
            for (const key of keys) store.delete(key);
          }
          tx.oncomplete = resolve;
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error || new Error('Lifecycle cleanup aborted'));
        });
        rowsRemoved = Object.values(removals).reduce(
          (count, keys) => count + keys.length,
          0,
        );
      }
    } catch (error) {
      errors.push(String(error?.message || error));
    }
    const after = await this.estimateSizeBytes();
    return {
      last_cleanup_at: new Date(now).toISOString(),
      rows_removed: rowsRemoved,
      bytes_removed: Math.max(0, before - after),
      errors,
    };
  }

  async deleteCachedSession(sessionId) {
    const db = await this.ready();
    const interactions = await _getAllByIndex(db, 'interactions', 'by_session', sessionId);
    const tx = db.transaction(['interactions', 'sessions'], 'readwrite');
    for (const msg of interactions) tx.objectStore('interactions').delete(msg.id);
    tx.objectStore('sessions').delete(sessionId);
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  // ── Bulk / maintenance ──────────────────────────────────────────────

  /**
   * Wipe all data from every store. Equivalent to clearing browser storage
   * for this origin. Use with caution.
   */
  async clearAll() {
    const db = await this.ready();
    await Promise.all([
      _clear(db, 'sessions'),
      _clear(db, 'interactions'),
      _clear(db, 'agent_config'),
      _clear(db, 'session_runs'),
      _clear(db, 'memories'),
      _clear(db, 'genui_pages'),
      _clear(db, 'genui_html'),
      _clear(db, 'user_files'),
      _clear(db, 'tool_details'),
      _clear(db, 'sync_outbox'),
    ]);
  }

  /**
   * Export every store in the current tenant database. Blob values are encoded
   * so a JSON download preserves browser-authority attachments/user files.
   */
  async exportAll() {
    const db = await this.ready();
    const names = Array.from(db.objectStoreNames);
    const raw = {};
    // One transaction is the consistency boundary for this database.
    await new Promise((resolve, reject) => {
      const tx = db.transaction(names, 'readonly');
      for (const name of names) {
        const request = tx.objectStore(name).getAll();
        request.onsuccess = () => { raw[name] = request.result || []; };
        request.onerror = () => reject(request.error);
      }
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error('Browser export transaction aborted'));
    });
    const stores = {};
    for (const name of names) {
      stores[name] = [];
      for (const row of raw[name]) stores[name].push(await _exportValue(row));
    }
    return {
      format: 'webagent-browser-export',
      version: 2,
      owner_scope: this.ownerScope,
      database: {
        name: db.name,
        version: db.version,
        stores,
      },
    };
  }

  /**
   * Return storage usage stats for debugging/display.
   * @returns {Promise<{sessions: number, interactions: number, agent_config: number}>}
   */
  async stats() {
    const db = await this.ready();
    const [sessions, interactions, agent_config, session_runs, memories, genui_pages, genui_html, user_files, tool_details, sync_outbox] = await Promise.all([
      _count(db, 'sessions'),
      _count(db, 'interactions'),
      _count(db, 'agent_config'),
      _count(db, 'session_runs'),
      _count(db, 'memories'),
      _count(db, 'genui_pages'),
      _count(db, 'genui_html'),
      _count(db, 'user_files'),
      _count(db, 'tool_details'),
      _count(db, 'sync_outbox'),
    ]);
    return { sessions, interactions, agent_config, session_runs, memories, genui_pages, genui_html, user_files, tool_details, sync_outbox };
  }

  /**
   * Estimate the total size of stored data (in bytes). Iterates all stores
   * and sums the approximate byte length of each serialized object.
   * Rough and slow for large datasets — primarily a diagnostics tool.
   * @returns {Promise<number>}
   */
  async estimateSizeBytes() {
    const db = await this.ready();
    let total = 0;
    for (const name of SESSION_DB_STORES) {
      const all = await _getAll(db, name);
      for (const obj of all) {
        total += _valueSize(obj);
      }
    }
    return total;
  }
}

const _SECRET_FIELD = /(?:^|_)(?:access_?token|refresh_?token|remember_?token|authorization|api_?key|secret|password|private_?key|signing_?key|capability_?token)(?:$|_)/i;

async function _exportValue(value, key = '') {
  if (_SECRET_FIELD.test(key)) return '[REDACTED]';
  if (value instanceof Blob) {
    const bytes = new Uint8Array(await value.arrayBuffer());
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return {
      __webagent_blob__: true,
      type: value.type || 'application/octet-stream',
      size: value.size,
      sha256: Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join(''),
      base64: btoa(binary),
    };
  }
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    const bytes = value instanceof ArrayBuffer
      ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return {
      __webagent_bytes__: true,
      size: bytes.byteLength,
      sha256: Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join(''),
      base64: btoa(binary),
    };
  }
  if (Array.isArray(value)) return Promise.all(value.map(item => _exportValue(item)));
  if (value && typeof value === 'object') {
    const copy = {};
    for (const [childKey, child] of Object.entries(value)) {
      copy[childKey] = await _exportValue(child, childKey);
    }
    return copy;
  }
  return value;
}

function _valueSize(value) {
  if (value instanceof Blob) return value.size;
  if (value instanceof ArrayBuffer) return value.byteLength;
  if (ArrayBuffer.isView(value)) return value.byteLength;
  if (Array.isArray(value)) return value.reduce((total, item) => total + _valueSize(item), 0);
  if (value && typeof value === 'object') {
    return Object.entries(value).reduce(
      (total, [key, child]) => total + new TextEncoder().encode(key).length + _valueSize(child),
      0,
    );
  }
  return new TextEncoder().encode(String(value ?? '')).length;
}

/**
 * Singleton instance — the entire app shares one IndexedDB connection.
 */
const defaultSessionDB = new SessionDB();
export default defaultSessionDB;
