'use strict';

/**
 * Connections modal — manage webhook base URL and view registered webhooks.
 * Opened from the Config dropdown menu.
 */

import { apiPath } from './config.js';
import { icon } from './icons.js';
import { isAdmin, showRestrictedModal } from './left-login.js';

// ── DOM refs ──
let CONN = {};

function qs(id) { return document.getElementById(id); }

function bindDom() {
  CONN = {
    menuItem: qs('connections-menu-item'),
    modal: qs('connections-modal'),
    backdrop: qs('connections-backdrop'),
    close: qs('connections-close'),
    closeBtn: qs('connections-close-btn'),
    baseUrlInput: qs('conn-base-url'),
    baseUrlSave: qs('conn-base-url-save'),
    baseUrlStatus: qs('conn-base-url-status'),
    webhookList: qs('conn-webhook-list'),
    telegramStatus: qs('conn-telegram-status'),
    dropdown: qs('settings-dropdown-menu'),
  };
}

// ── Open / Close ──

function openConnections() {
  if (CONN.modal) {
    CONN.modal.style.display = 'block';
    loadConnections();
  }
}

function closeConnections() {
  if (CONN.modal) CONN.modal.style.display = 'none';
}

// ── Load data ──

async function loadConnections() {
  if (!CONN.webhookList || !CONN.baseUrlInput) return;

  CONN.webhookList.innerHTML = '<div style="color:#565f89;font-size:12px;padding:8px 0;">Loading...</div>';

  try {
    const resp = await fetch(apiPath('/admin/webhooks'));
    const data = await resp.json();

    // Base URL
    CONN.baseUrlInput.value = data.webhook_base_url || '';

    const baseUrl = data.webhook_base_url || 'http://localhost:8080';

    // Telegram status
    await loadTelegram(baseUrl);

    // Webhook list
    const hooks = data.webhooks || [];
    if (hooks.length === 0) {
      CONN.webhookList.innerHTML = '<div style="color:#565f89;font-size:12px;padding:8px 0;">No webhooks registered. Use the <code>register_webhook</code> agent tool to create one.</div>';
      return;
    }
    let html = '';
    for (const h of hooks) {
      const url = baseUrl + '/api/v1/webhooks/generic/' + h.id;
      const active = h.active !== false && h.active !== 0;
      html += `
        <div style="background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;padding:10px 12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
            <span style="font-weight:600;color:#c0caf5;font-size:13px;">${escHtml(h.name || 'Unnamed')}</span>
            <span style="font-size:11px;font-weight:500;color:${active ? '#b8bb26' : '#565f89'};">${active ? '● Active' : '○ Disabled'}</span>
          </div>
          <div style="display:flex;align-items:center;gap:4px;background:#16161e;border-radius:4px;padding:4px 6px;margin-bottom:4px;">
            <code style="flex:1;font-size:11px;color:#7dcfff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(url)}</code>
            <button class="conn-copy-btn" data-url="${escHtml(url)}" style="background:none;border:none;color:#565f89;cursor:pointer;font-size:12px;padding:2px 4px;" title="Copy URL">${icon('clipboard', { size: '12px' })}</button>
          </div>
          <div style="font-size:10px;color:#565f89;">
            ID: <code style="color:#a9b1d6;font-size:10px;">${escHtml(h.id)}</code>
            · Created: ${h.created_at ? new Date(h.created_at + 'Z').toLocaleString() : '?'}
          </div>
        </div>
      `;
    }
    CONN.webhookList.innerHTML = html;

    // Wire copy buttons
    CONN.webhookList.querySelectorAll('.conn-copy-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText(btn.dataset.url).then(() => {
          btn.innerHTML = icon('check', { size: '12px' });
          setTimeout(() => { btn.innerHTML = icon('clipboard', { size: '12px' }); }, 1500);
        });
      });
    });

  } catch (e) {
    CONN.webhookList.innerHTML = `<div style="color:#fb4934;font-size:12px;padding:8px 0;">Error loading: ${escHtml(e.message)}</div>`;
  }
}

// ── Save base URL ──

async function saveBaseUrl() {
  if (!CONN.baseUrlInput || !CONN.baseUrlStatus) return;
  const url = CONN.baseUrlInput.value.trim();

  CONN.baseUrlStatus.style.display = 'inline';
  CONN.baseUrlStatus.textContent = 'Saving...';
  CONN.baseUrlStatus.style.color = '#565f89';

  try {
    const resp = await fetch(apiPath('/admin/communications/webhook-url'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await resp.json();
    if (data.status === 'ok') {
      CONN.baseUrlStatus.textContent = '✓ Saved';
      CONN.baseUrlStatus.style.color = '#b8bb26';
      // Reload to update URLs
      await loadConnections();
    } else {
      CONN.baseUrlStatus.textContent = 'Error: ' + (data.message || 'unknown');
      CONN.baseUrlStatus.style.color = '#fb4934';
    }
  } catch (e) {
    CONN.baseUrlStatus.textContent = 'Error: ' + e.message;
    CONN.baseUrlStatus.style.color = '#fb4934';
  }

  setTimeout(() => {
    if (CONN.baseUrlStatus) {
      CONN.baseUrlStatus.style.display = 'none';
    }
  }, 3000);
}

// ── Telegram ──

async function loadTelegram(baseUrl) {
  if (!CONN.telegramStatus) return;

  try {
    const resp = await fetch(apiPath('/admin/communications/plugins'));
    const data = await resp.json();
    const tgPlugin = (data.plugins || []).find(p => p.name === 'telegram');

    if (!tgPlugin) {
      CONN.telegramStatus.innerHTML = '<div style="color:#565f89;">Telegram plugin not found.</div>';
      return;
    }

    const configured = tgPlugin.has_token;
    const enabled = tgPlugin.enabled;
    const webhookUrl = (baseUrl || data.webhook_base_url || 'http://localhost:8080') + '/api/v1/webhooks/telegram';
    const mode = (baseUrl && !baseUrl.includes('localhost') && !baseUrl.includes('127.0.0.1')) ? 'webhook' : 'polling';

    let html = '';

    if (!configured) {
      html = `
        <div style="background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="color:#fb4934;font-weight:600;font-size:13px;">○ Not configured</span>
            <span style="font-size:11px;color:#565f89;">Disabled</span>
          </div>
          <div style="font-size:11px;color:#a9b1d6;line-height:1.5;margin-bottom:10px;">
            <p style="margin:0 0 6px 0;">Talk to <a href="https://t.me/BotFather" target="_blank" style="color:#7dcfff;">@BotFather</a> on Telegram, send <code>/newbot</code>, then paste the token below:</p>
          </div>
          <div style="display:flex;gap:6px;margin-bottom:8px;">
            <input type="text" id="conn-tg-token-input" placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz" autocomplete="off" style="flex:1;padding:8px 10px;background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;color:#c0caf5;font-size:12px;font-family:monospace;">
            <button id="conn-tg-token-save-btn" style="padding:8px 14px;background:#7dcfff;border:none;border-radius:6px;color:#0d0d1a;font-size:12px;font-weight:600;cursor:pointer;">Save</button>
          </div>
          <div id="conn-tg-token-status" style="font-size:11px;color:#565f89;margin-bottom:8px;"></div>
          <div style="font-size:11px;color:#565f89;">After saving the token, you can enable the plugin and set the webhook URL above.</div>
        </div>
      `;
    } else {
      html = `
        <div style="background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
            <span style="color:#b8bb26;font-weight:600;font-size:13px;">● ${enabled ? 'Connected' : 'Disabled'}</span>
            <span style="font-size:11px;color:#565f89;">${mode === 'polling' ? icon('refresh-cw', { size: '11px' }) + ' Polling' : icon('radio', { size: '11px' }) + ' Webhook'} · Token configured</span>
          </div>
          <div style="display:flex;align-items:center;gap:4px;background:#16161e;border-radius:4px;padding:4px 6px;margin-bottom:6px;">
            <code style="flex:1;font-size:11px;color:#7dcfff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escHtml(webhookUrl)}</code>
            <button class="conn-copy-btn" data-url="${escHtml(webhookUrl)}" style="background:none;border:none;color:#565f89;cursor:pointer;font-size:12px;padding:2px 4px;" title="Copy URL">${icon('clipboard', { size: '12px' })}</button>
          </div>
          <div style="display:flex;gap:8px;margin-top:6px;">
            <button id="conn-tg-enable-btn" class="tg-toggle-btn" data-enable="${enabled ? 'false' : 'true'}" style="padding:5px 14px;${enabled ? 'background:transparent;border:1px solid #565f89;color:#565f89' : 'background:#7dcfff;border:none;color:#0d0d1a'};border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;">
              ${enabled ? icon('pause', { size: '13px' }) + ' Disable' : icon('play', { size: '13px' }) + ' Enable'}
            </button>
          </div>
          <details style="margin-top:8px;font-size:11px;">
            <summary style="cursor:pointer;color:#565f89;font-size:11px;">Change token</summary>
            <div style="margin-top:6px;display:flex;gap:6px;">
              <input type="text" id="conn-tg-token-input" placeholder="New token from @BotFather" autocomplete="off" style="flex:1;padding:8px 10px;background:#0d0d1a;border:1px solid #2a2a4a;border-radius:6px;color:#c0caf5;font-size:12px;font-family:monospace;">
              <button id="conn-tg-token-save-btn" style="padding:8px 14px;background:#7dcfff;border:none;border-radius:6px;color:#0d0d1a;font-size:12px;font-weight:600;cursor:pointer;">Save</button>
            </div>
            <div id="conn-tg-token-status" style="font-size:11px;color:#565f89;margin-top:4px;"></div>
          </details>
        </div>
      `;
    }

    CONN.telegramStatus.innerHTML = html;

    // Wire enable/disable button
    const toggleBtn = document.getElementById('conn-tg-enable-btn');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', async () => {
        const shouldEnable = toggleBtn.dataset.enable === 'true';
        toggleBtn.disabled = true;
        toggleBtn.textContent = '...';
        try {
          const endpoint = shouldEnable ? 'enable' : 'disable';
          const resp = await fetch(apiPath('/admin/communications/plugins/telegram/' + endpoint), { method: 'POST' });
          const result = await resp.json();
          if (result.status === 'ok') {
            // Reload Telegram section
            await loadTelegram(baseUrl);
          } else {
            alert('Failed: ' + (result.message || 'unknown'));
          }
        } catch (e) {
          alert('Error: ' + e.message);
        }
      });
    }

    // Wire token save button
    const tokenInput = document.getElementById('conn-tg-token-input');
    const tokenSaveBtn = document.getElementById('conn-tg-token-save-btn');
    const tokenStatus = document.getElementById('conn-tg-token-status');

    async function saveToken() {
      if (!tokenInput || !tokenStatus) return;
      const token = tokenInput.value.trim();
      if (!token) {
        tokenStatus.textContent = 'Please paste the bot token from @BotFather.';
        tokenStatus.style.color = '#fb4934';
        return;
      }
      tokenStatus.textContent = 'Saving...';
      tokenStatus.style.color = '#565f89';
      try {
        const resp = await fetch(apiPath('/admin/communications/plugins/telegram/token'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: token }),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
          let mode = result.mode || 'webhook';
          let modeLabel = mode === 'polling' ? 'Polling' : 'Webhook';
          let msg = `✓ Token saved! Plugin enabled (${modeLabel} ${mode}).`;
          if (mode === 'webhook' && result.webhook_registered) {
            msg += ' Webhook registered.';
          } else if (mode === 'webhook' && !result.webhook_registered) {
            msg += ' Webhook registration failed.';
          }
          tokenStatus.textContent = msg;
          tokenStatus.style.color = '#b8bb26';
          // Reload the whole section to reflect new state
          await loadTelegram(baseUrl);
        } else {
          tokenStatus.textContent = 'Error: ' + (result.message || 'unknown');
          tokenStatus.style.color = '#fb4934';
        }
      } catch (e) {
        tokenStatus.textContent = 'Error: ' + e.message;
        tokenStatus.style.color = '#fb4934';
      }
    }

    if (tokenSaveBtn) {
      tokenSaveBtn.addEventListener('click', saveToken);
    }
    if (tokenInput) {
      tokenInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') saveToken();
      });
    }

    // Wire copy buttons
    CONN.telegramStatus.querySelectorAll('.conn-copy-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        navigator.clipboard.writeText(btn.dataset.url).then(() => {
          btn.innerHTML = icon('check', { size: '12px' });
          setTimeout(() => { btn.innerHTML = icon('clipboard', { size: '12px' }); }, 1500);
        });
      });
    });

  } catch (e) {
    CONN.telegramStatus.innerHTML = `<div style="color:#fb4934;font-size:12px;padding:8px 0;">Error: ${escHtml(e.message)}</div>`;
  }
}


// ── Init ──

export function initConnections() {
  bindDom();
  if (!CONN.menuItem || !CONN.modal) return;

  // Menu item click → close dropdown → open modal (admin only)
  CONN.menuItem.addEventListener('click', () => {
    if (CONN.dropdown) CONN.dropdown.style.display = 'none';
    if (!isAdmin()) {
      showRestrictedModal();
      return;
    }
    openConnections();
  });

  // Close handlers
  if (CONN.backdrop) CONN.backdrop.addEventListener('click', closeConnections);
  if (CONN.close) CONN.close.addEventListener('click', closeConnections);
  if (CONN.closeBtn) CONN.closeBtn.addEventListener('click', closeConnections);

  // Base URL save
  if (CONN.baseUrlSave) {
    CONN.baseUrlSave.addEventListener('click', saveBaseUrl);
  }
  if (CONN.baseUrlInput) {
    CONN.baseUrlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') saveBaseUrl();
    });
  }
}

// ── Helper ──

function escHtml(s) {
  if (s == null) return '';
  const div = document.createElement('div');
  div.textContent = String(s);
  return div.innerHTML;
}
