'use strict';

// Instances — drop-in Admin Tools view module.
//
// A grid of every WebAgent INSTANCE signed in to the shared database PLUS every
// cloud VM in a connected cloud account, styled after the main Agents page
// (ui/main-panel/agents/): each instance is a square tile; clicking one expands a
// tabbed detail region below it (the "Overview" tab). THIS device is always
// present (the backend inserts a self row even before its first heartbeat) and
// marked "This device".
//
// This page is the SINGLE HOME that absorbed two now-deleted admin pages plus the
// old Data Settings → Deployment section:
//   • the old "Database & Devices" page — its "This device" connection config
//     (database sign-in, secrets-vault sign-in, the silent-fallback warning) now
//     lives in a "Connections" section INSIDE the "This device" tile's Overview
//     (open the tile to reach it). The linked-devices list it showed is the tile
//     grid here.
//   • the old "Server Manager" page — its cloud VMs now appear as tiles in the
//     grid, with Start / Stop / Delete / Open / Ping in the open card's header
//     + a live log in each cloud tile's Overview; its cloud-ACCOUNT manager
//     (add a provider, paste its key)
//     also lives in the "This device" Overview's Connections section. Its manual
//     "machines" and "sites" (hand-typed boxes/URLs) and the shared-vs-local
//     explainer were intentionally dropped — this page is about auto-discovered
//     instances + cloud infrastructure you actually control, not a manual asset
//     inventory.
//   • Data Settings → Deployment's "local deployments" list split in two here:
//     (a) this app's Server reset + port editor → a "Server" section INSIDE the
//     "This device" Overview (_serverSectionHtml, self tile only), since those act
//     on the running app you're on; (b) any sibling WebAgent checkouts on this
//     machine → their own first-class grid TILES (kind:'local', _normalizeLocal /
//     _localTiles), with Start / Stop / Delete / Open in the card header +
//     inline edit in each tile's Overview (_renderLocalOverview) — managed just
//     like cloud VMs, and CREATED
//     from the New instance tile → New Deployment → "This device". Backed by the
//     same /admin/deploy/instances/* + /admin/storage/server/restart endpoints
//     (app/local_instances.py) the old section used — only the UI home moved;
//     deploy.js's own list/restart code is now orphaned (see its header). So the
//     whole deployment surface now sits with the fleet.
//
// Backed by this folder's OWN router — ui/main-panel/instances/server.py
// (mounted via page.json `router`), which consolidates everything the two old
// pages' backends did under one prefix:
//   GET  /admin/instances/devices           the presence registry (fleet tiles)
//   POST /admin/instances/device/rename      set a device's custom name / icon (shared DB)
//   POST /admin/instances/device/unlink      drop a stale (offline) device's row
//   GET  /admin/instances/backend-status     DB + vault health / shared-ness
//   GET  /admin/instances/providers          manageable cloud providers
//   GET  /admin/instances/cloud-instances    VMs in one cloud account (+repo overlay)
//   POST /admin/instances/connect|disconnect cloud-account sign-in / removal
//   POST /admin/instances/instance/action    start/stop/delete a VM (NDJSON stream)
//   POST /admin/instances/annotate|ping      set a VM's repo / reachability check
// The DB + vault CONNECT/TEST/ACTIVATE controls still call the shared, app-wide
// /admin/storage/* endpoints (never re-implemented here).
//
// INSTANT FIRST PAINT — the "New instance" create tile and the "This device" tile
// show immediately, with no network wait (last-known fleet cache, else a synthetic
// self tile from the browser), and This device's Overview is OPEN by default (a
// saved selection for another device/cloud/New tile overrides — see startView +
// _setActive). The full fleet + cloud VMs + connection health then load behind a
// spinner; the 20s poll refreshes presence silently. To keep this off the
// first-paint path the heavy New-Deployment machinery (deploy.js/dns.js) is NOT
// imported at load — it's lazily dynamic-imported only when the New instance tile
// is first opened (see _mountNewDeploy).
//
// Discovered via ui/main-panel/instances/page.json and driven generically by the
// admin shell (ui/shared/js/files.js applySidebarView). PANEL-LESS view.
// Styling: instances.css — self-contained (the tile grid + the in-Overview
// Connections section; the smb-/cvm- card/form primitives were folded in when the
// two old pages were removed). All colours come from the design-system palette (CSS variables only)
// — correct in dark + light.
// REMOVE-WHEN: the Instances view is dropped from the admin page catalog.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';
// Shared credential-entry popup (ui/credential-popup/) — the app-wide reusable
// credential form; the ↑ HTTPS button opens it when the device isn't linked yet.
import { openCredentialPopup } from '../../credential-popup/credential-popup.js';
import { createChatWidget } from '../../chat-widget/js/chat-widget.js';
import { app } from '../../shared/js/state.js';
// Shared searchable Lucide icon picker — the SAME popover the Agents page uses to
// pick an agent icon, so renaming/re-iconing an instance feels identical. Its CSS
// is duplicated into instances.css (this folder is self-contained), so the popover
// is styled here even though the Agents stylesheet isn't loaded on admin pages.
import { openIconPicker, ICON_PICKER_ICONS } from '../../shared/js/icon-picker.js';
import { applyRubberBand } from '../../shared/js/rubber-band.js';
import { showQrPopup, closeQrPopup } from '../../shared/js/qr-popup.js';
// The "New instance" tile's "New Deployment" tab is its own drop-in sub-feature
// (ui/main-panel/instances/new-deployment/) that owns the moved deploy/domain
// markup + the HEAVY deploy.js/dns.js glue (~3k lines). It is LAZY-loaded — a
// dynamic import fired only when the New instance tile is first opened (see
// _mountNewDeploy) — so that chain stays OFF this page's first-paint critical path:
// the tiles show instantly and the deploy machinery downloads on demand. We only
// need the synthetic tile id up-front, so it is duplicated here as a plain constant
// (MUST match new-deployment.js's NEW_DEPLOY_ID export) rather than pulling in the
// whole module just to name it.
//
// The card's SECOND tab, "New Instance" (the make-your-own-copy wizard), is the
// same drop-in shape — ui/main-panel/instances/new-instance/ — lazy-loaded the
// same way (_mountNewInstance / _prewarmNewInstance below), so it also stays off
// this page's first-paint critical path.
const NEW_DEPLOY_ID = '__new_deployment__';

// Resolved to new-deployment.js's mountNewDeployment on the first New-tile open.
let _mountNewDeploymentFn = null;
function _mountNewDeploy(host) {
  if (!host) return;
  if (_mountNewDeploymentFn) { _mountNewDeploymentFn(host); return; }
  import('./new-deployment/new-deployment.js')
    .then((m) => {
      _mountNewDeploymentFn = m.mountNewDeployment;
      // A re-render may have swapped the host while the module loaded — mount into
      // whichever host is live now.
      const live = document.getElementById('inst-nd-host');
      if (live) _mountNewDeploymentFn(live);
    })
    .catch((e) => console.error('[instances] new-deployment load failed', e));
}
// Warm the New-Deployment module + its HTML fragment in the BACKGROUND, a beat
// after the page's first paint (never on the critical path). By the time the admin
// opens the New instance tile the fragment is usually already built, so the
// skeleton swaps to the real bars with no wait. The heavy deploy.js/dns.js stay
// unloaded until a bar is actually touched. Runs at most once per session.
let _ndPrewarmed = false;
function _prewarmNewDeploy() {
  if (_ndPrewarmed) return;
  _ndPrewarmed = true;
  const go = () => import('./new-deployment/new-deployment.js')
    .then((m) => { _mountNewDeploymentFn = m.mountNewDeployment; try { m.prewarmNewDeployment && m.prewarmNewDeployment(); } catch {} })
    .catch(() => { _ndPrewarmed = false; });
  if (typeof requestIdleCallback === 'function') requestIdleCallback(go, { timeout: 3000 });
  else setTimeout(go, 1500);
}

// Resolved to new-instance.js's mountNewInstance on the first New-Instance tab open.
let _mountNewInstanceFn = null;
function _mountNewInstance(host) {
  if (!host) return;
  if (_mountNewInstanceFn) { _mountNewInstanceFn(host); return; }
  import('./new-instance/new-instance.js')
    .then((m) => {
      _mountNewInstanceFn = m.mountNewInstance;
      // A re-render may have swapped the host while the module loaded — mount into
      // whichever host is live now.
      const live = document.getElementById('inst-ni-host');
      if (live) _mountNewInstanceFn(live);
    })
    .catch((e) => console.error('[instances] new-instance load failed', e));
}
// Warm the New-Instance module + its HTML fragment in the BACKGROUND, on the same
// idle beat as the New-Deployment prewarm (never on the critical path). The wizard
// fragment is tiny, so by the time the admin opens the tab it is usually already
// built and mounts with no wait. Runs at most once per session.
let _niPrewarmed = false;
function _prewarmNewInstance() {
  if (_niPrewarmed) return;
  _niPrewarmed = true;
  const go = () => import('./new-instance/new-instance.js')
    .then((m) => { _mountNewInstanceFn = m.mountNewInstance; try { m.prewarmNewInstance && m.prewarmNewInstance(); } catch {} })
    .catch(() => { _niPrewarmed = false; });
  if (typeof requestIdleCallback === 'function') requestIdleCallback(go, { timeout: 3000 });
  else setTimeout(go, 1500);
}

// ── State ────────────────────────────────────────────────────────────────────
// devices : last /devices payload (null = never loaded). cloud : normalized cloud
// VM tiles (merged into the grid). providers : cloud targets from /providers.
// active/tab : the open tile + its detail tab. status/config : /backend-status +
// /admin/storage/config (DB+vault) — the connection details now live INSIDE the
// "This device" tile's Overview (there is no longer a top connection strip).
// open/sel : DB/vault card expand + provider-dropdown choice. acct* : cloud-account
// manager expand/pending sets. cloudBusy : a Start/Stop/Delete is streaming.
const S = {
  wired: false, devices: null, self: '', cloud: [], providers: null,
  active: null, tab: 'overview', poll: null, busy: false, loading: false,
  status: null, config: null,
  open: { db: false, vault: false }, sel: { db: null, vault: null },
  acctExpanded: new Set(), acctAdding: new Set(), cloudBusy: false,
  // Per-device Cloud Run build status. Kept outside the DOM so opening another
  // tile and returning does not erase a long-running build log.
  cloudRunDeploy: new Map(),
    // (headfullUrls map removed — the headfull tunnel is now fire-and-forget)
  cloudLoading: false,   // true while _loadCloud is fetching (shows skeleton in overview)
  // HTTPS state per cloud VM, keyed by d._ref. Each entry: {state, text, status,
  // statusKind, certHtml, log}. Auto-checked on tile open; persisted across
  // re-renders so "Active" stays active until the user explicitly checks again.
  httpsState: new Map(),
  // _ref of the cloud VM whose destructive Delete is awaiting a typed-name
  // confirm (null = no delete armed). See _delConfirmHtml / _onInput.
  confirmDel: null,
  // annotation_key whose tunnel URL removal is awaiting confirmation (null = none).
  // While set, the background poll skips re-render so the inline confirm buttons survive.
  tunnelRemoveConfirm: null,
  // Canonical URL + redirect toggle — set by /admin/instances/canonical-url
  // (POST with no url/redirect_enabled fields reads current state). The UI shows
  // which URL is primary and whether non-canonical hostnames get 301'd to it.
  canonicalUrl: null, canonicalRedirect: false, canonicalExcluded: [],
  // URL rows in the Overview's URL section that are expanded to reveal their
  // copy/QR action row, keyed by "url:"/"custom:" + the URL string. Kept in state
  // (not just the DOM) so re-renders and the 10 s URL smart-poll don't collapse an
  // open row under the user's cursor.
  openUrls: new Set(),
  // instance_id whose name is being inline-edited right now (null = none). While
  // set, the background poll skips its re-render so it can't blow away the live
  // contenteditable field mid-type (see the poll guard in startView).
  editing: null,
  // Local WebAgent server(s) on this machine (moved here from Data Settings →
  // Deployment). `local` = last /admin/deploy/instances payload ({ instances:[hub +
  // siblings], hub_port }): the hub drives the "This device" Server card (restart +
  // port), the non-hub siblings become their own grid TILES (_localTiles). localBusy
  // = a start/stop/restart/relaunch is streaming (poll skips so the live log
  // survives); localEditing = a Server-card or local-tile edit field has focus (poll
  // skips so typing isn't clobbered).
  local: null, localBusy: false, localEditing: false,
  // Overview smart-poll — fetches fresh device data and patches URL facts in-place
  // without a full re-render. Timer = interval handle; data = last URLs snapshot
  // keyed by instance_id for change detection.
  _overviewPollTimer: null, _overviewPollData: null,
  // Device→cloud "Link this device to its cloud VM" panel state. open = panel
  // visible; mode = 'summary' (show already-saved credentials first) | 'form'
  // (show the login form, prefilled); provider = the provider the form is on;
  // fromSummary = the form was opened from a saved-credentials row (so Cancel
  // returns to the summary instead of closing the panel); note/noteKind = a
  // one-line status at the top of the panel ("no credential saved yet", or an
  // honest "connected, but this device doesn't match any VM" message);
  // pendingDomain = the domain an ↑ HTTPS click queued while the panel opened,
  // so "Use saved" can continue straight into HTTPS once the device is linked.
  _deviceConnectOpen: false, _deviceConnectMode: 'summary', _deviceConnectProvider: '',
  _deviceConnectFromSummary: false,
  _deviceConnectNote: '', _deviceConnectNoteKind: '', _deviceConnectPendingDomain: '',
};

const _TABS = [['overview', 'Overview'], ['dashboard', 'Dashboard'], ['settings', 'Configuration'], ['users', 'Users']];

// Instant-paint plumbing (device tile only — cloud + health are always live).
const _STUB_ID = 'this-device';
const _CACHE_KEY = 'inst.devices.v1';
const _SELF_KEY = 'inst.selfId.v1';
// Which tile was open last visit — but ONLY a selection OTHER than "This device"
// (another device, a cloud VM, the New instance tile) is remembered. Opening This
// device or closing the panel clears it, so the next load falls back to the default
// of opening This device. Absent = no other selection → default. See startView.
const _ACTIVE_KEY = 'inst.active.v1';
// Which tab and scroll/search state was active in the Configuration tab, so the
// user returns to the same sub-section + search results they left. Cleared when
// switching away from "This device" or closing the tile.
const _CONFIG_STATE_KEY = 'inst.config.v1';

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _root() { return document.getElementById('inst-root'); }
function _cssEsc(v) { return (window.CSS && CSS.escape) ? CSS.escape(v) : v; }

const _FETCH_TIMEOUT_MS = 30000;  // 30 s ceiling for any API call — prevents a hung request from freezing the page

async function _get(path) {
  const controller = new AbortController();
  const timer = setTimeout(function() { controller.abort(); }, _FETCH_TIMEOUT_MS);
  try {
    const sep = path.includes('?') ? '&' : '?';
    const res = await fetch(apiPath(path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())), { signal: controller.signal });
    let body = null;
    try { body = await res.json(); } catch {}
    return { ok: res.ok, status: res.status, body };
  } catch (e) {
    if (e && e.name === 'AbortError') return { ok: false, status: 0, body: null };
    return { ok: false, status: 0, body: null };
  } finally {
    clearTimeout(timer);
  }
}
async function _post(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(function() { controller.abort(); }, _FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(apiPath(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _uid(), ...(payload || {}) }),
      signal: controller.signal,
    });
    let body = null;
    try { body = await res.json(); } catch {}
    return { ok: res.ok, status: res.status, body };
  } catch (e) {
    if (e && e.name === 'AbortError') return { ok: false, status: 0, body: null };
    return { ok: false, status: 0, body: null };
  } finally {
    clearTimeout(timer);
  }
}

// ── Instant-paint helpers ─────────────────────────────────────────────────────
function _selfId() { try { return localStorage.getItem(_SELF_KEY) || _STUB_ID; } catch { return _STUB_ID; } }
function _rememberSelfId(id) { if (id) { try { localStorage.setItem(_SELF_KEY, id); } catch {} } }
function _readCache() { try { return JSON.parse(sessionStorage.getItem(_CACHE_KEY) || 'null'); } catch { return null; } }
function _writeCache(p) { try { sessionStorage.setItem(_CACHE_KEY, JSON.stringify(p)); } catch {} }

// Saved open-tile memory (only non-self selections are stored — see _ACTIVE_KEY).
function _readActive() { try { return localStorage.getItem(_ACTIVE_KEY) || null; } catch { return null; } }
function _saveActive(v) { try { if (v) localStorage.setItem(_ACTIVE_KEY, v); else localStorage.removeItem(_ACTIVE_KEY); } catch {} }
// ── Configuration tab state (tab, scroll, search text) ────────────────────────
function _saveConfigState(state) { try { localStorage.setItem(_CONFIG_STATE_KEY, JSON.stringify(state)); } catch {} }
function _readConfigState() { try { return JSON.parse(localStorage.getItem(_CONFIG_STATE_KEY) || 'null'); } catch { return null; } }
function _clearConfigState() { try { localStorage.removeItem(_CONFIG_STATE_KEY); } catch {} }
function _saveConfigField(key, value) {
  try {
    const state = _readConfigState() || {};
    state[key] = value;
    localStorage.setItem(_CONFIG_STATE_KEY, JSON.stringify(state));
  } catch {}
}
// Set the open tile AND persist the choice so the page reopens where you left it.
// Opening This device (any of its id forms) or closing (null) clears the memory,
// leaving the default — open This device — to apply on the next load. _render() is
// the caller's responsibility (matches the old direct S.active writes).
function _setActive(id) {
  S.active = id;
  // Clear HTTPS state when switching to a different tile (it belongs to the
  // previous cloud VM's Overview section).
  S.https = { state: 'off', text: 'Not enabled', status: '', statusKind: '', certHtml: '', log: '' };
  const selfIds = new Set([S.self, _selfId(), _STUB_ID].filter(Boolean));
  _saveActive((id == null || selfIds.has(id)) ? null : id);
  // Clear saved config state when closing tile or switching to a different device
  if (id == null || !selfIds.has(id)) _clearConfigState();
}

function _navPlatform() {
  try { const uad = navigator.userAgentData; if (uad && uad.platform) return uad.platform; } catch {}
  try { return navigator.platform || navigator.userAgent || ''; } catch { return ''; }
}
function _selfStub() {
  let origin = '';
  try { origin = location.origin || ''; } catch {}
  return {
    kind: 'device', instance_id: _selfId(), label: 'This device', online: true, is_self: true,
    platform: _navPlatform(), endpoint: origin, repo: '', branch: '',
    last_seen: new Date().toISOString(), _stub: true,
  };
}

// ── Small helpers ────────────────────────────────────────────────────────────
function _platformKey(platform) {
  const p = (platform || '').toLowerCase();
  if (p === 'cloud') return 'cloud';
  if (p.includes('win')) return 'win';
  if (p.includes('darwin') || p.includes('mac')) return 'mac';
  if (p.includes('linux')) return 'linux';
  return 'other';
}
function _platformIcon(platform) {
  switch (_platformKey(platform)) {
    case 'cloud': return 'cloud';
    case 'win': return 'monitor';
    case 'mac': return 'laptop';
    case 'linux': return 'terminal';
    default: return 'cpu';
  }
}
// ── Display name + icon resolvers ─────────────────────────────────────────────
// A device tile's name/icon prefer the admin's shared overrides (custom_label /
// custom_icon, set from this page and stored on the device_presence row so every
// device shows the same) and fall back to the machine's self-reported hostname /
// platform icon. Cloud tiles never carry overrides, so they resolve to their
// existing name / cloud icon unchanged.
function _instName(d) { return (d.custom_label || '').trim() || d.label || d.instance_id; }
// Is this tile's name/icon editable via the shared device-rename? Only real
// shared-registry DEVICE rows — never cloud VMs (name comes from the provider),
// local checkouts (renamed via their own overview fields), nor the pre-heartbeat stub.
function _instEditable(d) { return d.kind !== 'cloud' && d.kind !== 'local' && !d._stub; }
// The chip's inner glyph markup. A custom icon wins: a known Lucide name renders as
// an icon; anything else (an emoji / short text a future picker might allow) renders
// as sized text. With no override, the platform-derived Lucide icon shows.
function _instIconInner(d) {
  const ic = (d.custom_icon || '').trim();
  if (ic) {
    if (ICON_PICKER_ICONS.includes(ic)) return '<i data-lucide="' + _escAttr(ic) + '"></i>';
    return '<span class="inst-emoji-icon">' + _esc(ic) + '</span>';
  }
  if (d.kind === 'local') return '<i data-lucide="folder-cog"></i>';   // a checkout folder on this machine
  return '<i data-lucide="' + _platformIcon(d.platform) + '"></i>';
}
function _openUrl(addr) {
  const a = String(addr || '').trim();
  if (!a) return '';
  return /^https?:\/\//i.test(a) ? a : 'http://' + a;
}
function _repoUrl(repo) {
  const r = String(repo || '').trim();
  if (/^https?:\/\//i.test(r)) return r;
  if (/^git@github\.com:/i.test(r)) return 'https://github.com/' + r.replace(/^git@github\.com:/i, '').replace(/\.git$/i, '');
  if (/^[\w.-]+\/[\w.-]+$/.test(r)) return 'https://github.com/' + r.replace(/\.git$/i, '');
  return r;
}
function _relTime(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
// Untruncated twin of _relTime — same rounding rule, but spells the unit out
// ("12 minutes ago" instead of "12m ago"). Used for the click-to-expand label.
function _relTimeFull(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s === 1 ? '1 second ago' : s + ' seconds ago';
  if (s < 3600) { const m = Math.round(s / 60); return m === 1 ? '1 minute ago' : m + ' minutes ago'; }
  if (s < 86400) { const h = Math.round(s / 3600); return h === 1 ? '1 hour ago' : h + ' hours ago'; }
  const d = Math.round(s / 86400); return d === 1 ? '1 day ago' : d + ' days ago';
}
function _fmtDate(iso) {
  try {
    const dt = new Date(iso);
    if (isNaN(dt.getTime())) return iso;
    return dt.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  } catch { return iso; }
}
// Google Compute states → friendly label + status-dot class (cloud tiles).
function _statusMeta(status) {
  switch ((status || '').toUpperCase()) {
    case 'RUNNING': return { label: 'Running', dot: 'cvm-dot-run', online: true };
    case 'TERMINATED': return { label: 'Stopped', dot: 'cvm-dot-stop', online: false };
    case 'SUSPENDED': return { label: 'Suspended', dot: 'cvm-dot-stop', online: false };
    case 'STOPPING': return { label: 'Stopping…', dot: 'cvm-dot-busy', online: false };
    case 'SUSPENDING': return { label: 'Suspending…', dot: 'cvm-dot-busy', online: false };
    case 'PROVISIONING': return { label: 'Starting…', dot: 'cvm-dot-busy', online: false };
    case 'STAGING': return { label: 'Starting…', dot: 'cvm-dot-busy', online: false };
    case 'REPAIRING': return { label: 'Repairing…', dot: 'cvm-dot-busy', online: false };
    default: return { label: status || 'Unknown', dot: 'cvm-dot-stop', online: false };
  }
}

// Host part of a URL / host[:port] string, lower-cased ('' if none). Used to
// match a device's reported endpoint against a cloud VM's public IP.
function _hostOf(url) {
  const s = String(url || '').trim();
  if (!s) return '';
  try { return new URL(/^[a-z]+:\/\//i.test(s) ? s : 'http://' + s).hostname.toLowerCase(); }
  catch { return s.replace(/^[a-z]+:\/\//i, '').split(/[:/?#]/)[0].toLowerCase(); }
}
// Is this registry device the SAME machine as this cloud VM? A deployed VM that
// also runs WebAgent against the shared DB appears in BOTH lists; we fold them
// into one tile. Match on the strongest signal available:
//   • the device's reported endpoint host equals the VM's public IP, or
//   • the device name (its OS hostname) equals the VM name — cloud guests default
//     their hostname to the instance name, so this catches the common case even
//     before an endpoint has been recorded.
function _deviceMatchesVm(dev, vm) {
  const vmIp = String(vm.ip || '').trim().toLowerCase();
  if (vmIp && _hostOf(dev.endpoint) === vmIp) return true;
  const vmName = String(vm.name || '').trim().toLowerCase();
  const devName = String(dev.label || '').trim().toLowerCase();
  if (vmName && devName && devName === vmName) return true;
  return false;
}

// Merge the two sources into ONE fleet list: each cloud VM that has a registry
// twin absorbs the twin's shared-DB membership (fleet flag + the app's own URL /
// repo), and the twin is dropped so it isn't shown a second time. Recomputed on
// every call (flags reset first) so a twin appearing/vanishing is always reflected.
//
// SELF DEVICE ON A CLOUD VM: when the "This device" tile matches a cloud VM (e.g.
// a Google Cloud VM running WebAgent), the cloud VM's metadata is attached to the
// self device as `_cloudVm` and the cloud VM is removed from the list — the self
// tile becomes a hybrid that shows its normal device UI plus cloud actions and
// HTTPS / SSL config.
function _mergeFleet(devices, cloud) {
  cloud.forEach(vm => { vm.fleet = false; vm.fleet_online = false; });
  const claimed = new Set();
  const consumedCloud = new Set();
  cloud.forEach((vm, i) => {
    const twin = devices.find(d =>
      !claimed.has(d.instance_id) && _deviceMatchesVm(d, vm));
    if (!twin) return;

    if (twin.is_self) {
      // Self device is running on this cloud VM — attach the VM's infrastructure
      // metadata to the self tile so its Overview can show cloud actions and HTTPS.
      twin._cloudVm = {
        provider: vm.provider,
        providerName: vm.providerName,
        project: vm.project || '',
        zone: vm.zone,
        name: vm.name,
        status: vm.status,
        machine_type: vm.machine_type,
        ip: vm.ip,
        created: vm.created,
        is_this_app: vm.is_this_app,
        is_webagent: vm.is_webagent,
        annotation_key: vm.annotation_key,
        domain: vm.domain,
        domains: vm.domains,
        _ref: vm._ref,
      };
      consumedCloud.add(i);
      if (!twin.endpoint && vm.ip) twin.endpoint = 'http://' + vm.ip;
      if (!twin.repo && vm.repo) twin.repo = vm.repo;
      if (vm.domains && vm.domains.length) twin.domains = vm.domains;
      // Don't claim the self — it stays in the device list with its own identity
    } else {
      claimed.add(twin.instance_id);
      vm.fleet = true;
      vm.fleet_online = !!twin.online;
      vm.device_id = twin.instance_id;
      if (twin.last_seen) vm.last_seen = twin.last_seen;
      if (twin.endpoint) vm.endpoint = twin.endpoint;   // the app's own reported URL beats http://<ip>
      if (!vm.repo && twin.repo) vm.repo = twin.repo;
      if (twin.branch && !vm.branch) vm.branch = twin.branch;
      if (twin.tunnel) vm.tunnel = twin.tunnel;
      // Diffstat rides along only when it describes the SAME repo being shown —
      // an admin-annotated repo that differs from the twin's would be a lie.
      if (twin.repo_stats && (!vm.repo || vm.repo === twin.repo)) vm.repo_stats = twin.repo_stats;
      if (!vm.domains && twin.domains) vm.domains = twin.domains;         // its published Remote Access tunnel state
    }
  });
  const keptDevices = devices.filter(d => !claimed.has(d.instance_id));
  const keptCloud = cloud.filter((_, i) => !consumedCloud.has(i));
  return [...keptDevices, ...keptCloud];
}

// Is this tile joined to the SHARED database? Mirrors _dbShareValue's verdict:
//   • a cloud VM is shared only when it correlates to a registry twin (d.fleet);
//   • a registry device rides the app's own DB, whose shared-ness is the fleet-wide
//     /backend-status fact (remote Postgres = shared; local SQLite = not).
function _isShared(d) {
  if (d.kind === 'cloud') return !!d.fleet;
  if (d.kind === 'local') return false;   // a sibling checkout runs its own separate DB
  return !!(S.status && S.status.db && S.status.db.shared);
}

// Fixed tile order (the "New instance" tile is prepended separately, so it stays
// left of everything): This device always first, then shared-DB instances, then
// unshared ones — each of the two groups sorted alphabetically by display name.
function _sortFleet(list) {
  const name = d => String(_instName(d) || '').toLowerCase();
  const rank = d => d.is_self ? 0 : (_isShared(d) ? 1 : 2);
  return list.slice().sort((a, b) =>
    (rank(a) - rank(b)) || name(a).localeCompare(name(b)));
}

// Local sibling checkouts (other WebAgent folders on THIS machine, each on its own
// port) as fleet tiles — the NON-builtin entries from /admin/deploy/instances. The
// builtin one is the hub = this device, already the self tile, so it's skipped here.
// They get Start / Stop / Delete / Open on their own tile, just like cloud VMs; new
// ones are created from the New instance tile → New Deployment → "This device".
function _normalizeLocal(i) {
  return {
    kind: 'local', instance_id: 'local:' + i.id, local_id: i.id,
    label: i.label, online: i.status === 'running', platform: 'local',
    status: i.status, port: i.port, folder: i.folder, url: i.url || '',
    endpoint: i.url || '', is_checkout: i.is_checkout, port_aware: i.port_aware,
  };
}
function _localTiles() {
  const insts = (S.local && S.local.instances) || [];
  return insts.filter(i => !i.builtin).map(_normalizeLocal);
}

// The whole fleet: registry devices + cloud VM tiles (deduped, see _mergeFleet) +
// local checkout tiles, then ordered (see _sortFleet).
function _list() {
  return _sortFleet([
    ..._mergeFleet(((S.devices && S.devices.devices) || []), S.cloud || []),
    ..._localTiles(),
  ]);
}
function _find(id) { return _list().find(d => d.instance_id === id) || null; }
// True while the active tile's data source hasn't loaded yet, so a completed load of
// a DIFFERENT source (devices/cloud finishing before local) must not treat it as
// vanished and close it. Only local tiles start from a null source (S.cloud is []).
function _activePending() { return String(S.active || '').startsWith('local:') && S.local === null; }

// ═══════════════════════════════════════════════════════════════════════════
// Provider field specs (keys MUST match the /admin/storage endpoints) — ported
// verbatim from the Database & Devices page so the DB/vault forms behave the same.
// ═══════════════════════════════════════════════════════════════════════════
const DB_PROVIDERS = [
  { v: 'sqlite', t: 'SQLite (local file)' },
  { v: 'postgres', t: 'Postgres' },
  { v: 'aws_rds', t: 'Amazon RDS' },
  { v: 'gcp_cloud_sql', t: 'Google Cloud SQL' },
  { v: 'azure_postgres', t: 'Azure Database' },
  { v: 'neon', t: 'Neon' },
];
const _PG_FIELDS = [
  { key: 'host', label: 'Host', type: 'text', required: true },
  { key: 'port', label: 'Port', type: 'number', placeholder: '5432', half: true },
  { key: 'database', label: 'Database name', type: 'text', required: true },
  { key: 'username', label: 'Username', type: 'text', required: true },
  { key: 'password', label: 'Password', type: 'password', keep: true },
];
const DB_FIELDS = {
  sqlite: [],
  postgres: [..._PG_FIELDS, { key: 'ssl_mode', label: 'SSL mode', type: 'select', options: ['disable', 'require', 'verify-ca', 'verify-full'], half: true }],
  aws_rds: _PG_FIELDS,
  gcp_cloud_sql: _PG_FIELDS,
  azure_postgres: _PG_FIELDS,
  neon: _PG_FIELDS,
};
const DB_NOTE = {
  sqlite: 'A single local file on this device. Zero setup — but local-only: nothing is shared with other devices.',
  postgres: 'Any Postgres you run. Shared across every device that signs in to it.',
  aws_rds: 'Managed Postgres on AWS (RDS / Aurora). Shared across devices.',
  gcp_cloud_sql: 'Managed Postgres on Google Cloud. Shared across devices.',
  azure_postgres: 'Managed Postgres on Azure (Flexible Server). Shared across devices.',
  neon: 'Serverless Postgres with scale-to-zero. Shared across devices.',
};

const VAULT_PROVIDERS = [
  { v: 'inline_db', t: 'App DB (default, plaintext)' },
  { v: 'env', t: 'Environment variables (read-only)' },
  { v: 'os_keyring', t: 'OS Keyring (this machine only)' },
  { v: 'hashicorp_vault', t: 'HashiCorp Vault' },
  { v: 'azure_key_vault', t: 'Azure Key Vault' },
  { v: 'gcp_secret_manager', t: 'GCP Secret Manager' },
  { v: 'aws_secrets_manager', t: 'AWS Secrets Manager' },
];
const VAULT_FIELDS = {
  hashicorp_vault: [
    { key: 'address', label: 'Vault address', placeholder: 'https://vault.example.com:8200', required: true },
    { key: 'token', label: 'Vault token', secret: true, placeholder: 's.xxxxxxxx' },
    { key: 'kv_mount', label: 'KV mount', placeholder: 'secret', optional: true, half: true },
    { key: 'kv_prefix', label: 'Key prefix', placeholder: 'webagent', optional: true, half: true },
  ],
  azure_key_vault: [
    { key: 'vault_url', label: 'Key Vault URL', placeholder: 'https://my-vault.vault.azure.net', required: true },
    { key: 'tenant_id', label: 'Tenant ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'client_id', label: 'Client ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'token', label: 'Client secret', secret: true, placeholder: '(optional)', optional: true },
  ],
  gcp_secret_manager: [
    { key: 'project', label: 'GCP project ID', placeholder: 'my-gcp-project', required: true },
    { key: 'token', label: 'Service-account key (JSON)', secret: true, textarea: true, optional: true, placeholder: 'Paste the whole service-account JSON, or leave blank to use the host\'s default Google credentials.' },
  ],
  aws_secrets_manager: [
    { key: 'region', label: 'AWS region', placeholder: 'us-east-1', required: true },
    { key: 'access_key_id', label: 'Access key ID', placeholder: '(optional)', optional: true, half: true },
    { key: 'token', label: 'Secret access key', secret: true, placeholder: '(optional)', optional: true, half: true },
  ],
};
const VAULT_NOTE = {
  inline_db: 'Secrets stored in the app database (plaintext). Fine for local dev — but on a shared remote DB this traps the DB password inside the DB it unlocks. Upgrade before production.',
  env: 'Secrets injected by your deployment as environment variables. Read-only — the app can\'t store new tokens.',
  os_keyring: 'This machine\'s own credential store. Strong on one box, but per-machine: other devices can\'t share these secrets.',
  hashicorp_vault: 'A Vault server you run (or HCP Vault). Cloud-agnostic and shared across every device.',
  azure_key_vault: 'Managed secrets in Azure, shared across devices.',
  gcp_secret_manager: 'Managed secrets in Google Cloud, shared across every device — the right pick for a multi-device setup.',
  aws_secrets_manager: 'Managed secrets in AWS, shared across devices.',
};

function _provLabel(list, v) { const o = list.find(x => x.v === v); return o ? o.t : v; }

// One labelled input/select/textarea for a DB or vault connect form (smb- styling).
function _fieldHtml(scope, f, savedVal) {
  const id = 'inst-f-' + scope + '-' + _escAttr(f.key);
  const req = (f.required && !f.keep && !f.secret) ? ' <span class="smb-req">*</span>' : '';
  const half = f.half ? ' smb-field-half' : '';
  let control;
  if (f.type === 'select') {
    control = '<select id="' + id + '" class="smb-input" data-fk="' + _escAttr(f.key) + '">'
      + (f.options || []).map(o => '<option value="' + _escAttr(o) + '"' + (o === savedVal ? ' selected' : '') + '>' + _esc(o) + '</option>').join('')
      + '</select>';
  } else if (f.textarea) {
    const ph = (f.secret && f.keep) ? 'saved — leave blank to keep' : (f.placeholder || '');
    control = '<textarea id="' + id + '" class="smb-input smb-textarea" rows="3" data-fk="' + _escAttr(f.key)
      + '" data-secret="' + (f.secret ? '1' : '0') + '" placeholder="' + _escAttr(ph) + '"></textarea>';
  } else {
    const isSecret = f.secret || f.type === 'password';
    const keepPh = (isSecret || f.keep) ? 'saved — leave blank to keep' : (f.placeholder || '');
    const type = isSecret ? 'password' : (f.type === 'number' ? 'number' : 'text');
    const val = (!isSecret && savedVal != null) ? ' value="' + _escAttr(savedVal) + '"' : '';
    control = '<input id="' + id + '" class="smb-input" type="' + type + '" data-fk="' + _escAttr(f.key) + '"'
      + ' data-secret="' + (f.secret ? '1' : '0') + '"' + val
      + ' placeholder="' + _escAttr((isSecret || f.keep) ? keepPh : (f.placeholder || '')) + '"'
      + ' autocomplete="off" spellcheck="false">';
  }
  return '<div class="smb-field' + half + '"><label class="smb-label" for="' + id + '">' + _esc(f.label) + req + '</label>' + control + '</div>';
}

// ═══════════════════════════════════════════════════════════════════════════
// Connection cards — "This device" DB + vault sign-in, the silent-fallback
// warning, and the cloud-account manager. These used to be a collapsible strip at
// the top of the grid; they now render INSIDE the "This device" tile's Overview
// (see _renderDeviceOverview). The degraded banner still floats at the top of the
// grid as an always-visible alert; its "fix" button opens this device's Overview.
// ═══════════════════════════════════════════════════════════════════════════
function _connDot(reachable, busy) {
  const cls = busy ? 'cvm-dot-busy' : reachable ? 'cvm-dot-run' : 'cvm-dot-stop';
  return '<span class="cvm-dot ' + cls + '"></span>';
}

function _degradedBannerHtml() {
  const db = (S.status && S.status.db) || {};
  if (!db.degraded) return '';
  const msg = db.fallback_message
    || 'Couldn\'t reach the shared database, so the app is running on this device\'s local copy.';
  const detail = db.fallback_detail
    ? '<span class="smb-degraded-detail">' + _esc(db.fallback_detail) + '</span>' : '';
  return '<div class="smb-degraded">'
    + '<i data-lucide="database-zap"></i>'
    + '<div><span class="smb-degraded-title">Not connected to the shared database</span><br>'
    +   _esc(msg) + detail
    +   '<button class="smb-btn" data-act="fix-db">Open database sign-in</button>'
    + '</div></div>';
}

// The connection cards, stacked — rendered inside the "This device" Overview.
// DB sign-in, secrets-vault sign-in, then the cloud-account manager.
function _connCardsHtml() {
  return '<div class="inst-conn-section">'
    + '<div class="inst-conn-section-head"><i data-lucide="database"></i><span>Connections</span></div>'
    + '<div class="inst-conn-section-hint">This device\'s database and secrets vault, plus any cloud accounts whose servers appear as tiles here.</div>'
    + '<div class="inst-conn-cards">'
    +   _dbCardHtml()
    +   _vaultCardHtml()
    +   _cloudAccountsHtml()
    + '</div>'
    + '</div>';
}

// ── Database card ────────────────────────────────────────────────────────────
function _dbCardHtml() {
  const st = S.status || {};
  const db = st.db || {};
  const provLabel = _provLabel(DB_PROVIDERS, db.provider || 'sqlite');
  const reachTxt = db.degraded ? 'On local copy — not connected'
    : db.reachable ? 'Connected' : (db.detail || 'Unreachable');
  const sharedBadge = db.shared
    ? '<span class="smb-badge smb-badge-shared">Shared</span>'
    : '<span class="smb-badge smb-badge-local">Local only</span>';
  const head =
      '<div class="smb-card-head" data-toggle="db">'
    +   _connDot(db.degraded ? false : db.reachable, S.busy)
    +   '<i data-lucide="database" class="smb-card-icon"></i>'
    +   '<div class="smb-card-titles"><div class="smb-card-title">Database</div>'
    +     '<div class="smb-card-sub">' + _esc(provLabel) + ' · ' + _esc(db.target || '') + '</div></div>'
    +   sharedBadge
    +   '<span class="smb-card-status">' + _esc(reachTxt) + '</span>'
    +   '<i data-lucide="chevron-' + (S.open.db ? 'down' : 'right') + '" class="smb-chevron"></i>'
    + '</div>';
  const body = S.open.db ? '<div class="smb-card-body">' + _dbFormHtml() + '</div>' : '';
  return '<div class="smb-card' + (S.open.db ? ' smb-open' : '') + '">' + head + body + '</div>';
}
function _dbFormHtml() {
  const active = (S.config && S.config.db && S.config.db.active) || {};
  const sel = S.sel.db || active.provider || 'sqlite';
  const prefill = (sel === active.provider) ? active : {};
  const opts = DB_PROVIDERS.map(p => '<option value="' + p.v + '"' + (p.v === sel ? ' selected' : '') + '>' + _esc(p.t) + '</option>').join('');
  return ''
    + '<label class="smb-label">Provider</label>'
    + '<select id="inst-db-provider" class="smb-input">' + opts + '</select>'
    + '<div class="smb-note">' + _esc(DB_NOTE[sel] || '') + '</div>'
    + '<div id="inst-db-fields" class="smb-fields">' + _dbFieldsHtml(sel, prefill) + '</div>'
    + '<div class="smb-actions">'
    +   '<button class="smb-btn" data-act="db-test">Test</button>'
    +   '<button class="smb-btn" data-act="db-save">Save</button>'
    +   '<button class="smb-btn" data-act="db-sql">Show Schema SQL</button>'
    +   '<button class="smb-btn" data-act="db-bootstrap">Auto-Create Tables</button>'
    +   '<button class="smb-btn smb-btn-primary" data-act="db-activate">Sign in (activate)</button>'
    + '</div>'
    + '<pre id="inst-db-output" class="smb-output" hidden></pre>';
}
function _dbFieldsHtml(provider, active) {
  const specs = DB_FIELDS[provider] || [];
  if (!specs.length) return '<div class="smb-hint">No connection details needed — the file lives under data/db/.</div>';
  return specs.map(f => _fieldHtml('db', f, active ? active[f.key] : '')).join('');
}

// ── Vault card ───────────────────────────────────────────────────────────────
function _vaultCardHtml() {
  const st = S.status || {};
  const v = st.vault || {};
  const provLabel = _provLabel(VAULT_PROVIDERS, v.provider || 'inline_db');
  const reachTxt = v.reachable ? 'Reachable' : (v.detail || 'Unreachable');
  const reach = v.reach || 'local';
  const badge = reach === 'shared' ? '<span class="smb-badge smb-badge-shared">Shared</span>'
    : reach === 'in_db' ? '<span class="smb-badge smb-badge-warn">In DB</span>'
    : reach === 'external' ? '<span class="smb-badge smb-badge-local">External</span>'
    : '<span class="smb-badge smb-badge-local">This machine</span>';
  const head =
      '<div class="smb-card-head" data-toggle="vault">'
    +   _connDot(v.reachable, S.busy)
    +   '<i data-lucide="key-round" class="smb-card-icon"></i>'
    +   '<div class="smb-card-titles"><div class="smb-card-title">Secrets vault</div>'
    +     '<div class="smb-card-sub">' + _esc(provLabel) + ' · ' + _esc(v.locator || '') + '</div></div>'
    +   badge
    +   '<span class="smb-card-status">' + _esc(reachTxt) + '</span>'
    +   '<i data-lucide="chevron-' + (S.open.vault ? 'down' : 'right') + '" class="smb-chevron"></i>'
    + '</div>';
  const body = S.open.vault ? '<div class="smb-card-body">' + _vaultFormHtml() + '</div>' : '';
  return '<div class="smb-card' + (S.open.vault ? ' smb-open' : '') + '">' + head + body + '</div>';
}
function _vaultFormHtml() {
  const secrets = (S.config && S.config.secrets) || {};
  const sel = S.sel.vault || secrets.provider || 'inline_db';
  const opts = VAULT_PROVIDERS.map(p => '<option value="' + p.v + '"' + (p.v === sel ? ' selected' : '') + '>' + _esc(p.t) + '</option>').join('');
  const restart = (S.status && S.status.vault && S.status.vault.restart_recommended)
    ? '<div class="smb-restart"><i data-lucide="alert-triangle"></i> Now serving from <b>' + _esc(S.status.vault.provider) + '</b>, but the server booted on <b>' + _esc(S.status.vault.boot_provider || '') + '</b>. The switch is live for new lookups; restart for a clean cutover. '
      + '<button class="smb-btn" data-act="vault-restart">Restart server</button> <span class="smb-restart-status"></span></div>'
    : '';
  return ''
    + '<label class="smb-label">Provider</label>'
    + '<select id="inst-vault-provider" class="smb-input">' + opts + '</select>'
    + '<div class="smb-note">' + _esc(VAULT_NOTE[sel] || '') + '</div>'
    + '<div id="inst-vault-fields" class="smb-fields">' + _vaultFieldsHtml(sel, secrets) + '</div>'
    + '<div class="smb-actions">'
    +   '<button class="smb-btn" data-act="vault-test">Test</button>'
    +   '<button class="smb-btn smb-btn-primary" data-act="vault-activate">Sign in (activate)</button>'
    + '</div>'
    + '<pre id="inst-vault-output" class="smb-output" hidden></pre>'
    + restart;
}
function _vaultFieldsHtml(provider, secrets) {
  const specs = VAULT_FIELDS[provider] || [];
  if (!specs.length) return '<div class="smb-hint">No connection details needed for this vault.</div>';
  const saved = (secrets.configs && secrets.configs[provider]) || {};
  const tokenSaved = secrets.token_saved && secrets.token_saved[provider];
  return specs.map(f => {
    const spec = (f.secret && tokenSaved) ? { ...f, keep: true } : f;
    return _fieldHtml('vault', spec, saved[f.key]);
  }).join('');
}

// ═══════════════════════════════════════════════════════════════════════════
// Cloud-account manager (in the "This device" Connections section) — ported from Server Manager.
// ═══════════════════════════════════════════════════════════════════════════
function _providerById(id) { return (S.providers || []).find(p => p.id === id) || null; }
function _idComplete(p) { return (p.connect_fields || []).filter(f => f.required).every(f => String(f.value || '').trim()); }
function _hasSavedId(p) { return (p.connect_fields || []).some(f => String(f.value || '').trim()); }
function _isConnected(p) { return !!p.has_key && _idComplete(p); }
function _accounts() {
  const provs = S.providers || [];
  const added = provs.filter(p => p.has_key || _hasSavedId(p));
  const pending = provs.filter(p => S.acctAdding.has(p.id) && !added.includes(p));
  return [...added, ...pending];
}

function _cloudAccountsHtml() {
  const accounts = _accounts();
  const taken = new Set(accounts.map(p => p.id));
  const addable = (S.providers || []).filter(p => !taken.has(p.id));
  const addCtl = addable.length
    ? '<div class="cvm-add">'
      + '<select id="inst-add-provider" class="cvm-input">'
      +   addable.map(p => '<option value="' + _escAttr(p.id) + '">' + _esc(p.display_name) + '</option>').join('')
      + '</select>'
      + '<button id="inst-add-btn" class="cvm-add-btn" data-act="acct-add" title="Add this cloud account"><i data-lucide="plus"></i></button>'
      + '</div>'
    : '';
  const rows = accounts.length
    ? '<div class="cvm-accounts">' + accounts.map(_acctHtml).join('') + '</div>'
    : (S.providers === null
        ? '<div class="cvm-hint">Loading cloud providers…</div>'
        : '<div class="cvm-hint">No cloud accounts yet — pick a cloud above and press +.</div>');
  return '<div class="inst-conn-sub">'
    + '<div class="smb-label">Cloud accounts</div>'
    + '<div class="inst-conn-sub-hint">Add a cloud provider\'s key here to see and control its servers as tiles below. If this device is itself a cloud VM (e.g. GCE), connecting unlocks: <strong>VM start/stop</strong>, <strong>HTTPS / SSL</strong> via Caddy + Let\'s Encrypt, and cloud infra facts on the "This device" Overview. Stored encrypted in the app vault.</div>'
    + addCtl + rows
    + '</div>';
}

function _acctStatus(p) {
  if (_isConnected(p)) return { cls: 'cvm-acct-pill-ok', text: 'Connected' };
  if (p.has_key && !_idComplete(p)) return { cls: 'cvm-acct-pill-warn', text: 'Add id' };
  if (!p.has_key && _hasSavedId(p)) return { cls: 'cvm-acct-pill-warn', text: 'Needs key' };
  return { cls: 'cvm-acct-pill-off', text: 'Set up' };
}
function _acctHtml(p) {
  const open = S.acctExpanded.has(p.id);
  const st = _acctStatus(p);
  const icon = p.icon || 'cloud';
  return '<div class="cvm-acct' + (open ? ' cvm-open' : '') + '" data-provider="' + _escAttr(p.id) + '">'
    + '<button class="cvm-acct-row" type="button" data-acct-toggle="' + _escAttr(p.id) + '">'
    +   '<i data-lucide="' + _escAttr(icon) + '" class="cvm-acct-icon"></i>'
    +   '<span class="cvm-acct-name">' + _esc(p.display_name) + '</span>'
    +   '<span class="cvm-acct-pill ' + st.cls + '">' + _esc(st.text) + '</span>'
    +   '<i data-lucide="chevron-down" class="cvm-acct-chevron"></i>'
    + '</button>'
    + '<div class="cvm-acct-menu"' + (open ? '' : ' hidden') + '>' + (open ? _connectFormHtml(p) : '') + '</div>'
    + '</div>';
}
function _connectFormHtml(p) {
  const haveKey = !!p.has_key;
  const idInputs = (p.connect_fields || []).map(f =>
    '<div class="cvm-field">'
    + '<label class="cvm-field-label">' + _esc(f.label || f.key) + (f.required ? ' <span class="cvm-req">*</span>' : '') + '</label>'
    + '<input type="text" class="cvm-connect-input" data-key="' + _escAttr(f.key) + '" '
    +   'value="' + _escAttr(f.value || '') + '" placeholder="' + _escAttr(f.placeholder || '') + '" '
    +   'autocomplete="off" spellcheck="false">'
    + (f.tip ? '<div class="cvm-field-tip">' + _esc(f.tip) + '</div>' : '')
    + '</div>'
  ).join('');
  const keyInputs = (p.credential_fields || []).filter(f => f.secret).map(f =>
    '<div class="cvm-field">'
    + '<label class="cvm-field-label">' + _esc(f.label || f.key) + (haveKey ? '' : ' <span class="cvm-req">*</span>') + '</label>'
    + '<textarea class="cvm-connect-input cvm-key-input" data-key="' + _escAttr(f.key) + '" rows="4" '
    +   'placeholder="' + _escAttr(haveKey ? 'A key is already saved — leave blank to keep it' : (f.placeholder || 'Paste this account’s cloud key here')) + '" '
    +   'autocomplete="off" spellcheck="false"></textarea>'
    + (f.tip ? '<div class="cvm-field-tip">' + _esc(f.tip) + '</div>' : '')
    + '</div>'
  ).join('');
  const actions = '<div class="cvm-acct-actions">'
    + '<button class="cvm-btn cvm-btn-primary" data-acct-save="' + _escAttr(p.id) + '"><i data-lucide="plug"></i> Save</button>'
    + (haveKey ? '<button class="cvm-btn" data-acct-signout="' + _escAttr(p.id) + '"><i data-lucide="log-out"></i> Sign out</button>' : '')
    + '<button class="cvm-btn cvm-btn-danger" data-acct-remove="' + _escAttr(p.id) + '"><i data-lucide="trash-2"></i> Remove</button>'
    + '<span class="cvm-key-status" data-acct-status="' + _escAttr(p.id) + '"></span>'
    + '</div>';
  return idInputs + keyInputs + actions
    + '<div class="cvm-field-tip cvm-acct-foot">Stored encrypted in the app vault — never shown again after saving.</div>';
}

function _acctStatusEl(id) { return document.querySelector('[data-acct-status="' + _cssEsc(id) + '"]'); }
function _setAcctStatus(id, text, kind) {
  const el = _acctStatusEl(id); if (!el) return;
  el.textContent = text || '';
  el.className = 'cvm-key-status' + (kind === 'ok' ? ' cvm-key-ok' : kind === 'err' ? ' cvm-key-err' : '');
}
function _menuEl(id) {
  const acct = document.querySelector('.cvm-acct[data-provider="' + _cssEsc(id) + '"]');
  return acct ? acct.querySelector('.cvm-acct-menu') : null;
}

async function _saveAccount(id) {
  const p = _providerById(id); if (!p) return;
  const menu = _menuEl(id); if (!menu) return;
  const values = {};
  menu.querySelectorAll('.cvm-connect-input').forEach(el => {
    const v = (el.value || '').trim();
    if (v) values[el.dataset.key] = v;
  });
  const missingId = (p.connect_fields || []).filter(f => f.required && !values[f.key]);
  if (missingId.length) { _setAcctStatus(id, 'Enter ' + (missingId[0].label || 'the account id') + ' first.', 'err'); return; }
  const keyFields = (p.credential_fields || []).filter(f => f.secret);
  const keyGiven = keyFields.some(f => values[f.key]);
  if (!p.has_key && !keyGiven) { _setAcctStatus(id, 'Paste this account’s key first.', 'err'); return; }
  _setAcctStatus(id, 'Saving…');
  const r = await _post('/admin/instances/connect', { provider: id, values });
  if (!r.ok) { _setAcctStatus(id, (r.body && r.body.detail) || 'Could not save.', 'err'); return; }
  S.acctAdding.delete(id);
  await _loadProviders();
  _render();
  _loadCloud(true);
}
async function _signOut(id) {
  if (!window.confirm('Sign out of this cloud account?\n\nThis removes the stored cloud key. Your servers are not affected — reconnect any time by entering the key again.')) return;
  await _post('/admin/instances/disconnect', { provider: id });
  S.acctExpanded.add(id);
  await _loadProviders();
  _render();
  _loadCloud(true);
}
async function _removeAccount(id) {
  const p = _providerById(id);
  const backendAdded = p && (p.has_key || _hasSavedId(p));
  if (backendAdded) {
    if (!window.confirm('Remove this cloud account?\n\nIts stored key and saved id are cleared from the app. Your actual servers in the cloud are not affected.')) return;
    await _post('/admin/instances/disconnect', { provider: id, forget_config: true });
  }
  S.acctAdding.delete(id);
  S.acctExpanded.delete(id);
  await _loadProviders();
  _render();
  _loadCloud(true);
}
function _addAccount() {
  const sel = document.getElementById('inst-add-provider');
  const id = sel && sel.value;
  if (!id) return;
  S.acctAdding.add(id);
  S.acctExpanded.add(id);
  _render();
}

// ── Connect cloud provider (Device facts) → shared credential popover ────────
// The "Connect cloud provider →" link in the Device facts section now opens the
// SAME shared credential popover as the ↑ HTTPS button (_deviceConnectPopup) —
// a small floating card anchored to the link, no inline form. mode: 'summary'
// shows saved credentials first, 'form' goes straight to the login form; the
// popover falls through to the form when nothing is saved.
function _deviceConnectToggle(mode, anchor) {
  _deviceConnectPopup('', mode || 'summary', anchor);
}

// ── Shared credential popover (↑ HTTPS path) ─────────────────────────────────
// The ↑ HTTPS button on an http:// URL opens the shared credential popover
// (ui/credential-popup/credential-popup.js) instead of the inline connect panel
// (_deviceConnectFormHtml) — same providers + /admin/instances/connect endpoint,
// as a small floating card anchored to the ↑ HTTPS button (no dim / no blur).
// Keeps the pending HTTPS domain so the upgrade fires as soon as this device
// links to its cloud VM.
// mode: 'summary' (saved credentials first) | 'form' (straight to the login form).
// The popover asks ONLY for the service-account key JSON — the project id is
// embedded in that JSON and extracted server-side on save (manager.py
// save_connection). The deployment-only secrets (github_token, admin_password)
// belong to the New-Deployment flow, so they never render here.
function _deviceConnectPopup(domain, mode, anchor) {
  var provs = S.providers || [];
  if (!provs.length) return;
  // If the inline panel was open, close it first so the popover is the only surface.
  if (S._deviceConnectOpen) {
    S._deviceConnectOpen = false; S._deviceConnectMode = 'summary';
    S._deviceConnectFromSummary = false;
    S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
    _render();
  }
  S._deviceConnectPendingDomain = domain || '';
  openCredentialPopup({
    title: 'Link this device to its cloud VM',
    hint: domain
      ? 'Enable HTTPS for ' + domain + ' — link this device to its cloud VM first. After linking, the setup proceeds automatically.'
      : 'Paste your Google Cloud service-account key below — the project is read from it automatically.',
    anchor: anchor,
    providers: provs,
    includeFields: ['service_account_json'],
    fieldTips: {
      service_account_json: {
        html: '<b>Service-account key (JSON)</b><br>In the Google Cloud console: IAM &amp; Admin → Service Accounts → open your account → Keys → Add key → JSON, then download the file and paste its entire contents here (or drag the file onto the box). The project ID is read from the key automatically — you only need this one field.',
        wide: true,
        images: [
          '/ui/admin-tools/instances/app-config/data-settings/img/gcp-sa-create-role.png',
          '/ui/admin-tools/instances/app-config/data-settings/img/gcp-sa-keys-addkey.png',
        ],
      },
    },
    mode: mode || 'form',
    saveLabel: 'Connect',
    extraBody: { requesting_user_id: _uid() },
    onSaved: _deviceConnectPopupSaved,
    onUseSaved: _deviceConnectPopupUseSaved,
    onCancel: function() { S._deviceConnectPendingDomain = ''; },
  });
}

// Popup save succeeded — reload providers + cloud and see whether this device
// now matches a VM. If yes: close and fire the queued HTTPS upgrade. If no:
// keep the popup open with an honest note.
async function _deviceConnectPopupSaved(popup) {
  await _loadProviders();
  await _loadCloud(true);
  _render();
  var selfInst = _find(S.self || _selfId());
  if (selfInst && selfInst._cloudVm) {
    var dom = S._deviceConnectPendingDomain;
    S._deviceConnectPendingDomain = '';
    S._deviceConnectOpen = false; S._deviceConnectMode = 'summary';
    S._deviceConnectFromSummary = false;
    S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
    popup.close();
    if (dom) _urlUpgradeHttps('http://' + dom);
    return;
  }
  S._deviceConnectPendingDomain = '';
  popup.showNote('Connected! But this device does not match any cloud VM yet — its HTTPS / SSL settings are available on the VM\'s own tile below.', 'warn');
}

// "Use saved" in the popup — attempt to link this device to the chosen
// account's VM (same re-match logic as the inline panel's Use saved).
async function _deviceConnectPopupUseSaved(providerId, popup) {
  if (!providerId) return;
  await _loadProviders();
  await _loadCloud(true);
  _render();
  var selfInst = _find(S.self || _selfId());
  if (selfInst && selfInst._cloudVm) {
    var dom = S._deviceConnectPendingDomain;
    S._deviceConnectPendingDomain = '';
    popup.close();
    if (dom) _urlUpgradeHttps('http://' + dom);
    return;
  }
  popup.showNote('This device does not match any cloud VM yet. For HTTPS / SSL settings, open the VM\'s own tile below (grid → your cloud VM → HTTPS / SSL section).', 'warn');
}

async function _deviceConnectSave() {
  var provider = document.getElementById('inst-device-connect-provider');
  var project = document.getElementById('inst-device-connect-project');
  var key = document.getElementById('inst-device-connect-key');
  var status = document.getElementById('inst-device-connect-status');
  var pid = provider && provider.value;
  if (!pid) { if (status) { status.textContent = 'Select a provider.'; status.className = 'cvm-key-err'; } return; }
  var projVal = (project && project.value || '').trim();
  var keyVal = (key && key.value || '').trim();
  if (!projVal) { if (status) { status.textContent = 'Enter the project ID.'; status.className = 'cvm-key-err'; } return; }
  // keyVal can be empty if a key is already saved — we read it from S.state.
  var p = _providerById(pid);
  if (!p.has_key && !keyVal) { if (status) { status.textContent = 'Paste your service-account key first.'; status.className = 'cvm-key-err'; } return; }

  if (status) { status.textContent = 'Connecting…'; status.className = ''; }

  var values = {};
  if (p && p.connect_fields && p.connect_fields.length) { values[p.connect_fields[0].key] = projVal; }
  else { values['project_id'] = projVal; }
  if (p && p.credential_fields && p.credential_fields.length) {
    var secField = p.credential_fields.find(function(f) { return f.secret; });
    if (keyVal) values[secField ? secField.key : 'key'] = keyVal;
  }

  var r = await _post('/admin/instances/connect', { provider: pid, values: values });
  if (!r.ok) {
    if (status) { status.textContent = (r.body && r.body.detail) || 'Could not connect.'; status.className = 'cvm-key-err'; }
    return;
  }

  // Reload providers + cloud VMs so we can check whether a VM matched.
  await _loadProviders();
  await _loadCloud(true);
  _render();

  var selfInst = _find(S.self || _selfId());
  if (selfInst && selfInst._cloudVm) {
    // Device linked to a cloud VM — success. Close the panel.
    S._deviceConnectOpen = false; S._deviceConnectMode = 'summary';
    S._deviceConnectFromSummary = false;
    S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
    // If an ↑ HTTPS click queued a domain, fire the HTTPS flow now.
    if (S._deviceConnectPendingDomain) {
      var dom = S._deviceConnectPendingDomain;
      S._deviceConnectPendingDomain = '';
      _urlUpgradeHttps('http://' + dom);
    }
    _render();
    return;
  }

  // Connected OK but this device doesn't match any cloud VM yet — keep the
  // panel open with an honest note.
  if (status) { status.textContent = ''; status.className = ''; }
  S._deviceConnectNote = 'Connected! But this device does not match any cloud VM yet — its HTTPS / SSL settings are available on the VM\'s own tile below.';
  S._deviceConnectNoteKind = 'warn';
  S._deviceConnectPendingDomain = '';
  _render();
}

// "Use saved" — the user picked an already-connected account. Attempt to
// link this device to it now (re-runs the match logic). If the device is
// already linked to this VM, close the panel. If not, show the note and
// keep the panel open so they can see the VM tile instead.
async function _deviceConnectUseSaved(providerId) {
  if (!providerId) return;
  // Re-trigger a cloud load so _mergeFleet re-runs with fresh provider data.
  await _loadProviders();
  await _loadCloud(true);
  _render();

  var selfInst = _find(S.self || _selfId());
  if (selfInst && selfInst._cloudVm) {
    S._deviceConnectOpen = false; S._deviceConnectMode = 'summary';
    S._deviceConnectFromSummary = false;
    S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
    if (S._deviceConnectPendingDomain) {
      var dom = S._deviceConnectPendingDomain;
      S._deviceConnectPendingDomain = '';
      _urlUpgradeHttps('http://' + dom);
    }
    _render();
    return;
  }
  S._deviceConnectNote = 'This device does not match any cloud VM yet. For HTTPS / SSL settings, open the VM\'s own tile below (grid → your cloud VM → HTTPS / SSL section).';
  S._deviceConnectNoteKind = 'warn';
  _render();
}

// "Use different" — flip into the login form mode, prefilled for the chosen
// provider (or defaulting to the first provider with saved data).
function _deviceConnectSwitchToForm(providerId) {
  S._deviceConnectMode = 'form';
  S._deviceConnectProvider = providerId || '';
  S._deviceConnectFromSummary = true;  // Cancel → back to summary
  S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
  _render();
}

// The provider dropdown changed — update the project field and key hint to
// match the newly-selected provider's saved values.
function _deviceConnectProviderChange() {
  var sel = document.getElementById('inst-device-connect-provider');
  var pid = sel ? sel.value : '';
  if (!pid) return;
  var p = _providerById(pid);
  if (!p) return;
  var projInp = document.getElementById('inst-device-connect-project');
  var keyInp = document.getElementById('inst-device-connect-key');
  if (projInp) {
    var val = '';
    if (p.connect_fields && p.connect_fields.length) {
      val = p.connect_fields[0].value || '';
    } else {
      val = p.project || '';
    }
    projInp.value = val;
  }
  if (keyInp) {
    keyInp.value = '';
    keyInp.placeholder = p.has_key
      ? 'A key is already saved — leave blank to keep it'
      : 'Paste the full JSON key here';
  }
  // Update the save button label
  var btn = document.querySelector('[data-act="device-connect-save"]');
  if (btn) {
    btn.innerHTML = '<i data-lucide="plug"></i> ' + (p.has_key ? 'Reconnect' : 'Connect');
    _refreshLucideIcons(btn);
  }
}

// Cancel: if we were in the form opened from a saved-credentials summary row,
// go back to the summary. Otherwise close the panel.
function _deviceConnectCancel() {
  if (S._deviceConnectFromSummary && S._deviceConnectMode === 'form') {
    S._deviceConnectMode = 'summary';
    S._deviceConnectProvider = '';
    S._deviceConnectFromSummary = false;
    S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
    _render();
    return;
  }
  S._deviceConnectOpen = false; S._deviceConnectMode = 'summary';
  S._deviceConnectFromSummary = false;
  S._deviceConnectNote = ''; S._deviceConnectNoteKind = '';
  S._deviceConnectPendingDomain = '';
  _render();
}

// ── One tile ─────────────────────────────────────────────────────────────────
function _squareHtml(d) {
  const platKey = d.kind === 'local' ? 'local' : _platformKey(d.platform);
  let sub;
  if (d.kind === 'cloud') sub = d.providerName || 'Cloud';
  else if (d.kind === 'local') sub = 'Port ' + d.port;
  else sub = d.branch ? d.branch : (d.platform || '');
  return '<div class="inst-square' + (d.is_self ? ' is-self' : '') + (d._stub ? ' is-stub' : '')
    + (d.instance_id === S.active ? ' active' : '') + '" data-iid="' + _escAttr(d.instance_id) + '">'
    + '<div class="inst-square-top">'
    +   '<span class="inst-icon-wrap plat-' + platKey + '">' + _instIconInner(d) + '</span>'
    +   '<span class="inst-status-dot' + (d.online ? ' online' : '') + '" title="' + (d.online ? 'Online' : 'Offline') + '"></span>'
    + '</div>'
    + '<div class="inst-square-body">'
    +   (d.is_self ? '<span class="inst-self-badge">This device</span>'
        : (d.kind === 'cloud' ? '<span class="inst-cloud-badge">Cloud</span>'
        : (d.kind === 'local' ? '<span class="inst-local-badge">Local</span>' : '')))
    +   '<span class="inst-square-name" title="' + _escAttr(_instName(d)) + '">' + _esc(_instName(d)) + '</span>'
    +   (sub ? '<span class="inst-square-meta" title="' + _escAttr(sub) + '">' + _esc(sub) + '</span>' : '')
    + '</div>'
    + '</div>';
}

// The "New instance" create tile — always first (top-left of the grid / left of
// the carousel), mirroring the Agents page's "New Agent" tile. Clicking it opens
// the New Deployment detail below. Styling lives in new-deployment/new-deployment.css.
function _newSquareHtml() {
  return '<div class="inst-square inst-square-new' + (S.active === NEW_DEPLOY_ID ? ' active' : '') + '" data-new-deploy="1">'
    + '<div class="inst-square-new-inner">'
    +   '<span class="inst-square-new-plus"><i data-lucide="plus"></i></span>'
    +   '<span class="inst-square-new-label">New instance</span>'
    + '</div>'
    + '</div>';
}

// ── Detail region (Overview) ─────────────────────────────────────────────────
// Build the provider console link for a cloud VM / Cloud Run instance.
// Returns '' for unknown/unrecognised providers.
function _providerConsoleUrl(d) {
  var cv = d._cloudVm;
  var provider = (cv && cv.provider) || d.provider || '';
  var project = (cv && cv.project) || d.project || '';
  var zone = (cv && cv.zone) || d.zone || '';
  if (!provider || !project) return '';
  if (provider === 'google_vm') {
    return 'https://console.cloud.google.com/compute/instances?chat=true&project=' + encodeURIComponent(project);
  }
  if (provider === 'google_cloud_run') {
    return 'https://console.cloud.google.com/run?chat=true&project=' + encodeURIComponent(project);
  }
  if (provider === 'aws_ec2') {
    var region = zone.replace(/[a-z]$/, '');
    return 'https://console.aws.amazon.com/ec2/home?region=' + encodeURIComponent(region || zone) + '#Instances';
  }
  return '';
}

// The Device fact value: OS type + cloud-provider details when running on a VM.
function _deviceValueHtml(d) {
  var cv = d._cloudVm;
  // ── Cloud Run ──
  if (d.deployment_provider === 'google_cloud_run' && d.cloud_run) {
    var crPrj = d.cloud_run.project || '';
    var crLink = crPrj ? 'https://console.cloud.google.com/run?chat=true&project=' + encodeURIComponent(crPrj) : '';
    var crLines = '<span class="inst-device-os">' + _esc(d.platform || 'Linux') + '</span>'
      + ' · <span class="inst-device-prov">Google Cloud Run</span>';
    if (crPrj) {
      crLines += '<br><span class="inst-device-proj">Project: '
        + (crLink ? '<a href="' + _escAttr(crLink) + '" target="_blank" rel="noopener">' + _esc(crPrj) + ' ↗</a>' : _esc(crPrj))
        + '</span>';
    }
    return crLines;
  }
  // ── Cloud VM (device running on VM, or bare cloud tile) ──
  var provider = (cv && cv.provider) || d.provider || '';
  var providerName = (cv && cv.providerName) || d.providerName || '';
  var project = (cv && cv.project) || d.project || '';
  if (provider && providerName) {
    var conUrl = _providerConsoleUrl(d);
    var osType = (d.platform && d.platform !== 'cloud') ? d.platform : 'Linux';
    var lines = '<span class="inst-device-os">' + _esc(osType) + '</span>'
      + ' · <span class="inst-device-prov">'
      + (conUrl ? '<a href="' + _escAttr(conUrl) + '" target="_blank" rel="noopener">' + _esc(providerName) + ' ↗</a>' : _esc(providerName))
      + '</span>';
    if (project) {
      lines += '<br><span class="inst-device-proj">Project: '
        + (conUrl ? '<a href="' + _escAttr(conUrl) + '" target="_blank" rel="noopener">' + _esc(project) + ' ↗</a>' : _esc(project))
        + '</span>';
    }
    return lines;
  }
  // ── Plain device ──
  var osName = d.platform ? '<span class="inst-device-os">' + _esc(d.platform) + '</span>' : '';
  // Self device with no cloud provider connected: offer to connect
  if (d.is_self && !d._cloudVm && d.deployment_provider !== 'google_cloud_run') {
    var provs = S.providers || [];
    var btn = provs.length
      ? ' <button class="inst-device-connect-btn" data-act="device-connect-toggle">Connect cloud provider →</button>'
      : '';
    return (osName + btn) || '<span class="inst-device-os">Unknown</span>';
  }
  return osName;
}

// ── Inline provider-connect panel shown below the Device facts when no cloud
// provider is linked for the self device. Reuses the same providers list and
// the same /admin/instances/connect endpoint as the Connections section.
//
// Two modes:
//   'summary' — shows already-saved credentials as selectable rows (Use saved /
//     Use different); if none are connected, falls through to the form.
//   'form'    — the login form, prefilled from saved state.
function _deviceConnectFormHtml(d) {
  if (!d.is_self) return '';
  var provs = S.providers || [];
  if (!provs.length) return '';

  // One-line note at the top of the panel (set by _deviceConnectSave after
  // a partial connect, or cleared when opening fresh).
  var noteHtml = '';
  if (S._deviceConnectNote) {
    var noteCls = S._deviceConnectNoteKind === 'warn' ? ' inst-device-connect-note-warn'
      : S._deviceConnectNoteKind === 'ok' ? ' inst-device-connect-note-ok' : '';
    noteHtml = '<div class="inst-device-connect-note' + noteCls + '">' + _esc(S._deviceConnectNote) + '</div>';
  }

  var panelBody = '';
  if (S._deviceConnectMode === 'summary') {
    // Build the saved-credentials summary.
    var connectedRows = [];
    provs.forEach(function(p) {
      if (p.has_key && _idComplete(p)) {
        var projVal = '';
        if (p.connect_fields && p.connect_fields.length) {
          projVal = p.connect_fields[0].value || '';
        } else {
          projVal = p.project || '';
        }
        connectedRows.push(
          '<div class="inst-dc-saved-row" data-provider="' + _escAttr(p.id) + '">'
          + '<span class="cvm-acct-icon"><i data-lucide="' + _escAttr(p.icon || 'cloud') + '"></i></span>'
          + '<span class="inst-dc-saved-name">' + _esc(p.display_name) + '</span>'
          + (projVal ? '<span class="inst-dc-saved-project">' + _esc(projVal) + '</span>' : '')
          + '<span class="cvm-acct-pill cvm-acct-pill-ok">Connected</span>'
          + '<div class="inst-dc-saved-actions">'
          +   '<button class="cvm-btn cvm-btn-primary" data-act="device-connect-use-saved" data-provider="' + _escAttr(p.id) + '">Use saved</button>'
          +   '<button class="cvm-btn" data-act="device-connect-use-different" data-provider="' + _escAttr(p.id) + '">Use different</button>'
          + '</div>'
          + '</div>'
        );
      }
    });

    panelBody = '<div class="inst-device-connect-fields">'
      + '<div class="inst-device-connect-head">Link this device to its cloud VM</div>';

    if (connectedRows.length) {
      panelBody += '<div class="inst-dc-saved-label">Saved cloud credentials</div>'
        + connectedRows.join('');

      // Also show a "Connect a new account" entry if there's one that isn't
      // connected already.
      var unconnected = provs.filter(function(p) { return !(p.has_key && _idComplete(p)); });
      if (unconnected.length) {
        var unconnOpts = unconnected.map(function(p) {
          return '<option value="' + _escAttr(p.id) + '">' + _esc(p.display_name) + '</option>';
        }).join('');
        panelBody += '<div class="inst-dc-saved-label" style="margin-top: 10px;">Or connect a new account</div>'
          + '<div class="inst-dc-new-row">'
          +   '<select id="inst-device-connect-new-pick" class="cvm-input" style="flex:1">' + unconnOpts + '</select>'
          +   '<button class="cvm-btn cvm-btn-primary" data-act="device-connect-use-different" data-new-pick="1">Continue</button>'
          + '</div>';
      }

      panelBody += '</div>';
    } else {
      // No saved credentials at all — note that and show the form.
      S._deviceConnectMode = 'form';
      panelBody += '<div class="inst-device-connect-note inst-device-connect-note-warn">'
        + 'No cloud auth credential saved yet — enter your provider credentials below to connect.'
        + '</div>'
        + _deviceConnectFormInner(d);
      panelBody += '</div>';
    }
  } else {
    // Mode == 'form' — show the login form directly.
    panelBody = '<div class="inst-device-connect-head">Link this device to its cloud VM</div>'
      + _deviceConnectFormInner(d);
  }

  return '<div class="inst-device-connect" id="inst-device-connect"' + (S._deviceConnectOpen ? '' : ' hidden') + '>'
    + noteHtml
    + panelBody
    + '</div>';
}

// The login-form inner — provider picker + project id + key + save/cancel.
// Reusable from both the summary→"Use different" path and the direct-form path.
function _deviceConnectFormInner(d) {
  var provs = S.providers || [];
  var prefillPid = S._deviceConnectProvider || '';
  var prefillP = prefillPid ? _providerById(prefillPid) : null;
  var showPid = prefillPid || '';
  // If the user clicked "Use different" on a specific saved row, open that
  // provider's form prefilled.
  if (!showPid) {
    // Default: the first provider that has a key or id saved.
    var firstConnected = provs.find(function(p) { return p.has_key && _idComplete(p); });
    var firstPartial = provs.find(function(p) { return (p.has_key || _hasSavedId(p)) && !_idComplete(p); });
    showPid = (firstConnected || firstPartial || provs[0] || {}).id || '';
  }
  var showP = _providerById(showPid);
  var haveKey = showP ? !!showP.has_key : false;

  var projVal = '';
  if (showP && showP.connect_fields && showP.connect_fields.length) {
    projVal = showP.connect_fields[0].value || '';
  } else if (showP) {
    projVal = showP.project || '';
  }

  var opts = provs.map(function(p) {
    var sel = (p.id === showPid) ? ' selected' : '';
    return '<option value="' + _escAttr(p.id) + '"' + sel + '>' + _esc(p.display_name) + '</option>';
  }).join('');

  return '<div class="inst-device-connect-hint">'
    + 'Enter the provider project ID and service-account key so the Overview can show VM details, start/stop actions, and HTTPS setup.'
    + '</div>'
    + '<div class="inst-device-connect-fields">'
    +   '<label class="inst-device-connect-label">Provider</label>'
    +   '<select id="inst-device-connect-provider" class="cvm-input" data-act="device-connect-provider-change">' + opts + '</select>'
    +   '<label class="inst-device-connect-label">Project ID</label>'
    +   '<input id="inst-device-connect-project" class="cvm-input" type="text" value="' + _escAttr(projVal) + '" placeholder="my-project-123456" autocomplete="off" spellcheck="false">'
    +   '<label class="inst-device-connect-label">Service-account key (JSON)</label>'
    +   '<textarea id="inst-device-connect-key" class="cvm-input" rows="5" placeholder="' + _escAttr(haveKey ? 'A key is already saved — leave blank to keep it' : 'Paste the full JSON key here') + '" autocomplete="off" spellcheck="false"></textarea>'
    + '</div>'
    + '<div class="inst-device-connect-actions">'
    +   '<button class="cvm-btn cvm-btn-primary" data-act="device-connect-save"><i data-lucide="plug"></i> ' + (haveKey ? 'Reconnect' : 'Connect') + '</button>'
    +   '<button class="cvm-btn" data-act="device-connect-cancel">Cancel</button>'
    +   '<span id="inst-device-connect-status"></span>'
    + '</div>';
}

function _factHtml(icon, label, valueHtml) {
  if (!valueHtml) return '';
  return '<div class="inst-fact" data-fact="' + _escAttr(label).toLowerCase() + '"><span class="inst-fact-label"><i data-lucide="' + icon + '"></i>' + _esc(label) + '</span>'
    + '<span class="inst-fact-value">' + valueHtml + '</span></div>';
}

function _renderOverview(d) {
  if (d.kind === 'cloud') return _renderCloudOverview(d);
  if (d.kind === 'local') return _renderLocalOverview(d);
  return _renderDeviceOverview(d);
}

// The "Database" fact value — tells the admin whether THIS instance is on the
// shared Postgres database or a separate/unshared one. The signal differs by kind:
//   • Registry devices only exist because they wrote a heartbeat into the shared
//     database, so they ride whatever DB this app reads. That fleet-wide fact is
//     exactly /backend-status's db.shared (remote Postgres = shared; local SQLite =
//     not). One machine on local SQLite is the only device it can ever list, so the
//     "not shared" wording is safe there too.
//   • Cloud VMs come from the provider's API (infra level), NOT the presence
//     registry — we can't confirm their database, and their not appearing in the
//     fleet means they aren't joined to the shared DB. So: "not shared".
// Renders a coloured pill + a one-line plain-English note (styling in instances.css).
function _dbShareValue(d) {
  if (d.kind === 'cloud') {
    if (d.fleet) {
      // Correlated to a registry device (see _mergeFleet) → it IS on the shared DB.
      const stale = d.fleet_online ? ''
        : ' <span class="inst-db-note inst-db-warn">Its app heartbeat is quiet right now.</span>';
      return '<span class="inst-db-badge inst-db-shared">Shared</span>'
        + '<span class="inst-db-note">Running WebAgent against the same shared database as this fleet.</span>' + stale;
    }
    return '<span class="inst-db-badge inst-db-sep">Not shared</span>'
      + '<span class="inst-db-note">Managed cloud server — not joined to the shared-database fleet.</span>';
  }
  const db = (S.status && S.status.db) || null;
  if (!db) return '<span class="inst-db-note">Checking database…</span>';
  if (db.shared) {
    // db.degraded is a live fact about THIS machine's connection, so only annotate
    // it on the self tile — we don't know another device's current reachability.
    const degraded = (d.is_self && db.degraded)
      ? ' <span class="inst-db-note inst-db-warn">Currently running on this device’s local copy.</span>' : '';
    return '<span class="inst-db-badge inst-db-shared">Shared</span>'
      + '<span class="inst-db-note">Same Postgres database as the rest of the fleet.</span>' + degraded;
  }
  return '<span class="inst-db-badge inst-db-sep">Not shared</span>'
    + '<span class="inst-db-note">Local database on this device only.</span>';
}

// The port this app is reached on, for the localhost fallback when an instance
// never reported its own URL. Uses the current page's port (the app's port),
// defaulting to the protocol's implicit port.
function _localPort() {
  try { return location.port || (location.protocol === 'https:' ? '443' : '80'); }
  catch { return '8080'; }
}
// The best address to SHOW for an instance: its reported URL, else a configured
// HTTPS domain, else the cloud VM's IP, else (for this device) the browser's own
// origin, else localhost:<port>. The raw IP stays available as its own "IP
// address" fact — the domain is the live URL, the IP is the box itself.
function _displayUrl(d) {
  if (d.endpoint) return d.endpoint;
  if (d.kind === 'cloud' && d.domains && d.domains.length) return 'https://' + d.domains[0];
  if (d.kind === 'cloud' && d.ip) return 'http://' + d.ip;
  if (d.is_self) { try { return location.origin; } catch (_) {} }
  return 'localhost:' + _localPort();
}
// Small copy + QR buttons shown next to any URL value in the overview facts.
function _urlActionsHtml(url) {
  if (!url) return '';
  return '<span class="inst-url-actions">'
    + '<button class="inst-url-btn" data-act="open-url" data-url="' + _escAttr(url) + '" title="Open URL"><i data-lucide="external-link"></i></button>'
    + '<button class="inst-url-btn" data-act="copy-url" data-url="' + _escAttr(url) + '" title="Copy URL"><i data-lucide="copy"></i></button>'
    + '<button class="inst-url-btn" data-act="qr-url" data-url="' + _escAttr(url) + '" title="Show QR code"><i data-lucide="qr-code"></i></button>'
    + '</span>';
}

// Repo address toolbar (Overview Repository fact). The repo link expands to this
// on click instead of navigating — same expand/collapse pattern as the URL rows
// above (_onUrlDocClick toggles .inst-url-open via the data-url-key state). It
// carries the same Open/Copy/QR actions as the URL section, plus the debug
// console's commit & push star and a Pull button, dispatched over the device
// queue when the instance is online (cloud VMs address their fleet twin, exactly
// like the tunnel buttons).
function _repoUrlActionsHtml(d) {
  if (!d.repo) return '';
  var html = _urlActionsHtml(d.repo);
  var iid = (d.kind === 'cloud')
    ? (d.device_id && d.fleet_online ? d.device_id : '')
    : (d.instance_id && d.online ? d.instance_id : '');
  if (iid) {
    html += '<button class="inst-url-btn" data-act="device-push" data-iid="' + _escAttr(iid)
      + '" title="Commit &amp; push all changes"><i data-lucide="star"></i></button>'
      + '<button class="inst-url-btn" data-act="device-pull" data-iid="' + _escAttr(iid)
      + '" title="Pull latest changes"><i data-lucide="download"></i></button>';
  }
  return html;
}

// Working-tree diffstat badge for the repo address row, fed by the device's own
// heartbeat (identity.repo_stats): "3 changed · +42 −7" when the tree is dirty,
// or "clean" in green when nothing has changed since HEAD. Shown next to the
// repo link the same way .inst-url-stale sits beside a URL.
function _repoStatsHtml(d) {
  const rs = d.repo_stats;
  if (!rs || typeof rs !== 'object') return '';
  const files = Number(rs.files) || 0;
  const ins = Number(rs.insertions) || 0;
  const del = Number(rs.deletions) || 0;
  if (files <= 0) {
    return '<span class="inst-repo-stats clean" title="Working tree clean">clean</span>';
  }
  return '<span class="inst-repo-stats dirty" title="' + files + ' file' + (files === 1 ? '' : 's')
    + ' changed · +' + ins + ' −' + del + ' lines since last commit">'
    + files + ' changed · <b class="add">+' + ins + '</b> <b class="del">−' + del + '</b></span>';
}

// Freshness label shown next to each URL in the overview — the time since this
// URL was last detected ("28s ago"), always visible on the URL row (not hidden
// in the collapsed action toolbar). Colour-coded like the status chips by how
// old the signal is: green < 1 min (actively reporting), grey < 1 h, amber
// < 1 day, red ≥ 1 day. When a URL can't be detected (no valid last_seen), it
// shows "2d ago" in red — the last signal was 2 days back. Clicking the label
// temporarily swaps in the untruncated "Completed 12 minutes ago" form.
function _staleAgeSeconds(lastSeen) {
  if (!lastSeen) return null;
  const t = Date.parse(lastSeen);
  if (Number.isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}
function _staleTone(secs) {
  if (secs === null) return 'stale';    // undetectable — worst case
  if (secs < 60) return 'fresh';        // actively reporting
  if (secs < 3600) return 'warm';       // seen within the hour
  if (secs < 86400) return 'aging';     // seen today
  return 'stale';                       // a day or more
}
function _urlStalenessHtml(lastSeen) {
  const secs = _staleAgeSeconds(lastSeen);
  const label = secs === null ? '2d ago' : _relTime(lastSeen);
  return '<span class="inst-url-stale ' + _staleTone(secs) + '"'
    + ' data-last-seen="' + _escAttr(lastSeen || '') + '"'
    + ' title="Click for full time">' + _esc(label) + '</span>';
}

// ── URL + Tunnel section (combined) ────────────────────────────────────────
// Renders one "URL" fact section containing: URL sub-rows (multi-URL), managed
// tunnel rows, custom tunnel URLs, and the "+" button to add a tunnel. This
// replaces the separate Tunnel fact row — tunnels are now part of the URL block.
function _urlFactHtml(d) {
  var parts = [];
  var renderedUrls = new Set();
  // URLs the admin marked hidden — collected from the urls map (or empty for
  // the fallback URL path) and rendered behind the "Hidden (N)" disclosure row
  // at the END of this section.
  var hiddenRows = [];

  // ── URLs ──
  if (d.urls && typeof d.urls === 'object' && Object.keys(d.urls).length) {
    // Sort by last_seen descending (most recently updated first), with hidden
    // URLs stashed AFTER the visible ones so they don't clutter the section.
    var urlKeys = Object.keys(d.urls);
    urlKeys.sort(function(a, b) {
      var ha = !!(d.urls[a] && d.urls[a].hidden);
      var hb = !!(d.urls[b] && d.urls[b].hidden);
      if (ha !== hb) return ha ? 1 : -1;
      var ta = (d.urls[a] && d.urls[a].last_seen) || '';
      var tb = (d.urls[b] && d.urls[b].last_seen) || '';
      return tb.localeCompare(ta);
    });
    var hiddenRows = [];
    urlKeys.forEach(function(url) {
      var entry = d.urls[url] || {};
      renderedUrls.add(String(url).replace(/\/+$/, '').toLowerCase());
      if (entry.hidden) hiddenRows.push([url, entry]);
      else parts.push(_urlRowHtml(url, entry, d, false));
    });
  } else {
    var url = _displayUrl(d);
    if (url) {
      renderedUrls.add(String(url).replace(/\/+$/, '').toLowerCase());
      parts.push(_urlRowHtml(url, { https_auto: d.endpoint_https_auto }, d, false));
    }
  }

  // ── Instances tunnel slave (cloudflared / ngrok) ──
  var t = d.tunnel;
  if (t && t.provider) {
    var pill = t.running ? '<span class="inst-tun-pill inst-tun-on">Running</span>'
      : (t.configured ? '<span class="inst-tun-pill inst-tun-off">Off</span>'
        : '<span class="inst-tun-pill inst-tun-na">Not ready</span>');
    var slaveUrl = (t.headful_url || t.public_url || '').trim();
    var slaveUrlKey = slaveUrl.replace(/\/+$/, '').toLowerCase();
    // Render the provider address through the same expandable URL component as
    // every live endpoint. The report endpoint persists this same normalized key,
    // so a later real request refreshes its timestamp instead of adding a duplicate.
    if (slaveUrl && !renderedUrls.has(slaveUrlKey)) {
      parts.push(_urlRowHtml(slaveUrl, { last_seen: t.connected_at || '' }, d, false));
      renderedUrls.add(slaveUrlKey);
    }
    parts.push(
      '<div class="inst-url-item inst-tunnel-row">'
      + '<div class="inst-url-link">' + pill + ' <span class="inst-tun-prov">Tunnel (slave)</span>'
      + '</div>'
      + '</div>');
  }

  // ── Custom tunnel URL ──
  var customUrl = (d.custom_tunnel_url || '').trim();
  var annotationKey = d.annotation_key || '';
  var customKey = 'custom:' + customUrl;
  var customOpen = S.openUrls.has(customKey) ? ' inst-url-open' : '';
  if (customUrl) {
    if (S.tunnelRemoveConfirm === annotationKey) {
      parts.push(
        '<div class="inst-url-item inst-tunnel-row' + customOpen + '" data-url-key="' + _escAttr(customKey) + '">'
        + '<div class="inst-url-link"><a href="' + _escAttr(_openUrl(customUrl)) + '" target="_blank" rel="noopener">' + _esc(customUrl) + '</a></div>'
        + '<div class="inst-url-meta">'
        + _urlActionsHtml(customUrl)
        + '<span class="inst-tun-confirm-actions">'
        +   '<button class="inst-tb-btn" data-act="tunnel-remove-cancel" data-key="' + _escAttr(annotationKey) + '">Cancel</button>'
        +   '<button class="inst-tb-btn inst-tb-danger" data-act="tunnel-remove-url" data-key="' + _escAttr(annotationKey) + '">'
        +     '<i data-lucide="trash-2"></i>Remove</button>'
        + '</span></div>'
        + '</div>');
    } else {
      parts.push(
        '<div class="inst-url-item inst-tunnel-row' + customOpen + '" data-url-key="' + _escAttr(customKey) + '">'
        + '<div class="inst-url-link"><a href="' + _escAttr(_openUrl(customUrl)) + '" target="_blank" rel="noopener">' + _esc(customUrl) + '</a></div>'
        + '<div class="inst-url-meta">' + _urlActionsHtml(customUrl)
        + '<span class="inst-tun-act-btn" data-act="tunnel-edit-url" data-key="' + _escAttr(annotationKey) + '" data-url="' + _escAttr(customUrl) + '" title="Change custom tunnel URL">&#9998;</span>'
        + '<span class="inst-tun-act-btn" data-act="tunnel-remove-ask" data-key="' + _escAttr(annotationKey) + '" title="Remove custom tunnel URL">&times;</span></div>'
        + '</div>');
    }
  }

  // ── "+" button to add a tunnel URL (always visible, even when no tunnel exists) ──
  parts.push(
    '<div class="inst-url-item inst-tunnel-add">'
    + '<span class="inst-tun-plus" data-act="tunnel-edit-url" data-key="' + _escAttr(annotationKey) + '" title="Add tunnel URL">+ Add tunnel URL</span>'
    + '</div>');

  // ── "Get a domain" — opens a floating chat widget with the default agent ──
  // The agent helps the user pick a registrar, registers/connects a domain, and
  // configures DNS using vault_login (see the browser_control skill). This is for
  // unsupported providers — any site behind a login form. The button is only shown
  // on the "This device" tile so the agent can see the URLs it needs to point at.
  if (d.is_self) {
    parts.push(
      '<div class="inst-url-item inst-get-domain">'
      + '<button class="inst-get-domain-btn" data-act="get-domain" title="Get help registering a domain or configuring DNS">'
      + '<i data-lucide="globe"></i> Get a domain</button>'
      + '</div>');
  }

  // ── Hidden URLs disclosure — the LAST row of the URL section ──
  // Hidden URLs live behind a disclosure row: a chevron + "Hidden (N)" after
  // every URL and the "+ Add tunnel URL" row. Clicking it expands all of the
  // hidden URLs below it, each with its own Unhide action in the toolbar.
  if (hiddenRows.length) {
    var hiddenKey = 'hidden:' + (d.instance_id || '');
    var hiddenOpen = S.openUrls.has(hiddenKey);
    parts.push(
      '<div class="inst-url-item inst-url-hidden-group' + (hiddenOpen ? ' open' : '') + '"'
      + ' data-act="toggle-hidden-group" data-ref="' + _escAttr(d.instance_id || '') + '"'
      + ' role="button" tabindex="0" title="' + (hiddenOpen ? 'Hide the hidden URLs' : 'Show the hidden URLs') + '">'
      + '<div class="inst-url-link">'
      +   '<i data-lucide="' + (hiddenOpen ? 'chevron-down' : 'chevron-right') + '"></i>'
      +   '<span class="inst-url-hidden-label">Hidden</span>'
      +   '<span class="inst-url-hidden-count">(' + hiddenRows.length + ')</span>'
      + '</div>'
      + '</div>');
    if (hiddenOpen) {
      hiddenRows.forEach(function(pair) {
        parts.push(_urlRowHtml(pair[0], pair[1], d, true));
      });
    }
  }

  if (!parts.length) return '';
  return _factHtml('link', 'URL', parts.join(''));
}

// One URL row in the Overview URL section. `entry` is the URL's metadata entry
// ({last_seen, https_auto, hidden?}); `hidden` renders it dimmed behind the
// "Hidden" group with a show (eye) button instead of the hide button.
function _urlRowHtml(url, entry, d, hidden) {
  var e = entry || {};
  // Auto-detected local-network addresses (http://<lan-ip>:<port>/) carry no
  // heartbeat signal by design — show a neutral "Local network" label instead of
  // the red "2d ago" staleness badge, since "no signal" is expected for them.
  var staleHtml = (e.local_net && !e.last_seen)
    ? '<span class="inst-url-stale local" title="Auto-detected local network address — works on the same network, listed by default">Local network</span>'
    : _urlStalenessHtml(e.last_seen);
  var autoBadge = e.https_auto
    ? ' <span class="inst-url-auto-https" title="HTTPS detected automatically via reverse proxy">auto</span>'
    : '';
  // Upgrade-to-HTTPS button for http:// URLs
  var upgradeBtn = '';
  if (url.indexOf('http://') === 0) {
    upgradeBtn = ' <button class="inst-url-https-btn" data-act="url-upgrade-https" data-url="' + _escAttr(url) + '" title="Upgrade to HTTPS">\u2191 HTTPS</button>';
  }
  var urlKey = 'url:' + url;
  var urlOpen = S.openUrls.has(urlKey) ? ' inst-url-open' : '';
  var ref = d.instance_id || '';
  var hiddenBadge = hidden
    ? ' <span class="inst-url-hidden-badge" title="Hidden from the main list — click Unhide in the toolbar to bring it back">Hidden</span>' : '';
  var toggleBtn = ' <button class="inst-url-btn inst-url-hide-btn" data-act="' + (hidden ? 'show-url' : 'hide-url')
    + '" data-url="' + _escAttr(url) + '" data-ref="' + _escAttr(ref) + '" title="'
    + (hidden ? 'Move this URL back to the main list' : 'Move this URL to the hidden list') + '">'
    + '<i data-lucide="' + (hidden ? 'eye' : 'eye-off') + '"></i>'
    + (hidden ? 'Unhide' : 'Hide') + '</button>';
  var clearBtn = ' <button class="inst-url-clear-btn" data-act="clear-url" data-url="' + _escAttr(url) + '" data-ref="' + _escAttr(ref) + '" title="Remove this URL"><i data-lucide="x"></i></button>';
  // Canonical URL: primary star + redirect toggle.
  var isPrimary = (S.canonicalUrl && _stripUrl(url) === _stripUrl(S.canonicalUrl));
  var primaryBadge = isPrimary
    ? ' <span class="inst-url-primary-badge" title="Primary URL — other hostnames redirect here">Primary</span>' : '';
  var redirectedBadge = '';
  if (!isPrimary && S.canonicalUrl && S.canonicalRedirect) {
    var host = _hostFromUrl(url);
    var isExcluded = S.canonicalExcluded && S.canonicalExcluded.indexOf(host) >= 0;
    if (isExcluded) {
      redirectedBadge = ' <button class="inst-url-redirected-badge excluded" data-act="canonical-reinclude"'
        + ' data-url="' + _escAttr(url) + '"'
        + ' title="Not redirected — redirect to ' + _escAttr(S.canonicalUrl) + ' is OFF for this host">Direct</button>';
    } else {
      redirectedBadge = ' <button class="inst-url-redirected-badge" data-act="canonical-unredirect"'
        + ' data-url="' + _escAttr(url) + '"'
        + ' title="Redirects to ' + _escAttr(S.canonicalUrl) + ' — click to un-redirect">Redirected</button>';
    }
  }
  var primaryBtn = ' <button class="inst-url-btn inst-url-primary-btn' + (isPrimary ? ' active' : '') + '"'
    + ' data-act="canonical-set" data-url="' + _escAttr(url) + '"'
    + ' title="' + (isPrimary ? 'This is the primary URL' : 'Set as primary URL') + '">'
    + '<i data-lucide="' + (isPrimary ? 'star' : 'star') + '"></i></button>';
  var redirectBtn = '';
  if (isPrimary) {
    redirectBtn = ' <button class="inst-url-btn inst-url-redir-btn' + (S.canonicalRedirect ? ' active' : '') + '"'
      + ' data-act="canonical-redirect" title="'
      + (S.canonicalRedirect ? 'Redirect is ON — non-primary URLs will 301 to this one' : 'Redirect is OFF — each URL works independently')
      + '"><i data-lucide="' + (S.canonicalRedirect ? 'corner-up-right' : 'corner-up-right') + '"></i>'
      + ' <span class="inst-url-redir-label">' + (S.canonicalRedirect ? 'Redirect on' : 'Redirect off') + '</span></button>';
  }
  return '<div class="inst-url-item' + (hidden ? ' inst-url-hidden' : '') + (isPrimary ? ' inst-url-primary' : '') + urlOpen + '" data-url-key="' + _escAttr(urlKey) + '">'
    + '<div class="inst-url-link"><a href="' + _escAttr(_openUrl(url)) + '" target="_blank" rel="noopener">' + _esc(url) + '</a>' + staleHtml + upgradeBtn + primaryBadge + redirectedBadge + hiddenBadge + '</div>'
    + '<div class="inst-url-meta">' + _urlActionsHtml(url) + autoBadge + primaryBtn + redirectBtn + toggleBtn + clearBtn + '</div>'
    + '</div>';
}

// ── Canonical URL helpers ───────────────────────────────────────────────────
// Strip trailing slashes and normalize scheme for comparison.
function _stripUrl(url) {
  return String(url || '').replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase();
}

// Extract just the host[:port] from a URL.
function _hostFromUrl(url) {
  var s = String(url || '').trim();
  if (s.indexOf('://') >= 0) s = s.split('://')[1];
  return s.split('/')[0].toLowerCase();
}

// Set this URL as the deployment's canonical primary. Writes it server-side,
// which creates/updates webhook_base_url.txt so the middleware redirects.
async function _setCanonicalUrl(url) {
  if (!url) return;
  try {
    var r = await _post('/admin/instances/canonical-url', { url: url, redirect_enabled: true });
    if (r.ok && r.body) {
      S.canonicalUrl = r.body.url || url;
      S.canonicalRedirect = !!r.body.redirect_enabled;
      S.canonicalExcluded = Array.isArray(r.body.excluded) ? r.body.excluded : [];
    }
  } catch (_) {}
}

// Toggle the redirect on/off for the current canonical URL.
async function _toggleCanonicalRedirect() {
  var enabled = !S.canonicalRedirect;
  try {
    var r = await _post('/admin/instances/canonical-url', { redirect_enabled: enabled });
    if (r.ok && r.body) {
      S.canonicalUrl = r.body.url || null;
      S.canonicalRedirect = !!r.body.redirect_enabled;
      S.canonicalExcluded = Array.isArray(r.body.excluded) ? r.body.excluded : [];
    }
  } catch (_) {}
}

// Exclude a specific URL's host from the redirect (it won't get 301'd).
async function _excludeCanonicalHost(url) {
  if (!url) return;
  try {
    var r = await _post('/admin/instances/canonical-url', { exclude_url: url });
    if (r.ok && r.body) {
      S.canonicalExcluded = Array.isArray(r.body.excluded) ? r.body.excluded : [];
    }
  } catch (_) {}
}

// Re-include a previously excluded host (it will get 301'd again).
async function _reincludeCanonicalHost(url) {
  if (!url) return;
  try {
    var r = await _post('/admin/instances/canonical-url', { include_url: url });
    if (r.ok && r.body) {
      S.canonicalExcluded = Array.isArray(r.body.excluded) ? r.body.excluded : [];
    }
  } catch (_) {}
}

// Re-render every URL fact section WITHOUT a full page render. Called after
// setting/changing the canonical URL so the primary badge + redirect toggle
// update in-place on the visible tile.
function _patchAllUrlRows() {
  // Find every .inst-fact[data-fact="url"] and replace its .inst-fact-value inner
  // HTML. We re-derive the data from S.devices (and cloud/local instances).
  if (!S.active) return;
  // Simplest: inline-render the URL fact for the active tile only, since that's
  // the one the admin is looking at.
  var d = _find(S.active);
  if (d) _patchUrlFact(d);
}


// ── "Get a domain" — open a floating chat widget with the default agent ──
// The agent receives a handoff prompt describing that the user wants to get or
// configure a domain. The agent should help the user pick a registrar, then use
// vault_login + browser actions to configure DNS — the same pattern the
// Namecheap DNS genui uses, now generalized for any provider behind a login form.
// The prompt text lives in get-domain-prompt.json (same folder, near this code)
// so it can be edited without touching the JS.
async function _getDomainChat() {
  var message = '';
  try {
    const res = await fetch(new URL('./get-domain-prompt.json', import.meta.url));
    if (res.ok) {
      const data = await res.json();
      message = (data && data.template) || '';
    }
  } catch (_) {}
  if (!message) {
    // Fallback only — the source of truth is get-domain-prompt.json.
    message = '[WebAgent Request | Source: Instances Page — Get a Domain]\n\n'
      + 'The user wants to get or configure a domain for their WebAgent deployment. '
      + 'Help them pick a registrar, register or connect a domain, and configure DNS '
      + 'to point at their server. Use the browser_control skill with vault_login if '
      + 'they have an account at a provider without a direct integration — you never '
      + 'see their password. Be conversational and ask what they want first.';
  }

  var w = createChatWidget({
    title: 'Get a domain',
    iconName: 'globe',
    ensureAgent: app.startWebagentSession,
    sessionTargetName: 'Domain hosting configuration',
    initialMessage: message,
  });
  w.open();
}
async function _clearUrl(url, ref, btn) {
  if (!url || !ref) return;
  try {
    var r = await _post('/admin/instances/clear-url', { ref: ref, url: url });
    if (r.ok) {
      // Remove from live data and re-render the URL fact in-place.
      var inst = _find(ref);
      if (inst && inst.urls && inst.urls[url]) {
        delete inst.urls[url];
      }
      _patchUrlFact(inst || { instance_id: ref, urls: {} });
      if (S._overviewPollData) {
        S._overviewPollData = _serializeUrls(inst || { urls: {} });
      }
    }
  } catch (_) {}
}

// ── Hide / re-show a URL in the instances metadata ──
// Mirrors _clearUrl: POST to the backend, patch the live row in-place, and keep
// the overview poll's URL snapshot in sync so it doesn't re-render redundantly.
// Works for the FALLBACK URL too (an instance with no `urls` metadata yet): the
// entry is created locally + on the backend so the "Hidden (N)" disclosure row
// can appear immediately.
async function _setUrlHidden(url, ref, hidden) {
  if (!url || !ref) return;
  try {
    var r = await _post('/admin/instances/set-url-hidden', { ref: ref, url: url, hidden: !!hidden });
    if (r.ok) {
      var inst = _find(ref);
      if (inst) {
        inst.urls = inst.urls || {};
        inst.urls[url] = inst.urls[url] || { last_seen: new Date().toISOString() };
        inst.urls[url].hidden = !!hidden;
      }
      _patchUrlFact(inst || { instance_id: ref, urls: {} });
      if (S._overviewPollData) {
        S._overviewPollData = _serializeUrls(inst || { urls: {} });
      }
    }
  } catch (_) {}
}

// ── Toggle the "Hidden URLs (N)" group row in the Overview URL section ──
function _toggleHiddenGroup(ref) {
  var key = 'hidden:' + (ref || '');
  if (S.openUrls.has(key)) S.openUrls.delete(key);
  else S.openUrls.add(key);
  _patchUrlFact(_find(ref) || { instance_id: ref || '', urls: {} });
}

// ── Upgrade an http:// URL to HTTPS ──
// If this device isn't connected to its cloud VM yet, open the connect form.
// Otherwise extract the domain and trigger the HTTPS enable flow.
function _urlUpgradeHttps(url, anchor) {
  if (!url || url.indexOf('http://') !== 0) return;
  var domain = url.replace(/^https?:\/\//, '').split(':')[0].split('/')[0];
  if (!domain) return;

  // The self device needs cloud-vm credentials to SSH in and set up HTTPS.
  var selfInst = _find(S.self || _selfId());
  if (!selfInst || !selfInst._cloudVm) {
    // Device not linked — open the shared credential POPOVER (a small floating
    // panel anchored to the ↑ HTTPS button, not the inline panel) and queue the
    // domain so HTTPS fires once the device links to its VM. Component:
    // ui/credential-popup/credential-popup.js.
    var provs = S.providers || [];
    var anyConnected = provs.some(function(p) { return _isConnected(p); });
    _deviceConnectPopup(domain, anyConnected ? 'summary' : 'form', anchor);
    return;
  }

  // Connected — trigger the HTTPS enable flow with the domain.
  var cv = selfInst._cloudVm;
  var ref = cv._ref || '';
  var parts = ref.split('|');
  var provider = parts[0] || '';
  var zone = parts[1] || '';
  var name = parts[2] || cv.name || '';

  if (!provider || !name) {
    _deviceConnectToggle('summary', anchor);
    return;
  }

  // Show status we're working
  var statusEl = document.getElementById('inst-https-status');
  if (statusEl) { statusEl.textContent = 'Enabling HTTPS for ' + domain + '…'; statusEl.className = 'inst-ping-status'; }
  _httpsPill('busy', 'Enabling…');
  _httpsLogReset();

  try {
    _httpsStream('/admin/instances/https/enable', {
      provider: provider, zone: zone, name: name,
      domain: domain, sibling_domain: '',
    }, function(result, ok) {
      if (ok) {
        _httpsPill('on', 'Active');
        if (statusEl) { statusEl.textContent = 'HTTPS is now active for ' + domain; statusEl.className = 'inst-ping-status ok'; }
        var st = _httpsStateRef();
        var domains = [domain];
        if (st) { st.domains = domains; }
        _post('/admin/instances/annotate', {
          key: ref, kind: 'cloud_vm', domains: domains,
        }).catch(function() {});
        _httpsUpdateOverviewFacts(domains);
      } else {
        _httpsPill('error', 'Failed');
        if (statusEl) { statusEl.textContent = result.message || 'Failed to enable HTTPS.'; statusEl.className = 'inst-ping-status err'; }
      }
    });
  } catch (e) {
    _httpsPill('error', 'Failed');
    if (statusEl) { statusEl.textContent = e.message || 'Could not reach the server.'; statusEl.className = 'inst-ping-status err'; }
  }
}

// ── Tunnel helpers (used by the combined URL+Tunnel section + tile actions) ──
// Start/Stop buttons for an ONLINE instance whose tunnel provider is set up. The
// command is dispatched to that instance over the device queue, where its
// detached slave owns the provider process. Off → Start; Running → Stop. iid is the
// registry instance id to address (a cloud VM passes its fleet-twin device id).
function _tunnelActionsHtml(d, iid, online) {
  const t = d.tunnel;
  if (!t || !t.provider || !iid || !online) return [];
  if (t.running) {
    return ['<button class="inst-tb-btn" data-act="tunnel-stop" data-iid="' + _escAttr(iid)
      + '"><i data-lucide="square"></i>Stop tunnel</button>'];
  }
  return ['<button class="inst-tb-btn" data-act="tunnel-start" data-iid="' + _escAttr(iid)
    + '"><i data-lucide="terminal"></i>Start tunnel</button>'];
}

// Server/repo fleet buttons for an ONLINE instance — Restart · Pull · Commit &
// push. Each is dispatched to that instance over the device queue (it runs the
// action on its OWN machine + repo), exactly like the tunnel buttons above. iid is
// the registry instance id to address. Restart is visible on EVERY online device:
// remote devices receive a fleet job, while this device takes the local guarded
// restart path (the same relauncher used by the Server section).
function _deviceControlHtml(iid, online, isSelf) {
  if (!iid || !online) return [];
  const out = [];
  out.push('<button class="inst-tb-btn" data-act="device-restart" data-iid="' + _escAttr(iid)
    + '" data-self="' + (isSelf ? '1' : '0') + '" data-state="idle" title="Restart this server">'
    + '<i data-lucide="rotate-ccw"></i>Restart</button>');
  out.push('<button class="inst-tb-btn" data-act="device-pull" data-iid="' + _escAttr(iid)
    + '"><i data-lucide="download"></i>Pull</button>');
  out.push('<button class="inst-tb-btn" data-act="device-push" data-iid="' + _escAttr(iid)
    + '"><i data-lucide="upload-cloud"></i>Commit &amp; push</button>');
  return out;
}

// running / busy / stopped → friendly label + status-dot class (local tiles + the
// This device Server card share this).
function _sibMeta(status) {
  return status === 'running' ? { label: 'Running', dot: 'cvm-dot-run' }
    : status === 'busy' ? { label: 'Port busy', dot: 'cvm-dot-busy' }
    : { label: 'Stopped', dot: 'cvm-dot-stop' };
}

// ═══════════════════════════════════════════════════════════════════════════
// "Server" section (This device only) — restart / re-port THIS running app.
// MOVED here out of Data Settings → Deployment (that whole section was removed).
// Just the two controls that are about the machine you're actually on:
//   • Restart this server (two-tap confirm) → POST /admin/storage/server/restart.
//   • Change this app's port + relaunch    → POST /admin/deploy/instances/set-hub-port.
// (Other local checkouts are no longer listed here — they are first-class TILES in
// the grid now, created from New instance → New Deployment → "This device" and
// managed like cloud VMs; see _renderLocalOverview.)
// ═══════════════════════════════════════════════════════════════════════════
function _serverSectionHtml() {
  const loc = S.local;
  const hubPort = (loc && loc.hub_port) || _localPort();
  const dis = S.localBusy ? ' disabled' : '';
  return '<div class="inst-conn-section inst-server-section">'
    + '<div class="inst-conn-section-head"><i data-lucide="server-cog"></i><span>Server</span></div>'
    + '<div class="inst-conn-section-hint">Restart WebAgent on this device or change the port it runs on.</div>'
    + '<div class="inst-conn-cards"><div class="inst-srv-hub">'
    +   '<div class="inst-srv-reset-row">'
    +     '<span class="inst-srv-reset-label-text">Server restart:</span>'
    +     '<button class="inst-tb-btn inst-srv-reset" data-act="server-reset" data-state="idle" title="Restart this server"' + dis + '>'
    +       '<i data-lucide="rotate-ccw"></i><span class="inst-srv-reset-label">Restart</span>'
    +     '</button>'
    +   '</div>'
    +   '<label class="smb-label">This app&rsquo;s port</label>'
    +   '<div class="inst-srv-port-row">'
    +     '<input id="inst-hub-port" class="smb-input" type="number" min="1024" max="65535" value="' + _escAttr(String(hubPort)) + '"' + dis + '>'
    +     '<button class="inst-tb-btn" data-act="hub-port-save"' + dis + '><i data-lucide="refresh-cw"></i>Change &amp; relaunch</button>'
    +   '</div>'
    +   '<div class="smb-hint">Each deployment needs its own port. Changing this <strong>restarts WebAgent</strong> on the new port — this connection drops and you reopen it at the new address.</div>'
    +   '<span class="inst-srv-status" data-hub-status></span>'
    + '</div></div>'
    + '</div>';
}

// ── Overview action buttons (pinned in the open card's header) ────────────────
// The primary controls for an open tile (Open / Ping / Start / Stop / Delete /
// tunnel / Restart·Pull·Commit&push / hold-to-unlink) used to live in the
// Overview body; they now render in the sticky card header (_detailHtml →
// .inst-detail-head-actions) so they stay in reach while the facts scroll.
// Kind-aware: cloud VMs, local checkouts, and fleet/self devices each build
// their own row. Returns '' when the instance has no actions.

function _overviewActionsHtml(d) {
  if (d.kind === 'cloud') return _cloudActionsHtml(d);
  if (d.kind === 'local') return _localActionsHtml(d);
  return _deviceActionsHtml(d);
}

function _localActionsHtml(d) {
  const running = d.status === 'running';
  const dis = S.localBusy ? ' disabled' : '';
  const actions = [];
  if (running && d.url) actions.push('<a class="inst-tb-btn" href="' + _escAttr(d.url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (running) actions.push('<button class="inst-tb-btn" data-act="local-stop" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="square"></i>Stop</button>');
  else actions.push('<button class="inst-tb-btn" data-act="local-start" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="play"></i>Start</button>');
  actions.push('<button class="inst-tb-btn inst-tb-danger" data-act="local-remove" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="trash-2"></i>Delete</button>');
  return actions.join('');
}

function _deviceActionsHtml(d) {
  const actions = [];
  if (d.endpoint) actions.push('<a class="inst-tb-btn" href="' + _escAttr(_openUrl(d.endpoint)) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (d.endpoint) actions.push('<button class="inst-tb-btn" data-act="ping" data-addr="' + _escAttr(d.endpoint) + '"><i data-lucide="activity"></i>Ping</button>');
  _tunnelActionsHtml(d, d.instance_id, d.online).forEach(a => actions.push(a));
  // Cloud VM actions (start/stop from the provider) when this device runs on a VM
  const cv = d._cloudVm;
  if (cv && d.is_self) {
    const running = (cv.status || '').toUpperCase() === 'RUNNING';
    const stopped = ['TERMINATED', 'SUSPENDED'].includes((cv.status || '').toUpperCase());
    const dis = S.cloudBusy ? ' disabled' : '';
    if (stopped) actions.push('<button class="inst-tb-btn" data-act="cloud-start" data-ref="' + _escAttr(cv._ref) + '" data-name="' + _escAttr(cv.name) + '"' + dis + '><i data-lucide="play"></i>Start VM</button>');
    if (running) actions.push('<button class="inst-tb-btn" data-act="cloud-stop" data-ref="' + _escAttr(cv._ref) + '" data-name="' + _escAttr(cv.name) + '"' + dis + '><i data-lucide="square"></i>Stop VM</button>');
  }
  // Cloud Run is immutable-infrastructure deployment: this action builds the
  // published repo/branch and rolls the service image. It is intentionally
  // independent of Pull and Commit & push, which keep their existing semantics.
  const cr = d.deployment_provider === 'google_cloud_run' && d.cloud_run ? d.cloud_run : null;
  if (cr) {
    const crState = S.cloudRunDeploy.get(d.instance_id);
    const crDis = (S.cloudBusy || (crState && crState.busy)) ? ' disabled' : '';
    actions.push('<button class="inst-tb-btn" data-act="cloud-run-build-deploy"'
      + ' data-iid="' + _escAttr(d.instance_id) + '"'
      + ' data-service="' + _escAttr(cr.service || '') + '"' + crDis + '>'
      + '<i data-lucide="package-up"></i>Build &amp; Deploy</button>');
  }
  // Restart / Pull / Commit & push this device over the fleet queue (Restart is
  // for all online devices; this device follows the same guarded restart path as
  // its Server card below. Skipped for the pre-heartbeat self stub, whose
  // synthetic id isn't a real registry claim key yet (it would 404); it appears
  // once the real heartbeat row lands. The synthetic self row can safely restart
  // THIS server directly, even before its first fleet heartbeat. Other actions
  // still wait for a real registry row.
  if (!d._stub || d.is_self) _deviceControlHtml(d.instance_id, d.online, d.is_self).forEach(a => actions.push(a));
  // Unlink — remove a STALE device from the fleet list. Offered only for an
  // OFFLINE device that isn't this machine (and never the synthetic stub): a live
  // one would just re-appear on its next check-in, so the button would be a lie.
  // It clears the record only, never the machine (see _unlinkDevice). Guard-railed
  // again server-side in /device/unlink. Confirmation is a PRESS-AND-HOLD: the
  // button fills over ~1s and only fires when the fill completes — the deliberate
  // hold replaces the old confirm popup (see _holdStart / .inst-hold in the CSS).
  if (!d.is_self && !d._stub && !d.online) {
    actions.push('<button class="inst-tb-btn inst-tb-danger inst-hold" data-act="unlink-hold"'
      + ' data-iid="' + _escAttr(d.instance_id) + '" data-name="' + _escAttr(d.label || d.instance_id) + '"'
      + ' title="Press and hold to unlink">'
      + '<span class="inst-hold-fill"></span>'
      + '<span class="inst-hold-label"><i data-lucide="unlink"></i>Hold to unlink</span>'
      + '</button>');
  }
  return actions.join('');
}

function _cloudActionsHtml(d) {
  // Phantom skeleton while cloud data is loading — ghost actions in the header
  // slot instead of blank space (matches the ghost facts in the Overview body).
  if (!d.ip && !d.machine_type && S.cloudLoading) {
    return '<span class="inst-ghost inst-ghost-actions">Loading actions…</span>';
  }
  const url = (d.domains && d.domains.length) ? ('https://' + d.domains[0])
    : (d.ip ? ('http://' + d.ip) : '');
  const running = (d.status || '').toUpperCase() === 'RUNNING';
  const stopped = ['TERMINATED', 'SUSPENDED'].includes((d.status || '').toUpperCase());
  const dis = S.cloudBusy ? ' disabled' : '';
  const actions = [];
  if (running && url) actions.push('<a class="inst-tb-btn" href="' + _escAttr(url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (d.ip) actions.push('<button class="inst-tb-btn" data-act="ping" data-addr="' + _escAttr(d.ip) + '"' + dis + '><i data-lucide="activity"></i>Ping</button>');
  if (stopped) actions.push('<button class="inst-tb-btn" data-act="cloud-start" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="play"></i>Start</button>');
  if (running) actions.push('<button class="inst-tb-btn" data-act="cloud-stop" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="square"></i>Stop</button>');
  // Tunnel control only for a VM correlated to an ONLINE fleet twin (it's the
  // twin's device that runs cloudflared and claims the job).
  if (!S.cloudBusy) _tunnelActionsHtml(d, d.device_id, d.fleet_online).forEach(a => actions.push(a));
  actions.push('<button class="inst-tb-btn inst-tb-danger" data-act="cloud-delete-ask" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="trash-2"></i>Delete</button>');
  return actions.join('');
}

// ── Local checkout tile Overview ─────────────────────────────────────────────
// A sibling WebAgent checkout on this machine, managed like a cloud VM: facts +
// Open / Start / Stop / Delete + inline edit of its name / folder / port, with the
// Start/Stop stream in a live log. Created from New instance → New Deployment →
// "This device". id math: the tile's instance_id is 'local:<id>'; the registry id
// used by the endpoints is d.local_id.
function _renderLocalOverview(d) {
  // Primary action buttons (Open/Start/Stop/Delete) moved to the card header
  // (_localActionsHtml); the body keeps facts, the live log, and the inline
  // Edit section (whose Save button still uses `dis` below).
  const busy = S.localBusy;
  const dis = busy ? ' disabled' : '';

  let warn = '';
  if (d.is_checkout === false) warn = '<div class="inst-db-note inst-db-warn">This folder is no longer a WebAgent checkout — fix the path below before starting.</div>';
  else if (d.port_aware === false) warn = '<div class="inst-db-note inst-db-warn">This checkout is too old to run on a custom port — update it (git pull) before starting.</div>';

  const facts = [
    _urlFactHtml(d),
    _factHtml('database', 'Database', '<span class="inst-db-badge inst-db-sep">Not shared</span>'
      + '<span class="inst-db-note">A separate WebAgent install on this machine — its own database.</span>'),
    _factHtml('plug', 'Port', '<span class="mono">' + _esc(String(d.port)) + '</span>'),
    _factHtml('folder', 'Repo folder', '<span class="mono">' + _esc(d.folder) + '</span>'),
  ];

  const edit = '<div class="inst-local-edit">'
    + '<label class="smb-label">Name</label>'
    + '<input class="smb-input" type="text" data-local-field="label" value="' + _escAttr(d.label) + '">'
    + '<label class="smb-label">Repo folder</label>'
    + '<input class="smb-input" type="text" data-local-field="folder" value="' + _escAttr(d.folder) + '" spellcheck="false">'
    + '<label class="smb-label">Port</label>'
    + '<input class="smb-input" type="number" min="1024" max="65535" data-local-field="port" value="' + _escAttr(String(d.port)) + '">'
    + '<div class="inst-detail-actions"><button class="inst-tb-btn" data-act="local-save" data-lid="' + _escAttr(d.local_id) + '"' + dis + '>Save changes</button>'
    + '<span class="inst-srv-status" data-local-status></span></div>'
    + '</div>';

  return '<div class="inst-facts">' + facts.join('') + '</div>'
    + warn
    + '<pre id="inst-log" class="cvm-log" hidden></pre>'
    + '<div class="inst-conn-section"><div class="inst-conn-section-head"><i data-lucide="sliders-horizontal"></i><span>Edit</span></div>' + edit + '</div>';
}

function _renderDeviceOverview(d) {
  const cv = d._cloudVm;   // cloud-VM metadata when this device runs on a cloud VM
  const cr = d.deployment_provider === 'google_cloud_run' && d.cloud_run
    ? d.cloud_run : null;
  const crState = cr ? S.cloudRunDeploy.get(d.instance_id) : null;
  var repoKey = d.repo ? 'repo:' + d.repo : '';
  var repoOpen = S.openUrls.has(repoKey) ? ' inst-url-open' : '';
  var repoHtml = d.repo
    ? '<div class="inst-url-item inst-repo-item' + repoOpen + '" data-url-key="' + _escAttr(repoKey) + '">'
      + '<div class="inst-url-link"><a href="' + _escAttr(_repoUrl(d.repo)) + '" target="_blank" rel="noopener">' + _esc(d.repo) + '</a>' + _repoStatsHtml(d) + '</div>'
      + '<div class="inst-url-meta">' + _repoUrlActionsHtml(d) + '</div>'
      + '</div>' : '';
  // Branch as a stats toolbar below the repo URL
  if (d.branch) {
    repoHtml += '<div class="inst-repo-toolbar">Branch: <span class="mono">' + _esc(d.branch) + '</span></div>';
  }

  const facts = [
    _urlFactHtml(d),
    // When this box runs on a cloud VM it shows its own public IP (discovered
    // via the GCE metadata server / WEBAGENT_PUBLIC_IP, see backend-status) —
    // even when the admin reaches it through a domain, the VM's raw address is
    // right here on the tile. Skipped when the cloud-VM twin (cv) is attached,
    // since that path already renders an IP fact from the cloud API.
    ...((!cv && S.status && S.status.public_ip) ? [
      _factHtml('globe', 'IP address', '<span class="mono">' + _esc(S.status.public_ip) + '</span>'),
    ] : []),
    _factHtml('database', 'Database', _dbShareValue(d)),
    _factHtml('monitor', 'Device', _deviceValueHtml(d)),
    _factHtml('github', 'Repository', repoHtml),
    // When running on a cloud VM, show its infra facts
    ...(cv ? [
      _factHtml('cloud', 'Cloud provider', cv.providerName ? _esc(cv.providerName) : ''),
      _factHtml('cpu', 'Machine type', cv.machine_type ? _esc(cv.machine_type) : ''),
      _factHtml('globe', 'IP address', cv.ip ? '<span class="mono">' + _esc(cv.ip) + '</span>' : ''),
      _factHtml('map-pin', 'Zone', cv.zone ? _esc(cv.zone) : ''),
    ] : []),
    // HTTPS domains when this device runs on a cloud VM with Caddy enabled.
    ...((cv && cv.domains && cv.domains.length) ? [
      _factHtml('shield-check', 'HTTPS',
        cv.domains.map(function(dom) {
          return '<div class="inst-https-domain-row">'
            + '<a href="https://' + _escAttr(dom) + '" target="_blank" rel="noopener">https://' + _esc(dom) + '</a>'
            + '<button class="inst-https-test-btn" data-act="https-test-domain" data-domain="' + _escAttr(dom) + '">Test</button>'
            + '<span class="inst-https-test-result" data-domain="' + _escAttr(dom) + '"></span>'
            + '</div>';
        }).join('')),
    ] : []),
    ...(cr ? [
      _factHtml('cloud-cog', 'Cloud provider', 'Google Cloud Run'),
      _factHtml('box', 'Service', cr.service ? _esc(cr.service) : ''),
      _factHtml('map-pin', 'Region', cr.region ? _esc(cr.region) : ''),
      _factHtml('folder-cloud', 'Project', cr.project ? _esc(cr.project) : ''),
    ] : []),
    (d._stub ? ''
      : '<div class="inst-fact" data-fact="instance"><span class="inst-fact-label"><i data-lucide="fingerprint"></i>Instance ID</span>'
        + '<span class="inst-fact-value mono">' + _esc(d.instance_id) + '</span></div>'),
  ];

  // Only THIS device carries the Server controls (restart / port / local checkouts,
  // moved here from Data Settings → Deployment) and the connection cards (DB/vault
  // sign-in + cloud accounts) — both act on the machine you're actually on, so they
  // live in its own tile. The self stub (pre-heartbeat) still owns this machine, so
  // it shows them too.
  const server = d.is_self ? _serverSectionHtml() : '';
  const conn = d.is_self ? _connCardsHtml() : '';
  // HTTPS / SSL section when this device runs on a cloud VM with Caddy.
  // Seed state from persisted domains so the pill shows "Active" on refresh.
  if (cv && d.is_self && cv.domains && cv.domains.length) {
    const st = _httpsState(cv._ref || '');
    if (st && st.state === 'off') {
      st.state = 'on'; st.text = 'Active'; st.domains = cv.domains;
    }
  }
  const https = (cv && d.is_self) ? _httpsSectionHtml(cv) : '';
  const crLog = cr
    ? '<pre id="inst-log" class="cvm-log"' + (crState && crState.lines.length ? '' : ' hidden')
      + '>' + _esc(crState ? crState.lines.join('\n') + '\n' : '') + '</pre>'
    : '';

  return '<div class="inst-facts">' + facts.join('') + '</div>'
    + (d.is_self ? _deviceConnectFormHtml(d) : '')
    + crLog + server + conn + https;
}

function _renderCloudOverview(d) {
  // ── Phantom skeleton while cloud data is loading ──
  // When the tile is first opened before cloud VMs finish populating (the tile
  // exists in the grid but has minimal data), show ghost placeholder facts
  // instead of blank space. Once data arrives the real values swap in smoothly.
  if (!d.ip && !d.machine_type && S.cloudLoading) {
    const ghostFacts = [
      _factHtml('database', 'Database', '<span class="inst-ghost">Shared</span>'),
      _factHtml('monitor', 'Device', d.providerName ? '<span class="inst-device-os">Linux</span> · <span class="inst-device-prov">' + _esc(d.providerName) + '</span>' : '<span class="inst-ghost">Loading…</span>'),
      _factHtml('cpu', 'Machine type', '<span class="inst-ghost">Loading…</span>'),
      _factHtml('globe', 'IP address', '<span class="inst-ghost">Loading…</span>'),
      _factHtml('map-pin', 'Zone', d.zone ? _esc(d.zone) : '<span class="inst-ghost">Loading…</span>'),
      _factHtml('git-branch', 'Repository', '<span class="inst-ghost">Loading repo…</span>'),
      _factHtml('server', 'Name', '<span class="mono">' + _esc(d.name) + '</span>'),
    ].join('');
    return '<div class="inst-facts">' + ghostFacts + '</div>'
      + '<pre id="inst-log" class="cvm-log" hidden></pre>'
      + '<div class="inst-conn-section"><div class="inst-conn-section-head"><i data-lucide="shield-check"></i><span>HTTPS / SSL</span></div><div class="inst-conn-section-hint"><span class="inst-ghost">Loading HTTPS status…</span></div></div>';
  }
  // Seed HTTPS state from persisted domains (survives full page refresh).
  if (d.domains && d.domains.length) {
    const st = _httpsState(d._ref || '');
    if (st && st.state === 'off') {
      st.state = 'on'; st.text = 'Active'; st.domains = d.domains;
    }
  }

  const repoKey = d.repo ? 'repo:' + d.repo : '';
  const repoOpen = S.openUrls.has(repoKey) ? ' inst-url-open' : '';
  const repoInner = d.repo
    ? '<span class="inst-url-row"><div class="inst-url-item inst-repo-item' + repoOpen + '" data-url-key="' + _escAttr(repoKey) + '">'
      + '<div class="inst-url-link"><a href="' + _escAttr(_repoUrl(d.repo)) + '" target="_blank" rel="noopener">' + _esc(d.repo) + '</a>' + _repoStatsHtml(d) + '</div>'
      + '<div class="inst-url-meta">' + _repoUrlActionsHtml(d) + '</div>'
      + '</div></span>'
    : '<span class="inst-repo-none">no repo set</span>';
  var repoHtml = repoInner
    + ' <button class="inst-repo-edit" data-act="repo-edit" data-key="' + _escAttr(d.annotation_key || '') + '" title="Set the repo this runs"><i data-lucide="pencil"></i></button>';
  if (d.branch) {
    repoHtml += '<div class="inst-repo-toolbar">Branch: <span class="mono">' + _esc(d.branch) + '</span></div>';
  }

  const facts = [
    _urlFactHtml(d),
    _factHtml('database', 'Database', _dbShareValue(d)),
    _factHtml('monitor', 'Device', _deviceValueHtml(d)),
    _factHtml('cpu', 'Machine type', d.machine_type ? _esc(d.machine_type) : ''),
    _factHtml('globe', 'IP address', d.ip ? '<span class="mono">' + _esc(d.ip) + '</span>' : ''),
  ];

  // Show HTTPS domain(s) as a fact row when enabled.
  const httpsSt = _httpsState(d._ref || '');
  if (httpsSt && httpsSt.domains && httpsSt.domains.length) {
    var httpsDomHtml = httpsSt.domains.map(function(dom) {
      return '<div class="inst-https-domain-row">'
        + '<a href="https://' + _escAttr(dom) + '" target="_blank" rel="noopener">https://' + _esc(dom) + '</a>'
        + '<button class="inst-https-test-btn" data-act="https-test-domain" data-domain="' + _escAttr(dom) + '">Test</button>'
        + '<span class="inst-https-test-result" data-domain="' + _escAttr(dom) + '"></span>'
        + '</div>';
    }).join('');
    facts.push(_factHtml('shield-check', 'HTTPS', httpsDomHtml));
  }

  facts.push(
    _factHtml('map-pin', 'Zone', d.zone ? _esc(d.zone) : ''),
    _factHtml('calendar', 'Created', d.created ? _esc(_fmtDate(d.created)) : ''),
    _factHtml('git-branch', 'Repository', repoHtml),
    _factHtml('server', 'Name', '<span class="mono">' + _esc(d.name) + '</span>'),
  );

  // Action buttons (Open/Ping/Start/Stop/tunnel/Delete) moved to the card header
  // (_cloudActionsHtml); the body keeps facts, the delete-confirm, the live log,
  // and the HTTPS section. `busy` still gates the inline delete confirm below.
  const busy = S.cloudBusy;

  // Destructive delete is gated behind a type-the-name confirm (shown inline once
  // the admin clicks Delete) — a reflex click shouldn't be able to destroy a real
  // server + its disk.
  const confirm = (S.confirmDel === d._ref && !busy) ? _delConfirmHtml(d) : '';

  return '<div class="inst-facts">' + facts.join('') + '</div>'
    + confirm
    + '<pre id="inst-log" class="cvm-log" hidden></pre>'
    + _httpsSectionHtml(d);
}

// ── HTTPS / SSL section (cloud VM Overview) ─────────────────────────────────
// Renders a card below the cloud-vm facts to enable HTTPS via Caddy's automatic
// Let's Encrypt. The domain can be pre-filled from the deploy config or a saved
// annotation; a live status pill + cert details appear once HTTPS is active.
function _httpsSectionHtml(d) {
  const ref = d._ref || '';
  const parts = ref.split('|');
  const provider = parts[0] || '';
  const zone = parts[1] || '';
  const name = parts[2] || d.name || '';
  // Try to pre-fill from d.domain (cloud VM's deploy domain) or annotation.
  const prefillDomain = (d.domain || '').trim();
  // Per-VM HTTPS state, persisted across re-renders.
  if (!S.httpsState.has(ref)) S.httpsState.set(ref, { state: 'off', text: 'Not enabled', status: '', statusKind: '', certHtml: '', log: '' });
  const h = S.httpsState.get(ref);
  // Build the registered-domains list (populated async via /https/domains-read).
  var regDomainsHtml = '';
  if (h.registered && h.registered.length) {
    regDomainsHtml = '<div class="inst-https-registered">'
      + '<div class="inst-https-registered-head">Registered domains</div>'
      + h.registered.map(function(dm) {
          return '<div class="inst-https-reg-row">'
            + '<span class="inst-https-reg-domain">' + _esc(dm) + '</span>'
            + '<button class="inst-https-reg-btn inst-https-test-btn" data-act="https-test-domain" data-domain="' + _escAttr(dm) + '">Test</button>'
            + '<button class="inst-https-reg-btn inst-https-del-btn" data-act="https-delete-domain" data-domain="' + _escAttr(dm) + '">✕</button>'
            + '<span class="inst-https-test-result" data-domain="' + _escAttr(dm) + '"></span>'
            + '</div>';
        }).join('')
      + '</div>';
  }
  return '<div class="inst-conn-section inst-https-section">'
    + '<div class="inst-conn-section-head"><i data-lucide="shield-check"></i><span>HTTPS / SSL</span>'
    +   '<span id="inst-https-pill" class="inst-https-pill ' + h.state + '">' + _esc(h.text) + '</span>'
    + '</div>'
    + '<div class="inst-conn-section-hint">'
    +   'Enter a domain you own, pointed at this server\'s IP, to enable automatic HTTPS (Let\'s Encrypt) via Caddy. '
    +   'Caddy provisions and renews the certificate automatically — no certbot or cron needed.'
    + '</div>'
    + '<div class="inst-https-row">'
    +   '<input id="inst-https-domain" class="smb-input" type="text"'
    +     ' placeholder="app.yourcompany.com" autocomplete="off"'
    +     ' value="' + _escAttr(prefillDomain) + '"'
    +     ' data-ref="' + _escAttr(ref) + '"'
    +     ' data-provider="' + _escAttr(provider) + '"'
    +     ' data-zone="' + _escAttr(zone) + '"'
    +     ' data-name="' + _escAttr(name) + '">'
    +   '<button class="inst-tb-btn" data-act="https-enable">'
    +     '<i data-lucide="lock"></i>Enable HTTPS</button>'
    +   '<button class="inst-tb-btn" data-act="https-check" title="Read configured domains from the server">'
    +     '<i data-lucide="list"></i> CHECK</button>'
    +   '<button class="inst-tb-btn" data-act="https-status" title="Check HTTPS status">'
    +     '<i data-lucide="refresh-cw"></i></button>'
    + '</div>'
    + '<div id="inst-https-sibling-row" class="inst-https-sibling-row" hidden>'
    +   '<label class="inst-https-sibling-label">'
    +     '<input id="inst-https-sibling" type="checkbox" checked>'
    +     '<span id="inst-https-sibling-text"></span>'
    +   '</label>'
    + '</div>'
    + regDomainsHtml
    + '<span id="inst-https-status" class="inst-ping-status' + (h.statusKind ? ' ' + h.statusKind : '') + '">' + _esc(h.status) + '</span>'
    + '<div id="inst-https-cert" class="inst-https-cert-details"' + (h.certHtml ? '' : ' hidden') + '>' + h.certHtml + '</div>'
    + '<pre id="inst-https-log" class="cvm-log"' + (h.log ? '' : ' hidden') + '>' + _esc(h.log) + '</pre>'
    + '</div>';
}

// Type-to-confirm gate for the destructive cloud-VM Delete. The final button
// stays disabled until the typed value exactly matches the server name (armed
// live by _onInput). Cancel collapses it and changes nothing.
function _delConfirmHtml(d) {
  const name = d.name || '';
  return '<div class="inst-confirm">'
    + '<div class="inst-confirm-warn"><i data-lucide="alert-triangle"></i>'
    +   '<span>This permanently destroys the server <b>' + _esc(name) + '</b> and its disk at '
    +     _esc(d.providerName || 'the provider') + '. This cannot be undone.</span></div>'
    + '<label class="inst-confirm-label" for="inst-del-confirm">Type the server name to confirm:</label>'
    + '<input class="inst-confirm-input" id="inst-del-confirm" data-expect="' + _escAttr(name) + '"'
    +   ' placeholder="' + _escAttr(name) + '" autocomplete="off" spellcheck="false">'
    + '<div class="inst-confirm-actions">'
    +   '<button class="inst-tb-btn" data-act="cloud-delete-cancel">Cancel</button>'
    +   '<button class="inst-tb-btn inst-tb-danger" id="inst-del-go" data-act="cloud-delete-go"'
    +     ' data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(name) + '" disabled>'
    +     '<i data-lucide="trash-2"></i>Delete permanently</button>'
    + '</div>'
    + '</div>';
}

// Render the settings tab content — embeds the full App Config UI
// (Data Settings, App Settings, Agent Settings) for the local device.
function _renderSettingsTab(d) {
  // Create the host div - the container will be moved there after render
  // and then the app config will be initialized
  return '<div id="inst-settings-host" class="inst-settings-host">'
    + '<div class="inst-settings-loading">'
    +   '<span class="inst-spin"><i data-lucide="loader-2"></i></span>'
    +   '<span>Loading settings…</span>'
    + '</div>'
    + '</div>';
}

// ── Dashboard tab (embedded) ────────────────────────────────────────────────
// Renders a placeholder host that mountDashboard (in ./dashboard/dashboard.js)
// fills with the toolbar + metric grid when the tab is first shown.

let _dashboardMounted = false;

function _renderDashboardTab(d) {
  return '<div id="inst-dashboard-host" class="inst-dashboard-host">'
    + '<div class="inst-settings-loading">'
    +   '<span class="inst-spin"><i data-lucide="loader-2"></i></span>'
    +   '<span>Loading dashboard…</span>'
    + '</div>'
    + '</div>';
}

function _onDashboardTabRendered() {
  if (S.tab !== 'dashboard') return;
  const host = document.getElementById('inst-dashboard-host');
  if (!host) return;
  if (_dashboardMounted) return;
  _dashboardMounted = true;

  // Dynamically import the dashboard module and mount it into the host
  import('./dashboard/dashboard.js')
    .then((m) => {
      // A poll or re-render may have swapped the host while the module was loading
      const live = document.getElementById('inst-dashboard-host');
      if (!live || S.tab !== 'dashboard') { _dashboardMounted = false; return; }
      if (typeof m.mountDashboard === 'function') {
        m.mountDashboard(live);
      }
    })
    .catch((e) => {
      console.error('[instances] dashboard mount failed', e);
      _dashboardMounted = false;
      const host2 = document.getElementById('inst-dashboard-host');
      if (host2) host2.innerHTML = '<div class="inst-settings-loading inst-settings-error">Failed to load dashboard</div>';
    });
}

function _unmountDashboard() {
  if (!_dashboardMounted) return;
  _dashboardMounted = false;
  // Dynamically import the module to reach unmountDashboard
  import('./dashboard/dashboard.js')
    .then((m) => {
      if (typeof m.unmountDashboard === 'function') {
        try { m.unmountDashboard(); } catch (e) { console.error('[instances] dashboard unmount failed', e); }
      }
    })
    .catch(() => {});
}

// ── Users tab (embedded) ────────────────────────────────────────────────────
// App-wide user management for "This device", mirroring the Agents page Members
// tab: an access-policy card (app access mode — moved here from Data Settings →
// App Access) over Admins / Users tables with activity stats + authorize /
// restrict / make-admin / reject actions. The sub-feature lives in its own
// directory (./users/) so all of its page details stay together, mirroring the
// dashboard tab's drop-in shape. See users/users.js.

let _usersMounted = false;

function _renderUsersTab(d) {
  return '<div id="inst-users-host" class="inst-users-host"></div>';
}

function _onUsersTabRendered() {
  if (S.tab !== 'users') return;
  const host = document.getElementById('inst-users-host');
  if (!host) return;
  if (_usersMounted) return;
  _usersMounted = true;

  // Dynamically import the users module and mount it into the host
  import('./users/users.js')
    .then((m) => {
      // A poll or re-render may have swapped the host while the module was loading
      const live = document.getElementById('inst-users-host');
      if (!live || S.tab !== 'users') { _usersMounted = false; return; }
      if (typeof m.mountUsers === 'function') {
        m.mountUsers(live);
      }
    })
    .catch((e) => {
      console.error('[instances] users mount failed', e);
      _usersMounted = false;
      const host2 = document.getElementById('inst-users-host');
      if (host2) host2.innerHTML = '<div class="inst-settings-loading inst-settings-error">Failed to load users</div>';
    });
}

function _unmountUsers() {
  if (!_usersMounted) return;
  _usersMounted = false;
  // Dynamically import the module to reach unmountUsers
  import('./users/users.js')
    .then((m) => {
      if (typeof m.unmountUsers === 'function') {
        try { m.unmountUsers(); } catch (e) { console.error('[instances] users unmount failed', e); }
      }
    })
    .catch(() => {});
}

// Post-render hook to initialize app config after the settings tab is rendered
// Track whether app config has been loaded
let _appConfigLoaded = false;
// The container is shared with the Admin Tools Settings view. Keep a direct
// reference because an async settings initializer can otherwise move it into a
// card that has just been replaced by a background Instances re-render. Once it
// is in a detached card, document.getElementById can no longer recover it.
let _appConfigContainer = null;
let _settingsRenderGeneration = 0;
let _configDetailResizeObserver = null;

// Configuration uses the Instances page's single scrollbar. Measure the
// grid and chat-assistant heights so the assistant bar can stick at the
// bottom of the viewport. The instance header no longer sticks above,
// so its height is no longer tracked (the header scrolls away).
function _syncConfigurationStickyOffset(root) {
  const grid = root?.querySelector('.inst-grid.carousel');
  const detail = grid?.querySelector('.inst-detail');
  if (!grid || !detail?.querySelector('.inst-settings-host')) return;
  const host = detail.querySelector('.inst-settings-host');
  if (!host) return;

  const apply = () => {
    host.style.setProperty('--inst-config-grid-height', grid.clientHeight + 'px');
    const chat = host.querySelector('#ac-unified-pa-bar');
    if (chat) host.style.setProperty('--inst-config-chat-height', chat.offsetHeight + 'px');
  };
  apply();

  if (typeof ResizeObserver !== 'undefined') {
    _configDetailResizeObserver?.disconnect();
    _configDetailResizeObserver = new ResizeObserver(apply);
    _configDetailResizeObserver.observe(grid);
    const chat = host.querySelector('#ac-unified-pa-bar');
    if (chat) _configDetailResizeObserver.observe(chat);
  }
}

function _dockConfigurationAssistant(container) {
  const chat = container?.querySelector('#ac-unified-pa-bar');
  const scrollWrap = container?.querySelector('#app-config-scroll-wrap');
  if (chat && scrollWrap && chat.previousElementSibling !== container.querySelector('#app-config-subnav')) {
    container.insertBefore(chat, scrollWrap);
  }
}

function _restoreConfigurationAssistant(container) {
  const chat = container?.querySelector('#ac-unified-pa-bar');
  const scrollWrap = container?.querySelector('#app-config-scroll-wrap');
  if (chat && scrollWrap && chat.previousElementSibling !== scrollWrap) {
    scrollWrap.after(chat);
  }
}

function _getAppConfigContainer() {
  const container = document.getElementById('app-config-container') || _appConfigContainer;
  if (container) _appConfigContainer = container;
  return container;
}

// Scroll the tab bar to the top of the Instances scroller after the user clicks
// any detail tab (Overview, Dashboard, Configuration, New Deployment, New
// Instance) — on desktop AND mobile — hiding everything above the tab row
// (toolbar, tile carousel, instance header).
// Retried on each animation frame because some tab bodies mount asynchronously
// (Configuration's module import, the New-Deployment form): while only the short
// loading skeleton is in the DOM the grid's max scroll is too small to reach the
// tab bar (the browser clamps the scroll target), so we keep nudging until the
// real content is tall enough — or the user leaves the tab, or a deadline
// passes. After landing it keeps watching briefly so a background re-render
// that rebuilds the grid (new node, scroll reset) gets re-landed, but yields
// the moment the user scrolls the SAME grid node themselves.
let _tabsScrollRaf = null;
let _scrollTargetTab = null;
function _scrollTabsToTop(tabId) {
  _scrollTargetTab = tabId;
  if (_tabsScrollRaf) cancelAnimationFrame(_tabsScrollRaf);
  const deadline = Date.now() + 4000;
  let gridNode = null, lastSet = null;
  const tick = () => {
    _tabsScrollRaf = null;
    if (S.tab !== _scrollTargetTab) return;
    const grid = _root()?.querySelector('.inst-grid.carousel');
    const tabs = grid?.querySelector('.inst-tabs');
    if (!grid || !tabs) return;
    const delta = tabs.getBoundingClientRect().top - grid.getBoundingClientRect().top;
    if (Math.abs(delta) <= 2) {
      // Tab bar is at the top. Keep watching until the deadline so a rebuild
      // that resets the scroll gets re-landed; a user scroll stops it (below).
      if (Date.now() <= deadline) _tabsScrollRaf = requestAnimationFrame(tick);
      return;
    }
    // Same grid node, scrolled away from where we left it, and there was room
    // to reach the target → the user took over. Stop fighting them.
    const maxScroll = grid.scrollHeight - grid.clientHeight;
    if (gridNode === grid && lastSet !== null && grid.scrollTop !== lastSet && maxScroll >= lastSet) return;
    gridNode = grid;
    lastSet = grid.scrollTop + delta;
    grid.scrollTop = lastSet;                    // clamped by the browser while the skeleton is short
    if (Date.now() <= deadline) _tabsScrollRaf = requestAnimationFrame(tick);
  };
  _tabsScrollRaf = requestAnimationFrame(tick);
}

async function _ensureAppConfigLoaded() {
  if (_appConfigLoaded) return;
  
  // Import the app-config module to load its container and expose functions
  try {
    await import('./app-config/settings-view.js');
    _appConfigLoaded = true;
  } catch (e) {
    console.error('[instances] Failed to load app config module:', e);
    throw e;
  }
}

function _onSettingsTabRendered() {
  if (S.tab !== 'settings') return;
  const host = document.getElementById('inst-settings-host');
  if (!host) return;
  const generation = _settingsRenderGeneration;

  // Ensure app config module is loaded first
  _ensureAppConfigLoaded()
    .then(() => {
      // A poll, action, or tab change may have rebuilt the detail card while
      // the import was pending. Only the current card is allowed to claim the
      // shared container; stale cards must leave it in its permanent home.
      if (generation !== _settingsRenderGeneration || S.tab !== 'settings'
          || host !== document.getElementById('inst-settings-host') || !host.isConnected) return;

      const container = _getAppConfigContainer();
      if (!container) {
        // The app-config partial is normally mounted before this view starts.
        // Do not replace the loading state with a misleading transient error if
        // the page is still being assembled.
        console.error('[instances] App config container is unavailable');
        return;
      }
      
      // Replace the host's initial loading placeholder rather than appending
      // below it. The leftover placeholder was the spinner appearing above the
      // Configuration navigation after App Config had already mounted.
      if (container.parentElement !== host || host.childElementCount !== 1) {
        host.replaceChildren(container);
      }
      container.removeAttribute('hidden');
      _dockConfigurationAssistant(container);
      _syncConfigurationStickyOffset(_root());
      
      // Initialize and start app config
      try {
        if (window.initAppConfig) window.initAppConfig();
        if (window.startAppConfig) window.startAppConfig({ preserveScroll: true });
      } catch (e) {
        console.error('[instances] Failed to start app config:', e);
        host.innerHTML = '<div class="inst-settings-loading inst-settings-error">Failed to load settings: ' + e.message + '</div>';
      }

      // Restore saved scroll position and search text — retries up to 3× because
      // app config sections load async (app-settings's load is async), and the
      // content element may not have its full height on the first attempt.
      ;(function _restoreConfig(attempts = 4) {
        const cfg = _readConfigState();
        if (!cfg) return;
        const content = document.getElementById('app-config-content');
        const searchInput = document.getElementById('ac-unified-pa-input');
        if (content && cfg.scrollTop) {
          if (content.scrollHeight > cfg.scrollTop || attempts <= 1) {
            content.scrollTop = cfg.scrollTop;
          } else {
            return setTimeout(() => _restoreConfig(attempts - 1), 200);
          }
        }
        if (searchInput && cfg.searchText) {
          searchInput.value = cfg.searchText;
          searchInput.dispatchEvent(new Event('input', { bubbles: true }));
        }
      })();

      // Save scroll position and search text on changes (debounced scroll)
      requestAnimationFrame(() => {
        const content = document.getElementById('app-config-content');
        if (content) {
          let scrollTimer;
          content.addEventListener('scroll', () => {
            clearTimeout(scrollTimer);
            scrollTimer = setTimeout(() => _saveConfigField('scrollTop', content.scrollTop), 150);
          }, { passive: true });
        }
        const searchInput = document.getElementById('ac-unified-pa-input');
        if (searchInput) {
          searchInput.addEventListener('input', () => {
            _saveConfigField('searchText', searchInput.value);
          });
        }
      });
    })
    .catch((e) => {
      console.error('[instances] Failed to load app config:', e);
      host.innerHTML = '<div class="inst-settings-loading inst-settings-error">Failed to load settings module</div>';
    });
}

function _renderTab(d) {
  if (S.tab === 'settings' && d.is_self) {
    return _renderSettingsTab(d);
  }
  if (S.tab === 'dashboard' && d.is_self) {
    return _renderDashboardTab(d);
  }
  if (S.tab === 'users' && d.is_self) {
    return _renderUsersTab(d);
  }
  return _renderOverview(d);
}

function _detailHtml(d) {
  const platKey = d.kind === 'local' ? 'local' : _platformKey(d.platform);
  // Primary action buttons for the open tile (Open/Start/Stop/Delete/Ping/…),
  // built by the kind-aware helpers and pinned in the sticky card header between
  // the head row and the tabs — they used to live in the Overview body.
  const headActions = _overviewActionsHtml(d);
  // Only show Dashboard, Settings and Users tabs for this device (self), not for cloud/local instances
  const tabs = _TABS
    .filter(([id]) => (id !== 'settings' && id !== 'dashboard' && id !== 'users') || d.is_self)
    .map(([id, label]) =>
      '<button class="inst-tab' + (id === S.tab ? ' active' : '') + '" data-tab="' + id + '">' + _esc(label) + '</button>'
    ).join('');
  const sub = (d.kind === 'cloud')
    ? (_statusMeta(d.status).label + (d.providerName ? ' · ' + d.providerName : ''))
    : (d.kind === 'local')
      ? (_sibMeta(d.status).label + ' · Port ' + d.port)
      : ((d.online ? 'Online' : 'Offline') + (d.platform ? ' · ' + d.platform : ''));
  // Head + tab bar wrapper — NOT sticky (scrolled away when the user scrolls
  // down). Only the Configuration page's own #app-config-subnav sticks at top:0
  // inside the .inst-grid scroller (see the sticky rules in instances.css).
  // Device tiles (shared registry) can be renamed + re-iconed from here, mirroring
  // the Agents page: the icon chip opens the shared icon picker, the name is
  // inline-editable (double-click). Cloud VMs + the pre-heartbeat self stub aren't
  // editable (their identity isn't a shared-registry row we can override).
  const editable = _instEditable(d);
  const iconWrap = '<span class="inst-icon-wrap plat-' + platKey + (editable ? ' inst-icon-editable' : '') + '"'
    + (editable ? ' data-act="inst-icon" data-iid="' + _escAttr(d.instance_id) + '" title="Click to change icon"' : '')
    + '>' + _instIconInner(d) + '</span>';
  const nameInner = editable
    ? '<span class="inst-name-edit" data-iid="' + _escAttr(d.instance_id) + '" title="Double-click or long-press to edit" data-has-long-press="true">' + _esc(_instName(d)) + '</span>'
    : _esc(_instName(d));

  return '<div class="inst-detail">'
    + '<div class="inst-detail-sticky">'
    +   '<div class="inst-detail-head">'
    +     iconWrap
    +     '<div class="inst-detail-titles">'
    +       '<span class="inst-detail-name">' + nameInner
    +         (d.is_self ? ' <span class="inst-self-badge">This device</span>' : '') + '</span>'
    +       '<span class="inst-detail-sub">' + _esc(sub) + '</span>'
    +     '</div>'
    +     '<button class="inst-detail-close" data-act="close" title="Close"><i data-lucide="x"></i></button>'
    +   '</div>'
    +   (headActions ? '<div class="inst-detail-head-actions">' + headActions + '<span class="inst-ping-status"></span></div>' : '')
    +   '<div class="inst-tabs">' + tabs + '</div>'
    + '</div>'
    + '<div class="inst-detail-body">' + _renderTab(d) + '</div>'
    + '</div>';
}

// The "New instance" detail card — same shell as an instance's detail (sticky head
// + tab bar) but with TWO tabs: the existing "New Deployment" tab (stock install,
// body filled by the new-deployment sub-feature) and the new "New Instance" tab
// (make-your-own-copy wizard, filled by new-instance/mountNewInstance). The active
// tab is S.tab (defaults to new-deploy); each tab's body is an empty host that its
// own mount module fills, so the fetched fragments never bloat this render.
function _newDetailHtml() {
  const niTab = S.tab === 'new-instance';
  const tabs =
      '<button class="inst-tab' + (niTab ? '' : ' active') + '" data-tab="new-deploy">New Deployment</button>'
    + '<button class="inst-tab' + (niTab ? ' active' : '') + '" data-tab="new-instance">New Instance</button>';
  // The New-Deployment host starts with a lightweight SKELETON of the four collapsed
  // bars so the card is never blank while the real form loads (mountNewDeployment
  // removes it once the fetched fragment is attached — see new-deployment.js
  // _clearSkeleton). The New-Instance host mounts straight into (its wizard fragment
  // is tiny and prewarmed at idle).
  const body = niTab
    ? '<div class="inst-detail-body"><div id="inst-ni-host" class="inst-ni-host"></div></div>'
    : '<div class="inst-detail-body"><div id="inst-nd-host" class="inst-nd-host">' + _ndSkeletonHtml() + '</div></div>';
  return '<div class="inst-detail inst-detail-new">'
    + '<div class="inst-detail-sticky">'
    +   '<div class="inst-detail-head">'
    +     '<span class="inst-icon-wrap plat-cloud"><i data-lucide="plus"></i></span>'
    +     '<div class="inst-detail-titles">'
    +       '<span class="inst-detail-name">New instance</span>'
    +       '<span class="inst-detail-sub">Deploy WebAgent somewhere new</span>'
    +     '</div>'
    +     '<button class="inst-detail-close" data-act="close" title="Close"><i data-lucide="x"></i></button>'
    +   '</div>'
    +   '<div class="inst-tabs">' + tabs + '</div>'
    + '</div>'
    + body
    + '</div>';
}

// Four phantom deploy bars shown instantly in the New instance card while the real
// New-Deployment fragment (+ its heavy deploy.js/dns.js) loads. Mirrors the four
// real bars' collapsed shape (icon chip · title/desc lines · chevron) so the swap
// to the live form is seamless. Styling: .inst-nd-skeleton in instances.css.
function _ndSkeletonHtml() {
  const row = () =>
      '<div class="inst-nd-skel-row">'
    +   '<span class="inst-nd-skel-ico"></span>'
    +   '<div class="inst-nd-skel-lines"><span class="inst-nd-skel-line lg"></span><span class="inst-nd-skel-line sm"></span></div>'
    +   '<span class="inst-nd-skel-chev"></span>'
    + '</div>';
  return '<div class="inst-nd-skeleton" aria-hidden="true">' + row() + row() + row() + row() + '</div>';
}

// ── Full render ──────────────────────────────────────────────────────────────
function _render() {
  const el = _root(); if (!el) return;

  _configDetailResizeObserver?.disconnect();
  _configDetailResizeObserver = null;

  // Preserve scroll position across the wholesale innerHTML rebuild below. Both
  // scroll containers (.inst-grid = the vertical page scroll; .inst-squares = the
  // horizontal tile carousel) are children of #inst-root, so replacing el's
  // innerHTML destroys them and their scrollTop/scrollLeft reset to 0 — which is
  // why a background poll or opening any menu used to yank the page back to the
  // top. Capture the offsets now and restore them onto the fresh nodes after.
  const _prevGrid = el.querySelector('.inst-grid');
  const _prevTop = _prevGrid ? _prevGrid.scrollTop : 0;
  const _prevSquares = el.querySelector('.inst-squares');
  const _prevLeft = _prevSquares ? _prevSquares.scrollLeft : 0;
  _settingsRenderGeneration++;

  // Before rebuilding innerHTML, return the app-config container to a
  // safe zone outside #inst-root (it may have been moved into #inst-root
  // by _onSettingsTabRendered, and innerHTML would destroy it).
  const _acC = _getAppConfigContainer();
  const _acH = document.body;
  if (_acC && _acH && _acC.parentElement !== _acH) {
    _restoreConfigurationAssistant(_acC);
    _acH.appendChild(_acC);
    _acC.hidden = true;   // was unhidden by _onSettingsTabRendered; hide it now that settings is leaving
  }
  // Unmount the embedded dashboard before the innerHTML rebuild destroys its DOM.
  // When the dashboard tab is active, skip the async unmount (the DOM is about to be
  // destroyed by innerHTML anyway) and just reset the flag so the re-mount below
  // (via _onDashboardTabRendered) gets a clean state.
  if (S.tab === 'dashboard') {
    _dashboardMounted = false;
  } else {
    _unmountDashboard();
  }
  // Same reset for the Users tab before the innerHTML rebuild destroys its DOM.
  if (S.tab === 'users') {
    _usersMounted = false;
  } else {
    _unmountUsers();
  }

  const list = _list();
  const online = list.filter(d => d.online).length;
  const stub = !!(S.devices && S.devices._stub);

  let sub;
  if (S.loading) {
    sub = (list.length && !stub) ? ('Refreshing… · ' + list.length + ' total') : 'Loading fleet…';
  } else {
    sub = list.length ? (online + ' online · ' + list.length + ' total') : 'No instances found';
  }
  const spinner = S.loading ? '<span class="inst-spin"><i data-lucide="loader-2"></i></span>' : '';

  // An open tile flips the grid into carousel mode (single scrolling row of tiles
  // + a sticky detail header) — mirrors the main Agents page. The "New instance"
  // create tile counts as open too (it opens the New Deployment detail).
  const activeInst = (S.active && S.active !== NEW_DEPLOY_ID) ? _find(S.active) : null;
  const isNew = S.active === NEW_DEPLOY_ID;
  const carousel = !!activeInst || isNew;

  // The New instance tile is ALWAYS first, then the fleet tiles. The wrap +
  // chevrons render in both modes; the chevrons + edge fades only reveal in
  // carousel mode (CSS), via the can-scroll-* classes _wireCarousel sets.
  let grid = '<div class="inst-squares-wrap">'
    +   '<button type="button" class="inst-carousel-chev left" aria-label="Scroll instances left" tabindex="-1">❮</button>'
    +   '<button type="button" class="inst-carousel-chev right" aria-label="Scroll instances right" tabindex="-1">❯</button>'
    +   '<div class="inst-squares">' + _newSquareHtml() + list.map(_squareHtml).join('') + '</div>'
    + '</div>';
  if (activeInst) grid += _detailHtml(activeInst);
  else if (isNew) grid += _newDetailHtml();
  else if (!list.length) grid += '<div class="inst-empty"><i data-lucide="boxes"></i><div>No instances yet. Any machine running WebAgent against '
    + 'this database appears here automatically — as does any cloud server in a connected cloud account. Use the New instance tile to deploy one.</div></div>';

  el.innerHTML =
      '<div class="inst-grid' + (carousel ? ' carousel' : '') + '">'
    +   '<div class="inst-toolbar">'
    +     '<div class="inst-toolbar-titles">'
    +       '<span class="inst-toolbar-title"><i data-lucide="boxes"></i> Instances</span>'
    +       '<span class="inst-toolbar-sub">' + spinner + _esc(sub) + '</span>'
    +     '</div>'
    +     '<button class="inst-tb-btn" data-act="refresh"' + (S.loading ? ' disabled' : '') + '><i data-lucide="refresh-cw"></i> Refresh</button>'
    +   '</div>'
    +   _degradedBannerHtml()
    +   grid
    + '</div>';

  // Restore the pre-rebuild scroll offsets onto the freshly-created nodes so a
  // background poll / menu toggle re-render doesn't jump the page to the top or
  // reset the tile carousel (see the capture at the top of _render).
  const _newGrid = el.querySelector('.inst-grid');
  if (_newGrid && _prevTop) _newGrid.scrollTop = _prevTop;
  const _newSquares = el.querySelector('.inst-squares');
  if (_newSquares && _prevLeft) _newSquares.scrollLeft = _prevLeft;

  _refreshLucideIcons(el);
  if (carousel) _wireCarousel(el);
  _httpsSiblingUpdate();
  // Mount (or re-parent) the active New-tile tab into its freshly-rendered host:
  // the New Deployment form or the New Instance wizard. Each mount module
  // re-parents its persistent node so typed state survives re-renders.
  if (isNew && S.tab === 'new-instance') {
    const host = document.getElementById('inst-ni-host');
    if (host) _mountNewInstance(host);
  } else if (isNew) {
    const host = document.getElementById('inst-nd-host');
    if (host) _mountNewDeploy(host);
  }
  // Initialize app config if the settings tab is being shown
  _onSettingsTabRendered();
  // Mount the embedded dashboard if the dashboard tab is being shown
  _onDashboardTabRendered();
  // Mount the embedded users tab if the users tab is being shown
  _onUsersTabRendered();
  _syncConfigurationStickyOffset(el);
  // Start (or restart) the overview smart-poll when a device tile's Overview is open.
  if (activeInst && S.tab === 'overview' && activeInst.kind === 'device') {
    _startOverviewPoll(activeInst);
    // When "This device"'s Overview opens, ask the backend to detect the machine's
    // local-network URLs (http://<lan-ip>:<port>/) and add them to the URL list by
    // default — they work for anyone on the same network even with no signal and
    // no users actually using them.
    if (activeInst.is_self) _discoverLocalUrls(activeInst);
  } else {
    _stopOverviewPoll();
  }
}

// ── Squares carousel wiring (drag-to-scroll + chevron/edge-fade affordances) ──
// Ported from the Agents page's _wireSquaresCarousel (ui/main-panel/agents/js/
// view.js). Re-run on every _render, since _render rebuilds the DOM wholesale;
// the old listeners die with the old nodes.
function _wireCarousel(root) {
  const wrap = root.querySelector('.inst-squares-wrap'); if (!wrap) return;
  const scroller = wrap.querySelector('.inst-squares'); if (!scroller) return;
  const chevLeft  = wrap.querySelector('.inst-carousel-chev.left');
  const chevRight = wrap.querySelector('.inst-carousel-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd   = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };
  scroller.addEventListener('scroll', updateAffordances, { passive: true });
  applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  setTimeout(updateAffordances, 120);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(updateAffordances).observe(scroller);

  const page = () => Math.max(scroller.clientWidth * 0.7, 132);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left:  page(), behavior: 'smooth' }); });

  let dragging = false, startX = 0, startScroll = 0, moved = false;
  scroller.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    dragging = true; moved = false;
    startX = e.clientX; startScroll = scroller.scrollLeft;
  });
  scroller.addEventListener('pointermove', e => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    if (Math.abs(dx) > 4) { moved = true; try { scroller.setPointerCapture(e.pointerId); } catch (_) {} }
    if (moved) { scroller.scrollLeft = startScroll - dx; e.preventDefault(); }
  });
  const endDrag = e => {
    if (!dragging) return;
    dragging = false;
    try { scroller.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  scroller.addEventListener('pointerup', endDrag);
  scroller.addEventListener('pointercancel', endDrag);
  // Swallow the click that ends a drag so it doesn't toggle a tile open/closed.
  scroller.addEventListener('click', e => {
    if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; }
  }, true);
}

// ── Loads ────────────────────────────────────────────────────────────────────
// Devices (the presence registry) — drives instant paint + the 20s poll.
async function _load(foreground) {
  if (S.busy) return;
  S.busy = true;
  if (foreground) { S.loading = true; if (!S._renderSuppressed) _render(); }
  try {
    const r = await _get('/admin/instances/devices').catch(() => null);
    if (r && r.body && Array.isArray(r.body.devices)) {
      r.body.devices.forEach(d => { d.kind = 'device'; });
      S.devices = r.body;
      S.self = r.body.self || S.self;
      _rememberSelfId(S.self);
      _writeCache(r.body);
      // NEW_DEPLOY_ID is a VIRTUAL tile (prepended in _render, never in _list()),
      // so _find() can't see it — exempt it or every completed load would close
      // the open New instance card.
      if (S.active && S.active !== NEW_DEPLOY_ID && !_find(S.active) && !_activePending()) {
        // The stub id resolves to the real self row once it loads; any other
        // vanished selection closes (and clears its saved memory via _setActive).
        _setActive((S.active === _STUB_ID) ? (S.self || null) : null);
      }
    }
  } finally {
    S.busy = false;
    S.loading = false;
    if (!S._renderPending && !S._renderSuppressed) _render();
  }
}

// ── Local-network URL discovery ─────────────────────────────────────────────
// When the Overview tab opens for "This device", ask the backend to enumerate
// this machine's LAN IPs (http://192.168.x.x:<port>/) and add them to the URL
// list by default — they work for anyone on the same network even when no
// heartbeat signal has ever been seen and no users are using them. Runs once per
// session per device. Instead of triggering a full re-render via _load(false),
// we merge the new URLs directly into the cached device data and patch the URL
// row in-place — same lightweight approach as the old overview poll.
const _lanDiscovered = new Set();
async function _discoverLocalUrls(inst) {
  if (!inst || _lanDiscovered.has(inst.instance_id)) return;
  _lanDiscovered.add(inst.instance_id);
  try {
    const r = await _post('/admin/instances/discover-local-urls', {});
    if (r.ok && r.body && Array.isArray(r.body.urls) && r.body.urls.length) {
      // Merge the discovered URLs into the cached device data so future renders
      // include them, then patch the URL facts row in-place without a full rebuild.
      const devices = (S.devices && S.devices.devices) || [];
      const dev = devices.find(function(d) { return d.instance_id === inst.instance_id; });
      if (dev) {
        dev.urls = dev.urls || {};
        r.body.urls.forEach(function(u) {
          if (!dev.urls[u]) dev.urls[u] = { url: u, local_net: true, last_seen: null };
        });
      }
      _patchUrlFact(inst);
    }
  } catch (_) {}
}
function _startOverviewPoll(inst) {
  // Overview smart-poll removed — the main fleet poll (20s) already fetches
  // fresh device data, and the on-return _patchUrlFacts() call keeps URL rows
  // current. A separate 10s poll was redundant and doubled the request load.
}

function _stopOverviewPoll() {
  if (S._overviewPollTimer) { clearInterval(S._overviewPollTimer); S._overviewPollTimer = null; }
  S._overviewPollData = null;
}

// Patch ALL open URL fact rows from the latest device data after the fleet poll.
// Same in-place DOM surgery the old overview poll did, but driven by the single
// fleet poll instead of a separate 10s interval.
function _patchUrlFactsIfOverview() {
  if (S.tab !== 'overview' || !S.active) return;
  const devices = (S.devices && S.devices.devices) || [];
  const fresh = devices.find(function(d) { return d.instance_id === S.active; });
  if (fresh) _patchUrlFact(fresh);
}

// Snapshot the URLs for change detection.
function _serializeUrls(inst) {
  if (!inst || !inst.urls) return '{}';
  return JSON.stringify(inst.urls);
}

// Patch the URL fact row's value in-place by re-running _urlFactHtml on fresh
// device data — this preserves tunnel rows, custom URLs, and the "+" button.
function _patchUrlFact(inst) {
  var fact = document.querySelector('.inst-detail [data-fact="url"] .inst-fact-value');
  if (!fact) return;
  var tmp = document.createElement('div');
  tmp.innerHTML = _urlFactHtml(inst);
  var newVal = tmp.querySelector('.inst-fact-value');
  if (newVal) fact.innerHTML = newVal.innerHTML;
}

// Extract a provider URL from a tunnel-slave job result string.
function _headfulUrlOf(text) {
  if (!text) return '';
  var m = /https:\/\/[a-z0-9.-]+\.(?:trycloudflare\.com|ngrok(?:-free)?\.app|ngrok\.io)/i.exec(String(text));
  return m ? m[0] : '';
}

// Stamp a freshly-resolved slave tunnel URL into the instance's local state and
// re-render the active tile's URL section in place (no full page render), so the
// URL appears the moment the launch completes instead of waiting for a heartbeat.
function _applyHeadfulUrl(iid, url) {
  var inst = _find(iid);
  if (!inst) return;
  if (!inst.tunnel) inst.tunnel = {};
  inst.tunnel.headful_url = url || '';
  inst.tunnel.public_url = url || '';
  inst.tunnel.running = !!url;
  if (url) {
    var now = new Date().toISOString();
    inst.tunnel.connected_at = now;
    if (!inst.urls || typeof inst.urls !== 'object') inst.urls = {};
    var normalized = String(url).replace(/\/+$/, '');
    var previous = inst.urls[normalized] || inst.urls[normalized + '/'] || {};
    delete inst.urls[normalized + '/'];
    inst.urls[normalized] = Object.assign({}, previous, { last_seen: now });
  }
  if (inst.instance_id === S.active) _patchUrlFact(inst);
}

// DB + vault health/config (for the Connections cards + degraded banner).
async function _loadConn() {
  const [stat, cfg] = await Promise.all([
    _get('/admin/instances/backend-status').catch(() => ({ body: null })),
    _get('/admin/storage/config').catch(() => ({ body: null })),
  ]);
  S.status = (stat && stat.body) || S.status || {};
  S.config = (cfg && cfg.body) || S.config || {};
  S.sel.db = null; S.sel.vault = null;
  if (!S._renderSuppressed) _render();
}

// This machine's own WebAgent server(s) — the hub (this app) + any registered
// sibling checkouts — for the "Server" section on the This device tile. Backed by
// /admin/deploy/instances (app/local_instances.py). Cheap; refreshed on the poll.
async function _loadLocal() {
      const r = await _get('/admin/deploy/instances').catch(() => null);
      if (r && r.body && Array.isArray(r.body.instances)) S.local = r.body;
      else if (S.local === null) S.local = { instances: [], hub_port: parseInt(_localPort(), 10) || 8080 };
      if (!S._renderPending && !S._renderSuppressed) _render();
    }

async function _loadProviders() {
  const r = await _get('/admin/instances/providers').catch(() => null);
  const list = (r && r.body && r.body.providers) || [];
  S.providers = list.filter(p => p.available !== false || p.has_key || _hasSavedId(p));
}

// Cloud VMs across every connected account → normalized tiles merged into the grid.
async function _loadCloud(foreground) {
  if (S.providers === null) await _loadProviders();
  S.cloudLoading = true;  // show skeleton while loading
  if (!S._renderPending && S.active && S.active.startsWith('cloud:')) _render();
  const accounts = _accounts().filter(_isConnected);
  const parts = await Promise.all(accounts.map(async (p) => {
    const r = await _get('/admin/instances/cloud-instances?provider=' + encodeURIComponent(p.id)).catch(() => null);
    const data = (r && r.body) || {};
    if (!Array.isArray(data.instances)) return [];
    return data.instances.map(inst => _normalizeCloud(inst, p));
  }));
  S.cloud = parts.flat();
  S.cloudLoading = false;
  // Exempt the virtual New instance tile (never in _list(); see _load's note).
  if (S.active && S.active !== NEW_DEPLOY_ID && !_find(S.active) && !_activePending()) _setActive(null);   // an open cloud tile that vanished
  if (!S._renderPending && !S._renderSuppressed) _render();
}

function _normalizeCloud(inst, p) {
  const sm = _statusMeta(inst.status);
  const ref = String(p.id) + '|' + String(inst.zone) + '|' + String(inst.name);
  return {
    kind: 'cloud', instance_id: 'cloud:' + ref, label: inst.name, online: sm.online,
    platform: 'cloud', endpoint: inst.ip ? ('http://' + inst.ip) : '', repo: inst.repo || '', branch: '',
    provider: p.id, providerName: p.display_name, zone: inst.zone, name: inst.name,
    project: inst.project || '',
    status: inst.status, machine_type: inst.machine_type || '', ip: inst.ip || '', created: inst.created || '',
    is_this_app: !!inst.is_this_app, is_webagent: !!inst.is_webagent, annotation_key: inst.annotation_key || '',
    domain: inst.domain || '', domains: inst.domains || [],
    _ref: ref,
  };
}

// ── Output-box + status helpers ──────────────────────────────────────────────
function _out(id, text, ok) {
  const el = document.getElementById(id); if (!el) return;
  el.hidden = false;
  el.classList.toggle('inst-out-ok', ok === true);
  el.classList.toggle('inst-out-err', ok === false);
  el.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
}
function _pingStatus(text, kind) {
  const el = document.querySelector('.inst-ping-status'); if (!el) return;
  if (kind && kind.includes('link')) {
    el.innerHTML = text || '';
  } else {
    el.textContent = text || '';
  }
  el.className = 'inst-ping-status' + (kind === 'ok' ? ' ok' : kind === 'err' ? ' err' : kind === 'ok link' ? ' ok' : '');
}

// ── DB actions (talk to /admin/storage/db/*) ─────────────────────────────────
function _collectDb() {
  const prov = document.getElementById('inst-db-provider');
  const provider = prov ? prov.value : 'sqlite';
  const o = { provider };
  (DB_FIELDS[provider] || []).forEach(f => {
    const inp = document.querySelector('#inst-db-fields [data-fk="' + _cssEsc(f.key) + '"]');
    if (!inp) return;
    let v = inp.value;
    if (f.type === 'number' && v) v = parseInt(v, 10);
    if (v === '') v = null;
    o[f.key] = v;
  });
  return o;
}
async function _dbTest() {
  _out('inst-db-output', 'Testing connection…', null);
  const r = await _post('/admin/storage/db/test', _collectDb());
  _out('inst-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbSave() {
  _out('inst-db-output', 'Saving config…', null);
  const r = await _post('/admin/storage/db/config', _collectDb());
  _out('inst-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbSql() {
  const provider = (document.getElementById('inst-db-provider') || {}).value || 'sqlite';
  const dialect = provider === 'sqlite' ? 'sqlite' : 'postgres';
  const r = await _post('/admin/storage/db/schema-sql', { dialect, idempotent: true });
  if (r.body && r.body.ddl) _out('inst-db-output', r.body.ddl, true);
  else _out('inst-db-output', r.body || { error: 'no response' }, false);
}
async function _dbBootstrap() {
  if (!window.confirm('Connect to the configured database and create all WebAgent tables there? Idempotent.')) return;
  _out('inst-db-output', 'Creating tables…', null);
  const r = await _post('/admin/storage/db/bootstrap', _collectDb());
  if (r.body && r.body.needs_manual_sql && r.body.error) { _out('inst-db-output', r.body.error, false); return; }
  _out('inst-db-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
}
async function _dbActivate() {
  if (!window.confirm('Sign in to this database for the LIVE app? New requests use it immediately.')) return;
  _out('inst-db-output', 'Saving config…', null);
  const saved = await _post('/admin/storage/db/config', _collectDb());
  if (!(saved.body && saved.body.ok)) { _out('inst-db-output', saved.body || { error: 'Could not save config.' }, false); return; }
  _out('inst-db-output', 'Activating…', null);
  const r = await _post('/admin/storage/db/activate', {});
  const ok = !!(r.body && r.body.ok);
  if (!ok && r.body && typeof r.body.error === 'string') _out('inst-db-output', r.body.error, false);
  else _out('inst-db-output', r.body || { error: 'no response' }, ok);
  _loadConn(); _load(false);
}

// ── Vault actions (talk to /admin/storage/secrets/* + /server/*) ─────────────
function _collectVault() {
  const prov = document.getElementById('inst-vault-provider');
  const provider = prov ? prov.value : 'inline_db';
  const out = { config: {}, token: undefined };
  (VAULT_FIELDS[provider] || []).forEach(f => {
    const inp = document.querySelector('#inst-vault-fields [data-fk="' + _cssEsc(f.key) + '"]');
    if (!inp) return;
    if (f.secret) { if (inp.value) out.token = inp.value; }
    else out.config[f.key] = (inp.value || '').trim();
  });
  return { provider, ...out };
}
async function _saveVaultCfg() {
  const { provider, config, token } = _collectVault();
  if (!VAULT_FIELDS[provider]) return null;
  return _post('/admin/storage/secrets/provider-config', { provider, config, token });
}
async function _vaultTest() {
  const provider = (document.getElementById('inst-vault-provider') || {}).value || 'inline_db';
  const saved = await _saveVaultCfg();
  if (saved && !(saved.body && saved.body.ok)) { _out('inst-vault-output', saved.body || { error: 'could not save details' }, false); return; }
  _out('inst-vault-output', 'Testing…', null);
  const r = await _post('/admin/storage/secrets/test', { provider });
  _out('inst-vault-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  _loadConn();
}
async function _vaultActivate() {
  const provider = (document.getElementById('inst-vault-provider') || {}).value || 'inline_db';
  if (!window.confirm('Sign in to the "' + provider + '" vault?')) return;
  const saved = await _saveVaultCfg();
  if (saved && !(saved.body && saved.body.ok)) { _out('inst-vault-output', saved.body || { error: 'could not save details' }, false); return; }
  _out('inst-vault-output', 'Activating…', null);
  const r = await _post('/admin/storage/secrets/mode', { provider });
  _out('inst-vault-output', r.body || { error: 'no response' }, !!(r.body && r.body.ok));
  _loadConn();
}
const _sleep = (ms) => new Promise(r => setTimeout(r, ms));
function _probeHealth() { return fetch(apiPath('/health'), { cache: 'no-store' }).then(r => r.ok).catch(() => false); }
async function _vaultRestart(btn) {
  const statusEl = btn && btn.parentElement && btn.parentElement.querySelector('.smb-restart-status');
  const set = (t) => { if (statusEl) statusEl.textContent = t; };
  if (btn) btn.disabled = true;
  set('Checking…');
  const info = await _get('/admin/storage/server/restart-info');
  if (!info.body || !info.body.auto_restart_available) {
    if (btn) btn.disabled = false;
    set((info.body && info.body.reason) || 'Automatic restart isn\'t available here — restart the server manually.');
    return;
  }
  set('Restarting…');
  const r = await _post('/admin/storage/server/restart', {});
  if (!r.ok) { if (btn) btn.disabled = false; set((r.body && r.body.detail) || 'Restart could not be started.'); return; }
  const downBy = Date.now() + 30000;
  while (Date.now() < downBy) { if (!(await _probeHealth())) break; await _sleep(700); }
  set('Waiting for the server to come back…');
  const upBy = Date.now() + 180000;
  while (Date.now() < upBy) { if (await _probeHealth()) { set('Server restarted ✓'); _loadConn(); return; } await _sleep(1000); }
  if (btn) btn.disabled = false;
  set('Server hasn\'t answered yet — give it a moment, then Refresh.');
}

// ── Cloud VM actions (Ping / Start / Stop / Delete + live log) ───────────────
async function _doPing(address, btn) {
  _pingStatus('Checking ' + address + '…', null);
  if (btn) btn.disabled = true;
  try {
    const r = await _post('/admin/instances/ping', { address });
    const reach = r.body && r.body.reachable;
    _pingStatus((r.body && r.body.detail) || (reach ? 'Reachable' : 'Unreachable'), reach ? 'ok' : 'err');
  } catch (e) {
    _pingStatus('Ping failed', 'err');
  } finally { if (btn) btn.disabled = false; }
}

// Start / stop the detached tunnel slave through the target's device queue.
async function _tunnelAction(action, iid, btn) {
  if (!iid) return;
  if (action === 'stop' && !window.confirm('Stop the tunnel on this instance?\n\nIts public tunnel address stops working until you start it again.')) return;
  if (btn) btn.disabled = true;
  _pingStatus(action === 'stop' ? 'Sending stop-tunnel to the device…' : 'Sending start-tunnel to the device…', null);
  try {
    const r = await _post('/admin/instances/tunnel/control', { instance_id: iid, action });
    if (!r.ok) {
      _pingStatus((r.body && r.body.detail) || 'Could not send the tunnel command.', 'err');
      if (btn) btn.disabled = false;
      return;
    }
    const result = (r.body && r.body.result) || '';
    const url = _headfulUrlOf(result);
    if (action === 'stop') {
      _applyHeadfulUrl(iid, '');
      _pingStatus('Stop sent — the device will shut its tunnel down shortly.', 'ok');
    } else if (url) {
      _applyHeadfulUrl(iid, url);
      _pingStatus('Tunnel ready: <a href="' + _escAttr(_openUrl(url)) + '" target="_blank" rel="noopener">' + _esc(url) + '</a>', 'ok link');
    } else if (result.indexOf('Managed fallback') === 0) {
      _pingStatus('The slave could not launch, so the managed tunnel was started instead. ' + _esc(result), 'ok');
    } else {
      _pingStatus(_esc(result || 'Start sent — the device is bringing its tunnel up.'), 'ok');
    }
    [1500, 3500, 7000, 12000, 20000].forEach(ms => setTimeout(() => _load(false), ms));
  } catch (e) {
    _pingStatus('Tunnel command failed: ' + _esc(e.message || ''), 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Restart / Pull / Commit & push a fleet device. Dispatched over the device queue
// like the tunnel — the target machine runs it on its own repo — so there's no
// live stream here. Git actions leave no visible heartbeat state, so after sending
// we poll the job's outcome a few times and show a plain result line; a restart
// instead re-polls the fleet so the tile flips Offline→Online as it comes back.
const _DEVICE_ACTION_LABEL = {
  restart: 'restart', git_pull: 'pull', git_commit_push: 'commit & push',
};
async function _deviceControl(action, iid, btn) {
  if (!iid) return;
  if (action === 'restart' && !window.confirm('Restart WebAgent on this device?\n\nIt goes offline for a few seconds while it comes back up.')) return;
  if (action === 'git_commit_push' && !window.confirm('Commit ALL changes on this device and push?\n\nEverything currently changed in its app repo is staged, committed with an auto-written message, and pushed.')) return;
  const label = _DEVICE_ACTION_LABEL[action] || action;
  if (btn) btn.disabled = true;
  _pingStatus('Sending ' + label + ' to the device…', null);
  try {
    const r = await _post('/admin/instances/device/control', { instance_id: iid, action });
    if (!r.ok) {
      _pingStatus((r.body && r.body.detail) || ('Could not send the ' + label + ' command.'), 'err');
      if (btn) btn.disabled = false;
      return;
    }
    if (action === 'restart') {
      _pingStatus('Restart sent — the device is coming back up (this takes a few seconds).', 'ok');
      [3000, 8000, 15000, 25000].forEach(ms => setTimeout(() => _load(false), ms));
      return;   // leave disabled; the tile re-renders on reload
    }
    _pingStatus('Sent — waiting for the device to finish…', null);
    _pollDeviceAction((r.body && r.body.job_id) || '', label, btn);
  } catch (e) {
    _pingStatus('The ' + label + ' command failed.', 'err');
    if (btn) btn.disabled = false;
  }
}

// Poll a dispatched git action's outcome and surface the result inline. The target
// acts within its poll window (instant on this box, up to ~15s remote), so we check
// a handful of times before giving up on a plain "still running" note.
function _pollDeviceAction(jobId, label, btn) {
  if (!jobId) { if (btn) btn.disabled = false; return; }
  const delays = [1500, 3500, 7000, 12000, 20000];
  let i = 0;
  const tick = async () => {
    let done = false;
    try {
      const r = await _get('/admin/instances/device/action-status?job_id=' + encodeURIComponent(jobId));
      const b = r.body || {};
      if (b.status === 'done') {
        _pingStatus(b.result || (label + ' done'), 'ok');
        done = true;
      } else if (b.status === 'error' || b.status === 'skipped') {
        _pingStatus((b.error || b.result || (label + ' failed')), 'err');
        done = true;
      }
    } catch (e) { /* keep polling */ }
    if (done) { if (btn) btn.disabled = false; return; }
    if (i < delays.length) { setTimeout(tick, delays[i++]); }
    else {
      _pingStatus('Still running on the device — check back shortly.', null);
      if (btn) btn.disabled = false;
    }
  };
  setTimeout(tick, delays[i++]);
}

function _confirmText(action, name) {
  if (action === 'delete') return 'Delete the server “' + name + '”?\n\nThis permanently destroys it and its disk. This cannot be undone.';
  if (action === 'stop') return 'Stop the server “' + name + '”?\n\nIt will go offline until you start it again.';
  return 'Start the server “' + name + '”?\n\nThis resumes the server (and its billing).';
}
function _logReset() { const log = document.getElementById('inst-log'); if (log) { log.hidden = false; log.textContent = ''; } }
function _logLine(text, level) {
  const log = document.getElementById('inst-log'); if (!log) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : level === 'warn' ? '! ' : '· ';
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}
// True when the typed confirm box matches the server name it's guarding.
function _delMatches() {
  const inp = document.getElementById('inst-del-confirm');
  if (!inp) return false;
  return (inp.value || '').trim() === (inp.dataset.expect || '');
}

// Unlink a STALE device from the shared registry. Reached only after the admin
// completed the press-and-hold on the button (_holdStart), so the deliberate hold
// IS the confirmation — no popup. The button is only offered on offline, non-self
// devices (and the endpoint re-checks), and this clears the record, not the
// machine: a live device pointed here would just re-link on its next check-in.
async function _unlinkDevice(iid, name) {
  if (!iid) return;
  const r = await _post('/admin/instances/device/unlink', { instance_id: iid });
  if (!r.ok) { window.alert((r.body && r.body.detail) || 'Could not unlink that device.'); return; }
  if (S.active === iid) _setActive(null);   // its detail card is going away
  _load(true);
}

async function _doCloudAction(action, ref, name) {
  const [provider, zone, vmName] = String(ref || '').split('|');
  if (!provider || !zone || !vmName) return;
  // Delete already passed the inline type-to-confirm gate; start/stop still use a
  // quick browser confirm.
  if (action !== 'delete' && !window.confirm(_confirmText(action, name || vmName))) return;
  S.cloudBusy = true;
  _render();
  _logReset();
  const verb = action === 'delete' ? 'Deleting' : action === 'stop' ? 'Stopping' : 'Starting';
  _logLine(verb + ' ' + (name || vmName) + '…');
  try {
    await _stream('/admin/instances/instance/action', { provider, action, zone, name: vmName }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally {
    S.cloudBusy = false;
    await _loadCloud(false);   // re-renders with fresh status (log element is rebuilt)
  }
}

function _cloudRunLogLine(iid, text, level) {
  const state = S.cloudRunDeploy.get(iid);
  if (!state) return;
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ '
    : level === 'warn' ? '! ' : '· ';
  state.lines.push(mark + String(text || ''));
  const log = document.getElementById('inst-log');
  if (log && S.active === iid) {
    log.hidden = false;
    log.textContent = state.lines.join('\n') + '\n';
    log.scrollTop = log.scrollHeight;
  }
}

async function _doCloudRunBuildDeploy(iid, service) {
  if (!iid || S.cloudBusy) return;
  if (!window.confirm(
    'Build the configured repository and deploy a new revision to “'
      + (service || 'this Cloud Run service') + '”?\n\n'
      + 'The current revision stays active until the new revision is ready.'
  )) return;
  const state = { busy: true, lines: [] };
  S.cloudRunDeploy.set(iid, state);
  S.cloudBusy = true;
  _render();
  _cloudRunLogLine(iid, 'Starting Cloud Run build and deployment…');
  try {
    await _stream(
      '/admin/instances/cloud-run/build-deploy',
      { instance_id: iid },
      (result) => _cloudRunLogLine(
        iid,
        result.message || (result.ok ? 'Deployment complete.' : 'Deployment failed.'),
        result.ok ? 'ok' : 'err',
      ),
      (event) => _cloudRunLogLine(iid, event.message, event.level),
    );
  } catch (e) {
    _cloudRunLogLine(iid, e.message, 'err');
  } finally {
    state.busy = false;
    S.cloudBusy = false;
    _render();
  }
}
// Repo annotation edit (cloud VMs). key = the VM's annotation_key.
async function _editRepo(key) {
  if (!key) return;
  const next = window.prompt('Which code repo does this run?\n\nEnter "owner/repo" or a git URL, or leave blank to clear.');
  if (next === null) return;
  await _post('/admin/instances/annotate', { key, repo: next.trim() });
  _loadCloud(false);
}

// Clicking the "+" span or pencil span triggers inline editing of the custom
// tunnel URL. Replaces the element with an input + Save/Cancel icon buttons.
function _startTunnelUrlEdit(el) {
  var key = el.dataset.key;
  var currentUrl = el.dataset.url || '';

  // Prevent background polls from re-rendering while editing
  S._tunEditing = true;

  // If editing an existing URL (pencil button), hide the action row
  var customRow = el.closest('.inst-tun-custom');
  if (customRow) {
    var urlRow = customRow.querySelector('.inst-url-row');
    var actBtns = customRow.querySelectorAll('.inst-tun-act-btn');
    customRow.classList.add('inst-tun-editing');
    var row = document.createElement('span');
    row.className = 'inst-tun-input-row';
    row.innerHTML = '<input class="inst-tun-input" type="text" value="' + _escAttr(currentUrl) + '" placeholder="https://…">'
      + '<span class="inst-tun-act-btn" data-act="tunnel-save-url" data-key="' + _escAttr(key) + '" title="Save"><i data-lucide="check"></i></span>'
      + '<span class="inst-tun-act-btn" data-act="tunnel-cancel-edit" data-key="' + _escAttr(key) + '" title="Cancel"><i data-lucide="x"></i></span>';
    customRow.appendChild(row);
    _refreshLucideIcons(row);
    var inp = row.querySelector('.inst-tun-input');
    _tunInputWire(inp, key);
    setTimeout(function () { inp.focus(); inp.select(); }, 50);
    return;
  }

  // Adding new: replace the "+" span with an input row
  var row = document.createElement('span');
  row.className = 'inst-tun-input-row';
  row.innerHTML = '<input class="inst-tun-input" type="text" placeholder="https://…">'
    + '<span class="inst-tun-act-btn" data-act="tunnel-save-url" data-key="' + _escAttr(key) + '" title="Save"><i data-lucide="check"></i></span>'
    + '<span class="inst-tun-act-btn" data-act="tunnel-cancel-edit" data-key="' + _escAttr(key) + '" title="Cancel"><i data-lucide="x"></i></span>';
  el.replaceWith(row);
  _refreshLucideIcons(row);
  var inp = row.querySelector('.inst-tun-input');
  _tunInputWire(inp, key);
  setTimeout(function () { inp.focus(); }, 50);
}

function _tunInputWire(inp, key) {
  var done = false;
  inp.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); done = true; _saveTunnelUrl(key); }
    else if (e.key === 'Escape') { e.preventDefault(); done = true; _cancelTunnelEdit(key); }
  });
  inp.addEventListener('blur', function () {
    setTimeout(function () { if (!done) _saveTunnelUrl(key); }, 150);
  });
}

// Save the inline tunnel URL input.
async function _saveTunnelUrl(key) {
  S._tunEditing = false;
  var inp = document.querySelector('.inst-tun-input');
  if (!inp || !key) { _reloadAfterAnnotate(); return; }
  var trimmed = (inp.value || '').trim();
  await _post('/admin/instances/annotate', { key, tunnel_url: trimmed });
  _reloadAfterAnnotate();
}

// Cancel the inline tunnel URL edit and revert.
function _cancelTunnelEdit(key) {
  S._tunEditing = false;
  _reloadAfterAnnotate();
}

// Remove the custom tunnel URL for an instance.
async function _removeTunnelUrl(key) {
  if (!key) return;
  S.tunnelRemoveConfirm = null;
  await _post('/admin/instances/annotate', { key, tunnel_url: '' });
  _reloadAfterAnnotate();
}

// Reload the appropriate view after an annotation update.
function _reloadAfterAnnotate() {
  const active = S.active || '';
  if (active.startsWith('cloud:')) { _loadCloud(false); return; }
  // For devices, reload the full device list (which triggers re-render of the active)
  _load(true);
}

// ── HTTPS / SSL actions (cloud VM Overview) ──────────────────────────────────
// Read the domain input and the data-* attrs stashed on it (provider/zone/name
// from the cloud instance's _ref), then stream the HTTPS-enable log. The
// backend SSHs into the VM and rewrites Caddyfile.
async function _httpsEnable() {
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return;
  const domain = (inp.value || '').trim();
  if (!domain || domain.indexOf('.') === -1) {
    _httpsStatusLine('Enter a valid domain name (e.g. app.yourcompany.com).', 'err');
    return;
  }
  const provider = inp.dataset.provider || '';
  const zone = inp.dataset.zone || '';
  const name = inp.dataset.name || '';
  if (!provider || !name) {
    _httpsStatusLine('Cannot identify this server — missing provider/name.', 'err');
    return;
  }

  _httpsPill('busy', 'Enabling…');
  _httpsStatusLine('Connecting to the server over SSH…', null);
  _httpsLogReset();

  // Read the sibling checkbox
  const siblingCb = document.getElementById('inst-https-sibling');
  const sibling = siblingCb && siblingCb.checked ? _httpsSiblingDomain(domain) : '';

  try {
    await _httpsStream('/admin/instances/https/enable', {
      provider, zone, name, domain, sibling_domain: sibling,
    }, async (result, ok) => {
      if (ok) {
        _httpsPill('on', 'Active');
        _httpsStatusLine(result.message || 'HTTPS is now active.', 'ok');
        // Persist the domains so the overview facts stay across re-renders.
        const st = _httpsStateRef();
        const domains = [domain];
        if (sibling) domains.push(sibling);
        if (st) { st.domains = domains; }
        // Save to DB so it survives refresh + shared-DB migration.
        try {
          await _post('/admin/instances/annotate', {
            key: inp.dataset.ref || '', kind: 'cloud_vm', domains: domains,
          });
        } catch (_) { /* non-fatal */ }
        // Update the overview facts in-place without blowing away the live log.
        _httpsUpdateOverviewFacts(domains);
      } else {
        _httpsPill('error', 'Failed');
        _httpsStatusLine(result.message || 'Failed to enable HTTPS.', 'err');
      }
    });
  } catch (e) {
    _httpsPill('error', 'Failed');
    _httpsStatusLine(e.message || 'Could not reach the server.', 'err');
  }
}

// Test a single HTTPS domain from the overview facts Test button.
// Streams NDJSON progress, showing results inline next to the button.
async function _httpsTestDomain(domain, btn) {
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return;
  const provider = inp.dataset.provider || '';
  const zone = inp.dataset.zone || '';
  const name = inp.dataset.name || '';
  if (!provider || !name) return;

  // Mark all test result spans for this domain as busy.
  var spans = document.querySelectorAll('.inst-https-test-result[data-domain="' + _cssEsc(domain) + '"]');
  spans.forEach(function(s) { s.textContent = '…'; s.className = 'inst-https-test-result busy'; });

  try {
    await _httpsStream('/admin/instances/https/test-domain', {
      provider, zone, name, domain,
    }, function(result, ok) {
      var msg = '';
      var cls = '';
      if (ok && result.results && result.results.length) {
        var r = result.results[0];
        if (r.reachable) {
          msg = '✓ HTTP ' + (r.http_status || '200');
          cls = 'ok';
          if (r.cert && r.cert.days_left != null) {
            msg += ' · cert ' + (r.cert.days_left > 30 ? '✓' : r.cert.days_left > 0 ? '⚠ expires in ' + r.cert.days_left + 'd' : '✗ expired');
          }
        } else {
          msg = '✗ ' + (r.error || 'unreachable');
          cls = 'err';
        }
      } else {
        msg = result.message || 'Test failed';
        cls = 'err';
      }
      spans.forEach(function(s) { s.textContent = msg; s.className = 'inst-https-test-result ' + cls; });
    });
  } catch (e) {
    spans.forEach(function(s) { s.textContent = '✗ error'; s.className = 'inst-https-test-result err'; });
  }
}

// Quick HTTPS reachability + cert detail check.
async function _httpsCheckStatus() {
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return;
  const provider = inp.dataset.provider || '';
  const zone = inp.dataset.zone || '';
  const name = inp.dataset.name || '';
  if (!provider || !name) {
    _httpsStatusLine('Cannot identify this server.', 'err');
    return;
  }

  _httpsPill('busy', 'Checking…');
  _httpsStatusLine('Checking HTTPS…', null);
  try {
    const r = await _post('/admin/instances/https/status', { provider, zone, name });
    const b = r.body || {};
    if (b.https_active) {
      _httpsPill('on', 'Active');
      _httpsStatusLine('HTTPS is active.', 'ok');
      _showCertDetails(b);
    } else {
      _httpsPill('off', 'Not enabled');
      _httpsStatusLine(b.detail || 'HTTPS is not active on this server.', null);
      _hideCertDetails();
    }
  } catch (e) {
    _httpsPill('error', 'Error');
    _httpsStatusLine(e.message || 'Could not check HTTPS status.', 'err');
    _hideCertDetails();
  }
}

// Read the current Caddyfile domains from the server and show them in the
// registered-domains list below the input row. Also seeds S.httpsState.
async function _httpsCheckDomains() {
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return;
  const provider = inp.dataset.provider || '';
  const zone = inp.dataset.zone || '';
  const name = inp.dataset.name || '';
  if (!provider || !name) return;

  _httpsStatusLine('Reading configured domains…', null);
  try {
    const r = await _post('/admin/instances/https/domains-read', { provider, zone, name });
    const b = r.body || {};
    const domains = (b.ok && b.domains) ? b.domains : [];
    const st = _httpsStateRef();
    if (st) { st.registered = domains; if (domains.length) { st.state = 'on'; st.text = 'Active'; st.domains = domains; } }
    _httpsStatusLine(domains.length ? domains.length + ' domain(s) found' : 'No domains configured.', 'ok');
    _render();
  } catch (e) {
    _httpsStatusLine('Could not read domains from server.', 'err');
  }
}

// Delete a domain from the Caddyfile on the server.
async function _httpsDeleteDomain(domain, btn) {
  if (!domain) return;
  if (!window.confirm('Remove ' + domain + ' from the Caddyfile?\n\nThis deletes the domain block and reloads Caddy.')) return;
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return;
  const provider = inp.dataset.provider || '';
  const zone = inp.dataset.zone || '';
  const name = inp.dataset.name || '';
  if (!provider || !name) return;

  if (btn) btn.disabled = true;
  _httpsLogReset();
  _httpsStatusLine('Removing ' + domain + '…', null);
  try {
    await _httpsStream('/admin/instances/https/domain-delete', {
      provider, zone, name, domain,
    }, function(result, ok) {
      if (ok) {
        _httpsStatusLine(result.message || 'Domain removed.', 'ok');
        // Re-read domains to refresh the list
        setTimeout(function() { _httpsCheckDomains(); }, 800);
      } else {
        _httpsStatusLine(result.message || 'Could not remove domain.', 'err');
      }
    });
  } catch (e) {
    _httpsStatusLine('Failed to remove domain.', 'err');
  }
  if (btn) btn.disabled = false;
}

// Compute the sibling domain (bare ↔ www). Returns '' if input is empty or not
// a dotted domain (e.g. 'domain.com' → 'www.domain.com', 'www.domain.com' → 'domain.com').
function _httpsSiblingDomain(val) {
  val = (val || '').trim();
  if (!val || val.indexOf('.') === -1) return '';
  if (val.indexOf('www.') === 0) {
    const stem = val.slice(4);
    return stem.indexOf('.') !== -1 ? stem : '';
  }
  return 'www.' + val;
}

// Called on input in the domain field — updates the sibling checkbox text.
function _httpsSiblingUpdate() {
  const inp = document.getElementById('inst-https-domain');
  const row = document.getElementById('inst-https-sibling-row');
  const lbl = document.getElementById('inst-https-sibling-text');
  const cb = document.getElementById('inst-https-sibling');
  if (!inp || !row || !lbl) return;
  const sibling = _httpsSiblingDomain(inp.value);
  if (sibling) {
    row.hidden = false;
    lbl.textContent = 'Also enable HTTPS for ' + sibling;
    if (cb) cb.checked = true;
  } else {
    row.hidden = true;
  }
}

// When the checkbox is toggled, no special action — the _httpsEnable() reads it.

// After HTTPS is enabled, update the overview facts in-place so the live progress
// log is not blown away by a full _render(). Inserts an HTTPS row with the
// activated domain(s) and updates the existing URL row to point at https://.
function _httpsUpdateOverviewFacts(domains) {
  var factsEl = document.querySelector('.inst-detail .inst-facts');
  if (!factsEl || !domains || !domains.length) return;

  // Build the stacked domain list with test buttons.
  var httpsHtml = domains.map(function(dom) {
    return '<div class="inst-https-domain-row">'
      + '<a href="https://' + _escAttr(dom) + '" target="_blank" rel="noopener">https://' + _esc(dom) + '</a>'
      + '<button class="inst-https-test-btn" data-act="https-test-domain" data-domain="' + _escAttr(dom) + '">Test</button>'
      + '<span class="inst-https-test-result" data-domain="' + _escAttr(dom) + '"></span>'
      + '</div>';
  }).join('');

  // 1. Update the URL fact row — swap http://ip → stacked https://domain(s).
  var urlFact = factsEl.querySelector('[data-fact="url"]');
  if (urlFact) {
    var val = urlFact.querySelector('.inst-fact-value');
    if (val) {
      val.innerHTML = httpsHtml;
    }
  }

  // 2. Insert (or replace) an HTTPS fact row.
  var existingHTTPS = factsEl.querySelector('[data-fact="https"]');
  if (existingHTTPS) existingHTTPS.remove();
  var httpsRow = document.createElement('div');
  httpsRow.className = 'inst-fact';
  httpsRow.setAttribute('data-fact', 'https');
  httpsRow.innerHTML = '<span class="inst-fact-label"><i data-lucide="shield-check"></i>HTTPS</span>'
    + '<span class="inst-fact-value">' + httpsHtml + '</span>';
  var after = factsEl.querySelector('[data-fact="url"]') || factsEl.querySelector('[data-fact="database"]');
  if (after) {
    after.insertAdjacentElement('afterend', httpsRow);
  } else {
    factsEl.appendChild(httpsRow);
  }
  _refreshLucideIcons(factsEl);
}

function _httpsState(ref) {
  // Get the per-VM HTTPS state entry. If ref is omitted, uses the currently open
  // cloud tile's ref. If ref starts with 'cloud:', strips that prefix.
  if (!ref) {
    if (!S.active || !S.active.startsWith('cloud:')) return null;
    ref = S.active.slice('cloud:'.length);
  }
  if (ref.startsWith('cloud:')) ref = ref.slice('cloud:'.length);
  if (!S.httpsState.has(ref)) S.httpsState.set(ref, { state: 'off', text: 'Not enabled', status: '', statusKind: '', certHtml: '', log: '' });
  return S.httpsState.get(ref);
}

function _httpsStateRef() {
  // Get HTTPS state using the ref stashed on the domain input element.
  const inp = document.getElementById('inst-https-domain');
  if (!inp) return null;
  return _httpsState(inp.dataset.ref || '');
}

function _httpsPill(state, text) {
  const st = _httpsState();
  if (st) { st.state = state; st.text = text; }
  const pill = document.getElementById('inst-https-pill');
  if (!pill) return;
  pill.className = 'inst-https-pill ' + state;
  pill.textContent = text;
}

function _httpsStatusLine(text, kind) {
  const st = _httpsState();
  if (st) { st.status = text || ''; st.statusKind = kind || ''; }
  const el = document.getElementById('inst-https-status');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'inst-ping-status' + (kind === 'ok' ? ' ok' : kind === 'err' ? ' err' : '');
}

function _httpsLogReset() {
  const st = _httpsState();
  if (st) st.log = '';
  const log = document.getElementById('inst-https-log');
  if (!log) return;
  log.hidden = false;
  log.textContent = '';
}

function _httpsLogLine(text, level) {
  const mark = level === 'ok' ? '✓ ' : level === 'err' ? '✗ ' : '· ';
  const st = _httpsState();
  if (st) st.log += mark + text + '\n';
  const log = document.getElementById('inst-https-log');
  if (!log) return;
  log.hidden = false;
  log.textContent += mark + text + '\n';
  log.scrollTop = log.scrollHeight;
}

function _showCertDetails(status) {
  const st = _httpsState();
  if (st) st.certHtml = '';
  const el = document.getElementById('inst-https-cert');
  if (!el) return;
  const results = status.results || [];
  if (!results.length) { el.hidden = true; return; }
  let html = '';
  results.forEach((r) => {
    html += '<div class="inst-https-cert-row">'
      + '<span class="mono">' + _esc(r.domain) + '</span>'
      + (r.reachable ? ' <span class="inst-https-cert-check">✓</span>' : ' <span class="inst-https-cert-nocheck">✗</span>')
      + '</div>';
    if (r.cert) {
      html += '<div class="inst-https-cert-detail">'
        + 'Issuer: <strong>' + _esc(r.cert.issuer) + '</strong> · '
        + 'Expires: <strong>' + _esc(r.cert.not_after) + '</strong>'
        + (r.cert.days_left != null ? ' (' + r.cert.days_left + ' days)' : '')
        + '</div>';
    }
  });
  if (st) st.certHtml = html;
  el.innerHTML = html;
  el.hidden = false;
}

function _hideCertDetails() {
  const st = _httpsState();
  if (st) st.certHtml = '';
  const el = document.getElementById('inst-https-cert');
  if (!el) return;
  el.hidden = true;
  el.innerHTML = '';
}

// ═══════════════════════════════════════════════════════════════════════════
// Server-section actions (This device) — restart / re-port this app + manage local
// sibling checkouts. Moved here from Data Settings → Deployment (deploy.js). Talk to
// /admin/storage/server/* and /admin/deploy/instances/*.
// ═══════════════════════════════════════════════════════════════════════════
function _srvStatus(el, text, err) {
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('inst-srv-error', !!err);
}

// ── Two-tap "Server reset" confirm ───────────────────────────────────────────
// Mirrors the chat header's arm→confirm delete affordance (SHARED-DELETE-CONTROL),
// but restarts this server instead of deleting. First tap arms the button (danger
// tint + "Confirm reset" + hazard icon) and auto-reverts after 3s; the second tap
// fires _doRestart. Icons swap by rebuilding innerHTML so Lucide re-renders them.
function _srvResetInner(state) {
  return state === 'armed'
    ? '<i data-lucide="alert-triangle"></i><span class="inst-srv-reset-label">Confirm restart</span>'
    : '<i data-lucide="rotate-ccw"></i><span class="inst-srv-reset-label">Restart</span>';
}
function _serverResetIdle(btn) {
  if (!btn) return;
  clearTimeout(btn._resetTimer);
  btn.dataset.state = 'idle';
  btn.classList.remove('inst-srv-reset-armed');
  btn.title = 'Restart this server';
  btn.innerHTML = _srvResetInner('idle');
  _refreshLucideIcons(btn);
}
function _serverResetClick(btn) {
  if (!btn || btn.disabled || S.localBusy) return;
  if ((btn.dataset.state || 'idle') === 'idle') {
    btn.dataset.state = 'armed';
    btn.classList.add('inst-srv-reset-armed');
    btn.title = 'Click again to restart this server';
    btn.innerHTML = _srvResetInner('armed');
    _refreshLucideIcons(btn);
    clearTimeout(btn._resetTimer);
    btn._resetTimer = setTimeout(() => _serverResetIdle(btn), 3000);
    return;
  }
  clearTimeout(btn._resetTimer);   // armed → confirmed
  _doRestart();                    // sets S.localBusy + re-renders (rebuilds this button fresh)
}

// Restart THIS server — fired after the two-tap confirm on the Server reset button
// (_serverResetClick), so the second tap IS the confirmation (no popup). The server
// exits a beat after answering; we poll /health and reload once it's back.
async function _doRestart() {
  if (S.localBusy) return;
  S.localBusy = true;
  _render();   // disables the Server controls; also rebuilds [data-hub-status]…
  _srvStatus(document.querySelector('[data-hub-status]'), 'Restarting…');   // …so set the text AFTER
  const r = await _post('/admin/storage/server/restart', {});
  if (!r.ok) {
    S.localBusy = false;
    _render();
    window.alert('Could not restart the server:\n\n' + ((r.body && r.body.detail) || 'unknown error'));
    return;
  }
  // Wait for the old process to drop, then for the relauncher to answer again. The
  // poll is pinned off (S.localBusy) so no re-render clobbers the status while we wait.
  const downBy = Date.now() + 30000;
  while (Date.now() < downBy) { if (!(await _probeHealth())) break; await _sleep(700); }
  const upBy = Date.now() + 180000;
  while (Date.now() < upBy) { if (await _probeHealth()) { window.location.reload(); return; } await _sleep(1000); }
  S.localBusy = false;
  _render();
  _srvStatus(document.querySelector('[data-hub-status]'), 'Server hasn’t answered yet — give it a moment, then Refresh.', true);
}

// Change this app's port + relaunch. The server exits right after responding, so we
// swap the port row for a "reopen here" link.
async function _hubPortSave() {
  const inp = document.getElementById('inst-hub-port');
  const st = document.querySelector('[data-hub-status]');
  const port = parseInt(inp && inp.value || '', 10);
  const current = (S.local && S.local.hub_port) || parseInt(_localPort(), 10);
  if (!port) return _srvStatus(st, 'Enter a port.', true);
  if (port === current) return _srvStatus(st, 'That’s already the current port.', true);
  if (!window.confirm('Change this app’s port to ' + port + ' and restart now?\n\n'
      + 'This connection will drop and WebAgent will reopen at http://localhost:' + port + '/')) return;
  // Pin the poll off so a background re-render can't wipe the "reopen here" link we
  // inject below while the server is relaunching.
  S.localBusy = true;
  _srvStatus(st, 'Saving & restarting…');
  const r = await _post('/admin/deploy/instances/set-hub-port', { port });
  if (!r.ok) { S.localBusy = false; return _srvStatus(st, (r.body && r.body.detail) || 'Could not change the port.', true); }
  const b = r.body || {};
  const url = b.url || ('http://localhost:' + port + '/');
  const hub = document.querySelector('.inst-srv-hub');
  if (hub) {
    hub.innerHTML = '<div class="smb-hint inst-hub-restarting">WebAgent is restarting on port <strong>'
      + _esc(String(b.port || port)) + '</strong>. '
      + (b.auto_restart === false ? 'Start it again from your server window, then open: ' : 'It’ll be back in a few seconds — reopen it here: ')
      + '<a href="' + _escAttr(url) + '" target="_blank" rel="noopener">' + _esc(url) + '</a></div>';
  }
}

// Start / Stop a local checkout tile (streams NDJSON into the tile's #inst-log).
// `id` is the registry id (d.local_id), not the 'local:'-prefixed tile instance_id.
async function _localAction(action, id) {
  if (S.localBusy) return;
  const inst = ((S.local && S.local.instances) || []).find(x => x.id === id);
  if (!inst) return;
  if (action === 'stop' && !window.confirm('Stop “' + inst.label + '”?\n\nIts server (port ' + inst.port + ') is shut down; anyone using it is disconnected.')) return;
  S.localBusy = true;
  _render();
  _logReset();
  _logLine((action === 'stop' ? 'Stopping ' : 'Starting ') + inst.label + '…');
  try {
    await _stream('/admin/deploy/instances/' + action, { id }, (result) => {
      _logLine(result.message || (result.ok ? 'Done.' : 'Failed.'), result.ok ? 'ok' : 'err');
    });
  } catch (e) { _logLine(e.message, 'err'); }
  finally { S.localBusy = false; await _loadLocal(); }
}

// Delete a local checkout — removes it from WebAgent's list (the folder on disk is
// never touched; a running one keeps running until stopped). Unlike a cloud VM's
// Delete (which destroys the server), this only un-registers it, so a plain confirm.
async function _localRemove(id) {
  const inst = ((S.local && S.local.instances) || []).find(x => x.id === id);
  if (!inst) return;
  const msg = inst.status === 'running'
    ? 'Delete “' + inst.label + '” from WebAgent?\n\nIt is still RUNNING — this only removes it from this list; it keeps running until you Stop it. The repo folder on disk is not touched.'
    : 'Delete “' + inst.label + '” from WebAgent?\n\nThis removes it from the list only — the repo folder on disk is not touched.';
  if (!window.confirm(msg)) return;
  const r = await _post('/admin/deploy/instances/remove', { id });
  if (!r.ok) { window.alert((r.body && r.body.detail) || 'Could not delete that instance.'); return; }
  if (S.active === 'local:' + id) _setActive(null);   // its detail card is going away
  _loadLocal();
}

// Save the inline name / folder / port edits from the open local tile Overview.
async function _localSave(id) {
  const st = document.querySelector('[data-local-status]');
  const val = (k) => { const el = document.querySelector('[data-local-field="' + k + '"]'); return el ? el.value : ''; };
  const port = parseInt(val('port') || '', 10);
  _srvStatus(st, 'Saving…');
  const r = await _post('/admin/deploy/instances/update', { id, label: val('label') || '', folder: (val('folder') || '').trim(), port: port || 0 });
  if (!r.ok) return _srvStatus(st, (r.body && r.body.detail) || 'Could not save.', true);
  _loadLocal();
}

// ── Instance rename + icon (device tiles; shared DB) ─────────────────────────
// Mirrors the Agents page: a device tile's display name is inline-editable and its
// icon opens the shared picker. Both persist to the device's shared-registry row
// (custom_label / custom_icon via POST /device/rename), so the change shows on
// every device pointed at this database and survives the device's heartbeats.

// Persist one override field, then reflect it on the in-memory row so the tile +
// detail update instantly (the 20s poll re-fetches it too). Pass { label } or
// { icon }; an empty string clears that override (revert to hostname / platform
// icon). Returns true on success.
async function _saveInstOverride(iid, patch) {
  const r = await _post('/admin/instances/device/rename', { instance_id: iid, ...patch });
  if (!r.ok) { window.alert((r.body && r.body.detail) || 'Could not save that change.'); return false; }
  const d = _find(iid);
  if (d) {
    if ('label' in patch) d.custom_label = patch.label;
    if ('icon' in patch) d.custom_icon = patch.icon;
  }
  if (S.devices) _writeCache(S.devices);   // keep the instant-paint cache current
  return true;
}

// Open the shared icon picker for a device, seeded with its current icon (its
// override, else the platform default), and persist the choice.
function _openInstIcon(iid) {
  const d = _find(iid); if (!d) return;
  const current = (d.custom_icon || '').trim() || _platformIcon(d.platform);
  openIconPicker({ current, title: 'Choose an icon' }).then(async (chosen) => {
    if (!chosen) return;
    if (await _saveInstOverride(iid, { icon: chosen })) _render();
  });
}

// Turn a device's detail-head name into a live contenteditable field. Enter / blur
// commits, Escape cancels. Blanking it (or typing the hostname back) clears the
// override so the tile reverts to the machine's self-reported name. S.editing pins
// the poll off this row while the field is live (see the poll guard in startView).
function _beginNameEdit(span) {
  const iid = span && span.dataset.iid;
  if (!iid || S.editing) return;
  const d = _find(iid); if (!d) return;
  S.editing = iid;
  const before = _instName(d);
  span.setAttribute('contenteditable', 'true');
  span.classList.add('editing');
  span.textContent = before;
  span.focus();
  const range = document.createRange(); range.selectNodeContents(span);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);

  let done = false;
  const finish = async (commit) => {
    if (done) return; done = true;
    span.removeEventListener('keydown', onKey);
    span.removeEventListener('blur', onBlur);
    span.removeAttribute('contenteditable');
    span.classList.remove('editing');
    S.editing = null;
    const val = (span.textContent || '').trim();
    if (commit && val !== before.trim()) {
      // Typing the hostname back (or clearing) stores "" so the row reverts to its
      // self-reported name rather than pinning the current hostname as an override.
      const label = (!val || val === (d.label || d.instance_id)) ? '' : val;
      if (await _saveInstOverride(iid, { label })) { _render(); return; }
    }
    _render();   // revert the display to the stored value
  };
  const onKey = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  const onBlur = () => finish(true);
  span.addEventListener('keydown', onKey);
  span.addEventListener('blur', onBlur);
}

// Delegated double-click → begin an inline name edit (survives re-render).
function _onDblClick(e) {
  const span = e.target.closest && e.target.closest('.inst-name-edit');
  if (span) { e.preventDefault(); _beginNameEdit(span); }
}

// Long-press on .inst-name-edit → inline rename (same gesture as the Agents page
// and the carousel reorder handle). A 500ms hold starts the edit; releasing early
// or moving away cancels it. Mirrors view.js _wireInlineEdit's pointerdown timer
// so touch users can rename without the OS contextmenu firing the point-and-ask panel.
let _nameLpTimer = null;
function _onNamePointerDown(e) {
  const span = e.target.closest && e.target.closest('.inst-name-edit');
  if (!span || span.classList.contains('editing')) return;
  _nameLpTimer = setTimeout(() => { _nameLpTimer = null; _beginNameEdit(span); }, 500);
}
function _onNamePointerUp() {
  if (_nameLpTimer) { clearTimeout(_nameLpTimer); _nameLpTimer = null; }
}

// NDJSON stream reader (same shape as the Deploy card + the old Server Manager).
async function _stream(path, body, onDone, onEvent) {
  const res = await fetch(apiPath(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onDone(obj.result || obj);
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.phase === 'done') onDone(ev.result || {});
      else if (ev.message) {
        if (onEvent) onEvent(ev);
        else _logLine(ev.message, ev.level);
      }
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
}

// HTTPS-specific stream: same NDJSON reader as _stream but routes intermediate
// events to _httpsLogLine so the log is separate from the cloud VM action log.
async function _httpsStream(path, body, onDone) {
  const res = await fetch(apiPath(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...body }),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onDone(obj.result || obj, obj.result ? obj.result.ok : false);
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (!line) continue;
      let ev;
      try { ev = JSON.parse(line); } catch { continue; }
      if (ev.message) _httpsLogLine(ev.message, ev.ok ? 'ok' : (ev.level === 'err' ? 'err' : null));
      if (ev.phase === 'done') onDone(ev.result || {}, ev.result ? !!ev.result.ok : false);
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
}

// ── Events (delegated; survive re-render) ────────────────────────────────────
function _onClick(e) {
  const tab = e.target.closest('.inst-tab');
  if (tab) {
    const tabId = tab.dataset.tab;
    // Only allow settings, dashboard and users tabs for self device
    const activeInst = S.active ? _find(S.active) : null;
    if ((tabId === 'settings' || tabId === 'dashboard' || tabId === 'users') && activeInst && !activeInst.is_self) {
      return;
    }
    S.tab = tabId;
    // Save config state when switching tabs — remember which tab and the
    // current scroll/search state for the Configuration tab (only relevant
    // when the active tab is actually settings).
    if (S.active === (S.self || _selfId())) {
      const content = document.getElementById('app-config-content');
      const searchInput = document.getElementById('ac-unified-pa-input');
      _saveConfigState({
        tab: tabId,
        scrollTop: content ? content.scrollTop : 0,
        searchText: searchInput ? searchInput.value : '',
      });
    }
    _render();
    // When any detail tab is clicked, scroll the tab bar to the top of the page
    // — hiding everything above the tab row (toolbar, tile carousel, instance
    // header) so the user lands directly on that tab's content. The instance
    // header scrolls away naturally (see the carousel rules in instances.css),
    // so without this the tab bar can land mid-screen and the content starts
    // below the fold.
    _scrollTabsToTop(tabId);
    return;
  }

  // Connection strip: DB/vault card expand.
  const toggle = e.target.closest('[data-toggle]');
  if (toggle) { const k = toggle.dataset.toggle; S.open[k] = !S.open[k]; _render(); return; }

  // Cloud-account row toggle / save / signout / remove.
  const at = e.target.closest('[data-acct-toggle]');
  if (at) { const id = at.dataset.acctToggle; if (S.acctExpanded.has(id)) S.acctExpanded.delete(id); else S.acctExpanded.add(id); _render(); return; }
  const asv = e.target.closest('[data-acct-save]'); if (asv) { _saveAccount(asv.dataset.acctSave); return; }
  const aso = e.target.closest('[data-acct-signout]'); if (aso) { _signOut(aso.dataset.acctSignout); return; }
  const arm = e.target.closest('[data-acct-remove]'); if (arm) { _removeAccount(arm.dataset.acctRemove); return; }

  const act = e.target.closest('[data-act]');
  if (act) {
    switch (act.dataset.act) {
      case 'refresh': _load(true); _loadCloud(true); _loadConn().catch(function(){}); _loadLocal().catch(function(){}); return;
      case 'close': _setActive(null); S.confirmDel = null; S.tunnelRemoveConfirm = null; _render(); return;
      case 'fix-db':
        // Open THIS device's Overview (where the DB sign-in card now lives) and
        // expand the database card.
        _setActive(S.self || _selfId()); S.tab = 'overview'; S.open.db = true;
        _render();
        if (S.status === null) _loadConn();
        return;
      case 'acct-add': _addAccount(); return;
      case 'device-connect-toggle': _deviceConnectToggle('summary', act); return;
      case 'device-connect-save': _deviceConnectSave(); return;
      case 'device-connect-use-saved': _deviceConnectUseSaved(act.dataset.provider); return;
      case 'device-connect-use-different':
        var prov = act.dataset.provider || '';
        if (!prov && act.dataset.newPick === '1') {
          var pick = document.getElementById('inst-device-connect-new-pick');
          prov = pick ? pick.value : '';
        }
        _deviceConnectSwitchToForm(prov);
        return;
      case 'device-connect-provider-change': _deviceConnectProviderChange(); return;
      case 'device-connect-cancel': _deviceConnectCancel(); return;
      case 'db-test': _dbTest(); return;
      case 'db-save': _dbSave(); return;
      case 'db-sql': _dbSql(); return;
      case 'db-bootstrap': _dbBootstrap(); return;
      case 'db-activate': _dbActivate(); return;
      case 'vault-test': _vaultTest(); return;
      case 'vault-activate': _vaultActivate(); return;
      case 'vault-restart': _vaultRestart(act); return;
      case 'ping': _doPing(act.dataset.addr, act); return;
      case 'tunnel-start': _tunnelAction('start', act.dataset.iid, act); return;
      case 'tunnel-stop': _tunnelAction('stop', act.dataset.iid, act); return;
      case 'device-restart':
        if (act.dataset.self === '1') _serverResetClick(act);
        else _deviceControl('restart', act.dataset.iid, act);
        return;
      case 'device-pull': _deviceControl('git_pull', act.dataset.iid, act); return;
      case 'device-push': _deviceControl('git_commit_push', act.dataset.iid, act); return;
      case 'inst-icon': _openInstIcon(act.dataset.iid); return;
      case 'unlink-hold': return;  // fired by press-and-hold (_holdStart), not click

      // Server section (This device): restart / re-port this running app.
      case 'server-reset': _serverResetClick(act); return; // two-tap arm→confirm
      case 'hub-port-save': _hubPortSave(); return;

      // Local checkout tiles: Start / Stop / Delete / edit (like cloud VMs).
      case 'local-start': _localAction('start', act.dataset.lid); return;
      case 'local-stop': _localAction('stop', act.dataset.lid); return;
      case 'local-remove': _localRemove(act.dataset.lid); return;
      case 'local-save': _localSave(act.dataset.lid); return;

      case 'cloud-start': _doCloudAction('start', act.dataset.ref, act.dataset.name); return;
      case 'cloud-stop': _doCloudAction('stop', act.dataset.ref, act.dataset.name); return;
      case 'cloud-run-build-deploy':
        _doCloudRunBuildDeploy(act.dataset.iid, act.dataset.service);
        return;
      case 'cloud-delete-ask': S.confirmDel = act.dataset.ref; _render(); return;
      case 'cloud-delete-cancel': S.confirmDel = null; _render(); return;
      case 'cloud-delete-go':
        if (_delMatches()) { S.confirmDel = null; _doCloudAction('delete', act.dataset.ref, act.dataset.name); }
        return;
      case 'repo-edit': _editRepo(act.dataset.key); return;
      case 'tunnel-edit-url': _startTunnelUrlEdit(act); return;
      case 'tunnel-save-url': _saveTunnelUrl(act.dataset.key); return;
      case 'tunnel-cancel-edit': _cancelTunnelEdit(act.dataset.key); return;
      case 'tunnel-remove-url': _removeTunnelUrl(act.dataset.key); return;
      case 'tunnel-remove-ask': S.tunnelRemoveConfirm = act.dataset.key; _render(); return;
      case 'tunnel-remove-cancel': S.tunnelRemoveConfirm = null; _render(); return;
      case 'copy-url': _copyUrl(act.dataset.url, act); return;
      case 'open-url': window.open(_openUrl(act.dataset.url), '_blank', 'noopener'); return;
      case 'qr-url': showQrPopup({ url: act.dataset.url, anchor: act }); return;
      case 'https-enable': _httpsEnable(); return;
      case 'https-status': _httpsCheckStatus(); return;
      case 'https-check': _httpsCheckDomains(); return;
      case 'https-delete-domain': _httpsDeleteDomain(act.dataset.domain, act); return;
      case 'https-test-domain': _httpsTestDomain(act.dataset.domain, act); return;
      case 'clear-url': _clearUrl(act.dataset.url, act.dataset.ref, act); return;
      case 'hide-url': _setUrlHidden(act.dataset.url, act.dataset.ref, true); return;
      case 'show-url': _setUrlHidden(act.dataset.url, act.dataset.ref, false); return;
      case 'canonical-set': _setCanonicalUrl(act.dataset.url); _patchAllUrlRows(); return;
      case 'canonical-redirect': _toggleCanonicalRedirect(); _patchAllUrlRows(); return;
      case 'canonical-unredirect': _excludeCanonicalHost(act.dataset.url); _patchAllUrlRows(); return;
      case 'canonical-reinclude': _reincludeCanonicalHost(act.dataset.url); _patchAllUrlRows(); return;
      case 'get-domain': _getDomainChat(); return;
      case 'toggle-hidden-group': _toggleHiddenGroup(act.dataset.ref); return;
      case 'url-upgrade-https': _urlUpgradeHttps(act.dataset.url, act); return;
    }
  }

  // The "New instance" create tile (also an .inst-square) — handle before the
  // generic tile handler, which would read its absent data-iid.
  const newsq = e.target.closest('[data-new-deploy]');
  if (newsq) {
    _setActive((S.active === NEW_DEPLOY_ID) ? null : NEW_DEPLOY_ID);   // toggle
    S.tab = 'new-deploy';
    _render();
    return;
  }

  const sq = e.target.closest('.inst-square');
  if (sq) {
    const iid = sq.dataset.iid;
    _setActive((S.active === iid) ? null : iid);   // toggle
    S.tab = 'overview';
    S.confirmDel = null;   // don't carry an armed delete onto another tile
    S.tunnelRemoveConfirm = null;
    _render();
  }
}

// ── URL row expand/collapse (Overview URL section) ────────────────────────────
// The URL fact shows only the URL link by default; clicking a URL link expands
// its copy/QR action row (with a scale-up animation) instead of navigating.
// Clicking the same link again — or anywhere outside the URL rows — collapses it.
// Ctrl/Cmd/Shift/middle-clicks keep the normal open-in-new-tab behaviour.

// Click-to-expand on a URL freshness label: swaps the compact "12m ago" for the
// untruncated "Completed 12 minutes ago" form for 1 second, then reverts. Uses
// the same relative-time rule as _relTime via _relTimeFull. A re-render (poll)
// may detach the span mid-expansion — reverting a detached node is harmless.
let _staleExpandTimer = null;
let _staleExpandEl = null;
function _expandStaleLabel(el) {
  const lastSeen = el.getAttribute('data-last-seen') || '';
  if (_staleExpandTimer) { clearTimeout(_staleExpandTimer); _staleExpandTimer = null; }
  if (_staleExpandEl && _staleExpandEl !== el) {
    const prevSeen = _staleExpandEl.getAttribute('data-last-seen') || '';
    _staleExpandEl.textContent = prevSeen ? _relTime(prevSeen) : '2d ago';
  }
  _staleExpandEl = el;
  el.textContent = 'Completed ' + (lastSeen ? _relTimeFull(lastSeen) : '2 days ago');
  _staleExpandTimer = setTimeout(() => {
    el.textContent = lastSeen ? _relTime(lastSeen) : '2d ago';
    _staleExpandTimer = null;
    _staleExpandEl = null;
  }, 1000);
}

function _onUrlDocClick(e) {
  const stale = e.target.closest && e.target.closest('.inst-url-stale');
  // Only freshness labels carry a data-last-seen attribute — the static "Local
  // network" label on auto-detected LAN URLs has none and is not expandable.
  if (stale) { if (stale.hasAttribute('data-last-seen')) _expandStaleLabel(stale); return; }
  const link = e.target.closest && e.target.closest('.inst-url-link > a');
  if (link) {
    // Only two-row items (link + hidden .inst-url-meta) toggle; the managed
    // tunnel row's inline link is not matched (its parent is .inst-url-row).
    const item = link.closest('.inst-url-item');
    if (item && item.querySelector('.inst-url-meta')
        && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      const key = item.dataset.urlKey;
      if (item.classList.contains('inst-url-open')) {
        item.classList.remove('inst-url-open');
        if (key) S.openUrls.delete(key);
      } else {
        item.classList.add('inst-url-open');
        if (key) S.openUrls.add(key);
      }
    }
    return;
  }
  // Click anywhere outside the URL rows collapses every open row.
  if (e.target.closest && e.target.closest('.inst-url-item')) return;
  if (S.openUrls.size) {
    S.openUrls.clear();
    document.querySelectorAll('.inst-url-item.inst-url-open')
      .forEach(el => el.classList.remove('inst-url-open'));
  }
}

// Provider dropdown changes → keep the choice + re-render that card's fields/note.
function _onChange(e) {
  if (e.target.id === 'inst-db-provider') { S.sel.db = e.target.value; _render(); return; }
  if (e.target.id === 'inst-vault-provider') { S.sel.vault = e.target.value; _render(); return; }
}

// Live-arm the destructive-delete button as the admin types the server name. Done
// by toggling .disabled directly (not a re-render) so the field keeps focus.
function _onInput(e) {
  if (e.target.id === 'inst-del-confirm') {
    const go = document.getElementById('inst-del-go');
    if (go) go.disabled = (e.target.value || '').trim() !== (e.target.dataset.expect || '');
  }
  if (e.target.id === 'inst-https-domain') {
    _httpsSiblingUpdate();
  }
}

// While any Server-section field (port editor, sibling edit, register form) has
// focus, pin the 20s poll off so a background re-render never blows away what the
// admin is typing (mirrors the S.editing guard for inline device renames).
const _SRV_FIELDS = '.inst-server-section input, .inst-local-edit input';
function _onFocusIn(e) { if (e.target.closest && e.target.closest(_SRV_FIELDS)) S.localEditing = true; }
function _onFocusOut(e) { if (e.target.closest && e.target.closest(_SRV_FIELDS)) S.localEditing = false; }

// ── Press-and-hold confirm (device Unlink) ───────────────────────────────────
// The Unlink button fills over HOLD_MS while pressed; the action fires only when
// the fill reaches the end. Releasing early aborts silently — no popup. Pointer
// capture keeps the gesture alive through small finger/mouse drift while held.
const HOLD_MS = 950;
let _holdTimer = null;
let _holdBtn = null;

function _holdStart(e) {
  const btn = e.target.closest && e.target.closest('[data-act="unlink-hold"]');
  if (!btn || btn.disabled) return;
  if (e.pointerType === 'mouse' && e.button !== 0) return;   // primary button only
  e.preventDefault();
  _holdClear();                       // drop any stale in-flight hold
  _holdBtn = btn;
  btn.classList.add('holding');       // CSS runs the fill transition
  if (e.pointerId != null && btn.setPointerCapture) {
    try { btn.setPointerCapture(e.pointerId); } catch (_) {}
  }
  _holdTimer = setTimeout(() => {
    const b = _holdBtn;
    _holdTimer = null; _holdBtn = null;
    if (!b) return;
    b.classList.remove('holding'); b.classList.add('held');
    // Fill-to-fire gesture: unlink a stale device. (The Server reset now uses a
    // two-tap arm→confirm instead — see _serverResetClick.)
    _unlinkDevice(b.dataset.iid, b.dataset.name);
  }, HOLD_MS);
}

// Cancel an in-flight hold and snap the fill back. Safe to call any time; a no-op
// once the hold has already fired (timer + button reference are cleared).
function _holdClear() {
  if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
  if (_holdBtn) { _holdBtn.classList.remove('holding'); _holdBtn = null; }
}
function _holdEnd() { _holdClear(); }

// ── Lifecycle (called by the admin shell: files.js applySidebarView) ─────────
// ── QR popup (shared: ui/shared/js/qr-popup.js) ──────────────────────────────
// _copyUrl still lives here for the copy button's icon-swap feedback.
// Copy URL to clipboard with brief visual feedback on the button.
function _copyUrl(url, btn) {
  navigator.clipboard.writeText(url).then(() => {
    // Show a checkmark briefly, then restore the copy icon
    const orig = btn.innerHTML;
    btn.innerHTML = '<i data-lucide="check"></i>';
    _refreshLucideIcons(btn);
    setTimeout(() => { btn.innerHTML = orig; _refreshLucideIcons(btn); }, 1500);
  }).catch(() => {
    // Fallback: select-and-copy
    const ta = document.createElement('textarea');
    ta.className = 'inst-copy-ta';
    ta.value = url;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (_) {}
    document.body.removeChild(ta);
  });
}

// ── Start / Stop ──────────────────────────────────────────────────────────────

export function startView() {
  const el = _root(); if (!el) return;
  if (!S.wired) {
    S.wired = true;
    el.addEventListener('click', _onClick);
    el.addEventListener('dblclick', _onDblClick);
    el.addEventListener('change', _onChange);
    el.addEventListener('input', _onInput);
    el.addEventListener('focusin', _onFocusIn);
    el.addEventListener('focusout', _onFocusOut);
    // URL-row expand/collapse + outside-click collapse (document-level so clicks
    // on the rest of the page also close an open URL row).
    document.addEventListener('click', _onUrlDocClick);
    // Press-and-hold confirm for the Unlink button (fill-to-fire, release aborts).
    el.addEventListener('pointerdown', _holdStart);
    el.addEventListener('pointerup', _holdEnd);
    el.addEventListener('pointercancel', _holdEnd);
    el.addEventListener('lostpointercapture', _holdEnd);
    // Long-press on .inst-name-edit → inline rename (touch-friendly).
    el.addEventListener('pointerdown', _onNamePointerDown);
    el.addEventListener('pointerup', _onNamePointerUp);
    el.addEventListener('pointercancel', _onNamePointerUp);
  }
  // Fetch current canonical URL + redirect state (fire-and-forget — renders
  // whichever URL row is primary and whether the redirect toggle is live).
  _post('/admin/instances/canonical-url').then(function(r) {
    if (r.ok && r.body && r.body.url) {
      S.canonicalUrl = String(r.body.url).trim() || null;
      S.canonicalRedirect = !!r.body.redirect_enabled;
      S.canonicalExcluded = Array.isArray(r.body.excluded) ? r.body.excluded : [];
    } else { S.canonicalUrl = null; S.canonicalRedirect = false; S.canonicalExcluded = []; }
    _patchAllUrlRows();
  }).catch(function() {});

  // Instant paint: last-known fleet, else a synthetic "this device" tile.
  if (S.devices == null) {
    const cached = _readCache();
    if (cached && Array.isArray(cached.devices) && cached.devices.length) {
      cached.devices.forEach(d => { d.kind = d.kind || 'device'; });
      S.devices = cached; S.self = cached.self || '';
    } else {
      S.devices = { devices: [_selfStub()], self: _selfId(), _stub: true };
      S.self = _selfId();
    }
  }
  // Open a tile by default so the page never lands on a bare grid: THIS device
  // (its Overview shows status + connections) unless a saved selection from a
  // previous visit points elsewhere — another device, a cloud VM, or the New
  // instance tile — which wins. Runs only when nothing is already open in memory
  // (S.active survives view stop/start within a session), so navigating away and
  // back keeps whatever was open. The self tile matches the instant-paint stub, so
  // this opens immediately, before any DB round-trip.
  if (S.active == null) {
    const saved = _readActive();
    if (saved) { S.active = saved; S.tab = (saved === NEW_DEPLOY_ID) ? 'new-deploy' : 'overview'; }
    else {
      S.active = S.self || _selfId();
      // Restore saved tab state (e.g. Configuration) from last visit on this device
      const cfg = _readConfigState();
      S.tab = (cfg && (cfg.tab === 'settings' || cfg.tab === 'dashboard')) ? cfg.tab : 'overview';
    }
  }
  S.loading = true;
  // Initial paint with cached/stub data — shows the toolbar + tile grid immediately.
  _render();

  // Batch all startup loads so _render() fires exactly ONCE after all data
  // arrives, instead of 4–5 times in rapid succession (each innerHTML rebuild
  // destroys and recreates the entire DOM, which was overwhelming the browser).
  S._renderSuppressed = true;
  Promise.all([
    _load(true),
    _loadCloud(true),
    _loadConn(),
    _loadLocal(),
  ]).then(function() {
    S._renderSuppressed = false;
    _render();
    // After the single post-load render, patch URL facts if the Overview tab is
    // open — replaces the old separate 10 s overview poll (which doubled the
    // /devices request load).
    _patchUrlFactsIfOverview();
  }).catch(function() {
    S._renderSuppressed = false;
    _render();
  });

  _prewarmNewDeploy();    // warm the New-Deployment fragment in the background (off the critical path)
  _prewarmNewInstance();  // warm the New Instance wizard fragment too (same idle beat)
  // Let the New-Deployment "This device" target (deploy.js _registerAdd) refresh the
  // local tiles the instant it registers a new checkout, so it shows up without
  // waiting for the 20s poll.
  window.__instancesReloadLocal = _loadLocal;
  if (S.poll) clearInterval(S.poll);
  // Single fleet poll — fetches devices, cloud VMs, and local instances every 20 s.
  // The old separate 10 s overview poll that also hit /admin/instances/devices is
  // removed; this single poll now patches URL facts in-place after each refresh
  // so the Overview tab stays current without doubling the request load.
  S.poll = setInterval(() => {
    if (S.cloudBusy || S.confirmDel || S.tunnelRemoveConfirm || S.editing || S._tunEditing) return;
    S._renderPending = true;
    Promise.all([_load(false), _loadCloud(false), _loadLocal()])
      .catch(() => {})
      .finally(() => {
        S._renderPending = false;
        // Patch URL fact rows in-place after each fleet poll — replaces the
        // old separate 10 s overview poll so /devices is only called once.
        _patchUrlFactsIfOverview();
      });
  }, 20000);
}

export function stopView() {
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
  S.loading = false;
  S.editing = null;   // never leave the poll pinned off after navigating away mid-edit
  S._tunEditing = false;
  S.tunnelRemoveConfirm = null;
  _stopOverviewPoll();
  _unmountDashboard();
  _unmountUsers();
  if (window.__instancesReloadLocal === _loadLocal) window.__instancesReloadLocal = null;
}
