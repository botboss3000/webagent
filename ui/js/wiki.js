'use strict';

// Wiki tab — the company-wide, searchable knowledge base.
// Markup: ui/wiki.html · styles: ui/css/wiki.css · API: app/api/wiki.py.
// One shared collection of articles (NOT per-user). People search/browse/edit
// here; agents manage the same data through the wiki_control ability. Lifecycle
// (startWiki/stopWiki) is driven by ui/js/tabs.js.
//
// Layout is a persistent encyclopedia shell: a banner on top, a TREE SIDEBAR on
// the left (articles grouped by category, collapsible), and the ARTICLE on the
// right. Searching or applying a tag/category filter re-shapes the sidebar; the
// article surface on the right stays put.

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

// A signed-in member (not an anonymous visitor) — gates the write controls and
// whether drafts are even returned by the API. Anonymous users get an 'anon_…'
// id; members get a real one (email / 'admin_default').
function _isMember() {
  const uid = String(app.currentUserId || '');
  return !!uid && !uid.startsWith('anon_');
}

let _active = false;
let _inited = false;

// View state for the article surface.
let _current = null;   // the article being viewed, or null when nothing is open
let _editing = false;

let _searchTimer = null;
let _toastTimer = null;

// Browse + linking state.
let _allArticles = [];            // full (unfiltered) browse list
let _index = new Map();           // lowercased title/slug -> {slug, title}
let _activeFilter = null;         // { type:'category'|'tag', value } or null
let _viewingRevisionId = null;    // set while reading an old revision (read-only)
let _searchResults = null;        // ranked results while a search is active, else null
let _collapsed = new Set();       // category names currently collapsed in the tree

const UNCATEGORIZED = 'General';  // tree group label for articles with no category

// ── Small DOM helpers ────────────────────────────────────────────────────────

function _root() { return document.getElementById('tab-wiki'); }
function _q(sel) { const r = _root(); return r ? r.querySelector(sel) : null; }

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
  // Always send the auth token so the backend knows whether the caller is a
  // member (sees drafts, may edit) or an anonymous visitor (published only).
  opts = opts || {};
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
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
  _applyRoleVisibility();
  _refresh();
}

// Hide the editing controls (New, and the per-article actions) from anonymous
// visitors. The backend enforces this too; this just keeps the UI honest —
// anon users get a read-only, published-only wiki.
function _applyRoleVisibility() {
  const member = _isMember();
  const root = _root();
  if (!root) return;
  const newBtn = root.querySelector('.wiki-new-btn');
  if (newBtn) newBtn.hidden = !member;
  const welcomeNew = root.querySelector('.wiki-welcome [data-act="new"]');
  if (welcomeNew) welcomeNew.style.display = member ? '' : 'none';
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
      if (search.value.trim()) _activeFilter = null;   // searching overrides a chip filter
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => _runSearch(), 220);
    });
  }
  if (clear && search) {
    clear.addEventListener('click', () => {
      search.value = '';
      clear.hidden = true;
      _searchResults = null;
      _renderSidebar();
      search.focus();
    });
  }

  // Delegated clicks for the whole tab.
  root.addEventListener('click', (e) => {
    // 1) A clickable [[wiki-link]] or backlink inside the reader.
    const link = e.target.closest('.wiki-link, .wiki-backlink');
    if (link && root.contains(link)) {
      e.preventDefault();
      if (link.dataset.slug) _openArticle(link.dataset.slug);
      else if (link.dataset.newtitle) _openNew(link.dataset.newtitle);
      return;
    }
    // 2) A tree category header → collapse / expand.
    const cat = e.target.closest('.wiki-tree-cat');
    if (cat && root.contains(cat)) {
      e.preventDefault();
      _toggleCategory(cat.dataset.cat);
      return;
    }
    // 3) A tree article link (sidebar) → open it.
    const item = e.target.closest('.wiki-tree-item');
    if (item && item.dataset.slug) {
      e.preventDefault();
      _openArticle(item.dataset.slug);
      return;
    }
    // 4) A category/tag filter chip (on a row/reader) → filter the sidebar.
    const fchip = e.target.closest('[data-filter-type]');
    if (fchip && root.contains(fchip)) {
      e.preventDefault();
      e.stopPropagation();
      _applyFilter(fchip.dataset.filterType, fchip.dataset.filterValue);
      return;
    }
    // 5) A revision row's actions.
    const revAct = e.target.closest('[data-rev-act]');
    if (revAct && root.contains(revAct)) {
      e.preventDefault();
      const id = revAct.dataset.revId;
      if (revAct.dataset.revAct === 'view') _viewRevision(id);
      else if (revAct.dataset.revAct === 'restore') _restoreRevision(id);
      return;
    }
    // 6) Toolbar / banner / welcome actions.
    const btn = e.target.closest('[data-act]');
    if (btn && root.contains(btn)) {
      const act = btn.dataset.act;
      if (act === 'new') _openNew();
      else if (act === 'edit') _enterEdit();
      else if (act === 'cancel') _cancelEdit();
      else if (act === 'save') _save();
      else if (act === 'delete') _delete();
      else if (act === 'toggle-status') _toggleStatus();
      else if (act === 'history') _toggleHistory();
      else if (act === 'clear-filter') { _activeFilter = null; _renderSidebar(); }
      else if (act === 'restore-this' && _viewingRevisionId) _restoreRevision(_viewingRevisionId);
      else if (act === 'exit-revision') _exitRevisionView();
      return;
    }
  });
}

// ── Link index (for resolving [[Title]] → slug) ───────────────────────────────

function _buildIndex(articles) {
  _index = new Map();
  for (const a of articles) {
    if (!a || !a.slug) continue;
    const ref = { slug: a.slug, title: a.title };
    _index.set(a.slug.toLowerCase(), ref);
    if (a.title) _index.set(a.title.toLowerCase(), ref);
  }
}

// ── Load articles + render the sidebar ────────────────────────────────────────

async function _refresh() {
  const tree = _q('.wiki-tree');
  if (tree) tree.innerHTML = '<div class="wiki-loading">Loading&hellip;</div>';
  try {
    const data = await _api('/api/v1/wiki');
    if (!_active) return;
    _allArticles = (data && data.articles) || [];
    _buildIndex(_allArticles);
    _renderSidebar();
  } catch (e) {
    if (!_active) return;
    if (tree) tree.innerHTML = `<div class="wiki-loading">Couldn't load the wiki: ${_esc(e.message)}</div>`;
  }
}

async function _runSearch() {
  const search = _q('.wiki-search-input');
  const query = search ? search.value.trim() : '';
  if (!query) { _searchResults = null; _renderSidebar(); return; }
  const tree = _q('.wiki-tree');
  if (tree) tree.innerHTML = '<div class="wiki-loading">Searching&hellip;</div>';
  try {
    const data = await _api(`/api/v1/wiki/search?q=${encodeURIComponent(query)}`);
    if (!_active) return;
    _searchResults = (data && data.results) || [];
    _renderSidebar();
  } catch (e) {
    if (!_active) return;
    if (tree) tree.innerHTML = `<div class="wiki-loading">Search failed: ${_esc(e.message)}</div>`;
  }
}

// Decide what the sidebar shows: search results > active filter > full tree.
function _renderSidebar() {
  const tree = _q('.wiki-tree');
  const countEl = _q('.wiki-sidebar-count');
  const filterBar = _q('.wiki-filter-active');
  if (!tree) return;

  // Active tag/category filter banner.
  if (_activeFilter && !_searchResults) {
    if (filterBar) {
      filterBar.hidden = false;
      const txt = filterBar.querySelector('.wiki-filter-active-text');
      if (txt) txt.innerHTML = `Filtered by ${_activeFilter.type === 'tag' ? '#' : ''}${_esc(_activeFilter.value)}`;
    }
  } else if (filterBar) {
    filterBar.hidden = true;
  }

  // Search results: a flat, ranked list.
  if (_searchResults) {
    const results = _searchResults;
    if (countEl) { countEl.hidden = false; countEl.textContent = `${results.length} result${results.length === 1 ? '' : 's'}`; }
    if (!results.length) {
      tree.innerHTML = '<div class="wiki-tree-empty">No matching articles.</div>';
      return;
    }
    tree.innerHTML = '<ul class="wiki-tree-items wiki-tree-flat">'
      + results.map(a => _itemHtml(a, true)).join('') + '</ul>';
    _markActiveInTree();
    return;
  }

  // Tag/category filter: a flat list of matches.
  if (_activeFilter) {
    const matches = _allArticles.filter(a => _matchesFilter(a, _activeFilter));
    if (countEl) { countEl.hidden = false; countEl.textContent = `${matches.length} article${matches.length === 1 ? '' : 's'}`; }
    if (!matches.length) {
      tree.innerHTML = '<div class="wiki-tree-empty">Nothing matches this filter.</div>';
      return;
    }
    tree.innerHTML = '<ul class="wiki-tree-items wiki-tree-flat">'
      + matches.map(a => _itemHtml(a)).join('') + '</ul>';
    _markActiveInTree();
    return;
  }

  // Default: the full category tree.
  if (countEl) {
    countEl.hidden = false;
    countEl.textContent = `${_allArticles.length} article${_allArticles.length === 1 ? '' : 's'}`;
  }
  if (!_allArticles.length) {
    tree.innerHTML = '<div class="wiki-tree-empty">No articles yet.<br>Use <b>New</b> above to start one.</div>';
    return;
  }
  tree.innerHTML = _treeHtml(_allArticles);
  _markActiveInTree();
}

// Group articles by category and render collapsible groups.
function _treeHtml(articles) {
  const groups = new Map();
  for (const a of articles) {
    const key = a.category && a.category.trim() ? a.category.trim() : UNCATEGORIZED;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(a);
  }
  // Sort categories alphabetically, but keep "General" (uncategorized) last.
  const cats = [...groups.keys()].sort((x, y) => {
    if (x === UNCATEGORIZED) return 1;
    if (y === UNCATEGORIZED) return -1;
    return x.localeCompare(y);
  });
  return cats.map((cat) => {
    const items = groups.get(cat).sort((x, y) => (x.title || '').localeCompare(y.title || ''));
    const collapsed = _collapsed.has(cat);
    return `<div class="wiki-tree-group${collapsed ? ' collapsed' : ''}">
      <button class="wiki-tree-cat" data-cat="${_esc(cat)}" aria-expanded="${collapsed ? 'false' : 'true'}">
        <span class="wiki-tree-caret">&#9656;</span>
        <span class="wiki-tree-cat-name">${_esc(cat)}</span>
        <span class="wiki-tree-cat-count">${items.length}</span>
      </button>
      <ul class="wiki-tree-items">${items.map(a => _itemHtml(a)).join('')}</ul>
    </div>`;
  }).join('');
}

function _itemHtml(a, withSnippet) {
  const snip = withSnippet && a.snippet
    ? `<span class="wiki-tree-snippet">${_esc(a.snippet)}</span>` : '';
  // Drafts (internal) get a small dot so members can tell them apart at a glance.
  const isDraft = (a.status || 'draft') !== 'published';
  const draftDot = isDraft ? '<span class="wiki-tree-draft-dot" title="Draft (internal)"></span>' : '';
  const cls = 'wiki-tree-item' + (isDraft ? ' is-draft' : '');
  return `<li><a class="${cls}" data-slug="${_esc(a.slug)}" title="${_esc(a.title)}">`
    + `<span class="wiki-tree-item-title">${draftDot}${_esc(a.title)}</span>${snip}</a></li>`;
}

function _toggleCategory(cat) {
  if (!cat) return;
  if (_collapsed.has(cat)) _collapsed.delete(cat);
  else _collapsed.add(cat);
  _renderSidebar();
}

// Highlight the open article's row in the sidebar.
function _markActiveInTree() {
  const tree = _q('.wiki-tree');
  if (!tree) return;
  tree.querySelectorAll('.wiki-tree-item.active').forEach(el => el.classList.remove('active'));
  if (!_current || !_current.slug) return;
  const el = tree.querySelector(`.wiki-tree-item[data-slug="${CSS.escape(_current.slug)}"]`);
  if (el) el.classList.add('active');
}

function _matchesFilter(a, f) {
  if (!f) return true;
  if (f.type === 'category') return (a.category || '') === f.value;
  if (f.type === 'tag') return Array.isArray(a.tags) && a.tags.includes(f.value);
  return true;
}

function _applyFilter(type, value) {
  // Clicking the active chip again clears the filter.
  if (_activeFilter && _activeFilter.type === type && _activeFilter.value === value) {
    _activeFilter = null;
  } else {
    _activeFilter = { type, value };
  }
  // Clear any search so the filter is what drives the sidebar.
  const search = _q('.wiki-search-input');
  if (search && search.value) { search.value = ''; const c = _q('.wiki-search-clear'); if (c) c.hidden = true; }
  _searchResults = null;
  _renderSidebar();
}

// ── Markdown rendering + [[wiki-links]] ───────────────────────────────────────

function _mdReady() {
  return !!(window.marked && typeof window.marked.parse === 'function'
         && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function');
}

// Convert an article body (Markdown + [[links]]) into safe HTML, or null when
// the libs are missing (caller falls back to plain text). [[Title]] / [[Title|label]]
// become links to a #wiki/<target> sentinel that _wireReaderLinks() resolves.
function _mdToHtml(body) {
  if (!body || !_mdReady()) return null;
  const pre = body.replace(/\[\[([^\]]+)\]\]/g, (m, inner) => {
    const bar = inner.indexOf('|');
    const target = (bar >= 0 ? inner.slice(0, bar) : inner).trim();
    const label = (bar >= 0 ? inner.slice(bar + 1) : inner).trim();
    if (!target) return m;
    return `[${label}](#wiki/${encodeURIComponent(target)})`;
  });
  let html;
  try { html = window.marked.parse(pre, { gfm: true, breaks: true }); }
  catch (_) { return null; }
  return window.DOMPurify.sanitize(html, { FORBID_ATTR: ['style'] });
}

// Turn #wiki/<target> sentinel links into resolved/missing wiki-links, and make
// every other link open safely in a new tab.
function _wireReaderLinks(rootEl) {
  rootEl.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    if (href.startsWith('#wiki/')) {
      const target = decodeURIComponent(href.slice('#wiki/'.length));
      const hit = _index.get(target.toLowerCase());
      a.classList.add('wiki-link');
      a.removeAttribute('href');
      if (hit) { a.dataset.slug = hit.slug; a.title = hit.title; }
      else { a.classList.add('wiki-link-missing'); a.dataset.newtitle = target; a.title = `Create "${target}"`; }
    } else {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    }
  });
}

// ── Surface switching (welcome ↔ article) ─────────────────────────────────────

function _showWelcome() {
  _current = null;
  _editing = false;
  _viewingRevisionId = null;
  const w = _q('.wiki-welcome');
  const art = _q('.wiki-article');
  if (w) w.hidden = false;
  if (art) art.hidden = true;
  _markActiveInTree();
}

function _showArticleSurface() {
  const w = _q('.wiki-welcome');
  const art = _q('.wiki-article');
  if (w) w.hidden = true;
  if (art) art.hidden = false;
}

// ── Open / read an article ───────────────────────────────────────────────────

async function _openArticle(slug) {
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(slug)}`);
    _current = data.article;
    _editing = false;
    _viewingRevisionId = null;
    _showArticleSurface();
    _renderReader();
    _loadBacklinks(slug);
    _markActiveInTree();
  } catch (e) {
    _toast(`Couldn't open article: ${e.message}`, true);
  }
}

// Render the reader from an article-like object {title, body, tags, category}.
function _renderReader(source) {
  const a = source || _current || {};
  _q('.wiki-reader').hidden = false;
  _q('.wiki-editor').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-reader-title').textContent = a.title || 'Untitled';

  // Draft/published badge — only the "Draft" state is called out (published is
  // the unremarkable default). Hidden while viewing an old revision.
  const statusEl = _q('.wiki-reader-status');
  if (statusEl) {
    const isDraft = (a.status || 'draft') !== 'published';
    if (isDraft && !_viewingRevisionId) {
      statusEl.hidden = false;
      statusEl.textContent = 'Draft · internal';
      statusEl.className = 'wiki-reader-status wiki-status-draft';
    } else {
      statusEl.hidden = true;
    }
  }

  const cat = _q('.wiki-reader-category');
  if (cat) {
    if (a.category) {
      cat.hidden = false; cat.textContent = a.category;
      cat.className = 'wiki-chip wiki-chip-category wiki-reader-category';
      cat.dataset.filterType = 'category'; cat.dataset.filterValue = a.category;
    } else { cat.hidden = true; }
  }
  const tagsEl = _q('.wiki-reader-tags');
  if (tagsEl) {
    const tags = Array.isArray(a.tags) ? a.tags : [];
    tagsEl.innerHTML = tags.map(t =>
      `<span class="wiki-chip" data-filter-type="tag" data-filter-value="${_esc(t)}">${_esc(t)}</span>`).join('');
  }
  const body = _q('.wiki-reader-body');
  if (body) {
    const html = _mdToHtml(a.body || '');
    if (html != null) {
      body.classList.add('md-body');
      body.innerHTML = html;
      _wireReaderLinks(body);
      try { if (window.Prism) window.Prism.highlightAllUnder(body); } catch (_) {}
    } else {
      body.classList.remove('md-body');
      body.textContent = a.body || '';
    }
  }
  _setToolbarMode('reading');
}

async function _loadBacklinks(slug) {
  const box = _q('.wiki-backlinks');
  const listEl = _q('.wiki-backlinks-list');
  if (!box || !listEl) return;
  box.hidden = true; listEl.innerHTML = '';
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(slug)}/backlinks`);
    const links = (data && data.backlinks) || [];
    if (!links.length || _viewingRevisionId) return;          // hide when empty / viewing old rev
    listEl.innerHTML = links.map(l =>
      `<a class="wiki-backlink" data-slug="${_esc(l.slug)}">${_esc(l.title)}</a>`).join('');
    box.hidden = false;
  } catch (_) { /* backlinks are best-effort */ }
}

// ── Revision history ──────────────────────────────────────────────────────────

async function _toggleHistory() {
  const panel = _q('.wiki-revisions');
  if (!panel || !_current) return;
  if (!panel.hidden) { panel.hidden = true; return; }
  const listEl = _q('.wiki-revisions-list');
  listEl.innerHTML = '<div class="wiki-loading">Loading history&hellip;</div>';
  panel.hidden = false;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/revisions`);
    const revs = (data && data.revisions) || [];
    if (!revs.length) {
      listEl.innerHTML = '<div class="wiki-revisions-empty">No earlier versions yet — history starts the first time this article is edited.</div>';
      return;
    }
    listEl.innerHTML = revs.map(r => {
      const when = _fmtDate(r.created_at);
      const who = r.edited_by ? ` · ${_esc(r.edited_by)}` : '';
      return `<div class="wiki-revision-row">
        <div class="wiki-revision-meta"><span class="wiki-revision-when">${when}</span><span class="wiki-revision-who">${who}</span></div>
        <div class="wiki-revision-snippet">${_esc(r.snippet || '')}</div>
        <div class="wiki-revision-actions">
          <button class="wiki-btn wiki-btn-sm" data-rev-act="view" data-rev-id="${_esc(r.id)}">View</button>
          <button class="wiki-btn wiki-btn-sm" data-rev-act="restore" data-rev-id="${_esc(r.id)}">Restore</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = `<div class="wiki-loading">Couldn't load history: ${_esc(e.message)}</div>`;
  }
}

async function _viewRevision(revId) {
  try {
    const data = await _api(`/api/v1/wiki/revisions/${encodeURIComponent(revId)}`);
    const rev = data && data.revision;
    if (!rev) return;
    _viewingRevisionId = revId;
    _renderReader(rev);                       // render the OLD content read-only
    const banner = _q('.wiki-revision-banner');
    const txt = _q('.wiki-revision-banner-text');
    if (txt) txt.textContent = `Viewing an old version from ${_fmtDate(rev.created_at)} — this is read-only.`;
    if (banner) banner.hidden = false;
    _q('.wiki-backlinks').hidden = true;
    _setToolbarMode('revision');
  } catch (e) {
    _toast(`Couldn't load revision: ${e.message}`, true);
  }
}

function _exitRevisionView() {
  _viewingRevisionId = null;
  const banner = _q('.wiki-revision-banner');
  if (banner) banner.hidden = true;
  if (_current) { _renderReader(); _loadBacklinks(_current.slug); }
}

async function _restoreRevision(revId) {
  if (!_current) return;
  if (!window.confirm('Restore this version? The current version is saved to history first, so you can undo this.')) return;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/restore/${encodeURIComponent(revId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId || '' }),
    });
    _current = data.article;
    _viewingRevisionId = null;
    const banner = _q('.wiki-revision-banner');
    if (banner) banner.hidden = true;
    _renderReader();
    _loadBacklinks(_current.slug);
    _toast('Restored.');
  } catch (e) {
    _toast(`Couldn't restore: ${e.message}`, true);
  }
}

function _fmtDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return _esc(iso);
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch (_) { return _esc(iso); }
}

// ── Edit / new ───────────────────────────────────────────────────────────────

function _openNew(prefillTitle) {
  _current = null;            // null slug = create on save
  _editing = true;
  _viewingRevisionId = null;
  const banner = _q('.wiki-revision-banner');
  if (banner) banner.hidden = true;
  _showArticleSurface();
  _fillEditor({ title: typeof prefillTitle === 'string' ? prefillTitle : '', body: '', tags: [], category: '' });
  _q('.wiki-reader').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-backlinks').hidden = true;
  _q('.wiki-editor').hidden = false;
  _setToolbarMode('editing');
  _markActiveInTree();
  const t = _q('.wiki-editor-title');
  if (t) t.focus();
}

function _enterEdit() {
  if (!_current) return;
  _editing = true;
  _fillEditor(_current);
  _q('.wiki-reader').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-backlinks').hidden = true;
  _q('.wiki-editor').hidden = false;
  _setToolbarMode('editing');
}

function _cancelEdit() {
  _editing = false;
  if (_current) {
    _renderReader();
    _loadBacklinks(_current.slug);
  } else {
    _showWelcome();
  }
}

function _fillEditor(a) {
  const tags = Array.isArray(a.tags) ? a.tags : [];
  _q('.wiki-editor-title').value = a.title || '';
  _q('.wiki-editor-category').value = a.category || '';
  _q('.wiki-editor-tags').value = tags.join(', ');
  _q('.wiki-editor-body').value = a.body || '';
  const statusSel = _q('.wiki-editor-status');
  if (statusSel) statusSel.value = (a.status === 'published') ? 'published' : 'draft';
}

async function _save() {
  const title = _q('.wiki-editor-title').value.trim();
  if (!title) { _toast('Give the article a title first.', true); return; }
  const body = _q('.wiki-editor-body').value;
  const category = _q('.wiki-editor-category').value.trim();
  const tags = _q('.wiki-editor-tags').value
    .split(',').map(s => s.trim()).filter(Boolean);
  const statusSel = _q('.wiki-editor-status');
  const status = statusSel && statusSel.value === 'published' ? 'published' : 'draft';
  const userId = app.currentUserId || '';

  const saveBtn = _q('[data-act="save"]');
  if (saveBtn) saveBtn.disabled = true;
  try {
    let data;
    if (_current && _current.slug) {
      data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, status, user_id: userId }),
      });
    } else {
      data = await _api('/api/v1/wiki', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, status, user_id: userId }),
      });
    }
    _current = data.article;
    _editing = false;
    _renderReader();
    _loadBacklinks(_current.slug);
    _toast('Saved.');
    await _refresh();          // article set changed → rebuild tree + link index
    _markActiveInTree();
  } catch (e) {
    _toast(`Couldn't save: ${e.message}`, true);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function _delete() {
  if (!_current || !_current.slug) { _showWelcome(); return; }
  if (!window.confirm(`Delete "${_current.title}"? This removes it for everyone and can't be undone.`)) return;
  try {
    await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, { method: 'DELETE' });
    _toast('Deleted.');
    _showWelcome();
    await _refresh();
  } catch (e) {
    _toast(`Couldn't delete: ${e.message}`, true);
  }
}

// Toggle which toolbar buttons + panels show per mode: reading | editing | revision.
// Editing controls are members-only (anonymous visitors get a read-only view).
function _setToolbarMode(mode) {
  const show = (sel, on) => { const el = _q(sel); if (el) el.hidden = !on; };
  const member = _isMember();
  const reading = mode === 'reading';
  const editing = mode === 'editing';
  const revision = mode === 'revision';
  show('[data-act="history"]', reading && member);
  show('[data-act="edit"]', reading && member);
  show('[data-act="delete"]', reading && member);
  show('[data-act="save"]', editing && member);
  show('[data-act="cancel"]', editing);

  // Publish / unpublish button: only while reading the live article, member only.
  const pub = _q('.wiki-publish-btn');
  if (pub) {
    const showPub = reading && member && _current && _current.slug;
    pub.hidden = !showPub;
    if (showPub) {
      const isDraft = (_current.status || 'draft') !== 'published';
      const label = pub.querySelector('.wiki-publish-label');
      if (label) label.textContent = isDraft ? 'Publish' : 'Unpublish';
      pub.title = isDraft ? 'Make this article public' : 'Make this article internal (draft)';
      pub.classList.toggle('wiki-btn-primary', isDraft);
    }
  }
}

// Publish (draft → published) or unpublish (published → draft) the open article.
async function _toggleStatus() {
  if (!_current || !_current.slug) return;
  const isDraft = (_current.status || 'draft') !== 'published';
  const action = isDraft ? 'publish' : 'unpublish';
  if (isDraft && !window.confirm(`Publish "${_current.title}"? It will become visible to everyone, including anonymous visitors.`)) return;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId || '' }),
    });
    _current = data.article;
    _renderReader();
    _toast(isDraft ? 'Published — now public.' : 'Unpublished — now internal.');
    await _refresh();
    _markActiveInTree();
  } catch (e) {
    _toast(`Couldn't change visibility: ${e.message}`, true);
  }
}
