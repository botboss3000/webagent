'use strict';

import { app } from '../state.js';
import { apiPath } from '../config.js';
import { getAuthToken, authUrl } from '../left-login.js';

let queryTable = () => {};
let startAutoRefresh = () => {};

export function setTableDeps(deps) {
  queryTable = deps.queryTable;
  startAutoRefresh = deps.startAutoRefresh;
}

export async function fetchTables(dbName) {
  if (!getAuthToken()) return;

  try {
    const url = authUrl(apiPath(`/api/v1/db/tables?db=${encodeURIComponent(dbName)}`));
    const res = await fetch(url);
    if (res.status === 401) {
      localStorage.removeItem('auth_token');
      window.location.reload();
      return;
    }
    const data = await res.json();
    app.dbTables = data.tables || [];
    renderTableList();
  } catch (e) {
    document.getElementById('db-table-list').innerHTML =
      '<div class="db-hint">Error loading tables</div>';
  }
}

export async function updateTableCounts() {
  const token = getAuthToken();
  if (!token) return;

  const dbName = document.getElementById('db-select').value;
  try {
    const url = authUrl(apiPath(`/api/v1/db/tables?db=${encodeURIComponent(dbName)}`));
    const res = await fetch(url);
    if (res.status === 401) return;
    const data = await res.json();
    if (!data.tables) return;
    for (const fresh of data.tables) {
      const existing = app.dbTables.find((t) => t.name === fresh.name);
      if (existing) existing.row_count = fresh.row_count;
    }
    document.querySelectorAll('.db-table-item').forEach((item) => {
      const name = item.dataset.table;
      const t = app.dbTables.find((x) => x.name === name);
      if (t) {
        const countSpan = item.querySelector('.count');
        if (countSpan) countSpan.textContent = t.row_count;
      }
    });
  } catch (e) {
    /* ignore */
  }
}

export function renderTableList() {
  const el = document.getElementById('db-table-list');
  if (!app.dbTables.length) {
    el.innerHTML = '<div class="db-hint">No tables found</div>';
    return;
  }
  el.innerHTML = app.dbTables
    .map(
      (t) => {
        const isBold = ['interactions', 'context_templates', 'agent_templates'].includes(t.name);
        const fontStyle = isBold ? 'font-weight: bold;' : '';
        return `<div class="db-table-item${app.dbSelectedTable === t.name ? ' active' : ''}" data-table="${t.name}">
      <span style="${fontStyle}">${t.name}</span>
      <span class="count">${t.row_count}</span>
      <button class="db-table-reset-btn" data-table="${t.name}" title="Delete all rows">🗑️</button>
    </div>`;
      }
    )
    .join('');
  el.querySelectorAll('.db-table-item').forEach((item) => {
    item.addEventListener('click', (e) => {
      if (e.target.closest('.db-table-reset-btn')) return;
      app.dbSelectedTable = item.dataset.table;
      localStorage.setItem('lastDbTable', item.dataset.table);
      renderTableList();
      
      // Load user layout/settings before requerying DB
      try {
        const hidden = localStorage.getItem('dbHiddenCols');
        if (hidden) app.dbHiddenCols = JSON.parse(hidden);
        else app.dbHiddenCols = {};
      } catch (e) { app.dbHiddenCols = {}; }
      
      queryTable(app.dbSelectedTable).then(() => startAutoRefresh());
    });
  });
  el.querySelectorAll('.db-table-reset-btn').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const table = btn.dataset.table;
      try {
        const dbName = document.getElementById('db-select').value;
        const res = await fetch(
          authUrl(apiPath(
            '/api/v1/db/truncate?db=' +
              encodeURIComponent(dbName) +
              '&table=' +
              encodeURIComponent(table),
          )),
          { method: 'DELETE' },
        );
        const result = await res.json();
        if (result.success) {
          await fetchTables(dbName);
          if (app.dbSelectedTable === table) {
            app.dbSelectedTable = null;
            app.dbCurrentResult = null;
            document.getElementById('db-table-data').innerHTML =
              '<div class="db-hint">Table cleared</div>';
            if (typeof app.stopAutoRefresh === 'function') app.stopAutoRefresh();
          }
        } else {
          alert('Failed to clear table');
        }
      } catch (err) {
        alert('Error: ' + err.message);
      }
    });
  });
}
