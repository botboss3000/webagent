'use strict';

/**
 * App Config tab — consolidated settings page.
 *
 * Sections:
 *   1. Default LLM       — provider, base URL, API key, model
 *   2. Integrations      — Google OAuth and third-party service connections
 *   3. Database          — cloud / local toggle, display settings
 *   4. Optimizer Stats   — session stats table, run mode, schedule
 *   5. Git Providers     — GitHub token, repo status quick-view
 *   6. App Settings      — global feature toggles (extend LLM to agents, etc.)
 *
 * Note: App Connections (webhooks, Telegram bot config) was removed — managed
 *       via each agent's Connections tab instead.
 */

import { apiPath } from './config.js';
import { app } from './state.js';
import { isAdmin, showRestrictedModal } from './left-login.js';
import { icon } from './icons.js';
import { loadSessionChat, populateSessionSelect } from './sessions.js';
import { loopSessionChanged } from './loop.js';
import { loopVisualSessionChanged } from './loop-logic.js';

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

// Integration Admin chat state
let _intAdminSessionId = null;
let _intAdminAbort = null;
let _intAdminWired = false;

// ─────────────────────────────────────────────────────────────────────────
// ── Sidebar nav + scroll highlighting ────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
const _SECTION_KEY = 'appConfig_activeSection';
const _VALID_SECTIONS = ['llm', 'integrations', 'database', 'optimizer', 'git', 'automation', 'events', 'app-settings', 'user-management', 'monetization'];
let _activeSection = localStorage.getItem(_SECTION_KEY) || 'llm';

function _showSection(section) {
  _VALID_SECTIONS.forEach(id => {
    const el = _qs('ac-section-' + id);
    if (el) el.classList.toggle('active', id === section);
  });
  _activeSection = section;
  localStorage.setItem(_SECTION_KEY, section);
  _setNavActive(section);
  if (section === 'monetization') _renderPlatformBillingPanel();
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

  const baseUrlEl = _qs('ac-settings-base-url');
  const scheduleFetch = () => {
    if (_modelFetchDebounce) clearTimeout(_modelFetchDebounce);
    _modelFetchDebounce = setTimeout(() => { _fetchModels(); }, 500);
  };
  apiKeyEl?.addEventListener('input', scheduleFetch);
  baseUrlEl?.addEventListener('input', scheduleFetch);

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
        _uid: ++_parallelUidCounter,
      });
    } else if (apiKey && dupe.api_key !== apiKey) {
      dupe.api_key = apiKey;
    }

    await _saveParallelProviders();
    _showLLMStatus(data.message || 'Saved', 'success');
    await _loadLLM();
    _renderParallelRows();
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

function _initIntegrations() {
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
  const abilities = ['codebase_admin', 'create_tools', 'automation'];
  for (const a of abilities) {
    _initCollapsible(a);
    _qs(`ac-int-${a}-save`)?.addEventListener('click', () => _enableAbility(a));
    _qs(`ac-int-${a}-unconfigure`)?.addEventListener('click', () => _disableAbility(a));
  }

  _initIntegrationsSearch([...providers, ...genericProviders, ...channels, ...comingSoonChannels, ...abilities]);
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
  input.value = '';
  _intAdminSetBusy(true);
  _intAdminAppendBubble('user', text);
  const agentBubble = _intAdminAppendBubble('agent', '…');
  let agentBuffer = '';

  const payload = {
    message: text,
    session_id: _intAdminGetSession(),
    user_id: app.currentUserId,
    agent_template_id: 'integration-admin-agent',
  };

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
  sync();
  _intAdminWired = true;
}

function _initIntegrationsSearch(providers) {
  const input = _qs('ac-int-search');
  if (!input) return;
  const emptyEl = _qs('ac-int-search-empty');
  const cards = providers
    .map(p => {
      const card = document.getElementById(`ac-int-${p}-card`);
      if (!card) return null;
      const header = card.querySelector(':scope > div');
      const haystack = (p + ' ' + (header ? header.textContent : '')).toLowerCase();
      return { card, haystack };
    })
    .filter(Boolean);

  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let visible = 0;
    for (const { card, haystack } of cards) {
      const match = !q || haystack.includes(q);
      card.style.display = match ? '' : 'none';
      if (match) visible++;
    }
    if (emptyEl) emptyEl.style.display = (q && visible === 0) ? 'block' : 'none';
  };
  input.addEventListener('input', apply);
}

async function _loadIntegrations() {
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
    _applyAbilityStatus('codebase_admin', data.codebase_admin_configured);
    _applyAbilityStatus('create_tools',   data.create_tools_configured);
    _applyAbilityStatus('automation',     data.automation_configured);
    // Browser session is per-user — fetched from a separate endpoint.
    _loadBrowserSessionStatus();
  } catch (e) {
    for (const p of ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon', 'scraper', 'browser_session', 'telegram', 'codebase_admin', 'create_tools', 'automation']) {
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
}

async function _enableAbility(ability) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (statusEl) statusEl.style.display = 'none';
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
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
      body: JSON.stringify({ extend_llm_to_agents: cb ? cb.checked : true }),
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

  // Tutorial-hint preferences card. Lazy-import so the user-management
  // section doesn't fail to render if tutorial.js is unavailable for any
  // reason. Re-renders on its own when prefs change.
  const host = _qs('ac-tutorial-card-host');
  if (host) {
    import('./tutorial.js').then(m => {
      try { m.renderTutorialPrefsCard(host); } catch (_) {}
      document.addEventListener('tutorial-prefs-changed', () => {
        try { m.renderTutorialPrefsCard(host); } catch (_) {}
      });
    }).catch(() => {});
  }
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

  // 2. Users list — only if admin
  if (isAdmin()) {
    await _loadUsersList();
  } else {
    const tbody = _qs('ac-um-users-tbody');
    if (tbody) tbody.innerHTML = `<tr><td colspan="8" class="ac-table-empty">Admin access required to view users.</td></tr>`;
    const sum = _qs('ac-um-users-summary');
    if (sum) sum.textContent = 'Admin only';
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
  // Show the last-active section (or default to llm)
  _showSection(_activeSection || 'llm');

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
