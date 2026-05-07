'use strict';

/**
 * Settings module — provider, base URL, API key, and model configuration.
 * Per-provider key+model persist across provider switches.
 */

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

const MASKED_PLACEHOLDER = '*******************************************';
let allModels = [];
let selectedModel = '';
let keyHasBeenModified = false;
let providerPresets = {};

// Per-provider config map: { openrouter: {api_key: "...", model: "..."}, ... }
// Updated on provider switch and on save.
let providerConfigs = {};
let currentProvider = 'openrouter';

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
            openSettings();
        });
    }

    if (SETTINGS_CLOSE) SETTINGS_CLOSE.addEventListener('click', closeSettings);
    if (SETTINGS_BACKDROP) SETTINGS_BACKDROP.addEventListener('click', closeSettings);

    SETTINGS_SAVE.addEventListener('click', saveSettings);
    SETTINGS_CLEAR.addEventListener('click', clearSettings);

    SETTINGS_API_KEY.addEventListener('input', () => {
        const val = SETTINGS_API_KEY.value;
        keyHasBeenModified = !(val === MASKED_PLACEHOLDER || val === '');
    });

    // Provider switch: save current to map, load new from map
    SETTINGS_PROVIDER.addEventListener('change', () => {
        // Save current provider's key+model to map before switching
        const prevProvider = currentProvider;
        saveCurrentToMap(prevProvider);

        const newProv = SETTINGS_PROVIDER.value;
        currentProvider = newProv;

        // Auto-fill base URL
        if (providerPresets[newProv]) {
            SETTINGS_BASE_URL.value = providerPresets[newProv].base_url;
        } else if (newProv === '_custom') {
            SETTINGS_BASE_URL.value = '';
            SETTINGS_BASE_URL.placeholder = 'https://your-endpoint.com/v1';
        }

        // Load saved key+model for new provider from map (or blank)
        const saved = providerConfigs[newProv];
        if (saved && saved.api_key) {
            SETTINGS_API_KEY.value = MASKED_PLACEHOLDER;
            SETTINGS_API_KEY.placeholder = '';
            keyHasBeenModified = false;
        } else {
            SETTINGS_API_KEY.value = '';
            SETTINGS_API_KEY.placeholder = 'sk-...';
            keyHasBeenModified = false;
        }

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
    let apiKey = SETTINGS_API_KEY.value;
    if (apiKey === MASKED_PLACEHOLDER) {
        // Preserve the key from the stored map (we can't read masked value)
        const existing = providerConfigs[providerKey];
        apiKey = existing ? existing.api_key || '' : '';
    }
    providerConfigs[providerKey] = {
        api_key: apiKey,
        model: selectedModel,
    };
}

async function fetchProviderPresets() {
    try {
        const res = await fetch('/admin/settings/providers');
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
}

function closeSettings() {
    if (SETTINGS_MODAL) SETTINGS_MODAL.style.display = 'none';
    SETTINGS_STATUS.style.display = 'none';
    SETTINGS_MODEL_DROPDOWN.style.display = 'none';
}

async function loadSettings() {
    try {
        const res = await fetch('/admin/settings/provider');
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

        // API key — show masked if saved, blank otherwise
        if (data.api_key && data.api_key.length > 0) {
            SETTINGS_API_KEY.value = MASKED_PLACEHOLDER;
            SETTINGS_API_KEY.placeholder = '';
        } else {
            SETTINGS_API_KEY.value = '';
            SETTINGS_API_KEY.placeholder = 'sk-...';
        }

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
        const res = await fetch(`/admin/settings/models?provider=${encodeURIComponent(provider)}`);
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
    let apiKey = '';
    if (keyHasBeenModified) {
        apiKey = SETTINGS_API_KEY.value.trim();
        if (!apiKey) {
            showStatus('Please enter an API key', 'error');
            return;
        }
    } else {
        // Not modified — preserve from map
        const saved = providerConfigs[provider];
        if (saved && saved.api_key) {
            apiKey = saved.api_key;
        }
    }

    // Update current provider in map
    providerConfigs[provider] = {
        api_key: apiKey,
        model: selectedModel,
    };

    try {
        const res = await fetch('/admin/settings/provider', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider,
                base_url: baseUrl,
                api_key: apiKey,
                model: selectedModel,
                providers: providerConfigs,
            }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        showStatus(`✅ ${data.message}`, 'success');
        keyHasBeenModified = false;
        await loadSettings();
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
            const res = await fetch('/admin/settings/provider/clear', {
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
            const res = await fetch('/admin/settings/provider', {
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

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}
