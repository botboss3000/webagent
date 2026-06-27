'use strict';

// VS Code-style file base editor.
//
// State: open tabs live in `openTabs` (ordered, draggable). File and
// terminal tabs share the array but track separate "active" pointers
// (`activeFilePath` / `activeTerminalId`) — they render into separate
// main panels (explorer vs terminal). The directory tree is rendered
// lazily — each folder fetches its children on first expand.

import { _refreshLucideIcons } from './dom-utils.js';
import { copyText } from './clipboard.js';
import { openGitPanel, renderGitMain, restartServerAndReload, startGitAutoRefresh, stopGitAutoRefresh, streamCopy, streamPush, _RELEASE_PHASE_LABELS } from './files-git.js';
import { createTerminalInstance } from './terminal.js';
import { randomUUID } from './uuid.js';
import { startAutoRefresh, stopAutoRefresh } from './pagination.js';
import { startDbViewer } from './index.js';
import { isMobileLayout } from './layout.js';
import { app } from './state.js';
import { startLoop, stopLoop, renderInteractionsSidebar } from '../../main-panel/agents/agent-loop/js/loop.js';
import { startLoopVisual, stopLoopVisual, renderRuntimeLoopSidebar } from '../../main-panel/agents/agent-loop/js/loop-logic.js';
import { startDiagnostics, stopDiagnostics, renderDiagnosticsSidebar } from './diagnostics.js';
import { showRestrictedModal, authHeaders } from './left-login.js';

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
// Production mirror: folders marked dev-only are stripped from the trimmed copy
// published to the separate production repo. The list is owned by the backend
// (shared with the Git page) and mirrored here as absolute paths so tree rows
// can show a "Dev" badge; the production-preview toggle reveals the per-row
// checkboxes. The toolbar "More" button (#files-more-btn) opens a popover with
// that toggle, the production repo (GitHub remote) + GitHub key + folder fields,
// a live "in sync / N changes" line, and the two release halves — "Sync to
// production" (one-way sync the trimmed tree into the sister repo + commit
// locally) and "Push to GitHub" — the same engine the Git page's Release runs.
// The key is a secret: it's masked, never echoed back, and saved to the
// encrypted vault; the repo + folder save to the local production config.
let prodExcluded = new Set();  // absolute paths of dev-only folders
let prodViewMode = 'dev';      // 'dev' | 'prod' (production preview hides them)

// Git line-change badges: every tree row shows how many lines it gained (+N
// green) / lost (-N red) since the last commit. Data comes from
// GET /api/v1/github/line-stats (keyed by repo-relative path) and is mirrored
// here as absolute paths so a row can look itself up. A FILE shows its own
// count; a FOLDER shows the SUM of every changed file beneath it — but only
// while collapsed: an expanded folder defers to its now-visible children (CSS
// hides the open folder's own badge) so the counts never double up.
let gitLineStats = {};   // absolute file path -> { added, removed } since HEAD
let gitFolderStats = {}; // absolute folder path -> summed { added, removed } of its descendants
let gitStatsRoot = '';   // repo root the stats are keyed under (aggregation boundary)
let _prodActionBusy = false;   // guards against a double-fire while syncing/pushing
let _prodFolder = '';          // cached destination folder (editable in the More menu)
let _prodFolderDefault = '';   // backend's suggested default (placeholder when unset)
let _prodRemote = '';          // cached production GitHub remote (editable in the More menu)
let _prodRemoteDefault = '';   // backend's stored remote (for blank-field repopulation)
let _prodToken = '';           // GitHub key typed this session (secret — sent as a backstop, never displayed)
let _prodTokenSet = false;     // whether a key is stored in the vault (drives the field's placeholder)

// Persisted state (across tab switches and reloads)
const LS_SIDEBAR_WIDTH    = 'files.sidebarWidth';
const LS_SIDEBAR_COLLAPSED = 'files.sidebarCollapsed';
const LS_OPEN_TABS         = 'files.openTabs';
const LS_ACTIVE_TAB        = 'files.activeTab';        // legacy unified key (still read for migration)
const LS_ACTIVE_FILE       = 'files.activeFile';
const LS_ACTIVE_TERMINAL   = 'files.activeTerminal';
const LS_EXPANDED          = 'files.expandedDirs';
const LS_CURRENT_ROOT      = 'files.currentRoot';
const LS_PROD_VIEW         = 'files.prodView';        // 'dev' | 'prod' explorer view

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

function withUserIdParam(path) {
  // Append the active user_id as a query param. The backend prefers the
  // JWT when valid, but falls back to this — same pattern as the other
  // admin pages — so the page still works if the cached token is stale.
  const uid = localStorage.getItem('auth_user_id') || '';
  if (!uid) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'user_id=' + encodeURIComponent(uid);
}

// Upload a pasted/dropped image blob to the server and return the saved file's
// absolute path. The caller types that path into the terminal so Claude Code
// (or any program that reads an image path) can pick the image up — the web
// terminal is a text-only pipe, so an image can't be sent through it directly.
// Used by both the keybar Paste chip and the drag-an-image-onto-the-terminal
// drop handler. Throws on a non-2xx response so the caller can surface why.
async function uploadTerminalPasteImage(blob, mimeType) {
  const sub = (mimeType && mimeType.split('/')[1]) || 'png';
  const ext = '.' + sub.split('+')[0];   // image/svg+xml → .svg
  const form = new FormData();
  form.append('file', blob, 'clipboard' + ext);
  // Raw fetch (not apiFetch): FormData must set its own multipart boundary, so
  // we can't let apiFetch force a JSON Content-Type. Auth + user_id match the
  // rest of the terminal API.
  const res = await fetch(
    withUserIdParam('/api/v1/terminal/paste-image'),
    { method: 'POST', headers: authHeaders(), body: form },
  );
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
    throw new Error(detail);
  }
  const data = await res.json();
  return (data && data.path) ? data.path : '';
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  // Avoid a CORS preflight on GETs by only setting Content-Type when
  // we're actually sending a body.
  if (opts.body && !('Content-Type' in headers)) {
    headers['Content-Type'] = 'application/json';
  }
  // Paths that already start with /api/ are absolute — used by the terminal
  // launcher panel to hit /api/v1/terminal/... endpoints. Everything else is
  // a files-relative subpath ('/tree?...', '/write', etc.) and gets the
  // /api/v1/files prefix.
  const url = path.startsWith('/api/') ? withUserIdParam(path) : (API_BASE + withUserIdParam(path));
  const res = await fetch(url, Object.assign({}, opts, { headers }));
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

// ── Production mirror (dev-only folder marking) ───────────────────
// The exclude list lives in the backend (data/config/production-mirror.json)
// and is shared with the Git page's Production panel. We fetch it once on open
// and POST a single path on toggle, so both surfaces stay in sync.

async function loadProdExcludes() {
  try {
    const data = await apiFetch('/api/v1/github/production/exclude');
    const root = String(data.project_root || projectRoot || '').replace(/\/+$/, '');
    const abs = (data.exclude_paths || []).map((rel) => (root ? root + '/' + rel : rel));
    prodExcluded = new Set(abs);
  } catch (_) {
    // Non-fatal — without the list we simply render no badges.
  }
}

// Fetch the production destination folder (shown + editable in the More menu's
// folder field). The backend fills in its sibling default (…-dev → …-prod) when
// nothing is stored, so prod_folder is always populated. Cached so the menu can
// build synchronously; refreshed when the menu opens.
async function loadProdConfig() {
  try {
    const cfg = await apiFetch('/api/v1/github/production/config');
    _prodFolder = String(cfg.prod_folder || '');
    _prodFolderDefault = _prodFolder;
    _prodRemote = String(cfg.prod_remote_url || '');
    _prodRemoteDefault = _prodRemote;
    _prodTokenSet = !!cfg.token_set;
  } catch (_) {
    // Non-fatal — the field just starts blank with its placeholder.
  }
}

// Persist the production folder to the shared config (called on field blur/Enter).
async function saveProdFolder(value) {
  const folder = String(value || '').trim();
  _prodFolder = folder;              // keep the cache current immediately
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prod_folder: folder }),
    });
  } catch (_) {
    // Non-fatal — sync/push also send the folder in their payload as a backstop.
  }
}

// Persist the production GitHub remote to the shared config (field blur/Enter).
// Accepts a friendly owner/repo shorthand; the backend expands it to a full URL.
async function saveProdRemote(value) {
  const remote = String(value || '').trim();
  _prodRemote = remote;              // keep the cache current immediately
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prod_remote_url: remote }),
    });
  } catch (_) {
    // Non-fatal — sync/push also send the remote in their payload as a backstop.
  }
}

// Persist the production GitHub **key** to the encrypted vault (field blur/Enter).
// A blank value is ignored so an untouched save never wipes the stored key; the
// key is never read back from the server, so the field is never pre-filled.
async function saveProdToken(value) {
  const token = String(value || '').trim();
  if (!token) return;                // blank = keep the stored key
  _prodToken = token;                // cache for the action backstop
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ github_token: token }),
    });
    _prodTokenSet = true;            // a key is now stored
  } catch (_) {
    // Non-fatal — sync/push also send the key in their payload as a backstop.
  }
}

// Placeholder for the GitHub key field — reflects whether a key is already
// stored (without ever revealing it).
function _prodTokenPlaceholder() {
  return _prodTokenSet ? '•••••••• saved — blank keeps it' : 'ghp_… or fine-grained token';
}

// Friendly display of a GitHub remote: a standard https://github.com/owner/repo(.git)
// collapses to "owner/repo"; anything else (SSH, self-hosted) shows in full.
function _shortRemote(url) {
  const u = String(url || '').trim();
  const m = u.match(/^https?:\/\/github\.com\/(.+?)(?:\.git)?\/?$/i);
  return m ? m[1] : u;
}

// This EXACT path was marked dev-only (folder OR file — both are supported).
function isProdExcluded(path) {
  return prodExcluded.has(path);
}

// The excluded ancestor folder of `path`, if any — a parent folder that is
// itself dev-only implicitly drops everything beneath it. Returns the ancestor
// path or '' . Used so a child shows unticked + locked under an excluded
// parent (you re-include it by re-including the parent, not the child).
function prodExcludedAncestor(path) {
  for (const ex of prodExcluded) {
    if (path !== ex && path.startsWith(ex + '/')) return ex;
  }
  return '';
}

// True when `path` actually travels to the production mirror: not excluded
// itself and not sitting under an excluded folder.
function prodWillShip(path) {
  return !isProdExcluded(path) && !prodExcludedAncestor(path);
}

// The excluded paths that live *inside* this folder (its dev-only descendants) —
// what makes a folder show a partial/indeterminate tick.
function prodExcludedDescendants(path) {
  const out = [];
  for (const ex of prodExcluded) {
    if (ex !== path && ex.startsWith(path + '/')) out.push(ex);
  }
  return out;
}

// Tri-state for a FOLDER checkbox:
//   'checked'       — the whole folder ships (nothing inside is dev-only)
//   'indeterminate' — the folder ships but SOME item inside is dev-only (partial)
//   'unchecked'     — the folder itself is dev-only, or it sits under an excluded
//                     parent (then it is also locked)
function prodFolderState(path) {
  if (isProdExcluded(path) || prodExcludedAncestor(path)) return 'unchecked';
  return prodExcludedDescendants(path).length ? 'indeterminate' : 'checked';
}

function prodCheckTitle(ships, underExcluded) {
  if (underExcluded) return 'Excluded — a parent folder is marked dev-only';
  return ships ? 'Ships to production — uncheck to keep it dev-only'
               : 'Dev-only — check to include it in production';
}

function prodFolderTitle(state, underExcluded) {
  if (underExcluded) return 'Excluded — a parent folder is marked dev-only';
  if (state === 'indeterminate')
    return 'Some items inside are dev-only — click to include everything, click again to exclude everything';
  if (state === 'checked') return 'Everything here ships — click to keep the whole folder dev-only';
  return 'Folder is dev-only — click to include it in production';
}

// Persist one path's include/exclude state, then repaint the marks in place (no
// tree reload, so the admin can rattle through many checkboxes smoothly). The
// exclude list lives in the backend and is shared with the Git page.
async function setProdExclude(path, excluded) {
  try {
    await apiFetch('/api/v1/github/production/exclude', {
      method: 'POST',
      body: JSON.stringify({ path, excluded }),
    });
    if (excluded) prodExcluded.add(path); else prodExcluded.delete(path);
    refreshProdMarks();
  } catch (e) {
    alert('Could not update production exclude: ' + (e.message || 'failed'));
  }
}

// Apply several exclude changes in one round-trip (used when a folder checkbox
// includes / excludes its whole subtree). add/remove are absolute paths; we keep
// the local set authoritative from the same lists so no rel→abs remap is needed.
async function setProdExcludeBulk(addPaths, removePaths) {
  const add = addPaths || [];
  const remove = removePaths || [];
  if (!add.length && !remove.length) return;
  try {
    await apiFetch('/api/v1/github/production/exclude-bulk', {
      method: 'POST',
      body: JSON.stringify({ add, remove }),
    });
    remove.forEach((p) => prodExcluded.delete(p));
    add.forEach((p) => prodExcluded.add(p));
    refreshProdMarks();
  } catch (e) {
    alert('Could not update production exclude: ' + (e.message || 'failed'));
  }
}

// Include a folder's WHOLE subtree: clear the folder's own mark plus every
// dev-only descendant, so everything inside ships again.
function prodIncludeSubtree(path) {
  setProdExcludeBulk([], [path, ...prodExcludedDescendants(path)]);
}

// Exclude a folder's WHOLE subtree: mark the folder dev-only (which covers
// everything beneath it) and drop the now-redundant per-descendant marks.
function prodExcludeSubtree(path) {
  setProdExcludeBulk([path], prodExcludedDescendants(path));
}

// Right-click entry point — flip dev-only state. For a folder this mirrors the
// checkbox's first-click rule: a fully-shipping folder is excluded wholesale,
// anything else (dev-only or partially-excluded) is included wholesale.
function toggleProdExclude(entry) {
  if (entry.is_dir) {
    if (prodFolderState(entry.path) === 'checked') prodExcludeSubtree(entry.path);
    else prodIncludeSubtree(entry.path);
  } else {
    setProdExclude(entry.path, !isProdExcluded(entry.path));
  }
}

// Walk every rendered row and resync its checkbox, "Dev" badge and dropped
// styling to the current exclude set — called after any single toggle so the
// whole visible subtree reflects the change without a server round-trip.
function refreshProdMarks() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-node').forEach((node) => {
    const path = node.dataset.path;
    if (!path) return;
    const isDir = node.dataset.kind === 'dir';
    const ships = prodWillShip(path);
    const underExcluded = !!prodExcludedAncestor(path);
    node.classList.toggle('is-prod-dropped', !ships);
    const row = node.querySelector(':scope > .files-tree-row');
    if (!row) return;
    const check = row.querySelector('.files-tree-check');
    if (check) {
      if (isDir) {
        // Folders carry a tri-state: a partial tick when some items inside are
        // dev-only. The indeterminate flag is visual-only and must be re-set here.
        const st = prodFolderState(path);
        check.checked = st === 'checked';
        check.indeterminate = st === 'indeterminate';
        check.disabled = underExcluded;
        check.title = prodFolderTitle(st, underExcluded);
      } else {
        check.checked = ships;
        check.indeterminate = false;
        check.disabled = underExcluded;
        check.title = prodCheckTitle(ships, underExcluded);
      }
    }
    const badge = row.querySelector('.files-tree-badge.dev-only');
    if (badge) badge.classList.toggle('show', isProdExcluded(path));
  });
}

// Reflect the dev/production view mode onto the tree container. In production-
// preview mode the per-row checkboxes appear so the admin can tick exactly what
// ships; dev view hides them again. The eye lives in the "More" popover now, so
// there's no toolbar button to restyle here — just the tree + the status line.
function applyProdViewClass() {
  const tree = document.getElementById('files-tree');
  if (tree) tree.classList.toggle('prod-view', prodViewMode === 'prod');
  // Leaving production-preview clears any lingering copy/push status line.
  const status = document.getElementById('files-prod-status');
  if (status && prodViewMode !== 'prod') status.classList.remove('show');
}

// Flip production-preview on/off (the eye / "Show dev checkboxes" menu item).
function toggleProdView() {
  prodViewMode = prodViewMode === 'prod' ? 'dev' : 'prod';
  try { localStorage.setItem(LS_PROD_VIEW, prodViewMode); } catch (_) {}
  applyProdViewClass();
}

// Fetch how dev and the production folder differ and write a one-line summary
// into the More menu's diff row (#files-prod-diff). Read-only; called when the
// menu opens. The backend walks + byte-compares the shipping set, so this is a
// touch slower than the other reads — the row shows "Checking…" until it lands.
async function loadProdDiff() {
  const set = (txt) => {
    const el = document.getElementById('files-prod-diff');
    if (el) el.textContent = txt;
  };
  try {
    const d = await apiFetch('/api/v1/github/production/diff');
    if (!d) { set('Could not compare with production.'); return; }
    if (d.configured === false) { set('Set a production repo to compare.'); return; }
    if (d.first_sync) { set(`Not synced yet — ${d.shipping_count} files would be sent.`); return; }
    if (d.in_sync) { set('In sync — production matches dev.'); return; }
    const parts = [];
    if (d.added)   parts.push(`${d.added} to add`);
    if (d.updated) parts.push(`${d.updated} to update`);
    if (d.removed) parts.push(`${d.removed} to remove`);
    set(parts.length ? `Differs: ${parts.join(', ')}.` : 'Differences found.');
  } catch (_) {
    set('Could not compare with production.');
  }
}

// Write the header status line under the toolbar (tones: info/success/error).
function setProdStatus(text, tone) {
  const el = document.getElementById('files-prod-status');
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone || 'info';
  el.classList.add('show');
}

// Run one production action (Copy or Push) as a live NDJSON stream, driving the
// header status line with phase labels then the terminal result. Both halves of a
// release share this so they can't drift in confirm/progress/result handling.
//   streamFn   — streamCopy or streamPush (from files-git.js)
//   confirmMsg — confirmation prompt (these are outward-facing / hard to undo)
//   okStatuses — backend terminal statuses treated as success (green)
async function _runProdAction(streamFn, confirmMsg, okStatuses) {
  if (_prodActionBusy) return;
  if (confirmMsg && !confirm(confirmMsg)) return;

  _prodActionBusy = true;
  setProdStatus('Starting…', 'info');

  let result = null;
  try {
    // Send the current destination folder so the backend uses the freshest value
    // even if the field's auto-save is still in flight. The backend also auto-
    // generates the commit message. The stream helper adds {stream:true}.
    await streamFn({ prod_folder: _prodFolder || undefined, prod_remote_url: _prodRemote || undefined, github_token: _prodToken || undefined }, (ev) => {
      if (ev.phase === 'done') { result = ev.result || {}; return; }
      // The first copy clones the existing repo (~20s) — say so instead of a
      // generic "Building…" label that looks frozen.
      if (ev.phase === 'copying' && ev.first_setup) {
        setProdStatus('Setting up the production repo (first run — cloning, ~20s)…', 'info');
        return;
      }
      if (ev.phase === 'message_ready') {
        // Show the AI-written title (mirrors the ⭐ commit&push step display).
        setProdStatus(ev.title ? `Message: "${ev.title}"` : 'Message ready', 'info');
        return;
      }
      const label = _RELEASE_PHASE_LABELS[ev.phase];
      if (label) setProdStatus(label, 'info');
    });
  } catch (e) {
    result = { status: 'error', message: (e && e.message) || 'action failed' };
  }

  const r = result || { status: 'error', message: 'no result' };
  const ok = okStatuses.includes(r.status);
  const tone = ok ? 'success'
    : r.status === 'nothing' ? 'info'
    : (r.status === 'blocked' || r.status === 'error') ? 'error' : 'info';
  setProdStatus(r.message || r.status || 'Done', tone);

  _prodActionBusy = false;
  // Auto-clear a successful / neutral result; leave errors up so they can be read.
  if (tone !== 'error') {
    const el = document.getElementById('files-prod-status');
    setTimeout(() => {
      if (el && el.dataset.tone !== 'error') el.classList.remove('show');
    }, 9000);
  }
}

// "Sync to production": one-way sync the shipping set (every file NOT marked
// dev-only) from dev into the sister production folder — adds new/changed files
// and drops files no longer shipped — then commit there LOCALLY (no push). Lands
// the files so they can be reviewed before publishing. The backend's secret-scan
// still guards the commit.
function syncToProduction() {
  return _runProdAction(
    streamCopy,
    'Sync to production?\n\nThis one-way syncs the shipping set (every file NOT marked dev-only) from dev into the production folder — adding changed files and removing files no longer shipped — then commits there locally. Nothing is pushed to GitHub yet.',
    ['copied'],
  );
}

// "Push to GitHub": send whatever Copy last committed in the production folder up
// to the production GitHub remote.
function pushToProduction() {
  return _runProdAction(
    streamPush,
    "Push to GitHub?\n\nThis pushes the production folder's latest commit to the production GitHub remote.",
    ['pushed'],
  );
}

// The toolbar "More" popover: the production-preview toggle, the production repo
// (GitHub remote) + GitHub key + folder fields, a live in-sync/diff line, and the
// two release halves. Reuses the shared floating-menu builder so it matches the
// tab/context menus; the eye row shows a ✓ while production-preview is on.
function openProductionMenu(anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const previewing = prodViewMode === 'prod';
  const items = [
    { icon: previewing ? 'eye-off' : 'eye', label: 'Show dev checkboxes',
      checked: previewing, action: toggleProdView },
    { separator: true },
    // Production GitHub remote (owner/repo shorthand accepted). Persists on blur;
    // Sync/Push also send it as a backstop.
    { field: true, label: 'Production repo', value: _shortRemote(_prodRemote),
      placeholder: 'owner/repo', fieldKey: 'remote',
      onInput: (v) => { _prodRemote = v.trim(); },
      onSave: (v) => saveProdRemote(v) },
    // GitHub key (secret) → encrypted vault. Masked, NEVER pre-filled/echoed;
    // blank keeps the stored key. Sync/Push also send it as a backstop.
    { field: true, label: 'GitHub key', value: '', fieldType: 'password', fieldKey: 'token',
      placeholder: _prodTokenPlaceholder(),
      onInput: (v) => { _prodToken = v.trim(); },
      onSave: (v) => saveProdToken(v) },
    // Destination folder field. Updates the cache per keystroke and persists on
    // blur/Enter; Sync/Push also send it as a backstop.
    { field: true, label: 'Production folder', value: _prodFolder,
      placeholder: _prodFolderDefault || '…/your-repo-prod', fieldKey: 'folder',
      onInput: (v) => { _prodFolder = v.trim(); },
      onSave: (v) => saveProdFolder(v) },
    { separator: true },
    // Live diff line (filled in by loadProdDiff once the menu is up).
    { info: true, id: 'files-prod-diff', label: 'Checking for changes…' },
    { icon: 'refresh-cw',   label: 'Sync to production', disabled: _prodActionBusy, action: syncToProduction },
    { icon: 'upload-cloud', label: 'Push to GitHub',     disabled: _prodActionBusy, action: pushToProduction },
  ];
  _openFloatingMenu(items, rect.bottom + 2, rect.right - 240);
  loadProdDiff();
  // Refresh the folder + remote from the backend in case they changed elsewhere;
  // update an untouched, still-blank field in place if the value arrives while the
  // menu is up. Fields are matched by data-field (not index) so inserting the key
  // field can't misalign them; the key field is never populated (it's a secret).
  loadProdConfig().then(() => {
    const menu = document.getElementById('files-floating-menu');
    if (!menu) return;
    const remoteInp = menu.querySelector('input[data-field="remote"]');
    const folderInp = menu.querySelector('input[data-field="folder"]');
    const tokenInp  = menu.querySelector('input[data-field="token"]');
    if (remoteInp && document.activeElement !== remoteInp && !remoteInp.value) remoteInp.value = _shortRemote(_prodRemote);
    if (folderInp && document.activeElement !== folderInp && !folderInp.value) folderInp.value = _prodFolder;
    if (tokenInp  && document.activeElement !== tokenInp  && !tokenInp.value)  tokenInp.placeholder = _prodTokenPlaceholder();
  });
}

// ── Git line-change badges (+N / -N since last commit) ────────────
// Fetch the per-file added/removed map from the backend (one call, whole repo),
// remap it to absolute paths, roll the files up into folder sums, then repaint
// the badges on every rendered row. Non-fatal throughout: no data → no badges.

async function loadLineStats() {
  try {
    const data = await apiFetch('/api/v1/github/line-stats');
    const root = String(data.project_root || projectRoot || '').replace(/\/+$/, '');
    gitStatsRoot = root;
    const raw = data.stats || {};
    const map = {};
    for (const rel in raw) {
      const abs = root ? root + '/' + rel : rel;
      map[abs] = { added: raw[rel].added || 0, removed: raw[rel].removed || 0 };
    }
    gitLineStats = map;
    rebuildFolderStats();
    refreshLineStatMarks();
  } catch (_) {
    // Non-fatal — without the stats we simply render no counts.
  }
}

// Sum each changed file's counts into every ancestor folder up to (and
// including) the repo root, so a collapsed folder can show its subtree total.
function rebuildFolderStats() {
  const folders = {};
  const boundary = gitStatsRoot || projectRoot || '';
  for (const abs in gitLineStats) {
    const st = gitLineStats[abs];
    let idx = abs.lastIndexOf('/');
    while (idx > 0) {
      const folder = abs.slice(0, idx);
      // Stop once we'd climb above the repo root (those folders aren't shown).
      if (boundary && (folder.length < boundary.length || !folder.startsWith(boundary))) break;
      const agg = folders[folder] || (folders[folder] = { added: 0, removed: 0 });
      agg.added += st.added;
      agg.removed += st.removed;
      idx = folder.lastIndexOf('/');
    }
  }
  gitFolderStats = folders;
}

// Paint one row's badge span from a { added, removed } record (or clear it when
// there's no change). The span is always present in the DOM so it can be
// updated in place after a refresh without rebuilding the row.
function applyLineStat(span, st) {
  if (!span) return;
  span.textContent = '';
  if (!st || (!st.added && !st.removed)) { span.classList.remove('show'); return; }
  if (st.added) {
    const a = document.createElement('span');
    a.className = 'files-tree-stat-add';
    a.textContent = '+' + st.added;
    span.appendChild(a);
  }
  if (st.removed) {
    const r = document.createElement('span');
    r.className = 'files-tree-stat-del';
    r.textContent = '-' + st.removed;
    span.appendChild(r);
  }
  const parts = [];
  if (st.added) parts.push(st.added + ' added');
  if (st.removed) parts.push(st.removed + ' removed');
  span.title = parts.join(', ') + ' since last commit';
  span.classList.add('show');
}

// The current stat record for a path: files use their own count, folders the
// rolled-up subtree sum.
function lineStatFor(path, isDir) {
  return isDir ? gitFolderStats[path] : gitLineStats[path];
}

// Resync every rendered row's badge to the current maps — called after a fresh
// fetch (the tree itself isn't rebuilt). Lazily-expanded children pick up their
// badge at render time straight from the maps, so this only needs to touch what
// is already on screen.
function refreshLineStatMarks() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-node').forEach((node) => {
    const path = node.dataset.path;
    if (!path) return;
    const row = node.querySelector(':scope > .files-tree-row');
    if (!row) return;
    const span = row.querySelector('.files-tree-stat');
    if (span) applyLineStat(span, lineStatFor(path, node.dataset.kind === 'dir'));
  });
}

// Keep the +/- badges live while the Explorer tree is open: re-fetch the counts
// on a timer so commits/edits made elsewhere (the agent, the terminal, another
// device) show up without a manual reload. Paused while the browser tab is
// hidden or the active sidebar view isn't the tree, and only one timer ever
// runs (start is idempotent). Mirrors the Source-Control auto-refresh.
const LINE_STATS_REFRESH_MS = 20000;   // 20s
let _lineStatsTimer = null;

function startLineStatsAutoRefresh() {
  if (_lineStatsTimer) return;          // already polling
  _lineStatsTimer = setInterval(() => {
    if (document.hidden) return;        // browser tab not visible → skip the round-trip
    if (!isAdmin) return;
    const sb = document.getElementById('files-sidebar');
    if (sb && sb.dataset.view !== 'explorer') return;  // only while the tree is the active view
    loadLineStats();                    // cheap re-fetch; repaints badges in place
  }, LINE_STATS_REFRESH_MS);
}

function stopLineStatsAutoRefresh() {
  if (_lineStatsTimer) { clearInterval(_lineStatsTimer); _lineStatsTimer = null; }
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

  // Production-preview checkbox (hidden unless the tree is in prod-view). Ticked
  // = this path ships to the production mirror; unticking marks it dev-only. A
  // path under an already-excluded folder shows unticked + locked. Shared
  // exclude list with the Git page's Production panel. Created here but appended
  // AFTER the chevron, so the row reads: chevron → checkbox → name.
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'files-tree-check';
  const underExcluded = !!prodExcludedAncestor(entry.path);
  if (entry.is_dir) {
    // Folder checkbox is tri-state: ticked (all ships), a partial dash (some
    // items inside are dev-only), or unticked (the whole folder is dev-only).
    const st = prodFolderState(entry.path);
    check.checked = st === 'checked';
    check.indeterminate = st === 'indeterminate';
    check.disabled = underExcluded;
    check.title = prodFolderTitle(st, underExcluded);
  } else {
    const ships = prodWillShip(entry.path);
    check.checked = ships;
    check.disabled = underExcluded;
    check.title = prodCheckTitle(ships, underExcluded);
  }
  // A checkbox click must not open the file / toggle the folder. (The checkbox
  // also re-enables pointer-events in CSS, since the row sets them off on every
  // child so the whole row is one click target.)
  check.addEventListener('click', (e) => e.stopPropagation());
  check.addEventListener('change', (e) => {
    e.stopPropagation();
    if (entry.is_dir) {
      // The browser has already resolved the tri-state on click: ticking a
      // partial (or empty) folder includes the whole subtree, unticking a full
      // one excludes it. So check.checked now carries the intent.
      if (check.checked) prodIncludeSubtree(entry.path);
      else prodExcludeSubtree(entry.path);
    } else {
      setProdExclude(entry.path, !check.checked);
    }
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
  row.appendChild(check);

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

  // Dev-only badge — shown (via the `show` class) when this exact path is
  // excluded from the production mirror, for files and folders alike. Always
  // present but hidden otherwise, so refreshProdMarks() can toggle it live
  // without rebuilding the row.
  const badge = document.createElement('span');
  badge.className = 'files-tree-badge dev-only' + (isProdExcluded(entry.path) ? ' show' : '');
  badge.textContent = 'Dev';
  badge.title = 'Excluded from production';
  row.appendChild(badge);

  // Git +N/-N change badge (filled now from the loaded maps; an expanded folder's
  // own badge is hidden by CSS so its visible children carry the counts instead).
  const stat = document.createElement('span');
  stat.className = 'files-tree-stat';
  applyLineStat(stat, lineStatFor(entry.path, entry.is_dir));
  row.appendChild(stat);
  return row;
}

function renderTreeNode(entry, depth) {
  const node = document.createElement('div');
  node.className = 'files-tree-node';
  node.dataset.path = entry.path;
  node.dataset.kind = entry.is_dir ? 'dir' : 'file';
  // Mark non-shipping nodes (excluded themselves or under an excluded folder)
  // so production-preview can dim + strike them — files and folders alike.
  if (!prodWillShip(entry.path)) node.classList.add('is-prod-dropped');

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
    _refreshLucideIcons(container);
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
    _refreshLucideIcons(tree);
    // Fire-and-forget: fetch the git +/- line counts and paint the badges once
    // they land (the tree is already visible — counts fill in a beat later).
    loadLineStats();
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
    if (tab.kind === 'terminal') {
      // Terminal tabs no longer show a glyph icon — only the connection-
      // status dot, rendered as a small standalone leading indicator (the
      // `.files-tab-icon-dotonly` modifier shrinks the wrap to the dot and
      // un-overlays it). State is driven by the xterm instance via
      // onStateChange (see buildPaneForTab); we render an initial state here
      // so the dot exists before the instance binds.
      iconWrap.classList.add('files-tab-icon-dotonly');
      const dot = document.createElement('span');
      dot.className = 'files-tab-conn-dot';
      const initialState = (tab.instance && tab.instance.getState && tab.instance.getState()) || 'connecting';
      dot.dataset.state = initialState;
      dot.title = _connStateTitle(initialState);
      iconWrap.appendChild(dot);
    } else {
      const iconI = document.createElement('i');
      iconI.setAttribute('data-lucide', fileIconName(tab.name));
      iconI.className = 'lucide-icon';
      iconWrap.appendChild(iconI);
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
  if (fileBar) _refreshLucideIcons(fileBar);
  if (termBar) _refreshLucideIcons(termBar);
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
    // A non-interactive info line (e.g. the production sync-diff summary).
    // Carries an optional id so callers can update its text asynchronously.
    if (item.info) {
      const row = document.createElement('div');
      row.className = 'files-tab-menu-info';
      if (item.id) row.id = item.id;
      row.textContent = item.label || '';
      menu.appendChild(row);
      continue;
    }
    // A text field row (e.g. the production folder path). Lives inside the menu
    // so clicks/keys don't dismiss it; onInput fires per keystroke (cheap cache
    // update), onSave fires on blur/Enter (persist).
    if (item.field) {
      const wrap = document.createElement('div');
      wrap.className = 'files-tab-menu-field';
      if (item.label) {
        const lab = document.createElement('label');
        lab.textContent = item.label;
        wrap.appendChild(lab);
      }
      const inp = document.createElement('input');
      inp.type = item.fieldType || 'text';   // 'password' masks a secret key
      inp.value = item.value || '';
      inp.placeholder = item.placeholder || '';
      inp.spellcheck = false;
      inp.autocomplete = item.fieldType === 'password' ? 'new-password' : 'off';
      if (item.fieldKey) inp.dataset.field = item.fieldKey;
      inp.addEventListener('click', (e) => e.stopPropagation());
      if (typeof item.onInput === 'function') {
        inp.addEventListener('input', () => item.onInput(inp.value));
      }
      if (typeof item.onSave === 'function') {
        inp.addEventListener('change', () => item.onSave(inp.value));
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); item.onSave(inp.value); inp.blur(); }
        });
      }
      wrap.appendChild(inp);
      menu.appendChild(wrap);
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

  _refreshLucideIcons(menu);

  // Dismiss handlers. These check the LIVE menu element (by id), not this call's
  // `menu` closure: a prior open's handler that never fired still sits on the
  // document, and if it tested its own (now-detached) menu it would wrongly close
  // a freshly-reopened menu the moment you click inside it — which broke typing in
  // the folder field on the 2nd+ open. Testing the live menu makes stale handlers
  // self-heal (they detach when no menu is present) and keeps clicks inside the
  // current menu (its buttons OR field) from dismissing it.
  const cleanup = () => {
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('contextmenu', outside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const outside = (ev) => {
    const cur = document.getElementById('files-floating-menu');
    if (!cur) { cleanup(); return; }            // stale handler — detach self
    if (!cur.contains(ev.target)) { closeFloatingMenu(); cleanup(); }
  };
  const onKey = (ev) => {
    if (!document.getElementById('files-floating-menu')) { cleanup(); return; }
    if (ev.key === 'Escape') { closeFloatingMenu(); cleanup(); }
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
      (prodFolderState(entry.path) === 'checked'
        ? { icon: 'package-x',     label: 'Exclude from production', action: () => toggleProdExclude(entry) }
        : prodFolderState(entry.path) === 'indeterminate'
          ? { icon: 'package-check', label: 'Include everything in production', action: () => toggleProdExclude(entry) }
          : { icon: 'package-check', label: 'Include in production', checked: true, action: () => toggleProdExclude(entry) }),
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
      isProdExcluded(entry.path)
        ? { icon: 'package-check', label: 'Include in production', checked: true, action: () => toggleProdExclude(entry) }
        : { icon: 'package-x',     label: 'Exclude from production', action: () => toggleProdExclude(entry) },
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
  // copyText handles insecure http://<ip> contexts (e.g. phones on the LAN)
  // via an execCommand fallback; prompt() is the last resort if even that fails.
  try {
    await copyText(path);
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

  // Always-visible "new terminal tab" button. Pinned outside the scrolling
  // carousel (#files-term-tabs) in terminal.html, so it never scrolls out of
  // view. Bind the handler to the button itself (the inner icon node can be
  // swapped by the central Lucide renderer — see ui-guidance Lucide rule 4).
  //
  // Tap  = open a blank terminal tab. Long-press (≥500ms) = start a new tmux
  // session with an auto-generated default name (no naming dialog), and flash
  // a small white confirmation popup. The long-press sets `lpFired` so the
  // click that follows the release is swallowed (no extra blank tab).
  const addBtn = document.getElementById('files-term-new-tab');
  if (addBtn && !addBtn.dataset.wired) {
    addBtn.dataset.wired = '1';
    let lpTimer = null;
    let lpFired = false;
    const LP_MS = 500;
    const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
    addBtn.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;   // primary button / touch / pen only
      lpFired = false;
      clearLp();
      lpTimer = setTimeout(() => {
        lpTimer = null;
        lpFired = true;
        const name = openNewTmuxSessionDefault();
        _flashTermPop(addBtn, 'New tmux · ' + name);
      }, LP_MS);
    });
    addBtn.addEventListener('pointerup', clearLp);
    addBtn.addEventListener('pointerleave', clearLp);
    addBtn.addEventListener('pointercancel', clearLp);
    // Suppress the OS context menu a touch/pen long-press would otherwise pop.
    addBtn.addEventListener('contextmenu', (e) => e.preventDefault());
    addBtn.addEventListener('click', (e) => {
      if (lpFired) { lpFired = false; e.preventDefault(); e.stopPropagation(); return; }
      openNewTerminalTab();
    });
  }
}

// Deliver a named key to a tmux session via the backend's `tmux send-keys`.
// Used for keys that a raw escape sequence can't reliably trigger inside tmux:
// modern TUIs (Claude Code) negotiate extended keys with tmux 3.x and then stop
// recognising the legacy form of MODIFIED keys (Shift+Tab → \e[Z). Letting tmux
// itself originate the key means it's encoded to match the pane's current
// keyboard mode, whatever that is. Rejects (so callers can fall back to raw)
// if tmux isn't installed, the session is gone, or the request fails.
async function _sendTmuxKey(session, key) {
  await apiFetch('/api/v1/terminal/tmux/send-keys', {
    method: 'POST',
    body: JSON.stringify({ session, key }),
  });
}

// Host hook for terminal.js's Shift+Tab interceptor. terminal.js catches the
// key at keydown (so the browser can't steal it for focus traversal) and asks
// us to deliver it. For a tmux tab we re-originate it through `tmux send-keys`
// (BTab) so it reaches a TUI (Claude Code) that ignores the raw back-tab under
// tmux's extended-keys; we return true to tell terminal.js we took over. For a
// non-tmux tab we return false and terminal.js injects the raw \e[Z itself.
// Defined at module scope (not inside the keybar) so it works on desktop too.
window.__termShiftTab = (sessionId) => {
  let tab = null;
  if (sessionId) {
    tab = openTabs.find((t) => t.kind === 'terminal' && t.path === sessionId) || null;
  }
  if (!tab) tab = getActiveTerminalTab();
  if (!tab || !tab.tmuxSession) return false;  // plain shell → let terminal.js send raw \e[Z
  _sendTmuxKey(tab.tmuxSession, 'shift-tab').catch(() => {
    // tmux call failed (session gone / not installed) — fall back to raw byte.
    try { if (tab.instance && tab.instance.paste) tab.instance.paste('\x1b[Z'); } catch (_) {}
  });
  return true;
};

// Shortcut-key panel in the terminal tab bar. On mobile keyboards there
// are no arrow keys, so the bottom row + middle cell of this 3x3 pad
// pipes ANSI cursor escapes into the active terminal. The remaining
// cells are placeholders for future shortcuts.
// Always-visible sticky bottom bar of chips: Ctrl (one-shot/lock), ^C copy,
// arrows, new-line, copy, paste, mic. Ctrl chip arms on tap, locks on
// long-press so subsequent arrow taps become word-jump sequences.
function initTerminalKeybar() {
  const bar = document.getElementById('files-term-keybar');
  if (!bar || bar.dataset.wired) return;
  bar.dataset.wired = '1';

  // ── Modifier state ───────────────────────────────────────────────
  // 'off' | 'armed' (one-shot) | 'locked' (until tapped again).
  // tmux locked sends the Ctrl+B prefix once at lock time; subsequent keys
  // pass through unmodified — tmux interprets them server-side.
  const mod = { ctrl: 'off', tmux: 'off' };
  function setMod(name, state) {
    mod[name] = state;
    const chip = bar.querySelector('[data-mod="' + name + '"]');
    if (!chip) return;
    chip.dataset.armed = state === 'armed' ? '1' : '';
    chip.dataset.locked = state === 'locked' ? '1' : '';
  }
  function consumeArm() {
    if (mod.ctrl === 'armed') setMod('ctrl', 'off');
    if (mod.tmux === 'armed') setMod('tmux', 'off');
  }
  function sendToActive(bytes) {
    const tab = getActiveTerminalTab();
    if (!tab || !tab.instance || !tab.instance.paste) return false;
    tab.instance.paste(bytes);
    try { tab.instance.focus(); } catch (_) {}
    return true;
  }

  // Translate a chip key (data-key) into raw bytes the PTY expects. When
  // Ctrl is armed/locked, arrows escalate to word-jump sequences.
  function chipBytes(key) {
    const PLAIN = {
      esc:           '\x1b',
      tab:           '\t',
      'shift-tab':   '\x1b[Z',   // CSI Z — back-tab (e.g. cycles modes in Claude Code)
      enter:         '\r',
      sigint:        '\x03',     // Ctrl+C interrupt (the row-1 "^C" chip is copy, not this)
      up:            '\x1b[A',
      down:          '\x1b[B',
      right:         '\x1b[C',
      left:          '\x1b[D',
      'shift-enter': '\x1b\r',
      'ctrl-d':      '\x04',
      'ctrl-l':      '\x0c',
      'ctrl-r':      '\x12',
      'ctrl-z':      '\x1a',
      'lit-pipe':    '|',
      'lit-tilde':   '~',
      'lit-fwd':     '/',
      'lit-bk':      '\\',
      'lit-tick':    '`',
    };
    if (mod.ctrl !== 'off' && /^(up|down|left|right)$/.test(key)) {
      const map = { up: 'A', down: 'B', right: 'C', left: 'D' };
      return '\x1b[1;5' + map[key];
    }
    return Object.prototype.hasOwnProperty.call(PLAIN, key) ? PLAIN[key] : null;
  }

  // ── Long-press detection on modifier chips ───────────────────────
  // 500 ms hold = lock; quick tap = arm. The synthetic click that fires
  // after pointerup is suppressed when a long-press already ran.
  function attachLongPress(chip, modName) {
    const HOLD_MS = 500;
    let timer = null;
    let didLongPress = false;
    let pressed = false;
    function start(e) {
      didLongPress = false;
      pressed = true;
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!pressed) return;
        didLongPress = true;
        const nextState = mod[modName] === 'locked' ? 'off' : 'locked';
        setMod(modName, nextState);
        if (modName === 'tmux' && nextState === 'locked') sendToActive('\x02');
      }, HOLD_MS);
    }
    function cancel() {
      pressed = false;
      clearTimeout(timer);
    }
    chip.addEventListener('pointerdown',  start);
    chip.addEventListener('pointerup',    cancel);
    chip.addEventListener('pointercancel', cancel);
    chip.addEventListener('pointerleave', cancel);
    chip.addEventListener('click', (e) => {
      if (didLongPress) {
        e.preventDefault();
        e.stopPropagation();
        didLongPress = false;
        return;
      }
      const cur = mod[modName];
      if (cur === 'off') {
        setMod(modName, 'armed');
        // tmux: arming means "send prefix now, next key is the tmux command".
        if (modName === 'tmux') sendToActive('\x02');
      } else {
        setMod(modName, 'off');
      }
    });
  }
  bar.querySelectorAll('.ftk-chip-mod[data-mod]').forEach((chip) => {
    attachLongPress(chip, chip.dataset.mod);
  });

  // ── Non-modifier chip clicks ─────────────────────────────────────
  bar.addEventListener('click', async (e) => {
    const chip = e.target.closest('.ftk-chip');
    if (!chip) return;
    const key = chip.dataset.key;
    if (!key) return;
    // Modifier chips own their own click via attachLongPress.
    if (chip.classList.contains('ftk-chip-mod')) return;
    if (key === 'copy')   { await chipCopy();  consumeArm(); return; }
    if (key === 'ctrl-c') { await chipCopy();  consumeArm(); return; }
    if (key === 'paste')  { await chipPaste(); consumeArm(); return; }
    if (key === 'mic')   { toggleMic(chip);   consumeArm(); return; }
    // Shift+Tab inside a tmux session: route through `tmux send-keys` so it
    // reaches a TUI (Claude Code) that remaps modified keys under tmux's
    // extended-keys. The raw \e[Z we'd otherwise inject is silently ignored by
    // such apps. Falls back to the raw byte path if the tmux call fails or the
    // active tab isn't a tmux session.
    if (key === 'shift-tab' && mod.ctrl === 'off' && mod.tmux === 'off') {
      const tab = getActiveTerminalTab();
      if (tab && tab.tmuxSession) {
        consumeArm();
        _sendTmuxKey(tab.tmuxSession, 'shift-tab').catch(() => sendToActive('\x1b[Z'));
        try { if (tab.instance && tab.instance.focus) tab.instance.focus(); } catch (_) {}
        return;
      }
    }
    const bytes = chipBytes(key);
    if (bytes != null) {
      sendToActive(bytes);
      consumeArm();
    }
  });

  // ── Copy / Paste chips ───────────────────────────────────────────
  async function chipCopy() {
    const tab = getActiveTerminalTab();
    if (!tab || !tab.instance || !tab.instance.term) return;
    let text = '';
    try { text = tab.instance.term.getSelection() || ''; } catch (_) {}
    if (!text) {
      // No selection — grab the visible viewport so the user gets *something*.
      try {
        const t = tab.instance.term;
        const buf = t.buffer.active;
        const lines = [];
        const end = buf.viewportY + t.rows;
        for (let i = buf.viewportY; i < end; i++) {
          const line = buf.getLine(i);
          if (line) lines.push(line.translateToString(true));
        }
        text = lines.join('\n').replace(/\s+$/, '');
      } catch (_) {}
    }
    if (!text) return;
    try { await copyText(text); } catch (_) {}   // copyText: works in insecure contexts (phones)
  }
  async function chipPaste() {
    // Prefer the rich clipboard API so a pasted IMAGE (a screenshot, a copied
    // picture) is caught too — not just text. An image can't be "typed" into
    // the PTY text stream, so we hand it to _pasteImageBlob, which uploads it
    // to the server and types back the saved file's path; Claude Code (and any
    // program that reads an image path) then picks it up. We fall through to a
    // plain-text paste when there's no image, and to readText() when the rich
    // API is unavailable (older browser, or an insecure http:// context where
    // image-clipboard reads are blocked — see deployment.md "Secure-context").
    if (navigator.clipboard && typeof navigator.clipboard.read === 'function') {
      let items = null;
      try { items = await navigator.clipboard.read(); } catch (_) { items = null; }
      if (items) {
        for (const item of items) {
          const imgType = (item.types || []).find((t) => t.startsWith('image/'));
          if (imgType) {
            try {
              const blob = await item.getType(imgType);
              await _pasteImageBlob(blob, imgType);
            } catch (e) {
              alert('Could not paste image: ' + ((e && e.message) ? e.message : e));
            }
            return;
          }
        }
        // No image among the items — take text from them if present.
        for (const item of items) {
          if ((item.types || []).includes('text/plain')) {
            try {
              const blob = await item.getType('text/plain');
              const text = await blob.text();
              if (text) sendToActive(text);
            } catch (_) {}
            return;
          }
        }
      }
    }
    // Fallback: plain-text clipboard (older browsers, or read() blocked).
    let text = '';
    try { text = await navigator.clipboard.readText(); } catch (_) { return; }
    if (text) sendToActive(text);
  }

  // Upload a pasted image blob, then type the saved file's absolute path into
  // the active terminal. The path is double-quoted (it usually contains spaces,
  // e.g. "C:\Users\Alex R\...") and trailed with a space so the user can keep
  // typing their request after it. No Enter is sent.
  async function _pasteImageBlob(blob, mimeType) {
    if (!blob) return;
    const path = await uploadTerminalPasteImage(blob, mimeType);
    if (path) sendToActive('"' + path + '" ');
  }

  // ── Microphone dictation ────────────────────────────────────────
  // Web Speech API. One result is appended to the PTY input. Tap again to
  // stop. We don't auto-submit (no trailing \n) so the user can review
  // the recognised text before hitting Enter.
  let recognition = null;
  let listeningChip = null;
  function toggleMic(chip) {
    if (listeningChip === chip && recognition) {
      try { recognition.stop(); } catch (_) {}
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      alert('Speech recognition is not supported in this browser.');
      return;
    }
    recognition = new SR();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = navigator.language || 'en-US';
    recognition.onresult = (ev) => {
      const text = Array.from(ev.results).map((r) => r[0].transcript).join(' ');
      if (text) sendToActive(text);
    };
    const stopUi = () => {
      chip.dataset.listening = '';
      listeningChip = null;
      recognition = null;
    };
    recognition.onend = stopUi;
    recognition.onerror = stopUi;
    try {
      recognition.start();
      chip.dataset.listening = '1';
      listeningChip = chip;
    } catch (_) { /* InvalidStateError — already running */ }
  }

  // ── Soft-keyboard transform ──────────────────────────────────────
  // Lets the Ctrl chord work with characters the user types on the soft
  // keyboard (not just other chips). Wired into terminal.js via
  // window.__termInputTransform so we don't need a cross-module import.
  window.__termInputTransform = (data) => {
    if (mod.ctrl === 'off' && mod.tmux !== 'armed') return data;
    let out = '';
    if (mod.ctrl !== 'off') {
      for (let i = 0; i < data.length; i++) {
        const code = data.charCodeAt(i);
        if (code >= 0x61 && code <= 0x7a) out += String.fromCharCode(code - 0x60);       // a-z
        else if (code >= 0x41 && code <= 0x5a) out += String.fromCharCode(code - 0x40);  // A-Z
        else out += data[i];
      }
    } else {
      out = data;
    }
    consumeArm();
    return out;
  };
}

// ── Pinch-zoom on the terminal pane ───────────────────────────────
// Two fingers on the terminal content scale the font size. Uses the
// existing setTerminalFontSize (which propagates to every open terminal).
function initTerminalPinchZoom() {
  const host = document.getElementById('files-term-content');
  if (!host || host.dataset.pinchWired) return;
  host.dataset.pinchWired = '1';
  let initialDist = 0;
  let initialFontSize = 0;
  function dist(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }
  host.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 2) return;
    initialDist = dist(e.touches);
    initialFontSize = getTerminalFontSize();
  }, { passive: true });
  host.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 2 || initialDist <= 0) return;
    e.preventDefault(); // stop the page from pinch-zooming
    const ratio = dist(e.touches) / initialDist;
    setTerminalFontSize(Math.round(initialFontSize * ratio));
  }, { passive: false });
  function release(e) {
    if (e.touches.length < 2) initialDist = 0;
  }
  host.addEventListener('touchend',    release, { passive: true });
  host.addEventListener('touchcancel', release, { passive: true });
}

// ── Visual viewport refit ─────────────────────────────────────────
// When the mobile soft keyboard opens/closes, the visual viewport shrinks
// or grows. xterm sees its host height change only on a window resize,
// not on visualViewport resize, so the prompt ends up under the keyboard.
// Refit the active terminal each time the viewport changes.
function initTerminalViewportRefit() {
  const vv = window.visualViewport;
  if (!vv || vv.dataset && vv.dataset.refitWired) return;
  const handler = () => {
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 60);
  };
  vv.addEventListener('resize', handler);
  vv.addEventListener('scroll', handler);
}

// ── Swipe between terminal tabs ───────────────────────────────────
// Horizontal swipe on the tab strip switches to the previous / next
// terminal tab. Threshold + axis check keep accidental vertical
// scrolls from triggering a switch.
function initTerminalTabSwipe() {
  const strip = document.getElementById('files-term-tabs');
  if (!strip || strip.dataset.swipeWired) return;
  strip.dataset.swipeWired = '1';
  let startX = 0, startY = 0, t0 = 0;
  strip.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    t0 = Date.now();
  }, { passive: true });
  strip.addEventListener('touchend', (e) => {
    if (e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    if (Date.now() - t0 > 500) return;
    if (Math.abs(dx) < 60) return;
    if (Math.abs(dy) > Math.abs(dx)) return;
    const termTabs = openTabs.filter((t) => t.kind === 'terminal');
    if (termTabs.length < 2) return;
    const cur = termTabs.findIndex((t) => t.path === activeTerminalId);
    if (cur < 0) return;
    const next = dx < 0
      ? (cur + 1) % termTabs.length
      : (cur - 1 + termTabs.length) % termTabs.length;
    activateTab(termTabs[next].path);
  }, { passive: true });
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
    _refreshLucideIcons(host);
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
    _refreshLucideIcons(pane);
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

    // "Disconnected" overlay — shown whenever the WS isn't connected so the
    // user knows their keystrokes aren't reaching the shell (the xterm canvas
    // still looks live even when the socket is dead). pointer-events:none in
    // CSS keeps clicks going through to xterm for focus restore.
    const overlay = document.createElement('div');
    overlay.className = 'files-terminal-overlay';
    const overlayText = document.createElement('div');
    overlayText.className = 'files-terminal-overlay-text';
    overlay.appendChild(overlayText);
    pane.appendChild(overlay);

    // Scroll-to-bottom FAB — floats bottom-right; visible only when scrolled up.
    const scrollBot = document.createElement('button');
    scrollBot.className = 'files-term-scroll-bot';
    scrollBot.type = 'button';
    scrollBot.title = 'Scroll to bottom';
    scrollBot.setAttribute('aria-label', 'Scroll to bottom');
    scrollBot.innerHTML = '<i data-lucide="chevrons-down" class="lucide-icon"></i>';
    pane.appendChild(scrollBot);

    // Clicking anywhere on the pane should restore xterm focus. xterm's input
    // is in a hidden helper textarea that only auto-focuses when the click
    // lands on a row glyph; clicks on padding/margins otherwise look focused
    // but actually drop input on the floor.
    host.addEventListener('mousedown', () => {
      if (tab.instance) {
        try { tab.instance.focus(); } catch (_) {}
      }
    });

    // Drag-and-drop. Two cases:
    //  • A file from the file TREE → its absolute path (shell-quoted) is pasted
    //    at the prompt. The tree marshals the path as text/plain in dragstart.
    //  • An external IMAGE file dropped from the OS → uploaded to the server via
    //    the same relay as the Paste chip, then its saved path typed in (quoted)
    //    so Claude Code reads it as an image. Detected via dataTransfer "Files".
    host.addEventListener('dragover', (e) => {
      if (!e.dataTransfer) return;
      const types = Array.from(e.dataTransfer.types || []);
      if (types.indexOf('text/plain') !== -1 || types.indexOf('Files') !== -1) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        host.classList.add('files-terminal-drop-target');
      }
    });
    host.addEventListener('dragleave', () => host.classList.remove('files-terminal-drop-target'));
    host.addEventListener('drop', (e) => {
      host.classList.remove('files-terminal-drop-target');
      if (!tab.instance) return;
      // An external image file takes priority over the text/plain path.
      const dt = e.dataTransfer;
      const imgFile = dt && dt.files && Array.from(dt.files).find(
        (f) => f.type && f.type.startsWith('image/'),
      );
      if (imgFile) {
        e.preventDefault();
        uploadTerminalPasteImage(imgFile, imgFile.type)
          .then((path) => {
            if (path && tab.instance) {
              tab.instance.paste('"' + path + '" ');
              tab.instance.focus();
            }
          })
          .catch((err) => alert('Could not add image: ' + ((err && err.message) ? err.message : err)));
        return;
      }
      const raw = dt && dt.getData('text/plain');
      if (!raw) return;
      e.preventDefault();
      tab.instance.paste(shellQuote(raw) + ' ');
      tab.instance.focus();
    });

    // Long-press on mobile → context menu with Copy / Paste / Select all.
    // Fires after a 500ms hold that didn't move; cancelled on move / lift.
    wireTerminalLongPress(host, () => tab.instance);
    // Two-finger pinch → adjust the global terminal font size.
    wireTerminalPinchZoom(host);
    // One-finger drag → scroll the scrollback (touch screens have no wheel
    // and xterm doesn't pan on a single-finger drag by itself).
    wireTerminalDragScroll(host, () => tab.instance);

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
          claudeSessionId: tab.claudeSessionId || '',
          wrap: tab.wrap !== false,           // default true unless persisted false
          fontSize: getTerminalFontSize(),    // global setting, shared across tabs
          // Re-evaluated on every (re)connect so inline-rename and the cached
          // localStorage name are reflected on the server without reconnecting.
          nameProvider: () => (tab.name || getTerminalSessionName(tab.path) || ''),
        });
        // Consume the command — buildPaneForTab can be called again later
        // (e.g. pane mode swap), but the shell already has it.
        tab.initialCommand = '';
        // Wire scroll-to-bottom FAB: show when viewport is not at the buffer end.
        const _term = tab.instance && tab.instance.term;
        if (_term && scrollBot) {
          const _updateFab = () => {
            const buf = _term.buffer.active;
            scrollBot.classList.toggle('visible', buf.viewportY < buf.length - _term.rows);
          };
          _term.onScroll(_updateFab);
          scrollBot.addEventListener('click', () => {
            try { _term.scrollToBottom(); _term.focus(); } catch (_) {}
          });
          _refreshLucideIcons(scrollBot);
        }
        // Drive the per-tab status dot AND the pane overlay from the WS
        // state machine. The dot is a small at-a-glance hint on the tab; the
        // overlay is the loud "your keystrokes aren't reaching the shell"
        // signal on the pane itself.
        tab.instance.onStateChange((s) => {
          _updateTabConnDot(tab.path, s);
          pane.classList.toggle('files-terminal-disconnected', s !== 'connected');
          overlayText.textContent = _connStateTitle(s);
        });
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
    '<input type="text" class="files-findbar-input files-findbar-find"    placeholder="Find" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
    '<span class="files-findbar-count">0 / 0</span>' +
    '<button type="button" class="files-findbar-btn" data-act="prev"  title="Previous (Shift+Enter)"><i data-lucide="chevron-up" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="next"  title="Next (Enter)"><i data-lucide="chevron-down" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="case"  title="Match case">Aa</button>' +
    '<input type="text" class="files-findbar-input files-findbar-replace" placeholder="Replace" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
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
  _refreshLucideIcons(bar);
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
    // Saving changes the file's line count vs HEAD — refresh the tree badges.
    loadLineStats();
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
        return { path: t.path, name: t.name, kind: 'terminal', wrap: t.wrap !== false, tmuxSession: t.tmuxSession || '' };
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
  try {
    const v = localStorage.getItem(LS_PROD_VIEW);
    if (v === 'prod' || v === 'dev') prodViewMode = v;
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
          pushTerminalTab(t.path, t.name, { tmuxSession: t.tmuxSession || '' });
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

function initFiles() {
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

  // Toolbar "More" → production popover (preview toggle + Copy + Push).
  const moreBtn = document.getElementById('files-more-btn');
  if (moreBtn) {
    moreBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openProductionMenu(moreBtn);
    });
  }

  initSidebarResize();
  initTabCarousel();
  initTerminalKeybar();
  initTerminalPinchZoom();
  initTerminalViewportRefit();
  initTerminalTabSwipe();
  installFilesDropGuard();
  // Build the sidebar strip's view-switch icons from the admin page catalog
  // BEFORE the view switcher / maximize init apply their active highlighting —
  // the buttons must exist first. __applyAdminPanelOrder → __buildAdminStrip
  // (header-build.js) renders them from window.__pagesCatalog.admin (the same
  // drop-in catalog the main header uses). startAdminTools rebuilds once the
  // catalog has loaded, in case this ran before the boot fetch resolved.
  if (window.__applyAdminPanelOrder) window.__applyAdminPanelOrder();
  initSidebarViewSwitcher();
  initSettingsToggle();
  injectPanelCollapseButtons();
  initSidebarMaximize();
  initFilesTerminalButton();
  renderTabs();
  renderEditorPanes();
}

// (The global header "restart & reload" button was removed — restart is still
// available via #btn-restart in the chat header and #ft-refresh in the terminal
// sidebar. Removing it lets initFiles() be deferred to first Admin Tools open.)

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

// ── Mobile drag-to-scroll ─────────────────────────────────────────
//
// One-finger vertical drag pans the terminal scrollback. xterm has no
// built-in touch panning and there's no scroll wheel on touch devices, so
// without this you can't reach earlier output on a phone/tablet. We convert
// the finger's pixel travel into whole rows and feed them to xterm's
// scrollLines (negative = toward the top). Single-finger only — two-finger
// gestures belong to pinch-zoom, and a stationary hold belongs to the
// long-press menu (both wired separately on the same host).
function wireTerminalDragScroll(host, getInstance) {
  let active = false;
  let lastY = 0;
  let accumRows = 0;   // fractional rows carried between moves

  function rowHeightPx(term) {
    const rows = term.rows || 24;
    const h = (term.element && term.element.clientHeight) || host.clientHeight || (rows * 17);
    return rows > 0 ? h / rows : 17;
  }

  host.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) { active = false; return; }
    active = true;
    lastY = e.touches[0].clientY;
    accumRows = 0;
  }, { passive: true });

  host.addEventListener('touchmove', (e) => {
    if (!active || !e.touches || e.touches.length !== 1) { active = false; return; }
    const inst = getInstance && getInstance();
    const term = inst && inst.term;
    if (!term) return;
    const y = e.touches[0].clientY;
    const dy = y - lastY;
    lastY = y;
    const px = rowHeightPx(term);
    if (px <= 0) return;
    accumRows += dy / px;
    const rows = Math.trunc(accumRows);
    if (rows !== 0) {
      accumRows -= rows;
      // Finger moving down reveals earlier content → scroll toward the top
      // (negative). scrollLines is positive-down, hence the sign flip.
      try { term.scrollLines(-rows); } catch (_) {}
      // Once we're actually scrolling, swallow the gesture so xterm doesn't
      // start a text selection and the page doesn't rubber-band underneath.
      if (e.cancelable) e.preventDefault();
    }
  }, { passive: false });

  const end = () => { active = false; };
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
      if (text) await copyText(text);   // copyText: works in insecure contexts (phones)
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

  _refreshLucideIcons(menu);

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
    '<input type="text" class="files-terminal-findbar-input" placeholder="Find in terminal" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
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
  _refreshLucideIcons(bar);
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
      // For terminal tabs, cache the name locally AND push it to the server
      // so the "Your sessions" list on other devices shows the new label.
      if (tab.kind === 'terminal') {
        try { setTerminalSessionName(tab.path, v); } catch (_) {}
        if (tab.instance && typeof tab.instance.setName === 'function') {
          try { tab.instance.setName(v); } catch (_) {}
        }
      }
    }
    renderTabs();   // rebuild — swaps the input back to a span
  }
  // ╔═╗ RENAME-FIELD PATTERN  ════════════════════════════════════════════════════╗
  // ║ Inline rename: create <input>, replace label, Enter/Escape/blur commit.    ║
  // ║ Duplicated in sessions.js (startRename & _headerRenameSession) and           ║
  // ║ canvas.js (_startRenamePage). Mirror fixes across all copies.            ║
  // ╚══════════════════════════════════════════════════════════════════════════════╝
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

// Extract the tmux SESSION NAME a terminal tab is driving, parsed from its
// launch command (`tmux new -As <name>`, `tmux attach -t <name>`, etc.). Empty
// for a plain shell or a non-session tmux command (`tmux ls`). The keybar uses
// this so it can deliver Shift+Tab through `tmux send-keys` — see _sendTmuxKey.
function _parseTmuxSession(cmd) {
  if (!cmd) return '';
  // Lazily skip from `tmux` to the first session selector (-s / -As / -t),
  // then grab the session name. The name may carry an EMBEDDED single quote in
  // the resume launcher (`tmux new -As claude-'<sid>' …`), so we include the
  // quote in the captured token and strip every quote afterwards — otherwise the
  // match stops at the inner quote and yields a truncated name (`claude-`) that
  // no live session matches, breaking `tmux send-keys` (e.g. Shift+Tab).
  const m = /\btmux\b[\s\S]*?\s-(?:A?s|t)\s+([A-Za-z0-9_.'-]+)/.exec(cmd);
  return m ? m[1].replace(/'/g, '') : '';
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
    // For "Resume Claude" tabs: the Claude conversation id this tab is resuming.
    // Passed up on the WS so the backend marks that conversation "already open"
    // (a second click on the quick-launch then skips to the next-newest). Not
    // persisted — but the backend session keeps its own copy across reloads.
    claudeSessionId: opts.claudeSessionId || '',
    // tmux session this tab is attached to (if any). Lets the keybar route
    // Shift+Tab via `tmux send-keys` so it reaches modern TUIs (Claude Code)
    // that remap modified keys under tmux's extended-keys. Persisted so it
    // survives a reload even though initialCommand isn't.
    tmuxSession: opts.tmuxSession || _parseTmuxSession(opts.initialCommand),
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
// the sidebar's quick-launch and tmux-session lists. `opts.claudeSessionId`
// tags the tab with a Claude conversation id (see "Resume Claude").
function openNewTerminalTabWithCommand(command, name, opts) {
  opts = opts || {};
  const id = newTerminalSessionId();
  pushTerminalTab(id, name, {
    initialCommand: command || '',
    claudeSessionId: opts.claudeSessionId || '',
  });
  activeTerminalId = id;
  renderTabs();
  renderEditorPanes();
  persistTabs();
  _switchToTerminalStrip();
}

function _switchToTerminalStrip() {
  applySidebarView('terminal');
  try { localStorage.setItem(LS_SIDEBAR_VIEW, 'terminal'); } catch (_) {}
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

// isMobileLayout now lives in ./layout.js (imported at the top of this file)
// so leaf modules like db/tables.js can use it without importing this large
// module — that import created a db/* <-> files.js cycle.

// Give every sidebar panel its own collapse / switch-display control. This
// replaces the single shared strip button: each view owns the control in its
// panel header, so a dropped-in view's panel gets one for FREE (no markup in
// the view's own HTML). Idempotent. Panels that ship a .files-sidebar-header
// get the button prepended (far-left, strip-adjacent — it collapses toward the
// icon strip); panels without one get a minimal header created to hold it.
function injectPanelCollapseButtons() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  sidebar.querySelectorAll('.files-sidebar-panel').forEach((panel) => {
    if (panel.querySelector('.files-panel-collapse-btn')) return;
    let header = panel.querySelector(':scope > .files-sidebar-header');
    if (!header) {
      header = document.createElement('div');
      header.className = 'files-sidebar-header files-panel-collapse-header';
      panel.insertBefore(header, panel.firstChild);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'files-icon-btn files-panel-collapse-btn';
    btn.title = 'Collapse sidebar';
    btn.setAttribute('aria-label', 'Collapse sidebar');
    btn.innerHTML = '<i data-lucide="chevrons-left" class="lucide-icon"></i>';
    header.insertBefore(btn, header.firstChild);
  });
  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

function initSidebarMaximize() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore prior state. On mobile, fold 'split' into 'strip'.
  let saved = localStorage.getItem(LS_SIDEBAR_STATE) || 'split';
  if (saved !== 'split' && saved !== 'max' && saved !== 'strip') saved = 'split';
  if (isMobileLayout() && saved === 'split') saved = 'strip';
  setSidebarState(saved);

  // Delegate clicks on the per-view collapse buttons and the strip's
  // view-switch buttons. Each view's panel header carries its own
  // .files-panel-collapse-btn (injected by injectPanelCollapseButtons); they
  // all drive the same cycleSidebarState — collapse to the icon strip on
  // desktop, switch panel↔main on mobile.
  sidebar.addEventListener('click', (e) => {
    const cycle = e.target.closest('.files-panel-collapse-btn');
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
      // Belt-and-braces: non-admin clicking a strip view should re-show the
      // restricted overlay rather than activate the sub-page. The parent
      // gate in startAdminTools normally hides this strip already; this
      // covers DevTools-style bypasses where someone unhides #admin-tools
      // without re-running /check-access.
      if (!isAdmin) {
        showRestrictedModal();
        return;
      }
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

// Not exported: db/tables.js reaches this via app.setSidebarState (registered
// below) to avoid a db/* <-> files.js import cycle.
function setSidebarState(state) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  if (state !== 'split' && state !== 'max' && state !== 'strip') state = 'split';
  if (isMobileLayout() && state === 'split') state = 'strip';
  // 'max' only exists on mobile — coerce stale localStorage to 'split' on desktop.
  if (!isMobileLayout() && state === 'max') state = 'split';
  sidebar.dataset.state = state;
  sidebar.classList.toggle('maximized', state === 'max');
  sidebar.classList.toggle('strip',     state === 'strip');

  // Keep each panel's collapse button's icon + title in sync. They only show
  // while a panel is expanded (panels are hidden in strip mode), so in practice
  // they always read "Collapse"; the strip-mode branch is harmless bookkeeping.
  const iconName = state === 'strip' ? 'chevrons-right' : 'chevrons-left';
  const title    = state === 'strip' ? 'Expand sidebar' : 'Collapse sidebar';
  sidebar.querySelectorAll('.files-panel-collapse-btn').forEach((b) => {
    b.title = title;
    b.innerHTML = '<i data-lucide="' + iconName + '" class="lucide-icon"></i>';
  });

  // Strip is always rendered now; panels follow the current view (via
  // applySidebarView) but stay hidden when in strip mode.
  applySidebarView(sidebar.dataset.view || 'explorer');

  // icons.js auto-renders via MutationObserver — no manual refresh needed
  try { localStorage.setItem(LS_SIDEBAR_STATE, state); } catch (_) {}
}

// Exposed on `app` so db/tables.js can collapse the sidebar after a table is
// selected WITHOUT importing files.js (that import was the db/* <-> files.js
// cycle edge). files.js is always loaded before the DB viewer it launches, so
// app.setSidebarState is set well before any table click.
app.setSidebarState = setSidebarState;

// ── Sidebar view switcher (Explorer ↔ Source Control) ─────────────

function initSidebarViewSwitcher() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore last view (default: explorer)
  const stored = localStorage.getItem(LS_SIDEBAR_VIEW);
  const want = (stored && (stored in VIEW_MAIN_ID || _adminCatalogPage(stored)))
    ? stored : 'explorer';
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
  terminal: 'Terminal launchers',
};
const VIEW_SWITCH = {
  explorer: 'file manager',
  terminal: 'terminal launchers',
};

// Each sidebar view has a dedicated <main> on the right side. Switching
// the strip swaps which main is visible.
// Only the views whose lifecycles are STILL wired inline here remain built-in:
// Explorer + Terminal (shared tab engine, split in a later phase). Every other
// admin view — including Settings — is a drop-in driven from its descriptor
// entry/start/stop; listing one here would mark it built-in and skip that
// dynamic dispatch, so keep this map to the inline-wired views only.
const VIEW_MAIN_ID = {
  explorer:       'files-explorer-main',
  terminal:       'files-terminal-main',
};

// ── Drop-in admin views ────────────────────────────────────────────────────
// The eight views above are the BUILT-INS — their lifecycles (panel + main
// renders, poll loops) are wired inline in applySidebarView, exactly as
// tabs.js keeps startAgents/stopAgents etc. as static hooks. A brand-new admin
// view dropped in as ui/admin-tools/<id>/page.json is NOT listed here; it is
// driven generically from its descriptor's entry/start/stop via a dynamic
// import (mirrors tabs.js _startPage/_stopDynamic). Its main pane must be a
// <main class="files-main" id="files-<id>-main" data-view="<id>"> under
// #admin-tools; the strip button + #files-<id>-main swap come for free.
function _adminCatalogPage(id) {
  try {
    const c = window.__pagesCatalog;
    const list = (c && Array.isArray(c.admin)) ? c.admin : [];
    return list.find((p) => p.id === id) || null;
  } catch (_) { return null; }
}
const _dynAdminMods = {};      // view id → Promise<module> (cached after import)
let _activeDynAdminView = null;
function _dynAdminModule(id, entry) {
  if (!_dynAdminMods[id]) {
    try { _dynAdminMods[id] = import(new URL(entry, document.baseURI).href); }
    catch (e) { _dynAdminMods[id] = Promise.reject(e); }
  }
  return _dynAdminMods[id];
}
function _startDynAdminView(id) {
  const p = _adminCatalogPage(id);
  if (p && p.entry && p.start) {
    _dynAdminModule(id, p.entry)
      .then((m) => { const fn = m && m[p.start]; if (typeof fn === 'function') fn(); })
      .catch((e) => console.error('admin view start ' + id + ' failed', e));
  }
}
function _stopDynAdminView(id) {
  const p = _adminCatalogPage(id);
  if (p && p.entry && p.stop && _dynAdminMods[id]) {
    _dynAdminMods[id]
      .then((m) => { const fn = m && m[p.stop]; if (typeof fn === 'function') fn(); })
      .catch(() => {});
  }
}

function applySidebarView(view) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Accept any built-in view OR a catalog-known drop-in admin view; fall back
  // to explorer for anything unrecognised.
  const isBuiltin = (view in VIEW_MAIN_ID);
  if (!isBuiltin && !_adminCatalogPage(view)) view = 'explorer';
  sidebar.dataset.view = view;
  // Mirror the active sub-view into the address bar (?tab=admin-tools&view=<id>)
  // so a sub-view is shareable and survives a refresh. tabs.js (window.__setMainSubView)
  // guards this to the live Admin Tools tab; Explorer (the default view) collapses
  // to the bare ?tab=admin-tools. Best-effort — never blocks the view swap.
  try {
    if (typeof window.__setMainSubView === 'function') {
      window.__setMainSubView('admin-tools', view, 'explorer');
    }
  } catch (_) { /* address-bar sync is best-effort */ }
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
  if (view === 'terminal' && state !== 'strip') {
    openTerminalLaunchersPanel();
  } else {
    stopTerminalLaunchersPolling();
  }
  // Right-pane swap: hide every per-view main except the one matching
  // `view`. Built-ins map via VIEW_MAIN_ID; a drop-in view follows the
  // #files-<id>-main convention (overridable by its descriptor `mount`).
  const dynPage = isBuiltin ? null : _adminCatalogPage(view);
  const wantId = VIEW_MAIN_ID[view]
    || (dynPage && dynPage.mount && dynPage.mount.replace(/^#/, ''))
    || ('files-' + view + '-main');
  document.querySelectorAll('#admin-tools .files-main[data-view]').forEach((el) => {
    el.hidden = (el.id !== wantId);
  });
  // Non-admin (or pre-check-access): stop here. Skip per-view background
  // work (polls, fetches, lazy panel renders) so a non-admin who somehow
  // reaches this code path — or the brief window before startAdminTools'
  // /check-access has set the local isAdmin flag — doesn't kick off
  // database/loop/git polling. startAdminTools re-calls applySidebarView
  // after the access check so the side effects fire for real admins.
  if (!isAdmin) return;
  // Per-view background work for the inline-wired built-ins. (Settings,
  // database, git, interactions, runtime-loop and diagnostics are drop-ins now
  // — their start/stop run via the dynamic dispatch at the end of this
  // function.)
  // The git +/- badge poll only runs while the tree is the active view — stop it
  // on every switch, then (re)start it below when landing on Explorer.
  stopLineStatsAutoRefresh();
  if (view === 'terminal') {
    // Refit the active terminal once the main becomes visible — xterm
    // can't measure a display:none host.
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 30);
  } else if (view === 'explorer') {
    // Returning to the tree (e.g. after committing in Source Control) — refresh
    // the +/- badges now and keep them live on a 20s timer while it's open.
    loadLineStats();
    startLineStatsAutoRefresh();
  }
  // Drop-in admin views (not built-in): stop the one we navigated away from,
  // then start the active one via its descriptor entry/start/stop. Mirrors the
  // tabs.js page runtime; built-in views are already handled above.
  if (_activeDynAdminView && _activeDynAdminView !== view) {
    _stopDynAdminView(_activeDynAdminView);
    _activeDynAdminView = null;
  }
  if (!isBuiltin) {
    _startDynAdminView(view);
    _activeDynAdminView = view;
  }
}

// ── Terminal launchers sidebar panel ──────────────────────────────
//
// Three sections (quick launches, unified session list, static hints)
// in the terminal launcher sidebar. Quick launches come from
// /api/v1/terminal/quick-launches and are essentially static for the
// lifetime of the page; the session list (PTY + tmux) is polled every
// 5s while the panel is visible.

let _ftQuickLaunchesLoaded = false;
let _ftTmuxPollTimer = null;

// Friendly names for terminal sessions, scoped to the current user. Sent on
// WS open via the `name` query param so other devices see a useful label in
// the "Your sessions" list instead of the raw UUID.
const LS_TERM_SESSION_NAMES = 'files.terminalSessionNames';
function _loadTermNames() {
  try {
    const raw = localStorage.getItem(LS_TERM_SESSION_NAMES);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (_) { return {}; }
}
function _saveTermNames(map) {
  try { localStorage.setItem(LS_TERM_SESSION_NAMES, JSON.stringify(map)); } catch (_) {}
}
function getTerminalSessionName(sessionId) {
  if (!sessionId) return '';
  return _loadTermNames()[sessionId] || '';
}
function setTerminalSessionName(sessionId, name) {
  if (!sessionId) return;
  const map = _loadTermNames();
  if (name) map[sessionId] = name;
  else delete map[sessionId];
  _saveTermNames(map);
}

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
    const ic = it.icon || 'terminal';
    row.innerHTML = '<i data-lucide="' + ftEscapeHtml(ic) + '" class="lucide-icon ft-row-icon"></i>' +
                    '<span class="ft-row-label">' + ftEscapeHtml(it.name) + '</span>';
    if (it.action === 'claude-resume') {
      // Dynamic launcher: the exact command (which conversation to resume) is
      // resolved server-side at click time, so this row carries an `action`
      // instead of a static `command`. Tap = resume in a plain terminal;
      // long-press = resume inside tmux. See ftLaunchClaudeResume.
      row.title = 'Tap: resume your most recent Claude conversation in a terminal. Long-press: resume in tmux.';
      _ftWireLaunchRow(row,
        () => ftLaunchClaudeResume(row, false),
        () => ftLaunchClaudeResume(row, true),
        it.name);
    } else {
      // A short tap opens the plain `command` in a normal terminal window; a
      // long-press opens the tmux variant (`tmux_command`) when one exists, so
      // the session survives tab close/refresh. Rows without a distinct tmux
      // variant (attach/ls/plain shell) do the same thing either way.
      const plain = it.command || '';
      const tmuxCmd = it.tmux_command || '';
      const hasTmux = !!tmuxCmd && tmuxCmd !== plain;
      row.title = plain ? ('Run: ' + plain) : 'Open a plain shell';
      if (hasTmux) row.title += '  ·  Long-press to run in tmux';
      _ftWireLaunchRow(row,
        () => openNewTerminalTabWithCommand(plain, it.name || undefined),
        hasTmux ? () => openNewTerminalTabWithCommand(tmuxCmd, it.name || undefined) : null,
        it.name);
    }
    host.appendChild(row);
  }
  _refreshLucideIcons(host);
}

// Wire a quick-launch row for tap vs long-press. `onShort` fires on a normal
// tap; `onLong` (optional) fires after a ≥500ms hold and flashes a small "in
// tmux" confirmation. When there's no `onLong`, the row is a plain click.
// Mirrors the "+" new-tab button's long-press pattern (see initTabCarousel):
// the long-press sets `lpFired` so the trailing synthetic click is swallowed.
function _ftWireLaunchRow(row, onShort, onLong, name) {
  if (!onLong) { row.addEventListener('click', onShort); return; }
  let lpTimer = null;
  let lpFired = false;
  const LP_MS = 500;
  const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
  row.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;  // primary button / touch / pen only
    lpFired = false;
    clearLp();
    lpTimer = setTimeout(() => {
      lpTimer = null;
      lpFired = true;
      onLong();
      _flashTermPop(row, (name ? name + ' · ' : '') + 'in tmux');
    }, LP_MS);
  });
  row.addEventListener('pointerup', clearLp);
  row.addEventListener('pointerleave', clearLp);
  row.addEventListener('pointercancel', clearLp);
  // Suppress the OS context menu a touch/pen long-press would otherwise pop.
  row.addEventListener('contextmenu', (e) => e.preventDefault());
  row.addEventListener('click', (e) => {
    if (lpFired) { lpFired = false; e.preventDefault(); e.stopPropagation(); return; }
    onShort();
  });
}

// "Resume Claude" quick-launch. Unlike the static launches, the command is
// resolved on the server at click time (/api/v1/terminal/claude-resume-target):
// it returns the newest Claude conversation that isn't already open in a
// running `claude`, as a ready-to-run shell command — or a fresh `claude` when
// there's nothing to resume. We then open it like any other quick-launch.
async function ftLaunchClaudeResume(row, useTmux) {
  if (row && row.dataset.busy === '1') return;   // ignore double-taps mid-lookup
  if (row) row.dataset.busy = '1';
  try {
    const target = await apiFetch('/api/v1/terminal/claude-resume-target');
    // Tap resumes in a plain terminal (`command`); long-press resumes inside a
    // named tmux session (`tmux_command`) so it survives tab close/refresh.
    const cmd = (useTmux && target && target.tmux_command)
      ? target.tmux_command
      : ((target && target.command) || 'claude');
    const name = (target && target.name) || 'Claude';
    // Tag the tab with the resumed conversation id so the backend counts it as
    // open — clicking the button again then skips it and resumes the next-newest
    // (the whole point of this fix; works on Windows where /proc detection can't).
    openNewTerminalTabWithCommand(cmd, name, {
      claudeSessionId: (target && target.session_id) || '',
    });
  } catch (e) {
    // Endpoint unreachable (e.g. server not yet restarted after this change) —
    // still do the useful thing and launch a fresh Claude so the button never
    // dead-ends.
    console.warn('Resume Claude lookup failed, launching fresh:', (e && e.message) || e);
    openNewTerminalTabWithCommand('claude', 'Claude');
  } finally {
    if (row) row.dataset.busy = '';
  }
}

// ── Unified session list renderer (PTY + tmux) ────────────────────
//
// Renders both PTY terminal sessions and tmux sessions as one unified
// list. PTY rows use a `square-terminal` icon; tmux rows use a `layers`
// icon (same as the "New tmux session" button), replacing the old
// "tmux: " text prefix.

// Append tmux's mouse-mode toggle to a launch/attach command so the
// browser's scroll wheel is forwarded to a mouse-aware TUI (Claude) in
// the pane — which scrolls its OWN message history — instead of tmux's
// default (mouse off), where the wheel becomes cursor-up/down keys that
// scroll Claude's prompt-history. `-g` is server-global and idempotent,
// so it's safe on every command; `';'` is a tmux command separator wrapped
// in single quotes so it survives both bash/WSL and PowerShell (PowerShell
// treats `;` as a statement separator, so `\;` breaks there).
// Mirrors `_TMUX_MOUSE_ON` in app/api/terminal.py.
function _ftTmuxMouseOn(cmd) {
  return cmd + " ';' set -g mouse on";
}

function ftRenderAllSessions(sessions, tmuxItems) {
  const host = document.getElementById('ft-list-sessions');
  if (!host) return;

  // Combine: tag tmux items for the render loop
  const combined = [];

  // PTY sessions (from /api/v1/terminal/sessions)
  const live = (sessions || []).filter((s) => s && s.alive !== false);
  // Sort: attached first, then by age desc
  live.sort((a, b) => {
    const aAtt = (a.attached_clients || 0) > 0 ? 1 : 0;
    const bAtt = (b.attached_clients || 0) > 0 ? 1 : 0;
    if (aAtt !== bAtt) return bAtt - aAtt;
    return (b.age_secs || 0) - (a.age_secs || 0);
  });
  for (const s of live) combined.push({ kind: 'pty', data: s });

  // Tmux sessions (from /api/v1/terminal/tmux-sessions)
  // Filter out any that are already represented as PTY sessions
  // (named sessions that also show up as alive PTYs)
  for (const t of (tmuxItems || [])) {
    if (!t || !t.name) continue;
    // If there's already a PTY entry with a matching name/label, skip it
    const hasPty = live.some((s) => {
      const label = _sessionLabel(s);
      return label === t.name || s.name === t.name;
    });
    if (!hasPty) combined.push({ kind: 'tmux', data: t });
  }

  if (!combined.length) {
    host.innerHTML = '<div class="ft-empty">No running sessions</div>';
    return;
  }

  host.innerHTML = '';
  for (const item of combined) {
    if (item.kind === 'pty') {
      _renderPtyRow(item.data, host);
    } else {
      _renderTmuxRow(item.data, host);
    }
  }
  _refreshLucideIcons(host);
}

function _renderPtyRow(s, host) {
  const label = _sessionLabel(s);
  const attached = (s.attached_clients || 0) > 0;
  const isAgent = !!s.agent_driven;
  const row = document.createElement('div');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.className = 'ft-row';
  row.title = attached
    ? 'Open: ' + label + ' (attached on ' + s.attached_clients + ' client' + (s.attached_clients === 1 ? '' : 's') + ')'
    : 'Reattach: ' + label + (s.idle_secs != null ? ' (idle ' + _fmtIdle(s.idle_secs) + ')' : '');
  const dotCls = attached ? 'ft-row-dot ft-row-dot-on' : 'ft-row-dot';
  const meta = isAgent
    ? (s.launch_command || '')
    : (attached ? '' : (s.idle_secs != null ? _fmtIdle(s.idle_secs) + ' idle' : ''));
  const icon = isAgent ? 'bot' : 'square-terminal';
  const chip = isAgent
    ? '<span class="ft-agent-chip" title="Opened and driven by an agent">AGENT</span>'
    : '';
  row.innerHTML =
    '<i data-lucide="' + icon + '" class="lucide-icon ft-row-icon"></i>' +
    '<span class="ft-row-label">' + ftEscapeHtml(label) + '</span>' +
    chip +
    (meta ? '<span class="ft-row-meta">' + ftEscapeHtml(meta) + '</span>' : '') +
    '<span class="' + dotCls + '" title="' + (attached ? 'attached' : 'detached') + '"></span>';
  const open = () => openOrAttachTerminalSession(s.session_id, label);
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
  if (isAgent) {
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'ft-pause-btn' + (s.paused ? ' ft-pause-btn-on' : '');
    pause.textContent = s.paused ? 'Resume' : 'Pause';
    pause.title = s.paused
      ? 'Resume: let the agent drive this terminal again'
      : 'Pause the agent so you can take over typing';
    pause.addEventListener('click', async (e) => {
      e.stopPropagation();
      pause.disabled = true;
      try {
        await ftSetSessionPaused(s.session_id, !s.paused);
        ftLoadAllSessions();
      } catch (err) {
        pause.disabled = false;
      }
    });
    row.appendChild(pause);
  }
  // 3-dot "more" menu — rename or delete this session
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'ft-row-more';
  more.title = 'More — rename or delete';
  more.setAttribute('aria-label', 'Session actions');
  more.innerHTML = '<i data-lucide="more-vertical" class="lucide-icon"></i>';
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    _ftShowSessionMenu(s, label, more);
  });
  more.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
  });
  row.appendChild(more);
  host.appendChild(row);
}

function _renderTmuxRow(t, host) {
  // Use a div (role=button) so it can host child buttons (3-dot menu).
  const row = document.createElement('div');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.className = 'ft-row';
  row.title = "Attach: tmux attach -t '" + (t.name || '') + "'";
  const dot = t.attached ? 'ft-row-dot ft-row-dot-on' : 'ft-row-dot';
  row.innerHTML =
    '<i data-lucide="layers" class="lucide-icon ft-row-icon"></i>' +
    '<span class="ft-row-label">' + ftEscapeHtml(t.name) + '</span>' +
    '<span class="ft-row-meta">' + ftEscapeHtml(t.windows + (t.windows === 1 ? ' win' : ' wins')) + '</span>' +
    '<span class="' + dot + '" title="' + (t.attached ? 'attached' : 'detached') + '"></span>';
  const attach = () => {
    const cmd = _ftTmuxMouseOn("tmux attach -t '" + String(t.name).replace(/'/g, "'\\''") + "'");
    openNewTerminalTabWithCommand(cmd, t.name);
  };
  row.addEventListener('click', attach);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); attach(); }
  });

  // 3-dot "more" menu — rename or kill this tmux session
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'ft-row-more';
  more.title = 'More — rename or kill';
  more.setAttribute('aria-label', 'Tmux session actions');
  more.innerHTML = '<i data-lucide="more-vertical" class="lucide-icon"></i>';
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    _ftShowTmuxSessionMenu(t, more);
  });
  more.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
  });
  row.appendChild(more);
  host.appendChild(row);
}

// 3-dot row menu for a tmux session entry: rename + kill (two-click hazard in-menu).
function _ftShowTmuxSessionMenu(t, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  _openFloatingMenu([
    { icon: 'pencil', label: 'Rename…', action: () => _ftRenameTmuxSession(t) },
    // Kill starts as a normal danger item; first click arms it in-place
    { icon: 'trash-2', label: 'Kill session…', danger: true, action: null },
  ], rect.bottom + 2, rect.right - 180);

  // Wire the kill item (last danger button) with two-click hazard in the menu
  const menu = document.getElementById('files-floating-menu');
  if (!menu) return;
  const items = menu.querySelectorAll('.files-tab-menu-item.danger');
  const killBtn = items[items.length - 1];
  if (!killBtn) return;

  const killAction = async () => {
    try {
      await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name),
        { method: 'DELETE' });
      ftLoadAllSessions();
    } catch (e) {
      alert('Could not kill tmux session: ' + ((e && e.message) || e));
    }
  };

  // Replace the default click handler on the kill item (which auto-closes)
  // with one that stays open on first click, then confirms on second.
  // CloneNode removes the original listeners.
  const newKillBtn = killBtn.cloneNode(true);
  killBtn.parentNode.replaceChild(newKillBtn, killBtn);
  newKillBtn.className = 'files-tab-menu-item danger';
  newKillBtn.innerHTML =
    '<i data-lucide="trash-2" class="lucide-icon"></i><span>Kill session…</span><span class="files-tab-menu-check">✓</span>';
  _refreshLucideIcons(newKillBtn);
  newKillBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const state = newKillBtn.dataset.state || '';
    if (state === 'armed') {
      // Second click — confirm
      closeFloatingMenu();
      killAction();
    } else {
      // First click — arm in-place, stay open
      newKillBtn.dataset.state = 'armed';
      newKillBtn.classList.add('armed');
      newKillBtn.innerHTML =
        '<i data-lucide="alert-triangle" class="lucide-icon"></i><span>Confirm Kill</span>';
      _refreshLucideIcons(newKillBtn);
    }
  });
}

// Rename a tmux session via the backend tmux rename endpoint.
async function _ftRenameTmuxSession(t) {
  const raw = window.prompt('Rename tmux session "' + (t.name || '') + '":', t.name || '');
  if (raw == null) return;
  const name = raw.trim();
  if (!name || name === t.name) return;
  try {
    await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name) + '/rename',
      { method: 'POST', body: JSON.stringify({ name }) });
    ftLoadAllSessions();
  } catch (e) {
    alert('Could not rename tmux session: ' + ((e && e.message) || e));
  }
}

// Kill (delete) a tmux session via the backend tmux delete endpoint.
async function _ftDeleteTmuxSession(t) {
  if (!confirm('Kill tmux session "' + (t.name || '') + '"?\n\nThis kills the session and all its panes.')) return;
  try {
    await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name),
      { method: 'DELETE' });
    ftLoadAllSessions();
  } catch (e) {
    alert('Could not kill tmux session: ' + ((e && e.message) || e));
  }
}

async function _ftFetchAndRender(url, renderFn, errorElId) {
  try {
    const data = await apiFetch(url);
    renderFn(Array.isArray(data) ? data : []);
  } catch (e) {
    const host = document.getElementById(errorElId);
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}

async function ftLoadQuickLaunches() {
  await _ftFetchAndRender('/api/v1/terminal/quick-launches', ftRenderLaunches, 'ft-list-launches');
  _ftQuickLaunchesLoaded = true;
}



function _fmtIdle(secs) {
  if (secs == null) return '';
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h';
  return Math.floor(secs / 86400) + 'd';
}

function _sessionLabel(sess) {
  // Server-supplied name wins; then a local name this browser remembered; then
  // an open tab's name in this browser; then a shortened id.
  if (sess.name) return sess.name;
  const localName = getTerminalSessionName(sess.session_id);
  if (localName) return localName;
  const openTab = openTabs.find((t) => t.kind === 'terminal' && t.path === sess.session_id);
  if (openTab && openTab.name) return openTab.name;
  // session_id is 'terminal:<uuid>' — show the first 8 chars of the uuid.
  const raw = String(sess.session_id || '');
  const tail = raw.startsWith('terminal:') ? raw.slice(9) : raw;
  return tail.slice(0, 8) || 'session';
}

// ftRenderSessions removed — replaced by ftRenderAllSessions above

// 3-dot row menu for a "Your sessions" entry: rename + delete. Reuses the
// shared floating-menu component (same look as the terminal tab menu).
function _ftShowSessionMenu(s, label, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  _openFloatingMenu([
    { icon: 'pencil', label: 'Rename…', action: () => _ftRenameSession(s.session_id, label) },
    {
      icon: 'trash-2',
      label: (s.agent_driven ? 'Kill session…' : 'Delete…'),
      danger: true,
      action: () => _ftDeleteSession(s.session_id, label),
    },
  ], rect.bottom + 2, rect.right - 180);
}

// Rename a session from the sidebar. Names are per-user: we remember the new
// label locally (sidebar fallback + sent on future WS opens), update any tab
// open for it in THIS browser, and push it to the server so the user's other
// devices pick it up too. The server name is authoritative in _sessionLabel,
// so a session that already had a server/agent name still gets renamed.
async function _ftRenameSession(sessionId, currentLabel) {
  const next = prompt('Rename session:', currentLabel || '');
  if (next == null) return;                       // cancelled
  const name = next.trim();
  try { setTerminalSessionName(sessionId, name); } catch (_) {}
  const tab = openTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  if (tab) {
    if (name) tab.name = name;
    if (tab.instance && typeof tab.instance.setName === 'function') {
      try { tab.instance.setName(name); } catch (_) {}
    }
    renderTabs();
    persistTabs();
  }
  try {
    await apiFetch('/api/v1/terminal/sessions/' + encodeURIComponent(sessionId) + '/rename',
      { method: 'POST', body: JSON.stringify({ name }) });
  } catch (e) {
    // Older server without the rename route — the local rename above still
    // relabels this browser; degrade quietly.
    console.warn('Session rename (server) failed:', (e && e.message) || e);
  }
  ftLoadSessions();
}

// Delete (kill) a session from the sidebar. If a tab for it is open in this
// browser, closeTab kills the PTY and tears down the tab; otherwise we DELETE
// the session directly. Either way we drop the remembered name and refresh.
async function _ftDeleteSession(sessionId, label) {
  if (!confirm('Delete terminal session "' + label + '"?\n\n' +
               'This kills the running shell — anything unsaved in it is lost.')) return;
  const tab = openTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  try {
    if (tab && tab.instance) {
      await closeTab(sessionId);                  // kills the PTY + removes the tab
    } else {
      await apiFetch('/api/v1/terminal/sessions/' + encodeURIComponent(sessionId),
        { method: 'DELETE' });
    }
  } catch (e) {
    alert('Could not delete session: ' + ((e && e.message) || e));
  }
  try { setTerminalSessionName(sessionId, ''); } catch (_) {}
  ftLoadSessions();
}

// Take-over lock: pause/resume an agent's control of a terminal session.
async function ftSetSessionPaused(sessionId, paused) {
  return apiFetch(
    '/api/v1/terminal/sessions/' + encodeURIComponent(sessionId) + '/pause',
    { method: 'POST', body: JSON.stringify({ paused: !!paused }) },
  );
}

// Combined loader: fetches PTY sessions AND tmux sessions then renders them
// as one unified list with different icons (PTY = square-terminal, tmux = layers).
async function ftLoadAllSessions() {
  try {
    const [sessions, tmux] = await Promise.all([
      apiFetch('/api/v1/terminal/sessions'),
      apiFetch('/api/v1/terminal/tmux-sessions'),
    ]);
    ftRenderAllSessions(Array.isArray(sessions) ? sessions : [], Array.isArray(tmux) ? tmux : []);
  } catch (e) {
    const host = document.getElementById('ft-list-sessions');
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}
// Backward-compat so existing callers (pause button, delete, etc.) still work
async function ftLoadSessions() { return ftLoadAllSessions(); }

// Click handler for a "Your sessions" row. If we already have a tab open in
// this browser for that session_id, activate it; otherwise add a tab with the
// same id — the WebSocket layer reattaches to the live PTY automatically and
// replays scrollback.
function openOrAttachTerminalSession(sessionId, name) {
  if (!sessionId) return;
  const existing = openTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  if (existing) {
    activeTerminalId = sessionId;
    activateTab(sessionId);
  } else {
    pushTerminalTab(sessionId, name || undefined);
    activeTerminalId = sessionId;
    renderTabs();
    renderEditorPanes();
    persistTabs();
  }
  _switchToTerminalStrip();
}

function openTerminalLaunchersPanel() {
  // Quick launches are effectively static; load once per page lifetime.
  if (!_ftQuickLaunchesLoaded) ftLoadQuickLaunches();
  // Sessions + tmux refresh on every panel show, then poll every 5s while
  // the panel stays open. Sessions surface PTYs created on any device the
  // user is signed into, so opening this panel on a new device shows the
  // running shells from elsewhere and lets you reattach.
  ftLoadAllSessions();
  stopTerminalLaunchersPolling();
  _ftTmuxPollTimer = setInterval(() => {
    ftLoadAllSessions();
  }, 5000);
  // Wire the refresh button once. Repeat-safe: removeEventListener-then-add
  // would be wordy, so we use a sentinel attribute.
  const btn = document.getElementById('ft-refresh');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.title = 'Restart server & reload page';
    btn.addEventListener('click', async () => {
      if (btn.dataset.busy === '1') return;
      btn.dataset.busy = '1';
      const origTitle = btn.title;
      btn.title = 'Restarting server…';
      btn.classList.add('is-spinning');
      const ok = await restartServerAndReload();
      if (!ok) {
        btn.dataset.busy = '';
        btn.title = origTitle;
        btn.classList.remove('is-spinning');
        alert('Server did not come back within 60s. Check `journalctl -u webagent -f`.');
      }
    });
  }
  // Wire the "New terminal" primary action in the launcher panel. Replaces
  // the old standalone Terminal Tab strip icon.
  const newBtn = document.getElementById('ft-new-terminal');
  if (newBtn && !newBtn.dataset.wired) {
    newBtn.dataset.wired = '1';
    newBtn.addEventListener('click', () => openNewTerminalTab());
  }
  // "New tmux session" — prompts for a name, then opens a tab that runs
  // `tmux new -As <name>`. -A reattaches if a session by that name already
  // exists, so clicking the same button twice reattaches instead of
  // erroring. Sanitise the name to what tmux actually accepts.
  const newTmuxBtn = document.getElementById('ft-new-tmux');
  if (newTmuxBtn && !newTmuxBtn.dataset.wired) {
    newTmuxBtn.dataset.wired = '1';
    newTmuxBtn.addEventListener('click', () => openNewTmuxSessionDefault());
  }
}

function _sanitiseTmuxName(raw) {
  // tmux disallows '.', ':' and whitespace in session names. Collapse to
  // [A-Za-z0-9_-], replacing runs of unsupported chars with a single dash.
  return String(raw || '')
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

function promptNewTmuxSession() {
  // Suggest a fresh name like work-3 based on how many tmux tabs already exist.
  const existing = openTabs.filter((t) => t.kind === 'terminal' && !!t.tmuxSession);
  const suggested = 'work-' + (existing.length + 1);
  const raw = window.prompt(
    "New tmux session name (letters, numbers, '-' or '_'):",
    suggested,
  );
  if (raw == null) return;
  const name = _sanitiseTmuxName(raw);
  if (!name) {
    alert('Invalid name. Use letters, numbers, dashes, or underscores.');
    return;
  }
  // -A = attach if it already exists, otherwise create. Single-quote the
  // name; tmux session names can't contain a single quote, so no need to
  // escape further.
  openNewTerminalTabWithCommand(_ftTmuxMouseOn("tmux new -As '" + name + "'"), name);
}

// Open a new tmux session with an auto-generated default name — no naming
// dialog. Used by the "+" button's long-press (see initTabCarousel). Mirrors
// promptNewTmuxSession's create path but skips the prompt; returns the name so
// the caller can show it in the confirmation popup.
function openNewTmuxSessionDefault() {
  const existing = openTabs.filter((t) => t.kind === 'terminal' && !!t.tmuxSession);
  const name = _sanitiseTmuxName('work-' + (existing.length + 1)) || 'work';
  openNewTerminalTabWithCommand(_ftTmuxMouseOn("tmux new -As '" + name + "'"), name);
  return name;
}

// Small white confirmation popup. Mirrors the ability-tree ⚠ `.ac-save-pop`
// hazard callout (same little plate + pointing nub + lift-in) but WHITE — a
// confirmation, not a hazard — anchored just under `anchorEl`. Appended to
// <body> and FIXED-positioned so the tab bar's overflow:hidden can't clip it.
// Auto-removes after ~1.9s. Styled `.files-term-pop` in files.css.
let _termPopEl = null;
let _termPopTimer = null;
function _flashTermPop(anchorEl, text) {
  if (!anchorEl) return;
  try { if (_termPopEl) _termPopEl.remove(); } catch (_) {}
  if (_termPopTimer) { clearTimeout(_termPopTimer); _termPopTimer = null; }
  const pop = document.createElement('div');
  pop.className = 'files-term-pop';
  pop.textContent = text;
  document.body.appendChild(pop);
  const r = anchorEl.getBoundingClientRect();
  pop.style.left = Math.round(r.left + r.width / 2) + 'px';
  pop.style.top = Math.round(r.bottom + 8) + 'px';
  _termPopEl = pop;
  // Force a synchronous reflow so the opacity/transform transition actually
  // runs when we add `.show` — don't use requestAnimationFrame here, it's
  // paused in backgrounded/throttled tabs (ui-guidance gotcha).
  void pop.offsetWidth;
  pop.classList.add('show');
  _termPopTimer = setTimeout(() => {
    pop.classList.remove('show');
    setTimeout(() => {
      try { pop.remove(); } catch (_) {}
      if (_termPopEl === pop) _termPopEl = null;
    }, 220);
    _termPopTimer = null;
  }, 1900);
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
// Admin Tools layout. The originals are parked at the bottom of #stage
// in index.html so this module owns their final mount point. Idempotent.
export function relocateAdminToolsContainers() {
  // App Config — Settings view host
  const acHost = document.getElementById('files-settings-main');
  const acContainer = document.getElementById('app-config-container');
  if (acHost && acContainer && acContainer.parentElement !== acHost) {
    acHost.appendChild(acContainer);
    acContainer.removeAttribute('hidden');
  }
  // Database viewer — sidebar host receives #db-sidebar; the main host
  // receives #db-toolbar then #db-table-view. The empty #db-panel and
  // #db-viewer wrappers are dropped once their children have been moved.
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

  // Non-admin: show the Restricted Access overlay and stop. The backend rejects
  // every file/admin call on its own (app/api/files.py → _require_admin), so
  // this is the UX layer of the gate — and the header tab is normally hidden
  // for non-admins anyway (a deep link or stale tab can still land here).
  if (!isAdmin) {
    if (editor) editor.style.display = 'none';
    if (overlay) overlay.style.display = 'flex';
    const diag = document.getElementById('files-restricted-diag');
    if (diag) {
      diag.textContent = accessInfo.authenticated
        ? `Signed in as ${accessInfo.user_id || 'a non-admin account'}, which is not an admin.`
        : 'You are not signed in.';
    }
    // Full-screen the restricted view: hide the chat side panel (no agent
    // context to chat in here) so the main panel fills the window. Reverted on
    // leave (stopAdminTools) or once admin access is confirmed below.
    document.body.classList.add('admin-restricted');
    return;
  }

  // Admin confirmed — make sure the restricted full-screen layout is cleared
  // (e.g. an earlier non-admin check on this tab, now superseded).
  _clearRestrictedLayout();

  if (overlay) overlay.style.display = 'none';
  if (editor) editor.style.display = 'flex';

  // Build the strip from the authoritative page catalog. initFiles() built it
  // from whatever was cached/memoized synchronously; this re-runs once the boot
  // fetch has resolved so a freshly-dropped admin view is present before we
  // activate a view. Also injects each panel's collapse control.
  try {
    if (window.__loadPagesCatalog) {
      const cat = await window.__loadPagesCatalog();
      if (window.__buildAdminStrip) window.__buildAdminStrip(cat && cat.admin);
    }
  } catch (_) {}
  injectPanelCollapseButtons();

  // Re-apply the current sidebar view now that admin status is confirmed.
  // initFiles() called applySidebarView() before /check-access resolved, at
  // which point the cached isAdmin() may still have been false — so the
  // per-view side effects (git/terminal/database panels) were skipped by
  // the guard. Re-running here lets them fire for the real admin.
  const sb = document.getElementById('files-sidebar');
  const view = sb?.dataset.view || 'explorer';
  applySidebarView(view);

  // Load the production-mirror exclude list before the tree so dev-only badges
  // render on first paint, then load the tree and apply the dev/prod view mode.
  await loadProdExcludes();
  loadProdConfig();   // fire-and-forget — populates the More menu's folder field
  await loadRoot();
  applyProdViewClass();

  // Restore previously open tabs only once per session
  if (!openTabs.length) await restoreOpenTabs();

  // If a terminal tab is active, refit xterm — it can't measure a
  // display:none host while another main tab was active.
  const activeTermTab = getActiveTerminalTab();
  if (activeTermTab && activeTermTab.instance) {
    setTimeout(() => activeTermTab.instance.fit(), 30);
  }
}

// Exit the restricted full-screen layout and restore the user's saved chat
// visibility. Safe to call when not restricted (the class toggle is a no-op).
function _clearRestrictedLayout() {
  if (!document.body.classList.contains('admin-restricted')) return;
  document.body.classList.remove('admin-restricted');
  // Reassert the user's chat panel preference (the restricted CSS only forced
  // it hidden; this brings it back exactly as the user last left it).
  try { window.__applyChatVisible(window.__getChatVisible()); } catch (_) {}
}

export function stopAdminTools() {
  // Leaving Admin Tools: drop the restricted full-screen layout so the chat
  // panel returns on whatever tab the user switches to.
  _clearRestrictedLayout();
  // Stop the Explorer +/- badge poll (it resumes when Explorer is reopened).
  stopLineStatsAutoRefresh();
  // Quiet background loops; the view stays selected so polling resumes when the
  // user returns. Every admin view is a drop-in now, so the active view's
  // descriptor stop (e.g. Settings' stopView → stopAppConfig/stopBilling)
  // quiets its pollers.
  if (_activeDynAdminView) { try { _stopDynAdminView(_activeDynAdminView); } catch (_) {} }
}
