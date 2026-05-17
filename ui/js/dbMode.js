'use strict';

/**
 * Database mode modal.
 * Opens from Config → Database, shows Cloud/Local toggle with status info.
 */

import { app } from './state.js';
import { CONTROL_MODE_KEY, apiPath } from './config.js';
import { isAdmin, showRestrictedModal } from './left-login.js';
import { icon } from './icons.js';

// ── DOM refs ──
let DB = {};

function qs(id) { return document.getElementById(id); }

function bindDom() {
  DB = {
    menuItem: qs('database-menu-item'),
    viewSettingsBtn: qs('db-settings-btn'),
    modal: qs('database-modal'),
    backdrop: qs('database-backdrop'),
    close: qs('database-close'),
    closeBtn: qs('database-close-btn'),
    segCloud: qs('modal-seg-cloud'),
    segLocal: qs('modal-seg-local'),
    modeLabel: qs('database-current-mode'),
    statusEl: qs('database-modal-status'),
    dropdown: qs('settings-dropdown-menu'),
    showHiddenCheckbox: qs('db-setting-show-hidden'),
  };
}

export async function fetchDbMode() {
  try {
    const res = await fetch(apiPath('/admin/db/mode'));
    if (res.ok) {
      const data = await res.json();
      return data.mode;
    }
  } catch (e) {
    /* server not up yet */
  }
  return null;
}

export async function setDbMode(mode) {
  try {
    const res = await fetch(apiPath('/admin/db/mode'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (res.ok) {
      const data = await res.json();
      const s = data.status || {};
      const tableCount = s.tables
        ? Object.values(s.tables).reduce((a, b) => a + (b > 0 ? b : 0), 0)
        : 0;
      // Also update the old tStat if present
      if (app.tStat) {
        app.tStat.textContent = `Mode: ${data.status.mode} (${tableCount} records)`;
      }
      applyMode(mode);
      updateModeLabel(mode);
      return true;
    }
  } catch (e) {
    console.error('Failed to switch mode:', e);
  }
  return false;
}

function applyMode(mode) {
  const isLocal = mode === 'local';
  [DB.segCloud, DB.segLocal].forEach(b => {
    if (!b) return;
    b.style.borderColor = '#2a2a4a';
    b.style.background = '#0d0d1a';
    b.style.color = '#c0caf5';
    b.style.boxShadow = 'none';
  });
  const active = isLocal ? DB.segLocal : DB.segCloud;
  if (active) {
    active.style.borderColor = '#7dcfff';
    active.style.background = '#7dcfff10';
    active.style.color = '#7dcfff';
    active.style.boxShadow = '0 0 8px rgba(125, 207, 255, 0.2)';
  }
}

function updateModeLabel(mode) {
  if (DB.modeLabel) {
    const modeIcon = mode === 'local' ? icon('monitor', { size: '13px' }) : icon('cloud', { size: '13px' });
    const modeText = mode === 'local' ? 'Local (SQLite)' : 'Cloud (Supabase)';
    DB.modeLabel.innerHTML = `${modeIcon} ${modeText}`;
  }
}

// ── Open / Close ──

function openModal() {
  if (DB.modal) {
    DB.modal.style.display = 'block';
    loadAndDisplay();
  }
}

function closeModal() {
  if (DB.modal) DB.modal.style.display = 'none';
}

async function loadAndDisplay() {
  if (!DB.modeLabel) return;

  if (DB.statusEl) DB.statusEl.textContent = '';

  const mode = await fetchDbMode();
  if (mode) {
    updateModeLabel(mode);
    applyMode(mode);
    localStorage.setItem(CONTROL_MODE_KEY, mode);
  } else {
    const saved = localStorage.getItem(CONTROL_MODE_KEY);
    const fallback = saved === 'cloud' ? 'cloud' : 'local';
    updateModeLabel(fallback);
    applyMode(fallback);
  }
}

// ── Init ──

export function initDbModeUi() {
  bindDom();
  if (!DB.menuItem || !DB.modal) return;

  // View settings button click
  if (DB.viewSettingsBtn) {
    DB.viewSettingsBtn.addEventListener('click', () => {
      openModal();
    });
  }

  // Checkbox sync and toggle logic
  if (DB.showHiddenCheckbox) {
    DB.showHiddenCheckbox.checked = app.dbShowHidden;
    DB.showHiddenCheckbox.addEventListener('change', async (e) => {
      const isChecked = e.target.checked;
      app.dbShowHidden = isChecked;
      localStorage.setItem('dbShowHidden', isChecked ? 'true' : 'false');
      
      // Force UI reload so headers/columns rebuild
      if (app.dbSelectedTable) {
        const { queryTable, startAutoRefresh } = await import('./db/query-render.js');
        queryTable(app.dbSelectedTable).then(() => {
          // If auto refresh was doing things, optional hook here
        });
      }
    });
  }

  // Menu item → close dropdown → open modal (admin only)
  DB.menuItem.addEventListener('click', () => {
    if (DB.dropdown) DB.dropdown.style.display = 'none';
    if (!isAdmin()) {
      showRestrictedModal();
      return;
    }
    openModal();
  });

  // Close handlers
  if (DB.backdrop) DB.backdrop.addEventListener('click', closeModal);
  if (DB.close) DB.close.addEventListener('click', closeModal);
  if (DB.closeBtn) DB.closeBtn.addEventListener('click', closeModal);

  // Cloud click
  if (DB.segCloud) {
    DB.segCloud.addEventListener('click', async () => {
      DB.segCloud.disabled = true;
      DB.segLocal.disabled = true;
      if (DB.statusEl) DB.statusEl.textContent = 'Switching to Cloud...';
      const ok = await setDbMode('cloud');
      localStorage.setItem(CONTROL_MODE_KEY, 'cloud');
      if (DB.statusEl) DB.statusEl.innerHTML = ok ? `${icon('check-circle', { size: '13px' })} Switched to Cloud (Supabase)` : `${icon('x-circle', { size: '13px' })} Failed to switch`;
      DB.segCloud.disabled = false;
      DB.segLocal.disabled = false;
    });
  }

  // Local click
  if (DB.segLocal) {
    DB.segLocal.addEventListener('click', async () => {
      DB.segCloud.disabled = true;
      DB.segLocal.disabled = true;
      if (DB.statusEl) DB.statusEl.textContent = 'Switching to Local...';
      const ok = await setDbMode('local');
      localStorage.setItem(CONTROL_MODE_KEY, 'local');
      if (DB.statusEl) DB.statusEl.innerHTML = ok ? `${icon('check-circle', { size: '13px' })} Switched to Local (SQLite)` : `${icon('x-circle', { size: '13px' })} Failed to switch`;
      DB.segCloud.disabled = false;
      DB.segLocal.disabled = false;
    });
  }
}
