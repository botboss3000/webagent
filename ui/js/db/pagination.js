'use strict';

import { app } from '../state.js';
import { queryTable, updatePageInfo, cancelEditing } from './query-render.js';
import { fetchTables, updateTableCounts, renderTableList } from './tables.js';

const AUTO_REFRESH_MS = 5000;

export function stopAutoRefresh() {
  if (app.autoRefreshInterval) {
    clearInterval(app.autoRefreshInterval);
    app.autoRefreshInterval = null;
  }
  document.getElementById('db-auto-status').textContent = '';
}

export function startAutoRefresh() {
  stopAutoRefresh();
  if (!app.dbSelectedTable) return;
  document.getElementById('db-auto-status').textContent = '⟳ auto 5s';
  app.autoRefreshInterval = setInterval(() => {
    if (app.dbSelectedTable && !app.editingCell) {
      queryTable(app.dbSelectedTable, { silent: true });
    }
    updateTableCounts();
  }, AUTO_REFRESH_MS);
}

export function restartAutoRefresh() {
  if (app.dbSelectedTable) {
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
}

export function initDbPaginationAndToolbar() {
  app.stopAutoRefresh = stopAutoRefresh;

  const prevBtn = document.getElementById('db-page-prev');
  const nextBtn = document.getElementById('db-page-next');
  if (prevBtn) {
    prevBtn.addEventListener('click', () => {
      if (app.dbPageOffset <= 0) return;
      app.dbPageOffset = Math.max(0, app.dbPageOffset - app.dbPageLimit);
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable, { keepOffset: true });
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', () => {
      if (app.dbPageOffset + app.dbPageLimit >= app.dbTotalRows) return;
      app.dbPageOffset += app.dbPageLimit;
      if (app.dbSelectedTable) queryTable(app.dbSelectedTable, { keepOffset: true });
    });
  }

  // Reset DB button — two-click confirm (tables wiped except templates)
  const resetBtn = document.getElementById('db-reset');
  let resetPending = false;
  let resetTimer = null;

  resetBtn.addEventListener('click', async () => {
    if (!resetPending) {
      resetPending = true;
      resetBtn.textContent = 'PERMANENT RESET?';
      resetBtn.style.background = '#9d0006';
      resetBtn.style.borderColor = '#cc241d';
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        resetPending = false;
        resetBtn.textContent = 'Reset DB';
        resetBtn.style.background = '#cc241d';
        resetBtn.style.borderColor = '#fb4934';
      }, 4000);
      return;
    }
    // Second click — execute
    resetPending = false;
    clearTimeout(resetTimer);
    resetBtn.textContent = 'Resetting...';
    resetBtn.style.background = '#cc241d';
    resetBtn.style.borderColor = '#fb4934';
    try {
      const dbName = document.getElementById('db-select').value;
      const res = await fetch('/api/v1/db/reset?db=' + encodeURIComponent(dbName), { method: 'DELETE' });
      const result = await res.json();
      if (result.success) {
        // Reload tables and refresh view
        const { fetchTables } = await import('./tables.js');
        resetBtn.textContent = '✓ Reset';
        resetBtn.style.background = '#689d6a';
        resetBtn.style.borderColor = '#8ec07c';
        setTimeout(() => {
          resetBtn.textContent = 'Reset DB';
          resetBtn.style.background = '#cc241d';
          resetBtn.style.borderColor = '#fb4934';
        }, 2000);
        app.dbSelectedTable = null;
        app.dbCurrentResult = null;
        cancelEditing();
        stopAutoRefresh();
        await fetchTables(dbName);
        document.getElementById('db-table-data').innerHTML =
          '<div class="db-hint">Database reset (templates preserved)</div>';
      } else {
        resetBtn.textContent = 'Reset DB';
        alert('Reset failed');
      }
    } catch (err) {
      resetBtn.textContent = 'Reset DB';
      alert('Error: ' + err.message);
    }
  });

  document.getElementById('db-refresh').addEventListener('click', () => {
    const dbName = document.getElementById('db-select').value;
    app.dbSelectedTable = null;
    app.dbCurrentResult = null;
    cancelEditing();
    stopAutoRefresh();
    fetchTables(dbName);
    document.getElementById('db-table-data').innerHTML =
      '<div class="db-hint">Select a table to view its contents</div>';
  });
  document.getElementById('db-select').addEventListener('change', () => {
    document.getElementById('db-refresh').click();
  });
  document.getElementById('db-sort-col').addEventListener('change', () => {
    app.dbPageOffset = 0;
    if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
  });
  document.getElementById('db-sort-dir').addEventListener('change', () => {
    app.dbPageOffset = 0;
    if (app.dbSelectedTable) queryTable(app.dbSelectedTable);
  });
}

export function runInitialDbTableLoad() {
  fetchTables('local.db').then(() => {
    // Try saved table, fall back to 'interactions', else nothing
    const savedTable = localStorage.getItem('lastDbTable');
    const tableToLoad =
      savedTable && app.dbTables.some((t) => t.name === savedTable)
        ? savedTable
        : app.dbTables.some((t) => t.name === 'interactions')
          ? 'interactions'
          : null;
    if (tableToLoad) {
      app.dbSelectedTable = tableToLoad;
      renderTableList();
      document.getElementById('db-sort-col').value = 'created_at';
      document.getElementById('db-sort-dir').value = 'DESC';
      queryTable(tableToLoad).then(() => startAutoRefresh());
    }
  });
}
