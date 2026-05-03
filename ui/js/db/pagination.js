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
  fetchTables('local_webagent.db').then(() => {
    if (app.dbTables.some((t) => t.name === 'interactions')) {
      app.dbSelectedTable = 'interactions';
      renderTableList();
      document.getElementById('db-sort-col').value = 'created_at';
      document.getElementById('db-sort-dir').value = 'DESC';
      queryTable('interactions').then(() => startAutoRefresh());
    }
  });
}
