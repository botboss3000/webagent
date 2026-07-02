'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agent Settings tab — Models, Integrations, Agent Tools, Admin Chat.
 *
 * Sections:
 *   1. Models        — providers, model search, default-model pick, auto-save
 *   2. Integrations  — OAuth scopes, generic providers, config cards
 *   3. Agent Tools   — ability compact rows, enable/disable
 *   4. Admin Chat    — integration admin assistant chat
 *
 * This is the largest App Config tab (~2,200 lines). Sub-split candidates:
 * agent-settings-models.js, agent-settings-integrations.js, agent-settings-abilities.js
 */

import { apiPath } from '../../../shared/js/config.js';
import { app } from '../../../shared/js/state.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js';
import { build as buildAdminAbilityTable } from '../../../shared/js/admin-ability-table.js';
import { wireChatPillUploads } from '../../../shared/js/attachments.js';
import { spawnWebagentPageChat } from '../../../chat-widget/js/chat-widget.js';
// Bottom page-assistant pill: the shared hybrid SEARCH/CHAT pill mounted at the
// bottom of this page (App-Settings page-assistant style), plus the shared context
// engine that swaps its hint + wraps its prompt by the hovered section. See
// ../page-assistant.js and ui/shared/js/dom-utils.js (buildAbilitySearchPill).
import { createPageAssistant } from '../page-assistant.js';
import { registerSectionHook } from '../nav.js';
import { initStickyNav } from '../sticky-nav.js';
import { mountModelTable } from '../../../shared/js/model-table.js';
// Automation Engine panels (relocated from the standalone App Config "Automation"
// and "Event Sources" tabs) — the scheduler + event-sources halves rendered in
// this page's "Automation Engine" section. init() wires their buttons; load()
// runs lazily on first expand (see init()).
import { init as initSchedulerPanel, load as loadSchedulerPanel } from './scheduler.js';
import { init as initEventSourcesPanel, load as loadEventSourcesPanel } from './event-sources.js';
import {
  _fetch, _qs, _esc, _setIntStatus,
  _makeCopyBtn, _copyToClipboard, _bindUri, _attachUriCopy,
} from '../utils.js';
// Unified save-status indicator (spinner → ✓ / ⚠-with-revert, centred ON TOP of
// the control) — one save language for every toggle / dropdown / input / Save
// button across the config panels. See docs/claude/ui-guidance.md.
import { _markSaving, _flashSaveCheck, buildAbilitySearchPill } from '../../../shared/js/dom-utils.js';

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 1: Models ─────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
// SISTER-PANEL: MODEL-TABLE — the Models configurator + saved-models table is
// the shared component ui/shared/js/model-table.js, mounted here AND on the agent
// card's Config tab; keep the two mirrored (change the component, not a copy).
// This file only supplies the admin data adapter (the user/admin default provider
// config + the multi-providers roster) and relocates the app-global extras
// (AI suggestions + "Extend default LLM to agents") into the table's Advanced panel.

let _modelTable = null;

// Timestamp of the last full Agent-Settings data load (model table + suggestions
// + integrations). init() kicks these off when the Admin tab initialises; load()
// fires again the moment the App Config view is activated — on a first open both
// happen within the same tick, which used to double every fetch (including the
// slow /admin/integrations and the model-usage N+1). load() consults this stamp
// and skips the redundant re-fetch when init() just triggered one, while still
// refreshing on genuine later re-activations.
let _lastAgentLoadAt = 0;

function _adminModelAdapter() {
  const headers = { 'Content-Type': 'application/json' };
  return {
    loadConfig: async () => {
      let single = {}, multi = {};
      try { const r = await _fetch(apiPath('/admin/settings/provider')); if (r.ok) single = await r.json(); } catch (_) {}
      try { const r = await _fetch(apiPath('/admin/settings/multi-providers')); if (r.ok) multi = await r.json(); } catch (_) {}
      return {
        provider: single.provider, base_url: single.base_url, api_key: single.api_key, model: single.model,
        text_capable: single.text_capable, image_capable: single.image_capable, image_out_capable: single.image_out_capable,
        providerConfigs: single.providers || {},
        roster: multi.providers || [],
      };
    },
    saveSingle: async (s) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      const payload = { ...s, use_for_image_out: s.image_out_capable };
      const r = await _fetch(apiPath('/admin/settings/provider'), { method: 'POST', headers, body: JSON.stringify(payload) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    },
    saveRoster: async ({ providers }) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      const payload = {
        providers: providers.map(p => ({
          provider: p.provider === '_custom' ? 'custom' : p.provider,
          base_url: p.base_url, api_key: p.api_key, model: p.model,
          enabled: p.enabled,
          text_capable: p.text_capable !== false, image_capable: !!p.image_capable, use_for_image: !!p.use_for_image,
          image_out_capable: !!p.image_out_capable, use_for_image_out: !!p.use_for_image_out,
          high_effort_capable: !!p.high_effort_capable,
        })),
      };
      // Surface a real failure so the saved-model box settles to the orange ⚠
      // (and reverts) instead of falsely flashing a green ✓.
      const r = await _fetch(apiPath('/admin/settings/multi-providers'), { method: 'POST', headers, body: JSON.stringify(payload) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    },
    clearSingle: async () => {
      if (!isAdmin()) { showRestrictedModal(); return; }
      await _fetch(apiPath('/admin/settings/provider/clear'), { method: 'POST', headers });
    },
    // Provider presets — try the server first, fall back to a hardcoded mirror
    // of PROVIDER_PRESETS in app/admin/settings.py so the dropdown always shows
    // every known provider even when the backend endpoint is unreachable.
    loadPresets: async () => {
      try {
        const r = await _fetch(apiPath('/admin/settings/providers'));
        if (r.ok) { const d = await r.json(); if (d && Object.keys(d).length) return d; }
      } catch (_) {}
      return {
        openrouter:  { name: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1' },
        openai:      { name: 'OpenAI',           base_url: 'https://api.openai.com/v1' },
        groq:        { name: 'Groq',             base_url: 'https://api.groq.com/openai/v1' },
        together:    { name: 'Together AI',       base_url: 'https://api.together.xyz/v1' },
        deepseek:    { name: 'DeepSeek',          base_url: 'https://api.deepseek.com/v1' },
        mistral:     { name: 'Mistral AI',        base_url: 'https://api.mistral.ai/v1' },
        fireworks:   { name: 'Fireworks AI',      base_url: 'https://api.fireworks.ai/inference/v1' },
        xai:         { name: 'xAI (Grok)',        base_url: 'https://api.x.ai/v1' },
        perplexity:  { name: 'Perplexity',        base_url: 'https://api.perplexity.ai' },
        ollama:      { name: 'Ollama (local)',    base_url: 'http://localhost:11434/v1' },
        deepinfra:   { name: 'DeepInfra',         base_url: 'https://api.deepinfra.com/v1/openai' },
        lmstudio:    { name: 'LM Studio (local)', base_url: 'http://localhost:1234/v1' },
      };
    },
  };
}

function _initModelTable() {
  const host = _qs('ac-model-host');
  if (!host) return;
  _modelTable = mountModelTable(host, {
    adapter: _adminModelAdapter(),
    fetchFn: (...a) => _fetch(...a),
    apiPath,
    // Admin Agent Settings shows app-wide usage: every agent, every user, and
    // background tasks (git messages, suggestion text, compaction, embeddings).
    usageScope: 'global',
    advancedExtra: (body) => {
      // Relocate the app-global extras into the table's advanced body. NOTE: this
      // runs while `body` is still DETACHED from the document (mountModelTable
      // attaches the table to the host only after this callback), so the moved
      // checkbox is NOT yet findable by document.getElementById — its change
      // listener is wired in init() right after _initModelTable() returns, once
      // the table (and this checkbox) are in the document.
      const extras = _qs('ac-model-adv-extras');
      if (extras) { extras.style.display = ''; body.appendChild(extras); }
    },
    onModelChange: () => { app.refreshModelContext?.(); },
  });
}

// -- AI Message Suggestions checkbox ----------------------------------------
// Toggle on/off the silent suggestion-engine. "Off" by default.
function _initSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  cb.addEventListener('change', _saveSuggestionsCheckbox);
}

async function _loadSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  try {
    const res = await _fetch(apiPath('/api/v1/chat/suggestions/config'));
    if (res.ok) {
      const cfg = await res.json();
      cb.checked = cfg.mode === 'on' || cfg.mode === 'scheduler';
    }
  } catch (_) {}
}

async function _saveSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  // Overlay sits on the whole checkbox row (the wrapping label).
  const ctrl = cb.closest('label');
  const mode = cb.checked ? 'on' : 'off';
  _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath('/api/v1/chat/suggestions/config'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(ctrl, true);
  } catch (e) {
    cb.checked = !cb.checked;  // revert on failure
    _flashSaveCheck(ctrl, false, e.message);
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 2b: Integrations ─────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

const _PROVIDER_SCOPES = {
  google: [
    { value: 'openid',                                              label: 'Sign in (OpenID)' },
    { value: 'email',                                               label: 'Email address' },
    { value: 'profile',                                             label: 'Basic profile' },
    { value: 'https://www.googleapis.com/auth/drive.file',          label: 'Drive: app-created files' },
    { value: 'https://www.googleapis.com/auth/drive',               label: 'Drive: all files (read & write)' },
    { value: 'https://www.googleapis.com/auth/drive.readonly',      label: 'Drive: all files (read only)' },
    { value: 'https://www.googleapis.com/auth/docs',                label: 'Docs: read & write' },
    { value: 'https://www.googleapis.com/auth/docs.readonly',       label: 'Docs: read only' },
    { value: 'https://www.googleapis.com/auth/calendar',            label: 'Calendar: read & write' },
    { value: 'https://www.googleapis.com/auth/calendar.readonly',   label: 'Calendar: read only' },
    { value: 'https://www.googleapis.com/auth/gmail.modify',        label: 'Gmail: read & modify' },
    { value: 'https://www.googleapis.com/auth/gmail.compose',       label: 'Gmail: compose' },
    { value: 'https://www.googleapis.com/auth/gmail.readonly',      label: 'Gmail: read only' },
  ],
  microsoft: [
    { value: 'openid',               label: 'Sign in (OpenID)' },
    { value: 'email',                label: 'Email address' },
    { value: 'profile',              label: 'Basic profile' },
    { value: 'offline_access',       label: 'Stay signed in (refresh tokens)' },
    { value: 'User.Read',            label: 'User profile' },
    { value: 'Mail.ReadWrite',       label: 'Mail: read & write' },
    { value: 'Mail.Send',            label: 'Mail: send' },
    { value: 'Calendars.ReadWrite',  label: 'Calendar: read & write' },
    { value: 'Files.ReadWrite.All',  label: 'OneDrive: read & write' },
    { value: 'Sites.ReadWrite.All',  label: 'SharePoint: read & write' },
  ],
  yahoo: [
    { value: 'openid',   label: 'Sign in (OpenID)' },
    { value: 'email',    label: 'Email address' },
    { value: 'profile',  label: 'Basic profile' },
    { value: 'mail-r',   label: 'Mail: read' },
    { value: 'mail-w',   label: 'Mail: write' },
  ],
  dropbox: [
    { value: 'account_info.read',      label: 'Account info' },
    { value: 'files.metadata.read',    label: 'Files: read metadata' },
    { value: 'files.metadata.write',   label: 'Files: write metadata' },
    { value: 'files.content.read',     label: 'Files: read content' },
    { value: 'files.content.write',    label: 'Files: write content' },
    { value: 'sharing.read',           label: 'Sharing: read' },
    { value: 'sharing.write',          label: 'Sharing: write' },
  ],
  meta: [
    { value: 'public_profile',              label: 'Public profile' },
    { value: 'email',                       label: 'Email address' },
    { value: 'pages_manage_posts',          label: 'Pages: manage posts' },
    { value: 'pages_read_engagement',       label: 'Pages: read engagement' },
    { value: 'instagram_basic',             label: 'Instagram: basic' },
    { value: 'instagram_content_publish',   label: 'Instagram: publish content' },
    { value: 'instagram_manage_comments',   label: 'Instagram: manage comments' },
    { value: 'instagram_manage_insights',   label: 'Instagram: insights' },
  ],
  twitter: [
    { value: 'tweet.read',          label: 'Tweets: read' },
    { value: 'tweet.write',         label: 'Tweets: write' },
    { value: 'tweet.moderate.write',label: 'Tweets: moderate' },
    { value: 'users.read',          label: 'Users: read' },
    { value: 'follows.read',        label: 'Follows: read' },
    { value: 'follows.write',       label: 'Follows: write' },
    { value: 'offline.access',      label: 'Stay signed in (refresh tokens)' },
    { value: 'like.read',           label: 'Likes: read' },
    { value: 'like.write',          label: 'Likes: write' },
    { value: 'dm.read',             label: 'DMs: read' },
    { value: 'dm.write',            label: 'DMs: write' },
  ],
  linkedin: [
    { value: 'openid',          label: 'Sign in (OpenID)' },
    { value: 'profile',         label: 'Basic profile' },
    { value: 'email',           label: 'Email address' },
    { value: 'w_member_social', label: 'Posts: write' },
    { value: 'r_liteprofile',   label: 'Profile: read' },
    { value: 'r_emailaddress',  label: 'Email: read' },
  ],
  tiktok: [
    { value: 'user.info.basic',    label: 'User: basic info' },
    { value: 'user.info.profile',  label: 'User: profile' },
    { value: 'video.list',         label: 'Videos: list' },
    { value: 'video.upload',       label: 'Videos: upload' },
    { value: 'video.publish',      label: 'Videos: publish' },
  ],
  pinterest: [
    { value: 'boards:read',         label: 'Boards: read' },
    { value: 'boards:write',        label: 'Boards: write' },
    { value: 'pins:read',           label: 'Pins: read' },
    { value: 'pins:write',          label: 'Pins: write' },
    { value: 'user_accounts:read',  label: 'Account: read' },
  ],
  reddit: [
    { value: 'identity',        label: 'Identity' },
    { value: 'read',            label: 'Posts: read' },
    { value: 'submit',          label: 'Posts: submit' },
    { value: 'history',         label: 'History' },
    { value: 'mysubreddits',    label: 'Subreddits' },
    { value: 'privatemessages', label: 'Messages' },
  ],
  snapchat: [
    { value: 'https://auth.snapchat.com/oauth2/api/user.display_name',   label: 'Display name' },
    { value: 'https://auth.snapchat.com/oauth2/api/user.bitmoji.avatar', label: 'Bitmoji avatar' },
    { value: 'https://auth.snapchat.com/oauth2/api/user.external_id',    label: 'External ID' },
  ],
  twitch: [
    { value: 'user:read:email',              label: 'Email address' },
    { value: 'user:read:follows',            label: 'Follows: read' },
    { value: 'channel:read:subscriptions',   label: 'Subscriptions: read' },
    { value: 'channel:manage:broadcast',     label: 'Broadcast: manage' },
    { value: 'clips:edit',                   label: 'Clips: edit' },
    { value: 'chat:read',                    label: 'Chat: read' },
    { value: 'chat:edit',                    label: 'Chat: write' },
  ],
  ebay: [
    { value: 'https://api.ebay.com/oauth/api_scope',                              label: 'Public APIs' },
    { value: 'https://api.ebay.com/oauth/api_scope/buy.order.readonly',           label: 'Buy: orders (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.inventory',               label: 'Sell: inventory' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.inventory.readonly',      label: 'Sell: inventory (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.account',                 label: 'Sell: account' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.account.readonly',        label: 'Sell: account (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.fulfillment',             label: 'Sell: fulfillment' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly',    label: 'Sell: fulfillment (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.marketing',               label: 'Sell: marketing' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.marketing.readonly',      label: 'Sell: marketing (read)' },
  ],
  etsy: [
    { value: 'email_r',          label: 'Email (read)' },
    { value: 'profile_r',        label: 'Profile (read)' },
    { value: 'shops_r',          label: 'Shops (read)' },
    { value: 'shops_w',          label: 'Shops (write)' },
    { value: 'listings_r',       label: 'Listings (read)' },
    { value: 'listings_w',       label: 'Listings (write)' },
    { value: 'listings_d',       label: 'Listings (delete)' },
    { value: 'transactions_r',   label: 'Transactions (read)' },
  ],
  shopify: [
    { value: 'read_products',    label: 'Products (read)' },
    { value: 'write_products',   label: 'Products (write)' },
    { value: 'read_inventory',   label: 'Inventory (read)' },
    { value: 'write_inventory',  label: 'Inventory (write)' },
    { value: 'read_orders',      label: 'Orders (read)' },
    { value: 'write_orders',     label: 'Orders (write)' },
    { value: 'read_locations',   label: 'Locations (read)' },
  ],
  amazon: [
    { value: 'sellingpartnerapi::client_credential:refresh_token', label: 'SP-API refresh token' },
  ],
};

function _renderScopeCheckboxes(provider) {
  const formEl = document.getElementById(`ac-int-${provider}-form`);
  if (!formEl) return;
  const scopes = _PROVIDER_SCOPES[provider];
  if (!scopes || scopes.length === 0) return;

  const container = document.createElement('div');
  container.id = `ac-int-${provider}-scopes-wrap`;
  container.style.cssText = 'margin-top:14px;';

  const header = document.createElement('div');
  header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;';
  header.innerHTML = `
    <label class="ac-label" style="margin:0;">Enabled Scopes</label>
    <span style="font-size:11px;color:var(--fg-muted);">
      <a href="#" id="ac-int-${provider}-scopes-all" style="color:var(--fg-muted);text-decoration:underline;">all</a>
      &nbsp;/&nbsp;
      <a href="#" id="ac-int-${provider}-scopes-none" style="color:var(--fg-muted);text-decoration:underline;">none</a>
    </span>`;
  container.appendChild(header);

  const grid = document.createElement('div');
  grid.id = `ac-int-${provider}-scopes-grid`;
  grid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;';

  scopes.forEach((scope, i) => {
    const row = document.createElement('label');
    row.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;color:var(--fg-main);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    row.title = scope.value;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = `ac-scope-${provider}-${i}`;
    cb.value = scope.value;
    cb.checked = true;
    cb.style.cssText = 'flex-shrink:0;accent-color:var(--accent);';
    row.appendChild(cb);
    row.appendChild(document.createTextNode(scope.label));
    grid.appendChild(row);
  });
  container.appendChild(grid);

  // Insert before the save button row
  const saveBtn = document.getElementById(`ac-int-${provider}-save`);
  const saveRow = saveBtn ? saveBtn.parentElement : null;
  if (saveRow && saveRow.parentElement === formEl) {
    formEl.insertBefore(container, saveRow);
  } else {
    formEl.appendChild(container);
  }

  // "all" / "none" links
  document.getElementById(`ac-int-${provider}-scopes-all`)?.addEventListener('click', e => {
    e.preventDefault();
    grid.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = true);
  });
  document.getElementById(`ac-int-${provider}-scopes-none`)?.addEventListener('click', e => {
    e.preventDefault();
    grid.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
  });
}

function _setScopeSelection(provider, enabledScopes) {
  const grid = document.getElementById(`ac-int-${provider}-scopes-grid`);
  if (!grid) return;
  const checkboxes = grid.querySelectorAll('input[type=checkbox]');
  if (!enabledScopes || enabledScopes.length === 0) {
    checkboxes.forEach(cb => cb.checked = true);
    return;
  }
  const set = new Set(enabledScopes);
  checkboxes.forEach(cb => { cb.checked = set.has(cb.value); });
}

function _getSelectedScopes(provider) {
  const grid = document.getElementById(`ac-int-${provider}-scopes-grid`);
  if (!grid) return null;
  const checked = [...grid.querySelectorAll('input[type=checkbox]')]
    .filter(cb => cb.checked)
    .map(cb => cb.value);
  return checked;
}

// ── Agent Tools — compact ability rows ─────────────────────────────────────
//
// Each ability is rendered as a single row (icon + name + desc + toggle).
// Simple abilities (no config needed) toggle directly.
// Complex abilities (need credentials/config) show a warning when toggled
// and expand to reveal the config panel. The toggle only activates once
// all required fields are filled.

// ── Ability render data — FALLBACK ONLY ────────────────────────────────────
// ── Ability catalog ───────────────────────────────────────────────────────
// The admin-ability-table.js component fetches and caches the catalog for its
// own rendering. This host file keeps a lightweight copy of abilities + groups
// for _compactifyAllIntegrations / _placeIntegrationGroupCards (the transitional
// path that still uses HTML config cards placed into group slots). Once those
// HTML cards move into the component, this can be removed.
let _catAbilities = {};  // populated from catalog, used by _catNote

// Fetch the server-built ability catalog ONCE and populate the minimum needed
// for the transitional integration-card path. The UI component (admin-ability-
// table.js) fetches the catalog independently.
let _abilityCatalogPromise = null;
function _ensureAbilityCatalog() {
  if (_abilityCatalogPromise) return _abilityCatalogPromise;
  _abilityCatalogPromise = (async () => {
    try {
      const res = await fetch('/api/v1/abilities/catalog');
      if (!res.ok) return;
      const cat = await res.json();
      if (!cat || !Array.isArray(cat.groups) || !cat.groups.length) return;

      _catAbilities = cat.abilities || {};

      // Rebuild integration groups from catalog data (used by _placeIntegrationGroupCards).
      const intGroups = [];
      for (const g of (cat.groups || [])) {
        const members = [];
        const soonCards = [];
        for (const mid of (g.members || [])) {
          const ab = cat.abilities && cat.abilities[mid];
          if (!ab) continue;
          if (ab.placeholder || ab.kind === 'placeholder') {
            soonCards.push(mid);
          } else if (ab.kind === 'oauth' || ab.kind === 'credential' || ab.kind === 'channel') {
            members.push({ id: mid, kind: ab.kind });
          }
        }
        intGroups.push({
          id: g.id, name: g.name, icon: g.icon, color: g.color, desc: g.desc,
          members: members, soonCards: soonCards, soonRowsKey: null,
        });
      }
      _INTEGRATION_GROUPS.length = 0;
      _INTEGRATION_GROUPS.push(...intGroups);
    } catch (e) {
      console.warn('Ability catalog fetch failed', e);
    }
  })();
  return _abilityCatalogPromise;
}

// ── Catalog-backed note lookup (used by _compactifyCard) ─────────────────────
function _catNote(id) {
  return (_catAbilities && _catAbilities[id] && _catAbilities[id].note) || '';
}

let _abilityStates = {}; // { [ability]: boolean }
let _adminTableHandle = null;  // returned by AdminAbilityTable.build()
let _pa = null;                // bottom page-assistant pill instance (search/chat + context)

// When the admin toggles an ability, we stamp the moment of that action here.
// `_loadIntegrations()` runs on every settings/admin-tools (re)activation and
// force-applies a fetched snapshot onto every toggle. That fetch is slow (it
// makes ~30 sequential DB lookups), so if the admin toggles an ability while a
// load is still in flight, the load would resolve with its PRE-toggle snapshot
// and silently revert the just-made change — the backend keeps the new value
// (so the agent's Abilities page is correct) but the admin toggle flips back,
// which reads as "it didn't save". We guard against that by skipping, in the
// load's apply step, any ability the admin changed after that load began.
let _abilityLastActionAt = {}; // { [ability]: DOMHighResTimeStamp }

function _markAbilityAction(ability) {
  _abilityLastActionAt[ability] = (typeof performance !== 'undefined' ? performance.now() : Date.now());
}

// Build one always-visible ability row (icon + name + desc + toggle + delete).
// Used as a member row inside a group body. The delete button uses a long-press
// (hold 1.5s) to prevent accidental deletion — it fills a progress ring then
// fires the delete API on completion.

// (Web Scraper + Browser Cookies are now ordinary drop-in abilities — their
// credential forms render inline in the Web group of the shared ability table,
// so the old _placeWebCredentialRows relocation is gone.)


function _applyAbilityRowStatus(ability, enabled) {
  _abilityStates[ability] = enabled;
  const toggle = _qs(`ac-ability-toggle-${ability}`);
  if (toggle) toggle.checked = !!enabled;
  const row = _qs(`ac-ability-row-${ability}`);
  if (row) row.classList.toggle('ac-ability-enabled', !!enabled);
}

function _initCollapsible(provider) {
  const card = document.getElementById(`ac-int-${provider}-card`);
  if (!card) return;
  const header = card.querySelector(':scope > div');
  if (!header) return;

  // Wrap everything after the header in a collapsible body div
  const body = document.createElement('div');
  body.id = `ac-int-${provider}-body`;
  body.style.display = 'none';
  [...card.children].slice(1).forEach(el => body.appendChild(el));
  card.appendChild(body);

  // Remove header bottom margin (restored when expanded)
  header.style.marginBottom = '0';
  header.style.cursor = 'pointer';
  header.style.userSelect = 'none';

  // Chevron icon — appended after the badge
  const chevron = document.createElement('span');
  chevron.id = `ac-int-${provider}-chevron`;
  chevron.style.cssText = 'display:flex;align-items:center;margin-left:8px;flex-shrink:0;transition:transform 0.2s;color:var(--fg-muted);';
  chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;
  header.appendChild(chevron);

  header.addEventListener('click', e => {
    if (e.target.closest('button, a, input')) return;
    _toggleCard(provider);
  });
}

function _toggleCard(provider, forceOpen) {
  const body = document.getElementById(`ac-int-${provider}-body`);
  const chevron = document.getElementById(`ac-int-${provider}-chevron`);
  const card = document.getElementById(`ac-int-${provider}-card`);
  const header = card?.querySelector(':scope > div');
  if (!body) return;
  const isOpen = body.style.display !== 'none';
  const open = forceOpen !== undefined ? forceOpen : !isOpen;
  body.style.display = open ? 'block' : 'none';
  if (chevron) chevron.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
  if (header) header.style.marginBottom = open ? '12px' : '0';
}

function _expandCard(provider) {
  _toggleCard(provider, true);
}

// ── Unified compact rows for every integration / channel / generic ─────────
// Goal: the rest of the page looks like the Agent Tools table — one slim row
// (icon · name · requirement note · toggle · chevron) that expands to the
// existing config form. We reuse each card's collapsible body (built by
// _initCollapsible) and its existing save/OAuth/unconfigure wiring; only the
// row chrome + the requirement note are layered on top.

const _OAUTH_PROVIDER_IDS = ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon'];
const _GENERIC_PROVIDER_IDS = [];  // Web Scraper/Browser Cookies are now drop-in abilities
const _CHANNEL_IDS = ['telegram'];
const _SOON_CHANNEL_IDS = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

// One-to-1.5-sentence requirement note per row (cost / OAuth / special setup).
// Coming-soon integrations that have no config card — rendered as greyed rows
// into the per-category containers added in the section HTML.


// ── Former settings "categories" → expandable groups in the ONE Agent Tools
// table ──────────────────────────────────────────────────────────────────────
// Each entry renders as an `.ac-row.ac-group` appended into #ac-abilities-compact
// right after the host-ability groups (Administrator / Core / Web), so the whole
// tab reads as a single flush table instead of a stack of collapsible category
// sections. Members are existing integration cards (relocated into the group body
// at runtime by _placeIntegrationGroupCards) and/or coming-soon placeholder rows.
//
// The group's 3-position toggle (Off · Mixed · On) reflects and bulk-controls
// ONLY the available members — coming-soon rows never count, and a group with no
// available members yet (Payments / Developer / CRM) shows no toggle at all, just
// an expand chevron. Turning a group off disables/unconfigures its members;
// turning it on can only enable members that need no credentials (a channel) —
// an OAuth app still needs its keys entered on its own row.
const _INTEGRATION_GROUPS = [];  // populated from catalog at runtime

function _integrationGroupOf(id) {
  return _INTEGRATION_GROUPS.find(g => g.members.some(m => m.id === id));
}

// Build the empty group shells (head + body with a slot per card member) and
// append them to the single Agent Tools table, after the host-ability groups.
// The real cards are relocated into the slots later by _placeIntegrationGroupCards.
function _placeIntegrationGroupCards() {
  for (const group of _INTEGRATION_GROUPS) {
    const body = document.getElementById(`ac-group-body-${group.id}`);
    if (!body) continue;
    // Available member cards → their slots, in order.
    for (const m of group.members) {
      const slot = document.getElementById(`ac-group-slot-${m.id}`);
      const card = document.getElementById(`ac-int-${m.id}-card`);
      if (slot && card) slot.replaceWith(card);
    }
    // Coming-soon channel cards (real cards, compactified as 'soon' rows).
    for (const sid of (group.soonCards || [])) {
      const card = document.getElementById(`ac-int-${sid}-card`);
      if (card) body.appendChild(card);
    }
    // Coming-soon rows are now rendered by admin-ability-table.js from the catalog.
  }
  // Hide the emptied legacy category sections (their cards now live in groups).
  for (const cid of ['ac-cat-channels', 'ac-cat-productivity', 'ac-cat-social', 'ac-cat-marketplace', 'ac-cat-payments', 'ac-cat-developer', 'ac-cat-crm']) {
    const cat = document.getElementById(cid);
    if (cat) cat.style.display = 'none';
  }
}

// ── Integration-group 3-position toggle: reflect state + bulk on/off ─────────
// Only AVAILABLE members count (coming-soon never does). "On" = configured /
// enabled (the row's badge carries ac-int-badge-on, read by _rowConfigured).
function _compactifyCard(id, kind) {
  const card = _qs(`ac-int-${id}-card`);
  if (!card || card.dataset.compactified) return;
  if (!_qs(`ac-int-${id}-body`)) { try { _initCollapsible(id); } catch (_) {} }
  card.classList.add('ac-card-compact');

  const header = card.querySelector(':scope > div');
  const soon = (kind === 'soon');
  const note = _catNote(id) || '';

  // Clone the card's existing brand icon (keeps Google's multicolour mark etc.)
  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  const srcIcon = header ? header.querySelector('svg, i[data-lucide]') : null;
  if (srcIcon) {
    const ic = srcIcon.cloneNode(true);
    ic.setAttribute('width', '18'); ic.setAttribute('height', '18');
    ic.style.width = '18px'; ic.style.height = '18px';
    iconWrap.appendChild(ic);
  }

  let name = id;
  if (header) {
    const nameNode = header.querySelector('div[style*="font-weight:600"]');
    if (nameNode) name = nameNode.textContent.trim();
  }

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = note;

  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-int-row' + (soon ? ' ac-soon-row' : '');
  row.id = `ac-int-row-${id}`;
  row.appendChild(iconWrap);
  row.appendChild(label);

  if (soon) {
    const pill = document.createElement('span');
    pill.className = 'ac-soon-pill';
    pill.textContent = 'Coming soon';
    row.appendChild(pill);
    card.insertBefore(row, card.firstChild);
    for (const ch of [...card.children]) { if (ch !== row) ch.style.display = 'none'; }
    card.dataset.compactified = '1';
    return;
  }

  const toggleWrap = document.createElement('label');
  toggleWrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  toggleWrap.title = 'Enable';
  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.className = 'conn-toggle ac-int-row-toggle';
  toggle.id = `ac-int-row-toggle-${id}`;
  const track = document.createElement('span');
  track.className = 'conn-toggle-track';
  toggleWrap.appendChild(toggle);
  toggleWrap.appendChild(track);

  const chevron = document.createElement('span');
  chevron.className = 'ac-int-row-chevron';
  chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;

  row.appendChild(toggleWrap);
  row.appendChild(chevron);
  if (header) header.style.display = 'none';
  card.insertBefore(row, card.firstChild);
  card.dataset.compactified = '1';

  // Row click (away from the toggle) expands / collapses the config body.
  row.addEventListener('click', (e) => {
    if (e.target.closest('.ac-ability-toggle-wrap')) return;
    const body = _qs(`ac-int-${id}-body`);
    const open = body && body.style.display !== 'none';
    _toggleCard(id, !open);
    chevron.style.transform = !open ? 'rotate(90deg)' : 'rotate(0deg)';
  });

  // Toggle semantics: channels enable/disable directly; OAuth/generic providers
  // expand the form when turned on unconfigured, and unconfigure when turned off.
  toggle.addEventListener('change', () => {
    const on = toggle.checked;
    // The save overlay sits on the row's toggle switch.
    if (kind === 'channel') {
      if (on) _enableChannel(id, toggleWrap); else _disableChannel(id, toggleWrap);
      return;
    }
    if (on) {
      if (!_rowConfigured(id)) {
        toggle.checked = false;
        _toggleCard(id, true);
        chevron.style.transform = 'rotate(90deg)';
      }
    } else if (_rowConfigured(id)) {
      if (!confirm(`Disable ${name}? This removes the saved app credentials.`)) {
        toggle.checked = true;
        return;
      }
      _unconfigureProvider(id, toggleWrap);
    }
  });
}

function _compactifyAllIntegrations() {
  for (const p of _OAUTH_PROVIDER_IDS) _compactifyCard(p, 'oauth');
  for (const p of _GENERIC_PROVIDER_IDS) _compactifyCard(p, 'generic');
  for (const c of _CHANNEL_IDS) _compactifyCard(c, 'channel');
  for (const c of _SOON_CHANNEL_IDS) _compactifyCard(c, 'soon');
  _placeIntegrationGroupCards();    // channels/providers into their groups; hide legacy categories
  if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
}

function _rowConfigured(id) {
  const badge = _qs(`ac-int-${id}-badge`);
  return badge && badge.classList.contains('ac-int-badge-on');
}

function _syncRowToggle(id) {
  const toggle = _qs(`ac-int-row-toggle-${id}`);
  if (!toggle) return;
  const on = _rowConfigured(id);
  toggle.checked = on;
  const row = _qs(`ac-int-row-${id}`);
  if (row) row.classList.toggle('ac-ability-enabled', on);
  // Keep the parent group's header tri in step with this row's configured state.
  const grp = _integrationGroupOf(id);
  if (grp) _syncIntegrationGroupTri(grp);
}

function _syncAllRowToggles() {
  for (const id of [..._OAUTH_PROVIDER_IDS, ..._GENERIC_PROVIDER_IDS, ..._CHANNEL_IDS]) {
    _syncRowToggle(id);
  }
}

// ── Integration-group header tri-toggle (reflect + bulk on/off) ──────────────
// The shared admin-ability-table.js builds a *disabled* group tri for groups
// that hold no on/off "app abilities" — which is every credential-only group
// (Productivity, Social, Marketplace, …): their members are OAuth/credential
// providers and channels, which that component deliberately ignores. Here we
// adopt that otherwise-dead tri and give it the behaviour it used to have before
// the table migration: it REFLECTS how many of the group's providers/channels
// are configured (Off · Mixed · On) and BULK-controls them — the left half turns
// every configured provider OFF (removing its saved app credentials), the right
// half enables every member that needs no credentials (a channel). An OAuth app
// still needs its keys entered on its own row, so it is never bulk-enabled.
// (The provider/channel state + actions all live in this file — `_rowConfigured`,
// `_unconfigureProvider`, `_enableChannel`, `_disableChannel` — so this stays an
// admin-page concern and the shared sister-panel component is untouched.)

// The configurable members of one integration group ([{id, kind}], coming-soon
// rows already excluded when _INTEGRATION_GROUPS is built from the catalog).
function _intGroupMembers(group) {
  return group.members || [];
}

// Paint one group's tri from how many of its members are currently configured.
function _syncIntegrationGroupTri(group) {
  const tri = _qs(`ac-group-tri-${group.id}`);
  if (!tri || !tri.dataset.intWired) return;
  const members = _intGroupMembers(group);
  if (!members.length) return;
  const onCount = members.filter(m => _rowConfigured(m.id)).length;
  tri.dataset.state = onCount === 0 ? 'off' : (onCount === members.length ? 'on' : 'mixed');
}

function _syncAllIntegrationGroupTris() {
  for (const g of _INTEGRATION_GROUPS) _syncIntegrationGroupTri(g);
}

// Bulk-apply a group tri click. goOn=false → turn every configured member off
// (one confirm, since unconfiguring removes saved credentials). goOn=true →
// enable only members that need no credentials (channels); OAuth providers can't
// be bulk-enabled without their keys, so they're left for their own row.
// `tri` (the group header toggle) carries the unified spinner → ✓ / ⚠ overlay
// for the whole bulk operation: the per-member enable/disable calls run WITHOUT
// their own overlay (no ctrl) so the one tri reflects the combined outcome.
async function _setIntegrationGroup(group, goOn, tri) {
  const members = _intGroupMembers(group);
  let allOk = true;
  if (goOn) {
    if (tri) _markSaving(tri);
    for (const m of members) {
      if (m.kind === 'channel' && !_rowConfigured(m.id)) {
        if (!(await _enableChannel(m.id))) allOk = false;
      }
    }
  } else {
    const configured = members.filter(m => _rowConfigured(m.id));
    if (!configured.length) { _syncIntegrationGroupTri(group); return; }
    const names = configured.map(m => m.id).join(', ');
    if (!confirm(`Turn off every configured provider in ${group.name}?\n\n`
        + `This removes the saved app credentials for: ${names}.`)) {
      _syncIntegrationGroupTri(group);
      return;   // cancelled — no overlay shown (we never marked it saving)
    }
    if (tri) _markSaving(tri);
    for (const m of configured) {
      const ok = (m.kind === 'channel') ? await _disableChannel(m.id) : await _unconfigureProvider(m.id);
      if (!ok) allOk = false;
    }
  }
  _syncIntegrationGroupTri(group);
  if (tri) _flashSaveCheck(tri, allOk);
}

// Adopt the shared component's disabled tri for each credential-only group: drop
// the disabled styling, restore keyboard access, and attach the click/keydown
// bulk handlers (mirrors _wireTriToggle's left=off / right=on hit-testing). Only
// groups the shared component left *disabled* are adopted, so a group that has a
// real on/off ability (and thus an active ability tri) is never double-wired.
function _wireIntegrationGroupTris() {
  for (const group of _INTEGRATION_GROUPS) {
    if (!_intGroupMembers(group).length) continue;
    const tri = _qs(`ac-group-tri-${group.id}`);
    if (!tri || tri.dataset.intWired) continue;
    if (!tri.classList.contains('ac-tri-disabled')) continue;  // active ability tri — leave it

    tri.classList.remove('ac-tri-disabled');
    tri.removeAttribute('aria-disabled');
    tri.setAttribute('tabindex', '0');
    tri.title = 'Off · Mixed · On — click the left half to turn off every '
      + 'configured provider, the right half to enable channels';
    tri.dataset.intWired = '1';

    const onSetAll = (goOn) => { _setIntegrationGroup(group, goOn, tri); };
    tri.addEventListener('click', e => {
      e.stopPropagation();
      const r = tri.getBoundingClientRect();
      onSetAll((e.clientX - r.left) > r.width / 2);
    });
    tri.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onSetAll(tri.dataset.state !== 'on');
      }
    });
    _syncIntegrationGroupTri(group);
  }
}

async function _initIntegrations() {
  // Load the drop-in ability catalog before any ability UI is built so the
  // panel reflects whatever lives in plugins/abilities/ (not the JS fallback).
  await _ensureAbilityCatalog();
  const providers = ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon'];
  for (const p of providers) {
    _initCollapsible(p);
    _renderScopeCheckboxes(p);
    _qs(`ac-int-${p}-save`)?.addEventListener('click', () => _saveProviderConfig(p));
    _qs(`ac-int-${p}-edit`)?.addEventListener('click', () => _editProviderConfig(p));
    _qs(`ac-int-${p}-unconfigure`)?.addEventListener('click', (e) => _unconfigureProvider(p, e.currentTarget));
  }
  // (Web Scraper + Browser Cookies are drop-in abilities now; their credential
  // forms are wired by the shared ability table, not here.)

  // Channels (admin enable/disable; per-agent creds live in the agent's Abilities tab)
  const channels = ['telegram'];
  for (const c of channels) {
    _initCollapsible(c);
    _qs(`ac-int-${c}-save`)?.addEventListener('click', (e) => _enableChannel(c, e.currentTarget));
    _qs(`ac-int-${c}-unconfigure`)?.addEventListener('click', (e) => _disableChannel(c, e.currentTarget));
  }
  // Coming-soon channel placeholders — searchable but not interactive.
  const comingSoonChannels = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

  // Complex ability config panels (image_generation, automation, visualizer)
  // have save/unconfigure buttons in #ac-ability-config-panels. Wire them.
  const complexAbilities = ['image_generation', 'automation', 'visualizer'];
  for (const a of complexAbilities) {
    _qs(`ac-int-${a}-save`)?.addEventListener('click', (e) => _enableAbility(a, e.currentTarget));
    _qs(`ac-int-${a}-unconfigure`)?.addEventListener('click', (e) => _disableAbility(a, e.currentTarget));
  }

  // Agent Tools — rendered by the shared admin-ability-table.js component.
  // See SISTER-PANEL: AGENT-ABILITY-TABLE in agent-ability-table.js.
  const container = _qs('ac-abilities-compact');
  if (container) {
    _adminTableHandle = await buildAdminAbilityTable(container, {
      abilityStates: _abilityStates,
      integrationStates: {},
      callbacks: {
        onEnableChannel: _enableChannel,
        onDisableChannel: _disableChannel,
        onUnconfigureProvider: _unconfigureProvider,
      },
      skipIntegrationGroups: true,  // integration groups still use _compactifyCard for now
      // The search/chat pill is NOT mounted inline above the table here — this page
      // mounts the same shared pill at the BOTTOM of the page (App-Settings page-
      // assistant style); see _mountBottomPageAssistant below.
      showSearch: false,
    });
    // Build integration group shells into the same .ac-list grid so
    // _compactifyAllIntegrations + _placeIntegrationGroupCards can relocate
    // the compactified cards into them.
    const grid = container.querySelector('.ac-list');
  }

  // Mount the bottom search/chat pill once the table exists (so its filter has a
  // grid to drive). Guarded on the table handle so the pill never mounts early.
  _mountBottomPageAssistant();

  // ── Init collapsible for the remaining integration provider cards.
  // These are still in the section HTML and compactified by
  // _compactifyAllIntegrations below. Once they move into the shared table
  // this init + compactify step can be removed.

  // Unify every OAuth / channel / generic card into the same compact-row look,
  // and render the card-less coming-soon integrations as greyed rows.
  _compactifyAllIntegrations();
  // Adopt the shared component's otherwise-dead group tris so a credential-only
  // group (Productivity, Social, …) reflects + bulk-controls its providers.
  _wireIntegrationGroupTris();

  _initIntegrationsSearch([...providers, ...channels, ...comingSoonChannels]);
  _initIntegrationAdminChat();
}

// ── Bottom page-assistant pill (search/chat + context) ─────────────────────
// Mount the SHARED hybrid search/chat pill at the BOTTOM of this page (App-Settings
// page-assistant style) instead of inline above the ability table. Typing live-
// filters the ability tree via the table's exposed runFilter; send / Enter / attach
// chats with WebAgent the manager, with the message wrapped by the hovered section's
// context (createPageAssistant in attached mode — it owns only the placeholder swap,
// suggestions/typewriter, Tab-to-fill and buildMessage; the pill itself already
// wires send/voice/uploads/filter). The bar host (#ac-as-pa-bar) is a sibling below
// the scroller in app-config.html; its float layout + reveal-on-this-tab live in
// app3.css and the opaque skin in index.css.
function _mountBottomPageAssistant() {
  if (_pa) return;                          // already mounted (idempotent)
  const host = _qs('ac-as-pa-bar');
  if (!host || !_adminTableHandle) return;  // never mount before the table exists
  host.innerHTML = '';                      // guard against a stale pill on rebuild
  const pill = buildAbilitySearchPill(host, {
    onFilter: (q) => _adminTableHandle.runFilter?.(q),
    onChatSend: ({ text, attachmentIds }) =>
      spawnWebagentPageChat({ message: _pa.buildMessage(text), attachmentIds, title: _pa.title() }),
  });
  _pa = createPageAssistant({
    page: 'agent_settings',
    section: _qs('ac-section-agent-settings'),
    input: pill.input,
    wireComposer: false,                    // the pill already wires send/voice/uploads/filter
  });
  _pa.init();
}

// ── Integration Admin chat ───────────────────────────────────────────────
function _intAdminStorageKey() {
  const uid = app.currentUserId || 'anon';
  return `intAdminChat_session_${uid}`;
}

function _intAdminGetSession() {
  if (_intAdminSessionId) return _intAdminSessionId;
  try {
    const stored = localStorage.getItem(_intAdminStorageKey());
    if (stored) { _intAdminSessionId = stored; return stored; }
  } catch (_) {}
  const fresh = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('s-' + Date.now() + '-' + Math.random().toString(36).slice(2));
  _intAdminSessionId = fresh;
  try { localStorage.setItem(_intAdminStorageKey(), fresh); } catch (_) {}
  return fresh;
}

// Each visit to the Agent Settings section starts a brand-new WebAgent session,
// so the chat there is a fresh conversation (matching the page chat pills).
// Clears the cached session id + the inline transcript.
function _intAdminResetSession() {
  _intAdminSessionId = null;
  try { localStorage.removeItem(_intAdminStorageKey()); } catch (_) {}
  const box = _qs('ac-int-admin-chat-messages');
  if (box) { box.innerHTML = ''; box.style.display = 'none'; }
}

function _intAdminAppendBubble(role, text) {
  const box = _qs('ac-int-admin-chat-messages');
  if (!box) return null;
  if (box.style.display === 'none') box.style.display = 'block';
  const bubble = document.createElement('div');
  bubble.style.cssText = 'margin:6px 0;padding:8px 10px;border-radius:6px;white-space:pre-wrap;word-wrap:break-word;'
    + (role === 'user'
       ? 'background:var(--bg-2);align-self:flex-end;'
       : 'background:var(--bg-1);border:1px solid var(--border);');
  bubble.dataset.role = role;
  bubble.textContent = text || '';
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

function _intAdminShowError(msg) {
  const err = _qs('ac-int-admin-chat-error');
  if (!err) return;
  err.textContent = msg || '';
  err.style.display = msg ? 'block' : 'none';
}

function _intAdminSetBusy(busy) {
  const input = _qs('ac-int-admin-chat-input');
  const send = _qs('ac-int-admin-chat-send');
  if (input) input.disabled = busy;
  if (send) send.disabled = busy || !(input && input.value.trim());
}

// Integration Admin chat has its own send flow (separate template, no main
// chat forwarding), so it keeps its own preview bar and pending-attachments
// list rather than sharing the main chat's.
let _intAdminSessionId = null;
let _intAdminAbort = null;
let _intAdminWired = false;
const _intAdminPending = [];
function _intAdminClearPending() {
  _intAdminPending.length = 0;
  const bar = _qs('ac-int-admin-preview-bar');
  if (bar) { bar.innerHTML = ''; bar.style.display = 'none'; }
}

async function _intAdminSend() {
  const input = _qs('ac-int-admin-chat-input');
  if (!input) return;
  const text = (input.value || '').trim();
  if (!text) return;
  if (!app.currentUserId) {
    _intAdminShowError('Sign in to use the Integration Admin chat.');
    return;
  }
  _intAdminShowError('');

  // ═══════════════════════════════════════════════════════════════════════
  // PROMPT / PRETEXT NOTE:
  // The message sent to the agent from this Integration Admin chat pill is
  // loaded from app/defaults/app-prompts.json → ui_handoffs.admin_integrations_chat.
  // To change the tag format, edit that JSON file — NOT the fallback below.
  // ═══════════════════════════════════════════════════════════════════════

  // Try to load tag template from app-prompts.json, fall back to raw text
  let message = text;
  try {
    const resp = await fetch(apiPath('/api/v1/app-prompts'));
    if (resp.ok) {
      const data = await resp.json();
      const tpl = (data.ui_handoffs || {}).admin_integrations_chat?.template || '';
      if (tpl && tpl !== '{text}') {
        message = tpl.replace(/\{text\}/g, text);
      }
    }
  } catch (_) { /* fall through — use raw text */ }

  // This chat targets the shared WebAgent (the `default` template), same as the
  // Agents / Gen UI / Source Control pills. Resolve (or create) that agent first.
  let agentId;
  try {
    agentId = await app.ensureWebagentAgent(app.currentUserId);
  } catch (e) {
    _intAdminShowError('Could not start WebAgent: ' + (e.message || e));
    return;
  }

  input.value = '';
  _intAdminSetBusy(true);
  _intAdminAppendBubble('user', text);
  const agentBubble = _intAdminAppendBubble('agent', '…');
  let agentBuffer = '';

  const payload = {
    message: message,
    session_id: _intAdminGetSession(),
    user_id: app.currentUserId,
    agent_id: agentId,
  };
  if (_intAdminPending.length > 0) {
    payload.attachment_ids = _intAdminPending.map(a => a.attachment_id);
    _intAdminClearPending();
  }

  if (_intAdminAbort) { try { _intAdminAbort.abort(); } catch (_) {} }
  _intAdminAbort = new AbortController();

  try {
    const resp = await _fetch(apiPath('/api/v1/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: _intAdminAbort.signal,
    });
    if (!resp.ok) {
      const detail = resp.status === 403
        ? 'Admin access required.'
        : `Server error: ${resp.status}`;
      if (agentBubble) agentBubble.textContent = detail;
      _intAdminShowError(detail);
      _intAdminSetBusy(false);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let event;
        try { event = JSON.parse(line.slice(6)); } catch { continue; }
        if (event.type === 'stream') {
          agentBuffer += (event.content || '');
          if (agentBubble) {
            agentBubble.textContent = agentBuffer;
            const box = _qs('ac-int-admin-chat-messages');
            if (box) box.scrollTop = box.scrollHeight;
          }
        } else if (event.type === 'response') {
          agentBuffer = '';
          if (agentBubble) agentBubble.textContent = event.content || '';
        } else if (event.type === 'error') {
          _intAdminShowError(event.message || 'Unknown error');
          if (agentBubble) agentBubble.textContent = 'Error: ' + (event.message || '');
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      _intAdminShowError('Request failed: ' + e.message);
      if (agentBubble) agentBubble.textContent = 'Error: ' + e.message;
    }
  } finally {
    _intAdminSetBusy(false);
    _intAdminAbort = null;
  }
}

function _initIntegrationAdminChat() {
  if (_intAdminWired) return;
  const input = _qs('ac-int-admin-chat-input');
  const send = _qs('ac-int-admin-chat-send');
  if (!input || !send) return;
  const sync = () => { send.disabled = !input.value.trim(); };
  send.addEventListener('click', () => { _intAdminSend(); });
  input.addEventListener('input', sync);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _intAdminSend();
    }
  });

  const row = _qs('ac-int-admin-chat-input-row');
  const bar = _qs('ac-int-admin-preview-bar');
  if (row && bar) {
    wireChatPillUploads(row, input, {
      previewBar: bar,
      pending: _intAdminPending,
      onChange: (pending) => {
        bar.style.display = pending.length > 0 ? 'flex' : 'none';
      },
    });
  }

  sync();
  _intAdminWired = true;
}

function _initIntegrationsSearch(providers) {
  const input = _qs('ac-int-filter');
  if (!input) return;
  input.value = '';
  const emptyEl = _qs('ac-int-filter-empty');
  const cards = providers
    .map(p => {
      const card = document.getElementById(`ac-int-${p}-card`);
      if (!card) return null;
      const header = card.querySelector(':scope > div');
      const haystack = (p + ' ' + (header ? header.textContent : '')).toLowerCase();
      return { card, haystack };
    })
    .filter(Boolean);

  // Also include compact ability rows in search (member rows only — never the
  // group head rows, which always stay visible as section labels).
  const compactContainer = _qs('ac-abilities-compact');
  const compactRows = compactContainer
    ? [...compactContainer.querySelectorAll('.ac-ability-row:not(.ac-group-head):not(.ac-int-row)')]
    : [];
  const groupEls = compactContainer
    ? [...compactContainer.querySelectorAll('.ac-group')]
    : [];

  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let visible = 0;

    // While searching, expand every group so member rows are reachable; collapse
    // them all when the query is cleared.
    for (const g of groupEls) g.classList.toggle('expanded', !!q);

    // Track which categories have visible items
    const catVisibility = {};

    for (const { card, haystack } of cards) {
      const match = !q || haystack.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) {
        visible++;
        // Mark parent category
        const cat = card.closest('.ac-category-group');
        if (cat) catVisibility[cat.id] = true;
      }
    }
    // Filter compact rows
    for (const row of compactRows) {
      const name = row.querySelector('.ac-ability-name')?.textContent || '';
      const desc = row.querySelector('.ac-ability-desc')?.textContent || '';
      const haystack = (name + ' ' + desc).toLowerCase();
      const match = !q || haystack.includes(q);
      row.style.display = match ? '' : 'none';
      if (match) {
        visible++;
        // Parent is the agent-tools category
        const cat = _qs('ac-cat-agent-tools');
        if (cat) catVisibility[cat.id] = true;
      }
    }
    // Also filter config panels
    const configPanels = _qs('ac-ability-config-panels');
    if (configPanels) {
      const panels = configPanels.querySelectorAll('.ac-card');
      for (const panel of panels) {
        const text = (panel.textContent || '').toLowerCase();
        const match = !q || text.includes(q);
        panel.style.display = match ? '' : 'none';
        if (match) {
          visible++;
          const cat = _qs('ac-cat-agent-tools');
          if (cat) catVisibility[cat.id] = true;
        }
      }
    }

    // Hide a group entirely when none of its member rows/cards survive the
    // filter, so a search doesn't leave empty expanded groups behind.
    for (const g of groupEls) {
      if (!q) { g.style.display = ''; continue; }
      const body = g.querySelector('.ac-group-body');
      const anyVisible = body && [...body.children].some(el => el.style.display !== 'none');
      g.style.display = anyVisible ? '' : 'none';
    }

    // When searching, auto-open categories with matches; when cleared, close all.
    // Collapse is class-driven now (`.ac-open`) — the category groups are plain
    // <div>s under the sticky-nav flatten (display:contents broke <details>), so
    // the old native `.open` attribute no longer controls them.
    for (const cat of document.querySelectorAll('.ac-category-group')) {
      if (q) {
        cat.classList.toggle('ac-open', !!catVisibility[cat.id]);
      } else {
        cat.classList.remove('ac-open');
      }
    }

    if (emptyEl) emptyEl.style.display = (q && visible === 0) ? 'block' : 'none';
  };
  input.addEventListener('input', apply);
}

async function _loadIntegrations() {
  // Stamp the instant this load begins, BEFORE the (slow) fetch. Any ability the
  // admin toggles after this point must not be clobbered by this load's stale
  // snapshot — see `_abilityLastActionAt` above.
  const _loadStart = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  const _applyAbilityFromLoad = (ability, enabled) => {
    if ((_abilityLastActionAt[ability] || 0) > _loadStart) return; // user changed it mid-load
    _applyAbilityStatus(ability, enabled);
  };
  try {
    const res = await _fetch(apiPath('/admin/integrations'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _applyProviderStatus('google',    data.google_configured,    data.google_client_id,    data.redirect_uri,              data.google_scopes,    data.redirect_uri_suggested);
    _applyProviderStatus('microsoft', data.microsoft_configured, data.microsoft_client_id, data.microsoft_redirect_uri,    data.microsoft_scopes, data.microsoft_redirect_uri_suggested);
    _applyProviderStatus('yahoo',     data.yahoo_configured,     data.yahoo_client_id,     data.yahoo_redirect_uri,        data.yahoo_scopes,     data.yahoo_redirect_uri_suggested);
    _applyProviderStatus('dropbox',   data.dropbox_configured,   data.dropbox_client_id,   data.dropbox_redirect_uri,      data.dropbox_scopes,   data.dropbox_redirect_uri_suggested);
    _applyProviderStatus('meta',      data.meta_configured,      data.meta_client_id,      data.meta_redirect_uri,         data.meta_scopes,      data.meta_redirect_uri_suggested);
    _applyProviderStatus('twitter',   data.twitter_configured,   data.twitter_client_id,   data.twitter_redirect_uri,      data.twitter_scopes,   data.twitter_redirect_uri_suggested);
    _applyProviderStatus('linkedin',  data.linkedin_configured,  data.linkedin_client_id,  data.linkedin_redirect_uri,     data.linkedin_scopes,  data.linkedin_redirect_uri_suggested);
    _applyProviderStatus('tiktok',    data.tiktok_configured,    data.tiktok_client_id,    data.tiktok_redirect_uri,       data.tiktok_scopes,    data.tiktok_redirect_uri_suggested);
    _applyProviderStatus('pinterest', data.pinterest_configured, data.pinterest_client_id, data.pinterest_redirect_uri,    data.pinterest_scopes, data.pinterest_redirect_uri_suggested);
    _applyProviderStatus('reddit',    data.reddit_configured,    data.reddit_client_id,    data.reddit_redirect_uri,       data.reddit_scopes,    data.reddit_redirect_uri_suggested);
    _applyProviderStatus('snapchat',  data.snapchat_configured,  data.snapchat_client_id,  data.snapchat_redirect_uri,     data.snapchat_scopes,  data.snapchat_redirect_uri_suggested);
    _applyProviderStatus('twitch',    data.twitch_configured,    data.twitch_client_id,    data.twitch_redirect_uri,       data.twitch_scopes,    data.twitch_redirect_uri_suggested);
    _applyProviderStatus('ebay',      data.ebay_configured,      data.ebay_client_id,      data.ebay_redirect_uri,         data.ebay_scopes,      data.ebay_redirect_uri_suggested);
    _applyProviderStatus('etsy',      data.etsy_configured,      data.etsy_client_id,      data.etsy_redirect_uri,         data.etsy_scopes,      data.etsy_redirect_uri_suggested);
    _applyProviderStatus('shopify',   data.shopify_configured,   data.shopify_client_id,   data.shopify_redirect_uri,      data.shopify_scopes,   data.shopify_redirect_uri_suggested);
    _applyProviderStatus('amazon',    data.amazon_configured,    data.amazon_client_id,    data.amazon_redirect_uri,       data.amazon_scopes,    data.amazon_redirect_uri_suggested);
    _applyChannelStatus('telegram', data.telegram_configured);
    // Agent Tools — generic, catalog-driven. The backend returns an `abilities`
    // map {id: enabled} built from every kind="ability" drop-in, so a newly
    // dropped-in ability reflects its saved toggle here with NO per-ability
    // wiring. (Falls back to the legacy <id>_configured flags if the map is
    // absent, e.g. an older backend.)
    if (data.abilities && typeof data.abilities === 'object') {
      for (const [aid, enabled] of Object.entries(data.abilities)) {
        _applyAbilityFromLoad(aid, !!enabled);
      }
    } else {
      _applyAbilityFromLoad('codebase_admin',   data.codebase_admin_configured);
      _applyAbilityFromLoad('git_control',      data.git_control_configured);
      _applyAbilityFromLoad('ui_admin',         data.ui_admin_configured);
      _applyAbilityFromLoad('terminal_control', data.terminal_control_configured);
      _applyAbilityFromLoad('create_tools',     data.create_tools_configured);
      _applyAbilityFromLoad('automation',       data.automation_configured);
      _applyAbilityFromLoad('web_access',       data.web_access_configured);
      _applyAbilityFromLoad('browser_control',  data.browser_control_configured);
      _applyAbilityFromLoad('image_generation', data.image_generation_configured);
      _applyAbilityFromLoad('visualizer',       data.visualizer_configured);
      _applyAbilityFromLoad('agent_orchestration', data.agent_orchestration_configured);
      _applyAbilityFromLoad('diagnostics',         data.diagnostics_configured);
      _applyAbilityFromLoad('agent_management',    data.agent_management_configured);
      _applyAbilityFromLoad('app_control',         data.app_control_configured);
      _applyAbilityFromLoad('wiki_control',        data.wiki_control_configured);
      _applyAbilityFromLoad('image_vision',        data.image_vision_configured);
      _applyAbilityFromLoad('session_titler',      data.session_titler_configured);
    }
    // Reflect each provider/channel's configured state onto its unified row toggle.
    _syncAllRowToggles();
    // Reflect ability states onto each group's 3-position toggle
    // (the admin-ability-table.js component manages these now).
    if (_adminTableHandle && _adminTableHandle.syncAllTriToggles) {
      _adminTableHandle.syncAllTriToggles();
    }
    // …then re-sync the credential-only group tris LAST so they win: the shared
    // syncAllTriToggles above counts a group's placeholder rows (which carry
    // data-ability) and would otherwise force these groups back to "off".
    _syncAllIntegrationGroupTris();
  } catch (e) {
    for (const p of ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon', 'telegram', 'codebase_admin', 'create_tools', 'automation', 'web_access', 'browser_control', 'image_generation', 'visualizer', 'agent_orchestration', 'diagnostics', 'agent_management', 'app_control', 'wiki_control', 'image_vision', 'session_titler']) {
      const s = _qs(`ac-int-${p}-status`);
      _setIntStatus(s, `Failed to load: ${e.message}`);
    }
  }
}

function _applyChannelStatus(channel, enabled) {
  const badge        = _qs(`ac-int-${channel}-badge`);
  const configuredEl = _qs(`ac-int-${channel}-configured`);
  const form         = _qs(`ac-int-${channel}-form`);
  if (enabled) {
    if (badge) { badge.textContent = 'Enabled'; badge.className = 'ac-int-badge ac-int-badge-on'; }
    if (configuredEl) configuredEl.style.display = 'block';
    if (form) form.style.display = 'none';
  } else {
    if (badge) { badge.textContent = 'Disabled'; badge.className = 'ac-int-badge ac-int-badge-off'; }
    if (configuredEl) configuredEl.style.display = 'none';
    if (form) form.style.display = 'block';
  }
  _syncRowToggle(channel);
}

// `ctrl` (optional) is the control the user touched — the row toggle wrap or the
// in-card Enable/Disable button — so the unified spinner → ✓ / ⚠ overlay sits on
// it. Returns true on success / false on failure so the group-tri bulk path can
// reflect the outcome. On success the visible badge/form transition is the main
// confirmation; on failure the inline status text carries the detail too.
async function _enableChannel(channel, ctrl) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.style.display = 'none';
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, true);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

async function _disableChannel(channel, ctrl) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.style.display = 'none';
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, false);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// ── Agent Tools (admin enable/disable; per-agent picks in Abilities tab) ──
function _applyAbilityStatus(ability, enabled) {
  // Same DOM contract as channel cards (badge / configured / form).
  _applyChannelStatus(ability, enabled);
  // Also update the compact row if it exists
  _applyAbilityRowStatus(ability, enabled);
  // Hide the config panel when disabled
  if (!enabled) {
    const card = _qs(`ac-int-${ability}-card`);
    if (card) card.style.display = 'none';
  }
}

async function _enableAbility(ability, ctrl) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (statusEl) statusEl.style.display = 'none';
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, true);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

async function _disableAbility(ability, ctrl) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (ability === 'automation') {
    const ok = confirm(
      'Disable Automation?\n\n'
      + 'This will permanently delete every agent\'s scheduled tasks and event subscriptions. '
      + 'This cannot be undone.'
    );
    if (!ok) return false;
  }
  if (statusEl) statusEl.style.display = 'none';
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, false);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// (Web Scraper + Browser Cookies credential entry now lives in the shared
// ability table's inline credentials form — see _buildCredentialsSection in
// ui/shared/js/admin-ability-table.js. The old bespoke load/save/unconfigure
// helpers and their /admin/integrations/scraper + /browser-session calls are gone.)

// ── Provider status ───────────────────────────────────────────────────────

// Update the soft warning shown under the redirect_uri input when the host
// in the URI differs from the host the admin is viewing the app on. The
// host is the legitimate degree of freedom (apex vs www, tunnel, custom
// domain), so this is informational — not blocking.
function _refreshRedirectUriWarning(provider) {
  const input = _qs(`ac-int-${provider}-input-redirect_uri`);
  const warn  = _qs(`ac-int-${provider}-form-uri-warn`);
  if (!input || !warn) return;
  const value = (input.value || '').trim();
  if (!value) { warn.style.display = 'none'; return; }
  let urlHost = '';
  try { urlHost = new URL(value).host.toLowerCase(); } catch { warn.style.display = 'none'; return; }
  const here = (window.location.host || '').toLowerCase();
  if (!urlHost || !here || urlHost === here) { warn.style.display = 'none'; return; }
  warn.textContent = `Heads up: ${urlHost} differs from your current host (${here}). Make sure DNS for ${urlHost} points to this app and that you've registered the URI with the provider.`;
  warn.style.display = 'block';
}

function _applyProviderStatus(provider, configured, clientId, redirectUri, enabledScopes, suggestedUri) {
  const badge        = _qs(`ac-int-${provider}-badge`);
  const configuredEl = _qs(`ac-int-${provider}-configured`);
  const form         = _qs(`ac-int-${provider}-form`);
  // Populate the editable redirect-URI input with whatever the admin
  // saved, falling back to the request-derived suggestion as a placeholder
  // for unconfigured providers. Attach a one-time keystroke listener that
  // shows/hides the host-mismatch warning.
  const input = _qs(`ac-int-${provider}-input-redirect_uri`);
  if (input) {
    input.value = redirectUri || suggestedUri || '';
    if (!input.dataset.warnAttached) {
      input.addEventListener('input', () => _refreshRedirectUriWarning(provider));
      input.dataset.warnAttached = '1';
    }
    _refreshRedirectUriWarning(provider);
  }
  if (configured) {
    if (badge) { badge.textContent = 'Configured'; badge.className = 'ac-int-badge ac-int-badge-on'; }
    if (configuredEl) configuredEl.style.display = 'block';
    if (form) form.style.display = 'none';
    const cidEl = _qs(`ac-int-${provider}-cid`);
    if (cidEl) cidEl.textContent = clientId || '';
    const uriEl = _qs(`ac-int-${provider}-uri`);
    _attachUriCopy(uriEl, redirectUri || '');
    // Show scope count in configured summary
    let scopeCountEl = document.getElementById(`ac-int-${provider}-scope-count`);
    if (!scopeCountEl && configuredEl) {
      scopeCountEl = document.createElement('div');
      scopeCountEl.id = `ac-int-${provider}-scope-count`;
      scopeCountEl.style.cssText = 'font-size:12px;color:var(--fg-muted);margin-top:4px;';
      configuredEl.querySelector('div')?.appendChild(scopeCountEl);
    }
    if (scopeCountEl) {
      const total = (_PROVIDER_SCOPES[provider] || []).length;
      const active = enabledScopes ? enabledScopes.length : total;
      scopeCountEl.textContent = `${active} of ${total} scopes enabled`;
    }
  } else {
    if (badge) { badge.textContent = 'Not configured'; badge.className = 'ac-int-badge ac-int-badge-off'; }
    if (configuredEl) configuredEl.style.display = 'none';
    if (form) form.style.display = 'block';
  }
  _setScopeSelection(provider, enabledScopes || null);
  _syncRowToggle(provider);
}

function _editProviderConfig(provider) {
  const configuredEl = _qs(`ac-int-${provider}-configured`);
  const form         = _qs(`ac-int-${provider}-form`);
  const cidDisplay   = _qs(`ac-int-${provider}-cid`);
  const cidInput     = _qs(`ac-int-${provider}-input-cid`);
  const secInput     = _qs(`ac-int-${provider}-input-secret`);
  const statusEl     = _qs(`ac-int-${provider}-status`);

  // Pre-fill client ID from the displayed (masked) value so the user can see what's there
  if (cidInput && cidDisplay) cidInput.value = cidDisplay.textContent || '';
  // Clear the secret field so user must re-enter it
  if (secInput) secInput.value = '';
  // Clear any lingering status message
  if (statusEl) statusEl.style.display = 'none';

  // Swap panels: hide configured summary, show edit form
  if (configuredEl) configuredEl.style.display = 'none';
  if (form) form.style.display = 'block';
}

async function _saveProviderConfig(provider) {
  if (!isAdmin()) { showRestrictedModal(); return false; }
  const cidInput = _qs(`ac-int-${provider}-input-cid`);
  const secInput = _qs(`ac-int-${provider}-input-secret`);
  const uriInput = _qs(`ac-int-${provider}-input-redirect_uri`);
  const statusEl = _qs(`ac-int-${provider}-status`);
  // The overlay lives on this provider's Save button.
  const saveBtn  = _qs(`ac-int-${provider}-save`);
  if (!cidInput?.value?.trim() || !secInput?.value?.trim()) {
    _setIntStatus(statusEl, 'Both Client ID and Client Secret are required.');
    if (saveBtn) _flashSaveCheck(saveBtn, false, 'Missing fields');
    return false;
  }
  const selectedScopes = _getSelectedScopes(provider);
  const body = {
    client_id: cidInput.value.trim(),
    client_secret: secInput.value.trim(),
    scopes: selectedScopes,
  };
  const uriValue = (uriInput?.value || '').trim();
  if (uriValue) body.redirect_uri = uriValue;
  if (saveBtn) _markSaving(saveBtn);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/${provider}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      // Surface server-side validation errors (e.g. wrong path, http instead
      // of https) with the actual reason string from the backend.
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = String(data.detail);
      } catch {}
      throw new Error(detail);
    }
    cidInput.value = '';
    secInput.value = '';
    if (saveBtn) _flashSaveCheck(saveBtn, true);
    _loadIntegrations();
    _expandCard(provider);
    return true;
  } catch (e) {
    // Keep the detailed reason inline (the short overlay popup can't carry it).
    _setIntStatus(statusEl, `Error: ${e.message}`);
    if (saveBtn) _flashSaveCheck(saveBtn, false, e.message);
    return false;
  }
}

async function _unconfigureProvider(provider, ctrl) {
  if (!isAdmin()) { showRestrictedModal(); return false; }
  const statusEl = _qs(`ac-int-${provider}-status`);
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/${provider}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (ctrl) _flashSaveCheck(ctrl, true);
    _loadIntegrations();
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Error: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// Keep legacy aliases (referenced by any older inline calls)
async function _saveGoogleConfig()    { return _saveProviderConfig('google'); }
async function _unconfigureGoogle()   { return _unconfigureProvider('google'); }

// ── Automation Engine rows ─────────────────────────────────────────────────
// The Automation Engine group is one flush `.ac-list` of expandable `.ac-row`s
// (Scheduler · Event runtime · Event sources · Recent deliveries — see the
// AUTOMATION ENGINE block in agent-settings.html). Each row toggles `.expanded`
// on a head click (ignoring clicks on the controls inside its body), mirroring
// _wireBootRow() in app-settings.js. The engine's admin-only data is fetched
// LAZILY the first time ANY row is expanded — both halves load together, so the
// scheduler status, event runtime and sources/deliveries are all populated the
// moment the admin opens any one row.
let _autoEngineLoaded = false;
function _loadAutomationEngineOnce() {
  if (_autoEngineLoaded) return;
  _autoEngineLoaded = true;
  loadSchedulerPanel().catch(() => {});
  loadEventSourcesPanel().catch(() => {});
}

function _wireAutomationRows() {
  const list = document.getElementById('ac-automation-engine-list');
  if (!list) return;
  for (const row of list.querySelectorAll(':scope > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    head?.addEventListener('click', (e) => {
      // A click on a control inside the body must never collapse the row.
      if (e.target.closest('select, input, button, a, label, pre')) return;
      row.classList.toggle('expanded');
      if (row.classList.contains('expanded')) _loadAutomationEngineOnce();
    });
  }
}

// ── Templates and prompts rows ─────────────────────────────────────────────
// The "Templates and prompts" group (see agent-settings.html) is one `.ac-list`
// of expandable `.ac-row`s — the Global system prompt and the Agent Prompt
// Templates (moved here from Data Management). Same expand/collapse behaviour as
// _wireAutomationRows above; a click on a control inside a body never collapses
// the row. The row controls themselves bind by element id and are driven
// elsewhere (global-prompt textarea + Save → app-settings.js; the `ac-tpl-*`
// template controls → ui/shared/js/storage.js), so there is nothing to load
// lazily here — just the toggle.
function _wireTemplatesRows() {
  const list = document.getElementById('ac-templates-prompts-list');
  if (!list) return;
  for (const row of list.querySelectorAll(':scope > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    head?.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label, pre, textarea')) return;
      row.classList.toggle('expanded');
    });
  }
}

// ── Public API ───────────────────────────────────────────────────────────

export function init() {
  _initModelTable();
  // Wire the relocated app-global checkboxes AFTER the model table is mounted into
  // the document — _initModelTable()'s advancedExtra moves them in while the table
  // is still detached, so getElementById can't find them until now (the cause of
  // the AI-suggestions toggle never saving).
  _initSuggestionsCheckbox();
  _loadSuggestionsCheckbox();

  // ── Automation Engine: wire buttons now, load data lazily on first expand ──
  // Both halves (scheduler + event sources) hit admin-only endpoints, so defer
  // the fetch until the admin actually expands one of the engine's table rows
  // instead of firing on every Agent Settings visit. Each panel keeps a manual
  // Refresh button for re-fetching after that.
  initSchedulerPanel();
  initEventSourcesPanel();
  _wireAutomationRows();
  _wireTemplatesRows();

  // `_initIntegrations` is the async structural build (ability table +
  // integration relocation) that the `is-building` gate waits on. Drop the gate
  // the moment it settles — pass OR fail — so the finished tables reveal in one
  // step instead of flashing the raw markup. `.finally` guarantees we never get
  // stuck on the spinner if the build throws.
  //
  // init() owns the WHOLE reveal so it never depends on load() having run —
  // init() (gate up) fires when the Admin tab initialises, but load() only fires
  // when the App Config view is activated, so gating the reveal on load() could
  // leave the spinner stuck. Sequence: build the structure → load the first data
  // snapshot (so every toggle/badge is already in its real state) → lift the
  // gate. Revealing on the build alone would flash default toggle states for a
  // beat before the data lands — the "transient incorrect view". load() still
  // refreshes the data on each later activation; that's just an idempotent
  // re-fetch over the already-revealed tables.
  _lastAgentLoadAt = Date.now();
  _initIntegrations()
    .then(() => _loadIntegrations())
    .catch(() => {})
    .finally(() => {
      _clearBuildGate();
      // Measure the sticky section navigator only once the build gate is lifted —
      // while `is-building` the section is hidden and every heading measures 0.
      // mobileCarousel: on a phone this section shows a horizontal heading-chip
      // strip instead of the stacked sticky headings (see ../sticky-nav.js).
      initStickyNav('agent-settings', { mobileCarousel: true });
    });
  // Re-measure each time Agent Settings is shown (also resets the admin chat).
  registerSectionHook('agent-settings', () => {
    _intAdminResetSession();
    initStickyNav('agent-settings', { mobileCarousel: true });
  });
}

// Remove the first-paint build gate so the section's real content is shown.
// Cleared directly (no requestAnimationFrame): the structure and the first data
// snapshot are both already applied by the time this runs, so the browser lays
// out and paints the finished tables in one frame — there's no half-built frame
// to hide. (rAF would be wrong here anyway: it's paused while the page isn't the
// foreground tab, which would leave the spinner stuck.) Idempotent: safe to call
// again on every revisit (the class is already gone).
function _clearBuildGate() {
  document.getElementById('ac-section-agent-settings')?.classList.remove('is-building');
}

export function load() {
  // Skip the re-fetch when init() just kicked off the same three loads (first
  // open fires init() then load() in the same tick). Anything past a couple of
  // seconds is a real re-activation, so refresh normally.
  if (Date.now() - _lastAgentLoadAt < 2000) return;
  _lastAgentLoadAt = Date.now();
  _modelTable?.reload();
  _loadSuggestionsCheckbox();
  _loadIntegrations();
}

export function stop() {
  // Abort any pending admin chat request
  if (typeof _intAdminAbort !== 'undefined' && _intAdminAbort) {
    try { _intAdminAbort.abort(); } catch (_) {}
    _intAdminAbort = null;
  }
  _intAdminResetSession();
}

