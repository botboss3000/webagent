'use strict';

/**
 * Settings module — provider, API key, and model configuration.
 * Saves to backend via /admin/settings/provider endpoint.
 * Settings persist across app restarts.
 * API key is stored on-device only, never sent to third parties.
 */

const SETTINGS_MODAL = document.getElementById('settings-modal');
const SETTINGS_BTN = document.getElementById('settings-btn');
const SETTINGS_CLOSE = document.getElementById('settings-close');
const SETTINGS_BACKDROP = document.getElementById('settings-backdrop');
const SETTINGS_PROVIDER = document.getElementById('settings-provider');
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

const providerNames = {
    openrouter: 'OpenRouter',
    openai: 'OpenAI',
};

export function initSettings() {
    if (!SETTINGS_BTN) return;

    SETTINGS_BTN.addEventListener('click', openSettings);
    SETTINGS_CLOSE.addEventListener('click', closeSettings);
    SETTINGS_BACKDROP.addEventListener('click', closeSettings);
    SETTINGS_SAVE.addEventListener('click', saveSettings);
    SETTINGS_CLEAR.addEventListener('click', clearSettings);

    // Track whether user modified the API key field
    SETTINGS_API_KEY.addEventListener('input', () => {
        const val = SETTINGS_API_KEY.value;
        if (val === MASKED_PLACEHOLDER || val === '') {
            keyHasBeenModified = false;
        } else {
            keyHasBeenModified = true;
        }
    });

    // Model search: show dropdown on focus, filter as user types
    SETTINGS_MODEL_SEARCH.addEventListener('focus', () => {
        renderModelDropdown(SETTINGS_MODEL_SEARCH.value.toLowerCase().trim());
    });
    SETTINGS_MODEL_SEARCH.addEventListener('input', () => {
        const q = SETTINGS_MODEL_SEARCH.value.toLowerCase().trim();
        renderModelDropdown(q);
    });

    // Hide dropdown when clicking outside
    document.addEventListener('click', (e) => {
        if (!SETTINGS_MODEL_GROUP.contains(e.target)) {
            SETTINGS_MODEL_DROPDOWN.style.display = 'none';
        }
    });

    // Show model section for any provider that has model selection
    SETTINGS_PROVIDER.addEventListener('change', () => {
        SETTINGS_MODEL_GROUP.style.display = 'block';
        fetchAndRenderModels();
    });
}

async function openSettings() {
    SETTINGS_MODAL.style.display = 'block';
    keyHasBeenModified = false;
    await loadSettings();
}

function closeSettings() {
    SETTINGS_MODAL.style.display = 'none';
    SETTINGS_STATUS.style.display = 'none';
    SETTINGS_MODEL_DROPDOWN.style.display = 'none';
}

async function loadSettings() {
    try {
        const res = await fetch('/admin/settings/provider');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        SETTINGS_PROVIDER.value = data.provider || 'openrouter';

        // API key field
        if (data.api_key && data.api_key.length > 0) {
            SETTINGS_API_KEY.value = MASKED_PLACEHOLDER;
            SETTINGS_API_KEY.placeholder = '';
        } else {
            SETTINGS_API_KEY.value = '';
            SETTINGS_API_KEY.placeholder = 'sk-...';
        }

        // Model selection
        selectedModel = data.model || '';
        SETTINGS_MODEL_GROUP.style.display = 'block';
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
    const provider = SETTINGS_PROVIDER.value;
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
        // Don't render dropdown here — only on user keystroke in search input
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

    // Show first 200 results to avoid perf issues
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

    // Scroll selected model into view
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
    const provider = SETTINGS_PROVIDER.value;
    let apiKey = '';

    if (keyHasBeenModified) {
        apiKey = SETTINGS_API_KEY.value.trim();
        if (!apiKey) {
            showStatus('Please enter an API key', 'error');
            return;
        }
    }
    // If key not modified, send empty string → backend preserves existing key

    try {
        const res = await fetch('/admin/settings/provider', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                provider,
                api_key: apiKey,
                model: selectedModel,
            }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        showStatus(`✅ ${data.message}`, 'success');
        // Reload to show saved state
        keyHasBeenModified = false;
        await loadSettings();
    } catch (e) {
        showStatus(`❌ Error: ${e.message}`, 'error');
    }
}

async function clearSettings() {
    try {
        const res = await fetch('/admin/settings/provider/clear', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        showStatus(`🗑️ ${data.message}`, 'success');
        selectedModel = '';
        allModels = [];
        keyHasBeenModified = false;
        await loadSettings();
    } catch (e) {
        showStatus(`❌ Error: ${e.message}`, 'error');
    }
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
