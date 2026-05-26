'use strict';

// VS Code-style file base editor.
//
// State: open tabs live in `openTabs` (ordered, draggable). File and
// terminal tabs share the array but track separate "active" pointers
// (`activeFilePath` / `activeTerminalId`) — they render into separate
// main panels (explorer vs terminal). The directory tree is rendered
// lazily — each folder fetches its children on first expand.

import { openGitPanel, renderGitMain } from './files-git.js';
import { createTerminalInstance } from './terminal.js';
import { randomUUID } from './uuid.js';
import { startAppConfig, stopAppConfig } from './app-config.js';
import { startAutoRefresh, stopAutoRefresh } from './db/pagination.js';
import { startLoop, stopLoop } from './loop.js';
import { startLoopVisual, stopLoopVisual } from './loop-logic.js';

const API_BASE = '/api/v1/files';
const LS_SIDEBAR_VIEW = 'files.sidebarView';   // 'explorer' | 'git' | 'database'
const LS_TERM_FONT_SIZE = 'files.terminalFontSize';
const TERM_FONT_DEFAULT = 14;

function getTerminalFontSize() {
  const raw = parseInt(localStorage.getItem(LS_TERM_FONT_SIZE), 10);
  return Number.isFinite(raw) && raw >= 8 && raw <= 32 ? raw : TERM_FONT_DEFAULT;
}
function setTerminalFontSize(n) {
  try { localStorage.setItem(LS_TERM_FONT_SIZE, String(n)); } catch (_) {}
  // Propagate to every open terminal — font size is global, not per-tab.
  for (const t of openTabs) {
    if (t.kind === 'terminal' && t.instance && t.instance.setFontSize) {
      try { t.instance.setFontSize(n); } catch (_) {}
    }
  }
}

// Look up the active terminal tab (used by the terminal main and the
// global keyboard shortcuts).
function getActiveTerminalTab() {
  if (!activeTerminalId) return null;
  return openTabs.find((t) => t.kind === 'terminal' && t.path === activeTerminalId) || null;
}

let initialised = false;
let isAdmin = false;
// File and terminal tabs share one array so the existing actions (close,
// rename, drag, persistence) still operate on a single store. Rendering
// routes file tabs into #files-tabs / #files-content and terminal tabs
// into #files-term-tabs / #files-term-content based on `kind`.
let openTabs = [];          // { path, name, content, dirty, binary, encoding, size, kind? }
let activeFilePath = null;       // path of the active FILE tab (in explorer main)
let activeTerminalId = null;     // session_id of the active TERMINAL tab (in terminal main)
let expandedDirs = new Set();  // absolute paths of currently expanded directories
let dragSrcPath = null;        // path of the tab being dragged
let currentRoot = '';          // absolute path of the directory the tree is rooted at
let projectRoot = '';          // absolute path of the project root (server-reported)

// Persisted state (across tab switches and reloads)
const LS_SIDEBAR_WIDTH    = 'files.sidebarWidth';
const LS_SIDEBAR_COLLAPSED = 'files.sidebarCollapsed';
const LS_OPEN_TABS         = 'files.openTabs';
const LS_ACTIVE_TAB        = 'files.activeTab';        // legacy unified key (still read for migration)
const LS_ACTIVE_FILE       = 'files.activeFile';
const LS_ACTIVE_TERMINAL   = 'files.activeTerminal';
const LS_EXPANDED          = 'files.expandedDirs';
const LS_CURRENT_ROOT      = 'files.currentRoot';

// ── Active-tab helpers ────────────────────────────────────────────
// File and terminal tabs each track their own "active" pointer. The two
// helpers below let callers stay agnostic about which kind a tab is.

function activePathForKind(kind) {
  return kind === 'terminal' ? activeTerminalId : activeFilePath;
}

function setActivePathForTab(tab) {
  if (!tab) return;
  if (tab.kind === 'terminal') activeTerminalId = tab.path;
  else activeFilePath = tab.path;
}

// ── Auth helper ────────────────────────────────────────────────────

function authHeaders() {
  const t = localStorage.getItem('auth_token');
  return t ? { Authorization: 'Bearer ' + t } : {};
}

function withUserIdParam(path) {
  // Append the active user_id as a query param. The backend prefers the
  // JWT when valid, but falls back to this — same pattern as the other
  // admin pages — so the page still works if the cached token is stale.
  const uid = localStorage.getItem('auth_user_id') || '';
  if (!uid) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'user_id=' + encodeURIComponent(uid);
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  // Avoid a CORS preflight on GETs by only setting Content-Type when
  // we're actually sending a body.
  if (opts.body && !('Content-Type' in headers)) {
    headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(API_BASE + withUserIdParam(path), Object.assign({}, opts, { headers }));
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
  // Make every row draggable so the user can drop its path onto a terminal
  // pane (handled in buildPaneForTab). text/plain is the canonical type
  // both DataTransfer.types and our drop handlers look for.
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    try {
      e.dataTransfer.setData('text/plain', entry.path);
      e.dataTransfer.effectAllowed = 'copy';
    } catch (_) {}
  });

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

  // Right-click → context menu with the advanced actions
  row.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    e.stopPropagation();
    selectTreeRow(row);
    showTreeContextMenu(entry, e.clientX, e.clientY);
  });

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
    // ── Drag-and-drop file upload from the OS ──
    // The user drags one or more files from their OS file manager onto
    // a folder row in the tree; we upload each via /api/v1/files/write.
    wireFolderDropTarget(row, entry.path);
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

// ── Drag-and-drop file upload ──────────────────────────────────────

function wireFolderDropTarget(row, folderPath) {
  // Counter for dragenter/leave pairs — these events fire on every child
  // boundary crossing too, so plain add/remove is unreliable.
  let depth = 0;

  function hasFiles(e) {
    if (!e.dataTransfer) return false;
    const types = e.dataTransfer.types;
    if (!types) return false;
    // DOMStringList in Firefox, array-like elsewhere
    for (let i = 0; i < types.length; i++) {
      if (types[i] === 'Files') return true;
    }
    return false;
  }

  row.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    row.classList.add('files-drop-target');
  });
  // dragover must preventDefault on *every* fire or the drop event never
  // happens. We don't gate on hasFiles here because some browsers only
  // expose `types` reliably on dragenter/drop.
  row.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  row.addEventListener('dragleave', () => {
    if (depth > 0) depth--;
    if (depth === 0) row.classList.remove('files-drop-target');
  });
  row.addEventListener('drop', async (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    row.classList.remove('files-drop-target');
    await uploadFilesToFolder(folderPath, Array.from(e.dataTransfer.files));
  });
}

// Catch file drops anywhere in the Files page that *aren't* on a folder
// row so the browser doesn't navigate to / download the dropped file
// (which is the default behaviour when no handler preventDefaults).
let _filesDropGuardInstalled = false;
function installFilesDropGuard() {
  if (_filesDropGuardInstalled) return;
  _filesDropGuardInstalled = true;
  const editor = document.getElementById('admin-tools');
  if (!editor) return;
  editor.addEventListener('dragover', (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types).indexOf('Files') !== -1) {
      e.preventDefault();
    }
  });
  editor.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length &&
        !(e.target.closest && e.target.closest('.files-tree-row[data-kind="dir"], .files-tree-node[data-kind="dir"] > .files-tree-row'))) {
      // Dropped on the editor but not on a folder row — swallow so the
      // browser doesn't navigate away from the page.
      e.preventDefault();
    }
  });
}

async function uploadFilesToFolder(folderPath, fileList) {
  // Make sure the destination folder is expanded so the user can see
  // the new entries arrive after the tree refresh below.
  expandedDirs.add(folderPath);
  persistExpanded();

  let okCount = 0;
  let failCount = 0;
  const errors = [];

  for (const file of fileList) {
    const destPath = folderPath.replace(/\/+$/, '') + '/' + file.name;
    try {
      const { content, encoding } = await readFileForUpload(file);
      await apiFetch('/write', {
        method: 'POST',
        body: JSON.stringify({ path: destPath, content, encoding }),
      });
      okCount++;
    } catch (err) {
      failCount++;
      errors.push(file.name + ': ' + (err.message || err));
    }
  }

  await loadRoot();

  if (failCount) {
    alert(
      'Uploaded ' + okCount + ' of ' + fileList.length + ' file' + (fileList.length === 1 ? '' : 's') + '.\n\n' +
      'Failed:\n' + errors.join('\n')
    );
  }
}

function readFileForUpload(file) {
  return new Promise((resolve, reject) => {
    // Decide text-vs-binary up front from MIME type. Text-ish types go
    // up as utf-8, everything else as base64. The backend accepts both.
    const looksTexty =
      !file.type ||
      file.type.startsWith('text/') ||
      /\b(json|xml|javascript|x-sh|x-python|yaml|toml|csv|svg\+xml)\b/.test(file.type);

    const reader = new FileReader();
    reader.onerror = () => reject(new Error('read failed'));
    if (looksTexty) {
      reader.onload = () => resolve({ content: String(reader.result || ''), encoding: 'utf-8' });
      reader.readAsText(file);
    } else {
      reader.onload = () => {
        // readAsDataURL returns "data:<mime>;base64,<payload>"; strip the prefix
        const s = String(reader.result || '');
        const comma = s.indexOf(',');
        resolve({ content: comma >= 0 ? s.slice(comma + 1) : s, encoding: 'base64' });
      };
      reader.readAsDataURL(file);
    }
  });
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
// The clickable path segments were removed in favour of the simpler
// Up + Home buttons; we just render the current path as a read-only
// label so the user can still see where the tree is rooted.

function renderBreadcrumb(absPath, parentPath) {
  const pathEl = document.getElementById('files-current-path');
  if (pathEl) {
    pathEl.textContent = absPath || '';
    pathEl.title = absPath || '';
  }

  const upBtn = document.getElementById('files-breadcrumb-up');
  if (upBtn) {
    upBtn.disabled = !parentPath;
    upBtn.title = parentPath ? 'Go to parent: ' + parentPath : 'No parent directory';
    upBtn.onclick = () => { if (parentPath) setRoot(parentPath); };
  }
  const homeBtn = document.getElementById('files-breadcrumb-home');
  if (homeBtn) {
    const atHome = !projectRoot || projectRoot === absPath;
    homeBtn.disabled = atHome;
    homeBtn.title = atHome ? 'Already at project root' : 'Back to project root: ' + projectRoot;
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
  const fileBar = document.getElementById('files-tabs');
  const termBar = document.getElementById('files-term-tabs');
  if (!fileBar && !termBar) return;
  if (fileBar) fileBar.innerHTML = '';
  if (termBar) termBar.innerHTML = '';
  for (const tab of openTabs) {
    const bar = tab.kind === 'terminal' ? termBar : fileBar;
    if (!bar) continue;
    const isActive = tab.path === activePathForKind(tab.kind);
    const el = document.createElement('div');
    el.className = 'files-tab'
      + (isActive ? ' active' : '')
      + (tab.dirty ? ' dirty' : '')
      + (tab.closing ? ' closing' : '');
    el.dataset.path = tab.path;
    el.draggable = true;
    el.title = tab.path;

    const iconWrap = document.createElement('span');
    iconWrap.className = 'files-tab-icon';
    const iconI = document.createElement('i');
    iconI.setAttribute('data-lucide', tab.kind === 'terminal' ? 'terminal' : fileIconName(tab.name));
    iconI.className = 'lucide-icon';
    iconWrap.appendChild(iconI);
    // Terminal tabs get a small connection-status dot overlaid on the icon
    // wrap. State is driven by the xterm instance via onStateChange (see
    // buildPaneForTab); we render an initial state here so the dot exists
    // before the instance binds.
    if (tab.kind === 'terminal') {
      const dot = document.createElement('span');
      dot.className = 'files-tab-conn-dot';
      const initialState = (tab.instance && tab.instance.getState && tab.instance.getState()) || 'connecting';
      dot.dataset.state = initialState;
      dot.title = _connStateTitle(initialState);
      iconWrap.appendChild(dot);
    }
    el.appendChild(iconWrap);

    const label = document.createElement('span');
    label.className = 'files-tab-label';
    label.textContent = tab.name;
    // Double-click the label to rename a terminal tab inline. File tabs are
    // named after the file on disk and don't get this affordance.
    if (tab.kind === 'terminal') {
      label.title = 'Double-click to rename';
      label.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        e.preventDefault();
        startInlineRename(tab, label);
      });
    }
    el.appendChild(label);

    // ── 3-dot "more" menu button ──
    // For file tabs this opens the rename/delete/wrap/preview/find menu;
    // for terminal tabs it shows the (different) terminal-specific items.
    const more = document.createElement('button');
    more.className = 'files-tab-more';
    more.type = 'button';
    more.title = 'More actions';
    more.draggable = false;
    const moreI = document.createElement('i');
    moreI.setAttribute('data-lucide', 'more-vertical');
    moreI.className = 'lucide-icon';
    more.appendChild(moreI);
    more.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0) {
        e.preventDefault();
        if (tab.kind === 'terminal') showTerminalTabMenu(tab, more);
        else showTabMenu(tab, more);
      }
    });
    more.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });
    more.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(more);

    const close = document.createElement('button');
    close.className = 'files-tab-close';
    close.type = 'button';
    close.title = tab.closing ? 'Closing…' : 'Close (middle-click also works)';
    close.draggable = false;
    close.disabled = !!tab.closing;
    const xI = document.createElement('i');
    // Swap the X for a spinner while the backend DELETE is in flight so the
    // user can see the close is being verified.
    xI.setAttribute('data-lucide', tab.closing ? 'loader-2' : 'x');
    xI.className = 'lucide-icon' + (tab.closing ? ' files-tab-spin' : '');
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
  if (window.lucide) {
    if (fileBar) window.lucide.createIcons({ nodes: Array.from(fileBar.querySelectorAll('[data-lucide]:not(.lucide)')) });
    if (termBar) window.lucide.createIcons({ nodes: Array.from(termBar.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }
  updateTabCarousel();
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

// ── Floating menus (tab "more" menu + tree context menu) ──────────

function closeFloatingMenu() {
  const m = document.getElementById('files-floating-menu');
  if (m) m.remove();
}
// Back-compat alias for older callers
function closeTabMenu() { closeFloatingMenu(); }

function _openFloatingMenu(items, top, left) {
  closeFloatingMenu();

  const menu = document.createElement('div');
  menu.className = 'files-tab-menu';
  menu.id = 'files-floating-menu';

  for (const item of items) {
    if (item.separator) {
      const hr = document.createElement('div');
      hr.className = 'files-tab-menu-sep';
      menu.appendChild(hr);
      continue;
    }
    const btn = document.createElement('button');
    btn.className = 'files-tab-menu-item' + (item.danger ? ' danger' : '') + (item.checked ? ' checked' : '');
    btn.type = 'button';
    btn.disabled = !!item.disabled;
    const i = document.createElement('i');
    i.setAttribute('data-lucide', item.icon || 'circle');
    i.className = 'lucide-icon';
    btn.appendChild(i);
    const lbl = document.createElement('span');
    lbl.textContent = item.label;
    btn.appendChild(lbl);
    const check = document.createElement('span');
    check.className = 'files-tab-menu-check';
    check.textContent = '✓';
    btn.appendChild(check);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeFloatingMenu();
      if (!item.disabled && typeof item.action === 'function') item.action();
    });
    menu.appendChild(btn);
  }

  document.body.appendChild(menu);

  // Clamp position into the viewport
  const menuWidth = 180;
  const rect = menu.getBoundingClientRect();
  const menuHeight = rect.height || 8;
  const clampedTop  = Math.max(8, Math.min(window.innerHeight - menuHeight - 8, top));
  const clampedLeft = Math.max(8, Math.min(window.innerWidth  - menuWidth  - 8, left));
  menu.style.top  = clampedTop + 'px';
  menu.style.left = clampedLeft + 'px';

  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(menu.querySelectorAll('[data-lucide]:not(.lucide)')) });

  const outside = (ev) => {
    if (!menu.contains(ev.target)) {
      closeFloatingMenu();
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('contextmenu', outside, true);
      document.removeEventListener('keydown', onKey, true);
    }
  };
  const onKey = (ev) => {
    if (ev.key === 'Escape') {
      closeFloatingMenu();
      document.removeEventListener('mousedown', outside, true);
      document.removeEventListener('contextmenu', outside, true);
      document.removeEventListener('keydown', onKey, true);
    }
  };
  setTimeout(() => {
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('contextmenu', outside, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

function showTabMenu(tab, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const items = [
    { icon: 'pencil',     label: 'Rename…', action: () => renameTab(tab.path) },
    { icon: 'trash-2',    label: 'Delete…', danger: true, action: () => deleteTab(tab.path) },
    { icon: 'refresh-cw', label: 'Refresh',  action: () => refreshTab(tab.path) },
    { icon: 'wrap-text',  label: 'Wrap',     checked: !!tab.wrap, action: () => toggleWrap(tab.path) },
  ];
  if (isMarkdownFile(tab.name)) {
    items.push({ icon: 'eye', label: 'Markdown preview', checked: !!tab.preview, action: () => togglePreview(tab.path) });
  }
  const findDisabled = !!tab.binary || isImageFile(tab.name) || (isMarkdownFile(tab.name) && tab.preview);
  items.push({ icon: 'search', label: 'Find / Replace…', disabled: findDisabled, action: () => openFindBarForActiveTab(tab.path, false) });
  // Right-align under the button
  _openFloatingMenu(items, rect.bottom + 2, rect.right - 180);
}

function showTerminalTabMenu(tab, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const wrapOn = tab.wrap !== false;
  const items = [
    { icon: 'pencil',    label: 'Rename…',        action: () => startInlineRename(tab, document.querySelector('#files-term-tabs .files-tab[data-path="' + cssEscape(tab.path) + '"] .files-tab-label')) },
    { icon: 'wrap-text', label: 'Wrap lines',     checked: wrapOn, action: () => toggleTerminalWrap(tab.path) },
    { separator: true },
    { icon: 'zoom-in',   label: 'Zoom in',        action: () => terminalZoom(+1) },
    { icon: 'zoom-out',  label: 'Zoom out',       action: () => terminalZoom(-1) },
    { icon: 'refresh-cw', label: 'Reset zoom',    action: () => terminalResetZoom() },
    { separator: true },
    { icon: 'search',    label: 'Find…',          action: () => openTerminalFindFromMenu(tab.path) },
  ];
  _openFloatingMenu(items, rect.bottom + 2, rect.right - 180);
}

function toggleTerminalWrap(tabPath) {
  const tab = openTabs.find((t) => t.path === tabPath);
  if (!tab || tab.kind !== 'terminal') return;
  tab.wrap = !(tab.wrap !== false);   // flip; treat undefined as true
  // Sync the CSS classes that control the scroll wrapper's overflow-x and
  // the host's width. The terminal pane is a sibling of the find bar; the
  // host lives inside .files-terminal-scroll.
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(tabPath) + '"]');
  if (pane) {
    const scrollWrap = pane.querySelector('.files-terminal-scroll');
    const host = pane.querySelector('.files-terminal-host');
    if (scrollWrap) scrollWrap.classList.toggle('files-terminal-scroll-nowrap', !tab.wrap);
    if (host)       host.classList.toggle('files-terminal-host-nowrap', !tab.wrap);
  }
  if (tab.instance && tab.instance.setWrap) tab.instance.setWrap(tab.wrap);
  // Refit after the layout change. Two rAF ticks guarantee the browser has
  // applied the new width to the host BEFORE fitAddon measures — on mobile
  // a 30ms setTimeout sometimes fires before layout has reflowed, so xterm
  // keeps its old cols and wrap appears not to work.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!tab.instance) return;
    tab.instance.fit();
    // Print a one-line confirmation so the user can see the toggle took
    // effect and what cols xterm is now using. Future shell output will
    // wrap (or not) at this column count.
    try {
      const cols = (tab.instance.term && tab.instance.term.cols) || '?';
      const msg = tab.wrap
        ? '\r\n\x1b[2;33m[wrap ON — ' + cols + ' cols, lines wrap to next row]\x1b[0m\r\n'
        : '\r\n\x1b[2;33m[wrap OFF — ' + cols + ' cols, swipe horizontally to see overflow]\x1b[0m\r\n';
      tab.instance.term.write(msg);
    } catch (_) {}
  }));
  persistTabs();
}

function terminalZoom(delta) {
  const next = getTerminalFontSize() + delta;
  setTerminalFontSize(next);
}
function terminalResetZoom() {
  setTerminalFontSize(TERM_FONT_DEFAULT);
}

function openTerminalFindFromMenu(tabPath) {
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(tabPath) + '"]');
  const bar = pane && pane.querySelector('.files-terminal-findbar');
  const tab = openTabs.find((t) => t.path === tabPath);
  if (bar && tab && tab.instance) openTerminalFindBar(bar, tab.instance);
}

function togglePreview(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  tab.preview = !tab.preview;
  // Rebuild this pane on next render
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  if (pane) pane.remove();
  renderEditorPanes();
  persistTabs();
}

function openFindBarForActiveTab(path, withReplace) {
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  if (pane) openFindBar(pane, !!withReplace);
}

function showTreeContextMenu(entry, x, y) {
  const isOpen = !!openTabs.find((t) => t.path === entry.path);

  let items;
  if (entry.is_dir) {
    items = [
      { icon: 'file-plus',   label: 'New File…',   action: () => newInFolder(entry.path, 'file') },
      { icon: 'folder-plus', label: 'New Folder…', action: () => newInFolder(entry.path, 'dir')  },
      { separator: true },
      { icon: 'pencil',      label: 'Rename…',     action: () => renameEntry(entry) },
      { icon: 'trash-2',     label: 'Delete…',     danger: true, action: () => deleteEntry(entry) },
      { separator: true },
      { icon: 'refresh-cw',  label: 'Refresh',     action: () => loadRoot() },
      { icon: 'copy',        label: 'Copy path',   action: () => copyPath(entry.path) },
    ];
  } else {
    items = [
      { icon: 'file',     label: isOpen ? 'Focus tab' : 'Open', action: () => openFile(entry.path, entry.name) },
      { separator: true },
      { icon: 'pencil',   label: 'Rename…', action: () => renameEntry(entry) },
      { icon: 'trash-2',  label: 'Delete…', danger: true, action: () => deleteEntry(entry) },
      { separator: true },
      { icon: 'copy',     label: 'Copy path', action: () => copyPath(entry.path) },
    ];
  }
  _openFloatingMenu(items, y, x);
}

// ── Tree actions (rename / delete / new-in-folder / copy path) ────

async function renameEntry(entry) {
  const newName = prompt('Rename to:', entry.name);
  if (!newName || newName === entry.name) return;
  // Replace the basename in the absolute path
  const parts = entry.path.split('/');
  parts[parts.length - 1] = newName;
  const newPath = parts.join('/');
  try {
    const r = await apiFetch('/rename', {
      method: 'POST',
      body: JSON.stringify({ path: entry.path, new_path: newPath }),
    });
    const finalPath = r.to || newPath;
    // If the renamed entry (or any descendant under a renamed folder) is
    // currently open as a tab, update those tabs in place so saves still
    // hit the right file.
    const prefix = entry.path + '/';
    for (const tab of openTabs) {
      if (tab.path === entry.path) {
        tab.path = finalPath;
        tab.name = finalPath.split('/').pop();
      } else if (entry.is_dir && tab.path.startsWith(prefix)) {
        tab.path = finalPath + tab.path.slice(entry.path.length);
      }
    }
    if (activeFilePath === entry.path) activeFilePath = finalPath;
    renderTabs();
    renderEditorPanes();
    persistTabs();
    await loadRoot();
  } catch (e) {
    alert('Rename failed: ' + e.message);
  }
}

async function deleteEntry(entry) {
  const what = entry.is_dir ? 'folder (and all its contents)' : 'file';
  if (!confirm('Delete ' + entry.name + ' ' + what + '?\n\n' + entry.path + '\n\nThis cannot be undone.')) return;
  try {
    await apiFetch('/delete', {
      method: 'POST',
      body: JSON.stringify({ path: entry.path }),
    });
    // Close any open tabs for this entry (or anything under it if folder)
    const prefix = entry.path + '/';
    const toClose = openTabs
      .filter((t) => t.path === entry.path || (entry.is_dir && t.path.startsWith(prefix)))
      .map((t) => t.path);
    for (const p of toClose) {
      const tab = openTabs.find((t) => t.path === p);
      if (tab) tab.dirty = false;
      closeTab(p);
    }
    await loadRoot();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function newInFolder(folderPath, kind) {
  const name = prompt('New ' + (kind === 'dir' ? 'folder' : 'file') + ' name:');
  if (!name) return;
  const newPath = folderPath.replace(/\/+$/, '') + '/' + name;
  try {
    await apiFetch('/create', {
      method: 'POST',
      body: JSON.stringify({ path: newPath, kind }),
    });
    // Make sure the parent folder is expanded so the new entry is visible
    expandedDirs.add(folderPath);
    persistExpanded();
    await loadRoot();
    if (kind === 'file') openFile(newPath, name);
  } catch (e) {
    alert('Create failed: ' + e.message);
  }
}

async function copyPath(path) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(path);
      return;
    }
  } catch (_) {}
  // Fallback for insecure contexts where the Clipboard API is blocked
  try {
    const ta = document.createElement('textarea');
    ta.value = path;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  } catch (_) {
    prompt('Copy path:', path);
  }
}

async function renameTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  const newPath = prompt('Rename to (absolute or relative to project root):', tab.path);
  if (!newPath || newPath === tab.path) return;
  try {
    const r = await apiFetch('/rename', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path, new_path: newPath }),
    });
    const newAbs = r.to || newPath;
    // Update the tab in place
    tab.path = newAbs;
    tab.name = newAbs.split('/').pop();
    if (activeFilePath === path) activeFilePath = newAbs;
    // The editor pane keys panes by data-path; update it too
    const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.dataset.path = newAbs;
    renderTabs();
    renderEditorPanes();
    persistTabs();
    await loadRoot();
  } catch (e) {
    alert('Rename failed: ' + e.message);
  }
}

async function deleteTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (!confirm('Delete ' + tab.name + ' from disk?\n\n' + tab.path + '\n\nThis cannot be undone.')) return;
  try {
    await apiFetch('/delete', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path }),
    });
    // File is gone — drop the dirty flag so closeTab doesn't prompt
    tab.dirty = false;
    closeTab(path);
    await loadRoot();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function refreshTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.dirty && !confirm('Discard unsaved changes and reload from disk?')) return;
  try {
    const data = await apiFetch('/read?path=' + encodeURIComponent(tab.path));
    tab.content = data.content;
    tab.binary = data.binary;
    tab.encoding = data.encoding;
    tab.size = data.size;
    tab.dirty = false;
    // Replace the pane so the textarea picks up the new content cleanly
    const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
    renderTabs();
    renderEditorPanes();
  } catch (e) {
    alert('Refresh failed: ' + e.message);
  }
}

function toggleWrap(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  tab.wrap = !tab.wrap;
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  const ta = pane && pane.querySelector('textarea.files-textarea');
  if (ta) ta.classList.toggle('wrap', tab.wrap);
  // Highlight overlay also needs to wrap so it lines up with the textarea
  const hl = pane && pane.querySelector('.files-code-highlight');
  if (hl) hl.classList.toggle('wrap', tab.wrap);
  persistTabs();
}

// ── Tab carousel ──────────────────────────────────────────────────

const CAROUSEL_BARS = [
  { bar: 'files-tabs',      prev: 'files-tabs-prev',      next: 'files-tabs-next' },
  { bar: 'files-term-tabs', prev: 'files-term-tabs-prev', next: 'files-term-tabs-next' },
];

function _updateCarouselFor(barId, prevId, nextId) {
  const bar = document.getElementById(barId);
  const prev = document.getElementById(prevId);
  const next = document.getElementById(nextId);
  if (!bar || !prev || !next) return;
  const overflow = bar.scrollWidth > bar.clientWidth + 1;
  if (!overflow) {
    prev.style.display = 'none';
    next.style.display = 'none';
    return;
  }
  prev.style.display = 'inline-flex';
  next.style.display = 'inline-flex';
  prev.disabled = bar.scrollLeft <= 0;
  next.disabled = bar.scrollLeft + bar.clientWidth >= bar.scrollWidth - 1;
}

function updateTabCarousel() {
  for (const c of CAROUSEL_BARS) _updateCarouselFor(c.bar, c.prev, c.next);
}

function initTabCarousel() {
  const SCROLL_STEP = 160;
  for (const c of CAROUSEL_BARS) {
    const bar = document.getElementById(c.bar);
    const prev = document.getElementById(c.prev);
    const next = document.getElementById(c.next);
    if (!bar || !prev || !next) continue;
    prev.addEventListener('click', () => { bar.scrollBy({ left: -SCROLL_STEP, behavior: 'smooth' }); });
    next.addEventListener('click', () => { bar.scrollBy({ left:  SCROLL_STEP, behavior: 'smooth' }); });
    bar.addEventListener('scroll', updateTabCarousel, { passive: true });

    // Auto-scroll while dragging a tab past either edge
    [c.prev, c.next].forEach((id) => {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.addEventListener('dragover', (e) => {
        e.preventDefault();
        const dir = id === c.prev ? -1 : 1;
        bar.scrollBy({ left: dir * 40, behavior: 'auto' });
      });
    });

    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(updateTabCarousel).observe(bar);
    }
  }
  window.addEventListener('resize', updateTabCarousel);
}

function renderEditorPanes() {
  const fileContent = document.getElementById('files-content');
  const termContent = document.getElementById('files-term-content');
  if (!fileContent && !termContent) return;

  const fileTabs = openTabs.filter((t) => t.kind !== 'terminal');
  const termTabs = openTabs.filter((t) => t.kind === 'terminal');

  // Welcome placeholders — show when the container has no tabs. Skip if
  // a welcome is already on screen, so the welcome doesn't flicker on
  // every render.
  function showWelcome(host, html) {
    if (!host) return;
    if (host.querySelector('.files-welcome')) return;
    host.innerHTML = html;
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(host.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }
  if (!fileTabs.length) {
    showWelcome(fileContent, `
      <div class="files-welcome">
        <i data-lucide="folder-tree" class="lucide-icon files-welcome-icon"></i>
        <div class="files-welcome-title">File Editor</div>
        <div class="files-welcome-text">Pick a file from the Explorer on the left to open it here.</div>
      </div>`);
  }
  if (!termTabs.length) {
    showWelcome(termContent, `
      <div class="files-welcome">
        <i data-lucide="terminal" class="lucide-icon files-welcome-icon"></i>
        <div class="files-welcome-title">Terminal</div>
        <div class="files-welcome-text">Open a session from the launcher on the left, or press <kbd>Ctrl</kbd>+<kbd>\`</kbd>.</div>
      </div>`);
  }

  // Re-use existing panes; create missing ones; remove closed ones — once
  // per container so we don't accidentally drop a pane that lives in the
  // other content area.
  function syncContainer(host, tabs) {
    if (!host) return;
    const existing = new Map();
    host.querySelectorAll('.files-editor-pane').forEach((p) => existing.set(p.dataset.path, p));
    for (const [path, pane] of existing) {
      if (!tabs.find((t) => t.path === path)) {
        pane.remove();
        existing.delete(path);
      }
    }
    if (tabs.length) {
      const welcome = host.querySelector('.files-welcome');
      if (welcome) welcome.remove();
    }
    for (const tab of tabs) {
      let pane = existing.get(tab.path);
      const wantMode = paneModeForTab(tab);
      if (pane && pane.dataset.mode !== wantMode) {
        pane.remove();
        pane = null;
      }
      if (pane && wantMode === 'text') {
        const ta = pane.querySelector('textarea.files-textarea');
        if (ta && ta.value !== tab.content) {
          ta.value = tab.content || '';
          updateHighlightOverlay(pane);
        }
      }
      if (!pane) {
        pane = buildPaneForTab(tab, wantMode);
        host.appendChild(pane);
      }
      pane.classList.toggle('active', tab.path === activePathForKind(tab.kind));
    }
  }
  syncContainer(fileContent, fileTabs);
  syncContainer(termContent, termTabs);

  const activeFile = activeFilePath
    ? openTabs.find((t) => t.kind !== 'terminal' && t.path === activeFilePath)
    : null;
  updateStatusBar(activeFile || null);
}

// ── Per-tab pane mode ─────────────────────────────────────────────

function paneModeForTab(tab) {
  if (tab.kind === 'terminal') return 'terminal';
  if (isImageFile(tab.name)) return 'image';
  if (tab.binary)             return 'binary';
  if (isMarkdownFile(tab.name) && tab.preview) return 'markdown';
  return 'text';
}

function isImageFile(name) {
  return /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(name);
}
function isMarkdownFile(name) {
  return /\.(md|markdown|mdx)$/i.test(name);
}

function imageDataUrl(tab) {
  const ext = (tab.name.split('.').pop() || '').toLowerCase();
  const mime = {
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
    gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
    ico: 'image/x-icon', svg: 'image/svg+xml', avif: 'image/avif',
  }[ext] || 'application/octet-stream';
  if (tab.encoding === 'base64') return 'data:' + mime + ';base64,' + tab.content;
  // utf-8 SVG: percent-encode and inline
  return 'data:' + mime + ';utf8,' + encodeURIComponent(tab.content || '');
}

function buildPaneForTab(tab, mode) {
  const pane = document.createElement('div');
  pane.className = 'files-editor-pane';
  pane.dataset.path = tab.path;
  pane.dataset.mode = mode;

  if (mode === 'image') {
    pane.classList.add('files-image-pane');
    const img = document.createElement('img');
    img.className = 'files-image-preview';
    img.alt = tab.name;
    img.src = imageDataUrl(tab);
    pane.appendChild(img);
  } else if (mode === 'binary') {
    pane.innerHTML =
      '<div class="files-binary-msg">' +
        '<i data-lucide="file-warning" class="lucide-icon"></i>' +
        '<div>Binary file (' + formatBytes(tab.size) + ')</div>' +
        '<div style="margin-top:6px;font-size:11px;opacity:0.7;">Editing binary files is not supported here.</div>' +
      '</div>';
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(pane.querySelectorAll('[data-lucide]:not(.lucide)')) });
  } else if (mode === 'markdown') {
    const md = document.createElement('div');
    md.className = 'files-markdown-preview';
    md.innerHTML = renderMarkdown(tab.content || '');
    pane.appendChild(md);
  } else if (mode === 'terminal') {
    pane.classList.add('files-terminal-pane');
    // Find bar above the host — hidden until Ctrl+F. Placed before the
    // host so it stacks on top in flex-column layout.
    const findBar = buildTerminalFindBar();
    pane.appendChild(findBar);
    // Scroll wrapper owns the horizontal scrollbar in no-wrap mode. The
    // host inside is grown wider than the wrapper via CSS so the user can
    // swipe / drag to see off-screen content.
    const scrollWrap = document.createElement('div');
    scrollWrap.className = 'files-terminal-scroll';
    if (tab.wrap === false) scrollWrap.classList.add('files-terminal-scroll-nowrap');
    const host = document.createElement('div');
    host.className = 'files-terminal-host';
    if (tab.wrap === false) host.classList.add('files-terminal-host-nowrap');
    scrollWrap.appendChild(host);
    pane.appendChild(scrollWrap);

    // Drag-and-drop: dropping a file from the file tree pastes its absolute
    // path (shell-quoted) at the current prompt. The tree marshals the path
    // as text/plain in dragstart; here we just unpack and forward to the PTY.
    host.addEventListener('dragover', (e) => {
      if (e.dataTransfer && Array.from(e.dataTransfer.types).indexOf('text/plain') !== -1) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        host.classList.add('files-terminal-drop-target');
      }
    });
    host.addEventListener('dragleave', () => host.classList.remove('files-terminal-drop-target'));
    host.addEventListener('drop', (e) => {
      host.classList.remove('files-terminal-drop-target');
      const raw = e.dataTransfer && e.dataTransfer.getData('text/plain');
      if (!raw || !tab.instance) return;
      e.preventDefault();
      tab.instance.paste(shellQuote(raw) + ' ');
      tab.instance.focus();
    });

    // Long-press on mobile → context menu with Copy / Paste / Select all.
    // Fires after a 500ms hold that didn't move; cancelled on move / lift.
    wireTerminalLongPress(host, () => tab.instance);
    // Two-finger pinch → adjust the global terminal font size.
    wireTerminalPinchZoom(host);

    // xterm.open() measures its host immediately. The pane has just been
    // created (not yet appended to the document) — defer xterm creation
    // until after the pane is attached AND the browser has reflowed, so
    // fit() sees the final host width. Without the rAF, on slow mobile
    // browsers the host can still report a stale width and xterm picks
    // too many cols, making the shell think it has more room than it
    // does and wrap behaviour appears broken.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!document.body.contains(pane)) return;
      try {
        // tab.path is the backend session_id (see pushTerminalTab). Passing
        // the existing id on restore reattaches to the running shell.
        tab.instance = createTerminalInstance(host, tab.path, {
          initialCommand: tab.initialCommand || '',
          wrap: tab.wrap !== false,           // default true unless persisted false
          fontSize: getTerminalFontSize(),    // global setting, shared across tabs
        });
        // Consume the command — buildPaneForTab can be called again later
        // (e.g. pane mode swap), but the shell already has it.
        tab.initialCommand = '';
        // Drive the per-tab status dot from the WS state machine.
        tab.instance.onStateChange((s) => _updateTabConnDot(tab.path, s));
        tab.instance.fit();
        // Belt-and-braces second fit after another paint — covers any
        // late layout shift from the address bar settling on iOS Safari.
        setTimeout(() => { if (tab.instance) tab.instance.fit(); }, 150);
        // Wire Ctrl+F → find bar. Capture-phase on the host so we preempt
        // xterm's keydown handler (which otherwise forwards Ctrl+F bytes
        // to the shell).
        host.addEventListener('keydown', (e) => {
          if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
            e.preventDefault();
            e.stopPropagation();
            openTerminalFindBar(findBar, tab.instance);
          }
        }, true);
        if (tab.path === activeTerminalId) tab.instance.focus();
      } catch (e) {
        host.textContent = 'Failed to start terminal: ' + (e.message || e);
      }
    }));
  } else {
    // text mode: textarea (transparent) overlaid on a <pre> for Prism
    buildTextEditorPane(pane, tab);
  }
  return pane;
}

// ── Text editor pane (textarea + Prism overlay + find bar) ────────

function buildTextEditorPane(pane, tab) {
  const findBar = buildFindBar(tab);
  pane.appendChild(findBar);

  const wrap = document.createElement('div');
  wrap.className = 'files-code-wrap';

  const highlight = document.createElement('pre');
  highlight.className = 'files-code-highlight';
  highlight.setAttribute('aria-hidden', 'true');
  const code = document.createElement('code');
  code.className = 'language-' + getPrismLang(tab.name);
  highlight.appendChild(code);
  wrap.appendChild(highlight);

  const ta = document.createElement('textarea');
  ta.className = 'files-textarea files-textarea-overlay' + (tab.wrap ? ' wrap' : '');
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
    scheduleHighlight(pane);
  });
  ta.addEventListener('scroll', () => {
    highlight.scrollTop = ta.scrollTop;
    highlight.scrollLeft = ta.scrollLeft;
  });
  ta.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveTab(tab.path);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      openFindBar(pane, /* withReplace */ false);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'h') {
      e.preventDefault();
      openFindBar(pane, /* withReplace */ true);
      return;
    }
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = start + 2;
      tab.content = ta.value;
      if (!tab.dirty) { tab.dirty = true; renderTabs(); }
      updateStatusBar(tab);
      scheduleHighlight(pane);
    }
  });
  wrap.appendChild(ta);
  pane.appendChild(wrap);

  // Initial highlight pass — done lazily so the autoloader has a tick
  // to fetch the grammar.
  scheduleHighlight(pane);
}

// ── Syntax highlighting (Prism, debounced) ────────────────────────

function getPrismLang(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  return {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'tsx', jsx: 'jsx',
    py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java',
    c: 'c', cpp: 'cpp', cc: 'cpp', h: 'c', hpp: 'cpp',
    html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup',
    css: 'css', scss: 'scss', sass: 'sass', less: 'less',
    json: 'json', json5: 'json5', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    md: 'markdown', mdx: 'markdown',
    sh: 'bash', bash: 'bash', zsh: 'bash',
    sql: 'sql', php: 'php', swift: 'swift', kt: 'kotlin',
    dockerfile: 'docker', makefile: 'makefile',
  }[ext] || 'plain';
}

const _highlightTimers = new WeakMap();
function scheduleHighlight(pane) {
  const prev = _highlightTimers.get(pane);
  if (prev) cancelAnimationFrame(prev);
  const id = requestAnimationFrame(() => updateHighlightOverlay(pane));
  _highlightTimers.set(pane, id);
}

function updateHighlightOverlay(pane) {
  const code = pane && pane.querySelector('.files-code-highlight > code');
  const ta = pane && pane.querySelector('textarea.files-textarea-overlay');
  if (!code || !ta) return;
  const value = ta.value;
  // Append a newline so the highlight has the same trailing-line height
  // as the textarea (which always reserves a blank line for the caret).
  if (window.Prism && window.Prism.highlight) {
    const langClass = (code.className.match(/language-(\S+)/) || [])[1] || 'plain';
    if (langClass !== 'plain') {
      const grammar = window.Prism.languages[langClass];
      if (grammar) {
        try {
          code.innerHTML = window.Prism.highlight(value + '\n', grammar, langClass);
          return;
        } catch (_) {}
      } else if (window.Prism.plugins && window.Prism.plugins.autoloader) {
        // Trigger the autoloader to fetch the language grammar, then retry.
        window.Prism.plugins.autoloader.loadLanguages([langClass], () => {
          if (document.body.contains(pane)) updateHighlightOverlay(pane);
        });
      }
    }
  }
  // Fallback / pre-Prism state / plain text: just show the raw value.
  code.textContent = value + '\n';
}

// ── Markdown rendering ────────────────────────────────────────────

function renderMarkdown(src) {
  try {
    if (window.marked && typeof window.marked.parse === 'function') {
      return window.marked.parse(src || '');
    }
  } catch (_) {}
  // Fallback if marked hasn't loaded: escape and show as preformatted text.
  const escaped = (src || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  return '<pre style="white-space:pre-wrap;">' + escaped + '</pre>';
}

// ── Find / Replace bar ────────────────────────────────────────────

function buildFindBar(tab) {
  const bar = document.createElement('div');
  bar.className = 'files-findbar';
  bar.hidden = true;
  bar.innerHTML =
    '<input type="text" class="files-findbar-input files-findbar-find"    placeholder="Find" spellcheck="false">' +
    '<span class="files-findbar-count">0 / 0</span>' +
    '<button type="button" class="files-findbar-btn" data-act="prev"  title="Previous (Shift+Enter)"><i data-lucide="chevron-up" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="next"  title="Next (Enter)"><i data-lucide="chevron-down" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="case"  title="Match case">Aa</button>' +
    '<input type="text" class="files-findbar-input files-findbar-replace" placeholder="Replace" spellcheck="false">' +
    '<button type="button" class="files-findbar-btn" data-act="replace"     title="Replace">↩</button>' +
    '<button type="button" class="files-findbar-btn" data-act="replace-all" title="Replace all">↩↩</button>' +
    '<button type="button" class="files-findbar-btn" data-act="close" title="Close (Esc)"><i data-lucide="x" class="lucide-icon"></i></button>';

  const findInput    = bar.querySelector('.files-findbar-find');
  const replaceInput = bar.querySelector('.files-findbar-replace');
  const countEl      = bar.querySelector('.files-findbar-count');
  const caseBtn      = bar.querySelector('[data-act="case"]');
  let caseSensitive = false;

  function getTextarea() {
    const pane = bar.closest('.files-editor-pane');
    return pane && pane.querySelector('textarea.files-textarea-overlay');
  }

  function allMatches() {
    const ta = getTextarea();
    const needle = findInput.value;
    if (!ta || !needle) return [];
    const hay = caseSensitive ? ta.value : ta.value.toLowerCase();
    const n   = caseSensitive ? needle   : needle.toLowerCase();
    const out = [];
    let i = 0;
    while ((i = hay.indexOf(n, i)) !== -1) {
      out.push(i);
      i += Math.max(1, n.length);
    }
    return out;
  }

  function refresh(active) {
    const matches = allMatches();
    countEl.textContent = matches.length ? ((typeof active === 'number' ? active + 1 : 1) + ' / ' + matches.length) : '0 / 0';
    return matches;
  }

  function findCurrentIndex(matches) {
    const ta = getTextarea();
    if (!ta || !matches.length) return -1;
    const caret = ta.selectionStart;
    for (let i = 0; i < matches.length; i++) {
      if (matches[i] >= caret) return i;
    }
    return matches.length - 1;
  }

  function jumpTo(matches, idx) {
    const ta = getTextarea();
    if (!ta || !matches.length) return;
    const i = ((idx % matches.length) + matches.length) % matches.length;
    const start = matches[i];
    const end = start + findInput.value.length;
    ta.focus();
    ta.setSelectionRange(start, end);
    // Make sure the selection is visible (textarea doesn't auto-scroll on programmatic selection)
    const before = ta.value.slice(0, start);
    const line = before.split('\n').length;
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 18;
    const target = (line - 3) * lineHeight;
    if (ta.scrollTop > target || ta.scrollTop + ta.clientHeight < target + lineHeight * 4) {
      ta.scrollTop = Math.max(0, target);
    }
    refresh(i);
  }

  function next() {
    const matches = refresh();
    if (!matches.length) return;
    jumpTo(matches, findCurrentIndex(matches));
  }
  function prev() {
    const matches = refresh();
    if (!matches.length) return;
    const cur = findCurrentIndex(matches);
    jumpTo(matches, cur - 1);
  }
  function replaceOne() {
    const ta = getTextarea();
    if (!ta || !findInput.value) return;
    const selected = ta.value.substring(ta.selectionStart, ta.selectionEnd);
    const eq = caseSensitive ? selected === findInput.value : selected.toLowerCase() === findInput.value.toLowerCase();
    if (eq) {
      const start = ta.selectionStart;
      const replacement = replaceInput.value;
      ta.setRangeText(replacement, start, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
    next();
  }
  function replaceAll() {
    const ta = getTextarea();
    if (!ta || !findInput.value) return;
    const needle = findInput.value;
    const flags = caseSensitive ? 'g' : 'gi';
    const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(escaped, flags);
    const newVal = ta.value.replace(re, replaceInput.value);
    if (newVal !== ta.value) {
      ta.value = newVal;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      refresh();
    }
  }

  findInput.addEventListener('input', () => refresh());
  findInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); e.shiftKey ? prev() : next(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFindBar(bar); }
  });
  replaceInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); replaceOne(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFindBar(bar); }
  });
  bar.querySelector('[data-act="next"]').addEventListener('click', next);
  bar.querySelector('[data-act="prev"]').addEventListener('click', prev);
  bar.querySelector('[data-act="replace"]').addEventListener('click', replaceOne);
  bar.querySelector('[data-act="replace-all"]').addEventListener('click', replaceAll);
  bar.querySelector('[data-act="close"]').addEventListener('click', () => closeFindBar(bar));
  caseBtn.addEventListener('click', () => {
    caseSensitive = !caseSensitive;
    caseBtn.classList.toggle('active', caseSensitive);
    refresh();
  });
  return bar;
}

function openFindBar(pane, withReplace) {
  const bar = pane && pane.querySelector('.files-findbar');
  if (!bar) return;
  const wasHidden = bar.hidden;
  bar.classList.toggle('with-replace', !!withReplace);
  bar.hidden = false;
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(bar.querySelectorAll('[data-lucide]:not(.lucide)')) });
  const input = withReplace
    ? bar.querySelector('.files-findbar-replace')
    : bar.querySelector('.files-findbar-find');
  // Pre-fill find input with the current selection only when opening fresh
  if (wasHidden) {
    const ta = pane.querySelector('textarea.files-textarea-overlay');
    if (ta) {
      const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
      if (sel && !sel.includes('\n')) {
        bar.querySelector('.files-findbar-find').value = sel;
      }
    }
  }
  setTimeout(() => { if (input) { input.focus(); input.select(); } }, 0);
}

function closeFindBar(bar) {
  bar.hidden = true;
  bar.classList.remove('with-replace');
  const pane = bar.closest('.files-editor-pane');
  const ta = pane && pane.querySelector('textarea.files-textarea-overlay');
  if (ta) ta.focus();
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
  // Terminal tabs have no path / save / dirty state — render a simple label.
  if (tab.kind === 'terminal') {
    path.textContent = tab.name;
    info.classList.remove('dirty');
    info.innerHTML = '';
    const meta = document.createElement('span');
    meta.textContent = 'PTY session';
    info.appendChild(meta);
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

  const tabName = name || path.split('/').pop();
  openTabs.push({
    path: data.path || path,
    name: tabName,
    content: data.content,
    dirty: false,
    binary: data.binary,
    encoding: data.encoding,
    size: data.size,
    // .md files open in rendered preview by default; toggle in tab menu
    // switches back to raw editing. Other types open as plain text.
    preview: isMarkdownFile(tabName),
  });
  activeFilePath = path;
  renderTabs();
  renderEditorPanes();
  persistTabs();
}

function activateTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  setActivePathForTab(tab);
  renderTabs();
  // Just flip active class on the matching content area instead of
  // re-rendering everything.
  const host = document.getElementById(
    tab.kind === 'terminal' ? 'files-term-content' : 'files-content',
  );
  if (host) {
    host.querySelectorAll('.files-editor-pane').forEach((p) => {
      p.classList.toggle('active', p.dataset.path === path);
    });
  }
  if (tab.kind !== 'terminal') updateStatusBar(tab);
  // Terminal tabs need a refit each time they regain focus — xterm can't
  // measure while its pane is display:none, so any window resize that
  // happened while another tab was active is unaccounted for until now.
  if (tab.kind === 'terminal' && tab.instance) {
    setTimeout(() => {
      tab.instance.fit();
      tab.instance.focus();
    }, 30);
  }
  // Bring the activated tab into view if it's outside the visible window
  const barId = tab.kind === 'terminal' ? '#files-term-tabs' : '#files-tabs';
  const tabEl = document.querySelector(barId + ' .files-tab[data-path="' + cssEscape(path) + '"]');
  if (tabEl && typeof tabEl.scrollIntoView === 'function') {
    tabEl.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
  try {
    localStorage.setItem(
      tab.kind === 'terminal' ? LS_ACTIVE_TERMINAL : LS_ACTIVE_FILE,
      path,
    );
  } catch (_) {}
}

async function closeTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.closing) return;          // already in-flight; ignore repeat clicks
  if (tab.dirty) {
    if (!confirm('Discard unsaved changes to ' + tab.name + '?')) return;
  }

  // Terminal tabs: only remove from the UI after the backend confirms the
  // PTY is gone. If the DELETE fails (network blip, server down, …) we keep
  // the tab open so the user can retry instead of silently leaking the
  // still-running shell.
  if (tab.kind === 'terminal' && tab.instance) {
    tab.closing = true;
    renderTabs();
    try {
      await tab.instance.closeBackendSession();
    } catch (e) {
      tab.closing = false;
      renderTabs();
      alert(
        'Could not close terminal "' + tab.name + '":\n\n' + (e.message || e) +
        '\n\nThe shell may still be running on the server. Try again.',
      );
      return;
    }
    try { tab.instance.dispose(); } catch (_) {}
    tab.instance = null;
  }

  const idx = openTabs.findIndex((t) => t.path === path);
  if (idx < 0) return;              // another close finished first
  const closedKind = tab.kind === 'terminal' ? 'terminal' : 'file';
  openTabs.splice(idx, 1);
  // Remove pane from whichever container hosts it.
  const hostId = closedKind === 'terminal' ? 'files-term-content' : 'files-content';
  const host = document.getElementById(hostId);
  if (host) {
    const pane = host.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
  }
  // Promote the neighbour of the same kind to active (don't pick a file
  // tab as the active terminal, or vice versa).
  const sameKind = openTabs.filter((t) =>
    closedKind === 'terminal' ? t.kind === 'terminal' : t.kind !== 'terminal'
  );
  if (closedKind === 'terminal' && activeTerminalId === path) {
    activeTerminalId = sameKind.length ? sameKind[Math.min(idx, sameKind.length - 1)].path : null;
  }
  if (closedKind === 'file' && activeFilePath === path) {
    activeFilePath = sameKind.length ? sameKind[Math.min(idx, sameKind.length - 1)].path : null;
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
    const editorRect = document.getElementById('admin-tools').getBoundingClientRect();
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
    // Terminal tabs are persisted by session_id only. On reload, the backend
    // PTY is still alive (sessions outlive page reloads) and reconnecting
    // with the same id reattaches us to it.
    const minimal = openTabs.map((t) => {
      if (t.kind === 'terminal') {
        return { path: t.path, name: t.name, kind: 'terminal', wrap: t.wrap !== false };
      }
      return { path: t.path, name: t.name, wrap: !!t.wrap, preview: !!t.preview };
    });
    localStorage.setItem(LS_OPEN_TABS, JSON.stringify(minimal));
    localStorage.setItem(LS_ACTIVE_FILE, activeFilePath || '');
    localStorage.setItem(LS_ACTIVE_TERMINAL, activeTerminalId || '');
    // Clear the legacy unified key so old code (or stale tabs) don't pick
    // up a stale pointer of the wrong kind.
    localStorage.removeItem(LS_ACTIVE_TAB);
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
    // Read both new per-kind keys; fall back to the legacy unified key for
    // users who saved their state before the split.
    const legacyActive = localStorage.getItem(LS_ACTIVE_TAB) || '';
    let wantFile = localStorage.getItem(LS_ACTIVE_FILE) || '';
    let wantTerm = localStorage.getItem(LS_ACTIVE_TERMINAL) || '';
    if (!wantFile && !wantTerm && legacyActive) {
      if (legacyActive.startsWith('terminal:')) wantTerm = legacyActive;
      else wantFile = legacyActive;
    }
    if (!Array.isArray(saved) || !saved.length) return;
    // Open in order, swallow failures (file may have been deleted, or a
    // terminal's backend session may have died and need to respawn).
    for (const t of saved) {
      try {
        if (t.kind === 'terminal') {
          // Reattach to the running PTY identified by t.path. If the shell
          // already exited, the backend will spawn a fresh one for that id.
          pushTerminalTab(t.path, t.name);
          const restored = openTabs[openTabs.length - 1];
          if (restored && t.wrap === false) restored.wrap = false;
          continue;
        }
        await openFile(t.path, t.name);
        const opened = openTabs.find((o) => o.path === t.path);
        if (!opened) continue;
        if (t.wrap)    opened.wrap = true;
        if (t.preview) opened.preview = true;
        if (t.wrap || t.preview) {
          // Re-render so the wrap class / preview mode get applied
          const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(opened.path) + '"]');
          if (pane) pane.remove();
        }
      } catch (_) {}
    }
    renderEditorPanes();
    if (wantFile && openTabs.find((t) => t.kind !== 'terminal' && t.path === wantFile)) {
      activateTab(wantFile);
    }
    if (wantTerm && openTabs.find((t) => t.kind === 'terminal' && t.path === wantTerm)) {
      activateTab(wantTerm);
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
  initTabCarousel();
  installFilesDropGuard();
  initSidebarViewSwitcher();
  initSettingsToggle();
  initSidebarMaximize();
  initFilesTerminalButton();
  renderTabs();
  renderEditorPanes();
}

// ── In-page terminal tabs ──────────────────────────────────────────
//
// Each click of the "new terminal" button in the sidebar pushes a fresh
// tab with kind === 'terminal' and spawns its own xterm + PTY WebSocket.
// Terminal tabs sit alongside file tabs in the same tab bar and share the
// same activate / close / drag-reorder machinery.

function _connStateTitle(s) {
  return s === 'connected'    ? 'Connected'
       : s === 'reconnecting' ? 'Reconnecting…'
       : s === 'error'        ? 'Disconnected — refresh to retry'
       :                        'Connecting…';
}

function _updateTabConnDot(tabPath, state) {
  // Connection dots live on terminal tabs, which render into the terminal
  // tab bar; fall back to the file bar for safety.
  const tabEl =
    document.querySelector('#files-term-tabs .files-tab[data-path="' + cssEscape(tabPath) + '"]') ||
    document.querySelector('#files-tabs .files-tab[data-path="' + cssEscape(tabPath) + '"]');
  if (!tabEl) return;
  const dot = tabEl.querySelector('.files-tab-conn-dot');
  if (!dot) return;
  dot.dataset.state = state;
  dot.title = _connStateTitle(state);
}

function newTerminalSessionId() {
  return 'terminal:' + randomUUID();
}

// ── Mobile long-press → Copy / Paste menu ─────────────────────────
//
// xterm.js doesn't natively expose a touch-friendly copy/paste UI.
// We watch for a still touch held >500ms on the terminal host and pop a
// small floating menu near the touch point. Cancelled on touchmove /
// touchcancel so it doesn't fight with the user's scrolling or selection.

const LONG_PRESS_MS = 500;
const LONG_PRESS_MOVE_TOLERANCE = 8;  // px — touchmove farther than this aborts

function wireTerminalLongPress(host, getInstance) {
  let timer = null;
  let startX = 0;
  let startY = 0;

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  host.addEventListener('touchstart', (e) => {
    // Multi-touch (e.g. pinch-to-zoom) cancels the long-press intent.
    if (!e.touches || e.touches.length !== 1) { cancel(); return; }
    const t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      const inst = getInstance && getInstance();
      if (inst) showTerminalContextMenu(startX, startY, inst);
    }, LONG_PRESS_MS);
  }, { passive: true });

  host.addEventListener('touchmove', (e) => {
    if (!e.touches || e.touches.length !== 1) { cancel(); return; }
    const t = e.touches[0];
    if (Math.abs(t.clientX - startX) > LONG_PRESS_MOVE_TOLERANCE ||
        Math.abs(t.clientY - startY) > LONG_PRESS_MOVE_TOLERANCE) {
      cancel();
    }
  }, { passive: true });

  host.addEventListener('touchend',    cancel, { passive: true });
  host.addEventListener('touchcancel', cancel, { passive: true });
}

// ── Mobile pinch-to-zoom ──────────────────────────────────────────
//
// Two-finger pinch on the terminal host adjusts the global font size.
// Uses the same setter as the menu's Zoom in / out items so the new size
// propagates to every open terminal and persists in localStorage.

function wireTerminalPinchZoom(host) {
  let startDist = 0;
  let startFontSize = 0;
  let lastApplied = 0;

  function dist(t0, t1) {
    const dx = t0.clientX - t1.clientX;
    const dy = t0.clientY - t1.clientY;
    return Math.hypot(dx, dy);
  }

  host.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length < 2) return;
    // Prevent the OS-level pinch zoom from kicking in on top of ours.
    if (e.cancelable) e.preventDefault();
    startDist = dist(e.touches[0], e.touches[1]);
    startFontSize = getTerminalFontSize();
    lastApplied = startFontSize;
  }, { passive: false });

  host.addEventListener('touchmove', (e) => {
    if (!e.touches || e.touches.length < 2 || startDist === 0) return;
    if (e.cancelable) e.preventDefault();
    const d = dist(e.touches[0], e.touches[1]);
    if (d === 0) return;
    const ratio = d / startDist;
    let next = Math.round(startFontSize * ratio);
    if (next < 8) next = 8;
    if (next > 32) next = 32;
    if (next !== lastApplied) {
      setTerminalFontSize(next);
      lastApplied = next;
    }
  }, { passive: false });

  function end() { startDist = 0; }
  host.addEventListener('touchend',    end, { passive: true });
  host.addEventListener('touchcancel', end, { passive: true });
}

function showTerminalContextMenu(x, y, instance) {
  closeTerminalContextMenu();
  const menu = document.createElement('div');
  menu.className = 'files-terminal-ctxmenu';
  menu.id = 'files-terminal-ctxmenu';

  function btn(label, icon, action, disabled) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'files-terminal-ctxmenu-item' + (disabled ? ' disabled' : '');
    b.disabled = !!disabled;
    b.innerHTML = '<i data-lucide="' + icon + '" class="lucide-icon"></i><span>' + label + '</span>';
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      closeTerminalContextMenu();
      if (!disabled) action();
    });
    return b;
  }

  const hasSel = !!(instance && instance.term && instance.term.hasSelection && instance.term.hasSelection());

  menu.appendChild(btn('Copy',       'copy',      async () => {
    try {
      const text = (instance.term.getSelection && instance.term.getSelection()) || '';
      if (text) await navigator.clipboard.writeText(text);
    } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }, !hasSel));

  menu.appendChild(btn('Paste',      'clipboard', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && instance.paste) instance.paste(text);
    } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }));

  menu.appendChild(btn('Select all', 'list',      () => {
    try { instance.term.selectAll && instance.term.selectAll(); } catch (_) {}
  }));

  menu.appendChild(btn('Clear',      'trash-2',   () => {
    try { instance.term.clear && instance.term.clear(); } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }));

  document.body.appendChild(menu);

  // Clamp into viewport.
  const rect = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(window.innerWidth  - rect.width  - 8, x - rect.width / 2));
  const top  = Math.max(8, Math.min(window.innerHeight - rect.height - 8, y - rect.height - 8));
  menu.style.left = left + 'px';
  menu.style.top  = top  + 'px';

  if (window.lucide) {
    window.lucide.createIcons({ nodes: Array.from(menu.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }

  // Close on any tap / click outside, or on scroll. Touchstart capture so
  // we catch the gesture before xterm processes it as a new selection.
  const outside = (ev) => {
    if (!menu.contains(ev.target)) closeTerminalContextMenu();
  };
  setTimeout(() => {
    document.addEventListener('mousedown',  outside, true);
    document.addEventListener('touchstart', outside, true);
    document.addEventListener('scroll',     closeTerminalContextMenu, true);
  }, 0);
  menu._outsideHandler = outside;
}

function closeTerminalContextMenu() {
  const menu = document.getElementById('files-terminal-ctxmenu');
  if (!menu) return;
  if (menu._outsideHandler) {
    document.removeEventListener('mousedown',  menu._outsideHandler, true);
    document.removeEventListener('touchstart', menu._outsideHandler, true);
  }
  document.removeEventListener('scroll', closeTerminalContextMenu, true);
  menu.remove();
}

// ── Drag a file from the tree onto a terminal pane ────────────────
// Quote a path so it can be safely pasted at a POSIX shell prompt: bare
// when only safe chars, single-quoted otherwise (with embedded ' escaped).
function shellQuote(s) {
  if (s == null) return '';
  s = String(s);
  if (/^[a-zA-Z0-9._\/\-+=:@,]+$/.test(s)) return s;
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

// ── In-terminal find bar (xterm search addon) ─────────────────────

function buildTerminalFindBar() {
  const bar = document.createElement('div');
  bar.className = 'files-terminal-findbar';
  bar.hidden = true;
  bar.innerHTML =
    '<input type="text" class="files-terminal-findbar-input" placeholder="Find in terminal" spellcheck="false">' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="case" title="Match case">Aa</button>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="prev" title="Previous (Shift+Enter)"><i data-lucide="chevron-up" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="next" title="Next (Enter)"><i data-lucide="chevron-down" class="lucide-icon"></i></button>' +
    '<span class="files-terminal-findbar-status"></span>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="close" title="Close (Esc)"><i data-lucide="x" class="lucide-icon"></i></button>';
  return bar;
}

function openTerminalFindBar(bar, instance) {
  if (!bar || !instance) return;
  bar.hidden = false;
  if (window.lucide) {
    window.lucide.createIcons({ nodes: Array.from(bar.querySelectorAll('[data-lucide]:not(.lucide)')) });
  }
  const input = bar.querySelector('.files-terminal-findbar-input');
  const status = bar.querySelector('.files-terminal-findbar-status');
  const caseBtn = bar.querySelector('[data-act="case"]');

  // Listeners are wired once per bar. Subsequent opens just refocus.
  if (!bar.dataset.wired) {
    bar.dataset.wired = '1';
    bar._caseSensitive = false;

    function find(dir) {
      const q = input.value;
      if (!q) { status.textContent = ''; return; }
      const opts = { caseSensitive: bar._caseSensitive };
      const ok = dir === 'prev'
        ? instance.findPrevious(q, opts)
        : instance.findNext(q, opts);
      status.textContent = ok ? '' : 'No match';
    }
    function close() {
      bar.hidden = true;
      try { instance.clearSearch(); } catch (_) {}
      instance.focus();
    }
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); find(e.shiftKey ? 'prev' : 'next'); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    input.addEventListener('input', () => find('next'));
    bar.querySelector('[data-act="next"]').addEventListener('click', () => find('next'));
    bar.querySelector('[data-act="prev"]').addEventListener('click', () => find('prev'));
    bar.querySelector('[data-act="close"]').addEventListener('click', close);
    caseBtn.addEventListener('click', () => {
      bar._caseSensitive = !bar._caseSensitive;
      caseBtn.classList.toggle('active', bar._caseSensitive);
      find('next');
    });
  }

  input.focus();
  input.select();
}

function startInlineRename(tab, labelEl) {
  // Swap the label for a text input; commit on Enter/blur, cancel on Esc.
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'files-tab-rename-input';
  input.value = tab.name;
  input.spellcheck = false;
  input.maxLength = 60;

  let committed = false;
  function finish(save) {
    if (committed) return;
    committed = true;
    const v = input.value.trim();
    if (save && v && v !== tab.name) {
      tab.name = v;
      persistTabs();
    }
    renderTabs();   // rebuild — swaps the input back to a span
  }
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  // Prevent the parent tab's click/drag handlers from firing while editing.
  input.addEventListener('mousedown', (e) => e.stopPropagation());
  input.addEventListener('click', (e) => e.stopPropagation());

  labelEl.replaceWith(input);
  input.focus();
  input.select();
}

function pushTerminalTab(sessionId, name, opts) {
  opts = opts || {};
  openTabs.push({
    // The tab path doubles as the backend session_id — terminal tabs use a
    // 'terminal:<uuid>' prefix that can't collide with real file paths.
    path: sessionId,
    name: name || ('Terminal ' + (openTabs.filter((t) => t.kind === 'terminal').length + 1)),
    kind: 'terminal',
    instance: null,           // set by buildPaneForTab once xterm is opened
    dirty: false,
    binary: false,
    // If set, typed into the shell once on first WS open. Not persisted —
    // persistTabs() strips it so reloads don't re-run the command.
    initialCommand: opts.initialCommand || '',
  });
}

function openNewTerminalTab() {
  const id = newTerminalSessionId();
  pushTerminalTab(id);
  activeTerminalId = id;
  renderTabs();
  renderEditorPanes();
  persistTabs();
  // Land the user on the terminal main so the freshly-opened session is
  // actually visible. Same call as a click on the zap strip icon.
  applySidebarView('terminal');
  try { localStorage.setItem(LS_SIDEBAR_VIEW, 'terminal'); } catch (_) {}
}

// Like openNewTerminalTab but pre-types `command` into the shell. Used by
// the sidebar's quick-launch and tmux-session lists.
export function openNewTerminalTabWithCommand(command, name) {
  const id = newTerminalSessionId();
  pushTerminalTab(id, name, { initialCommand: command || '' });
  activeTerminalId = id;
  renderTabs();
  renderEditorPanes();
  persistTabs();
  applySidebarView('terminal');
  try { localStorage.setItem(LS_SIDEBAR_VIEW, 'terminal'); } catch (_) {}
  // On mobile the sidebar fills the screen; switch to the strip so the
  // newly-opened terminal becomes visible.
  const sidebar = document.getElementById('files-sidebar');
  if (sidebar && isMobileLayout() && sidebar.dataset.state === 'max') {
    setSidebarState('strip');
  }
}

function initFilesTerminalButton() {
  // Keep visible terminal tabs in sync with sidebar resize drags.
  const handle = document.getElementById('files-resize-handle');
  if (handle) {
    handle.addEventListener('mouseup', () => {
      const tab = getActiveTerminalTab();
      if (tab && tab.instance) {
        setTimeout(() => tab.instance.fit(), 30);
      }
    });
  }

  // Ctrl+` (Backquote) opens a new terminal tab from anywhere in the app.
  // Capture-phase so it preempts xterm's keyboard handler when focus is in
  // an existing terminal — without that, xterm swallows the backtick.
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Backquote') return;
    if (!e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    e.stopPropagation();
    // Switch to the Admin Tools tab if we're not already on it.
    const tabSelect = document.getElementById('main-tab-select');
    if (tabSelect && tabSelect.value !== 'files') {
      tabSelect.value = 'files';
      tabSelect.dispatchEvent(new Event('change', { bubbles: true }));
    }
    openNewTerminalTab();
  }, true);

  // Zoom shortcuts — only fire when a terminal tab is the active tab so
  // they don't hijack browser zoom on other pages. Capture-phase for the
  // same reason as Ctrl+`: xterm would otherwise see Ctrl+= / Ctrl+- and
  // forward bytes to the shell.
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    const active = getActiveTerminalTab();
    if (!active) return;
    // Don't steal these keys while the user is typing in an input — e.g.
    // the find bar or the inline rename input.
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    // '=' on US layouts produces Equal; '+' is the same key with shift,
    // which we excluded above. Some keyboards report '+' directly though.
    const isPlus  = e.code === 'Equal' || e.key === '+' || e.key === '=';
    const isMinus = e.code === 'Minus' || e.key === '-' || e.key === '_';
    const isZero  = e.code === 'Digit0' || e.key === '0';
    if (!isPlus && !isMinus && !isZero) return;
    e.preventDefault();
    e.stopPropagation();
    if (isPlus)  terminalZoom(+1);
    if (isMinus) terminalZoom(-1);
    if (isZero)  terminalResetZoom();
  }, true);
}

export function reconnectAllTerminals() {
  for (const t of openTabs) {
    if (t.kind === 'terminal' && t.instance && !t.closing) {
      try { t.instance.reconnect(); } catch (_) {}
    }
  }
}

// Refit the currently active terminal on viewport resize. Background tabs
// refit themselves when they next become active (see activateTab).
window.addEventListener('resize', () => {
  const tab = getActiveTerminalTab();
  if (tab && tab.instance) tab.instance.fit();
});

// ── Sidebar state cycle ───────────────────────────────────────────
//
// Desktop toggles split ↔ strip (no "max" — the editor is always visible
// alongside the sidebar). Mobile cycles strip ↔ max (no usable split view
// on small screens). The strip column itself is always rendered; in strip
// state it's the only thing visible, in split/max it's the left rail next
// to the active panel.

const LS_SIDEBAR_STATE = 'files.sidebarState';   // 'split' | 'max' | 'strip'

export function isMobileLayout() {
  if (typeof window.__isMobileChatLayout === 'function') return window.__isMobileChatLayout();
  return window.innerWidth <= 800;
}

function initSidebarMaximize() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore prior state. On mobile, fold 'split' into 'strip'.
  let saved = localStorage.getItem(LS_SIDEBAR_STATE) || 'split';
  if (saved !== 'split' && saved !== 'max' && saved !== 'strip') saved = 'split';
  if (isMobileLayout() && saved === 'split') saved = 'strip';
  setSidebarState(saved);

  // Delegate clicks on the cycle button and the strip's view-switch buttons.
  sidebar.addEventListener('click', (e) => {
    const cycle = e.target.closest('.files-maximize-btn');
    if (cycle && sidebar.contains(cycle)) {
      e.stopPropagation();
      cycleSidebarState();
      return;
    }
    const stripView = e.target.closest('.files-strip-view');
    if (stripView && sidebar.contains(stripView)) {
      e.stopPropagation();
      const v = stripView.dataset.view;
      if (!v) return;
      applySidebarView(v);
      try { localStorage.setItem(LS_SIDEBAR_VIEW, v); } catch (_) {}
      // Other views need the panel column visible — expand from strip
      // mode. Settings has no panel and renders full-bleed via CSS, so
      // leave the data-state alone (and skip the mobile 'max' that
      // would otherwise hide the settings main).
      if (v !== 'settings') {
        setSidebarState(isMobileLayout() ? 'max' : 'split');
      }
    }
  });
}

function cycleSidebarState() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  const cur = sidebar.dataset.state || 'split';
  const mobile = isMobileLayout();
  let next;
  if (mobile) {
    // 2-stage: strip ↔ max
    next = (cur === 'max') ? 'strip' : 'max';
  } else {
    // 2-stage: split ↔ strip (max removed on desktop)
    next = (cur === 'strip') ? 'split' : 'strip';
  }
  setSidebarState(next);
}

export function setSidebarState(state) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  if (state !== 'split' && state !== 'max' && state !== 'strip') state = 'split';
  if (isMobileLayout() && state === 'split') state = 'strip';
  // 'max' only exists on mobile — coerce stale localStorage to 'split' on desktop.
  if (!isMobileLayout() && state === 'max') state = 'split';
  sidebar.dataset.state = state;
  sidebar.classList.toggle('maximized', state === 'max');
  sidebar.classList.toggle('strip',     state === 'strip');

  // Update the cycle button's icon + title (lives in the strip). The icon
  // hints at the NEXT action, not the current state.
  const iconName = state === 'strip' ? 'chevrons-right' : 'chevrons-left';
  const title    = state === 'strip' ? 'Expand sidebar' : 'Collapse sidebar';
  sidebar.querySelectorAll('.files-maximize-btn').forEach((b) => {
    b.title = title;
    b.innerHTML = '<i data-lucide="' + iconName + '" class="lucide-icon"></i>';
  });

  // Strip is always rendered now; panels follow the current view (via
  // applySidebarView) but stay hidden when in strip mode.
  applySidebarView(sidebar.dataset.view || 'explorer');

  if (window.lucide) {
    window.lucide.createIcons({
      nodes: Array.from(sidebar.querySelectorAll('[data-lucide]:not(.lucide)')),
    });
  }
  try { localStorage.setItem(LS_SIDEBAR_STATE, state); } catch (_) {}
}

// ── Sidebar view switcher (Explorer ↔ Source Control) ─────────────

function initSidebarViewSwitcher() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore last view (default: explorer)
  const stored = localStorage.getItem(LS_SIDEBAR_VIEW);
  const want = (stored && stored in VIEW_MAIN_ID) ? stored : 'explorer';
  applySidebarView(want);
  // The toggle icons live in BOTH panel headers (so each header has its
  // own copy). Delegate click handling at the sidebar level so we catch
  // whichever pair is currently rendered.
  sidebar.addEventListener('click', (e) => {
    const btn = e.target.closest('.files-view-toggle-btn');
    if (!btn || !sidebar.contains(btn)) return;
    const v = btn.dataset.view;
    if (!v) return;
    // Click on the currently-active (greyed-out) icon = no-op.
    if (btn.classList.contains('active')) return;
    applySidebarView(v);
    try { localStorage.setItem(LS_SIDEBAR_VIEW, v); } catch (_) {}
  });
}

const VIEW_TITLE = {
  explorer: 'File Manager',
  git: 'Source control',
  database: 'Database',
  terminal: 'Terminal launchers',
  settings: 'Admin Configuration',
  interactions: 'Interactions',
  'runtime-loop': 'Runtime Loop',
};
const VIEW_SWITCH = {
  explorer: 'file manager',
  git: 'source control',
  database: 'database',
  terminal: 'terminal launchers',
  settings: 'admin configuration',
  interactions: 'interactions',
  'runtime-loop': 'runtime loop',
};

// Each sidebar view has a dedicated <main> on the right side. Switching
// the strip swaps which main is visible. The Settings view is just
// another entry — no overlay/toggle special-case.
const VIEW_MAIN_ID = {
  explorer:       'files-explorer-main',
  git:            'files-git-main',
  database:       'files-database-main',
  terminal:       'files-terminal-main',
  settings:       'files-settings-main',
  interactions:   'files-interactions-main',
  'runtime-loop': 'files-runtime-loop-main',
};

function applySidebarView(view) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  if (!(view in VIEW_MAIN_ID)) view = 'explorer';
  sidebar.dataset.view = view;
  // Update aria-selected on every view-toggle button (in panel headers
  // and in the strip). The Settings strip button now lives in
  // .files-strip-view, so it's covered by the same selector.
  sidebar.querySelectorAll(
    '.files-view-toggle-btn, .files-strip-view'
  ).forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
    if (b.classList.contains('files-view-toggle-btn')) {
      if (active) {
        b.setAttribute('aria-disabled', 'true');
        b.title = VIEW_TITLE[view] + ' (current view)';
      } else {
        b.removeAttribute('aria-disabled');
        b.title = 'Switch to ' + (VIEW_SWITCH[b.dataset.view] || 'explorer');
      }
    }
  });
  // In strip mode all sidebar panels stay hidden; otherwise the matching
  // panel shows. No panel has data-view="settings", so all panels hide
  // naturally for that view — the CSS rule on data-view="settings"
  // additionally collapses the panel column to strip width.
  const state = sidebar.dataset.state || 'split';
  sidebar.querySelectorAll('.files-sidebar-panel').forEach((p) => {
    p.hidden = (state === 'strip') || (p.dataset.view !== view);
  });
  if (view === 'git' && state !== 'strip') {
    // Lazy-load the git panel the first time, refresh on subsequent shows.
    openGitPanel(sidebar);
  }
  if (view === 'terminal' && state !== 'strip') {
    openTerminalLaunchersPanel();
  } else {
    stopTerminalLaunchersPolling();
  }
  // Right-pane swap: hide every per-view main except the one matching
  // `view`.
  const wantId = VIEW_MAIN_ID[view];
  document.querySelectorAll('#admin-tools .files-main[data-view]').forEach((el) => {
    el.hidden = (el.id !== wantId);
  });
  // Per-view background work (poll loops, lazy renders).
  if (view === 'database') {
    try { startAutoRefresh(); } catch (_) {}
  } else {
    try { stopAutoRefresh(); } catch (_) {}
  }
  if (view === 'settings') {
    try { startAppConfig(); } catch (_) {}
  } else {
    try { stopAppConfig(); } catch (_) {}
  }
  if (view === 'interactions') {
    try { startLoop(); } catch (_) {}
  } else {
    try { stopLoop(); } catch (_) {}
  }
  if (view === 'runtime-loop') {
    try { startLoopVisual(); } catch (_) {}
  } else {
    try { stopLoopVisual(); } catch (_) {}
  }
  if (view === 'git') {
    try { renderGitMain(); } catch (_) {}
  }
  if (view === 'terminal') {
    // Refit the active terminal once the main becomes visible — xterm
    // can't measure a display:none host.
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 30);
  }
}

// ── Terminal launchers sidebar panel ──────────────────────────────
//
// Two lists (quick launches, live tmux sessions) and a static hints
// section. Quick launches come from /api/v1/terminal/quick-launches and
// are essentially static for the lifetime of the page; tmux sessions are
// polled every 5s while the panel is visible.

let _ftQuickLaunchesLoaded = false;
let _ftTmuxPollTimer = null;

function ftEscapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function ftRenderLaunches(items) {
  const host = document.getElementById('ft-list-launches');
  if (!host) return;
  if (!items || !items.length) {
    host.innerHTML = '<div class="ft-empty">No quick launches configured</div>';
    return;
  }
  host.innerHTML = '';
  for (const it of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ft-row';
    row.title = it.command ? ('Run: ' + it.command) : 'Open a plain shell';
    const ic = it.icon || 'terminal';
    row.innerHTML = '<i data-lucide="' + ftEscapeHtml(ic) + '" class="lucide-icon ft-row-icon"></i>' +
                    '<span class="ft-row-label">' + ftEscapeHtml(it.name) + '</span>';
    row.addEventListener('click', () => {
      openNewTerminalTabWithCommand(it.command || '', it.name || undefined);
    });
    host.appendChild(row);
  }
  if (window.lucide) {
    try { window.lucide.createIcons({ nodes: Array.from(host.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {}
  }
}

function ftRenderTmux(items) {
  const host = document.getElementById('ft-list-tmux');
  if (!host) return;
  if (!items || !items.length) {
    host.innerHTML = '<div class="ft-empty">No tmux sessions running</div>';
    return;
  }
  host.innerHTML = '';
  for (const it of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ft-row';
    row.title = "Attach: tmux attach -t '" + (it.name || '') + "'";
    const dot = it.attached ? 'ft-row-dot ft-row-dot-on' : 'ft-row-dot';
    row.innerHTML =
      '<i data-lucide="square-terminal" class="lucide-icon ft-row-icon"></i>' +
      '<span class="ft-row-label">' + ftEscapeHtml(it.name) + '</span>' +
      '<span class="ft-row-meta">' + ftEscapeHtml(it.windows + (it.windows === 1 ? ' win' : ' wins')) + '</span>' +
      '<span class="' + dot + '" title="' + (it.attached ? 'attached' : 'detached') + '"></span>';
    row.addEventListener('click', () => {
      const cmd = "tmux attach -t '" + String(it.name).replace(/'/g, "'\\''") + "'";
      openNewTerminalTabWithCommand(cmd, 'tmux: ' + it.name);
    });
    host.appendChild(row);
  }
  if (window.lucide) {
    try { window.lucide.createIcons({ nodes: Array.from(host.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch (_) {}
  }
}

async function ftLoadQuickLaunches() {
  try {
    const data = await apiFetch('/api/v1/terminal/quick-launches');
    ftRenderLaunches(Array.isArray(data) ? data : []);
    _ftQuickLaunchesLoaded = true;
  } catch (e) {
    const host = document.getElementById('ft-list-launches');
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}

async function ftLoadTmuxSessions() {
  try {
    const data = await apiFetch('/api/v1/terminal/tmux-sessions');
    ftRenderTmux(Array.isArray(data) ? data : []);
  } catch (e) {
    const host = document.getElementById('ft-list-tmux');
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}

function openTerminalLaunchersPanel() {
  // Quick launches are effectively static; load once per page lifetime.
  if (!_ftQuickLaunchesLoaded) ftLoadQuickLaunches();
  // tmux list refreshes on every panel show, then polls every 5s while
  // the panel stays open.
  ftLoadTmuxSessions();
  stopTerminalLaunchersPolling();
  _ftTmuxPollTimer = setInterval(ftLoadTmuxSessions, 5000);
  // Wire the refresh button once. Repeat-safe: removeEventListener-then-add
  // would be wordy, so we use a sentinel attribute.
  const btn = document.getElementById('ft-refresh');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => {
      ftLoadQuickLaunches();
      ftLoadTmuxSessions();
    });
  }
  // Wire the "New terminal" primary action in the launcher panel. Replaces
  // the old standalone Terminal Tab strip icon.
  const newBtn = document.getElementById('ft-new-terminal');
  if (newBtn && !newBtn.dataset.wired) {
    newBtn.dataset.wired = '1';
    newBtn.addEventListener('click', () => openNewTerminalTab());
  }
}

function stopTerminalLaunchersPolling() {
  if (_ftTmuxPollTimer) {
    clearInterval(_ftTmuxPollTimer);
    _ftTmuxPollTimer = null;
  }
}

// ── Settings view (App Config) DOM relocation ────────────────────
//
// The Settings strip icon is a plain `.files-strip-view` with
// data-view="settings"; dispatch happens through applySidebarView. The
// only setup-time work needed is moving #app-config-container into
// #files-settings-main so the App Config UI lives where the view shows.
// Lifecycle (startAppConfig / stopAppConfig) is driven by
// applySidebarView too.

function initSettingsToggle() {
  const container = document.getElementById('app-config-container');
  const host = document.getElementById('files-settings-main');
  if (container && host && container.parentElement !== host) {
    host.appendChild(container);
    container.removeAttribute('hidden');
  }
}

// Relocate detached markup (App Config and the Database viewer) into the
// Admin Tools layout. The originals are parked at the bottom of #app-container
// in index.html so this module owns their final mount point. Idempotent.
export function relocateAdminToolsContainers() {
  // App Config — Settings view host
  const acHost = document.getElementById('files-settings-main');
  const acContainer = document.getElementById('app-config-container');
  if (acHost && acContainer && acContainer.parentElement !== acHost) {
    acHost.appendChild(acContainer);
    acContainer.removeAttribute('hidden');
  }
  // Database viewer — sidebar host receives #db-sidebar; main host receives
  // #db-toolbar then #db-table-view. The empty #db-panel and #db-viewer
  // wrappers are dropped once their children have been moved.
  const dbSbHost = document.getElementById('db-sidebar-host');
  const dbMainHost = document.getElementById('files-database-main');
  const dbSidebar = document.getElementById('db-sidebar');
  const dbToolbar = document.getElementById('db-toolbar');
  const dbTableView = document.getElementById('db-table-view');
  if (dbSbHost && dbSidebar && dbSidebar.parentElement !== dbSbHost) {
    dbSbHost.appendChild(dbSidebar);
  }
  if (dbMainHost && dbToolbar && dbToolbar.parentElement !== dbMainHost) {
    dbMainHost.appendChild(dbToolbar);
  }
  if (dbMainHost && dbTableView && dbTableView.parentElement !== dbMainHost) {
    dbMainHost.appendChild(dbTableView);
  }
  const dbPanel = document.getElementById('db-panel');
  if (dbPanel && !dbPanel.children.length) dbPanel.remove();
  const dbViewer = document.getElementById('db-viewer');
  if (dbViewer && !dbViewer.children.length) dbViewer.remove();
  const dbPark = document.getElementById('db-viewer-park');
  if (dbPark && !dbPark.children.length) dbPark.remove();
}

export async function startAdminTools() {
  initFiles();
  // Check admin access; show overlay if not
  let accessInfo = { is_admin: false, user_id: '', authenticated: false };
  try {
    accessInfo = await apiFetch('/check-access');
  } catch (e) {
    accessInfo = { is_admin: false, user_id: '', authenticated: false, error: e.message };
  }
  isAdmin = !!accessInfo.is_admin;

  const overlay = document.getElementById('files-restricted-overlay');
  const editor = document.getElementById('admin-tools');
  if (!isAdmin) {
    if (overlay) overlay.style.display = 'flex';
    if (editor) editor.style.display = 'none';
    const diag = document.getElementById('files-restricted-diag');
    if (diag) {
      diag.style.whiteSpace = 'pre-line';
      const cachedUid = localStorage.getItem('auth_user_id') || '';
      if (!accessInfo.authenticated) {
        if (cachedUid) {
          diag.textContent =
            'Browser thinks you are: ' + cachedUid + '\n' +
            'The server could not verify your session (token may be stale). ' +
            'Try signing out and back in.';
        } else {
          diag.textContent = 'Not signed in. Sign in as an admin user to access Admin Tools.';
        }
      } else {
        diag.textContent =
          'Signed in as: ' + (accessInfo.user_id || '?') + '\n' +
          'This account does not have user_profiles.is_admin = 1. ' +
          'Ask an admin to promote it via Settings → User Management.';
      }
    }
    return;
  }
  if (overlay) overlay.style.display = 'none';
  if (editor) editor.style.display = 'flex';

  // Load tree (always refresh on tab activation so the user sees current state)
  await loadRoot();

  // Restore previously open tabs only once per session
  if (!openTabs.length) await restoreOpenTabs();

  // If a terminal tab is active, refit xterm — it can't measure a
  // display:none host while another main tab was active.
  const activeTermTab = getActiveTerminalTab();
  if (activeTermTab && activeTermTab.instance) {
    setTimeout(() => activeTermTab.instance.fit(), 30);
  }

  // Resume background polling for whichever view is currently active.
  // The view persists across top-level tab switches; we just need to
  // restart its loop now that Admin Tools is on screen again.
  const sb = document.getElementById('files-sidebar');
  const view = sb?.dataset.view;
  if (view === 'settings')          try { startAppConfig(); } catch (_) {}
  else if (view === 'database')     try { startAutoRefresh(); } catch (_) {}
  else if (view === 'interactions') try { startLoop(); } catch (_) {}
  else if (view === 'runtime-loop') try { startLoopVisual(); } catch (_) {}
}

export function stopAdminTools() {
  // Quiet any background loops; the view stays selected so polling
  // resumes when the user returns.
  try { stopAppConfig(); } catch (_) {}
  try { stopAutoRefresh(); } catch (_) {}
  try { stopLoop(); } catch (_) {}
  try { stopLoopVisual(); } catch (_) {}
}
