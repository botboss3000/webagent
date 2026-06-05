'use strict';

// Wiki tab — the company-wide, searchable knowledge base.
// Markup: ui/wiki.html · styles: ui/css/wiki.css · API: app/api/wiki.py.
// One shared collection of articles (NOT per-user). People search/browse/edit
// here; agents manage the same data through the wiki_control ability. Lifecycle
// (startWiki/stopWiki) is driven by ui/js/tabs.js.

import { app } from './state.js';
import { apiPath } from './config.js';

let _active = false;
let _inited = false;

// View state for the article pane.
let _current = null;   // the article being viewed, or null for a new draft
let _editing = false;

let _searchTimer = null;
let _toastTimer = null;

// ── Small DOM helpers ────────────────────────────────────────────────────────

function _root() { return document.getElementById('tab-wiki'); }
function _q(sel) { const r = _root(); return r ? r.querySelector(sel) : null; }
function _qa(sel) { const r = _root(); return r ? Array.from(r.querySelectorAll(sel)) : []; }

function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _toast(msg, isError) {
  const el = _q('.wiki-toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

async function _api(path, opts) {
  const res = await fetch(apiPath(path), opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

// ── Lifecycle ────────────────────────────────────────────────────────────────

export function startWiki() {
  _active = true;
  _init();
  // Always return to the list view when (re)entering the tab.
  _showList();
  _refresh();
}

export function stopWiki() {
  _active = false;
  clearTimeout(_searchTimer);
}

function _init() {
  if (_inited) return;
  const root = _root();
  if (!root) return;
  _inited = true;

  // Search (debounced) + clear button.
  const search = _q('.wiki-search-input');
  const clear = _q('.wiki-search-clear');
  if (search) {
    search.addEventListener('input', () => {
      if (clear) clear.hidden = !search.value;
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => _refresh(), 220);
    });
  }
  if (clear && search) {
    clear.addEventListener('click', () => {
      search.value = '';
      clear.hidden = true;
      _refresh();
      search.focus();
    });
  }

  // Toolbar actions (both views) via delegation.
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || !root.contains(btn)) return;
    const act = btn.dataset.act;
    if (act === 'new') _openNew();
    else if (act === 'back') { _showList(); _refresh(); }
    else if (act === 'edit') _enterEdit();
    else if (act === 'cancel') _cancelEdit();
    else if (act === 'save') _save();
    else if (act === 'delete') _delete();
  });

  // Click an article row to open it.
  const list = _q('.wiki-list');
  if (list) {
    list.addEventListener('click', (e) => {
      const row = e.target.closest('.wiki-row');
      if (row && row.dataset.slug) _openArticle(row.dataset.slug);
    });
  }
}

// ── List + search ────────────────────────────────────────────────────────────

async function _refresh() {
  const search = _q('.wiki-search-input');
  const query = search ? search.value.trim() : '';
  const list = _q('.wiki-list');
  const empty = _q('.wiki-empty');
  const count = _q('.wiki-count');
  if (!list) return;

  list.innerHTML = '<li class="wiki-loading">Loading&hellip;</li>';
  if (empty) empty.classList.remove('show');
  if (count) count.hidden = true;

  try {
    let articles;
    if (query) {
      const data = await _api(`/api/v1/wiki/search?q=${encodeURIComponent(query)}`);
      articles = (data && data.results) || [];
    } else {
      const data = await _api('/api/v1/wiki');
      articles = (data && data.articles) || [];
    }
    if (!_active) return;
    _renderList(articles, query);
  } catch (e) {
    if (!_active) return;
    list.innerHTML = `<li class="wiki-loading">Couldn't load the wiki: ${_esc(e.message)}</li>`;
  }
}

function _renderList(articles, query) {
  const list = _q('.wiki-list');
  const empty = _q('.wiki-empty');
  const count = _q('.wiki-count');
  if (!list) return;

  if (!articles.length) {
    list.innerHTML = '';
    if (empty) {
      empty.classList.add('show');
      const title = empty.querySelector('.wiki-empty-title');
      const text = empty.querySelector('.wiki-empty-text');
      if (query) {
        if (title) title.textContent = 'No matching articles';
        if (text) text.textContent = `Nothing in the wiki matches "${query}". Try different words, or add a new article.`;
      } else {
        if (title) title.textContent = 'The wiki is empty';
        if (text) text.textContent = 'Add your first article — company info, a policy, contacts, anything worth keeping. You can also ask an agent (with the Wiki ability) to fill it in for you.';
      }
    }
    return;
  }
  if (empty) empty.classList.remove('show');
  if (count) {
    count.hidden = false;
    count.textContent = query
      ? `${articles.length} result${articles.length === 1 ? '' : 's'} for "${query}"`
      : `${articles.length} article${articles.length === 1 ? '' : 's'}`;
  }

  list.innerHTML = articles.map(_rowHtml).join('');
}

function _rowHtml(a) {
  const tags = Array.isArray(a.tags) ? a.tags : [];
  const chips = [];
  if (a.category) chips.push(`<span class="wiki-chip wiki-chip-category">${_esc(a.category)}</span>`);
  for (const t of tags.slice(0, 5)) chips.push(`<span class="wiki-chip">${_esc(t)}</span>`);
  const snippet = a.snippet ? `<p class="wiki-row-snippet">${_esc(a.snippet)}</p>` : '';
  const meta = chips.length ? `<div class="wiki-row-meta">${chips.join('')}</div>` : '';
  return `<li class="wiki-row" data-slug="${_esc(a.slug)}">
    <div class="wiki-row-title">${_esc(a.title)}</div>
    ${snippet}${meta}
  </li>`;
}

// ── View switching ───────────────────────────────────────────────────────────

function _showList() {
  _current = null;
  _editing = false;
  const lv = _q('.wiki-list-view');
  const av = _q('.wiki-article-view');
  if (lv) lv.hidden = false;
  if (av) av.hidden = true;
}

function _showArticle() {
  const lv = _q('.wiki-list-view');
  const av = _q('.wiki-article-view');
  if (lv) lv.hidden = true;
  if (av) av.hidden = false;
}

// ── Open / read an article ───────────────────────────────────────────────────

async function _openArticle(slug) {
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(slug)}`);
    _current = data.article;
    _editing = false;
    _showArticle();
    _renderReader();
  } catch (e) {
    _toast(`Couldn't open article: ${e.message}`, true);
  }
}

function _renderReader() {
  const a = _current || {};
  _q('.wiki-reader').hidden = false;
  _q('.wiki-editor').hidden = true;
  _q('.wiki-reader-title').textContent = a.title || 'Untitled';

  const cat = _q('.wiki-reader-category');
  if (cat) {
    if (a.category) { cat.hidden = false; cat.textContent = a.category; cat.className = 'wiki-chip wiki-chip-category wiki-reader-category'; }
    else cat.hidden = true;
  }
  const tagsEl = _q('.wiki-reader-tags');
  if (tagsEl) {
    const tags = Array.isArray(a.tags) ? a.tags : [];
    tagsEl.innerHTML = tags.map(t => `<span class="wiki-chip">${_esc(t)}</span>`).join('');
  }
  const body = _q('.wiki-reader-body');
  if (body) body.textContent = a.body || '';

  _setToolbarMode('reading');
}

// ── Edit / new ───────────────────────────────────────────────────────────────

function _openNew() {
  // Reset any active search so the new article isn't hidden by a stale filter
  // when the user returns to the list after saving.
  const search = _q('.wiki-search-input');
  if (search && search.value) {
    search.value = '';
    const clear = _q('.wiki-search-clear');
    if (clear) clear.hidden = true;
  }
  _current = null;            // null slug = create on save
  _editing = true;
  _showArticle();
  _fillEditor({ title: '', body: '', tags: [], category: '' });
  _q('.wiki-reader').hidden = true;
  _q('.wiki-editor').hidden = false;
  _setToolbarMode('editing');
  const t = _q('.wiki-editor-title');
  if (t) t.focus();
}

function _enterEdit() {
  if (!_current) return;
  _editing = true;
  _fillEditor(_current);
  _q('.wiki-reader').hidden = true;
  _q('.wiki-editor').hidden = false;
  _setToolbarMode('editing');
}

function _cancelEdit() {
  _editing = false;
  if (_current) {
    _renderReader();
  } else {
    _showList();
    _refresh();
  }
}

function _fillEditor(a) {
  const tags = Array.isArray(a.tags) ? a.tags : [];
  _q('.wiki-editor-title').value = a.title || '';
  _q('.wiki-editor-category').value = a.category || '';
  _q('.wiki-editor-tags').value = tags.join(', ');
  _q('.wiki-editor-body').value = a.body || '';
}

async function _save() {
  const title = _q('.wiki-editor-title').value.trim();
  if (!title) { _toast('Give the article a title first.', true); return; }
  const body = _q('.wiki-editor-body').value;
  const category = _q('.wiki-editor-category').value.trim();
  const tags = _q('.wiki-editor-tags').value
    .split(',').map(s => s.trim()).filter(Boolean);
  const userId = app.currentUserId || '';

  const saveBtn = _q('[data-act="save"]');
  if (saveBtn) saveBtn.disabled = true;
  try {
    let data;
    if (_current && _current.slug) {
      data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, user_id: userId }),
      });
    } else {
      data = await _api('/api/v1/wiki', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, user_id: userId }),
      });
    }
    _current = data.article;
    _editing = false;
    _renderReader();
    _toast('Saved.');
  } catch (e) {
    _toast(`Couldn't save: ${e.message}`, true);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function _delete() {
  if (!_current || !_current.slug) { _showList(); return; }
  if (!window.confirm(`Delete "${_current.title}"? This removes it for everyone and can't be undone.`)) return;
  try {
    await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, { method: 'DELETE' });
    _toast('Deleted.');
    _showList();
    _refresh();
  } catch (e) {
    _toast(`Couldn't delete: ${e.message}`, true);
  }
}

// Toggle which toolbar buttons show for reading vs editing.
function _setToolbarMode(mode) {
  const editing = mode === 'editing';
  const show = (sel, on) => { const el = _q(sel); if (el) el.hidden = !on; };
  show('[data-act="edit"]', !editing);
  show('[data-act="delete"]', !editing);
  show('[data-act="save"]', editing);
  show('[data-act="cancel"]', editing);
}
