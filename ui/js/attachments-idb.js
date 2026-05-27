'use strict';

/**
 * IndexedDB-backed attachment storage for the "browser" attachment backend.
 *
 * Schema: one store, `attachments`, keyed by attachment_id. Each record:
 *   { id: string, blob: Blob, mime_type: string, name: string, size: number, created: number }
 *
 * Object URLs are minted on demand via getObjectUrl(id) and cached in a
 * Map so repeated renders of the same attachment in chat bubbles don't
 * leak. Callers can revokeAllObjectUrls() on logout / session reset.
 */

const DB_NAME = 'webagent-attachments';
const DB_VERSION = 1;
const STORE = 'attachments';

let _dbPromise = null;
const _urlCache = new Map(); // id -> object URL

function _openDb() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) {
      reject(new Error('IndexedDB is not available in this browser.'));
      return;
    }
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: 'id' });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error('IndexedDB open failed'));
  });
  return _dbPromise;
}

function _tx(mode) {
  return _openDb().then(db => {
    const tx = db.transaction(STORE, mode);
    return { tx, store: tx.objectStore(STORE) };
  });
}

export async function putAttachment({ id, blob, mime_type, name, size }) {
  const { tx, store } = await _tx('readwrite');
  return new Promise((resolve, reject) => {
    const req = store.put({
      id,
      blob,
      mime_type,
      name,
      size,
      created: Date.now(),
    });
    req.onsuccess = () => resolve(true);
    req.onerror = () => reject(req.error || new Error('IndexedDB put failed'));
    tx.oncomplete = () => {};
  });
}

export async function getAttachment(id) {
  const { store } = await _tx('readonly');
  return new Promise((resolve, reject) => {
    const req = store.get(id);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error || new Error('IndexedDB get failed'));
  });
}

export async function deleteAttachment(id) {
  const cached = _urlCache.get(id);
  if (cached) {
    try { URL.revokeObjectURL(cached); } catch {}
    _urlCache.delete(id);
  }
  const { store } = await _tx('readwrite');
  return new Promise((resolve, reject) => {
    const req = store.delete(id);
    req.onsuccess = () => resolve(true);
    req.onerror = () => reject(req.error || new Error('IndexedDB delete failed'));
  });
}

export async function getObjectUrl(id) {
  const cached = _urlCache.get(id);
  if (cached) return cached;
  const rec = await getAttachment(id);
  if (!rec || !rec.blob) return null;
  const url = URL.createObjectURL(rec.blob);
  _urlCache.set(id, url);
  return url;
}

export function revokeAllObjectUrls() {
  for (const url of _urlCache.values()) {
    try { URL.revokeObjectURL(url); } catch {}
  }
  _urlCache.clear();
}

export async function getStats() {
  try {
    const { store } = await _tx('readonly');
    return new Promise((resolve) => {
      const req = store.count();
      req.onsuccess = () => resolve({ count: req.result });
      req.onerror = () => resolve({ count: 0 });
    });
  } catch {
    return { count: 0 };
  }
}
