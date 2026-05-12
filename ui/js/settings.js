'use strict';

import { apiPath } from './config.js';
import { isAdmin, showRestrictedModal } from './left-login.js';

/**
 * Settings module — provider, base URL, API key, and model configuration.
 * Per-provider key+model persist across provider switches.
 * Each user (anonymous or authenticated) has their own isolated provider config.
 */

/** Wrap fetch with auth token if available (per-user settings isolation). */
function _authFetch(url, opts = {}) {
  const token = localStorage.getItem('auth_token');
  if (token) {
    opts.headers = { ...(opts.headers || {}) };
    opts.headers['Authorization'] = `Bearer ${token}`;
  }
  return fetch(url, opts);
}

const SETTINGS_MENU_BTN = document.getElementById('settings-menu-btn');
const SETTINGS_DROPDOWN_MENU = document.getElementById('settings-dropdown-menu');
const LLM_MENU_ITEM = document.getElementById('llm-menu-item');
const SETTINGS_MODAL = document.getElementById('settings-modal');
const SETTINGS_CLOSE = document.getElementById('settings-close');
const SETTINGS_BACKDROP = document.getElementById('settings-backdrop');
const SETTINGS_PROVIDER = document.getElementById('settings-provider');
const SETTINGS_BASE_URL = document.getElementById('settings-base-url');
const SETTINGS_API_KEY = document.getElementById('settings-api-key');
const SETTINGS_SAVE = document.getElementById('settings-save');
const SETTINGS_CLEAR = document.getElementById('settings-clear');
const SETTINGS_STATUS = document.getElementById('settings-status');
const SETTINGS_MODEL_GROUP = document.getElementById('settings-model-group');
const SETTINGS_MODEL_SEARCH = document.getElementById('settings-model-search');
const SETTINGS_MODEL_DROPDOWN = document.getElementById('settings-model-dropdown');
const SETTINGS_MODEL_STATUS = document.getElementById('settings-model-status');

// ── Parallel Providers DOM refs ──
const PARALLEL_ROWS = document.getElementById('settings-saved-list');

// No longer masking API keys — always show plaintext
let allModels = [];
let selectedModel = '';
let keyHasBeenModified = false;
let providerPresets = {};

// Per-provider config map: { openrouter: {api_key: "...", model: "..."}, ... }
// Updated on provider switch and on save.
let providerConfigs = {};
let currentProvider = 'openrouter';

// ── Parallel Providers state ──
let parallelProviders = []; // [{provider, base_url, api_key, model, enabled, _uid}, ...]
let parallelUidCounter = 0;

export function initSettings() {
    if (!SETTINGS_MENU_BTN) return;

    SETTINGS_MENU_BTN.addEventListener('click', (e) => {
        e.stopPropagation();
        const isHidden = SETTINGS_DROPDOWN_MENU.style.display === 'none' || SETTINGS_DROPDOWN_MENU.style.display === '';
        if (isHidden) {
            SETTINGS_DROPDOWN_MENU.style.display = 'block';
        } else {
            SETTINGS_DROPDOWN_MENU.style.display = 'none';
        }
    });

    if (LLM_MENU_ITEM) {
        LLM_MENU_ITEM.addEventListener('click', () => {
            SETTINGS_DROPDOWN_MENU.style.display = 'none';
            if (!isAdmin()) {
                showRestrictedModal();
                return;
            }
            openSettings();
        });
    }

    if (SETTINGS_CLOSE) SETTINGS_CLOSE.addEventListener('click', closeSettings);
    if (SETTINGS_BACKDROP) SETTINGS_BACKDROP.addEventListener('click', closeSettings);

    SETTINGS_SAVE.addEventListener('click', saveSettings);
    SETTINGS_CLEAR.addEventListener('click', clearSettings);

    SETTINGS_API_KEY.addEventListener('input', () => {
        keyHasBeenModified = SETTINGS_API_KEY.value !== '';
    });

    // Provider switch: save current to map, load new from map
    SETTINGS_PROVIDER.addEventListener('change', () => {
        // Save current provider's state to map before switching
        const prevProvider = currentProvider;
        saveCurrentToMap(prevProvider);

        const newProv = SETTINGS_PROVIDER.value;
        currentProvider = newProv;

        const saved = providerConfigs[newProv];

        // Load base URL: saved value > preset value > blank
        if (saved && saved.base_url) {
            SETTINGS_BASE_URL.value = saved.base_url;
        } else if (providerPresets[newProv]) {
            SETTINGS_BASE_URL.value = providerPresets[newProv].base_url;
        } else if (newProv === '_custom') {
            SETTINGS_BASE_URL.value = '';
            SETTINGS_BASE_URL.placeholder = 'https://your-endpoint.com/v1';
        }

        // Always blank out API key on switch as requested
        SETTINGS_API_KEY.value = '';
        SETTINGS_API_KEY.placeholder = 'sk-...';
        keyHasBeenModified = false;

        selectedModel = (saved && saved.model) || '';
        SETTINGS_MODEL_SEARCH.value = selectedModel;
        SETTINGS_MODEL_STATUS.textContent = selectedModel ? `Selected: ${selectedModel}` : '';
        SETTINGS_MODEL_STATUS.style.color = selectedModel ? '#9ece6a' : '#565f89';
        allModels = [];
        SETTINGS_MODEL_DROPDOWN.style.display = 'none';
        fetchAndRenderModels();
    });

    SETTINGS_MODEL_SEARCH.addEventListener('focus', () => {
        renderModelDropdown(SETTINGS_MODEL_SEARCH.value.toLowerCase().trim());
    });
    SETTINGS_MODEL_SEARCH.addEventListener('input', () => {
        const q = SETTINGS_MODEL_SEARCH.value.toLowerCase().trim();
        renderModelDropdown(q);
    });

    document.addEventListener('click', (e) => {
        if (!e.target.closest('.settings-dropdown')) {
            SETTINGS_DROPDOWN_MENU.style.display = 'none';
        }
        if (!SETTINGS_MODEL_GROUP.contains(e.target)) {
            SETTINGS_MODEL_DROPDOWN.style.display = 'none';
        }
    });

    fetchProviderPresets();
}

function saveCurrentToMap(providerKey) {
    if (!providerKey || providerKey === '_custom') return;
    providerConfigs[providerKey] = {
        api_key: SETTINGS_API_KEY.value,
        model: selectedModel,
        base_url: SETTINGS_BASE_URL.value.trim(),
    };
}

async function fetchProviderPresets() {
    try {
        const res = await _authFetch('/admin/settings/providers');
        if (!res.ok) return;
        providerPresets = await res.json();

        const select = SETTINGS_PROVIDER;
        select.innerHTML = '';
        for (const [key, preset] of Object.entries(providerPresets)) {
            const opt = document.createElement('option');
            opt.value = key;
            opt.textContent = preset.name;
            select.appendChild(opt);
        }
        const custom = document.createElement('option');
        custom.value = '_custom';
        custom.textContent = 'Custom';
        select.appendChild(custom);
    } catch (e) {
        console.error('Failed to load provider presets:', e);
    }
}

async function openSettings() {
    SETTINGS_MODAL.style.display = 'block';
    keyHasBeenModified = false;
    await loadSettings();
    await loadMultiProviders();
    renderParallelRows();
}

function closeSettings() {
    if (SETTINGS_MODAL) SETTINGS_MODAL.style.display = 'none';
    SETTINGS_STATUS.style.display = 'none';
    SETTINGS_MODEL_DROPDOWN.style.display = 'none';
    if (PARALLEL_FORM_MODEL_DROPDOWN) PARALLEL_FORM_MODEL_DROPDOWN.style.display = 'none';
    // Reset parallel state for next open
    parallelProviders = [];
}

async function loadSettings() {
    try {
        const res = await _authFetch(apiPath('/admin/settings/provider'));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        // Store the full providers map from server
        providerConfigs = data.providers || {};
        currentProvider = data.provider || 'openrouter';

        // Provider dropdown
        const providerKey = providerConfigs[currentProvider] ? currentProvider : (currentProvider || 'openrouter');
        if (providerPresets[providerKey]) {
            SETTINGS_PROVIDER.value = providerKey;
            currentProvider = providerKey;
            SETTINGS_BASE_URL.value = data.base_url || providerPresets[providerKey].base_url;
        } else {
            SETTINGS_PROVIDER.value = '_custom';
            currentProvider = '_custom';
            SETTINGS_BASE_URL.value = data.base_url || '';
        }

        // API key — show plaintext always
        SETTINGS_API_KEY.value = data.api_key || '';
        SETTINGS_API_KEY.placeholder = 'sk-...';

        // Model
        selectedModel = data.model || '';
        SETTINGS_MODEL_SEARCH.value = selectedModel;
        if (selectedModel) {
            SETTINGS_MODEL_STATUS.textContent = `Selected: ${selectedModel}`;
            SETTINGS_MODEL_STATUS.style.color = '#9ece6a';
        } else {
            SETTINGS_MODEL_STATUS.textContent = '';
        }

        fetchAndRenderModels();
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function fetchAndRenderModels() {
    SETTINGS_MODEL_STATUS.textContent = 'Loading models...';
    SETTINGS_MODEL_STATUS.style.color = '#565f89';
    const provider = SETTINGS_PROVIDER.value === '_custom' ? '' : SETTINGS_PROVIDER.value;
    try {
        const res = await _authFetch(`/admin/settings/models?provider=${encodeURIComponent(provider)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.error) {
            if (data.error === 'No API key configured') {
                SETTINGS_MODEL_STATUS.textContent = 'Save an API key first to see available models.';
                SETTINGS_MODEL_STATUS.style.color = '#565f89';
            } else {
                SETTINGS_MODEL_STATUS.textContent = `Error: ${data.error}`;
                SETTINGS_MODEL_STATUS.style.color = '#f7768e';
            }
            allModels = [];
            SETTINGS_MODEL_DROPDOWN.style.display = 'none';
            return;
        }
        allModels = data.models || [];
        if (allModels.length === 0) {
            SETTINGS_MODEL_STATUS.textContent = 'No models available.';
            SETTINGS_MODEL_STATUS.style.color = '#565f89';
        } else {
            SETTINGS_MODEL_STATUS.textContent = `${allModels.length} models available. Type to filter.`;
            SETTINGS_MODEL_STATUS.style.color = '#565f89';
        }
    } catch (e) {
        SETTINGS_MODEL_STATUS.textContent = `Failed to load models: ${e.message}`;
        SETTINGS_MODEL_STATUS.style.color = '#f7768e';
        allModels = [];
    }
}

function renderModelDropdown(filter) {
    const dropdown = SETTINGS_MODEL_DROPDOWN;
    if (allModels.length === 0) {
        dropdown.style.display = 'none';
        return;
    }

    const filtered = filter
        ? allModels.filter(m =>
            m.id.toLowerCase().includes(filter) ||
            m.name.toLowerCase().includes(filter)
          )
        : allModels;

    if (filtered.length === 0) {
        dropdown.style.display = 'none';
        return;
    }

    dropdown.innerHTML = '';
    dropdown.style.display = 'block';

    const slice = filtered.slice(0, 200);
    for (const m of slice) {
        const item = document.createElement('div');
        item.className = 'model-dropdown-item';
        item.dataset.modelId = m.id;
        if (m.id === selectedModel) {
            item.style.background = '#2a2a4a';
        }
        item.innerHTML = `<span style="font-weight:500;">${escapeHtml(m.id)}</span> <span style="color:#565f89;font-size:11px;margin-left:6px;">${escapeHtml(m.name)}</span>`;
        item.addEventListener('click', () => {
            selectModel(m.id);
        });
        dropdown.appendChild(item);
    }

    const selectedEl = dropdown.querySelector(`[data-model-id="${selectedModel}"]`);
    if (selectedEl) {
        selectedEl.scrollIntoView({ block: 'nearest' });
    }
}

function selectModel(modelId) {
    selectedModel = modelId;
    SETTINGS_MODEL_SEARCH.value = modelId;
    SETTINGS_MODEL_DROPDOWN.style.display = 'none';
    SETTINGS_MODEL_STATUS.textContent = `Selected: ${modelId}`;
    SETTINGS_MODEL_STATUS.style.color = '#9ece6a';
}

async function saveSettings() {
    const provider = currentProvider === '_custom' ? 'custom' : currentProvider;
    const baseUrl = SETTINGS_BASE_URL.value.trim();

    if (!baseUrl) {
        showStatus('Please enter a base URL', 'error');
        return;
    }

    // Read current api_key from form
    let apiKey = SETTINGS_API_KEY.value.trim();

    if (!apiKey) {
        showStatus('Please enter an API key', 'error');
        return;
    }

    if (!selectedModel) {
        showStatus('Please select a model', 'error');
        return;
    }

    // Add or update in parallel list
    let existingIndex = parallelProviders.findIndex(p => p.provider === provider && p.model === selectedModel);
    if (existingIndex >= 0) {
        parallelProviders[existingIndex].api_key = apiKey;
        parallelProviders[existingIndex].base_url = baseUrl;
    } else {
        parallelProviders.push({
            provider: provider,
            base_url: baseUrl,
            api_key: apiKey,
            model: selectedModel,
            enabled: true,
            rating: 0,
            _uid: _nextUid()
        });
    }

    // Update current provider in map
    providerConfigs[provider] = {
        api_key: apiKey,
        model: selectedModel,
    };

    const payload = {
        provider,
        base_url: baseUrl,
        api_key: apiKey,
        model: selectedModel,
        providers: providerConfigs,
    };

    try {
        const res = await _authFetch(apiPath('/admin/settings/provider'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        // Also save parallel providers config
        await saveMultiProviders();

        showStatus(`✅ ${data.message}`, 'success');
        keyHasBeenModified = false;
        
        // Refresh UI
        await loadSettings();
        await loadMultiProviders();
        renderParallelRows();
        
        // Clear input form slightly for next entry, but keep provider URL combo
        SETTINGS_MODEL_SEARCH.value = '';
        selectedModel = '';
        SETTINGS_MODEL_STATUS.textContent = '';
        
    } catch (e) {
        showStatus(`❌ Error: ${e.message}`, 'error');
    }
}

async function clearSettings() {
    // Clear current provider from map
    const provider = currentProvider === '_custom' ? 'custom' : currentProvider;
    delete providerConfigs[provider];

    // If no providers left, make a clean reset
    if (Object.keys(providerConfigs).length === 0) {
        try {
            const res = await _authFetch('/admin/settings/provider/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            showStatus(`🗑️ ${data.message}`, 'success');
        } catch (e) {
            showStatus(`❌ Error: ${e.message}`, 'error');
        }
    } else {
        // Save updated map (without current provider)
        const baseUrl = SETTINGS_BASE_URL.value.trim();
        try {
            const res = await _authFetch('/admin/settings/provider', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    provider: currentProvider === '_custom' ? 'custom' : currentProvider,
                    base_url: baseUrl,
                    api_key: '',
                    model: '',
                    providers: providerConfigs,
                }),
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            showStatus(`🗑️ Cleared ${currentProvider} settings`, 'success');
        } catch (e) {
            showStatus(`❌ Error: ${e.message}`, 'error');
        }
    }

    selectedModel = '';
    allModels = [];
    keyHasBeenModified = false;
    await loadSettings();
}

function showStatus(msg, type) {
    SETTINGS_STATUS.textContent = msg;
    SETTINGS_STATUS.style.color = type === 'error' ? '#f7768e' : '#9ece6a';
    SETTINGS_STATUS.style.display = 'block';
    setTimeout(() => {
        SETTINGS_STATUS.style.display = 'none';
    }, 4000);
}

// ══════════════════════════════════════════════
// ── Parallel Multi-Provider Functions ──
// ══════════════════════════════════════════════

function _nextUid() {
    return ++parallelUidCounter;
}

function _maskKey(key) {
    if (!key || key.length < 12) return key ? '***' : '';
    return key.slice(0, 8) + '...' + key.slice(-4);
}

async function loadMultiProviders() {
    try {
        const res = await _authFetch('/admin/settings/multi-providers');
        if (!res.ok) return;
        const data = await res.json();
        parallelProviders = (data.providers || []).map(p => ({
            provider: p.provider || 'openrouter',
            base_url: p.base_url || '',
            api_key: p.api_key || '',
            model: p.model || '',
            enabled: p.enabled !== false,
            rating: p.rating || 0,
            _uid: _nextUid(),
        }));
    } catch (e) {
        console.error('Failed to load multi-providers:', e);
        parallelProviders = [];
    }
}

async function saveMultiProviders() {
    try {
        const payload = {
            parallel_mode: parallelProviders.filter(p => p.enabled).length >= 2,
            providers: parallelProviders.map(p => ({
                provider: p.provider === '_custom' ? 'custom' : p.provider,
                base_url: p.base_url,
                api_key: p.api_key,
                model: p.model,
                enabled: p.enabled,
                rating: p.rating || 0,
            })),
        };
        const res = await _authFetch('/admin/settings/multi-providers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
        console.error('Failed to save multi-providers:', e);
    }
}

function renderParallelRows() {
    if (!PARALLEL_ROWS) return;
    PARALLEL_ROWS.innerHTML = '';

    if (parallelProviders.length === 0) {
        return; // Nothing saved yet
    }

    // Title label for the list
    const label = document.createElement('div');
    label.textContent = "Saved Configurations";
    label.style.cssText = 'color:#a9b1d6;font-size:13px;font-weight:600;margin-top:12px;';
    PARALLEL_ROWS.appendChild(label);
    
    // Sub-label info
    const sub = document.createElement('div');
    sub.textContent = "Check multiple rows below to send requests in parallel (fastest responds)";
    sub.style.cssText = 'font-size:10px;color:#565f89;margin-bottom:8px;';
    PARALLEL_ROWS.appendChild(sub);

    const enabledCount = parallelProviders.filter(p => p.enabled).length;

    for (let i = 0; i < parallelProviders.length; i++) {
        const p = parallelProviders[i];
        const row = document.createElement('div');
        row.className = 'parallel-provider-row';
        row.dataset.uid = p._uid;
        row.style.cssText = 'display:flex;align-items:center;gap:8px;background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;padding:8px 10px;';

        // Checkbox
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = p.enabled;
        cb.title = "Use in parallel";
        cb.style.cssText = 'width:16px;height:16px;accent-color:#7dcfff;cursor:pointer;flex-shrink:0;';
        cb.addEventListener('change', () => {
            p.enabled = cb.checked;
            // Auto-save logic so checkboxes persist immediately without "Save"
            saveMultiProviders().then(() => {
                const countEl = document.getElementById('settings-parallel-enabled-count');
                if (countEl) {
                    const ec = parallelProviders.filter(x => x.enabled).length;
                    countEl.textContent = `${ec} checked (need 2+ for parallel)`;
                    countEl.style.color = ec < 2 ? '#f7768e' : '#565f89';
                }
            });
        });
        row.appendChild(cb);
        
        // Model string
        const modelSpan = document.createElement('span');
        modelSpan.textContent = p.model || '-';
        modelSpan.style.cssText = 'font-size:12px;color:#c0caf5;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500;';
        row.appendChild(modelSpan);

        // Rating
        const ratingSpan = document.createElement('span');
        const rColor = (p.rating || 0) < 0 ? '#f7768e' : (p.rating || 0) > 0 ? '#9ece6a' : '#565f89';
        ratingSpan.textContent = `★ ${p.rating || 0}`;
        ratingSpan.title = 'Current performance score (auto-updates)';
        ratingSpan.style.cssText = `font-size:11px;color:${rColor};font-weight:600;min-width:30px;text-align:right;margin-right:6px;`;
        row.appendChild(ratingSpan);

        // Remove button
        const removeBtn = document.createElement('button');
        removeBtn.textContent = '✕';
        removeBtn.title = 'Remove saved config';
        removeBtn.style.cssText = 'background:none;border:none;color:#565f89;cursor:pointer;font-size:12px;padding:2px 6px;border-radius:4px;flex-shrink:0;';
        removeBtn.addEventListener('mouseenter', () => { removeBtn.style.background = 'rgba(251,73,52,0.15)'; removeBtn.style.color = '#fb4934'; });
        removeBtn.addEventListener('mouseleave', () => { removeBtn.style.background = 'none'; removeBtn.style.color = '#565f89'; });
        removeBtn.addEventListener('click', async () => {
            removeParallelRow(p._uid);
            await saveMultiProviders();
        });
        row.appendChild(removeBtn);

        PARALLEL_ROWS.appendChild(row);
    }

    // Show enabled count
    const countEl = document.createElement('div');
    countEl.id = 'settings-parallel-enabled-count';
    countEl.style.cssText = 'font-size:10px;text-align:right;margin-top:4px;';
    countEl.textContent = `${enabledCount} checked (need 2+ for parallel)`;
    countEl.style.color = enabledCount < 2 ? '#f7768e' : '#565f89';
    PARALLEL_ROWS.appendChild(countEl);
}

async function removeParallelRow(uid) {
    parallelProviders = parallelProviders.filter(p => p._uid !== uid);
    renderParallelRows();
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
