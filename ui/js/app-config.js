'use strict';

/**
 * App Config tab — consolidated settings page.
 *
 * Sections:
 *   1. Default LLM       — provider, base URL, API key, model
 *   2. App Connections   — webhook base URL, registered webhooks, Telegram status
 *   3. Database          — cloud / local toggle, display settings
 *   4. Optimizer Stats   — session stats table, run mode, schedule
 *   5. Git Providers     — GitHub token, repo status quick-view
 */

import { apiPath } from './config.js';
import { app } from './state.js';
import { isAdmin, showRestrictedModal } from './left-login.js';
import { icon } from './icons.js';
import { loadSessionChat, populateSessionSelect } from './sessions.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';
import { streamSessionChanged } from './stream.js';

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

// ─────────────────────────────────────────────────────────────────────────
// ── Sidebar nav + scroll highlighting ────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
let _activeSection = 'llm';

function _showSection(section) {
  const sections = ['llm', 'connections', 'database', 'optimizer', 'git'];
  sections.forEach(id => {
    const el = _qs('ac-section-' + id);
    if (el) el.classList.toggle('active', id === section);
  });
  _activeSection = section;
  _setNavActive(section);
}

function _initNav() {
  const sidebar = _qs('app-config-sidebar');
  if (!sidebar) return;

  sidebar.querySelectorAll('.ac-nav-item').forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      _showSection(link.dataset.section);
    });
  });

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
  document.querySelectorAll('#app-config-sidebar .ac-nav-item').forEach(n => {
    n.classList.toggle('active', n.dataset.section === section);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 1: Default LLM ───────────────────────────────────────────────
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
    _allModels = [];
    const dd = _qs('ac-settings-model-dropdown');
    if (dd) dd.style.display = 'none';
    _fetchModels();
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
  if (statusEl) { statusEl.textContent = 'Loading models…'; statusEl.style.color = '#565f89'; }
  const provider = (_currentProvider === '_custom') ? '' : _currentProvider;
  try {
    const res = await _fetch(apiPath(`/admin/settings/models?provider=${encodeURIComponent(provider)}`));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (data.error) {
      if (statusEl) {
        statusEl.textContent = data.error === 'No API key configured'
          ? 'Save an API key first to see available models.'
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
    item.innerHTML = `<span style="font-weight:500;">${_esc(m.id)}</span> <span style="color:#565f89;font-size:11px;margin-left:6px;">${_esc(m.name)}</span>`;
    item.addEventListener('click', () => {
      _selectedModel = m.id;
      const srch = _qs('ac-settings-model-search');
      if (srch) srch.value = m.id;
      dd.style.display = 'none';
      const statusEl = _qs('ac-settings-model-status');
      if (statusEl) { statusEl.textContent = `Selected: ${m.id}`; statusEl.style.color = '#9ece6a'; }
    });
    dd.appendChild(item);
  });
}

async function _saveLLM() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _currentProvider === '_custom' ? 'custom' : _currentProvider;
  const baseUrl  = _qs('ac-settings-base-url')?.value?.trim() || '';
  const apiKey   = _qs('ac-settings-api-key')?.value?.trim() || '';

  if (!baseUrl)    return _showLLMStatus('Please enter a Base URL', 'error');
  if (!apiKey)     return _showLLMStatus('Please enter an API Key', 'error');
  if (!_selectedModel) return _showLLMStatus('Please select a model', 'error');

  const payload = { provider, base_url: baseUrl, api_key: apiKey, model: _selectedModel, providers: _providerConfigs };
  try {
    const res = await _fetch(apiPath('/admin/settings/provider'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    await _saveParallelProviders();
    _showLLMStatus(data.message || 'Saved', 'success');
    await _loadLLM();
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
  const enabledCount = _parallelProviders.filter(p => p.enabled).length;

  _parallelProviders.forEach(p => {
    const row = document.createElement('div');
    row.className = 'ac-provider-row';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = p.enabled;
    cb.style.cssText = 'width:16px;height:16px;accent-color:var(--accent);cursor:pointer;flex-shrink:0;';
    cb.addEventListener('change', () => {
      p.enabled = cb.checked;
      _saveParallelProviders().then(_renderParallelRows);
    });
    row.appendChild(cb);

    const modelSpan = document.createElement('span');
    modelSpan.textContent = p.model || '—';
    modelSpan.style.cssText = 'flex:1;font-size:12px;color:var(--fg-1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500;';
    row.appendChild(modelSpan);

    const rColor = p.rating < 0 ? '#f7768e' : p.rating > 0 ? '#9ece6a' : '#565f89';
    const ratingSpan = document.createElement('span');
    ratingSpan.style.cssText = `font-size:11px;color:${rColor};min-width:30px;text-align:right;margin-right:6px;`;
    ratingSpan.textContent = `★ ${p.rating || 0}`;
    row.appendChild(ratingSpan);

    const removeBtn = document.createElement('button');
    removeBtn.textContent = '×';
    removeBtn.style.cssText = 'background:none;border:none;color:#565f89;cursor:pointer;font-size:16px;padding:0 4px;flex-shrink:0;line-height:1;';
    removeBtn.addEventListener('mouseenter', () => { removeBtn.style.color = '#f7768e'; });
    removeBtn.addEventListener('mouseleave', () => { removeBtn.style.color = '#565f89'; });
    removeBtn.addEventListener('click', () => {
      _parallelProviders = _parallelProviders.filter(x => x._uid !== p._uid);
      _saveParallelProviders().then(_renderParallelRows);
    });
    row.appendChild(removeBtn);

    list.appendChild(row);
  });

  if (countEl) {
    countEl.textContent = `${enabledCount} checked (need 2+ for parallel)`;
    countEl.style.color = enabledCount < 2 ? '#f7768e' : '#565f89';
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 2: App Connections ───────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
function _initConnections() {
  const saveBtn = _qs('ac-conn-base-url-save');
  saveBtn?.addEventListener('click', _saveWebhookBaseUrl);
}

async function _loadConnections() {
  const listEl    = _qs('ac-conn-webhook-list');
  const baseUrlEl = _qs('ac-conn-base-url');
  const telegramEl = _qs('ac-conn-telegram-status');

  if (listEl) listEl.innerHTML = '<div class="ac-hint">Loading…</div>';

  try {
    const resp = await fetch(apiPath('/admin/webhooks'));
    const data = await resp.json();

    if (baseUrlEl) baseUrlEl.value = data.webhook_base_url || '';

    const baseUrl = data.webhook_base_url || window.location.origin;
    const webhooks = data.webhooks || [];

    if (listEl) {
      if (!webhooks.length) {
        listEl.innerHTML = '<div class="ac-hint">No webhooks registered yet.</div>';
      } else {
        listEl.innerHTML = webhooks.map(w => `
          <div style="padding:8px 10px;background:var(--bg-base,#0d0d1a);border:1px solid var(--border,#2a2a4a);border-radius:6px;font-size:12px;">
            <div style="font-weight:600;color:var(--fg-1,#c0caf5);">${_esc(w.plugin_name || w.webhook_id)}</div>
            <div style="color:var(--fg-muted,#565f89);margin-top:2px;word-break:break-all;">${_esc(baseUrl)}/api/v1/webhooks/${_esc(w.plugin_name || w.webhook_id)}</div>
          </div>`).join('');
      }
    }

    // Telegram status
    if (telegramEl) {
      const tg = (data.plugins || []).find(p => p.name === 'telegram');
      if (tg && tg.configured) {
        telegramEl.innerHTML = `<span style="color:#9ece6a;">✓ Configured</span><span style="color:var(--fg-muted);margin-left:8px;">${_esc(tg.bot_name || '')}</span>`;
      } else {
        telegramEl.innerHTML = '<span style="color:var(--fg-muted,#565f89);">Not configured — use the agent to set up a Telegram bot.</span>';
      }
    }
  } catch (e) {
    if (listEl) listEl.innerHTML = '<div class="ac-hint" style="color:#f7768e;">Failed to load connections.</div>';
  }
}

async function _saveWebhookBaseUrl() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const el = _qs('ac-conn-base-url');
  const statusEl = _qs('ac-conn-base-url-status');
  if (!el) return;
  try {
    const res = await _fetch(apiPath('/admin/webhooks/base-url'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_url: el.value.trim() }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Saved';
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

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 3: Database ──────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
function _initDatabase() {
  _qs('ac-db-seg-cloud')?.addEventListener('click', () => _setDbMode('cloud'));
  _qs('ac-db-seg-local')?.addEventListener('click', () => _setDbMode('local'));

  // Mirror the show-hidden checkbox to the existing DB viewer checkbox
  const acCb = _qs('ac-db-show-hidden');
  const origCb = _qs('db-setting-show-hidden');
  if (acCb && origCb) {
    // Sync initial state
    acCb.checked = origCb.checked;
    acCb.addEventListener('change', () => {
      origCb.checked = acCb.checked;
      origCb.dispatchEvent(new Event('change', { bubbles: true }));
    });
    // Keep in sync if original changes
    origCb.addEventListener('change', () => { acCb.checked = origCb.checked; });
  }
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

  // Sync show-hidden checkbox
  const origCb = _qs('db-setting-show-hidden');
  const acCb   = _qs('ac-db-show-hidden');
  if (origCb && acCb) acCb.checked = origCb.checked;
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
    // Also update the original modal's buttons so they stay in sync
    const origCloud = _qs('modal-seg-cloud');
    const origLocal = _qs('modal-seg-local');
    if (origCloud) origCloud.classList.toggle('active', mode === 'cloud');
    if (origLocal) origLocal.classList.toggle('active', mode === 'local');
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
        streamSessionChanged(sid);
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

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 5: Git Providers ─────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
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
    // Sync to the original GitHub tab token input if present
    const origToken = _qs('gh-token-input');
    if (origToken) origToken.value = '';
    await _loadGit();
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = '#f7768e';
      statusEl.style.display = 'block';
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── Public API ───────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

/** Called once on page load — sets up all event listeners. */
export function initAppConfig() {
  _initNav();
  _initLLM();
  _initConnections();
  _initDatabase();
  _initOptimizer();
  _initGit();
  _initialized = true;
}

/** Called when the App Config tab becomes active — loads fresh data. */
export async function startAppConfig() {
  _active = true;
  // Show the last-active section (or default to llm)
  _showSection(_activeSection || 'llm');

  // Load all sections in parallel (non-blocking)
  _loadLLM();
  _loadConnections();
  _loadDatabase();
  _loadOptimizer();
  _loadGit();
}

/** Called when leaving the App Config tab. */
export function stopAppConfig() {
  _active = false;
}
