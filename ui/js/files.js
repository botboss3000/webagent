'use strict';

// VS Code-style file base editor.
//
// State: open tabs live in `openTabs` (ordered, draggable) and the active
// tab is `activeTabPath`. The directory tree is rendered lazily — each
// folder fetches its children on first expand.

import { openGitPanel } from './files-git.js';
import { app } from './state.js';

const API_BASE = '/api/v1/files';
const LS_SIDEBAR_VIEW = 'files.sidebarView';   // 'explorer' | 'git'
const LS_TERMINAL_ON  = 'files.terminalOn';    // '1' if terminal pane is open

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
  const editor = document.getElementById('files-editor');
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

    // ── 3-dot "more" menu button ──
    const more = document.createElement('button');
    more.className = 'files-tab-more';
    more.type = 'button';
    more.title = 'More actions';
    more.draggable = false;
    const moreI = document.createElement('i');
    moreI.setAttribute('data-lucide', 'more-vertical');
    moreI.className = 'lucide-icon';
    more.appendChild(moreI);
    // Same draggable-parent guard as the close button.
    more.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0) {
        e.preventDefault();
        showTabMenu(tab, more);
      }
    });
    more.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });
    more.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(more);

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
    if (activeTabPath === entry.path) activeTabPath = finalPath;
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
    if (activeTabPath === path) activeTabPath = newAbs;
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

function updateTabCarousel() {
  const bar = document.getElementById('files-tabs');
  const prev = document.getElementById('files-tabs-prev');
  const next = document.getElementById('files-tabs-next');
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

function initTabCarousel() {
  const bar = document.getElementById('files-tabs');
  const prev = document.getElementById('files-tabs-prev');
  const next = document.getElementById('files-tabs-next');
  if (!bar || !prev || !next) return;

  const SCROLL_STEP = 160;
  prev.addEventListener('click', () => { bar.scrollBy({ left: -SCROLL_STEP, behavior: 'smooth' }); });
  next.addEventListener('click', () => { bar.scrollBy({ left:  SCROLL_STEP, behavior: 'smooth' }); });
  bar.addEventListener('scroll', updateTabCarousel, { passive: true });

  // Auto-scroll while dragging a tab past either edge
  ['files-tabs-prev', 'files-tabs-next'].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.addEventListener('dragover', (e) => {
      e.preventDefault();
      const dir = id === 'files-tabs-prev' ? -1 : 1;
      bar.scrollBy({ left: dir * 40, behavior: 'auto' });
    });
  });

  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(updateTabCarousel).observe(bar);
  } else {
    window.addEventListener('resize', updateTabCarousel);
  }
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
    const wantMode = paneModeForTab(tab);
    if (pane && pane.dataset.mode !== wantMode) {
      // The right kind of pane no longer matches the tab (e.g. preview
      // toggled, or refresh changed binary-ness) — drop and rebuild.
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
      content.appendChild(pane);
    }
    pane.classList.toggle('active', tab.path === activeTabPath);
  }
  const active = openTabs.find((t) => t.path === activeTabPath);
  updateStatusBar(active || null);
}

// ── Per-tab pane mode ─────────────────────────────────────────────

function paneModeForTab(tab) {
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
  // Bring the activated tab into view if it's outside the visible window
  const tabEl = document.querySelector('#files-tabs .files-tab[data-path="' + cssEscape(path) + '"]');
  if (tabEl && typeof tabEl.scrollIntoView === 'function') {
    tabEl.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
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
    const minimal = openTabs.map((t) => ({ path: t.path, name: t.name, wrap: !!t.wrap, preview: !!t.preview }));
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
  initTabCarousel();
  installFilesDropGuard();
  initSidebarViewSwitcher();
  initSidebarMaximize();
  initFilesTerminal();
  renderTabs();
  renderEditorPanes();
}

// ── In-page terminal toggle ────────────────────────────────────────
//
// The terminal lives in #files-terminal-wrap, which sits next to .files-main
// inside the .files-editor. Toggling on hides the editor and reveals the
// terminal pane; toggling off restores the editor. The #terminal-container
// element inside the wrap is driven by terminal.js (xterm + WebSocket) and
// is initialised once at app startup, independent of this toggle.

function setFilesTerminalOn(on) {
  const editor = document.getElementById('files-editor');
  const wrap = document.getElementById('files-terminal-wrap');
  if (!editor || !wrap) return;
  editor.classList.toggle('terminal-on', !!on);
  wrap.hidden = !on;

  // Update every toggle button (sidebar headers + strip) so they reflect
  // the new state regardless of which one was clicked.
  document.querySelectorAll('.files-terminal-toggle').forEach((btn) => {
    btn.classList.toggle('active', !!on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.title = on ? 'Hide terminal' : 'Toggle terminal in main panel';
  });

  try { localStorage.setItem(LS_TERMINAL_ON, on ? '1' : '0'); } catch (_) {}

  if (on) {
    // On mobile, the sidebar may be filling the screen (state=max). Switch
    // to the strip so the main panel — and the terminal we just opened —
    // becomes visible.
    if (isMobileLayout()) {
      const sidebar = document.getElementById('files-sidebar');
      if (sidebar && sidebar.dataset.state === 'max') setSidebarState('strip');
    }
    // xterm needs a refit whenever its container's size changes (which
    // happens both when the wrap becomes visible and when the sidebar
    // resize toggles). Defer a tick so layout has settled.
    fitFilesTerminal();
  }
}

function fitFilesTerminal() {
  setTimeout(() => {
    try {
      if (app && app.fitAddon && app.term) {
        app.fitAddon.fit();
        // Re-send the new geometry to the backend so the PTY agrees.
        if (app.termWs && app.termWs.readyState === WebSocket.OPEN) {
          app.termWs.send(JSON.stringify({ type: 'resize', rows: app.term.rows, cols: app.term.cols }));
        }
      }
    } catch (_) {}
  }, 60);
}

function initFilesTerminal() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;

  // Restore last state (default: off)
  const wantOn = localStorage.getItem(LS_TERMINAL_ON) === '1';
  setFilesTerminalOn(wantOn);

  // Delegate clicks at the sidebar — covers the buttons in both panel
  // headers plus the one in the strip without re-binding when the view
  // switches.
  sidebar.addEventListener('click', (e) => {
    const btn = e.target.closest('.files-terminal-toggle');
    if (!btn || !sidebar.contains(btn)) return;
    e.stopPropagation();
    const editor = document.getElementById('files-editor');
    const isOn = !!(editor && editor.classList.contains('terminal-on'));
    setFilesTerminalOn(!isOn);
  });

  // Keep xterm's columns in sync when the user drags the sidebar resize
  // handle (only relevant while the terminal is visible).
  const handle = document.getElementById('files-resize-handle');
  if (handle) {
    handle.addEventListener('mouseup', () => {
      const editor = document.getElementById('files-editor');
      if (editor && editor.classList.contains('terminal-on')) fitFilesTerminal();
    });
  }
}

// ── Sidebar state cycle: split → max → strip → split ───────────────
//
// Desktop has all three states. Mobile skips 'split' (no usable split
// view on small screens) and cycles strip ↔ max only.

const LS_SIDEBAR_STATE = 'files.sidebarState';   // 'split' | 'max' | 'strip'

function isMobileLayout() {
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

  // Delegate clicks on the cycle button (in panel headers AND the strip),
  // the strip's refresh button, and the strip's view-switch buttons.
  sidebar.addEventListener('click', (e) => {
    const cycle = e.target.closest('.files-maximize-btn');
    if (cycle && sidebar.contains(cycle)) {
      e.stopPropagation();
      cycleSidebarState();
      return;
    }
    const stripRefresh = e.target.closest('.files-strip-refresh');
    if (stripRefresh && sidebar.contains(stripRefresh)) {
      e.stopPropagation();
      const view = sidebar.dataset.view || 'explorer';
      if (view === 'git') {
        import('./files-git.js').then(m => m.refreshGit(sidebar)).catch(() => {});
      } else {
        const r = document.getElementById('files-refresh');
        if (r) r.click();
      }
      return;
    }
    const stripView = e.target.closest('.files-strip-view');
    if (stripView && sidebar.contains(stripView)) {
      e.stopPropagation();
      const v = stripView.dataset.view;
      if (!v) return;
      // Switching view from the strip also expands the sidebar so the
      // chosen view is actually visible.
      applySidebarView(v);
      try { localStorage.setItem(LS_SIDEBAR_VIEW, v); } catch (_) {}
      setSidebarState(isMobileLayout() ? 'max' : 'split');
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
    // 3-stage: split → max → strip → split → …
    next = (cur === 'split') ? 'max'
         : (cur === 'max')   ? 'strip'
         : 'split';
  }
  setSidebarState(next);
}

function setSidebarState(state) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  if (state !== 'split' && state !== 'max' && state !== 'strip') state = 'split';
  if (isMobileLayout() && state === 'split') state = 'strip';
  sidebar.dataset.state = state;
  sidebar.classList.toggle('maximized', state === 'max');
  sidebar.classList.toggle('strip',     state === 'strip');

  // Update the cycle button's icon + title in every spot it appears
  // (panel headers AND the strip). The icon hints at the NEXT action,
  // not the current state.
  const iconName = state === 'split' ? 'maximize-2'
                 : state === 'max'   ? 'chevrons-left'
                 :                     'panel-left-open';
  const title    = state === 'split' ? 'Maximize sidebar'
                 : state === 'max'   ? 'Collapse sidebar'
                 :                     'Expand sidebar';
  sidebar.querySelectorAll('.files-maximize-btn').forEach((b) => {
    b.title = title;
    b.innerHTML = '<i data-lucide="' + iconName + '" class="lucide-icon"></i>';
  });

  // Strip is visible only when state=strip; panels follow the current
  // view (via applySidebarView) but stay hidden when in strip mode.
  const strip = sidebar.querySelector('.files-sidebar-strip');
  if (strip) strip.hidden = (state !== 'strip');
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
  const want = localStorage.getItem(LS_SIDEBAR_VIEW) === 'git' ? 'git' : 'explorer';
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

function applySidebarView(view) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  sidebar.dataset.view = view;
  // Update aria-selected on every view-toggle button (in panel headers
  // and in the strip).
  sidebar.querySelectorAll('.files-view-toggle-btn, .files-strip-view').forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
    if (b.classList.contains('files-view-toggle-btn')) {
      if (active) {
        b.setAttribute('aria-disabled', 'true');
        b.title = (view === 'git' ? 'Source control' : 'Explorer') + ' (current view)';
      } else {
        b.removeAttribute('aria-disabled');
        b.title = 'Switch to ' + (b.dataset.view === 'git' ? 'source control' : 'explorer');
      }
    }
  });
  // In strip mode both panels stay hidden; otherwise the matching panel
  // shows.
  const state = sidebar.dataset.state || 'split';
  sidebar.querySelectorAll('.files-sidebar-panel').forEach((p) => {
    p.hidden = (state === 'strip') || (p.dataset.view !== view);
  });
  if (view === 'git' && state !== 'strip') {
    // Lazy-load the git panel the first time, refresh on subsequent shows.
    openGitPanel(sidebar);
  }
}

export async function startFiles() {
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
  const editor = document.getElementById('files-editor');
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
          diag.textContent = 'Not signed in. Sign in as an admin user to access the file editor.';
        }
      } else {
        diag.textContent =
          'Signed in as: ' + (accessInfo.user_id || '?') + '\n' +
          'This account does not have user_profiles.is_admin = 1. ' +
          'Ask an admin to promote it via App Config → User Management.';
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

  // If the embedded terminal is currently visible, refit xterm — the page
  // was hidden until just now and xterm can't measure a display:none host.
  if (editor && editor.classList.contains('terminal-on')) fitFilesTerminal();
}

export function stopFiles() {
  // No teardown needed — tabs and state are kept so reopening the page is instant.
}
