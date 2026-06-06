'use strict';

/**
 * App Config tab — consolidated settings page.
 *
 * Sections:
 *   1. Models            — providers, models, per-model capabilities
 *                          (text / image-in / image-out)
 *   2. Integrations      — Google OAuth and third-party service connections
 *   3. Database          — cloud / local toggle, display settings
 *   4. Optimizer Stats   — session stats table, run mode, schedule
 *   5. Git Providers     — GitHub token, repo status quick-view
 *   6. App Settings      — global feature toggles (extend LLM to agents, etc.)
 *
 * Note: App Connections (webhooks, Telegram bot config) was removed — managed
 *       via each agent's Connections tab instead.
 *
 * ⚠ DROP-IN POLICY — do NOT hardcode a new ability, integration, or tool in this
 * file. Capabilities are discovered from their plugin folders (abilities:
 * plugins/abilities/; integrations: app/integrations/) and rendered generically
 * here (Agent Tools / Abilities render from GET /api/v1/abilities/catalog; the
 * Features report from GET /api/v1/features). Add a capability by dropping a file
 * in the matching plugin folder — never by editing this file. See CLAUDE.md
 * "Core vs. plugins".
 */

import { apiPath } from './config.js';
import { app } from './state.js';
import { isAdmin } from './left-login.js';
import { icon } from './icons.js';
import { loadSessionChat, populateSessionSelect } from './sessions.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { wireChatPillUploads } from './attachments.js';

// ── Auth-aware fetch ──────────────────────────────────────────────────────
function _fetch(url, opts = {}) {
  const token = localStorage.getItem('auth_token');
  if (token) {
    opts.headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
  }
  return fetch(url, opts);
}

function _qs(id) { return document.getElementById(id); }

function _esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str || ''));
  return d.innerHTML;
}

// ── Module state ──────────────────────────────────────────────────────────
let _initialized = false;
let _active = false;

// LLM state
let _allModels = [];
let _selectedModel = '';
let _providerPresets = {};
let _providerConfigs = {};
let _currentProvider = 'openrouter';
let _parallelProviders = [];
let _parallelUidCounter = 0;
let _modelFetchDebounce = null;
let _autosaveDebounce = null;
// Capabilities of the currently-configured model, detected from the model
// catalog (models.dev / OpenRouter). Drives the read-only badges on the Model
// row and what gets persisted on auto-save / Add — no hand-entered checkboxes.
let _selCaps = { text: true, image: false, imageOut: false };
// Per-model token/cost metadata cache for the Saved Models expand detail.
const _modelMetaCache = {};

// Integration Admin chat state
let _intAdminSessionId = null;
let _intAdminAbort = null;
let _intAdminWired = false;

// ─────────────────────────────────────────────────────────────────────────
// ── Sidebar nav + scroll highlighting ────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
const _SECTION_KEY = 'appConfig_activeSection';
const _VALID_SECTIONS = ['app-settings', 'agent-settings', 'user-management', 'database', 'optimizer', 'git', 'automation', 'events', 'monetization', 'features'];
	let _activeSection = localStorage.getItem(_SECTION_KEY) || 'agent-settings';

function _showSection(section) {
  _VALID_SECTIONS.forEach(id => {
    const el = _qs('ac-section-' + id);
    if (el) el.classList.toggle('active', id === section);
  });
  _activeSection = section;
  localStorage.setItem(_SECTION_KEY, section);
  _setNavActive(section);
  if (section === 'monetization') _renderPlatformBillingPanel();
  if (section === 'app-settings' && typeof window.__refreshRemoteAccess === 'function') window.__refreshRemoteAccess();
  if (section === 'agent-settings') _intAdminResetSession();
  if (section === 'features') _renderFeaturesCatalog();
}

// ─────────────────────────────────────────────────────────────────────────
// ── Features (discovery catalog) ──────────────────────────────────────────
// Read-only report of every capability the app discovered: its maturity, if
// it's true drop-in, and whether the active edition includes it. Backed by
// GET /api/v1/features. See docs/claude/production-editions.md. Phase 1 is
// report-only — nothing is gated here.
// ─────────────────────────────────────────────────────────────────────────
const _FEATURE_STATUS_STYLE = {
  stable:       { label: 'Stable',       color: 'var(--success)' },
  beta:         { label: 'Beta',         color: 'var(--warning)' },
  experimental: { label: 'Experimental', color: 'var(--danger)' },
  unknown:      { label: 'Unmarked',     color: 'var(--fg-4)' },
};
const _FEATURE_CAT_ICON = {
  integration: 'plug', event_source: 'radio', channel: 'message-square',
  connector: 'database', scheduler: 'clock', encryption: 'lock',
  payment: 'credit-card', secrets: 'key-round', storage: 'hard-drive',
  tool: 'wrench', ability: 'sparkles',
};
const _FEATURE_CAT_LABEL = {
  integration: 'Integrations', event_source: 'Event sources', channel: 'Communication channels',
  connector: 'Data connectors', scheduler: 'Scheduler providers', encryption: 'Encryption methods',
  payment: 'Payment processors', secrets: 'Secrets vaults', storage: 'Storage backends',
  tool: 'Tools', ability: 'Abilities',
};
const _FEATURE_CAT_ORDER = ['integration', 'event_source', 'channel', 'connector', 'scheduler', 'encryption', 'payment', 'secrets', 'storage', 'tool', 'ability'];

function _featEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function _featChip(label, n, color) {
  return `<span style="border:1px solid var(--border);border-radius:10px;padding:2px 9px;color:var(--fg-2);">` +
    `<span style="color:${color || 'var(--fg-3)'};font-weight:600;">${n == null ? 0 : n}</span> ` +
    `<span style="color:var(--fg-4);">${_featEsc(label)}</span></span>`;
}

function _featureRow(f) {
  const st = _FEATURE_STATUS_STYLE[f.status] || _FEATURE_STATUS_STYLE.unknown;
  const reqs = (f.requires && f.requires.length)
    ? ` <span style="color:var(--fg-4);">· needs ${_featEsc(f.requires.join(', '))}</span>` : '';
  const desc = (f.summary ? _featEsc(f.summary) : '<span style="color:var(--fg-4);">no description yet</span>') + reqs;
  const errTag = f.error
    ? ` <span style="color:var(--danger);font-size:10px;" title="${_featEsc(f.error)}">import error</span>` : '';
  const incIcon = f.included
    ? '<span title="included in the active edition" style="color:var(--success);font-size:13px;">&#9679;</span>'
    : `<span title="${_featEsc(f.reason || 'excluded')}" style="color:var(--fg-4);font-size:13px;">&#9675;</span>`;
  return `<div class="ac-ability-row"${f.included ? '' : ' style="opacity:.62;"'}>` +
    `<i data-lucide="${_FEATURE_CAT_ICON[f.category] || 'box'}" class="lucide-icon ac-ability-icon"></i>` +
    `<div class="ac-ability-label">` +
      `<div class="ac-ability-name">${_featEsc(f.display_name)}${errTag}</div>` +
      `<div class="ac-ability-desc">${desc}</div>` +
    `</div>` +
    `<div class="ac-ability-status" style="display:flex;align-items:center;gap:10px;">` +
      `<span style="font-size:10px;font-weight:600;color:${st.color};border:1px solid var(--border);border-radius:10px;padding:1px 8px;white-space:nowrap;">${st.label}</span>` +
      incIcon +
    `</div></div>`;
}

async function _renderFeaturesCatalog() {
  const banner = _qs('ac-features-banner');
  const list = _qs('ac-features-list');
  if (!banner || !list) return;
  if (typeof isAdmin === 'function' && !isAdmin()) {
    banner.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Platform admin access required.</div>';
    list.innerHTML = '';
    return;
  }
  banner.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Loading feature catalog…</div>';
  list.innerHTML = '';

  let data;
  try {
    const res = await _fetch(apiPath('/api/v1/features'));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    data = await res.json();
  } catch (e) {
    banner.innerHTML = `<div style="color:var(--danger);font-size:13px;">Could not load feature catalog: ${_featEsc(String(e))}</div>`;
    return;
  }

  const c = data.counts || {};
  const bs = c.by_status || {};
  const editions = Object.keys(data.editions || {}).map(_featEsc).join(' &middot; ');
  banner.innerHTML =
    `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">` +
      `<span style="font-size:13px;color:var(--fg-2);">Active edition</span>` +
      `<span style="font-weight:600;color:var(--accent);font-size:14px;">${_featEsc(data.active_edition)}</span>` +
      `<span style="margin-left:auto;font-size:11px;color:var(--fg-4);">Report only — nothing is gated yet</span>` +
    `</div>` +
    `<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;font-size:12px;">` +
      _featChip('Total', c.total) +
      _featChip('In this edition', c.included, 'var(--success)') +
      _featChip('Stable', bs.stable || 0, 'var(--success)') +
      _featChip('Beta', bs.beta || 0, 'var(--warning)') +
      _featChip('Experimental', bs.experimental || 0, 'var(--danger)') +
      _featChip('Unmarked', bs.unknown || 0, 'var(--fg-4)') +
      _featChip('Drop-in', c.drop_in) +
    `</div>` +
    `<div style="margin-top:8px;font-size:11px;color:var(--fg-4);">Editions defined: ${editions || '—'}. Set the active edition with the WEBAGENT_EDITION env var.</div>`;

  const feats = (data.features || []).slice();
  const groups = {};
  feats.forEach(f => { (groups[f.category] = groups[f.category] || []).push(f); });
  const cats = Object.keys(groups).sort((a, b) => {
    const ia = _FEATURE_CAT_ORDER.indexOf(a), ib = _FEATURE_CAT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });

  let html = '';
  cats.forEach(cat => {
    const items = groups[cat];
    const dropIn = items.length && items[0].drop_in;
    const tag = dropIn
      ? '<span style="font-size:10px;color:var(--success);border:1px solid var(--border);border-radius:10px;padding:1px 7px;" title="add/remove by dropping or deleting a file">drop-in</span>'
      : '<span style="font-size:10px;color:var(--fg-4);border:1px solid var(--border);border-radius:10px;padding:1px 7px;" title="adding/removing needs a central registry edit today">registry</span>';
    html +=
      `<div style="margin:16px 0 6px;display:flex;align-items:center;gap:8px;">` +
        `<i data-lucide="${_FEATURE_CAT_ICON[cat] || 'box'}" class="lucide-icon" style="width:15px;height:15px;color:var(--accent);"></i>` +
        `<span style="font-weight:600;font-size:13px;color:var(--fg-1);">${_featEsc(_FEATURE_CAT_LABEL[cat] || cat)}</span>` +
        `<span style="font-size:11px;color:var(--fg-4);">${items.length}</span>` +
        tag +
      `</div>` +
      `<div class="ac-list">${items.map(_featureRow).join('')}</div>`;
  });
  list.innerHTML = html;
}

function _renderPlatformBillingPanel() {
  const mount = _qs('billing-platform-panel');
  if (!mount) return;
  if (!isAdmin()) {
    mount.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Platform admin access required.</div>';
    return;
  }
  if (window.AppBilling && typeof window.AppBilling.renderMonetizationPanel === 'function') {
    window.AppBilling.renderMonetizationPanel('platform', mount);
  } else {
    mount.innerHTML = '<div style="color:var(--fg-3);font-size:13px;">Billing module is still loading…</div>';
    setTimeout(_renderPlatformBillingPanel, 400);
  }
}

function _initNav() {
  const tabBar   = _qs('app-config-tabs');
  const tabWrap  = _qs('app-config-tabs-wrap');
  const chevLeft = _qs('app-config-tabs-chev-left');
  const chevRight= _qs('app-config-tabs-chev-right');
  if (!tabBar || !tabWrap) return;

  tabBar.querySelectorAll('.ac-tab').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault();
      _showSection(btn.dataset.section);
      btn.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
    });
  });

  if (chevLeft && chevRight) {
    const updateChevrons = () => {
      const overflow = tabBar.scrollWidth - tabBar.clientWidth > 1;
      tabWrap.classList.toggle('has-overflow', overflow);
      chevLeft.classList.toggle('visible', overflow && tabBar.scrollLeft > 1);
      chevRight.classList.toggle('visible', overflow && tabBar.scrollLeft < tabBar.scrollWidth - tabBar.clientWidth - 1);
    };
    const scrollStep = () => Math.max(80, Math.floor(tabBar.clientWidth * 0.6));
    chevLeft.addEventListener('click', () => tabBar.scrollBy({ left: -scrollStep(), behavior: 'smooth' }));
    chevRight.addEventListener('click', () => tabBar.scrollBy({ left: scrollStep(), behavior: 'smooth' }));
    tabBar.addEventListener('scroll', updateChevrons, { passive: true });

    requestAnimationFrame(() => {
      updateChevrons();
      const active = tabBar.querySelector('.ac-tab.active');
      if (active) active.scrollIntoView({ inline: 'center', block: 'nearest' });
    });
    if (typeof ResizeObserver !== 'undefined') {
      let roPending = false;
      const ro = new ResizeObserver(() => {
        if (roPending) return;
        roPending = true;
        requestAnimationFrame(() => { roPending = false; updateChevrons(); });
      });
      ro.observe(tabBar);
      ro.observe(tabWrap);
    }
    window.addEventListener('resize', updateChevrons);
  }

  // "GitHub tab" links inside the page
  document.querySelectorAll('.ac-tab-link[data-tab]').forEach(el => {
    el.addEventListener('click', () => {
      const tabSel = _qs('main-tab-select');
      if (tabSel) {
        tabSel.value = el.dataset.tab;
        tabSel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  });
}

function _setNavActive(section) {
  const tabBar = _qs('app-config-tabs');
  if (!tabBar) return;
  let active = null;
  tabBar.querySelectorAll('.ac-tab').forEach(t => {
    const isActive = t.dataset.section === section;
    t.classList.toggle('active', isActive);
    if (isActive) active = t;
  });
  if (active) active.scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' });
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 1: Models ─────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
function _initLLM() {
  const saveBtn   = _qs('ac-settings-save');
  const clearBtn  = _qs('ac-settings-clear');
  const apiKeyEl  = _qs('ac-settings-api-key');
  const provEl    = _qs('ac-settings-provider');
  const modelSrch = _qs('ac-settings-model-search');

  if (!saveBtn) return;

  saveBtn.addEventListener('click', _saveLLM);
  clearBtn?.addEventListener('click', _clearLLM);

  const baseUrlEl = _qs('ac-settings-base-url');
  const scheduleFetch = () => {
    if (_modelFetchDebounce) clearTimeout(_modelFetchDebounce);
    _modelFetchDebounce = setTimeout(() => { _fetchModels(); }, 500);
  };
  // Each field persists to the DB on its own (no Save button); the matching
  // green check flashes once the backend confirms — same pattern as the agent
  // abilities table.
  const scheduleAutosave = (checkId) => {
    if (_autosaveDebounce) clearTimeout(_autosaveDebounce);
    _autosaveDebounce = setTimeout(() => { _autosaveLLM(_qs(checkId)); }, 700);
  };
  apiKeyEl?.addEventListener('input', () => { scheduleFetch(); scheduleAutosave('ac-cfg-check-key'); });
  baseUrlEl?.addEventListener('input', () => { scheduleFetch(); scheduleAutosave('ac-cfg-check-url'); });

  provEl?.addEventListener('change', () => {
    _saveCurrentProviderToMap(_currentProvider);
    _currentProvider = provEl.value;
    const saved = _providerConfigs[_currentProvider];
    const baseUrlEl = _qs('ac-settings-base-url');
    if (baseUrlEl) {
      baseUrlEl.value = (saved && saved.base_url) || (_providerPresets[_currentProvider]?.base_url) || '';
    }
    if (apiKeyEl) {
      apiKeyEl.value = '';
      apiKeyEl.placeholder = 'sk-...';
    }
    _selectedModel = (saved && saved.model) || '';
    if (modelSrch) modelSrch.value = _selectedModel;
    const modelStatus = _qs('ac-settings-model-status');
    if (modelStatus) {
      modelStatus.textContent = _selectedModel ? `Selected: ${_selectedModel}` : '';
      modelStatus.style.color = _selectedModel ? '#9ece6a' : '#565f89';
    }
    _renderDetectedCaps(null);
    _allModels = [];
    const dd = _qs('ac-settings-model-dropdown');
    if (dd) dd.style.display = 'none';
    _fetchModels();
    _autosaveLLM(_qs('ac-cfg-check-provider'));
  });

  modelSrch?.addEventListener('focus', () => _renderModelDropdown(modelSrch.value.toLowerCase()));
  modelSrch?.addEventListener('input', () => _renderModelDropdown(modelSrch.value.toLowerCase()));

  document.addEventListener('click', e => {
    if (!e.target.closest('#ac-settings-model-group')) {
      const dd = _qs('ac-settings-model-dropdown');
      if (dd) dd.style.display = 'none';
    }
  });
}

function _saveCurrentProviderToMap(key) {
  if (!key || key === '_custom') return;
  _providerConfigs[key] = {
    api_key:  (_qs('ac-settings-api-key')?.value || ''),
    model:    _selectedModel,
    base_url: (_qs('ac-settings-base-url')?.value?.trim() || ''),
  };
}

async function _loadLLM() {
  await _fetchProviderPresets();
  try {
    const res = await _fetch(apiPath('/admin/settings/provider'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _providerConfigs = data.providers || {};
    _currentProvider = data.provider || 'openrouter';

    const provEl = _qs('ac-settings-provider');
    if (provEl) {
      provEl.value = _providerPresets[_currentProvider] ? _currentProvider : '_custom';
      _currentProvider = provEl.value;
    }

    const baseUrlEl = _qs('ac-settings-base-url');
    if (baseUrlEl) baseUrlEl.value = data.base_url || '';

    const apiKeyEl = _qs('ac-settings-api-key');
    if (apiKeyEl) apiKeyEl.value = data.api_key || '';

    _selectedModel = data.model || '';
    const modelSrch = _qs('ac-settings-model-search');
    if (modelSrch) modelSrch.value = _selectedModel;
    const modelStatus = _qs('ac-settings-model-status');
    if (modelStatus) {
      if (_selectedModel) {
        modelStatus.textContent = `Selected: ${_selectedModel}`;
        modelStatus.style.color = '#9ece6a';
      } else {
        modelStatus.textContent = '';
      }
    }
    _selCaps = {
      text:    data.text_capable !== false,
      image:   !!data.image_capable,
      imageOut: !!data.image_out_capable,
    };
    _renderDetectedCaps(_selectedModel ? {
      text_capable: _selCaps.text, image_capable: _selCaps.image, image_out_capable: _selCaps.imageOut,
    } : null);
    _fetchModels();
  } catch (e) {
    console.warn('ac: failed to load LLM settings', e);
  }
  await _loadParallelProviders();
  _renderParallelRows();
}

async function _fetchProviderPresets() {
  try {
    const res = await _fetch(apiPath('/admin/settings/providers'));
    if (!res.ok) return;
    _providerPresets = await res.json();
    const sel = _qs('ac-settings-provider');
    if (!sel) return;
    sel.innerHTML = '';
    for (const [key, preset] of Object.entries(_providerPresets)) {
      const opt = document.createElement('option');
      opt.value = key;
      opt.textContent = preset.name;
      sel.appendChild(opt);
    }
    const custom = document.createElement('option');
    custom.value = '_custom';
    custom.textContent = 'Custom';
    sel.appendChild(custom);
  } catch (e) {
    console.warn('ac: failed to load provider presets', e);
  }
}

async function _fetchModels() {
  const statusEl = _qs('ac-settings-model-status');
  const provider = (_currentProvider === '_custom') ? '' : _currentProvider;
  const typedKey = _qs('ac-settings-api-key')?.value?.trim() || '';
  const typedBase = _qs('ac-settings-base-url')?.value?.trim()
    || _providerPresets[_currentProvider]?.base_url
    || '';

  if (!typedKey) {
    if (statusEl) {
      statusEl.textContent = 'Enter an API key to see available models.';
      statusEl.style.color = '#565f89';
    }
    _allModels = [];
    const dd = _qs('ac-settings-model-dropdown');
    if (dd) dd.style.display = 'none';
    return;
  }

  if (statusEl) { statusEl.textContent = 'Loading models…'; statusEl.style.color = '#565f89'; }
  const params = new URLSearchParams({ provider });
  if (typedKey && typedBase) {
    params.set('api_key', typedKey);
    params.set('base_url', typedBase);
  }
  try {
    const res = await _fetch(apiPath(`/admin/settings/models?${params.toString()}`));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) {
      if (statusEl) {
        statusEl.textContent = data.error === 'No API key configured'
          ? 'Enter an API key to see available models.'
          : `Error: ${data.error}`;
        statusEl.style.color = '#565f89';
      }
      _allModels = [];
      return;
    }
    _allModels = data.models || [];
    if (statusEl) {
      statusEl.textContent = _allModels.length
        ? `${_allModels.length} models available. Type to filter.`
        : 'No models available.';
      statusEl.style.color = '#565f89';
    }
    // Refresh the detected-capability badges for the already-selected model
    // from the freshly-fetched list (the catalog has the authoritative signal).
    if (_selectedModel) {
      const sel = _allModels.find(m => m.id === _selectedModel);
      if (sel) {
        _selCaps = {
          text: sel.text_capable !== false,
          image: !!sel.image_capable,
          imageOut: !!sel.image_out_capable,
        };
      }
      _renderDetectedCaps(sel || {
        text_capable: _selCaps.text, image_capable: _selCaps.image, image_out_capable: _selCaps.imageOut,
      });
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Failed to load models: ${e.message}`; statusEl.style.color = '#f7768e'; }
    _allModels = [];
  }
}

function _renderModelDropdown(filter) {
  const dd = _qs('ac-settings-model-dropdown');
  if (!dd) return;
  if (!_allModels.length) { dd.style.display = 'none'; return; }
  const filtered = filter
    ? _allModels.filter(m => m.id.toLowerCase().includes(filter) || m.name.toLowerCase().includes(filter))
    : _allModels;
  if (!filtered.length) { dd.style.display = 'none'; return; }
  dd.innerHTML = '';
  dd.style.display = 'block';
  filtered.slice(0, 200).forEach(m => {
    const item = document.createElement('div');
    item.className = 'ac-model-item';
    if (m.id === _selectedModel) item.style.background = 'rgba(125,207,255,0.12)';
    const ctx = _fmtContext(m.context);
    const price = _fmtPrice(m.cost_input, m.cost_output);
    const badges = [ctx, price].filter(Boolean)
      .map(t => `<span class="ac-model-badge">${_esc(t)}</span>`).join('');
    item.innerHTML = `<div class="ac-model-item-row"><span style="font-weight:500;">${_esc(m.id)}</span>`
      + `<span style="color:var(--fg-3,#565f89);font-size:11px;margin-left:6px;">${_esc(m.name)}</span></div>`
      + (badges ? `<div class="ac-model-item-badges">${badges}</div>` : '');
    item.addEventListener('click', () => {
      _selectedModel = m.id;
      const srch = _qs('ac-settings-model-search');
      if (srch) srch.value = m.id;
      dd.style.display = 'none';
      const statusEl = _qs('ac-settings-model-status');
      if (statusEl) { statusEl.textContent = `Selected: ${m.id}`; statusEl.style.color = '#9ece6a'; }
      _selCaps = {
        text: m.text_capable !== false,
        image: !!m.image_capable,
        imageOut: !!m.image_out_capable,
      };
      _renderDetectedCaps(m);
      _autosaveLLM(_qs('ac-cfg-check-model'));
    });
    dd.appendChild(item);
  });
}

// ── Model metadata formatting (context / cost / description) ─────────────────

function _fmtContext(ctx) {
  if (!ctx || typeof ctx !== 'number') return '';
  if (ctx >= 1_000_000) return `${(ctx / 1_000_000).toFixed(ctx % 1_000_000 ? 1 : 0)}M ctx`;
  if (ctx >= 1000) return `${Math.round(ctx / 1000)}K ctx`;
  return `${ctx} ctx`;
}

function _fmtPrice(inCost, outCost) {
  // Costs are USD per 1M tokens. Show "in / out" when known.
  const f = (v) => (v === 0 ? 'free' : (typeof v === 'number' ? `$${v}` : null));
  const a = f(inCost), b = f(outCost);
  if (a === 'free' && b === 'free') return 'free';
  if (a && b) return `${a}/${b} per 1M`;
  if (a) return `${a} in /1M`;
  if (b) return `${b} out /1M`;
  return '';
}

// Read-only capability badges for the configured model, detected from the
// catalog — NOT user-entered checkboxes. Shows all three states (on = capable).
function _renderDetectedCaps(m) {
  const el = _qs('ac-settings-detected-caps');
  if (!el) return;
  if (!m) { el.innerHTML = ''; return; }
  const caps = [
    ['Text',      m.text_capable !== false],
    ['Image-in',  !!m.image_capable],
    ['Image-out', !!m.image_out_capable],
  ];
  el.innerHTML = caps
    .map(([label, on]) => `<span class="ac-cap-badge ${on ? 'on' : 'off'}">${label}</span>`)
    .join('');
}

// ── Per-field auto-save indicator (mirrors agents.js _flashSaved) ────────────
function _markCheckSaving(el) {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '<span class="agents-spinner"></span>';
}
function _flashCheck(el, ok, errMsg = '') {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '';
  if (ok) {
    el.classList.add('saved');
    el.textContent = '✓';
    el.title = 'Saved';
    el._fadeT = setTimeout(() => { el.classList.remove('saved'); el.textContent = ''; }, 2200);
  } else {
    el.classList.add('error');
    el.textContent = '!';
    el.title = errMsg || 'Save failed';
    el._fadeT = setTimeout(() => { el.classList.remove('error'); el.textContent = ''; }, 4000);
  }
}

// Persist the current provider/url/key/model to the DB (the default-provider
// config) without the Add button. Skips quietly until an API key is present,
// since the config can't be saved without one. Flashes `checkEl` on success.
async function _autosaveLLM(checkEl) {
  if (!isAdmin()) return;
  const provider = _currentProvider === '_custom' ? 'custom' : _currentProvider;
  const baseUrl  = _qs('ac-settings-base-url')?.value?.trim() || '';
  const apiKey   = _qs('ac-settings-api-key')?.value?.trim() || '';
  if (!apiKey) return;                       // nothing persistable yet
  _saveCurrentProviderToMap(_currentProvider);
  const payload = {
    provider, base_url: baseUrl, api_key: apiKey, model: _selectedModel || '',
    providers: _providerConfigs,
    text_capable: _selCaps.text, image_capable: _selCaps.image,
    image_out_capable: _selCaps.imageOut, use_for_image_out: _selCaps.imageOut,
  };
  _markCheckSaving(checkEl);
  try {
    const res = await _fetch(apiPath('/admin/settings/provider'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashCheck(checkEl, true);
  } catch (e) {
    _flashCheck(checkEl, false, e.message);
  }
}

async function _saveLLM() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _currentProvider === '_custom' ? 'custom' : _currentProvider;
  const baseUrl  = _qs('ac-settings-base-url')?.value?.trim() || '';
  const apiKey   = _qs('ac-settings-api-key')?.value?.trim() || '';

  if (!baseUrl)    return _showLLMStatus('Please enter a Base URL', 'error');
  if (!apiKey)     return _showLLMStatus('Please enter an API Key', 'error');
  if (!_selectedModel) return _showLLMStatus('Please select a model', 'error');

  // Capabilities come from catalog detection (_selCaps), not hand-entered boxes.
  const capText  = _selCaps.text !== false;
  const capImage = !!_selCaps.image;
  const capImageOut = !!_selCaps.imageOut;
  const payload = { provider, base_url: baseUrl, api_key: apiKey, model: _selectedModel, providers: _providerConfigs, text_capable: capText, image_capable: capImage, image_out_capable: capImageOut, use_for_image_out: capImageOut };
  try {
    const res = await _fetch(apiPath('/admin/settings/provider'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const dupe = _parallelProviders.find(p =>
      p.provider === provider && p.model === _selectedModel && p.base_url === baseUrl
    );
    if (!dupe) {
      _parallelProviders.push({
        provider,
        base_url: baseUrl,
        api_key:  apiKey,
        model:    _selectedModel,
        enabled:  true,
        rating:   0,
        text_capable:  capText,
        image_capable: capImage,
        use_for_image: capImage,
        image_out_capable: capImageOut,
        use_for_image_out: capImageOut,
        _uid: ++_parallelUidCounter,
      });
    } else if (apiKey && dupe.api_key !== apiKey) {
      dupe.api_key = apiKey;
    }

    await _saveParallelProviders();
    _showLLMStatus(data.message || 'Saved', 'success');
    await _loadLLM();
    _renderParallelRows();
    // Refresh the chat footer's context/max indicator for the new model.
    app.refreshModelContext?.();
  } catch (e) {
    _showLLMStatus(`Error: ${e.message}`, 'error');
  }
}

async function _clearLLM() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  try {
    const res = await _fetch(apiPath('/admin/settings/provider/clear'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _showLLMStatus(data.message || 'Cleared', 'success');
    await _loadLLM();
  } catch (e) {
    _showLLMStatus(`Error: ${e.message}`, 'error');
  }
}

function _showLLMStatus(msg, type) {
  const el = _qs('ac-settings-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'error' ? '#f7768e' : '#9ece6a';
  el.style.display = 'inline';
  setTimeout(() => { el.style.display = 'none'; }, 4000);
}

// ── Parallel providers ──
async function _loadParallelProviders() {
  try {
    const res = await _fetch(apiPath('/admin/settings/multi-providers'));
    if (!res.ok) return;
    const data = await res.json();
    _parallelProviders = (data.providers || []).map(p => ({
      provider: p.provider || 'openrouter',
      base_url: p.base_url || '',
      api_key:  p.api_key  || '',
      model:    p.model    || '',
      enabled:  p.enabled  !== false,
      rating:   p.rating   || 0,
      text_capable:  p.text_capable  !== false,
      image_capable: !!p.image_capable,
      use_for_image: !!p.use_for_image,
      image_out_capable: !!p.image_out_capable,
      use_for_image_out: !!p.use_for_image_out,
      _uid: ++_parallelUidCounter,
    }));
  } catch (e) {
    _parallelProviders = [];
  }
}

async function _saveParallelProviders() {
  const payload = {
    parallel_mode: _parallelProviders.filter(p => p.enabled).length >= 2,
    providers: _parallelProviders.map(p => ({
      provider: p.provider === '_custom' ? 'custom' : p.provider,
      base_url: p.base_url,
      api_key:  p.api_key,
      model:    p.model,
      enabled:  p.enabled,
      rating:   p.rating || 0,
      text_capable:  p.text_capable !== false,
      image_capable: !!p.image_capable,
      use_for_image: !!p.use_for_image,
      image_out_capable: !!p.image_out_capable,
      use_for_image_out: !!p.use_for_image_out,
    })),
  };
  try {
    await _fetch(apiPath('/admin/settings/multi-providers'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) { console.warn('ac: failed to save parallel providers', e); }
}

// One capability column cell: a checkbox when the model has the capability,
// else a faint placeholder dot so the columns stay aligned. Clicks are kept off
// the row so toggling a use-mode never expands/collapses the row.
function _capCell(capable, checked, title, onChange) {
  const cell = document.createElement('span');
  cell.className = 'ac-saved-cap';
  cell.addEventListener('click', e => e.stopPropagation());
  if (!capable) {
    const dot = document.createElement('span');
    dot.className = 'ac-cap-dot';
    dot.textContent = '·';
    cell.appendChild(dot);
    return cell;
  }
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = checked;
  cb.title = title;
  cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;';
  cb.addEventListener('change', () => onChange(cb.checked));
  cell.appendChild(cb);
  return cell;
}

// Fill a saved-model row's expanded body with token usage + cost (lazy, cached).
async function _loadSavedModelDetail(p, body) {
  if (body.dataset.loaded === '1') return;
  const cacheKey = `${p.provider || ''}|${p.model || ''}`;
  let meta = _modelMetaCache[cacheKey];
  if (!meta) {
    try {
      const params = new URLSearchParams({ model: p.model || '' });
      const res = await _fetch(apiPath(`/admin/settings/model-info?${params.toString()}`));
      const data = res.ok ? await res.json() : null;
      meta = (data && data.info) || {};
    } catch (e) { meta = {}; }
    _modelMetaCache[cacheKey] = meta;
  }
  body.dataset.loaded = '1';
  const ctx = _fmtContext(meta.context);
  const out = (typeof meta.max_output === 'number')
    ? `${_fmtContext(meta.max_output).replace(' ctx', '')} out` : '';
  const price = _fmtPrice(meta.cost_input, meta.cost_output);
  const chips = [ctx, out, price].filter(Boolean)
    .map(t => `<span class="ac-model-badge">${_esc(t)}</span>`).join('');
  const caps = `<div class="ac-model-caps" style="margin-top:8px;">`
    + `<span class="ac-cap-badge ${p.text_capable !== false ? 'on' : 'off'}">Text</span>`
    + `<span class="ac-cap-badge ${p.image_capable ? 'on' : 'off'}">Image-in</span>`
    + `<span class="ac-cap-badge ${p.image_out_capable ? 'on' : 'off'}">Image-out</span></div>`;
  const desc = meta.description
    ? `<div class="ac-model-meta-desc" style="margin-top:8px;">${_esc(meta.description)}</div>` : '';
  body.innerHTML = (chips ? `<div class="ac-model-meta-chips">${chips}</div>` : '')
    + caps
    + desc
    + (chips ? '' : '<div class="ac-hint" style="margin:8px 0 0;">No token or cost data available for this model.</div>');
}

function _renderParallelRows() {
  const wrap = _qs('ac-settings-saved-wrap');
  const list = _qs('ac-settings-saved-list');
  const countEl = _qs('ac-settings-parallel-count');
  if (!list) return;

  if (!_parallelProviders.length) {
    if (wrap) wrap.style.display = 'none';
    return;
  }
  if (wrap) wrap.style.display = '';

  list.innerHTML = '';

  // ── Column header: Model · Text / In / Out ──
  const head = document.createElement('div');
  head.className = 'ac-ability-row ac-saved-head';
  const headName = document.createElement('span');
  headName.className = 'ac-ability-label';
  headName.innerHTML = '<span class="ac-saved-th">Model</span>';
  head.appendChild(headName);
  [
    ['Text', 'Use for text answers (joins the parallel race)'],
    ['In',   'Use to read attached images (vision)'],
    ['Out',  'Use to generate images'],
  ].forEach(([label, tip]) => {
    const cell = document.createElement('span');
    cell.className = 'ac-saved-cap';
    const th = document.createElement('span');
    th.className = 'ac-saved-th';
    th.textContent = label;
    th.title = tip;
    cell.appendChild(th);
    head.appendChild(cell);
  });
  const delSpacer = document.createElement('span'); delSpacer.className = 'ac-saved-del-spacer'; head.appendChild(delSpacer);
  const chevSpacer = document.createElement('span'); chevSpacer.className = 'ac-saved-chev-spacer'; head.appendChild(chevSpacer);
  list.appendChild(head);

  _parallelProviders.forEach(p => {
    const rowWrap = document.createElement('div');
    rowWrap.className = 'ac-row';

    const row = document.createElement('div');
    row.className = 'ac-ability-row';

    const label = document.createElement('span');
    label.className = 'ac-ability-label';
    label.innerHTML = `<span class="ac-ability-name ac-saved-model-name">${_esc(p.model || '—')}</span>`
      + `<span class="ac-ability-desc">${_esc(_providerPresets[p.provider]?.name || p.provider || 'custom')}</span>`;
    row.appendChild(label);

    // Text → enabled (every chat model is text-capable)
    row.appendChild(_capCell(
      p.text_capable !== false, p.enabled,
      'Use for text answers (joins the parallel race)',
      v => { p.enabled = v; _saveParallelProviders().then(_renderParallelRows); },
    ));
    // Image-in → use_for_image (box only when the model can see images)
    row.appendChild(_capCell(
      !!p.image_capable, !!p.use_for_image,
      'Use to read attached images',
      v => { p.use_for_image = v; _saveParallelProviders().then(_renderParallelRows); },
    ));
    // Image-out → use_for_image_out (box only when the model can make images)
    row.appendChild(_capCell(
      !!p.image_out_capable, !!p.use_for_image_out,
      'Use to generate images',
      v => { p.use_for_image_out = v; _saveParallelProviders().then(_renderParallelRows); },
    ));

    const removeBtn = document.createElement('button');
    removeBtn.textContent = '×';
    removeBtn.title = 'Remove this model';
    removeBtn.className = 'ac-prov-del';
    removeBtn.addEventListener('click', e => {
      e.stopPropagation();
      _parallelProviders = _parallelProviders.filter(x => x._uid !== p._uid);
      _saveParallelProviders().then(_renderParallelRows);
    });
    row.appendChild(removeBtn);

    const chev = document.createElement('span');
    chev.className = 'ac-row-chevron';
    chev.innerHTML = '<i data-lucide="chevron-right" style="width:15px;height:15px;"></i>';
    row.appendChild(chev);

    // Expanded body — token usage + cost, lazily loaded on first open.
    const body = document.createElement('div');
    body.className = 'ac-ability-body';
    body.innerHTML = '<div class="ac-hint" style="margin:0;">Loading details…</div>';

    row.addEventListener('click', () => {
      const open = rowWrap.classList.toggle('expanded');
      if (open) _loadSavedModelDetail(p, body);
    });

    rowWrap.appendChild(row);
    rowWrap.appendChild(body);
    list.appendChild(rowWrap);
  });

  const enabledCount = _parallelProviders.filter(p => p.enabled).length;
  if (countEl) {
    countEl.textContent = `${enabledCount} ticked for Text (need 2+ to race in parallel)`;
    countEl.style.color = enabledCount < 2 ? 'var(--warning)' : 'var(--fg-3)';
  }

  const describerHint = _qs('ac-settings-describer-hint');
  if (describerHint) {
    const describer = _parallelProviders.find(p => p.image_capable && p.use_for_image);
    describerHint.textContent = describer
      ? `Image describer: ${describer.model}`
      : 'No Image-in model ticked — text-only models won\'t see attached images.';
  }

  const imageGenHint = _qs('ac-settings-imagegen-hint');
  if (imageGenHint) {
    const gen = _parallelProviders.find(p => p.image_out_capable && p.use_for_image_out);
    imageGenHint.textContent = gen
      ? `Image generator: ${gen.model}`
      : 'No Image-out model ticked — agents can\'t create images.';
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
// These three objects + _CREDENTIAL_MEMBERS are OVERWRITTEN at runtime by the
// fetched ability catalog (GET /api/v1/abilities/catalog → _ensureAbilityCatalog).
// Do NOT add a new ability here — drop a file in plugins/abilities/ and it
// appears automatically. The literals below are only used if that fetch fails
// (offline / endpoint error). See app/abilities/__init__.py.
let _ABILITY_META = {
  codebase_admin:   { icon: 'folder-cog',   color: '#bb9af7', simple: true,  desc: 'Lets the agent read, write, edit, delete files, and run shell commands on the host. No credentials.' },
  git_control:      { icon: 'git-branch',   color: '#9ece6a', simple: true,  desc: 'Version control only — status, diff, commit, push, branch, pull — without shell access. On by default.' },
  ui_admin:         { icon: 'paintbrush',   color: '#7dcfff', simple: true,  desc: 'Edits only front-end files (ui/ — CSS & HTML); never backend code or the shell. On by default.' },
  create_tools:     { icon: 'wrench',       color: '#7dcfff', simple: true,  desc: 'Lets the agent define new tools in the database. No credentials.' },
  web_access:       { icon: 'globe',        color: '#7aa2f7', simple: true,  desc: 'Lets the agent search the web, look up weather, and geocode addresses. No API keys needed.' },
  browser_control:  { icon: 'mouse-pointer-2', color: '#9ece6a', simple: true, desc: 'Lets the agent drive a headless Chromium browser and make HTTP requests. No credentials.' },
  terminal_control: { icon: 'terminal',     color: '#f7768e', simple: true,  desc: 'Lets the agent open and drive interactive terminal programs (REPLs, CLIs) — effectively shell access, so grant per agent with care.' },
  image_generation: { icon: 'image',        color: '#bb9af7', simple: false, desc: 'Lets the agent generate images from text prompts. Requires a provider + model config.' },
  automation:       { icon: 'clock',        color: '#e0af68', simple: false, desc: 'Scheduled tasks and event-triggered jobs that can call integrations.' },
  visualizer:       { icon: 'layout',       color: '#7aa2f7', simple: false, desc: 'Lets the agent create, edit, rename, and delete pages in the Pages workspace.' },
  agent_orchestration: { icon: 'share-2',   color: '#7dcfff', simple: true,  desc: 'Lets the agent delegate the session to other agents and run the prompt optimizer. On by default; switch off to remove it platform-wide.' },
  diagnostics:      { icon: 'activity',     color: '#e0af68', simple: true,  desc: 'Lets the agent read the in-app flight recorder to diagnose the running app. On by default; switch off to remove it platform-wide.' },
  agent_management: { icon: 'users',        color: '#9ece6a', simple: true,  desc: 'Lets the agent list, create, and update the user\'s own agents and edit their prompts and abilities. On by default; switch off to remove it platform-wide.' },
  app_control:      { icon: 'app-window',   color: '#73daca', simple: true,  desc: 'Lets the agent change what you\'re looking at — switch the main view (Browser, Pages, Agents…), show or hide the chat panel, and resize it. On by default; switch off to remove it platform-wide.' },
  wiki_control:     { icon: 'book-open',     color: '#7dcfff', simple: true,  desc: 'Lets the agent read, search, create, edit, and delete entries in the company-wide Wiki. Writes change shared data everyone sees, so grant per agent with care.' },
};

// Human labels for every ability key (shared by the row builder and search).
// Fallback only — overwritten by the fetched ability catalog (see above).
let _ABILITY_NAMES = {
  codebase_admin: 'Codebase Admin',
  git_control: 'Git Control',
  ui_admin: 'UI Admin',
  create_tools: 'Create Tools',
  web_access: 'Web Access',
  browser_control: 'Browser Control',
  terminal_control: 'Terminal Control',
  image_generation: 'Image Generation',
  automation: 'Automation',
  visualizer: 'Visualizer',
  agent_orchestration: 'Agent Orchestration',
  diagnostics: 'Diagnostics',
  agent_management: 'Agent Management',
  app_control: 'App Control',
  wiki_control: 'Wiki Control',
};

// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE  —  KEEP THE TWO IN SYNC                ║
// ║  This is one of the TWO ability tables that must stay MIRRORED IN DESIGN: ║
// ║    • here — the admin **Agent Settings** panel (`_ABILITY_GROUPS` +       ║
// ║      `_INTEGRATION_GROUPS` / `_initAbilitiesCompact`), and                ║
// ║    • the per-agent **Abilities tab** in agents.js (`_AGENT_ABILITY_GROUPS`║
// ║      / `_buildAbilityGroupsGrid`).                                        ║
// ║  Both render the shared `.ac-list` / `.ac-row` / `.ac-tri` component and  ║
// ║  read as the SAME table. If you change the look, structure, grouping, or  ║
// ║  toggle behaviour on EITHER side, apply the matching change on the other  ║
// ║  (grep `SISTER-PANEL: AGENT-ABILITY-TABLE`). See docs/claude/ui-guidance. ║
// ╚══════════════════════════════════════════════════════════════════════════╝
//
// ── Agent Tools grouping ───────────────────────────────────────────────────
// The 13 host abilities are presented in three alphabetical groups, each an
// expandable header row carrying a 3-position toggle (Off · Mixed · On). The
// grouping is purely a UI convenience — there is NO group concept on the
// backend; turning a group On/Off just flips each member with the same per-
// ability save the individual rows use.
//
// Two Web members — `scraper` (Web Scraper) and `browser_session` (Browser
// Cookies) — are credential providers, not simple toggles. They keep their own
// config cards (relocated into the Web group body by _placeWebCredentialRows)
// and are NOT part of a group's On/Off math (they can't be "on" without
// credentials); they're managed by their own row. Members are alphabetical by
// display name; groups are alphabetical (Administrator, Basic, Web).
let _CREDENTIAL_MEMBERS = new Set(['scraper', 'browser_session']);  // fallback; overwritten by catalog
let _ABILITY_GROUPS = [
  {
    id: 'administrator', name: 'Administrator', icon: 'shield-alert', color: '#f7768e',
    desc: 'Full host control — files & shell, version control, terminals, and diagnostics. Grant with care.',
    members: ['codebase_admin', 'diagnostics', 'git_control', 'terminal_control'],
  },
  {
    id: 'basic', name: 'Core', icon: 'wrench', color: '#7dcfff',
    desc: 'Everyday, non-destructive capabilities — UI edits, tool creation, automation, pages, image generation, orchestration, agent management, and app view control.',
    members: ['agent_management', 'agent_orchestration', 'app_control', 'automation', 'create_tools', 'image_generation', 'ui_admin', 'visualizer'],
  },
  {
    id: 'productivity', name: 'Productivity', icon: 'briefcase', color: '#4285F4',
    desc: 'Read, search, create, edit, and delete entries in the company-wide Wiki.',
    members: ['wiki_control'],
  },
  {
    id: 'web', name: 'Web', icon: 'globe', color: '#7aa2f7',
    desc: 'Reach the open web — search, a headless browser, third-party scraping, and authenticated cookie sessions.',
    members: ['browser_control', 'browser_session', 'web_access', 'scraper'],
  },
];

// Fetch the server-built ability catalog ONCE and overwrite the fallback render
// data above. This is what makes abilities drop-in: a file added to
// plugins/abilities/ shows up here with no JS edit. Resolves quietly (keeps the
// fallback) on any error. Must be awaited before the ability panel renders.
let _abilityCatalogPromise = null;
function _ensureAbilityCatalog() {
  if (_abilityCatalogPromise) return _abilityCatalogPromise;
  _abilityCatalogPromise = (async () => {
    try {
      const res = await fetch('/api/v1/abilities/catalog');
      if (!res.ok) return;
      const cat = await res.json();
      if (!cat || !Array.isArray(cat.groups) || !cat.groups.length) return; // keep fallback
      const meta = {}, names = {};
      for (const [id, a] of Object.entries(cat.abilities || {})) {
        meta[id] = { icon: a.icon, color: a.color, simple: !!a.simple, desc: a.description || '' };
        names[id] = a.display_name || id;
      }
      _ABILITY_META = meta;
      _ABILITY_NAMES = names;
      _ABILITY_GROUPS = cat.groups.map(g => ({
        id: g.id, name: g.name, icon: g.icon, color: g.color, desc: g.desc,
        members: (g.members || []).slice(),
      }));
      _CREDENTIAL_MEMBERS = new Set(cat.credential_members || []);
    } catch (e) {
      console.warn('Ability catalog fetch failed; using built-in fallback', e);
    }
  })();
  return _abilityCatalogPromise;
}

let _abilityStates = {}; // { [ability]: boolean }

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

// Build one always-visible ability row (icon + name + desc + toggle). Used as a
// member row inside a group body. The change handler matches the legacy flat
// list: simple abilities toggle directly; complex ones (need a config panel)
// expand their card and warn until configured.
function _buildAbilityRow(id, meta) {
  const row = document.createElement('div');
  row.className = 'ac-ability-row';
  row.id = `ac-ability-row-${id}`;
  row.dataset.ability = id;

  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  iconWrap.style.color = meta.color;
  iconWrap.innerHTML = `<i data-lucide="${meta.icon}" style="width:18px;height:18px;"></i>`;

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = _ABILITY_NAMES[id] || id;
  label.querySelector('.ac-ability-desc').textContent = meta.desc;

  const toggleWrap = document.createElement('label');
  toggleWrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  toggleWrap.title = 'Enable';
  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.className = 'conn-toggle ac-ability-toggle';
  toggle.id = `ac-ability-toggle-${id}`;
  toggle.dataset.ability = id;
  const track = document.createElement('span');
  track.className = 'conn-toggle-track';
  toggleWrap.appendChild(toggle);
  toggleWrap.appendChild(track);

  const statusEl = document.createElement('span');
  statusEl.className = 'ac-status ac-ability-status';
  statusEl.id = `ac-ability-status-${id}`;
  statusEl.style.display = 'none';

  row.appendChild(iconWrap);
  row.appendChild(label);
  row.appendChild(statusEl);
  row.appendChild(toggleWrap);

  toggle.addEventListener('change', () => {
    const enabled = toggle.checked;
    _markAbilityAction(id);   // protect this toggle from a stale in-flight load
    if (meta.simple) {
      _toggleAbility(id, enabled, row);
    } else {
      if (enabled && !_abilityStates[id]) {
        toggle.checked = false;
        _showAbilityConfigWarning(id, row);
        _expandAbilityConfig(id);
      } else if (enabled && _abilityStates[id]) {
        _toggleAbility(id, true, row);
      } else {
        _toggleAbility(id, false, row);
      }
    }
  });

  return row;
}

function _initAbilitiesCompact() {
  const container = _qs('ac-abilities-compact');
  if (!container) return;
  container.innerHTML = '';

  for (const group of _ABILITY_GROUPS) {
    const groupEl = document.createElement('div');
    groupEl.className = 'ac-row ac-group';
    groupEl.id = `ac-group-${group.id}`;

    // ── Group head row (icon + name + 3-position toggle + chevron) ──
    const head = document.createElement('div');
    head.className = 'ac-ability-row ac-group-head';

    const iconWrap = document.createElement('div');
    iconWrap.className = 'ac-ability-icon';
    iconWrap.style.color = group.color;
    iconWrap.innerHTML = `<i data-lucide="${group.icon}" style="width:18px;height:18px;"></i>`;

    const label = document.createElement('div');
    label.className = 'ac-ability-label';
    label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
    label.querySelector('.ac-ability-name').textContent = group.name;
    label.querySelector('.ac-ability-desc').textContent = group.desc;

    const tri = document.createElement('span');
    tri.className = 'ac-tri';
    tri.id = `ac-group-tri-${group.id}`;
    tri.dataset.state = 'off';
    tri.setAttribute('role', 'button');
    tri.setAttribute('tabindex', '0');
    tri.title = 'Off · Mixed · On — click the left half for all-off, the right half for all-on';
    tri.innerHTML = `<span class="ac-tri-knob"></span>`;

    const chevron = document.createElement('span');
    chevron.className = 'ac-row-chevron';
    chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;

    head.appendChild(iconWrap);
    head.appendChild(label);
    head.appendChild(tri);
    head.appendChild(chevron);

    // ── Group body — member rows (credential members get a slot to fill) ──
    const bodyEl = document.createElement('div');
    bodyEl.className = 'ac-group-body';
    bodyEl.id = `ac-group-body-${group.id}`;
    for (const member of group.members) {
      if (_CREDENTIAL_MEMBERS.has(member)) {
        const slot = document.createElement('div');
        slot.id = `ac-group-slot-${member}`;
        bodyEl.appendChild(slot);
      } else {
        bodyEl.appendChild(_buildAbilityRow(member, _ABILITY_META[member]));
      }
    }

    groupEl.appendChild(head);
    groupEl.appendChild(bodyEl);
    container.appendChild(groupEl);

    // Expand / collapse on head click — but not when the click lands on the tri.
    head.addEventListener('click', (e) => {
      if (e.target.closest('.ac-tri')) return;
      groupEl.classList.toggle('expanded');
    });

    // 3-position toggle: left half → all off, right half → all on. The middle
    // "mixed" detent is derived (set by _syncGroupToggle), not clicked into.
    tri.addEventListener('click', (e) => {
      e.stopPropagation();
      const rect = tri.getBoundingClientRect();
      _setGroupAll(group, (e.clientX - rect.left) > rect.width / 2);
    });
    tri.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        _setGroupAll(group, tri.dataset.state !== 'on');
      }
    });
  }

  // Append the former settings "categories" as expandable groups in the SAME
  // table, after the host-ability groups. Their cards are relocated in later.
  _buildIntegrationGroupShells(container);

  // Wire up save/unconfigure buttons for complex ability config panels
  const complexAbilities = ['image_generation', 'automation', 'visualizer'];
  for (const a of complexAbilities) {
    _qs(`ac-int-${a}-save`)?.addEventListener('click', () => _enableAbility(a));
    _qs(`ac-int-${a}-unconfigure`)?.addEventListener('click', () => _disableAbility(a));
  }

  _syncAllGroupToggles();

  // Render Lucide icons
  if (typeof lucide !== 'undefined' && lucide.createIcons) {
    lucide.createIcons();
  }
}

// Flip every (non-credential) member of a group to `goOn`. Credential members
// (Web Scraper, Browser Cookies) are skipped — they need their own credentials
// and are managed from their own row.
function _setGroupAll(group, goOn) {
  for (const m of group.members) {
    if (_CREDENTIAL_MEMBERS.has(m)) continue;
    if (!!_abilityStates[m] === goOn) continue;
    _markAbilityAction(m);
    const toggle = _qs(`ac-ability-toggle-${m}`);
    if (toggle) toggle.checked = goOn;   // optimistic; _toggleAbility reverts on error
    _toggleAbility(m, goOn, _qs(`ac-ability-row-${m}`));
  }
  _syncGroupToggle(group.id);
}

// Reflect a group's 3-position toggle from its members' states.
function _syncGroupToggle(groupId) {
  const group = _ABILITY_GROUPS.find(g => g.id === groupId);
  const tri = _qs(`ac-group-tri-${groupId}`);
  if (!group || !tri) return;
  const togglable = group.members.filter(m => !_CREDENTIAL_MEMBERS.has(m));
  const onCount = togglable.filter(m => !!_abilityStates[m]).length;
  tri.dataset.state = onCount === 0 ? 'off'
    : (onCount === togglable.length ? 'on' : 'mixed');
}

function _syncAllGroupToggles() {
  for (const g of _ABILITY_GROUPS) _syncGroupToggle(g.id);
}

function _syncGroupOfAbility(ability) {
  const g = _ABILITY_GROUPS.find(gr => gr.members.includes(ability));
  if (g) _syncGroupToggle(g.id);
}

// Move the compactified credential cards (Web Scraper, Browser Cookies) into
// the Web group body so they sit with the other Web rows instead of forming a
// separate list below. Runs after _compactifyAllIntegrations turns them into
// `.ac-card-compact` rows; mirrors _placeIntegrationGroupCards, which does the
// same relocation for the channel / OAuth groups.
function _placeWebCredentialRows() {
  for (const id of ['scraper', 'browser_session']) {
    const slot = document.getElementById(`ac-group-slot-${id}`);
    const card = document.getElementById(`ac-int-${id}-card`);
    if (slot && card) slot.replaceWith(card);
  }
  // The standalone "Non-OAuth web tools" intro is now redundant (its cards moved
  // into the Web group), so hide it.
  const intro = document.getElementById('ac-web-tools-intro');
  if (intro) intro.style.display = 'none';
}

function _showAbilityConfigWarning(ability, row) {
  const statusEl = _qs(`ac-ability-status-${ability}`);
  if (!statusEl) return;
  statusEl.textContent = '⚠ Configure details below first';
  statusEl.style.color = '#e0af68';
  statusEl.style.display = 'inline';
  setTimeout(() => { statusEl.style.display = 'none'; }, 4000);
}

function _expandAbilityConfig(ability) {
  // Find the existing card for this ability and expand it
  const card = _qs(`ac-int-${ability}-card`);
  if (card) {
    card.style.display = 'block';
    // Init collapsible if not already done
    if (!card.dataset.collapsibleInited) {
      _initCollapsible(ability);
      card.dataset.collapsibleInited = '1';
    }
    _expandCard(ability);
    card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

async function _toggleAbility(ability, enabled, row) {
  const statusEl = _qs(`ac-ability-status-${ability}`);
  if (statusEl) statusEl.style.display = 'none';
  try {
    if (enabled) {
      const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } else {
      if (ability === 'automation') {
        const ok = confirm(
          'Disable Automation?\n\n'
          + 'This will permanently delete every agent\'s scheduled tasks and event subscriptions. '
          + 'This cannot be undone.'
        );
        if (!ok) {
          // Revert toggle
          const toggle = _qs(`ac-ability-toggle-${ability}`);
          if (toggle) toggle.checked = true;
          return;
        }
      }
      const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    }
    _markAbilityAction(ability);   // re-stamp: the write has now committed
    _abilityStates[ability] = enabled;
    _applyAbilityRowStatus(ability, enabled);
    if (statusEl) {
      statusEl.textContent = enabled ? '✓ Enabled' : '✓ Disabled';
      statusEl.style.color = '#9ece6a';
      statusEl.style.display = 'inline';
      setTimeout(() => { statusEl.style.display = 'none'; }, 2000);
    }
  } catch (e) {
    // Revert toggle
    const toggle = _qs(`ac-ability-toggle-${ability}`);
    if (toggle) toggle.checked = !enabled;
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'inline';
    }
  }
}

function _applyAbilityRowStatus(ability, enabled) {
  _abilityStates[ability] = enabled;
  const toggle = _qs(`ac-ability-toggle-${ability}`);
  if (toggle) toggle.checked = !!enabled;
  const row = _qs(`ac-ability-row-${ability}`);
  if (row) row.classList.toggle('ac-ability-enabled', !!enabled);
  _syncGroupOfAbility(ability);
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
const _GENERIC_PROVIDER_IDS = ['scraper', 'browser_session'];
const _CHANNEL_IDS = ['telegram'];
const _SOON_CHANNEL_IDS = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

// One-to-1.5-sentence requirement note per row (cost / OAuth / special setup).
const _ROW_NOTES = {
  google:    `Free — create a Google Cloud OAuth app. Grants Gmail, Drive, Docs, and Calendar.`,
  microsoft: `Free — register an Azure app. Grants Outlook, OneDrive, and Calendar.`,
  yahoo:     `Free OAuth — Yahoo exposes account identity only (no public Mail API).`,
  dropbox:   `Free — a Dropbox app for file read/write and sharing.`,
  meta:      `Free OAuth — a Meta app with App Review is required to publish to Facebook/Instagram.`,
  twitter:   `Paid — X's API needs a paid tier (Basic ≈ $200/mo) for most posting/reading; the free tier is tiny.`,
  linkedin:  `Free OAuth — posting requires LinkedIn's Community/Marketing API approval.`,
  tiktok:    `Free OAuth, but the app must pass TikTok's review before going live.`,
  pinterest: `Free OAuth — a Pinterest app (trial access, then standard approval).`,
  reddit:    `Free — register a Reddit app for reading and posting.`,
  snapchat:  `Free OAuth — Snap Kit exposes identity and Bitmoji only.`,
  twitch:    `Free — register a Twitch app for channel, chat, and clips.`,
  ebay:      `Free API — an eBay developer account; the production keyset needs approval.`,
  etsy:      `Free API — an Etsy app, reviewed before production use.`,
  shopify:   `Free API — a Shopify Partner app; installs per store.`,
  amazon:    `Requires a Professional Seller plan ($39.99/mo) plus SP-API developer registration.`,
  scraper:   `Bring-your-own scraping provider (Apify, RapidAPI, or custom HTTP) — most bill per request.`,
  browser_session: `Free — paste cookies so the agent can act as you on sites with no API. Per-user.`,
  telegram:  `Free — create a bot with @BotFather; each agent supplies its own token.`,
  whatsapp:  `Paid — per-conversation fees via Meta WhatsApp Business or Twilio.`,
  slack:     `Free OAuth app — Slack is free for basic workspaces.`,
  discord:   `Free — create a Discord bot/app.`,
  email:     `Free with your own SMTP/IMAP or an email provider.`,
  twilio:    `Paid — per-message / per-minute Twilio fees.`,
};

// Coming-soon integrations that have no config card — rendered as greyed rows
// into the per-category containers added in admin-configuration.html.
const _COMING_SOON_ROWS = {
  'ac-rows-productivity': [
    { name: 'Notion',        icon: 'file-text', note: `Free OAuth — connect Notion pages and databases.` },
    { name: 'Airtable',      icon: 'table',     note: `Free tier (OAuth or token); paid plans for larger bases.` },
    { name: 'Google Sheets', icon: 'sheet',     note: `Free OAuth — part of Google Cloud; read/write spreadsheets.` },
  ],
  'ac-rows-developer': [
    { name: 'GitHub',        icon: 'github',        note: `Free — OAuth app or GitHub App for repos, issues, and PRs.` },
    { name: 'GitLab',        icon: 'git-branch',    note: `Free OAuth — repos, issues, and pipelines.` },
    { name: 'Jira / Linear', icon: 'kanban-square', note: `Free OAuth — issues and projects.` },
  ],
  'ac-rows-crm': [
    { name: 'HubSpot',    icon: 'contact', note: `Free OAuth app — HubSpot has free and paid tiers.` },
    { name: 'Salesforce', icon: 'cloud',   note: `OAuth — requires a paid Salesforce org.` },
    { name: 'Mailchimp',  icon: 'mail',    note: `Free OAuth — Mailchimp has free and paid tiers.` },
  ],
  'ac-rows-payments': [
    { name: 'Stripe', icon: 'credit-card', note: `OAuth (Connect) — free to integrate; Stripe takes a per-transaction fee.` },
    { name: 'PayPal', icon: 'wallet',      note: `OAuth — free to integrate; per-transaction fees apply.` },
    { name: 'Square', icon: 'square',      note: `OAuth — free to integrate; per-transaction fees apply.` },
    { name: 'Bank Accounts', icon: 'landmark', note: `Via an aggregator (e.g. Plaid) — usage-based pricing.` },
  ],
};

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
const _INTEGRATION_GROUPS = [
  {
    id: 'communication', name: 'Communication', icon: 'message-square', color: '#26A5E4',
    desc: 'How agents send and receive messages. Enable a channel to make it available to your agents.',
    members: [{ id: 'telegram', kind: 'channel' }],
    soonCards: ['whatsapp', 'slack', 'discord', 'email', 'twilio'],
    soonRowsKey: null,
  },
  {
    id: 'productivity', name: 'Productivity', icon: 'briefcase', color: '#4285F4',
    desc: 'App-level credentials for mail, calendar and file providers. Users connect their personal accounts from the agent’s Abilities tab.',
    members: [{ id: 'google', kind: 'oauth' }, { id: 'microsoft', kind: 'oauth' }, { id: 'yahoo', kind: 'oauth' }, { id: 'dropbox', kind: 'oauth' }],
    soonCards: [],
    soonRowsKey: 'ac-rows-productivity',
  },
  {
    id: 'social', name: 'Social Media', icon: 'share-2', color: '#bb9af7',
    desc: 'App-level credentials for social platform OAuth. Users connect their personal accounts from the agent’s Abilities tab.',
    members: [{ id: 'meta', kind: 'oauth' }, { id: 'twitter', kind: 'oauth' }, { id: 'linkedin', kind: 'oauth' }, { id: 'tiktok', kind: 'oauth' }, { id: 'pinterest', kind: 'oauth' }, { id: 'reddit', kind: 'oauth' }, { id: 'snapchat', kind: 'oauth' }, { id: 'twitch', kind: 'oauth' }],
    soonCards: [],
    soonRowsKey: null,
  },
  {
    id: 'marketplace', name: 'Marketplaces', icon: 'shopping-bag', color: '#e0af68',
    desc: 'App-level credentials for e-commerce platform OAuth. Sellers connect their accounts from the agent’s Abilities tab.',
    members: [{ id: 'ebay', kind: 'oauth' }, { id: 'etsy', kind: 'oauth' }, { id: 'shopify', kind: 'oauth' }, { id: 'amazon', kind: 'oauth' }],
    soonCards: [],
    soonRowsKey: null,
  },
  {
    id: 'payments', name: 'Payments', icon: 'credit-card', color: '#9ece6a',
    desc: 'Accept payments and read transactions. Free to connect via OAuth; providers charge per-transaction fees.',
    members: [],
    soonCards: [],
    soonRowsKey: 'ac-rows-payments',
  },
  {
    id: 'developer', name: 'Developer', icon: 'git-branch', color: '#7aa2f7',
    desc: 'Repositories, issues, and CI. Free — connect with an OAuth app or token.',
    members: [],
    soonCards: [],
    soonRowsKey: 'ac-rows-developer',
  },
  {
    id: 'crm', name: 'CRM & Marketing', icon: 'contact', color: '#bb9af7',
    desc: 'Contacts, deals, and email campaigns. Mostly free OAuth; some products need a paid plan.',
    members: [],
    soonCards: [],
    soonRowsKey: 'ac-rows-crm',
  },
];

function _integrationGroupOf(id) {
  return _INTEGRATION_GROUPS.find(g => g.members.some(m => m.id === id));
}

// Build the empty group shells (head + body with a slot per card member) and
// append them to the single Agent Tools table, after the host-ability groups.
// The real cards are relocated into the slots later by _placeIntegrationGroupCards.
function _buildIntegrationGroupShells(container) {
  for (const group of _INTEGRATION_GROUPS) {
    const groupEl = document.createElement('div');
    groupEl.className = 'ac-row ac-group';
    groupEl.id = `ac-group-${group.id}`;

    const head = document.createElement('div');
    head.className = 'ac-ability-row ac-group-head';

    const iconWrap = document.createElement('div');
    iconWrap.className = 'ac-ability-icon';
    iconWrap.style.color = group.color;
    iconWrap.innerHTML = `<i data-lucide="${group.icon}" style="width:18px;height:18px;"></i>`;

    const label = document.createElement('div');
    label.className = 'ac-ability-label';
    label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
    label.querySelector('.ac-ability-name').textContent = group.name;
    label.querySelector('.ac-ability-desc').textContent = group.desc;

    head.appendChild(iconWrap);
    head.appendChild(label);

    // 3-position toggle — only when the group has at least one available
    // (non-coming-soon) member it can actually reflect / control.
    if (group.members.length) {
      const tri = document.createElement('span');
      tri.className = 'ac-tri';
      tri.id = `ac-group-tri-${group.id}`;
      tri.dataset.state = 'off';
      tri.setAttribute('role', 'button');
      tri.setAttribute('tabindex', '0');
      tri.title = 'Off · Mixed · On — click the left half for all-off, the right half for all-on';
      tri.innerHTML = `<span class="ac-tri-knob"></span>`;
      head.appendChild(tri);
      tri.addEventListener('click', (e) => {
        e.stopPropagation();
        const rect = tri.getBoundingClientRect();
        _setIntegrationGroupAll(group, (e.clientX - rect.left) > rect.width / 2);
      });
      tri.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          _setIntegrationGroupAll(group, tri.dataset.state !== 'on');
        }
      });
    }

    const chevron = document.createElement('span');
    chevron.className = 'ac-row-chevron';
    chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;
    head.appendChild(chevron);

    const bodyEl = document.createElement('div');
    bodyEl.className = 'ac-group-body';
    bodyEl.id = `ac-group-body-${group.id}`;
    for (const m of group.members) {
      const slot = document.createElement('div');
      slot.id = `ac-group-slot-${m.id}`;
      bodyEl.appendChild(slot);
    }

    groupEl.appendChild(head);
    groupEl.appendChild(bodyEl);
    container.appendChild(groupEl);

    head.addEventListener('click', (e) => {
      if (e.target.closest('.ac-tri')) return;
      groupEl.classList.toggle('expanded');
    });
  }
}

// Move the compactified integration cards + coming-soon rows into their group
// bodies in the single Agent Tools table, then hide the now-empty legacy category
// sections. Runs after _compactifyAllIntegrations has built the compact rows.
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
    // Coming-soon placeholder rows (no backing card) — build inline.
    const soon = group.soonRowsKey ? (_COMING_SOON_ROWS[group.soonRowsKey] || []) : [];
    for (const it of soon) {
      const row = document.createElement('div');
      row.className = 'ac-ability-row ac-int-row ac-soon-row';
      row.innerHTML = `<div class="ac-ability-icon"><i data-lucide="${it.icon}" style="width:18px;height:18px;"></i></div>`
        + `<div class="ac-ability-label"><div class="ac-ability-name"></div><div class="ac-ability-desc"></div></div>`
        + `<span class="ac-soon-pill">Coming soon</span>`;
      row.querySelector('.ac-ability-name').textContent = it.name;
      row.querySelector('.ac-ability-desc').textContent = it.note;
      body.appendChild(row);
    }
  }
  // Hide the emptied legacy category sections (their cards now live in groups).
  for (const cid of ['ac-cat-channels', 'ac-cat-productivity', 'ac-cat-social', 'ac-cat-marketplace', 'ac-cat-payments', 'ac-cat-developer', 'ac-cat-crm']) {
    const cat = document.getElementById(cid);
    if (cat) cat.style.display = 'none';
  }
  _syncAllIntegrationGroupTris();
}

// ── Integration-group 3-position toggle: reflect state + bulk on/off ─────────
// Only AVAILABLE members count (coming-soon never does). "On" = configured /
// enabled (the row's badge carries ac-int-badge-on, read by _rowConfigured).
function _syncIntegrationGroupTri(groupId) {
  const group = _INTEGRATION_GROUPS.find(g => g.id === groupId);
  const tri = _qs(`ac-group-tri-${groupId}`);
  if (!group || !tri || !group.members.length) return;
  const onCount = group.members.filter(m => _rowConfigured(m.id)).length;
  tri.dataset.state = onCount === 0 ? 'off'
    : (onCount === group.members.length ? 'on' : 'mixed');
}

function _syncAllIntegrationGroupTris() {
  for (const g of _INTEGRATION_GROUPS) _syncIntegrationGroupTri(g.id);
}

function _syncIntegrationGroupTriOf(id) {
  const g = _integrationGroupOf(id);
  if (g) _syncIntegrationGroupTri(g.id);
}

// Bulk on/off for a group's available members. Off disables channels and
// unconfigures providers (one confirm for the destructive provider case). On
// enables what needs no credentials (a channel); unconfigured OAuth providers
// can't be force-enabled here — they still need their keys on their own row.
function _setIntegrationGroupAll(group, goOn) {
  if (!goOn) {
    const configuredProviders = group.members.filter(m => m.kind !== 'channel' && _rowConfigured(m.id));
    if (configuredProviders.length &&
        !confirm(`Disable all configured ${group.name} integrations? This removes their saved app credentials.`)) {
      return;
    }
    for (const m of group.members) {
      if (!_rowConfigured(m.id)) continue;
      if (m.kind === 'channel') _disableChannel(m.id);
      else _unconfigureById(m.id, m.kind);
    }
  } else {
    for (const m of group.members) {
      if (m.kind === 'channel' && !_rowConfigured(m.id)) _enableChannel(m.id);
    }
  }
}

function _rowConfigured(id) {
  const badge = _qs(`ac-int-${id}-badge`);
  return !!(badge && badge.classList.contains('ac-int-badge-on'));
}

function _unconfigureById(id, kind) {
  if (id === 'scraper') return _unconfigureScraper();
  if (id === 'browser_session') return _unconfigureBrowserSession();
  return _unconfigureProvider(id);
}

// Transform one existing card into a compact row + collapsible config body.
function _compactifyCard(id, kind) {
  const card = _qs(`ac-int-${id}-card`);
  if (!card || card.dataset.compactified) return;
  if (!_qs(`ac-int-${id}-body`)) { try { _initCollapsible(id); } catch (_) {} }
  card.classList.add('ac-card-compact');

  const header = card.querySelector(':scope > div');
  const soon = (kind === 'soon');
  const note = _ROW_NOTES[id] || '';

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
    if (kind === 'channel') {
      if (on) _enableChannel(id); else _disableChannel(id);
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
      _unconfigureById(id, kind);
    }
  });
}

function _compactifyAllIntegrations() {
  for (const p of _OAUTH_PROVIDER_IDS) _compactifyCard(p, 'oauth');
  for (const p of _GENERIC_PROVIDER_IDS) _compactifyCard(p, 'generic');
  for (const c of _CHANNEL_IDS) _compactifyCard(c, 'channel');
  for (const c of _SOON_CHANNEL_IDS) _compactifyCard(c, 'soon');
  _placeWebCredentialRows();        // Web Scraper + Browser Cookies into the Web group
  _placeIntegrationGroupCards();    // channels/providers into their groups; hide legacy categories
  if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
}

function _syncRowToggle(id) {
  const toggle = _qs(`ac-int-row-toggle-${id}`);
  if (!toggle) return;
  const on = _rowConfigured(id);
  toggle.checked = on;
  const row = _qs(`ac-int-row-${id}`);
  if (row) row.classList.toggle('ac-ability-enabled', on);
  _syncIntegrationGroupTriOf(id);   // keep the parent group's 3-pos toggle in sync
}

function _syncAllRowToggles() {
  for (const id of [..._OAUTH_PROVIDER_IDS, ..._GENERIC_PROVIDER_IDS, ..._CHANNEL_IDS]) {
    _syncRowToggle(id);
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
    _qs(`ac-int-${p}-unconfigure`)?.addEventListener('click', () => _unconfigureProvider(p));
  }
  // Generic, non-OAuth providers (different field shapes)
  const genericProviders = ['scraper', 'browser_session'];
  for (const p of genericProviders) {
    _initCollapsible(p);
  }
  _qs('ac-int-scraper-save')?.addEventListener('click', _saveScraperConfig);
  _qs('ac-int-scraper-edit')?.addEventListener('click', () => _editGenericProvider('scraper'));
  _qs('ac-int-scraper-unconfigure')?.addEventListener('click', _unconfigureScraper);
  _qs('ac-int-browser_session-save')?.addEventListener('click', _saveBrowserSession);
  _qs('ac-int-browser_session-edit')?.addEventListener('click', () => _editGenericProvider('browser_session'));
  _qs('ac-int-browser_session-unconfigure')?.addEventListener('click', _unconfigureBrowserSession);

  // Channels (admin enable/disable; per-agent creds live in the agent's Abilities tab)
  const channels = ['telegram'];
  for (const c of channels) {
    _initCollapsible(c);
    _qs(`ac-int-${c}-save`)?.addEventListener('click', () => _enableChannel(c));
    _qs(`ac-int-${c}-unconfigure`)?.addEventListener('click', () => _disableChannel(c));
  }
  // Coming-soon channel placeholders — searchable but not interactive.
  const comingSoonChannels = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

  // Agent Tools (admin enable/disable; per-agent toggles in the Abilities tab)
  // Rendered as compact rows with toggle — see _renderAbilitiesCompact()
  _initAbilitiesCompact();

  // Unify every OAuth / channel / generic card into the same compact-row look,
  // and render the card-less coming-soon integrations as greyed rows.
  _compactifyAllIntegrations();

  _initIntegrationsSearch([...providers, ...genericProviders, ...channels, ...comingSoonChannels, ...Object.keys(_ABILITY_META)]);
  _initIntegrationAdminChat();
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

// Each visit to the Agent Settings section starts a brand-new webAgent session,
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

  // This chat targets the shared webAgent (the `default` template), same as the
  // Agents / Pages / Source Control pills. Resolve (or create) that agent first.
  let agentId;
  try {
    agentId = await app.ensureWebagentAgent(app.currentUserId);
  } catch (e) {
    _intAdminShowError('Could not start webAgent: ' + (e.message || e));
    return;
  }

  input.value = '';
  _intAdminSetBusy(true);
  _intAdminAppendBubble('user', text);
  const agentBubble = _intAdminAppendBubble('agent', '…');
  let agentBuffer = '';

  const payload = {
    message: text,
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

    // When searching, auto-open categories with matches; when cleared, close all
    for (const cat of document.querySelectorAll('.ac-category-group')) {
      if (q) {
        cat.open = !!catVisibility[cat.id];
      } else {
        cat.open = false;
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
    _applyScraperStatus(data);
    _applyChannelStatus('telegram', data.telegram_configured);
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
    _applyAbilityFromLoad('wiki_control',        data.wiki_control_configured);
    // Browser session is per-user — fetched from a separate endpoint.
    _loadBrowserSessionStatus();
    // Reflect each provider/channel's configured state onto its unified row toggle.
    _syncAllRowToggles();
    // Reflect ability states onto each group's 3-position toggle.
    _syncAllGroupToggles();
  } catch (e) {
    for (const p of ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon', 'scraper', 'browser_session', 'telegram', 'codebase_admin', 'create_tools', 'automation', 'web_access', 'browser_control', 'image_generation', 'visualizer', 'agent_orchestration', 'diagnostics', 'agent_management']) {
      const s = _qs(`ac-int-${p}-status`);
      if (s) { s.textContent = `Failed to load: ${e.message}`; s.style.color = '#f7768e'; s.style.display = 'block'; }
    }
  }
}

// ── Generic (non-OAuth) provider helpers ───────────────────────────────────

function _applyScraperStatus(data) {
  const badge        = _qs('ac-int-scraper-badge');
  const configuredEl = _qs('ac-int-scraper-configured');
  const form         = _qs('ac-int-scraper-form');
  if (data.scraper_configured) {
    if (badge) { badge.textContent = 'Configured'; badge.className = 'ac-int-badge ac-int-badge-on'; }
    if (configuredEl) configuredEl.style.display = 'block';
    if (form) form.style.display = 'none';
    const provEl = _qs('ac-int-scraper-provider'); if (provEl) provEl.textContent = data.scraper_provider || '';
    const epEl   = _qs('ac-int-scraper-endpoint'); if (epEl)   epEl.textContent   = data.scraper_endpoint || '(default)';
    const extra  = [data.scraper_actor_id && `actor=${data.scraper_actor_id}`,
                    data.scraper_host     && `host=${data.scraper_host}`].filter(Boolean).join(' · ');
    const exEl   = _qs('ac-int-scraper-extra'); if (exEl)      exEl.textContent   = extra || '(none)';
  } else {
    if (badge) { badge.textContent = 'Not configured'; badge.className = 'ac-int-badge ac-int-badge-off'; }
    if (configuredEl) configuredEl.style.display = 'none';
    if (form) form.style.display = 'block';
  }
  _syncRowToggle('scraper');
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

async function _enableChannel(channel) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.style.display = 'none';
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, true);
    if (statusEl) { statusEl.textContent = 'Enabled.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Failed: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _disableChannel(channel) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.style.display = 'none';
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, false);
    if (statusEl) { statusEl.textContent = 'Disabled.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Failed: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
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

async function _enableAbility(ability) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (statusEl) statusEl.style.display = 'none';
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, true);
    if (statusEl) { statusEl.textContent = 'Enabled.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Failed: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _disableAbility(ability) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (ability === 'automation') {
    const ok = confirm(
      'Disable Automation?\n\n'
      + 'This will permanently delete every agent\'s scheduled tasks and event subscriptions. '
      + 'This cannot be undone.'
    );
    if (!ok) return;
  }
  if (statusEl) statusEl.style.display = 'none';
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, false);
    if (statusEl) { statusEl.textContent = 'Disabled.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; }
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Failed: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _loadBrowserSessionStatus() {
  const badge        = _qs('ac-int-browser_session-badge');
  const configuredEl = _qs('ac-int-browser_session-configured');
  const form         = _qs('ac-int-browser_session-form');
  try {
    const res = await _fetch(apiPath('/admin/integrations/browser-session'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.configured) {
      if (badge) { badge.textContent = 'Configured'; badge.className = 'ac-int-badge ac-int-badge-on'; }
      if (configuredEl) configuredEl.style.display = 'block';
      if (form) form.style.display = 'none';
      const domEl  = _qs('ac-int-browser_session-domain'); if (domEl)  domEl.textContent  = data.domain || '(any)';
      const uaEl   = _qs('ac-int-browser_session-ua');     if (uaEl)   uaEl.textContent   = data.user_agent || '(default)';
      const cntEl  = _qs('ac-int-browser_session-count');  if (cntEl)  cntEl.textContent  = String(data.cookie_count || 0);
      const svEl   = _qs('ac-int-browser_session-saved');  if (svEl)   svEl.textContent   = data.saved_at || '';
    } else {
      if (badge) { badge.textContent = 'Not configured'; badge.className = 'ac-int-badge ac-int-badge-off'; }
      if (configuredEl) configuredEl.style.display = 'none';
      if (form) form.style.display = 'block';
    }
    _syncRowToggle('browser_session');
  } catch (e) {
    const s = _qs('ac-int-browser_session-status');
    if (s) { s.textContent = `Failed to load: ${e.message}`; s.style.color = '#f7768e'; s.style.display = 'block'; }
  }
}

function _editGenericProvider(provider) {
  const configuredEl = _qs(`ac-int-${provider}-configured`);
  const form         = _qs(`ac-int-${provider}-form`);
  const statusEl     = _qs(`ac-int-${provider}-status`);
  if (statusEl) statusEl.style.display = 'none';
  if (configuredEl) configuredEl.style.display = 'none';
  if (form) form.style.display = 'block';
}

async function _saveScraperConfig() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provInput   = _qs('ac-int-scraper-input-provider');
  const keyInput    = _qs('ac-int-scraper-input-key');
  const epInput     = _qs('ac-int-scraper-input-endpoint');
  const actorInput  = _qs('ac-int-scraper-input-actor');
  const hostInput   = _qs('ac-int-scraper-input-host');
  const statusEl    = _qs('ac-int-scraper-status');
  if (!keyInput?.value?.trim()) {
    if (statusEl) { statusEl.textContent = 'API key is required.'; statusEl.style.color = '#e0af68'; statusEl.style.display = 'block'; }
    return;
  }
  try {
    const res = await _fetch(apiPath('/admin/integrations/scraper'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: provInput?.value || 'apify',
        api_key: keyInput.value.trim(),
        endpoint: epInput?.value?.trim() || '',
        actor_id: actorInput?.value?.trim() || '',
        host: hostInput?.value?.trim() || '',
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (keyInput) keyInput.value = '';
    if (statusEl) { statusEl.textContent = 'Configured successfully.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadIntegrations();
    _expandCard('scraper');
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _unconfigureScraper() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const statusEl = _qs('ac-int-scraper-status');
  try {
    const res = await _fetch(apiPath('/admin/integrations/scraper'), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) { statusEl.textContent = 'Removed.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadIntegrations();
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _saveBrowserSession() {
  const domainInput  = _qs('ac-int-browser_session-input-domain');
  const uaInput      = _qs('ac-int-browser_session-input-ua');
  const cookiesInput = _qs('ac-int-browser_session-input-cookies');
  const statusEl     = _qs('ac-int-browser_session-status');
  if (!cookiesInput?.value?.trim()) {
    if (statusEl) { statusEl.textContent = 'Cookies are required.'; statusEl.style.color = '#e0af68'; statusEl.style.display = 'block'; }
    return;
  }
  try {
    const res = await _fetch(apiPath('/admin/integrations/browser-session'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        domain: domainInput?.value?.trim() || '',
        user_agent: uaInput?.value?.trim() || '',
        cookies: cookiesInput.value.trim(),
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (cookiesInput) cookiesInput.value = '';
    if (statusEl) { statusEl.textContent = 'Session saved.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadBrowserSessionStatus();
    _expandCard('browser_session');
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _unconfigureBrowserSession() {
  const statusEl = _qs('ac-int-browser_session-status');
  try {
    const res = await _fetch(apiPath('/admin/integrations/browser-session'), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) { statusEl.textContent = 'Removed.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadBrowserSessionStatus();
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

// ── URI copy-button helpers ───────────────────────────────────────────────

function _makeCopyBtn() {
  const btn = document.createElement('button');
  btn.type = 'button'; // prevent any accidental form submission
  btn.className = 'ac-uri-copy-btn';
  btn.title = 'Copy';
  // pointer-events:none on inner icon so clicks always land on the button
  btn.style.cssText = 'background:none;border:none;cursor:pointer;padding:2px;color:var(--fg-3);display:inline-flex;align-items:center;flex-shrink:0;line-height:0;';
  btn.innerHTML = `<span style="pointer-events:none;display:inline-flex;">${icon('copy', { size: '13px' })}</span>`;
  return btn;
}

function _copyToClipboard(text) {
  // Try modern API first
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for non-secure contexts (http://<ip>:8080 etc.)
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    } catch (e) { reject(e); }
  });
}

function _bindUri(btn, getText) {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const text = getText();
    _copyToClipboard(text).then(() => {
      btn.innerHTML = `<span style="pointer-events:none;display:inline-flex;">${icon('check', { size: '13px' })}</span>`;
      btn.style.color = 'var(--success)';
      setTimeout(() => {
        btn.innerHTML = `<span style="pointer-events:none;display:inline-flex;">${icon('copy', { size: '13px' })}</span>`;
        btn.style.color = 'var(--fg-3)';
      }, 1500);
    }).catch((err) => {
      console.error('[app-config] copy failed:', err);
      btn.title = 'Copy failed: ' + (err?.message || err);
    });
  });
}

// Attaches (or updates) a Lucide copy button next to a redirect-URI <code> element.
// Inline variant (no display:block): button inserted as sibling, parent becomes flex.
// Block variant (display:block): <code> becomes flex row [text-span][copy-btn].
function _attachUriCopy(codeEl, uri) {
  if (!codeEl) return;
  const single = uri || '';

  if (codeEl.dataset.uriCopy === 'block') {
    codeEl.querySelector('.ac-uri-text').textContent = single;
    return;
  }

  if (codeEl.dataset.uriCopy === 'inline') {
    codeEl.textContent = single;
    return;
  }

  if (codeEl.style.display === 'block') {
    codeEl.dataset.uriCopy = 'block';
    codeEl.style.display = 'flex';
    codeEl.style.alignItems = 'center';
    codeEl.style.justifyContent = 'space-between';
    codeEl.style.gap = '8px';
    const span = document.createElement('span');
    span.className = 'ac-uri-text';
    span.style.wordBreak = 'break-all';
    span.textContent = single;
    codeEl.appendChild(span);
    const btn = _makeCopyBtn();
    codeEl.appendChild(btn);
    _bindUri(btn, () => span.textContent);
  } else {
    codeEl.dataset.uriCopy = 'inline';
    codeEl.textContent = single;
    const btn = _makeCopyBtn();
    codeEl.insertAdjacentElement('afterend', btn);
    _bindUri(btn, () => codeEl.textContent);
    if (codeEl.parentElement) {
      Object.assign(codeEl.parentElement.style, {
        display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap',
      });
    }
  }
}

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
  if (!isAdmin()) { showRestrictedModal(); return; }
  const cidInput = _qs(`ac-int-${provider}-input-cid`);
  const secInput = _qs(`ac-int-${provider}-input-secret`);
  const uriInput = _qs(`ac-int-${provider}-input-redirect_uri`);
  const statusEl = _qs(`ac-int-${provider}-status`);
  if (!cidInput?.value?.trim() || !secInput?.value?.trim()) {
    if (statusEl) { statusEl.textContent = 'Both Client ID and Client Secret are required.'; statusEl.style.color = '#e0af68'; statusEl.style.display = 'block'; }
    return;
  }
  const selectedScopes = _getSelectedScopes(provider);
  const body = {
    client_id: cidInput.value.trim(),
    client_secret: secInput.value.trim(),
    scopes: selectedScopes,
  };
  const uriValue = (uriInput?.value || '').trim();
  if (uriValue) body.redirect_uri = uriValue;
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
    if (statusEl) { statusEl.textContent = 'Configured successfully.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadIntegrations();
    _expandCard(provider);
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

async function _unconfigureProvider(provider) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const statusEl = _qs(`ac-int-${provider}-status`);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/${provider}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) { statusEl.textContent = 'Unconfigured.'; statusEl.style.color = '#9ece6a'; statusEl.style.display = 'block'; setTimeout(() => { statusEl.style.display = 'none'; }, 3000); }
    _loadIntegrations();
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
  }
}

// Keep legacy aliases (referenced by any older inline calls)
async function _saveGoogleConfig()    { return _saveProviderConfig('google'); }
async function _unconfigureGoogle()   { return _unconfigureProvider('google'); }

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 3: Database ──────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
function _initDatabase() {
  _qs('ac-db-seg-cloud')?.addEventListener('click', () => _setDbMode('cloud'));
  _qs('ac-db-seg-local')?.addEventListener('click', () => _setDbMode('local'));
}

async function _loadDatabase() {
  const modeLabel = _qs('ac-db-current-mode');
  const statusEl  = _qs('ac-db-status');
  const cloudBtn  = _qs('ac-db-seg-cloud');
  const localBtn  = _qs('ac-db-seg-local');

  try {
    const res = await fetch(apiPath('/admin/db/mode'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const mode = data.mode || 'local';
    if (modeLabel) modeLabel.textContent = mode === 'cloud' ? 'Cloud (Supabase)' : 'Local (SQLite)';
    if (cloudBtn) cloudBtn.classList.toggle('active', mode === 'cloud');
    if (localBtn) localBtn.classList.toggle('active', mode === 'local');
    if (statusEl) statusEl.textContent = '';
  } catch (e) {
    if (modeLabel) modeLabel.textContent = 'Unknown';
    if (statusEl) { statusEl.textContent = 'Could not fetch mode'; statusEl.style.color = '#f7768e'; }
  }

  // Refresh the new Storage section (provider dropdown + secrets + migration).
  try { if (typeof window.__refreshStorageSection === 'function') window.__refreshStorageSection(); } catch {}
}

async function _setDbMode(mode) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const statusEl = _qs('ac-db-status');
  if (statusEl) { statusEl.textContent = `Switching to ${mode}…`; statusEl.style.color = '#565f89'; }
  try {
    const res = await _fetch(apiPath('/admin/db/mode'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (statusEl) {
      statusEl.textContent = data.message || 'Mode updated';
      statusEl.style.color = '#9ece6a';
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 3000);
    }
    await _loadDatabase();
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; }
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 4: Optimizer Stats ───────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
function _initOptimizer() {
  const modeLive = _qs('ac-opt-mode-live');
  const modeScheduled = _qs('ac-opt-mode-scheduled');
  const saveBtn = _qs('ac-opt-save');
  const runBtn  = _qs('ac-opt-run-now');

  modeLive?.addEventListener('click', () => _setOptMode('live'));
  modeScheduled?.addEventListener('click', () => _setOptMode('scheduled'));
  saveBtn?.addEventListener('click', _saveOptimizer);
  runBtn?.addEventListener('click', _runOptimizerNow);
}

function _setOptMode(mode) {
  const schedSec = _qs('ac-opt-schedule-section');
  const livBtn   = _qs('ac-opt-mode-live');
  const schBtn   = _qs('ac-opt-mode-scheduled');
  if (livBtn)  livBtn.classList.toggle('active', mode === 'live');
  if (schBtn)  schBtn.classList.toggle('active', mode === 'scheduled');
  if (schedSec) schedSec.style.display = mode === 'scheduled' ? '' : 'none';
}

async function _loadOptimizer() {
  await _loadSessionStats();
  try {
    const res = await _fetch(apiPath('/admin/settings/optimizer'));
    if (!res.ok) return;
    const cfg = await res.json();
    if (cfg.mode) _setOptMode(cfg.mode);
    const interval = _qs('ac-opt-schedule-interval');
    const minEl    = _qs('ac-opt-schedule-min');
    if (interval && cfg.schedule?.interval) interval.value = cfg.schedule.interval;
    if (minEl && cfg.schedule?.min_interactions) minEl.value = cfg.schedule.min_interactions;
  } catch (e) { /* use defaults */ }
}

async function _loadSessionStats() {
  const tbody    = _qs('ac-opt-session-tbody');
  const countEl  = _qs('ac-opt-session-count');
  const userId   = app.currentUserId;

  if (!tbody) return;
  if (!userId) {
    tbody.innerHTML = `<tr><td colspan="10" class="ac-table-empty">No user selected</td></tr>`;
    if (countEl) countEl.textContent = '— no user';
    return;
  }

  try {
    const res = await fetch(apiPath('/api/v1/db/session-stats?user_id=' + encodeURIComponent(userId) + '&db=local.db'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const sessions = data.sessions || [];
    if (countEl) countEl.textContent = `— ${sessions.length} session${sessions.length !== 1 ? 's' : ''}`;

    if (!sessions.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="ac-table-empty">No sessions found</td></tr>`;
      return;
    }

    const currentSid = app.currentSessionId;
    tbody.innerHTML = sessions.map(s => {
      const isActive = s.session_id === currentSid;
      const bg = isActive ? 'rgba(125,207,255,0.06)' : '';
      const bl = isActive ? '3px solid #7dcfff' : '3px solid transparent';
      return `<tr style="background:${bg};">
        <td style="border-left:${bl};font-weight:600;overflow:hidden;text-overflow:ellipsis;max-width:140px;" title="${_esc(s.session_id)}">${_esc(s.title)}</td>
        <td style="text-align:center;">${s.message_count}</td>
        <td style="text-align:center;">${s.turn_count}</td>
        <td style="text-align:right;color:#9ece6a;">${_fmtTokens(s.total_input_tokens)}</td>
        <td style="text-align:right;color:#7dcfff;">${_fmtTokens(s.total_output_tokens)}</td>
        <td style="text-align:right;color:#e0af68;font-weight:600;">${_fmtTokens(s.total_tokens)}</td>
        <td style="text-align:right;color:#bb9af7;">${_fmtDuration(s.total_duration_ms)}</td>
        <td style="text-align:right;color:${s.total_cost !== null ? '#9ece6a' : '#565f89'};">${_fmtCost(s.total_cost)}</td>
        <td style="font-size:10px;color:#565f89;white-space:nowrap;">${_fmtDate(s.last_active)}</td>
        <td style="text-align:center;">
          <button class="ac-session-switch" data-sid="${_esc(s.session_id)}"
            style="padding:4px 10px;background:${isActive ? 'rgba(125,207,255,0.1)' : 'transparent'};border:1px solid ${isActive ? '#7dcfff' : '#2a2a4a'};border-radius:4px;color:${isActive ? '#7dcfff' : '#565f89'};cursor:${isActive ? 'default' : 'pointer'};font-size:10px;font-family:inherit;"
            ${isActive ? 'disabled' : ''}>${isActive ? 'Active' : 'Switch'}</button>
        </td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('.ac-session-switch').forEach(btn => {
      if (btn.disabled) return;
      btn.addEventListener('click', () => {
        const sid = btn.dataset.sid;
        app.currentSessionId = sid;
        loadSessionChat(sid);
        populateSessionSelect();
        loopSessionChanged(sid);
        loopVisualSessionChanged(sid);
        _loadSessionStats();
      });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="ac-table-empty" style="color:#f7768e;">Error loading sessions</td></tr>`;
  }
}

async function _saveOptimizer() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const mode    = _qs('ac-opt-mode-live')?.classList.contains('active') ? 'live' : 'scheduled';
  const interval = _qs('ac-opt-schedule-interval')?.value || 'per-interaction';
  const minInt  = parseInt(_qs('ac-opt-schedule-min')?.value || '5', 10);
  const statusEl = _qs('ac-opt-status');

  try {
    const res = await _fetch(apiPath('/admin/settings/optimizer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, schedule: { interval, min_interactions: minInt } }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Config saved';
      statusEl.style.color = '#9ece6a';
      statusEl.style.display = 'inline';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'inline';
    }
  }
}

async function _runOptimizerNow() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const statusEl = _qs('ac-opt-status');
  if (statusEl) { statusEl.textContent = 'Running optimizer…'; statusEl.style.color = '#565f89'; statusEl.style.display = 'inline'; }
  try {
    const res = await _fetch(apiPath('/admin/optimizer/run'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) { statusEl.textContent = 'Optimizer started'; statusEl.style.color = '#9ece6a'; }
    setTimeout(() => { if (statusEl) statusEl.style.display = 'none'; }, 3000);
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = '#f7768e'; }
  }
}

// ── Formatter helpers ──
function _fmtTokens(n) { if (!n) return '—'; return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }
function _fmtDuration(ms) { if (!ms) return '—'; if (ms < 1000) return ms + 'ms'; if (ms < 60000) return (ms/1000).toFixed(1) + 's'; return Math.floor(ms/60000) + 'm ' + Math.floor((ms%60000)/1000) + 's'; }
function _fmtCost(c) { if (c === null || c === undefined) return '—'; return '$' + (c < 0.001 ? c.toFixed(6) : c < 0.01 ? c.toFixed(4) : c.toFixed(3)); }
function _fmtDate(ts) { if (!ts) return '—'; try { const d = new Date(ts); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); } catch { return ts; } }

// ───────────────────────────────────────────────────────────────────────────
// ── SECTION 5: Git Providers ──────────────────────────────────────────────────
// ───────────────────────────────────────────────────────────────────────────
function _initGit() {
  _qs('ac-gh-token-save')?.addEventListener('click', _saveGitHubToken);
  _qs('ac-gh-refresh')?.addEventListener('click', _loadGitStatus);
}

async function _loadGit() {
  // Load saved token status
  try {
    const res = await _fetch(apiPath('/api/v1/github/token-status'));
    if (res.ok) {
      const data = await res.json();
      const configuredEl = _qs('ac-gh-token-configured');
      if (configuredEl) {
        configuredEl.textContent = data.configured ? '✓ Configured' : '— not set';
        configuredEl.style.color = data.configured ? '#9ece6a' : '#565f89';
      }
    }
  } catch (e) { /* not critical */ }

  await _loadGitStatus();
}

async function _loadGitStatus() {
  const resultEl = _qs('ac-gh-refresh-result');
  if (resultEl) resultEl.textContent = 'Loading…';

  const fields = {
    branch:      _qs('ac-gh-branch'),
    remote:      _qs('ac-gh-remote'),
    lastCommit:  _qs('ac-gh-last-commit'),
    aheadBehind: _qs('ac-gh-ahead-behind'),
    fileCount:   _qs('ac-gh-file-count'),
  };

  try {
    const res = await _fetch(apiPath('/api/v1/github/status'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    if (fields.branch)      fields.branch.textContent      = data.branch      || '—';
    if (fields.remote)      fields.remote.textContent      = data.remote_url  || '—';
    if (fields.lastCommit)  fields.lastCommit.textContent  = data.last_commit ? data.last_commit.slice(0, 50) : '—';
    if (fields.aheadBehind) fields.aheadBehind.textContent = data.ahead_behind || '—';
    if (fields.fileCount)   fields.fileCount.textContent   = data.changed_files !== undefined ? String(data.changed_files) : '—';

    if (resultEl) resultEl.textContent = '';
  } catch (e) {
    if (resultEl) { resultEl.textContent = 'Could not fetch repo status'; resultEl.style.color = '#565f89'; }
    Object.values(fields).forEach(el => { if (el) el.textContent = '—'; });
  }
}

async function _saveGitHubToken() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const tokenEl  = _qs('ac-gh-token-input');
  const statusEl = _qs('ac-gh-token-status');
  const token    = tokenEl?.value?.trim() || '';
  if (!token) {
    if (statusEl) { statusEl.textContent = 'Please enter a token'; statusEl.style.color = '#f7768e'; statusEl.style.display = 'block'; }
    return;
  }
  try {
    const res = await _fetch(apiPath('/api/v1/github/token'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Token saved';
      statusEl.style.color = '#9ece6a';
      statusEl.style.display = 'block';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }
    if (tokenEl) tokenEl.value = '';
    await _loadGit();
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'block';
    }
  }
}

// ───────────────────────────────────────────────────────────────────────────
// ── App Settings ─────────────────────────────────────────────────────────────
// ───────────────────────────────────────────────────────────────────────────

function _initAppSettings() {
  const saveBtn = _qs('ac-app-settings-save');
  if (saveBtn) saveBtn.addEventListener('click', _saveAppSettings);
  _initBootAnimation();
  _initBootMobileMode();
  // Startup & Boot settings live in expandable compact rows: the collapsed line
  // shows the current choice; expanding reveals the dropdown.
  _wireBootRow('ac-boot-anim-row', 'ac-boot-animation');
  _wireBootRow('ac-boot-mobile-row', 'ac-boot-mobile-mode');
  _initMainPanelPages();
}

// Wire one Startup & Boot row: clicking the head expands/collapses it, and the
// right-hand status shows the currently selected option's label.
function _wireBootRow(rowId, selectId) {
  const row = _qs(rowId);
  if (!row) return;
  const sel = _qs(selectId);
  const head = row.querySelector(':scope > .ac-ability-row');
  const status = row.querySelector('.ac-ability-status');
  const syncStatus = () => {
    if (status && sel) status.textContent = sel.options[sel.selectedIndex]?.text || '';
  };
  syncStatus();
  sel?.addEventListener('change', syncStatus);
  head?.addEventListener('click', (e) => {
    // Don't toggle when interacting with the control itself.
    if (e.target.closest('select, input, button, a, label')) return;
    row.classList.toggle('expanded');
  });
}

// ── Main Panel Pages (tab visibility + order) ──────────────────────────────

const _MAIN_PANEL_PAGES = [
  { id: 'admin-tools', label: 'Admin Tools', icon: 'wrench', locked: true },
  { id: 'agents',      label: 'Agents',      icon: 'bot' },
  { id: 'autoagent',   label: 'Pages',       icon: 'files' },
  { id: 'web',         label: 'Web',         icon: 'globe' },
  { id: 'terminal',    label: 'Terminal',    icon: 'terminal' },
  { id: 'wiki',        label: 'Wiki',        icon: 'book-open' },
];

const _LS_MAIN_PANEL_ORDER = 'mainPanelOrder';
const _LS_MAIN_PANEL_HIDDEN = 'mainPanelHidden';

function _getMainPanelOrder() {
  try {
    const saved = localStorage.getItem(_LS_MAIN_PANEL_ORDER);
    if (saved) {
      const parsed = JSON.parse(saved);
      const allIds = _MAIN_PANEL_PAGES.map(p => p.id);
      const hasAll = allIds.every(id => parsed.includes(id));
      if (hasAll && parsed.length === allIds.length) return parsed;
    }
  } catch (_) {}
  return ['admin-tools', 'agents', 'autoagent', 'web', 'terminal', 'wiki'];
}

function _getMainPanelHidden() {
  try {
    const saved = localStorage.getItem(_LS_MAIN_PANEL_HIDDEN);
    if (saved) return JSON.parse(saved);
  } catch (_) {}
  return [];
}

function _saveMainPanelOrder(order) {
  try { localStorage.setItem(_LS_MAIN_PANEL_ORDER, JSON.stringify(order)); } catch (_) {}
}

function _saveMainPanelHidden(hidden) {
  try { localStorage.setItem(_LS_MAIN_PANEL_HIDDEN, JSON.stringify(hidden)); } catch (_) {}
}

function _renderMainPanelList() {
  const list = _qs('ac-main-panel-list');
  if (!list) return;

  const order = _getMainPanelOrder();
  const hidden = _getMainPanelHidden();

  list.innerHTML = '';

  order.forEach((id, index) => {
    const page = _MAIN_PANEL_PAGES.find(p => p.id === id);
    if (!page) return;

    const isHidden = hidden.includes(id);
    const isLocked = page.locked;

    const item = document.createElement('div');
    // Shared compact-row look (`.ac-ability-row` inside the `.ac-list`), plus a
    // drag handle so the list doubles as a drag-to-reorder list.
    item.className = 'ac-main-panel-item ac-ability-row' + (isLocked ? ' ac-mp-locked' : '');
    item.draggable = true;
    item.dataset.pageId = id;
    item.dataset.index = index;

    const handle = document.createElement('span');
    handle.className = 'ac-mp-drag-handle';
    handle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/></svg>';

    const iconWrap = document.createElement('span');
    iconWrap.className = 'ac-ability-icon';
    iconWrap.innerHTML = `<i data-lucide="${page.icon}" class="lucide-icon" style="width:18px;height:18px;"></i>`;

    const label = document.createElement('div');
    label.className = 'ac-ability-label';
    label.innerHTML = `<div class="ac-ability-name">${_esc(page.label)}</div>`;

    const toggle = document.createElement('button');
    toggle.className = 'ac-mp-toggle' + (isHidden ? '' : ' active');
    toggle.type = 'button';
    toggle.title = isLocked ? 'Admin Tools is always visible' : (isHidden ? 'Show in header' : 'Hide from header');
    toggle.disabled = isLocked;
    toggle.addEventListener('click', () => {
      if (isLocked) return;
      const currentHidden = _getMainPanelHidden();
      const idx = currentHidden.indexOf(id);
      if (idx >= 0) {
        currentHidden.splice(idx, 1);
        toggle.classList.add('active');
        toggle.title = 'Hide from header';
      } else {
        currentHidden.push(id);
        toggle.classList.remove('active');
        toggle.title = 'Show in header';
      }
      _saveMainPanelHidden(currentHidden);
      _applyMainPanelOrder();
    });

    item.appendChild(handle);
    item.appendChild(iconWrap);
    item.appendChild(label);
    item.appendChild(toggle);

    // Drag events
    item.addEventListener('dragstart', (e) => {
      item.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', id);
    });

    item.addEventListener('dragend', () => {
      item.classList.remove('dragging');
      list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
    });

    item.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
      item.classList.add('drag-over');
    });

    item.addEventListener('dragleave', () => {
      item.classList.remove('drag-over');
    });

    item.addEventListener('drop', (e) => {
      e.preventDefault();
      item.classList.remove('drag-over');
      const fromId = e.dataTransfer.getData('text/plain');
      const toId = id;
      if (fromId === toId) return;

      const currentOrder = _getMainPanelOrder();
      const fromIdx = currentOrder.indexOf(fromId);
      const toIdx = currentOrder.indexOf(toId);
      if (fromIdx < 0 || toIdx < 0) return;

      currentOrder.splice(fromIdx, 1);
      const newToIdx = currentOrder.indexOf(toId);
      currentOrder.splice(newToIdx + (fromIdx < toIdx ? 0 : 0), 0, fromId);

      _saveMainPanelOrder(currentOrder);
      _renderMainPanelList();
      _applyMainPanelOrder();
    });

    list.appendChild(item);
  });

  if (window.lucide) {
    try { lucide.createIcons(); } catch (_) {}
  }
}

function _applyMainPanelOrder() {
  const order = _getMainPanelOrder();
  const hidden = _getMainPanelHidden();

  // Update the hidden <select> options order
  const nativeSelect = document.getElementById('main-tab-select');
  if (nativeSelect) {
    const options = Array.from(nativeSelect.options);
    const accountOpt = options.find(o => o.value === 'account');
    const otherOpts = options.filter(o => o.value !== 'account');

    otherOpts.sort((a, b) => {
      const ai = order.indexOf(a.value);
      const bi = order.indexOf(b.value);
      return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
    });

    otherOpts.forEach(opt => nativeSelect.appendChild(opt));
    if (accountOpt) nativeSelect.appendChild(accountOpt);
  }

  // Reorder + show/hide the header tab buttons. Nodes are KEPT in the DOM
  // (display toggled, not removed) so a later un-hide restores them live
  // without a reload. This mirrors the pre-paint block in index.html that
  // applies the same preference before first paint to avoid a flash.
  const tabBar = document.getElementById('main-tabs');
  if (!tabBar) return;

  const adminGroup = document.getElementById('admin-tools-group');
  const tabButtons = {};
  if (adminGroup) tabButtons['admin-tools'] = adminGroup;
  tabBar.querySelectorAll('.main-tab').forEach(btn => {
    const id = btn.dataset.value;
    if (!id) return;
    tabButtons[id] = (adminGroup && adminGroup.contains(btn)) ? adminGroup : btn;
  });

  order.forEach(id => {
    const tab = tabButtons[id];
    if (!tab) return;
    const hide = id !== 'admin-tools' && hidden.includes(id);
    tab.style.display = hide ? 'none' : '';
    tabBar.appendChild(tab);   // reorder
  });
}

function _initMainPanelPages() {
  _renderMainPanelList();
  _applyMainPanelOrder();
}

const _BOOT_ANIM_ALLOWED = ['chat-slide-right', 'page-slide-in', 'crossfade'];

function _initBootAnimation() {
  const sel = _qs('ac-boot-animation');
  if (!sel) return;
  let saved = localStorage.getItem('bootAnimation');
  if (!_BOOT_ANIM_ALLOWED.includes(saved)) saved = 'chat-slide-right';
  sel.value = saved;
  sel.addEventListener('change', () => {
    const val = _BOOT_ANIM_ALLOWED.includes(sel.value) ? sel.value : 'chat-slide-right';
    localStorage.setItem('bootAnimation', val);
  });
}

const _BOOT_MOBILE_ALLOWED = ['chat-first', 'memory'];

function _initBootMobileMode() {
  const sel = _qs('ac-boot-mobile-mode');
  if (!sel) return;
  let saved = localStorage.getItem('bootMobileMode');
  if (!_BOOT_MOBILE_ALLOWED.includes(saved)) saved = 'chat-first';
  sel.value = saved;
  sel.addEventListener('change', () => {
    const val = _BOOT_MOBILE_ALLOWED.includes(sel.value) ? sel.value : 'chat-first';
    localStorage.setItem('bootMobileMode', val);
  });
}

async function _loadAppSettings() {
  try {
    const res = await _fetch(apiPath('/admin/settings/app'));
    if (!res.ok) return;
    const data = await res.json();
    const cb = _qs('ac-extend-llm-to-agents');
    if (cb) cb.checked = data.extend_llm_to_agents !== false;
  } catch (e) {
    console.warn('app-config: could not load app settings', e);
  }
}

async function _saveAppSettings() {
  const cb = _qs('ac-extend-llm-to-agents');
  const statusEl = _qs('ac-app-settings-status');
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        extend_llm_to_agents: cb ? cb.checked : true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Saved';
      statusEl.style.color = '#9ece6a';
      statusEl.style.display = 'block';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }

  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'block';
    }
  }
}

// ───────────────────────────────────────────────────────────────────────────
// ── User Management ─────────────────────────────────────────────────────────
// ───────────────────────────────────────────────────────────────────────────

function _initUserManagement() {
  _qs('ac-um-save')?.addEventListener('click', _saveUserManagement);
  _qs('ac-um-users-header')?.addEventListener('click', _toggleUsersCard);
  _qs('ac-um-access-header')?.addEventListener('click', _toggleAccessModeCard);
}

function _toggleAccessModeCard() {
  const body = _qs('ac-um-access-body');
  const chev = _qs('ac-um-access-chevron');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (chev) chev.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
}

async function _loadUserManagement() {
  // 1. Access mode from /admin/settings/app
  try {
    const res = await _fetch(apiPath('/admin/settings/app'));
    if (res.ok) {
      const data = await res.json();
      const mode = data.access_mode || 'public_anonymous';
      const radio = document.querySelector(`input[name="ac-um-access-mode"][value="${mode}"]`);
      if (radio) radio.checked = true;
    }
  } catch (e) {
    console.warn('user-management: could not load access mode', e);
  }

  // 2. Users list
  if (isAdmin()) {
    await _loadUsersList();
  }
}



async function _saveUserManagement() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const selected = document.querySelector('input[name="ac-um-access-mode"]:checked');
  const mode = selected ? selected.value : 'public_anonymous';
  const extendCb = _qs('ac-extend-llm-to-agents');

  const statusEl = _qs('ac-um-status');
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        access_mode: mode,
        extend_llm_to_agents: extendCb ? extendCb.checked : true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Saved';
      statusEl.style.color = '#9ece6a';
      statusEl.style.display = 'inline';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }
    // Broadcast to other modules (sign-in modal, chat gate)
    try {
      window.dispatchEvent(new CustomEvent('access-mode-changed', { detail: { access_mode: mode } }));
    } catch {}
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'inline';
    }
  }
}

function _toggleUsersCard() {
  const body = _qs('ac-um-users-body');
  const chev = _qs('ac-um-users-chevron');
  if (!body) return;
  const open = body.style.display === 'none';
  body.style.display = open ? '' : 'none';
  if (chev) chev.style.transform = open ? 'rotate(90deg)' : 'rotate(0deg)';
  if (open) _loadUsersList();
}

async function _loadUsersList() {
  const tbody = _qs('ac-um-users-tbody');
  const summary = _qs('ac-um-users-summary');
  if (!tbody) return;

  const userId = localStorage.getItem('auth_user_id') || '';
  if (!userId) {
    tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty">Sign in as admin to view users.</td></tr>`;
    if (summary) summary.textContent = '—';
    return;
  }

  try {
    const res = await _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(userId)));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const users = data.users || [];

    const pending = users.filter(u => !u.is_approved).length;
    if (summary) {
      summary.textContent = `${users.length} user${users.length !== 1 ? 's' : ''}` +
        (pending ? ` — ${pending} awaiting approval` : '');
    }

    if (!users.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty">No registered users.</td></tr>`;
      return;
    }

    tbody.innerHTML = users.map(u => {
      const role = u.is_admin
        ? `<span class="ac-um-badge ac-um-badge-admin">admin</span>`
        : `<span class="ac-um-badge ac-um-badge-member">member</span>`;
      const status = u.is_approved
        ? `<span class="ac-um-badge ac-um-badge-approved">active</span>`
        : `<span class="ac-um-badge ac-um-badge-pending">pending</span>`;
      const isSelf = u.user_id === userId;
      const isBuiltinAdmin = u.username === 'admin';

      let actions = '';
      if (!u.is_approved) {
        actions += `<button class="ac-um-action-btn" data-act="approve" data-uid="${_esc(u.user_id)}" title="Authorize this account to sign in">Authorize</button>`;
      } else if (!isSelf && !isBuiltinAdmin) {
        actions += `<button class="ac-um-action-btn" data-act="revoke" data-uid="${_esc(u.user_id)}" title="Restrict this account (block sign-in)">Restrict</button>`;
      }
      if (!isSelf && !isBuiltinAdmin && u.is_approved) {
        if (u.is_admin) {
          actions += `<button class="ac-um-action-btn" data-act="demote" data-uid="${_esc(u.user_id)}" title="Remove admin role">Demote</button>`;
        } else {
          actions += `<button class="ac-um-action-btn" data-act="promote" data-uid="${_esc(u.user_id)}" title="Grant admin role">Make admin</button>`;
        }
      }
      if (!isSelf && !isBuiltinAdmin) {
        actions += `<button class="ac-um-action-btn danger" data-act="delete" data-uid="${_esc(u.user_id)}" data-name="${_esc(u.username)}">Delete</button>`;
      }
      if (!actions) actions = '<span style="color:var(--fg-muted);font-size:11px;">—</span>';

      return `<tr>
        <td>
          <div style="font-weight:600;color:var(--fg-1);">${_esc(u.display_name || u.username)}</div>
          <div style="font-size:10px;color:var(--fg-muted);">${_esc(u.username)}</div>
        </td>
        <td style="text-align:center;">${role}</td>
        <td style="text-align:center;">${status}</td>
        <td style="text-align:right;">${u.session_count || 0}</td>
        <td style="text-align:right;">${u.interaction_count || 0}</td>
        <td style="font-size:10px;color:var(--fg-muted);">${_fmtDate(u.created_at)}</td>
        <td style="font-size:10px;color:var(--fg-muted);">${_fmtDate(u.last_login_at)}</td>
        <td style="text-align:center;white-space:nowrap;">${actions}</td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('.ac-um-action-btn').forEach(btn => {
      btn.addEventListener('click', () => _userAction(btn.dataset.act, btn.dataset.uid, btn.dataset.name));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty" style="color:#f7768e;">Error loading users: ${_esc(e.message)}</td></tr>`;
    if (summary) summary.textContent = 'Error';
  }
}

async function _userAction(act, uid, name) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const requesting = localStorage.getItem('auth_user_id') || '';

  if (act === 'delete') {
    if (!confirm(`Permanently delete user "${name}"? This cannot be undone.`)) return;
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}?requesting_user_id=${encodeURIComponent(requesting)}`), {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert('Delete failed: ' + e.message); return; }
  } else if (act === 'promote' || act === 'demote') {
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}/set-admin`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: requesting, is_admin: act === 'promote' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert(`${act} failed: ` + e.message); return; }
  } else {
    const path = act === 'approve' ? 'approve' : 'revoke';
    try {
      const res = await _fetch(apiPath(`/admin/users/${encodeURIComponent(uid)}/${path}`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: requesting }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) { alert(`${path} failed: ` + e.message); return; }
  }
  _loadUsersList();
}

// ───────────────────────────────────────────────────────────────────────────
// ── Public API ───────────────────────────────────────────────────────────────────
// ───────────────────────────────────────────────────────────────────────────

/** Called once on page load — sets up all event listeners. */
export function initAppConfig() {
  _initNav();
  _initLLM();
  _initIntegrations();
  _initDatabase();
  _initOptimizer();
  _initGit();
  _initAutomation();
  _initEvents();
  _initAppSettings();
  _initUserManagement();
  _initialized = true;
}

/** Called when the App Config tab becomes active — loads fresh data. */
export async function startAppConfig() {
  _active = true;
  // Show the last-active section (or default to agent-settings)
  _showSection(_activeSection || 'agent-settings');

  // Load all sections in parallel (non-blocking)
  _loadLLM();
  _loadIntegrations();
  _loadDatabase();
  _loadOptimizer();
  _loadGit();
  _loadAutomation();
  _loadEvents();
  _loadAppSettings();
  _loadUserManagement();
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 6b: Automation (scheduler provider) ──────────────────────────
// ─────────────────────────────────────────────────────────────────────────
let _schedProviders = [];
let _schedConfig = { provider: 'local', providers: {} };

function _initAutomation() {
  _qs('ac-sched-save')?.addEventListener('click', _saveAutomation);
  _qs('ac-sched-refresh')?.addEventListener('click', _loadAutomationStatus);
  _qs('ac-sched-test')?.addEventListener('click', _testAutomation);
  _qs('ac-sched-sync')?.addEventListener('click', _syncAutomation);
  _qs('ac-sched-provider')?.addEventListener('change', _renderSchedFields);
}

function _findProvider(id) {
  return (_schedProviders || []).find(p => p.id === id) || null;
}

function _renderSchedFields() {
  const provSel = _qs('ac-sched-provider');
  const descEl = _qs('ac-sched-provider-desc');
  const host = _qs('ac-sched-fields');
  if (!provSel || !host) return;
  const provId = provSel.value || 'local';
  const meta = _findProvider(provId);
  if (descEl) descEl.textContent = meta?.description || '';

  host.innerHTML = '';
  const saved = (_schedConfig.providers || {})[provId] || {};

  if (provId === 'local' || !meta || !(meta.fields || []).length) {
    const empty = document.createElement('div');
    empty.className = 'ac-hint';
    empty.style.fontSize = '11px';
    empty.textContent = meta?.fields?.length
      ? ''
      : 'No credentials required.';
    host.appendChild(empty);
    return;
  }

  for (const f of meta.fields) {
    const wrap = document.createElement('div');
    const lbl = document.createElement('label');
    lbl.className = 'ac-label';
    lbl.textContent = f.label + (f.required ? ' *' : '');
    wrap.appendChild(lbl);

    let input;
    if (f.type === 'textarea') {
      input = document.createElement('textarea');
      input.className = 'ac-input';
      input.style.cssText = 'width:100%;min-height:90px;font-family:monospace;font-size:11px;';
    } else {
      input = document.createElement('input');
      input.className = 'ac-input';
      input.type = (f.type === 'password' || f.secret) ? 'password' : 'text';
    }
    input.dataset.key = f.key;
    input.value = saved[f.key] ?? '';
    if (f.placeholder) input.placeholder = f.placeholder;
    wrap.appendChild(input);

    if (f.secret) {
      const hint = document.createElement('div');
      hint.className = 'ac-hint';
      hint.style.cssText = 'font-size:10px;color:#565f89;margin-top:2px;';
      hint.textContent = 'Stored in scheduler_config.json (plaintext).';
      wrap.appendChild(hint);
    }

    host.appendChild(wrap);
  }
}

function _collectSchedSettings() {
  const host = _qs('ac-sched-fields');
  const out = {};
  if (!host) return out;
  host.querySelectorAll('[data-key]').forEach(el => {
    out[el.dataset.key] = el.value || '';
  });
  return out;
}

async function _loadAutomation() {
  await _loadAutomationProviders();
  await _loadAutomationConfig();
  _renderSchedFields();
  await _loadAutomationStatus();
  _loadAutomationCount();
}

async function _loadAutomationCount() {
  const badge = _qs('ac-automation-count-badge');
  if (!badge) return;
  try {
    const r = await _fetch(apiPath('/admin/automations/count'));
    if (r.ok) {
      const d = await r.json();
      badge.textContent = d.count ?? 0;
    }
  } catch (_) {
    // leave badge as-is
  }
}

async function _loadAutomationProviders() {
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/providers'));
    if (!r.ok) return;
    const d = await r.json();
    _schedProviders = d.providers || [];
    const sel = _qs('ac-sched-provider');
    if (sel) {
      sel.innerHTML = '';
      for (const p of _schedProviders) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.display_name;
        sel.appendChild(opt);
      }
    }
  } catch (_) {}
}

async function _loadAutomationConfig() {
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler'));
    if (!r.ok) return;
    const d = await r.json();
    _schedConfig = {
      provider: d.provider || 'local',
      providers: d.providers || {},
    };
    const sel = _qs('ac-sched-provider');
    if (sel) sel.value = _schedConfig.provider;
  } catch (_) {}
}

async function _loadAutomationStatus() {
  const badge = _qs('ac-sched-active-badge');
  const statusEl = _qs('ac-sched-status');
  try {
    const r = await _fetch(apiPath('/admin/scheduler/status'));
    if (r.ok) {
      const d = await r.json();
      const running = !!d.running;
      if (badge) {
        badge.textContent = running ? `${d.provider} · running` : `${d.provider || 'unknown'} · stopped`;
        badge.style.color = running ? '#9ece6a' : '#f7768e';
      }
      if (statusEl) statusEl.textContent = JSON.stringify(d, null, 2);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Error fetching status: ${e.message}`;
  }
}

async function _saveAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _qs('ac-sched-provider')?.value || 'local';
  const settings = _collectSchedSettings();
  const statusEl = _qs('ac-sched-status');
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, settings }),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt}`);
    }
    if (statusEl) statusEl.textContent = 'Saved. Reloading…';
    _schedConfig.provider = provider;
    _schedConfig.providers = { ..._schedConfig.providers, [provider]: settings };
    setTimeout(_loadAutomationStatus, 400);
  } catch (e) {
    if (statusEl) statusEl.textContent = `Save failed: ${e.message}`;
  }
}

async function _testAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _qs('ac-sched-provider')?.value || 'local';
  const settings = _collectSchedSettings();
  const out = _qs('ac-sched-test-result');
  if (out) { out.textContent = 'Testing…'; out.style.color = '#565f89'; }
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/test'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, settings }),
    });
    const d = await r.json();
    if (out) {
      if (d.ok) {
        out.textContent = '✓ ' + (d.message || 'Connection OK');
        out.style.color = '#9ece6a';
      } else {
        out.textContent = '✗ ' + (d.error || 'Failed');
        out.style.color = '#f7768e';
      }
    }
  } catch (e) {
    if (out) {
      out.textContent = `Test failed: ${e.message}`;
      out.style.color = '#f7768e';
    }
  }
}

async function _syncAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const out = _qs('ac-sched-test-result');
  if (out) { out.textContent = 'Pushing jobs…'; out.style.color = '#565f89'; }
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/sync'), { method: 'POST' });
    const d = await r.json();
    if (out) {
      if (d.ok) {
        out.textContent = '✓ Jobs pushed';
        out.style.color = '#9ece6a';
      } else {
        out.textContent = '✗ ' + (d.error || 'Sync failed');
        out.style.color = '#f7768e';
      }
    }
    setTimeout(_loadAutomationStatus, 400);
  } catch (e) {
    if (out) {
      out.textContent = `Sync failed: ${e.message}`;
      out.style.color = '#f7768e';
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── Event Sources (admin panel) ──────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

function _initEvents() {
  _qs('ac-events-refresh')?.addEventListener('click', _loadEvents);
}

function _adminUserId() {
  return (window.app && app.currentUserId) || localStorage.getItem('webagent_active_user_id') || '';
}

async function _loadEvents() {
  await Promise.all([
    _loadEventsRuntime(),
    _loadEventsSources(),
    _loadEventsDeliveries(),
  ]);
}

async function _loadEventsRuntime() {
  const badge = _qs('ac-events-runtime-badge');
  const info  = _qs('ac-events-runtime-info');
  if (!badge || !info) return;
  badge.textContent = 'loading…';
  try {
    const r = await fetch(`/admin/events/runtime-status?requesting_user_id=${encodeURIComponent(_adminUserId())}`);
    if (!r.ok) {
      badge.textContent = (r.status === 403) ? 'admin only' : 'error';
      badge.style.color = '#f7768e';
      info.textContent = '';
      return;
    }
    const d = await r.json();
    const both = d.poller_running && d.renewer_running;
    badge.textContent = both ? '✓ running' : (d.poller_running || d.renewer_running ? 'partial' : '✗ stopped');
    badge.style.color = both ? '#9ece6a' : '#e0af68';
    info.innerHTML =
      `poller: <strong style="color:${d.poller_running ? '#9ece6a' : '#f7768e'};">${d.poller_running ? 'running' : 'stopped'}</strong> · ` +
      `renewer: <strong style="color:${d.renewer_running ? '#9ece6a' : '#f7768e'};">${d.renewer_running ? 'running' : 'stopped'}</strong> · ` +
      `expiring within 24h: <strong>${d.subscriptions_expiring_within_24h}</strong>`;
  } catch (e) {
    badge.textContent = 'error';
    badge.style.color = '#f7768e';
    info.textContent = e.message || '';
  }
}

async function _loadEventsSources() {
  const wrap = _qs('ac-events-sources');
  if (!wrap) return;
  wrap.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:8px;">Loading sources…</div>';
  try {
    const r = await fetch(`/admin/events/sources?requesting_user_id=${encodeURIComponent(_adminUserId())}`);
    if (!r.ok) {
      wrap.innerHTML = `<div style="font-size:12px;color:#f7768e;padding:8px;">${r.status === 403 ? 'Admin access required.' : 'Could not load sources.'}</div>`;
      return;
    }
    const d = await r.json();
    wrap.innerHTML = '';
    if (!d.sources || !d.sources.length) {
      wrap.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:8px;">No event sources registered.</div>';
      return;
    }
    for (const s of d.sources) {
      wrap.appendChild(_renderEventSourceRow(s));
    }
  } catch (e) {
    wrap.innerHTML = `<div style="font-size:12px;color:#f7768e;padding:8px;">Error: ${e.message}</div>`;
  }
}

function _renderEventSourceRow(s) {
  const row = document.createElement('div');
  row.style.cssText = 'border:1px solid var(--border);border-radius:6px;padding:10px 12px;background:var(--bg-0);display:grid;grid-template-columns:1fr auto;gap:10px;';

  const left = document.createElement('div');
  left.style.cssText = 'display:flex;flex-direction:column;gap:4px;min-width:0;';

  const titleRow = document.createElement('div');
  titleRow.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';
  const name = document.createElement('div');
  name.style.cssText = 'font-size:13px;font-weight:600;color:var(--fg-1);';
  name.textContent = s.name;
  titleRow.appendChild(name);
  const enabledBadge = document.createElement('span');
  const enColor = s.enabled ? '#9ece6a' : '#e0af68';
  enabledBadge.style.cssText = `font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:${enColor};border:1px solid ${enColor};border-radius:3px;padding:1px 6px;`;
  enabledBadge.textContent = s.enabled ? 'enabled' : 'disabled';
  titleRow.appendChild(enabledBadge);
  const pushBadge = document.createElement('span');
  pushBadge.style.cssText = 'font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 6px;';
  pushBadge.textContent = s.supports_push ? 'push' : 'poll';
  titleRow.appendChild(pushBadge);
  left.appendChild(titleRow);

  const types = document.createElement('div');
  types.style.cssText = 'font-size:11px;color:var(--accent);font-family:monospace;';
  types.textContent = (s.event_types || []).join(', ');
  left.appendChild(types);

  if (s.description) {
    const desc = document.createElement('div');
    desc.style.cssText = 'font-size:11px;color:var(--muted);';
    desc.textContent = s.description;
    left.appendChild(desc);
  }

  const counts = document.createElement('div');
  counts.style.cssText = 'font-size:11px;color:var(--fg-3);';
  const errPart = (s.subscription_errored ? ` · <span style="color:#f7768e;">${s.subscription_errored} errored</span>` : '');
  counts.innerHTML = `subs: <strong>${s.subscription_enabled}</strong> enabled / ${s.subscription_total} total${errPart}`;
  left.appendChild(counts);

  if (s.env_vars && s.env_vars.length) {
    const env = document.createElement('div');
    env.style.cssText = 'font-size:11px;color:var(--fg-3);font-family:monospace;';
    env.innerHTML = 'env: ' + s.env_vars.map(v =>
      `<span style="color:${v.set ? '#9ece6a' : '#f7768e'};">${v.name}=${v.set ? 'set' : 'unset'}</span>`
    ).join(' · ');
    left.appendChild(env);
  }
  if (s.required_provider) {
    const prov = document.createElement('div');
    prov.style.cssText = 'font-size:11px;color:var(--fg-3);';
    prov.textContent = `OAuth: ${s.required_provider}`;
    left.appendChild(prov);
  }

  row.appendChild(left);

  // Right column: actions
  const right = document.createElement('div');
  right.style.cssText = 'display:flex;flex-direction:column;gap:6px;align-items:flex-end;min-width:160px;';
  const rerunBtn = document.createElement('button');
  rerunBtn.className = 'ac-btn';
  rerunBtn.style.cssText = 'font-size:11px;padding:4px 10px;';
  rerunBtn.textContent = 'Re-register all';
  rerunBtn.title = 'Re-run register_subscription for every enabled subscription on this source. Use after recreating provider infrastructure.';
  const stopBtn = document.createElement('button');
  stopBtn.className = 'ac-btn';
  stopBtn.style.cssText = 'font-size:11px;padding:4px 10px;color:#f7768e;';
  stopBtn.textContent = 'Stop all';
  stopBtn.title = 'Unregister every subscription at the provider. Use before deleting provider infrastructure.';
  const result = document.createElement('div');
  result.style.cssText = 'font-size:11px;color:var(--muted);text-align:right;';

  rerunBtn.addEventListener('click', async () => {
    if (!confirm(`Re-register all ${s.subscription_enabled} enabled subscriptions for ${s.name}?`)) return;
    rerunBtn.textContent = 'Running…';
    rerunBtn.disabled = true;
    try {
      const r = await fetch(`/admin/events/sources/${encodeURIComponent(s.name)}/re-register-all?requesting_user_id=${encodeURIComponent(_adminUserId())}`, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        result.textContent = `✓ ${d.succeeded} ok, ${d.failed} failed`;
        result.style.color = d.failed ? '#e0af68' : '#9ece6a';
      } else {
        result.textContent = `✗ ${d.detail || 'failed'}`;
        result.style.color = '#f7768e';
      }
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.style.color = '#f7768e';
    }
    rerunBtn.textContent = 'Re-register all';
    rerunBtn.disabled = false;
    setTimeout(_loadEventsSources, 600);
  });

  stopBtn.addEventListener('click', async () => {
    const disable = confirm(`Stop all ${s.subscription_total} subscriptions for ${s.name}?\n\nClick OK to also disable the rows (so the renewer won't pick them back up).\nClick Cancel to just unregister at the provider but keep the rows enabled.`);
    stopBtn.textContent = 'Running…';
    stopBtn.disabled = true;
    try {
      const url = `/admin/events/sources/${encodeURIComponent(s.name)}/stop-all?requesting_user_id=${encodeURIComponent(_adminUserId())}&disable_rows=${disable ? 'true' : 'false'}`;
      const r = await fetch(url, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        result.textContent = `✓ ${d.stopped} stopped, ${d.failed} failed${disable ? ' (rows disabled)' : ''}`;
        result.style.color = d.failed ? '#e0af68' : '#9ece6a';
      } else {
        result.textContent = `✗ ${d.detail || 'failed'}`;
        result.style.color = '#f7768e';
      }
    } catch (e) {
      result.textContent = `✗ ${e.message}`;
      result.style.color = '#f7768e';
    }
    stopBtn.textContent = 'Stop all';
    stopBtn.disabled = false;
    setTimeout(_loadEventsSources, 600);
  });

  right.appendChild(rerunBtn);
  right.appendChild(stopBtn);
  right.appendChild(result);
  row.appendChild(right);
  return row;
}

async function _loadEventsDeliveries() {
  const wrap = _qs('ac-events-deliveries');
  if (!wrap) return;
  try {
    const r = await fetch(`/admin/events/deliveries?requesting_user_id=${encodeURIComponent(_adminUserId())}&limit=50`);
    if (!r.ok) {
      wrap.innerHTML = `<div>${r.status === 403 ? 'Admin access required.' : 'Could not load deliveries.'}</div>`;
      return;
    }
    const d = await r.json();
    const rows = d.deliveries || [];
    if (!rows.length) {
      wrap.innerHTML = '<div>(no deliveries yet)</div>';
      return;
    }
    const colorByStatus = {
      ok: '#9ece6a',
      test: '#7aa2f7',
      duplicate: '#565f89',
      pending: '#e0af68',
      error: '#f7768e',
    };
    wrap.innerHTML = rows.map(r => {
      const color = colorByStatus[r.status] || 'var(--fg-3)';
      const errPart = r.error ? `  ${r.error.substr(0, 80)}` : '';
      const t = (r.created_at || '').replace('T', ' ').substr(0, 19);
      return `<div style="margin-bottom:3px;"><span style="color:var(--muted);">${t}</span>  <span style="color:${color};font-weight:600;">${r.status.padEnd(9)}</span> <span style="color:var(--accent);">${r.source}.${r.event_type}</span> evt=${(r.event_external_id || '').substr(0, 36)} sub=${(r.subscription_id || '').substr(0, 8)}${errPart}</div>`;
    }).join('');
  } catch (e) {
    wrap.innerHTML = `<div>Error: ${e.message}</div>`;
  }
}

/** Called when leaving the App Config tab. */
export function stopAppConfig() {
  _active = false;
  if (_intAdminAbort) { try { _intAdminAbort.abort(); } catch (_) {} _intAdminAbort = null; }
  const box = _qs('ac-int-admin-chat-messages');
  if (box) { box.innerHTML = ''; box.style.display = 'none'; }
  const err = _qs('ac-int-admin-chat-error');
  if (err) { err.textContent = ''; err.style.display = 'none'; }
}
