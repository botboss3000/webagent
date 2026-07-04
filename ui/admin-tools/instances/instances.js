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
//     grid, with Start / Stop / Delete / Open / Ping + a live log in each cloud
//     tile's Overview; its cloud-ACCOUNT manager (add a provider, paste its key)
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
//     _localTiles), with Start / Stop / Delete / Open + inline edit in each tile's
//     Overview (_renderLocalOverview) — managed just like cloud VMs, and CREATED
//     from the New instance tile → New Deployment → "This device". Backed by the
//     same /admin/deploy/instances/* + /admin/storage/server/restart endpoints
//     (app/local_instances.py) the old section used — only the UI home moved;
//     deploy.js's own list/restart code is now orphaned (see its header). So the
//     whole deployment surface now sits with the fleet.
//
// Backed by this folder's OWN router — ui/admin-tools/instances/server.py
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
// Discovered via ui/admin-tools/instances/page.json and driven generically by the
// admin shell (ui/shared/js/files.js applySidebarView). PANEL-LESS view.
// Styling: instances.css — self-contained (the tile grid + the in-Overview
// Connections section; the smb-/cvm- card/form primitives were folded in when the
// two old pages were removed). All colours come from the design-system palette (CSS variables only)
// — correct in dark + light.
// REMOVE-WHEN: the Instances view is dropped from the admin page catalog.

import { apiPath } from '../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../shared/js/dom-utils.js';
// Shared searchable Lucide icon picker — the SAME popover the Agents page uses to
// pick an agent icon, so renaming/re-iconing an instance feels identical. Its CSS
// is duplicated into instances.css (this folder is self-contained), so the popover
// is styled here even though the Agents stylesheet isn't loaded on admin pages.
import { openIconPicker, ICON_PICKER_ICONS } from '../../shared/js/icon-picker.js';
// The "New instance" tile's "New Deployment" tab is its own drop-in sub-feature
// (ui/admin-tools/instances/new-deployment/) that owns the moved deploy/domain
// markup + the HEAVY deploy.js/dns.js glue (~3k lines). It is LAZY-loaded — a
// dynamic import fired only when the New instance tile is first opened (see
// _mountNewDeploy) — so that chain stays OFF this page's first-paint critical path:
// the tiles show instantly and the deploy machinery downloads on demand. We only
// need the synthetic tile id up-front, so it is duplicated here as a plain constant
// (MUST match new-deployment.js's NEW_DEPLOY_ID export) rather than pulling in the
// whole module just to name it.
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
  // _ref of the cloud VM whose destructive Delete is awaiting a typed-name
  // confirm (null = no delete armed). See _delConfirmHtml / _onInput.
  confirmDel: null,
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
};

const _TABS = [['overview', 'Overview']];

// Instant-paint plumbing (device tile only — cloud + health are always live).
const _STUB_ID = 'this-device';
const _CACHE_KEY = 'inst.devices.v1';
const _SELF_KEY = 'inst.selfId.v1';
// Which tile was open last visit — but ONLY a selection OTHER than "This device"
// (another device, a cloud VM, the New instance tile) is remembered. Opening This
// device or closing the panel clears it, so the next load falls back to the default
// of opening This device. Absent = no other selection → default. See startView.
const _ACTIVE_KEY = 'inst.active.v1';

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _root() { return document.getElementById('inst-root'); }
function _cssEsc(v) { return (window.CSS && CSS.escape) ? CSS.escape(v) : v; }

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())));
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}
async function _post(path, payload) {
  const res = await fetch(apiPath(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...(payload || {}) }),
  });
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}

// ── Instant-paint helpers ─────────────────────────────────────────────────────
function _selfId() { try { return localStorage.getItem(_SELF_KEY) || _STUB_ID; } catch { return _STUB_ID; } }
function _rememberSelfId(id) { if (id) { try { localStorage.setItem(_SELF_KEY, id); } catch {} } }
function _readCache() { try { return JSON.parse(sessionStorage.getItem(_CACHE_KEY) || 'null'); } catch { return null; } }
function _writeCache(p) { try { sessionStorage.setItem(_CACHE_KEY, JSON.stringify(p)); } catch {} }

// Saved open-tile memory (only non-self selections are stored — see _ACTIVE_KEY).
function _readActive() { try { return localStorage.getItem(_ACTIVE_KEY) || null; } catch { return null; } }
function _saveActive(v) { try { if (v) localStorage.setItem(_ACTIVE_KEY, v); else localStorage.removeItem(_ACTIVE_KEY); } catch {} }
// Set the open tile AND persist the choice so the page reopens where you left it.
// Opening This device (any of its id forms) or closing (null) clears the memory,
// leaving the default — open This device — to apply on the next load. _render() is
// the caller's responsibility (matches the old direct S.active writes).
function _setActive(id) {
  S.active = id;
  const selfIds = new Set([S.self, _selfId(), _STUB_ID].filter(Boolean));
  _saveActive((id == null || selfIds.has(id)) ? null : id);
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
// The self device is never merged away — it owns the Connections config.
function _mergeFleet(devices, cloud) {
  cloud.forEach(vm => { vm.fleet = false; vm.fleet_online = false; });
  const claimed = new Set();
  cloud.forEach(vm => {
    const twin = devices.find(d =>
      !d.is_self && !claimed.has(d.instance_id) && _deviceMatchesVm(d, vm));
    if (!twin) return;
    claimed.add(twin.instance_id);
    vm.fleet = true;
    vm.fleet_online = !!twin.online;
    vm.device_id = twin.instance_id;
    if (twin.last_seen) vm.last_seen = twin.last_seen;
    if (twin.endpoint) vm.endpoint = twin.endpoint;   // the app's own reported URL beats http://<ip>
    if (!vm.repo && twin.repo) vm.repo = twin.repo;
    if (twin.branch && !vm.branch) vm.branch = twin.branch;
    if (twin.tunnel) vm.tunnel = twin.tunnel;         // its published Remote Access tunnel state
  });
  const keptDevices = devices.filter(d => !claimed.has(d.instance_id));
  return [...keptDevices, ...cloud];
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
  { v: 'supabase', t: 'Supabase' },
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
  supabase: [
    { key: 'supabase_url', label: 'Project URL', type: 'text', required: true, placeholder: 'https://xxxx.supabase.co' },
    { key: 'supabase_service_key', label: 'Service role key', type: 'password', keep: true, placeholder: 'eyJhbGciOi…' },
  ],
  postgres: [..._PG_FIELDS, { key: 'ssl_mode', label: 'SSL mode', type: 'select', options: ['disable', 'require', 'verify-ca', 'verify-full'], half: true }],
  aws_rds: _PG_FIELDS,
  gcp_cloud_sql: _PG_FIELDS,
  azure_postgres: _PG_FIELDS,
  neon: _PG_FIELDS,
};
const DB_NOTE = {
  sqlite: 'A single local file on this device. Zero setup — but local-only: nothing is shared with other devices.',
  postgres: 'Any Postgres you run. Shared across every device that signs in to it.',
  supabase: 'Managed Postgres (URL + service-role key). Shared across devices; survives this app-host dying.',
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
  const isSupabase = sel === 'supabase';
  return ''
    + '<label class="smb-label">Provider</label>'
    + '<select id="inst-db-provider" class="smb-input">' + opts + '</select>'
    + '<div class="smb-note">' + _esc(DB_NOTE[sel] || '') + '</div>'
    + '<div id="inst-db-fields" class="smb-fields">' + _dbFieldsHtml(sel, prefill) + '</div>'
    + '<div class="smb-actions">'
    +   '<button class="smb-btn" data-act="db-test">Test</button>'
    +   '<button class="smb-btn" data-act="db-save">Save</button>'
    +   '<button class="smb-btn" data-act="db-sql">' + (isSupabase ? 'Create Tables (SQL Editor)' : 'Show Schema SQL') + '</button>'
    +   (isSupabase ? '' : '<button class="smb-btn" data-act="db-bootstrap">Auto-Create Tables</button>')
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
    + '<div class="inst-conn-sub-hint">Add a cloud provider\'s key here to see and control its servers as tiles below. Stored encrypted in the app vault.</div>'
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
function _factHtml(icon, label, valueHtml) {
  if (!valueHtml) return '';
  return '<div class="inst-fact"><span class="inst-fact-label"><i data-lucide="' + icon + '"></i>' + _esc(label) + '</span>'
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
// The best address to SHOW for an instance: its reported URL, else the cloud VM's
// IP, else (for this device) the browser's own origin, else localhost:<port>.
function _displayUrl(d) {
  if (d.endpoint) return d.endpoint;
  if (d.kind === 'cloud' && d.ip) return 'http://' + d.ip;
  if (d.is_self) { try { return location.origin; } catch (_) {} }
  return 'localhost:' + _localPort();
}
// The "URL" fact — a clickable link to _displayUrl(d) (always present).
function _urlFactHtml(d) {
  const url = _displayUrl(d);
  if (!url) return '';
  return _factHtml('link', 'URL',
    '<a href="' + _escAttr(_openUrl(url)) + '" target="_blank" rel="noopener">' + _esc(url) + '</a>');
}

// The "Tunnel" fact — this instance's Remote Access tunnel (cloudflared / ngrok),
// published in its heartbeat (d.tunnel). Shown only when a MANAGED tunnel is set
// up on that instance; a state pill (Running / Off / Not ready), the provider,
// and the public address (a named tunnel shows its fixed hostname even when off).
function _tunnelProvName(p) {
  return p === 'cloudflare' ? 'Cloudflare' : (p === 'ngrok' ? 'ngrok' : (p || ''));
}
function _tunnelFactHtml(d) {
  const t = d.tunnel;
  if (!t || !t.provider) return '';   // no managed tunnel configured on this instance
  const pill = t.running ? '<span class="inst-tun-pill inst-tun-on">Running</span>'
    : (t.configured ? '<span class="inst-tun-pill inst-tun-off">Off</span>'
      : '<span class="inst-tun-pill inst-tun-na">Not ready</span>');
  const link = t.public_url
    ? '<a href="' + _escAttr(_openUrl(t.public_url)) + '" target="_blank" rel="noopener">' + _esc(t.public_url) + '</a>'
    : '<span class="inst-db-note">no public address yet</span>';
  const val = pill + ' <span class="inst-tun-prov">' + _esc(_tunnelProvName(t.provider)) + '</span>'
    + '<div class="inst-tun-url">' + link + '</div>';
  return _factHtml('globe-lock', 'Tunnel', val);
}
// Start/Stop-tunnel buttons for an ONLINE instance whose tunnel is set up. The
// command is dispatched to that instance over the device queue (it runs
// cloudflared itself). Off + configured → Start; Running → Stop. iid is the
// registry instance id to address (a cloud VM passes its fleet-twin device id).
function _tunnelActionsHtml(d, iid, online) {
  const t = d.tunnel;
  if (!t || !t.provider || !iid || !online) return [];
  if (t.running) {
    return ['<button class="inst-tb-btn" data-act="tunnel-stop" data-iid="' + _escAttr(iid)
      + '"><i data-lucide="square"></i>Stop tunnel</button>'];
  }
  if (t.configured) {
    return ['<button class="inst-tb-btn" data-act="tunnel-start" data-iid="' + _escAttr(iid)
      + '"><i data-lucide="cloud"></i>Start tunnel</button>'];
  }
  return [];
}

// Server/repo fleet buttons for an ONLINE instance — Restart · Pull · Commit &
// push. Each is dispatched to that instance over the device queue (it runs the
// action on its OWN machine + repo), exactly like the tunnel buttons above. iid is
// the registry instance id to address. Restart is offered only for a REMOTE device
// (this machine already has its own Reset in the Server card, which shows a live
// health probe); Pull + Commit & push are offered everywhere including self.
function _deviceControlHtml(iid, online, isSelf) {
  if (!iid || !online) return [];
  const out = [];
  if (!isSelf) {
    out.push('<button class="inst-tb-btn" data-act="device-restart" data-iid="' + _escAttr(iid)
      + '"><i data-lucide="rotate-ccw"></i>Restart</button>');
  }
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
    +     '<span class="inst-srv-reset-label-text">Server reset:</span>'
    +     '<button class="inst-tb-btn inst-srv-reset" data-act="server-reset" data-state="idle" title="Restart this server"' + dis + '>'
    +       '<i data-lucide="rotate-ccw"></i><span class="inst-srv-reset-label">Reset</span>'
    +     '</button>'
    +   '</div>'
    +   '<label class="smb-label" style="margin-top:12px;">This app’s port</label>'
    +   '<div class="inst-srv-port-row">'
    +     '<input id="inst-hub-port" class="smb-input" type="number" min="1024" max="65535" value="' + _escAttr(String(hubPort)) + '"' + dis + '>'
    +     '<button class="inst-tb-btn" data-act="hub-port-save"' + dis + '><i data-lucide="refresh-cw"></i>Change &amp; relaunch</button>'
    +   '</div>'
    +   '<div class="smb-hint">Each deployment needs its own port. Changing this <strong>restarts WebAgent</strong> on the new port — this connection drops and you reopen it at the new address.</div>'
    +   '<span class="inst-srv-status" data-hub-status></span>'
    + '</div></div>'
    + '</div>';
}

// ── Local checkout tile Overview ─────────────────────────────────────────────
// A sibling WebAgent checkout on this machine, managed like a cloud VM: facts +
// Open / Start / Stop / Delete + inline edit of its name / folder / port, with the
// Start/Stop stream in a live log. Created from New instance → New Deployment →
// "This device". id math: the tile's instance_id is 'local:<id>'; the registry id
// used by the endpoints is d.local_id.
function _renderLocalOverview(d) {
  const m = _sibMeta(d.status);
  const running = d.status === 'running';
  const busy = S.localBusy;
  const dis = busy ? ' disabled' : '';
  const statusHtml = '<span class="inst-cloud-status"><span class="cvm-dot ' + m.dot + '"></span>' + _esc(m.label) + '</span>';

  let warn = '';
  if (d.is_checkout === false) warn = '<div class="inst-db-note inst-db-warn" style="margin-top:8px;">This folder is no longer a WebAgent checkout — fix the path below before starting.</div>';
  else if (d.port_aware === false) warn = '<div class="inst-db-note inst-db-warn" style="margin-top:8px;">This checkout is too old to run on a custom port — update it (git pull) before starting.</div>';

  const facts = [
    _factHtml('activity', 'Status', statusHtml),
    _factHtml('database', 'Database', '<span class="inst-db-badge inst-db-sep">Not shared</span>'
      + '<span class="inst-db-note">A separate WebAgent install on this machine — its own database.</span>'),
    _urlFactHtml(d),
    _factHtml('plug', 'Port', '<span class="mono">' + _esc(String(d.port)) + '</span>'),
    _factHtml('folder', 'Repo folder', '<span class="mono">' + _esc(d.folder) + '</span>'),
  ].join('');

  const actions = [];
  if (running && d.url) actions.push('<a class="inst-tb-btn" href="' + _escAttr(d.url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (running) actions.push('<button class="inst-tb-btn" data-act="local-stop" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="square"></i>Stop</button>');
  else actions.push('<button class="inst-tb-btn" data-act="local-start" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="play"></i>Start</button>');
  actions.push('<button class="inst-tb-btn inst-tb-danger" data-act="local-remove" data-lid="' + _escAttr(d.local_id) + '"' + dis + '><i data-lucide="trash-2"></i>Delete</button>');

  const edit = '<div class="inst-local-edit">'
    + '<label class="smb-label">Name</label>'
    + '<input class="smb-input" type="text" data-local-field="label" value="' + _escAttr(d.label) + '">'
    + '<label class="smb-label" style="margin-top:8px;">Repo folder</label>'
    + '<input class="smb-input" type="text" data-local-field="folder" value="' + _escAttr(d.folder) + '" spellcheck="false">'
    + '<label class="smb-label" style="margin-top:8px;">Port</label>'
    + '<input class="smb-input" type="number" min="1024" max="65535" data-local-field="port" value="' + _escAttr(String(d.port)) + '">'
    + '<div class="inst-detail-actions" style="margin-top:10px;"><button class="inst-tb-btn" data-act="local-save" data-lid="' + _escAttr(d.local_id) + '"' + dis + '>Save changes</button>'
    + '<span class="inst-srv-status" data-local-status></span></div>'
    + '</div>';

  return '<div class="inst-facts">' + facts + '</div>'
    + warn
    + '<div class="inst-detail-actions">' + actions.join('') + '</div>'
    + '<span class="inst-ping-status"></span>'
    + '<pre id="inst-log" class="cvm-log" hidden></pre>'
    + '<div class="inst-conn-section"><div class="inst-conn-section-head"><i data-lucide="sliders-horizontal"></i><span>Edit</span></div>' + edit + '</div>';
}

function _renderDeviceOverview(d) {
  const status = d.online
    ? '<span class="inst-pill-online">Online</span>'
    : '<span class="inst-pill-offline">Offline</span>';
  const seen = _relTime(d.last_seen);
  const repoHtml = d.repo
    ? '<a href="' + _escAttr(_repoUrl(d.repo)) + '" target="_blank" rel="noopener">' + _esc(d.repo) + '</a>' : '';

  const facts = [
    _factHtml('activity', 'Status', status + (seen ? ' <span class="inst-fact-value mono" style="display:inline">· seen ' + _esc(seen) + '</span>' : '')),
    _factHtml('database', 'Database', _dbShareValue(d)),
    _urlFactHtml(d),
    _tunnelFactHtml(d),
    _factHtml(_platformIcon(d.platform), 'Platform', d.platform ? _esc(d.platform) : ''),
    _factHtml('git-branch', 'Branch', d.branch ? _esc(d.branch) : ''),
    _factHtml('github', 'Repository', repoHtml),
    (d._stub ? ''
      : '<div class="inst-fact"><span class="inst-fact-label"><i data-lucide="fingerprint"></i>Instance ID</span>'
        + '<span class="inst-fact-value mono">' + _esc(d.instance_id) + '</span></div>'),
  ].join('');

  const actions = [];
  if (d.endpoint) actions.push('<a class="inst-tb-btn" href="' + _escAttr(_openUrl(d.endpoint)) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (d.endpoint) actions.push('<button class="inst-tb-btn" data-act="ping" data-addr="' + _escAttr(d.endpoint) + '"><i data-lucide="activity"></i>Ping</button>');
  _tunnelActionsHtml(d, d.instance_id, d.online).forEach(a => actions.push(a));
  // Restart / Pull / Commit & push this device over the fleet queue (Restart is
  // remote-only; this machine uses its own Reset in the Server card below). Skipped
  // for the pre-heartbeat self stub, whose synthetic id isn't a real registry claim
  // key yet (it would 404); it appears once the real heartbeat row lands.
  if (!d._stub) _deviceControlHtml(d.instance_id, d.online, d.is_self).forEach(a => actions.push(a));
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

  // Only THIS device carries the Server controls (restart / port / local checkouts,
  // moved here from Data Settings → Deployment) and the connection cards (DB/vault
  // sign-in + cloud accounts) — both act on the machine you're actually on, so they
  // live in its own tile. The self stub (pre-heartbeat) still owns this machine, so
  // it shows them too.
  const server = d.is_self ? _serverSectionHtml() : '';
  const conn = d.is_self ? _connCardsHtml() : '';

  return '<div class="inst-facts">' + facts + '</div>'
    + (actions.length ? '<div class="inst-detail-actions">' + actions.join('') + '</div>' : '')
    + '<span class="inst-ping-status"></span>'
    + server + conn;
}

function _renderCloudOverview(d) {
  const sm = _statusMeta(d.status);
  const statusHtml = '<span class="inst-cloud-status"><span class="cvm-dot ' + sm.dot + '"></span>' + _esc(sm.label) + '</span>';
  const url = d.ip ? ('http://' + d.ip) : '';
  const repoInner = d.repo
    ? '<a href="' + _escAttr(_repoUrl(d.repo)) + '" target="_blank" rel="noopener">' + _esc(d.repo) + '</a>'
    : '<span class="inst-repo-none">no repo set</span>';
  const repoHtml = repoInner
    + ' <button class="inst-repo-edit" data-act="repo-edit" data-key="' + _escAttr(d.annotation_key || '') + '" title="Set the repo this runs"><i data-lucide="pencil"></i></button>';

  const facts = [
    _factHtml('activity', 'Status', statusHtml),
    _factHtml('database', 'Database', _dbShareValue(d)),
    _factHtml('cloud', 'Provider', d.providerName ? _esc(d.providerName) : ''),
    _factHtml('cpu', 'Machine type', d.machine_type ? _esc(d.machine_type) : ''),
    _factHtml('globe', 'IP address', d.ip ? '<span class="mono">' + _esc(d.ip) + '</span>' : ''),
    _urlFactHtml(d),
    _tunnelFactHtml(d),
    _factHtml('map-pin', 'Zone', d.zone ? _esc(d.zone) : ''),
    _factHtml('calendar', 'Created', d.created ? _esc(_fmtDate(d.created)) : ''),
    _factHtml('git-branch', 'Repository', repoHtml),
    _factHtml('server', 'Name', '<span class="mono">' + _esc(d.name) + '</span>'),
  ].join('');

  const running = (d.status || '').toUpperCase() === 'RUNNING';
  const stopped = ['TERMINATED', 'SUSPENDED'].includes((d.status || '').toUpperCase());
  const busy = S.cloudBusy;
  const dis = busy ? ' disabled' : '';
  const actions = [];
  if (running && url) actions.push('<a class="inst-tb-btn" href="' + _escAttr(url) + '" target="_blank" rel="noopener"><i data-lucide="external-link"></i>Open</a>');
  if (d.ip) actions.push('<button class="inst-tb-btn" data-act="ping" data-addr="' + _escAttr(d.ip) + '"' + dis + '><i data-lucide="activity"></i>Ping</button>');
  if (stopped) actions.push('<button class="inst-tb-btn" data-act="cloud-start" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="play"></i>Start</button>');
  if (running) actions.push('<button class="inst-tb-btn" data-act="cloud-stop" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="square"></i>Stop</button>');
  // Tunnel control only for a VM correlated to an ONLINE fleet twin (it's the
  // twin's device that runs cloudflared and claims the job).
  if (!busy) _tunnelActionsHtml(d, d.device_id, d.fleet_online).forEach(a => actions.push(a));
  actions.push('<button class="inst-tb-btn inst-tb-danger" data-act="cloud-delete-ask" data-ref="' + _escAttr(d._ref) + '" data-name="' + _escAttr(d.name) + '"' + dis + '><i data-lucide="trash-2"></i>Delete</button>');

  // Destructive delete is gated behind a type-the-name confirm (shown inline once
  // the admin clicks Delete) — a reflex click shouldn't be able to destroy a real
  // server + its disk.
  const confirm = (S.confirmDel === d._ref && !busy) ? _delConfirmHtml(d) : '';

  return '<div class="inst-facts">' + facts + '</div>'
    + '<div class="inst-detail-actions">' + actions.join('') + '</div>'
    + confirm
    + '<span class="inst-ping-status"></span>'
    + '<pre id="inst-log" class="cvm-log" hidden></pre>';
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

function _renderTab(d) { return _renderOverview(d); }

function _detailHtml(d) {
  const platKey = d.kind === 'local' ? 'local' : _platformKey(d.platform);
  const tabs = _TABS.map(([id, label]) =>
    '<button class="inst-tab' + (id === S.tab ? ' active' : '') + '" data-tab="' + id + '">' + _esc(label) + '</button>'
  ).join('');
  const sub = (d.kind === 'cloud')
    ? (_statusMeta(d.status).label + (d.providerName ? ' · ' + d.providerName : ''))
    : (d.kind === 'local')
      ? (_sibMeta(d.status).label + ' · Port ' + d.port)
      : ((d.online ? 'Online' : 'Offline') + (d.platform ? ' · ' + d.platform : ''));
  // Head + tab bar live in a sticky wrapper so they pin to the top of the
  // .inst-grid scroller in carousel mode while the body below keeps scrolling
  // (mirrors the Agents page — see the sticky-header rules in instances.css).
  // Device tiles (shared registry) can be renamed + re-iconed from here, mirroring
  // the Agents page: the icon chip opens the shared icon picker, the name is
  // inline-editable (double-click). Cloud VMs + the pre-heartbeat self stub aren't
  // editable (their identity isn't a shared-registry row we can override).
  const editable = _instEditable(d);
  const iconWrap = '<span class="inst-icon-wrap plat-' + platKey + (editable ? ' inst-icon-editable' : '') + '"'
    + (editable ? ' data-act="inst-icon" data-iid="' + _escAttr(d.instance_id) + '" title="Click to change icon"' : '')
    + '>' + _instIconInner(d) + '</span>';
  const nameInner = editable
    ? '<span class="inst-name-edit" data-iid="' + _escAttr(d.instance_id) + '" title="Double-click to rename">' + _esc(_instName(d)) + '</span>'
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
    +   '<div class="inst-tabs">' + tabs + '</div>'
    + '</div>'
    + '<div class="inst-detail-body">' + _renderTab(d) + '</div>'
    + '</div>';
}

// The "New instance" detail card — same shell as an instance's detail (sticky head
// + tab bar) but with a "New Deployment" tab whose body is an empty host. The
// actual deploy/domain form is fetched + re-parented into #inst-nd-host by the
// new-deployment sub-feature (mountNewDeployment), so it never bloats this render.
function _newDetailHtml() {
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
    +   '<div class="inst-tabs"><button class="inst-tab active" data-tab="new-deploy">New Deployment</button></div>'
    + '</div>'
    // The host starts with a lightweight SKELETON of the four collapsed bars so the
    // card is never blank while the real form loads (mountNewDeployment removes it
    // once the fetched fragment is attached — see new-deployment.js _clearSkeleton).
    + '<div class="inst-detail-body"><div id="inst-nd-host" class="inst-nd-host">' + _ndSkeletonHtml() + '</div></div>'
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
      '<div class="inst-toolbar">'
    +   '<div class="inst-toolbar-titles">'
    +     '<span class="inst-toolbar-title"><i data-lucide="boxes"></i> Instances</span>'
    +     '<span class="inst-toolbar-sub">' + spinner + _esc(sub) + '</span>'
    +   '</div>'
    +   '<button class="inst-tb-btn" data-act="refresh"' + (S.loading ? ' disabled' : '') + '><i data-lucide="refresh-cw"></i> Refresh</button>'
    + '</div>'
    + '<div class="inst-grid' + (carousel ? ' carousel' : '') + '">'
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
  // Mount (or re-parent) the New Deployment form into its freshly-rendered host.
  if (isNew) { const host = document.getElementById('inst-nd-host'); if (host) _mountNewDeploy(host); }
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
  if (foreground) { S.loading = true; _render(); }
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
    _render();
  }
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
  _render();
}

// This machine's own WebAgent server(s) — the hub (this app) + any registered
// sibling checkouts — for the "Server" section on the This device tile. Backed by
// /admin/deploy/instances (app/local_instances.py). Cheap; refreshed on the poll.
async function _loadLocal() {
  const r = await _get('/admin/deploy/instances').catch(() => null);
  if (r && r.body && Array.isArray(r.body.instances)) S.local = r.body;
  else if (S.local === null) S.local = { instances: [], hub_port: parseInt(_localPort(), 10) || 8080 };
  _render();
}

// Cloud providers (which clouds can be added at all).
async function _loadProviders() {
  const r = await _get('/admin/instances/providers').catch(() => null);
  const list = (r && r.body && r.body.providers) || [];
  S.providers = list.filter(p => p.available !== false || p.has_key || _hasSavedId(p));
}

// Cloud VMs across every connected account → normalized tiles merged into the grid.
async function _loadCloud(foreground) {
  if (S.providers === null) await _loadProviders();
  const accounts = _accounts().filter(_isConnected);
  const parts = await Promise.all(accounts.map(async (p) => {
    const r = await _get('/admin/instances/cloud-instances?provider=' + encodeURIComponent(p.id)).catch(() => null);
    const data = (r && r.body) || {};
    if (!Array.isArray(data.instances)) return [];
    return data.instances.map(inst => _normalizeCloud(inst, p));
  }));
  S.cloud = parts.flat();
  // Exempt the virtual New instance tile (never in _list(); see _load's note).
  if (S.active && S.active !== NEW_DEPLOY_ID && !_find(S.active) && !_activePending()) _setActive(null);   // an open cloud tile that vanished
  _render();
}

function _normalizeCloud(inst, p) {
  const sm = _statusMeta(inst.status);
  const ref = String(p.id) + '|' + String(inst.zone) + '|' + String(inst.name);
  return {
    kind: 'cloud', instance_id: 'cloud:' + ref, label: inst.name, online: sm.online,
    platform: 'cloud', endpoint: inst.ip ? ('http://' + inst.ip) : '', repo: inst.repo || '', branch: '',
    provider: p.id, providerName: p.display_name, zone: inst.zone, name: inst.name,
    status: inst.status, machine_type: inst.machine_type || '', ip: inst.ip || '', created: inst.created || '',
    is_this_app: !!inst.is_this_app, is_webagent: !!inst.is_webagent, annotation_key: inst.annotation_key || '',
    _ref: ref,
  };
}

// ── Output-box + status helpers ──────────────────────────────────────────────
function _out(id, text, ok) {
  const el = document.getElementById(id); if (!el) return;
  el.hidden = false;
  el.style.color = ok === true ? 'var(--success)' : ok === false ? 'var(--danger)' : '';
  el.textContent = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
}
function _pingStatus(text, kind) {
  const el = document.querySelector('.inst-ping-status'); if (!el) return;
  el.textContent = text || '';
  el.className = 'inst-ping-status' + (kind === 'ok' ? ' ok' : kind === 'err' ? ' err' : '');
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
  const r = await _post('/admin/storage/db/schema-sql', { dialect, idempotent: provider !== 'supabase' });
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

// Start / stop the Remote Access tunnel on an instance. The command is dispatched
// over the cross-device job queue — the target machine runs cloudflared itself —
// so there's no live stream to watch here; instead we confirm it was queued and
// then re-poll the fleet a few times so the tile flips to Running (+ its public
// URL) once the target acts and its next heartbeat lands.
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
    _pingStatus(action === 'stop'
      ? 'Stop sent — the device will shut its tunnel down shortly.'
      : 'Start sent — the device is bringing its tunnel up (this can take a few seconds).', 'ok');
    // The target acts within its poll interval (instant for this device, up to
    // ~15s for a remote one); refresh a few times so the new state shows.
    [2000, 6000, 12000, 20000].forEach(ms => setTimeout(() => _load(false), ms));
  } catch (e) {
    _pingStatus('Tunnel command failed.', 'err');
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
// Repo annotation edit (cloud VMs). key = the VM's annotation_key.
async function _editRepo(key) {
  if (!key) return;
  const next = window.prompt('Which code repo does this run?\n\nEnter "owner/repo" or a git URL, or leave blank to clear.');
  if (next === null) return;
  await _post('/admin/instances/annotate', { key, repo: next.trim() });
  _loadCloud(false);
}

// ═══════════════════════════════════════════════════════════════════════════
// Server-section actions (This device) — restart / re-port this app + manage local
// sibling checkouts. Moved here from Data Settings → Deployment (deploy.js). Talk to
// /admin/storage/server/* and /admin/deploy/instances/*.
// ═══════════════════════════════════════════════════════════════════════════
function _srvStatus(el, text, err) {
  if (!el) return;
  el.textContent = text || '';
  el.style.color = err ? 'var(--danger)' : '';
}

// ── Two-tap "Server reset" confirm ───────────────────────────────────────────
// Mirrors the chat header's arm→confirm delete affordance (SHARED-DELETE-CONTROL),
// but restarts this server instead of deleting. First tap arms the button (danger
// tint + "Confirm reset" + hazard icon) and auto-reverts after 3s; the second tap
// fires _doRestart. Icons swap by rebuilding innerHTML so Lucide re-renders them.
function _srvResetInner(state) {
  return state === 'armed'
    ? '<i data-lucide="alert-triangle"></i><span class="inst-srv-reset-label">Confirm reset</span>'
    : '<i data-lucide="rotate-ccw"></i><span class="inst-srv-reset-label">Reset</span>';
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
    hub.innerHTML = '<div class="smb-hint" style="line-height:1.6;">WebAgent is restarting on port <strong>'
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

// NDJSON stream reader (same shape as the Deploy card + the old Server Manager).
async function _stream(path, body, onDone) {
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
      else if (ev.message) _logLine(ev.message, ev.level);
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
  if (tab) { S.tab = tab.dataset.tab; _render(); return; }

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
      case 'refresh': _load(true); _loadCloud(true); _loadConn(); _loadLocal(); return;
      case 'close': _setActive(null); S.confirmDel = null; _render(); return;
      case 'fix-db':
        // Open THIS device's Overview (where the DB sign-in card now lives) and
        // expand the database card.
        _setActive(S.self || _selfId()); S.tab = 'overview'; S.open.db = true;
        _render();
        if (S.status === null) _loadConn();
        return;
      case 'acct-add': _addAccount(); return;
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
      case 'device-restart': _deviceControl('restart', act.dataset.iid, act); return;
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
      case 'cloud-delete-ask': S.confirmDel = act.dataset.ref; _render(); return;
      case 'cloud-delete-cancel': S.confirmDel = null; _render(); return;
      case 'cloud-delete-go':
        if (_delMatches()) { S.confirmDel = null; _doCloudAction('delete', act.dataset.ref, act.dataset.name); }
        return;
      case 'repo-edit': _editRepo(act.dataset.key); return;
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
    _render();
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
    // Press-and-hold confirm for the Unlink button (fill-to-fire, release aborts).
    el.addEventListener('pointerdown', _holdStart);
    el.addEventListener('pointerup', _holdEnd);
    el.addEventListener('pointercancel', _holdEnd);
    el.addEventListener('lostpointercapture', _holdEnd);
  }
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
    else { S.active = S.self || _selfId(); S.tab = 'overview'; }
  }
  S.loading = true;
  _render();
  _load(true);
  _loadCloud(true);   // fetches providers then cloud VMs; also fills the Connections account count
  _loadConn();        // DB + vault health for the Connections cards + degraded banner
  _loadLocal();       // this machine's WebAgent server(s) — the Server card + local tiles
  _prewarmNewDeploy(); // warm the New-Deployment fragment in the background (off the critical path)
  // Let the New-Deployment "This device" target (deploy.js _registerAdd) refresh the
  // local tiles the instant it registers a new checkout, so it shows up without
  // waiting for the 20s poll.
  window.__instancesReloadLocal = _loadLocal;
  if (S.poll) clearInterval(S.poll);
  // Silent presence refresh — but never while a cloud Start/Stop/Delete is
  // streaming (a re-render would tear down the live log element mid-stream), nor
  // while the New Deployment form is open (a re-render re-parents its node, which
  // would blur whatever field the admin is typing in).
  S.poll = setInterval(() => {
    // Skip while a cloud action streams, the New Deployment form is open, a delete
    // confirm is armed, a name is being inline-edited, or a Server-section action is
    // streaming / a Server field has focus (a re-render would blow away the typed
    // value / live log / contenteditable field).
    if (S.cloudBusy || S.active === NEW_DEPLOY_ID || S.confirmDel || S.editing || S.localBusy || S.localEditing) return;
    _load(false); _loadCloud(false); _loadLocal();
  }, 20000);
}

export function stopView() {
  if (S.poll) { clearInterval(S.poll); S.poll = null; }
  S.loading = false;
  S.editing = null;   // never leave the poll pinned off after navigating away mid-edit
  if (window.__instancesReloadLocal === _loadLocal) window.__instancesReloadLocal = null;
}
