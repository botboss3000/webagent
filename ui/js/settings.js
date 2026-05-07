'use strict';

import { apiPath } from './config.js';

/**
 * Settings module — provider and API key configuration.
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
const SETTINGS_STATUS = document.getElementById('settings-status');

const providerNames = {
    openrouter: 'OpenRouter',
    openai: 'OpenAI',
};

export function initSettings() {
    if (!SETTINGS_BTN) return; // Not on this page

    SETTINGS_BTN.addEventListener('click', openSettings);
    SETTINGS_CLOSE.addEventListener('click', closeSettings);
    SETTINGS_BACKDROP.addEventListener('click', closeSettings);
    SETTINGS_SAVE.addEventListener('click', saveSettings);

    // Load current config when modal opens
    SETTINGS_BTN.addEventListener('click', loadSettings);
}

async function openSettings() {
    SETTINGS_MODAL.style.display = 'block';
    await loadSettings();
}

function closeSettings() {
    SETTINGS_MODAL.style.display = 'none';
    SETTINGS_STATUS.style.display = 'none';
}

async function loadSettings() {
    try {
        const res = await fetch(apiPath('/admin/settings/provider'));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        SETTINGS_PROVIDER.value = data.provider || 'openrouter';
        // Show masked key as placeholder
        if (data.api_key && data.api_key.length > 0) {
            SETTINGS_API_KEY.placeholder = data.api_key;
            SETTINGS_API_KEY.value = '';
        } else {
            SETTINGS_API_KEY.placeholder = 'sk-...';
        }
    } catch (e) {
        console.error('Failed to load settings:', e);
    }
}

async function saveSettings() {
    const provider = SETTINGS_PROVIDER.value;
    const apiKey = SETTINGS_API_KEY.value.trim();

    if (!apiKey) {
        showStatus('Please enter an API key', 'error');
        return;
    }

    try {
        const res = await fetch(apiPath('/admin/settings/provider'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, api_key: apiKey }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        showStatus(`✅ ${data.message}`, 'success');
        SETTINGS_API_KEY.value = '';
        // Reload to show masked key
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
