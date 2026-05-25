'use strict';

// VS Code-style file base editor.
//
// State: open tabs live in `openTabs` (ordered, draggable) and the active
// tab is `activeTabPath`. The directory tree is rendered lazily — each
// folder fetches its children on first expand.

const API_BASE = '/api/v1/files';

let initialised = false;
let isAdmin = false;
let openTabs = [];          // { path, name, content, dirty, binary, encoding, size }
let activeTabPath = null;
let expandedDirs = new Set();  // absolute paths of currently expanded directories
let dragSrcPath = null;        // path of the tab being dragged
let currentRoot = '';          // absolute path of the directory the tree is rooted at
let projectRoot = '';          // absolute path of the project root (server-reported)

// Persisted state (across tab switches and reloads)
const LS_SIDEBAR_WIDTH    = 'files.sidebarWidth';
const LS_SIDEBAR_COLLAPSED = 'files.sidebarCollapsed';
const LS_OPEN_TABS         = 'files.openTabs';
const LS_ACTIVE_TAB        = 'files.activeTab';
const LS_EXPANDED          = 'files.expandedDirs';
const LS_CURRENT_ROOT      = 'files.currentRoot';

// ── Auth helper ────────────────────────────────────────────────────

function authHeaders() {
  const t = localStorage.getItem('auth_token');
  return t ? { Authorization: 'Bearer ' + t } : {};
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign(
    { 'Content-Type': 'application/json' },
    authHeaders(),
    opts.headers || {},
  );
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  return res.json();
}

// ── Tree ──────────────────────────────────────────────────────────

function fileIconName(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['js', 'mjs', 'ts', 'tsx', 'jsx'].includes(ext)) return 'file-code';
  if (['py', 'rb', 'go', 'rs', 'java', 'c', 'cpp', 'h'].includes(ext)) return 'file-code';
  if (['html', 'css', 'scss', 'json', 'yaml', 'yml', 'xml'].includes(ext)) return 'file-code';
  if (['md', 'txt', 'rst'].includes(ext)) return 'file-text';
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) return 'image';
  if (['mp3', 'wav', 'ogg', 'm4a'].includes(ext)) return 'music';
  if (['mp4', 'webm', 'mov', 'avi'].includes(ext)) return 'video';
  if (['zip', 'tar', 'gz', '7z', 'rar'].includes(ext)) return 'archive';
  return 'file';
}

function renderTreeRow(entry, depth) {
  const row = document.createElement('div');
  row.className = 'files-tree-row';
  row.style.paddingLeft = (depth * 12 + 4) + 'px';
  row.dataset.path = entry.path;

  const chev = document.createElement('span');
  chev.className = 'files-tree-chev';
  if (entry.is_dir) {
    const i = document.createElement('i');
    i.setAttribute('data-lucide', 'chevron-right');
    i.className = 'lucide-icon';
    chev.appendChild(i);
  } else {
    chev.classList.add('invisible');
  }
  row.appendChild(chev);

  const icon = document.createElement('span');
  icon.className = 'files-tree-icon ' + (entry.is_dir ? 'dir' : 'file');
  const ic = document.createElement('i');
  ic.setAttribute('data-lucide', entry.is_dir ? 'folder' : fileIconName(entry.name));
  ic.className = 'lucide-icon';
  icon.appendChild(ic);
  row.appendChild(icon);

  const label = document.createElement('span');
  label.className = 'files-tree-label';
  label.textContent = entry.name;
  row.appendChild(label);
  return row;
}

function renderTreeNode(entry, depth) {
  const node = document.createElement('div');
  node.className = 'files-tree-node';
  node.dataset.path = entry.path;
  node.dataset.kind = entry.is_dir ? 'dir' : 'file';

  const row = renderTreeRow(entry, depth);
  node.appendChild(row);

  if (entry.is_dir) {
    const children = document.createElement('div');
    children.className = 'files-tree-children';
    node.appendChild(children);
    if (expandedDirs.has(entry.path)) {
      node.classList.add('expanded');
      // load children async
      loadDirInto(entry.path, children, depth + 1);
    }
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleDir(node, entry.path, depth);
    });
  } else {
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      selectTreeRow(row);
      openFile(entry.path, entry.name);
    });
  }
  return node;
}

function selectTreeRow(row) {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-row.selected').forEach((r) => r.classList.remove('selected'));
  if (row) row.classList.add('selected');
}

async function loadDirInto(path, container, depth) {
  container.innerHTML = '<div class="files-tree-loading">Loading…</div>';
  try {
    const data = await apiFetch('/tree?path=' + encodeURIComponent(path));
    container.innerHTML = '';
    if (!data.entries.length) {
      const empty = document.createElement('div');
      empty.className = 'files-tree-loading';
      empty.textContent = '(empty)';
      container.appendChild(empty);
    } else {
      for (const entry of data.entries) {
        container.appendChild(renderTreeNode(entry, depth));
      }
    }
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(container.querySelectorAll('[data-lucide]:not(.lucide)')) });
  } catch (e) {
    container.innerHTML = '<div class="files-tree-loading">Error: ' + (e.message || 'failed') + '</div>';
  }
}

async function toggleDir(node, path, depth) {
  const children = node.querySelector(':scope > .files-tree-children');
  if (!children) return;
  if (node.classList.contains('expanded')) {
    node.classList.remove('expanded');
    expandedDirs.delete(path);
  } else {
    node.classList.add('expanded');
    expandedDirs.add(path);
    if (!children.dataset.loaded) {
      await loadDirInto(path, children, depth + 1);
      children.dataset.loaded = 'true';
    }
  }
  persistExpanded();
}

async function loadRoot() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.innerHTML = '<div class="files-tree-loading">Loading…</div>';
  try {
    const data = await apiFetch('/tree?path=' + encodeURIComponent(currentRoot || ''));
    currentRoot = data.path || currentRoot;
    if (data.project_root) projectRoot = data.project_root;
    renderBreadcrumb(currentRoot, data.parent);

    tree.innerHTML = '';
    if (!data.entries.length) {
      tree.innerHTML = '<div class="files-tree-empty">Empty directory</div>';
    } else {
      for (const entry of data.entries) {
        tree.appendChild(renderTreeNode(entry, 0));
      }
    }
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(tree.querySelectorAll('[data-lucide]:not(.lucide)')) });
    try { localStorage.setItem(LS_CURRENT_ROOT, currentRoot); } catch (_) {}
  } catch (e) {
    tree.innerHTML = '<div class="files-tree-empty">Error: ' + (e.message || 'failed') + '</div>';
    // If the saved root no longer exists, fall back to the project root
    // so the user isn't stranded with a dead breadcrumb.
    if (currentRoot && currentRoot !== projectRoot) {
      currentRoot = '';
      try { localStorage.removeItem(LS_CURRENT_ROOT); } catch (_) {}
    }
  }
}

// ── Breadcrumb ────────────────────────────────────────────────────

function splitPathSegments(absPath) {
  if (!absPath) return [];
  // Backend returns forward-slash-normalised paths, even on Windows.
  // Detect a Windows drive (`C:`) and keep it as a single segment.
  const parts = absPath.split('/').filter(Boolean);
  if (parts.length && /^[A-Za-z]:$/.test(parts[0])) {
    return parts; // first segment is the drive letter
  }
  return parts;
}

function buildSegmentPath(segments, idx, rootIsWindows) {
  // Return the absolute path that should be navigated to when the user
  // clicks segment `idx`. For Unix, prepend "/"; for Windows, the drive
  // letter (segments[0]) already encodes the root.
  const slice = segments.slice(0, idx + 1);
  if (rootIsWindows) return slice.join('/');
  return '/' + slice.join('/');
}

function isAncestorOrEqual(a, b) {
  // Returns true if path `a` is the same as or an ancestor of `b`.
  if (!a || !b) return false;
  if (a === b) return true;
  const aSlash = a.endsWith('/') ? a : a + '/';
  return b.startsWith(aSlash);
}

function renderBreadcrumb(absPath, parentPath) {
  const segWrap = document.getElementById('files-breadcrumb-segments');
  if (!segWrap) return;
  segWrap.innerHTML = '';

  const currentSegs = splitPathSegments(absPath);
  const projectSegs = projectRoot ? splitPathSegments(projectRoot) : [];

  // The breadcrumb always extends at least down to the project root,
  // so the user can see the route home when they navigate above it.
  // If currentRoot is an ancestor of (or equal to) projectRoot, we show
  // projectRoot's full path and grey the tail that's "below" the current
  // location. Otherwise we just show currentRoot's path.
  let displaySegs = currentSegs;
  if (projectSegs.length > currentSegs.length && isAncestorOrEqual(absPath, projectRoot)) {
    displaySegs = projectSegs;
  }

  const rootIsWindows = displaySegs.length > 0 && /^[A-Za-z]:$/.test(displaySegs[0]);
  const currentDepth = currentSegs.length;

  // Leading "/" pill on Unix
  if (!rootIsWindows) {
    const rootBtn = document.createElement('button');
    rootBtn.className = 'files-breadcrumb-seg' + (absPath === '/' ? ' current' : '');
    rootBtn.textContent = '/';
    rootBtn.title = '/';
    rootBtn.addEventListener('click', () => setRoot('/'));
    segWrap.appendChild(rootBtn);
  }

  displaySegs.forEach((seg, idx) => {
    if (idx > 0 || rootIsWindows) {
      const sep = document.createElement('span');
      sep.className = 'files-breadcrumb-sep' + (idx >= currentDepth ? ' below-current' : '');
      sep.textContent = '/';
      segWrap.appendChild(sep);
    }
    const btn = document.createElement('button');
    btn.className = 'files-breadcrumb-seg';
    if (idx === currentDepth - 1) {
      btn.classList.add('current');
    } else if (idx >= currentDepth) {
      btn.classList.add('below-current');
    }
    btn.textContent = seg;
    btn.title = seg;
    const targetPath = buildSegmentPath(displaySegs, idx, rootIsWindows);
    btn.addEventListener('click', () => setRoot(targetPath));
    segWrap.appendChild(btn);
  });

  const upBtn = document.getElementById('files-breadcrumb-up');
  if (upBtn) {
    upBtn.disabled = !parentPath;
    upBtn.onclick = () => { if (parentPath) setRoot(parentPath); };
  }
  const homeBtn = document.getElementById('files-breadcrumb-home');
  if (homeBtn) {
    homeBtn.disabled = !projectRoot || projectRoot === absPath;
    homeBtn.onclick = () => { if (projectRoot) setRoot(projectRoot); };
  }
}

async function setRoot(absPath) {
  if (!absPath || absPath === currentRoot) return;
  currentRoot = absPath;
  // Keep expandedDirs intact — entries are keyed by absolute path, so
  // a stale path simply doesn't match anything in the new tree, while
  // navigating up then back down preserves the user's expanded folders.
  await loadRoot();
}

// ── Tabs ──────────────────────────────────────────────────────────

function renderTabs() {
  const bar = document.getElementById('files-tabs');
  if (!bar) return;
  bar.innerHTML = '';
  for (const tab of openTabs) {
    const el = document.createElement('div');
    el.className = 'files-tab' + (tab.path === activeTabPath ? ' active' : '') + (tab.dirty ? ' dirty' : '');
    el.dataset.path = tab.path;
    el.draggable = true;
    el.title = tab.path;

    const iconWrap = document.createElement('span');
    iconWrap.className = 'files-tab-icon';
    const iconI = document.createElement('i');
    iconI.setAttribute('data-lucide', fileIconName(tab.name));
    iconI.className = 'lucide-icon';
    iconWrap.appendChild(iconI);
    el.appendChild(iconWrap);

    const label = document.createElement('span');
    label.className = 'files-tab-label';
    label.textContent = tab.name;
    el.appendChild(label);

    const close = document.createElement('button');
    close.className = 'files-tab-close';
    close.type = 'button';
    close.title = 'Close (middle-click also works)';
    close.draggable = false;
    const xI = document.createElement('i');
    xI.setAttribute('data-lucide', 'x');
    xI.className = 'lucide-icon';
    xI.style.pointerEvents = 'none';
    close.appendChild(xI);
    // The parent `el` is draggable, which can swallow the click into a
    // potential drag operation on some browsers. Closing on mousedown is
    // the most reliable path; stop propagation so the tab's mousedown
    // (middle-click handler) and dragstart don't also fire.
    close.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0 || e.button === 1) {
        e.preventDefault();
        closeTab(tab.path);
      }
    });
    close.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
    });
    close.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(close);

    el.addEventListener('click', () => activateTab(tab.path));
    el.addEventListener('mousedown', (e) => {
      if (e.button === 1) {
        e.preventDefault();
        closeTab(tab.path);
      }
    });

    // ── Drag-and-drop reordering ──
    el.addEventListener('dragstart', (e) => {
      dragSrcPath = tab.path;
      el.classList.add('dragging');
      try { e.dataTransfer.setData('text/plain', tab.path); } catch (_) {}
      e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', () => {
      dragSrcPath = null;
      el.classList.remove('dragging');
      document.querySelectorAll('.files-tab.drop-before, .files-tab.drop-after')
        .forEach((t) => t.classList.remove('drop-before', 'drop-after'));
    });
    el.addEventListener('dragover', (e) => {
      if (!dragSrcPath || dragSrcPath === tab.path) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      el.classList.toggle('drop-before', before);
      el.classList.toggle('drop-after', !before);
    });
    el.addEventListener('dragleave', () => {
      el.classList.remove('drop-before', 'drop-after');
    });
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragSrcPath || dragSrcPath === tab.path) return;
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      reorderTab(dragSrcPath, tab.path, before);
    });

    bar.appendChild(el);
  }
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(bar.querySelectorAll('[data-lucide]:not(.lucide)')) });
}

function reorderTab(srcPath, destPath, before) {
  const srcIdx = openTabs.findIndex((t) => t.path === srcPath);
  const destIdx = openTabs.findIndex((t) => t.path === destPath);
  if (srcIdx < 0 || destIdx < 0) return;
  const [src] = openTabs.splice(srcIdx, 1);
  let insertAt = openTabs.findIndex((t) => t.path === destPath);
  if (!before) insertAt += 1;
  openTabs.splice(insertAt, 0, src);
  renderTabs();
  persistTabs();
}

function renderEditorPanes() {
  const content = document.getElementById('files-content');
  if (!content) return;

  if (!openTabs.length) {
    content.innerHTML = `
      <div class="files-welcome">
        <i data-lucide="folder-tree" class="lucide-icon files-welcome-icon"></i>
        <div class="files-welcome-title">File Editor</div>
        <div class="files-welcome-text">Pick a file from the Explorer on the left to open it here.</div>
      </div>`;
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(content.querySelectorAll('[data-lucide]:not(.lucide)')) });
    updateStatusBar(null);
    return;
  }

  // Re-use existing panes; create missing ones; remove closed ones
  const existing = new Map();
  content.querySelectorAll('.files-editor-pane').forEach((p) => existing.set(p.dataset.path, p));
  // Remove any pane whose tab is gone
  for (const [path, pane] of existing) {
    if (!openTabs.find((t) => t.path === path)) {
      pane.remove();
      existing.delete(path);
    }
  }
  // Remove the welcome panel if present
  const welcome = content.querySelector('.files-welcome');
  if (welcome) welcome.remove();

  for (const tab of openTabs) {
    let pane = existing.get(tab.path);
    if (pane && !tab.binary) {
      // Keep the textarea in sync if tab.content changed since the pane was created
      const ta = pane.querySelector('textarea.files-textarea');
      if (ta && ta.value !== tab.content) ta.value = tab.content || '';
    }
    if (!pane) {
      pane = document.createElement('div');
      pane.className = 'files-editor-pane';
      pane.dataset.path = tab.path;

      if (tab.binary) {
        pane.innerHTML = `
          <div class="files-binary-msg">
            <i data-lucide="file-warning" class="lucide-icon"></i>
            <div>Binary file (${formatBytes(tab.size)})</div>
            <div style="margin-top:6px;font-size:11px;color:#888;">Editing binary files is not supported here.</div>
          </div>`;
        if (window.lucide) window.lucide.createIcons({ nodes: Array.from(pane.querySelectorAll('[data-lucide]:not(.lucide)')) });
      } else {
        const ta = document.createElement('textarea');
        ta.className = 'files-textarea';
        ta.spellcheck = false;
        ta.autocomplete = 'off';
        ta.autocapitalize = 'off';
        ta.value = tab.content || '';
        ta.addEventListener('input', () => {
          tab.content = ta.value;
          if (!tab.dirty) {
            tab.dirty = true;
            renderTabs();
          }
          updateStatusBar(tab);
        });
        ta.addEventListener('keydown', (e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            e.preventDefault();
            saveTab(tab.path);
          }
          // Tab key inserts two spaces instead of changing focus
          if (e.key === 'Tab' && !e.shiftKey) {
            e.preventDefault();
            const start = ta.selectionStart;
            const end = ta.selectionEnd;
            ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end);
            ta.selectionStart = ta.selectionEnd = start + 2;
            tab.content = ta.value;
            if (!tab.dirty) { tab.dirty = true; renderTabs(); }
            updateStatusBar(tab);
          }
        });
        pane.appendChild(ta);
      }
      content.appendChild(pane);
    }
    pane.classList.toggle('active', tab.path === activeTabPath);
  }
  const active = openTabs.find((t) => t.path === activeTabPath);
  updateStatusBar(active || null);
}

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

function updateStatusBar(tab) {
  const path = document.getElementById('files-status-path');
  const info = document.getElementById('files-status-info');
  if (!path || !info) return;
  if (!tab) {
    path.textContent = '';
    info.textContent = '';
    info.classList.remove('dirty');
    return;
  }
  path.textContent = tab.path;
  info.classList.toggle('dirty', !!tab.dirty);
  info.innerHTML = '';
  const meta = document.createElement('span');
  meta.textContent = tab.dirty ? 'Unsaved changes' : (tab.binary ? 'binary' : 'Saved');
  info.appendChild(meta);
  if (!tab.binary) {
    const btn = document.createElement('button');
    btn.className = 'files-save-btn';
    btn.textContent = 'Save';
    btn.disabled = !tab.dirty;
    btn.addEventListener('click', () => saveTab(tab.path));
    info.appendChild(btn);
  }
}

// ── Tab actions ───────────────────────────────────────────────────

async function openFile(path, name) {
  const existing = openTabs.find((t) => t.path === path);
  if (existing) {
    activateTab(path);
    return;
  }

  let data;
  try {
    data = await apiFetch('/read?path=' + encodeURIComponent(path));
  } catch (e) {
    alert('Failed to open file: ' + e.message);
    return;
  }

  openTabs.push({
    path: data.path || path,
    name: name || path.split('/').pop(),
    content: data.content,
    dirty: false,
    binary: data.binary,
    encoding: data.encoding,
    size: data.size,
  });
  activeTabPath = path;
  renderTabs();
  renderEditorPanes();
  persistTabs();
}

function activateTab(path) {
  if (!openTabs.find((t) => t.path === path)) return;
  activeTabPath = path;
  renderTabs();
  // Just flip active class instead of re-rendering everything
  const content = document.getElementById('files-content');
  if (content) {
    content.querySelectorAll('.files-editor-pane').forEach((p) => {
      p.classList.toggle('active', p.dataset.path === path);
    });
  }
  const tab = openTabs.find((t) => t.path === path);
  updateStatusBar(tab || null);
  try { localStorage.setItem(LS_ACTIVE_TAB, path); } catch (_) {}
}

function closeTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.dirty) {
    if (!confirm('Discard unsaved changes to ' + tab.name + '?')) return;
  }
  const idx = openTabs.findIndex((t) => t.path === path);
  openTabs.splice(idx, 1);
  // Remove pane
  const content = document.getElementById('files-content');
  if (content) {
    const pane = content.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
  }
  if (activeTabPath === path) {
    activeTabPath = openTabs.length ? openTabs[Math.min(idx, openTabs.length - 1)].path : null;
  }
  renderTabs();
  renderEditorPanes();
  persistTabs();
}

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return s.replace(/(["\\])/g, '\\$1');
}

async function saveTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab || tab.binary) return;
  try {
    await apiFetch('/write', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path, content: tab.content, encoding: 'utf-8' }),
    });
    tab.dirty = false;
    renderTabs();
    updateStatusBar(tab);
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

// ── New file / folder ─────────────────────────────────────────────

async function promptNew(kind) {
  const name = prompt('New ' + kind + ' path (relative to project root):');
  if (!name) return;
  try {
    await apiFetch('/create', {
      method: 'POST',
      body: JSON.stringify({ path: name, kind }),
    });
    // Refresh root listing; if the parent of `name` is currently expanded,
    // refresh that subtree instead so the new entry shows up in context.
    await loadRoot();
    if (kind === 'file') {
      openFile(name, name.split('/').pop());
    }
  } catch (e) {
    alert('Create failed: ' + e.message);
  }
}

// ── Sidebar resize / toggle ───────────────────────────────────────

function initSidebarResize() {
  const sidebar = document.getElementById('files-sidebar');
  const handle = document.getElementById('files-resize-handle');
  if (!sidebar || !handle) return;

  // Restore width
  const savedWidth = parseInt(localStorage.getItem(LS_SIDEBAR_WIDTH), 10);
  if (!isNaN(savedWidth) && savedWidth >= 160) sidebar.style.width = savedWidth + 'px';

  if (localStorage.getItem(LS_SIDEBAR_COLLAPSED) === 'true') {
    sidebar.classList.add('collapsed');
  }

  let dragging = false;
  handle.addEventListener('mousedown', (e) => {
    dragging = true;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const editorRect = document.getElementById('files-editor').getBoundingClientRect();
    let w = e.clientX - editorRect.left;
    w = Math.max(160, Math.min(600, w));
    sidebar.style.width = w + 'px';
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    try { localStorage.setItem(LS_SIDEBAR_WIDTH, parseInt(sidebar.style.width, 10) || ''); } catch (_) {}
  });

  const toggle = document.getElementById('files-sidebar-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const collapsed = sidebar.classList.toggle('collapsed');
      try { localStorage.setItem(LS_SIDEBAR_COLLAPSED, String(collapsed)); } catch (_) {}
    });
  }
}

// ── Persistence ───────────────────────────────────────────────────

function persistTabs() {
  try {
    const minimal = openTabs.map((t) => ({ path: t.path, name: t.name }));
    localStorage.setItem(LS_OPEN_TABS, JSON.stringify(minimal));
    localStorage.setItem(LS_ACTIVE_TAB, activeTabPath || '');
  } catch (_) {}
}

function persistExpanded() {
  try {
    localStorage.setItem(LS_EXPANDED, JSON.stringify(Array.from(expandedDirs)));
  } catch (_) {}
}

function restoreState() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_EXPANDED) || '[]');
    if (Array.isArray(saved)) expandedDirs = new Set(saved);
  } catch (_) {}
  try {
    const r = localStorage.getItem(LS_CURRENT_ROOT);
    if (r) currentRoot = r;
  } catch (_) {}
}

async function restoreOpenTabs() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_OPEN_TABS) || '[]');
    const wantActive = localStorage.getItem(LS_ACTIVE_TAB) || '';
    if (!Array.isArray(saved) || !saved.length) return;
    // Open in order, swallow failures (file may have been deleted)
    for (const t of saved) {
      try {
        await openFile(t.path, t.name);
      } catch (_) {}
    }
    if (wantActive && openTabs.find((t) => t.path === wantActive)) {
      activateTab(wantActive);
    }
  } catch (_) {}
}

// ── Public API ────────────────────────────────────────────────────

export function initFiles() {
  if (initialised) return;
  initialised = true;

  restoreState();

  const refreshBtn = document.getElementById('files-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', loadRoot);

  const newFileBtn = document.getElementById('files-new-file');
  if (newFileBtn) newFileBtn.addEventListener('click', () => promptNew('file'));

  const newFolderBtn = document.getElementById('files-new-folder');
  if (newFolderBtn) newFolderBtn.addEventListener('click', () => promptNew('dir'));

  const collapseBtn = document.getElementById('files-collapse-all');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', () => {
      expandedDirs.clear();
      persistExpanded();
      loadRoot();
    });
  }

  initSidebarResize();
  renderTabs();
  renderEditorPanes();
}

export async function startFiles() {
  initFiles();
  // Check admin access; show overlay if not
  try {
    const r = await apiFetch('/check-access');
    isAdmin = !!r.is_admin;
  } catch (_) {
    isAdmin = false;
  }
  const overlay = document.getElementById('files-restricted-overlay');
  const editor = document.getElementById('files-editor');
  if (!isAdmin) {
    if (overlay) overlay.style.display = 'flex';
    if (editor) editor.style.display = 'none';
    return;
  }
  if (overlay) overlay.style.display = 'none';
  if (editor) editor.style.display = 'flex';

  // Load tree (always refresh on tab activation so the user sees current state)
  await loadRoot();

  // Restore previously open tabs only once per session
  if (!openTabs.length) await restoreOpenTabs();
}

export function stopFiles() {
  // No teardown needed — tabs and state are kept so reopening the page is instant.
}
